"""Execution venues and market-data sources.

Shadow and live share every component except the venue:
- LiveVenue: real Wallex orders (WallexClient).
- PaperVenue: simulated fills against the REAL current order book, conservatively:
  marketable LIMIT orders take at most `level_fraction` of each displayed level up to
  the limit price; resting remainders fill only if a later book crosses the limit
  (no passive queue fills are assumed). Taker fees on every fill.
Market data sources: WallexMarketData (public REST) and BinanceProxyMarketData
(USDT prices, symbols mapped XXXTMN->XXXUSDT) for hosts where Wallex is unreachable.
Proxy data is labeled in every status/evidence record.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .models import (AmbiguousSubmission, Balance, Book, Candle, DefinitiveRejection, Market, OrderIntent,
                     OrderObservation, OrderState, ZERO, dec, walk_book)
from .wallex import WallexClient

D = Decimal


# ---------------------------------------------------------------- market data
class WallexMarketData:
    """Wallex prices. Order books come from the WebSocket depth stream when it holds a fresh two-sided
    snapshot, otherwise from REST GET /v1/depth (the documented fallback)."""
    label = "wallex"

    def __init__(self, client: WallexClient, stream=None, ws_max_age_s: float = 4):
        self.client, self.stream, self.ws_max_age_s = client, stream, ws_max_age_s
        self.book_via: dict[str, str] = {}

    def fork_reader(self):
        # Connection pools/rebinding belong to one reader, never shared between workers.
        return WallexMarketData(self.client.fork_reader(), self.stream, self.ws_max_age_s)

    def watch(self, symbols: list[str]):
        if self.stream is not None:
            self.stream.watch(symbols)

    def markets(self) -> dict[str, Market]:
        return self.client.markets()

    def candles(self, symbol: str, seconds: int, start: int, end: int) -> list[Candle]:
        return self.client.candles(symbol, seconds, start, end)

    def book(self, symbol: str) -> Book:
        if self.stream is not None:
            b = self.stream.book(symbol, self.ws_max_age_s)
            if b is not None:
                self.book_via[symbol] = "websocket"
                return b
        b = self.client.book(symbol)
        self.book_via[symbol] = "rest"
        return b

    def status(self) -> dict:
        return {"depth_source": "websocket+rest_fallback" if self.stream is not None else "rest",
                "book_via": dict(self.book_via), "websocket": self.stream.status() if self.stream is not None else None}


class BinanceProxyMarketData:
    """Labeled proxy. Never used for live orders (the engine refuses that combination)."""
    label = "binance_proxy"
    URL = "https://data-api.binance.vision/api/v3"

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=8)

    @staticmethod
    def _sym(symbol: str) -> str:
        return symbol[:-3] + "USDT" if symbol.endswith("TMN") else symbol

    def markets(self) -> dict[str, Market]:
        r = self.http.get(f"{self.URL}/exchangeInfo", params={"permissions": "SPOT"})
        r.raise_for_status()
        out = {}
        for s in r.json()["symbols"]:
            if s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            sym = s["baseAsset"] + "TMN"
            out[sym] = Market(sym, s["symbol"], s["baseAsset"], "TMN", dec(f["PRICE_FILTER"]["tickSize"]),
                              dec(f["LOT_SIZE"]["stepSize"]), dec(f["LOT_SIZE"]["minQty"]),
                              dec((f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}).get("minNotional", "0")))
        return out

    def candles(self, symbol: str, seconds: int, start: int, end: int) -> list[Candle]:
        iv = {900: "15m", 3600: "1h", 14400: "4h"}[seconds]
        r = self.http.get(f"{self.URL}/klines", params={"symbol": self._sym(symbol), "interval": iv,
                                                        "startTime": start * 1000, "endTime": end * 1000, "limit": 1000})
        r.raise_for_status()
        return [Candle(int(k[0]) // 1000, dec(k[1]), dec(k[2]), dec(k[3]), dec(k[4]), dec(k[5])) for k in r.json()]

    def book(self, symbol: str) -> Book:
        r = self.http.get(f"{self.URL}/depth", params={"symbol": self._sym(symbol), "limit": 50})
        r.raise_for_status()
        j = r.json()
        return Book(symbol, tuple((dec(p), dec(q)) for p, q in j["bids"]), tuple((dec(p), dec(q)) for p, q in j["asks"]),
                    datetime.now(timezone.utc))


class KucoinProxyMarketData:
    """Labeled proxy (USDT prices, XXXTMN -> XXX-USDT). Never used for live orders."""
    label = "kucoin_proxy"
    URL = "https://api.kucoin.com/api"
    INTERVAL = {60: "1min", 300: "5min", 900: "15min", 3600: "1hour", 14400: "4hour"}

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=8)

    @staticmethod
    def _sym(symbol: str) -> str:
        return symbol[:-3] + "-USDT" if symbol.endswith("TMN") else symbol

    def markets(self) -> dict[str, Market]:
        r = self.http.get(f"{self.URL}/v2/symbols")
        r.raise_for_status()
        out = {}
        for m in r.json()["data"]:
            if m.get("quoteCurrency") != "USDT" or not m.get("enableTrading"):
                continue
            sym = m["baseCurrency"] + "TMN"
            out[sym] = Market(sym, m["symbol"], m["baseCurrency"], "TMN", dec(m["priceIncrement"]),
                              dec(m["baseIncrement"]), dec(m["baseMinSize"]), dec(m.get("minFunds") or "0"))
        return out

    def candles(self, symbol: str, seconds: int, start: int, end: int) -> list[Candle]:
        r = self.http.get(f"{self.URL}/v1/market/candles", params={"type": self.INTERVAL[seconds], "symbol": self._sym(symbol),
                                                                   "startAt": start, "endAt": end})
        r.raise_for_status()
        rows = [Candle(int(k[0]), dec(k[1]), dec(k[3]), dec(k[4]), dec(k[2]), dec(k[5])) for k in r.json().get("data") or []]
        return sorted((c for c in rows if c.open_time % seconds == 0), key=lambda c: c.open_time)

    def book(self, symbol: str) -> Book:
        r = self.http.get(f"{self.URL}/v1/market/orderbook/level2_20", params={"symbol": self._sym(symbol)})
        r.raise_for_status()
        j = r.json()["data"]
        return Book(symbol, tuple((dec(p), dec(q)) for p, q in j["bids"]), tuple((dec(p), dec(q)) for p, q in j["asks"]),
                    datetime.now(timezone.utc))


# ---------------------------------------------------------------- venues
class LiveVenue:
    label = "live"

    def __init__(self, client: WallexClient):
        self.client = client

    def submit(self, intent: OrderIntent, market: Market) -> OrderObservation:
        return self.client.submit(intent, market)

    def lookup(self, client_id: str):
        return self.client.lookup(client_id)

    def cancel(self, client_id: str):
        return self.client.cancel(client_id)

    def balances(self) -> dict[str, Balance]:
        return self.client.balances()

    def open_orders(self) -> list[OrderObservation]:
        return self.client.open_orders()

    def poll(self, books: dict[str, Book]):
        pass


@dataclass
class _PaperOrder:
    intent: OrderIntent
    market: Market
    executed: Decimal = ZERO
    quote: Decimal = ZERO
    fee_base: Decimal = ZERO
    fee_quote: Decimal = ZERO
    state: OrderState = OrderState.OPEN


@dataclass
class PaperVenue:
    """Conservative simulated venue. `inject` lets tests force ambiguous/rejected outcomes."""
    book_source: object
    fee_fraction: Decimal = D("0.0035")
    level_fraction: Decimal = D("0.5")
    balances_: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)
    inject: list = field(default_factory=list)      # queue of 'reject' | 'timeout_accepted' | 'timeout_lost'
    label: str = "paper"
    max_book_age_s: float = 8
    checkpoint: object = None
    clock: object = None

    def __post_init__(self):
        self.lock = threading.Lock()

    def fund(self, currency: str, amount: Decimal):
        self.balances_[currency] = self.balances_.get(currency, ZERO) + amount

    def balances(self) -> dict[str, Balance]:
        locked: dict[str, Decimal] = {}
        for o in self.orders.values():
            if o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                rem = o.intent.quantity - o.executed
                if o.intent.side == "BUY":
                    locked["TMN"] = locked.get("TMN", ZERO) + rem * o.intent.limit_price * (1 + self.fee_fraction)
                else:
                    locked[o.market.base] = locked.get(o.market.base, ZERO) + rem
        return {c: Balance(c, v - locked.get(c, ZERO), locked.get(c, ZERO)) for c, v in self.balances_.items()}

    def _obs(self, o: _PaperOrder) -> OrderObservation:
        return OrderObservation(o.intent.client_id, o.state, o.executed, o.quote, o.fee_base, o.fee_quote,
                                {"simulated": True, "symbol": o.intent.symbol, "side": o.intent.side,
                                 "price": str(o.intent.limit_price), "origQty": str(o.intent.quantity)})

    def open_orders(self) -> list[OrderObservation]:
        with self.lock:
            return [self._obs(o) for o in self.orders.values()
                    if o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)]

    def _try_fill(self, o: _PaperOrder, book: Book):
        now = self.clock() if self.clock else datetime.now(timezone.utc)
        if not (0 <= book.age_seconds(now) <= self.max_book_age_s) or book.spread_bps() is None:
            return
        # A displayed snapshot provides finite liquidity; polling it again cannot refill the same order.
        token = (book.received_at, book.bids, book.asks)
        if getattr(o, 'last_book', None) == token:
            return
        o.last_book = token
        rem = o.intent.quantity - o.executed
        if rem <= 0:
            return
        levels = book.asks if o.intent.side == "BUY" else book.bids
        scaled = tuple((p, q * self.level_fraction) for p, q in levels)
        qty, notional = walk_book(scaled, rem, o.intent.limit_price, o.intent.side)
        if qty <= 0:
            return
        base = o.market.base
        if o.intent.side == "BUY":
            fee = qty * self.fee_fraction                 # buy fee charged in base
            self.balances_["TMN"] = self.balances_.get("TMN", ZERO) - notional
            self.balances_[base] = self.balances_.get(base, ZERO) + qty - fee
            o.fee_base += fee
        else:
            fee = notional * self.fee_fraction            # sell fee charged in quote
            self.balances_[base] = self.balances_.get(base, ZERO) - qty
            self.balances_["TMN"] = self.balances_.get("TMN", ZERO) + notional - fee
            o.fee_quote += fee
        o.executed += qty
        o.quote += notional
        o.state = OrderState.FILLED if o.executed >= o.intent.quantity else OrderState.PARTIALLY_FILLED

    def submit(self, intent: OrderIntent, market: Market) -> OrderObservation:
        with self.lock:
            if intent.client_id in self.orders:
                raise DefinitiveRejection("duplicate client_id")     # mirrors Wallex uniqueness rule
            mode = self.inject.pop(0) if self.inject else None
            if mode == "reject":
                raise DefinitiveRejection("injected rejection")
            if mode == "timeout_lost":
                raise AmbiguousSubmission("injected timeout; order never reached venue")
            bal = self.balances()
            need = intent.quantity * intent.limit_price * (1 + self.fee_fraction) if intent.side == "BUY" else intent.quantity
            have = bal.get("TMN" if intent.side == "BUY" else market.base, Balance("", ZERO, ZERO)).free
            if need > have:
                raise DefinitiveRejection(f"insufficient balance need={need} have={have}")
            o = _PaperOrder(intent, market)
            self.orders[intent.client_id] = o
            try:
                self._try_fill(o, self.book_source.book(intent.symbol))
            finally:
                self._save()

            if mode == "timeout_accepted":
                raise AmbiguousSubmission("injected timeout after acceptance")
            return self._obs(o)

    def lookup(self, client_id: str):
        with self.lock:
            o = self.orders.get(client_id)
            return self._obs(o) if o else None

    def cancel(self, client_id: str):
        with self.lock:
            o = self.orders.get(client_id)
            if o and o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                o.state = OrderState.CANCELED
                self._save()
            return self._obs(o) if o else None

    def poll(self, books: dict[str, Book]):
        """Resting orders fill only when the new book crosses their limit."""
        with self.lock:
            for o in self.orders.values():
                if o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED) and o.intent.symbol in books:
                    self._try_fill(o, books[o.intent.symbol])
            self._save()

    def _save(self):
        if self.checkpoint:
            self.checkpoint(self.snapshot())

    def snapshot(self):
        from dataclasses import asdict
        return {'balances': {c: str(v) for c, v in self.balances_.items()},
                'orders': [{ 'intent': asdict(o.intent), 'market': asdict(o.market),
                             'executed': str(o.executed), 'quote': str(o.quote),
                             'fee_base': str(o.fee_base), 'fee_quote': str(o.fee_quote),
                             'state': o.state.value} for o in self.orders.values()]}

    def restore(self, state):
        self.balances_ = {c: dec(v) for c, v in state['balances'].items()}
        for row in state.get('orders', []):
            i, m = dict(row['intent']), dict(row['market'])
            for k in ('limit_price', 'quantity'):
                i[k] = dec(i[k])
            for k in ('tick', 'step', 'min_quantity', 'min_notional'):
                m[k] = dec(m[k])
            o = _PaperOrder(OrderIntent(**i), Market(**m), *(dec(row[k]) for k in ('executed','quote','fee_base','fee_quote')),
                            OrderState(row['state']))
            self.orders[o.intent.client_id] = o
