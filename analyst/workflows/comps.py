"""Public comps: target + peers -> a formula-driven Excel workbook.

    uv run python -m analyst.workflows run comps --ticker MSFT
    uv run python -m analyst.workflows run comps --ticker MSFT --peers ORCL,CRM,ADBE

Steps (all deterministic):
1. **resolve** the target and its peers (its `peer_group` in `data/universe.csv`, or `--peers`).
2. **gather** each company's inputs with the agent's valuation rules (`analyst.agent.tools`):
   - latest price;
   - share count, the newer of balance sheet and cover page;
   - latest debt and cash;
   - revenue and net income: LTM if four contiguous quarters reach the latest fiscal year,
     else that year;
   - operating income + D&A over one shared window.
   Stale balances are refused or noted.
3. **check** values with `analyst.metrics` (the same numbers the formulas must produce).
4. **write** `comps_{TICKER}_{date}.xlsx`:
   - `Inputs`: raw values in blue, each with a cell comment citing its facts or price row.
   - `Comps`: market cap, EV, EBITDA and multiples as live formulas on `Inputs`, in black.
     Multiples are "NM" when an input is missing or a denominator is not positive. The
     median, mean, 25th and 75th percentiles (QUARTILE, which Excel defines as QUARTILE.INC;
     the .INC name would need an `_xlfn.` prefix in the file) are over the peers only; Excel
     skips "NM" text, and `n` counts the peers with a value.
   - `Sources`: every filing used, with its URL, and the price file.

P/E here is market cap / net income over the same basis as the other flows. The agent's
`compute pe` (price / FY diluted EPS) can differ slightly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.agent.tools import (
    CompanyData,
    ToolError,
    ebitda_parts,
    flow,
    load_universe,
    price_of,
)
from analyst.db.models import Company, Fact, Filing
from analyst.metrics import finance as fin
from analyst.metrics.computed import Computed, Operand
from analyst.metrics.stats import peer_stat
from analyst.workflows.base import Context, State, Step, Workflow

# Inputs sheet columns: (key, header, number format)
INPUTS = [
    ("price", "Price", "$#,##0.00"),
    ("shares", "Shares (M)", "#,##0.0,,"),
    ("total_debt", "Total debt ($M)", "$#,##0,,"),
    ("cash", "Cash ($M)", "$#,##0,,"),
    ("revenue", "Revenue ($M)", "$#,##0,,"),
    ("operating_income", "Operating income ($M)", "$#,##0,,"),
    ("d_and_a", "D&A ($M)", "$#,##0,,"),
    ("net_income", "Net income ($M)", "$#,##0,,"),
]
MONEY = "$#,##0,,"
MULT = '0.0"x"'
BLUE = Font(color="0000FF")
BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
STATS = [
    ("Median", "MEDIAN({r})", "median"),
    ("Mean", "AVERAGE({r})", "mean"),
    ("25th percentile", "QUARTILE({r},1)", "q1"),
    ("75th percentile", "QUARTILE({r},3)", "q3"),
]
MULTIPLES = ["ev_to_revenue", "ev_to_ebitda", "pe"]


class CompsInputs(BaseModel):
    ticker: str
    peers: list[str] | None = Field(None, description="Override the universe peer group")

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


@dataclass
class Row:
    ticker: str
    name: str
    is_target: bool
    inputs: dict[str, Operand | None] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    check: dict[str, Computed] = field(default_factory=dict)  # Python values for each formula
    price_date: dt.date | None = None

    @property
    def fact_ids(self) -> list[int]:
        return [f for op in self.inputs.values() if op is not None for f in op.fact_ids]


# -- steps -----------------------------------------------------------------------------------


def resolve(state: State, ctx: Context) -> State:
    args: CompsInputs = state["inputs"]
    universe = {r.ticker: r for r in load_universe(ctx.universe_path)}
    if args.ticker not in universe:
        raise ToolError(f"{args.ticker} is not in the covered universe")
    if args.peers is not None:
        given = [p.strip().upper() for p in args.peers if p.strip()]
        peers = [p for p in dict.fromkeys(given) if p != args.ticker]  # no duplicates
        unknown = [p for p in peers if p not in universe]
        if unknown:
            raise ToolError(f"peers not in the covered universe: {unknown}")
    else:
        group = universe[args.ticker].peer_group
        peers = [t for t, r in universe.items() if r.peer_group == group and t != args.ticker]
    state["tickers"] = [args.ticker, *peers]
    state["names"] = {t: universe[t].name for t in state["tickers"]}
    return state


def gather(state: State, ctx: Context) -> State:
    rows = []
    with ctx.session_factory() as s:
        for t in state["tickers"]:
            row = Row(t, state["names"][t], is_target=t == state["tickers"][0])
            co = s.scalar(select(Company).where(Company.ticker == t))
            if co is None:
                row.notes.append("no data loaded for this company")
                row.inputs = {k: None for k, _, _ in INPUTS}
                rows.append(row)
                continue
            row.name = co.name
            d = CompanyData(s, co)
            price = price_of(d, ctx.prices)
            row.price_date = price.period_end if price else None
            revenue, rev_basis = flow(d, "revenue")
            oi, da, ebitda_basis = ebitda_parts(d)
            ni, ni_basis = flow(d, "net_income")
            row.inputs = {
                "price": price,
                "shares": d.market_cap_shares(),
                "total_debt": d.latest_instant("total_debt"),
                "cash": d.latest_instant("cash"),
                "revenue": revenue,
                "operating_income": oi,
                "d_and_a": da,
                "net_income": ni,
            }
            row.basis = {"revenue": rev_basis, "ebitda": ebitda_basis, "net_income": ni_basis}
            if price is None:
                row.notes.append("no price in the price file")
            row.notes += list(d.stale.values())
            if len({rev_basis, ebitda_basis, ni_basis} - {"none"}) > 1:
                row.notes.append(
                    f"bases differ: revenue {rev_basis}, EBITDA {ebitda_basis}, "
                    f"net income {ni_basis}"
                )
            rows.append(row)
    state["rows"] = rows
    return state


def check(state: State, ctx: Context) -> State:
    """The same arithmetic as the sheet's formulas, in `analyst.metrics` (tests compare)."""
    for row in state["rows"]:
        i = row.inputs
        mc = fin.market_cap(i["price"], i["shares"])
        mco = mc.as_operand(f"{row.ticker} market cap") if mc.value is not None else None
        ev = fin.enterprise_value(mco, i["total_debt"], i["cash"])
        evo = ev.as_operand(f"{row.ticker} EV") if ev.value is not None else None
        e = fin.ebitda(i["operating_income"], i["d_and_a"])
        eo = e.as_operand(f"{row.ticker} EBITDA") if e.value is not None else None
        row.check = {
            "market_cap": mc,
            "enterprise_value": ev,
            "ebitda": e,
            "ev_to_revenue": fin.ev_to_revenue(evo, i["revenue"]),
            "ev_to_ebitda": fin.ev_to_ebitda(evo, eo),
            "pe": fin.multiple(mco, i["net_income"], "pe", "market cap / net income"),
        }
        for m in MULTIPLES:
            c = row.check[m]
            if c.value is None:
                row.notes.append(f"{_LABELS[m]} NM: {c.note}")
    peers = [r for r in state["rows"] if not r.is_target]
    state["stats"] = {
        m: {key: peer_stat([r.check[m] for r in peers], key, m) for _, _, key in STATS}
        for m in MULTIPLES
    }
    return state


