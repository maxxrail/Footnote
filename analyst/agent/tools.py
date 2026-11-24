"""The agent's tools: JSON-schema definitions for the model, Pydantic-validated inputs.

Every citable item a tool returns gets a run-wide reference number (`ref`). The model
cites it as `[^ref]`, and `ToolBox.refs` maps the number to its citation (a filing span,
an XBRL fact, or a computed value with its lineage). Numbers reach the model only through
tool output: `get_financials` returns reported facts, `compute` returns values from
`analyst.metrics` (the model never does arithmetic), `search_filings` returns filing text.

Tools:
- resolve_company(query)            -> ticker / cik / name, fuzzy; says when not covered
- get_financials(ticker, metrics, periods, period_type, fiscal_years?)
- compute(metric, tickers, fiscal_year?, item?, years?)
- search_filings(query, ticker?, form?, section?, since?)
- list_peers(ticker)
- submit_answer(text, confidence, refused)  (handled by the loop, not here)
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.agent.citations import AnyCitation, ComputedCitation, FactCitation, OperandRef
from analyst.db.models import Chunk, Company, Fact, Filing
from analyst.edgar.concepts import CONCEPTS, SeriesPoint, get_series
from analyst.metrics import finance as fin
from analyst.metrics.computed import Computed, Operand
from analyst.metrics.prices import PriceProvider
from analyst.metrics.quarters import DerivedPoint, quarterly_series
from analyst.metrics.stats import peer_stat
from analyst.retrieval.citation import Citation
from analyst.retrieval.embed import Embedder
from analyst.retrieval.search import search

SessionFactory = Callable[[], AbstractContextManager[Session]]
EXCERPT_CHARS = 1500
# Balance-sheet inputs older than the latest balance sheet: noted after 100 days (many filers
# tag share counts only in the 10-K), refused after 400 (Citigroup's last tag is FY2010).
NOTE_BALANCE_AGE_DAYS = 100
MAX_BALANCE_AGE_DAYS = 400
MATCH_CUTOFF = 0.75


class ToolError(Exception):
    """A tool could not run; the message goes back to the model as an error result."""


# -- formatting (presentation only; the values themselves come from facts or metrics) -----


def display(value: Decimal | None, unit: str) -> str | None:
    if value is None:
        return None
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "x":
        return f"{value:.1f}x"
    if unit == "USD/shares":
        return f"${value:,.2f}"
    if unit == "shares":
        return f"{value / 1_000_000:,.1f}M shares"
    if unit == "USD":
        a = abs(value)
        sign = "-" if value < 0 else ""
        if a >= 1_000_000_000:
            return f"{sign}${a / 1_000_000_000:,.2f}B"
        if a >= 1_000_000:
            return f"{sign}${a / 1_000_000:,.1f}M"
        return f"{sign}${a:,.0f}"
    return str(value)


def _num(value: Decimal | None) -> str | None:
    """Exact value as a plain string (no exponent), trimmed to 10 decimal places."""
    if value is None:
        return None
    q = value.quantize(Decimal("1e-10")) if value != value.to_integral() else value
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# -- tool inputs ---------------------------------------------------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveCompanyInput(_Input):
    query: str = Field(description="A company name or ticker, e.g. 'Nvidia' or 'NVDA'")


Metric = Literal[
    "growth",
    "cagr",
    "ltm",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "ebitda",
    "ebitda_margin",
    "market_cap",
    "enterprise_value",
    "ev_to_revenue",
    "ev_to_ebitda",
    "pe",
    "net_debt_to_ebitda",
]


class GetFinancialsInput(_Input):
    ticker: str
    metrics: list[str] = Field(
        description=f"Canonical line items: {', '.join(sorted(CONCEPTS))}", min_length=1
    )
    periods: int = Field(3, ge=1, le=20, description="How many most recent periods")
    period_type: Literal["FY", "Q"] = Field(
        "FY", description="FY = fiscal years; Q = fiscal quarters (Q4 derived as FY - Q1-Q3)"
    )
    fiscal_years: list[int] | None = Field(
        None, description="Only these fiscal years (overrides `periods`)"
    )


class ComputeInput(_Input):
    metric: Metric = Field(
        description=(
            "growth/cagr/ltm use `item`; margins and EBITDA use fiscal-year facts; "
            "market_cap, enterprise_value, ev_to_*, pe need a market price from data/prices.csv"
        )
    )
    tickers: list[str] = Field(
        min_length=1,
        description="One ticker, or several for a peer set (adds median, mean, quartiles)",
    )
    fiscal_year: int | None = Field(None, description="Fiscal year; default the latest reported")
    item: str = Field("revenue", description="Line item for growth, cagr and ltm")
    years: int = Field(3, ge=1, le=20, description="Span for cagr")


class SearchFilingsInput(_Input):
    query: str
    ticker: str | None = None
    form: str | None = Field(None, description="10-K or 10-Q")
    section: str | None = Field(None, description="e.g. 'Item 7' (MD&A), 'Item 1A' (risk factors)")
    since: dt.date | None = Field(None, description="Only filings filed on or after this date")


class ListPeersInput(_Input):
    ticker: str


class SubmitAnswerInput(_Input):
    text: str = Field(description="The final answer. Cite every number and claim with [^ref].")
    confidence: Literal["high", "medium", "low"]
    refused: bool = Field(description="True when the data needed is not in the corpus")


TOOL_DOCS: dict[str, tuple[str, type[_Input]]] = {
    "resolve_company": (
        "Find a company in the covered universe by name or ticker. Returns ticker, CIK and name, "
        "or says the company is not covered (then refuse; do not answer from memory).",
        ResolveCompanyInput,
    ),
    "get_financials": (
        "Reported XBRL values from 10-K/10-Q filings for one company. Each value has a `ref` "
        "to cite. Line items not reported by the company come back empty: say so, don't guess.",
        GetFinancialsInput,
    ),
    "compute": (
        "Deterministic calculations (growth, CAGR, LTM, margins, EBITDA, market cap, EV, "
        "EV/Revenue, EV/EBITDA, P/E, net debt/EBITDA). Use this for every derived number; "
        "never compute in prose. A result with value null carries a note saying why.",
        ComputeInput,
    ),
    "search_filings": (
        "Hybrid search over 10-K/10-Q text. Returns excerpts with a `ref` each, for "
        "qualitative claims (what management attributed a change to, risks, strategy).",
        SearchFilingsInput,
    ),
    "list_peers": (
        "The peer group of a ticker from the covered universe.",
        ListPeersInput,
    ),
    "submit_answer": (
        "Submit the final answer. Call this exactly once, at the end.",
        SubmitAnswerInput,
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    """Anthropic tool definitions, in a fixed order (keeps the prompt cache stable)."""
    out = []
    for name, (desc, model) in TOOL_DOCS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        out.append({"name": name, "description": desc, "input_schema": schema})
    return out


# -- universe ------------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseRow:
    ticker: str
    name: str
    peer_group: str


def load_universe(path: Path) -> list[UniverseRow]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            UniverseRow(r["ticker"].strip().upper(), r["name"].strip(), r["peer_group"].strip())
            for r in csv.DictReader(f)
        ]


# -- the toolbox ---------------------------------------------------------------------------


@dataclass
class ToolBox:
    session_factory: SessionFactory
    prices: PriceProvider
    universe_path: Path
    embedder: Embedder | None = None
    refs: dict[int, AnyCitation] = field(default_factory=dict)

    # registry -------------------------------------------------------------------------
    def _register(self, citation: AnyCitation) -> int:
        n = len(self.refs) + 1
        self.refs[n] = citation
        return n

    # dispatch -------------------------------------------------------------------------
    def run(self, name: str, raw_input: dict[str, Any]) -> str:
        """Run a tool and return its JSON output. Raises ToolError for bad input."""
        handlers: dict[str, Callable[[Any], dict[str, Any]]] = {
            "resolve_company": self.resolve_company,
            "get_financials": self.get_financials,
            "compute": self.compute,
            "search_filings": self.search_filings,
            "list_peers": self.list_peers,
        }
        if name not in handlers:
            raise ToolError(f"unknown tool {name!r}")
        try:
            args = TOOL_DOCS[name][1].model_validate(raw_input)
        except ValidationError as exc:
            raise ToolError(f"invalid input for {name}: {exc.errors(include_url=False)}") from exc
        return json.dumps(handlers[name](args), default=str)

    # helpers --------------------------------------------------------------------------
    def _universe(self) -> list[UniverseRow]:
        return load_universe(self.universe_path)

    def _company(self, s: Session, ticker: str) -> Company:
        t = ticker.strip().upper()
        if t not in {r.ticker for r in self._universe()}:
            raise ToolError(f"{t} is not in the covered universe; the answer must say so")
        co = s.scalar(select(Company).where(Company.ticker == t))
        if co is None:
            raise ToolError(f"{t} is in the universe but has no data loaded")
        return co

    def _fact_citations(self, s: Session, fact_ids: Sequence[int]) -> list[FactCitation]:
        if not fact_ids:
            return []
        rows = s.execute(
            select(Fact, Filing, Company)
            .join(Filing, Filing.accession == Fact.accession)
            .join(Company, Company.cik == Fact.cik)
            .where(Fact.id.in_(list(fact_ids)))
        ).all()
        by_id = {f.id: (f, fl, co) for f, fl, co in rows}
        canonical = {c: k for k, v in CONCEPTS.items() for c in v.candidates}
        out = []
        for fid in fact_ids:
            f, fl, co = by_id[fid]
            out.append(
                FactCitation(
                    ticker=co.ticker,
                    company=co.name,
                    cik=co.cik,
                    canonical=canonical.get(f.concept, f.concept),
                    concept=f.concept,
                    label=_period_label(f.period_start, f.period_end),
                    period_start=f.period_start,
                    period_end=f.period_end,
                    unit=f.unit,
                    value=_num(f.value) or "",
                    fact_id=f.id,
                    accession=f.accession,
                    form=f.form,
                    filed_at=fl.filed_at,
                    url=fl.url,
                )
            )
        return out

    def _register_computed(self, s: Session, c: Computed, ticker: str | None) -> int:
        return self._register(
            ComputedCitation(
                ticker=ticker,
                metric=c.metric,
                label=c.label or c.metric,
                value=_num(c.value),
                unit=c.unit,
                formula=c.formula,
                note=c.note,
                inputs=[
                    OperandRef(
                        label=o.label,
                        value=_num(o.value) or "",
                        source=o.source,
                        detail=o.detail,
                        fact_ids=list(o.fact_ids),
                    )
                    for o in c.inputs
                ],
                facts=self._fact_citations(s, c.fact_ids),
            )
        )

    # tools ----------------------------------------------------------------------------
    def resolve_company(self, args: ResolveCompanyInput) -> dict[str, Any]:
        q = args.query.strip()
        universe = self._universe()
        with self.session_factory() as s:
            names = {c.ticker: c for c in s.scalars(select(Company))}
        exact = [r for r in universe if r.ticker == q.upper()]
        if not exact:
            scored = []
            for r in universe:
                db_name = names[r.ticker].name if r.ticker in names else r.name
                cands = [r.name.lower(), db_name.lower()]
                if any(q.lower() in c for c in cands if len(q) >= 3):
                    scored.append((1.0, r))
                    continue
                best = max(difflib.SequenceMatcher(None, q.lower(), c).ratio() for c in cands)
                if best >= MATCH_CUTOFF:
                    scored.append((best, r))
            exact = [r for _, r in sorted(scored, key=lambda t: -t[0])]
        if not exact:
            return {
                "found": False,
                "message": (
                    f"{q!r} is not in the covered universe of {len(universe)} companies. "
                    "There is no data for it: refuse, do not answer from memory."
                ),
            }
        return {
            "found": True,
            "matches": [
                {
                    "ticker": r.ticker,
                    "name": names[r.ticker].name if r.ticker in names else r.name,
                    "cik": names[r.ticker].cik if r.ticker in names else None,
                    "peer_group": r.peer_group,
                }
                for r in exact[:5]
            ],
        }

    def get_financials(self, args: GetFinancialsInput) -> dict[str, Any]:
        unknown = [m for m in args.metrics if m not in CONCEPTS]
        if unknown:
            raise ToolError(f"unknown line items {unknown}; known: {sorted(CONCEPTS)}")
        out: dict[str, Any] = {"ticker": args.ticker.upper(), "period_type": args.period_type}
        with self.session_factory() as s:
            co = self._company(s, args.ticker)
            out["company"] = co.name
            results = []
            for m in args.metrics:
                points: Sequence[SeriesPoint | DerivedPoint]
                if args.period_type == "Q":
                    points = quarterly_series(s, co.cik, m)
                else:
                    points = get_series(s, co.cik, m, "FY")
                if args.fiscal_years:
                    points = [p for p in points if p.fiscal_year in set(args.fiscal_years)]
                else:
                    points = list(points)[-args.periods :]
                values = []
                for p in points:
                    if isinstance(p, DerivedPoint):
                        ref = self._register_computed(s, _derived_computed(co.ticker, p), co.ticker)
                    else:
                        ref = self._register(self._fact_citations(s, [p.fact_id])[0])
                    values.append(
                        {
                            "ref": ref,
                            "period": _label(p),
                            "period_end": p.period_end.isoformat(),
                            "value": _num(p.value),
                            "display": display(p.value, p.unit),
                            "unit": p.unit,
                            "source": "derived: " + p.method
                            if isinstance(p, DerivedPoint)
                            else f"{p.concept} in {p.form} {p.accession}",
                        }
                    )
                item: dict[str, Any] = {"metric": m, "values": values}
                if not values:
                    item["note"] = (
                        f"{co.ticker} reports no {m} for these periods in its XBRL data. "
                        "Say it is not in the filings; do not estimate it."
                    )
                results.append(item)
            out["results"] = results
        return out

    def compute(self, args: ComputeInput) -> dict[str, Any]:
        if args.metric in ("growth", "cagr", "ltm") and args.item not in CONCEPTS:
            raise ToolError(f"unknown item {args.item!r}; known: {sorted(CONCEPTS)}")
        results = []
        computed: list[Computed] = []
        with self.session_factory() as s:
            for t in args.tickers:
                co = self._company(s, t)
                c = _compute_one(s, co, args, self.prices)
                computed.append(c)
                results.append(self._present(s, c, co.ticker))
            out: dict[str, Any] = {"metric": args.metric, "results": results}
            if len(computed) > 1:
                out["peer_stats"] = [
                    self._present(s, peer_stat(computed, stat, args.metric), None)
                    for stat in ("median", "mean", "q1", "q3")
                ]
        return out

    def _present(self, s: Session, c: Computed, ticker: str | None) -> dict[str, Any]:
        ref = self._register_computed(s, c, ticker)
        return {
            "ref": ref,
            "ticker": ticker,
            "label": c.label or c.metric,
            "value": _num(c.value),
            "display": display(c.value, c.unit),
            "unit": c.unit,
            "formula": c.formula,
            "note": c.note,
            "inputs": [
                {"label": o.label, "value": _num(o.value), "display": display(o.value, o.unit)}
                for o in c.inputs
            ],
        }

    def search_filings(self, args: SearchFilingsInput) -> dict[str, Any]:
        with self.session_factory() as s:
            if args.ticker:
                self._company(s, args.ticker)
            hits: list[Citation] = search(
                args.query,
                forms=[args.form] if args.form else None,
                sections=[args.section] if args.section else None,
                since=args.since,
                k=5,
                ticker=args.ticker,
                session=s,
                embedder=self.embedder,
                rerank=False,
            )
            texts = {
                c.id: c.text
                for c in s.scalars(
                    select(Chunk).where(Chunk.id.in_([h.chunk_id for h in hits if h.chunk_id]))
                )
            }
        results = []
        for h in hits:
            body = texts.get(h.chunk_id or -1, h.snippet)
            results.append(
                {
                    "ref": self._register(h),
                    "company": h.company,
                    "ticker": h.ticker,
                    "form": h.form,
                    "filed_at": h.filed_at.isoformat(),
                    "section": f"{h.section} {h.section_title}",
                    "excerpt": body[:EXCERPT_CHARS],
                }
            )
        out: dict[str, Any] = {"query": args.query, "results": results}
        if not results:
            out["note"] = "No matching filing text. Do not make the claim without a source."
        return out

    def list_peers(self, args: ListPeersInput) -> dict[str, Any]:
        t = args.ticker.strip().upper()
        universe = self._universe()
        row = next((r for r in universe if r.ticker == t), None)
        if row is None:
            raise ToolError(f"{t} is not in the covered universe")
        peers = [
            {"ticker": r.ticker, "name": r.name}
            for r in universe
            if r.peer_group == row.peer_group and r.ticker != t
        ]
        return {"ticker": t, "peer_group": row.peer_group, "peers": peers}


# -- compute internals ---------------------------------------------------------------------


def _label(p: SeriesPoint | DerivedPoint) -> str:
    return f"FY{p.fiscal_year}" + (f" Q{p.fiscal_quarter}" if p.fiscal_quarter else "")


def _period_label(start: dt.date | None, end: dt.date) -> str:
    return f"{start}..{end}" if start else f"at {end}"


def _fact_operand(ticker: str, p: SeriesPoint | DerivedPoint) -> Operand:
    label = f"{ticker} {p.canonical} {_label(p)}"
    if isinstance(p, DerivedPoint):
        return Operand(
            label,
            p.value,
            tuple(i.fact_id for i in p.inputs),
            "derived",
            p.method,
            p.period_start,
            p.period_end,
            p.unit,
        )
    return Operand(
        label, p.value, (p.fact_id,), "fact", p.accession, p.period_start, p.period_end, p.unit
    )


def _derived_computed(ticker: str, p: DerivedPoint) -> Computed:
    return Computed(
        "q4_derived",
        p.value,
        p.method,
        tuple(_fact_operand(ticker, i) for i in p.inputs),
        "USD",
        label=f"{ticker} {p.canonical} {_label(p)}",
    )


@dataclass
class _Data:
    """Lazy per-company series lookups for one compute call."""

    s: Session
    co: Company
    _fy: dict[str, dict[int, SeriesPoint]] = field(default_factory=dict)
    stale: dict[str, str] = field(default_factory=dict)  # item -> age note (refused or used)

    def fy(self, item: str) -> dict[int, SeriesPoint]:
        if item not in self._fy:
            self._fy[item] = {p.fiscal_year: p for p in get_series(self.s, self.co.cik, item, "FY")}
        return self._fy[item]

    def fy_op(self, item: str, year: int) -> Operand | None:
        p = self.fy(item).get(year)
        return _fact_operand(self.co.ticker, p) if p else None

    def balance_sheet_date(self) -> dt.date | None:
        """The company's latest balance-sheet date (from total assets, which all report)."""
        pts = get_series(self.s, self.co.cik, "total_assets", "Q")
        return pts[-1].period_end if pts else None

    def latest_instant(self, item: str) -> Operand | None:
        """The latest reported balance of `item`, or None if it is much older than the
        latest balance sheet (Citigroup's last share count tag is from FY2010, before a
        1-for-10 reverse split; using it would be a confident wrong answer). An older but
        usable balance gets an age note."""
        pts = get_series(self.s, self.co.cik, item, "Q") or get_series(
            self.s, self.co.cik, item, "FY"
        )
        if not pts:
            return None
        latest = self.balance_sheet_date()
        age = (latest - pts[-1].period_end).days if latest is not None else 0
        if age > MAX_BALANCE_AGE_DAYS:
            self.stale[item] = (
                f"latest {item} is from {pts[-1].period_end}, more than {MAX_BALANCE_AGE_DAYS} "
                f"days before the latest balance sheet ({latest}); not used"
            )
            return None
        if age > NOTE_BALANCE_AGE_DAYS:
            self.stale[item] = (
                f"{item} is as of {pts[-1].period_end}, {age} days before the latest balance "
                f"sheet ({latest}); a later split or buyback would not be reflected"
            )
        return _fact_operand(self.co.ticker, pts[-1])

    def ltm_op(self, item: str) -> Computed:
        qs = [_fact_operand(self.co.ticker, p) for p in quarterly_series(self.s, self.co.cik, item)]
        return fin.ltm(qs, metric=f"ltm_{item}")


