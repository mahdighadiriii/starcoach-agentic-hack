"""Parameter sweep on the TRAIN split only (baseline selector). Usage: python scripts/sweep.py"""
import itertools, json, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wtrader.config import Settings
from wtrader.backtest import load_series, run_backtest, metrics, CostModel
from wtrader.ai_agent import BaselineSelector

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "TRXUSDT"]
TRAIN_DAYS = 160

def job(args):
    fam, sig_s, ctx_s, stop_mult, target_r, min_stop, hold = args
    s = Settings()
    s.market.signal_seconds, s.market.context_seconds, s.market.warmup_bars = sig_s, ctx_s, 120
    s.strategy.enabled_families = [fam]
    s.strategy.atr_stop_multiplier, s.strategy.target_r_multiple = str(stop_mult), str(target_r)
    s.strategy.stop_distance_min_fraction, s.strategy.stop_distance_max_fraction = str(min_stop), "0.08"
    s.strategy.max_holding_bars = hold
    ser = load_series(Path("data"), "binance_proxy", SYMS, sig_s, ctx_s)
    t0 = ser["BTCUSDT"][0][0].open_time
    r = run_backtest(s, ser, t0, t0 + TRAIN_DAYS * 86400, BaselineSelector(), CostModel(), {}, str(args), record_decisions=False)
    m = metrics(r, 5e6)
    return {"params": dict(zip(["family", "sig_s", "ctx_s", "atr_stop", "target_r", "min_stop", "hold"], args)),
            **{k: m[k] for k in ["net_return_pct", "max_drawdown_pct", "trades", "win_rate", "profit_factor",
                                 "avg_net_bps_per_trade", "episode_return_pct"]}}

if __name__ == "__main__":
    grid = list(itertools.product(["TREND_PULLBACK", "CONFIRMED_BREAKOUT", "RSI_REVERSION"],
                                  [3600], [14400], [2.0, 3.0], [2.0, 3.0], [0.012, 0.02], [24, 48]))
    with ProcessPoolExecutor() as ex:
        rows = list(ex.map(job, grid))
    rows.sort(key=lambda r: r["net_return_pct"], reverse=True)
    Path("reports").mkdir(exist_ok=True)
    Path("reports/sweep_train.json").write_text(json.dumps(rows, indent=1))
    for r in rows[:12]:
        print(json.dumps(r))