def write(state: State, ctx: Context) -> State:
    args: CompsInputs = state["inputs"]
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.out_dir / f"comps_{args.ticker}_{ctx.today.isoformat()}.xlsx"
    with ctx.session_factory() as s:
        facts = fact_details(s, sorted({f for r in state["rows"] for f in r.fact_ids}))
    wb = build_workbook(state["rows"], facts, ctx.today)
    wb.save(path)
    state["path"] = path
    return state


_LABELS = {"ev_to_revenue": "EV/Revenue", "ev_to_ebitda": "EV/EBITDA", "pe": "P/E"}


class Comps(Workflow):
    name: ClassVar[str] = "comps"
    Inputs: ClassVar[type[BaseModel]] = CompsInputs

    def steps(self, inputs: Any) -> list[Step]:
        return [
            Step("resolve peers", "deterministic", resolve),
            Step("gather inputs", "deterministic", gather),
            Step("compute check values", "deterministic", check),
            Step("write xlsx", "deterministic", write),
        ]

    def outputs(self, state: State) -> tuple[list[Path], str]:
        target = state["rows"][0]
        parts = []
        for m in MULTIPLES:
            v, med = target.check[m].value, state["stats"][m]["median"].value
            parts.append(f"{_LABELS[m]} {_fmt(v)} vs peer median {_fmt(med)}")
        return [state["path"]], f"{target.ticker}: " + "; ".join(parts)


def _fmt(v: Decimal | None) -> str:
    return "NM" if v is None else f"{v:.1f}x"


