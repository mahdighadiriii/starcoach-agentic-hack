"""Normalized domain types shared by every mode (backtest, shadow, live).

Money and quantities are Decimal end to end. Derived from the challenge pack's
contracts/broker.py boundary.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

D = Decimal
ZERO = D("0")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dec(value) -> Decimal:
    """Build a finite Decimal from exchange input (always via str)."""
    if isinstance(value, Decimal):
        out = value
    elif value is None or value == "":
        raise ValueError("missing numeric value")
    else:
        out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError(f"non-finite numeric value {value!r}")
    return out


def floor_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class Market:
    symbol: str          # normalized, e.g. BTCTMN
    raw_symbol: str      # as sent to the exchange
    base: str
    quote: str           # always TMN for tradable markets in this system
    tick: Decimal
    step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    spot_active: bool = True


@dataclass(frozen=True)
class Candle:
    open_time: int       # epoch seconds, bar open
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def close_time(self, seconds: int) -> int:
        return self.open_time + seconds


@dataclass(frozen=True)
class Book:
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]   # (price, base qty), best first
    asks: tuple[tuple[Decimal, Decimal], ...]
    received_at: datetime

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    def spread_bps(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        bid, ask = self.bids[0][0], self.asks[0][0]
        if bid <= 0 or ask < bid:
            return None
        return (ask - bid) / ((ask + bid) / 2) * D(10000)

    def age_seconds(self, now: datetime) -> float:
        return (now - self.received_at).total_seconds()


def walk_book(levels, quantity: Decimal, limit: Decimal | None, side: str):
    """Return (filled_qty, notional) walking levels up to `limit` price.

    side='BUY' consumes asks (price <= limit); side='SELL' consumes bids (price >= limit).
    """
    remaining, filled, notional = quantity, ZERO, ZERO
    for price, qty in levels:
        if limit is not None and ((side == "BUY" and price > limit) or (side == "SELL" and price < limit)):
            break
        take = min(qty, remaining)
        filled += take
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break
    return filled, notional


@dataclass(frozen=True)
class Balance:
    currency: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class OrderState(str, enum.Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"   # outcome unresolvable; entries halted until operator review

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def unresolved(self) -> bool:
        return self in (OrderState.UNKNOWN, OrderState.RECONCILING, OrderState.QUARANTINED, OrderState.SUBMITTING)


TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    client_id: str
    symbol: str
    side: str            # BUY | SELL
    limit_price: Decimal
    quantity: Decimal    # base quantity
    purpose: str         # ENTRY | STOP | TARGET | TIME_EXIT | LIQUIDATION | BREAKER
    decision_id: str | None
    position_id: str | None


@dataclass(frozen=True)
class OrderObservation:
    client_id: str
    state: OrderState
    executed_qty: Decimal
    executed_quote: Decimal          # cumulative quote notional executed
    fee_base: Decimal                # cumulative fee charged in base asset
    fee_quote: Decimal               # cumulative fee charged in quote asset (TMN)
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def avg_price(self) -> Decimal | None:
        return self.executed_quote / self.executed_qty if self.executed_qty > 0 else None


class ExchangeError(Exception):
    """Base for adapter errors."""


class AmbiguousSubmission(ExchangeError):
    """Remote acceptance unknown (timeout, 5xx, connection reset). MUST reconcile, never blind-retry."""


class DefinitiveRejection(ExchangeError):
    """Verified evidence the order was not accepted (4xx validation, insufficient balance...)."""


class AuthError(ExchangeError):
    """401/403. Not transient."""


class RateLimited(ExchangeError):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class TransientError(ExchangeError):
    """Read failed after retries (network/5xx)."""
