"""Post-check of a final answer: every number must come from a tool, and be cited.

`check(text, tool_outputs, question, refs)` finds the numbers in the answer and compares
each with the numbers that appeared in the run's tool outputs (and the question).

- **Matching "within rounding":** a number written with d decimals matches a tool value
  if they differ by at most half a unit in the last written place, after scale words
  (`$1.5 billion`, `$340M`, `1,500 million`) are applied. "15.1%" also matches a ratio
  0.1506 (percent of a fraction). "$1.5 billion" matches 1,523,000,000; "$1.6 billion" doesn't.
- **Not checked:** citation markers `[^n]`, years and fiscal labels (2025, FY2025, Q3),
  form and item names (10-K, Item 7), dates, and bare integers 0-12 with no unit or
  decimals ("3 years", "4 quarters"). These are labels, not financial values.
- **Cited:** a sentence (or table row) that contains a checked number must carry at least
  one `[^n]` marker (unless the number is one the user wrote in the question), and every
  marker must point at a ref some tool returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MARKER = re.compile(r"\[\^(\d+)\]")
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_LABELS = [
    MARKER,
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO dates
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}\b"),  # December 31, 2025
    re.compile(rf"\b{_MONTHS}\s+\d{{4}}\b"),  # December 2025
    re.compile(r"\b(?:FY|CY)\s?'?\d{2,4}\b", re.I),  # FY2025, FY25
    re.compile(r"\bQ[1-4]\b"),
    re.compile(r"\b[1-4]Q\s?\d{2,4}\b"),
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|DEF 14A)(?:/A)?\b", re.I),
    re.compile(r"\bItem\s+\d+[A-C]?\b", re.I),
    re.compile(r"\bPart\s+[IV]+\b", re.I),
    re.compile(r"\b(?:fiscal|calendar)\s+(?:year\s+)?(?:19|20)\d{2}\b", re.I),
    # bare years, unless a unit follows ("2025 million" is an amount)
    re.compile(
        r"(?<![\d$.,])(?:19|20)\d{2}"
        r"(?![\d%]|\.\d|,\d{3}|\s?(?:trillion|billion|million|thousand|bn|mm|mn)\b|[TBMKx]\b)",
        re.I,
    ),
]
_SCALES = {
    "trillion": Decimal(10) ** 12,
    "t": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,
    "million": Decimal(10) ** 6,
    "mm": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "m": Decimal(10) ** 6,
    "thousand": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}
NUMBER = re.compile(
    r"(?<![\w.])(?P<neg>[-−–])?(?P<cur>\$)?\s?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?:\s?(?P<pct>%|percent\b|pct\b)"
    r"|(?P<x>x|×)(?![a-zA-Z])"
    r"|\s?(?P<scale>trillion|billion|million|thousand|bn|mm|mn)\b"
    r"|(?P<abbr>[TBMK])(?![a-zA-Z]))?",
    re.I,
)
SMALL_INT = 12
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z$(\[])|\n+")


@dataclass(frozen=True)
class Num:
    raw: str
    value: Decimal  # absolute, scaled (1.5 billion -> 1500000000)
    tol: Decimal  # half a unit in the last written place, scaled
    pct: bool
    mult: bool
    unitless_small_int: bool


def _strip_labels(text: str) -> str:
    for pat in _LABELS:
        text = pat.sub(lambda m: " " * len(m.group(0)), text)
    return text


def extract(text: str) -> list[Num]:
    """Numbers in `text`, with labels (years, forms, markers...) blanked out first."""
    out = []
    for m in NUMBER.finditer(_strip_labels(text)):
        intpart = m.group("int").replace(",", "")
        dec = m.group("dec") or ""
        try:
            base = Decimal(f"{intpart}.{dec}" if dec else intpart)
        except InvalidOperation:  # pragma: no cover - the regex only admits digits
            continue
        scale_word = (m.group("scale") or m.group("abbr") or "").lower()
        scale = _SCALES.get(scale_word, Decimal(1))
        unit = Decimal(10) ** -len(dec)
        pct, mult = bool(m.group("pct")), bool(m.group("x"))
        small = (
            not dec
            and not pct
            and not mult
            and not m.group("cur")
            and scale == 1
            and base <= SMALL_INT
        )
        out.append(Num(m.group(0).strip(), base * scale, unit * scale / 2, pct, mult, small))
    return out


def tool_values(outputs: Iterable[str], extra: Iterable[str] = ()) -> set[Decimal]:
    """Every number that appeared in tool outputs (and the question), in absolute units.
    A percentage like "15.1%" is stored both as 15.1 and as 0.151."""
    vals: set[Decimal] = set()
    for chunk in [*outputs, *extra]:
        for n in extract(chunk):
            vals.add(n.value)
            if n.pct:
                vals.add(n.value / 100)
        # raw JSON numbers like "0.1505983" or "1500000000" without separators
        for raw in re.findall(r"-?\d+(?:\.\d+)?", chunk):
            vals.add(abs(Decimal(raw)))
    return vals


def matches(n: Num, values: set[Decimal]) -> bool:
    for v in values:
        v = abs(v)
        if abs(n.value - v) <= n.tol:
            return True
        if n.pct and abs(n.value - v * 100) <= n.tol:
            return True
    return False


@dataclass
class CheckResult:
    unverified_numbers: list[str] = field(default_factory=list)
    uncited_numbers: list[str] = field(default_factory=list)
    unknown_refs: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unverified_numbers or self.uncited_numbers or self.unknown_refs)

    def problems(self) -> str:
        lines = []
        if self.unverified_numbers:
            lines.append(
                "These numbers do not match any tool output (within rounding): "
                f"{self.unverified_numbers}. Get them from get_financials or compute, "
                "or remove them."
            )
        if self.uncited_numbers:
            lines.append(
                f"These numbers are in sentences without a [^ref] marker: {self.uncited_numbers}."
            )
        if self.unknown_refs:
            lines.append(
                f"These [^ref] markers were not returned by any tool: {self.unknown_refs}."
            )
        return " ".join(lines)


def check(
    text: str, outputs: Iterable[str], question: str, known_refs: Iterable[int]
) -> CheckResult:
    values = tool_values(outputs, [question])
    asked = tool_values([question])  # numbers the user wrote need no citation
    known = set(known_refs)
    result = CheckResult()
    result.unknown_refs = sorted({int(r) for r in MARKER.findall(text)} - known)
    for sentence in _SENTENCE.split(text):
        nums = [n for n in extract(sentence) if not n.unitless_small_int]
        if not nums:
            continue
        cited = bool(MARKER.search(sentence))
        for n in nums:
            if not matches(n, values):
                result.unverified_numbers.append(n.raw)
            elif not cited and not matches(n, asked):
                result.uncited_numbers.append(n.raw)
    return result
