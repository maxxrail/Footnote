"""Earnings brief: the latest 10-Q quarter vs the same quarter a year earlier.

    uv run python -m analyst.workflows run earnings --ticker JPM

1. **Quarter**: the latest 10-Q with stored text, and the fiscal quarter it reports. The
   current point is the one that 10-Q reports. The comparison is the same fiscal quarter a
   year earlier, latest-filing value.
2. **Key metric deltas** (deterministic, `analyst.metrics`):
   - YoY growth of revenue, gross profit, operating income, net income and diluted EPS;
   - the change in gross, operating and net margin, in percentage points.
3. **MD&A drivers** (agent): the 10-Q's Part I, Item 2 excerpts plus the computed deltas,
   given as citable source material. Every number must match one of them.
4. **What changed in risk factors**: the 10-Q's Part II, Item 1A is diffed paragraph by
   paragraph against the latest 10-K before it. A paragraph with no close match (difflib
   ratio < 0.85) is new. With no new paragraphs, the brief cites the 10-Q's own statement
   (usually "no material changes") and calls no model. Otherwise only the new paragraphs go
   to the agent to summarize.

Output: `earnings_{TICKER}_FY{year}Q{q}.md`.
"""

from __future__ import annotations

import difflib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, field_validator
from sqlalchemy import select

from analyst.agent.tools import Excerpt, ToolError, display, load_universe
from analyst.db.models import Company
from analyst.edgar.concepts import SeriesPoint, fiscal_period
from analyst.metrics import finance as fin
from analyst.metrics.computed import Computed, Operand
from analyst.metrics.quarters import DerivedPoint, quarterly_series
from analyst.workflows.base import Context, State, Step, Workflow
from analyst.workflows.comps import fact_details
from analyst.workflows.report import Footnotes, agent_section
from analyst.workflows.sources import (
    latest_filing,
    latest_filing_with_section,
    section_chunks,
    section_text,
    span_citation,
    to_excerpt,
)

METRICS = [
    ("revenue", "Revenue"),
    ("gross_profit", "Gross profit"),
    ("operating_income", "Operating income"),
    ("net_income", "Net income"),
    ("eps_diluted", "Diluted EPS"),
]
MARGINS = [
    ("gross_profit", "Gross margin"),
    ("operating_income", "Operating margin"),
    ("net_income", "Net margin"),
]
MDNA = "Part I, Item 2"
RISKS_10Q = "Part II, Item 1A"
MDNA_CHUNKS = 6
NEW_PARAGRAPH_RATIO = 0.85
MIN_PARAGRAPH = 80
MAX_NEW_PARAGRAPHS = 8
NO_CHANGE = re.compile(r"no material changes?", re.I)

Point = SeriesPoint | DerivedPoint


class EarningsInputs(BaseModel):
    ticker: str

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


# -- steps -----------------------------------------------------------------------------------


def _quarter(state: State, ctx: Context) -> State:
    t = state["inputs"].ticker
    if t not in {r.ticker for r in load_universe(ctx.universe_path)}:
        raise ToolError(f"{t} is not in the covered universe")
    with ctx.session_factory() as s:
        co = s.scalar(select(Company).where(Company.ticker == t))
        if co is None:
            raise ToolError(f"{t} is in the universe but has no data loaded")
        tenq = latest_filing(s, co.cik, ["10-Q", "10-Q/A"])
        if tenq is None:
            raise ToolError(f"no 10-Q text for {t} in the corpus")
        series = {k: quarterly_series(s, co.cik, k) for k, _ in METRICS}
        prior_10k = latest_filing_with_section(
            s, co.cik, ["10-K", "10-K/A"], "Item 1A", tenq.filed_at
        )
        fye = co.fiscal_year_end
        state["company"] = {"cik": co.cik, "ticker": co.ticker, "name": co.name}
        state["tenq"] = {"accession": tenq.accession, "filed_at": tenq.filed_at, "url": tenq.url}
        state["tenk"] = (
            {"accession": prior_10k.accession, "filed_at": prior_10k.filed_at}
            if prior_10k
            else None
        )
    # The quarter is the one the latest 10-Q reports: its own revenue point, else its period
    # of report. Never fall back to some other quarter (an old derived Q4 would be compared
    # while citing this 10-Q's MD&A).
    current = _reported_by(series["revenue"], tenq.accession)
    if current is not None:
        fy, fq = current.fiscal_year, current.fiscal_quarter
    elif tenq.period_of_report is not None:
        fy, fq = fiscal_period(tenq.period_of_report, fye)
    else:
        raise ToolError(
            f"can't tell which quarter {t}'s latest 10-Q ({tenq.accession}) reports: no revenue "
            "fact and no period of report"
        )
    state["label"] = f"FY{fy} Q{fq}"
    state["prior_label"] = f"FY{fy - 1} Q{fq}"
    state["points"] = {k: (_at(pts, fy, fq), _at(pts, fy - 1, fq)) for k, pts in series.items()}
    return state


