"""Redaction pass — scrub obvious sensitive strings before any LLM call.

Conservative by design: only the distilled daily summary is sent to the model,
and this strips the patterns most likely to leak *secrets* (emails, card/account
numbers, phone numbers, API keys/tokens). It deliberately does NOT touch normal
words — even when OCR runs them together — or plain dates, so the model keeps the
signal it needs.

Note: this catches secret-SHAPED strings, not semantically sensitive *topics*
(e.g. a private journal's page titles). For those, exclude the app via the
capture blocklist instead. Pure + testable.
"""
from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LABELLED = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S+"
)
_DIGIT_RUN = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _redact_digit_run(m: "re.Match") -> str:
    s = m.group(0)
    return _PLACEHOLDER if _digits(s) >= 9 else s


def _redact_token(m: "re.Match") -> str:
    s = m.group(0)
    has_alpha = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return _PLACEHOLDER if (has_alpha and has_digit) else s


def redact(text: str) -> str:
    if not text:
        return text
    out = _EMAIL.sub(_PLACEHOLDER, text)
    out = _LABELLED.sub(_PLACEHOLDER, out)
    out = _DIGIT_RUN.sub(_redact_digit_run, out)
    out = _TOKEN.sub(_redact_token, out)
    return out
