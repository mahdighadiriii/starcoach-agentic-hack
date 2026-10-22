"""Wallex integration: documented endpoints, trade-credit reads, health/circuit, WebSocket depth, secret
handling and the guarantee that shadow mode never sends an order. All Wallex responses are mocked."""
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from wtrader.config import RedactingFilter, Secrets, load_dotenv, redact
from wtrader.models import OrderIntent, TransientError
from wtrader.venue import WallexMarketData
from wtrader.wallex import TradingDisabled, WallexClient
from wtrader.wallex_ws import DepthStream

SECRET = "123456|SuperSecretWallexKeyValue0123456789"

HECTOR = {"result": {"markets": [
    {"symbol": "BTCTMN", "base_asset": "BTC", "quote_asset": "TMN", "is_spot": True, "amount_precision": 6, "price_precision": 0},
    {"symbol": "ETHTMN", "base_asset": "ETH", "quote_asset": "TMN", "is_spot": True, "amount_precision": 4, "price_precision": 0},
    {"symbol": "OLDTMN", "base_asset": "OLD", "quote_asset": "TMN", "is_spot": False, "amount_precision": 2, "price_precision": 0},
    {"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT", "is_spot": True, "amount_precision": 6, "price_precision": 2},
]}, "success": True}


def mock_client(handler, key="KEY", sub="SUB1", **kw):
    return WallexClient(key, sub, "https://api.test", read_attempts=kw.pop("read_attempts", 2),
                        http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def intent(cid="WABC"):
    return OrderIntent(cid, cid, "BTCTMN", "BUY", D("1000"), D("2"), "ENTRY", None, None)


def market():
    from conftest import mk_market
    return mk_market()


# ---------------------------------------------------------------- documented market data
def test_markets_use_documented_endpoint_and_skip_non_spot_without_undocumented_enrichment():
    def h(req):
        if req.url.path == "/hector/web/v1/markets":
            return httpx.Response(200, json=HECTOR)
        return httpx.Response(404, json={})        # undocumented /v1/markets absent: must not be required
    c = mock_client(h)
    m = c.markets()
    assert set(m) == {"BTCTMN", "ETHTMN"}
    assert m["ETHTMN"].step == D("0.0001") and m["BTCTMN"].min_notional == 0
    assert c.limits_source.startswith("none")


def test_candles_240_is_requested_natively():
    seen = []

    def h(req):
        seen.append(req.url.params["resolution"])
        return httpx.Response(200, json={"s": "ok", "t": [14400], "o": ["1"], "h": ["2"], "l": ["1"], "c": ["2"], "v": ["1"]})
    assert len(mock_client(h).candles("BTCTMN", 14400, 0, 28800)) == 1 and seen == ["240"]


# ---------------------------------------------------------------- account + trade credit
def test_main_account_balances_without_sub_account_header():
    seen = []

    def h(req):
        seen.append((req.url.path, req.headers.get("sub-account-client-id")))
        return httpx.Response(200, json={"result": {"balances": {"TMN": {"value": "500", "locked": "100"}}}})
    b = mock_client(h, sub=None).balances()
    assert seen == [("/v1/account/balances", None)] and b["TMN"].free == D("400") and b["TMN"].total == D("500")


def test_trade_credit_reads_sub_accounts_and_package():
    def h(req):
        assert req.headers["x-api-key"] == "KEY" and req.method == "GET"
        if req.url.path == "/sub-accounts":
            return httpx.Response(200, json={"result": [{"userId": 1, "clientId": "SUB1", "isActive": True, "isSuspend": False,
                                                         "type": "prop", "darkAvatarUrl": "x"}], "success": True})
        return httpx.Response(200, json={"result": {
            "packageDetail": {"title": "5M", "loan": "4000000", "assurance": "1000000", "currency": "TMN"},
            "userPackageDetail": {"status": "active", "warningLimit": "4300000", "liquidityLimit": "4100000",
                                  "connectToWallet": None, "expireAt": "2026-11-20T00:00:00Z"}, "alertsData": []}})
    c = mock_client(h)
    subs = c.sub_accounts()
    assert subs == [{"clientId": "SUB1", "title": None, "type": "prop", "isActive": True, "isSuspend": False, "createdAt": None}]
    pkg = c.active_package()
    assert pkg["package"]["loan"] == "4000000" and pkg["user_package"]["connectToWallet"] is None
    assert c.health()["auth"] == "ok"


def test_order_history_parses_keyed_lists():
    body = {"result": {"a": [{"clientOrderId": "W1"}], "b": [{"clientOrderId": "W2"}]}, "success": True}
    assert [o["clientOrderId"] for o in mock_client(lambda r: httpx.Response(200, json=body)).order_history()] == ["W1", "W2"]


def test_preflight_fails_when_trade_credit_wallet_is_linked_or_sub_account_inactive(tmp_path):
    from test_short_duration import FakeClient, pf_settings
    from wtrader.preflight import run_preflight
    sec = Secrets("k", "sub", "https://x", None)
    rep = run_preflight(pf_settings(tmp_path), FakeClient(linked_wallet="enabled"), sec)
    assert "trade_credit_wallet_isolation" in rep["failures"]
    rep = run_preflight(pf_settings(tmp_path), FakeClient(sub_active=False), sec)
    assert "trade_credit_sub_account_active" in rep["failures"]
    assert run_preflight(pf_settings(tmp_path), FakeClient(), sec)["passed"]


# ---------------------------------------------------------------- auth state, circuit, trading guard
def test_auth_state_tracks_rejection_and_missing_key():
    c = mock_client(lambda r: httpx.Response(401, json={}))
    with pytest.raises(Exception):
        c.balances()
    assert c.health()["auth"] == "rejected"
    assert mock_client(lambda r: httpx.Response(200, json={}), key=None).health()["auth"] == "not_configured"


def test_connect_timeouts_open_circuit_and_fail_fast():
    calls = []

    def h(req):
        calls.append(req.url.path)
        raise httpx.ConnectTimeout("handshake stalled", request=req)
    c = mock_client(h, read_attempts=3, circuit_failures=3, circuit_cooldown=60)
    with pytest.raises(TransientError):
        c.book("BTCTMN")
    assert len(calls) == 3 and c.circuit_open()
    with pytest.raises(TransientError, match="circuit open"):
        c.book("ETHTMN")
    assert len(calls) == 3                       # no request while open
    h_ = c.health()
    assert h_["circuit_open"] and h_["last_error"]["kind"] == "ConnectTimeout" and "egress" in h_["hint"]
    c.enable_trading()
    with pytest.raises(Exception, match="circuit open"):
        c.submit(intent(), market())
    assert len(calls) == 3                       # submit refused locally, nothing sent


def test_success_resets_transport_failures():
    n = []

    def h(req):
        n.append(1)
        if len(n) == 1:
            raise httpx.ConnectTimeout("x", request=req)
        return httpx.Response(200, json={"result": {"bid": [], "ask": []}})
    c = mock_client(h)
    c.book("BTCTMN")
    assert c.health()["consecutive_transport_failures"] == 0 and c.health()["hint"] is None


def test_trading_disabled_by_default_sends_nothing():
    sent = []
    c = mock_client(lambda r: sent.append(r) or httpx.Response(201, json={}))
    with pytest.raises(TradingDisabled):
        c.submit(intent(), market())
    with pytest.raises(TradingDisabled):
        c.cancel("WABC")
    assert sent == [] and c.health()["trading_enabled"] is False


# ---------------------------------------------------------------- secrets
def test_secret_never_in_errors_health_or_logs(monkeypatch, caplog):
    monkeypatch.setenv("WALLEX_API_KEY", SECRET)

    def h(req):
        return httpx.Response(422, json={"message": f"bad key {req.headers['x-api-key']}"})
    c = mock_client(h, key=SECRET)
    with pytest.raises(Exception) as e:
        c.balances()
    assert SECRET not in str(e.value) and SECRET not in json.dumps(c.health())
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "leak %s", (SECRET,), None)
    RedactingFilter().filter(rec)
    assert SECRET not in rec.getMessage() and redact(f"x{SECRET}y") == "x***y"
    assert SECRET not in repr(Secrets.from_env()) and SECRET not in str(Secrets.from_env())


