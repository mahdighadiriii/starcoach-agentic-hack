"""Small provider boundary. No exchange, order, database or trading permissions."""
from __future__ import annotations

import math
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .config import AiCfg, redact


def safe_text(value, client=None):
    text = redact(value)
    for attr in ('api_key', 'auth_token'):
        secret = getattr(client, attr, None)
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '***')
    return text


def effective_provider(cfg):
    return 'baseline' if cfg.mode == 'baseline' or cfg.provider == 'baseline' else cfg.provider


def credentials_available(cfg):
    provider = effective_provider(cfg)
    return (True if provider == 'baseline' else bool(os.environ.get('BOXAPI_API_KEY')) if provider == 'boxapi'
            else bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')))


class ProviderError(RuntimeError):
    def __init__(self, kind, *, status=None, retryable=False, retry_after=None, usage=None):
        self.kind, self.status, self.retryable, self.retry_after, self.usage = kind, status, retryable, retry_after, usage
        super().__init__(kind + (f' (HTTP {status})' if status else ''))


def provider_error(exc):
    """Never retain SDK messages, URLs, headers, response bodies or exception reprs."""
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, 'status_code', None)
    if status in (401, 403):
        return ProviderError('authentication_failed', status=status)
    if status == 429:
        value = getattr(getattr(exc, 'response', None), 'headers', {}).get('retry-after')
        delay = None
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None and (not math.isfinite(delay) or delay < 0):
            delay = None
        return ProviderError('rate_limited', status=status, retryable=True,
                             retry_after=min(3600, delay) if delay is not None else 1.0)
    if status in (400, 404, 422):
        return ProviderError('unsupported_model_or_request', status=status)
    if status is not None and status >= 500:
        return ProviderError('provider_unavailable', status=status, retryable=True)
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or 'Timeout' in name:
        # The server may already have billed a timed-out generation. No immediate retry.
        return ProviderError('timeout')
    if isinstance(exc, ConnectionError) or name in ('APIConnectionError', 'ConnectError'):
        return ProviderError('connection_failed', retryable=True)
    return ProviderError('provider_error')


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def usage_summary(usage, *, anthropic=False):
    if usage is None:
        return {'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': None}
    if anthropic:
        direct = token_count(getattr(usage, 'input_tokens', None))
        def cache_count(field):
            value = getattr(usage, field, None)
            # Optional SDK fields default to None when absent from an uncached response.
            # Explicit null/invalid counts remain unknown rather than an assumed zero.
            fields = getattr(usage, 'model_fields_set', set())
            return 0 if value is None and field not in fields else token_count(value)
        cached = cache_count('cache_read_input_tokens')
        created = cache_count('cache_creation_input_tokens')
        # Anthropic's input count excludes cache reads/writes; OpenAI's includes cached input.
        inp = direct + cached + created if None not in (direct, cached, created) else None
        out = token_count(getattr(usage, 'output_tokens', None))
        return {'input_tokens': inp, 'output_tokens': out, 'cached_input_tokens': cached,
                'cache_creation_input_tokens': created}
    details = getattr(usage, 'prompt_tokens_details', None)
    reasoning = getattr(usage, 'completion_tokens_details', None)
    return {'input_tokens': token_count(getattr(usage, 'prompt_tokens', None)),
            'output_tokens': token_count(getattr(usage, 'completion_tokens', None)),
            'cached_input_tokens': token_count(getattr(details, 'cached_tokens', 0)),
            'reasoning_output_tokens': token_count(getattr(reasoning, 'reasoning_tokens', None))}


@dataclass
class ProviderReply:
    text: str
    usage: dict
    model: str


class LLMProvider(Protocol):
    def complete(self, system: str, user: str, schema: dict, timeout: float) -> ProviderReply: ...


