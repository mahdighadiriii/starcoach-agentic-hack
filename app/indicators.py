def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) <= n:
        return None
    gains = losses = 0.0
    for prev, cur in zip(values[-n - 1:-1], values[-n:]):
        diff = cur - prev
        gains += max(diff, 0)
        losses += max(-diff, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def summarize(candles: list[dict]) -> dict:
    closes = [c["c"] for c in candles]
    if not closes:
        return {}
    return {
        "last_close": closes[-1],
        "sma_12": sma(closes, 12),
        "sma_26": sma(closes, 26),
        "rsi_14": rsi(closes),
        "high_48h": max(c["h"] for c in candles),
        "low_48h": min(c["l"] for c in candles),
    }
