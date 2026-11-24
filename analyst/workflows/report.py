"""Shared pieces for document workflows: footnotes, agent sections, PDF building.

A document collects `Footnotes`: every agent answer's `[^n]` markers are renumbered into
one document-wide list, and deterministic tables add their own (one per table row, listing
the facts behind it). Agent sections that the number post-check flagged, or that refused,
say so in the text instead of passing silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analyst.agent.ask import describe
from analyst.agent.citations import AnyCitation
from analyst.agent.loop import Answer
from analyst.agent.postcheck import MARKER


@dataclass
class Footnotes:
    entries: list[str] = field(default_factory=list)

    def add(self, text: str) -> int:
        self.entries.append(text)
        return len(self.entries)

    def add_citation(self, c: AnyCitation) -> int:
        return self.add(describe(c))

    def absorb(self, answer: Answer) -> str:
        """The answer's text with its markers renumbered into this document's list."""
        mapping = {i: self.add_citation(c) for i, c in enumerate(answer.citations, 1)}
        return MARKER.sub(lambda m: f"[^{mapping.get(int(m.group(1)), m.group(1))}]", answer.text)

    def markdown(self) -> str:
        return "\n".join(f"[^{i}]: {e}" for i, e in enumerate(self.entries, 1))


def agent_section(answer: Answer | None, notes: Footnotes, skipped: str) -> str:
    """Markdown for an agent-written section, with its check status made visible."""
    if answer is None:
        return f"_{skipped}_"
    text = notes.absorb(answer)
    flags = []
    if answer.refused:
        flags.append("the agent found this is not in the filings")
    if answer.unverified_numbers:
        flags.append(f"unverified numbers {answer.unverified_numbers}")
    if answer.uncited_numbers or answer.miscited_numbers:
        flags.append(f"citation problems {answer.uncited_numbers + answer.miscited_numbers}")
    if flags:
        text += f"\n\n_Number check: {'; '.join(flags)}._"
    return text


# -- PDF -------------------------------------------------------------------------------------


def md_inline_to_rl(text: str) -> str:
    """Markdown bold and footnote markers -> ReportLab paragraph markup (escaped)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    runs = re.compile(r"(?:\[\^\d+\])+")  # adjacent markers -> one superscript "4,5,6"
    return runs.sub(lambda m: f"<super>{','.join(MARKER.findall(m.group(0)))}</super>", text)


def pdf_styles() -> dict[str, Any]:
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=8, leading=10)
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=15, leading=18, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=body, fontSize=8.5, textColor="#444444"),
        "h": ParagraphStyle(
            "h", parent=base["Heading3"], fontSize=9.5, leading=12, spaceBefore=5, spaceAfter=2
        ),
        "body": body,
        "bullet": ParagraphStyle("b", parent=body, leftIndent=8, bulletIndent=0),
        "small": ParagraphStyle("sm", parent=body, fontSize=6.5, leading=8),
    }


def markdown_blocks(md: str, styles: dict[str, Any]) -> list[Any]:
    """A section's Markdown (paragraphs and '- ' bullets) as ReportLab flowables."""
    from reportlab.platypus import Paragraph

    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if lines and all(re.match(r"\s*(?:[-*]|\d+[.)])\s", ln) for ln in lines):
            for ln in lines:
                item = re.sub(r"^\s*(?:[-*]|\d+[.)])\s", "", ln)
                out.append(Paragraph(md_inline_to_rl(item), styles["bullet"], bulletText="•"))
        else:
            out.append(Paragraph(md_inline_to_rl(" ".join(lines)), styles["body"]))
    return out


def page_count(path: Path) -> int:
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page[^s]", data))
