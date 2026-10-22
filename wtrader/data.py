"""Historical OHLCV download and CSV storage with provenance.

Two sources:
- wallex: GET /v1/udf/history (official). The authoritative source for evaluation.
- binance_proxy: data-api.binance.vision public klines. Used ONLY as a labeled proxy
  when Wallex is unreachable from the research host. Proxy results are not Wallex fills.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path

import httpx

from .models import Candle, dec

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
BINANCE_INTERVAL = {900: "15m", 3600: "1h"}
WALLEX_RESOLUTION = {900: "15", 3600: "60"}


def fetch_binance(symbol: str, interval_s: int, start: int, end: int, client: httpx.Client) -> list[Candle]:
    out: list[Candle] = []
    cursor = start * 1000
    while cursor < end * 1000:
        r = client.get(BINANCE_URL, params={"symbol": symbol, "interval": BINANCE_INTERVAL[interval_s],
                                             "startTime": cursor, "endTime": end * 1000 - 1, "limit": 1000})
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for k in rows:
            out.append(Candle(int(k[0]) // 1000, dec(k[1]), dec(k[2]), dec(k[3]), dec(k[4]), dec(k[5])))
        cursor = int(rows[-1][0]) + interval_s * 1000
        time.sleep(0.2)
    return out


def fetch_wallex(symbol: str, interval_s: int, start: int, end: int, client: httpx.Client,
                 base_url: str = "https://api.wallex.ir", chunk_bars: int = 500) -> list[Candle]:
    out: dict[int, Candle] = {}
    cursor = start
    while cursor < end:
        to = min(end, cursor + chunk_bars * interval_s)
        r = client.get(f"{base_url}/v1/udf/history",
                       params={"symbol": symbol, "resolution": WALLEX_RESOLUTION[interval_s], "from": cursor, "to": to})
        r.raise_for_status()
        body = r.json()
        if body.get("s") == "ok":
            for t, o, h, l, c, v in zip(body["t"], body["o"], body["h"], body["l"], body["c"], body["v"]):
                t = int(t)
                if t % interval_s:   # never trust requested resolution: drop off-grid bars
                    continue
                out[t] = Candle(t, dec(o), dec(h), dec(l), dec(c), dec(v))
        cursor = to
        time.sleep(0.3)
    return [out[k] for k in sorted(out)]


def save_csv(path: Path, candles: list[Candle], provenance: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.open_time, c.open, c.high, c.low, c.close, c.volume])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".meta.json").write_text(json.dumps({**provenance, "rows": len(candles), "sha256": digest}, indent=2))


def load_csv(path: Path) -> list[Candle]:
    with path.open() as f:
        r = csv.DictReader(f)
        return [Candle(int(row["open_time"]), dec(row["open"]), dec(row["high"]), dec(row["low"]),
                       dec(row["close"]), dec(row["volume"])) for row in r]


def dataset_path(root: Path, source: str, symbol: str, interval_s: int) -> Path:
    return root / source / f"{symbol}_{interval_s}.csv"


def download(symbols: list[str], source: str, days: int, root: Path, end: int | None = None) -> list[Path]:
    end = end or (int(time.time()) // 3600 * 3600)
    start = end - days * 86400
    paths = []
    with httpx.Client(timeout=30) as client:
        for sym in symbols:
            for interval in (900, 3600):
                fetch = fetch_binance if source == "binance_proxy" else fetch_wallex
                candles = fetch(sym, interval, start, end, client)
                p = dataset_path(root, source, sym, interval)
                save_csv(p, candles, {"source": source, "symbol": sym, "interval_s": interval,
                                      "start": start, "end": end, "proxy": source != "wallex"})
                paths.append(p)
    return paths


def resample(candles: list[Candle], from_s: int, to_s: int) -> list[Candle]:
    """Aggregate to a coarser grid, keeping only complete, UTC-aligned buckets."""
    if to_s == from_s:
        return candles
    need = to_s // from_s
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        buckets.setdefault(c.open_time - c.open_time % to_s, []).append(c)
    out = []
    for t in sorted(buckets):
        b = sorted(buckets[t], key=lambda x: x.open_time)
        if len(b) != need:
            continue
        out.append(Candle(t, b[0].open, max(x.high for x in b), min(x.low for x in b), b[-1].close,
                          sum((x.volume for x in b), b[0].volume - b[0].volume)))
    return out


def load_frames(root: Path, source: str, symbol: str, sig_s: int, ctx_s: int) -> tuple[list[Candle], list[Candle]]:
    """Load signal and context frames, resampling from the finest stored file that divides them."""
    def frame(s):
        base = 900 if s % 3600 else 3600
        return resample(load_csv(dataset_path(root, source, symbol, base)), base, s)
    return frame(sig_s), frame(ctx_s)
