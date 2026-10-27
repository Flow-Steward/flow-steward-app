"""An action's audit policy must survive real compilation, or protect nothing.

Flow Steward redacts an extension's sensitive result fields from the audit record,
and reduces a failed invocation to codes, according to a policy the *action*
declares. Core applies that policy without knowing the extension.

The policy therefore has to reach the runtime through the real compiler. It did
not: `_normalize_action_manifest` rebuilt each action from a fixed key list, so
`audit` was dropped and every declared protection was silently off. Tests that
hand-build `action_definitions` cannot see that, so these go through a synthetic
bundle on disk and `compile_extension_bundle()`.

Everything here is `acme.*`: Core must not need a real extension to prove this.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.application.extensions.operation_projection import action_audit_policy
from core.infrastructure.extension_runtime.extension_runtime_compile import (
    compile_extension_bundle,
)

VALID_POLICY = {
    "redact_result_fields": ["payload", "note", "sender"],
    "failure_projection": {"keep_row_fields": ["item_id"]},
}


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _synthetic_bundle(
    root: Path,
    *,
    audit: object,
    second_audit: object = None,
    action_connection_policy: dict | None = None,
) -> Path:
    """Write a minimal `acme.*` bundle whose one or two actions declare `audit`."""
    bundle = root / "acme_record_source"
    actions: list[dict] = [
        {
            "action_id": "fetch_record",
            "title": "Fetch Record",
            "description": "Return one record.",
            "required_scopes": ["extension:invoke"],
            "handler": {"mode": "extension"},
            "mutates_platform": False,
            "workflow_visible": True,
            "parameters": [{"name": "connection_ref", "type": "connection_ref", "required": True}],
            "result_fields": ["record"],
            "error_codes": ["invalid_payload", "internal_error"],
        }
    ]
    if audit is not ...:
        actions[0]["audit"] = audit
    actions[0].update(action_connection_policy or {})
    if second_audit is not None:
        actions.append(
            {
                "action_id": "list_records",
                "title": "List Records",
                "description": "Return many records.",
                "required_scopes": ["extension:invoke"],
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "workflow_visible": True,
                "parameters": [],
                "result_fields": ["records"],
                "error_codes": ["internal_error"],
                "audit": second_audit,
            }
        )
    _write_yaml(
        bundle / "extension.yaml",
        {
            "manifest_version": 2,
            "runtime": {"contract_version": "extension_host_v2"},
            "extension_id": "acme.record-source",
            "kind": "tool_provider",
            "version": "1.0.0",
            "display_name": "Acme Record Source",
            "description": "Test-only record source.",
            "vendor": {"name": "Acme Test"},
            "features": ["invoke"],
            "required_scopes": ["extension:invoke"],
            "entrypoint": {"command": ["python3", "main.py"]},
            "health": {"command": ["python3", "health.py"]},
            "ui_manifest": "ui/ui_manifest.yaml",
            "action_manifest": "ui/actions/actions.yaml",
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        },
    )
    _write_yaml(bundle / "ui" / "ui_manifest.yaml", {"version": "1", "pages": []})
    _write_yaml(bundle / "ui" / "actions" / "actions.yaml", {"actions": actions})
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    return bundle


def _compiled_actions(bundle: Path) -> tuple[dict, list[dict]]:
    compiled = compile_extension_bundle(str(bundle))
    return compiled, list(compiled.get("action_definitions") or [])


def test_a_declared_policy_survives_compilation_and_reaches_the_runtime(tmp_path: Path) -> None:
    """The whole path: manifest on disk -> compiler -> the policy Core applies."""
    bundle = _synthetic_bundle(tmp_path, audit=VALID_POLICY)
    compiled, actions = _compiled_actions(bundle)

    assert compiled.get("compile_errors") == []
    assert actions[0]["audit"] == {
        "redact_result_fields": ["payload", "note", "sender"],
        "failure_projection": {"keep_row_fields": ["item_id"]},
    }

    policy = action_audit_policy(compiled, "fetch_record")
    assert policy["redact_result_fields"] == frozenset({"payload", "note", "sender"})
    assert policy["failure_projection"] == frozenset({"item_id"})


def test_an_action_without_a_policy_compiles_and_declares_nothing(tmp_path: Path) -> None:
    """An absent policy is legal — most actions handle nothing sensitive."""
    bundle = _synthetic_bundle(tmp_path, audit=...)
    compiled, actions = _compiled_actions(bundle)

    assert compiled.get("compile_errors") == []
    assert "audit" not in actions[0]
    assert action_audit_policy(compiled, "fetch_record") == {}


def test_action_manifest_does_not_own_operation_connection_policy(tmp_path: Path) -> None:
    bundle = _synthetic_bundle(
        tmp_path,
        audit=...,
        action_connection_policy={
            "connection_type_ids": ["canonical_elsewhere"],
            "connection_type_id": "legacy_connection",
            "connection_type": "another_legacy_connection",
        },
    )
    compiled, actions = _compiled_actions(bundle)

    assert compiled.get("compile_errors") == []
    assert "connection_type_ids" not in actions[0]
    assert "connection_type_id" not in actions[0]
    assert "connection_type" not in actions[0]


def test_each_action_keeps_its_own_policy(tmp_path: Path) -> None:
    bundle = _synthetic_bundle(
        tmp_path,
        audit={"redact_result_fields": ["payload"]},
        second_audit={"failure_projection": {"keep_row_fields": ["item_id"]}},
    )
    compiled, _actions = _compiled_actions(bundle)

    assert compiled.get("compile_errors") == []
    assert action_audit_policy(compiled, "fetch_record") == {
        "redact_result_fields": frozenset({"payload"})
    }
    assert action_audit_policy(compiled, "list_records") == {
        "failure_projection": frozenset({"item_id"})
    }


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("not-a-mapping", id="policy_is_not_a_mapping"),
        pytest.param({}, id="policy_declares_nothing"),
        pytest.param({"redact_result_fields": ["payload"], "extra": 1}, id="unknown_policy_key"),
        pytest.param({"redact_result_fields": "payload"}, id="fields_not_a_list"),
        pytest.param({"redact_result_fields": []}, id="fields_empty"),
        pytest.param({"redact_result_fields": [1]}, id="field_not_a_string"),
        pytest.param({"redact_result_fields": [""]}, id="field_blank"),
        pytest.param({"redact_result_fields": ["a" * 65]}, id="field_too_long"),
        pytest.param({"redact_result_fields": ["not a name"]}, id="field_not_an_identifier"),
        pytest.param({"redact_result_fields": ["payload", "payload"]}, id="field_duplicated"),
        pytest.param({"redact_result_fields": [f"f{n}" for n in range(65)]}, id="too_many_fields"),
        pytest.param({"failure_projection": []}, id="projection_not_a_mapping"),
        pytest.param({"failure_projection": {"nope": []}}, id="unknown_projection_key"),
        pytest.param({"failure_projection": {"keep_row_fields": "uid"}}, id="rows_not_a_list"),
        pytest.param({"failure_projection": {"keep_row_fields": [7]}}, id="row_not_a_string"),
        pytest.param({"failure_projection": {"keep_row_fields": ["a b"]}}, id="row_not_a_name"),
    ],
)
def test_a_malformed_policy_fails_compilation_instead_of_being_dropped(
    tmp_path: Path, malformed: object
) -> None:
    """Dropping it would turn a declared protection off with nothing to notice."""
    bundle = _synthetic_bundle(tmp_path, audit=malformed)
    compiled, _actions = _compiled_actions(bundle)

    errors = compiled.get("compile_errors") or []
    assert errors, "a malformed audit policy must fail compilation"
    codes = {str(issue.get("code") or "") for issue in errors}
    assert "invalid_action_audit_policy" in codes, errors
    fields = {str(issue.get("field_path") or "") for issue in errors}
    assert any(path.startswith("actions[0].audit") for path in fields), errors


def test_a_malformed_policy_is_rejected_by_the_public_validate_command(tmp_path: Path) -> None:
    """`extensions validate` is where an author finds out, before anything installs."""
    from core.application.tooling.extensions.validate_extensions import (
        ExtensionValidationRequest,
        validate_extensions,
    )

    _synthetic_bundle(tmp_path, audit={"failure_projection": {"keep_row_fields": ["a b"]}})
    result = validate_extensions(
        ExtensionValidationRequest(extension_id="acme.record-source", root=str(tmp_path))
    )

    assert result.exit_code != 0
    assert result.status == "failed"
    assert "manifest invalid" in result.output
    # The author is told which action and which key, not just that something is wrong.
    assert "actions[0].audit.failure_projection.keep_row_fields" in result.output


def test_sensitive_values_are_absent_from_the_persisted_audit_after_a_compiled_run(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end on the real path: manifest -> compiler -> execute -> what was logged.

    The earlier tests for this behaviour handed `action_definitions` to the service
    directly, so they kept passing while the compiler dropped the policy and the
    audit record carried everything. This one runs the action through the compiled
    descriptor and reads the row that was persisted.
    """
    from unittest.mock import MagicMock, patch

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.extensions.binding_service import ExtensionBindingService
    from core.application.management.requests import ExtensionActionRequest
    from core.application.management.scope import ScopeContext

    audit_policy = {
        **VALID_POLICY,
        "redact_result_fields": [*VALID_POLICY["redact_result_fields"], "item_id"],
    }
    bundle = _synthetic_bundle(tmp_path, audit=audit_policy)
    compiled = compile_extension_bundle(str(bundle))
    assert compiled.get("compile_errors") == []
    # Only runtime identity/state is attached; the audit policy remains exactly
    # what the compiler emitted from the synthetic bundle.
    compiled["enabled"] = True
    compiled["extension_install_id"] = "install-acme-record-source"

    bl = MagicMock()
    bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_project_connection.return_value = {
        "connection_id": "conn_acme",
        "project_id": "7",
        "account_id": DEFAULT_ACCOUNT_ID,
        "extension_id": "acme.record-source",
        "extension_install_id": "install-acme-record-source",
        "enabled": True,
        "connection_type": "acme_record_source",
        "connection_config": {"acme_host": "records.example.test"},
        "secret_refs": {"refs": {"password": "acme-password-ref"}},  # pragma: allowlist secret
    }
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *args, **kwargs: {  # pragma: allowlist secret
            "password": "HYDRATED_ACME_SECRET_SENTINEL"
        },
    )
    service = ExtensionBindingService(
        get_logger=lambda: bl,
        get_operation_store=lambda: bl,
        resolve_extension_runtime_record=lambda extension_id: (compiled, bl),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *args, **kwargs: "extension",
        extension_operation_failure_status=lambda invocation: 502,
    )
    body = ExtensionActionRequest(
        context={"project_id": "7"},
        target={"connection_id": "conn_acme"},
        input={
            "connection_ref": "conn_acme",
            "note": "AUDIT_REQUEST_NOTE_SENTINEL",
            "sender": "audit-request-sender@example.test",
        },
    )
    invocation = {
        "status": "succeeded",
        "response": {
            "result": {
                "record": {
                    "item_id": 41,
                    "note": "AUDIT_RESPONSE_NOTE_SENTINEL",
                    "sender": "audit-response-sender@example.test",
                    "payload": "AUDIT_RESPONSE_PAYLOAD_SENTINEL",
                }
            }
        },
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value=invocation,
    ):
        result = service.execute_action(
            "acme.record-source",
            "fetch_record",
            body,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
        ).dump()

    # The caller still receives the payload; only the audit record is reduced.
    assert (
        result["action"]["response"]["result"]["record"]["note"] == "AUDIT_RESPONSE_NOTE_SENTINEL"
    )

    logged = bl.log_extension_invocation.call_args.kwargs
    assert logged["response"]["response"]["result"]["record"]["item_id"] == "<redacted>"
    persisted = json.dumps(logged, default=str, sort_keys=True)
    for sensitive_value in (
        "AUDIT_REQUEST_NOTE_SENTINEL",
        "audit-request-sender@example.test",
        "AUDIT_RESPONSE_NOTE_SENTINEL",
        "audit-response-sender@example.test",
        "AUDIT_RESPONSE_PAYLOAD_SENTINEL",
        "HYDRATED_ACME_SECRET_SENTINEL",
    ):
        assert sensitive_value not in persisted