def _target_year(d: _Data, item: str, requested: int | None) -> int | None:
    if requested is not None:
        return requested
    years = sorted(d.fy(item))
    return years[-1] if years else None


def _ltm_end(c: Computed) -> dt.date | None:
    ends = [o.period_end for o in c.inputs if o.period_end is not None]
    return max(ends) if ends else None


def _flow(d: _Data, item: str) -> tuple[Operand | None, str]:
    """LTM of a flow item if four contiguous quarters exist and end no earlier than the
    latest fiscal year; otherwise the latest fiscal year. The basis names the window
    ("LTM to 2025-12-31" or "FY2025") so two flows can be checked for the same window."""
    last12 = d.ltm_op(item)
    year = _target_year(d, item, None)
    fy_end = d.fy(item)[year].period_end if year is not None else None
    end = _ltm_end(last12)
    if last12.value is not None and end is not None and (fy_end is None or end >= fy_end):
        return last12.as_operand(f"{d.co.ticker} {item} LTM to {end}"), f"LTM to {end}"
    return (d.fy_op(item, year) if year else None), f"FY{year}" if year else "none"


def _ebitda_flow(d: _Data) -> tuple[Operand | None, str]:
    oi, oi_basis = _flow(d, "operating_income")
    da, da_basis = _flow(d, "d_and_a")
    if oi_basis != da_basis:  # same window for both legs, else fall back to the same FY
        year = _target_year(d, "operating_income", None)
        if year is None:
            return None, "none"
        oi, da, oi_basis = d.fy_op("operating_income", year), d.fy_op("d_and_a", year), f"FY{year}"
    e = fin.ebitda(oi, da)
    return (
        e.as_operand(f"{d.co.ticker} EBITDA {oi_basis}") if e.value is not None else None
    ), oi_basis


