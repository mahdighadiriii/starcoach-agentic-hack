"""Hard guardrails. The LLM proposes, this module disposes.

Every rule here is deterministic code, never a prompt — that's the
governance story for the demo.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.markets import DISABLED_MARKETS


@dataclass
class Decision:
    symbol: str
    action: str  # BUY | SELL | HOLD
    confidence: float
    size_quote: float
    reasoning: str


@dataclass
class RiskResult:
    approved: bool
    decision: Decision
    notes: list[str] = field(default_factory=list)


@dataclass
class RiskState:
    start_equity: float | None = None
    halted: bool = False


def in_flatten_window(s: Settings, now: datetime | None = None) -> bool:
    """True once we are close enough to the end of the trading window to sell everything to TMN."""
    if s.window_end is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= s.window_end - timedelta(minutes=s.flatten_minutes_before)


def check(d: Decision, *, price: float, balances: dict[str, float], equity: float,
          state: RiskState, s: Settings, min_notional: float = 0.0) -> RiskResult:
    notes: list[str] = []

    if state.start_equity is None:
        state.start_equity = equity
    if state.start_equity and equity < state.start_equity * (1 - s.max_daily_loss_pct):
        state.halted = True
    if state.halted:
        return RiskResult(False, d, ["kill switch: daily loss limit hit, trading halted"])

    if d.action not in ("BUY", "SELL"):
        return RiskResult(False, d, ["HOLD — nothing to do"])
    if d.symbol in DISABLED_MARKETS:
        return RiskResult(False, d, [f"{d.symbol} is disabled in the hackathon credit account"])
    if d.confidence < s.min_confidence:
        return RiskResult(False, d, [f"confidence {d.confidence:.2f} < {s.min_confidence}"])
    if price <= 0:
        return RiskResult(False, d, ["no valid price"])
    if d.action == "BUY" and in_flatten_window(s):
        return RiskResult(False, d, ["end of trading window: no new positions, flattening to TMN"])

    if d.size_quote > s.max_order_quote:
        notes.append(f"size capped {d.size_quote:.2f} -> {s.max_order_quote:.2f}")
        d.size_quote = s.max_order_quote

    base = d.symbol.removesuffix(s.quote_asset)
    if d.action == "BUY":
        cash = balances.get(s.quote_asset, 0.0)
        if not s.dry_run and d.size_quote > cash:
            return RiskResult(False, d, notes + [f"insufficient {s.quote_asset}: {cash:.2f}"])
        held_value = balances.get(base, 0.0) * price
        if equity and (held_value + d.size_quote) / equity > s.max_position_pct:
            return RiskResult(False, d, notes + ["would exceed max position size"])
    else:
        held = balances.get(base, 0.0)
        if not s.dry_run and held * price < d.size_quote:
            d.size_quote = held * price
            notes.append(f"sell size reduced to holdings ({held} {base})")
        if not s.dry_run and d.size_quote <= 0:
            return RiskResult(False, d, notes + [f"no {base} to sell"])

    if d.size_quote < min_notional:
        return RiskResult(False, d, notes + [f"order {d.size_quote:,.0f} below min notional {min_notional:,.0f}"])

    return RiskResult(True, d, notes or ["ok"])
