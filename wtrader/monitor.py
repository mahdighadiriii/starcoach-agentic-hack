"""Read-only monitoring endpoint and evidence export. No trading controls exist here by design."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings


def _ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def query(db_path: str, sql: str, args=()) -> list[dict]:
    with _ro(db_path) as con:
        return [dict(r) for r in con.execute(sql, args)]


def serve(s: Settings, host: str | None = None):
    host = host or os.environ.get("MONITOR_HOST", "127.0.0.1")
    routes = {
        "/decisions": "SELECT * FROM decisions ORDER BY created_at DESC LIMIT 200",
        "/orders": "SELECT * FROM orders ORDER BY created_at DESC LIMIT 200",
        "/positions": "SELECT * FROM positions ORDER BY rowid DESC LIMIT 200",
        "/events": "SELECT * FROM events ORDER BY seq DESC LIMIT 300",
        "/alerts": "SELECT * FROM events WHERE kind='alert' ORDER BY seq DESC LIMIT 100",
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path in ("/", "/status"):
                    body = Path(s.status_path).read_text()
                elif self.path in routes:
                    body = json.dumps(query(s.db_path, routes[self.path]), default=str)
                else:
                    self.send_error(404)
                    return
            except Exception as e:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(json.dumps({"error": repr(e)}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    ThreadingHTTPServer((host, s.monitor_port), H).serve_forever()


def export(s: Settings, out_dir: str | None = None) -> Path:
    out = Path(out_dir or Path(s.evidence_dir) / "export")
    out.mkdir(parents=True, exist_ok=True)
    tables = {"decisions": "SELECT * FROM decisions ORDER BY created_at",
              "orders": "SELECT * FROM orders ORDER BY created_at",
              "positions": "SELECT * FROM positions ORDER BY rowid",
              "equity": "SELECT at, equity_tmn, free_tmn FROM equity ORDER BY at",
              "events": "SELECT * FROM events ORDER BY seq",
              "runs": "SELECT * FROM runs"}
    for name, sql in tables.items():
        rows = query(s.db_path, sql)
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1, default=str))
        if rows:
            with (out / f"{name}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    # trade trace: position -> decision -> snapshot -> orders
    trace = []
    for p in query(s.db_path, "SELECT * FROM positions ORDER BY rowid"):
        d = query(s.db_path, "SELECT * FROM decisions WHERE decision_id=?", (p["decision_id"],))
        o = query(s.db_path, "SELECT client_id, side, purpose, state, quantity, limit_price, executed_qty, executed_quote, "
                             "fee_base, fee_quote FROM orders WHERE position_id=? ORDER BY created_at", (p["position_id"],))
        trace.append({"position": p, "decision": d[0] if d else None, "orders": o})
    (out / "trade_trace.json").write_text(json.dumps(trace, indent=1, default=str))
    return out
