"""Market prices behind a small protocol.

SEC filings have no market prices, so multiples (P/E, EV/EBITDA...) need another source.
The default reads `data/prices.csv` (`ticker,date,close`), which the user maintains. This
is a real limitation: a price is only as fresh as that file, and every result that uses a
price cites the row (ticker, date, close, file) it came from, so the staleness is visible.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from analyst.metrics.computed import Operand


@dataclass(frozen=True)
class PriceQuote:
    ticker: str
    date: dt.date
    close: Decimal
    source: str  # "data/prices.csv"

    def as_operand(self) -> Operand:
        return Operand(
            label=f"{self.ticker} close {self.date.isoformat()}",
            value=self.close,
            source="price",
            detail=f"{self.source}: {self.ticker},{self.date.isoformat()},{self.close}",
            period_end=self.date,
            unit="USD/shares",
        )


class PriceProvider(Protocol):
    def latest(self, ticker: str, on_or_before: dt.date | None = None) -> PriceQuote | None: ...


class CsvPriceProvider:
    """Prices from a `ticker,date,close` CSV. Bad rows raise: a wrong price is worse than none."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, list[PriceQuote]] | None = None

    def _load(self) -> dict[str, list[PriceQuote]]:
        if self._rows is None:
            rows: dict[str, list[PriceQuote]] = {}
            if self.path.exists():
                with self.path.open(newline="", encoding="utf-8") as f:
                    for i, r in enumerate(csv.DictReader(f), start=2):
                        try:
                            q = PriceQuote(
                                r["ticker"].strip().upper(),
                                dt.date.fromisoformat(r["date"].strip()),
                                Decimal(r["close"].strip()),
                                self.path.as_posix(),
                            )
                        except (KeyError, ValueError, InvalidOperation, AttributeError) as exc:
                            raise ValueError(f"{self.path}:{i}: bad price row {r}") from exc
                        if q.close <= 0:
                            raise ValueError(f"{self.path}:{i}: close must be positive")
                        rows.setdefault(q.ticker, []).append(q)
            for quotes in rows.values():
                quotes.sort(key=lambda q: q.date)
            self._rows = rows
        return self._rows

    def latest(self, ticker: str, on_or_before: dt.date | None = None) -> PriceQuote | None:
        quotes = self._load().get(ticker.upper(), [])
        eligible = [q for q in quotes if on_or_before is None or q.date <= on_or_before]
        return eligible[-1] if eligible else None
