"""Trading engine: one process, one order writer, two clocks.

Protection clock (every exit_poll_seconds): reconcile orders, mark equity, enforce
breakers, manage exits and liquidation. It never waits for the AI.
Decision clock (each closed signal bar): build the frozen snapshot, run the
selector in a worker thread, then apply the risk gate on a fresh book and submit.
Shadow and live differ only in the venue object.

Capital scope: equity, sizing, daily loss and drawdown are computed on the bot's own
allocation (safety.compute_allocation), never on the whole account. Other assets in
the account are reported but never sold or modified. Every BUY passes the executor
guard (_pre_submit): kill switch, live arm, unresolved orders, duplicates, position
count, breakers and the allocation budget, re-checked against a fresh balance read.
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .ai_agent import BaselineSelector, build_context
from .account import read_account_snapshot
from .buffered import BookUnavailable
from .config import Settings, Secrets, redact
from .executor import ACTIVE, Executor
from .models import Balance, Book, Market, ZERO, dec, floor_step
from .llm import credentials_available, effective_provider
from .markets import DISABLED_MARKETS
from .risk import OpenRisk, PortfolioState, breaker_status, competition_phase, evaluate_entry, exit_signal
from .safety import KillSwitch, check_buy, compute_allocation, foreign_buy_reserved
from .store import Store
from .strategy import analyze, apply_edge_gate, apply_market_filter, strategy_version, trail_stop

log = logging.getLogger(__name__)
D = Decimal
TEHRAN = ZoneInfo("Asia/Tehran")


def hold_reason(a) -> str:
    """Why a market produced no entry candidate this bar (from the analysis features)."""
    f = a.features or {}
    if a.regime == "INSUFFICIENT_DATA":
        return "insufficient data" + (" (stale last signal bar)" if "stale_last_bar" in f else
                                      " (stale context bar)" if "stale_context_bar" in f else
                                      " (gaps or duplicate timestamps in required candle window)" if f.get('candle_gaps') else "")
    if f.get("market_filter") == "blocked":
        return f"{a.regime}, blocked by market filter (reference market not TREND_UP)"
    if f.get("edge_gate") == "blocked":
        return f"{a.regime}, blocked by strict edge gate (no validated positive edge)"
    if a.candidates:
        return f"{a.regime}, {len(a.candidates)} candidate(s)"
    missing = [f"{family}: {', '.join(k for k, ok in checks.items() if not ok)}"
               for family, checks in f.get('setup_checks', {}).items() if not all(checks.values())]
    return f"{a.regime}, no setup" + (" (" + "; ".join(missing) + ")" if missing else "")


class Alerter:
    def __init__(self, store: Store, webhook: str | None):
        self.store, self.webhook, self.sent = store, webhook, {}

    def __call__(self, message: str, detail: dict | None = None):
        log.critical("ALERT %s %s", message, detail or "")
        self.store.event("alert", {"message": message, **(detail or {})})
        if not self.webhook or time.time() - self.sent.get(message, 0) < 600:
            return
        self.sent[message] = time.time()
        if getattr(self, 'delivery', None) is None or self.delivery.done():
            if not hasattr(self, 'pool'):
                self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='alerts')
            self.delivery = self.pool.submit(self._deliver, message, detail)

    def _deliver(self, message, detail):
        try:
            httpx.post(self.webhook, json={"text": redact(f"[wtrader] {message} {json.dumps(detail or {}, default=str)}")}, timeout=3)
        except Exception:
            log.warning("alert webhook failed")


class Engine:
    def __init__(self, settings: Settings, store: Store, market_data, venue, selector, manifest: dict,
                 secrets: Secrets | None = None, clock=None):
        if settings.mode != 'live' and venue.label == 'live':
            raise ValueError("Shadow/backtest engines cannot use a real execution venue")
        self.s, self.store, self.md, self.venue, self.selector = settings, store, market_data, venue, selector
        self.manifest = manifest
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.alert = Alerter(store, secrets.alert_webhook if secrets else None)
        self.executor = Executor(store, venue, self._on_fill, settings.execution.unknown_reconcile_seconds, self.alert,
                                 guard=self._pre_submit)
        self.kill = KillSwitch(settings.execution.kill_switch_path)
        self.live_arm_check = None          # set by the CLI in live mode: () -> list of failures
        self.baseline = BaselineSelector()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai")
        self.data_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decision-data")
        self.account_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-display")
        self.data_pending = None
        self.account_pending = None
        self._decision_reader = None
        self._account_reader = None
        self.abandoned_ai = None
        self.selector_started = None
        self._tick_lock = threading.Lock()
        self.last_cycle_duration_s = None
        self.pending: tuple[Future, dict, list, str] | None = None
        self.markets: dict[str, Market] = {}
        self.books: dict[str, Book] = {}
        self.fees: dict[str, Decimal] = {}
        self.edge = self._load_edge()
        self.counters = {"api_failures": 0, "ai_failures": 0, "tick_errors": 0, "ticks": 0, "orders_blocked": 0}
        self.last_tick_at = None
        self._buffer_failures = 0
        self.balances_cache: dict[str, Balance] = {}
        self.last_account_ok: datetime | None = None
        self.last_md_ok: datetime | None = None
        self.last_error: dict | None = None
        self.foreign_reserved = ZERO
        self.foreign_orders: list[dict] = []
        self.allocation = None
        self.account_reads = 0
        self.account_client = None          # set by the CLI when a Wallex key exists: read-only account sync in shadow
        self.exchange_account: dict | None = None
        self._hb = 0
        self.final_written = store.get_kv("final_report_written", False)
        self.strategy_version = strategy_version(settings.strategy.enabled_families, settings.market.signal_seconds,
                                                 settings.market.context_seconds)
        Path(settings.evidence_dir, "snapshots").mkdir(parents=True, exist_ok=True)

    # ================================================================ setup
    def _load_edge(self) -> dict:
        p = self.s.strategy.edge_artifact
        if p and Path(p).exists():
            return json.loads(Path(p).read_text()).get("families", {})
        return {}

    def bootstrap(self):
        all_markets = self.md.markets()
        allow = self.s.market.account_allowlist
        universe = [s for s in self.s.market.candidate_symbols if s in all_markets and (not allow or s in allow)]
        flt = self.s.strategy.market_filter_symbol
        self.markets = {s: all_markets[s] for s in set(universe) | ({flt} if flt in all_markets else set())}
        self.tradable = set(universe)
        default_fee = D(self.s.execution.taker_fee_fraction)
        fetched = {}
        if hasattr(self.venue, "client"):
            try:
                fetched = self.venue.client.taker_fees()
            except Exception as e:
                self.alert("could not fetch account fees; using configured default", {"error": type(e).__name__})
        self.fees = {s: max(fetched.get(m.raw_symbol, default_fee), ZERO) for s, m in self.markets.items()}
        self.store.event("bootstrap", {"universe": sorted(self.tradable), "filter": flt, "fees": self.fees,
                                       "data_source": getattr(self.md, "label", "?"), "venue": self.venue.label})
        if hasattr(self.md, "watch"):
            self.md.watch(sorted(self.markets))       # WebSocket depth for every market the engine reads
        self.last_md_ok = None  # metadata does not establish a current executable price
        # restart recovery: venue is the source of truth for every order we know about
        self.executor.reconcile_all()
        self.reconcile_exchange()
        bal = self._balances(refresh=True)
        if self.store.get_kv("external_baseline") is None and not self.store.positions():
            # assets present before the bot ever traded belong to someone else: never sold, never counted
            self.store.set_kv("external_baseline", {c: str(b.total) for c, b in bal.items() if c != "TMN" and b.total > 0})
        for p in self.store.positions():
            m = self.markets.get(p["symbol"])
            b = bal.get(m.base) if m else None
            if m and dec(p["qty"]) > (b.total if b else ZERO):
                self.alert("position larger than exchange balance after restart; exits limited to what exists",
                           {"position_id": p["position_id"], "symbol": p["symbol"], "qty": p["qty"],
                            "exchange_total": str(b.total if b else ZERO)})
        self.store.event("recovered", {"open_positions": len(self.store.positions()),
                                       "active_orders": len(self.store.orders(ACTIVE)),
                                       "foreign_open_orders": len(self.foreign_orders)})

    # ================================================================ fills
    def _meta(self, pid):
        return self.store.get_kv(f"pos_meta:{pid}", {})

    def _on_fill(self, order: dict, dq, dquote, dfb, dfq):
        pid = order["position_id"]
        if not pid:
            self.store.event("orphan_fill", {"client_id": order["client_id"], "qty": str(dq)})
            return
        p = self.store.position(pid)
        meta = self._meta(pid)
        qty, cost, proceeds = dec(p["qty"]), dec(p["cost_tmn"]), dec(p["proceeds_tmn"])
        px = dquote / dq if dq > 0 else dec(p["entry_price"] or "0")
        fees = dec(p["fees_tmn"]) + dfq + dfb * px
        if order["side"] == "BUY":
            qty += dq - dfb
            cost += dquote + dfq
            entry = cost / qty if qty > 0 else ZERO
            stop = dec(meta.get("initial_stop", p["stop"]))
            gross_qty = dec(order["applied_qty"]) + dq
            gross_quote = dec(order["applied_quote"]) + dquote
            gross_entry = gross_quote / gross_qty if gross_qty > 0 else entry
            target = gross_entry + (gross_entry - stop) * dec(meta.get("target_r", "2"))
            meta["peak"] = str(max(dec(meta.get("peak", "0")), entry))
            self.store.update_position(pid, qty=qty, cost_tmn=cost, entry_price=entry, target=target, status="OPEN",
                                       opened_at=p["opened_at"] or self.clock().isoformat(), fees_tmn=fees)
            self.store.set_kv(f"pos_meta:{pid}", meta)
        else:
            qty -= dq + dfb
            proceeds += dquote - dfq
            self.store.update_position(pid, qty=qty, proceeds_tmn=proceeds, fees_tmn=fees)
        self.store.event("fill", {"position_id": pid, "client_id": order["client_id"], "side": order["side"],
                                  "qty": str(dq), "quote": str(dquote), "fee_base": str(dfb), "fee_quote": str(dfq)})

    # ================================================================ state
    def _book(self, symbol: str) -> Book | None:
        try:
            b = self.md.book(symbol)
            self.books[symbol] = b
            now = self.clock()
            if 0 <= b.age_seconds(now) <= self.s.market.max_book_age_seconds and b.spread_bps() is not None:
                self.last_md_ok = b.received_at
            return b
        except BookUnavailable:
            return self.books.get(symbol)
        except Exception as e:
            self.counters["api_failures"] += 1
            self.last_error = {"at": self.clock().isoformat(), "error": f"book {symbol}: {type(e).__name__}"}
            log.warning("book %s failed: %s", symbol, type(e).__name__)
            return self.books.get(symbol)

    def _balances(self, refresh: bool = True) -> dict[str, Balance]:
        """Exchange balances; on an API failure the last good read is reused and entries halt once it is stale."""
        if refresh:
            try:
                self.balances_cache = self.venue.balances()
                self.last_account_ok = self.clock()
                self.account_reads += 1
            except Exception as e:
                self.counters["api_failures"] += 1
                self.last_error = {"at": self.clock().isoformat(), "error": f"balances: {type(e).__name__}"}
                log.warning("balances failed: %s", type(e).__name__)
        return self.balances_cache

    def sync_exchange_account(self, now: datetime):
        """Real Wallex balances for display, kept apart from the bot allocation. In shadow this is a read-only
        GET (the paper venue still trades simulated balances); in live it mirrors the venue's own reads."""
        if self.account_client is None:
            return
        prev = self.exchange_account or {}
        if self.venue.label == "live":
            bal, at, err = self.balances_cache, self.last_account_ok, None
        else:
            try:
                bal, at, err = self.account_client.balances(), now, None
            except Exception as e:
                bal, at, err = None, None, f"{type(e).__name__}: {redact(str(e))[:160]}"
        rows = prev.get("balances", {}) if bal is None else {
            c: {"free": str(b.free), "locked": str(b.locked), "total": str(b.total)} for c, b in sorted(bal.items()) if b.total > 0}
        self.exchange_account = {"source": "wallex", "read_only": self.venue.label != "live",
                                 "endpoint": "/v1/account/balances-detail" if getattr(self.account_client, "_sub", None)
                                 else "/v1/account/balances",
                                 "synced_at": at.isoformat() if at else prev.get("synced_at"), "error": err, "balances": rows}

    def _sync_account_async(self, now):
        if self.account_client is None:
            return
        if self.account_pending is not None:
            if not self.account_pending.done():
                return
            try:
                self.exchange_account = self.account_pending.result()
            except Exception as e:
                self.exchange_account = {**(self.exchange_account or {}), "error": type(e).__name__}
            self.account_pending = None
        if self.counters['ticks'] % max(1, self.s.execution.exchange_reconcile_every_ticks):
            return
        if self.venue.label == "live":
            self.sync_exchange_account(now)
            return
        self.account_pending = self.account_pool.submit(self._read_display_account)

    def _read_display_account(self):
        if self._account_reader is None:
            self._account_reader = self.account_client.fork_reader() if hasattr(self.account_client, 'fork_reader') else self.account_client
        snapshot = read_account_snapshot(self._account_reader, self.s.market.candidate_symbols,
                                         previous=getattr(self, '_display_account_snapshot', None), clock=self.clock)
        self._display_account_snapshot = snapshot   # only the account worker writes this cache
        return snapshot

    def _age(self, t: datetime | None, now: datetime) -> float:
        return float("inf") if t is None else (now - t).total_seconds()

    def reconcile_exchange(self):
        """Compare the exchange's open orders with our records. Unknown ones are reserved and halt entries."""
        if not hasattr(self.venue, "open_orders"):
            return
        try:
            remote = self.venue.open_orders()
        except Exception as e:
            self.counters["api_failures"] += 1
            log.warning("openOrders failed: %s", type(e).__name__)
            return
        known = {o["client_id"]: o for o in self.store.orders()}
        for r in remote:       # an order we recorded as finished is still open on the exchange: re-observe it
            o = known.get((r.client_id or "").upper())
            if o and o["state"] in ("FILLED", "CANCELED", "REJECTED"):
                self.executor.reconcile(o)
        reserved, rows = foreign_buy_reserved(remote, set(known))
        if rows and not self.foreign_orders:
            self.alert("exchange has open orders the bot did not create; they are reserved and never touched",
                       {"count": len(rows)})
            self.store.event("foreign_orders", {"orders": rows})
        self.foreign_reserved, self.foreign_orders = reserved, rows

    def portfolio_state(self, now: datetime, refresh: bool = True) -> tuple[PortfolioState, dict]:
        bal = self._balances(refresh)
        tmn = bal.get("TMN")
        free_tmn = tmn.free if tmn else ZERO
        default_fee = D(self.s.execution.taker_fee_fraction)
        bids = {s: b.best_bid for s, b in self.books.items() if b.best_bid}
        positions = self.store.positions()
        alloc = compute_allocation(risk=self.s.risk, positions=positions, orders=self.store.orders(ACTIVE),
                                   closed=self.store.closed_positions(1_000_000), bids=bids, fees=self.fees,
                                   default_fee=default_fee, foreign_reserved=self.foreign_reserved,
                                   exchange_free_tmn=free_tmn)
        self.allocation = alloc
        equity = alloc.bot_equity_tmn
        # whole-account view, for reporting only (includes assets the bot does not own)
        account_value, unvalued = (tmn.total if tmn else ZERO), []
        holdings = {}
        by_base = {m.base: s for s, m in self.markets.items()}
        for cur, b in bal.items():
            if cur == "TMN" or b.total <= 0:
                continue
            book = self.books.get(by_base.get(cur, ""))
            bid = book.best_bid if book else None
            value = b.total * bid if bid else None
            holdings[cur] = {"qty": str(b.total), "free": str(b.free), "locked": str(b.locked), "bid": str(bid) if bid else None,
                             "value_tmn": f"{value:.0f}" if value is not None else None}
            if value is None:
                unvalued.append(cur)
            else:
                account_value += value
        exposures = []
        for p in positions:
            book = self.books.get(p["symbol"])
            bid = book.best_bid if book and book.best_bid else dec(p["entry_price"] or "0")
            qty, stop = dec(p["qty"]), dec(p["stop"])
            pending_qty = sum((dec(o["quantity"]) - dec(o["executed_qty"]) for o in self.executor.active_for_position(p["position_id"])
                               if o["side"] == "BUY"), ZERO)
            notional = (qty + pending_qty) * bid
            fee = self.fees.get(p['symbol'], default_fee)
            pending_cost = sum((max(ZERO, dec(o['quantity']) - dec(o['executed_qty'])) * dec(o['limit_price']) * (1 + fee)
                                for o in self.executor.active_for_position(p['position_id']) if o['side'] == 'BUY'), ZERO)
            remaining_cost = qty * dec(p['entry_price'] or '0') + pending_cost
            stop_proceeds = (qty + pending_qty * (1 - fee)) * stop * (1 - fee) * (1 - D(self.s.risk.expected_exit_slippage_bps) / D(10000))
            exposures.append(OpenRisk(p["symbol"], notional, max(ZERO, remaining_cost - stop_proceeds)))
        day = now.astimezone(TEHRAN).date().isoformat()
        if self.store.get_kv("day") != day:
            self.store.set_kv("day", day)
            self.store.set_kv("bot_day_start_equity", str(equity))
        hwm = max(dec(self.store.get_kv("bot_high_water", "0")), equity)
        self.store.set_kv("bot_high_water", str(hwm))
        if self.store.get_kv("bot_initial_equity") is None:
            self.store.set_kv("bot_initial_equity", str(equity))
            self.store.set_kv("started_at", now.isoformat())
        halted, reason = False, None
        ks = self.kill.status()
        if ks:
            halted, reason = True, f"kill switch ({ks['level']})"
        elif self.store.get_kv("breaker_latched"):
            halted, reason = True, "drawdown breaker latched"
        elif self.store.get_kv("daily_halt") == day:
            halted, reason = True, "daily loss limit"
        elif self.executor.unresolved():
            halted, reason = True, "unresolved order outcome"
        elif self.foreign_orders and self.s.risk.halt_on_foreign_orders:
            halted, reason = True, "foreign open orders on the exchange"
        elif any(dec(p['qty']) > 0 and (p['symbol'] not in self.books or not 0 <= self.books[p['symbol']].age_seconds(now) <= self.s.market.max_book_age_seconds)
                 for p in positions):
            halted, reason = True, "position price stale (protection degraded)"
        elif self._age(self.last_account_ok, now) > self.s.market.max_account_data_age_seconds:
            halted, reason = True, "account data stale (API outage?)"
        elif self._age(self.last_md_ok, now) > self.s.market.max_market_data_age_seconds:
            halted, reason = True, "market data stale"
        cooldown = frozenset(s for s, until in (self.store.get_kv("cooldown", {}) or {}).items()
                             if datetime.fromisoformat(until) > now)
        st = PortfolioState(equity, free_tmn, tuple(exposures), dec(self.store.get_kv("bot_day_start_equity", str(equity))),
                            hwm, halted, reason, cooldown, budget_tmn=alloc.budget_tmn, committed_tmn=alloc.committed_tmn)
        return st, {"holdings": holdings, "free_tmn": str(free_tmn), "account_value_tmn": f"{account_value:.0f}",
                    "account_unvalued": unvalued, "allocation": alloc.as_dict()}

    # ================================================================ execution guard
    def _pre_submit(self, *, side: str, symbol: str, quantity: Decimal, limit_price: Decimal, purpose: str,
                    position_id: str | None) -> str | None:
        """Last check before any order leaves the process. Returns a refusal reason or None."""
        if self.venue.label == 'live' and (self.s.experimental_shadow or self.s.strategy.edge_gate != 'strict'
                                          or not self.s.execution.live_rollout_approved):
            return "live execution is blocked by audit/configuration safety hold"
        k = self.kill.blocks(side)
        if k:
            return k
        if side == "SELL":
            position = self.store.position(position_id) if position_id else None
            if position is None or position['symbol'] != symbol or quantity <= 0 or quantity > dec(position['qty']):
                return "SELL requires an attributed bot position and cannot exceed its owned quantity"
            if any(o['side'] == 'SELL' for o in self.executor.active_for_position(position_id)):
                return "duplicate protective SELL already active"
            return None
        if side != "BUY":
            return "unsupported order side"
        if symbol in DISABLED_MARKETS:
            return "market excluded by develop hackathon deny-only reference"
        if self.live_arm_check is not None:
            fails = self.live_arm_check()
            if fails:
                return "live arm invalid: " + "; ".join(fails)
        if self.executor.unresolved():
            return "unresolved order outcome"
        halt_day = self.store.get_kv("daily_halt")
        if self.store.get_kv("breaker_latched") or (halt_day and halt_day == self.store.get_kv("day")):
            return "loss breaker active"
        if self.foreign_orders and self.s.risk.halt_on_foreign_orders:
            return "foreign open orders on the exchange"
        active = self.store.orders(ACTIVE)
        if any(o["side"] == "BUY" and o["symbol"] == symbol for o in active):
            return f"duplicate: an active BUY for {symbol} already exists"
        others = [p for p in self.store.positions() if p["position_id"] != position_id]
        if len(others) >= self.s.risk.max_positions:
            return f"max positions {self.s.risk.max_positions} reached"
        book = self._book(symbol)
        if book is None or not 0 <= book.age_seconds(self.clock()) <= self.s.market.max_book_age_seconds or book.spread_bps() is None:
            return "fresh executable book unavailable"
        before = self.account_reads
        self._balances(refresh=True)
        if self.account_reads == before:
            return "fresh balance read failed"
        current, _ = self.portfolio_state(self.clock(), refresh=False)
        br = breaker_status(current, self.s.risk)
        if current.entries_halted or br.daily_loss_hit or br.drawdown_hit:
            return current.halt_reason or "loss breaker active"
        if side == "BUY" and any(p["symbol"] == symbol for p in others):
            return "duplicate position symbol"
        fee = self.fees.get(symbol, D(self.s.execution.taker_fee_fraction))
        return check_buy(self.allocation, quantity * limit_price * (1 + fee), self.s.risk)

    # ================================================================ protection clock
    def protect(self, now: datetime, phase: str):
        self.executor.reconcile_all()
        if self.counters["ticks"] % max(1, self.s.execution.exchange_reconcile_every_ticks) == 0:
            self.reconcile_exchange()
        self._sync_account_async(now)
        syms = {p["symbol"] for p in self.store.positions()} | {o["symbol"] for o in self.store.orders(ACTIVE)}
        universe = sorted(getattr(self, "tradable", set()))
        if universe and getattr(getattr(self.md, "stream", None), "connected", False):
            syms.update(universe)       # WebSocket books are in memory: refresh every market (REST only fills gaps)
        elif universe:    # heartbeat: one extra market per tick keeps prices and the data-freshness clock current
            syms.add(universe[self._hb % len(universe)])
            self._hb += 1
        if hasattr(self.md, "refresh"):
            protected = {p["symbol"] for p in self.store.positions()} | {o["symbol"] for o in self.store.orders(ACTIVE)}
            self.md.refresh(sorted(syms), protected=protected)
            failures = getattr(self.md, 'failures', 0)
            self.counters['api_failures'] += max(0, failures - self._buffer_failures)
            self._buffer_failures = failures
        for s in sorted(syms):
            self._book(s)
        now = self.clock()  # reads may finish after the tick timestamp
        self.venue.poll({s: self.books[s] for s in syms if s in self.books
                         and 0 <= self.books[s].age_seconds(now) <= self.s.market.max_book_age_seconds})
        ks = self.kill.status()
        if ks:
            if self.store.get_kv("kill_seen") != ks.get("at"):
                self.store.set_kv("kill_seen", ks.get("at"))
                self.alert(f"KILL SWITCH engaged ({ks['level']}): no new entries", ks)
            for o in self.store.orders(ACTIVE):
                if o["side"] == "BUY":
                    self.executor.cancel(o["client_id"])
        state, detail = self.portfolio_state(now)
        br = breaker_status(state, self.s.risk)
        if br.drawdown_hit and not self.store.get_kv("breaker_latched"):
            self.store.set_kv("breaker_latched", True)
            self.alert("DRAWDOWN BREAKER: flattening and halting entries", {"drawdown": str(br.drawdown_fraction)})
        if br.daily_loss_hit and self.store.get_kv("daily_halt") != self.store.get_kv("day"):
            self.store.set_kv("daily_halt", self.store.get_kv("day"))
            self.alert("daily loss limit hit: entries halted for the rest of the Tehran day",
                       {"daily_loss": str(br.daily_loss_fraction)})
        level = ks["level"] if ks else None
        if level != "freeze":     # freeze: no orders at all; existing exit orders are left untouched
            for p in self.store.positions():
                self._manage_position(p, now, phase, bool(self.store.get_kv("breaker_latched")), flatten=level == "flatten")
            if phase in ("LIQUIDATE", "ENDED"):
                self._sweep_residuals(now)
        self.store.record_equity(state.equity_tmn, state.free_tmn, detail)
        self.last_detail = detail
        return state, br

    def _manage_position(self, p: dict, now: datetime, phase: str, breaker: bool, flatten: bool = False):
        pid, sym = p["position_id"], p["symbol"]
        market = self.markets.get(sym)
        meta = self._meta(pid)
        active = self.executor.active_for_position(pid)
        entries = [o for o in active if o["side"] == "BUY"]
        exits = [o for o in active if o["side"] == "SELL"]
        cfg = self.s.execution
        for o in entries:   # stale or no-longer-wanted entry remainder
            age = (now - datetime.fromisoformat(o["created_at"])).total_seconds()
            unwanted = phase not in ("TRADING", "UNSCHEDULED") or breaker or self.kill.status() is not None
            if o["state"] in ("OPEN", "PARTIALLY_FILLED") and (age > cfg.entry_cancel_after_seconds or unwanted):
                self.executor.cancel(o["client_id"])
        qty = dec(p["qty"])
        if p["status"] == "PENDING":
            if not active and qty <= 0:
                self.store.update_position(pid, status="CANCELED", closed_at=now.isoformat(), exit_reason="ENTRY_UNFILLED")
            return
        book = self.books.get(sym)
        bid = book.best_bid if book else None
        if book is None or not 0 <= book.age_seconds(now) <= self.s.market.max_book_age_seconds or book.spread_bps() is None:
            self.alert("protection degraded: stale or missing book for open position", {"symbol": sym})
            return  # no stale-price stop/target evaluation, trailing, liquidation or simulated fills
        if bid:
            peak = max(dec(meta.get("peak", "0")), bid)
            new_stop = trail_stop(dec(p["stop"]), peak, dec(meta["trail_distance"]) if meta.get("trail_distance") else None)
            if peak != dec(meta.get("peak", "0")):
                meta["peak"] = str(peak)
                self.store.set_kv(f"pos_meta:{pid}", meta)
            if new_stop != dec(p["stop"]):
                self.store.update_position(pid, stop=new_stop)
                p["stop"] = str(new_stop)
        # done: nothing sellable remains (remaining dust is recorded, never re-sold for the original size)
        dust = market is not None and (qty <= 0 or qty < market.min_quantity or (bid is not None and qty * bid < market.min_notional))
        if not active and dust:
            pnl = dec(p["proceeds_tmn"]) - dec(p["cost_tmn"])
            reason = meta.get("exit_reason") or "DUST_ENTRY"
            self.store.update_position(pid, status="CLOSED", closed_at=now.isoformat(), realized_pnl_tmn=pnl, exit_reason=reason)
            self.store.event("position_closed", {"position_id": pid, "pnl_tmn": str(pnl), "dust_qty": str(qty), "reason": reason})
            if reason == "STOP":
                cd = self.store.get_kv("cooldown", {}) or {}
                cd[sym] = datetime.fromtimestamp(now.timestamp() + self.s.risk.cooldown_bars_after_stop * self.s.market.signal_seconds, timezone.utc).isoformat()
                self.store.set_kv("cooldown", cd)
            return
        for o in exits:     # re-price unfilled exits
            age = (now - datetime.fromisoformat(o["created_at"])).total_seconds()
            if o["state"] in ("OPEN", "PARTIALLY_FILLED") and age > 15:
                self.executor.cancel(o["client_id"])
        if exits or entries or market is None or qty <= 0:
            return
        bars = int((now - datetime.fromisoformat(p["opened_at"])).total_seconds() // p["signal_seconds"]) if p["opened_at"] else 0
        sig = meta.get("exit_reason") and p["status"] == "CLOSING"
        es = exit_signal(bid=bid, stop=dec(p["stop"]), target=dec(p["target"] or "1e30"), bars_held=bars,
                         max_bars=p["max_bars"], phase=phase, drawdown_hit=breaker)
        if not es and not sig and not flatten:
            return
        reason = es.reason if es else (meta.get("exit_reason") or "KILL_FLATTEN")
        if flatten and reason not in ("LIQUIDATION", "BREAKER", "STOP"):
            reason = "KILL_FLATTEN"
        if bid is None:
            return
        attempt = int(meta.get("exit_attempt", 0))
        steps = [D(x) for x in cfg.liquidation_slippage_steps_bps]
        slip = steps[min(attempt, len(steps) - 1)] if reason in ("LIQUIDATION", "BREAKER", "STOP", "KILL_FLATTEN") else D(cfg.exit_limit_slippage_bps)
        free_base = self._balances(refresh=False).get(market.base)
        sell_qty = floor_step(min(qty, free_base.free if free_base else ZERO), market.step)
        if sell_qty <= 0 or sell_qty < market.min_quantity:
            return
        limit = floor_step(bid * (1 - slip / D(10000)), market.tick)
        meta.update(exit_reason=reason, exit_attempt=attempt + 1)
        self.store.set_kv(f"pos_meta:{pid}", meta)
        self.store.update_position(pid, status="CLOSING")
        self.store.event("exit_signal", {"position_id": pid, "reason": reason, "bid": str(bid), "stop": p["stop"],
                                         "target": p["target"], "bars": bars, "attempt": attempt})
        self.executor.place(economic_key=f"EXIT:{pid}:{attempt}", symbol=sym, side="SELL", quantity=sell_qty,
                            limit_price=limit, purpose=reason, market=market, position_id=pid)

    def _sweep_residuals(self, now: datetime):
        """Dust is conservatively written off in its closed position and retained as evidence.

        Unattributed sales cannot be reconciled to position P&L. Never infer ownership
        from an account deposit or place an orphan liquidation order. Sellable bot
        positions are exited by _manage_position; below-minimum dust remains disclosed.
        """
        dust = {p['position_id']: {'symbol': p['symbol'], 'qty': p['qty']}
                for p in self.store.closed_positions(1_000_000) if dec(p['qty']) > 0}
        if dust != self.store.get_kv('retained_dust', {}):
            self.store.set_kv('retained_dust', dust)
            self.store.event('retained_dust', {'positions': dust, 'note': 'written off conservatively; no unassigned sale'})

    # ================================================================ decision clock
    def maybe_decide(self, now: datetime, phase: str, state: PortfolioState):
        sig_s = self.s.market.signal_seconds
        boundary = int(now.timestamp()) // sig_s * sig_s
        if self.abandoned_ai is not None and self.abandoned_ai.done():
            did = getattr(self, '_abandoned_decision_id', None)
            if did:
                try:
                    late = self.abandoned_ai.result()
                except Exception:
                    late = None  # expiration already persisted; never use the late choice
                if late is not None:
                    self.store.update_decision(did, decision_json={'reason': 'selector deadline exceeded; late result discarded',
                        'usage': late.usage, 'provider': late.provider, 'model': late.model}, final_action='EXPIRED')
            self.abandoned_ai = None
        if self.pending and ((self.selector_started is not None and time.monotonic() - self.selector_started > self.s.ai.deadline_seconds) or now.timestamp() - self.pending[1]['as_of_utc'] > self.s.strategy.candidate_ttl_seconds + self.s.ai.deadline_seconds):
            fut, ctx, analyses, did = self.pending
            self.store.update_decision(did, action="HOLD", source="deadline", final_action="EXPIRED",
                                       decision_json={"reason": "selector deadline exceeded; no entry allowed"})
            self.counters["ai_failures"] += 1
            self.abandoned_ai, self.pending = fut, None
            self._abandoned_decision_id = did
        if self.pending and self.pending[0].done():
            fut, ctx, analyses, did = self.pending
            self.pending = None
            self._finish_decision(fut, ctx, analyses, did, phase)
        if self.data_pending and self.data_pending[0].done():
            fut, as_of = self.data_pending
            self.data_pending = None
            fetched = fut.result()
            if as_of == boundary:
                self._start_decision(as_of, now, phase, state, fetched=fetched)
            else:
                self.store.event("decision_data_expired", {"as_of": as_of, "current_bar": boundary})
        if self.data_pending is None and self.pending is None and self.abandoned_ai is None and boundary > int(self.store.get_kv("last_decision_bar", 0)) and now.timestamp() >= boundary + 8:
            self.store.set_kv("last_decision_bar", boundary)
            self.data_pending = (self.data_pool.submit(self._fetch_analyses, boundary), boundary)

    def _fetch_analyses(self, as_of):
        # No Store or venue access on this worker. One request batch may be outstanding at a time.
        if self._decision_reader is None:
            self._decision_reader = self.md.fork_reader() if hasattr(self.md, "fork_reader") else self.md
        reader = self._decision_reader
        mc, sc = self.s.market, self.s.strategy
        analyses, data_failed = [], {}
        for sym in sorted(self.markets):
            try:
                sig = reader.candles(sym, mc.signal_seconds, as_of - 230 * mc.signal_seconds, as_of)
                ctx = reader.candles(sym, mc.context_seconds, as_of - 110 * mc.context_seconds, as_of)
            except Exception as e:
                data_failed[sym] = type(e).__name__
                log.warning("candles %s failed: %s", sym, type(e).__name__)
                continue
            a = analyze(sym, sig, ctx, as_of, sc, mc.signal_seconds, mc.context_seconds, mc.warmup_bars)
            if sym not in self.tradable:
                a = type(a)(a.symbol, a.as_of, a.regime, a.features, ())    # reference market: context only
            analyses.append(a)
        analyses = apply_edge_gate(apply_market_filter(analyses, sc.market_filter_symbol), self.edge, sc.edge_gate)
        return analyses, data_failed

    def _start_decision(self, as_of: int, now: datetime, phase: str, state: PortfolioState, fetched=None):
        analyses, data_failed = fetched if fetched is not None else self._fetch_analyses(as_of)
        self.counters["api_failures"] += len(data_failed)
        if data_failed:
            self.last_error = {"at": now.isoformat(), "error": f"candle data unavailable: {data_failed}"}
        end = self.s.competition.end_dt()
        minutes = None if end is None else round(((end - now).total_seconds() / 60) - self.s.competition.halt_entries_before_end_minutes)
        feedback = self._feedback()
        br = breaker_status(state, self.s.risk)
        portfolio = {"equity_tmn": f"{state.equity_tmn:.0f}", "free_tmn": f"{state.free_tmn:.0f}",
                     "open_positions": [{"symbol": p["symbol"], "family": p["family"]} for p in self.store.positions()],
                     "drawdown_fraction": f"{br.drawdown_fraction:.4f}", "daily_loss_fraction": f"{br.daily_loss_fraction:.4f}",
                     "entries_halted": state.entries_halted}
        ctx = build_context(as_of=as_of, analyses=analyses, portfolio=portfolio, edge=self.edge, feedback=feedback,
                            minutes_to_entry_cutoff=minutes)
        snap_path = Path(self.s.evidence_dir, "snapshots", f"{ctx['snapshot_id']}.json")
        snap_path.write_text(json.dumps(ctx, default=str))
        if not ctx["candidates"] or phase not in ("TRADING", "UNSCHEDULED") or state.entries_halted:
            # category separates connectivity problems from strategy outcomes on the dashboard
            if data_failed and len(data_failed) == len(self.markets):
                category = "MARKET_DATA_UNAVAILABLE"
                why = f"market data unavailable for all {len(data_failed)} markets ({', '.join(sorted(set(data_failed.values())))})"
            elif ctx["candidates"] and state.entries_halted:
                category, why = "ENTRIES_HALTED", f"{len(ctx['candidates'])} candidate(s) but entries halted: {state.halt_reason}"
            elif ctx["candidates"]:
                category, why = "PHASE", f"{len(ctx['candidates'])} candidate(s) but phase {phase} allows no entries"
            else:
                category = "NO_SETUP"
                why = "no candidates: " + "; ".join(f"{a.symbol} {hold_reason(a)}" for a in analyses if a.symbol in self.tradable)
                if data_failed:
                    category = "NO_SETUP_PARTIAL_DATA"
                    why += f"; no data: {', '.join(sorted(data_failed))}"
                if state.entries_halted:
                    why += f" · entries also halted: {state.halt_reason}"
            markets = {a.symbol: hold_reason(a) for a in analyses}
            markets.update({sym: f"no market data ({err})" for sym, err in data_failed.items()})
            self.store.record_decision(snapshot_id=ctx["snapshot_id"], snapshot_ref=str(snap_path), n_candidates=len(ctx["candidates"]),
                                       action="HOLD", source="deterministic",
                                       decision={"reason": why[:600], "category": category, "data_failed": data_failed,
                                                 "data_source": getattr(self.md, "label", "?"),
                                                 "entries_halted": state.entries_halted, "halt_reason": state.halt_reason,
                                                 "markets": markets},
                                       final_action="HOLD", baseline_candidate_id=self.baseline.select(ctx).candidate_id)
            return
        did = self.store.record_decision(snapshot_id=ctx["snapshot_id"], snapshot_ref=str(snap_path),
                                         n_candidates=len(ctx["candidates"]), action="PENDING", source=self.selector.name,
                                         decision={}, final_action="PENDING",
                                         baseline_candidate_id=self.baseline.select(ctx).candidate_id)
        self.selector_started = time.monotonic()
        self.pending = (self.pool.submit(self.selector.select, ctx), ctx, analyses, did)

    def _finish_decision(self, fut: Future, ctx: dict, analyses: list, did: str, phase: str):
        now = self.clock()
        try:
            d = fut.result()
        except Exception as e:
            self.counters["ai_failures"] += 1
            self.store.update_decision(did, action="HOLD", source="error", final_action="HOLD",
                                       decision_json={"error": 'selector_failed: ' + type(e).__name__})
            return
        if d.source.startswith("fallback"):
            self.counters["ai_failures"] += 1
            self.alert("AI selector unavailable/invalid; entries blocked", {"policy": 'HOLD', "error": (d.error or "")[:120]})
        info = d.as_dict()
        if d.action != "SELECT":
            self.store.update_decision(did, action="HOLD", source=d.source, decision_json=info, final_action="HOLD")
            return
        cand = next(c for a in analyses for c in a.candidates if c.candidate_id == d.candidate_id)
        sc = self.s.strategy
        if now.timestamp() - ctx["as_of_utc"] > sc.candidate_ttl_seconds + self.s.ai.deadline_seconds:
            self.store.update_decision(did, action="SELECT", candidate_id=d.candidate_id, multiplier=d.multiplier,
                                       source=d.source, decision_json=info, final_action="EXPIRED")
            return
        book = self._book(cand.symbol)
        state, _ = self.portfolio_state(now)
        now = self.clock()
        phase = competition_phase(now, self.s.competition)
        allow = self.s.market.account_allowlist or None
        v = evaluate_entry(symbol=cand.symbol, stop=cand.stop, max_stop_fraction=D(sc.stop_distance_max_fraction),
                           min_stop_fraction=D(sc.stop_distance_min_fraction), multiplier=D(d.multiplier), state=state,
                           market=self.markets.get(cand.symbol), book=book, now=now, fee_fraction=self.fees.get(cand.symbol, D(self.s.execution.taker_fee_fraction)),
                           risk=self.s.risk, mcfg=self.s.market, allowlist=allow, phase=phase, target_r=cand.target_r)
        self.store.event("risk_verdict", {"decision_id": did, **v.as_dict()})
        if not v.approved:
            self.store.update_decision(did, action="SELECT", candidate_id=d.candidate_id, multiplier=d.multiplier,
                                       source=d.source, decision_json=info, risk_json=v.as_dict(), final_action="RISK_REJECTED")
            return
        pid = self.store.insert_position(symbol=cand.symbol, family=cand.family, candidate_id=cand.candidate_id,
                                         decision_id=did, stop=cand.stop, max_bars=cand.max_holding_bars,
                                         signal_seconds=self.s.market.signal_seconds)
        self.store.set_kv(f"pos_meta:{pid}", {"target_r": str(cand.target_r), "initial_stop": str(cand.stop),
                                              "trail_distance": str(cand.trail_distance) if cand.trail_distance else None})
        cid = self.executor.place(economic_key=f"ENTRY:{cand.candidate_id}", symbol=cand.symbol, side="BUY",
                                  quantity=v.quantity, limit_price=v.limit_price, purpose="ENTRY",
                                  market=self.markets[cand.symbol], decision_id=did, position_id=pid)
        self.store.update_decision(did, action="SELECT", candidate_id=d.candidate_id, multiplier=d.multiplier,
                                   source=d.source, decision_json=info, risk_json=v.as_dict(),
                                   final_action="ENTRY_SUBMITTED" if cid else "DUPLICATE_SUPPRESSED", order_client_id=cid)

    def _feedback(self) -> dict:
        closed = list(reversed(self.store.closed_positions(self.s.ai.feedback_trades)))
        by: dict = {}
        for p in closed:
            f = by.setdefault(p["family"], {"trades": 0, "wins": 0, "pnl_tmn": 0.0})
            pnl = float(p["realized_pnl_tmn"] or 0)
            f["trades"] += 1
            f["wins"] += pnl > 0
            f["pnl_tmn"] = round(f["pnl_tmn"] + pnl)
        return {"window_trades": len(closed), "by_family": by,
                "last_exits": [{"symbol": p["symbol"], "exit": p["exit_reason"], "pnl_tmn": round(float(p["realized_pnl_tmn"] or 0))} for p in closed[-5:]]}

    # ================================================================ loop
    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return False
        started = time.monotonic()
        try:
            self._tick()
        finally:
            self.last_cycle_duration_s = time.monotonic() - started
            self._tick_lock.release()
        return True

    def _tick(self):
        now = self.clock()
        phase = competition_phase(now, self.s.competition)
        state, br = self.protect(now, phase)
        if phase in ("TRADING", "UNSCHEDULED", "NO_NEW_ENTRIES"):
            self.maybe_decide(now, phase, state)
        if phase == "ENDED" and not self.final_written and not self.store.positions() and not self.store.orders(ACTIVE):
            self.write_final_report(state)
        self.last_tick_at = now
        self.counters["ticks"] += 1
        self.write_status(now, phase, state, br)

    def run(self, max_ticks: int | None = None, bootstrap_attempts: int = 30):
        for attempt in range(bootstrap_attempts):
            try:
                self.bootstrap()
                break
            except Exception as e:
                log.error("bootstrap failed (attempt %d): %s", attempt + 1, repr(e)[:200])
                self.store.event("bootstrap_error", {"attempt": attempt + 1, "error": repr(e)[:300]})
                self.last_error = {"at": self.clock().isoformat(), "error": f"bootstrap: {redact(repr(e))[:200]}"}
                self.write_boot_status(attempt + 1, bootstrap_attempts)
                if attempt == 0:
                    self.alert("engine bootstrap failing; retrying", {"error": repr(e)[:200]})
                if attempt == bootstrap_attempts - 1:
                    raise
                time.sleep(min(60, 2 ** attempt))
        n = 0
        while max_ticks is None or n < max_ticks:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as e:
                self.counters["tick_errors"] += 1
                self.last_error = {"at": self.clock().isoformat(), "error": f"tick: {repr(e)[:200]}"}
                log.error("tick failed: %s", traceback.format_exc())
                self.store.event("tick_error", {"error": repr(e)[:300]})
                if self.counters["tick_errors"] % 10 == 1:
                    self.alert("engine tick failing", {"error": repr(e)[:200]})
            n += 1
            time.sleep(max(0.0, self.s.execution.exit_poll_seconds - (time.monotonic() - t0)))

    def close(self):
        for pool in (self.pool, self.data_pool, self.account_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        if hasattr(self.md, 'close'):
            self.md.close()
        if hasattr(self.alert, 'pool'):
            self.alert.pool.shutdown(wait=False, cancel_futures=True)

    # ================================================================ reporting
    def _client(self):
        return getattr(self.md, "client", None) or getattr(self.venue, "client", None) or self.account_client

    def _client_health(self):
        client = self._client()
        base = client.health() if hasattr(client, 'health') else None
        if base is None:
            return None
        rest = self.md.status().get('rest') if hasattr(self.md, 'status') else None
        if rest:
            base.update(rest)
        private = (self.exchange_account or {}).get('health')
        if private:
            base['auth'] = private.get('auth', base.get('auth'))
            base['last_auth_ok_at'] = private.get('last_auth_ok_at')
            base['endpoint_access'] = private.get('endpoint_access', {})
        return base

    def _write_status_file(self, status: dict):
        p = Path(self.s.status_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(redact(json.dumps(status, indent=1, default=str)))
        tmp.replace(p)

    def write_boot_status(self, attempt: int, attempts: int):
        """Status while bootstrap is failing (e.g. Wallex unreachable): the dashboard shows this run, not a stale one."""
        now = self.clock()
        client = self._client()
        self._write_status_file({
            "mode": self.s.mode, "venue": self.venue.label, "data_source": getattr(self.md, "label", "?"),
            "simulated": self.venue.label != "live", "phase": "BOOTSTRAP",
            "engine_state": f"BOOTSTRAP_RETRY {attempt}/{attempts}", "run_id": self.store.run_id,
            "db_path": str(self.store.path), "started_at": self.store.get_kv("started_at"),
            "last_cycle_at": now.isoformat(), "entries_halted": True,
            "halt_reason": "bootstrap failing: no market metadata yet (see Wallex connection)",
            "kill_switch": self.kill.status(), "last_error": self.last_error,
            "limits": {k: getattr(self.s.risk, k) for k in ("authorized_capital_tmn", "total_credit_tmn", "max_order_tmn",
                                                             "max_positions", "daily_loss_limit_tmn")},
            "strategy": {"version": self.strategy_version, "families": self.s.strategy.enabled_families,
                         "edge_gate": self.s.strategy.edge_gate, "signal_seconds": self.s.market.signal_seconds,
                         "context_seconds": self.s.market.context_seconds,
                         "selector": getattr(self.selector, "name", "?"), "config": self.s.config_path},
            "exchange": {"wallex": self._client_health(),
                         "market_data": self.md.status() if hasattr(self.md, "status") else {"depth_source": getattr(self.md, "label", "?")},
                         "book_age_s": {}, "account": self.exchange_account},
            "open_positions": [], "open_orders": [], "counters": self.counters,
            "manifest": {k: self.manifest.get(k) for k in ("git_commit", "config_hash", "strategy_version", "prompt_version", "model")},
        })

    def write_status(self, now, phase, state: PortfolioState, br):
        initial = dec(self.store.get_kv("bot_initial_equity", str(state.equity_tmn)))
        closed = self.store.closed_positions(1_000_000)
        realized = sum((dec(p["realized_pnl_tmn"] or "0") for p in closed), ZERO)
        eq = [dec(r["equity_tmn"]) for r in self.store.db.execute("SELECT equity_tmn FROM equity ORDER BY at DESC LIMIT 20000")][::-1]
        peak, mdd = (eq[0] if eq else ZERO), ZERO
        for e in eq:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak if peak > 0 else ZERO)
        positions = []
        for p in self.store.positions():
            book = self.books.get(p["symbol"])
            bid = book.best_bid if book else None
            qty = dec(p["qty"])
            fee = self.fees.get(p["symbol"], D(self.s.execution.taker_fee_fraction))
            unreal = qty * bid * (1 - fee) + dec(p["proceeds_tmn"]) - dec(p["cost_tmn"]) if bid and qty > 0 else None
            opened = datetime.fromisoformat(p["opened_at"]) if p["opened_at"] else None
            positions.append({**{k: p[k] for k in ("position_id", "symbol", "family", "status", "qty", "entry_price", "stop",
                                                    "target", "fees_tmn", "opened_at", "max_bars", "signal_seconds")},
                              "current_bid": str(bid) if bid else None,
                              "book_age_s": round(book.age_seconds(now), 1) if book else None,
                              "unrealized_pnl_tmn": f"{unreal:.0f}" if unreal is not None else None,
                              "valuation_stale": book is None or not 0 <= book.age_seconds(now) <= self.s.market.max_book_age_seconds,
                              "duration_s": int((now - opened).total_seconds()) if opened else None,
                              "max_hold_s": p["max_bars"] * p["signal_seconds"]})
        detail = getattr(self, "last_detail", {}) or {}
        ks = self.kill.status()
        client = self._client()
        api_health = self._client_health()
        last_api = api_health.get("last_ok_at") if api_health else None  # real HTTP success, never a paper balance read
        status = {
            "mode": self.s.mode, "venue": self.venue.label, "data_source": getattr(self.md, "label", "?"),
            "simulated": self.venue.label != "live", "phase": phase, "engine_state": "DEGRADED" if state.entries_halted or any(not 0 <= b.age_seconds(now) <= self.s.market.max_book_age_seconds for b in self.books.values()) else "RUNNING",
            "experimental": self.s.experimental_shadow, "real_trading_enabled": self.venue.label == "live" and bool(getattr(self._client(), "trading_enabled", False)),
            "cycle_duration_s": self.last_cycle_duration_s, "decision_data_pending": self.data_pending is not None,
            "valuation_current": all(p.get("book_age_s") is not None and 0 <= p["book_age_s"] <= self.s.market.max_book_age_seconds for p in positions),
            "run_id": self.store.run_id,
            "db_path": str(self.store.path), "started_at": self.store.get_kv("started_at"),
            "last_cycle_at": now.isoformat(), "last_decision_bar": self.store.get_kv("last_decision_bar"),
            "equity_tmn": f"{state.equity_tmn:.0f}", "initial_equity_tmn": f"{initial:.0f}",
            "realized_pnl_tmn": f"{realized:.0f}", "max_drawdown_pct": f"{mdd * 100:.3f}",
            "drawdown_pct": f"{br.drawdown_fraction * 100:.3f}", "daily_loss_pct": f"{br.daily_loss_fraction * 100:.3f}",
            "day_start_equity_tmn": f"{state.day_start_equity_tmn:.0f}", "high_water_tmn": f"{state.high_water_tmn:.0f}",
            "entries_halted": state.entries_halted, "halt_reason": state.halt_reason,
            "breaker_latched": bool(self.store.get_kv("breaker_latched")),
            "daily_halt": bool(self.store.get_kv("daily_halt")) and self.store.get_kv("daily_halt") == self.store.get_kv("day"),
            "kill_switch": ks,
            "allocation": detail.get("allocation"),
            "account": {"tmn_free": detail.get("free_tmn"), "value_tmn": detail.get("account_value_tmn"),
                        "unvalued_assets": detail.get("account_unvalued"), "holdings": detail.get("holdings"),
                        "external_baseline": self.store.get_kv("external_baseline")},
            "foreign_open_orders": self.foreign_orders,
            "limits": {k: getattr(self.s.risk, k) for k in ("authorized_capital_tmn", "total_credit_tmn", "max_order_tmn",
                                                             "max_positions", "daily_loss_limit_tmn", "max_drawdown_fraction",
                                                             "min_net_target_bps")},
            "strategy": {"version": self.strategy_version, "edge_gate": self.s.strategy.edge_gate, "families": self.s.strategy.enabled_families,
                         "signal_seconds": self.s.market.signal_seconds, "context_seconds": self.s.market.context_seconds,
                         "selector": getattr(self.selector, "name", "?"), "config": self.s.config_path,
                         'ai_provider': effective_provider(self.s.ai), 'ai_model': self.s.ai.model if effective_provider(self.s.ai) != 'baseline' else None,
                         "ai_credentials": credentials_available(self.s.ai) if effective_provider(self.s.ai) != 'baseline' else None},
            'llm': self.selector.stats() if hasattr(self.selector, 'stats') else {'provider': 'baseline', 'request_attempts': 0},
            "connectivity": {"last_api_ok_at": last_api,
                             "last_account_ok_at": self.last_account_ok.isoformat() if self.last_account_ok else None,
                             "last_market_data_at": self.last_md_ok.isoformat() if self.last_md_ok else None,
                             "api_failures": self.counters["api_failures"]},
            "last_error": self.last_error,
            "exchange": {
                "wallex": self._client_health(),
                "market_data": self.md.status() if hasattr(self.md, "status") else {"depth_source": getattr(self.md, "label", "?")},
                "book_age_s": {s: round(b.age_seconds(now), 1) for s, b in sorted(self.books.items())},
                "account": self.exchange_account,
            },
            "fees": {k: str(v) for k, v in self.fees.items()},
            "open_positions": positions,
            "open_orders": [{k: o[k] for k in ("client_id", "symbol", "side", "purpose", "state", "quantity", "executed_qty", "limit_price", "created_at")} for o in self.store.orders(ACTIVE)],
            "counters": self.counters, "ai_pending": self.pending is not None,
            "recent_decisions": [{k: d[k] for k in ("created_at", "action", "candidate_id", "source", "final_action")} for d in self.store.decisions(5)],
            "manifest": {k: self.manifest.get(k) for k in ("git_commit", "config_hash", "strategy_version", "prompt_version", "model")},
        }
        self._write_status_file(status)

    def write_final_report(self, state: PortfolioState):
        balances = self._balances()
        bal = {c: {"free": str(b.free), "locked": str(b.locked)} for c, b in balances.items() if b.total > 0}
        closed = self.store.closed_positions(1_000_000)
        initial = dec(self.store.get_kv("bot_initial_equity", "0"))
        tmn = balances.get("TMN")
        residual, dust = {}, {}
        for c, v in bal.items():
            if c == "TMN":
                continue
            m = next((m for m in self.markets.values() if m.base == c), None)
            book = self.books.get(m.symbol) if m else None
            qty = dec(v["free"]) + dec(v["locked"])
            sellable = m is not None and qty >= m.min_quantity and (book is None or book.best_bid is None or qty * book.best_bid >= m.min_notional)
            (residual if sellable else dust)[c] = v
        report = {"run_id": self.store.run_id, "written_at": self.clock().isoformat(), "simulated": self.venue.label != "live",
                  "bot_initial_equity_tmn": str(initial), "final_tmn": str(tmn.total if tmn else 0),
                  "final_bot_equity_tmn": f"{state.equity_tmn:.0f}",
                  "bot_net_pnl_tmn": f"{state.equity_tmn - initial:.0f}",
                  "realized_pnl_from_positions_tmn": str(sum((dec(p["realized_pnl_tmn"] or "0") for p in closed), ZERO)),
                  "closed_positions": len(closed), "final_balances": bal,
                  "non_tmn_residuals": residual, "dust_below_min_order": dust,
                  "manifest": self.manifest, "strategy_version": self.strategy_version}
        Path(self.s.evidence_dir).mkdir(parents=True, exist_ok=True)
        Path(self.s.evidence_dir, "final_report.json").write_text(json.dumps(report, indent=2, default=str))
        self.store.event("final_report", report)
        self.store.set_kv("final_report_written", True)
        self.final_written = True
