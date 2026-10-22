"""Wallex WebSocket order-book feed (Centrifugo v2, public channels, no token).

Protocol per developers.wallex.ir (socket-raw-protocol, socket-buy-depth, socket-sell-depth):
  URL       wss://api.wallex.ir/connection/websocket
  connect   {"id": 1, "connect": {}}                      (public channels need no token)
  subscribe {"id": n, "subscribe": {"channel": "orderbook:BTCTMN@buyDepth"}}   (and @sellDepth)
  data      {"push": {"channel": ..., "pub": {"data": {"message": [{"price", "quantity", "sum"}, ...]}}}}
            (observed on the real server 2026-10-22; a bare list under "data" is also accepted)
            every message is a FULL snapshot of one side, not a diff
  ping      the server sends "{}" every 25 s; reply "{}" within 8 s or it closes with 3012
  errors    {"id": n, "error": {"code", "message"}}; close codes >= 3500 mean "do not retry"
Subscribing returns no snapshot and the server keeps no history, so a side is unknown until it
first changes. A book is served only when both sides arrived over a live connection and the older
side is younger than `max_age`; otherwise the caller falls back to REST GET /v1/depth.
"""
from __future__ import annotations

import json
import logging
import struct
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

from .config import redact
from .models import Book, dec

log = logging.getLogger(__name__)
SIDES = {"buyDepth": "bids", "sellDepth": "asks"}


def ws_url(base_url: str) -> str:
    return f"wss://{urlparse(base_url).hostname or 'api.wallex.ir'}/connection/websocket"


class StreamClosed(ConnectionError):
    def __init__(self, code):
        self.status_code = code
        super().__init__(f"server closed with {code}")


