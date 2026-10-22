import json
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from conftest import mk_book, mk_market
from wtrader.ai_agent import BaselineSelector, Decision, build_context
from wtrader.config import Settings
from wtrader.engine import Engine
from wtrader.store import Store
from wtrader.strategy import Analysis, Candidate
from wtrader.venue import PaperVenue


class FakeMD:
    label = "fake"

    def __init__(self):
        self.prices = {"BTCTMN": ("1000000", "1000500"), "ETHTMN": ("100000", "100050"), "SOLTMN": ("10000", "10005")}
        self.qty = "100"
        self.down = False

    def markets(self):
        return {s: mk_market(s, min_notional="10000") for s in self.prices}

    def candles(self, *a):
        return []

    def book(self, symbol):
        if self.down:
            raise ConnectionError("market data outage")
        bid, ask = self.prices[symbol]
        return mk_book(symbol, bid, ask, qty=self.qty)

    def set(self, symbol, bid, ask=None):
        self.prices[symbol] = (bid, ask or str(D(bid) * D("1.0005")))


def settings(tmp_path, end=None):
    s = Settings()
    s.db_path, s.evidence_dir, s.status_path = str(tmp_path / "t.sqlite"), str(tmp_path / "ev"), str(tmp_path / "st.json")
    s.execution.kill_switch_path = str(tmp_path / "KILL")
    s.execution.live_arm_path, s.execution.preflight_path = str(tmp_path / "arm.json"), str(tmp_path / "pre.json")
    s.market.candidate_symbols = ["BTCTMN", "ETHTMN", "SOLTMN"]
    s.strategy.market_filter_symbol = None
    s.strategy.edge_artifact = None
    s.ai.mode = "baseline"
    s.strategy.stop_distance_min_fraction, s.strategy.stop_distance_max_fraction = "0.008", "0.5"
    if end:
        s.competition.trade_start = (end - timedelta(days=3)).isoformat()
        s.competition.trade_end = end.isoformat()
    return s


def make(tmp_path, md=None, venue=None, end=None):
    s = settings(tmp_path, end)
    md = md or FakeMD()
    if venue is None:
        venue = PaperVenue(md, fee_fraction=D("0.0035"))
        venue.fund("TMN", D("5000000"))
    eng = Engine(s, Store(s.db_path, "RTEST"), md, venue, BaselineSelector(), {"git_commit": "test"})
    eng.bootstrap()
    return eng, md, venue


def enter(eng, symbol="BTCTMN", stop="980000", trail="40000", ref=None):
    now = int(datetime.now(timezone.utc).timestamp())
    ref = ref or eng.md.prices[symbol][1]
    cand = Candidate(f"{symbol}-DONCHIAN_TREND-{now}", symbol, "DONCHIAN_TREND", now, D(ref), D(stop),
                     D("0.05"), D("8"), 72, {}, D(trail) if trail else None)
    a = [Analysis(symbol, now, "TREND_UP", {"rsi14": 55.0}, (cand,))]
    ctx = build_context(as_of=now, analyses=a, portfolio={}, edge={}, feedback={}, minutes_to_entry_cutoff=None)
    did = eng.store.record_decision(snapshot_id=ctx["snapshot_id"], n_candidates=1, action="PENDING", source="baseline",
                                    decision={}, final_action="PENDING")
    fut = Future()
    fut.set_result(Decision("SELECT", cand.candidate_id, "1.0", "baseline"))
    eng._finish_decision(fut, ctx, a, did, "UNSCHEDULED")
    return did, cand


def test_entry_risk_order_position_and_trace(tmp_path):
    eng, md, venue = make(tmp_path)
    did, cand = enter(eng)
    d = eng.store.decisions(1)[0]
    assert d["final_action"] == "ENTRY_SUBMITTED", d["risk_json"]
    pos = eng.store.positions()[0]
    assert pos["status"] == "OPEN" and D(pos["qty"]) > 0 and pos["decision_id"] == did
    risk = json.loads(d["risk_json"])
    assert risk["approved"] and all(c["passed"] for c in risk["checks"])
    # the same candidate can never be entered twice
    enter(eng)
    assert eng.store.decisions(1)[0]["final_action"] in ("RISK_REJECTED", "DUPLICATE_SUPPRESSED")
    assert len(eng.store.positions()) == 1


