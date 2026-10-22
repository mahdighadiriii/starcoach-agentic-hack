"""Wallex REST adapter (spot only).

Endpoints verified against the official docs and swagger files at developers.wallex.ir (2026-10-22):
  GET    /hector/web/v1/markets         symbol, base/quote, is_spot, amount_precision/price_precision (documented)
  GET    /v1/markets                    UNDOCUMENTED: used only to enrich minQty/minNotional; never required
  GET    /v1/udf/history                OHLCV arrays {s,t,o,h,l,c,v}; resolution 1,15,60,240,480,720,1D...
  GET    /v1/depth?symbol=              {ask:[{price,quantity,sum}], bid:[...]}; docs warn about rate limits
  GET    /v1/account/balances           main account wallet: value/locked per asset (x-api-key)
  GET    /v1/account/balances-detail    trade-credit sub-account wallet: total/freeze/available (+ sub-account-client-id)
  GET    /v1/account/fee                makerFeeRate/takerFeeRate per market
  POST   /v1/account/orders             LIMIT|MARKET|STOP_LIMIT|STOP_MARKET; client_id [A-Z0-9_], must be unique; 201
  GET    /v1/account/orders/{client_id} order detail (404 = not found)
  DELETE /v1/account/orders/{client_id} cancel (only while not FINISHED)
  GET    /v1/account/openOrders         active orders, result = {key: [OrderResource]}
  GET    /v1/account/orders             paginated order history (per_page max 100)
  GET    /sub-accounts                  trade-credit accounts: clientId, isActive, isSuspend
  GET    /v1/prop/user/packages/active  trade-credit package: loan, warningLimit, liquidityLimit, connectToWallet
Trade-credit (competition) sub-accounts use their own API key AND the `sub-account-client-id` header.
The only documented rate limit is order POST: 20 requests / 10 s. Depth/trades polling is discouraged
(rate limits) in favour of the WebSocket channels (see wallex_ws.py).
Response-shape details the docs leave open are marked UNVERIFIED and must be confirmed with
`wtrader probe` against the real account.

Safety: a client is created with trading disabled; submit/cancel refuse locally (nothing is sent)
until the live CLI path calls enable_trading() after every gate has passed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from .config import redact
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
NETWORK_HINT = ("connection to Wallex did not complete. Known cause: the host's egress (VPN/tunnel/cloud) is outside "
                "Iran and api.wallex.ir never completes the TLS handshake. Run `python -m wtrader probe` for a step-by-step "
                "diagnosis. Fixes: exclude api.wallex.ir from the VPN (split tunnel), set WALLEX_BIND_INTERFACE to the "
                "physical interface with an Iranian egress (e.g. en0), or set WALLEX_HTTP_PROXY.")
IP_BOUND_IF = 25        # macOS <netinet/in.h>: bind a socket to an interface index (applied before connect)
SO_BINDTODEVICE = 25    # Linux <asm-generic/socket.h> (needs CAP_NET_RAW)


def interface_address(name: str) -> str:
    """IPv4 address of a local network interface, e.g. en0 -> 172.20.10.7. Raises if it has none."""
    if sys.platform == "darwin":
        out = subprocess.run(["ipconfig", "getifaddr", name], capture_output=True, text=True, timeout=5).stdout.strip()
    else:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", name], capture_output=True, text=True,
                             timeout=5).stdout.split()
        out = next((w.split("/")[0] for i, w in enumerate(out) if i and out[i - 1] == "inet"), "")
    if not out:
        raise OSError(f"network interface {name!r} has no IPv4 address (is it up?)")
    return out


def bind_sockopts(name: str) -> tuple:
    """setsockopt tuples that pin a socket to `name` before connect (used for the WebSocket)."""
    if sys.platform == "darwin":
        return ((socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(name)),)
    return ((socket.SOL_SOCKET, SO_BINDTODEVICE, name.encode()),)


class TradingDisabled(DefinitiveRejection):
    """Refused locally: this client was never enabled for real orders. Nothing was sent."""


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
                 http: httpx.Client | None = None, connect_timeout: float = 4, proxy: str | None = None,
                 circuit_failures: int = 3, circuit_cooldown: float = 30, bind_interface: str | None = None,
                 resolve_interface=interface_address):
        if bind_interface and proxy:
            raise ValueError("choose interface binding or an explicit proxy, not both")
        self._key = api_key
        self._sub = sub_account_id
        self.base_url = base_url.rstrip("/")
        self.read_attempts = read_attempts
        self._timeout = httpx.Timeout(timeout, connect=min(connect_timeout, timeout))
        self._proxy = proxy
        self.bind_interface, self._resolve = bind_interface, resolve_interface
        self.local_address: str | None = None
        self._owns_http = http is None
        if bind_interface and http is None:
            self.local_address = resolve_interface(bind_interface)
        self.http = http or self._new_http()
        self.order_throttle = Throttle(18, 10)     # documented 20/10s, keep headroom
        self.read_throttle = Throttle(30, 10)
        self.trading_enabled = False
        self.circuit_failures, self.circuit_cooldown = circuit_failures, circuit_cooldown
        self._open_until = 0.0                     # monotonic; reads fail fast while the circuit is open
        self._consecutive = 0
        self.last_ok_at: datetime | None = None    # last successful HTTP exchange (any endpoint)
        self.last_public_ok_at: datetime | None = None
        self.last_auth_ok_at: datetime | None = None
        self.auth_state = "not_configured" if not api_key else "unknown"   # unknown | ok | rejected
        self.endpoint_access: dict[str, dict] = {}
        self.last_error: dict | None = None
        self.error_counts: dict[str, int] = {}
        self.last_rate_headers: dict = {}
        self.proxied = bool(proxy)
        self.limits_source: str | None = None

    def _new_http(self) -> httpx.Client:
        # Binding the source address (not a socket option) is what routes around a full-tunnel VPN: httpcore
        # applies socket options only after connect(), while the source address is bound before it.
        transport = httpx.HTTPTransport(local_address=self.local_address) if self.local_address else None
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": "wtrader/1.0", "Content-Type": "application/json"}, proxy=self._proxy,
                            transport=transport, trust_env=False)

    def _rebind(self):
        """After a transport failure: if the bound interface got a new address (hotspot/DHCP), rebuild the client."""
        if not (self.bind_interface and self._owns_http):
            return
        try:
            addr = self._resolve(self.bind_interface)
        except Exception as e:
            log.warning("bound interface %s unavailable: %s", self.bind_interface, e)
            return
        if addr != self.local_address:
            log.warning("interface %s address changed %s -> %s; rebuilding Wallex HTTP client",
                        self.bind_interface, self.local_address, addr)
            old, self.local_address = self.http, addr
            self.http = self._new_http()
            old.close()

    def fork_reader(self):
        """Independent connection pool for one background reader; trading always disabled."""
        return WallexClient(self._key, self._sub, self.base_url, timeout=self._timeout.read,
                            read_attempts=1, connect_timeout=self._timeout.connect, proxy=self._proxy,
                            http=httpx.Client(transport=self.http._transport) if isinstance(self.http._transport, httpx.MockTransport) else None,
                            circuit_failures=self.circuit_failures, circuit_cooldown=self.circuit_cooldown,
                            bind_interface=self.bind_interface, resolve_interface=self._resolve)

    def close(self):
        self.http.close()

    def enable_trading(self):
        """Called only by the live CLI path after LIVE_ENABLED, the arm record and preflight have all passed."""
        self.trading_enabled = True

    # ------------------------------------------------------------------ health
    def _auth_access(self, path: str, allowed: bool):
        # Keep IDs out of health output and bound cardinality for order lookups.
        endpoint = re.sub(r"(/v1/account/orders)/[^/]+$", r"\1/{client_id}", path)
        self.endpoint_access[endpoint] = {"allowed": allowed, "at": datetime.now(timezone.utc).isoformat()}
        outcomes = [v['allowed'] for v in self.endpoint_access.values()]
        self.auth_state = 'partial' if any(outcomes) and not all(outcomes) else 'ok' if all(outcomes) else 'rejected'

    def _ok(self, auth: bool, path: str):
        now = datetime.now(timezone.utc)
        self.last_ok_at = now
        if auth:
            self.last_auth_ok_at = now
            self._auth_access(path, True)
        else:
            self.last_public_ok_at = now

    def _fail(self, path: str, kind: str, detail: str = "", transport: bool = False):
        self.error_counts[kind] = self.error_counts.get(kind, 0) + 1
        self.last_error = {"at": datetime.now(timezone.utc).isoformat(), "endpoint": path, "kind": kind,
                           "detail": redact(detail)[:200]}
        if transport:
            self._consecutive += 1
            self._rebind()
            if self._consecutive >= self.circuit_failures and not self.circuit_open():
                self._open_until = time.monotonic() + self.circuit_cooldown
                log.warning("Wallex circuit open for %.0fs after %d transport failures (%s)",
                            self.circuit_cooldown, self._consecutive, kind)

    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def health(self) -> dict:
        """Connectivity/auth summary for status and dashboard. Never contains credentials."""
        transport_kinds = ("ConnectTimeout", "ConnectError", "ReadTimeout", "ProxyError", "RemoteProtocolError")
        return {
            "base_url": self.base_url, "proxied": self.proxied,
            "bind_interface": self.bind_interface, "local_address": self.local_address,
            "auth": self.auth_state, "sub_account": bool(self._sub), "trading_enabled": self.trading_enabled,
            "endpoint_access": {p: dict(v) for p, v in self.endpoint_access.items()},
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_public_ok_at": self.last_public_ok_at.isoformat() if self.last_public_ok_at else None,
            "last_auth_ok_at": self.last_auth_ok_at.isoformat() if self.last_auth_ok_at else None,
            "consecutive_transport_failures": self._consecutive,
            "circuit_open": self.circuit_open(),
            "circuit_retry_in_s": round(max(0.0, self._open_until - time.monotonic()), 1),
            "last_error": self.last_error, "error_counts": dict(self.error_counts),
            "hint": NETWORK_HINT if self.last_error and self.last_error["kind"] in transport_kinds
            and self._consecutive else None,
        }

    # ------------------------------------------------------------------ transport
    def _headers(self, auth: bool) -> dict:
        if not auth:
            return {}
        if not self._key:
            raise AuthError("WALLEX_API_KEY not configured")
        h = {"x-api-key": self._key}
        if self._sub:
            h["sub-account-client-id"] = self._sub
        u = urlparse(self.base_url)
        if self._owns_http and (u.scheme != "https" or u.hostname != "api.wallex.ir" or u.username or u.password):
            raise AuthError("refusing to send Wallex credentials to an untrusted API origin")
        return h

    def _get(self, path: str, params: dict | None = None, auth: bool = False):
        last: Exception | None = None
        for attempt in range(self.read_attempts):
            if self.circuit_open():
                raise TransientError(f"GET {path}: Wallex circuit open (recent transport failures; retry in "
                                     f"{self._open_until - time.monotonic():.0f}s)")
            self.read_throttle.wait()
            try:
                r = self.http.get(self.base_url + path, params=params, headers=self._headers(auth))
            except httpx.TransportError as e:
                last = e
                self._fail(path, type(e).__name__, str(e), transport=True)
            else:
                self._consecutive = 0
                if 300 <= r.status_code < 400:
                    self._fail(path, "RedirectRefused")
                    raise TransientError("Wallex redirect refused")
                if r.status_code in (401, 403):
                    if auth:
                        self._auth_access(path, False)
                    self._fail(path, f"HTTP{r.status_code}")
                    raise AuthError(f"GET {path}: HTTP {r.status_code}")
                if r.status_code == 429:
                    ra = r.headers.get("retry-after")
                    last = RateLimited(f"GET {path}: 429", float(ra) if ra and ra.isdigit() else None)
                    self._fail(path, "HTTP429")
                    if attempt + 1 < self.read_attempts:
                        time.sleep(min(10.0, last.retry_after or 2.0))
                    continue
                if r.status_code == 404 and path.startswith("/v1/account/orders/"):
                    return None
                if r.status_code >= 500:
                    last = TransientError(f"GET {path}: HTTP {r.status_code}")
                    self._fail(path, f"HTTP{r.status_code}")
                else:
                    try:
                        body = r.json()
                    except json.JSONDecodeError:
                        last = TransientError(f"GET {path}: unparseable body")
                        self._fail(path, "BadBody")
                    else:
                        if r.status_code >= 400:
                            self._fail(path, f"HTTP{r.status_code}", str(body))
                            raise DefinitiveRejection(f"GET {path}: HTTP {r.status_code} {redact(str(body))[:200]}")
                        if not isinstance(body, dict) or body.get("success") is False:
                            self._fail(path, "InvalidResponse")
                            raise TransientError(f"GET {path}: invalid/unsuccessful response")
                        self._ok(auth, path)
                        self.last_rate_headers = {k: v for k, v in r.headers.items()
                                                  if "ratelimit" in k.lower() or k.lower() == "retry-after"}
                        return body
            if attempt + 1 < self.read_attempts:
                time.sleep(min(4.0, 0.4 * 2 ** attempt + random.random() * 0.3))
        raise TransientError(f"GET {path} failed after {self.read_attempts} attempts: {type(last).__name__}")

    # ------------------------------------------------------------------ public data
    def _hector_markets(self) -> list[dict]:
        body = self._get("/hector/web/v1/markets") or {}
        return list((body.get("result") or {}).get("markets") or [])

    def _legacy_limits(self) -> dict[str, dict]:
        """minQty/minNotional from the UNDOCUMENTED /v1/markets. Best effort: {} when absent or failing."""
        try:
            body = self._get("/v1/markets") or {}
        except Exception as e:
            log.info("optional /v1/markets enrichment unavailable: %s", type(e).__name__)
            return {}
        return {str(raw).upper(): m for raw, m in ((body.get("result") or {}).get("symbols") or {}).items()
                if isinstance(m, dict)}

    def markets(self) -> dict[str, Market]:
        """TMN spot markets from the documented markets endpoint. Exchange minimums come from the undocumented
        /v1/markets when it answers; otherwise they are 0 here and the configured risk.min_order_tmn applies."""
        rows = self._hector_markets()
        limits = self._legacy_limits()
        out = {}
        for m in rows:
            quote = QUOTE_ALIASES.get(str(m.get("quote_asset", "")).upper())
            base = str(m.get("base_asset", "")).upper()
            if quote != "TMN" or not base or m.get("is_spot") is not True:
                continue
            raw = str(m.get("symbol") or base + "TMN").upper()
            lim = limits.get(raw, {})
            try:
                mk = Market(symbol=base + "TMN", raw_symbol=raw, base=base, quote="TMN",
                            tick=precision_step(m["price_precision"]), step=precision_step(m["amount_precision"]),
                            min_quantity=dec(lim.get("minQty") or "0"), min_notional=dec(lim.get("minNotional") or "0"))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                log.warning("skipping market %s with incomplete metadata", raw)
                continue
            out[mk.symbol] = mk
        self.limits_source = "v1/markets (undocumented)" if limits else "none (config min_order_tmn applies)"
        return out

    def spot_flags(self) -> dict[str, bool]:
        return {str(m.get("symbol", "")).upper(): bool(m.get("is_spot")) for m in self._hector_markets()}

    NATIVE_RESOLUTIONS = {60: "1", 900: "15", 3600: "60", 14400: "240"}

    def candles(self, symbol: str, seconds: int, start: int, end: int) -> list[Candle]:
        """Native resolutions only (documented: 1, 15, 60, 240, ...); others (5m) are resampled from complete finer bars."""
        if seconds not in self.NATIVE_RESOLUTIONS:
            from .data import resample
            base = max(b for b in self.NATIVE_RESOLUTIONS if seconds % b == 0)
            return resample(self.candles(symbol, base, start - start % seconds, end), base, seconds)
        res = self.NATIVE_RESOLUTIONS[seconds]
        body = self._get("/v1/udf/history", {"symbol": symbol, "resolution": res, "from": start, "to": end}) or {}
        if body.get("s") == "no_data":
            return []
        if body.get("s") != "ok":
            raise TransientError("unsuccessful candle response")
        from .data import parse_wallex_candles
        return parse_wallex_candles(body, seconds, start, end)

    def book(self, symbol: str) -> Book:
        body = self._get("/v1/depth", {"symbol": symbol}) or {}
        res = body.get("result") or {}

        def side(rows, reverse):
            lv = [(dec(r["price"]), dec(r["quantity"])) for r in rows or [] if dec(r["quantity"]) > 0]
            return tuple(sorted(lv, key=lambda x: x[0], reverse=reverse))

        return Book(symbol, side(res.get("bid"), True), side(res.get("ask"), False), datetime.now(timezone.utc))

    # ------------------------------------------------------------------ account
    def balances(self) -> dict[str, Balance]:
        """Trade-credit sub-account: GET /v1/account/balances-detail (documented available/freeze).
        Main account: GET /v1/account/balances (value/locked; UNVERIFIED whether value includes locked,
        treated as total so free is never overstated)."""
        out = {}
        def amount(row, field):
            if not isinstance(row, dict) or row.get(field) is None:
                raise TransientError('incomplete balance response')
            value = dec(row[field])
            if not value.is_finite() or value < ZERO:
                raise TransientError('invalid balance amount')
            return value
        if self._sub:
            body = self._get("/v1/account/balances-detail", auth=True) or {}
            result = body.get('result')
            if not isinstance(result, dict):
                raise TransientError('incomplete balance response')
            for cur, b in result.items():
                cur = QUOTE_ALIASES.get(str(cur).upper(), str(cur).upper())
                free, locked, total = (amount(b, k) for k in ('available', 'freeze', 'total'))
                if free + locked != total:
                    raise TransientError('inconsistent detailed balance totals')
                out[cur] = Balance(cur, free, locked)
            return out
        body = self._get("/v1/account/balances", auth=True) or {}
        result = (body.get('result') or {}).get('balances')
        if not isinstance(result, dict):
            raise TransientError('incomplete balance response')
        for cur, b in result.items():
            cur = QUOTE_ALIASES.get(cur.upper(), cur.upper())
            value, locked = amount(b, 'value'), amount(b, 'locked')
            if locked > value:
                raise TransientError('unverified main balance total/locked semantics')
            out[cur] = Balance(cur, max(ZERO, value - locked), locked)
        return out

    # ------------------------------------------------------------------ trade-credit (read-only)
    def sub_accounts(self) -> list[dict]:
        """GET /sub-accounts: the trade-credit accounts visible to this key (identity/status fields only)."""
        out, seen = [], set()
        for page in range(1, 21):
            body = self._get("/sub-accounts", {"page": page, "per_page": 100}, auth=True) or {}
            res = body.get("result")
            rows = res if isinstance(res, list) else [res] if isinstance(res, dict) and 'clientId' in res else None
            if rows is None or any(not isinstance(r, dict) or not r.get('clientId') or
                                   type(r.get('isActive')) is not bool or type(r.get('isSuspend')) is not bool for r in rows):
                raise TransientError("incomplete sub-account response; account ownership is unverified")
            for row in rows:
                if row['clientId'] in seen:
                    raise TransientError("sub-account pagination repeated an ID; discovery incomplete")
                seen.add(row['clientId'])
                out.append({k: row.get(k) for k in ("clientId", "title", "type", "isActive", "isSuspend", "createdAt")})
            info = body.get('result_info') or {}
            if info.get('total_count') is not None:
                if len(out) >= int(info['total_count']):
                    return out
                if not rows:
                    raise TransientError("sub-account pagination ended before total_count")
            elif len(rows) < 100:
                return out
        raise TransientError("sub-account pagination exceeded safety bound; discovery incomplete")

    def credit_overview(self) -> dict:
        """GET aggregate credit metadata, without identities or automatic scope selection."""
        res = (self._get('/v1/prop/user/packages/overview', auth=True) or {}).get('result')
        if not isinstance(res, dict) or not isinstance(res.get('aggregatedDetail'), dict) or not isinstance(res.get('userPackages'), list):
            raise TransientError('incomplete credit overview')
        packages = []
        for row in res['userPackages']:
            if not isinstance(row, dict) or not isinstance(row.get('userPackageDetail'), dict):
                raise TransientError('incomplete credit package in overview')
            pkg, up = row.get('packageDetail') or {}, row['userPackageDetail']
            packages.append({'package': {k: pkg.get(k) for k in ('currency', 'assurance', 'loan', 'expireDays')},
                             'status': up.get('status'), 'expire_at': up.get('expireAt'),
                             'matches_configured': bool(self._sub and up.get('subAccountClientId') == self._sub)})
        return {'aggregate': {k: res['aggregatedDetail'].get(k) for k in ('currency', 'assurance', 'loan', 'profit', 'profitPercentage')},
                'packages': packages}

    def active_package(self) -> dict | None:
        """GET /v1/prop/user/packages/active (sub-account header): credit size, limits and connectToWallet."""
        body = self._get("/v1/prop/user/packages/active", auth=True) or {}
        res = body.get("result")
        if not isinstance(res, dict):
            return None
        pkg, up = res.get("packageDetail"), res.get("userPackageDetail")
        if not isinstance(pkg, dict) or not isinstance(up, dict) or 'connectToWallet' not in up:
            raise ValueError("active package/wallet connection state is incomplete")
        link = up['connectToWallet']
        if link is not None and (not isinstance(link, str) or not link):
            raise ValueError('unrecognized wallet connection state')
        return {"package": {k: pkg.get(k) for k in ("title", "assurance", "loan", "currency", "startBalance",
                                                     "dailyFee", "dailyFeeAfterDiscount", "expireDays")},
                "user_package": {k: up.get(k) for k in ("status", "createdAt", "expireAt", "warningLimit",
                                                        "liquidityLimit", "connectToWallet", "profit", "profitPercentage")},
                "alerts": res.get("alertsData") or []}

    def fee_rates(self) -> dict[str, dict[str, Decimal]]:
        """Maker/taker coefficients. Missing fields remain missing, never an assumed zero."""
        body = self._get("/v1/account/fee", auth=True) or {}
        result = body.get('result')
        if not isinstance(result, dict):
            raise TransientError('incomplete fee response')
        out = {}
        for sym, f in result.items():
            # Real endpoint also supplies non-market fee tiers/statistics (verified 2026-10-23).
            if sym in ('default', 'metaData'):
                continue
            rates = {}
            if not isinstance(f, dict):
                raise TransientError('incomplete market fee response')
            for name, field in (('maker', 'makerFeeRate'), ('taker', 'takerFeeRate')):
                if f.get(field) is not None:
                    rate = dec(f[field])
                    if not rate.is_finite() or not ZERO <= rate <= Decimal("0.01"):
                        raise ValueError("unverified fee coefficient/unit; refusing to guess percent conversion")
                    rates[name] = rate
            out[sym.upper()] = rates
        return out

    def taker_fees(self) -> dict[str, Decimal]:
        return {sym: rates['taker'] for sym, rates in self.fee_rates().items() if 'taker' in rates}

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
                raise ValueError("unsupported fee asset; reconciliation requires operator review")
        if dec(o.get("fee") or "0") > 0 and not o.get("fills"):
            raise ValueError("aggregate fee has no verified currency; refusing to omit execution costs")
        return OrderObservation(client_id=str(o.get("clientOrderId", "")), state=state, executed_qty=executed,
                                executed_quote=dec(o.get("executedSum") or "0"), fee_base=fee_base,
                                fee_quote=fee_quote, raw=o)

    def submit(self, intent: OrderIntent, market: Market) -> OrderObservation:
        if not self.trading_enabled:
            raise TradingDisabled(f"refused locally, not sent: trading disabled on this client ({intent.client_id})")
        if self.circuit_open():
            raise DefinitiveRejection(f"refused locally, not sent: Wallex circuit open ({intent.client_id})")
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
            self.auth_state = "rejected"
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
            raise DefinitiveRejection(f"POST order rejected HTTP {r.status_code}: {redact(payload.get('message'))[:200]}")
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

    def _open_orders_page(self, page) -> tuple[list, dict]:
        body = self._get("/v1/account/openOrders", {"per_page": 100, "page": page}, auth=True) or {}
        res = body.get("result") or {}
        rows = res.get("orders") if isinstance(res, dict) and "orders" in res else \
            [o for v in (res.values() if isinstance(res, dict) else []) if isinstance(v, list) for o in v]
        if not isinstance(rows, list) or (isinstance(res, dict) and 'orders' not in res and any(not isinstance(v, list) for v in res.values())):
            raise TransientError("unrecognized open-order response shape")
        return [self.observation(o) for o in rows], body.get('result_info') or {}

    def open_orders(self) -> list[OrderObservation]:
        out, seen = [], set()
        for page in range(1, 21):
            rows, info = self._open_orders_page(page)
            for row in rows:
                if row.client_id in seen:
                    raise TransientError("open-order pagination repeated an ID; reconciliation incomplete")
                seen.add(row.client_id)
                out.append(row)
            if isinstance(info, dict) and info.get('total_count') is not None:
                if len(out) >= int(info['total_count']):
                    return out
                if not rows:
                    raise TransientError("open-order pagination ended before total_count")
            elif len(rows) < 100:
                return out
        raise TransientError("open-order pagination exceeded safety bound; reconciliation incomplete")

    def order_history(self, per_page: int = 25) -> list[dict]:
        """GET /v1/account/orders (documented, paginated). Raw rows; read-only, used by probe/preflight."""
        body = self._get("/v1/account/orders", {"per_page": min(per_page, 100)}, auth=True) or {}
        res = body.get("result")
        if isinstance(res, dict):
            return [o for v in res.values() if isinstance(v, list) for o in v]
        return list(res or [])

    def cancel(self, client_id: str) -> OrderObservation | None:
        if not self.trading_enabled:
            raise TradingDisabled(f"refused locally, not sent: trading disabled on this client (cancel {client_id})")
        self.order_throttle.wait()
        try:
            r = self.http.delete(f"{self.base_url}/v1/account/orders/{client_id}", headers=self._headers(True))
        except httpx.TransportError as e:
            raise AmbiguousSubmission(f"DELETE {client_id}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            self.auth_state = "rejected"
            raise AuthError(f"DELETE order: HTTP {r.status_code}")
        if r.status_code >= 500:
            raise AmbiguousSubmission(f"DELETE {client_id}: HTTP {r.status_code}")
        try:
            payload = r.json()
        except json.JSONDecodeError:
            return None
        res = payload.get("result")
        return self.observation(res) if isinstance(res, dict) and res.get("clientOrderId") else None


D05 = Decimal("0.05")


def route_interface(host: str) -> str | None:
    """The interface the OS routes `host` through (macOS/Linux), e.g. utun4 for a full-tunnel VPN."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", host], capture_output=True, text=True, timeout=5).stdout
            return next((ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.strip().startswith("interface:")), None)
        out = subprocess.run(["ip", "route", "get", socket.gethostbyname(host)], capture_output=True, text=True,
                             timeout=5).stdout.split()
        return out[out.index("dev") + 1] if "dev" in out else None
    except Exception:
        return None


