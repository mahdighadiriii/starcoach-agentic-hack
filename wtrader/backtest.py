"""Bar-replay backtester sharing strategy, selector, risk and exit code with live trading.

Timing model (no look-ahead):
- A decision at bar close t sees only bars closed at or before t.
- An approved entry fills at the NEXT bar's open, plus half-spread and slippage.
- Exits are checked on every later bar: stop first when a bar touches both stop and
  target; gaps through the stop fill at the (worse) open. Time exits fill at close.
- Fees: buy fee reduces received base; sell fee reduces quote proceeds.
- The run is split into consecutive competition-length episodes; the drawdown
  breaker and daily loss limit are evaluated per episode, mirroring a 3-day contest.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .ai_agent import BaselineSelector, build_context
from .config import Settings
from .models import Book, Candle, Market
from .risk import OpenRisk, PortfolioState, breaker_status, evaluate_entry, exit_signal
from .strategy import analyze, apply_market_filter, trail_stop

D = Decimal
ZERO = D("0")


@dataclass
class CostModel:
    fee_fraction: Decimal = D("0.0035")
    half_spread_bps: Decimal = D("8")
    slippage_bps: Decimal = D("5")

    def buy_price(self, p: Decimal) -> Decimal:
        return p * (1 + (self.half_spread_bps + self.slippage_bps) / D(10000))

    def sell_price(self, p: Decimal) -> Decimal:
        return p * (1 - (self.half_spread_bps + self.slippage_bps) / D(10000))


@dataclass
class BtPosition:
    symbol: str
    family: str
    candidate_id: str
    qty: Decimal
    entry_price: Decimal
    cost_tmn: Decimal
    stop: Decimal
    target: Decimal
    opened_at: int
    max_bars: int
    bars_held: int = 0
    trail_distance: Decimal | None = None
    peak: Decimal = ZERO


@dataclass
class BtTrade:
    symbol: str
    family: str
    candidate_id: str
    opened_at: int
    closed_at: int
    entry_price: float
    exit_price: float
    qty: float
    cost_tmn: float
    proceeds_tmn: float
    pnl_tmn: float
    net_bps: float
    fees_tmn: float
    exit_reason: str
    selector_source: str
    multiplier: str


@dataclass
class BtResult:
    label: str
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)          # (t, equity)
    decisions: list = field(default_factory=list)
    episodes: list = field(default_factory=list)


def load_series(root: Path, source: str, symbols: list[str], sig_s: int = 900,
                ctx_s: int = 3600) -> dict[str, tuple[list[Candle], list[Candle]]]:
    from .data import load_frames
    return {s: load_frames(root, source, s, sig_s, ctx_s) for s in symbols}


def _synthetic_book(symbol: str, close: Decimal, costs: CostModel, t: int) -> Book:
    hs = costs.half_spread_bps / D(10000)
    ts = datetime.fromtimestamp(t, timezone.utc)
    big = D("1e12")
    return Book(symbol, ((close * (1 - hs), big),), ((close * (1 + hs), big),), ts)


def _market(symbol: str) -> Market:
    return Market(symbol, symbol, symbol[:-4], "TMN", D("1e-12"), D("1e-12"), ZERO, ZERO)


def run_backtest(settings: Settings, series: dict, start: int, end: int, selector, costs: CostModel,
                 edge: dict, label: str, episode_days: int = 3, record_decisions: bool = True) -> BtResult:
    sc, rc, mc = settings.strategy, settings.risk, settings.market
    sig_s = mc.signal_seconds
    capital = D(settings.competition.initial_capital_tmn)
    baseline = BaselineSelector()
    res = BtResult(label)
    # index candles by open_time for O(1) bar lookup
    bars = {s: {c.open_time: c for c in sig} for s, (sig, _) in series.items()}
    times = sorted({t for s in bars for t in bars[s] if start <= t + sig_s <= end})
    cash = capital
    positions: dict[str, BtPosition] = {}
    pending: list[tuple] = []        # entries to fill at next bar open
    episode_start, ep_start_equity = None, capital
    hwm, day, day_start_equity = capital, None, capital
    halted_until_day, latched = None, False
    cooldown: dict[str, int] = {}
    episode_len = episode_days * 86400

    def mark(t_close: int) -> Decimal:
        val = cash
        for p in positions.values():
            b = bars[p.symbol].get(t_close - sig_s)
            px = b.close if b else p.entry_price
            val += p.qty * costs.sell_price(px) * (1 - costs.fee_fraction)
        return val

    def close_pos(p: BtPosition, raw_px: Decimal, t: int, reason: str, src="", mult=""):
        nonlocal cash
        px = costs.sell_price(raw_px)
        gross = p.qty * px
        fee = gross * costs.fee_fraction
        proceeds = gross - fee
        cash += proceeds
        pnl = proceeds - p.cost_tmn
        entry_fee_tmn = p.cost_tmn - p.cost_tmn * (1 - costs.fee_fraction)
        res.trades.append(BtTrade(p.symbol, p.family, p.candidate_id, p.opened_at, t, float(p.entry_price), float(px),
                                  float(p.qty), float(p.cost_tmn), float(proceeds), float(pnl),
                                  float(pnl / p.cost_tmn * 10000), float(fee + entry_fee_tmn), reason, src, mult))
        del positions[p.symbol]
        if reason == "STOP":
            cooldown[p.symbol] = t + rc.cooldown_bars_after_stop * sig_s

    pos_meta: dict[str, tuple] = {}
    for t_open in times:
        t_close = t_open + sig_s
        # --- episode / day bookkeeping (UTC days) ---
        if episode_start is None or t_open - episode_start >= episode_len:
            if episode_start is not None:
                for p in list(positions.values()):     # end-of-episode liquidation at this bar's open
                    close_pos(p, bars[p.symbol][t_open].open if t_open in bars[p.symbol] else p.entry_price, t_open, "LIQUIDATION", *pos_meta.get(p.symbol, ("", "")))
                eq = cash
                res.episodes.append({"start": episode_start, "end": t_open, "start_equity": float(ep_start_equity),
                                     "end_equity": float(eq), "return_pct": float((eq / ep_start_equity - 1) * 100)})
            episode_start, ep_start_equity = t_open, cash
            hwm, latched = cash, False
        d = t_open // 86400
        if d != day:
            day, day_start_equity = d, mark(t_open)

        # --- fill pending entries at this bar's open ---
        for (sym, cand, qty, src, mult) in pending:
            b = bars[sym].get(t_open)
            if b is None or sym in positions:
                continue
            fill = costs.buy_price(b.open)
            if fill <= cand.stop or cash < qty * fill:
                continue
            cost = qty * fill
            cash -= cost
            received = qty * (1 - costs.fee_fraction)
            positions[sym] = BtPosition(sym, cand.family, cand.candidate_id, received, fill, cost, cand.stop,
                                        cand.target_for(fill), t_open, cand.max_holding_bars,
                                        trail_distance=cand.trail_distance, peak=fill)
            pos_meta[sym] = (src, mult)
        pending = []

        # --- exits on this bar (stop first) ---
        for sym, p in list(positions.items()):
            b = bars[sym].get(t_open)
            if b is None:
                continue
            p.bars_held += 1
            if b.low <= p.stop:
                close_pos(p, min(p.stop, b.open), t_close, "TRAIL_STOP" if p.stop > p.entry_price else "STOP", *pos_meta[sym])
            elif b.high >= p.target:
                close_pos(p, max(p.target, b.open), t_close, "TARGET", *pos_meta[sym])
            elif p.bars_held >= p.max_bars:
                close_pos(p, b.close, t_close, "TIME_EXIT", *pos_meta[sym])
            else:
                # ratchet the trailing stop using this bar's high; it applies from the next bar on
                p.peak = max(p.peak, b.high)
                p.stop = trail_stop(p.stop, p.peak, p.trail_distance)

        equity = mark(t_close)
        hwm = max(hwm, equity)
        res.equity.append((t_close, float(equity)))
        exposures = tuple(OpenRisk(p.symbol, p.qty * bars[p.symbol][t_open].close if t_open in bars[p.symbol] else p.cost_tmn,
                                   max(ZERO, p.qty * (bars[p.symbol][t_open].close - p.stop)) if t_open in bars[p.symbol] else ZERO)
                          for p in positions.values())
        state = PortfolioState(equity, cash, exposures, day_start_equity, hwm, entries_halted=latched,
                               halt_reason="drawdown breaker" if latched else None,
                               cooldown_symbols=frozenset(s for s, u in cooldown.items() if u > t_close))
        br = breaker_status(state, rc)
        if br.drawdown_hit and not latched:
            latched = True
            for p in list(positions.values()):
                close_pos(p, bars[p.symbol][t_open].close, t_close, "BREAKER", *pos_meta.get(p.symbol, ("", "")))
            continue
        # entries are not allowed in the last 3 hours of an episode (mirrors the live schedule)
        if t_close - episode_start > episode_len - settings.competition.halt_entries_before_end_minutes * 60:
            continue

        # --- decision ---
        analyses = []
        for sym, (sig, ctx) in series.items():
            # slice windows ending at t_close for speed; analyze() re-filters to closed bars
            i_end = _bisect_right(sig, t_close - sig_s)
            j_end = _bisect_right(ctx, t_close - mc.context_seconds)
            analyses.append(analyze(sym, sig[max(0, i_end - 200):i_end], ctx[max(0, j_end - 100):j_end], t_close, sc,
                                    sig_s, mc.context_seconds, mc.warmup_bars))
        analyses = apply_market_filter(analyses, settings.strategy.market_filter_symbol)
        if not any(a.candidates for a in analyses):
            continue
        portfolio = {"equity_tmn": f"{equity:.0f}", "free_tmn": f"{cash:.0f}",
                     "open_positions": [{"symbol": p.symbol, "family": p.family, "bars_held": p.bars_held} for p in positions.values()],
                     "drawdown_fraction": f"{br.drawdown_fraction:.4f}", "daily_loss_fraction": f"{br.daily_loss_fraction:.4f}"}
        feedback = recent_feedback(res.trades, settings.ai.feedback_trades)
        minutes_left = (episode_len - (t_close - episode_start)) / 60 - settings.competition.halt_entries_before_end_minutes
        ctxd = build_context(as_of=t_close, analyses=analyses, portfolio=portfolio, edge=edge, feedback=feedback,
                             minutes_to_entry_cutoff=round(minutes_left))
        dec = selector.select(ctxd)
        base = baseline.select(ctxd)
        verdict = None
        if dec.action == "SELECT":
            cand = next(c for a in analyses for c in a.candidates if c.candidate_id == dec.candidate_id)
            close = bars[cand.symbol][t_open].close
            verdict = evaluate_entry(symbol=cand.symbol, stop=cand.stop, max_stop_fraction=D(sc.stop_distance_max_fraction),
                                     min_stop_fraction=D(sc.stop_distance_min_fraction), multiplier=D(dec.multiplier),
                                     state=state, market=_market(cand.symbol), book=_synthetic_book(cand.symbol, close, costs, t_close),
                                     now=datetime.fromtimestamp(t_close, timezone.utc), fee_fraction=costs.fee_fraction,
                                     risk=rc, mcfg=mc, allowlist=None, phase="TRADING")
            if verdict.approved:
                pending.append((cand.symbol, cand, verdict.quantity, dec.source, dec.multiplier))
        if record_decisions:
            res.decisions.append({"t": t_close, "snapshot_id": ctxd["snapshot_id"],
                                  "candidates": [c["candidate_id"] for c in ctxd["candidates"]],
                                  "action": dec.action, "candidate_id": dec.candidate_id, "multiplier": dec.multiplier,
                                  "source": dec.source, "regime": dec.market_regime, "confidence": dec.confidence,
                                  "reason": dec.reason, "error": dec.error,
                                  "baseline_candidate_id": base.candidate_id,
                                  "risk": None if verdict is None else {"approved": verdict.approved, "reason": verdict.reason}})
    # close out
    if times:
        last = times[-1]
        for p in list(positions.values()):
            b = bars[p.symbol].get(last)
            close_pos(p, b.close if b else p.entry_price, last + sig_s, "LIQUIDATION", *pos_meta.get(p.symbol, ("", "")))
        if episode_start is not None:
            res.episodes.append({"start": episode_start, "end": last + sig_s, "start_equity": float(ep_start_equity),
                                 "end_equity": float(cash), "return_pct": float((cash / ep_start_equity - 1) * 100), "partial": True})
    return res


def _bisect_right(candles: list[Candle], open_time: int) -> int:
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid].open_time <= open_time:
            lo = mid + 1
        else:
            hi = mid
    return lo


def recent_feedback(trades: list, n: int) -> dict:
    last = trades[-n:]
    by_fam: dict[str, dict] = {}
    for t in last:
        f = by_fam.setdefault(t.family, {"trades": 0, "wins": 0, "net_bps_sum": 0.0})
        f["trades"] += 1
        f["wins"] += t.pnl_tmn > 0
        f["net_bps_sum"] += t.net_bps
    for f in by_fam.values():
        f["mean_net_bps"] = round(f.pop("net_bps_sum") / f["trades"], 1)
    return {"window_trades": len(last), "by_family": by_fam,
            "last_exits": [{"symbol": t.symbol, "family": t.family, "exit": t.exit_reason, "net_bps": round(t.net_bps, 1)} for t in last[-5:]]}


def metrics(res: BtResult, capital: float) -> dict:
    tr = res.trades
    pnl = [t.pnl_tmn for t in tr]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    eq = [e for _, e in res.equity] or [capital]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    ep = [e["return_pct"] for e in res.episodes if not e.get("partial")]
    fam: dict[str, dict] = {}
    for t in tr:
        f = fam.setdefault(t.family, {"trades": 0, "net_pnl_tmn": 0.0, "wins": 0, "net_bps": []})
        f["trades"] += 1
        f["net_pnl_tmn"] += t.pnl_tmn
        f["wins"] += t.pnl_tmn > 0
        f["net_bps"].append(t.net_bps)
    for f in fam.values():
        f["mean_net_bps"] = round(statistics.mean(f["net_bps"]), 1)
        f["win_rate"] = round(f["wins"] / f["trades"], 3)
        f["net_pnl_tmn"] = round(f["net_pnl_tmn"])
        del f["net_bps"], f["wins"]
    dec = res.decisions
    out = {
        "label": res.label,
        "net_pnl_tmn": round(sum(pnl)),
        "net_return_pct": round(sum(pnl) / capital * 100, 3),
        "max_drawdown_pct": round(mdd * 100, 3),
        "trades": len(tr),
        "win_rate": round(len(wins) / len(tr), 3) if tr else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "avg_pnl_per_trade_tmn": round(sum(pnl) / len(tr)) if tr else None,
        "avg_net_bps_per_trade": round(statistics.mean(t.net_bps for t in tr), 1) if tr else None,
        "fees_tmn": round(sum(t.fees_tmn for t in tr)),
        "exit_reasons": _count(t.exit_reason for t in tr),
        "by_family": fam,
        "episodes": len(ep),
        "episode_return_pct": {"mean": round(statistics.mean(ep), 3), "median": round(statistics.median(ep), 3),
                               "worst": round(min(ep), 3), "best": round(max(ep), 3),
                               "positive_share": round(sum(e > 0 for e in ep) / len(ep), 3)} if ep else None,
        "decision_points": len(dec),
        "selected": sum(d["action"] == "SELECT" for d in dec),
        "held": sum(d["action"] == "HOLD" for d in dec),
        "selector_sources": _count(d["source"] for d in dec),
        "differs_from_baseline": sum(d["candidate_id"] != d["baseline_candidate_id"] for d in dec),
    }
    return out


def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def edge_artifact(res: BtResult, period: tuple[int, int], seed: int = 7) -> dict:
    """Per-family held-out statistics with a bootstrap 5% lower bound on mean net bps."""
    rng = random.Random(seed)
    out = {}
    fams: dict[str, list[float]] = {}
    for t in res.trades:
        fams.setdefault(t.family, []).append(t.net_bps)
    for fam, xs in fams.items():
        boots = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(1000)) if len(xs) > 1 else [xs[0]]
        wins = [x for x in xs if x > 0]
        losses = [x for x in xs if x <= 0]
        out[fam] = {"trades": len(xs), "mean_net_bps": round(statistics.mean(xs), 1),
                    "lcb5_net_bps": round(boots[int(0.05 * len(boots))], 1),
                    "win_rate": round(len(wins) / len(xs), 3),
                    "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
                    "period_utc": [period[0], period[1]], "source": "proxy backtest (train split)"}
    return out


def equity_svg(curves: dict[str, list], width=900, height=260) -> str:
    """Tiny dependency-free SVG equity chart for the human-readable report."""
    pts = [e for c in curves.values() for _, e in c]
    if not pts:
        return ""
    lo, hi = min(pts), max(pts)
    t0 = min(c[0][0] for c in curves.values() if c)
    t1 = max(c[-1][0] for c in curves.values() if c)
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea"]
    lines = []
    for k, (name, c) in enumerate(curves.items()):
        step = max(1, len(c) // 600)
        path = " ".join(f"{(t - t0) / max(1, t1 - t0) * width:.1f},{height - (e - lo) / max(1e-9, hi - lo) * height:.1f}"
                        for t, e in c[::step])
        lines.append(f'<polyline fill="none" stroke="{colors[k % 4]}" stroke-width="1.5" points="{path}"/>'
                     f'<text x="8" y="{16 + 16 * k}" fill="{colors[k % 4]}" font-size="12">{name}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{"".join(lines)}</svg>'


def write_report(path_base: Path, payload: dict) -> None:
    path_base.parent.mkdir(parents=True, exist_ok=True)
    path_base.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str))
