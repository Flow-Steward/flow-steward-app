from __future__ import annotations

import pytest

from core.application.extensions.oauth_post_connect import (
    OAuthPostConnectResult,
    oauth_post_connect_result_from_invocation,
    parse_oauth_post_connect_result,
)


def test_closed_post_connect_result_normalizes_verified_identity_and_metadata() -> None:
    result = parse_oauth_post_connect_result(
        {
            "subject": "  external-subject-123  ",
            "verified_display_name": "  Verified Person  ",
            "verified_email": "  person@example.test  ",
            "safe_metadata": {"account_tier": "standard", "region": "eu"},
            "health": "ok",
        }
    )

    assert result == OAuthPostConnectResult(
        subject="external-subject-123",
        verified_display_name="Verified Person",
        verified_email="person@example.test",
        safe_metadata={"account_tier": "standard", "region": "eu"},
        health="ok",
        error_code=None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"subject": "subject", "health": "ok", "extra": "forbidden"},
        {"subject": "", "health": "ok"},
        {"subject": None, "health": "ok"},
        {"subject": "subject", "health": "unknown"},
        {"subject": "subject", "health": True},
        {"subject": "subject", "health": "ok", "error_code": "provider_unavailable"},
        {"subject": "subject", "health": "ok", "safe_metadata": []},
        {"subject": "subject", "health": "ok", "safe_metadata": {"Bad Key": "value"}},
        {"subject": "subject", "health": "ok", "safe_metadata": {"key": 1}},
        {
            "subject": "subject",
            "health": "ok",
            "safe_metadata": {f"key_{index}": "value" for index in range(17)},
        },
        {"subject": "💥" * 129, "health": "ok"},
        {"subject": "subject", "verified_display_name": "💥" * 129, "health": "ok"},
        {
            "subject": "subject",
            "verified_email": f"{'a' * 310}@example.test",
            "health": "ok",
        },
        {"subject": "subject", "verified_email": "not-an-email", "health": "ok"},
        {
            "subject": "subject",
            "health": "ok",
            "safe_metadata": {"key": "💥" * 257},
        },
        {"subject": "", "health": "needs_attention", "error_code": "Raw Provider Copy"},
        {
            "subject": "",
            "health": "authorization_required",
            "error_code": "provider_unavailable",
        },
    ],
)
def test_post_connect_parser_rejects_open_malformed_or_oversized_results(payload) -> None:
    with pytest.raises(ValueError, match="invalid_oauth_post_connect_result"):
        parse_oauth_post_connect_result(payload)


@pytest.mark.parametrize(
    ("payload", "expected_health", "expected_error"),
    [
        (
            {"subject": "", "health": "needs_attention", "error_code": "provider_unavailable"},
            "needs_attention",
            "provider_unavailable",
        ),
        (
            {
                "subject": "",
                "health": "authorization_required",
                "error_code": "authorization_required",
            },
            "authorization_required",
            "authorization_required",
        ),
    ],
)
def test_post_connect_parser_accepts_only_closed_failure_states(
    payload, expected_health, expected_error
) -> None:
    result = parse_oauth_post_connect_result(payload)
    assert result.health == expected_health
    assert result.error_code == expected_error
    assert result.subject == ""


def test_failed_extension_envelope_preserves_only_closed_provider_failure() -> None:
    result = oauth_post_connect_result_from_invocation(
        {
            "status": "failed",
            "response": {
                "ok": False,
                "result": {},
                "error_code": "provider_unavailable",
                "error": "raw-provider-copy-canary",
            },
            "detail": "raw-runtime-detail-canary",
        }
    )

    assert result == OAuthPostConnectResult(
        subject="",
        verified_display_name=None,
        verified_email=None,
        safe_metadata={},
        health="needs_attention",
        error_code="provider_unavailable",
    )
    assert "canary" not in repr(result)