# -- workbook ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FactDetail:
    fact_id: int
    concept: str
    period: str
    value: Decimal
    accession: str
    form: str
    filed_at: dt.date
    url: str
    ticker: str
    unit: str = ""

    def describe(self) -> str:
        """Concept, period, value, unit, form, accession and fact id: one XBRL citation."""
        unit = f" {self.unit}" if self.unit else ""
        return (
            f"{self.concept} {self.period} = {self.value}{unit} "
            f"({self.form} {self.accession}, fact {self.fact_id})"
        )


def fact_details(s: Session, ids: list[int]) -> dict[int, FactDetail]:
    if not ids:
        return {}
    rows = s.execute(
        select(Fact, Filing, Company)
        .join(Filing, Filing.accession == Fact.accession)
        .join(Company, Company.cik == Fact.cik)
        .where(Fact.id.in_(ids))
    ).all()
    return {
        f.id: FactDetail(
            f.id,
            f.concept,
            f"{f.period_start}..{f.period_end}" if f.period_start else f"at {f.period_end}",
            f.value,
            f.accession,
            fl.form,
            fl.filed_at,
            fl.url,
            co.ticker,
            f.unit,
        )
        for f, fl, co in rows
    }


def _comment(op: Operand, facts: dict[int, FactDetail]) -> str:
    if op.source == "price":
        return f"{op.label}\nSource: {op.detail} (Yahoo Finance daily close)"
    lines = [op.label]
    if op.source == "computed":
        lines.append(f"= {op.detail}")
    for fid in op.fact_ids:
        f = facts.get(fid)
        if f:
            lines.append(f"{f.concept} {f.period} = {f.value} ({f.form} {f.accession}, fact {fid})")
    return "\n".join(lines)


def _header(ws: Any, headers: list[str]) -> None:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "C2"