def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nWALLEX_API_KEY='abc|def'\nexport TRADING_MODE=shadow  # inline\nAI_MODE=\"baseline\"\nEMPTY=\n")
    monkeypatch.delenv("WALLEX_API_KEY", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.setenv("AI_MODE", "ai")
    loaded = load_dotenv(f)
    import os
    assert os.environ["WALLEX_API_KEY"] == "abc|def" and os.environ["TRADING_MODE"] == "shadow"
    assert os.environ["AI_MODE"] == "ai" and "AI_MODE" not in loaded       # real environment wins
    assert load_dotenv(tmp_path / "missing") == []


def test_env_file_is_git_ignored_and_example_has_no_values():
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=root)
    assert r.returncode == 0, ".env must be ignored by git"
    for line in (root / ".env.example").read_text().splitlines():
        if line.startswith(("WALLEX_API_KEY=", "WALLEX_SUB_ACCOUNT_CLIENT_ID=", "ANTHROPIC_API_KEY=", "WALLEX_HTTP_PROXY=")):
            assert line.split("=", 1)[1] == "", line


# ---------------------------------------------------------------- WebSocket depth
class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def push(sym, kind, rows):
    return json.dumps({"push": {"channel": f"orderbook:{sym}@{kind}", "pub": {"data": rows}}})


