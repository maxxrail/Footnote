"""Ingest companies, filings and XBRL facts from SEC EDGAR into Postgres.

    uv run python -m analyst.edgar.ingest --tickers AAPL MSFT NVDA
    uv run python -m analyst.edgar.ingest --universe data/universe.csv

Idempotent: every table is upserted, and an upsert only touches a row whose values
actually changed, so a second run reports 0 changed rows. Raw SEC responses are cached on
disk under `data/raw/`, so a second run also makes 0 SEC requests (use `--refresh` to
re-download the mutable JSON endpoints; filing documents never change and stay cached).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import FromClause, Table, literal, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analyst.config import get_settings
from analyst.db.models import Company, Fact, Filing
from analyst.db.session import session_scope
from analyst.edgar.client import SecClient, archive_url

log = logging.getLogger("analyst.edgar.ingest")

TAXONOMIES = ("us-gaap", "dei")
DOCS_TO_KEEP = {"10-K": 3, "10-Q": 4}
CHUNK = 2000


@dataclass
class IngestCounts:
    companies: int = 0
    filings: int = 0
    facts: int = 0
    documents_downloaded: int = 0

    def add(self, other: IngestCounts) -> None:
        self.companies += other.companies
        self.filings += other.filings
        self.facts += other.facts
        self.documents_downloaded += other.documents_downloaded


@dataclass
class IngestReport:
    per_company: dict[str, IngestCounts] = field(default_factory=dict)
    total: IngestCounts = field(default_factory=IngestCounts)
    network_requests: int = 0
    cache_hits: int = 0


# -- upsert ------------------------------------------------------------------------------


def upsert(
    session: Session,
    model_table: FromClause,
    rows: Sequence[dict[str, Any]],
    key: Sequence[str],
    update: bool = True,
) -> int:
    """Insert rows; on key conflict update only if a value differs. Returns rows changed."""
    table = cast(Table, model_table)
    changed = 0
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        stmt = insert(table).values(list(chunk))
        cols = [c for c in chunk[0] if c not in key]
        if update and cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(key),
                set_={c: stmt.excluded[c] for c in cols},
                where=or_(*[table.c[c].is_distinct_from(stmt.excluded[c]) for c in cols]),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key))
        changed += len(session.execute(stmt.returning(literal(1))).all())
    return changed


# -- parsing -----------------------------------------------------------------------------


def _date(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def company_row(cik: int, ticker: str, sub: dict[str, Any]) -> dict[str, Any]:
    return {
        "cik": cik,
        "ticker": ticker.upper(),
        "name": sub.get("name") or ticker,
        "sic": sub.get("sic") or None,
        "sic_description": sub.get("sicDescription") or None,
        "fiscal_year_end": sub.get("fiscalYearEnd") or None,
    }


def filing_rows(cik: int, sub: dict[str, Any]) -> list[dict[str, Any]]:
    recent = sub.get("filings", {}).get("recent", {})
    out = []
    for i, accn in enumerate(recent.get("accessionNumber", [])):
        doc = recent["primaryDocument"][i] or None
        out.append(
            {
                "accession": accn,
                "cik": cik,
                "form": recent["form"][i],
                "filed_at": _date(recent["filingDate"][i]),
                "period_of_report": _date(recent["reportDate"][i]),
                "primary_doc": doc,
                "url": archive_url(cik, accn, doc or ""),
            }
        )
    return out


def fact_rows(cik: int, companyfacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten companyfacts JSON into fact rows, one per (concept, unit, period, accession)."""
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for taxonomy in TAXONOMIES:
        for name, body in companyfacts.get("facts", {}).get(taxonomy, {}).items():
            concept = f"{taxonomy}:{name}"
            for unit, entries in body.get("units", {}).items():
                for e in entries:
                    row = {
                        "cik": cik,
                        "concept": concept,
                        "label": body.get("label"),
                        "unit": unit,
                        "value": Decimal(str(e["val"])),
                        "period_start": _date(e.get("start")),
                        "period_end": _date(e["end"]),
                        "fiscal_year": e.get("fy"),
                        "fiscal_period": e.get("fp"),
                        "form": e["form"],
                        "accession": e["accn"],
                        "frame": e.get("frame"),
                        "_filed": e.get("filed"),
                    }
                    k = (concept, unit, row["period_start"], row["period_end"], e["accn"])
                    # SEC occasionally lists the same fact twice in one filing; keep the one
                    # SEC assigned a frame to, so the choice is deterministic across runs.
                    if k not in rows or (row["frame"] and not rows[k]["frame"]):
                        rows[k] = row
    return list(rows.values())


