"""Short-duration (scalp) strategy, intrabar backtest exits, Wallex 1m/5m candles, preflight."""
import json
from decimal import Decimal as D

import httpx

from conftest import mk_book, mk_market, trend_candles
from wtrader.ai_agent import BaselineSelector
from wtrader.backtest import CostModel, run_backtest
from wtrader.config import Secrets, Settings
from wtrader.data import resample
from wtrader.models import Balance, Candle, OrderObservation, OrderState
from wtrader.preflight import run_preflight
from wtrader.strategy import analyze
from wtrader.wallex import WallexClient


def scalp_settings():
    s = Settings()
    s.strategy.edge_gate = "advisory"
    s.market.signal_seconds, s.market.context_seconds = 300, 3600
    s.strategy.enabled_families = ["SCALP_MOMENTUM", "SCALP_REVERSION"]
    s.strategy.stop_distance_min_fraction, s.strategy.stop_distance_max_fraction = "0.004", "0.03"
    s.strategy.scalp_min_atr_fraction = "0.0005"
    s.strategy.scalp_volume_ratio = "1.0"
    s.strategy.scalp_rsi_max = "101"                             # a steady synthetic uptrend has RSI ~100
    s.strategy.market_filter_symbol = None
    return s


def minute_series(n=60 * 24 * 4, drift=0.0005, wiggle=0.0002):
    return trend_candles(n, seconds=60, drift=drift, wiggle=wiggle)


def test_scalp_candidates_use_fixed_fraction_stop_target_and_30_min_hold():
    s = scalp_settings()
    one = minute_series()
    sig, ctx = resample(one, 60, 300), resample(one, 60, 3600)
    found = []
    for k in range(200, len(sig)):
        as_of = sig[k].open_time + 300
        a = analyze("AAATMN", sig[:k + 1], ctx, as_of, s.strategy, 300, 3600, s.market.warmup_bars)
        found += list(a.candidates)
    assert found, "synthetic uptrend should produce scalp candidates"
    for c in found:
        assert c.family in ("SCALP_MOMENTUM", "SCALP_REVERSION")
        assert abs(c.stop_fraction - D("0.007")) < D("0.00001")
        assert c.target_r == D("0.012") / D("0.007")
        assert c.max_holding_bars * 300 == 30 * 60
        assert c.trail_distance is None
        tgt = c.target_for(c.entry_ref)
        assert abs((tgt / c.entry_ref - 1) - D("0.012")) < D("0.00001")


def test_scalp_quiet_market_produces_nothing():
    s = scalp_settings()
    s.strategy.scalp_min_atr_fraction = "0.05"                   # ATR floor far above the synthetic volatility
    one = minute_series()
    sig, ctx = resample(one, 60, 300), resample(one, 60, 3600)
    as_of = sig[-1].open_time + 300
    assert analyze("AAATMN", sig, ctx, as_of, s.strategy, 300, 3600, 120).candidates == ()


def test_intrabar_backtest_respects_latency_and_max_hold():
    s = scalp_settings()
    one = minute_series()
    series = {"AAAUSDT": (resample(one, 60, 300), resample(one, 60, 3600))}
    t0, t1 = one[0].open_time, one[-1].open_time + 60
    r = run_backtest(s, series, t0, t1, BaselineSelector(), CostModel(), {}, "x", exit_series={"AAAUSDT": one},
                     exit_s=60, fill_delay_s=60)
    assert r.trades
    for t in r.trades:
        if t.exit_reason == "LIQUIDATION":
            continue
        held = t.closed_at - t.opened_at
        assert 60 <= held <= 30 * 60, (t.exit_reason, held)
        assert t.opened_at % 300 == 60                         # filled one minute after the 5m decision
        assert t.cost_tmn * (1) <= 200000 * 1.0000001         # never above the max order
    # determinism
    r2 = run_backtest(s, series, t0, t1, BaselineSelector(), CostModel(), {}, "y", exit_series={"AAAUSDT": one},
                      exit_s=60, fill_delay_s=60)
    assert [t.__dict__ for t in r.trades] == [t.__dict__ for t in r2.trades]


def test_wallex_5m_candles_resampled_from_1m():
    calls = []

    def h(req):
        calls.append(dict(req.url.params))
        start = int(req.url.params["from"]) // 60 * 60
        ts = [start + 60 * i for i in range(10)]
        return httpx.Response(200, json={"s": "ok", "t": ts, "o": ["1"] * 10, "h": [str(2 + i) for i in range(10)],
                                         "l": ["0.5"] * 10, "c": [str(1 + i) for i in range(10)], "v": ["1"] * 10})
    c = WallexClient(None, None, "https://api.test", http=httpx.Client(transport=httpx.MockTransport(h)))
    out = c.candles("BTCTMN", 300, 1_700_000_100, 1_700_000_700)
    assert calls[0]["resolution"] == "1"
    assert all(x.open_time % 300 == 0 for x in out) and out
    assert c.last_ok_at is not None


