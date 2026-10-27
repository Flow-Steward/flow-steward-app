from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.infrastructure.security.prompt_safety import (
    assert_prompt_payload_safe,
    find_forbidden_template_paths,
    find_prompt_guard_violations,
    redact_diagnostic_identifier_for_persistence,
    redact_structure_for_persistence,
    redact_text_for_persistence,
    sanitize_project_config_for_prompt,
    validate_reference_path,
)


def test_find_forbidden_template_paths_detects_sensitive_project_config_fields():
    template = (
        "Credentials: {{ project_config.credentials }}\n"
        "Connections: {project_config.project_connections}\n"
        "Allowed: {project_config.conventions.tone}\n"
        "Not allowlisted: {project_config.stack.language}\n"
    )
    blocked = find_forbidden_template_paths(template)
    assert "project_config.credentials" in blocked
    assert "project_config.project_connections" in blocked
    assert "project_config.conventions.tone" not in blocked
    assert "project_config.stack.language" in blocked


def test_sanitize_project_config_for_prompt_removes_sensitive_payloads():
    source = {
        "project_name": "demo",
        "project_id": 7,
        "credentials": {"primary_dsn": "postgres://admin:secret@db/app"},
        "project_connections": [{"name": "primary", "dsn": "postgres://secret"}],
        "project_providers": [{"token": "abc"}],
        "watch": {"enabled": True},
        "config": {"raw": True},
        "stack": {"language": "python", "framework": "fastapi"},
        "conventions": {"tone": "concise"},
        "project_rules": "always add tests",
    }
    safe = sanitize_project_config_for_prompt(source)
    assert safe["project_name"] == "demo"
    assert safe["conventions"]["tone"] == "concise"
    assert safe["project_rules"] == "always add tests"
    assert "stack" not in safe
    assert "credentials" not in safe
    assert "project_connections" not in safe
    assert "project_providers" not in safe
    assert "watch" not in safe
    assert "config" not in safe


def test_validate_reference_path_blocks_whole_project_config():
    try:
        validate_reference_path("project_config")
    except ValueError as e:
        assert "forbidden" in str(e).lower()
    else:
        raise AssertionError("expected ValueError for forbidden project_config path")


def test_validate_reference_path_allows_safe_conventions_path():
    validate_reference_path("project_config.conventions.tone")
    validate_reference_path("project_config.project_rules")


def test_validate_reference_path_blocks_non_allowlisted_stack_path():
    with pytest.raises(ValueError, match="prompt-safe allowlist"):
        validate_reference_path("project_config.stack.linter")


def test_validate_reference_path_blocks_bracket_sensitive_path_variants():
    with pytest.raises(ValueError):
        validate_reference_path("project_config['credentials']")
    with pytest.raises(ValueError):
        validate_reference_path('project_config["project_connections"]')


def test_prompt_guard_detects_sensitive_value_leak_from_project_config():
    project_config = {"credentials": {"primary_dsn": "postgres://admin:secret@db/app"}}
    violations = find_prompt_guard_violations(
        "System prompt",
        "Use DSN postgres://admin:secret@db/app",
        project_config=project_config,
    )
    assert any("credentials.primary_dsn" in item for item in violations)
    with pytest.raises(ValueError):
        assert_prompt_payload_safe(
            "System prompt",
            "Use DSN postgres://admin:secret@db/app",
            project_config=project_config,
        )


def test_prompt_guard_ignores_non_sensitive_values_from_non_allowlisted_config_paths():
    project_config = {
        "project_workflows": [
            {"workflow_id": "monthly_review"},
        ],
        "project_labels": [
            {"label_key": "priority_review"},
        ],
    }
    violations = find_prompt_guard_violations(
        "System prompt",
        "Workflow monthly_review is configured with priority_review labels.",
        project_config=project_config,
    )
    assert not violations


def test_prompt_guard_detects_values_from_explicit_forbidden_config_prefixes():
    project_config = {
        "credentials": {
            "oauth_profile": "strict_safety_mode_value",
        }
    }
    violations = find_prompt_guard_violations(
        "System prompt",
        "Use strict_safety_mode_value for runtime handling.",
        project_config=project_config,
    )
    assert any("project_config path 'credentials.oauth_profile'" in item for item in violations)


def test_prompt_guard_allows_safe_project_id_duplicated_in_connection_rows():
    project_id = "5b61fca2-03ab-446e-adbf-0b26a3e847a0"
    project_config = {
        "project_id": project_id,
        "project_connections": [
            {
                "project_id": project_id,
                "dsn": "postgres://admin:secret@db/app",
            }
        ],
    }

    violations = find_prompt_guard_violations(
        "System prompt",
        f"Work only in project {project_id}.",
        project_config=project_config,
    )

    assert violations == []


