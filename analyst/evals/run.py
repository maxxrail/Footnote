"""Run the eval suite and write a scored report.

    uv run python -m analyst.evals.run --dataset all --concurrency 4
    uv run python -m analyst.evals.run --dataset numeric --limit 20 --budget 1.00
    uv run python -m analyst.evals.run --dataset all --ablation no-search
    uv run python -m analyst.evals.run --dataset smoke --replay evals/recordings/smoke.json --gate

Writes `evals/results/{timestamp}/results.jsonl` (one graded item per line, with its
trace) and `summary.md`. `--out` also writes the whole run as JSON, and `--markdown`
copies the summary elsewhere (CI uses both).

Each item is asked with a fresh toolbox, graded, then reported:
- numeric items by `numeric_match` + `citation_valid`;
- qualitative items by the judge (correct / complete / grounded, 1-5) + `citation_valid`;
- every item by `refusal_correct`.

**Items:**
- An item whose companies aren't in the database is skipped (the CI seed corpus holds
  4 of 25), and so is any item left once `--budget` (USD, agent + judge) is spent.
- Skipped items are listed, not scored.

**Ablations:** `--ablation` turns one component off:
- `no-search` removes the `search_filings` tool;
- `no-postcheck` accepts the agent's first answer without the number check;
- `vector-only` uses only the vector leg of retrieval.

**Gate:** `--gate [FILE]` compares the summary with the thresholds stored for this dataset
in `evals/baseline/thresholds.json` and exits 1 if any metric is below its threshold.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select

from analyst.agent.ask import describe
from analyst.agent.loop import Answer
from analyst.agent.loop import run as run_agent
from analyst.agent.tools import ToolBox, load_universe
from analyst.config import get_settings
from analyst.db.models import Chunk, Company
from analyst.db.session import session_scope
from analyst.evals import graders
from analyst.evals.datasets import DATASET_VERSION, Item, load
from analyst.evals.replay import (
    Recording,
    RecordingJudge,
    RecordingLLM,
    ReplayJudge,
    ReplayLLM,
)
from analyst.llm.client import LLMClient, cost_usd
from analyst.metrics.prices import CsvPriceProvider
from analyst.retrieval.embed import Embedder, HashEmbedder

Ablation = Literal["none", "no-search", "no-postcheck", "vector-only"]
RESULTS = Path("evals/results")
THRESHOLDS = Path("evals/baseline/thresholds.json")
QUAL_PASS = 4  # a qualitative answer passes with correct >= 4 and grounded >= 4


class ItemResult(BaseModel):
    id: str
    category: str
    template: str | None = None
    question: str
    status: Literal["graded", "skipped", "error"]
    skip_reason: str | None = None
    error: str | None = None
    answer: str | None = None
    refused: bool | None = None
    confidence: str | None = None
    citations: list[str] = []
    flags: dict[str, list[str]] = {}
    passed: bool | None = None
    numeric: dict[str, Any] | None = None
    citation: dict[str, Any] | None = None
    refusal: dict[str, Any] | None = None
    judge: dict[str, Any] | None = None
    section_hit: bool | None = None
    expected: str | None = None
    latency_s: float | None = None
    cost_usd: Decimal | None = None
    steps: list[str] = []
    trace: dict[str, Any] | None = None


@dataclass
class Config:
    datasets: list[str]
    concurrency: int = 4
    budget: Decimal = Decimal("5")
    model: str | None = None
    judge_model: str | None = None
    ablation: Ablation = "none"
    limit: int | None = None
    ids: list[str] = field(default_factory=list)
    record: Path | None = None
    replay: Path | None = None


# -- running --------------------------------------------------------------------------------


class _Spend:
    def __init__(self) -> None:
        self.total = Decimal(0)
        self.unknown = False
        self.lock = threading.Lock()

    def add(self, c: Decimal | None) -> None:
        with self.lock:
            if c is None:
                self.unknown = True
            else:
                self.total += c


class _LockedEmbedder:
    """The local model isn't documented as thread-safe; one query at a time."""

    def __init__(self, inner: Embedder) -> None:
        self.inner, self.name, self.dim = inner, inner.name, inner.dim
        self._lock = threading.Lock()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            return self.inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            return self.inner.embed_query(text)


