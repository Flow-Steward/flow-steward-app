import pytest

from core.application.jobs.public_approval_serializer import (
    build_approval_notification_payload,
    serialize_token_approval_payload,
)


def test_token_approval_serializer_redacts_secret_values_without_losing_safe_text() -> None:
    serialized = serialize_token_approval_payload(
        {
            "approval_id": "gate-1",
            "message": "Approve with Bearer secret-123 after review",
            "content": {
                "summary": "Safe summary",
                "reason": "provider api_key=private-key; failed",
            },
        }
    )

    assert serialized == {
        "approval_id": "gate-1",
        "message": "Approve with Bearer [REDACTED] after review",
        "content": {
            "summary": "Safe summary",
            "reason": "provider api_key=[REDACTED]; failed",
        },
    }
    assert "secret-123" not in str(serialized)
    assert "private-key" not in str(serialized)


@pytest.mark.parametrize(
    "secret",
    [
        bytes((66, 97, 115, 105, 99, 32)).decode() + (chr(65) * 12) + "==",
        "eyJabcdefghijklmnopqrstuv.eyJabcdefghijklmnopqrstuv.abcdefghijklmnopqrstuv",  # pragma: allowlist secret
        "".join(
            (
                "postgresql",
                "://",
                "admin",
                ":",
                "super-secret",
                "@",
                "database.internal",
                "/app",
            )
        ),
        "-----BEGIN PRIVATE KEY-----\nabcdefghijklmnopqrstuvwxyz123456\n-----END PRIVATE KEY-----",  # pragma: allowlist secret
        "glpat-ABCDEFGHIJKLMNOPQRSTuv",  # pragma: allowlist secret
        "SG.ABCDEFGHIJKLMNOPQRSTUV.ABCDEFGHIJKLMNOPQRSTUV",  # pragma: allowlist secret
        "sk-ant-abcdefghijklmnopqrstuvwx",  # pragma: allowlist secret
        "gho_abcdefghijklmnopqrstuv",  # pragma: allowlist secret
        "xoxp-abcdefghijklmnopqrstuv",  # pragma: allowlist secret
    ],
)
def test_token_approval_serializer_uses_canonical_scanner_policy(secret: str) -> None:
    serialized = serialize_token_approval_payload(
        {
            "approval_id": "gate-1",
            "message": f"Approve after checking {secret}",
            "content": {"reason": f"Provider returned {secret}"},
        }
    )

    rendered = str(serialized)
    assert secret not in rendered
    assert "REDACTED" in rendered


@pytest.mark.parametrize(
    "secret",
    [
        bytes((66, 97, 115, 105, 99, 32, 100, 84, 112, 119)).decode(),
        "".join(
            (
                "https",
                "://",
                "admin",
                ":",
                "supersecret",
                "@",
                "example.com/path",
            )
        ),
        "ya" + "29." + ("a" * 32),
    ],
)
def test_token_approval_serializer_redacts_real_world_credential_variants(secret: str) -> None:
    serialized = serialize_token_approval_payload(
        {"approval_id": "gate-1", "message": f"Provider response: {secret}"}
    )

    rendered = str(serialized)
    assert secret not in rendered
    assert "REDACTED" in rendered


def test_token_approval_serializer_redacts_complete_quoted_password() -> None:
    serialized = serialize_token_approval_payload(
        {
            "approval_id": "gate-1",
            "message": 'Provider password="super secret value" failed',  # pragma: allowlist secret
        }
    )

    assert serialized["message"] == 'Provider password="[REDACTED]" failed'


def test_approval_notification_payload_is_occurrence_bound_and_human_readable() -> None:
    payload = build_approval_notification_payload(
        {
            "approval_id": "gate-2",
            "message": "Approve deploy with api_key=private-key",
            "group_id": "release",
            "step_id": "production-approval",
            "content": {"reason": "Production parity review"},
            "checkpoint": {"secrets": ["must-not-leak"]},
        }
    )

    assert payload == {
        "approval_id": "gate-2",
        "message": "Approve deploy with api_key=[REDACTED]",
        "group_id": "release",
        "step_id": "production-approval",
        "content": {"reason": "Production parity review"},
        "summary": "Approve deploy with api_key=[REDACTED]",
        "wait_id": "approval:gate-2",
        "group_label": "Release",
    }
    assert "checkpoint" not in payload
    assert "private-key" not in str(payload)