class FakeClient:
    def __init__(self, tmn="1000000", foreign=False, fee="0.0025", linked_wallet=None, sub_active=True):
        self.tmn, self.foreign, self.fee = D(tmn), foreign, D(fee)
        self.linked_wallet, self.sub_active = linked_wallet, sub_active
        self.last_rate_headers = {}
        self.trading_enabled = False

    def markets(self):
        return {s: mk_market(s) for s in ("BTCTMN", "ETHTMN")}

    def spot_flags(self):
        return {"BTCTMN": True, "ETHTMN": True}

    def book(self, sym):
        return mk_book(sym, qty="100")

    def balances(self):
        return {"TMN": Balance("TMN", self.tmn, D(0))}

    def taker_fees(self):
        return {"BTCTMN": self.fee, "ETHTMN": self.fee}

    def open_orders(self):
        if not self.foreign:
            return []
        return [OrderObservation("X1", OrderState.OPEN, D(0), D(0), D(0), D(0), {"side": "BUY", "price": "10", "origQty": "1"})]

    def order_history(self):
        return []

    def sub_accounts(self):
        return [{"clientId": "sub", "isActive": self.sub_active, "isSuspend": False, "type": "prop"}]

    def active_package(self):
        return {"package": {"loan": "4000000"}, "user_package": {"status": "active", "connectToWallet": self.linked_wallet}}

    def submit(self, *a, **k):
        raise AssertionError("preflight must never place orders")

    cancel = submit


def pf_settings(tmp_path):
    s = Settings()
    s.strategy.edge_gate = "advisory"
    s.market.candidate_symbols = ["BTCTMN", "ETHTMN"]
    s.market.account_allowlist = ["BTCTMN"]
    s.execution.kill_switch_path = str(tmp_path / "KILL")
    s.execution.preflight_path = str(tmp_path / "pre.json")
    return s


def test_preflight_passes_on_clean_isolated_account(tmp_path):
    rep = run_preflight(pf_settings(tmp_path), FakeClient(), Secrets("k", "sub", "https://x", None))
    assert rep["passed"], rep["failures"]
    assert json.loads((tmp_path / "pre.json").read_text())["passed"]


def test_preflight_fails_on_foreign_orders_low_balance_kill_and_missing_sub_account(tmp_path):
    s = pf_settings(tmp_path)
    rep = run_preflight(s, FakeClient(tmn="500000", foreign=True), Secrets("k", None, "https://x", None))
    assert {"tmn_covers_allocation", "no_foreign_open_orders", "sub_account_id_present"} <= set(rep["failures"])
    from wtrader.safety import KillSwitch
    KillSwitch(s.execution.kill_switch_path).engage("halt", "t")
    assert "kill_switch_clear" in run_preflight(s, FakeClient(), Secrets("k", "sub", "https://x", None))["failures"]


def test_preflight_warns_when_account_holds_more_than_allocation(tmp_path):
    rep = run_preflight(pf_settings(tmp_path), FakeClient(tmn="5000000", fee="0.005"), Secrets("k", "sub", "https://x", None))
    assert rep["passed"] and {"allocation_isolation", "fees"} <= set(rep["warnings"])


def test_strict_edge_gate_blocks_families_without_positive_edge():
    from wtrader.strategy import Analysis, Candidate, apply_edge_gate
    c = Candidate("X-SCALP_MOMENTUM-1", "XTMN", "SCALP_MOMENTUM", 1, D("1"), D("0.99"), D("0.01"), D("2"), 6, {})
    a = [Analysis("XTMN", 1, "TREND_UP", {}, (c,))]
    neg = {"SCALP_MOMENTUM": {"trades": 164, "mean_net_bps": -88.4}}
    pos = {"SCALP_MOMENTUM": {"trades": 40, "mean_net_bps": 12.0}}
    assert apply_edge_gate(a, neg, "strict")[0].candidates == ()
    assert apply_edge_gate(a, {}, "strict")[0].candidates == ()
    assert apply_edge_gate(a, {"SCALP_MOMENTUM": {"trades": 5, "mean_net_bps": 50}}, "strict")[0].candidates == ()
    assert apply_edge_gate(a, pos, "strict")[0].candidates == (c,)
    assert apply_edge_gate(a, neg, "advisory")[0].candidates == (c,)
