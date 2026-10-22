"""Engine-level safety: execution guard, kill switch, allocation across restarts/retries,
foreign orders, untouched external assets, API outages, dashboard vs persisted records."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from test_engine import FakeMD, enter, make, settings
from wtrader.ai_agent import BaselineSelector
from wtrader.engine import Engine
from wtrader.executor import ACTIVE
from wtrader.models import Balance, OrderObservation, OrderState
from wtrader.monitor import dashboard_data, trade_stats, trades_csv
from wtrader.safety import KillSwitch
from wtrader.store import Store
from wtrader.venue import PaperVenue


def place_buy(eng, key, symbol="BTCTMN", qty="0.1", limit="1000500"):
    pid = eng.store.insert_position(symbol=symbol, family="TEST", candidate_id=key, stop=D("900000"), max_bars=10,
                                    signal_seconds=900)
    return eng.executor.place(economic_key=key, symbol=symbol, side="BUY", quantity=D(qty), limit_price=D(limit),
                              purpose="ENTRY", market=eng.markets[symbol], position_id=pid)


def blocked(eng):
    return [json.loads(e["payload_json"])["reason"] for e in eng.store.events("order_blocked")]


# ---------------------------------------------------------------- execution guard
def test_guard_rejects_order_above_max_order_even_if_risk_gate_is_bypassed(tmp_path):
    eng, md, venue = make(tmp_path)
    assert place_buy(eng, "BIG", qty="0.25") is None                 # ~250k TMN > 200k
    assert "max_order_tmn" in blocked(eng)[0]
    assert not eng.store.orders()                                    # nothing persisted, nothing sent
    assert place_buy(eng, "OK", qty="0.19") is not None


def test_guard_enforces_total_allocation_and_position_count(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.s.risk.max_positions = 10
    md.qty = "100000"
    # 5 x ~190k fills = ~950k committed; the 6th BUY would exceed the 1M budget
    md.prices.update({f"X{i}TMN": ("1000000", "1000500") for i in range(6)})
    from conftest import mk_market
    eng.markets.update({f"X{i}TMN": mk_market(f"X{i}TMN", min_notional="10000") for i in range(6)})
    for i in range(5):
        assert place_buy(eng, f"K{i}", symbol=f"X{i}TMN", qty="0.19") is not None
    assert place_buy(eng, "K5", symbol="X5TMN", qty="0.19") is None
    assert "allocation exceeded" in blocked(eng)[-1]
    eng.s.risk.max_positions = 2
    assert "max positions" in (place_buy(eng, "K6", symbol="X5TMN", qty="0.01") or blocked(eng)[0])


def test_duplicate_active_buy_per_symbol_blocked(tmp_path):
    eng, md, venue = make(tmp_path)
    md.qty = "0.0001"                                                # entry rests unfilled
    assert place_buy(eng, "A", limit="1000000") is not None
    assert place_buy(eng, "B", limit="1000000") is None
    assert "duplicate" in blocked(eng)[0]


def test_unknown_submission_keeps_reservation_and_blocks_across_restart(tmp_path):
    eng, md, venue = make(tmp_path)
    venue.inject.append("timeout_lost")                              # POST outcome ambiguous
    cid = place_buy(eng, "AMB", qty="0.19")
    assert eng.store.order(cid)["state"] == "UNKNOWN"
    st, _ = eng.portfolio_state(datetime.now(timezone.utc))
    assert st.entries_halted and st.committed_tmn >= D("190000")
    assert place_buy(eng, "NEXT", symbol="ETHTMN", qty="1") is None   # no new entries while unresolved
    eng.store.db.close()
    eng2, _, _ = make(tmp_path, md=md, venue=venue)                  # restart on the same DB
    st2, _ = eng2.portfolio_state(datetime.now(timezone.utc))
    assert st2.entries_halted and st2.committed_tmn >= D("190000")
    assert len(eng2.store.orders()) == 1                              # never resubmitted
    assert eng2.executor.place(economic_key="AMB", symbol="BTCTMN", side="BUY", quantity=D("0.19"),
                               limit_price=D("1000500"), purpose="ENTRY", market=eng2.markets["BTCTMN"]) is None


# ---------------------------------------------------------------- kill switch
def test_kill_halt_blocks_entries_immediately_cancels_entry_orders_and_keeps_exits(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    assert len(eng.store.positions()) == 1
    md.qty = "0.0001"
    cid = place_buy(eng, "RESTING", symbol="ETHTMN", qty="1", limit="99000")    # rests below the ask
    KillSwitch(eng.s.execution.kill_switch_path).engage("halt", "test")
    assert place_buy(eng, "AFTER", symbol="SOLTMN", qty="5") is None           # blocked before the next tick
    assert "kill switch" in blocked(eng)[-1]
    md.qty = "100"
    eng.tick()
    assert eng.store.order(cid)["state"] == "CANCELED"
    md.set("BTCTMN", "970000")                                        # below stop: protective exit still runs
    eng.tick()
    eng.tick()
    assert any(p["exit_reason"] == "STOP" for p in eng.store.closed_positions())
    st = json.loads(open(eng.s.status_path).read())
    assert st["kill_switch"]["level"] == "halt" and st["entries_halted"]


def test_kill_freeze_blocks_exits_too(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    KillSwitch(eng.s.execution.kill_switch_path).engage("freeze", "test")
    md.set("BTCTMN", "970000")
    eng.tick()
    eng.tick()
    assert eng.store.positions() and not any(o["side"] == "SELL" for o in eng.store.orders())


def test_kill_flatten_exits_only_bot_positions(tmp_path):
    eng, md, venue = make(tmp_path)
    venue.fund("ETH", D("3"))                                         # pre-existing asset, not the bot's
    eng.bootstrap()
    enter(eng, trail=None)
    KillSwitch(eng.s.execution.kill_switch_path).engage("flatten", "test")
    for _ in range(3):
        eng.tick()
    assert not eng.store.positions()
    assert eng.store.closed_positions()[0]["exit_reason"] == "KILL_FLATTEN"
    assert venue.balances()["ETH"].total == D("3")                   # untouched


def test_partial_fill_then_kill_keeps_protected_position(tmp_path):
    eng, md, venue = make(tmp_path)
    md.qty = "0.1"                                                    # level_fraction 0.5 -> 0.05 BTC fill per level
    cid = place_buy(eng, "PART", qty="0.19")
    o = eng.store.order(cid)
    assert o["state"] == "PARTIALLY_FILLED" and D(o["executed_qty"]) < D("0.19")
    KillSwitch(eng.s.execution.kill_switch_path).engage("halt", "test")
    md.qty = "0.0001"
    eng.tick()
    assert eng.store.order(cid)["state"] == "CANCELED"
    p = eng.store.positions()[0]
    assert p["status"] == "OPEN" and D(p["qty"]) > 0                  # partial position kept, still managed


# ---------------------------------------------------------------- reconciliation and external assets
def test_foreign_open_order_halts_entries_is_reserved_and_never_canceled(tmp_path):
    eng, md, venue = make(tmp_path)
    foreign = OrderObservation("MANUAL_1", OrderState.OPEN, D(0), D(0), D(0), D(0),
                               {"side": "BUY", "price": "100000", "origQty": "2", "symbol": "ETHTMN"})
    venue.open_orders = lambda: [foreign]
    canceled = []
    venue.cancel = lambda cid: canceled.append(cid)
    eng.reconcile_exchange()
    st, detail = eng.portfolio_state(datetime.now(timezone.utc))
    assert st.entries_halted and "foreign" in st.halt_reason
    assert D(detail["allocation"]["foreign_reserved_tmn"]) == D("200000")
    enter(eng)
    assert eng.store.decisions(1)[0]["final_action"] == "RISK_REJECTED"
    eng.tick()
    assert canceled == []


def test_liquidation_never_sells_pre_existing_assets(tmp_path):
    end = datetime.now(timezone.utc) + timedelta(hours=10)
    md = FakeMD()
    venue = PaperVenue(md, fee_fraction=D("0.0035"))
    venue.fund("TMN", D("5000000"))
    venue.fund("ETH", D("3"))                                          # in the account before the bot starts
    eng, _, _ = make(tmp_path, md=md, venue=venue, end=end)
    assert eng.store.get_kv("external_baseline") == {"ETH": "3"}
    enter(eng)
    eng.s.competition.trade_end = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    for _ in range(4):
        eng.tick()
    assert not eng.store.positions()
    assert venue.balances()["ETH"].total == D("3")
    assert not [o for o in eng.store.orders() if o["symbol"] == "ETHTMN"]


# ---------------------------------------------------------------- outages
def test_balance_outage_still_manages_exits_and_halts_entries(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    eng.tick()
    class Outage:                                                    # the engine's account reads fail
        def __init__(self, v):
            self.v = v

        def balances(self):
            raise ConnectionError("api down")

        def __getattr__(self, k):
            return getattr(self.v, k)
    eng.venue = Outage(venue)
    eng.s.market.max_account_data_age_seconds = 0
    md.set("BTCTMN", "970000")
    eng.tick()                                                       # must not raise; exit placed from cached balances
    assert any(o["side"] == "SELL" for o in eng.store.orders())
    st, _ = eng.portfolio_state(datetime.now(timezone.utc), refresh=False)
    assert st.entries_halted and "account data stale" in st.halt_reason
    assert place_buy(eng, "DURING_OUTAGE", symbol="ETHTMN", qty="1") is None
    assert "balance read failed" in blocked(eng)[-1]


def test_stale_market_data_halts_entries(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.tick()
    md.down = True
    eng.last_md_ok = datetime.now(timezone.utc) - timedelta(minutes=10)
    eng.tick()
    st, _ = eng.portfolio_state(datetime.now(timezone.utc))
    assert st.entries_halted and st.halt_reason == "market data stale"


# ---------------------------------------------------------------- dashboard vs persisted records
def test_dashboard_metrics_match_persisted_records(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    md.set("BTCTMN", "970000")                                        # stop-out: a losing trade
    eng.tick()
    eng.tick()
    enter(eng, symbol="ETHTMN", stop="98000", trail=None)
    eng.tick()
    eng.s.risk.max_positions = 2
    closed = eng.store.closed_positions()
    assert len(closed) == 1
    d = dashboard_data(eng.s)
    expected_pnl = sum(D(p["realized_pnl_tmn"]) for p in closed)
    assert D(d["performance"]["all_records"]["net_pnl_tmn"]) == expected_pnl.quantize(D("1"))
    assert d["performance"]["all_records"]["trades"] == 1 and d["performance"]["all_records"]["win_rate"] == "0.000"
    assert d["pnl"]["realized_net_tmn"] == f"{expected_pnl:.0f}"
    assert d["status"]["simulated"] is True and d["engine_alive"]
    assert len(d["status"]["open_positions"]) == 1 and d["status"]["open_positions"][0]["current_bid"]
    rows = list(csv.DictReader(io.StringIO(trades_csv(eng.s))))
    assert len(rows) == 2 and {r["status"] for r in rows} == {"CLOSED", "OPEN"}
    assert D(rows[0]["realized_pnl_tmn"]) == D(closed[0]["realized_pnl_tmn"])


def test_trade_stats_hand_checked():
    rows = [{"realized_pnl_tmn": "300", "fees_tmn": "10"}, {"realized_pnl_tmn": "-100", "fees_tmn": "10"},
            {"realized_pnl_tmn": "-50", "fees_tmn": "5"}]
    s = trade_stats(rows)
    assert s["net_pnl_tmn"] == "150" and s["fees_tmn"] == "25" and s["win_rate"] == "0.333"
    assert s["profit_factor"] == "2.000" and s["avg_win_tmn"] == "300" and s["avg_loss_tmn"] == "-75"


# ---------------------------------------------------------------- live activation gate
def test_live_refuses_without_operator_arm(tmp_path, monkeypatch):
    import argparse
    import pytest
    from wtrader import cli
    monkeypatch.setenv("LIVE_ENABLED", "true")
    monkeypatch.setenv("WALLEX_API_KEY", "k")
    monkeypatch.setenv("WALLEX_SUB_ACCOUNT_CLIENT_ID", "s")
    monkeypatch.setenv("WALLEX_ALLOWED_MARKETS", "BTCTMN")
    monkeypatch.setenv("TRADE_START", "2026-10-25T09:00:00+03:30")
    monkeypatch.setenv("TRADE_END", "2026-10-28T09:00:00+03:30")
    s = settings(tmp_path)
    s.ai.mode = "baseline"
    cli.prepare_live(s)
    args = argparse.Namespace(data_source="wallex")
    with pytest.raises(SystemExit) as e:
        cli.build_engine(s, args, live=True)
    assert "live not armed" in str(e.value)
    with pytest.raises(SystemExit):
        cli.build_engine(s, argparse.Namespace(data_source="kucoin_proxy"), live=True)
    assert s.db_path.endswith("live.sqlite")                            # live never shares the shadow DB
