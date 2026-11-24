"""Mutation checks for Part 5: break each eval guarantee once, confirm a test fails, restore.

    uv run python scripts/mutation_check_part5.py

Files are restored byte-for-byte even if a test run crashes. A mutation whose tests don't
run stops the script instead of counting as caught. Run it alone: it edits source files.
"""

import pathlib
import subprocess
import sys

GR = "analyst/evals/graders.py"
RUN = "analyst/evals/run.py"
REPLAY = "analyst/evals/replay.py"
SEED = "analyst/evals/seed.py"
POST = "analyst/agent/postcheck.py"
T_G = "tests/test_graders.py::"
T_R = "tests/test_eval_run.py::"
T_S = "tests/test_eval_seed.py::"
T_P = "tests/test_postcheck.py::"

MUTS = [
    # -- graders ---------------------------------------------------------------------------
    (
        "numeric tolerance is 0.5% (5%)",
        GR,
        'REL_TOL = Decimal("0.005")',
        'REL_TOL = Decimal("0.05")',
        [T_G + "test_numeric_match_within_half_a_percent_even_if_written_precisely"],
    ),
    (
        "numeric sign must agree (ignored)",
        GR,
        "    if n.sign and exp != 0 and (exp > 0) != (n.sign > 0):",
        "    if False:",
        [T_G + "test_numeric_match"],
    ),
    (
        "the headline is the first number of the expected kind (any first number)",
        GR,
        '    if unit == "ratio":\n        return n.kind == "pct"',
        '    if unit == "ratio":\n        return True',
        [T_G + "test_numeric_match"],
    ),
    (
        "a fact citation must match the DB value (any value)",
        GR,
        "        and row.value == Decimal(f.value)\n",
        "",
        [T_G + "test_tampered_fact_does_not_resolve"],
    ),
    (
        "the headline must be in its cited source (not checked)",
        GR,
        "        if head is not None and head.raw in unsupported_numbers:",
        "        if False:",
        [T_G + "test_headline_cited_to_the_wrong_fact_fails"],
    ),
    (
        "the judge's unsupported sentences fail the citation check (ignored)",
        GR,
        "    if unsupported:",
        "    if False:",
        [T_G + "test_filing_span_supports_text_and_numbers"],
    ),
    (
        "refusal is correct only if it matches the item (always correct)",
        GR,
        "    ok = answer.refused == should",
        "    ok = True",
        [T_G + "test_refusal_correct"],
    ),
    # -- runner ----------------------------------------------------------------------------
    (
        "items about companies not loaded are skipped (run anyway)",
        RUN,
        '    return f"not in the loaded corpus: {missing}" if missing else None',
        "    return None",
        [T_R + "test_runner_grades_every_category"],
    ),
    (
        "the budget stops new items (no cap)",
        RUN,
        "        if reason is None and not spend.unknown and spend.total >= cfg.budget:",
        "        if False:",
        [T_R + "test_budget_stops_new_items"],
    ),
    (
        "an account-level API error stops the run (keeps calling)",
        RUN,
        "            if is_fatal(exc):",
        "            if False:",
        [T_R + "test_an_account_level_api_error_stops_the_run"],
    ),
    (
        "the gate fails below a threshold (never)",
        RUN,
        "        if got is None or got < floor:",
        "        if False:",
        [T_R + "test_gate"],
    ),
    (
        "replay raises when the recording runs out (returns nothing)",
        REPLAY,
        '            raise ReplayExhausted(f"{self.item_id}: no more recorded model responses")',
        "            return None",
        [T_R + "test_record_then_replay_reproduces_the_run"],
    ),
    (
        "the no-search ablation removes the tool (kept)",
        "analyst/agent/tools.py",
        'frozenset() if self.search_enabled else frozenset({"search_filings"})',
        "frozenset()",
        [T_R + "test_ablations_change_what_the_agent_gets"],
    ),
    (
        "the no-postcheck ablation skips the number check (still checks)",
        "analyst/agent/loop.py",
        "        if not check_numbers:  # eval ablation: accept the first answer as given",
        "        if False:",
        [T_R + "test_ablations_change_what_the_agent_gets"],
    ),
    # -- seed and splitting ------------------------------------------------------------------
    (
        "the seed is idempotent (re-stores every filing)",
        SEED,
        '            if existing is not None and existing.source_sha256 == f["source_sha256"]:',
        "            if False:",
        [T_S + "test_seed_loads_corpus_offline_and_is_idempotent"],
    ),
    (
        "abbreviations don't end a sentence (split at 'U.S.')",
        POST,
        r'    r"(?<!\bU\.S\.)(?<!\bInc\.)(?<!\bCorp\.)(?<!\bCo\.)(?<!\bNo\.)"',
        '    r""',
        [T_P + "test_abbreviations_do_not_end_a_sentence"],
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
