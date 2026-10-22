# Repository evaluation

Research date 2026-10-22. Each serious candidate was shallow-cloned and its Wallex/order code read; metadata from the GitHub API. "Verified" means read in source, not run against a live account (no Wallex credentials were available, and `api.wallex.ir` was unreachable from the research host).

## Candidates

| Repository | Lang / license | Activity | What the source actually shows |
|---|---|---|---|
| amirphl/simple-trader | Go / **no license** | last push 2025-09 | Real Wallex client (`internal/exchange/wallex.go`), Postgres candle store, backtest, Heikin-Ashi/stochastic strategies. `SubmitOrderWithRetry` retries order POSTs. No license → cannot be redistributed. No AI. |
| mhv1989/any-wallex | Python / Apache-2.0 | 2026-09 | Wallex client with correct endpoint list (udf/history, depth, orders by client_id), paper broker, backtest, AI strategy helper, FastAPI dashboard. **Live broker uses float math and MARKET orders, does not persist intent before POST, and logs "check the order manually on Wallex" on ambiguity.** Ships manual-order endpoints (prohibited for the contest). 4,200-line server module. |
| ARaminco/gisoo-trader | Python / MIT | 2026-10 | Best-structured Iranian-exchange bot: Wallex connector "written against official docs", risk manager, reconcile worker, LLM advisor that can only veto/shrink. Requires Postgres + Redis + auth/passkey/push stack; strategy is 4h portfolio momentum; README marks Wallex order placement as untested with real money. |
| Drakkar-Software/OctoBot | Python / GPL-3.0 | active | Full platform; exchange layer is CCXT-shaped. No Wallex connector; writing one plus competition overlays was estimated (challenge pack doc 12) at a multi-day spike. |
| freqtrade/freqtrade (+strategies) | Python / GPL-3.0 | very active | Excellent backtester; CCXT-only exchanges, no Wallex. Strategies used as idea reference only (GPL, not copied). |
| hummingbot/hummingbot | Python / Apache-2.0 | active | Market-making focus, connector framework is heavy; no Wallex connector. |
| amirSamanQ/python-wallex | Python / none | 2026-08 | 380-line SDK (already in workspace). Useful endpoint examples and response samples; float rounding, POST retried 10× in a loop (duplicate-order risk). |
| SajjadKiani/wallex-mcp-server | Python | 2025-05 | Community MCP wrapper; exposes order tools to an LLM — the opposite of the bounded design required here. |
| SetayeshSamani/quant-forge | Python | 2026-10 | 457 lines, early prototype. |

## Weighted score (0–5 per criterion)

| Criterion (weight) | simple-trader | any-wallex | gisoo-trader | OctoBot | freqtrade | **Built: pack Path A** |
|---|---|---|---|---|---|---|
| Wallex readiness (25%) | 3 | 3 | 3.5 | 0 | 0 | 4 |
| Engine completeness (20%) | 3 | 3 | 4 | 5 | 5 | 3.5 |
| Backtest/shadow (15%) | 3 | 3 | 3 | 4 | 5 | 3.5 |
| Risk/execution reliability (15%) | 1.5 | 1 | 3.5 | 3 | 4 | 4 |
| AI extensibility (10%) | 1 | 3 | 3.5 | 3 | 2 | 4 |
| Implementation speed (10%) | 1 (Go, no license) | 2.5 | 2 | 1 | 1 | 4 |
| Maintenance/quality (5%) | 2 | 2 | 4 | 5 | 5 | 3.5 |
| **Weighted** | 2.4 | 2.6 | 3.4 | 2.5 | 2.9 | **3.8** |

## Decision

No candidate could be forked safely: the two Wallex-native Python bots either lack a persisted-intent / no-blind-retry order path (any-wallex, python-wallex) or bring an infrastructure stack (Postgres, Redis, auth, push) and a 4h strategy that would take longer to adapt than to write a compact core (gisoo-trader). Simple-trader has no license. The large frameworks have no Wallex connector.

Following the challenge pack's documented **Path A**, the foundation is a compact Python modular monolith that reuses:

- **Challenge pack** (`wallex_challenge_pack/`): `contracts/broker.py` boundary (→ `wtrader/models.py`), `decision.schema.json` v1 (extended to v2), `schema.sql` (→ `wtrader/store.py`), `reference/risk.py` sizing formula (→ `wtrader/risk.py`), `reference/order_recovery.py` FSM policy (→ `wtrader/executor.py`), selector prompt rules (→ `prompts/selector_v3.md`), feature specifications.
- **python-wallex / any-wallex / gisoo-trader**: endpoint paths, `client_id` charset rule, precision-as-decimals convention, `udf/history` off-grid-bar filtering idea, response samples — all re-verified against developers.wallex.ir. No code copied.
- **Official docs**: trade-credit sub-account header, client_id uniqueness, order types, 20 POST/10 s limit (see `WALLEX_INTEGRATION.md`).

The workspace's `python-wallex` clone remains untouched as a reference.
