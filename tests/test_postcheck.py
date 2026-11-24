"""The number post-check: numbers must match a tool output within rounding, and be cited."""

from __future__ import annotations

from decimal import Decimal

import pytest

from analyst.agent.postcheck import check, extract, matches, tool_values

OUT = [
    '{"value": "1500000000", "display": "$1.50B", "ref": 1}',
    '{"value": "0.25", "display": "25.0%", "ref": 2}',
    '{"value": "9.3269230769", "display": "9.3x", "ref": 3}',
    '{"value": "3.6", "display": "$3.60", "ref": 4}',
]
REFS = [1, 2, 3, 4]


def nums(text: str) -> list[str]:
    return [n.raw for n in extract(text) if not n.unitless_small_int]


def test_labels_are_not_numbers() -> None:
    text = (
        "In FY2025 (fiscal 2024 was weaker), Q3 and 3Q25, per the 10-K/A Item 7 and Part II, "
        "filed 2026-02-15 and on December 31, 2025 [^12], over 3 years."
    )
    assert nums(text) == []


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("$1.5 billion", Decimal("1.5e9")),
        ("$1,500 million", Decimal("1.5e9")),
        ("$1.50B", Decimal("1.5e9")),
        ("$340M", Decimal("3.4e8")),
        ("25%", Decimal(25)),
        ("9.3x", Decimal("9.3")),
        ("−12.5%", Decimal("12.5")),
        ("2025 million", Decimal("2.025e9")),  # a year-like value with a unit is a number
    ],
)
def test_extract_scales(text: str, value: Decimal) -> None:
    (n,) = extract(text)
    assert n.value == value


def test_rounding_tolerance_follows_written_precision() -> None:
    values = tool_values(['{"value": "1523000000"}'])
    (ok,) = extract("$1.5 billion")  # 1.45-1.55B
    (bad,) = extract("$1.6 billion")
    (too_precise,) = extract("$1.52 billion")  # 1.515-1.525: still fine
    (wrong_precise,) = extract("$1.53 billion")
    assert matches(ok, values) and matches(too_precise, values)
    assert not matches(bad, values) and not matches(wrong_precise, values)


def test_percent_matches_a_ratio() -> None:
    values = tool_values(['{"value": "0.1505983"}'])
    assert matches(extract("15.1%")[0], values)
    assert matches(extract("15%")[0], values)
    assert not matches(extract("16%")[0], values)


def test_clean_answer_passes() -> None:
    text = (
        "Revenue was $1.5 billion in FY2025 [^1], up 25% [^2]. "
        "It trades at 9.3x EV/EBITDA [^3] with EPS of $3.60 [^4]."
    )
    r = check(text, OUT, "question", REFS)
    assert r.ok, r.problems()


def test_invented_number_is_caught() -> None:
    r = check("Revenue was $1.5 billion [^1], up 31% [^2].", OUT, "q", REFS)
    assert r.unverified_numbers == ["31%"]
    assert not r.ok
    assert "31%" in r.problems()


def test_llm_arithmetic_is_caught() -> None:
    # 1.5B - 1.2B = 300M is right, but the model computed it: no tool returned 300M.
    r = check("Revenue rose by $300 million [^1].", OUT, "q", REFS)
    assert r.unverified_numbers == ["$300 million"]


def test_uncited_number_is_flagged() -> None:
    r = check("Revenue was $1.5 billion. Margin was 25% [^2].", OUT, "q", REFS)
    assert r.uncited_numbers == ["$1.5 billion"]
    assert r.unverified_numbers == []
    assert "without a [^ref]" in r.problems()


def test_unknown_ref_is_flagged() -> None:
    r = check("Revenue was $1.5 billion [^9].", OUT, "q", REFS)
    assert r.unknown_refs == [9]
    assert "not returned by any tool" in r.problems()


def test_numbers_from_the_question_need_no_citation() -> None:
    r = check("No, the margin is not above 40%.", OUT, "Is CHIP's margin above 40%?", REFS)
    assert r.ok


def test_table_rows_are_checked_one_by_one() -> None:
    text = "| FY | Revenue |\n|---|---|\n| FY2025 | $1.50B [^1] |\n| FY2024 | $1.20B |"
    r = check(text, OUT, "q", REFS)
    assert r.unverified_numbers == ["$1.20B"]


def test_small_unitless_integers_are_labels_but_amounts_are_not() -> None:
    assert nums("over 3 years and 4 quarters") == []
    assert nums("$3 dividend, 3% and 3.0") == ["$3", "3%", "3.0"]
    assert nums("13 segments") == ["13"]
