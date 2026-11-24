"""Load the fixed eval corpus into the database, from committed fixtures (no SEC calls).

    uv run python -m analyst.evals.seed                 # embedder: bge if installed, else hash
    uv run python -m analyst.evals.seed --embedder hash

The corpus (`analyst/evals/seed_corpus/`, built by `scripts/build_seed_corpus.py`) holds
AAPL, MSFT, NVDA and JPM: XBRL facts since 2016, their 10-K/10-Q filing lists, and the
parsed text of each one's latest 10-K and 10-Q. The XBRL goes through the normal ingest
code with an SEC client that reads only the fixtures (any network request fails). The
filings go through the same chunker as the indexer, then get embedded. Idempotent: a
second run changes nothing.

Embedder: the default `auto` uses the local BGE model if sentence-transformers is
installed (`--extra ml`), else the hashing embedder. The runner uses whichever the
stored chunks were embedded with.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from analyst.db.models import Filing, FilingText
from analyst.db.session import session_scope
from analyst.edgar import ingest
from analyst.edgar.client import SecClient, TokenBucket
from analyst.retrieval.embed import Embedder, HashEmbedder, embed_pending
from analyst.retrieval.index import store_parsed
from analyst.retrieval.parse import ParsedFiling, Section

CORPUS = Path(__file__).parent / "seed_corpus"
TICKERS = ("AAPL", "MSFT", "NVDA", "JPM")
SEED_UA = "analyst-agent-eval-seed seed@example.com"
SessionFactory = Callable[[], AbstractContextManager[Session]]
log = logging.getLogger(__name__)


def _load(name: str) -> Any:
    return json.loads(gzip.decompress((CORPUS / name).read_bytes()), parse_float=Decimal)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"the eval seed must not reach the network: {request.url}")


def _fixture_cache(root: Path) -> SecClient:
    """An SEC client whose disk cache holds the fixtures and whose network is cut off."""
    client = SecClient(
        SEED_UA,
        root,
        transport=httpx.MockTransport(_refuse),
        sleep=lambda s: None,
        limiter=TokenBucket(1e6, sleep=lambda s: None),
    )
    tickers = _load("company_tickers.json.gz")
    put = lambda url, data: _write(client.cache_path(url), data)  # noqa: E731
    put("https://www.sec.gov/files/company_tickers.json", tickers)
    for row in tickers.values():
        cik = int(row["cik_str"])
        put(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            _load(f"{cik}_submissions.json.gz"))  # fmt: skip
        put(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            _load(f"{cik}_companyfacts.json.gz"))  # fmt: skip
    return client


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, default=str), encoding="utf-8")


@dataclass
class SeedReport:
    facts: int
    filings: int
    filings_stored: int
    chunks: int
    embedded: int


def pick_embedder(name: str) -> Embedder:
    if name == "hash":
        return HashEmbedder()
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        if name == "bge":
            raise SystemExit("--embedder bge needs `uv sync --extra ml`") from None
        log.warning("sentence-transformers not installed: using the hashing embedder")
        return HashEmbedder()
    from analyst.retrieval.embed import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder()


def seed(
    session_factory: SessionFactory = session_scope,
    embedder: Embedder | None = None,
    tickers: Sequence[str] = TICKERS,
) -> SeedReport:
    with tempfile.TemporaryDirectory() as tmp:
        client = _fixture_cache(Path(tmp))
        with client:
            report = ingest.run(
                list(tickers), client, Path(tmp), session_factory, download_docs=False
            )
    stored = chunks = 0
    for f in _load("filings.json.gz"):
        if f["ticker"] not in tickers:
            continue
        with session_factory() as s:
            filing = s.get(Filing, f["accession"])
            if filing is None:
                raise LookupError(f"seed filing {f['accession']} has no filings row")
            existing = s.get(FilingText, f["accession"])
            if existing is not None and existing.source_sha256 == f["source_sha256"]:
                continue
            parsed = ParsedFiling(
                text=f["text"],
                sections=[Section(code, int(a), int(b)) for code, a, b in f["sections"]],
            )
            chunks += store_parsed(s, filing, parsed, f["source_sha256"], existing)
            stored += 1
    with session_factory() as s:
        embedded = embed_pending(s, embedder or pick_embedder("auto"))
    return SeedReport(report.total.facts, report.total.filings, stored, chunks, embedded)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(prog="python -m analyst.evals.seed")
    ap.add_argument("--embedder", choices=["auto", "bge", "hash"], default="auto")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = seed(embedder=pick_embedder(args.embedder))
    print(
        f"seeded {', '.join(TICKERS)}: facts changed={r.facts} filings changed={r.filings} "
        f"filing texts stored={r.filings_stored} chunks={r.chunks} embedded={r.embedded}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