def _reported_by(points: list[Point], accession: str) -> Point | None:
    mine = [p for p in points if isinstance(p, SeriesPoint) and p.accession == accession]
    return max(mine, key=lambda p: p.period_end) if mine else None


def _at(points: list[Point], fy: int, fq: int | None) -> Point | None:
    return next((p for p in points if p.fiscal_year == fy and p.fiscal_quarter == fq), None)


def _op(ticker: str, label: str, p: Point | None) -> Operand | None:
    if p is None:
        return None
    if isinstance(p, DerivedPoint):
        ids = tuple(i.fact_id for i in p.inputs)
        return Operand(
            f"{ticker} {p.canonical} {label}",
            p.value,
            ids,
            "derived",
            p.method,
            p.period_start,
            p.period_end,
            p.unit,
        )
    return Operand(
        f"{ticker} {p.canonical} {label}",
        p.value,
        (p.fact_id,),
        "fact",
        p.accession,
        p.period_start,
        p.period_end,
        p.unit,
    )


def _deltas(state: State, ctx: Context) -> State:
    t, now, before = state["company"]["ticker"], state["label"], state["prior_label"]
    ops = {k: (_op(t, now, a), _op(t, before, b)) for k, (a, b) in state["points"].items()}
    rows: list[dict[str, Any]] = []
    for key, label in METRICS:
        cur, pri = ops[key]
        g = fin.growth(cur, pri, f"{key}_growth")
        g = Computed(
            g.metric,
            g.value,
            g.formula,
            g.inputs,
            g.unit,
            g.note,
            f"{t} {label} growth {before} to {now}",
        )
        rows.append({"label": label, "current": cur, "prior": pri, "change": g, "kind": "growth"})
    for key, label in MARGINS:
        rev_now, rev_before = ops["revenue"]
        m_now = fin.margin(ops[key][0], rev_now, key)
        m_before = fin.margin(ops[key][1], rev_before, key)
        cur = m_now.as_operand(f"{t} {label} {now}") if m_now.value is not None else None
        pri = m_before.as_operand(f"{t} {label} {before}") if m_before.value is not None else None
        ch = fin.difference(cur, pri, f"{key}_margin_change")
        ch = Computed(
            ch.metric,
            ch.value,
            ch.formula,
            ch.inputs,
            "ratio",
            ch.note,
            f"{t} {label} change {before} to {now} (percentage points)",
        )
        rows.append(
            {
                "label": label,
                "current": cur,
                "prior": pri,
                "change": ch,
                "kind": "pp",
                "m_now": m_now,
                "m_before": m_before,
            }
        )
    state["deltas"] = rows
    return state


def _mdna(state: State, ctx: Context) -> State:
    name, t = state["company"]["name"], state["company"]["ticker"]
    with ctx.session_factory() as s:
        cites = section_chunks(s, state["tenq"]["accession"], MDNA, MDNA_CHUNKS)
        excerpts: list[Excerpt] = [to_excerpt(s, c) for c in cites]
        box = ctx.toolbox()
        computed = [
            box.computed_excerpt(s, r["change"], t)
            for r in state["deltas"]
            if r["change"].value is not None
        ]
    if not excerpts or ctx.llm is None:
        state["mdna"] = None
        state["mdna_note"] = (
            "Agent step skipped (no LLM)." if excerpts else "Not in the filings: no MD&A text."
        )
        return state
    state["mdna"] = ctx.ask(
        f"What drove {name}'s results in {state['label']} compared with {state['prior_label']}? "
        "Give 3 to 5 Markdown bullets, each one sentence, each cited. Take the drivers from the "
        "10-Q MD&A excerpts, and take any number from the computed deltas or the excerpts "
        f"exactly as given (search_filings with ticker {t} and section '{MDNA}' if needed).",
        state,
        [*computed, *excerpts],
    )
    return state


