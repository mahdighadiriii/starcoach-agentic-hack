"""Read-only account visibility, independent from paper funds and entry authorization.

An API key's default wallet is not proof of main-wallet or credit ownership.
Discovery never selects a sub-account or establishes isolation of the bot allocation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import redact


def read_account_snapshot(client, symbols, *, previous=None, clock=None, metadata_interval_seconds=300):
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    previous = previous or {}
    stamp = now.isoformat()
    checks = dict(previous.get('checks') or {})

    def attempt(name, fn):
        try:
            value = fn()
            checks[name] = {'ok': True, 'at': stamp}
            return value
        except Exception as exc:
            checks[name] = {'ok': False, 'at': stamp, 'kind': type(exc).__name__,
                            'error': redact(str(exc))[:180]}
            return None

    bal = attempt('balances', client.balances)
    sub = getattr(client, '_sub', None)
    snapshot = {**previous, 'source': 'wallex', 'read_only': True,
                'scope': 'explicit_sub_account' if sub else 'key_default_unverified',
                'endpoint': '/v1/account/balances-detail' if sub else '/v1/account/balances',
                'synced_at': stamp if bal is not None else previous.get('synced_at'),
                'error': None if bal is not None else checks['balances']['error'],
                'balances': previous.get('balances', {}) if bal is None else {
                    cur: {'free': str(b.free), 'locked': str(b.locked), 'total': str(b.total)}
                    for cur, b in sorted(bal.items()) if b.total > 0}}
    last = previous.get('metadata_attempted_at')
    age = (now - datetime.fromisoformat(last)).total_seconds() if last else float('inf')
    if not 0 <= age < metadata_interval_seconds:
        snapshot['metadata_attempted_at'] = stamp
        fees = attempt('fees', client.fee_rates) if hasattr(client, 'fee_rates') else None
        snapshot['fee_rates'] = {sym: {k: str(v) for k, v in fees.get(sym, {}).items()} for sym in symbols} if fees is not None else {}
        subs = attempt('sub_accounts', client.sub_accounts) if hasattr(client, 'sub_accounts') else None
        selected = next((r for r in subs or [] if sub and r.get('clientId') == sub), None)
        # Deliberately omit titles, user IDs, client IDs and key material from display/evidence.
        credit = {'funding_source': 'unverified', 'wallet_connection': 'unknown',
                  'allocation_isolation': 'unverified', 'sub_account_configured': bool(sub),
                  'selected_account_listed': selected is not None if sub and subs is not None else None,
                  'selected_account_type': selected.get('type') if selected else None,
                  'visible_accounts': None if subs is None else [
                      {k: r.get(k) for k in ('type', 'isActive', 'isSuspend')} for r in subs],
                  'active_package': None, 'overview': None}
        if hasattr(client, 'credit_overview'):
            credit['overview'] = attempt('credit_overview', client.credit_overview)
        if sub and hasattr(client, 'active_package'):
            pkg = attempt('active_package', client.active_package)
            if pkg is not None:
                link = pkg['user_package']['connectToWallet']
                credit['wallet_connection'] = 'disabled' if link is None else 'enabled' if isinstance(link, str) and link else 'unknown'
                credit['active_package'] = {
                    'package': {k: v for k, v in pkg['package'].items() if k != 'title'},
                    'user_package': {k: v for k, v in pkg['user_package'].items() if k != 'connectToWallet'}}
        else:
            checks['active_package'] = {'ok': False, 'at': stamp, 'kind': 'ScopeNotSelected',
                                        'error': 'explicit sub-account configuration required; no scope was auto-selected'}
        # Even a disabled main-wallet link does not isolate 1M inside a 5M wallet.
        snapshot['credit'] = credit
    snapshot['checks'] = checks
    snapshot['health'] = client.health() if hasattr(client, 'health') else None
    return snapshot
