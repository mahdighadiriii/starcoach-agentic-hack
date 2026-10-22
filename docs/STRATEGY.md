# Strategy and evaluation

## Short-duration evaluation (2026-10-22, second session) — hypothesis rejected

Full report: [`reports/SHORT_DURATION_COMPARISON.md`](../reports/SHORT_DURATION_COMPARISON.md) (JSON alongside).

**Is DONCHIAN_TREND appropriate for 5–30 minute holds?** No. It is a 1h/4h trend follower with a 3×ATR
stop, 4×ATR trail and a 72-hour time exit; median holds in the backtests are 7–19 hours. It cannot be
"shortened" by parameters without becoming a different strategy, so two new long-only spot families were
added instead (`strategy.py`, profile `config/scalp.yaml`):

- `SCALP_MOMENTUM`: 5m close above the prior 12-bar (1h) high, above 5m EMA20 > EMA50, volume ≥ 1.5× median,
  RSI ≤ 78, breakout ≤ 1 ATR, inside a 1h uptrend.
- `SCALP_REVERSION`: 5m RSI < 30 on the previous bar, bullish reversal close, inside a 1h uptrend.
- Both: stop entry −0.7%, target +1.2%, time exit after 30 minutes, ATR(5m) ≥ 0.25% of price (skip quiet
  markets that cannot reach the target in time), 20-minute cooldown after a stop, BTC market filter.
- Overtrading guard (risk gate, all strategies): an entry is rejected unless the target, after two taker
  fees, the live spread and exit slippage, still nets ≥ 30 bps (`net_edge_covers_costs`).

Method: KuCoin USDT 1-minute candles (labeled proxy; Wallex and Binance were unreachable), 8 markets,
90 days. Train = first 60 days (selection only), test = last 30 days, evaluated once. 1M allocation, 200k
max order, 2 positions, 3-day episodes. Costs per side: 0.35% taker (primary), 8 bps half-spread, 5 bps
slippage; entries fill on the first 1-minute bar ≥ 60 s after the decision; exits on 1-minute bars, stop first.

Held-out results at 0.35%/side:

| run | net % | trades | win rate | PF | avg net bps/trade | median hold |
|---|---|---|---|---|---|---|
| baseline DONCHIAN_TREND 1h | −2.56 | 10 | 20% | 0.05 | −139 | 7 h |
| SCALP_REVERSION (selected on train) | −4.27 | 22 | 4.5% | 0.004 | −97 | 30 min |
| SCALP_MOMENTUM (operator hypothesis) | −5.89 | 33 | 12% | 0.08 | −90 | 30 min |

Findings:

1. **No gross edge at this horizon.** Even at zero fees, the selected short-duration variant lost 28 bps
   per trade on test (PF 0.19). Most trades end at the 30-minute time exit: a 1.2% move within 30 minutes
   is rare outside news spikes (momentum on train: 94 time exits, 44 stops, 23 targets out of 164 trades).
2. **Costs make it much worse.** Round trip ≈ 96 bps at 0.35%/side vs a +1.2% target: the target nets ~40
   bps while a stop costs ~170 bps, so break-even needs a win rate above ~80%.
3. **The cost gate works as intended but is not sufficient.** It blocked every trade of the SL 0.5% / TP 0.9%
   variant (target cannot cover costs), and none of the 1.2% target trades (they nominally cover costs),
   which is why the "no cost gate" run is identical. A floor on target economics cannot create edge.
4. **More trades meant more losses.** Momentum traded 2.7×/day on train and lost 29% of the allocation
   over 20 episodes. Frequency is not a goal.
5. The baseline made +3.65% on this train window but −2.56% on test; across both sessions it has not shown
   a stable positive edge either.

Decision: `config/scalp.yaml` ships with `edge_gate: strict` and the negative train artifact, so **it will
not open positions** unless a re-evaluation (ideally on Wallex TMN data) produces a positive validated edge.
Set `edge_gate: advisory` only to observe it in shadow. The 0.7% / 1.2% / 30-minute parameters remain
hypotheses that the data did not support.


## What runs

The reported strategy version is derived from the enabled families and timeframes
(`strategy.strategy_version`): `config/strategy.yaml` runs `donchian-trend/1.0.0 [1h/4h]`,
`config/scalp.yaml` runs `scalp-momentum/1.0.0 [5m/1h]`.

`DONCHIAN_TREND`, long-only spot:

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

The AI-vs-baseline harness (`python -m wtrader evaluate --ai`) is implemented and unit-tested with a fake model, with on-disk caching so the comparison is replayable (`--replay-only`). **Strategy evaluation with the real model has not been run.** A synthetic BoxAPI structured HOLD was verified on 2026-10-23; this establishes connectivity, not improved strategy performance. No AI improvement is claimed. See [provider verification](LLM_PROVIDERS.md).

## Recommended next research (in order)

1. Get the real taker/maker fee for the competition account (`GET /v1/account/fee`) — it decides viability.
2. Re-run the evaluation on Wallex TMN history (`fetch-data --source wallex`) and compare spreads with the 8 bps assumption.
3. Run the AI evaluation on the held-out split and report vetoes/changes honestly.
4. Only then consider maker-entry execution or a lower-turnover variant; do not tune on the test split.
