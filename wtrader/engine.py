"""Trading engine: one process, one order writer, two clocks.

Protection clock (every exit_poll_seconds): reconcile orders, mark equity, enforce
breakers, manage exits and liquidation. It never waits for the AI.
Decision clock (each closed signal bar): build the frozen snapshot, run the
selector in a worker thread, then apply the risk gate on a fresh book and submit.
Shadow and live differ only in the venue object.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import STRATEGY_VERSION
from .ai_agent import BaselineSelector, build_context
from .config import Settings, Secrets
from .executor import ACTIVE, Executor
from .models import Book, Market, ZERO, dec, floor_step
from .risk import OpenRisk, PortfolioState, breaker_status, competition_phase, evaluate_entry, exit_signal
from .store import Store
from .strategy import analyze, apply_market_filter, trail_stop

log = logging.getLogger(__name__)
D = Decimal
TEHRAN = ZoneInfo("Asia/Tehran")


class Alerter:
    def __init__(self, store: Store, webhook: str | None):
        self.store, self.webhook, self.sent = store, webhook, {}

    def __call__(self, message: str, detail: dict | None = None):
        log.critical("ALERT %s %s", message, detail or "")
        self.store.event("alert", {"message": message, **(detail or {})})
        if not self.webhook or time.time() - self.sent.get(message, 0) < 600:
            return
        self.sent[message] = time.time()
        try:
            httpx.post(self.webhook, json={"text": f"[wtrader] {message} {json.dumps(detail or {}, default=str)}"}, timeout=5)
        except Exception:
            log.warning("alert webhook failed")


class Engine:
    def __init__(self, settings: Settings, store: Store, market_data, venue, selector, manifest: dict,
                 secrets: Secrets | None = None, clock=None):
        self.s, self.store, self.md, self.venue, self.selector = settings, store, market_data, venue, selector
        self.manifest = manifest
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.alert = Alerter(store, secrets.alert_webhook if secrets else None)
        self.executor = Executor(store, venue, self._on_fill, settings.execution.unknown_reconcile_seconds, self.alert)
        self.baseline = BaselineSelector()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai")
        self.pending: tuple[Future, dict, list, str] | None = None
        self.markets: dict[str, Market] = {}
        self.books: dict[str, Book] = {}
        self.fees: dict[str, Decimal] = {}
        self.edge = self._load_edge()
        self.counters = {"api_failures": 0, "ai_failures": 0, "tick_errors": 0, "ticks": 0}
        self.last_tick_at = None
        self.final_written = store.get_kv("final_report_written", False)
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
        # restart recovery: venue is the source of truth for every order we know about
        self.executor.reconcile_all()
        self.store.event("recovered", {"open_positions": len(self.store.positions()),
                                       "active_orders": len(self.store.orders(ACTIVE))})

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
            target = entry + (entry - stop) * dec(meta.get("target_r", "2"))
            meta["peak"] = str(max(dec(meta.get("peak", "0")), entry))
            self.store.update_position(pid, qty=qty, cost_tmn=cost, entry_price=entry, target=target, status="OPEN",
                                       opened_at=p["opened_at"] or self.clock().isoformat(), fees_tmn=fees)
            self.store.set_kv(f"pos_meta:{pid}", meta)
        else:
            qty -= dq
            proceeds += dquote - dfq
            self.store.update_position(pid, qty=qty, proceeds_tmn=proceeds, fees_tmn=fees)
        self.store.event("fill", {"position_id": pid, "client_id": order["client_id"], "side": order["side"],
                                  "qty": str(dq), "quote": str(dquote), "fee_base": str(dfb), "fee_quote": str(dfq)})

    # ================================================================ state
    def _book(self, symbol: str) -> Book | None:
        try:
            b = self.md.book(symbol)
            self.books[symbol] = b
            return b
        except Exception as e:
            self.counters["api_failures"] += 1
            log.warning("book %s failed: %s", symbol, type(e).__name__)
            return self.books.get(symbol)

    def portfolio_state(self, now: datetime) -> tuple[PortfolioState, dict]:
        bal = self.venue.balances()
        tmn = bal.get("TMN")
        free_tmn = tmn.free if tmn else ZERO
        equity = tmn.total if tmn else ZERO
        holdings = {}
        for sym, m in self.markets.items():
            b = bal.get(m.base)
            if b and b.total > 0:
                book = self.books.get(sym)
                bid = book.best_bid if book else None
                value = b.total * bid * (1 - self.fees.get(sym, ZERO)) if bid else ZERO
                holdings[sym] = {"qty": str(b.total), "bid": str(bid), "value_tmn": str(value)}
                equity += value
        exposures = []
        for p in self.store.positions():
            book = self.books.get(p["symbol"])
            bid = book.best_bid if book and book.best_bid else dec(p["entry_price"] or "0")
            qty, stop = dec(p["qty"]), dec(p["stop"])
            pending_qty = sum((dec(o["quantity"]) - dec(o["executed_qty"]) for o in self.executor.active_for_position(p["position_id"])
                               if o["side"] == "BUY"), ZERO)
            notional = (qty + pending_qty) * bid
            exposures.append(OpenRisk(p["symbol"], notional, max(ZERO, (qty + pending_qty) * (bid - stop))))
        day = now.astimezone(TEHRAN).date().isoformat()
        if self.store.get_kv("day") != day:
            self.store.set_kv("day", day)
            self.store.set_kv("day_start_equity", str(equity))
        hwm = max(dec(self.store.get_kv("high_water", "0")), equity)
        self.store.set_kv("high_water", str(hwm))
        if self.store.get_kv("initial_equity") is None:
            self.store.set_kv("initial_equity", str(equity))
        halted, reason = False, None
        if self.store.get_kv("breaker_latched"):
            halted, reason = True, "drawdown breaker latched"
        elif self.store.get_kv("daily_halt") == day:
            halted, reason = True, "daily loss limit"
        elif self.executor.unresolved():
            halted, reason = True, "unresolved order outcome"
        cooldown = frozenset(s for s, until in (self.store.get_kv("cooldown", {}) or {}).items()
                             if datetime.fromisoformat(until) > now)
        st = PortfolioState(equity, free_tmn, tuple(exposures), dec(self.store.get_kv("day_start_equity", str(equity))),
                            hwm, halted, reason, cooldown)
        return st, {"holdings": holdings, "free_tmn": str(free_tmn)}

    # ================================================================ protection clock
    def protect(self, now: datetime, phase: str):
        self.executor.reconcile_all()
        syms = {p["symbol"] for p in self.store.positions()} | {o["symbol"] for o in self.store.orders(ACTIVE)}
        for s in syms:
            self._book(s)
        self.venue.poll({s: self.books[s] for s in syms if s in self.books})
        state, detail = self.portfolio_state(now)
        br = breaker_status(state, self.s.risk)
        if br.drawdown_hit and not self.store.get_kv("breaker_latched"):
            self.store.set_kv("breaker_latched", True)
            self.alert("DRAWDOWN BREAKER: flattening and halting entries", {"drawdown": str(br.drawdown_fraction)})
        if br.daily_loss_hit and self.store.get_kv("daily_halt") != self.store.get_kv("day"):
            self.store.set_kv("daily_halt", self.store.get_kv("day"))
            self.alert("daily loss limit hit: entries halted for the rest of the Tehran day",
                       {"daily_loss": str(br.daily_loss_fraction)})
        for p in self.store.positions():
            self._manage_position(p, now, phase, bool(self.store.get_kv("breaker_latched")))
        if phase in ("LIQUIDATE", "ENDED"):
            self._sweep_residuals(now)
        self.store.record_equity(state.equity_tmn, state.free_tmn, detail)
        return state, br

    def _manage_position(self, p: dict, now: datetime, phase: str, breaker: bool):
        pid, sym = p["position_id"], p["symbol"]
        market = self.markets.get(sym)
        meta = self._meta(pid)
        active = self.executor.active_for_position(pid)
        entries = [o for o in active if o["side"] == "BUY"]
        exits = [o for o in active if o["side"] == "SELL"]
        cfg = self.s.execution
        for o in entries:   # stale or no-longer-wanted entry remainder
            age = (now - datetime.fromisoformat(o["created_at"])).total_seconds()
            unwanted = phase not in ("TRADING", "UNSCHEDULED") or breaker
            if o["state"] in ("OPEN", "PARTIALLY_FILLED") and (age > cfg.entry_cancel_after_seconds or unwanted):
                self.executor.cancel(o["client_id"])
        qty = dec(p["qty"])
        if p["status"] == "PENDING":
            if not active and qty <= 0:
                self.store.update_position(pid, status="CANCELED", closed_at=now.isoformat(), exit_reason="ENTRY_UNFILLED")
            return
        book = self.books.get(sym)
        bid = book.best_bid if book else None
        if book is None or book.age_seconds(now) > self.s.market.max_book_age_seconds * 3:
            self.alert("protection degraded: stale or missing book for open position", {"symbol": sym})
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
        if not es and not sig:
            return
        reason = es.reason if es else meta["exit_reason"]
        if bid is None:
            return
        attempt = int(meta.get("exit_attempt", 0))
        steps = [D(x) for x in cfg.liquidation_slippage_steps_bps]
        slip = steps[min(attempt, len(steps) - 1)] if reason in ("LIQUIDATION", "BREAKER", "STOP") else D(cfg.exit_limit_slippage_bps)
        free_base = self.venue.balances().get(market.base)
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
        """Terminal conversion of any non-TMN holdings not owned by an open position."""
        owned = {p["symbol"] for p in self.store.positions()}
        if any(o["side"] == "BUY" for o in self.store.orders(ACTIVE)):
            for o in self.store.orders(ACTIVE):
                if o["side"] == "BUY":
                    self.executor.cancel(o["client_id"])
            return
        bal = self.venue.balances()
        for sym in self.tradable:
            m = self.markets[sym]
            b = bal.get(m.base)
            if sym in owned or not b or b.free <= 0:
                continue
            if any(o["symbol"] == sym for o in self.store.orders(ACTIVE)):
                continue
            book = self._book(sym)
            if not book or not book.best_bid:
                continue
            qty = floor_step(b.free, m.step)
            if qty < m.min_quantity or qty * book.best_bid < m.min_notional:
                continue
            n = int(self.store.get_kv(f"liq_attempt:{sym}", 0))
            steps = [D(x) for x in self.s.execution.liquidation_slippage_steps_bps]
            limit = floor_step(book.best_bid * (1 - steps[min(n, len(steps) - 1)] / D(10000)), m.tick)
            self.store.set_kv(f"liq_attempt:{sym}", n + 1)
            self.executor.place(economic_key=f"LIQ:{sym}:{n}", symbol=sym, side="SELL", quantity=qty, limit_price=limit,
                                purpose="LIQUIDATION", market=m)

    # ================================================================ decision clock
    def maybe_decide(self, now: datetime, phase: str, state: PortfolioState):
        sig_s = self.s.market.signal_seconds
        boundary = int(now.timestamp()) // sig_s * sig_s
        if self.pending is None and boundary > int(self.store.get_kv("last_decision_bar", 0)) and now.timestamp() >= boundary + 8:
            self.store.set_kv("last_decision_bar", boundary)
            self._start_decision(boundary, now, phase, state)
        if self.pending and self.pending[0].done():
            fut, ctx, analyses, did = self.pending
            self.pending = None
            self._finish_decision(fut, ctx, analyses, did, phase)

    def _start_decision(self, as_of: int, now: datetime, phase: str, state: PortfolioState):
        mc, sc = self.s.market, self.s.strategy
        analyses = []
        for sym in sorted(self.markets):
            try:
                sig = self.md.candles(sym, mc.signal_seconds, as_of - 230 * mc.signal_seconds, as_of)
                ctx = self.md.candles(sym, mc.context_seconds, as_of - 110 * mc.context_seconds, as_of)
            except Exception as e:
                self.counters["api_failures"] += 1
                log.warning("candles %s failed: %s", sym, type(e).__name__)
                continue
            a = analyze(sym, sig, ctx, as_of, sc, mc.signal_seconds, mc.context_seconds, mc.warmup_bars)
            if sym not in self.tradable:
                a = type(a)(a.symbol, a.as_of, a.regime, a.features, ())    # reference market: context only
            analyses.append(a)
        analyses = apply_market_filter(analyses, sc.market_filter_symbol)
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
            why = "no candidates" if not ctx["candidates"] else (state.halt_reason or phase)
            self.store.record_decision(snapshot_id=ctx["snapshot_id"], snapshot_ref=str(snap_path), n_candidates=len(ctx["candidates"]),
                                       action="HOLD", source="deterministic", decision={"reason": why},
                                       final_action="HOLD", baseline_candidate_id=self.baseline.select(ctx).candidate_id)
            return
        did = self.store.record_decision(snapshot_id=ctx["snapshot_id"], snapshot_ref=str(snap_path),
                                         n_candidates=len(ctx["candidates"]), action="PENDING", source=self.selector.name,
                                         decision={}, final_action="PENDING",
                                         baseline_candidate_id=self.baseline.select(ctx).candidate_id)
        self.pending = (self.pool.submit(self.selector.select, ctx), ctx, analyses, did)

    def _finish_decision(self, fut: Future, ctx: dict, analyses: list, did: str, phase: str):
        now = self.clock()
        try:
            d = fut.result()
        except Exception as e:
            self.counters["ai_failures"] += 1
            self.store.update_decision(did, action="HOLD", source="error", final_action="HOLD",
                                       decision_json={"error": repr(e)[:300]})
            return
        if d.source.startswith("fallback"):
            self.counters["ai_failures"] += 1
            self.alert("AI selector unavailable/invalid; outage policy applied", {"policy": self.s.ai.outage_action, "error": (d.error or "")[:120]})
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
        phase = competition_phase(now, self.s.competition)
        allow = self.s.market.account_allowlist or None
        v = evaluate_entry(symbol=cand.symbol, stop=cand.stop, max_stop_fraction=D(sc.stop_distance_max_fraction),
                           min_stop_fraction=D(sc.stop_distance_min_fraction), multiplier=D(d.multiplier), state=state,
                           market=self.markets.get(cand.symbol), book=book, now=now, fee_fraction=self.fees.get(cand.symbol, D(self.s.execution.taker_fee_fraction)),
                           risk=self.s.risk, mcfg=self.s.market, allowlist=allow, phase=phase)
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
                log.error("tick failed: %s", traceback.format_exc())
                self.store.event("tick_error", {"error": repr(e)[:300]})
                if self.counters["tick_errors"] % 10 == 1:
                    self.alert("engine tick failing", {"error": repr(e)[:200]})
            n += 1
            time.sleep(max(0.0, self.s.execution.exit_poll_seconds - (time.monotonic() - t0)))

    # ================================================================ reporting
    def write_status(self, now, phase, state: PortfolioState, br):
        initial = dec(self.store.get_kv("initial_equity", str(state.equity_tmn)))
        closed = self.store.closed_positions()
        realized = sum((dec(p["realized_pnl_tmn"] or "0") for p in closed), ZERO)
        eq = [dec(r["equity_tmn"]) for r in self.store.db.execute("SELECT equity_tmn FROM equity ORDER BY at DESC LIMIT 20000")][::-1]
        peak, mdd = (eq[0] if eq else ZERO), ZERO
        for e in eq:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak if peak > 0 else ZERO)
        status = {
            "mode": self.s.mode, "venue": self.venue.label, "data_source": getattr(self.md, "label", "?"),
            "simulated": self.venue.label != "live", "phase": phase, "run_id": self.store.run_id,
            "last_cycle_at": now.isoformat(), "last_decision_bar": self.store.get_kv("last_decision_bar"),
            "equity_tmn": f"{state.equity_tmn:.0f}", "initial_equity_tmn": f"{initial:.0f}",
            "realized_pnl_tmn": f"{realized:.0f}", "max_drawdown_pct": f"{mdd * 100:.3f}",
            "drawdown_pct": f"{br.drawdown_fraction * 100:.3f}", "daily_loss_pct": f"{br.daily_loss_fraction * 100:.3f}",
            "entries_halted": state.entries_halted, "halt_reason": state.halt_reason,
            "breaker_latched": bool(self.store.get_kv("breaker_latched")),
            "open_positions": [{k: p[k] for k in ("position_id", "symbol", "family", "status", "qty", "entry_price", "stop", "target")} for p in self.store.positions()],
            "open_orders": [{k: o[k] for k in ("client_id", "symbol", "side", "purpose", "state", "quantity", "executed_qty")} for o in self.store.orders(ACTIVE)],
            "counters": self.counters, "ai_pending": self.pending is not None,
            "recent_decisions": [{k: d[k] for k in ("created_at", "action", "candidate_id", "source", "final_action")} for d in self.store.decisions(5)],
            "manifest": {k: self.manifest.get(k) for k in ("git_commit", "config_hash", "strategy_version", "prompt_version", "model")},
        }
        p = Path(self.s.status_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=1, default=str))
        tmp.replace(p)

    def write_final_report(self, state: PortfolioState):
        bal = {c: {"free": str(b.free), "locked": str(b.locked)} for c, b in self.venue.balances().items() if b.total > 0}
        closed = self.store.closed_positions()
        initial = dec(self.store.get_kv("initial_equity", "0"))
        tmn = self.venue.balances().get("TMN")
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
                  "initial_equity_tmn": str(initial), "final_tmn": str(tmn.total if tmn else 0),
                  "final_equity_tmn": f"{state.equity_tmn:.0f}",
                  "net_pnl_tmn": str((tmn.total if tmn else ZERO) - initial),
                  "realized_pnl_from_positions_tmn": str(sum((dec(p["realized_pnl_tmn"] or "0") for p in closed), ZERO)),
                  "closed_positions": len(closed), "final_balances": bal,
                  "non_tmn_residuals": residual, "dust_below_min_order": dust,
                  "manifest": self.manifest, "strategy_version": STRATEGY_VERSION}
        Path(self.s.evidence_dir).mkdir(parents=True, exist_ok=True)
        Path(self.s.evidence_dir, "final_report.json").write_text(json.dumps(report, indent=2, default=str))
        self.store.event("final_report", report)
        self.store.set_kv("final_report_written", True)
        self.final_written = True
