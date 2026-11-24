# Progress

**Current part:** 2 (in review; acceptance check 3 met on the held-out set, not on the gold set by one query; Maxx to decide). Next: 3

| Part | Name | Status |
|------|------|--------|
| 1 | Financial data layer (SEC EDGAR + XBRL) | done (v0.1.0) |
| 2 | Filing search with citations (hybrid retrieval) | in review (v0.2.0, PR open): 3 of 4 checks pass, "hybrid ≥ both" met on held-out, not gold (14 vs 15) |
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
- P2: ADR 0003: section detection = weighted heading chain (skips TOC rows and end-of-document cross-reference indexes) + bare-title fallback for key items that are pointers ("see pages 50-160").
- P2: ADR 0004: hybrid retrieval and chunk size (see the recall table below). Chunks stay at 800 tokens: 450 tied (24/25 hybrid over all 25 queries).
- P2: Each chunk carries a contextual header (`chunks.context`: company, ticker, form, filing date, section) that is full-text indexed and embedded, never cited. `PARSER_VERSION = 2` rebuilt every filing.
- P2: A 10-query held-out set (`tests/fixtures/retrieval_holdout.yaml`) was committed before the header change was measured, so it was never tuned against.
- P2: Text before the first heading is labelled `Cover` and indexed too (it has the company description in some 10-Qs). Filter it out with `sections=`.
- P2: Added a `filing_sections` table (not in the spec) so section spans are stored once per filing and the chunker's labels can be checked against them.
- P2: The "BM25" leg is Postgres full-text search, not true BM25: query lexemes OR-ed, each term scored `ts_rank × BM25 IDF` (`log(1+(N-df+0.5)/(df+0.5))`) and summed. Plain `ts_rank` with AND matched almost nothing for natural-language queries.
- P2: The token count is a regex estimate (`\w+|[^\w\s]`); real BGE WordPiece tokens are 1.08× that (p10 1.04, p90 1.13). So an "800-token" chunk is ~860 model tokens, and bge-small truncates at 512, which is why ADR 0004 compares 450-token chunks.
- P2: `sentence-transformers`/torch are in an optional `ml` extra (`uv sync --extra dev --extra ml`). Tests use a deterministic `HashEmbedder` and a fake reranker, so CI doesn't download models.
- P2: Rerank is off by default (`RERANK=false` in settings, `--rerank` on the CLI).
- P2: The vector query orders by distance alone, so Postgres can use the HNSW index (review finding: an `id` tiebreak had forced an exact scan). `hnsw.iterative_scan = strict_order` keeps filtered queries at 50 candidates. With selective filters (one company) Postgres picks an exact scan instead, which is fine at this size.
- P2: Citations carry `text_sha256` of the cited text; `verify()` checks it, so a moved `char_end` fails too (review finding).
- P2: `forms`/`sections` filters are case-insensitive (`10-k`, `item 1a`).
- P2: Tests can't fall back to the default DB engine: an autouse fixture makes `get_engine()` raise, so code under test must be handed the test session.
- P2: The spec mentions BeautifulSoup/lxml; the parser uses lxml only, so `beautifulsoup4` is not a dependency.
- P2: Test DB isolation fix: tests create their tables with `checkfirst=False` in the throwaway schema and assert every table resolves there before truncating (see Notes for Maxx: incident).
- P2: Near-duplicate collapsing (same company + section, Jaccard ≥ 0.9) was tried for `orcl-capex` and reverted: recall identical on all three sets (ADR 0004). A second held-out set (`retrieval_holdout2.yaml`) was committed first; every leg scores 10/10 on it.
- P2: WFC and USB keep Risk Factors and MD&A in Exhibit 13, which isn't downloaded. Their Item 1A/7 are detected but short. Left as-is (ADR 0003 says how to fix).

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

### Part 2 (2026-11-23)

