"""Deterministic candidate generation.

Each family is a pure function of CLOSED candles available at decision time.
The output is a set of candidates with explicit levels; the AI selector may only
choose among them (or HOLD) and the risk gate sizes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from . import indicators as ind
from .config import StrategyCfg
from .models import Candle

# Bump a family's version whenever its entry/exit rules change. The run's strategy version is derived
# from the families actually enabled, so the dashboard and manifest name the code that is running.
FAMILY_VERSIONS = {"DONCHIAN_TREND": "1.0.1", "TREND_PULLBACK": "1.0.1", "CONFIRMED_BREAKOUT": "1.0.1",
                   "RSI_REVERSION": "1.0.1", "SCALP_MOMENTUM": "1.0.1", "SCALP_REVERSION": "1.0.1"}


def strategy_version(families: list[str], signal_seconds: int, context_seconds: int) -> str:
    """e.g. 'scalp-momentum/1.0.0 [5m/1h]'. Unknown families are named with version '?'."""
    def tf(sec):
        return f"{sec // 3600}h" if sec % 3600 == 0 else f"{sec // 60}m"
    fams = "+".join(f"{f.lower().replace('_', '-')}/{FAMILY_VERSIONS.get(f, '?')}" for f in sorted(families))
    return f"{fams} [{tf(signal_seconds)}/{tf(context_seconds)}]"

D = Decimal


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    symbol: str
    family: str
    signal_time: int          # close time of the signal bar (epoch s)
    entry_ref: Decimal        # signal close; executable entry is re-priced from the live book
    stop: Decimal             # absolute stop trigger level
    stop_fraction: Decimal    # (entry_ref - stop) / entry_ref
    target_r: Decimal
    max_holding_bars: int
    features: dict = field(compare=False)
    trail_distance: Decimal | None = None   # absolute trailing distance below the running peak

    def trail_stop(self, current_stop: Decimal, peak: Decimal) -> Decimal:
        return trail_stop(current_stop, peak, self.trail_distance)

    def target_for(self, entry: Decimal) -> Decimal:
        return entry + (entry - self.stop) * self.target_r


def trail_stop(current_stop: Decimal, peak: Decimal, distance: Decimal | None) -> Decimal:
    """Stops only ratchet upward."""
    if distance is None:
        return current_stop
    return max(current_stop, peak - distance)


@dataclass(frozen=True)
class Analysis:
    symbol: str
    as_of: int
    regime: str               # TREND_UP | TREND_DOWN | RANGE | INSUFFICIENT_DATA
    features: dict
    candidates: tuple[Candidate, ...]


def closed(candles: list[Candle], seconds: int, as_of: int) -> list[Candle]:
    """Only bars whose close time is at or before as_of. Prevents look-ahead."""
    return [c for c in candles if c.open_time + seconds <= as_of]


def _f(xs) -> list[float]:
    return [float(x) for x in xs]


def classify_regime(ema20_ctx, ema50_ctx, slope_ctx, atr_pct) -> str:
    if ema20_ctx is None or ema50_ctx is None or slope_ctx is None:
        return "INSUFFICIENT_DATA"
    if ema20_ctx > ema50_ctx and slope_ctx > 0:
        return "TREND_UP"
    if ema20_ctx < ema50_ctx and slope_ctx < 0:
        return "TREND_DOWN"
    return "RANGE"


def analyze(symbol: str, sig: list[Candle], ctx: list[Candle], as_of: int, cfg: StrategyCfg,
            sig_s: int = 900, ctx_s: int = 3600, warmup: int = 120) -> Analysis:
    sig = sorted(closed(sig, sig_s, as_of), key=lambda c: c.open_time)
    ctx = sorted(closed(ctx, ctx_s, as_of), key=lambda c: c.open_time)
    if len(sig) < warmup or len(ctx) < 60:
        return Analysis(symbol, as_of, "INSUFFICIENT_DATA", {"bars_15m": len(sig), "bars_1h": len(ctx)}, ())
    # the latest signal bar must be the one that just closed (no stale data)
    if sig[-1].open_time + sig_s != as_of - as_of % sig_s:
        return Analysis(symbol, as_of, "INSUFFICIENT_DATA", {"stale_last_bar": sig[-1].open_time}, ())

    expected_ctx_close = as_of - as_of % ctx_s
    if ctx[-1].open_time + ctx_s != expected_ctx_close:
        return Analysis(symbol, as_of, "INSUFFICIENT_DATA", {"stale_context_bar": ctx[-1].open_time}, ())
    for rows, interval, n in ((sig, sig_s, warmup), (ctx, ctx_s, 60)):
        tail = rows[-n:]
        if any(b.open_time - a.open_time != interval for a, b in zip(tail, tail[1:])):
            return Analysis(symbol, as_of, "INSUFFICIENT_DATA", {"candle_gaps": True}, ())

    o, h, l, c, v = (_f(getattr(x, k) for x in sig) for k in ("open", "high", "low", "close", "volume"))
    ch = _f(x.close for x in ctx)
    e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
    a = ind.atr(h, l, c, cfg.atr_period)
    r = ind.rsi(c, 14)
    c20, c50 = ind.ema(ch, 20), ind.ema(ch, 50)  # context timeframe
    i = len(c) - 1
    slope_ctx = (c20[-1] - c20[-4]) if c20[-1] is not None and c20[-4] is not None else None
    atr_v = a[i]
    atr_pct = atr_v / c[i] if atr_v else None
    regime = classify_regime(c20[-1], c50[-1], slope_ctx, atr_pct)
    vol_ratio = ind.volume_ratio(v, 20, i)
    hi20 = ind.prior_high(h, 20, i)
    feats = {
        "close": c[i], "ema20": e20[i], "ema50": e50[i], "atr14": atr_v, "atr_pct": atr_pct,
        "rsi14": r[i], "volume_ratio": vol_ratio, "prior_high20": hi20,
        "ema20_ctx": c20[-1], "ema50_ctx": c50[-1], "ema20_ctx_slope3": slope_ctx,
        "ret_1": c[i] / c[i - 1] - 1, "ret_4": c[i] / c[i - 4] - 1, "ret_16": c[i] / c[i - 16] - 1,
        "realized_vol_32": ind.realized_vol(c, 32, i),
        "distance_ema20_atr": (c[i] - e20[i]) / atr_v if atr_v else None,
        "regime": regime,
    }
    scalp = {"SCALP_MOMENTUM", "SCALP_REVERSION"} & set(cfg.enabled_families)
    if scalp:
        feats["prior_high_scalp"] = ind.prior_high(h, cfg.scalp_breakout_lookback, i)
    feats = {k: (round(x, 10) if isinstance(x, float) else x) for k, x in feats.items()}
    cands: list[Candidate] = []
    if atr_v and atr_v > 0 and None not in (e20[i], e50[i], r[i], vol_ratio, hi20):
        signal_time = sig[i].open_time + sig_s
        fams = set(cfg.enabled_families)
        stop_mult = float(cfg.atr_stop_multiplier)

        def make(family: str, stop: float, target_r=None, max_bars=None, trail=None):
            entry = c[i]
            frac = (entry - stop) / entry
            if not float(cfg.stop_distance_min_fraction) <= frac <= float(cfg.stop_distance_max_fraction):
                return
            cands.append(Candidate(
                candidate_id=f"{symbol}-{family}-{signal_time}", symbol=symbol, family=family,
                signal_time=signal_time, entry_ref=D(str(entry)), stop=D(str(round(stop, 12))),
                stop_fraction=D(str(round(frac, 8))), target_r=D(target_r or cfg.target_r_multiple),
                max_holding_bars=max_bars or cfg.max_holding_bars, features=feats,
                trail_distance=D(str(round(trail, 12))) if trail else None))

        uptrend_ctx = regime == "TREND_UP"
        uptrend_15 = e20[i] > e50[i]
        if "TREND_PULLBACK" in fams and uptrend_ctx and uptrend_15:
            touched = l[i] <= e20[i] + 0.25 * atr_v
            if (touched and c[i] > e20[i] and c[i] > o[i]
                    and float(cfg.pullback_rsi_min) <= r[i] <= float(cfg.pullback_rsi_max)
                    and vol_ratio >= float(cfg.min_volume_ratio)):
                make("TREND_PULLBACK", min(l[i] - 0.1 * atr_v, c[i] - stop_mult * atr_v))
        if "CONFIRMED_BREAKOUT" in fams and uptrend_ctx:
            if c[i] > hi20 and vol_ratio >= float(cfg.breakout_volume_ratio) and c[i] - hi20 <= atr_v:
                make("CONFIRMED_BREAKOUT", c[i] - stop_mult * atr_v)
        if "DONCHIAN_TREND" in fams and uptrend_ctx and uptrend_15:
            # trend-following: close at a new 20-bar high above EMA50, trailing ATR stop, no small target
            if c[i] > hi20 and c[i] > e50[i] and vol_ratio >= float(cfg.min_volume_ratio):
                make("DONCHIAN_TREND", c[i] - stop_mult * atr_v, target_r=cfg.trend_target_r,
                     max_bars=cfg.trend_max_holding_bars, trail=float(cfg.trail_atr_multiplier) * atr_v)
        if scalp and uptrend_ctx and atr_pct is not None and atr_pct >= float(cfg.scalp_min_atr_fraction):
            # Short-duration families: fixed-fraction stop and target, exit within scalp_max_holding_bars.
            # The ATR floor skips quiet markets where the target is unlikely to be reached in time.
            sf = float(cfg.scalp_stop_fraction)
            tr = D(cfg.scalp_target_fraction) / D(cfg.scalp_stop_fraction)
            hi_s = feats["prior_high_scalp"]
            if ("SCALP_MOMENTUM" in fams and hi_s is not None and uptrend_15 and c[i] > hi_s and c[i] > e20[i]
                    and c[i] - hi_s <= atr_v and vol_ratio >= float(cfg.scalp_volume_ratio)
                    and r[i] <= float(cfg.scalp_rsi_max)):
                make("SCALP_MOMENTUM", c[i] * (1 - sf), target_r=tr, max_bars=cfg.scalp_max_holding_bars)
            if ("SCALP_REVERSION" in fams and r[i - 1] is not None and r[i - 1] < float(cfg.scalp_reversion_rsi)
                    and c[i] > o[i] and c[i] > c[i - 1]):
                make("SCALP_REVERSION", c[i] * (1 - sf), target_r=tr, max_bars=cfg.scalp_max_holding_bars)
        if "RSI_REVERSION" in fams and uptrend_ctx:
            # oversold dip inside a 1h uptrend, confirmed by a bullish close
            if r[i - 1] is not None and r[i - 1] < 32 and c[i] > o[i] and c[i] > c[i - 1]:
                make("RSI_REVERSION", min(l[i], l[i - 1]) - 0.2 * atr_v)
    # Expose exact unmet prerequisites without changing the signal rules.
    checks = {}
    for fam in cfg.enabled_families:
        cks = {"context_uptrend": regime == "TREND_UP"}
        if fam in ("SCALP_MOMENTUM", "SCALP_REVERSION"):
            cks["atr_floor"] = atr_pct is not None and atr_pct >= float(cfg.scalp_min_atr_fraction)
        if fam in ("SCALP_MOMENTUM", "DONCHIAN_TREND"):
            cks["signal_uptrend"] = e20[i] is not None and e50[i] is not None and e20[i] > e50[i]
            level = feats.get("prior_high_scalp") if fam == "SCALP_MOMENTUM" else hi20
            cks["breakout_close"] = level is not None and c[i] > level
            cks["volume_ratio"] = vol_ratio is not None and vol_ratio >= float(cfg.scalp_volume_ratio if fam == "SCALP_MOMENTUM" else cfg.min_volume_ratio)
            if fam == "SCALP_MOMENTUM":
                cks["rsi_ceiling"] = r[i] is not None and r[i] <= float(cfg.scalp_rsi_max)
                cks["breakout_extension"] = level is not None and atr_v is not None and c[i] - level <= atr_v
        if fam == "SCALP_REVERSION":
            cks["prior_rsi_oversold"] = r[i-1] is not None and r[i-1] < float(cfg.scalp_reversion_rsi)
            cks["bullish_recovery"] = c[i] > o[i] and c[i] > c[i-1]
        checks[fam] = cks
    feats["setup_checks"] = checks
    # one candidate per symbol: keep the first family in configured priority order
    if len(cands) > 1:
        order = {f: n for n, f in enumerate(cfg.enabled_families)}
        cands.sort(key=lambda x: order.get(x.family, 99))
        cands = cands[:1]
    return Analysis(symbol, as_of, regime, feats, tuple(cands))


def apply_market_filter(analyses: list[Analysis], filter_symbol: str | None) -> list[Analysis]:
    """Drop all candidates unless the reference market (e.g. BTCTMN) is in a context uptrend."""
    if not filter_symbol:
        return analyses
    ref = next((a for a in analyses if a.symbol == filter_symbol), None)
    if ref is not None and ref.regime == "TREND_UP":
        return analyses
    return [Analysis(a.symbol, a.as_of, a.regime, {**a.features, "market_filter": "blocked"}, ()) for a in analyses]


def apply_edge_gate(analyses: list[Analysis], edge: dict, mode: str, min_trades: int = 20) -> list[Analysis]:
    """strict: drop candidates of families without a positive validated edge (>= min_trades, mean net bps > 0)."""
    if mode != "strict":
        return analyses

    def ok(fam):
        e = edge.get(fam) or {}
        return e.get("trades", 0) >= min_trades and (e.get("mean_net_bps") or 0) > 0
    return [Analysis(a.symbol, a.as_of, a.regime, {**a.features, "edge_gate": "blocked"} if any(not ok(c.family) for c in a.candidates) else a.features,
                     tuple(c for c in a.candidates if ok(c.family))) for a in analyses]
