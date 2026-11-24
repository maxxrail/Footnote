"""Post-check of a final answer: every number must come from the tool output it cites.

`check(text, tool_outputs, question, known_refs, refused)` finds the numbers in the answer
and checks each one against the values the run's tools returned.

What counts as a tool value (typed, never raw digits):
- `value` / `display` fields of items that carry a `ref` (and their `inputs`), with the
  item's `unit`: a `ratio` is a percentage (0.25 -> 25%), `x` a multiple, anything else an
  amount. Values are signed.
- numbers written in a filing `excerpt` (sign unknown, kind from how they're written).
Never: ref ids, CIKs, dates, accession numbers, notes, error messages, resolve_company text.

Rules for each number in the answer:
- **Matched within rounding:** it equals a value of the same kind (% with %, x with x,
  amounts with amounts; a bare number may also match a multiple) to within half a unit of
  its last written digit, after scale words (`$1.5 billion`, `$340M`). "$1.5 billion"
  matches 1,523,000,000; "$1.6 billion" doesn't.
- **Signed consistently:** an explicit minus or parentheses, or a direction word right before
  it ("grew 25%", "fell 25%"), must agree with the sign of a signed tool value.
- **Cited, to the right ref:** its sentence (or table row, or list item) carries a `[^n]`
  marker, and the number is among the values of a ref cited in that sentence. A number from
  some other tool output is "miscited"; one from no tool output is "unverified".
- A number the user wrote in the question may be echoed only in a sentence that also has a
  verified, cited number (a comparison); on its own it's unverified.
- **Not checked (labels):** markers, years and fiscal labels (2025, FY2025, Q3), form and item
  names, dates, list numbering, "Tier 1" / "Level 3" / "Note 7", and integers 0-12 followed
  by a count word ("3 years", "4 quarters", "2 segments").
- A non-refused answer must cite something.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Kind = Literal["pct", "mult", "amount"]

MARKER = re.compile(r"\[\^(\d+)\]")
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_LABELS = [
    MARKER,
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO dates
    re.compile(r"\b\d{10}-\d{2}-\d{6}\b"),  # accession numbers
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}\b"),  # December 31, 2025
    re.compile(rf"\b{_MONTHS}\s+\d{{4}}\b"),  # December 2025
    re.compile(r"\b(?:FY|CY)\s?'?\d{2,4}\b", re.I),  # FY2025, FY25
    re.compile(r"\bQ[1-4]\b"),
    re.compile(r"\b[1-4]Q\s?\d{2,4}\b"),
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|DEF 14A)(?:/A)?\b", re.I),
    re.compile(r"\b(?:Item|Note|Tier|Level|Section|Rule|Phase|Stage)\s+\d+[A-C]?\b", re.I),
    re.compile(r"\bPart\s+[IV]+\b", re.I),
    re.compile(r"^\s*\d+[.)]\s", re.M),  # numbered list items
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
    r"(?<![\w.])(?P<paren>\()?(?P<neg>[-−–])?(?P<cur>\$)?\s?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?:\s?(?P<pct>%|percent\b|pct\b)"
    r"|(?P<x>x|×)(?![a-zA-Z])|\s(?P<times>times)\b"
    r"|\s?(?P<scale>trillion|billion|million|thousand|bn|mm|mn)\b"
    r"|(?P<abbr>[TBMK])(?![a-zA-Z]))?"
    r"(?P<close>\))?",
    re.I,
)
SMALL_INT = 12
_COUNT_WORD = re.compile(
    r"\s+(?:(?:fiscal|calendar|full|reported|consecutive)\s+)?"
    r"(?:years?|quarters?|months?|weeks?|days?|segments?|compan(?:y|ies)|peers?|"
    r"businesses|regions?|tools?|steps?|filings?|sources?)\b",
    re.I,
)
_UP = re.compile(
    r"\b(?:grew|grows|grow|rose|rises|rise|increased|increases|increase|up|gained|gains|"
    r"expanded|improved|higher by)\s+(?:by\s+)?"
    r"(?:about\s+|roughly\s+|approximately\s+|nearly\s+)?$",
    re.I,
)
_DOWN = re.compile(
    r"\b(?:fell|falls|fall|declined|declines|decline|decreased|decreases|decrease|down|"
    r"dropped|drops|shrank|contracted|lower by)\s+(?:by\s+)?"
    r"(?:about\s+|roughly\s+|approximately\s+|nearly\s+)?$",
    re.I,
)
# Sentences, table rows and list items. Splits after . ! ? ; even before a digit or a
# lowercase word (stricter for citations: a marker only covers its own clause).
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


@dataclass(frozen=True)
class Num:
    raw: str
    value: Decimal  # absolute, scaled (1.5 billion -> 1500000000); percent for "%"
    tol: Decimal  # half a unit in the last written place, scaled
    kind: Kind
    bare: bool  # no $, no scale, no % or x: may match a multiple too
    sign: int  # -1 / +1 when the text says so (minus, parentheses, fell/grew), else 0
    label: bool  # a small count ("3 years"), not checked


@dataclass(frozen=True)
class Val:
    value: Decimal  # signed when `signed`; percent for kind "pct"
    kind: Kind
    signed: bool  # tool values have a sign; numbers read from filing text don't


def _strip_labels(text: str) -> str:
    for pat in _LABELS:
        text = pat.sub(lambda m: " " * len(m.group(0)), text)
    return text


def extract(text: str) -> list[Num]:
    """Numbers in `text`, with labels (years, forms, markers...) blanked out first."""
    out = []
    stripped = _strip_labels(text)
    for m in NUMBER.finditer(stripped):
        intpart = m.group("int").replace(",", "")
        dec = m.group("dec") or ""
        try:
            base = Decimal(f"{intpart}.{dec}" if dec else intpart)
        except InvalidOperation:  # pragma: no cover - the regex only admits digits
            continue
        scale_word = (m.group("scale") or m.group("abbr") or "").lower()
        scale = _SCALES.get(scale_word, Decimal(1))
        unit = Decimal(10) ** -len(dec)
        kind: Kind = (
            "pct" if m.group("pct") else "mult" if (m.group("x") or m.group("times")) else "amount"
        )
        bare = kind == "amount" and not m.group("cur") and scale == 1
        before = stripped[: m.start()]
        if m.group("neg") or (m.group("paren") and m.group("close")):
            sign = -1
        elif _UP.search(before):
            sign = 1
        elif _DOWN.search(before):
            sign = -1
        else:
            sign = 0
        label = (
            bare and not dec and base <= SMALL_INT and bool(_COUNT_WORD.match(stripped, m.end()))
        )
        raw = m.group(0).strip()
        out.append(Num(raw, base * scale, unit * scale / 2, kind, bare, sign, label))
    return out


# -- tool values ---------------------------------------------------------------------------


def _vals_from_display(display: Any) -> Iterator[Val]:
    if isinstance(display, str):
        for n in extract(display):
            signed_value = -n.value if n.sign < 0 else n.value
            yield Val(signed_value, n.kind, True)


def _vals_from_item(item: Any) -> Iterator[Val]:
    """Typed values inside one ref'd item: value/display (+unit), inputs, excerpt."""
    if isinstance(item, list):
        for x in item:
            yield from _vals_from_item(x)
        return
    if not isinstance(item, dict):
        return
    unit = item.get("unit")
    display = item.get("display")
    raw = item.get("value")
    if isinstance(raw, str):
        try:
            v = Decimal(raw)
        except InvalidOperation:
            v = None
        if v is not None:
            if unit == "ratio" or (isinstance(display, str) and display.endswith("%")):
                yield Val(v * 100, "pct", True)
            elif unit == "x" or (isinstance(display, str) and display.endswith("x")):
                yield Val(v, "mult", True)
            else:
                yield Val(v, "amount", True)
    yield from _vals_from_display(display)
    excerpt = item.get("excerpt")
    if isinstance(excerpt, str):
        for n in extract(excerpt):
            yield Val(n.value, n.kind, False)
    for key in ("inputs", "values"):
        if key in item:
            yield from _vals_from_item(item[key])


