"""Provider contract/security tests. SDK requests are mocked; no paid or exchange calls."""
import builtins
import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from openai import OpenAI

from test_ai import ctx_with, good
from wtrader.ai_agent import BaselineSelector, Decision, LLMSelector, make_selector
from wtrader.config import AiCfg, Settings, load_settings, redact, validate_settings
from wtrader.llm import BoxAPIProvider, AnthropicProvider, ProviderError, make_provider, credentials_available

KEY = 'box_test_secret_not_a_real_key_123456'


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for k in ('BOXAPI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'LLM_PROVIDER', 'LLM_MODEL_ID',
              'BOXAPI_BASE_URL', 'AI_MODE', 'LLM_INPUT_USD_PER_MILLION', 'LLM_OUTPUT_USD_PER_MILLION',
              'LLM_CACHED_INPUT_USD_PER_MILLION'):
        monkeypatch.delenv(k, raising=False)


def cfg(**kw):
    return AiCfg(provider='boxapi', model='gpt-5.4-mini', **kw)


def catalog(model='openai/gpt-5.4-mini'):
    return {'data': [{'id': model, 'object': 'model', 'created': 0, 'owned_by': 'boxapi',
                      'supported_parameters': ['response_format', 'reasoning_effort']}]}


def completion(text, finish='stop', refusal=None, model='gpt-5.4-mini', usage=True):
    return {'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': model,
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {
                'role': 'assistant', 'content': text, 'refusal': refusal}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                      'prompt_tokens_details': {'cached_tokens': 40}} if usage else None}


def selector(handler=None, *, context=None, config=None, cache_dir=None, **kw):
    context = context or ctx_with()
    config = config or cfg()
    calls = []
    def route(req):
        calls.append(req)
        if req.method == 'GET': return httpx.Response(200, json=catalog())
        return handler(req) if handler else httpx.Response(200, json=completion(json.dumps(good(context))))
    sdk = OpenAI(api_key=KEY, base_url=config.base_url, max_retries=0,
                 http_client=httpx.Client(transport=httpx.MockTransport(route), trust_env=False, follow_redirects=False))
    return LLMSelector(config, client=sdk, cache_dir=cache_dir, **kw), calls


def test_env_provider_selection_overrides_legacy_model_but_retains_risk(tmp_path, monkeypatch):
    p = tmp_path/'c.yaml'; p.write_text('ai:\n  model: claude-opus-5-5\n')
    for k,v in {'LLM_PROVIDER':'boxapi', 'LLM_MODEL_ID':'gpt-5.4-mini',
                'BOXAPI_BASE_URL':'https://ai.boxapi.ir/ai/v1', 'AI_MODE':'ai'}.items(): monkeypatch.setenv(k,v)
    s = load_settings(str(p))
    assert s.ai.provider == 'boxapi' and s.ai.model == 'gpt-5.4-mini'
    assert isinstance(make_provider(s.ai), BoxAPIProvider)
    assert s.risk.authorized_capital_tmn == '1000000' and not s.execution.live_rollout_approved
    monkeypatch.delenv('LLM_MODEL_ID')
    assert load_settings(str(p)).ai.model == 'gpt-5.4-mini'
    monkeypatch.setenv('LLM_PROVIDER','anthropic')
    assert isinstance(make_provider(load_settings(str(p)).ai), AnthropicProvider)


@pytest.mark.parametrize('mode,provider', [('baseline','boxapi'), ('ai','baseline')])
def test_explicit_baseline_needs_no_credentials_or_sdk(monkeypatch, mode, provider):
    original = builtins.__import__
    def guarded(name, *args, **kw):
        if name in ('openai','anthropic'): raise AssertionError('baseline imported an LLM SDK')
        return original(name,*args,**kw)
    monkeypatch.setattr(builtins,'__import__',guarded)
    c = AiCfg(mode=mode,provider=provider)
    sel = make_selector(c)
    assert isinstance(sel,BaselineSelector) and credentials_available(c)
    assert sel.select(ctx_with()).source == 'baseline'


def test_boxapi_sdk_format_and_catalog_alias_are_exact():
    sel,calls = selector()
    d = sel.select(ctx_with())
    assert d.action=='SELECT' and d.provider=='boxapi'
    assert [(r.method,r.url.path) for r in calls] == [('GET','/ai/v1/models'),('POST','/ai/v1/chat/completions')]
    r = calls[-1]; body=json.loads(r.content)
    assert r.headers['authorization']=='Bearer '+KEY
    assert body['model']=='gpt-5.4-mini'  # catalog prefix never silently changes the requested alias
    assert [m['role'] for m in body['messages']]==['system','user']
    assert body['response_format']['json_schema']['strict'] is True
    assert body['response_format']['json_schema']['schema']['additionalProperties'] is False
    assert body['reasoning_effort']=='low' and body['max_completion_tokens']==1200
    assert not set(body) & {'tools','fallbacks','betas','temperature','output_config'}
    assert all(0 < t <= sel.cfg.deadline_seconds for t in r.extensions['timeout'].values())
    assert sel.stats()['model_verification']['catalog_id']=='openai/gpt-5.4-mini'