def corpus_embedder(session_factory: Callable[..., Any]) -> Embedder:
    """The embedder the stored chunks were made with (queries must use the same one)."""
    with session_factory() as s:
        name = s.scalar(select(Chunk.embed_model).where(Chunk.embed_model.is_not(None)).limit(1))
    if name is None or name == HashEmbedder.name:
        return HashEmbedder()
    from analyst.retrieval.embed import SentenceTransformerEmbedder

    return _LockedEmbedder(SentenceTransformerEmbedder(name))


def _skip_reason(item: Item, loaded: set[str], universe: set[str]) -> str | None:
    if item.category == "refusal":
        # out-of-universe companies are the point; in-universe ones must be loaded
        missing = [t for t in item.tickers if t in universe and t not in loaded]
    else:
        missing = [t for t in item.tickers if t not in loaded]
    return f"not in the loaded corpus: {missing}" if missing else None


def grade(
    item: Item,
    answer: Answer,
    judge: graders.Judge | None,
    session_factory: Callable[..., Any],
) -> tuple[ItemResult, Decimal | None]:
    """Grade one answer; returns the result and the judge's cost."""
    r = ItemResult(
        id=item.id,
        category=item.category,
        template=item.template,
        question=item.question,
        status="graded",
        answer=answer.text,
        refused=answer.refused,
        confidence=answer.confidence,
        citations=[describe(c) for c in answer.citations],
        flags={
            k: v
            for k, v in {
                "unverified": answer.unverified_numbers,
                "uncited": answer.uncited_numbers,
                "miscited": answer.miscited_numbers,
            }.items()
            if v
        },
        expected=item.expected or (item.reason if item.category == "refusal" else None),
    )
    judge_cost: Decimal | None = Decimal(0)
    refusal = graders.refusal_correct(answer, item)
    r.refusal = {"passed": refusal.passed, "detail": refusal.detail}
    unsupported: list[int] = []
    with session_factory() as s:
        if item.category == "qualitative" and not answer.refused and judge is not None:
            result, usage = graders.llm_judge(s, judge, item, answer)
            unsupported = result.unsupported_sentences
            r.judge = result.model_dump()
            judge_cost = _usage_cost(judge.model, usage)
        if item.category != "refusal":
            cg = graders.citation_valid(s, answer, item, unsupported)
            r.citation = {"passed": cg.passed, "detail": cg.detail, **cg.extra}
    if item.category == "numeric":
        ng = graders.numeric_match(answer, item)
        r.numeric = {"passed": ng.passed, "detail": ng.detail, **ng.extra}
        r.passed = ng.passed
    elif item.category == "qualitative":
        r.section_hit = graders.section_hit(answer, item)
        j = r.judge
        r.passed = bool(j and j["correct"] >= QUAL_PASS and j["grounded"] >= QUAL_PASS)
    else:
        r.passed = refusal.passed
    return r, judge_cost


