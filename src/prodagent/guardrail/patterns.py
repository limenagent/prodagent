"""PII, secret, and injection detection patterns."""

from __future__ import annotations

import re

PII_PATTERNS: list[str] = [
    r"1[3-9]\d{9}",  # CN mobile
    r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",  # CN ID card
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",  # email
    r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN
    r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",  # credit card
]

SECRET_PATTERNS: list[str] = [
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI
    r"sk-ant-[a-zA-Z0-9\-_]{20,}",  # Anthropic
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub PAT
    r"ghs_[a-zA-Z0-9]{36}",  # GitHub Actions
    r"AKIA[0-9A-Z]{16}",  # AWS access key ID
    r"(?:password|passwd|secret|api_key|token)\s*[=:]\s*[\"']?\S{8,}",  # credential assignment
]

INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)",
    r"forget\s+(everything|all|your|previous)",
    r"you\s+are\s+now\s+(a\s+)?(?:different|new|another|unrestricted)",
    r"system\s*:\s*(ignore|override|bypass)",
    r"<\s*/?system\s*>",
    r"\[INST\]",
    r"###\s*instruction",
    r"<\|im_start\|>system",
    r"<\|system\|>",
    r"你现在是",
    r"忽略.*以上.*指令",
    r"忘记.*所有.*规则",
]


def compile_patterns(patterns: list[str], *, flags: int = 0) -> list[re.Pattern[str]]:
    return [re.compile(p, flags) for p in patterns]
