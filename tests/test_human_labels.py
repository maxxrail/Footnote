"""Human-label sheet and judge agreement."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from analyst.evals.human_labels import agreement, make, read_sheet


def results_file(tmp_path: Path) -> Path:
    rows = [
        {
            "id": "qual-01-NVDA",
            "category": "qualitative",
            "question": "Q1",
            "answer": "A1",
            "citations": ["c1"],
            "judge": {"correct": 5, "complete": 4, "grounded": 5},
        },
        {
            "id": "qual-02-NVDA",
            "category": "qualitative",
            "question": "Q2",
            "answer": "A2",
            "citations": [],
            "judge": {"correct": 2, "complete": 2, "grounded": 3},
        },
        {"id": "num-x", "category": "numeric", "question": "Q", "answer": "A", "citations": []},
    ]
    p = tmp_path / "results.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_make_writes_labels_and_a_sheet_without_judge_scores(tmp_path: Path) -> None:
    labels, sheet = tmp_path / "labels.jsonl", tmp_path / "sheet.csv"
    assert make(results_file(tmp_path), labels, sheet) == 2
    recs = [json.loads(x) for x in labels.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["human"] == {"correct": None, "complete": None, "grounded": None}
    assert recs[0]["key_points"][0].startswith("The U.S. government required a license")
    header = sheet.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == "id,question,gold key points,answer,sources,correct,complete,grounded"
    assert "judge" not in header


def test_agreement_metrics(tmp_path: Path) -> None:
    recs: list[dict[str, Any]] = [
        {
            "judge": {"correct": 5, "complete": 4, "grounded": 5},
            "human": {"correct": 5, "complete": 3, "grounded": 4},
        },
        {
            "judge": {"correct": 2, "complete": 2, "grounded": 3},
            "human": {"correct": 4, "complete": 2, "grounded": 4},
        },
        {
            "judge": {"correct": 4, "complete": 4, "grounded": 4},
            "human": {"correct": None, "complete": None, "grounded": None},
        },
    ]
    rep = agreement(recs)
    assert rep["correct"] == {"n": 2, "exact": 0.5, "within_1": 0.5, "mean_abs_diff": 1.0}
    assert rep["grounded"]["within_1"] == 1.0
    assert rep["pass"] == {"n": 2, "agree": 0.5}  # item 2: judge fails, human passes


def test_read_sheet_skips_blank_rows_and_checks_range(tmp_path: Path) -> None:
    sheet = tmp_path / "s.csv"
    with sheet.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "id",
                "question",
                "gold key points",
                "answer",
                "sources",
                "correct",
                "complete",
                "grounded",
            ]
        )
        w.writerow(["a", "", "", "", "", "5", "4", "3"])
        w.writerow(["b", "", "", "", "", "", "", ""])
    assert read_sheet(sheet) == {"a": {"correct": 5, "complete": 4, "grounded": 3}}
    with sheet.open("a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(["c", "", "", "", "", "9", "1", "1"])
    with pytest.raises(ValueError, match="1-5"):
        read_sheet(sheet)
