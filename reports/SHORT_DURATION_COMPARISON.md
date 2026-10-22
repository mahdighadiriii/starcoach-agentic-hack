# Short-duration vs baseline (backtest)

Generated 2026-10-22T16:02:09.524771+00:00. Data: **kucoin_proxy** — PROXY (KuCoin USDT 1-minute candles), not Wallex TMN prices, spreads or fills.
Train 2026-07-24 → 2026-09-22 · Test (held out, evaluated once) 2026-09-22 → 2026-10-22
Allocation 1000000 TMN, max order 200000 TMN, max 2 positions, 3-day episodes. Costs per side: taker fee (grid below; primary 0.35%), half-spread 8 bps, slippage 5 bps; short-duration entries fill 60 s after the decision; exits on 1-minute bars, stop first.

## Train (selection only)

| run | net_return_pct | net_pnl_tmn | max_drawdown_pct | trades | trades_per_day | win_rate | profit_factor | avg_net_bps_per_trade | fees_tmn | hold min (median/max) |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline_donchian_1h | 3.65 | 36495 | 2.311 | 28 | 0.47 | 0.429 | 2.02 | 85.2 | 35497 | 1140.0/2280.0 |
| scalp_momentum_sl0.7_tp1.2 | -28.882 | -288823 | 28.882 | 164 | 2.73 | 0.165 | 0.075 | -88.4 | 227961 | 30.0/30.0 |
| scalp_reversion_sl0.7_tp1.2 | -9.403 | -94030 | 9.46 | 49 | 0.82 | 0.041 | 0.003 | -96.4 | 68092 | 30.0/30.0 |
| scalp_both_sl0.7_tp1.2 | -34.192 | -341915 | 34.192 | 186 | 3.1 | 0.129 | 0.055 | -92.3 | 258485 | 30.0/30.0 |
| scalp_momentum_sl0.5_tp0.9 | 0.0 | 0 | 0.0 | 0 | 0.0 | None | None | None | 0 | None/None |
| scalp_momentum_sl1.0_tp1.8 | -29.508 | -295085 | 29.508 | 152 | 2.53 | 0.099 | 0.059 | -98.3 | 209215 | 30.0/30.0 |
| scalp_momentum_no_cost_gate | -28.882 | -288823 | 28.882 | 164 | 2.73 | 0.165 | 0.075 | -88.4 | 227961 | 30.0/30.0 |

Selected on train: **scalp_reversion_sl0.7_tp1.2** — rule: highest train net P&L among cost-gated variants with >= 20 train trades; falls back to the operator hypothesis when none qualifies.

## Held-out test (primary fee 0.35%/side)

| run | net_return_pct | net_pnl_tmn | max_drawdown_pct | trades | trades_per_day | win_rate | profit_factor | avg_net_bps_per_trade | fees_tmn | hold min (median/max) |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline_donchian_1h | -2.555 | -25549 | 2.555 | 10 | 0.33 | 0.2 | 0.051 | -139.1 | 13075 | 420.0/1500.0 |
| scalp_reversion_sl0.7_tp1.2 | -4.266 | -42660 | 4.283 | 22 | 0.73 | 0.045 | 0.004 | -97.4 | 30560 | 30.0/30.0 |
| scalp_momentum_sl0.7_tp1.2 | -5.885 | -58853 | 5.885 | 33 | 1.1 | 0.121 | 0.076 | -89.6 | 45874 | 30.0/30.0 |
| scalp_momentum_no_cost_gate | -5.885 | -58853 | 5.885 | 33 | 1.1 | 0.121 | 0.076 | -89.6 | 45874 | 30.0/30.0 |

## Fee sensitivity (held-out test)

| taker fee/side | run | net % | trades | win rate | PF | avg net bps |
|---|---|---|---|---|---|---|
| 0.0035 | baseline_donchian_1h | -2.555 | 10 | 0.2 | 0.051 | -139.1 |
| 0.0035 | scalp_reversion_sl0.7_tp1.2 | -4.266 | 22 | 0.045 | 0.004 | -97.4 |
| 0.0025 | baseline_donchian_1h | -2.275 | 10 | 0.2 | 0.088 | -119.3 |
| 0.0025 | scalp_reversion_sl0.7_tp1.2 | -3.399 | 22 | 0.045 | 0.017 | -77.5 |
| 0.0020 | baseline_donchian_1h | -2.104 | 10 | 0.2 | 0.109 | -109.4 |
| 0.0020 | scalp_reversion_sl0.7_tp1.2 | -2.964 | 22 | 0.045 | 0.026 | -67.6 |
| 0.0010 | baseline_donchian_1h | -2.238 | 11 | 0.182 | 0.132 | -125.2 |
| 0.0010 | scalp_reversion_sl0.7_tp1.2 | -2.092 | 22 | 0.091 | 0.057 | -47.7 |
| 0 | baseline_donchian_1h | -1.875 | 11 | 0.273 | 0.185 | -105.4 |
| 0 | scalp_reversion_sl0.7_tp1.2 | -1.218 | 22 | 0.273 | 0.188 | -27.7 |

## Spread sensitivity (held-out test, scalp_reversion_sl0.7_tp1.2, fee 0.35%)

| half-spread bps | net % | trades | win rate | PF | avg net bps |
|---|---|---|---|---|---|
| 4 | -3.908 | 22 | 0.045 | 0.011 | -89.2 |
| 8 | -4.266 | 22 | 0.045 | 0.004 | -97.4 |
| 15 | -4.873 | 22 | 0.0 | 0.0 | -111.3 |
| 25 | 0.0 | 0 | None | None | None |

Limitations: proxy USDT data (Wallex TMN books are thinner and spreads wider); fills are modeled, not observed; a single chronological split over a few months is a small sample; no AI selector in these runs (baseline selector).