class BoxAPIProvider:
    name = 'boxapi'

    def __init__(self, cfg: AiCfg, client=None):
        self.cfg, self._client = cfg, client
        self.model_info = None

    @property
    def client(self):
        if self._client is None:
            key = os.environ.get('BOXAPI_API_KEY')
            if not key:
                raise ProviderError('missing_credentials')
            u = urlsplit(self.cfg.base_url)
            if (u.scheme != 'https' or u.netloc not in ('ai.boxapi.ir', 'ai.boxapi.ir:443')
                    or u.path.rstrip('/') != '/ai/v1' or u.query or u.fragment):
                raise ProviderError('untrusted_api_origin')
            try:
                from openai import OpenAI
            except ImportError:
                raise ProviderError('openai_sdk_not_installed') from None
            self._client = OpenAI(api_key=key, base_url=self.cfg.base_url, max_retries=0,
                                  timeout=self.cfg.deadline_seconds,
                                  http_client=httpx.Client(trust_env=False, follow_redirects=False))
        return self._client

    def verify_model(self, timeout):
        """Validate against the authenticated catalog; keep the exact configured request name."""
        try:
            result = self.client.models.list(timeout=httpx.Timeout(timeout, connect=min(3, timeout)))
            matches = [m for m in result.data if m.id == self.cfg.model or
                       (self.cfg.model == 'gpt-5.4-mini' and m.id == 'openai/gpt-5.4-mini')]
            if not matches:
                raise ProviderError('model_not_in_catalog')
            row = matches[0]
            self.model_info = {'requested': self.cfg.model, 'catalog_id': row.id,
                               'documented_alias': row.id != self.cfg.model,
                               'supported_parameters': [p for p in (getattr(row, 'supported_parameters', None) or []) if isinstance(p, str)]}
            self.model_info = json.loads(safe_text(json.dumps(self.model_info), self._client))
            return dict(self.model_info)
        except Exception as exc:
            raise provider_error(exc) from None

    def complete(self, system, user, schema, timeout):
        try:
            options = {}
            supported = (self.model_info or {}).get('supported_parameters', [])
            if self.cfg.effort and ('reasoning' in supported or 'reasoning_effort' in supported):
                options['reasoning_effort'] = self.cfg.effort
            response = self.client.chat.completions.create(
                model=self.cfg.model, messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                response_format={'type': 'json_schema', 'json_schema': {'name': 'trading_decision', 'strict': True, 'schema': schema}},
                max_completion_tokens=self.cfg.max_output_tokens,
                timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
                **options,
            )
            usage = usage_summary(response.usage)
            allowed_models = {self.cfg.model, (self.model_info or {}).get('catalog_id')}
            if not isinstance(response.model, str) or response.model not in allowed_models:
                raise ProviderError('unexpected_response_model', usage=usage)
            choices = response.choices
            if not isinstance(choices, list) or len(choices) != 1:
                raise ProviderError('invalid_response_choices', usage=usage)
            choice = choices[0]
            message = getattr(choice, 'message', None)
            if getattr(choice, 'finish_reason', None) != 'stop' or getattr(message, 'refusal', None):
                raise ProviderError('refused_or_truncated', usage=usage)
            if getattr(message, 'tool_calls', None) or not isinstance(getattr(message, 'content', None), str):
                raise ProviderError('invalid_response_content', usage=usage)
            return ProviderReply(safe_text(message.content, self._client), usage, response.model)
        except Exception as exc:
            raise provider_error(exc) from None


class AnthropicProvider:
    name = 'anthropic'

    def __init__(self, cfg: AiCfg, client=None):
        self.cfg, self._client = cfg, client

    @property
    def client(self):
        if self._client is None:
            key, token = os.environ.get('ANTHROPIC_API_KEY'), os.environ.get('ANTHROPIC_AUTH_TOKEN')
            if not (key or token):
                raise ProviderError('missing_credentials')
            try:
                import anthropic
                import httpx2
            except ImportError:
                raise ProviderError('anthropic_sdk_not_installed') from None
            self._client = anthropic.Anthropic(api_key=key, auth_token=token, base_url='https://api.anthropic.com',
                timeout=self.cfg.deadline_seconds, max_retries=0,
                http_client=httpx2.Client(trust_env=False, follow_redirects=False))
        return self._client

    def complete(self, system, user, schema, timeout):
        try:
            response = self.client.beta.messages.create(
                model=self.cfg.model, max_tokens=self.cfg.max_output_tokens, system=system,
                output_config={'effort': self.cfg.effort, 'format': {'type': 'json_schema', 'schema': schema}},
                messages=[{'role': 'user', 'content': user}], timeout=timeout)
            usage = usage_summary(response.usage, anthropic=True)
            blocks = [b.text for b in response.content if b.type == 'text']
            if response.stop_reason != 'end_turn' or len(blocks) != 1:
                raise ProviderError('refused_or_truncated', usage=usage)
            return ProviderReply(safe_text(blocks[0], self._client), usage, safe_text(response.model, self._client))
        except Exception as exc:
            raise provider_error(exc) from None


def make_provider(cfg, client=None):
    if cfg.provider == 'boxapi':
        return BoxAPIProvider(cfg, client)
    if cfg.provider == 'anthropic':
        return AnthropicProvider(cfg, client)
    raise ProviderError('invalid_provider')
