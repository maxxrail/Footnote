# 0003. Section detection: weighted heading chain plus title fallback

- Status: accepted
- Date: 2026-11-23
- Part: 2

## Context

Citations and the `sections=` search filter need each chunk labelled with its 10-K
item (`Item 1A`, `Item 7`) or 10-Q part/item (`Part II, Item 1A`). The spec says to take
the **last** heading match that starts a real section, because the table of contents
lists every heading first. On the 175 downloaded filings (25 companies × 3 10-K + 4 10-Q)
a plain "last `^Item N` line" rule fails in five distinct ways:

| Pattern | Filers | What goes wrong |
|---|---|---|
| Running page header `Item 1A` on every page | MSFT | "last" match is the top of the last page of the section |
| TOC lists an item the body never repeats (`Item 16`) | ADSK | choosing it forces every earlier item back into the TOC |
| Cross-reference index at the **end** (`Item 7. \| MD&A \| Pages 22-60`) | INTC | "last" match is the index row |
| No `Item` headings at all; body uses bare titles (`RISK FACTORS`) | C, MS | nothing detected |
| `Item 7` is a pointer ("see pages 50–160"), MD&A comes later under a title | JPM, banks' 10-Qs | Item 7 is 396 chars |

## Options

1. **Last `^Item N` line per item.** Simplest; not measured on its own, but it is wrong
   by construction for MSFT (running headers) and INTC (index at the end).
2. **Back-to-front greedy** (the first version built): pick the last real Item 16, then
   the last real Item 15 before it, and so on. 69/75 10-Ks with both Item 1A and Item 7,
   but only 50/75 with both ≥ 2,000 chars. It broke ADSK completely: all 23 items
   landed in the TOC.
3. **Weighted chain + title fallback** (chosen). Ignore lines that end in a page
   reference; reject prose ("Item 1A of our 10-K…"); choose the chain of headings that
   increases in both position and item order with the highest weight (1 per heading,
   +0.5 if ≥ 200 chars to the next heading, ties to later positions); then, for the key
   items only (1A, 7, 7A, 8; 10-Q I-1, I-2, I-3, II-1A) that are missing or < 2,000
   chars, use the last bare-title line that starts a ≥ 2,000-char section.
4. **An LLM or a layout model to label sections.** Costs money and makes the stored
   spans depend on a model version. Rejected for an offline, deterministic parser.

## Decision

Option 3. Measured with `uv run python scripts/section_survey.py`:

| | Item 1A + Item 7 detected | both ≥ 2,000 chars |
|---|---|---|
| Option 2, 10-Ks | 69/75 (92.0%) | 50/75 (66.7%) |
| **Option 3, 10-Ks** | **75/75 (100%)** | **69/75 (92.0%)** |
| Option 3, 10-Qs (Part II 1A + Part I 2) | 87/100 (87.0%) | 61/100 (61.0%) |

The 6 10-Ks that are detected but short are WFC and USB: their 10-K is a wrapper, and
Risk Factors and MD&A are in Exhibit 13, which Part 1 does not download. Most short
10-Q Part II Item 1A sections are genuinely short ("no material changes to the risk
factors in our 10-K").

## Consequences

- Section labels are good enough to filter on for every company in the universe, banks
  included.
- Known mislabels: Citi's "Managing Global Risk" chapter (part of its MD&A) follows its
  Risk Factors and so is labelled `Item 1A`; JPM's 10-Q MD&A runs on into its financial
  statements. Chunk spans are still exact; only the label is coarse.
- A code can appear twice (JPM's pointer `Item 7` and the real MD&A). `ParsedFiling.section`
  returns the longest.
- Revisit if WFC/USB matter: fetch Exhibit 13 (2 extra SEC requests per 10-K) and index
  it as part of the filing. Bump `PARSER_VERSION` whenever these rules change; the
  indexer then rebuilds the stored text and chunks.
