"""Redaction patterns stay linear on text built to make them backtrack.

Each hostile input below took the previous pattern quadratic time: every
candidate start scanned to the end of the text before failing.
"""

from __future__ import annotations

import time

from core.application.orchestration.input_sanitization import _INJECTION_MARKER_RE
from core.application.security.credential_redaction import _URL_CREDENTIALS, redact_text

_BUDGET_SECONDS = 2.0


def _timed(call) -> tuple[object, float]:
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


def test_url_credentials_are_still_redacted() -> None:
    redacted = redact_text("dsn postgresql+psycopg://flow:s3cr3t@db:5432/app")

    assert "s3cr3t" not in redacted
    assert "postgresql+psycopg://flow:" in redacted


def test_url_credential_pattern_is_linear_on_a_long_word() -> None:
    hostile = "a" * 60_000 + " " + "b" * 60_000

    result, elapsed = _timed(lambda: _URL_CREDENTIALS.sub("", hostile))

    assert result == hostile
    assert elapsed < _BUDGET_SECONDS


def test_injection_marker_is_still_replaced() -> None:
    text = "before [INJECTION:prompt_override:REDACTED] after"

    assert _INJECTION_MARKER_RE.sub("[REDACTED]", text) == "before [REDACTED] after"


def test_injection_marker_pattern_is_linear_on_unclosed_markers() -> None:
    hostile = "[INJECTION:" * 20_000

    result, elapsed = _timed(lambda: _INJECTION_MARKER_RE.sub("[REDACTED]", hostile))

    assert result == hostile
    assert elapsed < _BUDGET_SECONDS
