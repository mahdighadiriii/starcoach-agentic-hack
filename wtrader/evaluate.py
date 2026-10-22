"""Evaluation protocol: chronological train/test split, edge artifact from TRAIN only,
deterministic baseline vs AI-enhanced selection on TEST, plus fee sensitivity.

Produces reports/evaluation.json (machine-readable) and reports/EVALUATION.md (human-readable).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .ai_agent import BaselineSelector, LLMSelector, prompt_hash
from .llm import effective_provider
from .backtest import CostModel, edge_artifact, equity_svg, load_series, metrics, run_backtest
from .config import Settings

D = Decimal


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def run_evaluation(s: Settings, *, data_root: Path, source: str, symbols: list[str], train_days: int,
                   with_ai: bool, ai_replay_only: bool = False, out_dir: Path = Path("reports"),
                   fee_grid=("0.0035", "0.0025", "0.0015", "0")) -> dict:
    if with_ai and effective_provider(s.ai) == 'baseline':
        raise ValueError('--ai requires AI_MODE=ai and an LLM provider; baseline never makes LLM calls')
    mc = s.market
    series = load_series(data_root, source, symbols, mc.signal_seconds, mc.context_seconds)
    ref = series[symbols[0]][0]
    t0, t_end = ref[0].open_time, ref[-1].open_time + mc.signal_seconds
    t_split = t0 + train_days * 86400
    cap = float(s.risk.authorized_capital_tmn)
    costs = CostModel(fee_fraction=D(s.execution.taker_fee_fraction))
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "data_source": source,
                 "proxy": source != "wallex", "symbols": symbols,
                 "train": [_iso(t0), _iso(t_split)], "test": [_iso(t_split), _iso(t_end)],
                 "families": s.strategy.enabled_families, "timeframes_s": [mc.signal_seconds, mc.context_seconds],
                 "cost_model": {"fee_fraction": str(costs.fee_fraction), "half_spread_bps": str(costs.half_spread_bps),
                                "slippage_bps": str(costs.slippage_bps)},
                 "config_hash": s.config_hash(), "results": {}}

    train = run_backtest(s, series, t0, t_split, BaselineSelector(), costs, {}, "baseline_train", enforce_edge_gate=False)
    edge = edge_artifact(train, (t0, t_split))
    art = {"families": edge, "created_from": "train split, baseline selector", "train": out["train"],
           "data_source": source, "config_hash": s.config_hash()}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "edge_artifact.json").write_text(json.dumps(art, indent=2))
    out["results"]["baseline_train"] = metrics(train, cap)
    out["edge_artifact"] = edge

    base_test = run_backtest(s, series, t_split, t_end, BaselineSelector(), costs, edge, "baseline_test")
    out["results"]["baseline_test"] = metrics(base_test, cap)
    curves = {"baseline (test)": base_test.equity}
    ai_test = None
    if with_ai:
        sel = LLMSelector(s.ai, cache_dir=out_dir / "ai_cache", replay_only=ai_replay_only)
        ai_test = run_backtest(s, series, t_split, t_end, sel, costs, edge, "ai_test")
        out["results"]["ai_test"] = metrics(ai_test, cap)
        out["ai"] = {"provider": s.ai.provider, "model": s.ai.model, "usage": sel.stats(), "prompt_version": s.ai.prompt_version, "prompt_sha256_16": prompt_hash(s.ai.prompt_version),
                     "effort": s.ai.effort, "failures": sel.failures, "cache_dir": str(out_dir / "ai_cache")}
        out["ai_changes"] = ai_change_analysis(base_test, ai_test)
        curves["AI-enhanced (test)"] = ai_test.equity
        (out_dir / "ai_decisions_test.json").write_text(json.dumps(ai_test.decisions, indent=1, default=str))
    sens = {}
    for fee in fee_grid:
        cm = CostModel(fee_fraction=D(fee))
        r = run_backtest(s, series, t_split, t_end, BaselineSelector(), cm, edge, f"fee_{fee}", record_decisions=False)
        m = metrics(r, cap)
        sens[fee] = {k: m[k] for k in ("net_return_pct", "max_drawdown_pct", "trades", "profit_factor", "avg_net_bps_per_trade")}
    out["fee_sensitivity_test_baseline"] = sens
    (out_dir / "equity_test.svg").write_text(equity_svg(curves))
    (out_dir / "evaluation.json").write_text(json.dumps(out, indent=2, default=str))
    (out_dir / "trades_test_baseline.json").write_text(json.dumps([t.__dict__ for t in base_test.trades], indent=1))
    if ai_test:
        (out_dir / "trades_test_ai.json").write_text(json.dumps([t.__dict__ for t in ai_test.trades], indent=1))
    (out_dir / "EVALUATION.md").write_text(render_markdown(out))
    return out


def ai_change_analysis(base, ai) -> dict:
    """Where did the AI change the deterministic choice, and what happened to those trades?"""
    by_snapshot = {d["snapshot_id"]: d for d in ai.decisions}
    changed = [d for d in ai.decisions if d["candidate_id"] != d["baseline_candidate_id"]]
    vetoed = [d for d in changed if d["action"] == "HOLD" and d["baseline_candidate_id"]]
    switched = [d for d in changed if d["action"] == "SELECT"]
    halved = [d for d in ai.decisions if d["action"] == "SELECT" and d["multiplier"] == "0.5"]
    base_by_cand = {t.candidate_id: t for t in base.trades}
    ai_by_cand = {t.candidate_id: t for t in ai.trades}
    vetoed_outcomes = [base_by_cand[d["baseline_candidate_id"]].pnl_tmn for d in vetoed if d["baseline_candidate_id"] in base_by_cand]
    return {
        "decision_points": len(ai.decisions), "changed_vs_baseline": len(changed),
        "vetoed_baseline_entries": len(vetoed), "switched_candidate": len(switched), "half_size_selections": len(halved),
        "vetoed_entries_that_baseline_traded": len(vetoed_outcomes),
        "baseline_pnl_of_vetoed_trades_tmn": round(sum(vetoed_outcomes)),
        "vetoed_trades_that_were_losers": sum(p <= 0 for p in vetoed_outcomes),
        "note": "Baseline PnL of vetoed trades is path-dependent (portfolio state differs between runs); treat as indicative.",
        "examples": [{k: d[k] for k in ("t", "candidate_id", "baseline_candidate_id", "action", "multiplier", "regime", "confidence", "reason")}
                     for d in changed[:8]],
        "fallback_count": sum(d["source"].startswith("fallback") for d in ai.decisions),
        "_unused": len(by_snapshot) - len(ai.decisions),
    }


def render_markdown(o: dict) -> str:
    r = o["results"]
    cols = ["net_return_pct", "net_pnl_tmn", "max_drawdown_pct", "trades", "win_rate", "profit_factor",
            "avg_pnl_per_trade_tmn", "avg_net_bps_per_trade", "fees_tmn"]
    lines = [f"# Backtest evaluation", "",
             f"Generated {o['generated_at']}. Data source: **{o['data_source']}**" + (" (PROXY — not Wallex prices or fills)" if o["proxy"] else ""),
             f"Train {o['train'][0]} → {o['train'][1]} · Test (held out) {o['test'][0]} → {o['test'][1]}",
             f"Families: {', '.join(o['families'])} · timeframes {o['timeframes_s']} s · cost model {o['cost_model']}", "",
             "| run | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for k, m in r.items():
        lines.append(f"| {k} | " + " | ".join(str(m.get(c)) for c in cols) + " |")
    lines += ["", "## 3-day episode returns (each episode = one simulated competition)", "",
              "| run | episodes | mean % | median % | worst % | best % | share positive |", "|---|---|---|---|---|---|---|"]
    for k, m in r.items():
        e = m.get("episode_return_pct") or {}
        lines.append(f"| {k} | {m.get('episodes')} | {e.get('mean')} | {e.get('median')} | {e.get('worst')} | {e.get('best')} | {e.get('positive_share')} |")
    lines += ["", "## Edge artifact (train split, consumed by the selector on test)", "", "```json", json.dumps(o["edge_artifact"], indent=1), "```"]
    if "ai_changes" in o:
        c = o["ai_changes"]
        lines += ["", "## Where the AI changed decisions (test)", "",
                  f"- Decision points with candidates: {c['decision_points']}; AI differed from baseline: {c['changed_vs_baseline']}",
                  f"- Vetoed baseline entries: {c['vetoed_baseline_entries']} (of which baseline actually traded {c['vetoed_entries_that_baseline_traded']}; "
                  f"their baseline PnL {c['baseline_pnl_of_vetoed_trades_tmn']} TMN, losers {c['vetoed_trades_that_were_losers']})",
                  f"- Switched candidate: {c['switched_candidate']}; half-size selections: {c['half_size_selections']}; fallbacks: {c['fallback_count']}",
                  f"- {c['note']}", "", "Examples:", ""]
        for e in c["examples"]:
            lines.append(f"- `{e['candidate_id'] or 'HOLD'}` vs baseline `{e['baseline_candidate_id']}` ×{e['multiplier']} "
                         f"({e['regime']}, conf {e['confidence']}): {e['reason']}")
    lines += ["", "## Fee sensitivity (test, baseline)", "", "| taker fee | net % | max DD % | trades | PF | avg bps |", "|---|---|---|---|---|---|"]
    for fee, m in o["fee_sensitivity_test_baseline"].items():
        lines.append(f"| {fee} | {m['net_return_pct']} | {m['max_drawdown_pct']} | {m['trades']} | {m['profit_factor']} | {m['avg_net_bps_per_trade']} |")
    lines += ["", "![equity](equity_test.svg)", "",
              "Limitations: proxy prices/liquidity differ from Wallex TMN books; fills are modeled, not observed; "
              "a single chronological split is a small sample; the selected configuration was chosen on the train split only."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- short-duration comparison
SCALP_VARIANTS = {
    # name: strategy overrides (the first is the operator's hypothesis: SL 0.7%, TP 1.2%, <= 30 min)
    "scalp_momentum_sl0.7_tp1.2": {"enabled_families": ["SCALP_MOMENTUM"]},
    "scalp_reversion_sl0.7_tp1.2": {"enabled_families": ["SCALP_REVERSION"]},
    "scalp_both_sl0.7_tp1.2": {"enabled_families": ["SCALP_MOMENTUM", "SCALP_REVERSION"]},
    "scalp_momentum_sl0.5_tp0.9": {"enabled_families": ["SCALP_MOMENTUM"], "scalp_stop_fraction": "0.005",
                                   "scalp_target_fraction": "0.009"},
    "scalp_momentum_sl1.0_tp1.8": {"enabled_families": ["SCALP_MOMENTUM"], "scalp_stop_fraction": "0.010",
                                   "scalp_target_fraction": "0.018"},
    "scalp_momentum_no_cost_gate": {"enabled_families": ["SCALP_MOMENTUM"], "_risk": {"min_net_target_bps": "-100000"}},
}
FEE_GRID = ("0.0035", "0.0025", "0.0020", "0.0010", "0")


def _settings_from(path: str, overrides: dict | None = None) -> Settings:
    from .config import load_settings
    s = load_settings(path)
    s.strategy.edge_artifact = None
    for k, v in (overrides or {}).items():
        if k == "_risk":
            for rk, rv in v.items():
                setattr(s.risk, rk, rv)
        else:
            setattr(s.strategy, k, v)
    return s


def _holding_minutes(res) -> dict:
    hm = sorted((t.closed_at - t.opened_at) / 60 for t in res.trades)
    if not hm:
        return {}
    return {"median": round(hm[len(hm) // 2], 1), "mean": round(sum(hm) / len(hm), 1), "max": round(hm[-1], 1)}


def run_comparison(*, data_root: Path, source: str, symbols: list[str], train_days: int, out_dir: Path,
                   baseline_config: str, scalp_config: str, fill_delay_s: int = 60, min_train_trades: int = 20) -> dict:
    from .data import load_frame
    import time as _time
    t_start = _time.time()
    base_s = _settings_from(baseline_config)
    scalp_s = _settings_from(scalp_config)
    filt = symbols[0]                     # BTC first: market filter reference
    base_s.strategy.market_filter_symbol = scalp_s.strategy.market_filter_symbol = filt
    one_min = {s: load_frame(data_root, source, s, 60) for s in symbols}
    t0 = max(c[0].open_time for c in one_min.values())
    t_end = min(c[-1].open_time for c in one_min.values()) + 60
    t_split = t0 + train_days * 86400
    cap = float(base_s.risk.authorized_capital_tmn)
    from .data import resample

    def frames(s):
        return {sym: (resample(one_min[sym], 60, s.market.signal_seconds), resample(one_min[sym], 60, s.market.context_seconds))
                for sym in symbols}

    base_series, scalp_series = frames(base_s), frames(scalp_s)
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "data_source": source, "proxy": source != "wallex",
                 "symbols": symbols, "train": [_iso(t0), _iso(t_split)], "test": [_iso(t_split), _iso(t_end)],
                 "allocation_tmn": base_s.risk.authorized_capital_tmn, "max_order_tmn": base_s.risk.max_order_tmn,
                 "cost_model": {"half_spread_bps": "8", "slippage_bps": "5", "entry_latency_s_scalp": fill_delay_s},
                 "train_results": {}, "test_results": {}, "fee_sensitivity_test": {}, "spread_sensitivity_test": {}}

    def bt(s, series, a, b, fee, label, half_spread="8", scalp=True):
        cm = CostModel(fee_fraction=D(fee), half_spread_bps=D(half_spread))
        kw = dict(exit_series=one_min, exit_s=60, fill_delay_s=fill_delay_s) if scalp else {}
        r = run_backtest(s, series, a, b, BaselineSelector(), cm, {}, label, record_decisions=False, enforce_edge_gate=False, **kw)
        m = metrics(r, cap)
        m["holding_minutes"] = _holding_minutes(r)
        days = max(1e-9, (b - a) / 86400)
        m["trades_per_day"] = round(len(r.trades) / days, 2)
        return r, m

    primary = "0.0035"
    _, out["train_results"]["baseline_donchian_1h"] = bt(base_s, base_series, t0, t_split, primary, "baseline_train", scalp=False)
    variants = {}
    for name, ov in SCALP_VARIANTS.items():
        s = _settings_from(scalp_config, ov)
        s.strategy.market_filter_symbol = filt
        r, m = bt(s, scalp_series, t0, t_split, primary, name)
        out["train_results"][name] = m
        variants[name] = (s, r, m)
    # selection on TRAIN only, among cost-gated variants
    # a variant that (almost) never trades "wins" by doing nothing; require a minimum sample to be selectable
    eligible = {k: v for k, v in variants.items() if "no_cost_gate" not in k and v[2]["trades"] >= min_train_trades}
    best = max(eligible, key=lambda k: (eligible[k][2]["net_pnl_tmn"], -eligible[k][2]["max_drawdown_pct"])) \
        if eligible else "scalp_momentum_sl0.7_tp1.2"
    out["selection_rule"] = (f"highest train net P&L among cost-gated variants with >= {min_train_trades} train trades; "
                             "falls back to the operator hypothesis when none qualifies")
    out["selected_on_train"] = best
    best_s, best_train, _ = variants[best]
    art = {"families": edge_artifact(best_train, (t0, t_split)), "created_from": f"train split, {best}",
           "train": out["train"], "data_source": source}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "edge_artifact_scalp.json").write_text(json.dumps(art, indent=2))

    # held-out TEST, evaluated once per configuration
    _, out["test_results"]["baseline_donchian_1h"] = bt(base_s, base_series, t_split, t_end, primary, "baseline_test", scalp=False)
    best_test, out["test_results"][best] = bt(best_s, scalp_series, t_split, t_end, primary, best)
    hyp = "scalp_momentum_sl0.7_tp1.2"
    if best != hyp:
        _, out["test_results"][hyp] = bt(variants[hyp][0], scalp_series, t_split, t_end, primary, hyp)
    ng = variants["scalp_momentum_no_cost_gate"][0]
    _, out["test_results"]["scalp_momentum_no_cost_gate"] = bt(ng, scalp_series, t_split, t_end, primary, "no_gate")
    keys = ("net_return_pct", "net_pnl_tmn", "max_drawdown_pct", "trades", "win_rate", "profit_factor", "avg_net_bps_per_trade",
            "fees_tmn", "trades_per_day")
    for fee in FEE_GRID:
        row = {}
        _, mb = bt(base_s, base_series, t_split, t_end, fee, "b", scalp=False)
        _, ms = bt(best_s, scalp_series, t_split, t_end, fee, "s")
        row["baseline_donchian_1h"] = {k: mb[k] for k in keys}
        row[best] = {k: ms[k] for k in keys}
        out["fee_sensitivity_test"][fee] = row
    for hs in ("4", "8", "15", "25"):
        _, ms = bt(best_s, scalp_series, t_split, t_end, primary, "sp", half_spread=hs)
        out["spread_sensitivity_test"][hs] = {k: ms[k] for k in keys}
    (out_dir / "trades_test_scalp.json").write_text(json.dumps([t.__dict__ for t in best_test.trades], indent=1))
    out["runtime_s"] = round(_time.time() - t_start)
    (out_dir / "short_duration_comparison.json").write_text(json.dumps(out, indent=2, default=str))
    (out_dir / "SHORT_DURATION_COMPARISON.md").write_text(render_comparison(out))
    return out


def render_comparison(o: dict) -> str:
    cols = ["net_return_pct", "net_pnl_tmn", "max_drawdown_pct", "trades", "trades_per_day", "win_rate", "profit_factor",
            "avg_net_bps_per_trade", "fees_tmn"]

    def table(rows: dict) -> list[str]:
        out = ["| run | " + " | ".join(cols) + " | hold min (median/max) |", "|" + "---|" * (len(cols) + 2)]
        for k, m in rows.items():
            h = m.get("holding_minutes") or {}
            out.append(f"| {k} | " + " | ".join(str(m.get(c)) for c in cols) + f" | {h.get('median')}/{h.get('max')} |")
        return out

    lines = ["# Short-duration vs baseline (backtest)", "",
             f"Generated {o['generated_at']}. Data: **{o['data_source']}**" +
             (" — PROXY (KuCoin USDT 1-minute candles), not Wallex TMN prices, spreads or fills." if o["proxy"] else ""),
             f"Train {o['train'][0]} → {o['train'][1]} · Test (held out, evaluated once) {o['test'][0]} → {o['test'][1]}",
             f"Allocation {o['allocation_tmn']} TMN, max order {o['max_order_tmn']} TMN, max 2 positions, 3-day episodes. "
             f"Costs per side: taker fee (grid below; primary 0.35%), half-spread 8 bps, slippage 5 bps; "
             f"short-duration entries fill {o['cost_model']['entry_latency_s_scalp']} s after the decision; exits on 1-minute bars, stop first.",
             "", "## Train (selection only)", ""] + table(o["train_results"]) + [
             "", f"Selected on train: **{o['selected_on_train']}** — rule: {o['selection_rule']}.",
             "", "## Held-out test (primary fee 0.35%/side)", ""] + table(o["test_results"]) + [
             "", "## Fee sensitivity (held-out test)", "", "| taker fee/side | run | net % | trades | win rate | PF | avg net bps |",
             "|---|---|---|---|---|---|---|"]
    for fee, row in o["fee_sensitivity_test"].items():
        for k, m in row.items():
            lines.append(f"| {fee} | {k} | {m['net_return_pct']} | {m['trades']} | {m['win_rate']} | {m['profit_factor']} | {m['avg_net_bps_per_trade']} |")
    lines += ["", f"## Spread sensitivity (held-out test, {o['selected_on_train']}, fee 0.35%)", "",
              "| half-spread bps | net % | trades | win rate | PF | avg net bps |", "|---|---|---|---|---|---|"]
    for hs, m in o["spread_sensitivity_test"].items():
        lines.append(f"| {hs} | {m['net_return_pct']} | {m['trades']} | {m['win_rate']} | {m['profit_factor']} | {m['avg_net_bps_per_trade']} |")
    lines += ["", "Limitations: proxy USDT data (Wallex TMN books are thinner and spreads wider); fills are modeled, not observed; "
              "a single chronological split over a few months is a small sample; no AI selector in these runs (baseline selector)."]
    return "\n".join(lines) + "\n"