def _paragraphs(text: str, offset: int) -> list[tuple[str, int, int]]:
    """(paragraph, char_start, char_end) for paragraphs of at least MIN_PARAGRAPH chars."""
    out = []
    for m in re.finditer(r"[^\n]+", text):
        para = m.group(0).strip()
        if len(para) >= MIN_PARAGRAPH:
            lead = len(m.group(0)) - len(m.group(0).lstrip())
            start = offset + m.start() + lead
            out.append((para, start, start + len(para)))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def new_paragraphs(
    quarter: list[tuple[str, int, int]],
    annual: list[tuple[str, int, int]],
    annual_text: str = "",
) -> list[tuple[str, int, int]]:
    """Quarter paragraphs that aren't in the annual. A paragraph is not new if its text
    appears anywhere in the annual (10-Q text is often cut at page breaks, so it can be a
    piece of a longer 10-K paragraph) or a 10-K paragraph matches it closely (difflib
    ratio >= NEW_PARAGRAPH_RATIO)."""
    annual_norm = [_norm(p) for p, _, _ in annual]
    exact = set(annual_norm)
    whole = _norm(annual_text) if annual_text else " ".join(annual_norm)
    out = []
    for para, start, end in quarter:
        q = _norm(para)
        if q in exact or q in whole:
            continue
        best = 0.0
        for a in annual_norm:
            sm = difflib.SequenceMatcher(None, q, a, autojunk=False)
            if (
                sm.real_quick_ratio() < NEW_PARAGRAPH_RATIO
                or sm.quick_ratio() < NEW_PARAGRAPH_RATIO
            ):
                continue
            best = max(best, sm.ratio())
            if best >= NEW_PARAGRAPH_RATIO:
                break
        if best < NEW_PARAGRAPH_RATIO:
            out.append((para, start, end))
    return out


def _risk_changes(state: State, ctx: Context) -> State:
    acc_q = state["tenq"]["accession"]
    with ctx.session_factory() as s:
        q = section_text(s, acc_q, RISKS_10Q)
        if q is None:
            state["risk_result"] = {
                "kind": "none",
                "note": "No Part II, Item 1A section was found in the 10-Q's text (it may be "
                "missing or the parser may have missed its heading); risk changes not assessed.",
            }
            return state
        q_text, q_start = q
        q_paras = _paragraphs(q_text, q_start)
        k = section_text(s, state["tenk"]["accession"], "Item 1A") if state["tenk"] else None
        if k is None:
            state["risk_result"] = {
                "kind": "none",
                "note": "No earlier 10-K with an Item 1A was found, so there is nothing to "
                "compare the 10-Q's risk factors with.",
            }
            return state
        k_paras = _paragraphs(*k)
        new = new_paragraphs(q_paras, k_paras, annual_text=k[0])
        # "There have been no material changes to the risk factors..." is new text relative
        # to the 10-K but says nothing changed. Unchanged = no new paragraph but such statements.
        statements = [p for p in new if NO_CHANGE.search(p[0])]
        new = [p for p in new if not NO_CHANGE.search(p[0])]
        state["risk_stats"] = {
            "quarter_paragraphs": len(q_paras),
            "annual_paragraphs": len(k_paras),
            "new": len(new),
        }
        if not new:
            fallback = (q_text.strip(), q_start, q_start + len(q_text.strip()))
            head = statements[0] if statements else (q_paras[0] if q_paras else fallback)
            cite = span_citation(s, acc_q, head[1], head[2], RISKS_10Q)
            state["risk_result"] = {"kind": "unchanged", "citation": cite, "text": head[0][:400]}
            return state
        excerpts = [
            to_excerpt(s, span_citation(s, acc_q, a, b, RISKS_10Q))
            for _, a, b in new[:MAX_NEW_PARAGRAPHS]
        ]
    state["risk_result"] = {"kind": "changed", "count": len(new)}
    if ctx.llm is None:
        state["risks"] = None
        state["risks_note"] = "Agent step skipped (no LLM)."
        return state
    state["risks"] = ctx.ask(
        f"These paragraphs are in {state['company']['name']}'s {state['label']} 10-Q risk factors "
        "but have no close match in its latest 10-K's Item 1A. Summarize what changed in 2 to 4 "
        "Markdown bullets, each one sentence and cited. Only use these paragraphs.",
        state,
        excerpts,
    )
    return state


def _write(state: State, ctx: Context) -> State:
    ids = sorted(
        {f for r in state["deltas"] for o in (r["current"], r["prior"]) if o for f in o.fact_ids}
    )
    with ctx.session_factory() as s:
        state["facts"] = fact_details(s, ids)
    notes = Footnotes()
    md = build_markdown(state, notes)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    label = state["label"].replace(" ", "")
    path = ctx.out_dir / f"earnings_{state['company']['ticker']}_{label}.md"
    path.write_text(md.rstrip() + "\n", encoding="utf-8")
    state["path"] = path
    return state