def build_workbook(rows: list[Row], facts: dict[int, FactDetail], today: dt.date) -> Workbook:
    wb = Workbook()
    comps = wb.active
    assert comps is not None
    comps.title = "Comps"
    inputs = wb.create_sheet("Inputs")
    sources = wb.create_sheet("Sources")

    # Inputs: one row per company, raw values (blue) with a citation comment each.
    in_cols = {key: 3 + i for i, (key, _, _) in enumerate(INPUTS)}
    _header(
        inputs,
        ["Ticker", "Company"]
        + [h for _, h, _ in INPUTS]
        + ["Price date", "Revenue basis", "EBITDA basis", "Net income basis", "Notes"],
    )
    extra = 3 + len(INPUTS)
    for r, row in enumerate(rows, 2):
        inputs.cell(row=r, column=1, value=row.ticker).font = BOLD if row.is_target else Font()
        inputs.cell(row=r, column=2, value=row.name)
        for key, _, fmt in INPUTS:
            op = row.inputs.get(key)
            cell = inputs.cell(row=r, column=in_cols[key])
            if op is not None:
                cell.value = float(op.value)
                cell.number_format = fmt
                cell.font = BLUE
                cell.comment = Comment(_comment(op, facts), "analyst-agent", width=420, height=160)
        inputs.cell(row=r, column=extra, value=row.price_date.isoformat() if row.price_date else "")
        for j, key in enumerate(("revenue", "ebitda", "net_income"), 1):
            inputs.cell(row=r, column=extra + j, value=row.basis.get(key, ""))
        inputs.cell(row=r, column=extra + 4, value="; ".join(row.notes))

    # Comps: formulas only, referencing Inputs.
    headers = [
        "Ticker",
        "Company",
        "Market cap ($M)",
        "Enterprise value ($M)",
        "Revenue ($M)",
        "EBITDA ($M)",
        "Net income ($M)",
        "EV / Revenue",
        "EV / EBITDA",
        "P / E",
        "Notes",
    ]
    _header(comps, headers)

    def ref(key: str, r: int) -> str:
        return f"Inputs!{get_column_letter(in_cols[key])}{r}"

    def num(*cells: str) -> str:
        return ",".join(f"ISNUMBER({c})" for c in cells)

    peer_rows: list[int] = []
    for r, row in enumerate(rows, 2):
        if not row.is_target:
            peer_rows.append(r)
        p, sh, debt, cash = ref("price", r), ref("shares", r), ref("total_debt", r), ref("cash", r)
        rev, oi, da, ni = (
            ref("revenue", r),
            ref("operating_income", r),
            ref("d_and_a", r),
            ref("net_income", r),
        )
        formulas = {
            3: f'=IF(AND({num(p, sh)}),IF(AND({p}>0,{sh}>0),{p}*{sh},"NM"),"NM")',
            4: f'=IF(AND({num(f"C{r}", debt, cash)}),C{r}+{debt}-{cash},"NM")',
            5: f'=IF(ISNUMBER({rev}),{rev},"NM")',
            6: f'=IF(AND({num(oi, da)}),{oi}+{da},"NM")',
            7: f'=IF(ISNUMBER({ni}),{ni},"NM")',
            8: f'=IF(AND({num(f"D{r}", f"E{r}")}),IF(E{r}>0,D{r}/E{r},"NM"),"NM")',
            9: f'=IF(AND({num(f"D{r}", f"F{r}")}),IF(F{r}>0,D{r}/F{r},"NM"),"NM")',
            10: f'=IF(AND({num(f"C{r}", f"G{r}")}),IF(G{r}>0,C{r}/G{r},"NM"),"NM")',
        }
        comps.cell(row=r, column=1, value=row.ticker).font = BOLD if row.is_target else Font()
        comps.cell(row=r, column=2, value=row.name + (" (target)" if row.is_target else ""))
        for c, f in formulas.items():
            cell = comps.cell(row=r, column=c, value=f)
            cell.number_format = MONEY if c <= 7 else MULT
            cell.alignment = Alignment(horizontal="right")
        comps.cell(row=r, column=11, value="; ".join(row.notes))

    # Peer statistics (target excluded). Excel's MEDIAN/AVERAGE/QUARTILE skip "NM" text.
    first = len(rows) + 3
    comps.cell(
        row=first - 1, column=1, value="Peer statistics (target excluded; NM skipped)"
    ).font = BOLD
    span = (min(peer_rows), max(peer_rows)) if peer_rows else None
    for k, (label, formula, _) in enumerate(
        [*STATS, ("n (peers with a value)", "COUNT({r})", "n")]
    ):
        r = first + k
        comps.cell(row=r, column=2, value=label).font = BOLD
        for c in (8, 9, 10):
            col = get_column_letter(c)
            rng = f"{col}{span[0]}:{col}{span[1]}" if span else None
            if rng is None:
                value: str = "NM"
            elif label.startswith("n "):
                value = f"={formula.format(r=rng)}"
            else:
                value = f'=IFERROR({formula.format(r=rng)},"NM")'
            cell = comps.cell(row=r, column=c, value=value)
            cell.number_format = "0" if label.startswith("n ") else MULT
            cell.alignment = Alignment(horizontal="right")
    comps.cell(
        row=first + len(STATS) + 2,
        column=1,
        value=(
            "Inputs in blue on the Inputs sheet (hover for each value's filing); formulas in "
            "black. NM = not meaningful: an input is missing or the denominator is not "
            f"positive. P/E = market cap / net income. Prices as of the date on Inputs. "
            f"Built {today.isoformat()}."
        ),
    )

    # Sources: every filing behind a value, and the price file.
    _header(sources, ["Ticker", "Form", "Filed", "Accession", "Facts used", "URL"])
    by_filing: dict[str, list[FactDetail]] = {}
    for detail in facts.values():
        by_filing.setdefault(detail.accession, []).append(detail)
    r = 2
    for acc, fs in sorted(by_filing.items(), key=lambda kv: (kv[1][0].ticker, kv[1][0].filed_at)):
        f0 = fs[0]
        sources.cell(row=r, column=1, value=f0.ticker)
        sources.cell(row=r, column=2, value=f0.form)
        sources.cell(row=r, column=3, value=f0.filed_at.isoformat())
        sources.cell(row=r, column=4, value=acc)
        sources.cell(row=r, column=5, value=", ".join(sorted({x.concept for x in fs})))
        link = sources.cell(row=r, column=6, value=f0.url)
        link.hyperlink = f0.url
        link.font = Font(color="0563C1", underline="single")
        r += 1
    sources.cell(
        row=r + 1,
        column=1,
        value="Prices: data/prices.csv (Yahoo Finance daily closes; refresh with "
        "scripts/fetch_prices.py). Each price cell's comment gives its row.",
    )

    for ws, widths in (
        (comps, [8, 30, 14, 16, 14, 14, 14, 12, 12, 10, 60]),
        (inputs, [8, 30, *[14] * len(INPUTS), 12, 18, 18, 18, 60]),
        (sources, [8, 8, 12, 24, 50, 90]),
    ):
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = w
        ws.row_dimensions[1].height = 32
    return wb
