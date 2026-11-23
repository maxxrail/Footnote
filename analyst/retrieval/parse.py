"""10-K / 10-Q HTML → clean text + section spans.

The clean text is what every citation points into: `text[char_start:char_end]`. It is
stored once per filing (`filing_texts`), so spans stay stable as long as that row does.

Cleaning:
- inline XBRL: `ix:header` (hidden facts) and `display:none` blocks are dropped; inline
  `ix:*` wrappers are unwrapped so the visible number stays in the text;
- tables become one line per row, cells joined with " | " (empty spacer cells dropped,
  a lone "$" glued to the next cell, ")" and "%" glued to the previous one);
- page furniture: bare page numbers and short lines that repeat ≥ 3 times (running
  headers/footers such as "Apple Inc. | 2025 Form 10-K | 12", "Table of Contents", or a
  bare "Item 1A" at the top of every page) are removed.

Sections: 10-K `Item 1`, `Item 1A`, … `Item 16`; 10-Q `Part I, Item 2`, `Part II, Item 1A`.
See `detect_sections` for how the table of contents is skipped.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

# Bump when cleaning or section detection changes: stored texts/chunks are rebuilt.
PARSER_VERSION = 1

BLOCK_TAGS = frozenset(
    [
        "p",
        "div",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "section",
        "article",
        "header",
        "footer",
        "body",
        "blockquote",
        "center",
        "pre",
        "dl",
        "dt",
        "dd",
        "caption",
    ]
)
DROP_TAGS = frozenset({"script", "style", "head", "title", "ix:header", "noscript"})
CELL_TAGS = frozenset({"td", "th"})

ITEMS_10K = [
    "1",
    "1A",
    "1B",
    "1C",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "7A",
    "8",
    "9",
    "9A",
    "9B",
    "9C",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
]
ITEMS_10Q = {"I": ("1", "2", "3", "4"), "II": ("1", "1A", "2", "3", "4", "5", "6")}

TITLES_10K = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "[Reserved]",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}
TITLES_10Q = {
    ("I", "1"): "Financial Statements",
    ("I", "2"): "Management's Discussion and Analysis",
    ("I", "3"): "Quantitative and Qualitative Disclosures About Market Risk",
    ("I", "4"): "Controls and Procedures",
    ("II", "1"): "Legal Proceedings",
    ("II", "1A"): "Risk Factors",
    ("II", "2"): "Unregistered Sales of Equity Securities and Use of Proceeds",
    ("II", "3"): "Defaults Upon Senior Securities",
    ("II", "4"): "Mine Safety Disclosures",
    ("II", "5"): "Other Information",
    ("II", "6"): "Exhibits",
}

_RISK = r"risk factors"
_MDNA = (
    r"management'?s discussion and analysis"
    r"(?: of (?:the )?(?:results of operations and )?financial condition"
    r"(?: and results of operations)?)?"
)
_MKT = r"quantitative and qualitative disclosures? (?:about|of) market risks?"
_FS10K = (
    r"(?:consolidated )?financial statements and supplementary data"
    r"|report of independent registered public accounting firm"
)
_FS10Q = r"(?:condensed )?(?:consolidated )?financial statements(?: \(unaudited\))?"
# Items whose body some filers put under a bare title heading (Citi, Morgan Stanley, Intel)
# or behind a pointer ("Item 7. … see pages 50-160", JPMorgan): found by title instead.
KEY_TITLES_10K: dict[tuple[str | None, str], str] = {
    (None, "1A"): _RISK,
    (None, "7"): _MDNA,
    (None, "7A"): _MKT,
    (None, "8"): _FS10K,
}
KEY_TITLES_10Q: dict[tuple[str | None, str], str] = {
    ("I", "1"): _FS10Q,
    ("I", "2"): _MDNA,
    ("I", "3"): _MKT,
    ("II", "1A"): _RISK,
}

# "Item 1A.", "ITEM 7 —", "Items 1 and 2.", "Item 1A|Risk Factors" at the start of a line.
ITEM_RE = re.compile(
    r"^items?\s*(\d{1,2})\s*([a-c])?(?![\w])(?:\s*(?:and|&|,)\s*\d{1,2}[a-c]?(?![\w]))?",
    re.IGNORECASE,
)
PART_RE = re.compile(r"^part\s*(iv|iii|ii|i|[1-4])(?![\w])", re.IGNORECASE)
# What may follow the item number in a heading: nothing, punctuation, or a Capitalised
# title. "Item 1A of our 10-K …" and "Item 7, “MD&A”, …" are prose, not headings.
HEADING_REST_RE = re.compile(r"^\s*(?:$|[.:|\-–—)]|[A-Z\[(“\"'])")
# A line that ends in a page reference is a table-of-contents or cross-reference entry.
TOC_LINE_RE = re.compile(
    r"\|\s*(?:pages?\s*)?[0-9ivx]{1,4}(?:\s*[-–—,]\s*[0-9ivx]{1,4})*\s*(?:\(\w\))?$", re.I
)
TRAILING_NUMBER_RE = re.compile(r"\d{1,3}$")
PAGE_NUMBER_RE = re.compile(
    r"^(?:page\s*)?(?:\d{1,3}|[ivxlc]{1,6}|[a-z]-\d{1,3}|\d{1,3}\s*of\s*\d{1,3})$", re.I
)
CURRENCY_PREFIX = frozenset({"$", "€", "£", "¥"})
CELL_SUFFIXES = frozenset({")", "%", ")%", "%)", "pts", "bps", "x"})
ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV"}

# Financial statements start at the first auditor's report, not the last (ICFR) one.
FIRST_TITLE_WINS = frozenset({"8"})
# Below this a heading only points elsewhere ("see pages 60-120") or says "None".
MIN_REAL_SECTION = 200
# A key item shorter than this is looked up by its title instead.
MIN_KEY_SECTION = 2000


@dataclass(frozen=True)
class Section:
    code: str  # "Item 1A" (10-K) or "Part II, Item 1A" (10-Q); "Cover" before the first item
    char_start: int
    char_end: int

    @property
    def length(self) -> int:
        return self.char_end - self.char_start


@dataclass(frozen=True)
class ParsedFiling:
    text: str
    sections: list[Section]

    def section(self, code: str) -> Section | None:
        """The longest section with this code (a pointer heading and the real body can
        both be labelled, e.g. JPM's "Item 7 … see pages" and its MD&A later on)."""
        found = [s for s in self.sections if s.code == code]
        return max(found, key=lambda s: s.length) if found else None


# -- HTML → text -------------------------------------------------------------------------


def _tag(el: etree._Element) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _hidden(el: etree._Element) -> bool:
    style = (el.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def _norm(s: str) -> str:
    s = s.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "").replace("\x00", "")
    return re.sub(r"\s+", " ", s).strip()


def _inner_text(el: etree._Element) -> str:
    """Visible text of an element (skipping hidden/dropped children), blocks spaced."""
    parts: list[str] = []

    def walk(node: etree._Element) -> None:
        tag = _tag(node)
        if tag and not _hidden(node) and tag not in DROP_TAGS:
            if node.text:
                parts.append(node.text)
            for child in node:
                walk(child)
            if tag in BLOCK_TAGS or tag == "br":
                parts.append(" ")
        if node.tail:
            parts.append(node.tail)

    if el.text:
        parts.append(el.text)
    for child in el:
        walk(child)
    return _norm("".join(parts))


def table_rows(table: etree._Element) -> list[str]:
    """One pipe-delimited line per row, spacer cells dropped, "$" and ")" glued on."""
    lines = []
    for tr in table.iter("tr"):
        cells = [_inner_text(c) for c in tr if _tag(c) in CELL_TAGS and not _hidden(c)]
        merged: list[str] = []
        prefix = ""
        for c in cells:
            if not c:
                continue
            if c in CURRENCY_PREFIX:
                prefix += c
                continue
            if c in CELL_SUFFIXES and merged and not prefix:
                merged[-1] += c
                continue
            merged.append(prefix + c)
            prefix = ""
        if prefix:
            merged.append(prefix)
        if merged:
            lines.append(" | ".join(merged))
    return lines


def html_to_lines(html: bytes | str) -> list[str]:
    """Visible text, one block (paragraph, table row, heading) per line."""
    parser = etree.HTMLParser(huge_tree=True, recover=True)
    root = etree.fromstring(html.encode() if isinstance(html, str) else html, parser)
    if root is None:
        return []
    out: list[str] = []

    def walk(el: etree._Element) -> None:
        tag = _tag(el)
        if not tag:  # comment / processing instruction: only its tail is text
            if el.tail:
                out.append(el.tail)
            return
        if tag in DROP_TAGS or _hidden(el):
            if el.tail:
                out.append(el.tail)
            return
        if tag == "table":
            out.append("\n")
            out.append("\n".join(table_rows(el)))
            out.append("\n")
        else:
            block = tag in BLOCK_TAGS
            if block or tag == "br":
                out.append("\n")
            if el.text:
                out.append(el.text)
            for child in el:
                walk(child)
            if block:
                out.append("\n")
        if el.tail:
            out.append(el.tail)

    walk(root)
    lines = (_norm(line) for line in "".join(out).split("\n"))
    return [line for line in lines if line]


def _heading_item(line: str) -> re.Match[str] | None:
    m = ITEM_RE.match(line)
    if m and HEADING_REST_RE.match(line[m.end() :]):
        return m
    return None


def _titled_item(line: str) -> bool:
    """An item heading that carries its title ("Item 1A. Risk Factors")."""
    m = _heading_item(line)
    return bool(m and re.search(r"[A-Za-z]{3}", line[m.end() :]))


def _title_line(line: str, keys: dict[tuple[str | None, str], str]) -> bool:
    flat = line.replace("’", "'").replace("`", "'")
    return any(_TITLE_CACHE(pat).match(flat) for pat in keys.values())


_TITLE_RES: dict[str, re.Pattern[str]] = {}


def _TITLE_CACHE(pat: str) -> re.Pattern[str]:
    if pat not in _TITLE_RES:
        _TITLE_RES[pat] = re.compile(
            r"^(?:part\s+[iv]+\s*[,.:\-–—|]?\s*)?(?:items?\s*\d{1,2}[a-c]?\s*[.:\-–—|]?\s*)?"
            rf"(?:{pat})\s*[.:]?$",
            re.IGNORECASE,
        )
    return _TITLE_RES[pat]


def _furniture_key(line: str) -> str:
    """Running footers differ only by page number: "Apple Inc. | 2025 Form 10-K | 23"."""
    return TRAILING_NUMBER_RE.sub("#", line)


def drop_furniture(lines: list[str]) -> list[str]:
    """Remove page numbers and running headers/footers.

    A short line (≤ 100 chars) is furniture if it repeats, ignoring a trailing page
    number, ≥ 3 times; table-like lines (with " | ") need ≥ 10 repeats, so real table
    rows are kept. Item headings with a title and Part headings are never removed. A
    repeated bare title ("Risk Factors" atop every page) keeps its first occurrence:
    that one is the heading.
    """
    counts = Counter(_furniture_key(line) for line in lines if len(line) <= 100)
    repeated = {
        key
        for key, n in counts.items()
        if n >= (10 if " | " in key else 3) and not _titled_item(key) and not PART_RE.match(key)
    }
    titles = {**KEY_TITLES_10K, **KEY_TITLES_10Q}
    kept_first: set[str] = set()
    out = []
    for line in lines:
        key = _furniture_key(line) if len(line) <= 100 else None
        if key in repeated:
            if line not in kept_first and _title_line(line, titles):
                kept_first.add(line)
                out.append(line)
            continue
        if PAGE_NUMBER_RE.match(line):
            continue
        out.append(line)
    return out


def clean_text(html: bytes | str) -> str:
    return "\n".join(drop_furniture(html_to_lines(html)))


# -- sections ----------------------------------------------------------------------------

Key = tuple[str | None, str]  # (part for 10-Q else None, item)


@dataclass(frozen=True)
class _Line:
    pos: int
    text: str
    part: str | None


def _lines(text: str) -> list[_Line]:
    out = []
    pos = 0
    part: str | None = None
    for line in text.split("\n"):
        pm = PART_RE.match(line)
        if pm and HEADING_REST_RE.match(line[pm.end() :]):
            p = pm.group(1).upper()
            part = ROMAN.get(p, p)
        out.append(_Line(pos, line, part))
        pos += len(line) + 1
    return out


def _item_of(line: str) -> str | None:
    """Item number of an item heading line, also inside "PART I — Item 1. …"."""
    pm = PART_RE.match(line)
    if pm:
        line = line[pm.end() :].lstrip(" .:-–—|,")
    m = _heading_item(line)
    return (m.group(1) + (m.group(2) or "")).upper() if m else None


def _chain(cands: list[tuple[int, int, float]]) -> list[int]:
    """Longest weighted chain of candidates increasing in both position and item order.

    `cands` are (pos, item_order, weight) sorted by pos. Returns chosen indexes.
    """
    n = len(cands)
    best = [0.0] * n
    prev = [-1] * n
    for j, (_, oj, wj) in enumerate(cands):
        best[j] = wj
        for i in range(j):
            if cands[i][1] < oj and best[i] + wj > best[j]:
                best[j] = best[i] + wj
                prev[j] = i
    if not n:
        return []
    j = max(range(n), key=lambda k: (best[k], cands[k][0]))
    picked = []
    while j != -1:
        picked.append(j)
        j = prev[j]
    return picked[::-1]


def detect_sections(text: str, form: str) -> list[Section]:
    """Section spans that tile `text` (text before the first item is "Cover").

    1. Item headings: lines starting "Item 7." / "ITEM 7 —" / "PART I — Item 2." whose
       next word is punctuation or a capitalised title. Lines that end in a page number
       (table of contents, cross-reference index) are ignored.
    2. The table of contents lists every item first, so per item there are several
       candidates. We pick the chain of candidates that is increasing in both position
       and item order and has the highest weight, where a candidate weighs 1, or 1.5 if
       it starts a real section (≥ MIN_REAL_SECTION chars to the next heading). Ties go
       to later positions: the **last** heading that starts a real section wins.
    3. Key items (Risk Factors, MD&A, market risk, financial statements) that are
       missing or shorter than MIN_KEY_SECTION are looked up by their bare title line
       ("RISK FACTORS", "Management's discussion and analysis"). The last title line
       that starts a real section (≥ MIN_KEY_SECTION chars to the next boundary) wins,
       except for financial statements, where the *first* auditor's report starts them
       (the second one is the internal-control opinion). The short pointer section
       keeps its label too, so a code can appear twice.
    """
    is_q = form.upper().startswith("10-Q")
    expected: list[Key]
    if is_q:
        expected = [(p, i) for p, items in ITEMS_10Q.items() for i in items]
        key_titles = KEY_TITLES_10Q
    else:
        expected = [(None, i) for i in ITEMS_10K]
        key_titles = KEY_TITLES_10K
    order = {k: n for n, k in enumerate(expected)}
    lines = _lines(text)

    heads: list[tuple[int, Key]] = []
    for ln in lines:
        item = _item_of(ln.text)
        if item is None or TOC_LINE_RE.search(ln.text):
            continue
        key: Key = (ln.part if is_q else None, item)
        if key in order:
            heads.append((ln.pos, key))

    def span(idx: int) -> int:
        nxt = heads[idx + 1][0] if idx + 1 < len(heads) else len(text)
        return nxt - heads[idx][0]

    cands = [
        (pos, order[k], 1.0 + (0.5 if span(i) >= MIN_REAL_SECTION else 0.0) + pos * 1e-12)
        for i, (pos, k) in enumerate(heads)
    ]
    starts: list[tuple[int, Key]] = [heads[i] for i in _chain(cands)]

    # 3. title fallback for key items
    title_cands: dict[Key, list[int]] = {k: [] for k in key_titles}
    for ln in lines:
        if len(ln.text) > 160 or TOC_LINE_RE.search(ln.text):
            continue
        flat = ln.text.replace("’", "'").replace("`", "'")
        for k, pat in key_titles.items():
            if _TITLE_CACHE(pat).match(flat):
                title_cands[k].append(ln.pos)
    # How long is each chosen item really? Measured to the next heading *or* title line,
    # so a pointer followed (much later) only by title-headed sections still looks short.
    all_bounds = sorted({p for p, _ in heads} | {p for ps in title_cands.values() for p in ps})

    def length_of(k: Key) -> int:
        lengths = [
            next((q for q in all_bounds if q > p), len(text)) - p for p, kk in starts if kk == k
        ]
        return max(lengths, default=0)

    weak = [k for k in key_titles if length_of(k) < MIN_KEY_SECTION]
    if weak:
        bounds = sorted({p for p, _ in starts} | {p for k in weak for p in title_cands[k]})
        for k in weak:
            # the last title line that starts a real section (same rule as item headings)
            real = [
                p
                for p in title_cands[k]
                if next((q for q in bounds if q > p), len(text)) - p >= MIN_KEY_SECTION
            ]
            if real:
                starts.append((real[0] if k[1] in FIRST_TITLE_WINS else real[-1], k))

    starts = sorted(set(starts))
    # two labels at the same position: keep the first (item heading over title)
    dedup: list[tuple[int, Key]] = []
    for p, k in starts:
        if not dedup or dedup[-1][0] != p:
            dedup.append((p, k))
    if not dedup:
        return [Section("Cover", 0, len(text))] if text else []
    sections: list[Section] = []
    if dedup[0][0] > 0:
        sections.append(Section("Cover", 0, dedup[0][0]))
    for j, (pos, (part, item)) in enumerate(dedup):
        end = dedup[j + 1][0] if j + 1 < len(dedup) else len(text)
        sections.append(Section(section_code(part, item), pos, end))
    return sections


def section_code(part: str | None, item: str) -> str:
    return f"Part {part}, Item {item}" if part else f"Item {item}"


def section_title(code: str) -> str:
    """Human name for a section code: "Item 1A" → "Risk Factors"."""
    m = re.fullmatch(r"(?:Part (\w+), )?Item (\w+)", code)
    if not m:
        return code
    part, item = m.groups()
    if part:
        return TITLES_10Q.get((part, item), code)
    return TITLES_10K.get(item, code)


def parse_filing(html: bytes | str, form: str) -> ParsedFiling:
    text = clean_text(html)
    return ParsedFiling(text=text, sections=detect_sections(text, form))


def parse_file(path: Path | str, form: str) -> ParsedFiling:
    return parse_filing(Path(path).read_bytes(), form)