def test_restart_recovery_then_stop_exit(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng)
    eng.store.db.close()
    # new process: same DB and the same (paper) venue state
    eng2, _, _ = make(tmp_path, md=md, venue=venue)
    assert len(eng2.store.positions()) == 1
    md.set("BTCTMN", "940000")                    # below stop
    eng2.tick()
    eng2.tick()
    closed = eng2.store.closed_positions()
    assert closed and closed[0]["exit_reason"] == "STOP" and D(closed[0]["realized_pnl_tmn"]) < 0
    assert json.loads(Path(eng2.s.status_path).read_text())["simulated"] is True


def test_drawdown_breaker_flattens_and_latches(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.s.risk.risk_per_trade_fraction, eng.s.risk.max_aggregate_stop_risk_fraction = "0.05", "0.2"
    enter(eng, stop="700000")                     # wide stop so the breaker, not the stop, fires first
    eng.tick()
    md.set("BTCTMN", "560000")                    # large gap down
    eng.tick()
    eng.tick()
    assert eng.store.get_kv("breaker_latched") is True
    assert not eng.store.positions()
    assert eng.store.closed_positions()[0]["exit_reason"] == "BREAKER"
    st, _ = eng.portfolio_state(datetime.now(timezone.utc))
    assert st.entries_halted and "breaker" in st.halt_reason


def test_daily_loss_halts_entries(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.s.risk.risk_per_trade_fraction, eng.s.risk.max_aggregate_stop_risk_fraction = "0.05", "0.2"
    enter(eng, stop="700000", trail=None)         # 200k position (max order), wide stop, no trailing
    eng.tick()
    md.set("BTCTMN", "790000")                    # ~-21% on ~160k: > 30k TMN bot loss, < 4% drawdown breaker
    eng.tick()
    st, _ = eng.portfolio_state(datetime.now(timezone.utc))
    assert st.entries_halted and st.halt_reason == "daily loss limit"
    assert not eng.store.get_kv("breaker_latched")


def test_external_tmn_movement_is_not_a_bot_loss(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.tick()
    venue.balances_["TMN"] -= D("1500000")        # someone else moves account funds
    eng.tick()
    st, _ = eng.portfolio_state(datetime.now(timezone.utc))
    assert not st.entries_halted and st.equity_tmn == D("1000000")


def test_final_liquidation_exits_bot_positions_and_preserves_external_deposits(tmp_path):
    end = datetime.now(timezone.utc) + timedelta(hours=10)
    eng, md, venue = make(tmp_path, end=end)
    enter(eng)
    venue.fund("ETH", D("3"))                    # residual holding not owned by any position
    assert eng.store.positions()
    eng.s.competition.trade_end = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()   # LIQUIDATE phase
    for _ in range(4):
        eng.tick()
    assert not eng.store.positions()
    assert eng.store.closed_positions()[0]["exit_reason"] == "LIQUIDATION"
    eng.s.competition.trade_end = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()    # ENDED
    for _ in range(3):
        eng.tick()
    rep = json.loads(Path(eng.s.evidence_dir, "final_report.json").read_text())
    assert rep["simulated"] is True and set(rep["non_tmn_residuals"]) == {"ETH"} and set(rep["dust_below_min_order"]) <= {"BTC", "ETH"}
    assert venue.balances_["ETH"] == D("3")
    assert not any(o["symbol"] == "ETHTMN" for o in eng.store.orders())
    assert D(rep["final_tmn"]) > 0


def test_no_entries_in_no_new_entries_phase(tmp_path):
    end = datetime.now(timezone.utc) + timedelta(hours=2)          # inside 180-minute entry halt window
    eng, md, venue = make(tmp_path, end=end)
    enter(eng)
    assert eng.store.decisions(1)[0]["final_action"] == "RISK_REJECTED"
    assert not eng.store.positions()


def test_bootstrap_retries_transient_failures(tmp_path, monkeypatch):
    import wtrader.engine as em
    monkeypatch.setattr(em.time, "sleep", lambda s: None)
    s = settings(tmp_path)
    md = FakeMD()
    calls = []
    orig = md.markets

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("network")
        return orig()
    md.markets = flaky
    venue = PaperVenue(md)
    venue.fund("TMN", D("5000000"))
    eng = Engine(s, Store(s.db_path, "RTEST"), md, venue, BaselineSelector(), {})
    eng.run(max_ticks=1)
    assert len(calls) == 3 and eng.counters["ticks"] == 1
    assert len(eng.store.events("bootstrap_error")) == 2
