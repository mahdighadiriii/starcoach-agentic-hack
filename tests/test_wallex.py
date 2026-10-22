"""Adapter contract tests with a mocked transport. Response shapes follow developers.wallex.ir examples."""
from decimal import Decimal as D

import httpx
import pytest

from wtrader.models import AmbiguousSubmission, AuthError, DefinitiveRejection, OrderIntent, OrderState
from wtrader.wallex import WallexClient

ORDER = {"symbol": "BTCTMN", "type": "LIMIT", "side": "BUY", "clientOrderId": "WABC", "price": "1000",
         "origQty": "2", "executedQty": "0.5", "executedSum": "500", "status": "NEW", "active": True,
         "fills": [{"fee": "0.001", "feeAsset": "BTC", "price": "1000", "quantity": "0.5"}]}


def client(handler, trading=False, **kw):
    c = WallexClient("KEY", kw.pop("sub", "SUB1"), "https://api.test", read_attempts=2,
                     http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
    if trading:
        c.enable_trading()      # only the gated live path does this in production
    return c


def intent(cid="WABC"):
    return OrderIntent(cid, cid, "BTCTMN", "BUY", D("1000"), D("2"), "ENTRY", None, None)


def mk(sym="BTCTMN"):
    from conftest import mk_market
    return mk_market(sym)


def test_markets_precision_and_tmn_filter():
    def h(req):
        if req.url.path == "/hector/web/v1/markets":
            return httpx.Response(200, json={"result": {"markets": [
                {"symbol": "BTCTMN", "base_asset": "BTC", "quote_asset": "TMN", "is_spot": True, "amount_precision": 6, "price_precision": 0},
                {"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT", "is_spot": True, "amount_precision": 6, "price_precision": 2}]}})
        return httpx.Response(200, json={"result": {"symbols": {
            "BTCTMN": {"baseAsset": "BTC", "quoteAsset": "TMN", "minQty": "0.000001", "minNotional": "100000"}}}})
    m = client(h).markets()
    assert set(m) == {"BTCTMN"} and m["BTCTMN"].step == D("0.000001") and m["BTCTMN"].tick == D("1")
    assert m["BTCTMN"].min_notional == D("100000")


def test_auth_and_sub_account_headers_sent_only_on_private_calls():
    seen = []

    def h(req):
        seen.append((req.url.path, req.headers.get("x-api-key"), req.headers.get("sub-account-client-id")))
        if req.url.path == "/v1/depth":
            return httpx.Response(200, json={"result": {"bid": [{"price": "99", "quantity": "1"}], "ask": [{"price": "101", "quantity": "2"}]}})
        return httpx.Response(200, json={"result": {"TMN": {"symbol": "TMN", "total": "100", "freeze": "30", "available": "70"}}})
    c = client(h)
    b = c.book("BTCTMN")
    bal = c.balances()
    assert b.best_bid == D("99") and b.best_ask == D("101")
    assert bal["TMN"].free == D("70") and bal["TMN"].locked == D("30")
    assert seen[0] == ("/v1/depth", None, None)
    assert seen[1] == ("/v1/account/balances-detail", "KEY", "SUB1")     # trade-credit wallet (documented)


def test_auth_error_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(401, json={})
    with pytest.raises(AuthError):
        client(h).balances()
    assert len(calls) == 1


def test_observation_maps_partial_fill_and_base_fee():
    o = WallexClient.observation(ORDER)
    assert o.state == OrderState.PARTIALLY_FILLED and o.executed_qty == D("0.5") and o.fee_base == D("0.001")
    sell = {**ORDER, "status": "FILLED", "fills": [{"fee": "2", "feeAsset": "TMN"}]}
    assert WallexClient.observation(sell).fee_quote == D("2")
    odd = {**ORDER, "status": "FINISHED", "executedQty": "2"}
    assert WallexClient.observation(odd).state == OrderState.FILLED


def test_submit_success_body_and_single_post():
    posts = []

    def h(req):
        posts.append(req)
        import json
        body = json.loads(req.content)
        assert body == {"symbol": "BTCTMN", "side": "BUY", "type": "LIMIT", "price": "1000", "quantity": "2", "client_id": "WABC"}
        return httpx.Response(201, json={"success": True, "result": ORDER})
    assert client(h, trading=True).submit(intent(), mk()).state == OrderState.PARTIALLY_FILLED and len(posts) == 1


@pytest.mark.parametrize("resp,exc", [
    (httpx.Response(500, json={}), AmbiguousSubmission),
    (httpx.Response(400, json={"success": False, "message": "min notional"}), DefinitiveRejection),
    (httpx.Response(201, json={"success": True, "result": {"clientOrderId": "OTHER"}}), AmbiguousSubmission),
    (httpx.Response(201, content=b"<html>"), AmbiguousSubmission),
])
def test_submit_error_classification_never_retries(resp, exc):
    posts = []

    def h(req):
        posts.append(1)
        return resp
    with pytest.raises(exc):
        client(h, trading=True).submit(intent(), mk())
    assert len(posts) == 1


def test_submit_timeout_is_ambiguous():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(AmbiguousSubmission):
        client(h, trading=True).submit(intent(), mk())


def test_bad_client_id_rejected_locally():
    with pytest.raises(DefinitiveRejection):
        client(lambda r: httpx.Response(500), trading=True).submit(intent("bad-id"), mk())


def test_lookup_not_found_is_none():
    assert client(lambda r: httpx.Response(404, json={})).lookup("WABC") is None


def test_read_retries_transient_then_succeeds():
    n = []

    def h(req):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"s": "ok", "t": [3600, 3601], "o": ["1", "1"], "h": ["2", "2"], "l": ["1", "1"], "c": ["2", "2"], "v": ["5", "5"]})
    cs = client(h).candles("BTCTMN", 3600, 0, 7200)
    assert len(n) == 2 and [c.open_time for c in cs] == [3600]     # off-grid bar dropped
