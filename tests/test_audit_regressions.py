"""Deterministic outage/recovery regressions. No exchange traffic and only temporary databases."""
import json
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from conftest import mk_book, mk_market, trend_candles
from test_engine import make, enter, FakeMD
from wtrader.buffered import BufferedMarketData
from wtrader.config import load_settings, Settings, validate_settings
from wtrader.data import resample, parse_wallex_candles
from wtrader.models import OrderIntent, OrderState, OrderObservation, TransientError
from wtrader.safety import KillSwitch, compute_allocation
from wtrader.strategy import analyze
from wtrader.venue import PaperVenue
from wtrader.wallex import WallexClient
from wtrader.cli import prepare_live, live_gate_failures


def test_stalled_candle_fetch_cannot_delay_stop_exit_or_overlap_cycles(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow(*args):
        calls.append(1); started.set()
        assert release.wait(3)
        raise ConnectionError('dropped request')
    md.candles = slow
    as_of = int(eng.clock().timestamp()) // eng.s.market.signal_seconds * eng.s.market.signal_seconds
    eng.maybe_decide(datetime.fromtimestamp(as_of + 9, timezone.utc), 'UNSCHEDULED', eng.portfolio_state(eng.clock())[0])
    assert started.wait(1)
    md.set('BTCTMN', '950000')
    try:
        begin = time.monotonic()
        eng.tick(); eng.tick()
        assert time.monotonic() - begin < .5
        assert eng.store.closed_positions()[0]['exit_reason'] == 'STOP'
        assert len(calls) == 1  # no second candle job queued while the first is blocked
        assert eng._tick_lock.acquire(False)
        assert eng.tick() is False
        eng._tick_lock.release()
    finally:
        release.set(); eng.close()


def test_book_network_stall_does_not_block_protection_and_stale_books_never_fill(tmp_path):
    eng, source, venue = make(tmp_path)
    enter(eng, trail=None)
    started, release = threading.Event(), threading.Event()
    def blocked(symbol):
        started.set(); release.wait(3)
        raise ConnectionError('connection dropped')
    buffered = BufferedMarketData(source)
    stale = mk_book(bid='900000', ask='900500', at=eng.clock() - timedelta(seconds=40))
    buffered.cache['BTCTMN'] = stale
    source.book = blocked
    eng.md = buffered; venue.book_source = buffered
    try:
        t0 = time.monotonic()
        eng.tick()
        assert started.wait(1)
        eng.tick()
        assert time.monotonic() - t0 < .5
        assert not any(o['side'] == 'SELL' for o in eng.store.orders())
        assert eng.store.events('alert')[0]['payload_json'].find('protection degraded') >= 0
        status = json.loads(__import__('pathlib').Path(eng.s.status_path).read_text())
        assert status['valuation_current'] is False
        assert status['open_positions'][0]['valuation_stale'] is True
    finally:
        release.set(); eng.close()


def test_account_display_stall_does_not_delay_stop(tmp_path):
    eng, md, venue = make(tmp_path)
    enter(eng, trail=None)
    started, release = threading.Event(), threading.Event()
    class SlowAccount:
        def health(self): return {}
        def balances(self):
            started.set(); release.wait(3)
            raise ConnectionError('account read stalled')
    eng.account_client = SlowAccount()
    try:
        eng.tick(); assert started.wait(1)
        md.set('BTCTMN', '950000')
        t0 = time.monotonic(); eng.tick(); eng.tick()
        assert time.monotonic() - t0 < .5
        assert eng.store.closed_positions()[0]['exit_reason'] == 'STOP'
    finally:
        release.set(); eng.close()


def test_late_data_batch_is_discarded_without_selector_or_order(tmp_path):
    eng, md, venue = make(tmp_path)
    fut = Future(); fut.set_result(([], {}))
    now = eng.clock(); old = int(now.timestamp()) // 900 * 900 - 900
    eng.data_pending = (fut, old)
    eng.maybe_decide(now, 'UNSCHEDULED', eng.portfolio_state(now)[0])
    assert eng.store.events('decision_data_expired')
    assert not eng.store.orders()
    eng.close()


def test_paper_restart_restores_partial_order_and_accounts_fill_once(tmp_path):
    eng, md, venue = make(tmp_path)
    venue.level_fraction = D(".0001")
    venue.checkpoint = lambda state: eng.store.set_kv('paper_state', state)
    enter(eng, trail=None)
    original = eng.store.orders()[0]
    assert original['state'] == 'PARTIALLY_FILLED'
    before = eng.store.position(original['position_id'])['qty']
    state = eng.store.get_kv('paper_state')
    venue2 = PaperVenue(md); venue2.restore(state)
    assert venue2.lookup(original['client_id']).executed_qty == D(original['executed_qty'])
    assert venue2.balances() == venue.balances()
    eng.executor.venue = eng.venue = venue2
    eng.executor.reconcile_all(); eng.executor.reconcile_all()
    assert eng.store.position(original['position_id'])['qty'] == before
    assert venue2.balances()['TMN'].locked > 0
    eng.close()


def test_same_book_snapshot_cannot_be_consumed_repeatedly():
    md = FakeMD(); b = mk_book(qty='0.01')
    md.book = lambda symbol: b
    v = PaperVenue(md); v.fund('TMN', D('1000000'))
    i = OrderIntent('W1','W1','BTCTMN','BUY',D('1002000'),D('.1'),'ENTRY',None,None)
    obs = v.submit(i, mk_market())
    for _ in range(10): v.poll({'BTCTMN': b})
    assert v.lookup('W1').executed_qty == obs.executed_qty
    assert obs.state == OrderState.PARTIALLY_FILLED


@pytest.mark.parametrize('body', [[], None, 'bad', 1])
def test_kill_file_nonobject_fails_closed(tmp_path, body):
    p = tmp_path / 'KILL'; p.write_text(json.dumps(body))
    assert KillSwitch(p).blocks('SELL')


def test_resampling_rejects_duplicates_disguised_as_complete_bucket():
    rows = trend_candles(5, start=0, seconds=60)
    assert len(resample(rows,60,300)) == 1
    assert resample(rows[:4] + [rows[3]],60,300) == []
    with pytest.raises(ValueError): resample(rows,60,100)


def test_candle_parser_rejects_truncation_and_drops_forming_and_outside_bars():
    body = {'s':'ok','t':[0,60,120], 'o':['1']*3,'h':['2']*3,'l':['1']*3,'c':['2']*3,'v':['1']*3}
    assert [c.open_time for c in parse_wallex_candles(body,60,60,150)] == [60]
    body['v'] = []
    with pytest.raises(ValueError): parse_wallex_candles(body,60,0,180)


def test_stale_context_or_gapped_signals_cannot_generate_candidates():
    s = Settings(); sig = trend_candles(200, start=0, seconds=900)
    as_of = sig[-1].open_time + 900
    ctx = trend_candles(60, start=0, seconds=3600)
    # Enough observations exist but the last context candle is old.
    future_sig = trend_candles(200, start=300000, seconds=900)
    a = analyze('BTCTMN',future_sig,ctx,future_sig[-1].open_time+900,s.strategy)
    assert a.regime == 'INSUFFICIENT_DATA' and 'stale_context_bar' in a.features


def test_advisory_evaluation_can_never_prepare_live(monkeypatch):
    s = load_settings('config/shadow_eval.yaml')
    assert s.experimental_shadow and s.strategy.edge_gate == 'advisory'
    assert s.db_path != load_settings('config/scalp.yaml').db_path
    assert s.status_path != load_settings('config/scalp.yaml').status_path
    with pytest.raises(SystemExit): prepare_live(s)
    s.mode = 'live'
    with pytest.raises(ValueError): validate_settings(s)


def test_unknown_remote_state_halts_entries_instead_of_reusing_open_state(tmp_path):
    eng, md, venue = make(tmp_path)
    venue.level_fraction = D('.0001'); enter(eng,trail=None)
    row = eng.store.orders()[0]
    eng.executor.apply(row['client_id'], OrderObservation(row['client_id'],OrderState.UNKNOWN,
                       D(row['executed_qty']),D(row['executed_quote']),D(row['fee_base']),D(row['fee_quote'])))
    assert eng.executor.unresolved()
    eng.close()


@pytest.mark.parametrize('body', [{'success':False,'result':{}}, []])
def test_http_200_failure_envelopes_are_not_success(body):
    c = WallexClient(http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body))))
    with pytest.raises(TransientError): c.book('BTCTMN')


