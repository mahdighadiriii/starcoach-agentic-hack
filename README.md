# StarCoach Agentic AI Hackathon — Team «نشستیم»

An agentic crypto trader on Wallex: an LLM proposes trades, deterministic guardrails approve or block them, and every decision is journaled with its reasoning.

```
observe ──► analyze (LLM via BoxAPI) ──► risk gate (code) ──► execute (Wallex) ──► journal
 market data,      BUY/SELL/HOLD +          size caps, position     dry-run by        dashboard
 RSI/SMA, balances  confidence + reason      limit, kill switch      default
```

**Running the bot during the trading window: see [docs/RUNBOOK.md](docs/RUNBOOK.md).**

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in LLM_API_KEY / WALLEX_API_KEY
.venv/bin/uvicorn app.main:app --reload
# open http://localhost:8000
```

With no keys it still runs: public Wallex market data + a rule-based fallback instead of the LLM, in dry-run.

## Safety

- `DRY_RUN=true` by default — no real orders until the team flips it.
- Guardrails in `app/risk.py` are plain code, not prompts: max order size, max position %, min confidence, min notional, disabled markets, daily-loss kill switch.
- Toman markets only; before `WINDOW_END` everything is converted back to TMN so profit is realized.
- Every decision is appended to `data/journal.jsonl` and reloaded on restart.
- Dashboard has a manual **Kill switch**.

## Deploy (Liara)

Dockerfile is included; the app listens on port 8000. Set the `.env` values as environment variables in the Liara app.

## Layout

| Path | What |
|---|---|
| `app/agent.py` | LangGraph pipeline |
| `app/wallex.py` | Wallex REST client (checked against the official API docs) |
| `app/markets.py` | Markets disabled in the hackathon credit account |
| `app/risk.py` | Guardrails |
| `app/indicators.py` | SMA / RSI |
| `app/main.py` | FastAPI API + autoloop |
| `app/static/index.html` | Dashboard |

## TODO

- [ ] Backtest on historical candles + shadow mode, results in the repo
- [ ] Deploy to Liara, go live when the team agrees
- [ ] Full challenge doc (architecture, AI role, buy/sell logic, risk, re-run) + demo video
