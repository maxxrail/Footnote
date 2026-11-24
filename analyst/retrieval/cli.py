"""Search filings from the command line.

    uv run python -m analyst.retrieval.cli "supply chain risk" --ticker NVDA
    uv run python -m analyst.retrieval.cli "deposit costs" --form 10-Q --section "Part I, Item 2"
    uv run python -m analyst.retrieval.cli "export controls" --since 2025-01-01 --rerank -k 5

Every hit prints its citation: accession, section and character span in the stored
filing text, and the sec.gov URL. Build the index first: `python -m analyst.retrieval.index`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import textwrap
from collections.abc import Sequence

from analyst.retrieval.search import search


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analyst.retrieval.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query")
    parser.add_argument("--ticker")
    parser.add_argument("--cik", type=int)
    parser.add_argument("--form", action="append", dest="forms", metavar="FORM")
    parser.add_argument("--section", action="append", dest="sections", metavar="SECTION")
    parser.add_argument("--since", type=dt.date.fromisoformat, metavar="YYYY-MM-DD")
    parser.add_argument("-k", type=int, default=8)
    parser.add_argument("--mode", choices=["hybrid", "vector", "text"], default="hybrid")
    parser.add_argument("--rerank", action="store_true", default=None)
    parser.add_argument("--json", action="store_true", help="print citations as JSON lines")
    args = parser.parse_args(argv)

    hits = search(
        args.query,
        cik=args.cik,
        forms=args.forms,
        sections=args.sections,
        since=args.since,
        k=args.k,
        ticker=args.ticker,
        mode=args.mode,
        rerank=args.rerank,
    )
    if not hits:
        print("No matching passages in the indexed filings.")
        return 1
    for n, h in enumerate(hits, start=1):
        if args.json:
            print(h.model_dump_json())
            continue
        print(
            f"[{n}] {h.ticker} {h.form} filed {h.filed_at} | {h.section} ({h.section_title}) "
            f"| {h.accession} chars {h.char_start}-{h.char_end}"
        )
        print(textwrap.indent(textwrap.fill(h.snippet, 96), "    "))
        print(f"    {h.url}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
