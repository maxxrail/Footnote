"""Build the search index: parse filings → filing_texts / filing_sections / chunks → embed.

    uv run python -m analyst.retrieval.index                 # every downloaded 10-K/10-Q
    uv run python -m analyst.retrieval.index --tickers NVDA  # just one company
    uv run python -m analyst.retrieval.index --no-embed      # parse + chunk only

Idempotent: a filing whose stored text came from the same HTML (sha256) and the same
PARSER_VERSION is skipped, so its spans never move. Only chunks without an embedding
from the current model are embedded. A second run changes 0 rows. No network: the HTML
comes from the Part 1 disk cache (`data/raw/filings/`).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from analyst.config import get_settings
from analyst.db.models import Chunk, Company, Filing, FilingSection, FilingText
from analyst.db.session import session_scope
from analyst.edgar.ingest import document_path
from analyst.retrieval.chunk import MAX_TOKENS, OVERLAP_TOKENS, chunk_filing
from analyst.retrieval.embed import Embedder, default_embedder, embed_pending
from analyst.retrieval.parse import PARSER_VERSION, parse_filing, section_title

log = logging.getLogger("analyst.retrieval.index")

FORMS = ("10-K", "10-Q")

# SEC conformed names carry state suffixes: "BANK OF AMERICA CORP /DE/", "US BANCORP \DE\"
_STATE_SUFFIX = re.compile(r"\s*[/\\][A-Z]{2}[/\\]?\s*$")


def company_label(name: str) -> str:
    return _STATE_SUFFIX.sub("", name).strip()


def chunk_context(company: Company, filing: Filing, section: str) -> str:
    """Header indexed and embedded with every chunk (never cited):
    'Texas Instruments Inc (TXN), 10-K filed 2025-02-14. Item 1: Business.'"""
    title = section_title(section)
    where = section if title == section else f"{section}: {title}"
    return (
        f"{company_label(company.name)} ({company.ticker}), {filing.form} filed "
        f"{filing.filed_at.isoformat()}. {where}."
    )


@dataclass
class IndexReport:
    filings_seen: int = 0
    filings_parsed: int = 0
    chunks_written: int = 0
    chunks_embedded: int = 0


def index_filing(
    session: Session,
    filing: Filing,
    html: bytes,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> int | None:
    """(Re)build one filing's text, sections and chunks. Returns chunks written, or None
    if the stored text is already current."""
    sha = hashlib.sha256(html).hexdigest()
    existing = session.get(FilingText, filing.accession)
    if (
        existing is not None
        and existing.parser_version == PARSER_VERSION
        and existing.source_sha256 == sha
    ):
        return None
    parsed = parse_filing(html, filing.form)
    if existing is not None:
        # cascades to sections and chunks; the text changes, so old spans must go
        session.delete(existing)
        session.flush()
    session.add(
        FilingText(
            accession=filing.accession,
            text=parsed.text,
            parser_version=PARSER_VERSION,
            source_sha256=sha,
        )
    )
    session.flush()
    if parsed.sections:
        session.execute(
            insert(FilingSection),
            [
                {
                    "accession": filing.accession,
                    "seq": i,
                    "section": s.code,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                }
                for i, s in enumerate(parsed.sections)
            ],
        )
    chunks = chunk_filing(parsed, max_tokens, overlap_tokens)
    company = session.get(Company, filing.cik)
    if company is None:
        raise LookupError(f"filing {filing.accession}: no company row for CIK {filing.cik}")
    if chunks:
        session.execute(
            insert(Chunk),
            [
                {
                    "accession": filing.accession,
                    "cik": filing.cik,
                    "section": c.section,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "text": c.text,
                    "context": chunk_context(company, filing, c.section),
                }
                for c in chunks
            ],
        )
    return len(chunks)


def filings_to_index(session: Session, tickers: Sequence[str] | None = None) -> list[Filing]:
    q = (
        select(Filing)
        .join(Company, Company.cik == Filing.cik)
        .where(Filing.form.in_(FORMS), Filing.primary_doc.is_not(None))
        .order_by(Company.ticker, Filing.filed_at, Filing.accession)
    )
    if tickers:
        q = q.where(Company.ticker.in_([t.upper() for t in tickers]))
    return list(session.scalars(q))


def run(
    raw_dir: Path,
    session_factory: Callable[[], AbstractContextManager[Session]] = session_scope,
    tickers: Sequence[str] | None = None,
    embedder: Embedder | None = None,
    embed: bool = True,
    batch_size: int = 64,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> IndexReport:
    report = IndexReport()
    with session_factory() as s:
        filings = filings_to_index(s, tickers)
        s.expunge_all()
    for f in filings:
        path = document_path(raw_dir, f.cik, f.accession, f.primary_doc or "")
        if not path.exists():
            # Part 1 downloads only the latest 3 10-Ks and 4 10-Qs per company
            continue
        report.filings_seen += 1
        with session_factory() as s:  # one transaction per filing
            n = index_filing(s, f, path.read_bytes(), max_tokens, overlap_tokens)
        if n is not None:
            report.filings_parsed += 1
            report.chunks_written += n
            log.info("parsed %s %s: %d chunks", f.accession, f.form, n)
    if embed:
        emb = embedder or default_embedder()
        t0 = time.time()

        def progress(done: int, total: int) -> None:
            if done % (batch_size * 10) == 0 or done == total:
                rate = done / max(time.time() - t0, 1e-9)
                log.info("embedded %d/%d chunks (%.1f/s)", done, total, rate)

        with session_factory() as s:
            report.chunks_embedded = embed_pending(s, emb, batch_size, progress=progress)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analyst.retrieval.index",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tickers", nargs="+", metavar="TICKER")
    parser.add_argument("--no-embed", action="store_true", help="parse and chunk only")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_TOKENS,
        help="chunk size; only applies to filings not yet indexed (use a fresh DB to compare)",
    )
    parser.add_argument("--overlap-tokens", type=int, default=OVERLAP_TOKENS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    report = run(
        get_settings().raw_dir,
        tickers=args.tickers,
        embed=not args.no_embed,
        batch_size=args.batch_size,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    print(
        f"filings: {report.filings_seen} on disk, {report.filings_parsed} parsed "
        f"| chunks written: {report.chunks_written} | chunks embedded: {report.chunks_embedded}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