**1. All Part 1 filings parsed; ≥ 95% of 10-Ks have Item 1A and Item 7 detected: PASS (100%)**
```
$ uv run python -m analyst.retrieval.index
# first run: 175/175 filings parsed, 20,456 chunks written (~70 s); embedding was killed at 13,632 (low memory)
# resumed run:
filings: 175 on disk, 0 parsed | chunks written: 0 | chunks embedded: 6824
$ psql -c "select count(embedding), count(*) from chunks"   ->   20456 | 20456
$ uv run python scripts/section_survey.py
10-K: 75 filings | risk factors + MD&A detected: 75/75 (100.0%) | both >= 2000 chars: 69/75 (92.0%) | avg 495,655 chars
10-Q: 100 filings | risk factors + MD&A detected: 87/100 (87.0%) | both >= 2000 chars: 61/100 (61.0%) | avg 273,457 chars
```
The 6 short 10-Ks are WFC × 3 and USB × 3 (Risk Factors ~250 chars, MD&A ~300 chars): their
10-K points to Exhibit 13, which is not downloaded. Most short 10-Q Part II Item 1A sections say
"no material changes" (e.g. TXN, 182 chars). Details and the earlier approach's numbers: ADR 0003.

**2. `pytest`, `ruff` clean: PASS**
```
144 passed in 28.06s
All checks passed!
58 files already formatted
Success: no issues found in 39 source files   (mypy)
No new upgrade operations detected.            (alembic check)
```

**3. Retrieval eval, recall@5: hybrid ≥ both MET on the held-out set (10/10), NOT met on the gold set (14 vs vector 15)**

Final index: 800-token chunks with contextual headers (ADR 0004 update), 20,456 chunks, HNSW.
```
$ uv run python -m analyst.retrieval.evaluate --rerank --misses
15 gold queries, recall@5
| Retriever | open (no filter) | ticker-filtered | s/query (open) |
|---|---|---|---|
| vector-only | 1.00 (15/15) | 1.00 (15/15) | 0.48 |
| full-text-only | 0.93 (14/15) | 0.93 (14/15) | 0.18 |
| hybrid (RRF) | 0.93 (14/15) | 0.93 (14/15) | 0.21 |
| hybrid + rerank | 1.00 (15/15) | 1.00 (15/15) | 7.61 |
full-text-only: open missed ['orcl-capex']; filtered missed ['orcl-capex']
hybrid (RRF): open missed ['orcl-capex']; filtered missed ['orcl-capex']

$ uv run python -m analyst.retrieval.evaluate --rerank --misses --gold tests/fixtures/retrieval_holdout.yaml
10 gold queries, recall@5
| Retriever | open (no filter) | ticker-filtered | s/query (open) |
|---|---|---|---|
| vector-only | 1.00 (10/10) | 1.00 (10/10) | 1.18 |
| full-text-only | 0.80 (8/10) | 0.80 (8/10) | 0.32 |
| hybrid (RRF) | 1.00 (10/10) | 1.00 (10/10) | 0.37 |
| hybrid + rerank | 1.00 (10/10) | 1.00 (10/10) | 11.70 |
full-text-only: open missed ['intu-seasonality', 'adi-industrial']; filtered missed ['intu-seasonality', 'adi-industrial']
```
(Held-out timings are higher because that run overlapped the 450-token embedding job.)

Second held-out set (`retrieval_holdout2.yaml`, 10 queries, committed before measuring any fix), 2026-11-24:
```
| vector-only | 1.00 (10/10) | 1.00 (10/10) | 3.57 |
| full-text-only | 1.00 (10/10) | 1.00 (10/10) | 0.18 |
| hybrid (RRF) | 1.00 (10/10) | 1.00 (10/10) | 0.16 |
| hybrid + rerank | 1.00 (10/10) | 1.00 (10/10) | 8.10 |
```
Across both held-out sets (20 queries): hybrid 20/20, vector 20/20, full-text 18/20. Near-duplicate
collapsing was tried against `orcl-capex` and changed no number on any set, so it was reverted (ADR 0004).

