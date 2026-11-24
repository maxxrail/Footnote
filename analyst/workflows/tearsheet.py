"""Tearsheet: a one-page company profile as PDF and Markdown.

    uv run python -m analyst.workflows run tearsheet --ticker NVDA

Page 1 has five sections:
- **Business**: the agent summarizes the latest 10-K's Item 1, cited.
- **Financial summary**: the last 5 fiscal years from XBRL, one footnote per row with its facts.
- **Margin chart**: gross, operating and net margin, computed in `analyst.metrics`
  (matplotlib PNG).
- **Top 5 risk factors**: the agent reads the 10-K's Item 1A, cited.
- **Valuation snapshot**: the target's comps multiples next to the peer medians.

The sources come after page 1. Agent sections go through the number post-check. A
section it flagged, or one with no source text, says so instead of passing silently.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, field_validator
from sqlalchemy import select

from analyst.agent.tools import ToolError, display, load_universe
from analyst.db.models import Company
from analyst.edgar.concepts import SeriesPoint, get_series
from analyst.metrics import finance as fin
from analyst.metrics.computed import Computed, Operand
from analyst.workflows import comps as comps_wf
from analyst.workflows.base import Context, State, Step, Workflow
from analyst.workflows.report import (
    Footnotes,
    agent_section,
    markdown_blocks,
    md_inline_to_rl,
    pdf_styles,
)
from analyst.workflows.sources import latest_filing_with_section, section_chunks, to_excerpt

YEARS = 5
ROWS = [
    ("revenue", "Revenue"),
    ("gross_profit", "Gross profit"),
    ("operating_income", "Operating income"),
    ("net_income", "Net income"),
    ("eps_diluted", "Diluted EPS"),
    ("operating_cash_flow", "Operating cash flow"),
]
MARGINS = [("gross_profit", "Gross"), ("operating_income", "Operating"), ("net_income", "Net")]
BUSINESS_CHUNKS = 3
SPLIT_RATIO = Decimal("1.8")
RISK_CHUNKS = 6


class TearsheetInputs(BaseModel):
    ticker: str

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


def _company(state: State, ctx: Context) -> State:
    t = state["inputs"].ticker
    universe = {r.ticker: r for r in load_universe(ctx.universe_path)}
    if t not in universe:
        raise ToolError(f"{t} is not in the covered universe")
    with ctx.session_factory() as s:
        co = s.scalar(select(Company).where(Company.ticker == t))
        if co is None:
            raise ToolError(f"{t} is in the universe but has no data loaded")
        tenk = latest_filing_with_section(s, co.cik, ["10-K", "10-K/A"], "Item 1")
        tenk_risks = latest_filing_with_section(s, co.cik, ["10-K", "10-K/A"], "Item 1A")
        state["company"] = {
            "cik": co.cik,
            "ticker": co.ticker,
            "name": co.name,
            "sic": co.sic_description or "",
            "fye": co.fiscal_year_end or "",
            "peer_group": universe[t].peer_group,
        }
        state["tenk"] = (
            {"accession": tenk.accession, "filed_at": tenk.filed_at, "url": tenk.url}
            if tenk
            else None
        )
        state["tenk_risks"] = {"accession": tenk_risks.accession} if tenk_risks else None
    return state


def _financials(state: State, ctx: Context) -> State:
    cik = state["company"]["cik"]
    with ctx.session_factory() as s:
        series = {k: {p.fiscal_year: p for p in get_series(s, cik, k, "FY")} for k, _ in ROWS}
    years = sorted({y for v in series.values() for y in v})[-YEARS:]
    margins: dict[str, dict[int, Computed]] = {}
    t = state["company"]["ticker"]
    for key, label in MARGINS:
        margins[label] = {}
        for y in years:
            num, rev = series[key].get(y), series["revenue"].get(y)
            margins[label][y] = fin.margin(_op(t, num), _op(t, rev), f"{label.lower()}_margin")
    state["years"], state["series"], state["margins"] = years, series, margins
    state["split_warning"] = split_warning(series["eps_diluted"], series["net_income"], years)
    return state


def _op(ticker: str, p: SeriesPoint | None) -> Operand | None:
    if p is None:
        return None
    return Operand(
        f"{ticker} {p.canonical} FY{p.fiscal_year}",
        p.value,
        (p.fact_id,),
        "fact",
        p.accession,
        p.period_start,
        p.period_end,
        p.unit,
    )


def split_warning(
    eps: dict[int, SeriesPoint],
    net_income: dict[int, SeriesPoint],
    years: list[int] | None = None,
) -> str:
    """Per-share values come from the latest filing that reported each year, so a stock
    split can leave early years unadjusted (NVIDIA FY2022 EPS $3.85 pre-split next to
    FY2023 $0.17 post-split). The share count implied by net income / EPS then jumps by
    the split ratio. Flags every jump of 1.8x or more between consecutive `years` (the
    years the table shows; all years if None)."""
    shown = sorted(eps) if years is None else sorted(years)
    implied = {
        y: net_income[y].value / eps[y].value
        for y in shown
        if y in eps and y in net_income and eps[y].value > 0 and net_income[y].value > 0
    }
    jumps = []
    for a, b in itertools.pairwise(sorted(implied)):
        ratio = implied[b] / implied[a]
        if ratio >= SPLIT_RATIO or ratio <= 1 / SPLIT_RATIO:
            jumps.append(f"{ratio:.1f}x from FY{a} to FY{b}")
    if not jumps:
        return ""
    return (
        "Per-share values may mix split bases: the share count implied by net income / diluted "
        f"EPS changes {', '.join(jumps)}, which looks like a stock split; EPS before the jump is "
        "as originally reported, not split-adjusted."
    )


def _chart(state: State, ctx: Context) -> State:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    years = state["years"]
    fig, ax = plt.subplots(figsize=(3.6, 2.0), dpi=200)
    for label, by_year in state["margins"].items():
        pts = [(y, float(c.value) * 100) for y, c in by_year.items() if c.value is not None]
        if pts:
            ax.plot(
                [p[0] for p in pts],
                [p[1] for p in pts],
                marker="o",
                linewidth=1.4,
                markersize=3,
                label=f"{label} margin",
            )
    ax.set_xticks(years)
    ax.set_xticklabels([f"FY{y}" for y in years], fontsize=6)
    ax.tick_params(axis="y", labelsize=6)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))
    ax.grid(alpha=0.3, linewidth=0.5)
    ax.legend(fontsize=6, frameon=False, loc="best")
    ax.set_title("Margins (analyst.metrics)", fontsize=7)
    fig.tight_layout()
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    path = (
        ctx.out_dir / f"tearsheet_{state['company']['ticker']}_{ctx.today.isoformat()}_margins.png"
    )
    fig.savefig(path)
    plt.close(fig)
    state["chart"] = path
    return state


def _valuation(state: State, ctx: Context) -> State:
    sub: State = {"inputs": comps_wf.CompsInputs(ticker=state["company"]["ticker"])}
    for fn in (comps_wf.resolve, comps_wf.gather, comps_wf.check):
        sub = fn(sub, ctx)
    state["comps"] = sub
    return state


def _excerpts(state: State, ctx: Context, section: str, n: int) -> list[Any]:
    filing = state["tenk_risks"] if section == "Item 1A" else state["tenk"]
    if filing is None:
        return []
    with ctx.session_factory() as s:
        cites = section_chunks(s, filing["accession"], section, n)
        return [to_excerpt(s, c) for c in cites]


def _business(state: State, ctx: Context) -> State:
    name = state["company"]["name"]
    excerpts = _excerpts(state, ctx, "Item 1", BUSINESS_CHUNKS)
    if not excerpts or ctx.llm is None:
        state["business"] = None
        state["business_note"] = (
            "Agent step skipped (no LLM)." if excerpts else "Not in the filings: no Item 1 text."
        )
        return state
    state["business"] = ctx.ask(
        f"In 3 or 4 sentences, describe what {name} does: its main products or services, "
        "its segments and who its customers are. Use the provided Item 1 excerpts, and "
        f"search_filings (ticker {state['company']['ticker']}, section 'Item 1', form 10-K) "
        "for anything they don't cover; cite each sentence. Describe only what the filings "
        "say: if something isn't there, leave it out rather than commenting on the sources. "
        "No numbers unless a filing states them.",
        state,
        excerpts,
    )
    return state


def _risks(state: State, ctx: Context) -> State:
    name, t = state["company"]["name"], state["company"]["ticker"]
    excerpts = _excerpts(state, ctx, "Item 1A", RISK_CHUNKS)
    if not excerpts or ctx.llm is None:
        state["risks"] = None
        state["risks_note"] = (
            "Agent step skipped (no LLM)." if excerpts else "Not in the filings: no Item 1A text."
        )
        return state
    state["risks"] = ctx.ask(
        f"List the 5 most significant risk factors in {name}'s latest 10-K (Item 1A), as 5 "
        "Markdown bullets of one sentence each, most significant first, each cited. Use the "
        f"provided excerpts and search_filings (ticker {t}, section 'Item 1A', form 10-K) "
        "if they don't cover the main risks.",
        state,
        excerpts,
    )
    return state


def _write(state: State, ctx: Context) -> State:
    with ctx.session_factory() as s:
        state["val_facts"] = comps_wf.fact_details(s, sorted(state["comps"]["rows"][0].fact_ids))
    notes = Footnotes()
    md = build_markdown(state, notes)
    t = state["company"]["ticker"]
    base = ctx.out_dir / f"tearsheet_{t}_{ctx.today.isoformat()}"
    md_path, pdf_path = base.with_suffix(".md"), base.with_suffix(".pdf")
    md_path.write_text(md.rstrip() + "\n", encoding="utf-8")
    state["profile_pages"] = build_pdf(state, notes, pdf_path)
    state["paths"] = [pdf_path, md_path, state["chart"]]
    return state


# -- rendering -------------------------------------------------------------------------------


def _row_footnote(state: State, key: str, label: str) -> str:
    pts = [state["series"][key].get(y) for y in state["years"]]
    facts = [
        f"FY{p.fiscal_year} {p.concept} ({p.form} {p.accession}, fact {p.fact_id})"
        for p in pts
        if p is not None
    ]
    return f"{label}: " + ("; ".join(facts) if facts else "not reported in XBRL")


def _cell(p: SeriesPoint | None) -> str:
    if p is None:
        return "n/a"
    if p.unit == "USD/shares":
        return f"${p.value:,.2f}"
    return f"{p.value / Decimal(1_000_000):,.0f}"


def financial_rows(state: State, notes: Footnotes) -> list[list[str]]:
    years = state["years"]
    rows = [["($M, except EPS)", *[f"FY{y}" for y in years]]]
    for key, label in ROWS:
        note = _row_footnote(state, key, label)
        if key == "eps_diluted" and state.get("split_warning"):
            note += ". " + state["split_warning"]
        ref = notes.add(note)
        rows.append([f"{label} [^{ref}]", *[_cell(state["series"][key].get(y)) for y in years]])
    for label, by_year in state["margins"].items():
        ref = notes.add(
            f"{label} margin = {label.lower()} line / revenue, from the rows above "
            "(analyst.metrics.finance.margin)"
        )
        rows.append(
            [
                f"{label} margin [^{ref}]",
                *[display(by_year[y].value, "ratio") or "n/a" for y in years],
            ]
        )
    return rows


def valuation_rows(state: State, notes: Footnotes) -> list[list[str]]:
    target = state["comps"]["rows"][0]
    stats = state["comps"]["stats"]
    details = state.get("val_facts", {})
    parts = []
    for key, op in target.inputs.items():
        if op is None:
            parts.append(f"{key}: not available")
            continue
        basis = {
            "revenue": "revenue",
            "operating_income": "ebitda",
            "d_and_a": "ebitda",
            "net_income": "net_income",
        }.get(key)
        tag = f" [{target.basis[basis]}]" if basis and basis in target.basis else ""
        cited = "; ".join(details[f].describe() for f in op.fact_ids if f in details)
        src = op.detail if op.source == "price" else cited
        parts.append(f"{op.label}{tag}: {src}")
    ref = notes.add(
        "Valuation: comps workflow (analyst.workflows.comps), the same inputs as "
        f"comps_{target.ticker}_*.xlsx; EV = market cap + debt - cash, EBITDA = operating "
        "income + D&A, P/E = market cap / net income. Inputs: "
        + " | ".join(parts)
        + ". Peers: "
        + ", ".join(r.ticker for r in state["comps"]["rows"][1:])
        + (". Notes: " + "; ".join(target.notes) if target.notes else "")
    )
    rows = [[f"Valuation [^{ref}]", target.ticker, "Peer median"]]
    rows.append(["Market cap ($M)", _money(target.check["market_cap"].value), ""])
    rows.append(["Enterprise value ($M)", _money(target.check["enterprise_value"].value), ""])
    for m in comps_wf.MULTIPLES:
        rows.append(
            [
                comps_wf._LABELS[m],
                comps_wf._fmt(target.check[m].value),
                comps_wf._fmt(stats[m]["median"].value),
            ]
        )
    return rows


def _money(v: Decimal | None) -> str:
    return "NM" if v is None else f"{v / Decimal(1_000_000):,.0f}"


def _md_table(rows: list[list[str]]) -> str:
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _sections(state: State, notes: Footnotes) -> dict[str, Any]:
    c = state["company"]
    # Footnotes are numbered in reading order: business, financials, risks, valuation.
    business = agent_section(state.get("business"), notes, state.get("business_note", ""))
    fin_rows = financial_rows(state, notes)
    risks = agent_section(state.get("risks"), notes, state.get("risks_note", ""))
    val_rows = valuation_rows(state, notes)
    tenk = state["tenk"]
    price = state["comps"]["rows"][0].inputs["price"]
    sub = " | ".join(
        x
        for x in (
            c["sic"],
            f"fiscal year ends {c['fye'][:2]}/{c['fye'][2:]}" if c["fye"] else "",
            f"latest 10-K filed {tenk['filed_at']}" if tenk else "no 10-K text",
            f"price ${price.value:,.2f} on {price.period_end}" if price else "no price",
        )
        if x
    )
    return {
        "title": f"{c['name']} ({c['ticker']})",
        "sub": sub,
        "business": business,
        "risks": risks,
        "fin": fin_rows,
        "val": val_rows,
    }


def build_markdown(state: State, notes: Footnotes) -> str:
    sec = _sections(state, notes)
    chart = Path(state["chart"]).name
    return "\n\n".join(
        [
            f"# {sec['title']}",
            f"_{sec['sub']}_",
            "## Business",
            sec["business"],
            "## Financial summary (fiscal years, from XBRL)",
            _md_table(sec["fin"]),
            f"_{state['split_warning']}_" if state.get("split_warning") else "",
            f"![Margins]({chart})",
            "## Top 5 risk factors (latest 10-K, Item 1A)",
            sec["risks"],
            "## Valuation snapshot",
            _md_table(sec["val"]),
            "## Sources",
            notes.markdown(),
        ]
    )


def build_pdf(state: State, notes: Footnotes, path: Path) -> int:
    """Profile on page 1, sources after a page break. Returns the profile's page count."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    st = pdf_styles()
    pdf_notes = Footnotes()
    sec = _sections(state, pdf_notes)  # same numbering as the Markdown (same order)

    def table(rows: list[list[str]], widths: list[float]) -> Table:
        data = [[Paragraph(md_inline_to_rl(c), st["small"]) for c in r] for r in rows]
        tb = Table(data, colWidths=widths)
        tb.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDEBF7")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        return tb

    n_years = len(sec["fin"][0]) - 1
    fin_table = table(sec["fin"], [1.5 * inch] + [0.55 * inch] * n_years)
    val_table = table(sec["val"], [1.2 * inch, 0.75 * inch, 0.75 * inch])
    chart = Image(str(state["chart"]), width=3.6 * inch, height=2.0 * inch)
    side = Table([[chart, val_table]], colWidths=[3.8 * inch, 2.9 * inch])
    side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    def profile() -> list[Any]:
        return [
            Paragraph(md_inline_to_rl(sec["title"]), st["title"]),
            Paragraph(md_inline_to_rl(sec["sub"]), st["sub"]),
            Paragraph("Business", st["h"]),
            *markdown_blocks(sec["business"], st),
            Paragraph("Financial summary (fiscal years, from XBRL)", st["h"]),
            fin_table,
            *(
                [Paragraph(md_inline_to_rl(state["split_warning"]), st["small"])]
                if state.get("split_warning")
                else []
            ),
            Spacer(1, 4),
            side,
            Paragraph("Top 5 risk factors (latest 10-K, Item 1A)", st["h"]),
            *markdown_blocks(sec["risks"], st),
        ]

    margins = {
        "leftMargin": 0.5 * inch,
        "rightMargin": 0.5 * inch,
        "topMargin": 0.45 * inch,
        "bottomMargin": 0.45 * inch,
    }
    probe = path.with_name(path.stem + "_profile_probe.pdf")
    SimpleDocTemplate(str(probe), pagesize=letter, **margins).build(profile())
    from analyst.workflows.report import page_count

    pages = page_count(probe)
    probe.unlink()
    sources = [Paragraph("Sources", st["h"])] + [
        Paragraph(md_inline_to_rl(f"{i}. {e}"), st["small"])
        for i, e in enumerate(pdf_notes.entries, 1)
    ]
    SimpleDocTemplate(str(path), pagesize=letter, title=sec["title"], **margins).build(
        [*profile(), PageBreak(), *sources]
    )
    return pages


class Tearsheet(Workflow):
    name: ClassVar[str] = "tearsheet"
    Inputs: ClassVar[type[BaseModel]] = TearsheetInputs

    def steps(self, inputs: Any) -> list[Step]:
        return [
            Step("company and latest 10-K", "deterministic", _company),
            Step("5-year financials and margins", "deterministic", _financials),
            Step("margin chart", "deterministic", _chart),
            Step("valuation snapshot (comps)", "deterministic", _valuation),
            Step("business description (Item 1)", "agent", _business),
            Step("top 5 risk factors (Item 1A)", "agent", _risks),
            Step("write PDF and Markdown", "deterministic", _write),
        ]

    def outputs(self, state: State) -> tuple[list[Path], str]:
        pages = state["profile_pages"]
        fit = "fits on one page" if pages == 1 else f"profile runs to {pages} pages"
        return state["paths"], f"{state['company']['ticker']} tearsheet: {fit}, sources after it"
