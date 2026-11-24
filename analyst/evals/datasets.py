"""Eval items and the JSONL files they live in (`analyst/evals/datasets/*.jsonl`).

Three kinds:
- **numeric**: generated from XBRL ground truth by `build_gold.py`; the expected value
  comes from `analyst.metrics`, never from a model, with the facts behind it.
- **qualitative**: hand-written; expected (ticker, section) and the key points an
  answer should make, taken from the filing text.
- **refusal**: questions the corpus can't answer; the agent must say so.

Every item carries the dataset version it was built with (`DATASET_VERSION`), so a score
always names the exact set it was measured on.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DATASETS = Path(__file__).parent / "datasets"
DATASET_VERSION = "2026-11-24.1"
Category = Literal["numeric", "qualitative", "refusal"]
FILES = {"numeric": "numeric.jsonl", "qualitative": "qualitative.jsonl", "refusal": "refusal.jsonl"}


class ExpectedSection(BaseModel):
    ticker: str
    section: str  # "Item 7", "Part I, Item 2", ...


class Item(BaseModel):
    id: str
    category: Category
    question: str
    tickers: list[str] = []
    version: str = DATASET_VERSION
    # numeric
    template: str | None = None
    expected: str | None = None  # exact Decimal as a string
    unit: Literal["USD", "USD/shares", "ratio", "x"] | None = None
    fact_ids: list[int] = []  # every fact behind the expected value
    # qualitative
    sections: list[ExpectedSection] = []
    key_points: list[str] = []
    # refusal
    reason: str | None = None


def load(names: Iterable[str]) -> list[Item]:
    """Items from dataset names: "all", a category, "smoke", or a file stem."""
    items: list[Item] = []
    for name in names:
        if name == "all":
            stems = [Path(f).stem for f in FILES.values()]
        else:
            stems = [Path(FILES.get(name, f"{name}.jsonl")).stem]
        for stem in stems:
            path = DATASETS / f"{stem}.jsonl"
            if not path.exists():
                raise FileNotFoundError(f"no dataset {path.name} (known: {sorted(FILES)}, smoke)")
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    items.append(Item.model_validate_json(line))
    seen: set[str] = set()
    unique = []
    for it in items:
        if it.id not in seen:
            seen.add(it.id)
            unique.append(it)
    return unique


def save(path: Path, items: Sequence[Item]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(it.model_dump(exclude_defaults=False), ensure_ascii=False) for it in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
