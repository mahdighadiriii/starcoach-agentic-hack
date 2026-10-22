# Strategy and evaluation

## What runs

`DONCHIAN_TREND` (strategy version `donchian-trend-1h4h/1.0.0`), long-only spot:

- Context (4h): EMA20 > EMA50 and EMA20 rising over 3 bars → regime `TREND_UP`.
- Signal (1h, closed bar only): close > prior 20-bar high, close > EMA50, EMA20 > EMA50, volume ≥ 0.8× 20-bar median.
- Market filter: no entries in any market unless **BTCTMN** is itself in a 4h uptrend.
- Stop: entry − 3×ATR14 (must be 0.8%–8% of entry); trailing stop = running peak − 4×ATR (ratchets up only); 8R cap target; 72-bar time exit; 4-bar cooldown after a stop.
- One candidate per market; at most 2 positions; AI chooses among candidates or HOLD.

Three other families remain implemented for comparison (`TREND_PULLBACK`, `CONFIRMED_BREAKOUT`, `RSI_REVERSION`) and are disabled.

## How it was selected (and what was not done)

Data: Binance spot 15m/1h klines (data-api.binance.vision), 8 USDT markets, 240 days to 2026-10-22, **used as a labeled proxy** because Wallex was unreachable. Split: first 160 days train, last 80 days held out. All parameter choices used train only (`reports/sweep_train.json`, `reports/sweep_trend_train.json`); the test split was evaluated once.

Cost model: taker fee 0.35%/side (conservative assumption, not a verified Wallex rate), half-spread 8 bps + slippage 5 bps per side → ~96 bps round trip. Backtests run as consecutive 3-day episodes (each a simulated competition: forced liquidation at the end, drawdown breaker per episode).

## Findings — negative, reported as measured

1. **15-minute families have no gross edge.** With zero costs the pullback/breakout/reversion families averaged −4 to +3 bps/trade (train); with costs −95 to −104 bps/trade. Fees, not bugs, dominate: a manual trade audit confirmed fills, stops, trailing and fees behave as specified.
2. **Train window was a down/choppy market** (BTC −2.5%, XRP −20%, ADA −29%); the held-out window rallied (+16% to +46% for most markets). Every train configuration lost money; the selected one lost least with the smallest drawdown.
3. **Held-out result (baseline, `reports/EVALUATION.md`)**: −3.31% net, max DD 7.0% over 80 days, 46 trades, PF 0.69, −30 bps/trade. 3-day episodes: mean −0.13%, median 0.0%, worst −1.26%, best +2.90%, 15% positive.
4. **Fee sensitivity (held-out)**: 0.35% → −3.3%; 0.25% → −2.0%; 0.15% → −0.3%; 0% → +2.9% (PF 1.31). The strategy only becomes viable if the real competition fee is ≲0.15% per side, or entries can be made as maker orders.
5. ~30% of trend trades are cut by the 3-day episode end — the contest horizon itself penalizes trend-following.

Conclusion: **no configuration has demonstrated positive net edge.** The edge artifact passed to the AI says so (`mean_net_bps −105`, LCB −142). The system is engineered so that, absent edge, its main property is small, bounded losses (per-trade risk 0.5%, daily stop 2%, run breaker 4%), and the AI is instructed that HOLD is a normal outcome.

## AI evaluation status

The AI-vs-baseline harness (`python -m wtrader evaluate --ai`) is implemented and unit-tested with a fake model, with on-disk caching so the comparison is replayable (`--replay-only`). **It has not been run: no Anthropic credentials are available on the development host.** No AI improvement is claimed.

## Recommended next research (in order)

1. Get the real taker/maker fee for the competition account (`GET /v1/account/fee`) — it decides viability.
2. Re-run the evaluation on Wallex TMN history (`fetch-data --source wallex`) and compare spreads with the 8 bps assumption.
3. Run the AI evaluation on the held-out split and report vetoes/changes honestly.
4. Only then consider maker-entry execution or a lower-turnover variant; do not tune on the test split.
