"""Citations an answer can carry.

- `Citation` (Part 2): a character span of a filing's text.
- `FactCitation`: one XBRL fact (concept, period, unit, filing accession, fact id).
- `ComputedCitation`: a number computed in `analyst.metrics`, with its formula, its
  inputs, and every fact behind it, so the result can be rebuilt by hand.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel

from analyst.retrieval.citation import Citation


class FactCitation(BaseModel):
    kind: Literal["fact"] = "fact"
    ticker: str
    company: str
    cik: int
    canonical: str  # "revenue"
    concept: str  # "us-gaap:Revenues"
    label: str  # "FY2025" or "FY2025 Q3"
    period_start: dt.date | None
    period_end: dt.date
    unit: str
    value: str  # exact Decimal as a string
    fact_id: int
    accession: str
    form: str
    filed_at: dt.date
    url: str


class OperandRef(BaseModel):
    label: str
    value: str
    source: str  # fact | derived | price | computed
    detail: str = ""
    fact_ids: list[int] = []


class ComputedCitation(BaseModel):
    kind: Literal["computed"] = "computed"
    ticker: str | None  # None for peer-set statistics
    metric: str
    label: str  # "CHIP operating_margin FY2025"
    value: str | None
    unit: str
    formula: str
    note: str = ""
    inputs: list[OperandRef]
    facts: list[FactCitation]  # every fact behind the number, resolved


AnyCitation = Citation | FactCitation | ComputedCitation