def ref_values(outputs: Iterable[str]) -> dict[int, list[Val]]:
    """{ref: typed values of that ref's item} from the run's tool outputs (JSON strings)."""
    found: dict[int, list[Val]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("ref"), int):
                found.setdefault(node["ref"], []).extend(_vals_from_item(node))
            for k, v in node.items():
                if k not in ("inputs",):
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for out in outputs:
        try:
            walk(json.loads(out))
        except (json.JSONDecodeError, TypeError):
            continue  # error messages carry no values
    return found


def matches(n: Num, values: Iterable[Val]) -> bool:
    for v in values:
        if v.kind != n.kind and not (n.bare and v.kind == "mult"):
            continue
        if abs(n.value - abs(v.value)) > n.tol:
            continue
        if n.sign and v.signed and v.value != 0 and (v.value > 0) != (n.sign > 0):
            continue
        return True
    return False


def _question_vals(question: str) -> list[Val]:
    return [Val(n.value, n.kind, False) for n in extract(question) if not n.label]


# -- the check -----------------------------------------------------------------------------


@dataclass
class CheckResult:
    unverified_numbers: list[str] = field(default_factory=list)
    uncited_numbers: list[str] = field(default_factory=list)
    miscited_numbers: list[str] = field(default_factory=list)
    unknown_refs: list[int] = field(default_factory=list)
    no_citations: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.unverified_numbers
            or self.uncited_numbers
            or self.miscited_numbers
            or self.unknown_refs
            or self.no_citations
        )

    def problems(self) -> str:
        lines = []
        if self.unverified_numbers:
            lines.append(
                "These numbers do not match any tool output (within rounding, with the same "
                f"unit and sign): {self.unverified_numbers}. Get them from get_financials or "
                "compute, write them the way the tool's `display` does, or remove them."
            )
        if self.miscited_numbers:
            lines.append(
                f"These numbers are not in the output of the [^ref] cited in their sentence: "
                f"{self.miscited_numbers}. Cite the ref whose result contains the number."
            )
        if self.uncited_numbers:
            lines.append(
                f"These numbers are in sentences without a [^ref] marker: {self.uncited_numbers}. "
                "Put a marker in the same sentence as each number (split the sentence or "
                "repeat the marker; one at the end of a later sentence doesn't count)."
            )
        if self.unknown_refs:
            lines.append(
                f"These [^ref] markers were not returned by any tool: {self.unknown_refs}."
            )
        if self.no_citations:
            lines.append(
                "The answer cites nothing. Cite the tool results it relies on, or set "
                "refused to true if the corpus can't answer the question."
            )
        return " ".join(lines)


def check(
    text: str,
    outputs: Iterable[str],
    question: str,
    known_refs: Iterable[int],
    refused: bool = False,
) -> CheckResult:
    by_ref = ref_values(outputs)
    every = [v for vals in by_ref.values() for v in vals]
    asked = _question_vals(question)
    known = set(known_refs)
    result = CheckResult()
    markers = {int(r) for r in MARKER.findall(text)}
    result.unknown_refs = sorted(markers - known)
    result.no_citations = not refused and not (markers & known)
    for sentence in _SENTENCE.split(text):
        nums = [n for n in extract(sentence) if not n.label]
        if not nums:
            continue
        cited_refs = {int(r) for r in MARKER.findall(sentence)} & known
        cited_vals = [v for r in cited_refs for v in by_ref.get(r, [])]
        echoes: list[Num] = []
        verified_here = False
        for n in nums:
            if cited_refs and matches(n, cited_vals):
                verified_here = True
            elif matches(n, every):
                (result.miscited_numbers if cited_refs else result.uncited_numbers).append(n.raw)
            elif matches(n, asked):
                echoes.append(n)
            else:
                result.unverified_numbers.append(n.raw)
        if echoes and not verified_here:
            result.unverified_numbers += [n.raw for n in echoes]
    return result
