"""Thin Wallex REST client.

Checked against the official docs (https://api-docs.wallex.ir, source:
github.com/PhinixHQ/wallex-new-api-docs) and the go-wallex SDK: auth is the
X-API-Key header, order type/side are uppercase (LIMIT/MARKET, BUY/SELL).
"""
import math
import time

import httpx

from app.config import Settings


def floor_to(x: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(x * q) / q


class WallexError(Exception):
    """Non-2xx reply. Wallex puts an error_code in the body, e.g. 1001 bad price precision, 1006 low balance."""


def _check(r: httpx.Response) -> dict:
    if r.is_error:
        raise WallexError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


class WallexClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.http = httpx.Client(
            base_url=settings.wallex_base_url,
            timeout=15,
            headers={"X-API-Key": settings.wallex_api_key} if settings.wallex_api_key else {},
        )
        self._info: dict[str, dict] = {}

    def _get(self, path: str, **params) -> dict:
        return _check(self.http.get(path, params=params)).get("result", {})

    # --- market data (public) ---
    def markets(self) -> dict[str, dict]:
        markets = self._get("/v1/markets").get("symbols", {})
        for sym, m in markets.items():
            self._info[sym] = {k: m.get(k) for k in ("stepSize", "tickSize", "minNotional", "isMarketTypeEnable")}
        return markets

    def market_info(self, symbol: str) -> dict:
        """stepSize / tickSize are decimal places for quantity / price; minNotional is in quote."""
        if symbol not in self._info:
            self.markets()
        return self._info.get(symbol, {})

    def ticker(self, symbol: str) -> dict:
        stats = self.markets().get(symbol, {}).get("stats", {})

        def num(k):
            try:
                return float(stats.get(k) or 0)
            except ValueError:  # Wallex returns "-" for missing stats
                return 0.0

        return {
            "symbol": symbol,
            "last": num("lastPrice"),
            "bid": num("bidPrice"),
            "ask": num("askPrice"),
            "change_24h_pct": num("24h_ch"),
            "volume_24h": num("24h_volume"),
        }

    def prices(self, quote: str) -> dict[str, float]:
        """Last price of every asset in `quote`, keyed by base asset (e.g. {"BTC": 2.2e10})."""
        out = {}
        for sym, m in self.markets().items():
            if m.get("quoteAsset") == quote:
                try:
                    out[m["baseAsset"]] = float(m["stats"].get("lastPrice") or 0)
                except ValueError:
                    pass
        return out

    def candles(self, symbol: str, resolution: str = "60", hours: int = 48) -> list[dict]:
        now = int(time.time())
        data = self._get_raw(
            "/v1/udf/history",
            symbol=symbol, resolution=resolution, **{"from": now - hours * 3600, "to": now},
        )
        keys = ("t", "o", "h", "l", "c", "v")
        rows = zip(*(data.get(k, []) for k in keys))
        return [dict(zip(keys, (int(r[0]), *map(float, r[1:])))) for r in rows]

    def _get_raw(self, path: str, **params) -> dict:
        return _check(self.http.get(path, params=params))

    # --- account (private) ---
    def balances(self) -> dict[str, float]:
        if not self.s.wallex_api_key:
            return {}
        raw = self._get("/v1/account/balances").get("balances", {})
        return {asset: float(b.get("value") or 0) - float(b.get("locked") or 0) for asset, b in raw.items()}

    def open_orders(self) -> list[dict]:
        if not self.s.wallex_api_key:
            return []
        return self._get("/v1/account/openOrders").get("orders", [])

    def cancel_order(self, client_id: str) -> dict:
        if self.s.dry_run:
            return {"dry_run": True, "cancel": client_id}
        return _check(self.http.delete("/v1/account/orders", params={"clientOrderId": client_id})).get("result", {})

    def place_order(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        info = self.market_info(symbol)
        step, tick = int(info.get("stepSize") or 8), int(info.get("tickSize") or 0)
        # no client id sent: docs say client_id, the SDK says clientOrderId, so we let Wallex
        # generate one and log the clientOrderId it returns
        order = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "quantity": f"{floor_to(quantity, step):.{step}f}",
            "price": f"{round(price, tick):.{tick}f}",
        }
        if self.s.dry_run:
            return {"dry_run": True, **order}
        return _check(self.http.post("/v1/account/orders", json=order)).get("result", {})
