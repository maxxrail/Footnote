"""Chunker: section-bounded, overlapping, exact spans."""

from __future__ import annotations

import itertools

import pytest

from analyst.retrieval.chunk import chunk_filing, chunk_section, count_tokens
from analyst.retrieval.parse import ParsedFiling, Section
from tests.test_parse import fixture_filing


@pytest.fixture(scope="module")
def aapl() -> ParsedFiling:
    return fixture_filing("aapl-10k-20250927.htm.gz", "10-K")


def test_spans_round_trip(aapl: ParsedFiling) -> None:
    chunks = chunk_filing(aapl)
    assert len(chunks) > 50
    for c in chunks:
        assert aapl.text[c.char_start : c.char_end] == c.text
        assert c.text == c.text.strip()


def test_chunks_never_cross_sections(aapl: ParsedFiling) -> None:
    for c in chunk_filing(aapl):
        sec = next(s for s in aapl.sections if s.char_start <= c.char_start < s.char_end)
        assert sec.code == c.section
        assert c.char_end <= sec.char_end


def test_every_section_char_is_covered(aapl: ParsedFiling) -> None:
    covered = bytearray(len(aapl.text))
    for c in chunk_filing(aapl):
        covered[c.char_start : c.char_end] = b"\x01" * (c.char_end - c.char_start)
    missing = [i for i, ch in enumerate(aapl.text) if not covered[i] and not ch.isspace()]
    assert missing == []


def test_size_and_overlap(aapl: ParsedFiling) -> None:
    risk = aapl.section("Item 1A")
    assert risk is not None
    chunks = chunk_section(aapl.text, risk, max_tokens=800, overlap_tokens=100)
    sizes = [count_tokens(c.text) for c in chunks]
    assert max(sizes) <= 800
    assert sum(sizes[:-1]) / len(sizes[:-1]) > 600  # packed, not tiny
    for a, b in itertools.pairwise(chunks):
        assert b.char_start > a.char_start  # always moves forward
        overlap = aapl.text[b.char_start : a.char_end] if b.char_start < a.char_end else ""
        assert count_tokens(overlap) <= 100
    # consecutive chunks share text (overlap exists for paragraph-sized lines)
    assert sum(b.char_start < a.char_end for a, b in itertools.pairwise(chunks)) > 0


def test_long_line_is_split_by_sentences_then_words() -> None:
    sentence = "word " * 50 + "end."
    text = " ".join([sentence] * 40)  # one 2,000+ token line
    sec = Section("Item 7", 0, len(text))
    chunks = chunk_section(text, sec, max_tokens=200, overlap_tokens=20)
    assert all(count_tokens(c.text) <= 200 for c in chunks)
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    one_line = "w " * 1000
    chunks = chunk_section(one_line, Section("Item 7", 0, len(one_line)), 300, 30)
    assert all(count_tokens(c.text) <= 300 for c in chunks)


def test_count_tokens() -> None:
    assert count_tokens("Net sales were $416,161 million.") == 9