class DepthStream:
    def __init__(self, url: str = "wss://api.wallex.ir/connection/websocket", connect_timeout: float = 6,
                 proxy: str | None = None, clock=None, sockopt: tuple = (), connector=None):
        self.url, self.connect_timeout, self.proxy = url, connect_timeout, proxy
        self.sockopt = tuple(sockopt)       # e.g. bind to a network interface (WALLEX_BIND_INTERFACE)
        self.connector = connector          # tests inject a fake websocket factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.symbols: list[str] = []
        self.sides: dict[tuple[str, str], tuple[tuple, datetime]] = {}
        self.connected = False
        self.connected_at: datetime | None = None
        self.last_msg_at: datetime | None = None
        self.last_error: dict | None = None
        self.reconnects = 0
        self.backoff_initial = 1.0
        self.fatal: str | None = None
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self.subscribed = set()

    # ------------------------------------------------------------------ pure protocol handling (tested)
    def subscribe_frames(self) -> list[str]:
        frames = [json.dumps({"id": 1, "connect": {}})]
        for i, (sym, side) in enumerate(((s, d) for s in self.symbols for d in SIDES), start=2):
            frames.append(json.dumps({"id": i, "subscribe": {"channel": f"orderbook:{sym}@{side}"}}))
        return frames

    def handle(self, raw: str) -> list[str]:
        """Process one frame (may hold several newline-separated messages). Returns frames to send back."""
        replies = []
        now = self.clock()
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            self.last_msg_at = now
            if line == "{}":
                replies.append("{}")            # server ping -> pong
                continue
            try:
                msg = json.loads(line, parse_float=Decimal)
            except ValueError:
                self._error("BadFrame", line[:80])
                continue
            if not isinstance(msg, dict):
                self._error("BadFrame", "expected object")
                continue
            if isinstance(msg.get("connect"), dict):
                self.connected = True
            if "subscribe" in msg:
                self.subscribed.add(msg.get("id"))
            if "error" in msg:
                err = msg["error"] or {}
                self._error(f"WS{err.get('code')}", str(err.get("message")))
                continue
            push = msg.get("push") or {}
            data = (push.get("pub") or {}).get("data")
            if isinstance(data, dict):
                data = data.get("message")
            channel = str(push.get("channel") or "")
            if data is None or not channel.startswith("orderbook:") or "@" not in channel:
                continue
            sym, kind = channel[len("orderbook:"):].split("@", 1)
            side = SIDES.get(kind)
            if side is None or not isinstance(data, list):
                continue
            try:
                levels = [(dec(r["price"]), dec(r["quantity"])) for r in data if dec(r["quantity"]) > 0]
            except (KeyError, TypeError, ValueError, ArithmeticError):
                self._error("BadLevels", channel)
                continue
            levels.sort(key=lambda x: x[0], reverse=side == "bids")
            with self.lock:
                self.sides[(sym.upper(), side)] = (tuple(levels), now)
        return replies

    def _error(self, kind: str, detail: str):
        self.last_error = {"at": self.clock().isoformat(), "kind": kind, "detail": redact(detail)[:200]}

    # ------------------------------------------------------------------ reads
    def book(self, symbol: str, max_age_s: float) -> Book | None:
        if not self.connected:
            return None
        with self.lock:
            bids, asks = self.sides.get((symbol, "bids")), self.sides.get((symbol, "asks"))
        if not bids or not asks:
            return None
        at = min(bids[1], asks[1])
        if (self.clock() - at).total_seconds() > max_age_s:
            return None
        return Book(symbol, bids[0], asks[0], at)

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            ages = {f"{s}.{side}": round((now - at).total_seconds(), 1) for (s, side), (_, at) in self.sides.items()}
        return {"url": self.url, "connected": self.connected, "symbols": list(self.symbols),
                "connected_at": self.connected_at.isoformat() if self.connected_at else None,
                "last_msg_at": self.last_msg_at.isoformat() if self.last_msg_at else None,
                "side_age_s": ages, "reconnects": self.reconnects, "last_error": self.last_error, "fatal": self.fatal, "subscribed_channels": len(self.subscribed)}

    # ------------------------------------------------------------------ connection thread
    def watch(self, symbols: list[str]):
        self.symbols = sorted({s.upper() for s in symbols})[:32]     # 2 channels each, server cap 64 per connection
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="wallex-ws", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._socket
        if ws is not None:
            try:
                ws.close(timeout=0.2)
            except (Exception,):
                pass

    def _recv(self, ws):
        if not hasattr(ws, 'recv_data'):
            return ws.recv()  # test connectors
        opcode, raw = ws.recv_data(control_frame=True)
        if opcode == 8:
            raise StreamClosed(struct.unpack('!H', raw[:2])[0] if len(raw) >= 2 else 1000)
        if opcode in (9, 10):
            return "{}" if opcode == 9 else " "
        return raw

    def _run(self):
        if self.connector is None:
            import websocket   # websocket-client; imported lazily so REST-only use never needs it
            self.connector = websocket.create_connection
        backoff = self.backoff_initial
        opts = {"sockopt": self.sockopt} if self.sockopt else {}
        if self.proxy:
            u = urlparse(self.proxy)
            opts = {**opts, "http_proxy_host": u.hostname, "http_proxy_port": u.port, "proxy_type": u.scheme or "http"}
            if u.username:
                opts["http_proxy_auth"] = (u.username, u.password or "")
        while not self._stop.is_set():
            ws = None
            try:
                ws = self.connector(self.url, timeout=self.connect_timeout, **opts)
                self._socket = ws
                ws.settimeout(40)              # server pings every 25 s; silence beyond this means a dead link
                for f in self.subscribe_frames():
                    ws.send(f)
                self.connected, self.connected_at, backoff = True, self.clock(), self.backoff_initial
                log.info("wallex websocket connected (%d symbols)", len(self.symbols))
                while not self._stop.is_set():
                    raw = self._recv(ws)
                    if not raw:
                        raise ConnectionError(f"closed by server (status {getattr(ws, 'status', None)})")
                    for reply in self.handle(raw if isinstance(raw, str) else raw.decode()):
                        ws.send(reply)
            except Exception as e:
                self._error(type(e).__name__, str(e))
                code = getattr(e, "status_code", None) or getattr(ws, "status", None)
                if isinstance(code, int) and 3500 <= code < 4000:
                    self.fatal = f"server closed with {code}; not retrying"
                    log.error("wallex websocket: %s", self.fatal)
                    self.connected = False
                    return
            finally:
                self.connected = False
                self._socket = None
                self.subscribed.clear()
                with self.lock:
                    self.sides.clear()         # updates during the gap are lost: never serve pre-gap snapshots
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                return
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(60.0, backoff * 2)