"full-text-only" is the spec's "BM25-only" leg: Postgres full-text with OR-ed terms and BM25 IDF
weights, not true BM25 (ADR 0004). Hybrid's gold miss is `orcl-capex`: the full-text leg ranks the
first gold-section chunk 15th, and Item 1 / Item 8 cash-flow chunks that sit mid-list in both legs win
the fusion (first Item 7 chunk at rank 14). I did not tune RRF, weights or labels to close it.

History: before headers, gold was vector 14/14, full-text 13/14, hybrid 13/14 (open/filtered); the
held-out set (written first) was vector 9/10, full-text 7/8, hybrid 8/8. Headers fixed `txn-300mm`.

Chunk size, 450 vs 800 (450-token index in `analyst_exp`, also with headers), open / filtered:
```
450 gold:     vector 15/15, full-text 14/14, hybrid 15/15, rerank 15/15
450 held-out: vector 10/10, full-text 9/10,  hybrid 9/10 (miss now-subscription, open), rerank 10/10
all 25:  800 hybrid 24/24  |  450 hybrid 24/25   -> tie; kept 800 (the spec's size)
```

**4. README "Part 2" section: PASS**: README.md → "Part 2: Filing search with citations".

Before the review fixes, with an exact scan instead of HNSW, vector-only took 1.54 s/query and hybrid + rerank 9.48 s/query (same recall).

**Mutation checks** (`uv run python scripts/mutation_check_part2.py`): **37/37** caught (30 before the review, 5 added for its findings, 2 for the chunk header).
```
CAUGHT | TOC lines skipped (keep page-numbered lines) | 1 failed, 1 passed
CAUGHT | last real heading wins (no weight for real sections) | 1 failed, 2 passed
CAUGHT | ties go to later headings (prefer earlier) | 1 failed, 3 passed
CAUGHT | prose mentions are not headings (accept any 'Item N') | 1 failed
CAUGHT | title fallback for pointer items (off) | 1 failed
CAUGHT | financial statements start at first auditor report (take last) | 1 failed
CAUGHT | running headers/footers removed (never) | 2 failed
CAUGHT | ix:header dropped (keep it) | 1 failed
CAUGHT | display:none dropped (keep it) | 1 failed
CAUGHT | tables as pipe rows (join cells with a space) | 1 failed
CAUGHT | chunks never cross sections (run 500 chars past the end) | 1 failed
CAUGHT | spans round-trip (text one char longer) | 1 failed
CAUGHT | chunk size cap (2x) | 1 failed
CAUGHT | chunks overlap (no overlap) | 1 failed
CAUGHT | RRF formula (rank from 0) | 1 failed
CAUGHT | RRF counts a duplicate once (count every time) | 1 failed
CAUGHT | cik filter (ignored) | 3 failed
CAUGHT | form filter (ignored) | 3 failed
CAUGHT | section filter (ignored) | 3 failed
CAUGHT | since filter (ignored) | 3 failed
CAUGHT | full-text OR-s query terms (AND) | 1 failed
CAUGHT | full-text IDF weighting (none) | 1 failed, 1 passed
CAUGHT | rerank off by default (always on) | 1 failed
CAUGHT | vector leg can use HNSW (add an id tiebreak to ORDER BY) | 1 failed
CAUGHT | ticker and cik must agree (ignore cik) | 1 failed
CAUGHT | form filter ignores case (exact match) | 1 failed
CAUGHT | chunk header in the full-text index (text only) | 1 failed
CAUGHT | chunk header embedded (text only) | 1 failed
CAUGHT | idempotent index (always reparse) | 1 failed
CAUGHT | reparse on changed HTML (never) | 1 failed
CAUGHT | only embed pending chunks (re-embed all) | 1 failed
CAUGHT | resolve returns the exact span (off by one) | 1 failed
CAUGHT | verify checks the text (always true) | 1 failed
CAUGHT | verify checks the exact span (snippet prefix only) | 1 failed
CAUGHT | resolve rejects spans past the end (accept) | 1 failed
CAUGHT | tests never touch the real tables (checkfirst=True) | 1 error
CAUGHT | tests can't reach the real DB via the default engine (guard off) | 1 failed
```
The first full run missed 3 (TOC filter, tie-to-later, IDF). Each now has a test that targets it:
the end-of-document cross-reference index, equal-weight chains, and 8 "company" chunks vs one rare-term chunk.

