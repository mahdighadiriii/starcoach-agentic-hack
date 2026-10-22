from decimal import Decimal as D

from conftest import trend_candles
from wtrader import indicators as ind
from wtrader.config import StrategyCfg
from wtrader.data import resample
from wtrader.strategy import Analysis, analyze, apply_market_filter, closed, trail_stop


def test_ema_seed_and_recursion():
    out = ind.ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0
    assert abs(out[3] - 3.0) < 1e-12


def test_rsi_edge_cases():
    assert ind.rsi([1.0] * 20, 14)[-1] == 50.0
    assert ind.rsi([float(i) for i in range(20)], 14)[-1] == 100.0


def test_atr_wilder():
    assert ind.atr([2.0] * 20, [1.0] * 20, [1.5] * 20, 14)[-1] == 1.0


def test_closed_filter_prevents_lookahead():
    cs = trend_candles(10)
    as_of = cs[5].open_time + 3600
    got = closed(cs, 3600, as_of)
    assert got[-1].open_time == cs[5].open_time


def test_resample_keeps_only_complete_aligned_buckets():
    cs = trend_candles(10, start=14400 * 1000 + 3600)   # starts mid-bucket
    out = resample(cs, 3600, 14400)
    assert all(c.open_time % 14400 == 0 for c in out)
    first = [c for c in cs if c.open_time >= out[0].open_time][:4]
    assert out[0].open == first[0].open and out[0].close == first[-1].close
    assert out[0].high == max(c.high for c in first) and out[0].volume == sum(c.volume for c in first)
    assert len(out) == 1   # 3 leading bars dropped, one complete bucket, partial trailing bucket dropped


def test_analysis_deterministic_and_ignores_future_bars():
    cfg = StrategyCfg(enabled_families=["DONCHIAN_TREND", "TREND_PULLBACK", "CONFIRMED_BREAKOUT"])
    sig, ctx = trend_candles(300), trend_candles(120, seconds=14400)
    as_of = sig[250].open_time + 3600
    a1 = analyze("BTCTMN", sig, ctx, as_of, cfg, 3600, 14400, 120)
    a2 = analyze("BTCTMN", sig[:251], ctx, as_of, cfg, 3600, 14400, 120)
    assert a1 == a2 and a1.regime == "TREND_UP"


def test_donchian_candidate_levels():
    cfg = StrategyCfg(enabled_families=["DONCHIAN_TREND"], stop_distance_min_fraction="0.001", stop_distance_max_fraction="0.2")
    sig = trend_candles(300, wiggle=0.0, drift=0.003)
    ctx = trend_candles(120, seconds=14400, wiggle=0.0, drift=0.01)
    a = analyze("BTCTMN", sig, ctx, sig[-1].open_time + 3600, cfg, 3600, 14400, 120)
    assert len(a.candidates) == 1
    c = a.candidates[0]
    assert c.stop < c.entry_ref and c.trail_distance > 0 and c.target_for(c.entry_ref) > c.entry_ref


def test_insufficient_data():
    a = analyze("BTCTMN", trend_candles(20), trend_candles(20, seconds=14400), 1_800_000_000, StrategyCfg())
    assert a.regime == "INSUFFICIENT_DATA" and a.candidates == ()


def test_market_filter():
    a = Analysis("ETHTMN", 0, "TREND_UP", {}, ("x",))
    assert not apply_market_filter([a, Analysis("BTCTMN", 0, "RANGE", {}, ())], "BTCTMN")[0].candidates
    assert apply_market_filter([a, Analysis("BTCTMN", 0, "TREND_UP", {}, ())], "BTCTMN")[0].candidates == ("x",)


def test_trailing_stop_only_ratchets_up():
    assert trail_stop(D("90"), D("120"), D("10")) == D("110")
    assert trail_stop(D("110"), D("105"), D("10")) == D("110")
    assert trail_stop(D("90"), D("200"), None) == D("90")
