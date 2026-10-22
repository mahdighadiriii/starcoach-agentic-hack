# Architecture

> Audit update (2026-10-22): the modular monolith is retained. Shadow book reads use fixed background workers, with a separate pool for held symbols; candle analysis and account display have their own bounded jobs. Workers never access SQLite or submit orders. The engine thread remains the sole database/order writer. See [the audit](AUDIT_2026-10-22.md) for the live-path and outage limitations.

Single-process modular monolith; one SQLite file per account, guarded by an exclusive `flock` (one order writer).

```
Wallex REST ──► venue.WallexMarketData ──► strategy.analyze (indicators) ──► candidates
                                                                   │
                                     ai_agent.build_context (frozen, hashed snapshot → evidence/snapshots/)
                                                                   │
                                   ai_agent.LLMSelector (worker thread, deadline, schema, validation)
                                                                   │  HOLD | candidate × {0.5, 1.0}
                                          risk.evaluate_entry (fresh book, all checks logged)
                                                                   │
                       executor.Executor (intent row → SUBMITTING → POST → reconcile) ──► venue.LiveVenue | PaperVenue
                                                                   │
                     store.Store (orders, positions, decisions, events, equity)  ──► monitor (read-only HTTP), export
```

| Module | Responsibility |
|---|---|
| `models.py` | Decimal domain types, order states, exchange error taxonomy |
| `wallex.py` | REST adapter: read retries/backoff, **no POST retry**, error classification, normalization |
| `venue.py` | `LiveVenue`, conservative `PaperVenue`, `WallexMarketData`, labeled `BinanceProxyMarketData` |
| `indicators.py`, `strategy.py` | Canonical features, families, regime, market filter, trailing stop |
| `ai_agent.py` | Context builder, baseline/LLM selector, validation, cache, bounded retries and usage |
| `llm.py` | BoxAPI (OpenAI SDK), optional Anthropic adapter, sanitized errors and token normalization; no exchange access |
| `risk.py` | Entry gate (incl. allocation, max order, net-edge-covers-costs), breakers, phases, exit priority — pure functions |
| `safety.py` | Kill switch, strict allocation accounting (`compute_allocation`, `check_buy`), foreign-order detection, live arm record |
| `preflight.py` | Read-only live-readiness checks used by `preflight`, `arm-live` and `live` startup |
| `executor.py` | Order FSM, idempotency, reconciliation, cumulative-delta fill accounting, pre-submit guard hook |
| `engine.py` | Protection clock (5 s) + decision clock (1h bar), positions, liquidation, final report, alerts |
| `backtest.py`, `evaluate.py` | Bar replay sharing strategy/selector/risk/exit code; train/test protocol |
| `store.py`, `monitor.py` + `dashboard.html`, `manifest.py`, `cli.py` | Persistence, dashboard/CSV/export, reproducibility manifest, commands |

## Mode equivalence

| Component | Backtest | Shadow | Live |
|---|---|---|---|
| `strategy.analyze`, `apply_market_filter` | same | same | same |
| Selector + validation | same (cached) | same | same |
| `risk.evaluate_entry` | same, synthetic book from bar close ± half spread | same, real book | same, real book |
| Exit rules | same `trail_stop`; stop/target checked on bar high/low, stop first | `exit_signal` on bid every 5 s | same as shadow |
| Fills | next-bar open + spread + slippage; short-duration: first 1m bar ≥ 60 s after the decision | PaperVenue walks real book (50% of each level) | Wallex |
| Exits (short-duration) | stop/target/time on every 1m bar, stop first | `exit_signal` on bid every 5 s | same as shadow |
| Capital | authorized allocation; budget = authorized + realized losses | bot-scoped allocation; paper account holds 5M | same, exchange-reconciled balances and open orders |

Known differences (documented, not hidden): backtest trails from bar highs and checks intrabar; live trails from polled bids. Backtest time-exit counts bars including the fill bar. Backtest daily-loss uses UTC days; live uses Asia/Tehran days.

## AI boundary

The model gets no tools. Output is constrained by structured outputs and then re-validated (`validate_decision`): snapshot id must match, candidate id must be in the frozen set, multiplier ∈ {0.5, 1.0}, cited evidence ids must exist. Timeouts, refusals, truncation, malformed or ungrounded output always produce `HOLD`, including legacy `outage_action: BASELINE` configurations. Baseline is an explicit mode. Exits never consult the model. Call budget: ≤160 generation attempts per rolling day per selector process; no candidates means no provider call. SDK retries are disabled; application attempts share a deadline and daily budget. Provider rate-limit cooldown prevents immediate repeated requests.

BoxAPI catalog verification and generation both execute on the existing selector worker. Expired decisions cannot trade; late usage can be attached to the expired record by the sole SQLite writer. Cache identities include provider, endpoint, model, schema, prompt, generation parameters and full snapshot content. Existing caches/evidence are retained. See [provider operations](LLM_PROVIDERS.md).

## Execution guard (defense in depth)

Every order passes `Engine._pre_submit` inside `Executor.place`, before the intent row is written:
kill switch (`halt`/`flatten` block BUY, `freeze` blocks all) → live arm record still valid → no unresolved
orders → no latched breaker / daily halt → no foreign open orders → no other active BUY for the symbol →
position count → a **fresh** balance read → `safety.check_buy` (max order, allocation budget, exchange free TMN).
A refusal is logged as `order_blocked`; nothing is persisted or sent. The risk gate already checks most of
this; the guard exists so that no code path (a bug, a retry, a restart) can bypass the limits.
