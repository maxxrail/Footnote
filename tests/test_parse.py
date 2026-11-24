"""Parser: HTML cleaning and section detection on recorded filings and small HTML."""

from __future__ import annotations

import gzip
from functools import cache

import pytest

from analyst.retrieval.parse import (
    ParsedFiling,
    clean_text,
    detect_sections,
    parse_filing,
    section_title,
)
from tests.conftest import FIXTURES


@cache
def fixture_filing(name: str, form: str) -> ParsedFiling:
    return parse_filing(gzip.decompress((FIXTURES / "filings" / name).read_bytes()), form)


TEN_KS = [
    # (fixture, risk factors chars, MD&A chars): lengths from the real documents ± 10%
    ("aapl-10k-20250927.htm.gz", 68_000, 18_100),
    ("msft-10k-20260630.htm.gz", 81_000, 50_900),
]


@pytest.mark.parametrize(("name", "risk_len", "mdna_len"), TEN_KS)
def test_10k_finds_risk_factors_and_mdna_not_toc(name: str, risk_len: int, mdna_len: int) -> None:
    p = fixture_filing(name, "10-K")
    risk, mdna = p.section("Item 1A"), p.section("Item 7")
    assert risk is not None and mdna is not None
    # sensible lengths: the real sections, not a table-of-contents line
    assert risk.length == pytest.approx(risk_len, rel=0.1)
    assert mdna.length == pytest.approx(mdna_len, rel=0.1)
    # the TOC sits in the first few thousand chars; the chosen headings come after it
    cover = p.section("Cover")
    assert cover is not None and cover.length > 3000
    assert risk.char_start > cover.char_end - 1
    # the heading line itself starts the span, followed by body text
    assert p.text[risk.char_start :].upper().startswith("ITEM 1A. RISK FACTORS")
    assert p.text[mdna.char_start :].upper().startswith("ITEM 7. MANAGEMENT")
    assert "|" not in p.text[risk.char_start : p.text.index("\n", risk.char_start)]


@pytest.mark.parametrize(("name", "risk_len", "mdna_len"), TEN_KS)
def test_10k_sections_are_ordered_and_tile_the_text(
    name: str, risk_len: int, mdna_len: int
) -> None:
    p = fixture_filing(name, "10-K")
    codes = [s.code for s in p.sections]
    assert codes[:4] == ["Cover", "Item 1", "Item 1A", "Item 1B"]
    assert codes[-1] == "Item 16"
    assert p.sections[0].char_start == 0
    assert p.sections[-1].char_end == len(p.text)
    for a, b in zip(p.sections, p.sections[1:], strict=False):
        assert a.char_end == b.char_start


def test_10q_parts_and_items() -> None:
    p = fixture_filing("aapl-10q-20260328.htm.gz", "10-Q")
    codes = [s.code for s in p.sections]
    assert codes[:3] == ["Cover", "Part I, Item 1", "Part I, Item 2"]
    assert "Part II, Item 1A" in codes
    mdna = p.section("Part I, Item 2")
    risk = p.section("Part II, Item 1A")
    assert mdna is not None and mdna.length > 15_000
    assert risk is not None and risk.length > 15_000
    assert mdna.char_start < risk.char_start


def test_clean_text_drops_xbrl_header_and_page_furniture() -> None:
    p = fixture_filing("aapl-10k-20250927.htm.gz", "10-K")
    assert "dei:AmendmentFlag" not in p.text
    assert "2025 Form 10-K |" not in p.text  # running footer on every page
    assert "\n23\n" not in p.text  # bare page numbers
    # inline XBRL numbers stay visible
    assert "416,161" in p.text


def test_tables_become_pipe_rows() -> None:
    html = """<html><body><table>
      <tr><td>Net sales</td><td></td><td>$</td><td>416,161</td><td></td><td>(5</td><td>)%</td></tr>
      <tr><td></td><td></td></tr>
    </table></body></html>"""
    assert clean_text(html) == "Net sales | $416,161 | (5)%"


def test_hidden_and_ix_header_dropped_inline_ix_unwrapped() -> None:
    html = (
        "<html><body><ix:header>XBRL HEADER</ix:header>"
        '<div style="display: none">HIDDEN BLOCK</div>'
        '<p>Revenue was <ix:nonFraction name="us-gaap:Revenues">12</ix:nonFraction> million.</p>'
        "</body></html>"
    )
    assert clean_text(html) == "Revenue was 12 million."


def _doc(*lines: str) -> str:
    return "\n".join(lines)


