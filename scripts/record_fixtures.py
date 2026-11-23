"""Record trimmed SEC fixtures for offline tests (run by hand; needs SEC_USER_AGENT).

    uv run python scripts/record_fixtures.py

Writes tests/fixtures/{company_tickers,<ticker>_submissions,<ticker>_companyfacts}.json,
trimmed to the canonical concepts and recent years so the files stay small.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from analyst.config import get_settings
from analyst.edgar.client import SecClient
from analyst.edgar.concepts import CONCEPTS

TICKERS = ("AAPL", "MSFT")
SINCE = "2016-01-01"  # keep facts whose period ends on/after this date
FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A"}
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def trim_facts(cf: dict[str, Any]) -> dict[str, Any]:
    wanted = {c for concept in CONCEPTS.values() for c in concept.candidates}
    facts: dict[str, dict[str, Any]] = {}
    for taxonomy, items in cf["facts"].items():
        for name, body in items.items():
            if f"{taxonomy}:{name}" not in wanted:
                continue
            units = {
                unit: [e for e in entries if e["end"] >= SINCE]
                for unit, entries in body["units"].items()
            }
            units = {u: es for u, es in units.items() if es}
            if units:
                facts.setdefault(taxonomy, {})[name] = {
                    "label": body.get("label"),
                    "description": None,
                    "units": units,
                }
    return {"cik": cf["cik"], "entityName": cf["entityName"], "facts": facts}


def trim_submissions(sub: dict[str, Any]) -> dict[str, Any]:
    recent = sub["filings"]["recent"]
    keep = [i for i, f in enumerate(recent["form"]) if f in FORMS][:60]
    cols = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")
    return {
        **{
            k: sub.get(k)
            for k in ("cik", "name", "tickers", "sic", "sicDescription", "fiscalYearEnd")
        },
        "filings": {"recent": {c: [recent[c][i] for i in keep] for c in cols}, "files": []},
    }


def _default(o: object) -> object:
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o))


def dump(name: str, data: Any) -> None:
    path = OUT / name
    path.write_text(json.dumps(data, indent=1, default=_default) + "\n", encoding="utf-8")
    print(f"{path.relative_to(OUT.parents[1])}: {path.stat().st_size / 1024:.0f} KB")


def main() -> None:
    s = get_settings()
    OUT.mkdir(parents=True, exist_ok=True)
    with SecClient(s.sec_user_agent, s.raw_dir) as client:
        tickers = client.get_json("https://www.sec.gov/files/company_tickers.json")
        dump("company_tickers.json", {k: v for k, v in tickers.items() if v["ticker"] in TICKERS})
        cik_map = {v["ticker"]: v["cik_str"] for v in tickers.values()}
        for t in TICKERS:
            cik = cik_map[t]
            dump(f"{t.lower()}_submissions.json", trim_submissions(client.submissions(cik)))
            dump(f"{t.lower()}_companyfacts.json", trim_facts(client.companyfacts(cik)))


if __name__ == "__main__":
    main()
