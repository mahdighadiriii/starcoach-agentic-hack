"""Develop comparison regressions: scoped account visibility and deny-only limits.

Every request here is mocked. Databases and kill switches use pytest's temp path.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from wtrader.account import read_account_snapshot
from wtrader.config import CompetitionCfg, Secrets
from wtrader.markets import DISABLED_MARKETS
from wtrader.models import Balance, TransientError
from wtrader.preflight import run_preflight
from wtrader.risk import competition_phase
from wtrader.wallex import WallexClient

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def client(handler, sub=None):
    return WallexClient('TESTKEY', sub, http=httpx.Client(transport=httpx.MockTransport(handler)), read_attempts=1)


def responses(req):
    assert req.method == 'GET'
    path = req.url.path
    if path.endswith('balances'):
        return httpx.Response(200, json={'result': {'balances': {'TMN': {'value': '5000000', 'locked': '0'}}}})
    if path.endswith('balances-detail'):
        return httpx.Response(200, json={'result': {'TMN': {'total': '5000000', 'available': '4999990', 'freeze': '10'}}})
    if path == '/v1/account/fee':
        return httpx.Response(200, json={'result': {'BTCTMN': {'makerFeeRate': '.0025', 'takerFeeRate': '.0030'},
            'default': [{'makerFeeRate': '.0035', 'takerFeeRate': '.0035'}],
            'metaData': {'levels': [], 'coin_levels': [], 'latestTradesSum': '0', 'latestTetherTradesSum': '0'}}})
    if path == '/sub-accounts':
        return httpx.Response(200, json={'result': [{'clientId': 'PRIVATE_SCOPE', 'userId': 123,
            'title': 'PRIVATE_TITLE', 'type': 'prop', 'isActive': True, 'isSuspend': False}],
            'result_info': {'total_count': 1}})
    return httpx.Response(401, json={'success': False})


def test_optional_credit_denial_keeps_valid_balances_and_reports_partial_access():
    c = client(responses)
    snap = read_account_snapshot(c, ['BTCTMN'], clock=lambda: NOW)
    assert snap['balances']['TMN'] == {'free': '5000000', 'locked': '0', 'total': '5000000'}
    assert snap['scope'] == 'key_default_unverified'
    assert snap['fee_rates']['BTCTMN'] == {'maker': '0.0025', 'taker': '0.0030'}
    assert snap['credit']['funding_source'] == 'unverified'
    assert snap['credit']['allocation_isolation'] == 'unverified'
    assert snap['credit']['wallet_connection'] == 'unknown'
    assert snap['credit']['visible_accounts'][0]['type'] == 'prop'
    assert snap['checks']['credit_overview']['ok'] is False
    assert snap['checks']['active_package']['kind'] == 'ScopeNotSelected'
    assert c.health()['auth'] == 'partial'
    c.balances()
    assert c.health()['auth'] == 'partial'  # success on another endpoint cannot hide the denied scope
    assert 'PRIVATE_' not in json.dumps(snap) and '123' not in json.dumps(snap)
    assert not c.trading_enabled


def test_failed_balance_keeps_original_timestamp_and_labels_stale_values():
    failed = []
    calls = []
    def h(req):
        calls.append(req.url.path)
        if failed and req.url.path.endswith('balances'):
            raise httpx.ReadTimeout('temporarily unavailable', request=req)
        return responses(req)
    c = client(h)
    first = read_account_snapshot(c, ['BTCTMN'], clock=lambda: NOW)
    failed.append(True)
    second = read_account_snapshot(c, ['BTCTMN'], previous=first, clock=lambda: NOW + timedelta(seconds=30))
    assert second['synced_at'] == first['synced_at']
    assert second['balances'] == first['balances'] and second['error']
    assert second['checks']['balances']['ok'] is False
    assert calls.count('/v1/account/fee') == 1  # metadata does not multiply private requests each tick


def test_selected_sub_account_does_not_imply_package_or_allocation_isolation():
    snap = read_account_snapshot(client(responses, 'PRIVATE_SCOPE'), ['BTCTMN'], clock=lambda: NOW)
    assert snap['scope'] == 'explicit_sub_account'
    assert snap['credit']['selected_account_listed'] is True
    assert snap['credit']['selected_account_type'] == 'prop'
    assert snap['credit']['wallet_connection'] == 'unknown'
    assert snap['credit']['allocation_isolation'] == 'unverified'
    assert snap['checks']['active_package']['ok'] is False
    assert snap['balances']['TMN']['locked'] == '10'
    assert 'PRIVATE_SCOPE' not in json.dumps(snap)


def test_credit_overview_filters_identities_and_marks_selected_only_explicitly():
    def h(req):
        return httpx.Response(200, json={'result': {'aggregatedDetail': {'currency': 'TMN', 'loan': '4000000'},
            'userPackages': [{'packageDetail': {'loan': '4000000', 'title': 'PRIVATE_TITLE'},
                             'userPackageDetail': {'id': 123, 'subAccountClientId': 'PRIVATE_SCOPE', 'status': 'active'}}]}})
    c = client(h, 'PRIVATE_SCOPE')
    result = c.credit_overview()
    assert result['packages'][0]['matches_configured'] is True
    assert result['aggregate']['loan'] == '4000000'
    assert 'PRIVATE_' not in json.dumps(result)


def test_sub_account_discovery_follows_total_count_without_assuming_page_size():
    def h(req):
        page = int(req.url.params['page'])
        return httpx.Response(200, json={'result': [{'clientId': str(page), 'isActive': True, 'isSuspend': False}],
                                        'result_info': {'total_count': 2}})
    assert [r['clientId'] for r in client(h).sub_accounts()] == ['1', '2']


@pytest.mark.parametrize('body', [
    {}, {'result': {}}, {'result': [{'clientId': 'X', 'isActive': 'true', 'isSuspend': False}]},
    {'result': [], 'result_info': {'total_count': 1}},
    {'result': [{'clientId': 'X', 'isActive': True, 'isSuspend': False}], 'result_info': {'total_count': 2}},
])
def test_incomplete_or_repeating_account_discovery_is_not_empty_success(body):
    with pytest.raises(TransientError):
        client(lambda req: httpx.Response(200, json=body)).sub_accounts()


@pytest.mark.parametrize('rate', ['NaN', 'Infinity', '-.001', '.35'])
def test_both_fee_coefficients_must_be_finite_valid_fractions(rate):
    c = client(lambda req: httpx.Response(200, json={'result': {'BTCTMN': {'makerFeeRate': rate, 'takerFeeRate': '.003'}}}))
    with pytest.raises(ValueError): c.fee_rates()


@pytest.mark.parametrize('body', [{}, {'result': {'TMN': {'available': '1', 'freeze': '0'}}},
    {'result': {'TMN': {'available': '1', 'freeze': '1', 'total': '1'}}},
    {'result': {'TMN': {'available': '-1', 'freeze': '0', 'total': '-1'}}}])
def test_incomplete_or_inconsistent_balances_fail_closed(body):
    with pytest.raises(TransientError): client(lambda req: httpx.Response(200, json=body), 'S').balances()


@pytest.mark.parametrize('link', [False, 0, ''])
def test_undocumented_false_wallet_states_do_not_mean_disconnected(link):
    body = {'result': {'packageDetail': {}, 'userPackageDetail': {'connectToWallet': link}}}
    with pytest.raises(ValueError): client(lambda req: httpx.Response(200, json=body), 'S').active_package()


def test_deny_only_reference_blocks_risk_and_direct_executor_bypass(tmp_path):
    from test_risk import run, failed
    from test_engine import make
    from test_live_safety import place_buy, blocked
    from conftest import mk_market
    assert len(DISABLED_MARKETS) == 345
    assert 'ZECTMN' in DISABLED_MARKETS
    assert not set(['BTCTMN', 'ETHTMN', 'SOLTMN', 'XRPTMN', 'DOGETMN', 'BNBTMN', 'ADATMN', 'TRXTMN']) & DISABLED_MARKETS
    verdict = run(symbol='ZECTMN', market=mk_market('ZECTMN'), allowlist=['ZECTMN'])
    assert not verdict.approved and 'market_not_reference_disabled' in failed(verdict)
    eng, _, _ = make(tmp_path)
    try:
        eng.markets['ZECTMN'] = mk_market('ZECTMN')
        assert place_buy(eng, 'DENIED', symbol='ZECTMN') is None
        assert 'deny-only' in blocked(eng)[-1]
        assert not eng.store.orders()
    finally: eng.close()


def test_preflight_refuses_disabled_allowlist_and_missing_wallet_flag(tmp_path):
    from test_short_duration import FakeClient, pf_settings
    s = pf_settings(tmp_path)
    s.market.account_allowlist.append('ZECTMN')
    c = FakeClient()
    c.active_package = lambda: {'package': {}, 'user_package': {'status': 'active'}}
    report = run_preflight(s, c, Secrets('k', 'sub', 'https://x', None))
    assert {'no_reference_disabled_markets', 'trade_credit_wallet_isolation'} <= set(report['failures'])


def test_live_preflight_refuses_partial_market_fee_coverage(tmp_path):
    from test_short_duration import FakeClient, pf_settings
    s = pf_settings(tmp_path)
    s.mode = 'live'
    c = FakeClient()
    c.taker_fees = lambda: {'BTCTMN': D('.003')}
    report = run_preflight(s, c, Secrets('k', 'sub', 'https://x', None))
    assert 'fees' in report['failures']
    assert next(r for r in report['checks'] if r['check'] == 'fees')['detail']['missing_markets'] == ['ETHTMN']


@pytest.mark.parametrize('active', [None, 'true'])
def test_unknown_sub_account_status_cannot_pass_preflight(tmp_path, active):
    from test_short_duration import FakeClient, pf_settings
    report = run_preflight(pf_settings(tmp_path), FakeClient(sub_active=active), Secrets('k', 'sub', 'https://x', None))
    assert 'trade_credit_sub_account_active' in report['failures']


def test_slow_credit_metadata_cannot_block_protection_or_queue_more_account_jobs(tmp_path):
    from test_engine import make, enter
    eng, md, _ = make(tmp_path)
    enter(eng, trail=None)
    started, release = threading.Event(), threading.Event()
    calls = []
    class Account:
        def balances(self): return {'TMN': Balance('TMN', D('5000000'), D(0))}
        def fee_rates(self):
            calls.append(1); started.set(); release.wait(3)
            raise ConnectionError('metadata stall')
    eng.account_client = Account()
    try:
        eng.tick(); assert started.wait(1)
        md.set('BTCTMN', '950000')
        begin = time.monotonic(); eng.tick(); eng.tick()
        assert time.monotonic() - begin < .5
        assert eng.store.closed_positions()[0]['exit_reason'] == 'STOP'
        assert len(calls) == 1
    finally: release.set(); eng.close()


def test_exact_flatten_boundary_retains_feature_entry_and_ownership_rules():
    comp = CompetitionCfg(trade_end=NOW.isoformat(), halt_entries_before_end_minutes=180,
                          begin_liquidation_before_end_minutes=60)
    boundary = NOW - timedelta(minutes=60)
    assert competition_phase(boundary - timedelta(microseconds=1), comp) == 'NO_NEW_ENTRIES'
    assert competition_phase(boundary, comp) == 'LIQUIDATE'
    assert competition_phase(NOW, comp) == 'ENDED'