**part-reviewer findings and what was done**
| # | Finding | Action |
|---|---|---|
| 1 | Status lines said "done" though check 3 isn't fully met | Fixed: status, README and CHANGELOG say exactly where it's met (now: held-out yes, gold no). Maxx decides whether that's enough to tag. |
| 2 | `verify()` accepted a citation whose `char_end` was moved inside the document | Fixed: `Citation.text_sha256`, checked by `verify()`. New test and mutation. |
| 3 | The `id` tiebreak in ORDER BY stopped Postgres using HNSW (EXPLAIN: seq scan + sort); docs claimed HNSW | Fixed: order by distance only. EXPLAIN now shows `Index Scan using ix_chunks_embedding_hnsw`. Test asserts the plan; mutation added. Eval re-run: same recall. |
| 4 | Code under test could fall back to the default engine (real DB) | Fixed: autouse fixture makes `get_engine()` raise. Test and mutation. |
| 5 | README said 75/75 without the 92% substantive figure | Fixed. |
| 6 | Hyphenated compound lexemes (`data-cent`) re-parsed into a phrase | Checked: the old phrase form matched the same rows (`third-parti`: 2,545 both ways), and `data-cent` really has df 0. Switched to a direct `::tsquery` cast anyway (simpler, exact lexeme). |
| 7 | ticker + conflicting cik → `[]` untested | Fixed: test and mutation. |
| 8 | Filters case-sensitive (`--form 10-k` found nothing) | Fixed: case-insensitive forms and sections. Test and mutation. |
| 9 | Reranker also truncates at 512 tokens; `score` becomes the cross-encoder logit | Documented in `search.py` and ADR 0004. |
| 10 | `beautifulsoup4` unused | Removed. |
| 11 | Stale "tag v0.1.0" note | Removed. |

## Notes for Maxx
<!-- Anything the user has to do by hand (keys, Docker, accounts). -->
- `.env` holds your `SEC_USER_AGENT` (you created it). It is gitignored.
- The project's Postgres runs on **localhost:5433** (container `footnote-db-1`), because your frontdesk-agent db and a native Postgres already use 5432.
- `data/raw/` (918 MB, gitignored) is the SEC cache. Deleting it means the next ingest re-downloads (~6 min).
- To pull new filings later: `uv run python -m analyst.edgar.ingest --universe data/universe.csv --refresh`.
- **P2 incident (fixed):** during Part 2 a test-setup change made `create_all(checkfirst=True)` see the real `public` tables through the search path, so the test fixture's TRUNCATE emptied your dev DB's `companies`, `filings` and `facts`. I rebuilt them from `data/raw/` with 0 SEC requests (`companies=25 filings=230954 facts=823354`, the same as Part 1's rebuild) and deleted only the synthetic test rows afterwards. Tests now always create their own tables and refuse to truncate unless every table resolves to the test schema (mutation-checked).
- Real embeddings need `uv sync --extra dev --extra ml` (CPU torch, ~1 GB). The first run downloads the models to `~/.cache/huggingface`. Full index: ~70 s to parse, ~30 min to embed.
- Embedding runs were killed twice by Claude Code for low memory. Re-running `python -m analyst.retrieval.index` picks up where it stopped. The embedder now loads columns, not ORM objects, so memory stays flat.
- `analyst_exp` is a scratch DB in the same container (450-token chunks with headers, embedded; the ADR 0004 comparison). Keep it for Part 5 or drop it: `docker exec footnote-db-1 dropdb -U analyst analyst_exp`.
- After merging: tag `v0.2.0` on main.
