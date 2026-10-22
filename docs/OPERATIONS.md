# Operations guide

> Audit update (2026-10-22): see [the independent audit](AUDIT_2026-10-22.md). Live execution is held disabled by `execution.live_rollout_approved: false`. The isolated experimental profile is `config/shadow_eval.yaml` (port 8791). Candle/book/account-display requests no longer run on the Shadow protection thread. SELLs require a persisted bot position; dust is retained and conservatively written off rather than sold through unassigned liquidation orders. The older baseline-based residual sweep described below has been retired.

All commands run from the repository root with the project venv. `--config` selects the profile:
`config/strategy.yaml` (baseline DONCHIAN_TREND, 1h/4h) or `config/scalp.yaml` (short-duration, 5m/1h).
Both carry the same allocation limits.

## 1. Capital allocation (what the bot may use)

| Setting (`risk.*`) | Value | Enforced where |
|---|---|---|
| `total_credit_tmn` | 5,000,000 | informational (what the account holds) |
| `authorized_capital_tmn` | 1,000,000 | risk gate (`allocation_capacity`, `within_allocation`) **and** executor guard (`safety.check_buy`) before every BUY |
| `max_order_tmn` | 200,000 (fee included) | risk gate (`max_order_allocation`, sizing cap) **and** executor guard |
| `max_positions` | 2 (pending entries count) | risk gate **and** executor guard |
| `daily_loss_limit_tmn` | 30,000 (bot realized + unrealized vs Tehran day start) | protection clock latches `daily_halt`; risk gate; executor guard |
| `max_drawdown_fraction` | 4% (baseline) / 5% (scalp) of the allocation | latching breaker: flattens bot positions, halts entries |

How committed capital is computed (`safety.compute_allocation`, from persisted records plus exchange reads):

- open bot positions at cost basis (`qty × entry_price`, fees included);
- + the unfilled remainder of every bot BUY in any non-terminal state, at its limit price plus fee. `UNKNOWN`,
  `SUBMITTING` and `QUARANTINED` orders keep their **full** reservation until reconciled, so retries,
  timeouts and restarts can never free capacity that may already be spent;
- + BUY orders open on the exchange that the bot did not create (from `GET /v1/account/openOrders`).
  These are never canceled and, by default, halt new entries.
- Budget = authorized + min(0, realized P&L). Losses shrink it; **profits, realized or not, never grow it**.
- Available = min(budget − committed, exchange free TMN).

Equity, sizing, daily loss and drawdown use the bot's own equity (authorized + realized + unrealized of bot
positions). The other 4,000,000 TMN, and any other assets, are reported on the dashboard and never counted,
sold or modified. Non-TMN balances present at the first start are recorded as `external_baseline`. A deposit never establishes bot ownership. All SELLs require a persisted bot position; below-minimum dust is retained, disclosed and conservatively written off.

**Isolation limitation.** The exchange cannot enforce the 1M cap: the API key can spend everything in the
account. The cap holds as long as this bot is the only writer and its code is unmodified. Strict isolation
needs a dedicated Wallex sub-account funded with exactly the allocation. `preflight` reports
`allocation_isolation` as a warning whenever the account holds more than the allocation.

Raising the allocation requires editing `authorized_capital_tmn`, committing, and re-arming (`arm-live`
binds the amount, the config hash and the commit). A running live process cannot pick up a higher amount.

## 2. Shadow mode (no real orders)

```bash
# Check the network path first (read-only; prints route, TLS, markets, candles, depth, WebSocket):
.venv/bin/python -m wtrader --config config/scalp.yaml probe
# Wallex prices, simulated fills. On a Mac with a full-tunnel VPN, send Wallex traffic out of en0
# (or exclude api.wallex.ir from the VPN and drop WALLEX_BIND_INTERFACE):
AI_MODE=baseline WALLEX_BIND_INTERFACE=en0 .venv/bin/python -m wtrader --config config/scalp.yaml shadow
# AI selector: LLM_PROVIDER=boxapi + BOXAPI_API_KEY, or optional Anthropic. Failures always HOLD.
# Labeled proxy prices when Wallex is unreachable (KuCoin USDT books, paper fills):
AI_MODE=baseline .venv/bin/python -m wtrader --config config/scalp.yaml shadow --data-source kucoin_proxy
```

Shadow funds a paper account with `competition.initial_capital_tmn` (5M, mirroring the credit) and trades
only the 1M allocation. Paper balances persist across restarts in the same DB.

## 3. Dashboard

```bash
.venv/bin/python -m wtrader --config config/scalp.yaml dashboard            # http://127.0.0.1:8787/
.venv/bin/python -m wtrader --config config/scalp.yaml dashboard --port 8790
```

