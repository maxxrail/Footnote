"""`Operand` (a sourced input) and `Computed` (a result with its formula and lineage).

Every number the agent reports that isn't a reported fact is a `Computed`. Its `inputs`
say exactly which facts (or prices, or other computed values) it came from, so a reader
can rebuild it by hand. A result that can't be computed honestly (missing input,
negative earnings for a P/E) has `value=None` and a `note` saying why: refuse, don't guess.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Source = Literal["fact", "derived", "price", "computed"]
Unit = Literal["ratio", "USD", "USD/shares", "shares", "x"]


@dataclass(frozen=True)
class Operand:
    """One input to a calculation and where it came from."""

    label: str  # "AAPL revenue FY2025"
    value: Decimal
    fact_ids: tuple[int, ...] = ()
    source: Source = "fact"
    detail: str = ""  # accession, price date, or the formula of a computed input
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    unit: str = ""  # "USD", "USD/shares", "shares", "ratio", "x" (empty if unknown)


@dataclass(frozen=True)
class Computed:
    metric: str
    value: Decimal | None
    formula: str
    inputs: tuple[Operand, ...] = ()
    unit: Unit = "ratio"
    note: str = ""  # why value is None, or a caveat (e.g. "FY used: no LTM")
    label: str = field(default="")

    @property
    def fact_ids(self) -> tuple[int, ...]:
        """Every fact behind this number, in input order, without repeats."""
        seen: dict[int, None] = {}
        for op in self.inputs:
            for f in op.fact_ids:
                seen.setdefault(f, None)
        return tuple(seen)

    def as_operand(self, label: str | None = None) -> Operand:
        """Feed this result into another calculation (EV from market cap, ...)."""
        if self.value is None:
            raise ValueError(f"{self.metric} has no value: {self.note}")
        return Operand(
            label=label or self.label or self.metric,
            value=self.value,
            fact_ids=self.fact_ids,
            source="computed",
            detail=self.formula,
            unit=self.unit,
        )


def missing(metric: str, formula: str, *ops: Operand | None, unit: Unit = "ratio") -> Computed:
    """A `Computed` with no value because an input is absent."""
    present = tuple(o for o in ops if o is not None)
    return Computed(metric, None, formula, present, unit, note="missing input")
