"""Mutation checks for Part 1: break each guarantee once, confirm a test fails, restore.

    uv run python scripts/mutation_check.py

Files are restored byte-for-byte even if a test run crashes.
"""

import pathlib
import subprocess

T_ING = "tests/test_ingest.py::"
T_CLI = "tests/test_client.py::"
T_CON = "tests/test_concepts.py::"
MUTS = [
    (
        "idempotent upsert (drop IS DISTINCT FROM guard)",
        "analyst/edgar/ingest.py",
        "where=or_(*[table.c[c].is_distinct_from(stmt.excluded[c]) for c in cols]),",
        "",
        [
            T_ING + "test_ingest_twice_is_idempotent",
            T_ING + "test_upsert_updates_only_changed_rows",
        ],
    ),
    (
        "instant facts unique (drop NULLS NOT DISTINCT)",
        "analyst/db/models.py",
        "postgresql_nulls_not_distinct=True",
        "postgresql_nulls_not_distinct=False",
        [T_ING + "test_instant_facts_are_not_duplicated"],
    ),
    (
        "disk cache (never read cache)",
        "analyst/edgar/client.py",
        "if not refresh and path.exists():",
        "if False:",
        [T_CLI + "test_cache_hit_makes_no_request", T_ING + "test_ingest_twice_is_idempotent"],
    ),
    (
        "rate limit (burst = rate)",
        "analyst/edgar/client.py",
        "capacity if capacity is not None else 1.0",
        "capacity if capacity is not None else rate",
        [T_CLI + "test_token_bucket_never_exceeds_rate"],
    ),
    (
        "User-Agent header (dropped)",
        "analyst/edgar/client.py",
        '"User-Agent": user_agent, ',
        "",
        [T_CLI + "test_user_agent_sent_on_every_request"],
    ),
    (
        "retry on 429/5xx (none)",
        "analyst/edgar/client.py",
        "RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})",
        "RETRY_STATUSES = frozenset()",
        [T_CLI + "test_retries_then_succeeds"],
    ),
    (
        "restatement de-dupe (keep oldest)",
        "analyst/edgar/concepts.py",
        "return (f.filed_at, f.accession, -priority[f.concept], f.fact_id)",
        "return (-priority[f.concept], f.filed_at, f.accession, f.fact_id)",
        [
            T_CON + "test_restatement_keeps_latest_filing",
            T_CON + "test_restatement_under_lower_priority_tag_still_wins",
        ],
    ),
    (
        "candidate fallback order (lowest priority wins)",
        "analyst/edgar/concepts.py",
        "-priority[f.concept], f.fact_id)",
        "priority[f.concept], f.fact_id)",
        [T_CON + "test_fallback_prefers_earlier_candidate_for_same_period"],
    ),
    (
        "client uses the rate limiter (skip acquire)",
        "analyst/edgar/client.py",
        "self.limiter.acquire()",
        "pass",
        [T_CLI + "test_every_network_request_goes_through_the_limiter"],
    ),
    (
        "redirects rate-limited (let httpx follow them)",
        "analyst/edgar/client.py",
        "follow_redirects=False,",
        "follow_redirects=True,",
        [T_CLI + "test_redirect_hops_are_rate_limited_and_keep_user_agent"],
    ),
    (
        "Q instants on quarter ends (accept any instant)",
        "analyst/edgar/concepts.py",
        "return _near_fiscal_quarter_end(f.period_end, fiscal_year_end)",
        "return True",
        [T_CON + "test_instant_q_needs_fiscal_quarter_end"],
    ),
    (
        "one FY point per fiscal year (key by exact end date)",
        "analyst/edgar/concepts.py",
        "fy_key = fiscal_year_label(f.period_end)",
        "fy_key = f.period_end",
        [T_CON + "test_fy_one_point_per_year_when_end_dates_disagree"],
    ),
    (
        "one Q point per fiscal quarter (key by exact end date)",
        "analyst/edgar/concepts.py",
        "else fiscal_period(f.period_end, fiscal_year_end)",
        "else f.period_end",
        [T_CON + "test_q_one_point_per_fiscal_quarter_when_end_dates_disagree"],
    ),
    (
        "Q4 arithmetic (add instead of subtract)",
        "analyst/metrics/quarters.py",
        "return full_year - sum(quarters, Decimal(0))",
        "return full_year + sum(quarters, Decimal(0))",
        ["tests/test_metrics_quarters.py::test_derive_q4_msft_fy2025"],
    ),
    (
        "Q4 needs 3 contiguous quarters (skip gap check)",
        "analyst/metrics/quarters.py",
        "if not 0 < (nxt.period_start - prev.period_end).days <= _MAX_GAP_DAYS:",
        "if False:",
        ["tests/test_metrics_quarters.py::test_refuses_when_quarters_leave_a_gap"],
    ),
    (
        "no Q4 for per-share items (derive EPS too)",
        "analyst/metrics/quarters.py",
        "if concept.instant or not concept.additive or fy.period_start is None:",
        "if concept.instant or fy.period_start is None:",
        ["tests/test_metrics_quarters.py::test_refuses_per_share_items"],
    ),
    (
        "fiscal quarter labels (use calendar quarters)",
        "analyst/edgar/concepts.py",
        "return fiscal_year_label(anchor), min(4, max(1, 4 - quarters_left))",
        "return fiscal_year_label(period_end), (period_end.month - 1) // 3 + 1",
        [T_CON + "test_q_points_carry_fiscal_labels"],
    ),
    (
        "Q4 balance from the 10-K (10-Q only)",
        "analyst/edgar/concepts.py",
        "forms = QUARTERLY_FORMS | ANNUAL_FORMS",
        "forms = QUARTERLY_FORMS",
        [T_CON + "test_q_instants_include_year_end_balance_from_10k"],
    ),
    (
        "FY duration filter (accept any duration)",
        "analyst/edgar/concepts.py",
        "FY_DAYS = (350, 380)",
        "FY_DAYS = (0, 1000)",
        [T_CON + "test_fy_keeps_only_annual_durations_from_10k"],
    ),
    (
        "Q duration filter (accept YTD)",
        "analyst/edgar/concepts.py",
        "Q_DAYS = (80, 100)",
        "Q_DAYS = (80, 200)",
        [T_CON + "test_q_keeps_only_quarterly_durations_from_10q"],
    ),
    (
        "shared filing keeps cik/url (keep nothing)",
        "analyst/edgar/ingest.py",
        'keep=["cik", "url"])',
        "keep=[])",
        [T_ING + "test_filing_listed_under_two_companies_does_not_flip"],
    ),
    (
        "older submission pages (never follow)",
        "analyst/edgar/ingest.py",
        'for page in sub.get("filings", {}).get("files", []):',
        "for page in []:",
        [T_ING + "test_older_submission_pages_fetched_until_documents_covered"],
    ),
]

for name, path, old, new, tests in MUTS:
    p = pathlib.Path(path)
    raw = p.read_bytes()
    text = raw.decode()
    assert text.count(old) == 1, (name, text.count(old))
    p.write_bytes(text.replace(old, new).encode())
    try:
        r = subprocess.run(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            capture_output=True,
            text=True,
        )
        last = r.stdout.strip().splitlines()[-1]
        print(f"{'CAUGHT' if r.returncode else 'MISSED'} | {name} | {last}")
    finally:
        p.write_bytes(raw)
