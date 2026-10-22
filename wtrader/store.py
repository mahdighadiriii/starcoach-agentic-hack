"""SQLite persistence: orders (intent before submit), positions, decisions, events, equity.

Adapted from the challenge pack contracts/schema.sql. Money is stored as TEXT
Decimal strings; never SUM() it in SQL. A process-level exclusive file lock
guarantees a single order writer per database/account.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  snapshot_ref TEXT, n_candidates INTEGER NOT NULL, action TEXT NOT NULL, candidate_id TEXT, multiplier TEXT,
  source TEXT NOT NULL, baseline_candidate_id TEXT, decision_json TEXT NOT NULL, risk_json TEXT,
  final_action TEXT NOT NULL, order_client_id TEXT);
CREATE TABLE IF NOT EXISTS orders (
  client_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, economic_key TEXT NOT NULL UNIQUE,
  symbol TEXT NOT NULL, side TEXT NOT NULL, purpose TEXT NOT NULL, quantity TEXT NOT NULL,
  limit_price TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, position_id TEXT,
  executed_qty TEXT NOT NULL DEFAULT '0', executed_quote TEXT NOT NULL DEFAULT '0',
  fee_base TEXT NOT NULL DEFAULT '0', fee_quote TEXT NOT NULL DEFAULT '0',
  applied_qty TEXT NOT NULL DEFAULT '0', applied_quote TEXT NOT NULL DEFAULT '0',
  applied_fee_base TEXT NOT NULL DEFAULT '0', applied_fee_quote TEXT NOT NULL DEFAULT '0',
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT);
CREATE INDEX IF NOT EXISTS orders_state ON orders(state);
CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, family TEXT NOT NULL,
  candidate_id TEXT NOT NULL, decision_id TEXT, status TEXT NOT NULL,
  qty TEXT NOT NULL, cost_tmn TEXT NOT NULL, entry_price TEXT, stop TEXT NOT NULL, target TEXT,
  max_bars INTEGER NOT NULL, signal_seconds INTEGER NOT NULL, opened_at TEXT, closed_at TEXT,
  proceeds_tmn TEXT NOT NULL DEFAULT '0', realized_pnl_tmn TEXT, exit_reason TEXT, fees_tmn TEXT NOT NULL DEFAULT '0');
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
  at TEXT NOT NULL, run_id TEXT NOT NULL, equity_tmn TEXT NOT NULL, free_tmn TEXT NOT NULL, detail_json TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SingleWriterLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"another trader instance holds {self.path}; refusing to start a second order writer")
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class Store:
    def __init__(self, path: str | Path, run_id: str | None = None):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.run_id = run_id or self.get_kv("run_id") or f"R{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        self.set_kv("run_id", self.run_id)

    # -------------------------------------------------- kv
    def get_kv(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set_kv(self, k, v):
        self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, default=str)))

    # -------------------------------------------------- runs/events
    def record_run(self, mode: str, manifest: dict):
        self.db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?)", (self.run_id, mode, now_iso(), json.dumps(manifest, default=str)))

    def event(self, kind: str, payload: dict):
        self.db.execute("INSERT INTO events(run_id,at,kind,payload_json) VALUES(?,?,?,?)",
                        (self.run_id, now_iso(), kind, json.dumps(payload, default=str)))

    def events(self, kind: str | None = None, limit: int = 100):
        q = "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY seq DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(q, (kind, limit) if kind else (limit,))]

    # -------------------------------------------------- decisions
    def record_decision(self, **f) -> str:
        did = f.pop("decision_id", None) or "D" + uuid.uuid4().hex[:16].upper()
        self.db.execute(
            "INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, self.run_id, now_iso(), f["snapshot_id"], f.get("snapshot_ref"), f["n_candidates"], f["action"],
             f.get("candidate_id"), f.get("multiplier"), f["source"], f.get("baseline_candidate_id"),
             json.dumps(f["decision"], default=str), json.dumps(f.get("risk"), default=str) if f.get("risk") else None,
             f["final_action"], f.get("order_client_id")))
        return did

    def update_decision(self, decision_id: str, **f):
        sets = ", ".join(f"{k}=?" for k in f)
        vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in f.values()]
        self.db.execute(f"UPDATE decisions SET {sets} WHERE decision_id=?", (*vals, decision_id))

    def decisions(self, limit: int = 50):
        return [dict(r) for r in self.db.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))]

    # -------------------------------------------------- orders
    def insert_order(self, *, client_id, economic_key, symbol, side, purpose, quantity, limit_price,
                     decision_id=None, position_id=None, state="CREATED") -> bool:
        """Returns False if this economic intent already exists (idempotency across restarts)."""
        try:
            self.db.execute(
                "INSERT INTO orders(client_id,run_id,economic_key,symbol,side,purpose,quantity,limit_price,state,"
                "decision_id,position_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, self.run_id, economic_key, symbol, side, purpose, str(quantity), str(limit_price), state,
                 decision_id, position_id, now_iso(), now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order(self, client_id: str, **f):
        f["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE orders SET {sets} WHERE client_id=?",
                        (*[str(v) if isinstance(v, Decimal) else v for v in f.values()], client_id))

    def order(self, client_id: str):
        r = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def orders(self, states: tuple | None = None):
        if states:
            q = f"SELECT * FROM orders WHERE state IN ({','.join('?' * len(states))}) ORDER BY created_at"
            return [dict(r) for r in self.db.execute(q, states)]
        return [dict(r) for r in self.db.execute("SELECT * FROM orders ORDER BY created_at")]

    # -------------------------------------------------- positions
    def insert_position(self, **f) -> str:
        pid = f.get("position_id") or "P" + uuid.uuid4().hex[:12].upper()
        self.db.execute(
            "INSERT INTO positions(position_id,run_id,symbol,family,candidate_id,decision_id,status,qty,cost_tmn,"
            "stop,max_bars,signal_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, self.run_id, f["symbol"], f["family"], f["candidate_id"], f.get("decision_id"), "PENDING",
             "0", "0", str(f["stop"]), f["max_bars"], f["signal_seconds"]))
        return pid

    def update_position(self, pid: str, **f):
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE positions SET {sets} WHERE position_id=?",
                        (*[str(v) if isinstance(v, Decimal) else v for v in f.values()], pid))

    def position(self, pid: str):
        r = self.db.execute("SELECT * FROM positions WHERE position_id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def positions(self, statuses=("PENDING", "OPEN", "CLOSING")):
        q = f"SELECT * FROM positions WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY rowid"
        return [dict(r) for r in self.db.execute(q, statuses)]

    def closed_positions(self, limit: int = 1000):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?", (limit,))]

    # -------------------------------------------------- equity
    def record_equity(self, equity, free, detail: dict):
        self.db.execute("INSERT INTO equity VALUES(?,?,?,?,?)",
                        (now_iso(), self.run_id, str(equity), str(free), json.dumps(detail, default=str)))

    def equity_series(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM equity ORDER BY at")]
