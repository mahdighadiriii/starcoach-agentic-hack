"""Order lifecycle with persisted intent, reconciliation and idempotent fill accounting.

CREATED -> SUBMITTING -> OPEN | PARTIALLY_FILLED | FILLED | REJECTED | UNKNOWN
UNKNOWN -> RECONCILING -> (found: venue state) | (not found past deadline: QUARANTINED)
OPEN/PARTIAL -> CANCEL_PENDING -> CANCELED | FILLED

Rules (from the challenge pack and the competition brief):
- The intent row is committed before the network call; the client_id is derived
  from the economic key, so a restart can never mint a second id for one intent,
  and Wallex itself rejects a reused client_id.
- An ambiguous POST is never retried. The order is reconciled by client_id.
- Fills are applied as deltas of the venue's cumulative executed values, so
  repeated observations (polls, restarts, late fills after cancel) cannot double count.
- Any UNKNOWN/RECONCILING/QUARANTINED order halts new entries; exits continue.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .models import (AmbiguousSubmission, DefinitiveRejection, Market, OrderIntent, OrderObservation, OrderState,
                     RateLimited, ZERO, dec)
from .store import Store

log = logging.getLogger(__name__)
ACTIVE = ("CREATED", "SUBMITTING", "UNKNOWN", "RECONCILING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING", "QUARANTINED")


def client_id_for(economic_key: str) -> str:
    return "W" + hashlib.sha256(economic_key.encode()).hexdigest()[:24].upper()


FillCallback = Callable[[dict, Decimal, Decimal, Decimal, Decimal], None]


class Executor:
    def __init__(self, store: Store, venue, on_fill: FillCallback, unknown_deadline_s: int = 120, alert=None):
        self.store, self.venue, self.on_fill = store, venue, on_fill
        self.unknown_deadline_s = unknown_deadline_s
        self.alert = alert or (lambda *a, **k: None)

    # ---------------------------------------------------------------- submit
    def place(self, *, economic_key: str, symbol: str, side: str, quantity: Decimal, limit_price: Decimal,
              purpose: str, market: Market, decision_id=None, position_id=None) -> str | None:
        cid = client_id_for(economic_key)
        if not self.store.insert_order(client_id=cid, economic_key=economic_key, symbol=symbol, side=side,
                                       purpose=purpose, quantity=quantity, limit_price=limit_price,
                                       decision_id=decision_id, position_id=position_id):
            log.info("intent %s already exists; not resubmitting", economic_key)
            return None
        self.store.update_order(cid, state="SUBMITTING", submitted_at=datetime.now(timezone.utc).isoformat())
        intent = OrderIntent(cid, cid, symbol, side, limit_price, quantity, purpose, decision_id, position_id)
        self.store.event("order_submit", {"client_id": cid, "symbol": symbol, "side": side, "qty": str(quantity),
                                          "limit": str(limit_price), "purpose": purpose})
        try:
            obs = self.venue.submit(intent, market)
        except DefinitiveRejection as e:
            self.store.update_order(cid, state="REJECTED", error=str(e)[:300])
            self.store.event("order_rejected", {"client_id": cid, "error": str(e)[:300]})
            return cid
        except (AmbiguousSubmission, RateLimited, Exception) as e:   # anything else is ambiguous too
            self.store.update_order(cid, state="UNKNOWN", error=f"{type(e).__name__}: {str(e)[:250]}")
            self.store.event("order_unknown", {"client_id": cid, "error": type(e).__name__})
            self.alert("order outcome UNKNOWN; entries halted until reconciled", {"client_id": cid, "purpose": purpose})
            return cid
        self.apply(cid, obs)
        return cid

    # ---------------------------------------------------------------- observe
    def apply(self, cid: str, obs: OrderObservation):
        o = self.store.order(cid)
        if o is None:
            return
        dq = obs.executed_qty - dec(o["applied_qty"])
        dquote = obs.executed_quote - dec(o["applied_quote"])
        dfb = obs.fee_base - dec(o["applied_fee_base"])
        dfq = obs.fee_quote - dec(o["applied_fee_quote"])
        if dq < 0 or dquote < 0:
            # cumulative values must be monotonic; a regression means a bad observation
            self.store.event("order_observation_regressed", {"client_id": cid})
            return
        state = obs.state
        if state == OrderState.UNKNOWN:
            state = OrderState(o["state"]) if o["state"] not in ("SUBMITTING", "CREATED") else OrderState.UNKNOWN
        if o["state"] == "CANCEL_PENDING" and state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            state = OrderState.CANCEL_PENDING          # cancel not yet confirmed by the venue
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            if dq > 0 or dfb > 0 or dfq > 0:
                self.on_fill(o, dq, dquote, dfb, dfq)
            self.store.update_order(cid, state=state.value, executed_qty=obs.executed_qty,
                                    executed_quote=obs.executed_quote, fee_base=obs.fee_base, fee_quote=obs.fee_quote,
                                    applied_qty=obs.executed_qty, applied_quote=obs.executed_quote,
                                    applied_fee_base=obs.fee_base, applied_fee_quote=obs.fee_quote)
            self.store.db.execute("COMMIT")
        except Exception:
            self.store.db.execute("ROLLBACK")
            raise
        if state.value != o["state"]:
            self.store.event("order_state", {"client_id": cid, "from": o["state"], "to": state.value,
                                             "executed": str(obs.executed_qty)})

    def reconcile(self, o: dict):
        cid = o["client_id"]
        try:
            obs = self.venue.lookup(cid)
        except Exception as e:
            log.warning("lookup %s failed: %s", cid, type(e).__name__)
            return
        if obs is not None:
            self.apply(cid, obs)
            return
        if o["state"] in ("UNKNOWN", "SUBMITTING", "RECONCILING"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(o["submitted_at"] or o["created_at"])).total_seconds()
            if age > self.unknown_deadline_s:
                self.store.update_order(cid, state="QUARANTINED")
                self.store.event("order_quarantined", {"client_id": cid, "age_s": int(age)})
                self.alert("order QUARANTINED: not found after deadline; operator review required", {"client_id": cid})
            elif o["state"] != "RECONCILING":
                self.store.update_order(cid, state="RECONCILING")
        elif o["state"] in ("OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING"):
            # previously acknowledged but not found now: keep state, retry next tick (eventual consistency)
            self.store.event("order_lookup_missing", {"client_id": cid, "state": o["state"]})

    def reconcile_all(self):
        for o in self.store.orders(("SUBMITTING", "UNKNOWN", "RECONCILING", "OPEN", "PARTIALLY_FILLED",
                                    "CANCEL_PENDING", "QUARANTINED")):
            self.reconcile(o)

    def cancel(self, cid: str):
        o = self.store.order(cid)
        if not o or o["state"] not in ("OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING"):
            return
        if o["state"] != "CANCEL_PENDING":
            self.store.update_order(cid, state="CANCEL_PENDING")
        try:
            obs = self.venue.cancel(cid)
        except Exception as e:
            log.warning("cancel %s ambiguous: %s", cid, type(e).__name__)
            return                                       # reconciled on the next tick
        if obs is not None:
            self.apply(cid, obs)

    # ---------------------------------------------------------------- queries
    def unresolved(self) -> list[dict]:
        return self.store.orders(("SUBMITTING", "UNKNOWN", "RECONCILING", "QUARANTINED"))

    def active_for_position(self, position_id: str) -> list[dict]:
        return [o for o in self.store.orders(ACTIVE) if o["position_id"] == position_id]