BODY = "x" * 2500


def test_toc_lines_with_page_numbers_are_skipped() -> None:
    text = _doc(
        "Item 1A. | Risk Factors | 15",
        "Item 7. | Management's Discussion and Analysis | 41",
        "Item 1A. Risk Factors",
        BODY,
        "Item 7. Management's Discussion and Analysis",
        BODY,
    )
    secs = {s.code: s for s in detect_sections(text, "10-K")}
    assert text[secs["Item 1A"].char_start :].startswith("Item 1A. Risk Factors\n")
    assert secs["Item 1A"].length > 2500


def test_cross_reference_index_at_the_end_is_ignored() -> None:
    # INTC: a Form 10-K cross-reference index *after* the body, rows ending in pages
    text = _doc(
        "Item 1A. Risk Factors",
        BODY,
        "Item 7. Management's Discussion and Analysis",
        BODY,
        "Item 1A. | Risk Factors | Pages 37-51",
        "Item 7. | Management's Discussion and Analysis | Pages 20-36",
        "Item 8. | Financial Statements and Supplementary Data | Pages 56-108",
    )
    for s in detect_sections(text, "10-K"):
        first_line = text[s.char_start :].split("\n", 1)[0]
        assert "Pages" not in first_line, s


def test_equal_weight_ties_go_to_the_later_heading() -> None:
    # every section is short, so TOC and body chains weigh the same: body must win
    text = _doc(
        "Item 1A.",
        "Item 7.",
        "Cover text.",
        "Item 1A. Risk Factors",
        "None of note.",
        "Item 7. Management's Discussion and Analysis",
        "Short.",
    )
    secs = {s.code: s for s in detect_sections(text, "10-K")}
    assert text[secs["Item 1A"].char_start :].startswith("Item 1A. Risk Factors")
    assert text[secs["Item 7"].char_start :].startswith("Item 7. Management")


def test_missing_body_heading_does_not_pull_items_into_toc() -> None:
    # ADSK: the TOC has an Item 16 the body never repeats. Choosing it must not force
    # every other item back into the TOC.
    text = _doc(
        "Item 1.",
        "Item 1A.",
        "Item 16.",
        "Item 1. Business",
        BODY,
        "Item 1A. Risk Factors",
        BODY,
    )
    secs = {s.code: s for s in detect_sections(text, "10-K")}
    assert secs["Item 1A"].length > 2500
    assert secs["Item 1"].length > 2500


def test_prose_mentions_are_not_headings() -> None:
    text = _doc(
        "Item 1A. Risk Factors",
        BODY,
        "Item 1A of our Annual Report describes these risks in more detail.",
        BODY,
        "Item 7. Management's Discussion and Analysis",
        BODY,
    )
    secs = {s.code: s for s in detect_sections(text, "10-K")}
    assert secs["Item 1A"].char_start == 0
    assert secs["Item 1A"].length > 5000


def test_title_fallback_for_pointer_items() -> None:
    # JPM/Citi style: "Item 7" is a pointer, MD&A is under a bare title later on.
    text = _doc(
        "Item 1A. Risk Factors",
        BODY,
        "Item 7. Management's Discussion and Analysis",
        "See pages 50-160.",
        "Item 8. Financial Statements and Supplementary Data",
        "See pages 170-300.",
        "Management’s discussion and analysis",
        BODY,
        "Report of Independent Registered Public Accounting Firm",
        BODY,
        "Report of Independent Registered Public Accounting Firm",  # ICFR opinion
        BODY,
    )
    p = ParsedFiling(text, detect_sections(text, "10-K"))
    mdna = p.section("Item 7")
    assert mdna is not None and text[mdna.char_start :].startswith("Management’s discussion")
    assert mdna.length > 2500
    fs = p.section("Item 8")
    # financial statements start at the *first* auditor's report
    assert fs is not None and fs.char_start == text.index("Report of Independent")
    # the pointer keeps its label too
    assert sum(s.code == "Item 7" for s in p.sections) == 2


def test_running_header_bare_item_lines_removed() -> None:
    html = (
        "<html><body>"
        + "".join(f"<p>Item 1A</p><p>Risk paragraph {i}.</p>" for i in range(5))
        + "</body></html>"
    )
    assert "Item 1A" not in clean_text(html)


def test_section_title() -> None:
    assert section_title("Item 1A") == "Risk Factors"
    assert section_title("Part I, Item 2") == "Management's Discussion and Analysis"
    assert section_title("Cover") == "Cover"