def test_unsupported_model_never_sends_generation():
    sel,calls=selector(config=AiCfg(provider='boxapi',model='invented-model'))
    d=sel.select(ctx_with())
    assert d.action=='HOLD' and d.error=='model_not_in_catalog'
    assert len(calls)==1 and calls[0].method=='GET'


@pytest.mark.parametrize('text', ['not json', '[]', '{}', '{"action":"SELECT"}',
    json.dumps(good(ctx_with(),candidate_id='made up')),
    json.dumps(good(ctx_with(),candidate_id=['bad'])),
    json.dumps(good(ctx_with(),exposure_multiplier='2.0')),
    json.dumps(good(ctx_with(),evidence_ids=['made up'])),
    json.dumps(good(ctx_with()))[:-1]+',"action":"HOLD"}',
])
def test_invalid_responses_fail_closed_even_under_legacy_baseline_outage(text):
    sel,_=selector(lambda r:httpx.Response(200,json=completion(text)),config=cfg(outage_action='BASELINE'))
    d=sel.select(ctx_with())
    assert d.action=='HOLD' and d.candidate_id is None and d.multiplier=='0' and d.error
    assert d.usage['input_tokens']==100  # invalid generated output can still be billed


@pytest.mark.parametrize('finish,refusal,model', [('length',None,'gpt-5.4-mini'),
    ('stop','refused','gpt-5.4-mini'), ('tool_calls',None,'gpt-5.4-mini'), ('stop',None,'another-model')])
def test_refusal_truncation_tools_or_model_substitution_are_not_decisions(finish,refusal,model):
    sel,_=selector(lambda r:httpx.Response(200,json=completion(json.dumps(good(ctx_with())),finish,refusal,model)))
    d=sel.select(ctx_with())
    assert d.action=='HOLD' and d.usage['output_tokens']==20


@pytest.mark.parametrize('status',[401,403])
def test_auth_failures_have_no_retry_or_secret_in_errors_logs_cache(status,monkeypatch,caplog,tmp_path):
    monkeypatch.setenv('BOXAPI_API_KEY',KEY)
    def fail(req):return httpx.Response(status,json={'error':{'message':KEY,'type':'invalid_key'}})
    sel,calls=selector(fail,cache_dir=tmp_path)
    with caplog.at_level(logging.WARNING):d=sel.select(ctx_with())
    assert d.action=='HOLD' and 'authentication_failed' in d.error
    assert len([r for r in calls if r.method=='POST'])==1
    assert KEY not in json.dumps(d.as_dict())+caplog.text+json.dumps(sel.stats())
    assert not list(tmp_path.iterdir())
    assert redact(KEY)=='***'


@pytest.mark.parametrize('provider', ['boxapi','anthropic'])
def test_missing_credentials_hold_without_sending(provider):
    c=AiCfg(provider=provider,model='gpt-5.4-mini' if provider=='boxapi' else 'claude-opus-5-5')
    assert not credentials_available(c)
    d=LLMSelector(c).select(ctx_with())
    assert d.action=='HOLD' and d.error=='missing_credentials'


def test_timeout_has_no_immediate_duplicate_paid_request():
    def fail(req):raise httpx.ReadTimeout(KEY,request=req)
    sel,calls=selector(fail)
    d=sel.select(ctx_with())
    assert d.action=='HOLD' and d.error=='timeout'
    assert len([r for r in calls if r.method=='POST'])==1
    assert sel.stats()['estimated_cost_usd'] is None


def test_rate_limit_retry_is_bounded_and_budget_counts_attempts():
    count=[]
    def limited(req):
        count.append(1)
        if len(count)==1:return httpx.Response(429,headers={'Retry-After':'0'},json={'error':{'message':KEY}})
        return httpx.Response(200,json=completion(json.dumps(good(ctx_with()))))
    sel,_=selector(limited)
    assert sel.select(ctx_with()).action=='SELECT' and len(count)==2
    assert sel.stats()['request_attempts']==2
    count.clear();sel,_=selector(limited,config=cfg(max_calls_per_day=1))
    assert sel.select(ctx_with()).action=='HOLD' and len(count)==1


def test_long_retry_after_does_not_sleep_and_blocks_next_candidate():
    def fail(req):return httpx.Response(429,headers={'Retry-After':'120'},json={'error':{}})
    slept=[];sel,calls=selector(fail,sleep=slept.append)
    assert sel.select(ctx_with()).action=='HOLD'
    n=len(calls)
    assert sel.select(ctx_with()).error=='rate_limit_cooldown' and len(calls)==n
    assert slept==[] and 100<sel.stats()['retry_in_s']<=120