def test_redact_text_for_persistence_uses_security_scan_output():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="sanitized"),
    ):
        assert redact_text_for_persistence("raw secret value") == "sanitized"


def test_redact_text_for_persistence_withholds_the_text_when_the_scan_fails():
    """It used to fall back to masking URIs and paths only, persisting everything else."""
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        side_effect=RuntimeError("scan failed"),
    ):
        value = redact_text_for_persistence("db=postgres://admin:secret@db/app")
    assert value == "[REDACTED: security scan unavailable]"


def test_redact_structure_for_persistence_redacts_nested_strings():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
    ):
        payload = {"a": "secret", "nested": {"b": "token"}, "items": ["one", "two"]}
        redacted = redact_structure_for_persistence(payload)
    assert redacted["a"] == "[REDACTED]"
    assert redacted["nested"]["b"] == "[REDACTED]"
    assert redacted["items"][0] == "[REDACTED]"


def test_redact_structure_preserves_safe_opaque_diagnostic_ids():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
    ):
        payload = {
            "request_id": "req_abc-123",
            "trace_id": "job:42",
            "correlation_id": "corr.safe-1",
            "message": "secret text",
        }
        redacted = redact_structure_for_persistence(payload)

    assert redacted["request_id"] == "req_abc-123"
    assert redacted["trace_id"] == "job:42"
    assert redacted["correlation_id"] == "corr.safe-1"
    assert redacted["message"] == "[REDACTED]"


def test_redact_structure_preserves_only_canonical_structured_registry_ids():
    from core.infrastructure.security.secret_detection import redact_detected_secrets

    canonical = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.demo_workflow"
    dotted_token = "stripe.abcdefghijklmnopqrstuvwxyz123456"
    prefixed_token = "extension.abcdefghijklmnopqrstuvwxyz123456.demo_workflow"

    def _scan(value, *_args, **_kwargs):
        return SimpleNamespace(action="redact", redacted_text=redact_detected_secrets(value))

    with patch("core.infrastructure.security.integration.scan_outbound", side_effect=_scan):
        redacted = redact_structure_for_persistence(
            {
                "workflow_id": canonical,
                "connector_id": canonical.replace("demo_workflow", "demo_tools"),
                "untrusted_workflow_id": dotted_token,
                "selected_workflow_id": prefixed_token,
                "message": f"provider returned {dotted_token}",
            }
        )

    assert redacted["workflow_id"] == canonical
    assert redacted["connector_id"].endswith(".demo_tools")
    assert "dotted_provider_token" in redacted["untrusted_workflow_id"]
    assert "dotted_provider_token" in redacted["selected_workflow_id"]
    assert dotted_token not in redacted["message"]


def test_secret_detection_preserves_canonical_registry_id_inside_summary():
    from core.infrastructure.security.secret_detection import redact_detected_secrets

    canonical = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.demo_workflow"
    summary = f"Child workflow '{canonical}' completed successfully"
    dotted_token = "stripe.abcdefghijklmnopqrstuvwxyz123456"

    assert redact_detected_secrets(summary) == summary
    assert "dotted_provider_token" in redact_detected_secrets(dotted_token)


def test_secret_detection_preserves_process_env_identifier() -> None:
    from core.infrastructure.security.secret_detection import redact_detected_secrets

    source = "process.env.SEED_INTERMITTENT_FAILURE === '1'"

    assert redact_detected_secrets(source) == source


def test_redact_structure_applies_diagnostic_key_context_to_lists():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
    ):
        redacted = redact_structure_for_persistence(
            {"request_id": ["req_safe-1", "postgres://admin:secret@db/app"]}
        )

    assert redacted["request_id"] == ["req_safe-1", "[REDACTED_DIAGNOSTIC_ID]"]


def test_redact_diagnostic_identifier_redacts_unsafe_values():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
    ):
        redacted = redact_diagnostic_identifier_for_persistence(
            "request_id",
            "postgres://admin:secret@db/app",
        )

    assert redacted == "[REDACTED_DIAGNOSTIC_ID]"


def test_redact_diagnostic_identifier_fully_redacts_email_and_secret_shaped_token():
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[SECRET:github_pat:REDACTED]"),
    ):
        token_redacted = redact_diagnostic_identifier_for_persistence(
            "correlation_id",
            "github_pat_abcdefghijklmnopqrstuvwxyz123456",
        )
        email_redacted = redact_diagnostic_identifier_for_persistence(
            "request_id",
            "john@example.com",
        )

    assert token_redacted == "[REDACTED_DIAGNOSTIC_ID]"
    assert email_redacted == "[REDACTED_DIAGNOSTIC_ID]"
