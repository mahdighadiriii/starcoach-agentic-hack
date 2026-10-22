import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtrader.models import Book, Candle, Market  # noqa: E402


def mk_market(symbol="BTCTMN", tick="1", step="0.000001", min_qty="0.000001", min_notional="100000"):
    return Market(symbol, symbol, symbol[:-3], "TMN", D(tick), D(step), D(min_qty), D(min_notional))


def mk_book(symbol="BTCTMN", bid="1000000", ask="1000500", qty="100", at=None, levels=3, step_pct="0.0005"):
    at = at or datetime.now(timezone.utc)
    b, a, s = D(bid), D(ask), D(step_pct)
    bids = tuple((b * (1 - s * i), D(qty)) for i in range(levels))
    asks = tuple((a * (1 + s * i), D(qty)) for i in range(levels))
    return Book(symbol, bids, asks, at)


def trend_candles(n, start=1_700_000_000, seconds=3600, base=100.0, drift=0.002, wiggle=0.004, vol=10.0):
    """Deterministic, gently rising series with oscillation."""
    import math
    out, px = [], base
    t0 = start - start % seconds
    for i in range(n):
        o = px
        px = px * (1 + drift + wiggle * math.sin(i / 3))
        h, l = max(o, px) * 1.002, min(o, px) * 0.998
        out.append(Candle(t0 + i * seconds, D(str(round(o, 6))), D(str(round(h, 6))), D(str(round(l, 6))),
                          D(str(round(px, 6))), D(str(vol * (2 if i == n - 1 else 1)))))
    return out


@pytest.fixture
def market():
    return mk_market()
