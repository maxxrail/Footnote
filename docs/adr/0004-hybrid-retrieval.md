# 0004. Hybrid retrieval: vector + Postgres full-text, fused with RRF, rerank optional

- Status: accepted
- Date: 2026-11-23
- Part: 2

## Context

The agent (Part 3) needs filing passages it can cite. The spec asks for vector top-50 +
BM25 top-50 fused with Reciprocal Rank Fusion, an optional cross-encoder, and recall@5 for
each leg on a 15-query gold set (`tests/fixtures/retrieval_gold.yaml`, written before any
results were seen). Constraints: one Postgres (no Elasticsearch), $0 to run, CPU only.

Index: 175 filings (25 companies × 3 10-K + 4 10-Q), 20,456 chunks of ≤ 800 estimated
tokens (avg 3,391 chars), `BAAI/bge-small-en-v1.5` embeddings (384-d, HNSW).

## Options

1. **Lexical leg = plain `ts_rank` with `plainto_tsquery` (AND of all terms).** Tried first:
   a natural-language query of 8-10 words almost never has every term in one chunk, so the
   leg returned few or no candidates.
2. **Lexical leg = OR of the query's lexemes, each scored `ts_rank × BM25 IDF`, summed**
   (chosen). Not true BM25 (no length normalisation beyond `ts_rank`'s, no k1/b), but it
   ranks rare terms ("HBM", "300mm", "VMware") above common ones, which is what BM25 is
   for here. A real BM25 needs an extension (ParadeDB `pg_search`) or a second store.
3. **Fusion = RRF, k=60** (chosen, as specced) vs a weighted score sum: RRF needs no score
   calibration across the two legs.
4. **Cross-encoder rerank (`BAAI/bge-reranker-base`) of the top 30, on by default** vs off.

## Decision

Options 2 + 3, rerank **off** by default (`RERANK=true` or `--rerank` turns it on).
`uv run python -m analyst.retrieval.evaluate --rerank --misses`, recall@5 (first run,
before chunk headers; current numbers in the update at the end):

| Retriever | open (no filter) | ticker-filtered | s/query (open, CPU) |
|---|---|---|---|
| vector-only | 0.93 (14/15) | 0.93 (14/15) | 0.48 |
| full-text-only (OR + IDF) | 0.87 (13/15) | 0.93 (14/15) | 0.13 |
| hybrid (RRF) | 0.87 (13/15) | 0.93 (14/15) | 0.15 |
| hybrid + rerank | **1.00 (15/15)** | **1.00 (15/15)** | 7.76 |

(vector-only's time includes loading the model on the first query; hybrid's does not,
because it runs after. These numbers use the HNSW index. A first run, where an `id`
tiebreak in ORDER BY forced an exact scan, gave identical recall and misses; vector-only
then took 1.54 s/query.)

**Hybrid is not ≥ vector-only on the open set: the spec's target is met only with the
ticker filter.** The one query hybrid loses is `txn-300mm`. Both legs rank a TXN chunk
first, but *different* TXN chunks, while near-identical Micron passages (Properties,
capex, repeated across MU's three 10-Ks and four 10-Qs) sit at ranks 3-8 in **both**
lists. Two middling ranks (≈ 2/65) beat one first place (1/61), so hybrid puts TXN 8th.
This is RRF working as designed on a corpus full of year-over-year near-duplicates
(6.4% of chunks are exact duplicates within a company; many more differ only by shifted
chunk boundaries). Ranks of the first relevant hit (top 20, open):

| Query | vector | full-text | hybrid |
|---|---|---|---|
| txn-300mm | 1 | 1 | 8 |
| qcom-apple-modem | 5 | 9 | 2 |
| mu-hbm | 3 | 1 | 1 |
| intc-foundry-risk | 2 | 1 | 1 |
| amd-datacenter | 1 | 4 | 4 |
| orcl-capex | 11 | 15 | > 20 |
| other 9 | 1 | 1-2 | 1 |

`orcl-capex` is missed by every leg: Oracle's capex discussion is a short paragraph in
MD&A's liquidity section, while ORCL's Item 1 and Item 1A talk about data centers at length.

Why the decision still stands:

- 15 queries is a smoke test: a single query is 0.07 of recall. Hybrid wins `qcom-apple-modem`
  (2 vs 5/9) and fixes the full-text misses. Vector and hybrid differ by one query in each
  direction of the rank table.
- The agent will search with the ticker it is asked about, and there all three are equal.
- I did **not** tune RRF k, leg weights or the gold set after seeing these numbers. That
  would fit 15 queries and prove nothing.

## Consequences

- Rerank is the clear quality win (15/15) but costs ~8 s/query on CPU, so it stays a flag.
  It also reads only the first 512 tokens of each chunk (`max_length=512`), and after a
  rerank `Citation.score` is the cross-encoder logit rather than the RRF score.
  Revisit in Part 5, with the larger eval set: turn it on for the agent if latency allows,
  or rerank a smaller pool.
- Near-duplicate passages across filings crowd results. Candidate fixes for Part 5, to be
  measured on a bigger gold set: collapse hits with the same company + section +
  near-identical text (keep the newest filing), or prefer the latest filing per company.
- Chunk size: tested after the decision, see the update below. Kept at 800.
- The full-text leg is labelled "full-text" everywhere, not "BM25".

## Update 2026-11-24: contextual chunk headers, a held-out set, chunk size

**Held-out set first.** Before changing anything, I wrote 10 more queries
(`tests/fixtures/retrieval_holdout.yaml`, commit `6cf211b`) and committed them before
measuring, so they were never tuned against. Baseline (800 tokens, no headers), open /
filtered: vector 9/10 / 10/10, full-text 7/10 / 8/10, hybrid 8/10 / 8/10, rerank 8/10 / 9/10.

**Change: a contextual header per chunk** (general technique, not tuned to a query).
Chunk text says "we", not "Texas Instruments", so a query that names the company has
nothing to match. Each chunk now stores `context`, e.g. "Texas Instruments Inc (TXN),
10-K filed 2025-02-14. Item 1: Business.". It is part of the full-text `tsv` and of what
the embedder and reranker read (`context + "\n\n" + text`), but never part of a citation:
the cited span is still `text` alone. `PARSER_VERSION = 2` rebuilds every filing.

**Chunk size: 800 vs 450 tokens** (450 + 60 overlap fits bge-small's 512-token window;
800 is ~860 model tokens). Both indexes have headers, `analyst_exp` holds the 450 one
(36,284 chunks, avg 1,893 chars). Recall@5, open / filtered:

| Retriever | 800 gold (15) | 800 held-out (10) | 450 gold (15) | 450 held-out (10) | 800 all 25 | 450 all 25 |
|---|---|---|---|---|---|---|
| vector-only | 15 / 15 | 10 / 10 | 15 / 15 | 10 / 10 | 25 / 25 | 25 / 25 |
| full-text-only | 14 / 14 | 8 / 8 | 14 / 14 | 9 / 10 | 22 / 22 | 23 / 24 |
| hybrid (RRF) | 14 / 14 | 10 / 10 | 15 / 15 | 9 / 10 | **24 / 24** | **24 / 25** |
| hybrid + rerank | 15 / 15 | 10 / 10 | 15 / 15 | 10 / 10 | 25 / 25 | 25 / 25 |

s/query (open, 800): vector 0.48, full-text 0.18, hybrid 0.21, rerank 7.61.

Findings:

- **Headers help every leg.** On the held-out set, hybrid goes from 8/10 to 10/10, and vector
  and rerank to 10/10. On the gold set, vector and full-text gain one each.
- **"Hybrid ≥ both" on 800 tokens: met on the held-out set (10 = 10 > 8), not on the gold
  set (14 vs vector 15, open and filtered).** The miss is `orcl-capex`. The full-text leg
  puts the first gold-section chunk at rank 15, while Item 1 overview and Item 8 cash-flow
  ("Unpaid capital expenditures") chunks appear in both lists mid-way and win the fusion,
  so the first Item 7 chunk lands at rank 14. That's the same RRF effect as `txn-300mm` above, which
  the headers fixed.
- **450 vs 800 is a tie.** Each size fails the hybrid check on a different set by one query
  (800: `orcl-capex` on gold; 450: `now-subscription` on held-out, open only), and the
  25-query totals are the same open (24/25). Picking 450 because it passes the spec's gold set
  would be choosing on the set it's scored on. **Kept 800** (the spec's size): no measured
  gain, and 450 costs 1.8× the chunks and embedding time. Chunk size stays a Part 5 question,
  on a larger set.
- Some held-out labels may be narrow (INTU seasonality is also discussed in Item 8 notes,
  ADI's industrial mix in Item 1). Kept as committed.
