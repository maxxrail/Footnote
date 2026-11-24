"""Generate the numeric golden set from XBRL ground truth.

    uv run python -m analyst.evals.build_gold          # writes datasets/numeric.jsonl

Templates × companies × years, sampled with a fixed seed so the set is reproducible:

| template | question | expected (from analyst.metrics / reported facts) |
|---|---|---|
| fy_revenue | total revenue for the fiscal year ended {date} | the revenue fact |
| fy_net_income | net income for the fiscal year ended {date} | the net income fact |
| fy_eps | diluted EPS for the fiscal year ended {date} | the EPS fact (skipped if split-mixed) |
| revenue_growth | revenue growth from FY{y-1} to FY{y} | `fin.growth` |
| operating_margin | operating margin for FY{y} | `fin.margin` |
| revenue_cagr | 3-year revenue CAGR to FY{y} | `fin.cagr` |
| q_revenue | revenue for the quarter ended {date} | the 10-Q fact, or Q4 = FY - Q1-Q3 |
| ev_to_revenue | EV/Revenue on the latest price | the `compute` tool's value (comps rules) |

Each question names the period by its end date, so fiscal-calendar labels can't make it
ambiguous. Items whose inputs are missing are skipped, never guessed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.agent.tools import ToolBox, load_universe
from analyst.config import get_settings
from analyst.db.models import Company
from analyst.db.session import session_scope
from analyst.edgar.concepts import SeriesPoint, get_series
from analyst.evals.datasets import DATASETS, Item, save
from analyst.metrics import finance as fin
from analyst.metrics.computed import Operand
from analyst.metrics.prices import CsvPriceProvider
from analyst.metrics.quarters import DerivedPoint, quarterly_series
from analyst.workflows.tearsheet import split_warning

SEED = 20261124
RECENT_YEARS = 3  # sample from each company's latest 3 fiscal years
EV_COMPANIES = 12


def _date(d: object) -> str:
    return f"{d:%B} {d.day}, {d.year}"  # type: ignore[attr-defined]


def _op(p: SeriesPoint) -> Operand:
    return Operand(
        p.canonical,
        p.value,
        (p.fact_id,),
        "fact",
        p.accession,
        p.period_start,
        p.period_end,
        p.unit,
    )


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f") if v == v.to_integral_value() else str(v)


def company_items(s: Session, co: Company, rng: random.Random) -> list[Item]:
    fy = {
        k: {p.fiscal_year: p for p in get_series(s, co.cik, k, "FY")}
        for k in ("revenue", "net_income", "eps_diluted", "operating_income")
    }
    years = sorted(fy["revenue"])[-RECENT_YEARS:]
    if not years:
        return []
    name, t = co.name, co.ticker
    out: list[Item] = []

    def add(template: str, question: str, value: Decimal | None, unit: str, ids: list[int]) -> None:
        if value is None:
            return
        out.append(
            Item(
                id=f"num-{template}-{t}",
                category="numeric",
                question=question,
                tickers=[t],
                template=template,
                expected=_num(value),
                unit=unit,
                fact_ids=ids,
            )
        )

    for key, template, label, unit in (
        ("revenue", "fy_revenue", "total revenue", "USD"),
        ("net_income", "fy_net_income", "net income attributable to the company", "USD"),
    ):
        y = rng.choice([y for y in years if y in fy[key]] or [None])
        if y is not None:
            p = fy[key][y]
            add(
                template,
                f"What was {name}'s {label} for the fiscal year ended {_date(p.period_end)}?",
                p.value,
                unit,
                [p.fact_id],
            )

    eps_years = [y for y in years if y in fy["eps_diluted"]]
    if eps_years and not split_warning(fy["eps_diluted"], fy["net_income"], eps_years):
        p = fy["eps_diluted"][rng.choice(eps_years)]
        add(
            "fy_eps",
            f"What was {name}'s diluted earnings per share for the fiscal year ended "
            f"{_date(p.period_end)}?",
            p.value,
            "USD/shares",
            [p.fact_id],
        )

    growth_years = [y for y in years if y - 1 in fy["revenue"] and fy["revenue"][y - 1].value > 0]
    if growth_years:
        y = rng.choice(growth_years)
        cur, pri = fy["revenue"][y], fy["revenue"][y - 1]
        g = fin.growth(_op(cur), _op(pri))
        add(
            "revenue_growth",
            f"By what percentage did {name}'s revenue change from the fiscal "
            f"year ended {_date(pri.period_end)} to the fiscal year ended {_date(cur.period_end)}?",
            g.value,
            "ratio",
            [cur.fact_id, pri.fact_id],
        )

    margin_years = [y for y in years if y in fy["operating_income"]]
    if margin_years:
        y = rng.choice(margin_years)
        oi, rev = fy["operating_income"][y], fy["revenue"][y]
        m = fin.margin(_op(oi), _op(rev))
        add(
            "operating_margin",
            f"What was {name}'s operating margin (operating income as a "
            f"share of revenue) for the fiscal year ended {_date(rev.period_end)}?",
            m.value,
            "ratio",
            [oi.fact_id, rev.fact_id],
        )

    end = years[-1]
    if end - 3 in fy["revenue"]:
        a, b = fy["revenue"][end - 3], fy["revenue"][end]
        c = fin.cagr(_op(a), _op(b), 3)
        add(
            "revenue_cagr",
            f"What was {name}'s compound annual revenue growth rate over the "
            f"three fiscal years from the year ended {_date(a.period_end)} to the year ended "
            f"{_date(b.period_end)}?",
            c.value,
            "ratio",
            [a.fact_id, b.fact_id],
        )

    quarters = quarterly_series(s, co.cik, "revenue")[-4:]
    if quarters:
        q = rng.choice(quarters)
        ids = [i.fact_id for i in q.inputs] if isinstance(q, DerivedPoint) else [q.fact_id]
        add(
            "q_revenue",
            f"What was {name}'s revenue for the fiscal quarter ended {_date(q.period_end)}?",
            q.value,
            "USD",
            ids,
        )
    return out


def ev_items(box: ToolBox, s: Session, tickers: Sequence[str], names: dict[str, str]) -> list[Item]:
    out = []
    for t in tickers:
        r = json.loads(box.run("compute", {"metric": "ev_to_revenue", "tickers": [t]}))
        res = r["results"][0]
        if res["value"] is None:
            continue
        cite = box.refs[res["ref"]]
        ids = [f.fact_id for f in cite.facts]  # type: ignore[union-attr]
        out.append(
            Item(
                id=f"num-ev_to_revenue-{t}",
                category="numeric",
                tickers=[t],
                template="ev_to_revenue",
                expected=res["value"],
                unit="x",
                fact_ids=ids,
                question=f"Using the latest share price in the data, what is "
                f"{names[t]}'s enterprise value to revenue (EV/Revenue) multiple?",
            )
        )
    return out


def build() -> list[Item]:
    settings = get_settings()
    rng = random.Random(SEED)
    universe = load_universe(settings.universe_csv)
    items: list[Item] = []
    with session_scope() as s:
        cos = {c.ticker: c for c in s.scalars(select(Company))}
        for row in universe:
            if row.ticker in cos:
                items += company_items(s, cos[row.ticker], rng)
        box = ToolBox(session_scope, CsvPriceProvider(settings.prices_csv), settings.universe_csv)
        non_banks = [r.ticker for r in universe if r.peer_group != "us_banks" and r.ticker in cos]
        picks = sorted(rng.sample(non_banks, min(EV_COMPANIES, len(non_banks))))
        items += ev_items(box, s, picks, {t: cos[t].name for t in picks})
    return items


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI over the dev DB
    ap = argparse.ArgumentParser(prog="python -m analyst.evals.build_gold")
    ap.parse_args(argv)
    items = build()
    save(DATASETS / "numeric.jsonl", items)
    by: dict[str | None, int] = {}
    for it in items:
        by[it.template] = by.get(it.template, 0) + 1
    print(f"{len(items)} numeric items -> datasets/numeric.jsonl: {by}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
