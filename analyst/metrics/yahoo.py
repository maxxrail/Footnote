"""Refresh data/prices.csv with daily closes for the universe, from Yahoo Finance's chart API.

    uv run python scripts/fetch_prices.py              # last month of closes, all 25 tickers
    uv run python scripts/fetch_prices.py --range 1y   # (same as python -m analyst.metrics.yahoo)

SEC filings have no market prices, so this is the one non-SEC source. Only completed trading
days are kept (a bar for today is dropped while the market is open). Raw responses are saved
under data/raw/prices/ so a price can be traced to what was downloaded. One request every
0.5 s; no key and no personal data are sent.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={range}&interval=1d"
USER_AGENT = "Mozilla/5.0 (analyst-agent price refresh)"


def closes(payload: dict[str, Any], now: float | None = None) -> list[tuple[dt.date, Decimal]]:
    """(exchange-local date, close) for completed days in one chart response."""
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    offset = dt.timedelta(seconds=int(meta.get("gmtoffset", 0)))
    stamps = result.get("timestamp") or []
    raw = result["indicators"]["quote"][0]["close"]
    session_end = meta.get("currentTradingPeriod", {}).get("regular", {}).get("end")
    now = time.time() if now is None else now
    out = []
    for ts, close in zip(stamps, raw, strict=True):
        if close is None:
            continue
        day = (dt.datetime.fromtimestamp(ts, dt.UTC) + offset).date()
        if session_end is not None and ts < session_end <= ts + 86400 and now < session_end:
            continue  # today's bar, market still open: not a close yet
        out.append((day, Decimal(str(close)).quantize(Decimal("0.01"))))
    return out


def universe(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r["ticker"].strip().upper() for r in csv.DictReader(f)]


def write_csv(path: Path, rows: Iterable[tuple[str, dt.date, Decimal]]) -> int:
    rows = sorted(rows)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ticker", "date", "close"])
        for t, d, c in rows:
            w.writerow([t, d.isoformat(), f"{c}"])
    return len(rows)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - network
    ap = argparse.ArgumentParser(prog="python scripts/fetch_prices.py")
    ap.add_argument("--range", default="1mo")
    ap.add_argument("--universe", type=Path, default=Path("data/universe.csv"))
    ap.add_argument("--out", type=Path, default=Path("data/prices.csv"))
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw/prices"))
    args = ap.parse_args(argv)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, dt.date, Decimal]] = []
    failed = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20) as client:
        for t in universe(args.universe):
            r = client.get(URL.format(ticker=t, range=args.range))
            if r.status_code != 200:
                failed.append(f"{t} (HTTP {r.status_code})")
            else:
                payload = r.json()
                (args.raw_dir / f"{t}.json").write_text(json.dumps(payload), encoding="utf-8")
                got = closes(payload)
                rows += [(t, d, c) for d, c in got]
                print(
                    f"{t}: {len(got)} closes, latest {got[-1][0]} {got[-1][1]}"
                    if got
                    else f"{t}: none"
                )
            time.sleep(0.5)
    n = write_csv(args.out, rows)
    print(f"wrote {n} rows to {args.out}" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
