# Wallex agentic spot trader

Autonomous **spot-only** trading system for the Wallex Agentic AI Hackathon (long only: no margin, no short
selling): deterministic strategy candidates, a bounded configurable selector (HOLD / pick a candidate / half size,
with a validated rationale), a non-bypassable risk gate, a strict capital allocation enforced again at the
executor, a persisted order state machine with exchange reconciliation, a kill switch, and a monitoring dashboard.

**Status (2026-10-23):** public Wallex REST/WebSocket and authenticated balance/fee/order reads work through
the verified `en0` path. Credit-package access returns 401; funding provenance, wallet linkage and allocation
isolation remain unverified. Shadow uses simulated funds and orders. **Live remains disabled**, and no strategy
has shown a credible positive net edge. [Develop comparison and account verification](docs/DEVELOP_REVIEW_2026-10-23.md).
The short-duration (5–30 min) hypothesis was implemented, backtested and **rejected** (see below).
Details: [live-readiness checklist](docs/COMPETITION_READINESS.md) · [implementation status](docs/IMPLEMENTATION_STATUS.md).

## Capital allocation

The exchange reports 5,000,000 TMN available (its funding source is unverified). The bot may commit at most **1,000,000 TMN**: max **200,000 TMN per order**,
**2** concurrent positions, **30,000 TMN** daily loss limit. Committed capital is computed from persisted
orders/positions plus exchange balances and open orders; unresolved submissions keep their full reservation;
profits never raise the allocation; assets the bot did not create are never sold or modified. The exchange
has not been verified to isolate that allocation; a software cap does not establish fund isolation.
See [operations §1](docs/OPERATIONS.md#1-capital-allocation-what-the-bot-may-use).

## Profiles

| Config | Strategy | Timeframes | Notes |
|---|---|---|---|
| `config/strategy.yaml` | `DONCHIAN_TREND` (baseline) | 1h signal / 4h context | hours-long holds; held-out results negative so far |
| `config/scalp.yaml` | `SCALP_MOMENTUM` (+ `SCALP_REVERSION` available) | 5m / 1h, exits every 5 s | SL 0.7%, TP 1.2%, ≤ 30 min; **`edge_gate: strict` → no entries** while the validated edge is negative |

Legacy backtest comparison (proxy data, historical test already examined, 0.35%/side fee): baseline −2.56% (10 trades),
short-duration −4.27% to −5.89% (22–33 trades, win rate 4–12%); short-duration loses even at zero fees.
[Legacy report](reports/SHORT_DURATION_COMPARISON.md) · [corrected independent replay](docs/AUDIT_2026-10-22.md) · [analysis](docs/STRATEGY.md).

## Setup

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp -n .env.example .env     # preserves an existing .env; fill in privately, never commit it
```

## Commands

```bash
.venv/bin/python -m pytest -q                                            # 254 tests, mocked/simulated
# Dashboard (read-only; engage-only kill button)  → http://127.0.0.1:8787/
.venv/bin/python -m wtrader --config config/scalp.yaml dashboard
# Shadow mode (paper fills). Wallex prices, or the labeled KuCoin proxy where Wallex is unreachable:
.venv/bin/python -m wtrader --config config/scalp.yaml shadow
AI_MODE=baseline .venv/bin/python -m wtrader --config config/scalp.yaml shadow --data-source kucoin_proxy
# Kill switch
.venv/bin/python -m wtrader kill --reason "..." [--level halt|flatten|freeze]   |   kill --status   |   kill --clear
# Read-only exchange checks
.venv/bin/python -m wtrader probe
.venv/bin/python -m wtrader --config config/shadow_eval.yaml llm-probe # one synthetic HOLD request, no Wallex access
.venv/bin/python -m wtrader --config config/scalp.yaml preflight
# Research
.venv/bin/python -m wtrader fetch-data --source kucoin_proxy --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,TRXUSDT --days 90
.venv/bin/python -m wtrader compare --train-days 60                       # → reports/SHORT_DURATION_COMPARISON.md
AI_MODE=baseline .venv/bin/python -m wtrader evaluate                     # original 1h/4h protocol
.venv/bin/python -m wtrader status | export
docker compose up -d                                                      # shadow + dashboard
```

## Live activation (operator only)

Live is off by default and never turns itself on. It requires, in order: a committed clean tree, `.env`
credentials and organizer facts, a passing `preflight`, an operator `arm-live` record (typed confirmation
`ARM LIVE 1000000 TMN`, expiry ≤ 24 h, bound to the exact config hash, commit and allocation), and
`LIVE_ENABLED=true ... live`, which re-runs every check before the first tick and re-validates the arm record
before every BUY. Live uses its own database. Full procedure: [operations §5](docs/OPERATIONS.md#5-live-activation-operator-only).

## Docs

[Operations](docs/OPERATIONS.md) · [Architecture](docs/ARCHITECTURE.md) · [Strategy & results](docs/STRATEGY.md) ·
[LLM providers and BoxAPI verification](docs/LLM_PROVIDERS.md) ·
[Wallex integration](docs/WALLEX_INTEGRATION.md) · [Readiness](docs/COMPETITION_READINESS.md) ·
[Repository evaluation](docs/REPOSITORY_EVALUATION.md) · [Demo outline](docs/DEMO_OUTLINE.md)

Built on the contracts and reference invariants of `../wallex_challenge_pack` (Path A).


## Independent Shadow audit (2026-10-22)

See [the audit report](docs/AUDIT_2026-10-22.md) and `reports/audit/` for independent
exchange checks, replay results, regression tests and browser evidence. The existing
strict profiles are unchanged. Live execution is held disabled by
`execution.live_rollout_approved: false`; previous live-start instructions are not a readiness verdict.

For isolated evaluation on real Wallex data with simulated orders:

```bash
WALLEX_BIND_INTERFACE=en0 .venv/bin/python -m wtrader --config config/shadow_eval.yaml shadow
.venv/bin/python -m wtrader --config config/shadow_eval.yaml dashboard
# http://127.0.0.1:8791/
```

Choose the interface based on the current route; `en0` was verified on the audit host.
The evaluation profile uses its own database, status, kill switch and evidence directory.
Its advisory edge gate cannot be used by `live`, `arm-live`, or live liquidation.
The signal rules and allocation/risk limits match the strict scalp profile.
Do not combine an interface binding with an explicit Wallex proxy.

Candle and book reads run in separate bounded workers; the Shadow protection loop
and SQLite writes remain on one thread. Stale prices are labeled and cannot cause
simulated fills or new entries. Stops cannot execute during missing-price intervals;
process or network uptime is not an execution guarantee. Durable paper venue snapshots
allow restart reconciliation, including partial fills. A legacy Shadow DB containing
orders but no such snapshot is refused; preserve it and use a new evaluation DB.
