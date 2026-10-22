"""Wallex REST adapter (spot only).

Endpoints verified against the official docs at developers.wallex.ir (2026-10-22):
  GET    /v1/markets                    symbols with stepSize/tickSize precision, minQty, minNotional
  GET    /hector/web/v1/markets         is_spot / is_tmn_based flags
  GET    /v1/udf/history                OHLCV arrays {s,t,o,h,l,c,v}; resolution 1,15,60,240,...
  GET    /v1/depth?symbol=              {ask:[{price,quantity,sum}], bid:[...]}
  GET    /v1/account/balances           (x-api-key)
  GET    /v1/account/fee                makerFeeRate/takerFeeRate per market
  POST   /v1/account/orders             LIMIT|MARKET|STOP_LIMIT|STOP_MARKET; client_id [A-Z0-9_], must be unique
  GET    /v1/account/orders/{client_id}
  DELETE /v1/account/orders/{client_id}
  GET    /v1/account/openOrders
Trade-credit (competition) sub-accounts use their own API key AND the
`sub-account-client-id` header. Order POST limit: 20 requests / 10 s.
Response-shape details below that the docs leave open are marked UNVERIFIED and
must be confirmed with `wtrader probe` against the real account.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .models import (AmbiguousSubmission, AuthError, Balance, Book, Candle, DefinitiveRejection, Market,
                     OrderIntent, OrderObservation, OrderState, RateLimited, TransientError, ZERO, dec)

log = logging.getLogger(__name__)
CLIENT_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
QUOTE_ALIASES = {"TMN": "TMN", "IRT": "TMN"}
STATUS_MAP = {
    "NEW": OrderState.OPEN, "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED, "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED, "CANCELLED": OrderState.CANCELED, "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.CANCELED,
}


def precision_step(p) -> Decimal:
    """Wallex reports precision as a count of decimals (e.g. 5 -> 0.00001)."""
    return Decimal(1).scaleb(-int(p))


class Throttle:
    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls, self.per = max_calls, per_seconds
        self.calls: list[float] = []
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            self.calls = [t for t in self.calls if now - t < self.per]
            if len(self.calls) >= self.max_calls:
                time.sleep(self.per - (now - self.calls[0]) + 0.01)
            self.calls.append(time.monotonic())


class WallexClient:
    def __init__(self, api_key: str | None = None, sub_account_id: str | None = None,
                 base_url: str = "https://api.wallex.ir", timeout: float = 6, read_attempts: int = 3,
                 http: httpx.Client | None = None):
        self._key = api_key
        self._sub = sub_account_id
        self.base_url = base_url.rstrip("/")
        self.read_attempts = read_attempts
        self.http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "wtrader/1.0"})
        self.order_throttle = Throttle(18, 10)     # documented 20/10s, keep headroom
        self.read_throttle = Throttle(30, 10)

    # ------------------------------------------------------------------ transport
    def _headers(self, auth: bool) -> dict:
        if not auth:
            return {}
        if not self._key:
            raise AuthError("WALLEX_API_KEY not configured")
        h = {"x-api-key": self._key}
        if self._sub:
            h["sub-account-client-id"] = self._sub
        return h

    def _get(self, path: str, params: dict | None = None, auth: bool = False):
        last: Exception | None = None
        for attempt in range(self.read_attempts):
            self.read_throttle.wait()
            try:
                r = self.http.get(self.base_url + path, params=params, headers=self._headers(auth))
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code in (401, 403):
                    raise AuthError(f"GET {path}: HTTP {r.status_code}")
                if r.status_code == 429:
                    ra = r.headers.get("retry-after")
                    last = RateLimited(f"GET {path}: 429", float(ra) if ra and ra.isdigit() else None)
                    time.sleep(min(10.0, last.retry_after or 2.0))
                    continue
                if r.status_code == 404:
                    return None
                if r.status_code >= 500:
                    last = TransientError(f"GET {path}: HTTP {r.status_code}")
                else:
                    body = r.json()
                    if r.status_code >= 400:
                        raise DefinitiveRejection(f"GET {path}: HTTP {r.status_code} {str(body)[:200]}")
                    return body
            time.sleep(min(4.0, 0.4 * 2 ** attempt + random.random() * 0.3))
        raise TransientError(f"GET {path} failed after {self.read_attempts} attempts: {type(last).__name__}")

    # ------------------------------------------------------------------ public data
    def markets(self) -> dict[str, Market]:
        body = self._get("/v1/markets") or {}
        out = {}
        for raw, m in ((body.get("result") or {}).get("symbols") or {}).items():
            quote = QUOTE_ALIASES.get(str(m.get("quoteAsset", "")).upper())
            base = str(m.get("baseAsset", "")).upper()
            if quote != "TMN" or not base:
                continue
            try:
                mk = Market(symbol=base + "TMN", raw_symbol=raw, base=base, quote="TMN",
                            tick=precision_step(m["tickSize"]), step=precision_step(m["stepSize"]),
                            min_quantity=dec(m.get("minQty") or "0"), min_notional=dec(m.get("minNotional") or "0"))
            except (KeyError, ValueError):
                log.warning("skipping market %s with incomplete metadata", raw)
                continue
            out[mk.symbol] = mk
        return out

    def spot_flags(self) -> dict[str, bool]:
        body = self._get("/hector/web/v1/markets") or {}
        return {str(m.get("symbol", "")).upper(): bool(m.get("is_spot"))
                for m in ((body.get("result") or {}).get("markets") or [])}

    def candles(self, symbol: str, seconds: int, start: int, end: int) -> list[Candle]:
        res = {900: "15", 3600: "60", 14400: "240"}[seconds]
        body = self._get("/v1/udf/history", {"symbol": symbol, "resolution": res, "from": start, "to": end}) or {}
        if body.get("s") != "ok":
            return []
        out = []
        for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
            t = int(t)
            if t % seconds:
                continue
            out.append(Candle(t, dec(o), dec(h), dec(l), dec(c), dec(v)))
        return sorted(out, key=lambda c: c.open_time)

    def book(self, symbol: str) -> Book:
        body = self._get("/v1/depth", {"symbol": symbol}) or {}
        res = body.get("result") or {}

        def side(rows, reverse):
            lv = [(dec(r["price"]), dec(r["quantity"])) for r in rows or [] if dec(r["quantity"]) > 0]
            return tuple(sorted(lv, key=lambda x: x[0], reverse=reverse))

        return Book(symbol, side(res.get("bid"), True), side(res.get("ask"), False), datetime.now(timezone.utc))

    # ------------------------------------------------------------------ account
    def balances(self) -> dict[str, Balance]:
        body = self._get("/v1/account/balances", auth=True) or {}
        out = {}
        for cur, b in ((body.get("result") or {}).get("balances") or {}).items():
            cur = QUOTE_ALIASES.get(cur.upper(), cur.upper())
            value, locked = dec(b.get("value") or "0"), dec(b.get("locked") or "0")
            # UNVERIFIED: python-wallex treats `value` as total including `locked`.
            out[cur] = Balance(cur, max(ZERO, value - locked), locked)
        return out

    def taker_fees(self) -> dict[str, Decimal]:
        body = self._get("/v1/account/fee", auth=True) or {}
        out = {}
        for sym, f in (body.get("result") or {}).items():
            if isinstance(f, dict) and f.get("takerFeeRate") is not None:
                rate = dec(f["takerFeeRate"])
                # UNVERIFIED unit: treat values > 0.05 as percent (0.25 -> 0.0025)
                out[sym.upper()] = rate / 100 if rate > D05 else rate
        return out

    # ------------------------------------------------------------------ orders
    @staticmethod
    def observation(o: dict) -> OrderObservation:
        status = str(o.get("status", "")).upper()
        executed = dec(o.get("executedQty") or "0")
        state = STATUS_MAP.get(status)
        if state is None:
            orig = dec(o.get("origQty") or "0")
            # FINISHED / undocumented: decide from execution, never guess "filled" without quantity
            state = OrderState.FILLED if orig > 0 and executed >= orig else (
                OrderState.CANCELED if o.get("active") is False else OrderState.UNKNOWN)
        if state == OrderState.OPEN and executed > 0:
            state = OrderState.PARTIALLY_FILLED
        fee_base = fee_quote = ZERO
        base_asset = str(o.get("symbol", ""))[:-3]
        for f in o.get("fills") or []:
            fee = dec(f.get("fee") or "0")
            asset = QUOTE_ALIASES.get(str(f.get("feeAsset", "")).upper(), str(f.get("feeAsset", "")).upper())
            if asset == "TMN":
                fee_quote += fee
            elif asset == base_asset or not asset:
                fee_base += fee
            else:
                fee_quote += ZERO   # foreign fee asset: recorded raw, not deducted here
        return OrderObservation(client_id=str(o.get("clientOrderId", "")), state=state, executed_qty=executed,
                                executed_quote=dec(o.get("executedSum") or "0"), fee_base=fee_base,
                                fee_quote=fee_quote, raw=o)

    def submit(self, intent: OrderIntent, market: Market) -> OrderObservation:
        if not CLIENT_ID_RE.match(intent.client_id):
            raise DefinitiveRejection("client_id violates Wallex charset A-Z 0-9 _")
        body = {"symbol": market.raw_symbol, "side": intent.side, "type": "LIMIT",
                "price": format(intent.limit_price.normalize(), "f"),
                "quantity": format(intent.quantity.normalize(), "f"), "client_id": intent.client_id}
        self.order_throttle.wait()
        try:
            r = self.http.post(self.base_url + "/v1/account/orders", json=body, headers=self._headers(True))
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            raise AmbiguousSubmission(f"POST order {intent.client_id}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise AuthError(f"POST order: HTTP {r.status_code}")
        if r.status_code == 429:
            # rejected before processing per HTTP semantics; still reconcile defensively in the executor
            raise RateLimited("POST order: 429")
        if r.status_code >= 500:
            raise AmbiguousSubmission(f"POST order {intent.client_id}: HTTP {r.status_code}")
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise AmbiguousSubmission(f"POST order {intent.client_id}: unparseable body") from e
        if 400 <= r.status_code < 500 or not payload.get("success", r.status_code < 300):
            raise DefinitiveRejection(f"POST order rejected HTTP {r.status_code}: {str(payload.get('message'))[:200]}")
        result = payload.get("result") or {}
        if str(result.get("clientOrderId", "")).upper() != intent.client_id:
            raise AmbiguousSubmission("accepted response missing matching clientOrderId")
        return self.observation(result)

    def lookup(self, client_id: str) -> OrderObservation | None:
        """None means 'not found now' — NOT proof that the order never existed."""
        body = self._get(f"/v1/account/orders/{client_id}", auth=True)
        if not body or not body.get("result"):
            return None
        return self.observation(body["result"])

    def open_orders(self) -> list[OrderObservation]:
        body = self._get("/v1/account/openOrders", {"per_page": 100}, auth=True) or {}
        res = body.get("result") or {}
        rows = res.get("orders") if isinstance(res, dict) and "orders" in res else \
            [o for v in (res.values() if isinstance(res, dict) else []) if isinstance(v, list) for o in v]
        return [self.observation(o) for o in rows or []]

    def cancel(self, client_id: str) -> OrderObservation | None:
        self.order_throttle.wait()
        try:
            r = self.http.delete(f"{self.base_url}/v1/account/orders/{client_id}", headers=self._headers(True))
        except httpx.TransportError as e:
            raise AmbiguousSubmission(f"DELETE {client_id}: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise AmbiguousSubmission(f"DELETE {client_id}: HTTP {r.status_code}")
        try:
            payload = r.json()
        except json.JSONDecodeError:
            return None
        res = payload.get("result")
        return self.observation(res) if isinstance(res, dict) and res.get("clientOrderId") else None


D05 = Decimal("0.05")


def payload_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]
