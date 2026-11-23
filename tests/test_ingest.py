"""Ingestion against recorded fixtures (no network) into a throwaway Postgres schema."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from analyst.db.models import Company, Fact, Filing
from analyst.edgar.concepts import get_series
from analyst.edgar.ingest import fact_rows, latest_documents, read_universe, run, upsert
from analyst.edgar.show import show
from tests.conftest import load_fixture

TICKERS = ["AAPL", "MSFT"]


def counts(session_factory) -> tuple[int, int, int]:
    with session_factory() as s:
        return (
            s.scalar(select(func.count()).select_from(Company)),
            s.scalar(select(func.count()).select_from(Filing)),
            s.scalar(select(func.count()).select_from(Fact)),
        )


def test_ingest_twice_is_idempotent(tmp_path, session_factory, sec_client, fake_sec):
    raw = tmp_path / "raw"
    first = run(TICKERS, sec_client(), raw, session_factory=session_factory)
    after_first = counts(session_factory)
    assert after_first[0] == 2 and after_first[1] > 0 and after_first[2] > 0
    assert first.total.facts == after_first[2]
    assert first.network_requests == len(fake_sec.requests) > 0

    fake_sec.requests.clear()
    second = run(TICKERS, sec_client(), raw, session_factory=session_factory)
    assert counts(session_factory) == after_first  # same row counts
    assert (second.total.companies, second.total.filings, second.total.facts) == (0, 0, 0)
    assert second.total.documents_downloaded == 0
    assert second.network_requests == 0 and fake_sec.requests == []  # all cache hits
    assert second.cache_hits == first.network_requests


def test_reingest_with_refresh_changes_nothing(tmp_path, session_factory, sec_client, fake_sec):
    raw = tmp_path / "raw"
    run(TICKERS, sec_client(), raw, session_factory=session_factory)
    again = run(TICKERS, sec_client(), raw, session_factory=session_factory, refresh=True)
    assert again.network_requests > 0  # JSON endpoints re-downloaded
    assert (again.total.companies, again.total.filings, again.total.facts) == (0, 0, 0)


def test_upsert_updates_only_changed_rows(session_factory):
    row = {
        "cik": 1,
        "ticker": "X",
        "name": "Old",
        "sic": None,
        "sic_description": None,
        "fiscal_year_end": "1231",
    }
    with session_factory() as s:
        assert upsert(s, Company.__table__, [row], ["cik"]) == 1
        assert upsert(s, Company.__table__, [row], ["cik"]) == 0
        assert upsert(s, Company.__table__, [{**row, "name": "New"}], ["cik"]) == 1
    with session_factory() as s:
        assert s.get(Company, 1).name == "New"


def test_instant_facts_are_not_duplicated(session_factory):
    """period_start is NULL for balance-sheet facts; the unique key must still hold."""
    with session_factory() as s:
        upsert(s, Company.__table__, [{"cik": 1, "ticker": "X", "name": "X"}], ["cik"])
        upsert(
            s,
            Filing.__table__,
            [
                {
                    "accession": "0000000001-24-000001",
                    "cik": 1,
                    "form": "10-K",
                    "filed_at": dt.date(2024, 2, 1),
                    "url": "u",
                }
            ],
            ["accession"],
        )
        fact = {
            "cik": 1,
            "concept": "us-gaap:Assets",
            "unit": "USD",
            "value": Decimal(5),
            "period_start": None,
            "period_end": dt.date(2023, 12, 31),
            "form": "10-K",
            "accession": "0000000001-24-000001",
        }
        key = ["cik", "concept", "unit", "period_start", "period_end", "accession"]
        assert upsert(s, Fact.__table__, [fact], key) == 1
        assert upsert(s, Fact.__table__, [fact], key) == 0
    assert counts(session_factory)[2] == 1


def test_every_fact_has_a_filing_and_documents_were_saved(tmp_path, session_factory, sec_client):
    raw = tmp_path / "raw"
    report = run(TICKERS, sec_client(), raw, session_factory=session_factory)
    with session_factory() as s:
        orphans = s.scalar(
            select(func.count())
            .select_from(Fact)
            .outerjoin(Filing, Filing.accession == Fact.accession)
            .where(Filing.accession.is_(None))
        )
        assert orphans == 0
    docs = list((raw / "filings").rglob("*.htm"))
    assert report.total.documents_downloaded == len(docs) == 2 * (3 + 4)


def test_get_series_from_ingested_fixtures(tmp_path, session_factory, sec_client):
    run(TICKERS, sec_client(), tmp_path / "raw", session_factory=session_factory)
    with session_factory() as s:
        pts = get_series(s, 320193, "revenue", "FY")
        assert len(pts) >= 5
        assert all(p.accession and p.fact_id for p in pts)
        assert all(p.form in ("10-K", "10-K/A") for p in pts)
        years = [p.fiscal_year for p in pts]
        assert years == sorted(set(years))  # one value per year
        for p in pts:  # provenance resolves to a real row with the same value
            f = s.get(Fact, p.fact_id)
            assert (f.value, f.accession, f.concept) == (p.value, p.accession, p.concept)
        out = show(s, "aapl", "revenue")
        assert "Apple" in out and pts[-1].accession in out


def test_show_unknown_ticker_refuses(session_factory):
    with session_factory() as s, pytest.raises(SystemExit):
        show(s, "ZZZZ", "revenue")


def test_fact_rows_parse_decimals_exactly():
    cf = {
        "facts": {
            "us-gaap": {
                "EarningsPerShareDiluted": {
                    "label": "EPS",
                    "units": {
                        "USD/shares": [
                            {
                                "start": "2023-10-01",
                                "end": "2024-09-28",
                                "val": Decimal("6.08"),
                                "accn": "a",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ]
                    },
                }
            }
        }
    }
    [row] = fact_rows(1, cf)
    assert row["value"] == Decimal("6.08") and row["concept"] == "us-gaap:EarningsPerShareDiluted"


def test_latest_documents_picks_3_10k_and_4_10q():
    sub = load_fixture("aapl_submissions.json")["filings"]["recent"]
    filings = [
        {"accession": a, "form": f, "filed_at": d, "primary_doc": p}
        for a, f, d, p in zip(
            sub["accessionNumber"],
            sub["form"],
            sub["filingDate"],
            sub["primaryDocument"],
            strict=True,
        )
    ]
    picked = latest_documents(filings)
    assert [f["form"] for f in picked] == ["10-K"] * 3 + ["10-Q"] * 4
    tenks = [f["filed_at"] for f in picked[:3]]
    assert tenks == sorted(tenks, reverse=True)
    newest_10k = max(f["filed_at"] for f in filings if f["form"] == "10-K")
    assert tenks[0] == newest_10k


def test_universe_csv_has_three_peer_groups_of_about_25():
    import csv
    from pathlib import Path

    path = Path(__file__).parents[1] / "data" / "universe.csv"
    tickers = read_universe(path)
    with path.open() as fh:
        groups = {r["peer_group"] for r in csv.DictReader(fh)}
    assert 20 <= len(tickers) <= 30 and len(set(tickers)) == len(tickers)
    assert len(groups) == 3
