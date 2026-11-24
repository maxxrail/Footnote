# ruff: noqa: E501 (mutations quote source lines verbatim)
"""Mutation checks for Part 2: break each guarantee once, confirm a test fails, restore.

    uv run python scripts/mutation_check_part2.py

Files are restored byte-for-byte even if a test run crashes.
"""

import pathlib
import subprocess
import sys

PARSE = "analyst/retrieval/parse.py"
CHUNK = "analyst/retrieval/chunk.py"
SEARCH = "analyst/retrieval/search.py"
INDEX = "analyst/retrieval/index.py"
EMBED = "analyst/retrieval/embed.py"
CITE = "analyst/retrieval/citation.py"
T_P = "tests/test_parse.py::"
T_C = "tests/test_chunk.py::"
T_S = "tests/test_search.py::"

MUTS = [
    # -- parser ---------------------------------------------------------------------------
    (
        "TOC lines skipped (keep page-numbered lines)",
        PARSE,
        "if item is None or TOC_LINE_RE.search(ln.text):",
        "if item is None:",
        [
            T_P + "test_toc_lines_with_page_numbers_are_skipped",
            T_P + "test_10k_finds_risk_factors_and_mdna_not_toc",
        ],
    ),
    (
        "last real heading wins (no weight for real sections)",
        PARSE,
        "(0.5 if span(i) >= MIN_REAL_SECTION else 0.0)",
        "0.0",
        [
            T_P + "test_missing_body_heading_does_not_pull_items_into_toc",
            T_P + "test_10k_finds_risk_factors_and_mdna_not_toc",
        ],
    ),
    (
        "ties go to later headings (prefer earlier)",
        PARSE,
        "+ pos * 1e-12)",
        "- pos * 1e-12)",
        [
            T_P + "test_10k_finds_risk_factors_and_mdna_not_toc",
            T_P + "test_toc_lines_with_page_numbers_are_skipped",
        ],
    ),
    (
        "prose mentions are not headings (accept any 'Item N')",
        PARSE,
        "if m and HEADING_REST_RE.match(line[m.end() :]):",
        "if m:",
        [T_P + "test_prose_mentions_are_not_headings"],
    ),
    (
        "title fallback for pointer items (off)",
        PARSE,
        "weak = [k for k in key_titles if length_of(k) < MIN_KEY_SECTION]",
        "weak: list[Key] = []",
        [T_P + "test_title_fallback_for_pointer_items"],
    ),
    (
        "financial statements start at first auditor report (take last)",
        PARSE,
        "real[0] if k[1] in FIRST_TITLE_WINS else real[-1]",
        "real[-1]",
        [T_P + "test_title_fallback_for_pointer_items"],
    ),
    (
        "running headers/footers removed (never)",
        PARSE,
        'if n >= (10 if " | " in key else 3)',
        "if n >= 10**9",
        [
            T_P + "test_running_header_bare_item_lines_removed",
            T_P + "test_clean_text_drops_xbrl_header_and_page_furniture",
        ],
    ),
    (
        "ix:header dropped (keep it)",
        PARSE,
        '"ix:header", ',
        "",
        [T_P + "test_hidden_and_ix_header_dropped_inline_ix_unwrapped"],
    ),
    (
        "display:none dropped (keep it)",
        PARSE,
        'return "display:none" in style',
        "return False",
        [T_P + "test_hidden_and_ix_header_dropped_inline_ix_unwrapped"],
    ),
    (
        "tables as pipe rows (join cells with a space)",
        PARSE,
        'lines.append(" | ".join(merged))',
        'lines.append(" ".join(merged))',
        [T_P + "test_tables_become_pipe_rows"],
    ),
    # -- chunker --------------------------------------------------------------------------
    (
        "chunks never cross sections (run 500 chars past the end)",
        CHUNK,
        "units = list(_units(text, section.char_start, section.char_end, max_tokens))",
        "units = list(_units(text, section.char_start, min(len(text), section.char_end + 500), max_tokens))",
        [T_C + "test_chunks_never_cross_sections"],
    ),
    (
        "spans round-trip (text one char longer)",
        CHUNK,
        "chunks.append(Chunk(section.code, s, e, text[s:e]))",
        "chunks.append(Chunk(section.code, s, e, text[s : e + 1]))",
        [T_C + "test_spans_round_trip"],
    ),
    (
        "chunk size cap (2x)",
        CHUNK,
        "total + units[j].tokens <= max_tokens)",
        "total + units[j].tokens <= 2 * max_tokens)",
        [T_C + "test_size_and_overlap"],
    ),
    (
        "chunks overlap (no overlap)",
        CHUNK,
        "        i = k\n",
        "        i = j\n",
        [T_C + "test_size_and_overlap"],
    ),
    # -- search ---------------------------------------------------------------------------
    (
        "RRF formula (rank from 0)",
        SEARCH,
        "1.0 / (k + rank)",
        "1.0 / (k + rank - 1)",
        [T_S + "test_rrf_toy_example"],
    ),
    (
        "RRF counts a duplicate once (count every time)",
        SEARCH,
        "if doc in seen:",
        "if False:",
        [T_S + "test_rrf_counts_duplicates_once_and_handles_empty"],
    ),
    (
        "cik filter (ignored)",
        SEARCH,
        "out.append(Chunk.cik == cik)",
        "pass",
        [T_S + "test_filters_are_respected"],
    ),
    (
        "form filter (ignored)",
        SEARCH,
        "out.append(Filing.form.in_(list(forms)))",
        "pass",
        [T_S + "test_filters_are_respected"],
    ),
    (
        "section filter (ignored)",
        SEARCH,
        "out.append(Chunk.section.in_(list(sections)))",
        "pass",
        [T_S + "test_filters_are_respected"],
    ),
    (
        "since filter (ignored)",
        SEARCH,
        "out.append(Filing.filed_at >= since)",
        "pass",
        [T_S + "test_filters_are_respected"],
    ),
    (
        "full-text OR-s query terms (AND)",
        SEARCH,
        '" | ".join(f"\'{lex}\'" for lex in lexemes)',
        '" & ".join(f"\'{lex}\'" for lex in lexemes)',
        [T_S + "test_text_leg_matches_any_term"],
    ),
    (
        "full-text IDF weighting (none)",
        SEARCH,
        "idf = math.log(1 + (total - df + 0.5) / (df + 0.5))",
        "idf = 1.0",
        [T_S + "test_text_leg_weights_rare_terms"],
    ),
    (
        "rerank off by default (always on)",
        SEARCH,
        "rerank = get_settings().rerank",
        "rerank = True",
        [T_S + "test_rerank_flag"],
    ),
    # -- index / embed / citations --------------------------------------------------------
    (
        "idempotent index (always reparse)",
        INDEX,
        "and existing.source_sha256 == sha",
        "and False",
        [T_S + "test_index_is_idempotent"],
    ),
    (
        "reparse on changed HTML (never)",
        INDEX,
        "existing is not None\n        and existing.parser_version == PARSER_VERSION\n        and existing.source_sha256 == sha",
        "existing is not None",
        [T_S + "test_changed_html_reparses_that_filing_only"],
    ),
    (
        "only embed pending chunks (re-embed all)",
        EMBED,
        "pending = or_(Chunk.embedding.is_(None), Chunk.embed_model.is_distinct_from(embedder.name))",
        "pending = Chunk.id > 0",
        [T_S + "test_index_is_idempotent"],
    ),
    (
        "resolve returns the exact span (off by one)",
        CITE,
        "func.substr(FilingText.text, char_start + 1, char_end - char_start)",
        "func.substr(FilingText.text, char_start, char_end - char_start)",
        [T_S + "test_citation_resolves_to_exact_chunk_text"],
    ),
    (
        "verify checks the text (always true)",
        CITE,
        "return text.startswith(citation.snippet.removesuffix(ELLIPSIS))",
        "return True",
        [T_S + "test_tampered_citation_fails_verification"],
    ),
    (
        "resolve rejects spans past the end (accept)",
        CITE,
        "if char_end > length:",
        "if False:",
        [T_S + "test_tampered_citation_fails_verification"],
    ),
    # -- test isolation -------------------------------------------------------------------
    (
        "tests never touch the real tables (checkfirst=True)",
        "tests/conftest.py",
        "Base.metadata.create_all(engine, checkfirst=False)",
        "Base.metadata.create_all(engine, checkfirst=True)",
        [T_S + "test_index_is_idempotent"],
    ),
]

missed = 0
for name, path, old, new, tests in MUTS:
    p = pathlib.Path(path)
    raw = p.read_bytes()
    text = raw.decode()
    n = text.replace("\r\n", "\n").count(old)
    assert n == 1, (name, n)
    p.write_bytes(text.replace("\r\n", "\n").replace(old, new).encode())
    try:
        r = subprocess.run(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            capture_output=True,
            text=True,
        )
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
        caught = r.returncode != 0
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'} | {name} | {last}", flush=True)
    finally:
        p.write_bytes(raw)
print(f"\n{len(MUTS) - missed}/{len(MUTS)} caught")
sys.exit(1 if missed else 0)
