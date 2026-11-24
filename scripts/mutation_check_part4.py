# ruff: noqa: E501 (mutations quote source lines verbatim)
"""Mutation checks for Part 4: break each guarantee once, confirm a test fails, restore.

    uv run python scripts/mutation_check_part4.py

Files are restored byte-for-byte even if a test run crashes. A mutation whose tests don't
run (renamed test, collection error) stops the script instead of counting as caught.
"""

import pathlib
import subprocess
import sys

COMPS = "analyst/workflows/comps.py"
BASE = "analyst/workflows/base.py"
TEAR = "analyst/workflows/tearsheet.py"
EARN = "analyst/workflows/earnings.py"
REPORT = "analyst/workflows/report.py"
TOOLS = "analyst/agent/tools.py"
LOOP = "analyst/agent/loop.py"
T_C = "tests/test_comps.py::"
T_D = "tests/test_workflows_docs.py::"
T_A = "tests/test_agent.py::"

MUTS = [
    # -- comps workbook --------------------------------------------------------------------
    (
        "EV formula = market cap + debt - cash (adds cash)",
        COMPS,
        'C{r}+{debt}-{cash},"NM")',
        'C{r}+{debt}+{cash},"NM")',
        [T_C + "test_formulas_recalculate_to_the_metrics_values"],
    ),
    (
        "NM rule: no multiple on a non-positive denominator (P/E of a loss)",
        COMPS,
        'IF(G{r}>0,C{r}/G{r},"NM")',
        "C{r}/G{r}",
        [T_C + "test_negative_earnings_pe_is_nm"],
    ),
    (
        "NM rule: missing inputs give NM, not an error (no ISNUMBER guard)",
        COMPS,
        '3: f\'=IF(AND({num(p, sh)}),IF(AND({p}>0,{sh}>0),{p}*{sh},"NM"),"NM")\',',
        "3: f'={p}*{sh}',",
        [T_C + "test_nm_rule_for_missing_and_negative_denominators"],
    ),
    (
        "peer stats exclude the target (include it)",
        COMPS,
        "        if not row.is_target:\n            peer_rows.append(r)",
        "        peer_rows.append(r)",
        [T_C + "test_peer_stats_exclude_target_and_nm_peers"],
    ),
    (
        "every input carries its citation comment (none)",
        COMPS,
        'cell.comment = Comment(_comment(op, facts), "analyst-agent", width=420, height=160)',
        "pass",
        [T_C + "test_comps_writes_three_sheets_with_live_formulas"],
    ),
    (
        "inputs are blue (black)",
        COMPS,
        "                cell.font = BLUE\n",
        "                cell.font = Font()\n",
        [T_C + "test_comps_writes_three_sheets_with_live_formulas"],
    ),
    (
        "peer override is used (ignored)",
        COMPS,
        "    if args.peers is not None:",
        "    if False:",
        [T_C + "test_peer_override"],
    ),
    (
        "check values follow the formulas (EBITDA = OI - D&A)",
        "analyst/metrics/finance.py",
        "value = operating_income.value + d_and_a.value",
        "value = operating_income.value - d_and_a.value",
        [T_C + "test_formulas_match_analyst_metrics_for_every_cell"],
    ),
    (
        "only functions Excel knows without an _xlfn. prefix (QUARTILE.INC)",
        COMPS,
        '    ("25th percentile", "QUARTILE({r},1)", "q1"),',
        '    ("25th percentile", "QUARTILE.INC({r},1)", "q1"),',
        [T_C + "test_every_function_is_one_excel_knows"],
    ),
    (
        "duplicate peers count once (counted twice)",
        COMPS,
        "        peers = [p for p in dict.fromkeys(given) if p != args.ticker]  # no duplicates",
        "        peers = [p for p in given if p != args.ticker]",
        [T_C + "test_duplicate_peers_count_once"],
    ),
    (
        "mixed bases are noted on the Comps sheet (silent)",
        COMPS,
        '            if len({rev_basis, ebitda_basis, ni_basis} - {"none"}) > 1:',
        "            if False:",
        [T_C + "test_mixed_bases_are_noted_on_the_comps_sheet"],
    ),
    # -- framework ---------------------------------------------------------------------------
    (
        "the run's final status is stored (stays 'running')",
        BASE,
        "                stored.status = status\n",
        "",
        [T_C + "test_run_is_stored", T_C + "test_failed_run_is_stored_as_failed"],
    ),
    (
        "workflow source material is citable (not in the post-check's outputs)",
        LOOP,
        "        outputs.append(provided)\n",
        "",
        [T_D + "test_earnings_brief"],
    ),
    # -- shares ------------------------------------------------------------------------------
    (
        "a newer cover-page share count is used (always the balance-sheet count)",
        TOOLS,
        "        if bs is not None and bs.period_end is not None and bs.period_end >= cover.period_end:",
        "        if True:",
        [T_A + "test_newer_cover_page_share_count_is_used"],
    ),
    # -- tearsheet ---------------------------------------------------------------------------
    (
        "split warning on mixed per-share bases (never)",
        TEAR,
        "        if ratio >= SPLIT_RATIO or ratio <= 1 / SPLIT_RATIO:",
        "        if False:",
        [T_D + "test_split_warning_flags_mixed_per_share_bases"],
    ),
    (
        "a flagged agent section says so (silent)",
        REPORT,
        "    if answer.unverified_numbers:",
        "    if False:",
        [T_D + "test_tearsheet_flags_an_unverified_number"],
    ),
    (
        "adjacent footnote markers stay separate in the PDF (run together)",
        REPORT,
        "f\"<super>{','.join(MARKER.findall(m.group(0)))}</super>\"",
        "f\"<super>{''.join(MARKER.findall(m.group(0)))}</super>\"",
        [T_D + "test_pdf_markup_merges_adjacent_markers_and_escapes"],
    ),
    # -- earnings brief ----------------------------------------------------------------------
    (
        "compare with the same quarter a year earlier (same year)",
        EARN,
        "k: (_at(pts, fy, fq), _at(pts, fy - 1, fq))",
        "k: (_at(pts, fy, fq), _at(pts, fy, fq))",
        [T_D + "test_earnings_brief"],
    ),
    (
        "margin change = current - prior (reversed)",
        EARN,
        'ch = fin.difference(cur, pri, f"{key}_margin_change")',
        'ch = fin.difference(pri, cur, f"{key}_margin_change")',
        [T_D + "test_earnings_brief"],
    ),
    (
        "near-copies of 10-K paragraphs are not new (everything is new)",
        EARN,
        "        if best < NEW_PARAGRAPH_RATIO:",
        "        if True:",
        [T_D + "test_new_paragraphs_ignores_near_copies"],
    ),
    (
        "'no material changes' means unchanged (counted as a new risk)",
        EARN,
        "        new = [p for p in new if not NO_CHANGE.search(p[0])]",
        "        new = list(new)",
        [T_D + "test_earnings_unchanged_risks_cite_the_10q_without_an_agent"],
    ),
    (
        "a piece of a 10-K paragraph is not new (only whole-paragraph matches)",
        EARN,
        "        if q in exact or q in whole:",
        "        if q in exact:",
        [T_D + "test_a_10q_paragraph_cut_from_a_longer_10k_paragraph_is_not_new"],
    ),
    (
        "the quarter is the 10-Q's own (fall back to the latest series point)",
        EARN,
        "    elif tenq.period_of_report is not None:\n        fy, fq = fiscal_period(tenq.period_of_report, fye)",
        "    elif series['revenue']:\n        fy, fq = series['revenue'][-1].fiscal_year, series['revenue'][-1].fiscal_quarter",
        [T_D + "test_quarter_comes_from_the_10q_period_when_its_revenue_is_not_tagged"],
    ),
    (
        "earnings footnotes cite full XBRL facts (labels only)",
        EARN,
        "        cited = [details[f].describe() for f in o.fact_ids if f in details]",
        "        cited = []",
        [T_D + "test_earnings_footnotes_cite_full_xbrl_facts"],
    ),
    (
        "markers after the full stop belong to that sentence (split off)",
        "analyst/agent/postcheck.py",
        r'_SENTENCE = re.compile(r"(?<=[.!?;])[ \t]+(?!\[\^)|\n+")',
        r'_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")',
        ["tests/test_postcheck.py::test_markers_after_the_full_stop_belong_to_that_sentence"],
    ),
]

missed = 0
for name, path, old, new, tests in MUTS:
    p = pathlib.Path(path)
    raw = p.read_bytes()
    text = raw.decode().replace("\r\n", "\n")
    n = text.count(old)
    assert n == 1, (name, n)
    p.write_bytes(text.replace(old, new).encode())
    try:
        r = subprocess.run(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            capture_output=True,
            text=True,
        )
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
        if "no tests ran" in last or "not found" in last or " error" in last:
            raise SystemExit(f"mutation {name!r}: its tests did not run: {last}")
        caught = r.returncode != 0
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'} | {name} | {last}", flush=True)
    finally:
        p.write_bytes(raw)
print(f"\n{len(MUTS) - missed}/{len(MUTS)} caught")
sys.exit(1 if missed else 0)
