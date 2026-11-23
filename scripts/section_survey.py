"""Parse every downloaded 10-K/10-Q and print section-detection coverage.

    uv run python scripts/section_survey.py [--verbose]

Reads filings from the DB (form) and the HTML from data/raw/filings/. No network.
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import select

from analyst.config import get_settings
from analyst.db.models import Company, Filing
from analyst.db.session import session_scope
from analyst.edgar.ingest import document_path
from analyst.retrieval.parse import parse_file

REAL = 2000  # chars: a section this long has real content, not just a pointer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    raw = get_settings().raw_dir
    with session_scope() as s:
        rows = s.execute(
            select(Filing, Company.ticker)
            .join(Company, Company.cik == Filing.cik)
            .where(Filing.form.in_(["10-K", "10-Q"]), Filing.primary_doc.is_not(None))
            .order_by(Company.ticker, Filing.filed_at)
        ).all()
    stats = {"10-K": [0, 0, 0, 0], "10-Q": [0, 0, 0, 0]}
    t0 = time.time()
    for f, ticker in rows:
        path = document_path(raw, f.cik, f.accession, f.primary_doc or "")
        if not path.exists():
            continue
        p = parse_file(path, f.form)
        if f.form == "10-K":
            a, b = p.section("Item 1A"), p.section("Item 7")
        else:
            a, b = p.section("Part II, Item 1A"), p.section("Part I, Item 2")
        st = stats[f.form]
        st[0] += 1
        st[1] += bool(a and b)
        st[2] += bool(a and b and a.length >= REAL and b.length >= REAL)
        st[3] += len(p.text)
        flag = "" if (a and b and a.length >= REAL and b.length >= REAL) else "  <-- short/missing"
        if args.verbose or flag:
            print(
                f"{ticker:5} {f.form:5} {f.accession} len={len(p.text):>8} "
                f"risk={a.length if a else None} mdna={b.length if b else None} "
                f"n={len(p.sections)}{flag}"
            )
    for form, (n, det, real, chars) in stats.items():
        if n:
            print(
                f"{form}: {n} filings | risk factors + MD&A detected: {det}/{n} "
                f"({det / n:.1%}) | both >= {REAL} chars: {real}/{n} ({real / n:.1%}) "
                f"| avg {chars // n:,} chars"
            )
    print(f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
