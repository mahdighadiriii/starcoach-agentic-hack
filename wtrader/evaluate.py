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
from .backtest import CostModel, edge_artifact, equity_svg, load_series, metrics, run_backtest
from .config import Settings

D = Decimal


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def run_evaluation(s: Settings, *, data_root: Path, source: str, symbols: list[str], train_days: int,
                   with_ai: bool, ai_replay_only: bool = False, out_dir: Path = Path("reports"),
                   fee_grid=("0.0035", "0.0025", "0.0015", "0")) -> dict:
    mc = s.market
    series = load_series(data_root, source, symbols, mc.signal_seconds, mc.context_seconds)
    ref = series[symbols[0]][0]
    t0, t_end = ref[0].open_time, ref[-1].open_time + mc.signal_seconds
    t_split = t0 + train_days * 86400
    cap = float(s.competition.initial_capital_tmn)
    costs = CostModel(fee_fraction=D(s.execution.taker_fee_fraction))
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "data_source": source,
                 "proxy": source != "wallex", "symbols": symbols,
                 "train": [_iso(t0), _iso(t_split)], "test": [_iso(t_split), _iso(t_end)],
                 "families": s.strategy.enabled_families, "timeframes_s": [mc.signal_seconds, mc.context_seconds],
                 "cost_model": {"fee_fraction": str(costs.fee_fraction), "half_spread_bps": str(costs.half_spread_bps),
                                "slippage_bps": str(costs.slippage_bps)},
                 "config_hash": s.config_hash(), "results": {}}

    train = run_backtest(s, series, t0, t_split, BaselineSelector(), costs, {}, "baseline_train")
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
        out["ai"] = {"model": s.ai.model, "prompt_version": s.ai.prompt_version, "prompt_sha256_16": prompt_hash(s.ai.prompt_version),
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
