# Competition readiness (300 points)

**Status: NOT live-ready.** Live mode is gated in code (`cli.live_gate_failures`) and was never enabled. Unit tests passing ≠ live readiness.

| Criterion (pts) | Evidence in this repo | Gap |
|---|---|---|
| Net realized profit (90) | Terminal liquidation to TMN, fee-aware sizing, final report | **No demonstrated edge**: held-out −3.3% at 0.35% fee (`reports/EVALUATION.md`). Viability depends on the real fee |
| Drawdown control (30) | 0.5% risk/trade, 2% daily halt, 4% latching breaker, 60% exposure cap; 3-day episodes worst −1.26% on test | Real-book slippage on exits untested |
| Trading activity (15) | Hourly decisions incl. logged HOLDs; ~46 trades / 80 days on test ≈ 1.7 per 3 days | Activity metric undefined by organizers; low trade count may score poorly |
| Runtime stability (15) | Exceptions caught per tick, bootstrap retry, docker restart policy, alerts, status heartbeat | No multi-day soak test yet |
| Strategy quality (40) | Explicit rules, documented selection on train only, negative results disclosed (`STRATEGY.md`) | Weak edge |
| AI involvement (35) | Claude selector chooses candidate/HOLD/half size, regime, rationale, evidence ids; bounded feedback from recent trades; strict validation | **AI evaluation not run** (no API key) |
| Risk & capital (30) | `risk.py` gate, every check persisted with the decision | — |
| Reproducibility (25) | Manifest (commit, config/prompt/source hashes, model), cached AI decisions, deterministic backtests | Repo not yet committed → `git_commit: null` |
| Backtest & shadow (20) | Train/test protocol, fee sensitivity, episode distribution; shadow on proxy | Backtest and shadow on **proxy** data only; Wallex history/shadow pending |

## Blockers (cannot be resolved from this machine)

1. `api.wallex.ir` unreachable from dev host → run `probe`, `fetch-data --source wallex`, and Wallex shadow from a reachable host.
2. Competition account: API key, `sub-account-client-id`, whether it is a trade-credit package (daily fee, liquidation level).
3. Organizer facts: exact start/end (Asia/Tehran), allowed markets, fee schedule, activity definition, whether HOLDs count, emergency-stop rules.
4. Anthropic API key for the AI evaluation and shadow-with-AI.
5. Explicit authorization before any live order.

## Live readiness checklist (in order)

- [ ] `git init` history: commit this exact tree; clean working tree (enforced by the live gate).
- [ ] `python -m wtrader probe` on the competition key: markets, units ratio ≈ 1, balances, fees, open orders.
- [ ] Fix any shape mismatch flagged UNVERIFIED in `wallex.py`; add the real responses as fixtures.
- [ ] `fetch-data --source wallex`, rerun `evaluate` (baseline and `--ai`) on Wallex data; decide go/no-go on edge.
- [ ] ≥24 h shadow on Wallex data with AI enabled; review decisions, alerts, restart once mid-run.
- [ ] One authorized minimum-size round trip (buy → partial/complete → sell) to verify fills, fee currency, cancel.
- [ ] Liquidation drill on the real account with minimum size.
- [ ] Set `TRADE_START/TRADE_END`, `WALLEX_ALLOWED_MARKETS`, `LIVE_ENABLED=true` only after the above.