def test_the_same_run_without_the_policy_keeps_host_safe_identifiers(
    tmp_path: Path, monkeypatch
) -> None:
    """The control proves declared policy redacts beyond the host-safe projection."""
    from unittest.mock import MagicMock, patch

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.extensions.binding_service import ExtensionBindingService
    from core.application.management.requests import ExtensionActionRequest
    from core.application.management.scope import ScopeContext

    bundle = _synthetic_bundle(tmp_path, audit=...)
    compiled = compile_extension_bundle(str(bundle))
    compiled["enabled"] = True
    compiled["extension_install_id"] = "install-acme-record-source"

    bl = MagicMock()
    bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_project_connection.return_value = {
        "connection_id": "conn_acme",
        "project_id": "7",
        "account_id": DEFAULT_ACCOUNT_ID,
        "extension_id": "acme.record-source",
        "extension_install_id": "install-acme-record-source",
        "enabled": True,
        "connection_type": "acme_record_source",
        "connection_config": {},
        "secret_refs": {"refs": {}},
    }
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *args, **kwargs: {},
    )
    service = ExtensionBindingService(
        get_logger=lambda: bl,
        get_operation_store=lambda: bl,
        resolve_extension_runtime_record=lambda extension_id: (compiled, bl),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *args, **kwargs: "extension",
        extension_operation_failure_status=lambda invocation: 502,
    )
    body = ExtensionActionRequest(
        context={"project_id": "7"},
        target={"connection_id": "conn_acme"},
        input={"connection_ref": "conn_acme"},
    )
    invocation = {
        "status": "succeeded",
        "response": {
            "result": {
                "record": {
                    "item_id": 41,
                    "note": "AUDIT_RESPONSE_NOTE_SENTINEL",
                }
            }
        },
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value=invocation,
    ):
        service.execute_action(
            "acme.record-source",
            "fetch_record",
            body,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
        )

    logged = bl.log_extension_invocation.call_args.kwargs
    assert logged["response"]["response"]["result"]["record"] == {"item_id": 41}


