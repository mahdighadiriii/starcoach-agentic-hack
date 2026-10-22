"""Operator safety controls: kill switch, strict capital allocation, live arming.

Kill switch (a JSON file, so it works while the engine holds the DB lock and even if
the dashboard is down). Levels:
- halt:    no new BUY orders; open entry orders are canceled; protective exits
           (stop / target / time / liquidation) keep running.
- flatten: as halt, plus every bot-owned position is exited. Assets the bot does not
           own are never touched.
- freeze:  no new orders of any kind (exits included); open bot entry orders are
           canceled; existing exit orders are left as they are. Positions are then
           unprotected until the switch is cleared or the operator acts manually.
The executor checks the file immediately before every submission, so engaging it
blocks the very next order, not the next tick.

Allocation: the bot may commit at most `authorized_capital_tmn`, shrunk by realized
losses and never grown by profits. Committed capital is computed from persisted
records (open positions at cost, unfilled remainder of every non-terminal BUY,
unresolved submissions at full size) plus BUY orders open on the exchange that the
bot did not create. Unresolved orders keep their reservation until reconciled, so a
retry or restart can never free capacity that might already be spent.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import RiskCfg
from .models import ZERO, dec

D = Decimal
KILL_LEVELS = ("halt", "flatten", "freeze")
BUY_RESERVING_STATES = ("CREATED", "SUBMITTING", "UNKNOWN", "RECONCILING", "OPEN", "PARTIALLY_FILLED",
                        "CANCEL_PENDING", "QUARANTINED")


# ---------------------------------------------------------------- kill switch
class KillSwitch:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def status(self) -> dict | None:
        """Engaged state, or None. An unreadable file counts as engaged at the strictest level."""
        if not self.path.exists():
            return None
        try:
            st = json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return {"level": "freeze", "reason": "kill file unreadable", "at": None}
        if not isinstance(st, dict):
            return {"level": "freeze", "reason": "kill file is not an object", "at": None}
        if st.get("level") not in KILL_LEVELS:
            st["level"] = "freeze"
        return st

    def engage(self, level: str = "halt", reason: str = "", by: str = "operator") -> dict:
        if level not in KILL_LEVELS:
            raise ValueError(f"level must be one of {KILL_LEVELS}")
        cur = self.status()
        if cur and KILL_LEVELS.index(cur["level"]) > KILL_LEVELS.index(level):
            level = cur["level"]                     # engaging never weakens an existing switch
        st = {"level": level, "reason": reason, "by": by, "at": datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.path)
        return st

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

    def blocks(self, side: str) -> str | None:
        st = self.status()
        if st is None:
            return None
        if st["level"] == "freeze" or side == "BUY":
            return f"kill switch ({st['level']}): {st.get('reason') or 'no reason given'}"
        return None


# ---------------------------------------------------------------- allocation
@dataclass(frozen=True)
class Allocation:
    authorized_tmn: Decimal       # configured ceiling
    budget_tmn: Decimal           # authorized + min(0, realized P&L)
    position_cost_tmn: Decimal    # open bot positions at cost basis
    reserved_tmn: Decimal         # unfilled remainder of bot BUY orders (incl. unresolved, full size)
    foreign_reserved_tmn: Decimal # BUY orders open on the exchange that the bot did not create
    realized_pnl_tmn: Decimal
    unrealized_pnl_tmn: Decimal
    exchange_free_tmn: Decimal

    @property
    def committed_tmn(self) -> Decimal:
        return self.position_cost_tmn + self.reserved_tmn + self.foreign_reserved_tmn

    @property
    def available_tmn(self) -> Decimal:
        return max(ZERO, min(self.budget_tmn - self.committed_tmn, self.exchange_free_tmn))

    @property
    def bot_equity_tmn(self) -> Decimal:
        """The allocation's own equity: never includes other account assets."""
        return self.authorized_tmn + self.realized_pnl_tmn + self.unrealized_pnl_tmn

    def as_dict(self) -> dict:
        return {"authorized_tmn": f"{self.authorized_tmn:.0f}", "budget_tmn": f"{self.budget_tmn:.0f}",
                "position_cost_tmn": f"{self.position_cost_tmn:.0f}", "reserved_tmn": f"{self.reserved_tmn:.0f}",
                "foreign_reserved_tmn": f"{self.foreign_reserved_tmn:.0f}", "committed_tmn": f"{self.committed_tmn:.0f}",
                "available_tmn": f"{self.available_tmn:.0f}", "realized_pnl_tmn": f"{self.realized_pnl_tmn:.0f}",
                "unrealized_pnl_tmn": f"{self.unrealized_pnl_tmn:.0f}", "bot_equity_tmn": f"{self.bot_equity_tmn:.0f}",
                "exchange_free_tmn": f"{self.exchange_free_tmn:.0f}"}


