# Architecture

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
| `ai_agent.py` | Context builder, baseline selector, Claude selector, validator, cache |
| `risk.py` | Entry gate, breakers, competition phases, exit priority — pure functions |
| `executor.py` | Order FSM, idempotency, reconciliation, cumulative-delta fill accounting |
| `engine.py` | Protection clock (5 s) + decision clock (1h bar), positions, liquidation, final report, alerts |
| `backtest.py`, `evaluate.py` | Bar replay sharing strategy/selector/risk/exit code; train/test protocol |
| `store.py`, `monitor.py`, `manifest.py`, `cli.py` | Persistence, monitoring/export, reproducibility manifest, commands |

## Mode equivalence

| Component | Backtest | Shadow | Live |
|---|---|---|---|
| `strategy.analyze`, `apply_market_filter` | same | same | same |
| Selector + validation | same (cached) | same | same |
| `risk.evaluate_entry` | same, synthetic book from bar close ± half spread | same, real book | same, real book |
| Exit rules | same `trail_stop`; stop/target checked on bar high/low, stop first | `exit_signal` on bid every 5 s | same as shadow |
| Fills | next-bar open + spread + slippage | PaperVenue walks real book (50% of each level) | Wallex |

Known differences (documented, not hidden): backtest trails from bar highs and checks intrabar; live trails from polled bids. Backtest time-exit counts bars including the fill bar. Backtest daily-loss uses UTC days; live uses Asia/Tehran days.

## AI boundary

The model gets no tools. Output is constrained by structured outputs and then re-validated (`validate_decision`): snapshot id must match, candidate id must be in the frozen set, multiplier ∈ {0.5, 1.0}, cited evidence ids must exist. Timeouts, refusals, truncation, malformed or ungrounded output → `HOLD` (default) or the deterministic baseline (`outage_action: BASELINE`). Exits never consult the model. Call budget: ≤160/day; HOLD without a call when there are no candidates.
