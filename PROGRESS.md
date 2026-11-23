# Progress

**Current part:** 1 (done, in review). Next: 2

| Part | Name | Status |
|------|------|--------|
| 1 | Financial data layer (SEC EDGAR + XBRL) | done (v0.1.0, PR open) |
| 2 | Filing search with citations (hybrid retrieval) | not started |
| 3 | The analyst agent (tools, deterministic math, cited answers) | not started |
| 4 | Banker deliverables (comps → Excel, tearsheet, earnings brief) | not started |
| 5 | Evals (golden set, numeric + citation graders, CI gate) | not started |
| 6 | Ship it (API, web UI, Docker, deploy, observability, demo) | not started |

## Decisions
<!-- Claude Code: record judgment calls here, one line each, with the part number. -->
- P1: ADR 0001: cache SEC responses forever (`--refresh` to re-fetch JSON); upserts write only changed rows.
- P1: ADR 0002: per-period candidate fallback for canonical concepts (439 vs 280 FY revenue points).
- P1: `docker compose` maps Postgres to host port **5433** (`POSTGRES_HOST_PORT`), because 5432 is taken on Maxx's machine. CI still uses its own 5432 service.
- P1: Rate limiter burst is 1, not 8. With burst 8 at 8/s the test saw 15 requests in the first second.
- P1: Only the `us-gaap` and `dei` taxonomies are ingested. `concept` is stored as `taxonomy:Name` (e.g. `us-gaap:Revenues`).
- P1: Facts whose accession isn't in the submissions listing get a stub `filings` row (form + filed date from the fact), so every fact has a filing FK. Stubs never overwrite full rows.
- P1: A filing listed under two companies (JPM's 13G on ADI) keeps the first company's `cik`/`url`, so re-runs don't flip it.
- P1: Older submissions pages are followed only until 3 10-Ks + 4 10-Qs are found (needed for GS, MS and other banks).
- P1: `SeriesPoint.fiscal_year` = calendar year of `period_end` (matches AAPL/MSFT/NVDA naming). Facts' own `fy`/`fp` belong to the reporting filing and are stored but not used for labels.
- P1: Per period, the **latest filing wins first** and tag priority only breaks ties inside one filing, so a restatement under a new tag beats the original under an old tag (review finding).
- P1: FY points are keyed by fiscal year, because GS tagged FY2008 as ending both 2008-11-28 and 2008-11-30. A year ending in the first week of January is labelled the prior year.
- P1: Instants: FY needs the fiscal year end ±7 days and Q needs a fiscal quarter end ±7 days. If the fiscal year end is unknown, instants are refused.
- P1: `shares_outstanding` uses only `us-gaap:CommonStockSharesOutstanding`. The dei cover-page count is measured weeks later and never matched a period end.
- P1: The SEC client follows redirects itself, so every hop is rate-limited and counted. `rate` must be < 10.
- P1: Tests have an autouse guard that blocks any socket connect except loopback.
- P1: Q series = discrete 80-100 day 10-Q facts, no YTD. Labelled with the company's **fiscal** year and quarter (`fiscal_period`; MSFT Jul–Sep 2025 = FY2026 Q1). Points are keyed by fiscal quarter, because NVDA tagged one year end as 01-25 and 01-27.
- P1 (Maxx asked for it after review): **Q4 is derived** in `analyst/metrics/quarters.py` as FY − (Q1+Q2+Q3), only for additive flow items, and only when Q1–Q3 tile the fiscal year. The result cites the FY fact and all three quarter facts. It is never derived for EPS or when a quarter is missing. Q4 balance-sheet values are the 10-K year-end balance (reported, not derived). Live: 1,483 derived Q4 points. MSFT FY2025 Q4 revenue = 76,441M, matching the $76.4B Microsoft reported.
- P1: CI workflows used `hashFiles()` in job-level `if:`, which GitHub rejects, so both workflows failed to parse. A gate job now checks for the file and the real job reads its output. The `pyproject.toml` gate was removed because that file now exists.
- P1: Missing line items are not derived (e.g. 5/25 companies don't tag `Liabilities`; banks have no `GrossProfit`). `get_series` returns empty for them.
- P1: XBRL values are parsed as `Decimal` (`json.loads(parse_float=Decimal)`), never float.
- P1: DB tests run in a throwaway Postgres schema per session. They skip locally if Postgres is down and fail in CI (`CI` env var).
- P1: Fixtures (`tests/fixtures/`, ~900 KB) are AAPL + MSFT, trimmed to canonical concepts since 2016. Re-record with `uv run python scripts/record_fixtures.py`.

## Acceptance results
<!-- Claude Code: paste the output of each part's acceptance checks here when done. -->

### Part 1 (2026-11-23)

**1. `docker compose up -d` then `uv run alembic upgrade head`: PASS** (fresh schema via `downgrade base` first)
```
 Container footnote-db-1 Running
INFO  [alembic.runtime.migration] Running downgrade 9c79c4d2bb5f -> , companies filings facts
INFO  [alembic.runtime.migration] Running upgrade  -> 9c79c4d2bb5f, companies filings facts
$ uv run alembic check
No new upgrade operations detected.
```

**2. `uv run pytest -q` passes; `uv run ruff check .` clean: PASS**
```
72 passed in 7.79s
All checks passed!
36 files already formatted
Success: no issues found in 19 source files   (mypy)
```
(Re-run after the review fixes: 72 passed. After adding fiscal-quarter labels and derived Q4: **92 passed**, ruff/format/mypy clean.)

**3. Live ingest of `data/universe.csv` finishes; second run makes 0 SEC requests and changes 0 rows: PASS**
```
# first live run (cold cache), after the pagination + shared-filing fixes were applied on top
25 companies | rows changed: companies=25 filings=108725 facts=823354 | documents downloaded=157 | SEC requests=203 cache hits=5
25 companies | rows changed: companies=0 filings=122297 facts=0 | documents downloaded=18 | SEC requests=78 cache hits=208
# rebuild into an empty DB from the disk cache
25 companies | rows changed: companies=25 filings=230954 facts=823354 | documents downloaded=0 | SEC requests=0 cache hits=286
# second run
25 companies | rows changed: companies=0 filings=0 facts=0 | documents downloaded=0 | SEC requests=0 cache hits=286
```
175/175 primary documents (3 10-K + 4 10-Q × 25) are in `data/raw/filings/{cik}/{accession}/`. `data/raw` totals 918 MB.

**4. `show AAPL revenue` returns ≥ 5 fiscal years, each with an accession; latest matches the 10-K: PASS**
```
AAPL  Apple Inc.  (CIK 320193)  revenue
FY    Period                            Value  Unit  Concept                                                      Form    Accession             Fact id
2007  2006-10-01..2007-09-29   24,578,000,000  USD   us-gaap:SalesRevenueNet                                      10-K/A  0001193125-10-012091  21268
...   (20 fiscal years, 2007-2026, all with accession numbers)
2016  2015-09-27..2016-09-24  215,639,000,000  USD   us-gaap:Revenues                                             10-K    0000320193-18-000145  21256
2017  2016-09-25..2017-09-30  229,234,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-19-000119  21136
2018  2017-10-01..2018-09-29  265,595,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-20-000096  21146
2019  2018-09-30..2019-09-28  260,174,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-21-000105  21166
2024  2023-10-01..2024-09-28  391,035,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-26-000026  21237
2025  2024-09-29..2025-09-27  416,161,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-26-000026  21249
2026  2025-09-28..2026-09-26  478,834,000,000  USD   us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax  10-K    0000320193-26-000026  21255
```
Spot check: accession `0000320193-26-000026` is Apple's 10-K filed 2026-11-03 for the period ending
2026-09-26 (`aapl-20260926.htm`, downloaded to `data/raw/filings/320193/0000320193-26-000026/`).
Its MD&A reads *"Total net sales $ 478,834 15 % $ 416,161 6 % $ 391,035"* ($ millions), which matches
478,834,000,000 for FY2026 and the two prior years. The check searched the downloaded HTML text;
it was not compared visually against the rendered page on sec.gov.

**5. README has a "Part 1" section: PASS**: see README.md → "Part 1: Financial data layer".

**Mutation checks** (`uv run python scripts/mutation_check.py`): **22/22** guarantees, each broken on purpose and caught by a failing test. The final run, after adding Q4, added 6 cases: Q4 arithmetic, the contiguity check, no-EPS, fiscal labels, the 10-K Q4 balance, and fiscal-quarter keying.
```
CAUGHT | idempotent upsert (drop IS DISTINCT FROM guard) | 2 failed
CAUGHT | instant facts unique (drop NULLS NOT DISTINCT) | 1 failed
CAUGHT | disk cache (never read cache) | 2 failed
CAUGHT | rate limit (burst = rate) | 1 failed
CAUGHT | User-Agent header (dropped) | 1 failed
CAUGHT | retry on 429/5xx (none) | 3 failed
CAUGHT | restatement de-dupe (keep oldest) | 1 failed
CAUGHT | candidate fallback order (lowest priority wins) | 1 failed
CAUGHT | client uses the rate limiter (skip acquire) | 1 failed
CAUGHT | redirects rate-limited (let httpx follow them) | 1 failed
CAUGHT | Q instants on quarter ends (accept any instant) | 1 failed
CAUGHT | one FY point per fiscal year (key by exact end date) | 1 failed
CAUGHT | FY duration filter (accept any duration) | 1 failed
CAUGHT | Q duration filter (accept YTD) | 1 failed
CAUGHT | shared filing keeps cik/url (keep nothing) | 1 failed
CAUGHT | older submission pages (never follow) | 1 failed
```
Earlier passes missed 3 mutations, and each test was fixed: the FY and Q filter tests passed because of input
order, and the shared-filing test bypassed the ingest call site.

**part-reviewer findings and what was done**
| # | Finding | Action |
|---|---|---|
| 1 | Restatement lost when the restated value is under a lower-priority tag | Fixed: the latest filing ranks first. New test and mutation. AAPL FY2017-18 now cite the 2019/2020 10-Ks (same values). |
| 2 | Q instants unfiltered; dei cover-page date showed up as a "quarter" | Fixed: Q instants must sit on a fiscal quarter end. dei fallback removed. |
| 3 | No test that the client calls the limiter | Fixed: counting-limiter test and mutation. |
| 4 | Redirect hops skipped the limiter | Fixed: redirects followed manually through the limiter. |
| 5 | First-week-of-January year ends mislabelled | Fixed: `fiscal_year_label`. |
| 6 | No fiscal year end → mid-year instants; same-end tie order-dependent | Fixed: refuse instants; deterministic rank. Also found GS duplicate FY2008 → key FY by year. |
| 7 | Shared-filing owner depends on ingest order | Accepted, documented in ADR 0001 (facts are unaffected). |
| 8 | `rate == 10` allowed | Fixed: must be < 10. |
| 9 | `show` column said "FY" in Q mode | Fixed: "Year". |
| 10 | No global network block in tests | Fixed: autouse guard plus a test for it. |
| 11 | `%` in DATABASE_URL breaks alembic configparser | Fixed: escaped. |

**FY coverage 2021+ (points per company × canonical item)**: 25/25 companies have ≥ 4 years of
revenue, net income, EPS, assets, cash and operating cash flow. The gaps are real absences in the
source data: banks don't report gross profit, operating income or (some) capex; ADI/ADSK/AMD/INTC/ORCL
don't tag `Liabilities`; C/PNC/TXN/WDAY have no FY-end `CommonStockSharesOutstanding`.

## Notes for Maxx
<!-- Anything the user has to do by hand (keys, Docker, accounts). -->
- `.env` holds your `SEC_USER_AGENT` (you created it). It is gitignored.
- The project's Postgres runs on **localhost:5433** (container `footnote-db-1`), because your frontdesk-agent db and a native Postgres already use 5432.
- `data/raw/` (918 MB, gitignored) is the SEC cache. Deleting it means the next ingest re-downloads (~6 min).
- To pull new filings later: `uv run python -m analyst.edgar.ingest --universe data/universe.csv --refresh`.
- After merging: tag `v0.1.0` on main.