def _usage_cost(model: str, usage: object) -> Decimal | None:
    if usage is None:
        return Decimal(0)
    return cost_usd(
        model,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


def run_items(
    items: Sequence[Item],
    cfg: Config,
    session_factory: Callable[..., Any] = session_scope,
    llm_factory: Callable[[str], LLMClient] | None = None,
    judge_factory: Callable[[str], graders.Judge | None] | None = None,
) -> list[ItemResult]:
    settings = get_settings()
    universe = {r.ticker for r in load_universe(settings.universe_csv)}
    with session_factory() as s:
        loaded = set(s.scalars(select(Company.ticker)))
        has_chunks = bool(s.scalar(select(func.count()).select_from(Chunk)))
    embedder = corpus_embedder(session_factory) if has_chunks else HashEmbedder()
    prices = CsvPriceProvider(settings.prices_csv)
    recording = Recording.load(cfg.replay) if cfg.replay else None
    recorder = (
        Recording(
            meta={
                "model": cfg.model or settings.anthropic_model,
                "judge_model": cfg.judge_model or cfg.model or settings.anthropic_model,
                "dataset_version": DATASET_VERSION,
            }
        )
        if cfg.record
        else None
    )
    spend = _Spend()

    def agent_llm(item_id: str) -> LLMClient:
        if recording is not None:
            return ReplayLLM(recording, item_id)
        assert llm_factory is not None
        llm = llm_factory(item_id)
        return RecordingLLM(llm, recorder, item_id) if recorder is not None else llm

    def item_judge(item_id: str) -> graders.Judge | None:
        if recording is not None:
            return ReplayJudge(recording, item_id)
        j = judge_factory(item_id) if judge_factory else None
        return RecordingJudge(j, recorder, item_id) if (j and recorder is not None) else j

    def one(item: Item) -> ItemResult:
        reason = _skip_reason(item, loaded, universe)
        if reason is None and not spend.unknown and spend.total >= cfg.budget:
            reason = f"budget of ${cfg.budget} spent"
        if reason:
            return ItemResult(
                id=item.id,
                category=item.category,
                template=item.template,
                question=item.question,
                status="skipped",
                skip_reason=reason,
            )
        box = ToolBox(
            session_factory,
            prices,
            settings.universe_csv,
            embedder,
            search_enabled=cfg.ablation != "no-search",
            search_mode="vector" if cfg.ablation == "vector-only" else "hybrid",
        )
        t0 = time.perf_counter()
        try:
            answer = run_agent(
                item.question, agent_llm(item.id), box, check_numbers=cfg.ablation != "no-postcheck"
            )
            result, judge_cost = grade(item, answer, item_judge(item.id), session_factory)
        except Exception as exc:
            return ItemResult(
                id=item.id,
                category=item.category,
                template=item.template,
                question=item.question,
                status="error",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}",
            )
        agent_cost = answer.trace.usage.cost_usd
        total = None if agent_cost is None or judge_cost is None else agent_cost + judge_cost
        spend.add(total)
        result.latency_s = round(time.perf_counter() - t0, 3)
        result.cost_usd = total
        result.steps = [st.tool for st in answer.trace.steps]
        result.trace = answer.trace.model_dump(mode="json")
        return result

    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        results = list(pool.map(one, items))
    if recorder is not None and cfg.record is not None:
        recorder.save(cfg.record)
    return results


# -- summary --------------------------------------------------------------------------------


def _rate(xs: Sequence[bool]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p * 100:.1f}%"