# -- rendering -------------------------------------------------------------------------------


def _fmt(op: Operand | None, kind: str, label: str) -> str:
    if op is None:
        return "n/a"
    if kind == "pp":
        return display(op.value, "ratio") or "n/a"
    if label == "Diluted EPS":
        return f"${op.value:,.2f}"
    return f"{op.value / Decimal(1_000_000):,.0f}"


def _change(c: Computed, kind: str) -> str:
    if c.value is None:
        return f"n/a ({c.note})"
    if kind == "pp":
        return f"{c.value * 100:+.1f} pp"
    return f"{c.value * 100:+.1f}%"


def _row_note(r: dict[str, Any], state: State) -> str:
    ops = [o for o in (r["current"], r["prior"]) if o is not None]
    if r["kind"] == "pp":
        src = f"{r['label']} = line / revenue for each quarter; change = current - prior"
    else:
        src = f"{r['label']} growth = (current - prior) / prior"
    details = state.get("facts", {})
    parts = []
    for o in ops:
        cited = [details[f].describe() for f in o.fact_ids if f in details]
        how = f" = {o.detail}" if o.source in ("derived", "computed") else ""
        parts.append(f"{o.label}{how}: " + "; ".join(cited))
    return f"{src} (analyst.metrics). " + " | ".join(parts)


def build_markdown(state: State, notes: Footnotes) -> str:
    c = state["company"]
    rows = [
        "| ($M, except EPS) | " + f"{state['label']} | {state['prior_label']} | Change |",
        "|---|---|---|---|",
    ]
    for r in state["deltas"]:
        ref = notes.add(_row_note(r, state))
        rows.append(
            f"| {r['label']} [^{ref}] | {_fmt(r['current'], r['kind'], r['label'])} | "
            f"{_fmt(r['prior'], r['kind'], r['label'])} | {_change(r['change'], r['kind'])} |"
        )
    mdna = agent_section(state.get("mdna"), notes, state.get("mdna_note", ""))
    risk = state["risk_result"]
    if risk["kind"] == "unchanged":
        ref = notes.add_citation(risk["citation"])
        stats = state.get("risk_stats", {})
        risks = (
            f"No new risk-factor paragraphs compared with the latest 10-K "
            f"({stats.get('quarter_paragraphs', 0)} paragraphs in the 10-Q's Part II, Item 1A, "
            f"none without a close match in the 10-K's Item 1A). The 10-Q says: "
            f'"{risk["text"]}" [^{ref}]'
        )
    elif risk["kind"] == "none":
        risks = f"_{risk['note']}_"
    else:
        risks = agent_section(state.get("risks"), notes, state.get("risks_note", ""))
        shown = min(risk["count"], MAX_NEW_PARAGRAPHS)
        scope = "all of them" if shown == risk["count"] else f"the first {shown}"
        risks = (
            f"{risk['count']} paragraph(s) in the 10-Q's Part II, Item 1A have no close match "
            f"in the latest 10-K's Item 1A; the summary covers {scope}.\n\n{risks}"
        )
    tenq = state["tenq"]
    return "\n\n".join(
        [
            f"# {c['name']} ({c['ticker']}): {state['label']} earnings brief",
            f"_10-Q filed {tenq['filed_at']} ({tenq['accession']}); compared with "
            f"{state['prior_label']}._",
            "## Key metrics (deterministic, from XBRL)",
            "\n".join(rows),
            "## What drove the quarter (MD&A)",
            mdna,
            "## What changed in risk factors",
            risks,
            "## Sources",
            notes.markdown(),
        ]
    )


class Earnings(Workflow):
    name: ClassVar[str] = "earnings"
    Inputs: ClassVar[type[BaseModel]] = EarningsInputs

    def steps(self, inputs: Any) -> list[Step]:
        return [
            Step("latest 10-Q and quarter", "deterministic", _quarter),
            Step("key metric deltas", "deterministic", _deltas),
            Step("MD&A drivers", "agent", _mdna),
            Step("risk factor changes vs 10-K", "agent", _risk_changes),
            Step("write Markdown", "deterministic", _write),
        ]

    def outputs(self, state: State) -> tuple[list[Path], str]:
        rev = state["deltas"][0]["change"]
        g = "n/a" if rev.value is None else f"{rev.value * 100:+.1f}%"
        kind = state["risk_result"]["kind"]
        return [state["path"]], (
            f"{state['company']['ticker']} {state['label']}: revenue {g} "
            f"vs {state['prior_label']}; risk factors {kind}"
        )
