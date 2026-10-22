"""Read-only live-readiness checks. Never places, modifies or cancels an order.

Run by `wtrader preflight`, by `wtrader arm-live` (which refuses on any failure) and
again by `wtrader live` at startup. Each check is FAIL (blocks live), WARN (allowed,
reported) or OK. Results are written to execution.preflight_path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import Secrets, Settings, redact
from .models import ZERO
from .markets import DISABLED_MARKETS
from .safety import KillSwitch, foreign_buy_reserved

D = Decimal


def run_preflight(s: Settings, client, secrets: Secrets, store=None) -> dict:
    res: list[dict] = []

    def add(name, level, detail):
        res.append({"check": name, "level": level, "detail": detail})

    def attempt(name, fn, level="FAIL"):
        try:
            return fn()
        except Exception as e:
            add(name, level, f"{type(e).__name__}: {redact(str(e))[:200]}")
            return None

    r = s.risk
    authorized, credit, max_order = D(r.authorized_capital_tmn), D(r.total_credit_tmn), D(r.max_order_tmn)
    if s.mode == 'live':
        add('live_rollout_approved', 'OK' if s.execution.live_rollout_approved else 'FAIL',
            'audit hold remains until authenticated exchange execution and outage safety are independently validated')
        add('strict_nonexperimental_profile', 'OK' if s.strategy.edge_gate == 'strict' and not s.experimental_shadow else 'FAIL', s.strategy.edge_gate)
    # ---- configuration sanity (no network)
    add("config_allocation_within_credit", "OK" if 0 < authorized <= credit else "FAIL", f"{authorized} <= {credit}")
    add("config_max_order_within_allocation", "OK" if 0 < max_order <= authorized else "FAIL", f"{max_order} <= {authorized}")
    add("config_positions_fit_allocation", "OK" if r.max_positions * max_order <= authorized else "FAIL",
        f"{r.max_positions} x {max_order} <= {authorized}")
    add("config_daily_loss_limit_set", "OK" if r.daily_loss_limit_tmn and D(r.daily_loss_limit_tmn) > 0 else "FAIL",
        r.daily_loss_limit_tmn)
    add("credentials_present", "OK" if secrets.wallex_api_key else "FAIL", repr(secrets))
    add("sub_account_id_present", "OK" if secrets.wallex_sub_account_id else "FAIL",
        "trade-credit accounts require the sub-account-client-id header")
    ks = KillSwitch(s.execution.kill_switch_path).status()
    add("kill_switch_clear", "OK" if ks is None else "FAIL", ks or "clear")
    if store is not None:
        bad = store.orders(("SUBMITTING", "UNKNOWN", "RECONCILING", "QUARANTINED"))
        add("no_unresolved_orders_in_db", "OK" if not bad else "FAIL", [o["client_id"] for o in bad])

    add("client_trading_disabled", "OK" if not getattr(client, "trading_enabled", False) else "FAIL",
        "preflight runs on a client that cannot submit or cancel orders")
    # ---- public endpoints
    markets = attempt("markets", client.markets) or {}
    if not markets and hasattr(client, "base_url"):
        from .wallex import diagnose_network
        diag = diagnose_network(client.base_url, s.execution.connect_timeout_seconds + 4,
                                getattr(client, "local_address", None))
        add("network_diagnosis", "FAIL", {"verdict": diag["verdict"], "failed_step": diag["failed_step"]})
    if getattr(client, "limits_source", None):
        add("exchange_minimums_source", "OK" if "undocumented" in client.limits_source else "FAIL" if s.mode == "live" else "WARN", client.limits_source)
    cands = s.market.candidate_symbols
    allow = s.market.account_allowlist
    excluded = sorted(set(cands + allow) & DISABLED_MARKETS)
    add('no_reference_disabled_markets', 'FAIL' if excluded else 'OK',
        {'excluded': excluded, 'source': 'develop 0ad75f3 app/markets.py; deny-only, not an account allowlist'})
    if markets:
        missing = [x for x in cands if x not in markets]
        add("markets", "OK" if not missing else "WARN", {"tmn_markets": len(markets), "missing_candidates": missing})
        add("allowlist_markets_exist", "OK" if allow and all(a in markets for a in allow) else "FAIL",
            {"allowlist": allow, "unknown": [a for a in allow if a not in markets]})
        filt = {}
        for sym in cands:
            m = markets.get(sym)
            if m:
                filt[sym] = {"tick": str(m.tick), "step": str(m.step), "min_qty": str(m.min_quantity),
                             "min_notional": str(m.min_notional)}
                if m.min_notional > max_order:
                    add(f"min_notional_{sym}", "FAIL", f"exchange minimum {m.min_notional} > max order {max_order}")
        add("exchange_filters", "OK", filt)
    flags = attempt("spot_flags", client.spot_flags)
    if flags is not None:
        not_spot = [x for x in (allow or cands) if x in flags and not flags[x]]
        add("spot_flags", "OK" if not not_spot else "FAIL", {"not_spot": not_spot})
    for sym in (allow or cands)[:4]:
        b = attempt(f"depth_{sym}", lambda sym=sym: client.book(sym))
        if b is not None:
            spread = b.spread_bps()
            depth = sum((p * q for p, q in b.asks[:5]), ZERO)
            lvl = "OK" if spread is not None and spread <= D(s.market.max_spread_bps) and depth >= max_order * 4 else "WARN"
            add(f"depth_{sym}", lvl, {"spread_bps": f"{spread:.1f}" if spread is not None else None,
                                      "ask_depth_5_levels_tmn": f"{depth:.0f}"})

    # ---- private endpoints (auth)
    if secrets.wallex_api_key:
        bal = attempt("balances", client.balances)
        if bal is not None:
            tmn = bal.get("TMN")
            free = tmn.free if tmn else ZERO
            add("auth_balances", "OK", {c: {"free": str(b.free), "locked": str(b.locked)} for c, b in bal.items() if b.total > 0})
            add("tmn_covers_allocation", "OK" if free >= authorized else "FAIL", f"free TMN {free:.0f} >= authorized {authorized}")
            others = [c for c, b in bal.items() if c != "TMN" and b.total > 0]
            if free > authorized or others:
                add("allocation_isolation", "WARN",
                    "account holds more than the authorized allocation; the bot enforces the cap in its own risk and "
                    "execution layers, but the exchange cannot. Use a dedicated sub-account holding exactly the "
                    f"allocation for strict isolation. Other assets: {others}")
        fees = attempt("fees", client.taker_fees)
        if fees is not None:
            raws = {x: markets[x].raw_symbol for x in cands if x in markets}
            got = {x: str(fees[r]) for x, r in raws.items() if r in fees}
            missing_fees = [x for x in cands if x not in got]
            assumed = D(s.execution.taker_fee_fraction)
            worse = {x: f for x, f in got.items() if D(f) > assumed}
            add("fees", "OK" if got and not worse and not missing_fees else "FAIL" if s.mode == "live" else "WARN",
                {"taker_fee_by_market": got, "configured_assumption": str(assumed),
                 "higher_than_assumed": worse, "missing_markets": missing_fees})
        oo = attempt("open_orders", client.open_orders)
        if oo is not None:
            known = {o["client_id"] for o in store.orders()} if store is not None else set()
            reserved, rows = foreign_buy_reserved(oo, known)
            add("no_foreign_open_orders", "OK" if not rows else "FAIL",
                {"foreign": rows, "reserved_tmn": f"{reserved:.0f}"})
        hist = attempt("order_history", client.order_history, level="WARN")
        if hist is not None:
            add("order_history", "OK", {"rows": len(hist)})
        if secrets.wallex_sub_account_id:
            subs = attempt("trade_credit_sub_accounts", client.sub_accounts, level="WARN")
            if subs is not None:
                me = next((x for x in subs if str(x.get("clientId")) == secrets.wallex_sub_account_id), None)
                ok = me is not None and me.get("isActive") is True and me.get("isSuspend") is False
                add("trade_credit_sub_account_active", "OK" if ok else "FAIL",
                    {"listed": me is not None, "isActive": me and me.get("isActive"), "isSuspend": me and me.get("isSuspend"),
                     "type": me and me.get("type"), "visible_accounts": len(subs)})
            pkg = attempt("trade_credit_package", client.active_package, level="WARN")
            if pkg is not None:
                up = pkg.get("user_package") or {}
                add("trade_credit_package", "OK", {**(pkg.get("package") or {}),
                                                   **{k: up.get(k) for k in ("status", "expireAt", "warningLimit", "liquidityLimit")}})
                add('trade_credit_package_active', 'OK' if up.get('status') == 'active' else 'FAIL', up.get('status'))
                linked = up.get("connectToWallet")
                wallet_detail = ("wallet connection state missing; isolation unverified" if 'connectToWallet' not in up else
                    "main-wallet link disabled; this does not isolate the bot allocation within the sub-account" if linked is None else
                    "connectToWallet is enabled or unrecognized: main-wallet exposure remains possible; verify in Wallex")
                add("trade_credit_wallet_isolation", "OK" if 'connectToWallet' in up and linked is None else "FAIL",
                    wallet_detail)
            else:
                add("trade_credit_wallet_isolation", "FAIL", "could not read the active package; verify connectToWallet "
                                                             "is disabled in the Wallex UI before arming")
        if getattr(client, "last_rate_headers", None):
            add("rate_limit_headers", "OK", client.last_rate_headers)

    fails = [x for x in res if x["level"] == "FAIL"]
    report = {"at": datetime.now(timezone.utc).isoformat(), "passed": not fails, "failures": [f["check"] for f in fails],
              "warnings": [x["check"] for x in res if x["level"] == "WARN"], "checks": res,
              "base_url": secrets.wallex_base_url}
    p = Path(s.execution.preflight_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str))
    return report
