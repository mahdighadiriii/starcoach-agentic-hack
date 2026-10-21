# Runbook — Wallex trading bot

Short guide for whoever runs the bot during the trading window.

## What it does

Every `LOOP_INTERVAL_SEC` (5 min) for each market in `SYMBOLS` (default `BTCTMN,ETHTMN,USDTTMN`):

1. **observe** — price, 48h hourly candles (SMA/RSI), balances, total equity in TMN
2. **analyze** — LLM (BoxAPI, `gpt-5-mini`) proposes BUY / SELL / HOLD with confidence, size in TMN and a reason
3. **risk gate** — plain code can block or shrink it (see below)
4. **execute** — limit order on Wallex (skipped while `DRY_RUN=true`)
5. **journal** — one line per decision in `data/journal.jsonl`

Near the end of the window it stops trading and **converts everything back to TMN** (profit only counts in toman).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/pytest -q            # should be all green
```

Fill in `.env` (never commit it, never paste keys in chat):

| Variable | Value |
|---|---|
| `LLM_API_KEY` | BoxAPI key ($3 credit — keep `LLM_MODEL=gpt-5-mini`) |
| `WALLEX_API_KEY` | API key from **the team's credit account** (needs advanced KYC). Give it trade permission only, no withdrawal |
| `DRY_RUN` | `true` until the whole team agrees to go live |
| `WINDOW_END` | official end of trading, with timezone, e.g. `2026-10-26T23:59:00+03:30`. **Empty = the convert-to-TMN step never runs** |

## Run

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 for the dashboard. API:

| Call | Does |
|---|---|
| `GET /api/health` | dry_run, keys present, halted, window_end, flattening |
| `POST /api/run` | run one cycle now |
| `POST /api/autoloop/on` / `off` | start / stop the 5-minute loop |
| `GET /api/journal?limit=50` | latest decisions |
| `POST /api/kill` | kill switch: stop the loop and block all trades |
| `POST /api/flatten` | run the convert-to-TMN step now (emergency only) |

Docker: `docker build -t wallex-bot . && docker run --env-file .env -p 8000:8000 -v $PWD/data:/app/data wallex-bot`.
On Liara, mount a disk at `/app/data`, otherwise the journal is lost on every redeploy.

## Going live — checklist

1. `GET /api/health` shows `wallex_key: true`, `window_end` set.
2. Wipe dry-run entries: `rm data/journal.jsonl` (the submitted log must be the real run).
3. Set `DRY_RUN=false`, restart, `POST /api/run` once, check the journal and the Wallex order history.
4. `POST /api/autoloop/on`.
5. **No manual trades** on the account during the window — it is against the rules.

## Guardrails (`app/risk.py`)

- Max order `MAX_ORDER_QUOTE` (500,000 TMN), max `MAX_POSITION_PCT` (25%) of equity per coin
- LLM confidence below `MIN_CONFIDENCE` (0.6) → no trade
- Orders below the market's min notional are blocked
- Equity down `MAX_DAILY_LOSS_PCT` (5%) from the start → kill switch, trading halted
- Markets disabled in the credit account (`app/markets.py`, 345 of them) are never traded
- From `FLATTEN_MINUTES_BEFORE` (60 min) before `WINDOW_END`: no buys; open orders cancelled; every coin sold at bid −0.5% each cycle until done

## When something breaks

- Errors are written to the journal as `"action": "ERROR"` with the Wallex response, including its `error_code`:
  1001 price precision, 1002 quantity precision, 1004 amount too small, 1006 insufficient balance.
- LLM down or bad JSON → the decision becomes HOLD (safe default).
- `halted: true` in health → kill switch fired; restart the app only after the team decides.