def _price(d: _Data, prices: PriceProvider) -> Operand | None:
    q = prices.latest(d.co.ticker)
    return q.as_operand() if q else None


def _compute_one(s: Session, co: Company, args: ComputeInput, prices: PriceProvider) -> Computed:
    d = _Data(s, co)
    t = co.ticker
    m = args.metric

    def labelled(c: Computed, label: str, note: str = "") -> Computed:
        notes = "; ".join(n for n in (c.note, note) if n)
        return Computed(c.metric, c.value, c.formula, c.inputs, c.unit, notes, label)

    if m in ("growth", "cagr"):
        year = _target_year(d, args.item, args.fiscal_year)
        if year is None:
            return Computed(
                m, None, "", (), note=f"no {args.item} reported", label=f"{t} {args.item} {m}"
            )
        back = 1 if m == "growth" else args.years
        start, end = d.fy_op(args.item, year - back), d.fy_op(args.item, year)
        c = fin.growth(end, start, m) if m == "growth" else fin.cagr(start, end, back, m)
        span = f"FY{year - back}-FY{year}"
        return labelled(c, f"{t} {args.item} {m} {span}")
    if m == "ltm":
        return labelled(d.ltm_op(args.item), f"{t} {args.item} LTM")
    if m in ("gross_margin", "operating_margin", "net_margin", "ebitda", "ebitda_margin"):
        year = _target_year(d, "revenue", args.fiscal_year)
        if year is None:
            return Computed(m, None, "", (), note="no revenue reported", label=f"{t} {m}")
        rev = d.fy_op("revenue", year)
        if m in ("ebitda", "ebitda_margin"):
            e = fin.ebitda(d.fy_op("operating_income", year), d.fy_op("d_and_a", year))
            if m == "ebitda":
                return labelled(e, f"{t} EBITDA FY{year}")
            eo = e.as_operand(f"{t} EBITDA FY{year}") if e.value is not None else None
            return labelled(fin.margin(eo, rev, m), f"{t} EBITDA margin FY{year}")
        num = {
            "gross_margin": "gross_profit",
            "operating_margin": "operating_income",
            "net_margin": "net_income",
        }[m]
        return labelled(fin.margin(d.fy_op(num, year), rev, m), f"{t} {m} FY{year}")

    # market-based metrics: latest price from the price file, latest balance sheet
    price = _price(d, prices)
    notes = [] if price else [f"no price for {t} in the price file"]
    if args.fiscal_year is not None and m != "pe":
        notes.append(
            f"fiscal_year {args.fiscal_year} ignored: uses the latest price and balance sheet"
        )
    debt, cash = d.latest_instant("total_debt"), d.latest_instant("cash")
    shares = d.latest_instant("shares_outstanding")
    items = {
        "market_cap": ("shares_outstanding",),
        "net_debt_to_ebitda": ("total_debt", "cash"),
        "pe": (),
    }.get(m, ("total_debt", "cash", "shares_outstanding"))
    notes += [d.stale[i] for i in items if i in d.stale]
    relevant = {"market_cap": (shares,), "net_debt_to_ebitda": (debt, cash), "pe": ()}
    used = [o for o in relevant.get(m, (debt, cash, shares)) if o is not None]
    if len({o.period_end for o in used}) > 1:
        notes.append(
            "balance-sheet inputs are from different dates: "
            + ", ".join(f"{o.label.split()[1]} at {o.period_end}" for o in used)
        )
    price_note = "; ".join(notes)
    if m == "pe":
        year = _target_year(d, "eps_diluted", args.fiscal_year)
        eps = d.fy_op("eps_diluted", year) if year else None
        basis = f"FY{year} diluted EPS" if year else "no diluted EPS reported"
        pe_note = "" if price else f"no price for {t} in the price file"
        return labelled(fin.pe(price, eps), f"{t} P/E ({basis})", pe_note)
    if m == "net_debt_to_ebitda":
        eb, basis = _ebitda_flow(d)
        c = fin.net_debt_to_ebitda(debt, cash, eb)
        return labelled(
            c,
            f"{t} net debt / EBITDA ({basis})",
            "; ".join(n for n in notes if not n.startswith("no price")),
        )
    mc = labelled(fin.market_cap(price, shares), f"{t} market cap", price_note)
    if m == "market_cap":
        return mc
    mco = mc.as_operand(f"{t} market cap") if mc.value is not None else None
    ev = labelled(
        fin.enterprise_value(mco, debt, cash),
        f"{t} enterprise value",
        price_note,
    )
    if m == "enterprise_value":
        return ev
    evo = ev.as_operand(f"{t} EV") if ev.value is not None else None
    if m == "ev_to_revenue":
        rev, basis = _flow(d, "revenue")
        return labelled(fin.ev_to_revenue(evo, rev), f"{t} EV / revenue ({basis})", price_note)
    eb, basis = _ebitda_flow(d)
    return labelled(fin.ev_to_ebitda(evo, eb), f"{t} EV / EBITDA ({basis})", price_note)
