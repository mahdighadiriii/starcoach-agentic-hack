from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from conftest import mk_book, mk_market
from wtrader.config import CompetitionCfg, MarketCfg, RiskCfg
from wtrader.risk import OpenRisk, PortfolioState, competition_phase, evaluate_entry, exit_signal

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def state(**kw):
    base = dict(equity_tmn=D("5000000"), free_tmn=D("5000000"), exposures=(), day_start_equity_tmn=D("5000000"),
                high_water_tmn=D("5000000"))
    base.update(kw)
    return PortfolioState(**base)


def run(**kw):
    args = dict(symbol="BTCTMN", stop=D("980000"), max_stop_fraction=D("0.08"), min_stop_fraction=D("0.005"),
                multiplier=D("1.0"), state=state(), market=mk_market(), book=mk_book(at=NOW), now=NOW,
                fee_fraction=D("0.0035"), risk=RiskCfg(), mcfg=MarketCfg(), allowlist=["BTCTMN"], phase="TRADING")
    args.update(kw)
    return evaluate_entry(**args)


def failed(v):
    return {c.name for c in v.checks if not c.passed}


def test_happy_path_respects_risk_budget_and_caps():
    v = run()
    assert v.approved, failed(v)
    assert v.planned_loss_tmn <= D("25000.0001")          # 0.5% of 5M
    assert v.notional_tmn <= D("1500000")                 # 30% symbol cap


def test_multiplier_halves_and_is_bounded():
    big = RiskCfg(max_order_tmn="5000000")          # lift the order cap so the risk budget binds
    assert run(multiplier=D("0.5"), risk=big).quantity < run(risk=big).quantity
    assert "ai_multiplier_bounded" in failed(run(multiplier=D("2.0")))


def test_whitelist_positions_duplicates():
    assert "market_whitelisted" in failed(run(allowlist=["ETHTMN"]))
    two = (OpenRisk("ETHTMN", D("1"), D("1")), OpenRisk("SOLTMN", D("1"), D("1")))
    assert "max_concurrent_positions" in failed(run(state=state(exposures=two)))
    assert "no_duplicate_symbol" in failed(run(state=state(exposures=(OpenRisk("BTCTMN", D("1"), D("1")),))))


def test_exposure_and_aggregate_risk_caps():
    assert "aggregate_exposure_capacity" in failed(run(state=state(exposures=(OpenRisk("ETHTMN", D("3000000"), D("1")),))))
    assert "aggregate_stop_risk_capacity" in failed(run(state=state(exposures=(OpenRisk("ETHTMN", D("1"), D("60000")),))))


def test_spread_stale_and_slippage():
    assert "spread_limit" in failed(run(book=mk_book(bid="1000000", ask="1010000", at=NOW)))
    assert "book_fresh" in failed(run(book=mk_book(at=NOW - timedelta(seconds=60))))
    assert failed(run(book=mk_book(qty="0.00001", at=NOW))) & {"slippage_limit", "min_notional", "quantity_positive", "min_quantity"}


def test_stop_rules_and_balance():
    assert "stop_below_entry" in failed(run(stop=D("1001000")))
    assert "stop_distance_bounds" in failed(run(stop=D("999000")))
    assert failed(run(state=state(free_tmn=D("50000")))) & {"min_notional", "quantity_positive"}


def test_daily_loss_and_drawdown():
    assert "daily_loss_limit" in failed(run(state=state(equity_tmn=D("4890000"), free_tmn=D("4890000"))))
    s = state(equity_tmn=D("4790000"), free_tmn=D("4790000"), day_start_equity_tmn=D("4790000"))
    assert "drawdown_breaker" in failed(run(state=s))


def test_halt_and_phase():
    assert "entries_not_halted" in failed(run(state=state(entries_halted=True)))
    assert "phase_allows_entries" in failed(run(phase="NO_NEW_ENTRIES"))


def test_competition_schedule():
    c = CompetitionCfg(trade_start="2026-10-25T00:00:00+00:00", trade_end="2026-10-28T00:00:00+00:00")
    e = c.end_dt()
    assert competition_phase(e - timedelta(hours=5), c) == "TRADING"
    assert competition_phase(e - timedelta(hours=2), c) == "NO_NEW_ENTRIES"
    assert competition_phase(e - timedelta(minutes=30), c) == "LIQUIDATE"
    assert competition_phase(e, c) == "ENDED"
    assert competition_phase(c.start_dt() - timedelta(seconds=1), c) == "PRE_START"


def test_exit_priority():
    kw = dict(stop=D("90"), target=D("130"), bars_held=1, max_bars=10, phase="TRADING", drawdown_hit=False)
    assert exit_signal(bid=D("89"), **kw).reason == "STOP"
    assert exit_signal(bid=D("131"), **kw).reason == "TARGET"
    assert exit_signal(bid=D("100"), **{**kw, "bars_held": 10}).reason == "TIME_EXIT"
    assert exit_signal(bid=D("100"), **{**kw, "phase": "LIQUIDATE"}).reason == "LIQUIDATION"
    assert exit_signal(bid=D("100"), **{**kw, "drawdown_hit": True}).reason == "BREAKER"
    assert exit_signal(bid=D("100"), **kw) is None
