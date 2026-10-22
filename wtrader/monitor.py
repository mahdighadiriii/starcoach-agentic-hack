"""Monitoring dashboard, read-only JSON/CSV endpoints and evidence export.

Every trade metric is computed from the persisted SQLite records (opened read-only);
live position marks, balances and bot health come from the engine's status file.
The only write this server can perform is ENGAGING the kill switch at level `halt`
(POST /api/kill). It cannot clear it, place, cancel or modify orders, or change config.
No secrets are ever loaded here.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings, redact

D = Decimal
TEHRAN = ZoneInfo("Asia/Tehran")


def _ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def query(db_path: str, sql: str, args=()) -> list[dict]:
    con = _ro(db_path)
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


# ---------------------------------------------------------------- metrics from persisted records
def trade_stats(closed: list[dict]) -> dict:
    """Net (after fees) statistics over closed positions. Decimal arithmetic, never SQL SUM over money."""
    pnl = [D(p["realized_pnl_tmn"] or "0") for p in closed]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    gross_loss = -sum(losses, D(0))
    durations = [(datetime.fromisoformat(p['closed_at']) - datetime.fromisoformat(p['opened_at'])).total_seconds()
                 for p in closed if p.get('opened_at') and p.get('closed_at')]
    return {"average_holding_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "trades": len(pnl), "net_pnl_tmn": f"{sum(pnl, D(0)):.0f}",
            "fees_tmn": f"{sum((D(p['fees_tmn'] or '0') for p in closed), D(0)):.0f}",
            "wins": len(wins), "losses": len(losses),
            "win_rate": f"{len(wins) / len(pnl):.3f}" if pnl else None,
            "avg_pnl_tmn": f"{sum(pnl, D(0)) / len(pnl):.0f}" if pnl else None,
            "avg_win_tmn": f"{sum(wins, D(0)) / len(wins):.0f}" if wins else None,
            "avg_loss_tmn": f"{sum(losses, D(0)) / len(losses):.0f}" if losses else None,
            "profit_factor": f"{sum(wins, D(0)) / gross_loss:.3f}" if gross_loss > 0 else None}


# HOLD categories for decisions recorded before categories existed, or decided after the selector ran
DECISION_CATEGORY = {"RISK_REJECTED": "RISK_REJECTED", "EXPIRED": "EXPIRED", "ENTRY_SUBMITTED": "ENTRY",
                     "DUPLICATE_SUPPRESSED": "DUPLICATE", "PENDING": "PENDING"}
CONNECTIVITY_CATEGORIES = ("MARKET_DATA_UNAVAILABLE", "NO_SETUP_PARTIAL_DATA")


def _status(s: Settings) -> dict:
    try:
        return json.loads(Path(s.status_path).read_text())
    except (OSError, ValueError):
        return {}


def _db(s: Settings, status: dict) -> str:
    return s.db_path  # never follow a status file into another profile's database


def dashboard_data(s: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    st = _status(s)
    if st.get('db_path') and Path(st['db_path']).resolve() != Path(s.db_path).resolve():
        return {'generated_at': now.isoformat(), 'status': {}, 'engine_alive': False,
                'error': 'status/database profile mismatch; refusing to combine runtime evidence'}
    db = _db(s, st)
    out: dict = {"generated_at": now.isoformat(), "status": st, "db_path": db}
    last = st.get("last_cycle_at")
    out["engine_alive"] = bool(last) and 0 <= (now - datetime.fromisoformat(last)).total_seconds() < max(15, s.execution.exit_poll_seconds * 3)
    out["status_age_s"] = round((now - datetime.fromisoformat(last)).total_seconds(), 1) if last else None
    if not Path(db).exists():
        out["error"] = f"database {db} not found: start shadow or live first"
        return out
    closed = query(db, "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at")
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_start = datetime.combine(now.astimezone(TEHRAN).date(), datetime.min.time(), TEHRAN).astimezone(timezone.utc).isoformat()
    started = st.get("started_at") or ""
    out["performance"] = {
        "since_startup": trade_stats([p for p in closed if (p["closed_at"] or "") >= started]),
        "last_hour": trade_stats([p for p in closed if (p["closed_at"] or "") >= hour_ago]),
        "today_tehran": trade_stats([p for p in closed if (p["closed_at"] or "") >= day_start]),
        "all_records": trade_stats(closed),
    }
    unreal = sum((D(p["unrealized_pnl_tmn"]) for p in st.get("open_positions", []) if p.get("unrealized_pnl_tmn")), D(0))
    realized = D(out["performance"]["all_records"]["net_pnl_tmn"])
    out["pnl"] = {"realized_net_tmn": f"{realized:.0f}", "unrealized_tmn": f"{unreal:.0f}",
                  "net_after_fees_tmn": f"{realized + unreal:.0f}",
                  "fees_tmn": f"{sum((D(p['fees_tmn'] or '0') for p in query(db, 'SELECT fees_tmn FROM positions')), D(0)):.0f}",
                  "valuation_current": st.get('valuation_current', True),
                  "note": "realized is net of both fees; unrealized is marked at bid net of the exit fee"}
    eq = query(db, "SELECT at, equity_tmn FROM equity ORDER BY at")
    step = max(1, len(eq) // 400)
    out["equity_curve"] = [[r["at"], r["equity_tmn"]] for r in eq[::step]] + ([[eq[-1]["at"], eq[-1]["equity_tmn"]]] if eq else [])
    out["recent_trades"] = [{k: p[k] for k in ("position_id", "symbol", "family", "opened_at", "closed_at", "entry_price",
                                                "cost_tmn", "proceeds_tmn", "fees_tmn", "realized_pnl_tmn", "exit_reason")}
                            for p in reversed(closed[-50:])]
    # Orders come from this run's own database: a live database only ever holds real exchange orders,
    # a shadow database only simulated (paper) ones. Each row says which, so the two can never be confused.
    real = st.get("simulated") is False and st.get("venue") == "live"
    out["orders_kind"] = "REAL_EXCHANGE" if real else "SIMULATED"
    out["orders"] = query(db, "SELECT client_id, created_at, symbol, side, purpose, state, quantity, limit_price, executed_qty, "
                              "executed_quote, fee_base, fee_quote, error FROM orders ORDER BY created_at DESC LIMIT 100")
    for o in out["orders"]:
        o["kind"] = out["orders_kind"]
    out["decisions"] = query(db, "SELECT created_at, action, candidate_id, multiplier, source, final_action, decision_json, "
                                 "risk_json FROM decisions ORDER BY created_at DESC LIMIT 30")
    for d in out["decisions"]:
        try:
            j = json.loads(d.pop("decision_json") or "{}")
            r = json.loads(d.pop("risk_json") or "null") or {}
        except ValueError:
            j, r = {}, {}
        reason = j.get("reason_summary") or j.get("reason") or j.get("error") or ""
        d["category"] = j.get("category") or DECISION_CATEGORY.get(d["final_action"]) or (
            "SELECTOR_ERROR" if d["source"] == "error" else
            # deterministic HOLDs recorded before categories existed carry only a free-text reason
            "UNCATEGORIZED" if d["source"] == "deterministic" else
            "SELECTOR_HOLD" if d["final_action"] == "HOLD" else d["final_action"])
        d["markets"] = j.get("markets") or {}
        if d["final_action"] == "RISK_REJECTED":
            failed = [f"{c['name']}: {c['detail']}" for c in r.get("checks", []) if not c.get("passed")]
            reason = f"risk gate rejected ({r.get('reason')}): " + "; ".join(failed) + (f" · selector: {reason}" if reason else "")
        elif d["final_action"] == "EXPIRED":
            reason = "candidate expired before the order could be placed" + (f" · selector: {reason}" if reason else "")
        d["reason"] = reason[:600]
    cats: dict[str, int] = {}
    for d in out["decisions"]:
        cats[d["category"]] = cats.get(d["category"], 0) + 1
    out["decision_summary"] = {"window": len(out["decisions"]), "by_category": cats,
                               "connectivity_holds": sum(cats.get(c, 0) for c in CONNECTIVITY_CATEGORIES)}
    out["alerts"] = query(db, "SELECT at, payload_json FROM events WHERE kind='alert' ORDER BY seq DESC LIMIT 20")
    out["errors"] = query(db, "SELECT at, kind, payload_json FROM events WHERE kind IN "
                              "('order_rejected','order_unknown','order_blocked','tick_error','order_quarantined','bootstrap_error') "
                              "ORDER BY seq DESC LIMIT 20")
    return out


def trades_csv(s: Settings) -> str:
    db = _db(s, _status(s))
    rows = query(db, "SELECT p.position_id, p.symbol, p.family, p.status, p.opened_at, p.closed_at, p.entry_price, p.stop, "
                     "p.target, p.qty AS remaining_qty, p.cost_tmn, p.proceeds_tmn, p.fees_tmn, p.realized_pnl_tmn, "
                     "p.exit_reason, p.decision_id FROM positions p ORDER BY p.rowid")
    orders = query(db, "SELECT position_id, client_id, side, purpose, state, quantity, limit_price, executed_qty, "
                       "executed_quote, fee_base, fee_quote, created_at FROM orders ORDER BY created_at")
    by: dict = {}
    for o in orders:
        by.setdefault(o["position_id"], []).append(f"{o['side']}:{o['purpose']}:{o['state']}:{o['executed_qty']}@{o['executed_quote']}")
    buf = io.StringIO()
    fields = list(rows[0]) + ["orders"] if rows else ["position_id"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow({**r, "orders": " | ".join(by.get(r["position_id"], []))})
    return buf.getvalue()


def orders_csv(s: Settings) -> str:
    rows = query(_db(s, _status(s)), "SELECT * FROM orders ORDER BY created_at")
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


def serve(s: Settings, host: str | None = None):
    make_server(s, host).serve_forever()


def make_server(s: Settings, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """Build the dashboard server (port 0 picks a free port; tests use that)."""
    host = host or os.environ.get("MONITOR_HOST", "127.0.0.1")
    routes = {
        "/decisions": "SELECT * FROM decisions ORDER BY created_at DESC LIMIT 200",
        "/orders": "SELECT * FROM orders ORDER BY created_at DESC LIMIT 200",
        "/positions": "SELECT * FROM positions ORDER BY rowid DESC LIMIT 200",
        "/events": "SELECT * FROM events ORDER BY seq DESC LIMIT 300",
        "/alerts": "SELECT * FROM events WHERE kind='alert' ORDER BY seq DESC LIMIT 100",
    }
    page = (Path(__file__).parent / "dashboard.html").read_text()

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: str, ctype="application/json", extra=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    self._send(200, page, "text/html; charset=utf-8")
                elif path == "/status":
                    self._send(200, redact(Path(s.status_path).read_text()))
                elif path == "/api/dashboard":
                    self._send(200, redact(json.dumps(dashboard_data(s), default=str)))
                elif path in ("/api/trades.csv", "/api/orders.csv"):
                    body = trades_csv(s) if "trades" in path else orders_csv(s)
                    name = f"wtrader_{path.split('/')[-1].replace('.csv', '')}_{datetime.now(timezone.utc):%Y%m%dT%H%M}.csv"
                    self._send(200, body, "text/csv", {"Content-Disposition": f'attachment; filename="{name}"'})
                elif path in routes:
                    self._send(200, redact(json.dumps(query(_db(s, _status(s)), routes[path]), default=str)))
                else:
                    self.send_error(404)
            except Exception as e:
                self._send(503, json.dumps({"error": redact(repr(e))[:300]}))

        def do_POST(self):
            # Engage-only kill switch. Same-origin check blocks cross-site form posts from other pages.
            if self.path != "/api/kill":
                self.send_error(404)
                return
            # Host validation also prevents DNS-rebinding writes from hostile websites.
            from urllib.parse import urlparse
            request_host = self.headers.get('Host') or ''
            hostname = urlparse('http://' + request_host).hostname
            if hostname not in ('localhost', '127.0.0.1', '::1'):
                self._send(403, json.dumps({'error': 'untrusted host'}))
                return
            origin = self.headers.get("Origin") or ""
            if origin and not origin.endswith(f"//{self.headers.get('Host')}"):
                self._send(403, json.dumps({"error": "cross-origin request refused"}))
                return
            from .safety import KillSwitch
            st = KillSwitch(s.execution.kill_switch_path).engage("halt", "dashboard kill button", by="dashboard")
            self._send(200, json.dumps(st))

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((host, s.monitor_port if port is None else port), H)


def export(s: Settings, out_dir: str | None = None) -> Path:
    out = Path(out_dir or Path(s.evidence_dir) / "export")
    out.mkdir(parents=True, exist_ok=True)
    db = _db(s, _status(s))
    tables = {"decisions": "SELECT * FROM decisions ORDER BY created_at",
              "orders": "SELECT * FROM orders ORDER BY created_at",
              "positions": "SELECT * FROM positions ORDER BY rowid",
              "equity": "SELECT at, equity_tmn, free_tmn FROM equity ORDER BY at",
              "events": "SELECT * FROM events ORDER BY seq",
              "runs": "SELECT * FROM runs"}
    for name, sql in tables.items():
        rows = query(db, sql)
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, default=str))
        if rows:
            with (out / f"{name}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    # trade trace: position -> decision -> snapshot -> orders
    trace = []
    for p in query(db, "SELECT * FROM positions ORDER BY rowid"):
        d = query(db, "SELECT * FROM decisions WHERE decision_id=?", (p["decision_id"],))
        o = query(db, "SELECT client_id, side, purpose, state, quantity, limit_price, executed_qty, executed_quote, "
                      "fee_base, fee_quote FROM orders WHERE position_id=? ORDER BY created_at", (p["position_id"],))
        trace.append({"position": p, "decision": d[0] if d else None, "orders": o})
    (out / "trade_trace.json").write_text(json.dumps(trace, indent=1, default=str))
    return out