def test_unverified_fee_units_are_refused():
    c = WallexClient('key',http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'result':{'BTCTMN':{'takerFeeRate':'.25'}}}))))
    with pytest.raises(ValueError): c.taker_fees()


def test_zero_remaining_qty_keeps_realized_exit_loss_until_position_closes():
    r = Settings().risk
    p = {'symbol':'BTCTMN','qty':'0','entry_price':'100','cost_tmn':'200000','proceeds_tmn':'195000'}
    a = compute_allocation(risk=r,positions=[p],orders=[],closed=[],bids={},fees={},default_fee=D('.0035'),foreign_reserved=D(0),exchange_free_tmn=D('4995000'))
    assert a.bot_equity_tmn == D('995000')


def test_next_bar_gap_cannot_fill_above_the_submitted_limit(monkeypatch):
    from wtrader import backtest as bt
    from wtrader.ai_agent import BaselineSelector
    from wtrader.models import Candle
    from wtrader.strategy import Candidate, Analysis
    s = Settings();s.market.signal_seconds = 300;s.market.context_seconds = 3600
    s.strategy.market_filter_symbol = None;s.strategy.edge_gate = 'advisory'
    s.strategy.stop_distance_max_fraction = '.05'
    def signal(sym,sig,ctx,asof,*args):
        c=Candidate(f'{sym}-{asof}',sym,'SCALP_MOMENTUM',asof,D(100),D(98),D('.02'),D(2),6,{})
        return Analysis(sym,asof,'TREND_UP',{},(c,))
    monkeypatch.setattr(bt,'analyze',signal)
    def run(gap):
        rows=[Candle(i*300,D(100 if i==0 else gap),D(max(100,gap)),D(100),D(100),D(1000)) for i in range(3)]
        return bt.run_backtest(s,{'BTCTMN':(rows,[])},300,900,BaselineSelector(),bt.CostModel(),{},'test')
    normal=run(100)
    assert normal.trades, 'control scenario must pass the risk gate and actually fill'
    assert normal.equity[-1][1] == pytest.approx(1000000 + sum(t.pnl_tmn for t in normal.trades))
    assert run(200).trades == []