def buy_reservation(order: dict, fee: Decimal) -> Decimal:
    """TMN still reserved by a bot BUY order: the unfilled remainder at its limit, fee included."""
    if order["side"] != "BUY" or order["state"] not in BUY_RESERVING_STATES:
        return ZERO
    rem = max(ZERO, dec(order["quantity"]) - dec(order["executed_qty"]))
    return rem * dec(order["limit_price"]) * (1 + fee)


def compute_allocation(*, risk: RiskCfg, positions: list[dict], orders: list[dict], closed: list[dict],
                       bids: dict[str, Decimal], fees: dict[str, Decimal], default_fee: Decimal,
                       foreign_reserved: Decimal, exchange_free_tmn: Decimal) -> Allocation:
    authorized = dec(risk.authorized_capital_tmn)
    realized = sum((dec(p["realized_pnl_tmn"] or "0") for p in closed), ZERO)
    cost = unreal = ZERO
    for p in positions:
        qty = dec(p["qty"])
        entry = dec(p["entry_price"] or "0")
        cost += qty * entry
        bid = bids.get(p["symbol"])
        if bid is not None or qty == 0:
            bid = bid or ZERO
            fee = fees.get(p["symbol"], default_fee)
            # mark-to-bid net of the exit fee, plus anything already sold, minus everything paid
            unreal += qty * bid * (1 - fee) + dec(p["proceeds_tmn"]) - dec(p["cost_tmn"])
    reserved = sum((buy_reservation(o, fees.get(o["symbol"], default_fee)) for o in orders), ZERO)
    return Allocation(authorized, authorized + min(ZERO, realized), cost, reserved, foreign_reserved,
                      realized, unreal, exchange_free_tmn)


def check_buy(alloc: Allocation, order_tmn: Decimal, risk: RiskCfg) -> str | None:
    """Execution-layer allocation guard. Returns a refusal reason, or None when the BUY fits."""
    max_order = dec(risk.max_order_tmn)
    if order_tmn > max_order:
        return f"order {order_tmn:.0f} TMN exceeds max_order_tmn {max_order:.0f}"
    if alloc.committed_tmn + order_tmn > alloc.budget_tmn:
        return (f"allocation exceeded: committed {alloc.committed_tmn:.0f} + order {order_tmn:.0f} "
                f"> budget {alloc.budget_tmn:.0f}")
    if order_tmn > alloc.exchange_free_tmn:
        return f"exchange free TMN {alloc.exchange_free_tmn:.0f} < order {order_tmn:.0f}"
    return None


def foreign_buy_reserved(open_orders, known_ids: set[str]) -> tuple[Decimal, list[dict]]:
    """Open exchange orders the bot did not create. They are reported and reserved, never canceled."""
    total, rows = ZERO, []
    for o in open_orders:
        if o.client_id and o.client_id.upper() in known_ids:
            continue
        raw = o.raw or {}
        side = str(raw.get("side", "")).upper()
        try:
            price, orig = dec(raw.get("price") or "0"), dec(raw.get("origQty") or "0")
        except ValueError:
            price = orig = ZERO
        rem = max(ZERO, orig - o.executed_qty)
        if side == "BUY":
            total += rem * price
        rows.append({"client_id": o.client_id, "symbol": raw.get("symbol"), "side": side, "price": str(price),
                     "remaining_qty": str(rem)})
    return total, rows


# ---------------------------------------------------------------- live arming
ARM_PHRASE = "ARM LIVE {amount} TMN"


def arm_record(*, operator: str, authorized_tmn: str, max_order_tmn: str, config_hash: str, git_commit: str | None,
               hours: float, preflight_at: str) -> dict:
    now = datetime.now(timezone.utc)
    return {"operator": operator, "armed_at": now.isoformat(), "expires_at": (now + timedelta(hours=hours)).isoformat(),
            "authorized_capital_tmn": authorized_tmn, "max_order_tmn": max_order_tmn, "config_hash": config_hash,
            "git_commit": git_commit, "preflight_at": preflight_at}


def arm_failures(arm: dict | None, *, authorized_tmn: str, max_order_tmn: str, config_hash: str,
                 git_commit: str | None, now: datetime | None = None) -> list[str]:
    """The operator's arm record must match exactly what is about to run, and must not be expired."""
    if not arm:
        return ["live not armed: run `wtrader arm-live` (operator confirmation required)"]
    now = now or datetime.now(timezone.utc)
    fails = []
    if datetime.fromisoformat(arm["expires_at"]) <= now:
        fails.append(f"live arm expired at {arm['expires_at']}")
    if D(arm["authorized_capital_tmn"]) != D(authorized_tmn):
        fails.append(f"authorized capital {authorized_tmn} != armed {arm['authorized_capital_tmn']} (re-arm to change it)")
    if D(arm["max_order_tmn"]) != D(max_order_tmn):
        fails.append(f"max order {max_order_tmn} != armed {arm['max_order_tmn']}")
    if arm["config_hash"] != config_hash:
        fails.append("configuration changed since arming")
    if arm.get("git_commit") != git_commit:
        fails.append("code commit changed since arming")
    return fails


def load_json(path: str | Path) -> dict | None:
    p = Path(path)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        return None
