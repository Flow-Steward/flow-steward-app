from __future__ import annotations

import click
import pytest
from pydantic import BaseModel, ValidationError

from core.application.lifecycle.errors import DatabaseSchemaRecoveryRequiredError
from core.application.management.command_specs.contracts import ExitCode, OperationType
from core.application.management.command_specs.registry import get_command_spec
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from core.entrypoints.cli.platform.errors import map_exception
from core.entrypoints.cli.platform.redaction import (
    RedactionContext,
    redact,
    redact_output,
    redact_text,
)


def test_redaction_is_structured_recursive_and_non_mutating() -> None:
    payload = {
        "password": "open-sesame",
        "nested": [{"api_key": "key-123"}],
        "database_url": "postgresql://user:pass@db/app",
        "secret_reference": "vault://team/user-password",
    }

    sanitized = redact(payload)

    assert payload["password"] == "open-sesame"
    assert sanitized["password"] == "[REDACTED]"
    assert sanitized["nested"][0]["api_key"] == "[REDACTED]"
    assert sanitized["database_url"] == "postgresql://user:[REDACTED]@db/app"
    assert sanitized["secret_reference"] == "vault://team/user-password"


def test_redaction_preserves_non_secret_session_counters() -> None:
    payload = {
        "revoked_sessions": 2,
        "active_session_count": 3,
        "must_change_password": False,
    }

    assert redact(payload) == payload
    assert redact_output(payload) == payload


def test_redaction_allows_only_explicit_symbolic_environment_metadata_keys() -> None:
    payload = {
        "bearer_env_var": "MCP_BEARER_TOKEN",
        "token_env_var": "MCP_BEARER_TOKEN",
        "token_env_state": "configured",
        "password_env_var": "REAL_PASSWORD_VALUE",
        "secret_env_state": "secret-state-value",
        "api_key_env_var": "actual-api-key",
    }

    assert redact(payload) == {
        "bearer_env_var": "MCP_BEARER_TOKEN",
        "token_env_var": "MCP_BEARER_TOKEN",
        "token_env_state": "configured",
        "password_env_var": "[REDACTED]",
        "secret_env_state": "[REDACTED]",
        "api_key_env_var": "[REDACTED]",
    }


def test_redact_output_redacts_nested_sensitive_keys_without_known_secret_value() -> None:
    payload = {
        "details": {
            "metadata": {
                "api_key": "unknown-secret-value",
                "secret_reference": "vault://team/api-key",
            }
        }
    }

    sanitized = redact_output(payload)

    assert sanitized["details"]["metadata"]["api_key"] == "[REDACTED]"
    assert sanitized["details"]["metadata"]["secret_reference"] == "vault://team/api-key"
    assert "unknown-secret-value" not in str(sanitized)


def test_redaction_removes_command_secret_values_from_neutral_keys_and_exceptions() -> None:
    context = RedactionContext.from_command_spec(
        get_command_spec(("users", "create")), {"password": "open-sesame"}
    )

    assert redact({"detail": "open-sesame"}, context=context)["detail"] == "[REDACTED]"
    assert redact(RuntimeError("database password=open-sesame"), context=context) == (
        "database password=[REDACTED]"
    )


@pytest.mark.parametrize(
    ("text", "expected", "secret"),
    (
        (
            "Authorization: Bearer bearer-secret",
            "Authorization: Bearer [REDACTED]",
            "bearer-secret",
        ),
        ("Bearer bearer-secret", "Bearer [REDACTED]", "bearer-secret"),
        ("token token-secret", "token [REDACTED]", "token-secret"),
        ("private key: private-secret", "private key: [REDACTED]", "private-secret"),
        ("api key: api-secret", "api key: [REDACTED]", "api-secret"),
        ("Cookie: session=cookie-secret", "Cookie: [REDACTED]", "cookie-secret"),
        ("session=session-secret", "session=[REDACTED]", "session-secret"),
    ),
)
def test_redact_text_removes_free_text_credentials(text: str, expected: str, secret: str) -> None:
    sanitized = redact_text(text)

    assert sanitized == expected
    assert secret not in sanitized


def test_mapped_error_redacts_free_text_credentials_from_cause_and_technical_detail() -> None:
    secrets = (
        "bearer-secret",
        "token-secret",
        "private-secret",
        "api-secret",
        "cookie-secret",
        "session-secret",
    )
    mapped = map_exception(
        RuntimeError(
            "Authorization: Bearer bearer-secret; "
            "token token-secret; private key: private-secret; api key: api-secret; "
            "Cookie: session=cookie-secret; session=session-secret"
        )
    )

    for secret in secrets:
        assert secret not in mapped.cause
        assert secret not in mapped.technical_detail


