# Wallex agentic spot trader

Autonomous spot trading system for the Wallex Agentic AI Hackathon: deterministic strategy candidates, a bounded Claude selector (HOLD / pick a candidate / half size, with a validated rationale), a non-bypassable risk gate, a persisted order state machine with reconciliation, and automatic conversion to TMN before the competition ends.

**Status:** shadow-ready on proxy data, **not live-ready**. No strategy has shown positive held-out net edge yet. See [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) and [docs/COMPETITION_READINESS.md](docs/COMPETITION_READINESS.md).

## Setup

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in; never commit .env
```

## Commands

```bash
.venv/bin/python -m pytest -q                                   # 78 tests, all mocked/simulated
.venv/bin/python -m wtrader probe                               # read-only Wallex checks (needs a host that reaches api.wallex.ir)
.venv/bin/python -m wtrader fetch-data --source binance_proxy --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,TRXUSDT --days 240
.venv/bin/python -m wtrader fetch-data --source wallex --symbols BTCTMN,ETHTMN,SOLTMN --days 120
AI_MODE=baseline .venv/bin/python -m wtrader evaluate           # train/test baseline → reports/EVALUATION.md
.venv/bin/python -m wtrader evaluate --ai                       # + AI comparison (needs ANTHROPIC_API_KEY); --replay-only reuses cache
.venv/bin/python -m wtrader shadow                              # paper execution on Wallex prices
.venv/bin/python -m wtrader shadow --data-source binance_proxy  # paper execution on labeled proxy prices
.venv/bin/python -m wtrader status | monitor | export           # status file, read-only HTTP :8787, evidence export
docker compose up -d                                            # shadow + monitor
```

`live` exists but refuses to start unless every gate passes (LIVE_ENABLED, keys, sub-account id, competition clock, market allowlist, clean committed tree, AI credentials). There is no manual buy/sell command.

## Docs

[Repository evaluation](docs/REPOSITORY_EVALUATION.md) · [Architecture](docs/ARCHITECTURE.md) · [Strategy & results](docs/STRATEGY.md) · [Wallex integration](docs/WALLEX_INTEGRATION.md) · [Demo outline](docs/DEMO_OUTLINE.md)

Built on the contracts and reference invariants of `../wallex_challenge_pack` (Path A).
