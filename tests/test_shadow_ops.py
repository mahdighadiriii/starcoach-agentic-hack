"""Shadow-mode operability: real WebSocket frame shape, reconnection, network binding and diagnosis,
categorized HOLD reasons, bootstrap status, read-only account sync, strategy version and the dashboard HTTP API.
All mocked: nothing here talks to Wallex."""
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal as D

import httpx
import pytest

from test_engine import FakeMD, make, settings
from wtrader import wallex
from wtrader.ai_agent import BaselineSelector
from wtrader.config import load_settings
from wtrader.engine import Engine
from wtrader.monitor import dashboard_data, make_server
from wtrader.store import Store
from wtrader.strategy import strategy_version
from wtrader.venue import PaperVenue
from wtrader.wallex import WallexClient, diagnose_network
from wtrader.wallex_ws import DepthStream

# Captured from wss://api.wallex.ir/connection/websocket on 2026-10-22 (levels trimmed)
REAL_CONNECT = '{"id":1,"connect":{"client":"2b1efd81","version":"6.9.1 OSS","ping":25,"pong":true}}'
REAL_SELL = ('{"push":{"channel":"orderbook:BTCTMN@sellDepth","pub":{"data":{"message":['
             '{"price":22112283587,"quantity":0.01005688,"sum":222380582.56042856},'
             '{"price":22115000000,"quantity":0.5,"sum":11057500000}]}}}}')
REAL_BUY = ('{"push":{"channel":"orderbook:BTCTMN@buyDepth","pub":{"data":{"message":['
            '{"price":22087909926,"quantity":0.0032707,"sum":72242926.99}]}}}}')


# ---------------------------------------------------------------- WebSocket
def test_ws_parses_the_real_server_payload_shape():
    s = DepthStream()
    s.connected = True
    assert s.handle(REAL_CONNECT) == []
    s.handle(REAL_SELL)
    s.handle(REAL_BUY)
    b = s.book("BTCTMN", 5)
    assert b is not None, "pub.data.message frames must be parsed (they were silently dropped before)"
    assert b.best_ask == D("22112283587") and b.best_bid == D("22087909926") and len(b.asks) == 2
    assert s.last_error is None


class FakeWS:
    def __init__(self, frames, hold=None):
        self.frames, self.sent, self.closed = list(frames), [], False
        self.status = None
        self.hold = hold                   # Event: keep the connection open until the test releases it

    def settimeout(self, t):
        pass

    def send(self, f):
        self.sent.append(f)

    def recv(self):
        if not self.frames:
            if self.hold is not None:
                self.hold.wait(3)
            return ""                      # server closed the connection
        f = self.frames.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def close(self):
        self.closed = True


def run_stream(connector, until, timeout=3.0):
    s = DepthStream(connector=connector)
    s.backoff_initial = 0.01
    s.watch(["BTCTMN"])
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if until(s):
            break
        deadline.wait(0.01)
    s.stop()
    s._thread.join(1)
    return s