def test_a_policy_survives_compilation_for_every_action_that_declares_one(tmp_path: Path) -> None:
    """Two actions, two different policies, both intact after compiling.

    This used to walk the real `extensions/` tree and compare each shipped
    declaration with its compiled form. That made the platform's own suite depend
    on bundles being present and on what they declare; a bundle's policy is its
    own to pin, in its own suite, and `extensions validate` is what enforces it.
    """
    bundle = _synthetic_bundle(
        tmp_path,
        audit={"redact_result_fields": ["payload", "note"]},
        second_audit={"failure_projection": {"keep_row_fields": ["item_id"]}},
    )
    compiled, actions = _compiled_actions(bundle)

    assert compiled.get("compile_errors") == []
    by_action = {row["action_id"]: row.get("audit") for row in actions}
    assert by_action["fetch_record"] == {"redact_result_fields": ["payload", "note"]}
    assert by_action["list_records"] == {"failure_projection": {"keep_row_fields": ["item_id"]}}


@pytest.mark.parametrize(
    "damaged",
    [
        pytest.param({}, id="empty_policy"),
        pytest.param("not-a-mapping", id="not_a_mapping"),
        pytest.param({"unknown_key": 1}, id="unknown_key"),
        pytest.param({"redact_result_fields": []}, id="empty_field_list"),
        pytest.param({"redact_result_fields": "payload"}, id="fields_not_a_list"),
        pytest.param({"failure_projection": []}, id="projection_not_a_mapping"),
        pytest.param({"failure_projection": {"nope": []}}, id="unknown_projection_key"),
        pytest.param({"failure_projection": {"keep_row_fields": "item_id"}}, id="rows_not_a_list"),
    ],
)
def test_the_runtime_fails_closed_on_damaged_policy_metadata(damaged: object) -> None:
    """Compilation rejects these, so reaching the runtime means the record was damaged.

    The one outcome that must not follow is "the action declared a protection and
    none was applied". A policy Core cannot read is treated as the strictest one.
    """
    from core.application.extensions.operation_projection import (
        _AUDIT_FAIL_CLOSED_KEY,
        action_audit_policy,
    )

    extension = {"action_definitions": [{"action_id": "fetch_record", "audit": damaged}]}
    assert action_audit_policy(extension, "fetch_record") == {_AUDIT_FAIL_CLOSED_KEY: True}


