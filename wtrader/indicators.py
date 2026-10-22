"""Canonical indicator implementations used by every mode.

Per challenge-pack spec: EMA alpha=2/(N+1) seeded with the first value's SMA of N;
ATR/RSI use Wilder smoothing seeded by the mean of the first N values; RSI with
zero loss = 100, all-flat = 50. Floats are fine here: indicators are signals,
not accounting. Results are aligned to the input (None during warm-up).
"""
from __future__ import annotations

import math
import statistics


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def true_ranges(high: list[float], low: list[float], close: list[float]) -> list[float]:
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return tr


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> list[float | None]:
    tr = true_ranges(high, low, close)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def rsi(close: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    if len(close) <= n:
        return out
    gains = [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def value(g, l):
        if g == 0 and l == 0:
            return 50.0
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    out[n] = value(ag, al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = value(ag, al)
    return out


def prior_high(high: list[float], n: int, i: int) -> float | None:
    """Highest high of the n bars before index i (excludes bar i)."""
    if i < n:
        return None
    return max(high[i - n:i])


def volume_ratio(volume: list[float], n: int, i: int) -> float | None:
    if i < n:
        return None
    med = statistics.median(volume[i - n:i])
    return volume[i] / med if med > 0 else None


def realized_vol(close: list[float], n: int, i: int) -> float | None:
    if i < n:
        return None
    rets = [math.log(close[j] / close[j - 1]) for j in range(i - n + 1, i + 1) if close[j - 1] > 0]
    return statistics.pstdev(rets) if len(rets) > 1 else None