def test_ws_reconnects_after_failures_and_never_serves_pre_gap_books():
    attempts, sockets, release = [], [], threading.Event()

    def connector(url, timeout, **kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise ConnectionRefusedError("first connect fails")
        ws = FakeWS([REAL_SELL, REAL_BUY, "{}"], hold=release) if len(attempts) == 2 else FakeWS([])
        sockets.append(ws)
        return ws

    seen = {}

    def until(s):
        if s.book("BTCTMN", 60) is not None:
            seen["book"] = True
            release.set()                  # now let the server drop the connection
        return len(attempts) >= 3 and seen

    s = run_stream(connector, until)
    assert seen.get("book"), "a two-sided book was served while connected"
    assert s.reconnects >= 2 and len(attempts) >= 3
    assert "{}" in sockets[0].sent, "server ping answered with pong"
    assert json.loads(sockets[0].sent[0]) == {"id": 1, "connect": {}}
    assert s.book("BTCTMN", 60) is None, "sides are cleared on disconnect"
    assert s.last_error is not None


def test_ws_fatal_close_code_stops_retrying():
    class Fatal(Exception):
        status_code = 3501
    calls = []

    def connector(url, timeout, **kw):
        calls.append(1)
        raise Fatal("bad request")
    s = run_stream(connector, lambda s: s.fatal is not None)
    assert s.fatal and len(calls) == 1


def test_ws_receives_bind_sockopts():
    got = {}

    def connector(url, timeout, **kw):
        got.update(kw)
        raise ConnectionError("stop")
    s = DepthStream(connector=connector, sockopt=((0, 25, 7),))
    s.backoff_initial = 0.01
    s.watch(["BTCTMN"])
    for _ in range(100):
        if got:
            break
        threading.Event().wait(0.01)
    s.stop()
    assert got["sockopt"] == ((0, 25, 7),)


# ---------------------------------------------------------------- network binding / diagnosis
def test_client_binds_interface_and_rebinds_when_address_changes():
    addrs = iter(["10.0.0.5", "10.0.0.5", "10.0.0.9"])
    c = WallexClient(bind_interface="en9", resolve_interface=lambda name: next(addrs), connect_timeout=0.2, timeout=0.2,
                     read_attempts=1, circuit_failures=99)
    assert c.local_address == "10.0.0.5" and c.health()["bind_interface"] == "en9"
    first = c.http
    c._fail("/x", "ConnectTimeout", transport=True)              # same address: client kept
    assert c.http is first
    c._fail("/x", "ConnectTimeout", transport=True)              # hotspot got a new address: rebuilt
    assert c.local_address == "10.0.0.9" and c.http is not first


def test_injected_http_client_is_never_replaced_by_rebind():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    c = WallexClient(http=http, bind_interface="en9", resolve_interface=lambda n: "10.0.0.1")
    c._fail("/x", "ConnectTimeout", transport=True)
    assert c.http is http


@pytest.mark.skipif(sys.platform != "darwin", reason="IP_BOUND_IF is macOS-specific")
def test_bind_sockopts_uses_ip_bound_if_on_macos():
    (lvl, opt, idx), = wallex.bind_sockopts("lo0")
    assert (lvl, opt, idx) == (socket.IPPROTO_IP, 25, socket.if_nametoindex("lo0"))


def test_diagnosis_names_vpn_tunnel_and_fix(monkeypatch):
    monkeypatch.setattr(wallex, "route_interface", lambda host: "utun4")
    monkeypatch.setattr(wallex.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("31.7.66.220", 443))])

    def no_connect(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(wallex.socket, "create_connection", no_connect)
    monkeypatch.setattr(wallex.httpx, "HTTPTransport", lambda **k: httpx.MockTransport(lambda r: (_ for _ in ()).throw(
        httpx.ConnectTimeout("handshake timed out"))))
    d = diagnose_network("https://api.wallex.ir", 0.1)
    assert d["failed_step"] == "tcp_connect" and d["route_interface"] == "utun4"
    assert "utun4" in d["verdict"] and "WALLEX_BIND_INTERFACE" in d["verdict"]


# ---------------------------------------------------------------- HOLD reasons
def _decide(eng):
    now = datetime.now(timezone.utc)
    as_of = int(now.timestamp()) // eng.s.market.signal_seconds * eng.s.market.signal_seconds
    state, _ = eng.portfolio_state(now)
    eng._start_decision(as_of, now, "UNSCHEDULED", state)
    return json.loads(eng.store.decisions(1)[0]["decision_json"])


class DeadCandles(FakeMD):
    def candles(self, *a):
        raise httpx.ConnectTimeout("handshake timed out")


def test_hold_reason_says_market_data_unavailable_not_strategy(tmp_path):
    eng, md, _ = make(tmp_path, md=DeadCandles())
    d = _decide(eng)
    assert d["category"] == "MARKET_DATA_UNAVAILABLE"
    assert "market data unavailable for all 3 markets" in d["reason"] and "ConnectTimeout" in d["reason"]
    assert d["markets"]["BTCTMN"] == "no market data (ConnectTimeout)"
    assert eng.store.decisions(1)[0]["final_action"] == "HOLD"


def test_hold_reason_explains_each_market_when_strategy_finds_no_setup(tmp_path):
    eng, md, _ = make(tmp_path)
    d = _decide(eng)
    assert d["category"] == "NO_SETUP" and d["data_source"] == "fake"
    assert set(d["markets"]) == {"BTCTMN", "ETHTMN", "SOLTMN"}
    assert all(v.startswith("insufficient data") for v in d["markets"].values())
    assert "BTCTMN insufficient data" in d["reason"]


class PartlyDead(FakeMD):
    def candles(self, symbol, *a):
        if symbol == "SOLTMN":
            raise httpx.ReadTimeout("slow")
        return []


def test_hold_reason_flags_partial_data_loss(tmp_path):
    eng, md, _ = make(tmp_path, md=PartlyDead())
    d = _decide(eng)
    assert d["category"] == "NO_SETUP_PARTIAL_DATA" and d["data_failed"] == {"SOLTMN": "ReadTimeout"}
    assert "no data: SOLTMN" in d["reason"]


# ---------------------------------------------------------------- bootstrap status / account sync
class Unreachable(FakeMD):
    def markets(self):
        raise httpx.ConnectTimeout("handshake timed out")


def test_bootstrap_failure_writes_status_the_dashboard_can_show(tmp_path, monkeypatch):
    s = settings(tmp_path)
    md = Unreachable()
    venue = PaperVenue(md)
    eng = Engine(s, Store(s.db_path, "RBOOT"), md, venue, BaselineSelector(), {"git_commit": "t"})
    monkeypatch.setattr("wtrader.engine.time.sleep", lambda x: None)
    with pytest.raises(httpx.ConnectTimeout):
        eng.run(max_ticks=1, bootstrap_attempts=2)
    st = json.loads(open(s.status_path).read())
    assert st["engine_state"] == "BOOTSTRAP_RETRY 2/2" and st["phase"] == "BOOTSTRAP"
    assert st["entries_halted"] and "bootstrap" in st["last_error"]["error"] and st["simulated"] is True
    d = dashboard_data(s)
    assert d["engine_alive"] and d["status"]["engine_state"].startswith("BOOTSTRAP")


class FlakyAccount:
    _sub = "SUB1"

    def __init__(self):
        self.fail = False

    def balances(self):
        if self.fail:
            raise wallex.TransientError("GET /v1/account/balances-detail: circuit open")
        from wtrader.models import Balance
        return {"TMN": Balance("TMN", D("4000000"), D("1000000"))}


def test_read_only_account_sync_keeps_last_good_balances_and_reports_error(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.account_client = acct = FlakyAccount()
    now = datetime.now(timezone.utc)
    eng.sync_exchange_account(now)
    x = eng.exchange_account
    assert x["read_only"] and x["endpoint"] == "/v1/account/balances-detail" and x["error"] is None
    assert x["balances"]["TMN"] == {"free": "4000000", "locked": "1000000", "total": "5000000"}
    acct.fail = True
    eng.sync_exchange_account(now)
    x = eng.exchange_account
    assert "circuit open" in x["error"] and x["balances"]["TMN"]["total"] == "5000000"
    assert x["synced_at"] == now.isoformat()
    # the paper venue's simulated balance is untouched by the real account read
    assert venue.balances()["TMN"].total == D("5000000")


# ---------------------------------------------------------------- strategy version
def test_strategy_version_names_the_running_families():
    s = load_settings("config/scalp.yaml")
    assert strategy_version(s.strategy.enabled_families, s.market.signal_seconds,
                            s.market.context_seconds) == "scalp-momentum/1.0.1 [5m/1h]"
    s = load_settings("config/strategy.yaml")
    assert strategy_version(s.strategy.enabled_families, 3600, 14400) == "donchian-trend/1.0.1 [1h/4h]"


def test_status_reports_derived_version(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.s.strategy.enabled_families = ["SCALP_MOMENTUM"]
    eng.tick()
    st = json.loads(open(eng.s.status_path).read())
    assert st["strategy"]["version"] == eng.strategy_version and "donchian-trend-1h4h" not in json.dumps(st)


# ---------------------------------------------------------------- dashboard HTTP API
def test_dashboard_http_api_serves_valid_json_html_and_engage_only_kill(tmp_path):
    eng, md, venue = make(tmp_path)
    eng.tick()
    _decide(eng)
    srv = make_server(eng.s, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        d = json.loads(urllib.request.urlopen(base + "/api/dashboard", timeout=5).read())
        assert d["engine_alive"] and d["orders_kind"] == "SIMULATED" and d["status"]["simulated"] is True
        assert d["decisions"][0]["category"] == "NO_SETUP" and d["decision_summary"]["by_category"]["NO_SETUP"] >= 1
        assert set(d["decisions"][0]["markets"]) == {"BTCTMN", "ETHTMN", "SOLTMN"}
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "<title>Wallex Trader Monitor</title>" in html and "decsum" in html
        for path in ("/status", "/decisions", "/orders", "/events"):
            json.loads(urllib.request.urlopen(base + path, timeout=5).read())
        req = urllib.request.Request(base + "/api/kill", method="POST", headers={"Origin": "http://evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 403 and eng.kill.status() is None
        urllib.request.urlopen(urllib.request.Request(base + "/api/kill", method="POST"), timeout=5)
        assert eng.kill.status()["level"] == "halt"
    finally:
        srv.shutdown()