def diagnose_network(base_url: str = "https://api.wallex.ir", timeout: float = 6.0,
                     local_address: str | None = None) -> dict:
    """Step-by-step DNS -> TCP -> TLS -> HTTP check of the Wallex API host. Public, read-only, no credentials.
    Uses the same egress as the client (`local_address` when WALLEX_BIND_INTERFACE is set). TLS is always verified."""
    host = urlparse(base_url).hostname or "api.wallex.ir"
    steps: list[dict] = []
    route = None if local_address else route_interface(host)

    def step(name, fn):
        t0 = time.monotonic()
        try:
            v = fn()
            steps.append({"step": name, "ok": True, "ms": round((time.monotonic() - t0) * 1000), "detail": v})
            return v if v is not None else True
        except Exception as e:
            steps.append({"step": name, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                          "error": f"{type(e).__name__}: {str(e)[:160]}"})
            return None

    addrs = step("dns", lambda: sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}))
    if addrs:
        src = (local_address, 0) if local_address else None
        sock = step("tcp_connect", lambda: socket.create_connection((host, 443), timeout, source_address=src))
        if sock:
            steps[-1]["detail"] = "connected to %s:%s" % sock.getpeername()[:2]
            def tls():
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as s:
                    return s.version()
            try:
                step("tls_handshake", tls)
            finally:
                sock.close()
        transport = httpx.HTTPTransport(local_address=local_address) if local_address else None
        def http_get():
            with httpx.Client(timeout=timeout, transport=transport) as c:
                return c.get(f"{base_url.rstrip('/')}/hector/web/v1/markets").status_code
        step("http_get_markets", http_get)
    failed = next((s["step"] for s in steps if not s["ok"]), None)
    via_tunnel = bool(route) and route.startswith(("utun", "tun", "wg", "ppp", "ipsec"))
    verdict = {
        None: "reachable",
        "dns": "DNS resolution failed for the API host",
        "tcp_connect": "TCP connection refused or blocked",
        "tls_handshake": "TCP accepted but the TLS handshake stalled: typical when the egress IP is outside Iran "
                         "(VPN/tunnel/cloud) or the path filters this SNI. A local VPN/tunnel can also accept TCP itself.",
        "http_get_markets": "the request did not complete: typical when the egress IP is outside Iran (VPN/tunnel/cloud). "
                            "A local VPN/tunnel accepts TCP itself, so only TLS/HTTP reveal the failure.",
    }[failed]
    if failed and via_tunnel:
        verdict += (f" The OS routes {host} through {route} (a VPN tunnel): exclude api.wallex.ir from the VPN, "
                    "or set WALLEX_BIND_INTERFACE to the physical interface (e.g. en0).")
    env_proxy = sorted(k for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")
                       if os.environ.get(k))
    return {"host": host, "route_interface": route, "local_address": local_address, "env_proxy_vars": env_proxy,
            "steps": steps, "failed_step": failed, "verdict": verdict}


def payload_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]
