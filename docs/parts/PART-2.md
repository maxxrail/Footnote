# Part 2 — Filing search with citations (hybrid retrieval)

## Goal
Turn 10-K / 10-Q documents into searchable, section-aware chunks, and return results that
can be cited down to the exact character span in the original filing.

## Why it matters for Rogo
Bankers ask things like "what did management say about pricing pressure?" The answer must
point to the exact paragraph. Retrieval quality and provenance are core to a trusted
finance AI.

## Build
1. **Parser** `analyst/retrieval/parse.py`: HTML → clean text (BeautifulSoup/lxml), drop
   XBRL inline tags and page furniture, keep tables as pipe-delimited text. Detect 10-K
   Items (1, 1A, 7, 7A, 8…) and 10-Q Parts/Items with robust regexes (handle the table of
   contents appearing first — take the **last** heading match that starts a real section).
   Store the clean text once per filing so spans are stable.
2. **Chunker** `analyst/retrieval/chunk.py`: split within sections, ~800 tokens with ~100
   overlap, never crossing a section boundary. Each chunk keeps
   `(accession, section, char_start, char_end, text)`.
3. **Embeddings** `analyst/retrieval/embed.py`: `Embedder` protocol; default local
   `BAAI/bge-small-en-v1.5`; batch, and only embed chunks not yet embedded.
4. **DB**: `filing_texts(accession PK, text)`, `chunks(id, accession, cik, section,
   char_start, char_end, text, embedding vector(384), tsv tsvector)` with HNSW and GIN indexes.
5. **Hybrid search** `analyst/retrieval/search.py`:
   `search(query, cik=None, forms=None, sections=None, since=None, k=8)`
   → vector top-50 + Postgres full-text top-50 → Reciprocal Rank Fusion → optional
   cross-encoder rerank (`BAAI/bge-reranker-base`, behind a flag) → top k.
   Each hit returns a `Citation` model: `accession, form, filed_at, company, section,
   char_start, char_end, snippet, url`.
6. **Citation resolver**: `resolve(citation) -> exact text` reading from `filing_texts`, so
   any citation can be verified later (Part 5 uses this).
7. CLI: `uv run python -m analyst.retrieval.cli "supply chain risk" --ticker NVDA`.

## Tests
- Section detection on 2 recorded filings (fixture HTML) — assert Item 1A and Item 7 found
  with sensible lengths and that TOC entries are not chosen.
- Chunks never cross sections; spans round-trip (`text[start:end] == chunk.text`).
- RRF math on a toy example; search filters (cik, form, section) respected (use a fake embedder).

## Acceptance checks
- [ ] All filings from Part 1 parsed; ≥ 95% of 10-Ks have Item 1A and Item 7 detected
      (print the stat, record it in PROGRESS.md).
- [ ] `pytest`, `ruff` clean.
- [ ] A tiny retrieval eval: write 15 query → expected (ticker, section) pairs in
      `tests/fixtures/retrieval_gold.yaml`; report recall@5 for vector-only, BM25-only, and
      hybrid. Hybrid should be ≥ both. Record the table in PROGRESS.md and README.
- [ ] README "Part 2" section.

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 2 exactly as specified in
> docs/parts/PART-2.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand.
