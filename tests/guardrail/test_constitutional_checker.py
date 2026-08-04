from __future__ import annotations

import asyncio

import pytest

from prodagent.evaluation.reflection.constitutional import (
    ConstitutionalChecker,
    ConstitutionalPrinciple,
)

_NO_PII = ConstitutionalPrinciple(
    name="no_pii",
    critique=lambda text: "@" in text and "." in text.split("@")[-1],
    revision="Remove or anonymise all personally identifiable information.",
)
_NO_UPPERCASE = ConstitutionalPrinciple(
    name="no_uppercase",
    critique=lambda text: text.isupper(),
    revision="Lowercase the output.",
)
_NO_LONG = ConstitutionalPrinciple(
    name="conciseness",
    critique=lambda text: len(text.split()) > 200,
    revision="Trim the response to under 200 words.",
)


def test_no_violations_returns_clean_result() -> None:
    result = asyncio.run(ConstitutionalChecker(principles=[_NO_PII]).check("Hello world."))
    assert result.violations == []
    assert result.revised == "Hello world."


def test_violation_flagged() -> None:
    checker = ConstitutionalChecker(principles=[_NO_PII])
    result = asyncio.run(checker.check("Email me at alice@example.com."))
    assert "no_pii" in result.violations


def test_empty_principles_rejected() -> None:
    with pytest.raises(ValueError, match="At least one principle"):
        ConstitutionalChecker(principles=[])


def test_custom_principle() -> None:
    checker = ConstitutionalChecker(principles=[_NO_UPPERCASE])
    result = asyncio.run(checker.check("SHOUTING"))
    assert "no_uppercase" in result.violations


def test_multiple_principles_apply_in_order() -> None:
    checker = ConstitutionalChecker(principles=[_NO_PII, _NO_UPPERCASE])
    result = asyncio.run(checker.check("Email alice@example.com"))
    assert "no_pii" in result.violations


def test_long_output_flagged() -> None:
    checker = ConstitutionalChecker(principles=[_NO_LONG])
    long_text = " ".join(["word"] * 250)
    result = asyncio.run(checker.check(long_text))
    assert "conciseness" in result.violations
