from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from conftest import mk_book, mk_market
from wtrader.config import MarketCfg, RiskCfg
from wtrader.models import OrderObservation, OrderState
from wtrader.risk import PortfolioState, breaker_status, evaluate_entry
from wtrader.safety import (KillSwitch, arm_failures, arm_record, buy_reservation, check_buy, compute_allocation,
                            foreign_buy_reserved)

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
FEE = D("0.0035")


# ---------------------------------------------------------------- kill switch
def test_kill_switch_levels(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    assert ks.status() is None and ks.blocks("BUY") is None
    ks.engage("halt", "test")
    assert ks.blocks("BUY") and ks.blocks("SELL") is None          # exits keep working
    ks.engage("freeze", "worse")
    assert ks.blocks("BUY") and ks.blocks("SELL")
    ks.engage("halt", "weaker")                                       # never weakens an engaged switch
    assert ks.status()["level"] == "freeze"
    assert ks.clear() and ks.status() is None


def test_unreadable_kill_file_is_strictest(tmp_path):
    (tmp_path / "KILL").write_text("{not json")
    ks = KillSwitch(tmp_path / "KILL")
    assert ks.status()["level"] == "freeze" and ks.blocks("SELL")


# ---------------------------------------------------------------- allocation
def order(side="BUY", state="OPEN", qty="0.1", executed="0", limit="1000000", symbol="BTCTMN"):
    return {"side": side, "state": state, "quantity": qty, "executed_qty": executed, "limit_price": limit, "symbol": symbol}


def pos(qty="0.1", entry="1000000", cost="100000", proceeds="0", symbol="BTCTMN"):
    return {"symbol": symbol, "qty": qty, "entry_price": entry, "cost_tmn": cost, "proceeds_tmn": proceeds}


def alloc(**kw):
    args = dict(risk=RiskCfg(), positions=[], orders=[], closed=[], bids={}, fees={}, default_fee=FEE,
                foreign_reserved=D(0), exchange_free_tmn=D("5000000"))
    args.update(kw)
    return compute_allocation(**args)


def test_reservations_count_every_unresolved_buy_at_full_size():
    assert buy_reservation(order(state="UNKNOWN"), FEE) == D("0.1") * D("1000000") * (1 + FEE)
    assert buy_reservation(order(state="QUARANTINED", executed="0.04"), FEE) == D("0.06") * D("1000000") * (1 + FEE)
    assert buy_reservation(order(state="FILLED"), FEE) == 0
    assert buy_reservation(order(side="SELL"), FEE) == 0


def test_committed_budget_and_available():
    a = alloc(positions=[pos()], orders=[order(state="SUBMITTING")], foreign_reserved=D("50000"))
    assert a.position_cost_tmn == D("100000") and a.foreign_reserved_tmn == D("50000")
    assert a.committed_tmn == D("100000") + D("100350") + D("50000")
    assert a.available_tmn == D("1000000") - a.committed_tmn


def test_realized_losses_shrink_budget_profits_never_grow_it():
    loss = alloc(closed=[{"realized_pnl_tmn": "-40000"}])
    assert loss.budget_tmn == D("960000")
    win = alloc(closed=[{"realized_pnl_tmn": "250000"}])
    assert win.budget_tmn == D("1000000")
    # unrealized gains raise bot equity but never the budget or available capital
    up = alloc(positions=[pos()], bids={"BTCTMN": D("2000000")})
    assert up.unrealized_pnl_tmn > 0 and up.budget_tmn == D("1000000") and up.available_tmn == D("900000")


def test_available_is_capped_by_exchange_free_tmn():
    assert alloc(exchange_free_tmn=D("300000")).available_tmn == D("300000")


def test_check_buy_limits():
    a = alloc()
    r = RiskCfg()
    assert check_buy(a, D("200000"), r) is None
    assert "max_order_tmn" in check_buy(a, D("200001"), r)
    full = alloc(positions=[pos(qty="0.9", entry="1000000")])        # 900k committed
    assert "allocation exceeded" in check_buy(full, D("150000"), r)
    assert "exchange free" in check_buy(alloc(exchange_free_tmn=D("100000")), D("150000"), r)


def test_foreign_orders_are_detected_and_reserved():
    mine = OrderObservation("WABC", OrderState.OPEN, D(0), D(0), D(0), D(0), {"side": "BUY", "price": "10", "origQty": "5"})
    theirs = OrderObservation("MANUAL1", OrderState.PARTIALLY_FILLED, D("2"), D(0), D(0), D(0),
                              {"side": "BUY", "price": "1000", "origQty": "10", "symbol": "ETHTMN"})
    sell = OrderObservation("", OrderState.OPEN, D(0), D(0), D(0), D(0), {"side": "SELL", "price": "1", "origQty": "3"})
    total, rows = foreign_buy_reserved([mine, theirs, sell], {"WABC"})
    assert total == D("8000") and len(rows) == 2


# ---------------------------------------------------------------- live arming
def test_arm_record_binds_config_commit_and_allocation():
    rec = arm_record(operator="op", authorized_tmn="1000000", max_order_tmn="200000", config_hash="h", git_commit="c",
                     hours=12, preflight_at="t")
    ok = dict(authorized_tmn="1000000", max_order_tmn="200000", config_hash="h", git_commit="c")
    assert arm_failures(rec, **ok) == []
    assert arm_failures(None, **ok)
    assert arm_failures(rec, **{**ok, "authorized_tmn": "2000000"})          # raising the allocation needs re-arming
    assert arm_failures(rec, **{**ok, "config_hash": "other"})
    assert arm_failures(rec, **{**ok, "git_commit": "other"})
    assert arm_failures(rec, **ok, now=datetime.now(timezone.utc) + timedelta(hours=13))


# ---------------------------------------------------------------- risk gate additions
def state(**kw):
    base = dict(equity_tmn=D("1000000"), free_tmn=D("5000000"), exposures=(), day_start_equity_tmn=D("1000000"),
                high_water_tmn=D("1000000"), budget_tmn=D("1000000"), committed_tmn=D(0))
    base.update(kw)
    return PortfolioState(**base)


def run(**kw):
    args = dict(symbol="BTCTMN", stop=D("993000"), max_stop_fraction=D("0.03"), min_stop_fraction=D("0.004"),
                multiplier=D("1.0"), state=state(), market=mk_market(), book=mk_book(at=NOW), now=NOW,
                fee_fraction=FEE, risk=RiskCfg(), mcfg=MarketCfg(), allowlist=["BTCTMN"], phase="TRADING",
                target_r=D("12") / D("7"))
    args.update(kw)
    return evaluate_entry(**args)


def failed(v):
    return {c.name for c in v.checks if not c.passed}


def test_order_never_exceeds_max_order_allocation():
    v = run()
    assert v.approved, failed(v)
    assert v.notional_tmn * (1 + FEE) <= D("200000")


def test_allocation_capacity_and_remaining_budget_bind():
    assert "allocation_capacity" in failed(run(state=state(committed_tmn=D("1000000"))))
    v = run(state=state(committed_tmn=D("880000")))                  # only 120k left
    assert v.approved and v.notional_tmn * (1 + FEE) <= D("120000")
    assert failed(run(state=state(committed_tmn=D("950000")))) & {"min_notional", "within_allocation"}


def test_net_edge_gate_blocks_trades_that_cannot_cover_costs():
    assert "net_edge_covers_costs" not in failed(run())
    # 0.3% target with 0.35% fees per side cannot pay for itself
    assert "net_edge_covers_costs" in failed(run(target_r=D("3") / D("7")))
    assert "net_edge_covers_costs" in failed(run(fee_fraction=D("0.006")))


def test_absolute_daily_loss_limit():
    r = RiskCfg()
    assert not breaker_status(state(equity_tmn=D("971000")), r).daily_loss_hit
    assert breaker_status(state(equity_tmn=D("970000")), r).daily_loss_hit      # 30,000 TMN
    assert "daily_loss_limit" in failed(run(state=state(equity_tmn=D("969000"))))
