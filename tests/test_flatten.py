from datetime import datetime, timedelta, timezone

from app import agent
from app.config import Settings
from app.risk import Decision, RiskState, check, in_flatten_window
from app.wallex import WallexClient


import pytest


@pytest.fixture(autouse=True)
def _tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "JOURNAL", [])
    monkeypatch.setattr(agent, "RISK_STATE", agent.RiskState())
    globals()["JOURNAL_FILE"] = tmp_path / "journal.jsonl"


def _s(**kw):
    return Settings(_env_file=None, journal_path=str(JOURNAL_FILE), **kw)


class FakeWallex(WallexClient):
    def __init__(self, s, balances):
        super().__init__(s)
        self._balances = balances
        self.orders = []

    def markets(self):
        m = {
            "BTCTMN": {"baseAsset": "BTC", "quoteAsset": "TMN", "stepSize": 8, "tickSize": 0, "minNotional": 50000,
                       "stats": {"bidPrice": "2000000000", "lastPrice": "2000000000"}},
            "ETHTMN": {"baseAsset": "ETH", "quoteAsset": "TMN", "stepSize": 4, "tickSize": 0, "minNotional": 50000,
                       "stats": {"bidPrice": "100000000", "lastPrice": "100000000"}},
        }
        for sym, x in m.items():
            self._info[sym] = x
        return m

    def balances(self):
        return self._balances

    def place_order(self, symbol, side, quantity, price):
        out = super().place_order(symbol, side, quantity, price)
        self.orders.append(out)
        return out


def test_flatten_window():
    end = datetime(2026, 10, 26, 23, 59, tzinfo=timezone(timedelta(hours=3, minutes=30)))
    s = _s(window_end=end, flatten_minutes_before=60)
    assert not in_flatten_window(s, end - timedelta(minutes=61))
    assert in_flatten_window(s, end - timedelta(minutes=59))
    assert not in_flatten_window(_s())


def test_no_buys_in_flatten_window():
    s = _s(dry_run=False, window_end=datetime.now(timezone.utc), max_position_pct=1)
    r = check(Decision("BTCTMN", "BUY", 0.9, 100000, ""), price=100.0, balances={"TMN": 1e6},
              equity=1e6, state=RiskState(), s=s)
    assert not r.approved and "flatten" in r.notes[0]


def test_flatten_sells_everything_to_tmn():
    s = _s(dry_run=True)
    wx = FakeWallex(s, {"TMN": 1_000_000, "BTC": 0.001, "ETH": 0.0001, "DOGE": 5})
    entries = agent.flatten(wx, s)
    by_sym = {e["symbol"]: e for e in entries}
    # BTC: 0.001 * 2e9 = 2M TMN -> sold below bid, price rounded to whole toman
    btc = by_sym["BTCTMN"]
    assert btc["approved"] and btc["execution"]["side"] == "SELL" and btc["execution"]["type"] == "LIMIT"
    assert btc["execution"]["quantity"] == "0.00100000"
    assert btc["execution"]["price"] == "1990000000"
    # ETH: 0.0001 * 1e8 = 10k TMN -> dust, below min notional
    assert not by_sym["ETHTMN"]["approved"] and "dust" in by_sym["ETHTMN"]["risk_notes"][0]
    # DOGE: no TMN market in the fake exchange
    assert not by_sym["DOGETMN"]["approved"]
    assert "TMNTMN" not in by_sym
    assert len(wx.orders) == 1


def test_order_quantity_rounds_down_to_step():
    s = _s(dry_run=True)
    wx = FakeWallex(s, {})
    out = wx.place_order("ETHTMN", "BUY", 0.123456789, 100000000.7)
    assert out["quantity"] == "0.1234" and out["price"] == "100000001"


def test_journal_is_persisted_and_reloaded():
    s = _s(dry_run=True)
    agent.flatten(FakeWallex(s, {"BTC": 0.001}), s)
    agent.record({"ts": 1, "symbol": "ETHTMN", "action": "HOLD", "equity": 5_000_000}, s)
    lines = JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    agent.JOURNAL.clear()
    assert agent.load_journal(s) == 2
    assert agent.JOURNAL[0]["symbol"] == "BTCTMN"
    assert agent.RISK_STATE.start_equity == 5_000_000
