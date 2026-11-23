# 0001. Idempotent ingest: cache-forever SEC responses + change-only upserts

- Status: accepted
- Date: 2026-11-23
- Part: 1

## Context

Part 1 must pull ~25 companies from EDGAR, and a second run must make **0 SEC requests and
change 0 rows**. SEC's fair-access policy caps us at 10 req/s and wants re-runs not to hit it
again. The data is large (one bank's companyfacts is ~40-50k facts) and some of it is
awkward: balance-sheet facts have no `period_start`, and a filing can be listed under two
companies.

## Options

1. **Cache with a TTL**: always fresh-ish, but a re-run inside the TTL window still has to
   decide whether to re-fetch, and the "0 requests" check becomes time-dependent.
2. **Cache forever, explicit `--refresh`**: re-runs are deterministic and free. Getting new
   filings is a deliberate act. Filing documents (immutable) stay cached even on refresh.
3. **Upsert with `ON CONFLICT DO UPDATE` unconditionally**: simple, but every re-run
   rewrites every row (823k dead tuples per run) and "rows changed" can't be measured.
4. **Upsert with `ON CONFLICT DO UPDATE ... WHERE any column IS DISTINCT FROM excluded`** and
   `RETURNING`: only real changes are written, and the count of returned rows is the number
   of rows actually inserted or changed.

## Decision

Options 2 + 4, plus three details found by running it live:

- **`NULLS NOT DISTINCT` on the fact key** (Postgres 15+). Without it, instant facts
  (`period_start IS NULL`) never conflict, and every re-ingest would duplicate the balance sheet.
  A mutation check (`scripts/mutation_check.py`) confirms the test fails without it.
- **A filing listed under two companies keeps its first `cik` and `url`.** The first live
  re-run changed **8 filing rows** with nothing new from SEC: 4 Schedule 13G filings by JPM
  about its ADI stake are in both companies' submissions, so `cik` (and the cik-based archive
  URL) flipped each run. After the fix, the re-run changed **0**.
- **Follow older submissions pages** until 3 10-Ks and 4 10-Qs are known. GS and MS fill
  their 1,000-row "recent" block with 424B2 prospectuses, so the first run found only 4 of 7
  documents for each of 6 banks (157 of 175). After the fix: 175/175, for 78 extra requests.

Numbers from the live run (25 companies):

| Run | SEC requests | Cache hits | Rows changed (companies / filings / facts) | Wall time |
|---|---|---|---|---|
| First (cold cache) | 203 + 78 (pages, docs) | 5 | 25 / 230,954 / 823,354 | 4m50s + ~1m |
| Empty DB, warm cache | 0 | 286 | 25 / 230,954 / 823,354 | ~4 min |
| Second run | **0** | 286 | **0 / 0 / 0** | ~3.5 min |

The rate limiter is a token bucket at 8 req/s with **burst 1**. Its test showed a burst of 8
lets **15 requests** into the first second, which breaks SEC's 10/s limit.

## Consequences

- Data only refreshes with `--refresh`. Part 6 will need a scheduled refresh job.
- A no-op re-run still takes ~3.5 min, because Postgres compares 823k rows. Fine for now;
  if it matters, skip a company whose cached companyfacts file hasn't changed (hash it).
- A shared filing is attributed to whichever company was ingested first. Facts are keyed by
  their own `cik`, so this only affects which company "lists" the filing.