def stub_filings(cik: int, facts: Iterable[dict[str, Any]], known: set[str]) -> list[dict]:
    """Filings referenced by facts but missing from the submissions 'recent' list (older
    filings). Built from the fact's own form and filed date so every fact has a filing."""
    stubs: dict[str, dict[str, Any]] = {}
    for f in facts:
        accn = f["accession"]
        if accn in known or accn in stubs:
            continue
        stubs[accn] = {
            "accession": accn,
            "cik": cik,
            "form": f["form"],
            "filed_at": _date(f["_filed"]),
            "period_of_report": None,
            "primary_doc": None,
            "url": archive_url(cik, accn, ""),
        }
    return list(stubs.values())


def latest_documents(filings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last 3 10-Ks and last 4 10-Qs (originals, not amendments) with a primary doc."""
    picked: list[dict[str, Any]] = []
    for form, n in DOCS_TO_KEEP.items():
        matching = [f for f in filings if f["form"] == form and f["primary_doc"]]
        matching.sort(key=lambda f: (f["filed_at"], f["accession"]), reverse=True)
        picked.extend(matching[:n])
    return picked


def document_path(raw_dir: Path, cik: int, accession: str, doc: str) -> Path:
    return raw_dir / "filings" / str(cik) / accession / doc


# -- orchestration -----------------------------------------------------------------------


def ingest_company(
    session: Session,
    client: SecClient,
    ticker: str,
    cik: int,
    raw_dir: Path,
    refresh: bool = False,
    download_docs: bool = True,
) -> IngestCounts:
    counts = IngestCounts()
    sub = client.submissions(cik, refresh=refresh)
    counts.companies = upsert(session, Company.__table__, [company_row(cik, ticker, sub)], ["cik"])

    filings = filing_rows(cik, sub)
    facts = fact_rows(cik, client.companyfacts(cik, refresh=refresh))
    stubs = stub_filings(cik, facts, {f["accession"] for f in filings})
    counts.filings = upsert(session, Filing.__table__, filings, ["accession"])
    # Stubs never overwrite a full row that a later submissions page may have written.
    counts.filings += upsert(session, Filing.__table__, stubs, ["accession"], update=False)

    for f in facts:
        f.pop("_filed")
    counts.facts = upsert(
        session,
        Fact.__table__,
        facts,
        ["cik", "concept", "unit", "period_start", "period_end", "accession"],
    )

    if download_docs:
        for f in latest_documents(filings):
            path = document_path(raw_dir, cik, f["accession"], f["primary_doc"])
            if not path.exists():
                counts.documents_downloaded += 1
            client.get_bytes(f["url"], cache_path=path)
    return counts


def read_universe(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row["ticker"].strip().upper() for row in csv.DictReader(fh) if row["ticker"]]


def run(
    tickers: Sequence[str],
    client: SecClient,
    raw_dir: Path,
    session_factory: Any = session_scope,
    refresh: bool = False,
    download_docs: bool = True,
) -> IngestReport:
    report = IngestReport()
    cik_map = client.tickers(refresh=refresh)
    missing = [t for t in tickers if t.upper() not in cik_map]
    if missing:
        raise SystemExit(f"unknown tickers (not in SEC company_tickers.json): {missing}")
    for ticker in tickers:
        cik = cik_map[ticker.upper()]
        with session_factory() as session:  # one transaction per company
            counts = ingest_company(session, client, ticker, cik, raw_dir, refresh, download_docs)
        log.info(
            "%-6s cik=%-8d companies=%d filings=%d facts=%d docs=%d",
            ticker,
            cik,
            counts.companies,
            counts.filings,
            counts.facts,
            counts.documents_downloaded,
        )
        report.per_company[ticker.upper()] = counts
        report.total.add(counts)
    report.network_requests = client.stats.network
    report.cache_hits = client.stats.cache_hits
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analyst.edgar.ingest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--tickers", nargs="+", metavar="TICKER")
    src.add_argument("--universe", type=Path, metavar="CSV", help="CSV with a 'ticker' column")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download SEC JSON instead of using the disk cache",
    )
    parser.add_argument(
        "--no-docs", action="store_true", help="skip downloading 10-K/10-Q primary documents"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = get_settings()
    tickers = args.tickers or read_universe(args.universe)
    with SecClient(settings.sec_user_agent, settings.raw_dir) as client:
        report = run(
            [t.upper() for t in tickers],
            client,
            settings.raw_dir,
            refresh=args.refresh,
            download_docs=not args.no_docs,
        )
    t = report.total
    print(
        f"\n{len(report.per_company)} companies | rows changed: companies={t.companies} "
        f"filings={t.filings} facts={t.facts} | documents downloaded={t.documents_downloaded}"
        f" | SEC requests={report.network_requests} cache hits={report.cache_hits}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
