"""Train-split sweep for DONCHIAN_TREND. Usage: python scripts/sweep_trend.py"""
import itertools, json, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wtrader.config import Settings
from wtrader.backtest import load_series, run_backtest, metrics, CostModel
from wtrader.ai_agent import BaselineSelector

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "TRXUSDT"]
TRAIN_DAYS = 160
KEYS = ["sig_s", "ctx_s", "atr_stop", "trail_atr", "market_filter"]

def job(args):
    sig_s, ctx_s, stop_mult, trail, filt = args
    s = Settings()
    s.market.signal_seconds, s.market.context_seconds = sig_s, ctx_s
    s.strategy.enabled_families = ["DONCHIAN_TREND"]
    s.strategy.atr_stop_multiplier, s.strategy.trail_atr_multiplier = str(stop_mult), str(trail)
    s.strategy.stop_distance_min_fraction, s.strategy.stop_distance_max_fraction = "0.008", "0.08"
    s.strategy.market_filter_symbol = "BTCUSDT" if filt else None
    ser = load_series(Path("data"), "binance_proxy", SYMS, sig_s, ctx_s)
    t0 = ser["BTCUSDT"][0][0].open_time
    m = metrics(run_backtest(s, ser, t0, t0 + TRAIN_DAYS * 86400, BaselineSelector(), CostModel(), {}, "", record_decisions=False), 5e6)
    return {"params": dict(zip(KEYS, args)), **{k: m[k] for k in ["net_return_pct", "max_drawdown_pct", "trades", "win_rate",
            "profit_factor", "avg_net_bps_per_trade", "episode_return_pct", "exit_reasons"]}}

if __name__ == "__main__":
    grid = list(itertools.product([900, 3600], [None], [2.0, 3.0], [2.5, 4.0], [True, False]))
    grid = [(a, a * 4, c, d, e) for a, _, c, d, e in grid]
    with ProcessPoolExecutor() as ex:
        rows = list(ex.map(job, grid))
    rows.sort(key=lambda r: r["net_return_pct"], reverse=True)
    Path("reports/sweep_trend_train.json").write_text(json.dumps(rows, indent=1))
    for r in rows:
        print(json.dumps({**r["params"], **{k: r[k] for k in ["net_return_pct", "max_drawdown_pct", "trades", "win_rate", "profit_factor", "avg_net_bps_per_trade"]}, "ep": r["episode_return_pct"]}))
