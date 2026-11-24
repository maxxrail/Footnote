"""Build the committed eval seed corpus from the local SEC cache and dev DB (no network).

    uv run python scripts/build_seed_corpus.py

Writes `analyst/evals/seed_corpus/`. `python -m analyst.evals.seed` loads it into a fresh
database, so CI can run evals without SEC access. Contents, all gzipped JSON:
- `company_tickers.json.gz`: the SEC ticker map, for the seed tickers only.
- `<cik>_submissions.json.gz`: company metadata plus every 10-K/10-Q row (merged from
  the "recent" block and older pages), so ingest never needs another page.
- `<cik>_companyfacts.json.gz`: XBRL facts for the canonical concepts plus the cover-page
  share count, periods ending 2016 or later.
- `filings.json.gz`: already-parsed text and sections of each company's latest 10-K and
  latest 10-Q (the seed chunks and embeds them itself).
"""

from __future__ import annotations

import gzip
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record_fixtures import trim_facts

from analyst.agent.tools import COVER_SHARES
from analyst.config import get_settings
from analyst.db.models import Company, Filing, FilingSection, FilingText
from analyst.db.session import session_scope
from analyst.edgar.client import pad_cik

TICKERS = ("AAPL", "MSFT", "NVDA", "JPM")
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
SINCE = "2016-01-01"
OUT = Path(__file__).resolve().parents[1] / "analyst" / "evals" / "seed_corpus"
COLS = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")


def _default(o: object) -> object:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def write(name: str, data: Any) -> None:
    path = OUT / name
    raw = json.dumps(data, default=_default, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, mtime=0))
    print(f"{name}: {len(raw) / 1024:.0f} KB -> {path.stat().st_size / 1024:.0f} KB gz")


def load(path: Path) -> Any:
    return json.loads(path.read_bytes(), parse_float=Decimal)


def merged_submissions(raw: Path, cik: int) -> dict[str, Any]:
    base = raw / "data.sec.gov" / "submissions"
    sub = load(base / f"CIK{pad_cik(cik)}.json")
    blocks = [sub["filings"]["recent"]]
    # older pages exist in the cache only where Part 1 needed them (banks)
    pages = [base / f["name"] for f in sub["filings"].get("files", [])]
    blocks += [load(p) for p in pages if p.exists()]
    rows = []
    for b in blocks:
        for i, form in enumerate(b["form"]):
            if form in FORMS and b["filingDate"][i] >= SINCE:
                rows.append({c: b[c][i] for c in COLS})
    rows.sort(key=lambda r: (r["filingDate"], r["accessionNumber"]), reverse=True)
    meta = ("cik", "name", "tickers", "sic", "sicDescription", "fiscalYearEnd")
    return {
        **{k: sub.get(k) for k in meta},
        "filings": {"recent": {c: [r[c] for r in rows] for c in COLS}, "files": []},
    }


def trimmed_facts(raw: Path, cik: int) -> dict[str, Any]:
    cf = load(raw / "data.sec.gov" / "api" / "xbrl" / "companyfacts" / f"CIK{pad_cik(cik)}.json")
    out = trim_facts(cf)
    tax, name = COVER_SHARES.split(":")
    body = cf["facts"].get(tax, {}).get(name)
    if body:  # the cover-page share count isn't a canonical concept, but market cap uses it
        units = {u: [e for e in es if e["end"] >= SINCE] for u, es in body["units"].items()}
        out["facts"].setdefault(tax, {})[name] = {"label": body.get("label"), "units": units}
    return out


def main() -> None:
    raw = get_settings().raw_dir
    OUT.mkdir(parents=True, exist_ok=True)
    tickers = load(raw / "www.sec.gov" / "files" / "company_tickers.json")
    write(
        "company_tickers.json.gz",
        {k: v for k, v in tickers.items() if v["ticker"] in TICKERS},
    )
    filings = []
    with session_scope() as s:
        for t in TICKERS:
            co = s.scalar(select(Company).where(Company.ticker == t))
            assert co is not None, t
            write(f"{co.cik}_submissions.json.gz", merged_submissions(raw, co.cik))
            write(f"{co.cik}_companyfacts.json.gz", trimmed_facts(raw, co.cik))
            for form in ("10-K", "10-Q"):
                f = s.scalars(
                    select(Filing)
                    .join(FilingText, FilingText.accession == Filing.accession)
                    .where(Filing.cik == co.cik, Filing.form == form)
                    .order_by(Filing.filed_at.desc())
                    .limit(1)
                ).one()
                text = s.get(FilingText, f.accession)
                assert text is not None
                secs = s.execute(
                    select(FilingSection.section, FilingSection.char_start, FilingSection.char_end)
                    .where(FilingSection.accession == f.accession)
                    .order_by(FilingSection.seq)
                ).all()
                filings.append(
                    {
                        "ticker": t,
                        "accession": f.accession,
                        "form": f.form,
                        "source_sha256": text.source_sha256,
                        "text": text.text,
                        "sections": [list(r) for r in secs],
                    }
                )
    write("filings.json.gz", filings)


if __name__ == "__main__":
    main()
