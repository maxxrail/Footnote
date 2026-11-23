"""Ingestion against recorded fixtures (no network) into a throwaway Postgres schema."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from analyst.db.models import Company, Fact, Filing
from analyst.edgar.concepts import get_series
from analyst.edgar.ingest import (
    fact_rows,
    latest_documents,
    read_universe,
    run,
    upsert,
    upsert_filings,
)
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


def test_older_submission_pages_fetched_until_documents_covered(tmp_path):
    """GS/MS-style: 'recent' holds 1 10-K; older 10-Ks are on extra pages."""
    import httpx

    from analyst.edgar.client import SecClient, TokenBucket
    from analyst.edgar.ingest import all_filing_rows

    def block(*rows):
        cols = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")
        return {c: [r[i] for r in rows] for i, c in enumerate(cols)}

    recent = block(
        ("0000000001-26-000001", "2026-02-01", "2025-12-31", "10-K", "k25.htm"),
        *[
            (f"0000000001-26-00001{i}", f"2026-0{i + 4}-01", "", "10-Q", f"q{i}.htm")
            for i in range(4)
        ],
        ("0000000001-26-000099", "2026-09-01", "", "424B2", "p.htm"),
    )
    page1 = block(("0000000001-25-000001", "2025-02-01", "2024-12-31", "10-K", "k24.htm"))
    page2 = block(("0000000001-24-000001", "2024-02-01", "2023-12-31", "10-K", "k23.htm"))
    page3 = block(("0000000001-23-000001", "2023-02-01", "2022-12-31", "10-K", "k22.htm"))
    pages = {
        f"CIK0000000001-submissions-00{i}.json": p for i, p in enumerate([page1, page2, page3], 1)
    }
    requested = []

    def handler(req):
        name = str(req.url).rsplit("/", 1)[1]
        requested.append(name)
        return httpx.Response(200, json=pages[name])

    client = SecClient(
        "t t@example.com",
        tmp_path,
        transport=httpx.MockTransport(handler),
        limiter=TokenBucket(1e6, sleep=lambda s: None),
    )
    sub = {"filings": {"recent": recent, "files": [{"name": n} for n in pages]}}
    filings = all_filing_rows(client, 1, sub)
    assert requested == list(pages)[:2]  # stops once 3 10-Ks are known
    docs = latest_documents(filings)
    assert [f["primary_doc"] for f in docs] == [
        "k25.htm",
        "k24.htm",
        "k23.htm",
        "q3.htm",
        "q2.htm",
        "q1.htm",
        "q0.htm",
    ]


def test_filing_listed_under_two_companies_does_not_flip(session_factory):
    """Real case: JPM's Schedule 13G on ADI is in both companies' submissions, and the
    archive URL embeds the listing company's CIK."""
    from analyst.edgar.client import archive_url

    accn = "0000019617-25-000610"
    shared = {
        "accession": accn,
        "form": "SCHEDULE 13G/A",
        "filed_at": dt.date(2025, 8, 5),
        "period_of_report": None,
        "primary_doc": "primary_doc.xml",
    }
    with session_factory() as s:
        for cik in (19617, 6281):
            upsert(s, Company.__table__, [{"cik": cik, "ticker": str(cik), "name": "n"}], ["cik"])
        for _ in range(2):  # two full runs: JPM then ADI each time
            changed = [
                upsert_filings(
                    s, [{**shared, "cik": cik, "url": archive_url(cik, accn, "primary_doc.xml")}]
                )
                for cik in (19617, 6281)
            ]
        assert changed == [0, 0]
        row = s.get(Filing, accn)
        assert row.cik == 19617 and "/data/19617/" in row.url


def test_quarterly_series_derives_cited_q4_from_fixtures(tmp_path, session_factory, sec_client):
    from analyst.metrics.quarters import DerivedPoint, quarterly_series

    run(TICKERS, sec_client(), tmp_path / "raw", session_factory=session_factory)
    with session_factory() as s:
        pts = quarterly_series(s, 789019, "revenue")  # MSFT, fiscal year ends June 30
        derived = [p for p in pts if isinstance(p, DerivedPoint)]
        assert derived, "expected derived Q4s"
        for d in derived:
            fy, *qs = d.inputs
            assert d.value == fy.value - sum(q.value for q in qs)
            assert d.fiscal_quarter == 4 and d.period_end.month == 6
            for i in d.inputs:  # every input resolves to a stored fact with that value
                assert s.get(Fact, i.fact_id).value == i.value
        labels = [(p.fiscal_year, p.fiscal_quarter) for p in pts]
        assert len(labels) == len(set(labels))
        assert "derived: FY - (Q1 + Q2 + Q3)" in show(s, "MSFT", "revenue", "Q")
        assert not [
            p for p in quarterly_series(s, 789019, "eps_diluted") if isinstance(p, DerivedPoint)
        ]