def test_server_retry_exhaustion_is_bounded():
    sel,calls=selector(lambda req:httpx.Response(503,json={'error':{}}),sleep=lambda _:None)
    assert sel.select(ctx_with()).action=='HOLD'
    assert len([r for r in calls if r.method=='POST'])==2


def test_usage_cost_and_cache_are_not_double_counted(tmp_path):
    c=cfg(input_usd_per_million='1',output_usd_per_million='2',cached_input_usd_per_million='.1')
    sel,calls=selector(config=c,cache_dir=tmp_path)
    d=sel.select(ctx_with())
    assert Decimal(d.usage['estimated_cost_usd'])==Decimal('.000104')
    assert sel.stats()['input_tokens']==100
    n=len(calls);replay=sel.select(ctx_with())
    assert len(calls)==n and replay.usage['incremental_estimated_cost_usd']=='0'
    assert sel.stats()['responses']==1 and sel.stats()['cache_hits']==1
    assert Decimal(sel.stats()['estimated_cost_usd'])==Decimal('.000104')
    other=LLMSelector(AiCfg(),cache_dir=tmp_path)
    assert other._cache_path(ctx_with())!=sel._cache_path(ctx_with())
    sel._cache_path(ctx_with()).write_text('broken cache')
    assert sel.select(ctx_with()).error=='invalid replay cache' and len(calls)==n


def test_unknown_usage_remains_unknown_instead_of_zero_cost():
    sel,_=selector(lambda req:httpx.Response(200,json=completion(json.dumps(good(ctx_with())),usage=False)))
    d=sel.select(ctx_with())
    assert d.action=='SELECT' and d.usage['estimated_cost_usd'] is None
    assert sel.stats()['usage_unknown_calls']==1 and sel.stats()['estimated_cost_usd'] is None


@pytest.mark.parametrize('choices', [None, []])
def test_invalid_choice_envelope_retains_reported_usage(choices):
    def malformed(req):
        body=completion('{}');body['choices']=choices
        return httpx.Response(200,json=body)
    sel,_=selector(malformed)
    d=sel.select(ctx_with())
    assert d.action=='HOLD' and d.usage['output_tokens']==20


def test_successful_content_cannot_leak_key_to_decisions_or_cache(monkeypatch,tmp_path):
    monkeypatch.setenv('BOXAPI_API_KEY',KEY)
    text=json.dumps(good(ctx_with(),reason_summary='provider echo '+KEY))
    sel,_=selector(lambda req:httpx.Response(200,json=completion(text)),cache_dir=tmp_path)
    d=sel.select(ctx_with())
    assert d.action=='SELECT' and KEY not in json.dumps(d.as_dict())
    assert all(KEY not in p.read_text() for p in tmp_path.iterdir())


def test_dashboard_api_exposes_provider_usage_without_keys(tmp_path,monkeypatch):
    from test_engine import make
    from wtrader.monitor import dashboard_data
    monkeypatch.setenv('BOXAPI_API_KEY',KEY)
    sel,_=selector(lambda req:httpx.Response(200,json=completion(json.dumps(good(ctx_with(),reason_summary=KEY)))))
    decision=sel.select(ctx_with())
    eng,_,_=make(tmp_path);eng.selector=sel;eng.s.ai=cfg()
    try:
        eng.store.record_decision(snapshot_id=ctx_with()['snapshot_id'],snapshot_ref='mock',n_candidates=1,
            action=decision.action,source='llm',decision=decision.as_dict(),final_action='HOLD')
        eng.tick()
        data=dashboard_data(eng.s)
        assert data['status']['llm']['provider']=='boxapi'
        assert data['status']['llm']['input_tokens']==100
        assert KEY not in json.dumps(data)
        assert not eng.store.orders()
    finally:eng.close()


@pytest.mark.parametrize('url',['http://ai.boxapi.ir/ai/v1','https://evil.example/ai/v1',
                               'https://ai.boxapi.ir/ai/v1?key='+KEY,'https://user:pass@ai.boxapi.ir/ai/v1'])
def test_credentials_are_not_sent_to_untrusted_origins(url,monkeypatch):
    monkeypatch.setenv('BOXAPI_API_KEY',KEY)
    s=Settings();s.ai=cfg();s.ai.base_url=url
    with pytest.raises(ValueError):validate_settings(s)
    d=LLMSelector(s.ai).select(ctx_with())
    assert d.error=='untrusted_api_origin' and KEY not in json.dumps(d.as_dict())


def test_no_candidates_avoids_catalog_and_generation():
    sel,calls=selector()
    assert sel.select(ctx_with(0)).source=='no_candidates' and calls==[]


