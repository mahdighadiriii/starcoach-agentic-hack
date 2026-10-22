from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from conftest import mk_book, mk_market
from wtrader.executor import Executor, client_id_for
from wtrader.models import OrderObservation, OrderState
from wtrader.store import SingleWriterLock, Store
from wtrader.venue import PaperVenue


class Books:
    def __init__(self, book):
        self.b = book

    def book(self, symbol):
        return self.b


@pytest.fixture
def env():
    store = Store(":memory:", "RTEST")
    venue = PaperVenue(Books(mk_book(qty="1")), fee_fraction=D("0.0035"), level_fraction=D("1"))
    venue.fund("TMN", D("5000000"))
    fills = []
    ex = Executor(store, venue, lambda o, dq, dqt, fb, fq: fills.append((o["client_id"], dq, dqt, fb, fq)), unknown_deadline_s=60)
    return store, venue, ex, fills


def place(ex, key="ENTRY:c1", qty="1", side="BUY", price="1002000"):
    return ex.place(economic_key=key, symbol="BTCTMN", side=side, quantity=D(qty), limit_price=D(price),
                    purpose="ENTRY", market=mk_market())


def test_intent_persisted_and_idempotent(env):
    store, venue, ex, fills = env
    cid = place(ex)
    assert cid == client_id_for("ENTRY:c1") and store.order(cid)["state"] == "FILLED"
    assert place(ex) is None                     # same economic intent never resubmitted
    assert len(venue.orders) == 1 and len(fills) == 1


def test_client_id_charset():
    import re
    assert re.fullmatch(r"[A-Z0-9_]+", client_id_for("ENTRY:BTCTMN-DONCHIAN_TREND-1"))


def test_definitive_rejection(env):
    store, venue, ex, _ = env
    venue.inject.append("reject")
    assert store.order(place(ex))["state"] == "REJECTED"


def test_timeout_after_acceptance_reconciles_to_filled_without_resubmit(env):
    store, venue, ex, fills = env
    venue.inject.append("timeout_accepted")
    cid = place(ex)
    assert store.order(cid)["state"] == "UNKNOWN" and ex.unresolved() and not fills
    ex.reconcile_all()
    assert store.order(cid)["state"] == "FILLED" and len(fills) == 1 and len(venue.orders) == 1
    assert not ex.unresolved()


def test_lost_order_is_quarantined_after_deadline(env):
    store, venue, ex, _ = env
    venue.inject.append("timeout_lost")
    cid = place(ex)
    ex.reconcile_all()
    assert store.order(cid)["state"] == "RECONCILING"
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    store.update_order(cid, submitted_at=old)
    ex.reconcile_all()
    assert store.order(cid)["state"] == "QUARANTINED" and ex.unresolved()
    assert store.events("order_quarantined")


def test_partial_fill_then_cancel_then_late_fill_counts_once(env):
    store, venue, ex, fills = env
    venue.book_source.b = mk_book(qty="0.4", levels=1)        # only 0.4 available at/below limit
    cid = place(ex, qty="1")
    assert store.order(cid)["state"] == "PARTIALLY_FILLED" and fills[-1][1] == D("0.4")
    # cancel race: venue fills more before processing the cancel
    venue.orders[cid].executed += D("0.1")
    venue.orders[cid].quote += D("0.1") * D("1000500")
    ex.cancel(cid)
    o = store.order(cid)
    assert o["state"] == "CANCELED" and D(o["executed_qty"]) == D("0.5")
    assert sum(f[1] for f in fills) == D("0.5")
    ex.reconcile_all()                                        # repeated observation: no double count
    assert sum(f[1] for f in fills) == D("0.5")


def test_regressed_observation_ignored(env):
    store, venue, ex, fills = env
    cid = place(ex)
    ex.apply(cid, OrderObservation(cid, OrderState.OPEN, D("0"), D("0"), D("0"), D("0")))
    assert store.order(cid)["state"] == "FILLED" and len(fills) == 1


def test_crash_between_persist_and_submit_is_reconciled(env):
    store, venue, ex, _ = env
    store.insert_order(client_id="WCRASH", economic_key="ENTRY:crash", symbol="BTCTMN", side="BUY", purpose="ENTRY",
                       quantity=D("1"), limit_price=D("1"), state="SUBMITTING")
    store.update_order("WCRASH", submitted_at=datetime.now(timezone.utc).isoformat())
    assert ex.unresolved()          # entries halted on restart until resolved
    ex.reconcile_all()
    assert store.order("WCRASH")["state"] == "RECONCILING"


def test_single_writer_lock(tmp_path):
    a = SingleWriterLock(tmp_path / "x.lock").acquire()
    with pytest.raises(RuntimeError):
        SingleWriterLock(tmp_path / "x.lock").acquire()
    a.release()
    SingleWriterLock(tmp_path / "x.lock").acquire().release()
