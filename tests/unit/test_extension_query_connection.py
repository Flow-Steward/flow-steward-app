"""A page query that runs with a project connection (ADR 0067), end to end through the host."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions.operation_service import ExtensionOperationService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceValidationError,
)
from core.application.management.requests import ExtensionQueryRequest
from core.application.management.scope import ScopeContext
from core.application.secrets.approval_gate import SecretConfigurationUnapproved

SECRET = "tok-5a7c9e1b3d"  # pragma: allowlist secret
RUN_SUBPROCESS = (
    "core.infrastructure.extension_runtime.extension_runtime_invocation._run_subprocess_capped"
)


def _extension(bundle_root: Path, **overrides) -> dict:
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.connector",
        "extension_install_id": "install-1",
        "enabled": True,
        "bundle_root": str(bundle_root),
        "entrypoint": {
            "command": [sys.executable, "main.py"],
            # The canonical policy gives an operation 330 s; a connection query
            # is capped below it.
            "timeout_policy": {
                "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
                "default_seconds": 300,
                "minimum_seconds": 30,
                "maximum_seconds": 3600,
                "headroom_seconds": 30,
            },
        },
        "query_definitions": [
            {
                "query_id": "runs.list",
                "connection": {"connection_type_ids": ["service_profile"]},
                "handler": {"mode": "extension"},
            },
            {"query_id": "about", "handler": {"mode": "extension"}},
        ],
    }
    extension.update(overrides)
    return extension


def _connection(**overrides) -> dict:
    row = {
        "connection_id": "conn-1",
        "account_id": "acct-1",
        "project_id": "7",
        "extension_id": "example.connector",
        "extension_install_id": "install-1",
        "connection_type": "service_profile",
        "display_name": "Production service",
        "environment": "production",
        "credential_revision": 3,
        "connection_config": {"base_url": "https://service.example"},
        "secret_refs": {
            "provider_secrets": {
                "provider_key": "example.connector",
                "keys": {"service_token": "service_token"},
            }
        },
        "enabled": True,
    }
    row.update(overrides)
    return row


def _store(connection: dict | None = None, *, reads_secrets: bool = False) -> MagicMock:
    methods = [
        "get_project",
        "get_project_connection",
        "get_provider_secret",
        "log_extension_invocation",
    ]
    store = MagicMock(spec=[*methods, "cursor"] if reads_secrets else methods)
    store.get_project.side_effect = lambda project_id: {
        "7": {"id": "7", "account_id": "acct-1"},
        "8": {"id": "8", "account_id": "acct-other"},
    }.get(project_id)
    store.get_project_connection.return_value = connection or _connection()
    store.get_provider_secret.return_value = SECRET
    return store


def _service(extension: dict, store: MagicMock, dispatch: MagicMock | None = None):
    return ExtensionOperationService(
        get_logger=lambda: store,
        get_operation_store=lambda: store,
        resolve_extension_runtime_record=lambda _extension_id: (extension, store),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
        command_dispatch_service_factory=dispatch,
        event_command_service_factory=dispatch,
    )


def _scope(**overrides) -> ScopeContext:
    values = {"account_id": "acct-1", "actor_id": "olga", "principal_type": "operator"}
    values.update(overrides)
    return ScopeContext(**values)


def _body(**overrides) -> ExtensionQueryRequest:
    values = {
        "page_id": "runs",
        "component_id": "runs_grid",
        "context": {"project_id": "7"},
        "params": {"state": "running"},
        "target": {"connection_id": "conn-1"},
    }
    values.update(overrides)
    return ExtensionQueryRequest(**values)


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "example_connector"
    root.mkdir()
    (root / "main.py").write_text("print('{}')\n", encoding="utf-8")
    return root


def _run(
    tmp_path: Path,
    *,
    response: dict | None = None,
    store: MagicMock | None = None,
    body: ExtensionQueryRequest | None = None,
    query_id: str = "runs.list",
    scope: ScopeContext | None = None,
    dispatch: MagicMock | None = None,
):
    store = store or _store()
    stdout = json.dumps(response or {"ok": True, "result": {"items": [{"run_id": "r-1"}]}})
    with patch(RUN_SUBPROCESS, return_value=(0, stdout, "", False)) as subprocess_call:
        try:
            result = _service(_extension(_bundle(tmp_path)), store, dispatch).execute_query(
                "example.connector", query_id, body or _body(), scope=scope or _scope()
            )
        except ServiceError as exc:
            return exc, subprocess_call, store
    return result, subprocess_call, store


def test_a_connection_query_gets_the_resolved_connection_at_the_envelope_root(tmp_path):
    result, subprocess_call, store = _run(tmp_path)

    assert result.query["status"] == "succeeded"
    envelope = json.loads(subprocess_call.call_args.kwargs["input_text"])
    assert envelope["mode"] == "query"
    assert envelope["query"]["query_id"] == "runs.list"
    assert envelope["query"]["page_id"] == "runs"
    assert envelope["query"]["component_id"] == "runs_grid"
    assert envelope["query"]["context"]["project_id"] == "7"
    assert envelope["query"]["params"] == {"state": "running"}
    assert envelope["target"] == {
        "connection_id": "conn-1",
        "connection": {
            "connection_id": "conn-1",
            "account_id": "acct-1",
            "project_id": "7",
            "display_name": "Production service",
            "connection_type": "service_profile",
            "environment": "production",
            "credential_revision": 3,
            "config": {"base_url": "https://service.example"},
            "secret_refs": {
                "provider_secrets": {
                    "provider_key": "example.connector",
                    "keys": {"service_token": "service_token"},
                }
            },
            "secrets": {"service_token": SECRET},
        },
    }
    runtime_context = envelope["runtime_context"]
    assert runtime_context["project_id"] == "7"
    assert runtime_context["connection_id"] == "conn-1"
    assert runtime_context["extension_install_id"] == "install-1"
    # The canonical timeout_policy would allow 330 s; a connection query gets 30.
    assert subprocess_call.call_args.kwargs["timeout"] == 30
    logged = store.log_extension_invocation.call_args.kwargs
    assert logged["request_context"]["connection_id"] == "conn-1"
    assert logged["request_context"]["project_id"] == "7"
    assert SECRET not in json.dumps(logged, default=str)


def test_a_connection_query_without_a_target_is_refused_before_any_lookup(tmp_path):
    refused, subprocess_call, store = _run(tmp_path, body=_body(target={}))

    assert isinstance(refused, ServiceValidationError)
    assert refused.status_code == 400
    assert refused.payload["code"] == "query_connection_required"
    subprocess_call.assert_not_called()
    store.get_project_connection.assert_not_called()


@pytest.mark.parametrize(
    "target",
    [
        {"connection_id": "conn-1", "secrets": {"service_token": "x"}},
        {"connection_id": "conn-1", "config": {"base_url": "https://evil.example"}},
        {"connection_id": "conn-1", "secret_refs": {}},
        {"connection_id": "conn-1", "project_id": "8"},
        {"connection": {"connection_id": "conn-1"}},
        {"connection_id": ""},
        {"connection_id": 7},
    ],
)
def test_a_query_target_is_only_a_connection_id(tmp_path, target):
    refused, subprocess_call, store = _run(tmp_path, body=_body(target=target))

    assert isinstance(refused, ServiceValidationError)
    assert refused.payload["code"] == "query_target_invalid"
    subprocess_call.assert_not_called()
    store.get_project_connection.assert_not_called()


def test_a_target_sent_to_a_query_without_a_connection_is_refused(tmp_path):
    refused, subprocess_call, _ = _run(tmp_path, query_id="about")

    assert isinstance(refused, ServiceValidationError)
    assert refused.payload["code"] == "query_target_invalid"
    subprocess_call.assert_not_called()


def test_a_query_without_a_connection_runs_with_an_empty_target(tmp_path):
    result, subprocess_call, store = _run(tmp_path, query_id="about", body=_body(target={}))

    assert result.query["status"] == "succeeded"
    envelope = json.loads(subprocess_call.call_args.kwargs["input_text"])
    assert envelope["target"] == {}
    store.get_project_connection.assert_not_called()


_ROW_REFUSAL = (
    "target.connection_id must reference an enabled project connection for this account, "
    "project, and extension"
)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_connection(project_id="9"), _ROW_REFUSAL),
        (_connection(account_id="acct-2"), _ROW_REFUSAL),
        (_connection(extension_id="example.other"), _ROW_REFUSAL),
        (_connection(extension_install_id="install-2"), _ROW_REFUSAL),
        (_connection(enabled=False), _ROW_REFUSAL),
        (
            _connection(connection_type="other_type"),
            "connection_runtime_requirement_failed:conn-1",
        ),
    ],
)
def test_a_connection_outside_the_query_is_refused(tmp_path, row, message):
    refused, subprocess_call, store = _run(tmp_path, store=_store(row))

    assert isinstance(refused, ServiceValidationError)
    assert refused.status_code == 400
    assert refused.payload == {"error": message}
    subprocess_call.assert_not_called()
    store.get_provider_secret.assert_not_called()


def test_an_unapproved_configuration_is_refused_before_a_stored_secret_is_read(tmp_path):
    row = _connection(
        secret_refs={"refs": {"service_token": "catalog:s-1"}, "catalog": {"service_token": "s-1"}}
    )
    store = _store(row, reads_secrets=True)
    with (
        patch(
            "core.application.secrets.approval_gate.require_approved_connection_use",
            side_effect=SecretConfigurationUnapproved(),
        ),
        patch(
            "core.infrastructure.security.connection_secret_store.resolve_connection_secrets",
            side_effect=AssertionError("no stored secret may be read"),
        ) as read_secrets,
    ):
        refused, subprocess_call, _ = _run(tmp_path, store=store)

    assert isinstance(refused, ServiceConflictError)
    assert refused.status_code == 409
    assert refused.payload["code"] == "secret_configuration_unapproved"
    read_secrets.assert_not_called()
    subprocess_call.assert_not_called()


@pytest.mark.parametrize("query_id", ["runs.list", "about"])
def test_an_operator_names_a_project_for_every_query(tmp_path, query_id):
    refused, subprocess_call, _ = _run(
        tmp_path, query_id=query_id, body=_body(context={}, target={})
    )

    assert isinstance(refused, ServiceForbiddenError)
    assert refused.payload["code"] == "project_context_required"
    subprocess_call.assert_not_called()


def test_a_connection_query_in_a_project_of_another_account_is_refused(tmp_path):
    refused, subprocess_call, store = _run(tmp_path, body=_body(context={"project_id": "8"}))

    assert isinstance(refused, ServiceForbiddenError)
    subprocess_call.assert_not_called()
    store.get_project_connection.assert_not_called()


def test_a_result_that_echoes_the_connection_secret_is_withheld_and_not_logged(tmp_path):
    refused, _subprocess, store = _run(
        tmp_path, response={"ok": True, "result": {"items": [{"note": f"token {SECRET}"}]}}
    )

    assert isinstance(refused, ServiceError)
    assert refused.payload["code"] == "extension_secret_exposure_blocked"
    assert SECRET not in json.dumps(refused.payload, default=str)
    logged = store.log_extension_invocation.call_args.kwargs
    assert SECRET not in json.dumps(logged, default=str)


@pytest.mark.parametrize(
    "effects",
    [
        {"commands": [{"command_type": "job.cancel", "target": {}, "payload": {}}]},
        {"events": [{"event_type": "example.updated", "payload": {}}]},
        {"project_state_updates": [{"namespace": "runs", "state_key": "last", "value": {}}]},
        {"result": {"project_state_updates": [{"namespace": "runs", "state_key": "last"}]}},
    ],
)
def test_a_connection_query_cannot_change_the_platform(tmp_path, effects):
    dispatch = MagicMock()
    refused, _subprocess, store = _run(
        tmp_path, response={"ok": True, "result": {}, **effects}, dispatch=dispatch
    )

    assert isinstance(refused, ServiceError)
    assert refused.payload["code"] == "query_connection_effects_forbidden"
    assert refused.payload["query"]["status"] == "failed"
    dispatch.assert_not_called()
    logged = store.log_extension_invocation.call_args.kwargs
    assert logged["status"] == "failed"
    assert "query_connection_effects_forbidden" in json.dumps(logged, default=str)


def test_a_remote_runtime_install_cannot_run_a_connection_query(tmp_path):
    with patch(
        "core.infrastructure.db.remote_extension_runtime.is_remote_runtime_enabled",
        return_value=True,
    ):
        refused, subprocess_call, store = _run(tmp_path)

    assert isinstance(refused, ServiceConflictError)
    assert refused.payload["code"] == "query_connection_runtime_unsupported"
    subprocess_call.assert_not_called()
    store.get_project_connection.assert_not_called()