def test_slow_boxapi_sdk_request_cannot_delay_position_protection(tmp_path):
    from test_engine import make,enter
    eng,md,_=make(tmp_path);enter(eng,trail=None)
    started,release=threading.Event(),threading.Event()
    def slow(req):
        started.set();release.wait(3)
        raise httpx.ReadTimeout('delayed provider',request=req)
    sel,_=selector(slow);eng.selector=sel
    future=eng.pool.submit(sel.select,ctx_with())
    try:
        assert started.wait(1);md.set('BTCTMN','950000')
        begin=time.monotonic();eng.tick();eng.tick()
        assert time.monotonic()-begin<.5
        assert eng.store.closed_positions()[0]['exit_reason']=='STOP'
    finally:release.set();future.result(timeout=2);eng.close()


def test_late_usage_is_saved_but_expired_selection_never_trades(tmp_path):
    from test_engine import make
    eng,_,_=make(tmp_path);ctx=ctx_with();f=Future()
    did=eng.store.record_decision(snapshot_id=ctx['snapshot_id'],snapshot_ref='test',n_candidates=1,
        action='PENDING',source='llm',decision={},final_action='PENDING')
    eng.pending=(f,ctx,[],did);eng.selector_started=time.monotonic()-eng.s.ai.deadline_seconds-1
    try:
        eng.maybe_decide(eng.clock(),'UNSCHEDULED',eng.portfolio_state(eng.clock())[0])
        f.set_result(Decision('SELECT',ctx['candidates'][0]['candidate_id'],'1.0','llm',
            provider='boxapi',usage={'input_tokens':100,'output_tokens':20}))
        eng.maybe_decide(eng.clock(),'UNSCHEDULED',eng.portfolio_state(eng.clock())[0])
        row=eng.store.decisions(1)[0]
        assert row['final_action']=='EXPIRED' and json.loads(row['decision_json'])['usage']['input_tokens']==100
        assert not eng.store.orders()
    finally:eng.close()


def test_probe_is_synthetic_and_never_builds_an_exchange_client(tmp_path,monkeypatch):
    from wtrader import cli
    monkeypatch.chdir(tmp_path);monkeypatch.setenv('LLM_PROVIDER','boxapi')
    def forbidden(*a,**k):raise AssertionError('probe contacted Wallex')
    monkeypatch.setattr(cli,'make_client',forbidden)
    # Missing credentials are a reported failed probe, never a secret-bearing traceback.
    (tmp_path/'c.yaml').write_text('evidence_dir: evidence\n')
    with pytest.raises(SystemExit) as error:cli.main(['--config','c.yaml','llm-probe'])
    assert error.value.code==1
    r=json.loads(next((tmp_path/'evidence/llm_probe').glob('*/report.json')).read_text())
    assert r['error']=='missing_credentials' and r['wallex_requests']==0


@pytest.mark.parametrize('provider,module', [('boxapi','openai'),('anthropic','anthropic')])
def test_missing_optional_sdk_is_a_safe_hold(monkeypatch,provider,module):
    monkeypatch.setenv('BOXAPI_API_KEY',KEY);monkeypatch.setenv('ANTHROPIC_API_KEY',KEY)
    original=builtins.__import__
    def missing(name,*a,**kw):
        if name==module:raise ImportError('uninstalled')
        return original(name,*a,**kw)
    monkeypatch.setattr(builtins,'__import__',missing)
    d=LLMSelector(AiCfg(provider=provider)).select(ctx_with())
    assert d.action=='HOLD' and d.error==module+'_sdk_not_installed'


def test_anthropic_sdk_contract_without_real_authentication():
    anthropic=pytest.importorskip('anthropic')
    httpx2=pytest.importorskip('httpx2')
    requests=[];context=ctx_with()
    def route(req):
        requests.append(req)
        return httpx2.Response(200,json={'id':'msg_test','type':'message','role':'assistant',
            'model':'claude-opus-5-5','content':[{'type':'text','text':json.dumps(good(context))}],
            'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':20}})
    sdk=anthropic.Anthropic(api_key=KEY,max_retries=0,http_client=httpx2.Client(transport=httpx2.MockTransport(route)))
    d=LLMSelector(AiCfg(),client=sdk).select(context)
    assert d.action=='SELECT' and d.provider=='anthropic' and d.usage['input_tokens']==10
    body=json.loads(requests[0].content)
    assert requests[0].headers['authorization']=='Bearer '+KEY and 'tools' not in body and 'fallbacks' not in body


@pytest.mark.parametrize('field,value',[('provider','unknown'),('max_attempts',99),('deadline_seconds',600),
                                     ('max_output_tokens',99999),('input_usd_per_million','NaN')])
def test_invalid_provider_or_unbounded_settings_are_rejected(field,value):
    s=Settings();setattr(s.ai,field,value)
    with pytest.raises(ValueError):validate_settings(s)
