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
        "(f.filed_at, f.accession) > (cur.filed_at, cur.accession)",
        "(f.filed_at, f.accession) < (cur.filed_at, cur.accession)",
        [T_CON + "test_restatement_keeps_latest_filing"],
    ),
    (
        "candidate fallback order (lowest priority wins)",
        "analyst/edgar/concepts.py",
        "priority[f.concept] < priority[cur.concept]",
        "priority[f.concept] > priority[cur.concept]",
        [T_CON + "test_fallback_prefers_earlier_candidate_for_same_period"],
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
