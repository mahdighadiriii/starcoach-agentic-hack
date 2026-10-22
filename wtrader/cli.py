"""Command line entry point: python -m wtrader <command>.

There is deliberately no buy/sell command: every order originates from the engine.
Live activation is operator-controlled: `preflight` (read-only) -> `arm-live` (typed
confirmation, expiring, bound to the exact config/commit/allocation) -> `live` with
LIVE_ENABLED=true, which re-runs every check before the first tick.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import RedactingFilter, Secrets, load_dotenv, load_settings
from .llm import credentials_available, effective_provider

log = logging.getLogger("wtrader")
DEFAULT_CONFIG = "config/strategy.yaml"


def _setup_logging(level="INFO"):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


def make_client(s, secrets: Secrets):
    """The one place a WallexClient is built. Trading stays disabled unless the live path enables it."""
    from .wallex import WallexClient
    ex = s.execution
    return WallexClient(secrets.wallex_api_key, secrets.wallex_sub_account_id, secrets.wallex_base_url,
                        ex.request_timeout_seconds, ex.read_attempts, connect_timeout=ex.connect_timeout_seconds,
                        proxy=secrets.wallex_proxy, circuit_failures=ex.circuit_failures,
                        circuit_cooldown=ex.circuit_cooldown_seconds, bind_interface=secrets.wallex_bind_interface)


def make_stream(s, secrets: Secrets):
    from .wallex import bind_sockopts
    from .wallex_ws import DepthStream, ws_url
    opts = bind_sockopts(secrets.wallex_bind_interface) if secrets.wallex_bind_interface else ()
    return DepthStream(ws_url(secrets.wallex_base_url), s.execution.connect_timeout_seconds, secrets.wallex_proxy,
                       sockopt=opts)


def live_gate_failures(s, secrets: Secrets, manifest: dict) -> list[str]:
    fails = []
    if not s.execution.live_rollout_approved:
        fails.append("audit hold: live execution and outage protection remain unverified")
    if s.experimental_shadow or s.strategy.edge_gate != "strict":
        fails.append("live requires a strict, nonexperimental configuration")
    if os.environ.get("LIVE_ENABLED", "").lower() != "true":
        fails.append("LIVE_ENABLED=true not set")
    if not secrets.wallex_api_key:
        fails.append("WALLEX_API_KEY missing")
    if not secrets.wallex_sub_account_id:
        fails.append("WALLEX_SUB_ACCOUNT_CLIENT_ID missing (competition credit accounts need it; set explicitly)")
    if not (s.competition.trade_start and s.competition.trade_end):
        fails.append("competition trade_start/trade_end not configured")
    else:
        for v in (s.competition.trade_start, s.competition.trade_end):
            if datetime.fromisoformat(v).tzinfo is None:
                fails.append(f"timestamp {v} lacks a timezone offset")
    if not s.market.account_allowlist:
        fails.append("organizer-confirmed market allowlist (WALLEX_ALLOWED_MARKETS) is empty")
    if manifest.get("git_dirty") is not False:
        fails.append("working tree is dirty or not a git checkout: commit the exact code before trading")
    if not credentials_available(s.ai):
        fails.append(f"AI mode configured but no {effective_provider(s.ai)} credentials in environment")
    return fails


def prepare_live(s):
    """Settings exactly as `live` will run them (the arm record is bound to this hash)."""
    if s.experimental_shadow or s.strategy.edge_gate != "strict":
        raise SystemExit("advisory/experimental configuration is Shadow-only")
    s.mode = "live"
    s.db_path = s.live_db_path
    return s


def make_arm_check(s, manifest: dict):
    from .safety import arm_failures, load_json

    def check() -> list[str]:
        return arm_failures(load_json(s.execution.live_arm_path), authorized_tmn=s.risk.authorized_capital_tmn,
                            max_order_tmn=s.risk.max_order_tmn, config_hash=s.config_hash(),
                            git_commit=manifest.get("git_commit"))
    return check


def build_engine(s, args, live: bool):
    from .ai_agent import make_selector
    from .engine import Engine
    from .manifest import build_manifest
    from .store import SingleWriterLock, Store
    from .venue import BinanceProxyMarketData, KucoinProxyMarketData, LiveVenue, PaperVenue, WallexMarketData

    secrets = Secrets.from_env()
    client = make_client(s, secrets)
    if args.data_source != "wallex":
        if live:
            raise SystemExit("refusing: live trading requires Wallex market data")
        md = BinanceProxyMarketData() if args.data_source == "binance_proxy" else KucoinProxyMarketData()
    else:
        stream = make_stream(s, secrets) if s.market.depth_source == "websocket" else None
        md = WallexMarketData(client, stream, ws_max_age_s=s.market.max_book_age_seconds / 2)
    if not live:
        from .buffered import BufferedMarketData
        md = BufferedMarketData(md, s.market.max_book_age_seconds)
    venue_label = "live" if live else "paper"
    manifest = build_manifest(s, md.label, venue_label)
    arm_check = None
    if live:
        arm_check = make_arm_check(s, manifest)
        fails = live_gate_failures(s, secrets, manifest) + ([] if getattr(args, "arm_verified", False) else arm_check())
        if fails:
            raise SystemExit("LIVE MODE BLOCKED:\n  - " + "\n  - ".join(fails))
        venue = LiveVenue(client)
    else:
        venue = PaperVenue(md, fee_fraction=Decimal(s.execution.taker_fee_fraction))
    lock = SingleWriterLock(Path(s.db_path).with_suffix(".lock")).acquire()
    store = Store(s.db_path, s.run_id)
    if live:
        from .preflight import run_preflight
        rep = run_preflight(s, client, secrets, store)
        if not rep["passed"]:
            lock.release()
            raise SystemExit("LIVE MODE BLOCKED by preflight:\n  - " + "\n  - ".join(rep["failures"]))
        client.enable_trading()     # the only call site: every live gate above has passed
    store.record_run(s.mode, manifest)
    Path(s.evidence_dir).mkdir(parents=True, exist_ok=True)
    manifest_dir = Path(s.evidence_dir, 'manifests')
    manifest_dir.mkdir(parents=True, exist_ok=True)
    archived = manifest_dir / f"{store.run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.json"
    archived.write_text(json.dumps(manifest, indent=2, default=str))
    Path(s.evidence_dir, "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    if not live:
        if store.get_kv("paper_state"):
            venue.restore(store.get_kv("paper_state"))
        elif store.orders():
            lock.release()
            raise SystemExit("legacy Shadow database has orders without durable paper venue state; use a fresh evaluation database")
        elif store.get_kv("paper_balances"):
            venue.balances_ = {k: Decimal(v) for k, v in store.get_kv("paper_balances").items()}
        else:
            venue.fund("TMN", Decimal(s.competition.initial_capital_tmn))
        venue.max_book_age_s = s.market.max_book_age_seconds
        venue.checkpoint = lambda state: store.set_kv("paper_state", state)
        venue._save()
    if not credentials_available(s.ai):
        log.warning("No %s credentials: LLM candidates will HOLD. Baseline is available as an explicit mode.", effective_provider(s.ai))
    selector = make_selector(s.ai, cache_dir=Path(s.evidence_dir, "ai_cache"))
    eng = Engine(s, store, md, venue, selector, manifest, secrets)
    eng.live_arm_check = arm_check
    eng.account_client = client if secrets.wallex_api_key else None    # read-only balance display in shadow
    return eng, store, venue, lock


def cmd_run(args, live: bool):
    s = load_settings(args.config)
    if live:
        prepare_live(s)
    else:
        s.mode = "shadow"
    eng, store, venue, lock = build_engine(s, args, live)
    try:
        eng.run(max_ticks=args.max_ticks)
    finally:
        eng.close()
        lock.release()


def cmd_probe(args):
    """Read-only verification of the Wallex integration. Never places, cancels or modifies orders."""
    from .wallex import diagnose_network
    s = load_settings(args.config)
    secrets = Secrets.from_env()
    c = make_client(s, secrets)
    out = Path(s.evidence_dir) / "probe" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out.mkdir(parents=True, exist_ok=True)
    report = {"at": datetime.now(timezone.utc).isoformat(), "base_url": secrets.wallex_base_url,
              "credentials": repr(secrets), "checks": {}}

    def check(name, fn, valid=None):
        try:
            v = fn()
            if valid is not None and not valid(v):
                raise ValueError(f"{name}: empty, stale, or inconsistent market data")
            report["checks"][name] = {"ok": True, "result": v}
            print(f"[ok]   {name}: {str(v)[:160]}")
        except Exception as e:
            report["checks"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:200]}")

    if not secrets.wallex_proxy:
        diag = diagnose_network(secrets.wallex_base_url, s.execution.connect_timeout_seconds + 4, c.local_address)
        report["network"] = diag
        print(f"       route: {diag['route_interface'] or ('bound to ' + str(c.bind_interface) + ' ' + str(c.local_address))}"
              f" · env proxy vars: {', '.join(diag['env_proxy_vars']) or 'none'}")
        for st in diag["steps"]:
            print(f"[{'ok' if st['ok'] else 'FAIL':4}] net.{st['step']}: {st.get('detail', st.get('error'))} ({st['ms']} ms)")
        print(f"       network verdict: {diag['verdict']}")
    mk = {}
    check("markets", lambda: (mk.update(c.markets()), {"tmn_spot_markets": len(mk), "limits_source": c.limits_source,
                                                       "sample": {s: {"tick": str(m.tick), "step": str(m.step), "min_qty": str(m.min_quantity),
                                                                      "min_notional": str(m.min_notional)} for s, m in list(mk.items())[:6]}})[1], valid=lambda v: v["tmn_spot_markets"] > 0)
    now = int(datetime.now(timezone.utc).timestamp())
    check("candles_BTCTMN_1m", lambda: (lambda cs: {"n": len(cs), "last_open": cs[-1].open_time if cs else None,
                                                    "age_s": now - cs[-1].open_time if cs else None})(c.candles("BTCTMN", 60, now - 3600, now)), valid=lambda v: v["n"] > 0 and 60 <= v["age_s"] <= 180)
    check("candles_BTCTMN_60", lambda: (lambda cs: {"n": len(cs), "last_open": cs[-1].open_time if cs else None,
                                                    "last_close": str(cs[-1].close) if cs else None, "age_s": now - cs[-1].open_time if cs else None})(c.candles("BTCTMN", 3600, now - 86400, now)), valid=lambda v: v["n"] > 0 and 3600 <= v["age_s"] <= 7380)
    check("depth_BTCTMN", lambda: (lambda b: {"bid": str(b.best_bid), "ask": str(b.best_ask), "spread_bps": str(b.spread_bps()),
                                              "levels": [len(b.bids), len(b.asks)]})(c.book("BTCTMN")))
    check("unit_consistency", lambda: (lambda a, b, u: {"BTCTMN": str(a), "BTCUSDT*USDTTMN": str(b * u), "ratio": str(a / (b * u))})(
        c.book("BTCTMN").best_bid, c.book("BTCUSDT").best_bid, c.book("USDTTMN").best_bid), valid=lambda v: Decimal(".98") <= Decimal(v["ratio"]) <= Decimal("1.02"))
    if args.ws_seconds > 0:
        def ws():
            st = make_stream(s, secrets)
            st.watch(["BTCTMN", "USDTTMN"])
            deadline = time.monotonic() + args.ws_seconds
            try:
                while time.monotonic() < deadline and not st.book("BTCTMN", args.ws_seconds):
                    time.sleep(0.5)
                # Capture while connected: stop() deliberately invalidates all cached sides.
                b = st.book("BTCTMN", args.ws_seconds)
                res = {**st.status(), "two_sided_book": bool(b), "bid": str(b.best_bid) if b else None,
                       "ask": str(b.best_ask) if b else None}
                if not b:
                    raise RuntimeError(f"no two-sided BTCTMN book over WebSocket within {args.ws_seconds}s: {res}")
                return res
            finally:
                st.stop()
        check("websocket_depth_BTCTMN", ws)
    if secrets.wallex_api_key:
        check("auth_balances", lambda: {k: {"free": str(v.free), "locked": str(v.locked)} for k, v in c.balances().items() if v.total > 0})
        check("fees", lambda: {sym: {k: str(v) for k, v in rates.items()} for sym, rates in c.fee_rates().items()
                               if sym in s.market.candidate_symbols}, valid=lambda v: all('maker' in v.get(sym, {}) and 'taker' in v.get(sym, {}) for sym in s.market.candidate_symbols))
        check("open_orders", lambda: {"count": len(c.open_orders())})
        check("order_history", lambda: {"rows": len(c.order_history())})
        from .account import read_account_snapshot
        snapshot = read_account_snapshot(c, s.market.candidate_symbols)
        report['account_snapshot'] = snapshot
        # Optional credit denial is visible independently of successful balance authentication.
        for name, outcome in snapshot['checks'].items():
            if not outcome['ok']:
                print(f"[warn] account.{name}: {outcome.get('kind', 'unverified')}")
    else:
        print("[skip] private endpoints: WALLEX_API_KEY not set (add it to .env)")
    report["client_health"] = c.health()
    report['verification_scope'] = 'core public/balance/fee/order checks; optional credit outcomes are separate'
    report['live_trading_ready'] = False
    assert not c.trading_enabled, "probe client must never be enabled for trading"
    report["orders_sent"] = 0
    report["passed"] = all(c["ok"] for c in report["checks"].values())
    (out / "probe_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"auth: {c.auth_state} · no orders were placed, canceled or modified")
    print(f"report: {out / 'probe_report.json'}")
    c.close()
    if not report["passed"]:
        raise SystemExit(1)


def cmd_llm_probe(args):
    """One synthetic HOLD request; never builds an engine or contacts Wallex."""
    from dataclasses import replace
    from .ai_agent import LLMSelector, build_context, validate_decision
    from .config import redact
    from .llm import provider_error
    s = load_settings(args.config)
    cfg = replace(s.ai, max_attempts=1, max_output_tokens=512)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'provider': effective_provider(cfg),
              'model': cfg.model, 'credentials_present': credentials_available(cfg),
              'synthetic_context_only': True, 'wallex_requests': 0, 'passed': False}
    selector = None
    try:
        if effective_provider(cfg) == 'baseline':
            report.update(passed=True, skipped='baseline mode makes no LLM request')
        else:
            selector = LLMSelector(cfg)
            ctx = build_context(as_of=0, analyses=[], portfolio={}, edge={}, feedback={}, minutes_to_entry_cutoff=None)
            raw, usage, latency = selector._call(ctx)
            decision = validate_decision(json.loads(raw), ctx)
            if decision['action'] != 'HOLD':
                raise ValueError('synthetic connectivity probe must HOLD')
            report.update(passed=True, action='HOLD', usage=usage, latency_ms=latency)
    except Exception as exc:
        err = provider_error(exc)
        report.update(error=err.kind, http_status=err.status, usage=err.usage)
    if selector is not None:
        report['metrics'] = selector.stats()
    out = Path(s.evidence_dir) / 'llm_probe' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out.mkdir(parents=True, exist_ok=False)
    text = redact(json.dumps(report, indent=2))
    (out / 'report.json').write_text(text)
    print(text)
    print(f'report: {out / "report.json"}')
    if not report['passed']:
        raise SystemExit(1)


def cmd_evaluate(args):
    from .evaluate import run_evaluation
    s = load_settings(args.config)
    s.strategy.market_filter_symbol = args.filter_symbol or s.strategy.market_filter_symbol
    o = run_evaluation(s, data_root=Path(args.data), source=args.source, symbols=args.symbols.split(","),
                       train_days=args.train_days, with_ai=args.ai, ai_replay_only=args.replay_only, out_dir=Path(args.out))
    for k, m in o["results"].items():
        print(k, json.dumps({x: m[x] for x in ("net_return_pct", "max_drawdown_pct", "trades", "win_rate", "profit_factor", "avg_net_bps_per_trade")}))
    print(f"reports written to {args.out}/")


def cmd_fetch(args):
    from .data import download
    ps = download(args.symbols.split(","), args.source, args.days, Path(args.data))
    print("\n".join(map(str, ps)))


def cmd_status(args):
    s = load_settings(args.config)
    print(Path(s.status_path).read_text())


def cmd_monitor(args):
    from .monitor import serve
    s = load_settings(args.config)
    if getattr(args, "port", None):
        s.monitor_port = args.port
    print(f"dashboard on http://127.0.0.1:{s.monitor_port}/  (JSON: /api/dashboard, CSV: /api/trades.csv)")
    serve(s)


def cmd_preflight(args):
    from .preflight import run_preflight
    from .store import Store
    s = prepare_live(load_settings(args.config))
    secrets = Secrets.from_env()
    client = make_client(s, secrets)
    store = Store(s.db_path) if Path(s.db_path).exists() else None
    rep = run_preflight(s, client, secrets, store)
    for c in rep["checks"]:
        print(f"[{c['level']:4}] {c['check']}: {json.dumps(c['detail'], default=str)[:180]}")
    print(f"\nPREFLIGHT {'PASSED' if rep['passed'] else 'FAILED'} -> {s.execution.preflight_path}")
    return rep


def cmd_arm_live(args):
    """Operator-only: binds a live authorization to this exact config, commit and allocation. Places no orders."""
    from .manifest import build_manifest
    from .safety import ARM_PHRASE, arm_record
    s = prepare_live(load_settings(args.config))
    phrase = ARM_PHRASE.format(amount=s.risk.authorized_capital_tmn)
    if args.confirm != phrase:
        raise SystemExit(f"refusing: pass --confirm \"{phrase}\" exactly (authorized allocation from config)")
    if Decimal(args.authorized_tmn) != Decimal(s.risk.authorized_capital_tmn):
        raise SystemExit(f"refusing: --authorized-tmn {args.authorized_tmn} != config {s.risk.authorized_capital_tmn}")
    if not 0 < args.hours <= 24:
        raise SystemExit("refusing: --hours must be in (0, 24]")
    manifest = build_manifest(s, "wallex", "live")
    if manifest.get("git_dirty") is not False:
        raise SystemExit("refusing: commit the exact code first (working tree dirty or not a git checkout)")
    rep = cmd_preflight(args)
    if not rep["passed"]:
        raise SystemExit("refusing to arm: preflight failed")
    rec = arm_record(operator=args.operator, authorized_tmn=s.risk.authorized_capital_tmn, max_order_tmn=s.risk.max_order_tmn,
                     config_hash=s.config_hash(), git_commit=manifest.get("git_commit"), hours=args.hours,
                     preflight_at=rep["at"])
    Path(s.execution.live_arm_path).parent.mkdir(parents=True, exist_ok=True)
    Path(s.execution.live_arm_path).write_text(json.dumps(rec, indent=2))
    print(json.dumps(rec, indent=2))
    print("ARMED. Live still requires LIVE_ENABLED=true and `python -m wtrader live` (which re-runs preflight).")


def cmd_disarm_live(args):
    s = load_settings(args.config)
    p = Path(s.execution.live_arm_path)
    print("disarmed" if p.exists() and (p.unlink() or True) else "was not armed")


def cmd_kill(args):
    from .safety import KillSwitch
    s = load_settings(args.config)
    ks = KillSwitch(s.execution.kill_switch_path)
    if args.clear:
        print("kill switch cleared" if ks.clear() else "kill switch was not engaged")
    elif args.status:
        print(json.dumps(ks.status() or {"engaged": False}))
    else:
        if not args.reason:
            raise SystemExit("--reason is required to engage")
        print(json.dumps(ks.engage(args.level, args.reason, by=os.environ.get("USER", "operator"))))


def cmd_compare(args):
    from .evaluate import run_comparison
    o = run_comparison(data_root=Path(args.data), source=args.source, symbols=args.symbols.split(","),
                       train_days=args.train_days, out_dir=Path(args.out), baseline_config=args.baseline_config,
                       scalp_config=args.scalp_config)
    print(f"reports written to {args.out}/SHORT_DURATION_COMPARISON.md")


def cmd_export(args):
    from .monitor import export
    print(export(load_settings(args.config), args.out))


def cmd_liquidate(args):
    """Operator emergency flatten: moves the competition end so the engine enters LIQUIDATE. Logged and disclosed."""
    s = load_settings(args.config)
    live = args.live
    if live:
        from .manifest import build_manifest
        prepare_live(s)
        fails = make_arm_check(s, build_manifest(s, "wallex", "live"))()   # before trade_end changes the hash
        if fails:
            raise SystemExit("LIVE LIQUIDATION BLOCKED:\n  - " + "\n  - ".join(fails))
        args.arm_verified = True
    else:
        s.mode = "shadow"
    s.competition.trade_end = (datetime.now(timezone.utc) + timedelta(minutes=s.competition.begin_liquidation_before_end_minutes - 1)).isoformat()
    eng, store, venue, lock = build_engine(s, args, live)
    store.event("operator_liquidation", {"reason": args.reason, "at": datetime.now(timezone.utc).isoformat()})
    try:
        eng.run(max_ticks=args.max_ticks)
    finally:
        eng.close()
        lock.release()


def main(argv=None):
    p = argparse.ArgumentParser(prog="wtrader")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("shadow", "live", "liquidate"):
        sp = sub.add_parser(name)
        sp.add_argument("--data-source", default="wallex", choices=["wallex", "binance_proxy", "kucoin_proxy"])
        sp.add_argument("--max-ticks", type=int, default=None)
        if name == "liquidate":
            sp.add_argument("--live", action="store_true")
            sp.add_argument("--reason", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("--ws-seconds", type=float, default=20, help="WebSocket depth test duration (0 skips)")
    sub.add_parser('llm-probe', help='one synthetic HOLD request to the configured LLM; no Wallex access')
    ev = sub.add_parser("evaluate")
    ev.add_argument("--data", default="data")
    ev.add_argument("--source", default="binance_proxy", choices=["wallex", "binance_proxy"])
    ev.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,TRXUSDT")
    ev.add_argument("--filter-symbol", default="BTCUSDT")
    ev.add_argument("--train-days", type=int, default=160)
    ev.add_argument("--ai", action="store_true")
    ev.add_argument("--replay-only", action="store_true", help="use only cached AI decisions (reproducible, no API calls)")
    ev.add_argument("--out", default="reports")
    fe = sub.add_parser("fetch-data")
    fe.add_argument("--source", default="wallex", choices=["wallex", "binance_proxy", "kucoin_proxy"])
    fe.add_argument("--symbols", default="BTCTMN,ETHTMN,SOLTMN,XRPTMN,DOGETMN,USDTTMN")
    fe.add_argument("--days", type=int, default=120)
    fe.add_argument("--data", default="data")
    sub.add_parser("status")
    for name in ("monitor", "dashboard"):
        sub.add_parser(name).add_argument("--port", type=int, default=None)
    sub.add_parser("preflight")
    ar = sub.add_parser("arm-live")
    ar.add_argument("--operator", required=True)
    ar.add_argument("--authorized-tmn", required=True)
    ar.add_argument("--hours", type=float, default=12)
    ar.add_argument("--confirm", required=True)
    sub.add_parser("disarm-live")
    ki = sub.add_parser("kill")
    ki.add_argument("--level", default="halt", choices=["halt", "flatten", "freeze"])
    ki.add_argument("--reason", default=None)
    ki.add_argument("--clear", action="store_true")
    ki.add_argument("--status", action="store_true")
    co = sub.add_parser("compare")
    co.add_argument("--data", default="data")
    co.add_argument("--source", default="kucoin_proxy", choices=["wallex", "kucoin_proxy"])
    co.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,TRXUSDT")
    co.add_argument("--train-days", type=int, default=60)
    co.add_argument("--baseline-config", default="config/strategy.yaml")
    co.add_argument("--scalp-config", default="config/scalp.yaml")
    co.add_argument("--out", default="reports")
    ex = sub.add_parser("export")
    ex.add_argument("--out", default=None)
    args = p.parse_args(argv)
    if args.cmd not in ("monitor", "dashboard"):     # the dashboard process never holds credentials
        load_dotenv(os.environ.get("WTRADER_ENV_FILE", ".env"))    # existing environment variables take precedence
    _setup_logging(args.log_level)
    {"shadow": lambda a: cmd_run(a, False), "live": lambda a: cmd_run(a, True), "liquidate": cmd_liquidate,
     "probe": cmd_probe, 'llm-probe': cmd_llm_probe, "evaluate": cmd_evaluate, "fetch-data": cmd_fetch, "status": cmd_status,
     "monitor": cmd_monitor, "dashboard": cmd_monitor, "export": cmd_export, "preflight": cmd_preflight,
     "arm-live": cmd_arm_live, "disarm-live": cmd_disarm_live, "kill": cmd_kill, "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    main()
