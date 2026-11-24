# ruff: noqa: E501 (mutations quote source lines verbatim)
"""Mutation checks for Part 3: break each guarantee once, confirm a test fails, restore.

    uv run python scripts/mutation_check_part3.py

Files are restored byte-for-byte even if a test run crashes.
"""

import pathlib
import subprocess
import sys

FIN = "analyst/metrics/finance.py"
COMP = "analyst/metrics/computed.py"
STATS = "analyst/metrics/stats.py"
PRICES = "analyst/metrics/prices.py"
POST = "analyst/agent/postcheck.py"
LOOP = "analyst/agent/loop.py"
TOOLS = "analyst/agent/tools.py"
LLM = "analyst/llm/client.py"
T_M = "tests/test_metrics_finance.py::"
T_P = "tests/test_postcheck.py::"
T_A = "tests/test_agent.py::"

MUTS = [
    # -- metrics --------------------------------------------------------------------------
    (
        "growth refuses a zero base (only negatives refused)",
        FIN,
        "if prior.value <= ZERO:",
        "if prior.value < ZERO:",
        [T_M + "test_growth_refuses_non_positive_base_and_missing"],
    ),
    (
        "CAGR formula (drop the 1/years root)",
        FIN,
        "** (ONE / Decimal(years)) - ONE",
        "- ONE",
        [T_M + "test_cagr_hand_computed"],
    ),
    (
        "LTM needs contiguous quarters (skip the gap check)",
        FIN,
        "if not 0 < gap <= MAX_QUARTER_GAP_DAYS:",
        "if False:",
        [T_M + "test_ltm_refuses_too_few_gaps_and_wrong_span"],
    ),
    (
        "EV = mkt cap + debt - cash (add cash)",
        FIN,
        "value = mkt_cap.value + debt.value - cash.value",
        "value = mkt_cap.value + debt.value + cash.value",
        [T_M + "test_market_cap_ev_and_multiples_hand_computed"],
    ),
    (
        "P/E is None for negative earnings (no guard)",
        FIN,
        "    if denominator.value <= ZERO:\n",
        "    if False:\n",
        [T_M + "test_negative_earnings_pe_is_none"],
    ),
    (
        "quartiles interpolate like QUARTILE.INC (take lower rank)",
        STATS,
        "(sorted_values[lo + 1] - sorted_values[lo]) * frac",
        "(sorted_values[lo + 1] - sorted_values[lo]) * 0",
        [T_M + "test_quantiles_match_excel_quartile_inc"],
    ),
    (
        "peer stats name the excluded peers (silent)",
        STATS,
        'f"excluded (no value): {',
        'f"{',
        [T_M + "test_peer_median_skips_missing_and_says_so"],
    ),
    (
        "Computed lineage lists its facts (empty)",
        COMP,
        "        return tuple(seen)",
        "        return ()",
        [T_M + "test_fact_ids_deduplicated_in_order", T_A + "test_compute_growth_cagr_margins"],
    ),
    (
        "bad price rows raise (skip them)",
        PRICES,
        'raise ValueError(f"{self.path}:{i}: bad price row {r}") from exc',
        "continue",
        [T_M + "test_csv_price_provider_missing_file_and_bad_rows"],
    ),
    # -- number post-check ----------------------------------------------------------------
    (
        "invented numbers are caught (everything matches)",
        POST,
        "def matches(n: Num, values: Iterable[Val]) -> bool:\n",
        "def matches(n: Num, values: Iterable[Val]) -> bool:\n    return True\n",
        [
            T_P + "test_invented_percent_is_caught_in_a_realistic_trace",
            T_P + "test_llm_arithmetic_is_caught",
            T_A + "test_number_still_invented_after_repair_is_flagged",
        ],
    ),
    (
        "rounding tolerance is half a unit (10x looser)",
        POST,
        "unit * scale / 2",
        "unit * scale * 5",
        [T_P + "test_rounding_tolerance_follows_written_precision"],
    ),
    (
        "a ratio is a percentage (no x100)",
        POST,
        'yield Val(v * 100, "pct", True)',
        'yield Val(v, "pct", True)',
        [T_P + "test_percent_written_more_precisely_than_display_uses_the_ratio"],
    ),
    (
        "numbers must be cited (every sentence counts as cited)",
        POST,
        "cited_refs = {int(r) for r in MARKER.findall(sentence)} & known",
        "cited_refs = known",
        [
            T_P + "test_uncited_number_is_flagged",
            T_A + "test_uncited_number_is_flagged_after_repair",
        ],
    ),
    (
        "unknown [^ref] markers are flagged (ignored)",
        POST,
        "result.unknown_refs = sorted(markers - known)",
        "result.unknown_refs = []",
        [T_P + "test_unknown_ref_is_flagged"],
    ),
    (
        "labels (years, forms) are not numbers (no label stripping)",
        POST,
        "    for pat in _LABELS:\n",
        "    for pat in []:\n",
        [T_P + "test_labels_are_not_numbers"],
    ),
    (
        "tool values are typed: ids, dates, accessions don't count (raw digits count)",
        POST,
        '    excerpt = item.get("excerpt")\n',
        "    for raw_digits in __import__('re').findall(r'\\d+', json.dumps(item)):\n"
        "        yield Val(Decimal(raw_digits), 'pct', False)\n"
        "        yield Val(Decimal(raw_digits), 'mult', False)\n"
        '    excerpt = item.get("excerpt")\n',
        [
            T_P + "test_invented_percent_is_caught_in_a_realistic_trace",
            T_A + "test_invented_number_caught_after_get_financials",
        ],
    ),
    (
        "units must agree (any kind matches)",
        POST,
        'if v.kind != n.kind and not (n.bare and v.kind == "mult"):',
        "if False:",
        [T_P + "test_units_must_agree"],
    ),
    (
        "a number must be in the cited ref (any ref will do)",
        POST,
        "if cited_refs and matches(n, cited_vals):",
        "if cited_refs and matches(n, every):",
        [T_P + "test_number_from_another_ref_is_miscited"],
    ),
    (
        "sign must agree (ignored)",
        POST,
        "if n.sign and v.signed and v.value != 0 and (v.value > 0) != (n.sign > 0):",
        "if False:",
        [T_P + "test_sign_must_agree"],
    ),
    (
        "question numbers only as a comparison (always fine)",
        POST,
        "if echoes and not verified_here:",
        "if False:",
        [T_P + "test_question_numbers_only_as_a_comparison"],
    ),
    (
        "small integers skipped only before a count word (always skipped)",
        POST,
        "and bool(_COUNT_WORD.match(stripped, m.end()))",
        "and True",
        [T_P + "test_invented_multiple_and_small_number_are_caught"],
    ),
    (
        "a non-refused answer must cite something (no check)",
        POST,
        "result.no_citations = not refused and not (markers & known)",
        "result.no_citations = False",
        [T_P + "test_answer_must_cite_something_unless_refused"],
    ),
    # -- loop ------------------------------------------------------------------------------
    (
        "exactly one repair turn (repair forever)",
        LOOP,
        "if res.ok or repair_used:",
        "if res.ok:",
        [T_A + "test_number_still_invented_after_repair_is_flagged"],
    ),
    (
        "a flagged answer is never 'high' confidence (keep the model's)",
        LOOP,
        'confidence=sub.confidence if res.ok else "low",',
        "confidence=sub.confidence,",
        [T_A + "test_number_still_invented_after_repair_is_flagged"],
    ),
    (
        "at most 10 steps (15)",
        LOOP,
        "while step < max_steps:",
        "while step < max_steps + 5:",
        [T_A + "test_max_steps"],
    ),
    (
        "the repair turn counts toward the 10 steps (one extra)",
        LOOP,
        "while step < max_steps:",
        "while step < max_steps + (1 if repair_used else 0):",
        [T_A + "test_repair_turn_counts_toward_the_step_limit"],
    ),
    (
        "unexpected tool exceptions go back as errors (crash the run)",
        LOOP,
        "                except Exception as exc:  # a bug or bad data must not crash the run\n",
        "                except ZeroDivisionError as exc:\n",
        [T_A + "test_unexpected_tool_exception_goes_back_as_an_error"],
    ),
    (
        "refs reset per run (keep old ones)",
        LOOP,
        "    toolbox.refs.clear()",
        "    pass",
        [T_A + "test_refs_are_reset_per_run"],
    ),
    (
        "parallel tool results go back in one message (one message each)",
        LOOP,
        'messages.append({"role": "user", "content": results})',
        'messages.extend({"role": "user", "content": [r]} for r in results)',
        [T_A + "test_agent_runs_tools_and_citations_flow_into_the_answer"],
    ),
    (
        "tool errors are reported as errors (as success)",
        LOOP,
        'out, err = f"error: {exc}", True',
        'out, err = f"error: {exc}", False',
        [T_A + "test_tool_errors_go_back_to_the_model"],
    ),
    (
        "citations renumbered in order of appearance (keep tool refs)",
        LOOP,
        "order[r] = len(order) + 1",
        "order[r] = r",
        [T_A + "test_agent_runs_tools_and_citations_flow_into_the_answer"],
    ),
    (
        "model refusal stops the run (ignored)",
        LOOP,
        'if resp.stop_reason == "refusal":',
        "if False:",
        [T_A + "test_model_refusal_stop_reason"],
    ),
    # -- tools -----------------------------------------------------------------------------
    (
        "companies outside the universe aren't matched (any fuzzy match)",
        TOOLS,
        "MATCH_CUTOFF = 0.75",
        "MATCH_CUTOFF = 0.0",
        [T_A + "test_resolve_company_outside_universe_says_refuse"],
    ),
    (
        "tools refuse tickers outside the universe (no check)",
        TOOLS,
        '        if t not in {r.ticker for r in self._universe()}:\n            raise ToolError(f"{t} is not in the covered universe; the answer must say so")\n',
        "",
        [T_A + "test_get_financials_refuses_unknown_items_and_companies"],
    ),
    (
        "derived Q4 cites all four facts (FY only)",
        TOOLS,
        "tuple(_fact_operand(ticker, i) for i in p.inputs),",
        "tuple(_fact_operand(ticker, i) for i in p.inputs[:1]),",
        [T_A + "test_get_financials_fiscal_years_and_quarters_with_derived_q4"],
    ),
    (
        "compute reports a missing price (silently)",
        TOOLS,
        'notes = [] if price else [f"no price for {t} in the price file"]',
        "notes = []",
        [T_A + "test_compute_refuses_negative_pe_and_missing_price"],
    ),
    (
        "EBITDA legs share one LTM window (any two LTMs)",
        TOOLS,
        'return last12.as_operand(f"{d.co.ticker} {item} LTM to {end}"), f"LTM to {end}"',
        'return last12.as_operand(f"{d.co.ticker} {item} LTM to {end}"), "LTM"',
        [T_A + "test_ebitda_never_mixes_ltm_windows"],
    ),
    (
        "LTM is not older than the latest fiscal year (any LTM)",
        TOOLS,
        "(fy_end is None or end >= fy_end)",
        "True",
        [T_A + "test_stale_ltm_loses_to_a_newer_fiscal_year"],
    ),
    (
        "balance-sheet inputs from different dates are noted (silent)",
        TOOLS,
        "    if len({o.period_end for o in used}) > 1:\n",
        "    if False:\n",
        [T_A + "test_balance_sheet_dates_that_differ_are_noted"],
    ),
    # -- cost ------------------------------------------------------------------------------
    (
        "cost is per million tokens (per thousand)",
        LLM,
        "MILLION = Decimal(1_000_000)",
        "MILLION = Decimal(1_000)",
        [T_A + "test_cost_accounting"],
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
        if "no tests ran" in last or "not found" in last or "ERROR" in r.stdout[-400:]:
            raise SystemExit(f"mutation {name!r}: its tests did not run: {last}")
        caught = r.returncode != 0
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'} | {name} | {last}", flush=True)
    finally:
        p.write_bytes(raw)
print(f"\n{len(MUTS) - missed}/{len(MUTS)} caught")
sys.exit(1 if missed else 0)