Read-only. Binds 127.0.0.1 (set `MONITOR_HOST` only behind a trusted proxy). Banner: **LIVE — REAL EXCHANGE
ORDERS** (red) vs **SIMULATED (SHADOW)** (blue), plus the data source with a PROXY label. Metrics are computed
from the SQLite records; marks and health come from `state/status.json`, which the engine rewrites every tick.
CSV: `/api/trades.csv`, `/api/orders.csv`. The one write it can do is engage the kill switch at `halt`.

## 4. Kill switch

```bash
.venv/bin/python -m wtrader kill --reason "manual stop"                    # halt (default)
.venv/bin/python -m wtrader kill --level flatten --reason "exit everything the bot owns"
.venv/bin/python -m wtrader kill --level freeze --reason "stop all order traffic"
.venv/bin/python -m wtrader kill --status
.venv/bin/python -m wtrader kill --clear                                   # resume (operator decision)
```

The switch is the file `state/KILL`. The executor reads it immediately before every order, so the next order
is blocked even mid-tick; it works while the dashboard is down. An unreadable file counts as `freeze`.
Engaging never weakens an already-engaged level.

| Level | New BUYs | Open bot entry orders | Open positions | Exit orders | Other assets |
|---|---|---|---|---|---|
| `halt` | blocked | canceled next tick | kept; stop/target/time/liquidation exits keep running | allowed | untouched |
| `flatten` | blocked | canceled | exited with marketable limits (escalating slippage 20→160 bps) as `KILL_FLATTEN` | allowed | untouched |
| `freeze` | blocked | canceled | **unprotected**: no exit is placed or re-priced | existing ones left as they are | untouched |

Use `halt` by default. `freeze` is for "the bot itself is misbehaving": positions must then be handled manually
on Wallex. Positions never get exchange-side stop orders, so if the process is down they are unprotected
regardless of the switch.

## 5. Live activation (operator only)

Live is disabled by default and never enables itself. Nothing in the repo sets `LIVE_ENABLED` or places a test
order.

1. Commit the exact code and config (the live gate refuses a dirty tree).
2. Fill `.env`: competition `WALLEX_API_KEY`, `WALLEX_SUB_ACCOUNT_CLIENT_ID`, `WALLEX_ALLOWED_MARKETS`
   (organizer-confirmed), `TRADE_START`/`TRADE_END` with offset, `LLM_PROVIDER` and its API key (or `AI_MODE=baseline`); see [LLM providers](LLM_PROVIDERS.md).
3. Read-only checks: `.venv/bin/python -m wtrader --config config/scalp.yaml preflight`.
   Fix every `FAIL`; read every `WARN` (fees above the assumption, isolation, thin books).
4. Shadow on Wallex data for at least several hours; review the dashboard, decisions and blocked orders.
5. Arm (binds operator, amount, max order, config hash and commit; expires after `--hours`, max 24):
   ```bash
   .venv/bin/python -m wtrader --config config/scalp.yaml arm-live --operator "<name>" \
       --authorized-tmn 1000000 --hours 12 --confirm "ARM LIVE 1000000 TMN"
   ```
   `arm-live` re-runs preflight and refuses on any failure.
6. Start: `LIVE_ENABLED=true .venv/bin/python -m wtrader --config config/scalp.yaml live`.
   Startup re-checks the env gates, the arm record and a fresh preflight, then reconciles orders. Every BUY
   re-validates the arm record, so an expired or mismatched arm stops new entries without touching exits.
7. Stop: `kill` (above), then stop the process. `disarm-live` removes the arm record.

Live uses its own database (`state/live.sqlite`); shadow and paper records can never become live positions.

## 6. Restart and recovery

On start the engine (a) takes the single-writer lock, (b) reloads positions and orders from SQLite,
(c) looks up every non-terminal order by `client_id` and applies fills as deltas (no double counting),
(d) compares `openOrders` with its records (unknown orders → reserved + entries halted; orders it thought
finished but are still open → re-observed), (e) alerts if a position is larger than the exchange balance.
`UNKNOWN` orders not found after 120 s become `QUARANTINED` and block entries until an operator resolves them.

## 7. Outages

- Wallex unreachable: reads fail fast once the circuit opens (3 transport failures → 30 s); bootstrap retries
  with backoff (bounded, 30 attempts) and writes `engine_state: BOOTSTRAP_RETRY n/30` to the status file so the
  dashboard shows the cause instead of a stale run.
- Every HOLD carries a `category`: `MARKET_DATA_UNAVAILABLE` / `NO_SETUP_PARTIAL_DATA` (connectivity),
  `ENTRIES_HALTED` / `PHASE` (risk/schedule), `NO_SETUP` (strategy), plus a per-market reason.

- Market data: a failed book read reuses the last book; no successful read for `max_market_data_age_seconds`
  halts entries. A stale book for an open position raises an alert.
- Account data: a failed balance read reuses the last read for exit management; after
  `max_account_data_age_seconds` entries halt; every BUY requires a fresh successful balance read.
- Order POST timeout/5xx: never retried; reconciled by `client_id`.
