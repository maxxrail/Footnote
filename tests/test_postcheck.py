"""The number post-check: numbers must match the cited tool output (unit, rounding, sign).

The tool outputs here look like real ones: they carry ref ids, ISO dates, accession
numbers and CIKs, none of which may count as a verified value.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from analyst.agent.postcheck import check, extract, matches, ref_values

FIN = json.dumps(
    {
        "ticker": "CHIP",
        "results": [
            {
                "metric": "revenue",
                "values": [
                    {
                        "ref": 1,
                        "period": "FY2025",
                        "period_end": "2025-12-31",
                        "value": "1500000000",
                        "display": "$1.50B",
                        "unit": "USD",
                        "source": "us-gaap:Revenues in 10-K 0000000001-26-000001",
                    }
                ],
            }
        ],
    }
)
GROWTH = json.dumps(
    {
        "metric": "growth",
        "results": [
            {
                "ref": 2,
                "ticker": "CHIP",
                "value": "0.25",
                "display": "25.0%",
                "unit": "ratio",
                "formula": "(current - prior) / prior",
                "note": "",
                "inputs": [
                    {"label": "CHIP revenue FY2025", "value": "1500000000", "display": "$1.50B"},
                    {"label": "CHIP revenue FY2024", "value": "1200000000", "display": "$1.20B"},
                ],
            }
        ],
    }
)
MULT = json.dumps(
    {"results": [{"ref": 3, "value": "9.3269230769", "display": "9.3x", "unit": "x"}]}
)
CAGR = json.dumps(
    {"results": [{"ref": 4, "value": "-0.0571", "display": "-5.7%", "unit": "ratio"}]}
)
EXCERPT = json.dumps(
    {
        "results": [
            {
                "ref": 5,
                "filed_at": "2026-02-25",
                "excerpt": "Gross margin was 72.7% compared with 56.9% a year ago. "
                "Operating expenses were up 41%, and a $4.5 billion charge was recorded.",
            }
        ]
    }
)
RESOLVE = json.dumps(
    {"found": True, "matches": [{"ticker": "CHIP", "cik": 31, "peer_group": "semis"}]}
)
OUT = [FIN, GROWTH, MULT, CAGR, EXCERPT, RESOLVE, "error: TSLA is not in the universe (12)"]
REFS = [1, 2, 3, 4, 5]


def ok(text: str, question: str = "q", refused: bool = False) -> bool:
    return check(text, OUT, question, REFS, refused).ok


def nums(text: str) -> list[str]:
    return [n.raw for n in extract(text) if not n.label]


# -- extraction ----------------------------------------------------------------------------


def test_labels_are_not_numbers() -> None:
    text = (
        "In FY2025 (fiscal 2024 was weaker), Q3 and 3Q25, per the 10-K/A Item 7 and Part II, "
        "filed 2026-02-15, accession 0001045810-26-000021, on December 31, 2025 [^12], "
        "Tier 1 capital, Level 3 assets, Note 7, over 3 years and 4 quarters."
    )
    assert nums(text) == []
    assert nums("1. Revenue\n2. Margin") == []  # list numbering


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("$1.5 billion", Decimal("1.5e9"), "amount"),
        ("$1,500 million", Decimal("1.5e9"), "amount"),
        ("$1.50B", Decimal("1.5e9"), "amount"),
        ("$340M", Decimal("3.4e8"), "amount"),
        ("25%", Decimal(25), "pct"),
        ("9.3x", Decimal("9.3"), "mult"),
        ("7 times", Decimal(7), "mult"),
        ("2025 million", Decimal("2.025e9"), "amount"),
    ],
)
def test_extract_scales_and_kinds(text: str, value: Decimal, kind: str) -> None:
    (n,) = extract(text)
    assert (n.value, n.kind) == (value, kind)


def test_extract_signs() -> None:
    assert [n.sign for n in extract("−12.5%, (2.4), grew 3%, fell by 4%, was 5%")] == [
        -1,
        -1,
        1,
        -1,
        0,
    ]


def test_small_integers_are_labels_only_before_a_count_word() -> None:
    assert nums("over 3 years, 2 segments") == []
    assert nums("a P/E of 9, 3 of them") == ["9", "3"]
    assert nums("$3 dividend, 3% and 3.0") == ["$3", "3%", "3.0"]


# -- typed tool values ---------------------------------------------------------------------


def test_ref_values_ignore_ids_dates_accessions_and_errors() -> None:
    by_ref = ref_values(OUT)
    assert set(by_ref) == {1, 2, 3, 4, 5}
    every = [v.value for vals in by_ref.values() for v in vals]
    for junk in (Decimal(2025), Decimal(12), Decimal(31), Decimal(26), Decimal(1), Decimal(2)):
        assert junk not in every


def test_rounding_tolerance_follows_written_precision() -> None:
    vals = ref_values(['{"results": [{"ref": 1, "value": "1523000000", "unit": "USD"}]}'])[1]
    assert matches(extract("$1.5 billion")[0], vals)  # 1.45-1.55B
    assert matches(extract("$1.52 billion")[0], vals)
    assert not matches(extract("$1.6 billion")[0], vals)
    assert not matches(extract("$1.53 billion")[0], vals)
    assert not matches(extract("1.52")[0], vals)  # unscaled doesn't match billions


def test_percent_written_more_precisely_than_display_uses_the_ratio() -> None:
    out = '{"results": [{"ref": 1, "value": "0.150598", "display": "15.1%", "unit": "ratio"}]}'
    assert check("Growth was 15.06% [^1].", [out], "q", [1]).ok
    assert not check("Growth was 15.16% [^1].", [out], "q", [1]).ok


def test_units_must_agree() -> None:
    vals = ref_values([GROWTH])[2]  # 25% and $1.5B / $1.2B inputs
    assert matches(extract("25%")[0], vals)
    assert not matches(extract("$25")[0], vals)
    assert not matches(extract("25x")[0], vals)


# -- whole answers -------------------------------------------------------------------------


def test_clean_answer_passes() -> None:
    text = (
        "Revenue was $1.5 billion in FY2025 [^1], up 25% [^2] from $1.20B [^2]. "
        "It trades at 9.3x EV/EBITDA [^3]. Revenue CAGR was -5.7% [^4]. "
        "Gross margin was 72.7%, from 56.9% [^5]; a $4.5 billion charge hit it [^5]."
    )
    r = check(text, OUT, "question", REFS)
    assert r.ok, r.problems()


@pytest.mark.parametrize("invented", ["31%", "12%", "26%", "23%"])
def test_invented_percent_is_caught_in_a_realistic_trace(invented: str) -> None:
    # dates (2025-12-31), accessions (-26-000001), ref ids and CIKs are in the trace
    r = check(f"Revenue grew {invented} [^2].", OUT, "q", REFS)
    assert r.unverified_numbers == [invented]


def test_invented_multiple_and_small_number_are_caught() -> None:
    assert check("It trades at 31x [^3].", OUT, "q", REFS).unverified_numbers == ["31x"]
    assert check("The P/E is 11 [^3].", OUT, "q", REFS).unverified_numbers == ["11"]
    assert ok("The P/E is about 9 [^3].")  # 9.33x written to the unit


def test_llm_arithmetic_is_caught() -> None:
    # 1.5B - 1.2B = 300M is right, but the model computed it: no tool returned 300M.
    r = check("Revenue rose by $300 million [^2].", OUT, "q", REFS)
    assert r.unverified_numbers == ["$300 million"]


def test_sign_must_agree() -> None:
    assert check("Revenue fell 25% [^2].", OUT, "q", REFS).unverified_numbers == ["25%"]
    assert check("Revenue was -$1.5 billion [^1].", OUT, "q", REFS).unverified_numbers
    assert check("Revenue grew 5.7% a year [^4].", OUT, "q", REFS).unverified_numbers == ["5.7%"]
    assert ok("Revenue declined 5.7% a year [^4].")
    assert ok("Revenue CAGR was 5.7% negative [^4].")  # no sign stated: not contradicted
    assert ok("Opex were up 41% [^5].")  # filing text has no sign to contradict


def test_number_from_another_ref_is_miscited() -> None:
    r = check("Revenue was $1.5 billion [^3].", OUT, "q", REFS)
    assert r.miscited_numbers == ["$1.5 billion"] and not r.ok
    assert "not in the output of the [^ref]" in r.problems()


def test_uncited_number_is_flagged() -> None:
    r = check("Revenue was $1.5 billion. Growth was 25% [^2].", OUT, "q", REFS)
    assert r.uncited_numbers == ["$1.5 billion"]
    assert "without a [^ref]" in r.problems()


def test_marker_in_a_later_clause_does_not_cover_a_number() -> None:
    r = check("Revenue was $1.5 billion; growth was 25% [^2].", OUT, "q", REFS)
    assert r.uncited_numbers == ["$1.5 billion"]


def test_unknown_ref_is_flagged() -> None:
    r = check("Revenue was $1.5 billion [^9].", OUT, "q", REFS)
    assert r.unknown_refs == [9]
    assert "not returned by any tool" in r.problems()


def test_question_numbers_only_as_a_comparison() -> None:
    q = "Was CHIP's revenue $3 billion?"
    assert ok("No, revenue was $1.50B [^1], below $3 billion.", q)
    r = check("Yes, revenue was $3 billion.", OUT, q, REFS)
    assert r.unverified_numbers == ["$3 billion"]


def test_answer_must_cite_something_unless_refused() -> None:
    r = check("CHIP is a chip company.", OUT, "q", REFS)
    assert r.no_citations and "cites nothing" in r.problems()
    assert ok("Not in the filings.", refused=True)


def test_table_rows_are_checked_one_by_one() -> None:
    text = "| FY | Revenue |\n|---|---|\n| FY2025 | $1.50B [^1] |\n| FY2024 | $1.10B [^2] |"
    r = check(text, OUT, "q", REFS)
    assert r.unverified_numbers == ["$1.10B"]
