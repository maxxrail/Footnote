"""Parsing of the price refresh script (recorded-shape JSON, no network)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from analyst.metrics.prices import CsvPriceProvider
from analyst.metrics.yahoo import closes, universe, write_csv

# Two finished days and today's live bar (16:00 ET close not reached yet). EST = UTC-5.
TODAY = 1795530600  # 2026-11-24 09:30 ET
DAY1 = TODAY - 4 * 86400  # 2026-11-20 09:30 ET (Friday)
DAY2 = TODAY - 86400  # 2026-11-23 09:30 ET (close missing)
SESSION_END = 1795554000  # 2026-11-24 16:00 ET
PAYLOAD = {
    "chart": {
        "result": [
            {
                "meta": {
                    "gmtoffset": -18000,
                    "currentTradingPeriod": {"regular": {"start": TODAY, "end": SESSION_END}},
                },
                "timestamp": [DAY1, DAY2, TODAY],
                "indicators": {"quote": [{"close": [348.1234, None, 352.18]}]},
            }
        ]
    }
}


def test_closes_drop_missing_and_todays_live_bar() -> None:
    got = closes(PAYLOAD, now=SESSION_END - 60)
    assert got == [(dt.date(2026, 11, 20), Decimal("348.12"))]


def test_closes_keep_today_after_the_close() -> None:
    got = closes(PAYLOAD, now=SESSION_END + 60)
    assert got[-1] == (dt.date(2026, 11, 24), Decimal("352.18"))


def test_written_csv_reads_back_in_the_provider(tmp_path: Path) -> None:
    p = tmp_path / "prices.csv"
    n = write_csv(
        p,
        [
            ("MSFT", dt.date(2026, 11, 21), Decimal("500.10")),
            ("AAPL", dt.date(2026, 11, 20), Decimal("348.12")),
        ],
    )
    assert n == 2
    assert p.read_text(encoding="utf-8").splitlines()[0] == "ticker,date,close"
    q = CsvPriceProvider(p).latest("AAPL")
    assert q is not None and q.close == Decimal("348.12")


def test_universe_reads_tickers(tmp_path: Path) -> None:
    u = tmp_path / "u.csv"
    u.write_text("ticker,name,peer_group\naapl,Apple,x\nMSFT,Microsoft,x\n", encoding="utf-8")
    assert universe(u) == ["AAPL", "MSFT"]
