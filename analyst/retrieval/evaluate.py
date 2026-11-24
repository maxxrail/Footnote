"""Tiny retrieval eval: recall@k of vector-only, full-text-only and hybrid search.

    uv run python -m analyst.retrieval.evaluate            # recall@5, open + ticker-filtered
    uv run python -m analyst.retrieval.evaluate --rerank   # also hybrid + cross-encoder
    uv run python -m analyst.retrieval.evaluate --misses   # list the queries each mode missed

Gold set: `tests/fixtures/retrieval_gold.yaml`. Needs the built index and the ml extra.
"open" searches all 25 companies, so the query's company name has to do the work;
"filtered" passes the ticker, as the agent will, and measures section retrieval only.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from analyst.db.session import session_scope
from analyst.retrieval.citation import Citation
from analyst.retrieval.search import Mode, search

GOLD = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "retrieval_gold.yaml"


@dataclass(frozen=True)
class GoldQuery:
    id: str
    query: str
    ticker: str
    sections: tuple[str, ...]


def load_gold(path: Path = GOLD) -> list[GoldQuery]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        GoldQuery(q["id"], q["query"], q["ticker"].upper(), tuple(q["sections"]))
        for q in data["queries"]
    ]


def is_hit(gold: GoldQuery, hits: Sequence[Citation], k: int) -> bool:
    return any(h.ticker == gold.ticker and h.section in gold.sections for h in hits[:k])


def recall_at_k(
    results: Mapping[str, Sequence[Citation]], gold: Sequence[GoldQuery], k: int
) -> float:
    """Fraction of gold queries with a relevant (ticker, section) in the top k."""
    if not gold:
        return 0.0
    return sum(is_hit(g, results.get(g.id, []), k) for g in gold) / len(gold)


Searcher = Callable[..., list[Citation]]


def run(
    gold: Sequence[GoldQuery],
    configs: Sequence[tuple[str, Mode, bool]],
    k: int = 5,
    filtered: bool = False,
    searcher: Searcher = search,
) -> dict[str, tuple[float, list[str], float]]:
    """{config name: (recall@k, missed query ids, seconds per query)}."""
    out = {}
    with session_scope() as s:
        for name, mode, rerank in configs:
            results: dict[str, Sequence[Citation]] = {}
            t0 = time.time()
            for g in gold:
                results[g.id] = searcher(
                    g.query,
                    k=k,
                    ticker=g.ticker if filtered else None,
                    mode=mode,
                    rerank=rerank,
                    session=s,
                )
            per_q = (time.time() - t0) / max(len(gold), 1)
            missed = [g.id for g in gold if not is_hit(g, results[g.id], k)]
            out[name] = (recall_at_k(results, gold, k), missed, per_q)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m analyst.retrieval.evaluate", description=__doc__)
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--rerank", action="store_true", help="also run hybrid + rerank")
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--gold", type=Path, default=GOLD, help="query set (default: the gold set)")
    args = ap.parse_args(argv)
    gold = load_gold(args.gold)
    configs: list[tuple[str, Mode, bool]] = [
        ("vector-only", "vector", False),
        ("full-text-only", "text", False),
        ("hybrid (RRF)", "hybrid", False),
    ]
    if args.rerank:
        configs.append(("hybrid + rerank", "hybrid", True))
    print(f"{len(gold)} gold queries, recall@{args.k}\n")
    print("| Retriever | open (no filter) | ticker-filtered | s/query (open) |")
    print("|---|---|---|---|")
    open_res = run(gold, configs, args.k, filtered=False)
    filt_res = run(gold, configs, args.k, filtered=True)
    for name, _, _ in configs:
        r_open, _, secs = open_res[name]
        r_filt = filt_res[name][0]
        n = len(gold)
        print(
            f"| {name} | {r_open:.2f} ({round(r_open * n)}/{n}) "
            f"| {r_filt:.2f} ({round(r_filt * n)}/{n}) | {secs:.2f} |"
        )
    if args.misses:
        print()
        for name, _, _ in configs:
            print(f"{name}: open missed {open_res[name][1]}; filtered missed {filt_res[name][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
