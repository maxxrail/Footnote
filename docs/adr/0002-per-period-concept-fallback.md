# 0002. Canonical concepts: per-period candidate fallback

- Status: accepted
- Date: 2026-11-23
- Part: 1

## Context

Companies tag the same line item with different us-gaap concepts, and they change tags over
time (ASC 606 moved most issuers from `SalesRevenueNet`/`Revenues` to
`RevenueFromContractWithCustomerExcludingAssessedTax` around 2018). `get_series` has to turn
this into one clean value per period, traceable to one fact.

## Options

1. **First candidate with any data, for all periods**: simple and consistent within a series,
   but it silently drops every year reported under a different tag.
2. **Per-period fallback**: for each period, take the value from the latest filing, and use
   tag priority to choose between tags inside that filing. Continuous series, but adjacent years may come from different tags (the tag is
   returned with every point, so this is visible).
3. **Hand-curated per-company mapping**: most precise, but doesn't scale past the universe
   and needs upkeep.

## Decision

Option 2. Measured on the 25-company universe for FY `revenue`:

- per-period fallback: **439** fiscal-year points
- first-candidate-only: **280** points; **17 of 25** companies lose years (AAPL keeps
  **3 of 20**, MSFT **3 of 19**, MU **3 of 18**).

Other rules, all unit-tested and mutation-checked:
- FY = annual forms (10-K, 10-K/A, 10-KT) with 350-380 day durations. Instants must fall
  within 7 days of the company's fiscal year end (52/53-week years drift).
- Q = 10-Q facts with 80-100 day durations. Year-to-date figures are skipped and Q4 is not
  derived, because deriving is arithmetic and belongs in `analyst/metrics/` (Part 3).
- Restatements: the latest filing (filed date, then accession) wins, **even across a tag
  change**. An earlier draft did priority first and kept a stale value when a company restated
  under a new tag; the part-reviewer caught it. FY points are keyed by fiscal year because
  GS tagged FY2008 with two end dates (11-28 and 11-30).

## Consequences

- Each point carries `concept`, `fact_id` and `accession`, so Part 3 can cite exactly
  which tag and filing a number came from.
- Items a company never tags stay empty rather than being derived. Example: 5 of 25
  companies don't tag `Liabilities`, and banks have no `GrossProfit`. Part 3 can derive
  them in `analyst/metrics/` with a citation to each input.
- Revisit if evals in Part 5 show wrong-tag picks (e.g. `Revenues` meaning something
  narrower than total revenue for some issuer).