def test_mapped_validation_error_has_no_secret_in_technical_detail() -> None:
    class _Input(BaseModel):
        count: int

    try:
        _Input.model_validate({"count": "open-sesame"})
    except ValidationError as exc:
        mapped = map_exception(exc, known_secrets=("open-sesame",))
    else:  # pragma: no cover - establishes the test fixture
        raise AssertionError("expected validation failure")

    assert mapped.code == "validation_failed"
    assert "open-sesame" not in mapped.technical_detail


def test_redact_text_redacts_quoted_json_and_python_mapping_values() -> None:
    text = "input_value={'password': 'python-secret'}; payload={\"api_key\": \"json-secret\"}"

    sanitized = redact_text(text)

    assert "python-secret" not in sanitized
    assert "json-secret" not in sanitized
    assert sanitized.count("[REDACTED]") == 2


def test_quoted_mapping_redaction_consumes_escaped_quotes_without_secret_suffix_leak() -> None:
    text = (
        r'payload={"api_key": "super\"secret"}; '
        r"input_value={'password': 'python\'secret'}"
    )

    sanitized = redact_text(text)

    assert "super" not in sanitized
    assert "secret" not in sanitized
    assert "python" not in sanitized
    assert sanitized.count("[REDACTED]") == 2


@pytest.mark.parametrize(
    ("text", "secret"),
    (
        (r'{"api\u005fkey": "json-secret"}', "json-secret"),
        (r"{'pass\x77ord': 'python-secret'}", "python-secret"),
        (r'{"api\u005fkey\"suffix": "quoted-json-secret"}', "quoted-json-secret"),
        (r"{'pass\x77ord\'suffix': 'quoted-python-secret'}", "quoted-python-secret"),
    ),
)
def test_quoted_mapping_redaction_decodes_escaped_secret_keys(
    text: str,
    secret: str,
) -> None:
    sanitized = redact_text(text)

    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


def test_quoted_mapping_redaction_preserves_reference_and_id_fields() -> None:
    text = (
        "{'secret_reference': 'vault://team/key', "
        "'token_id': 'tok_123', 'must_change_password': 'false'}"
    )

    assert redact_text(text) == text


def test_quoted_mapping_redaction_decodes_safe_reference_and_id_keys() -> None:
    text = (
        r'{"secret\u005freference": "vault://team/key", '
        r'"token\u005fid": "tok_123"}'
    )

    assert redact_text(text) == text


def test_internal_error_uses_generic_public_cause_and_redacted_technical_detail() -> None:
    mapped = map_exception(
        RuntimeError(
            "input_value={'password': 'python-secret'}; payload={\"api_key\": \"json-secret\"}"
        ),
        verbose=True,
    )

    assert mapped.code == "internal_error"
    assert mapped.cause == "An unexpected internal error occurred."
    assert "python-secret" not in mapped.technical_detail
    assert "json-secret" not in mapped.technical_detail
    assert "[REDACTED]" in mapped.technical_detail


def test_schema_contract_error_has_safe_actionable_recovery_message() -> None:
    mapped = map_exception(DatabaseSchemaRecoveryRequiredError("raw database driver detail"))

    assert mapped.code == "schema_recovery_required"
    assert mapped.cause == "The database schema is not ready for this Flow Steward release."
    assert "flow-steward database status" in mapped.remediation
    assert "flow-steward database upgrade" in mapped.remediation
    assert "raw database driver detail" not in mapped.cause


@pytest.mark.parametrize(
    ("operation_type", "expected_exit_code", "expected_retryable"),
    (
        (OperationType.READ, ExitCode.STABLE_FAILURE, True),
        (OperationType.CHANGE, ExitCode.RECOVERY_REQUIRED, False),
        (OperationType.DESTRUCTIVE, ExitCode.RECOVERY_REQUIRED, False),
    ),
)
def test_generic_error_mapping_uses_operation_specific_exit_semantics(
    operation_type: OperationType,
    expected_exit_code: ExitCode,
    expected_retryable: bool,
) -> None:
    mapped = map_exception(RuntimeError("broken"), operation_type=operation_type)

    assert mapped.code == "internal_error"
    assert mapped.exit_code is expected_exit_code
    assert mapped.retryable is expected_retryable


@pytest.mark.parametrize(
    ("error", "exit_code", "code"),
    (
        (click.UsageError("invalid"), 2, "syntax_error"),
        (ServiceNotFoundError("missing"), 4, "precondition_failed"),
        (ServiceForbiddenError("denied"), 5, "permission_denied"),
        (ServiceConflictError("exists"), 6, "conflict"),
        (ServiceUnavailableError("offline"), 7, "dependency_unavailable"),
        (KeyboardInterrupt(), 10, "interrupted"),
        (RuntimeError("broken"), 70, "internal_error"),
    ),
)
def test_error_mapping_owns_stable_exit_codes(
    error: BaseException, exit_code: int, code: str
) -> None:
    mapped = map_exception(error)

    assert int(mapped.exit_code) == exit_code
    assert mapped.code == code
    assert mapped.summary
    assert mapped.cause
    assert mapped.remediation
