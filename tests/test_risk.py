from app.config import Settings
from app.risk import Decision, RiskState, check


def _s(**kw):
    return Settings(_env_file=None, dry_run=False, max_order_quote=10, max_position_pct=0.5,
                    max_daily_loss_pct=0.05, min_confidence=0.6, **kw)


def _check(d, balances, equity, state=None, s=None):
    return check(d, price=100.0, balances=balances, equity=equity, state=state or RiskState(), s=s or _s())


def test_low_confidence_blocked():
    r = _check(Decision("BTCTMN", "BUY", 0.3, 5, ""), {"TMN": 100}, 100)
    assert not r.approved


def test_size_is_capped():
    r = _check(Decision("BTCTMN", "BUY", 0.9, 50, ""), {"TMN": 100}, 100)
    assert r.approved and r.decision.size_quote == 10


def test_insufficient_cash_blocked():
    r = _check(Decision("BTCTMN", "BUY", 0.9, 5, ""), {"TMN": 1}, 100)
    assert not r.approved


def test_kill_switch_on_drawdown():
    st = RiskState(start_equity=100)
    r = _check(Decision("BTCTMN", "BUY", 0.9, 5, ""), {"TMN": 90}, 90, state=st)
    assert not r.approved and st.halted


def test_sell_without_holdings_blocked():
    r = _check(Decision("BTCTMN", "SELL", 0.9, 5, ""), {"TMN": 100}, 100)
    assert not r.approved


def test_below_min_notional_blocked():
    r = check(Decision("BTCTMN", "BUY", 0.9, 5, ""), price=100.0, balances={"TMN": 100}, equity=100,
              state=RiskState(), s=_s(), min_notional=50)
    assert not r.approved and "min notional" in r.notes[-1]


def test_disabled_market_blocked():
    r = _check(Decision("ZECTMN", "BUY", 0.9, 5, ""), {"TMN": 100}, 100)
    assert not r.approved and "disabled" in r.notes[0]
