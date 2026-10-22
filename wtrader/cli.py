"""Command line entry point: python -m wtrader <command>.

There is deliberately no buy/sell command: every order originates from the engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import Secrets, load_settings

log = logging.getLogger("wtrader")
DEFAULT_CONFIG = "config/strategy.yaml"


def _setup_logging(level="INFO"):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def live_gate_failures(s, secrets: Secrets, manifest: dict) -> list[str]:
    fails = []
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
    if s.ai.mode == "ai" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        fails.append("AI mode configured but no Anthropic credentials in environment")
    return fails


def build_engine(s, args, live: bool):
    from .ai_agent import make_selector
    from .engine import Engine
    from .manifest import build_manifest
    from .store import SingleWriterLock, Store
    from .venue import BinanceProxyMarketData, LiveVenue, PaperVenue, WallexMarketData
    from .wallex import WallexClient

    secrets = Secrets.from_env()
    client = WallexClient(secrets.wallex_api_key, secrets.wallex_sub_account_id, secrets.wallex_base_url,
                          s.execution.request_timeout_seconds, s.execution.read_attempts)
    if args.data_source == "binance_proxy":
        if live:
            raise SystemExit("refusing: live trading requires Wallex market data")
        md = BinanceProxyMarketData()
    else:
        md = WallexMarketData(client)
    venue_label = "live" if live else "paper"
    manifest = build_manifest(s, md.label, venue_label)
    if live:
        fails = live_gate_failures(s, secrets, manifest)
        if fails:
            raise SystemExit("LIVE MODE BLOCKED:\n  - " + "\n  - ".join(fails))
        venue = LiveVenue(client)
    else:
        venue = PaperVenue(md, fee_fraction=Decimal(s.execution.taker_fee_fraction))
    lock = SingleWriterLock(Path(s.db_path).with_suffix(".lock")).acquire()
    store = Store(s.db_path, s.run_id)
    store.record_run(s.mode, manifest)
    Path(s.evidence_dir).mkdir(parents=True, exist_ok=True)
    Path(s.evidence_dir, "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    if not live:
        if store.get_kv("paper_balances"):
            venue.balances_ = {k: Decimal(v) for k, v in store.get_kv("paper_balances").items()}
        else:
            venue.fund("TMN", Decimal(s.competition.initial_capital_tmn))
    selector = make_selector(s.ai, cache_dir=Path(s.evidence_dir, "ai_cache"))
    eng = Engine(s, store, md, venue, selector, manifest, secrets)
    return eng, store, venue, lock


def cmd_run(args, live: bool):
    s = load_settings(args.config)
    s.mode = "live" if live else "shadow"
    eng, store, venue, lock = build_engine(s, args, live)
    if not live:
        # persist simulated balances so a restarted shadow run continues the same paper account
        orig_tick = eng.tick

        def tick():
            orig_tick()
            store.set_kv("paper_balances", {k: str(v) for k, v in venue.balances_.items()})
        eng.tick = tick
    try:
        eng.run(max_ticks=args.max_ticks)
    finally:
        lock.release()


def cmd_probe(args):
    """Read-only verification of the Wallex integration. Never places orders."""
    from .wallex import WallexClient
    secrets = Secrets.from_env()
    c = WallexClient(secrets.wallex_api_key, secrets.wallex_sub_account_id, secrets.wallex_base_url)
    out = Path("evidence/probe")
    out.mkdir(parents=True, exist_ok=True)
    report = {"at": datetime.now(timezone.utc).isoformat(), "base_url": secrets.wallex_base_url, "checks": {}}

    def check(name, fn):
        try:
            v = fn()
            report["checks"][name] = {"ok": True, "result": v}
            print(f"[ok]   {name}: {str(v)[:160]}")
        except Exception as e:
            report["checks"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:200]}")

    mk = {}
    check("markets", lambda: (mk.update(c.markets()), {s: {"tick": str(m.tick), "step": str(m.step), "min_qty": str(m.min_quantity),
                                                           "min_notional": str(m.min_notional)} for s, m in list(mk.items())[:8]})[1])
    check("spot_flags", lambda: {k: v for k, v in list(c.spot_flags().items())[:8]})
    now = int(datetime.now(timezone.utc).timestamp())
    check("candles_BTCTMN_60", lambda: (lambda cs: {"n": len(cs), "last_open": cs[-1].open_time if cs else None,
                                                    "last_close": str(cs[-1].close) if cs else None})(c.candles("BTCTMN", 3600, now - 86400, now)))
    check("depth_BTCTMN", lambda: (lambda b: {"bid": str(b.best_bid), "ask": str(b.best_ask), "spread_bps": str(b.spread_bps())})(c.book("BTCTMN")))
    check("unit_consistency", lambda: (lambda a, b, u: {"BTCTMN": str(a), "BTCUSDT*USDTTMN": str(b * u), "ratio": str(a / (b * u))})(
        c.book("BTCTMN").best_bid, c.book("BTCUSDT").best_bid, c.book("USDTTMN").best_bid))
    if secrets.wallex_api_key:
        check("balances", lambda: {k: {"free": str(v.free), "locked": str(v.locked)} for k, v in c.balances().items() if v.total > 0})
        check("fees", lambda: {k: str(v) for k, v in list(c.taker_fees().items())[:10]})
        check("open_orders", lambda: [o.client_id for o in c.open_orders()])
    else:
        print("[skip] private endpoints: WALLEX_API_KEY not set")
    (out / "probe_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"report: {out / 'probe_report.json'}")


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
    print(f"read-only monitor on http://127.0.0.1:{s.monitor_port}/status")
    serve(s)


def cmd_export(args):
    from .monitor import export
    print(export(load_settings(args.config), args.out))


def cmd_liquidate(args):
    """Operator emergency flatten: moves the competition end so the engine enters LIQUIDATE. Logged and disclosed."""
    s = load_settings(args.config)
    live = args.live
    s.mode = "live" if live else "shadow"
    s.competition.trade_end = (datetime.now(timezone.utc) + timedelta(minutes=s.competition.begin_liquidation_before_end_minutes - 1)).isoformat()
    eng, store, venue, lock = build_engine(s, args, live)
    store.event("operator_liquidation", {"reason": args.reason, "at": datetime.now(timezone.utc).isoformat()})
    try:
        eng.run(max_ticks=args.max_ticks)
    finally:
        lock.release()


def main(argv=None):
    p = argparse.ArgumentParser(prog="wtrader")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("shadow", "live", "liquidate"):
        sp = sub.add_parser(name)
        sp.add_argument("--data-source", default="wallex", choices=["wallex", "binance_proxy"])
        sp.add_argument("--max-ticks", type=int, default=None)
        if name == "liquidate":
            sp.add_argument("--live", action="store_true")
            sp.add_argument("--reason", required=True)
    sub.add_parser("probe")
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
    fe.add_argument("--source", default="wallex", choices=["wallex", "binance_proxy"])
    fe.add_argument("--symbols", default="BTCTMN,ETHTMN,SOLTMN,XRPTMN,DOGETMN,USDTTMN")
    fe.add_argument("--days", type=int, default=120)
    fe.add_argument("--data", default="data")
    sub.add_parser("status")
    sub.add_parser("monitor")
    ex = sub.add_parser("export")
    ex.add_argument("--out", default=None)
    args = p.parse_args(argv)
    _setup_logging(args.log_level)
    {"shadow": lambda a: cmd_run(a, False), "live": lambda a: cmd_run(a, True), "liquidate": cmd_liquidate,
     "probe": cmd_probe, "evaluate": cmd_evaluate, "fetch-data": cmd_fetch, "status": cmd_status,
     "monitor": cmd_monitor, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