def test_ws_protocol_frames_ping_and_snapshots():
    clk = Clock()
    s = DepthStream(clock=clk)
    s.symbols = ["BTCTMN"]
    frames = [json.loads(f) for f in s.subscribe_frames()]
    assert frames[0] == {"id": 1, "connect": {}}
    assert {f["subscribe"]["channel"] for f in frames[1:]} == {"orderbook:BTCTMN@buyDepth", "orderbook:BTCTMN@sellDepth"}
    assert s.handle("{}") == ["{}"]                                   # server ping -> pong
    s.connected = True
    s.handle(push("BTCTMN", "buyDepth", [{"price": 99.5, "quantity": 0.12345678, "sum": 1}, {"price": 100.1, "quantity": 1, "sum": 1},
                                         {"price": 98, "quantity": 0, "sum": 0}]))
    assert s.book("BTCTMN", 5) is None                                # one side only
    s.handle(push("BTCTMN", "sellDepth", [{"price": 102, "quantity": 3, "sum": 1}]) + "\n" + "{}")
    b = s.book("BTCTMN", 5)
    assert b.best_bid == D("100.1") and b.best_ask == D("102") and b.bids[1] == (D("99.5"), D("0.12345678"))
    clk.t += timedelta(seconds=6)
    assert s.book("BTCTMN", 5) is None                                # stale -> caller falls back to REST
    s.handle('{"id": 3, "error": {"code": 102, "message": "unknown channel"}}')
    assert s.status()["last_error"]["kind"] == "WS102"
    s.connected = False
    assert s.book("BTCTMN", 60) is None


def test_market_data_prefers_fresh_websocket_book_then_rest():
    rest = []

    def h(req):
        rest.append(req.url.path)
        return httpx.Response(200, json={"result": {"bid": [{"price": "1", "quantity": "1"}], "ask": [{"price": "2", "quantity": "1"}]}})
    s = DepthStream(clock=Clock())
    md = WallexMarketData(mock_client(h), s, ws_max_age_s=4)
    s.connected = True
    s.handle(push("BTCTMN", "buyDepth", [{"price": 10, "quantity": 1}]))
    s.handle(push("BTCTMN", "sellDepth", [{"price": 11, "quantity": 1}]))
    assert md.book("BTCTMN").best_bid == D("10") and rest == [] and md.book_via["BTCTMN"] == "websocket"
    assert md.book("ETHTMN").best_bid == D("1") and rest == ["/v1/depth"] and md.status()["book_via"]["ETHTMN"] == "rest"


# ---------------------------------------------------------------- end to end: shadow never sends orders
def test_shadow_run_against_mocked_wallex_sends_only_reads(tmp_path, monkeypatch):
    from wtrader import cli
    requests = []

    def h(req):
        requests.append((req.method, req.url.path))
        p = req.url.path
        if p == "/hector/web/v1/markets":
            return httpx.Response(200, json=HECTOR)
        if p == "/v1/depth":
            return httpx.Response(200, json={"result": {"bid": [{"price": "100", "quantity": "5"}], "ask": [{"price": "101", "quantity": "5"}]}})
        if p == "/v1/udf/history":
            return httpx.Response(200, json={"s": "no_data"})
        if p == "/v1/account/balances-detail":
            return httpx.Response(200, json={"result": {"TMN": {"total": "5000000", "freeze": "0", "available": "5000000"}}})
        return httpx.Response(404, json={})
    client = mock_client(h, key=SECRET)
    monkeypatch.setattr(cli, "make_client", lambda s, secrets: client)
    monkeypatch.setattr(client, "fork_reader", lambda: client)
    monkeypatch.chdir(tmp_path)
    for k, v in {"AI_MODE": "baseline", "WALLEX_API_KEY": SECRET, "WALLEX_SUB_ACCOUNT_CLIENT_ID": "SUB1",
                 "TRADING_MODE": "live", "LIVE_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "c.yaml").write_text("market:\n  candidate_symbols: [BTCTMN, ETHTMN]\n  depth_source: rest\n"
                                     "strategy:\n  market_filter_symbol: BTCTMN\nexecution:\n  exit_poll_seconds: 0.01\n")
    cli.main(["--config", "c.yaml", "shadow", "--max-ticks", "3"])
    assert requests and all(m == "GET" for m, _ in requests), requests
    assert not client.trading_enabled
    status = (tmp_path / "state" / "status.json").read_text()
    st = json.loads(status)
    assert st["simulated"] is True and st["venue"] == "paper" and st["mode"] == "shadow"
    assert st["exchange"]["wallex"]["auth"] == "ok" and st["exchange"]["account"]["balances"]["TMN"]["total"] == "5000000"
    assert SECRET not in status
    from wtrader.config import load_settings
    from wtrader.monitor import dashboard_data
    s = load_settings("c.yaml")
    d = dashboard_data(s)
    assert d["orders_kind"] == "SIMULATED"
    assert d["decisions"] and d["decisions"][0]["final_action"] == "HOLD" and d["decisions"][0]["reason"]