def test_sqlite_writer_is_confined_to_engine_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    eng,md,venue=make(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut=pool.submit(eng.store.event,'illegal_background_write',{})
        with pytest.raises(sqlite3.ProgrammingError): fut.result()
    assert not eng.store.events('illegal_background_write')
    eng.close()


def test_dashboard_refuses_to_mix_profile_database_and_status(tmp_path):
    from pathlib import Path
    from wtrader.monitor import dashboard_data
    eng,md,venue=make(tmp_path);eng.tick()
    p=Path(eng.s.status_path);status=json.loads(p.read_text());status['db_path']=str(tmp_path/'other.sqlite');p.write_text(json.dumps(status))
    assert 'mismatch' in dashboard_data(eng.s)['error']
    eng.close()


def test_open_order_pagination_cannot_hide_foreign_funds_when_server_caps_pages():
    pages=[]
    def handler(req):
        n=int(req.url.params['page']);pages.append(n)
        return httpx.Response(200,json={'result':{'orders':[{'clientOrderId':f'MANUAL{n}','status':'NEW','side':'BUY','symbol':'BTCTMN','origQty':'1','executedQty':'0','price':'100000'}]},'result_info':{'total_count':2}})
    c=WallexClient('k',http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(c.open_orders())==2 and pages==[1,2]


def test_missing_wallet_connection_field_is_not_treated_as_disabled():
    c=WallexClient('k',http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'result':{'packageDetail':{},'userPackageDetail':{'status':'active'}}}))))
    with pytest.raises(ValueError):c.active_package()


def test_full_restart_new_engine_and_new_venue_reconciles_partial_fill_once(tmp_path):
    from test_engine import settings
    from wtrader.engine import Engine
    from wtrader.store import Store
    from wtrader.ai_agent import BaselineSelector
    eng,md,venue=make(tmp_path);venue.level_fraction=D('.0001')
    venue.checkpoint=lambda state:eng.store.set_kv('paper_state',state)
    enter(eng,trail=None)
    row=eng.store.orders()[0];expected_qty=eng.store.position(row['position_id'])['qty']
    snapshot=eng.store.get_kv('paper_state');eng.close();eng.store.db.close()
    md2=FakeMD();venue2=PaperVenue(md2);venue2.restore(snapshot)
    store2=Store(settings(tmp_path).db_path)
    fresh=Engine(settings(tmp_path),store2,md2,venue2,BaselineSelector(),{})
    try:
        fresh.bootstrap();fresh.executor.reconcile_all()
        assert fresh.store.position(row['position_id'])['qty']==expected_qty
        assert fresh.store.orders()[0]['state']=='PARTIALLY_FILLED'
        assert len(fresh.store.orders())==1
    finally:fresh.close();store2.db.close()


