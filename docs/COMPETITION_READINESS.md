# Competition readiness (300 points)

## Live-readiness checklist — 2026-10-22 (second session)

**Verdict: NOT safe to activate live.** The safety machinery is implemented and tested, but the Wallex API
has never been exercised, and no strategy has a demonstrated net edge.

| # | Validation step | Result |
|---|---|---|
| 1 | Existing test suite | ✅ all original tests pass (3 updated to the new allocation semantics) |
| 2 | Tests for new risk/allocation rules | ✅ 115 tests total; `test_safety.py`, `test_live_safety.py`, `test_short_duration.py` |
| 3 | Realistic backtests | ✅ on **proxy** data (KuCoin 1m, 90 d, train/test): short-duration rejected; see `STRATEGY.md` |
| 4 | Fees and slippage | ✅ fee grid 0–0.35%, spread grid 4–25 bps, 60 s latency; ⛔ real Wallex fee unknown |
| 5 | Shadow on real Wallex market data | ⛔ not possible from this host; ✅ shadow ran on live KuCoin books (labeled proxy, paper fills): bootstrap, 5-min decisions, kill drill, restart — 0 entries (no candidates in that window) |
| 6 | Exchange connectivity / read-only endpoints | ⛔ `api.wallex.ir` unreachable (TCP timeout); `probe`/`preflight` ready to run |
| 7 | Dashboard metrics vs persisted records | ✅ `test_dashboard_metrics_match_persisted_records`; dashboard served against the live shadow DB |
| 8 | Restart and recovery | ✅ unit tests (positions, UNKNOWN orders, reservations); ✅ shadow restart kept run id, equity series, baselines |
| 9 | Kill switch | ✅ halt/flatten/freeze tests; ✅ drill on the running shadow engine (entries halted, alert, cleared) |
| 10 | This checklist | ✅ |

Before arming, on a host that reaches Wallex:

- [ ] `python -m wtrader probe` and `python -m wtrader --config <profile> preflight` pass; save outputs as fixtures.
- [ ] Confirm balance semantics (`value` vs `locked`), fee unit and level, `openOrders` shape, min order sizes.
- [ ] Decide isolation: preferably a dedicated sub-account holding exactly 1,000,000 TMN; otherwise accept the
      documented limitation (the cap is enforced only by this bot).
- [ ] `fetch-data --source wallex` and re-run `evaluate` / `compare` on Wallex TMN data; arm only a profile whose
      edge artifact is positive (the scalp profile is gated `strict`).
- [ ] ≥ several hours of shadow on Wallex data with the chosen profile and selector; review blocked orders and alerts.
- [ ] Organizer facts: start/end, allowed markets, fee schedule, credit-package daily fee and liquidation level.
- [ ] Explicit operator authorization, then `arm-live` (see `OPERATIONS.md` §5). The first live entry is a real
      order of up to 200,000 TMN: there is no "test order" mode.

## Previous assessment (first session)

**Status: NOT live-ready.** Live mode is gated in code (`cli.live_gate_failures`) and was never enabled. Unit tests passing ≠ live readiness.

| Criterion (pts) | Evidence in this repo | Gap |
|---|---|---|
| Net realized profit (90) | Terminal liquidation to TMN, fee-aware sizing, final report | **No demonstrated edge**: held-out −3.3% at 0.35% fee (`reports/EVALUATION.md`). Viability depends on the real fee |
| Drawdown control (30) | 0.5% risk/trade, 2% daily halt, 4% latching breaker, 60% exposure cap; 3-day episodes worst −1.26% on test | Real-book slippage on exits untested |
| Trading activity (15) | Hourly decisions incl. logged HOLDs; ~46 trades / 80 days on test ≈ 1.7 per 3 days | Activity metric undefined by organizers; low trade count may score poorly |
| Runtime stability (15) | Exceptions caught per tick, bootstrap retry, docker restart policy, alerts, status heartbeat | No multi-day soak test yet |
| Strategy quality (40) | Explicit rules, documented selection on train only, negative results disclosed (`STRATEGY.md`) | Weak edge |
| AI involvement (35) | Provider-neutral selector chooses candidate/HOLD/half size, regime, rationale, evidence ids; bounded feedback from recent trades; strict validation | **AI strategy evaluation not run**; BoxAPI synthetic HOLD connectivity verified |
| Risk & capital (30) | `risk.py` gate, every check persisted with the decision | — |
| Reproducibility (25) | Manifest (commit, config/prompt/source hashes, model), cached AI decisions, deterministic backtests | Repo not yet committed → `git_commit: null` |
| Backtest & shadow (20) | Train/test protocol, fee sensitivity, episode distribution; shadow on proxy | Backtest and shadow on **proxy** data only; Wallex history/shadow pending |

## Blockers (cannot be resolved from this machine)

1. `api.wallex.ir` unreachable from dev host → run `probe`, `fetch-data --source wallex`, and Wallex shadow from a reachable host.
2. Competition account: API key, `sub-account-client-id`, whether it is a trade-credit package (daily fee, liquidation level).
3. Organizer facts: exact start/end (Asia/Tehran), allowed markets, fee schedule, activity definition, whether HOLDs count, emergency-stop rules.
4. Provider credentials for AI evaluation and shadow-with-AI; BoxAPI connectivity is verified, strategy value remains unvalidated.
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
