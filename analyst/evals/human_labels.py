"""Human labels for the LLM judge: make the labeling sheet, then measure agreement.

    uv run python -m analyst.evals.human_labels make --from evals/results/<run>/results.jsonl
    # fill in evals/human_labels.csv (correct, complete, grounded: 1-5) in Excel
    uv run python -m analyst.evals.human_labels agreement

`make` picks 20 judged qualitative answers from a run. It writes
`analyst/evals/datasets/human_labels.jsonl`, the record with the judge's scores and empty
human scores, and `evals/human_labels.csv` for labeling. The CSV hides the judge's scores
so they don't anchor the labeler.

`agreement` reads the filled CSV, stores the human scores in the JSONL, and reports per
dimension:
- how many items are labeled;
- exact agreement and agreement within one point;
- the mean absolute difference;
- pass/fail agreement (>= 4, the runner's pass line).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from analyst.evals.datasets import DATASETS, load

LABELS = DATASETS / "human_labels.jsonl"
SHEET = Path("evals/human_labels.csv")
DIMS = ("correct", "complete", "grounded")
N = 20
PASS = 4


def make(results: Path, labels: Path = LABELS, sheet: Path = SHEET, n: int = N) -> int:
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line]
    judged = sorted(
        (r for r in rows if r["category"] == "qualitative" and r.get("judge")),
        key=lambda r: r["id"],
    )[:n]
    gold = {it.id: it for it in load(["qualitative"])}
    records = [
        {
            "id": r["id"],
            "question": r["question"],
            "key_points": gold[r["id"]].key_points,
            "answer": r["answer"],
            "citations": r["citations"],
            "judge": {d: r["judge"][d] for d in DIMS},
            "human": {d: None for d in DIMS},
        }
        for r in judged
    ]
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in records) + "\n", encoding="utf-8"
    )
    sheet.parent.mkdir(parents=True, exist_ok=True)
    with sheet.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(["id", "question", "gold key points", "answer", "sources", *DIMS])
        for x in records:
            w.writerow(
                [
                    x["id"],
                    x["question"],
                    "\n".join(f"- {k}" for k in x["key_points"]),
                    x["answer"],
                    "\n".join(x["citations"]),
                    "",
                    "",
                    "",
                ]
            )
    return len(records)


def read_sheet(sheet: Path) -> dict[str, dict[str, int]]:
    out = {}
    with sheet.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            scores = {d: row.get(d, "").strip() for d in DIMS}
            if all(scores.values()):
                vals = {d: int(v) for d, v in scores.items()}
                if not all(1 <= v <= 5 for v in vals.values()):
                    raise ValueError(f"{row['id']}: scores must be 1-5, got {vals}")
                out[row["id"]] = vals
    return out


def agreement(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    report: dict[str, dict[str, float | int | None]] = {}
    labeled = [r for r in records if all(r["human"].get(d) is not None for d in DIMS)]
    for d in (*DIMS, "pass"):
        if d == "pass":
            pairs = [(_passes(r["judge"]), _passes(r["human"])) for r in labeled]
            same = [a == b for a, b in pairs]
            report[d] = {"n": len(pairs), "agree": _rate(same)}
            continue
        diffs = [abs(r["judge"][d] - r["human"][d]) for r in labeled]
        report[d] = {
            "n": len(diffs),
            "exact": _rate([x == 0 for x in diffs]),
            "within_1": _rate([x <= 1 for x in diffs]),
            "mean_abs_diff": round(statistics.mean(diffs), 2) if diffs else None,
        }
    return report


def _passes(scores: dict[str, int]) -> bool:
    return scores["correct"] >= PASS and scores["grounded"] >= PASS


def _rate(xs: Sequence[bool]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(prog="python -m analyst.evals.human_labels")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("--from", dest="results", type=Path, required=True)
    sub.add_parser("agreement")
    args = ap.parse_args(argv)
    if args.cmd == "make":
        print(f"{make(args.results)} items -> {LABELS} and {SHEET} (fill in the CSV)")
        return 0
    records = [json.loads(x) for x in LABELS.read_text(encoding="utf-8").splitlines() if x]
    if SHEET.exists():
        human = read_sheet(SHEET)
        for r in records:
            if r["id"] in human:
                r["human"] = human[r["id"]]
        LABELS.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in records) + "\n", encoding="utf-8"
        )
    rep = agreement(records)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