def test_actual_websocket_close_frame_marks_fatal_without_retry():
    import struct
    from wtrader.wallex_ws import DepthStream
    class Socket:
        def settimeout(self,t):pass
        def send(self,frame):pass
        def recv_data(self,control_frame):return 8,struct.pack('!H',3501)
        def close(self,*args,**kwargs):pass
    calls=[]
    stream=DepthStream(connector=lambda *args,**kw:calls.append(1) or Socket())
    stream.watch(['BTCTMN']);stream._thread.join(1)
    assert stream.fatal and len(calls)==1 and not stream.connected


def test_tls_verification_enabled_and_private_origin_is_restricted():
    import ssl
    c=WallexClient('FAKE_TEST_KEY',base_url='http://untrusted.example')
    try:
        assert c.http._transport._pool._ssl_context.check_hostname
        assert c.http._transport._pool._ssl_context.verify_mode==ssl.CERT_REQUIRED
        from wtrader.models import AuthError
        with pytest.raises(AuthError):c._headers(True)
    finally:c.close()


def test_probe_captures_websocket_evidence_before_invalidating_it(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from wtrader import cli
    from wtrader.models import Candle
    class Client:
        limits_source='test'
        auth_state='not_configured'
        trading_enabled=False
        def markets(self):return {'BTCTMN':mk_market()}
        def candles(self,symbol,seconds,*args):return [Candle(int(time.time())//seconds*seconds-seconds,D(1),D(2),D(1),D(2),D(1))]
        def book(self,symbol):return mk_book(symbol,bid='1',ask='1.0005') if symbol=='BTCUSDT' else mk_book(symbol)
        def health(self):return {'auth':'not_configured'}
        def close(self):pass
    class Stream:
        connected=True
        def watch(self,symbols):pass
        def book(self,*args):return mk_book() if self.connected else None
        def status(self):return {'connected':self.connected}
        def stop(self):self.connected=False
    stream=Stream()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WALLEX_API_KEY','')
    monkeypatch.setenv('WALLEX_HTTP_PROXY','http://test-proxy') # skip the separate real network diagnosis
    monkeypatch.setattr(cli,'make_client',lambda *a:Client())
    monkeypatch.setattr(cli,'make_stream',lambda *a:stream)
    cli.cmd_probe(SimpleNamespace(config=None,ws_seconds=.01))
    report=json.loads(next((tmp_path/'evidence/probe').glob('*/probe_report.json')).read_text())
    assert report['checks']['websocket_depth_BTCTMN']['ok']
    assert report['checks']['websocket_depth_BTCTMN']['result']['two_sided_book']
    assert not stream.connected


def test_executor_cannot_sell_external_balance_without_bot_position(tmp_path):
    eng,md,venue=make(tmp_path);venue.fund('ETH',D(5))
    cid=eng.executor.place(economic_key='UNATTRIBUTED',symbol='ETHTMN',side='SELL',quantity=D(1),limit_price=D(99000),
                           purpose='LIQUIDATION',market=eng.markets['ETHTMN'])
    assert cid is None and not eng.store.orders() and venue.balances_['ETH']==D(5)
    eng.close()


def test_changed_build_preserves_ledger_and_keeps_both_run_manifests(tmp_path):
    from wtrader.store import Store
    store=Store(tmp_path/'ledger.sqlite','RFIRST')
    store.record_run('shadow',{'source_sha256':'old','config_hash':'same'})
    store.set_kv('daily_halt','2026-10-22')
    store.record_run('shadow',{'source_sha256':'new','config_hash':'same'})
    assert store.run_id!='RFIRST'
    assert store.get_kv('daily_halt')=='2026-10-22'
    rows=list(store.db.execute('SELECT manifest_json FROM runs'))
    assert {json.loads(r[0])['source_sha256'] for r in rows}=={'old','new'}
    store.db.close()


def test_paper_balance_refresh_does_not_claim_fresh_exchange_http_success(tmp_path):
    from pathlib import Path
    eng,md,venue=make(tmp_path)
    old=(eng.clock()-timedelta(hours=1)).isoformat()
    class Client:
        def health(self):return {'last_ok_at':old,'trading_enabled':False}
    md.client=Client()
    eng.tick()
    status=json.loads(Path(eng.s.status_path).read_text())
    assert status['connectivity']['last_api_ok_at']==old
    assert status['connectivity']['last_account_ok_at']!=old
    eng.close()
