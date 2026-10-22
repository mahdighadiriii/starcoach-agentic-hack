# Implementation status — 2026-10-22 (updated: short-duration, allocation, live readiness, dashboard)

## Second session additions

Test run: `.venv/bin/python -m pytest -q` → **114 passed** (+36: safety 13, live safety/engine 15, short-duration 7, risk +1).

| Area | Status | Evidence / notes |
|---|---|---|
| Short-duration families `SCALP_MOMENTUM`, `SCALP_REVERSION` (5m signal, 1h context, fixed SL/TP, 30-min max hold, ATR floor) | ✅ | `tests/test_short_duration.py`; profile `config/scalp.yaml` |
| Backtester: 1m intrabar exits, 60 s entry latency, allocation budget | ✅ | determinism + latency + max-hold tests |
| Cost gate (`net_edge_covers_costs`: target net of 2 fees, live spread, exit slippage ≥ 30 bps) | ✅ | `test_net_edge_gate_blocks_trades_that_cannot_cover_costs` |
| Strict allocation 1M of 5M, max order 200k, 2 positions, 30k daily loss — risk gate **and** executor guard | ✅ | `tests/test_safety.py`, `tests/test_live_safety.py` |
| Bot-scoped equity (other assets never counted, sold or modified; external baseline) | ✅ | `test_external_tmn_movement_is_not_a_bot_loss`, `test_liquidation_never_sells_pre_existing_assets` |
| Unresolved orders keep full reservation across retries/restarts | ✅ | `test_unknown_submission_keeps_reservation_and_blocks_across_restart` |
| Foreign open orders: reserved, entries halted, never canceled | ✅ | `test_foreign_open_order_halts_entries_is_reserved_and_never_canceled` |
| Kill switch halt / flatten / freeze, immediate at the executor | ✅ | 4 engine tests + shadow drill (engaged, alert, cleared) |
| API outage: cached balances for exits, entries halt when stale; stale market data halts entries | ✅ | 2 engine tests |
| Live activation: preflight → arm-live (typed phrase, expiry, bound to config/commit/amount) → live re-checks; separate live DB | ✅ (mock client) | `test_live_refuses_without_operator_arm`, 3 preflight tests; **never run against Wallex** |
| Dashboard (HTML + JSON + CSV), metrics from SQLite | ✅ | `test_dashboard_metrics_match_persisted_records`, `test_trade_stats_hand_checked`; served and rendered against a live shadow run |
| KuCoin 1m proxy data + `KucoinProxyMarketData` for shadow | ✅ | 90 days × 8 markets downloaded; shadow ran on live KuCoin books |
| Wallex real-API verification | ⛔ | `api.wallex.ir` unreachable from the dev host (see `WALLEX_INTEGRATION.md`) |

## First session

Test run: `.venv/bin/python -m pytest -q` → **78 passed** (strategy 10, risk 10, AI 25, executor 9, engine 7, Wallex adapter 13, backtest 4). All exchange interaction in tests is mocked or simulated; no real account was used.

Legend: ✅ implemented and tested · 🟡 implemented, untested or tested only against mocks · 🔵 simulated · ⛔ blocked · ❌ not implemented

| Area | Status | Evidence / notes |
|---|---|---|
| Indicators, strategy families, regime, market filter, trailing stop | ✅ | `tests/test_strategy.py` (incl. future-bar invariance) |
| Resampling 1h→4h, closed-candle filter | ✅ | complete aligned buckets only |
| Risk gate (sizing, caps, whitelist, spread, staleness, slippage, balance, min order, breakers, phases) | ✅ | `tests/test_risk.py` |
| Exit priority (liquidation/breaker > stop > target > time) | ✅ | never consults AI |
| AI selector, strict validation, prompt-injection containment, timeouts, refusal, budget, outage policy, cache replay | ✅ (fake model) | `tests/test_ai.py`; **BoxAPI synthetic structured HOLD verified (2026-10-23)**; no real-model strategy evaluation yet |
| Order FSM, persisted intent, deterministic client_id, no blind retry, UNKNOWN→reconcile, QUARANTINED, partial fills, cancel race, late fills, idempotent re-observation | ✅ | `tests/test_executor.py` |
| Restart recovery (positions/orders from DB, reconcile with venue) | ✅ | `tests/test_engine.py::test_restart_recovery_then_stop_exit` |
| Drawdown breaker (latching flatten), daily loss halt | ✅ | engine tests |
| Competition schedule: entry halt T−180 min, liquidation T−60 min, residual sweep, final report with dust separated | ✅ (paper) | `test_final_liquidation_converts_everything_to_tmn` |
| Bootstrap retry on transient failures | ✅ | added after a silent shadow failure was found |
| Single-writer lock | ✅ | `test_single_writer_lock` |
| Wallex REST adapter | 🟡 | mocked contract tests; ⛔ `api.wallex.ir` unreachable from dev host |
| Shadow mode | 🔵 ✅ | runs on live **Binance proxy** prices (labeled `data_source: binance_proxy`, `simulated: true`); restart continued same run id and paper balances. Not yet run on Wallex prices ⛔ |
| Backtester (next-bar fills, stop-first, fees, spread, slippage, episodes) | ✅ | `tests/test_backtest.py` (reproducibility, no look-ahead, hand-checked fees) |
| Evaluation protocol + reports | ✅ baseline · ⛔ AI | `reports/EVALUATION.md`, `evaluation.json`, `equity_test.svg` |
| Decision log, snapshots, export (JSON/CSV, trade trace) | 🟡 | export implemented; exercised only on HOLD-only shadow data |
| Monitoring (status file, read-only HTTP, alerts via webhook) | 🟡 | `/status` verified HTTP 200; webhook untested |
| Manifest (commit, dirty flag, config/prompt/source hashes, model) | ✅ | `git_commit` is null until the repo is committed |
| Docker / compose | 🟡 | files written; image not built in this session |
| Native exchange stop as crash backstop | ❌ | documented limitation |
| News / external evidence (pack v2 scope) | ❌ | deliberately cut; not needed for P0 |

## Defects fixed during the audit

- Final report listed unsellable dust as a material residual → dust now reported separately.
- `bootstrap()` network error exited the process silently → bounded retry + alert + event.
- Monitor bound to 127.0.0.1 inside Docker and used a read-only volume incompatible with SQLite WAL readers → `MONITOR_HOST`, rw state mount.

## Known limitations / remaining defects

- Live and backtest trailing differ (bid polling vs bar high); backtest daily-loss day is UTC, live is Tehran. Documented in `ARCHITECTURE.md`.
- Wallex response details marked UNVERIFIED in `wallex.py` (balance `value` semantics, fee unit, `t` = bar open, fee asset on buys).
- Fees from foreign fee assets are not deducted (recorded raw only).
- If the process is down, positions have no exchange-side protection.
- The 4h resolution (`240`) may return 1h bars on Wallex (gisoo observation); if so, switch `WallexClient.candles` to resample from 60.
- No strategy has shown positive held-out net edge (see `STRATEGY.md`).
