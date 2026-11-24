"""Peer-set statistics: median, mean and quartiles over computed values.

Values without a result (`value is None`) are left out and named in the note, so a
peer median never silently includes a guess. Quartiles use linear interpolation
between closest ranks, the same as Excel's QUARTILE.INC (and numpy's default), since
the comps tables these feed end up in Excel.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from analyst.metrics.computed import Computed

Stat = str  # "median" | "mean" | "q1" | "q3" | "min" | "max"


def _values(items: Sequence[Computed]) -> tuple[list[Decimal], list[Computed], list[Computed]]:
    used = [c for c in items if c.value is not None]
    skipped = [c for c in items if c.value is None]
    return sorted(c.value for c in used if c.value is not None), used, skipped


def quantile(sorted_values: Sequence[Decimal], q: Decimal) -> Decimal:
    """Inclusive quantile (QUARTILE.INC) of already-sorted values."""
    if not sorted_values:
        raise ValueError("no values")
    if not Decimal(0) <= q <= Decimal(1):
        raise ValueError("q must be in [0, 1]")
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_values):
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[lo + 1] - sorted_values[lo]) * frac


def peer_stat(items: Sequence[Computed], stat: Stat, metric: str | None = None) -> Computed:
    """One statistic over a peer set; inputs are each peer's result as an operand."""
    values, used, skipped = _values(items)
    name = f"{stat}({metric or (items[0].metric if items else 'metric')})"
    note = (
        f"excluded (no value): {', '.join(c.label or c.metric for c in skipped)}" if skipped else ""
    )
    inputs = tuple(c.as_operand() for c in used)
    unit = items[0].unit if items else "ratio"
    if not values:
        return Computed(name, None, stat, inputs, unit, note=note or "no values")
    fn = {
        "median": lambda v: quantile(v, Decimal("0.5")),
        "q1": lambda v: quantile(v, Decimal("0.25")),
        "q3": lambda v: quantile(v, Decimal("0.75")),
        "mean": lambda v: sum(v, Decimal(0)) / len(v),
        "min": lambda v: v[0],
        "max": lambda v: v[-1],
    }
    if stat not in fn:
        raise ValueError(f"unknown stat {stat!r}; known: {sorted(fn)}")
    formula = {"q1": "25th percentile (QUARTILE.INC)", "q3": "75th percentile (QUARTILE.INC)"}
    return Computed(name, fn[stat](values), formula.get(stat, stat), inputs, unit, note=note)


def peer_summary(items: Sequence[Computed], metric: str | None = None) -> dict[str, Computed]:
    return {s: peer_stat(items, s, metric) for s in ("median", "mean", "q1", "q3", "min", "max")}