def summarize(results: Sequence[ItemResult], cfg: Config) -> dict[str, Any]:
    graded = [r for r in results if r.status == "graded"]
    by_cat: dict[str, list[ItemResult]] = {}
    for r in graded:
        by_cat.setdefault(r.category, []).append(r)
    numeric = by_cat.get("numeric", [])
    qual = by_cat.get("qualitative", [])
    templates: dict[str, list[bool]] = {}
    for r in numeric:
        templates.setdefault(r.template or "?", []).append(bool(r.passed))
    should = [r for r in graded if r.category == "refusal"]
    refused = [r for r in graded if r.refused]
    tp = sum(1 for r in should if r.refused)
    cites = [r for r in graded if r.citation is not None and not r.refused]
    resolved = sum(int(r.citation["resolved"]) for r in cites if r.citation)
    total_cites = sum(int(r.citation["total"]) for r in cites if r.citation)
    lat = sorted(r.latency_s for r in graded if r.latency_s is not None)
    costs = [r.cost_usd for r in graded if r.cost_usd is not None]
    judged = [r.judge for r in qual if r.judge]
    return {
        "datasets": cfg.datasets,
        "dataset_version": DATASET_VERSION,
        "ablation": cfg.ablation,
        "model": cfg.model or get_settings().anthropic_model,
        "items": len(results),
        "graded": len(graded),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "errors": sum(1 for r in results if r.status == "error"),
        "accuracy": _rate([bool(r.passed) for r in graded]),
        "numeric_accuracy": _rate([bool(r.passed) for r in numeric]),
        "numeric_by_template": {k: _rate(v) for k, v in sorted(templates.items())},
        "qualitative_pass": _rate([bool(r.passed) for r in qual]),
        "qualitative_scores": {
            k: round(statistics.mean(j[k] for j in judged), 2) if judged else None
            for k in ("correct", "complete", "grounded")
        },
        "section_hit": _rate([bool(r.section_hit) for r in qual]),
        "refusal_accuracy": _rate([bool(r.passed) for r in should]),
        "refusal_precision": round(tp / len(refused), 4) if refused else None,
        "refusal_recall": round(tp / len(should), 4) if should else None,
        "citation_valid": _rate([bool(r.citation and r.citation["passed"]) for r in cites]),
        "citations_resolve": round(resolved / total_cites, 4) if total_cites else None,
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
        "cost_total_usd": str(sum(costs, Decimal(0)).quantize(Decimal("0.0001"))),
        "cost_per_query_usd": str((sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.0001")))
        if costs
        else None,
    }


def markdown(summary: dict[str, Any], results: Sequence[ItemResult], max_failures: int = 40) -> str:
    s = summary
    lines = [
        f"# Eval summary: {', '.join(s['datasets'])} (dataset {s['dataset_version']}, "
        f"ablation {s['ablation']}, model {s['model']})",
        "",
        f"{s['graded']} graded, {s['skipped']} skipped, {s['errors']} errors, "
        f"of {s['items']} items.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| **Overall accuracy** | **{_pct(s['accuracy'])}** |",
        f"| Numeric accuracy (headline within 0.5% or rounding) | {_pct(s['numeric_accuracy'])} |",
        f"| Qualitative pass (judge correct and grounded >= {QUAL_PASS}) | "
        f"{_pct(s['qualitative_pass'])} |",
        f"| Qualitative judge means (correct / complete / grounded, 1-5) | "
        f"{s['qualitative_scores']['correct']} / {s['qualitative_scores']['complete']} / "
        f"{s['qualitative_scores']['grounded']} |",
        f"| Expected section cited (qualitative) | {_pct(s['section_hit'])} |",
        f"| Refusal accuracy (should-refuse items) | {_pct(s['refusal_accuracy'])} |",
        f"| Refusal precision / recall | {_pct(s['refusal_precision'])} / "
        f"{_pct(s['refusal_recall'])} |",
        f"| Citation valid (answers whose citations resolve and support them) | "
        f"{_pct(s['citation_valid'])} |",
        f"| Citations that resolve | {_pct(s['citations_resolve'])} |",
        f"| Latency p50 / p95 | {s['latency_p50_s']} s / {s['latency_p95_s']} s |",
        f"| Cost per query / total | ${s['cost_per_query_usd']} / ${s['cost_total_usd']} |",
        "",
        "Numeric accuracy by template:",
        "",
        "| Template | Accuracy |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_pct(v)} |" for k, v in s["numeric_by_template"].items()]
    failures = [
        r for r in results if r.status == "error" or (r.status == "graded" and not r.passed)
    ]
    lines += ["", f"## Failures ({len(failures)})", ""]
    for r in failures[:max_failures]:
        why = r.error.splitlines()[0] if r.error else _why(r)
        lines.append(f"- **{r.id}** ({r.category}): {why}")
        lines.append(f"  - Q: {r.question}")
        if r.answer:
            lines.append(f"  - A: {' '.join(r.answer.split())[:400]}")
        if r.steps:
            lines.append(f"  - steps: {', '.join(r.steps)}")
    if len(failures) > max_failures:
        lines.append(f"- ... {len(failures) - max_failures} more in results.jsonl")
    skipped = [r for r in results if r.status == "skipped"]
    if skipped:
        reasons: dict[str, int] = {}
        for r in skipped:
            reasons[r.skip_reason or ""] = reasons.get(r.skip_reason or "", 0) + 1
        lines += ["", "## Skipped", ""] + [f"- {n} × {k}" for k, n in reasons.items()]
    return "\n".join(lines) + "\n"


def _why(r: ItemResult) -> str:
    if r.category == "numeric" and r.numeric:
        return f"expected {r.expected}; {r.numeric['detail']}"
    if r.category == "qualitative" and r.judge:
        j = r.judge
        return (
            f"judge correct {j['correct']}, complete {j['complete']}, "
            f"grounded {j['grounded']}: {j['rationale'][:200]}"
        )
    if r.category == "qualitative":
        return "refused" if r.refused else "not judged"
    return r.refusal["detail"] if r.refusal else "failed"


def gate(summary: dict[str, Any], key: str, path: Path) -> list[str]:
    """Metrics below their committed thresholds (empty list = pass)."""
    if not path.exists():
        return [f"no thresholds file {path}"]
    thresholds = json.loads(path.read_text(encoding="utf-8")).get(key)
    if thresholds is None:
        return [f"no thresholds for dataset {key!r} in {path}"]
    out = []
    for metric, floor in thresholds.items():
        got = summary.get(metric)
        if got is None or got < floor:
            out.append(f"{metric} {got} < {floor}")
    if summary.get("errors"):
        out.append(f"{summary['errors']} items errored")
    return out


# -- CLI ------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(prog="python -m analyst.evals.run")
    ap.add_argument(
        "--dataset", default="all", help="comma list: all, numeric, qualitative, refusal, smoke"
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--budget", type=Decimal, default=Decimal("5"), help="hard cap in USD (agent + judge)"
    )
    ap.add_argument("--model", help="agent model (default: ANTHROPIC_MODEL)")
    ap.add_argument("--judge-model", help="judge model (default: the agent model)")
    ap.add_argument(
        "--ablation", choices=["none", "no-search", "no-postcheck", "vector-only"], default="none"
    )
    ap.add_argument("--limit", type=int, help="first N items only")
    ap.add_argument("--ids", help="comma list of item ids")
    ap.add_argument("--record", type=Path, help="save every model response here")
    ap.add_argument("--replay", type=Path, help="replay a recording instead of calling the model")
    ap.add_argument(
        "--gate", nargs="?", const=THRESHOLDS, type=Path, help="fail below the committed thresholds"
    )
    ap.add_argument("--markdown", type=Path, help="also write the summary here")
    ap.add_argument("--out", type=Path, help="also write the full run as JSON here")
    args = ap.parse_args(argv)

    datasets = [d.strip() for d in args.dataset.split(",") if d.strip()]
    cfg = Config(
        datasets=datasets,
        concurrency=args.concurrency,
        budget=args.budget,
        model=args.model,
        judge_model=args.judge_model,
        ablation=args.ablation,
        limit=args.limit,
        ids=[i for i in (args.ids or "").split(",") if i],
        record=args.record,
        replay=args.replay,
    )
    items = load(datasets)
    if cfg.ids:
        items = [it for it in items if it.id in set(cfg.ids)]
    if cfg.limit:
        items = items[: cfg.limit]

    llm_factory = judge_factory = None
    if cfg.replay is None:
        from analyst.evals.graders import AnthropicJudge
        from analyst.llm.client import AnthropicLLM

        llm = AnthropicLLM(model=cfg.model)
        judge = AnthropicJudge(cfg.judge_model or cfg.model)
        llm_factory = lambda _id: llm  # noqa: E731
        judge_factory = lambda _id: judge  # noqa: E731
    t0 = time.time()
    results = run_items(items, cfg, llm_factory=llm_factory, judge_factory=judge_factory)
    summary = summarize(results, cfg)
    summary["wall_s"] = round(time.time() - t0, 1)
    md = markdown(summary, results)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS / f"{stamp}-{'-'.join(datasets)}-{cfg.ablation}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in results) + "\n", encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(md, encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=1, default=str), encoding="utf-8"
    )
    if args.markdown:
        args.markdown.write_text(md, encoding="utf-8")
    if args.out:
        body = {"summary": summary, "results": [r.model_dump(mode="json") for r in results]}
        args.out.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
    from analyst.agent.ask import safe_print

    safe_print(md.split("\nNumeric accuracy by template")[0])
    safe_print(f"results: {run_dir}")
    if args.gate:
        below = gate(summary, ",".join(datasets), args.gate)
        if below:
            safe_print("GATE FAILED: " + "; ".join(below))
            return 1
        safe_print("gate passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
