"""Deterministic, non-bypassable risk gate.

Pure functions of explicit state, so the backtester, shadow and live modes run the
identical logic. The AI can only pass an exposure multiplier from {0.5, 1.0};
everything else (caps, stops, fees, markets) comes from configuration and state.
Sizing follows the challenge-pack reference (reference/risk.py size_entry).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .config import RiskCfg, MarketCfg, CompetitionCfg
from .models import Book, Market, ceil_step, floor_step, walk_book

D = Decimal
ZERO = D("0")
ALLOWED_MULTIPLIERS = (D("0.5"), D("1.0"))


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    notional_tmn: Decimal      # valued at current bid (or reserved buy notional for pending entries)
    stop_risk_tmn: Decimal     # planned loss to stop including exit costs


@dataclass(frozen=True)
class PortfolioState:
    equity_tmn: Decimal        # conservative: TMN + positions at bid
    free_tmn: Decimal
    exposures: tuple[OpenRisk, ...]
    day_start_equity_tmn: Decimal
    high_water_tmn: Decimal
    entries_halted: bool = False          # latched breaker or unresolved order
    halt_reason: str | None = None
    cooldown_symbols: frozenset = frozenset()


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self):
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class EntryVerdict:
    approved: bool
    checks: list[Check] = field(default_factory=list)
    quantity: Decimal = ZERO
    limit_price: Decimal = ZERO
    expected_avg_price: Decimal = ZERO
    notional_tmn: Decimal = ZERO
    planned_loss_tmn: Decimal = ZERO
    reason: str = ""

    def as_dict(self):
        return {"approved": self.approved, "reason": self.reason, "quantity": str(self.quantity),
                "limit_price": str(self.limit_price), "expected_avg_price": str(self.expected_avg_price),
                "notional_tmn": str(self.notional_tmn), "planned_loss_tmn": str(self.planned_loss_tmn),
                "checks": [c.as_dict() for c in self.checks]}


@dataclass(frozen=True)
class BreakerStatus:
    daily_loss_hit: bool
    drawdown_hit: bool
    daily_loss_fraction: Decimal
    drawdown_fraction: Decimal


def breaker_status(state: PortfolioState, cfg: RiskCfg) -> BreakerStatus:
    dl = (state.day_start_equity_tmn - state.equity_tmn) / state.day_start_equity_tmn if state.day_start_equity_tmn > 0 else ZERO
    dd = (state.high_water_tmn - state.equity_tmn) / state.high_water_tmn if state.high_water_tmn > 0 else ZERO
    return BreakerStatus(dl >= D(cfg.daily_loss_fraction), dd >= D(cfg.max_drawdown_fraction), dl, dd)


def competition_phase(now: datetime, comp: CompetitionCfg) -> str:
    """PRE_START | TRADING | NO_NEW_ENTRIES | LIQUIDATE | ENDED | UNSCHEDULED."""
    start, end = comp.start_dt(), comp.end_dt()
    if end is None:
        return "UNSCHEDULED"
    from datetime import timedelta
    if start and now < start:
        return "PRE_START"
    if now >= end:
        return "ENDED"
    if now >= end - timedelta(minutes=comp.begin_liquidation_before_end_minutes):
        return "LIQUIDATE"
    if now >= end - timedelta(minutes=comp.halt_entries_before_end_minutes):
        return "NO_NEW_ENTRIES"
    return "TRADING"


def evaluate_entry(*, symbol: str, stop: Decimal, max_stop_fraction: Decimal, min_stop_fraction: Decimal,
                   multiplier: Decimal, state: PortfolioState, market: Market | None, book: Book | None,
                   now: datetime, fee_fraction: Decimal, risk: RiskCfg, mcfg: MarketCfg,
                   allowlist: list[str] | None, phase: str) -> EntryVerdict:
    """Every check is evaluated and recorded; the first failure determines the reason."""
    v = EntryVerdict(approved=False)

    def check(name, ok, detail=""):
        v.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    check("phase_allows_entries", phase in ("TRADING", "UNSCHEDULED"), phase)
    check("entries_not_halted", not state.entries_halted, state.halt_reason or "")
    br = breaker_status(state, risk)
    check("daily_loss_limit", not br.daily_loss_hit, f"{br.daily_loss_fraction:.4f} < {risk.daily_loss_fraction}")
    check("drawdown_breaker", not br.drawdown_hit, f"{br.drawdown_fraction:.4f} < {risk.max_drawdown_fraction}")
    check("ai_multiplier_bounded", multiplier in ALLOWED_MULTIPLIERS, str(multiplier))
    check("market_whitelisted", allowlist is None or symbol in allowlist, symbol)
    check("market_metadata_known", market is not None and market.spot_active and market.quote == "TMN", symbol)
    check("not_in_cooldown", symbol not in state.cooldown_symbols, symbol)
    held = {e.symbol for e in state.exposures}
    check("no_duplicate_symbol", symbol not in held, symbol)
    check("max_concurrent_positions", len(state.exposures) < risk.max_positions, f"{len(state.exposures)}/{risk.max_positions}")
    if book is None or market is None:
        check("book_available", False, "missing")
        return _finish(v)
    age = book.age_seconds(now)
    check("book_fresh", age <= mcfg.max_book_age_seconds, f"age={age:.1f}s")
    spread = book.spread_bps()
    check("spread_limit", spread is not None and spread <= D(mcfg.max_spread_bps), f"{spread} bps")
    ask = book.best_ask
    if ask is None or ask <= 0 or spread is None:
        return _finish(v)

    stop_frac = (ask - stop) / ask
    check("stop_below_entry", stop < ask, f"stop={stop} ask={ask}")
    check("stop_distance_bounds", min_stop_fraction <= stop_frac <= max_stop_fraction, f"{stop_frac:.5f}")

    slip = D(mcfg.max_slippage_bps) / D(10000)
    limit = ceil_step(ask * (1 + slip), market.tick)
    exit_cost = fee_fraction * 2 + slip
    loss_per_unit = ask - stop + ask * exit_cost if stop < ask else None
    E = state.equity_tmn
    open_notional = sum((e.notional_tmn for e in state.exposures), ZERO)
    open_risk = sum((e.stop_risk_tmn for e in state.exposures), ZERO)
    rem_total = E * D(risk.max_total_exposure_fraction) - open_notional
    rem_risk = E * D(risk.max_aggregate_stop_risk_fraction) - open_risk
    symbol_cap = E * D(risk.max_symbol_notional_fraction)
    depth_qty, _ = walk_book(book.asks, D("1e30"), limit, "BUY")
    depth_cap_qty = depth_qty * D(mcfg.max_depth_fraction)
    check("aggregate_exposure_capacity", rem_total > 0, f"remaining={rem_total:.0f}")
    check("aggregate_stop_risk_capacity", rem_risk > 0, f"remaining={rem_risk:.0f}")
    if not loss_per_unit or loss_per_unit <= 0 or not all(c.passed for c in v.checks):
        return _finish(v)

    budget = min(E * D(risk.risk_per_trade_fraction) * multiplier, rem_risk)
    q = floor_step(min(
        budget / loss_per_unit,
        symbol_cap * multiplier / limit,
        rem_total * multiplier / limit,
        depth_cap_qty,
        state.free_tmn / (limit * (1 + fee_fraction)),
    ), market.step)
    filled, notional = walk_book(book.asks, q, limit, "BUY")
    avg = notional / filled if filled > 0 else ask
    notional_at_limit = q * limit
    check("quantity_positive", q > 0, str(q))
    check("min_quantity", q >= market.min_quantity, f"{q} >= {market.min_quantity}")
    check("min_notional", notional_at_limit >= max(market.min_notional, D(risk.min_order_tmn)),
          f"{notional_at_limit:.0f} >= {max(market.min_notional, D(risk.min_order_tmn))}")
    check("balance_sufficient", notional_at_limit * (1 + fee_fraction) <= state.free_tmn,
          f"need={notional_at_limit * (1 + fee_fraction):.0f} free={state.free_tmn:.0f}")
    slip_bps = (avg - ask) / ask * D(10000)
    check("slippage_limit", filled >= q and slip_bps <= D(mcfg.max_slippage_bps), f"{slip_bps:.1f} bps, fillable={filled}")
    v.quantity, v.limit_price, v.expected_avg_price = q, limit, avg
    v.notional_tmn = notional_at_limit
    v.planned_loss_tmn = q * loss_per_unit if q > 0 else ZERO
    return _finish(v)


def _finish(v: EntryVerdict) -> EntryVerdict:
    failed = [c for c in v.checks if not c.passed]
    v.approved = not failed and v.quantity > 0
    v.reason = "approved" if v.approved else (failed[0].name if failed else "incomplete")
    return v


@dataclass(frozen=True)
class ExitSignal:
    reason: str      # BREAKER | LIQUIDATION | STOP | TARGET | TIME_EXIT
    priority: int


def exit_signal(*, bid: Decimal | None, stop: Decimal, target: Decimal, bars_held: int, max_bars: int,
                phase: str, drawdown_hit: bool) -> ExitSignal | None:
    """Exit priority from the pack: terminal/breaker > stop > target > time. Never consults the AI."""
    if phase in ("LIQUIDATE", "ENDED"):
        return ExitSignal("LIQUIDATION", 0)
    if drawdown_hit:
        return ExitSignal("BREAKER", 0)
    if bid is None:
        return None
    if bid <= stop:
        return ExitSignal("STOP", 1)
    if bid >= target:
        return ExitSignal("TARGET", 2)
    if bars_held >= max_bars:
        return ExitSignal("TIME_EXIT", 3)
    return None