def test_a_damaged_policy_keeps_the_payload_out_of_the_audit_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Fail-closed is not a flag; it has to change what is persisted."""
    from unittest.mock import MagicMock, patch

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.extensions.binding_service import ExtensionBindingService
    from core.application.management.requests import ExtensionActionRequest
    from core.application.management.scope import ScopeContext

    bundle = _synthetic_bundle(tmp_path, audit=VALID_POLICY)
    compiled = compile_extension_bundle(str(bundle))
    compiled["enabled"] = True
    compiled["extension_install_id"] = "install-acme-record-source"
    # Damage the stored descriptor the way only a corrupted record could.
    for action in compiled["action_definitions"]:
        action["audit"] = {"redact_result_fields": "payload"}

    bl = MagicMock()
    bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_project_connection.return_value = {
        "connection_id": "conn_acme",
        "project_id": "7",
        "account_id": DEFAULT_ACCOUNT_ID,
        "extension_id": "acme.record-source",
        "extension_install_id": "install-acme-record-source",
        "enabled": True,
        "connection_type": "acme_record_source",
        "connection_config": {},
        "secret_refs": {"refs": {}},
    }
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *args, **kwargs: {},
    )
    service = ExtensionBindingService(
        get_logger=lambda: bl,
        get_operation_store=lambda: bl,
        resolve_extension_runtime_record=lambda extension_id: (compiled, bl),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *args, **kwargs: "extension",
        extension_operation_failure_status=lambda invocation: 502,
    )
    body = ExtensionActionRequest(
        context={"project_id": "7"},
        target={"connection_id": "conn_acme"},
        input={"connection_ref": "conn_acme", "note": "DAMAGED_REQUEST_SENTINEL"},
    )
    invocation = {
        "status": "succeeded",
        "response": {"result": {"record": {"note": "DAMAGED_RESPONSE_SENTINEL"}}},
    }

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        return_value=invocation,
    ):
        result = service.execute_action(
            "acme.record-source",
            "fetch_record",
            body,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
        ).dump()

    assert result["action"]["response"]["result"]["record"]["note"] == "DAMAGED_RESPONSE_SENTINEL"
    persisted = json.dumps(bl.log_extension_invocation.call_args.kwargs, default=str)
    assert "DAMAGED_RESPONSE_SENTINEL" not in persisted
    assert "DAMAGED_REQUEST_SENTINEL" not in persisted
