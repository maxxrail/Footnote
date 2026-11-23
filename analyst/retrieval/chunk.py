"""Split a parsed filing into section-bounded, overlapping chunks with exact spans.

Every chunk satisfies `filing_text[char_start:char_end] == chunk.text`, and lies inside
one section. Sizes are measured in estimated tokens (`count_tokens`), packing whole
lines (paragraphs, table rows) and falling back to sentences, then words, for lines
that are longer than a chunk on their own.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from analyst.retrieval.parse import ParsedFiling, Section

MAX_TOKENS = 800
OVERLAP_TOKENS = 100

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")
_WORD_RE = re.compile(r"\S+")


def count_tokens(text: str) -> int:
    """Word pieces + punctuation: within ~10% of the BGE WordPiece count on filings
    (it splits "1,234" into 3 and most words into 1), with no tokenizer to load."""
    return len(_TOKEN_RE.findall(text))


@dataclass(frozen=True)
class Chunk:
    section: str
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    tokens: int


def _units(text: str, start: int, end: int, max_tokens: int) -> Iterator[_Unit]:
    """Lines in text[start:end]; a line longer than max_tokens is split into sentences,
    and a sentence longer than that into word windows."""
    pos = start
    while pos < end:
        nl = text.find("\n", pos, end)
        line_end = end if nl == -1 else nl
        if line_end > pos:
            yield from _split_long(text, pos, line_end, max_tokens)
        pos = line_end + 1


def _split_long(text: str, start: int, end: int, max_tokens: int) -> Iterator[_Unit]:
    n = count_tokens(text[start:end])
    if n <= max_tokens:
        yield _Unit(start, end, n)
        return
    pieces: list[tuple[int, int]] = []
    pos = start
    for m in _SENTENCE_END_RE.finditer(text, start, end):
        pieces.append((pos, m.start()))
        pos = m.end()
    pieces.append((pos, end))
    for s, e in pieces:
        sn = count_tokens(text[s:e])
        if sn <= max_tokens:
            yield _Unit(s, e, sn)
            continue
        words = [(m.start(), m.end()) for m in _WORD_RE.finditer(text, s, e)]
        window: list[tuple[int, int]] = []
        wn = 0
        for w in words:
            t = count_tokens(text[w[0] : w[1]])
            if window and wn + t > max_tokens:
                yield _Unit(window[0][0], window[-1][1], wn)
                window, wn = [], 0
            window.append(w)
            wn += t
        if window:
            yield _Unit(window[0][0], window[-1][1], wn)


def chunk_section(
    text: str,
    section: Section,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    units = list(_units(text, section.char_start, section.char_end, max_tokens))
    chunks: list[Chunk] = []
    i = 0
    while i < len(units):
        j = i
        total = 0
        while j < len(units) and (j == i or total + units[j].tokens <= max_tokens):
            total += units[j].tokens
            j += 1
        s, e = units[i].start, units[j - 1].end
        chunks.append(Chunk(section.code, s, e, text[s:e]))
        if j >= len(units):
            break
        # back up over trailing units worth ≤ overlap_tokens, but always move forward
        k = j
        back = 0
        while k - 1 > i and back + units[k - 1].tokens <= overlap_tokens:
            k -= 1
            back += units[k].tokens
        i = k
    return chunks


def chunk_filing(
    parsed: ParsedFiling,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    out: list[Chunk] = []
    for section in parsed.sections:
        out.extend(chunk_section(parsed.text, section, max_tokens, overlap_tokens))
    return out
