from decimal import Decimal as D

from conftest import trend_candles
from wtrader.ai_agent import BaselineSelector
from wtrader.backtest import CostModel, metrics, run_backtest
from wtrader.config import Settings
from wtrader.data import resample


def setup(n=900, wiggle=0.012):
    s = Settings()
    s.strategy.edge_gate = "advisory"
    s.market.signal_seconds, s.market.context_seconds = 3600, 14400
    s.strategy.enabled_families = ["DONCHIAN_TREND", "CONFIRMED_BREAKOUT"]
    s.strategy.stop_distance_min_fraction, s.strategy.stop_distance_max_fraction = "0.002", "0.2"
    s.strategy.market_filter_symbol = None
    sig = trend_candles(n, drift=0.0004, wiggle=wiggle)
    series = {"AAAUSDT": (sig, resample(sig, 3600, 14400))}
    return s, series, sig[0].open_time, sig[-1].open_time + 3600


def test_reproducible():
    s, ser, t0, t1 = setup()
    a = run_backtest(s, ser, t0, t1, BaselineSelector(), CostModel(), {}, "a")
    b = run_backtest(s, ser, t0, t1, BaselineSelector(), CostModel(), {}, "b")
    assert a.trades and [t.__dict__ for t in a.trades] == [t.__dict__ for t in b.trades]
    assert metrics(a, 5e6)["net_pnl_tmn"] == metrics(b, 5e6)["net_pnl_tmn"]


def test_no_lookahead_future_data_does_not_change_past_trades():
    s, ser, t0, t1 = setup()
    full = run_backtest(s, ser, t0, t1, BaselineSelector(), CostModel(), {}, "full", episode_days=1000)
    cut = t0 + (t1 - t0) // 2
    sig, ctx = ser["AAAUSDT"]
    trunc = {"AAAUSDT": ([c for c in sig if c.open_time < cut], [c for c in ctx if c.open_time + 14400 <= cut])}
    part = run_backtest(s, trunc, t0, cut, BaselineSelector(), CostModel(), {}, "part", episode_days=1000)
    done_before = [t.__dict__ for t in full.trades if t.closed_at < cut - 3600]
    assert done_before and done_before == [t.__dict__ for t in part.trades][:len(done_before)]


def test_fee_and_spread_accounting_by_hand():
    cm = CostModel(fee_fraction=D("0.0035"), half_spread_bps=D("8"), slippage_bps=D("5"))
    fill = cm.buy_price(D("100"))
    assert fill == D("100.13")
    qty = D("10")
    cost = qty * fill
    received = qty * (1 - cm.fee_fraction)
    sell = cm.sell_price(D("110"))
    proceeds = received * sell * (1 - cm.fee_fraction)
    # round trip ~ 2*35 bps fees + 2*13 bps spread/slippage on a +10% move
    net_bps = (proceeds / cost - 1) * 10000
    assert D("880") < net_bps < D("900")


def test_episode_liquidation_and_breaker_exist():
    s, ser, t0, t1 = setup()
    r = run_backtest(s, ser, t0, t1, BaselineSelector(), CostModel(), {}, "ep", episode_days=3)
    assert r.episodes and all("return_pct" in e for e in r.episodes)
    ep_starts = [e["start"] for e in r.episodes]
    for t in r.trades:
        assert not any(t.opened_at < st < t.closed_at for st in ep_starts[1:])
