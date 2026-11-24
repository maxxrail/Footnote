# 0006. Comps workbook: live formulas over cited inputs, tested by recalculation

- Status: accepted
- Date: 2026-11-24
- Part: 4

## Context

A comps sheet is only useful to a banker if they can audit and change it. That means:
- every multiple is a formula they can trace;
- every input says where it came from;
- a missing or meaningless number shows as NM, never `#DIV/0!` or a silent zero.

The tests can't open Excel, and LibreOffice isn't installed here, yet they must show the
formulas compute the right numbers.

## Options

Sheet design:
1. **Values only** (compute in Python, paste numbers): trivially correct, but not auditable and not what the spec asks for.
2. **Formulas on Comps over raw values on Inputs**, NM by `IF(AND(ISNUMBER(..)), IF(den>0, num/den, "NM"), "NM")`, stats by `MEDIAN`/`AVERAGE`/`QUARTILE.INC` (which skip text).

Testing the formulas:
- a. Assert formula strings only.
- b. Recalculate with LibreOffice headless (not installed; heavy for CI).
- c. Recalculate with the `formulas` Python package and compare every cell to the same arithmetic done by `analyst.metrics`.

## Decision

**Option 2, tested with (c).**
- **Inputs:** raw values (blue), in full dollars displayed in millions (`$#,##0,,`). Each has a comment with its facts (concept, period, form, accession, fact id) or its price row.
- **Comps:** market cap, EV, EBITDA and the three multiples as formulas (black); the stats rows cover peers only.
- **Sources:** each filing used, with a link.

`formulas` evaluates `IF`, `AND`, `ISNUMBER`, `IFERROR`, `MEDIAN`, `AVERAGE`, `QUARTILE.INC` and `COUNT` as Excel does: "NM" text is skipped, and the 25th percentile of {10, 20} is 12.5. The test recalculates every Comps cell of the fixture workbook and compares it to `analyst.metrics`; they agree on all 6 columns for every company. The three live workbooks recalculate with no errors:

| Workbook | Formulas | NM cells | Errors | Cited inputs | Source filings |
|---|---|---|---|---|---|
| comps_MSFT | 87 | 5 | none | 71 | ~36 |
| comps_NVDA | 79 | 1 | none | 64 | ~32 |
| comps_JPM | 79 | 24 | none | 46 | ~32 |

Other calls:
- **P/E = market cap / net income** over the net-income basis, so it is a formula on the
  sheet. The agent's `compute pe` uses price / FY diluted EPS and can differ slightly.
- **Stats exclude the target**, the usual comps convention, and `n` shows how many peers had a value.
- **Shares for market cap: the newer of the balance-sheet count and the cover-page count**
  (`dei:EntityCommonStockSharesOutstanding`). Citigroup, PNC and Texas Instruments tag only the
  cover page recently. Before this, 4 of 25 companies had no market cap (C, PNC, TXN, WDAY);
  after it, 1 does (Workday, last tagged 2018). Both counts are refused after 400 days and noted
  after 100, as in Part 3.

## Consequences

- A banker can change an input and every multiple and statistic updates; the Python check
  values only matter for the tests and the CLI summary.
- **Mixed bases are visible, not fixed.** Revenue and net income are usually LTM. D&A is
  rarely tagged by quarter (only year-to-date cash flow, which Part 1 skips), so EBITDA often
  falls back to the latest fiscal year. Each row's bases are on the Inputs sheet. Deriving
  LTM D&A from year-to-date values is the real fix.
- **Banks:** EV and EBITDA are not meaningful (no operating income; deposits aren't debt), so
  EV/EBITDA is NM for every bank and EV/Revenue for some. P/E is the usable bank multiple.
  Price-to-book would need a book-value concept, which isn't ingested.
- Excel itself was not used here. Maxx opens one workbook by hand (acceptance check 3).