def test_authored_approval_notification_carries_wait_reason_and_group_label() -> None:
    payload = build_approval_notification_payload(
        {
            "approval_id": "gate-3",
            "gate_kind": "approval",
            "wait_id": "wait-9",
            "group_id": "batch_review",
            "group_label": "Batch review",
            "step_id": "approve",
            "step_execution_id": "exec-1",
            "message": "Approve the batch",
        }
    )

    assert payload["wait_id"] == "wait-9"
    assert payload["reason"] == "authored"
    assert payload["group_label"] == "Batch review"
    assert "phase_label" not in payload


@pytest.mark.parametrize(
    ("gate_kind", "reason"),
    [("recovery", "recovery"), ("autonomy_gate", "autonomy")],
)
def test_recovery_and_autonomy_approvals_get_a_stable_recovery_wait_identity(
    gate_kind: str, reason: str
) -> None:
    payload = build_approval_notification_payload(
        {
            "approval_id": "interrupt-1",
            "gate_kind": gate_kind,
            "request_id": "req-7",
            "step_id": "deploy",
            "message": "Decide",
        }
    )

    assert payload["wait_id"] == "recovery:req-7"
    assert payload["reason"] == reason
    # The root of a workflow has no group label.
    assert "group_label" not in payload


@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        ("client_secret=alpha beta; retry", "client_secret=[REDACTED]; retry"),
        ("refresh_token=alpha beta; retry", "refresh_token=[REDACTED]; retry"),
        ("pwd=alpha beta; retry", "pwd=[REDACTED]; retry"),
        ("password=alpha beta; retry", "password=[REDACTED]; retry"),
        (  # pragma: allowlist secret
            r'password="alpha \"beta\" value"; retry',  # pragma: allowlist secret
            'password="[REDACTED]"; retry',
        ),
    ],
)
def test_token_approval_serializer_redacts_complete_credential_assignments(
    credential: str, expected: str
) -> None:
    serialized = serialize_token_approval_payload(
        {
            "approval_id": "gate-1",
            "message": f"Provider {credential}",
            "content": {"summary": credential},
        }
    )

    assert serialized["message"] == f"Provider {expected}"
    assert serialized["content"] == {"summary": expected}


@pytest.mark.parametrize(
    ("credential", "secret_value"),
    [
        ('"password":"plain-json-secret"', "plain-json-secret"),  # pragma: allowlist secret
        (
            '"client_secret": "oauth-client-secret"',
            "oauth-client-secret",
        ),  # pragma: allowlist secret
        (
            '"refresh_token":"oauth-refresh-token"',
            "oauth-refresh-token",
        ),  # pragma: allowlist secret
    ],
)
def test_token_approval_serializer_redacts_quoted_json_credentials(
    credential: str, secret_value: str
) -> None:
    serialized = serialize_token_approval_payload(
        {
            "approval_id": "gate-1",
            "message": "{" + credential + ',"safe":"visible"}',
        }
    )

    assert secret_value not in serialized["message"]
    assert '"safe":"visible"' in serialized["message"]
    assert '"[REDACTED]"' in serialized["message"]


@pytest.mark.parametrize("gate_kind", ["recovery", "r" * 200])
def test_token_approval_serializer_retains_bounded_recovery_decision_metadata(gate_kind):
    safe = serialize_token_approval_payload(
        {
            "approval_id": "gate-effects",
            "gate_kind": gate_kind,
            "recovery_kind": "external_effect_unknown",
            "approve_label": "Operation completed — continue",
            "reject_label": "Operation not completed — retry",
            "comment_required_on_approve": False,
            "comment_required_on_reject": True,
            "override_content_supported": False,
            "external_effect": {"effect_id": "private-ledger-identity"},
            "source_ref": {"job_id": "private-source"},
            "technical_details": "private-provider-details",
        }
    )

    assert safe == {
        "approval_id": "gate-effects",
        "gate_kind": gate_kind[:128],
        "recovery_kind": "external_effect_unknown",
        "approve_label": "Operation completed — continue",
        "reject_label": "Operation not completed — retry",
        "comment_required_on_approve": False,
        "comment_required_on_reject": True,
        "override_content_supported": False,
    }
