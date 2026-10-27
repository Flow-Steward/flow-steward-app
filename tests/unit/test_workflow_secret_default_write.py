"""Setting a secret default is write-only.

A secret input's configured value is accepted once, encrypted immediately, and
referred to by key from then on. Nothing else about the workflow may carry it:
the contract stores a reference, an export carries a reference, and a reload
tells the browser whether a value is configured, never what it is.
"""

from __future__ import annotations

import json

import pytest

from core.application.registry.workflow_contract import WorkflowInterface

_SECRET = "PLAINTEXT-MUST-NOT-PERSIST-8823"


@pytest.fixture(autouse=True)
def _hermetic_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.security import secret_codec

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "workflow-default-test"})


def _interface(field: dict) -> WorkflowInterface:
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [field],
                "output_mode": "none",
                "trigger_channels": ["api"],
            }
        }
    )


def test_a_secret_field_refuses_a_plaintext_default() -> None:
    """Otherwise the ordinary contract update is a way around the write-only path."""
    interface = _interface({"name": "login", "type": "secret", "default": _SECRET})

    codes = {item["code"] for item in interface.definition_issues()}
    assert "workflow.interface.secret_default_plaintext" in codes
    assert _SECRET not in json.dumps(interface.dump())


def test_a_secret_field_carries_only_its_reference() -> None:
    interface = _interface(
        {"name": "login", "type": "secret", "default_secret_ref": "wf:orders:login"}
    )

    dumped = json.dumps(interface.dump())
    assert "wf:orders:login" in dumped
    assert not interface.definition_issues()


def test_the_public_projection_reports_configured_not_the_value() -> None:
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    interface = _interface(
        {"name": "login", "type": "secret", "default_secret_ref": "wf:orders:login"}
    )
    payload = WorkflowInterfaceService().public_contract_payload(interface)

    field = next(row for row in payload["expected_inputs"] if row["name"] == "login")
    assert field["default_configured"] is True
    assert "default" not in field
    assert "default_secret_ref" not in field, (
        "the browser needs to know whether a value exists, not where it is stored"
    )


def test_the_public_projection_reports_an_unset_secret() -> None:
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    interface = _interface({"name": "login", "type": "secret"})
    payload = WorkflowInterfaceService().public_contract_payload(interface)

    field = next(row for row in payload["expected_inputs"] if row["name"] == "login")
    assert field["default_configured"] is False


def test_an_ordinary_default_is_still_visible_to_the_editor() -> None:
    """Only secrets are hidden; an operator must be able to see and change the rest."""
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    interface = _interface({"name": "region", "type": "string", "default": "eu-west-1"})
    payload = WorkflowInterfaceService().public_contract_payload(interface)

    field = next(row for row in payload["expected_inputs"] if row["name"] == "region")
    assert field["default"] == "eu-west-1"
    assert field["default_configured"] is True


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


def test_setting_a_secret_encrypts_it_and_returns_only_the_key() -> None:
    from core.application.registry.workflow_secret_defaults import set_secret_default

    stored: dict[str, str] = {}

    ref_key = set_secret_default(
        workflow_id="orders",
        field_name="login",
        plaintext=_SECRET,
        account_id="acct-1",
        encrypt=lambda value: f"encrypted::{value}",
        write_ciphertext=lambda account_id, key, ciphertext: stored.__setitem__(key, ciphertext),
    )

    assert ref_key.startswith("workflow-input:v2:")
    assert stored[ref_key] == f"encrypted::{_SECRET}"
    assert _SECRET not in ref_key


def test_setting_an_empty_secret_is_refused_rather_than_stored() -> None:
    """An empty credential is not a credential; storing it would read as configured."""
    from core.application.registry.workflow_secret_defaults import (
        SecretDefaultRejected,
        set_secret_default,
    )

    with pytest.raises(SecretDefaultRejected):
        set_secret_default(
            workflow_id="orders",
            field_name="login",
            plaintext="   ",
            account_id="acct-1",
            encrypt=lambda value: value,
            write_ciphertext=lambda *_args: None,
        )


def test_the_reference_key_is_scoped_to_the_workflow_and_field() -> None:
    """Two workflows configuring `login` must not share one stored value."""
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    assert secret_default_ref_key("orders", "login") != secret_default_ref_key("billing", "login")
    assert secret_default_ref_key("orders", "login") != secret_default_ref_key("orders", "token")


def test_distinct_valid_punctuation_in_field_names_never_collides() -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    assert secret_default_ref_key("orders", "api/key") != secret_default_ref_key(
        "orders", "api?key"
    )


def test_replacing_one_field_uses_a_new_immutable_reference() -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    assert secret_default_ref_key("orders", "api/key", version_token="4") != secret_default_ref_key(
        "orders", "api/key", version_token="5"
    )


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _secret_route_client(monkeypatch, db, registry=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
    from core.entrypoints.admin import workflows as workflow_routes
    from core.entrypoints.admin import workflows_routes_write as write_routes
    from core.entrypoints.admin import workflows_scope

    monkeypatch.setattr(write_routes, "_get_db", lambda *_a, **_k: db)
    if registry is None:
        registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    monkeypatch.setattr(write_routes, "_get_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(workflows_scope, "_get_registry", lambda *_a, **_k: registry)
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def workflow_db():
    from unittest.mock import MagicMock

    db = MagicMock()
    db.get_workflow.return_value = {
        "workflow_id": "orders",
        "revision": 3,
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "login", "type": "secret"}],
                "output_mode": "none",
                "trigger_channels": ["api"],
            }
        },
    }
    db.save_workflow.return_value = 4
    return db


def test_the_endpoint_stores_the_secret_and_answers_without_it(monkeypatch, workflow_db) -> None:
    response = _secret_route_client(monkeypatch, workflow_db).put(
        "/api/workflows/orders/inputs/login/secret",
        json={"value": _SECRET, "expected_revision": 3},
    )

    assert response.status_code == 200, response.text
    assert _SECRET not in response.text, "the response must never echo the value"
    assert response.json()["default_configured"] is True
    assert response.json()["revision"] == 4
    cur = workflow_db._transaction.return_value.__enter__.return_value
    assert any(
        "INSERT INTO secret_refs" in str(call.args[0]) for call in cur.execute.call_args_list
    ), "the value must reach the encrypted store"
    assert _SECRET not in json.dumps(workflow_db.save_workflow.call_args.args[0]), (
        "workflow settings must hold the reference, not the value"
    )


def test_the_endpoint_refuses_a_field_that_is_not_a_secret(monkeypatch, workflow_db) -> None:
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "string"}
    ]

    response = _secret_route_client(monkeypatch, workflow_db).put(
        "/api/workflows/orders/inputs/login/secret",
        json={"value": _SECRET, "expected_revision": 3},
    )

    assert response.status_code == 400
    assert _SECRET not in response.text


def test_clearing_removes_the_reference(monkeypatch, workflow_db) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    owned_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": owned_ref}
    ]

    response = _secret_route_client(monkeypatch, workflow_db).delete(
        "/api/workflows/orders/inputs/login/secret?expected_revision=3"
    )

    assert response.status_code == 200, response.text
    assert response.json()["default_configured"] is False
    assert response.json()["revision"] == 4
    saved = workflow_db.save_workflow.call_args.args[0]
    field = saved["settings"]["callable"]["expected_inputs"][0]
    assert not field.get("default_secret_ref")


def test_clearing_deletes_the_displaced_ciphertext_in_the_save_transaction(
    monkeypatch, workflow_db
) -> None:
    from core.application.management.scope import DEFAULT_ACCOUNT_ID
    from core.application.registry.workflow_secret_defaults import (
        secret_default_ref_key,
        secret_default_ref_owner_prefix,
    )

    owned_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["account_id"] = DEFAULT_ACCOUNT_ID
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": owned_ref}
    ]

    response = _secret_route_client(monkeypatch, workflow_db).delete(
        "/api/workflows/orders/inputs/login/secret?expected_revision=3"
    )

    assert response.status_code == 200, response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    delete_calls = [
        call
        for call in cur.execute.call_args_list
        if "DELETE FROM secret_refs" in str(call.args[0])
    ]
    owner_prefix = secret_default_ref_owner_prefix("orders", "login")
    assert [call.args[1] for call in delete_calls] == [
        (DEFAULT_ACCOUNT_ID, owner_prefix, owner_prefix, "", "")
    ]


def test_replacing_deletes_the_previous_ciphertext_in_the_save_transaction(
    monkeypatch, workflow_db
) -> None:
    from core.application.management.scope import DEFAULT_ACCOUNT_ID
    from core.application.registry.workflow_secret_defaults import (
        secret_default_ref_key,
        secret_default_ref_owner_prefix,
    )

    old_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["account_id"] = DEFAULT_ACCOUNT_ID
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": old_ref}
    ]

    response = _secret_route_client(monkeypatch, workflow_db).put(
        "/api/workflows/orders/inputs/login/secret",
        json={"value": _SECRET, "expected_revision": 3},
    )

    assert response.status_code == 200, response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    delete_calls = [
        call
        for call in cur.execute.call_args_list
        if "DELETE FROM secret_refs" in str(call.args[0])
    ]
    owner_prefix = secret_default_ref_owner_prefix("orders", "login")
    params = delete_calls[0].args[1]
    assert params[:3] == (DEFAULT_ACCOUNT_ID, owner_prefix, owner_prefix)
    assert params[3]
    assert params[3:] == (params[4], params[4])
    assert params[4] != old_ref


def test_clear_without_expected_revision_is_rejected_before_save(monkeypatch, workflow_db) -> None:
    response = _secret_route_client(monkeypatch, workflow_db).delete(
        "/api/workflows/orders/inputs/login/secret"
    )

    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "workflow_revision_required"
    workflow_db.save_workflow.assert_not_called()


def test_stale_clear_does_not_mutate_persisted_reference(monkeypatch, workflow_db) -> None:
    from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    owned_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": owned_ref}
    ]
    workflow_db.save_workflow.side_effect = WorkflowRevisionConflictError(
        workflow_id="orders", expected_revision=2
    )

    response = _secret_route_client(monkeypatch, workflow_db).delete(
        "/api/workflows/orders/inputs/login/secret?expected_revision=2"
    )

    assert response.status_code == 409
    assert (
        workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"][0][
            "default_secret_ref"
        ]
        == owned_ref
    )


# ---------------------------------------------------------------------------
# Ordering
#
# The ciphertext was written before the workflow save, and the save is the
# optimistic-concurrency check. A stale revision returned 409 with the stored
# credential already replaced: the caller is told nothing happened, and the
# previous value is gone.
# ---------------------------------------------------------------------------


def test_a_stale_revision_does_not_replace_the_stored_secret(monkeypatch, workflow_db) -> None:
    from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError

    workflow_db.save_workflow.side_effect = WorkflowRevisionConflictError(
        workflow_id="orders", expected_revision=2
    )

    response = _secret_route_client(monkeypatch, workflow_db).put(
        "/api/workflows/orders/inputs/login/secret",
        json={"value": _SECRET, "expected_revision": 2},
    )

    assert response.status_code == 409
    cur = workflow_db._transaction.return_value.__enter__.return_value
    cur.execute.assert_not_called()


def test_storage_failure_returns_typed_value_independent_error(monkeypatch, workflow_db) -> None:
    workflow_db._transaction.side_effect = RuntimeError(
        "private database detail with secret-ref sentinel"
    )

    response = _secret_route_client(monkeypatch, workflow_db).put(
        "/api/workflows/orders/inputs/login/secret",
        json={"value": _SECRET, "expected_revision": 3},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "workflow_secret_storage_unavailable",
        "message": "Workflow secret storage is unavailable.",
    }
    assert _SECRET not in response.text
    assert "sentinel" not in response.text


@pytest.mark.parametrize("method", ["put", "delete"])
def test_committed_secret_mutation_stays_successful_when_cache_refresh_fails(
    monkeypatch, workflow_db, method
) -> None:
    from unittest.mock import MagicMock

    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    registry = MagicMock()
    registry.refresh_workflow_cache.side_effect = RuntimeError("refresh failed")
    if method == "delete":
        current_ref = secret_default_ref_key("orders", "login", version_token="3")
        workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
            {"name": "login", "type": "secret", "default_secret_ref": current_ref}
        ]
    client = _secret_route_client(monkeypatch, workflow_db, registry)

    if method == "put":
        response = client.put(
            "/api/workflows/orders/inputs/login/secret",
            json={"value": _SECRET, "expected_revision": 3},
        )
    else:
        response = client.delete("/api/workflows/orders/inputs/login/secret?expected_revision=3")

    assert response.status_code == 200, response.text
    registry.refresh_workflow_cache.assert_called_once_with()
    registry.reload_all.assert_not_called()


def test_invalid_stored_reference_returns_typed_error_without_the_reference(
    monkeypatch, workflow_db
) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key

    foreign_ref = secret_default_ref_key("orders", "token", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": foreign_ref}
    ]

    response = _secret_route_client(monkeypatch, workflow_db).delete(
        "/api/workflows/orders/inputs/login/secret?expected_revision=3"
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "workflow_secret_rejected"
    assert foreign_ref not in response.text


# ---------------------------------------------------------------------------
# Surviving a round trip
# ---------------------------------------------------------------------------


def test_an_export_drops_the_reference_instead_of_redacting_it() -> None:
    """Key-name redaction turned the ref into the literal string "[redacted]",
    which imports as a configured secret pointing at nothing. Absent is the
    truthful answer: the target environment holds no such credential."""
    from core.application.workflows.workflow_portability_common import _redact_sensitive

    settings = {
        "callable": {
            "expected_inputs": [
                {
                    "name": "login",
                    "type": "secret",
                    "default_secret_ref": "workflow:orders:input:login",
                }
            ]
        }
    }

    row = _redact_sensitive(settings)["callable"]["expected_inputs"][0]

    assert "default_secret_ref" not in row
    assert "[redacted]" not in json.dumps(row)
    assert row["name"] == "login"
    assert row["type"] == "secret"


def test_an_ordinary_settings_save_keeps_a_configured_secret(monkeypatch, workflow_db) -> None:
    """The browser is never given the storage key, so an ordinary save cannot
    echo it back. Treating its absence as "clear it" silently unconfigures a
    credential every time someone edits an unrelated field."""
    from core.application.registry.workflow_secret_defaults import preserve_secret_references

    stored = {
        "callable": {
            "expected_inputs": [
                {
                    "name": "login",
                    "type": "secret",
                    "default_secret_ref": "workflow:orders:input:login",
                }
            ]
        }
    }
    incoming = {
        "callable": {"expected_inputs": [{"name": "login", "type": "secret", "label": "Login"}]}
    }

    merged = preserve_secret_references(incoming, stored)
    row = merged["callable"]["expected_inputs"][0]

    assert row["default_secret_ref"] == "workflow:orders:input:login"
    assert row["label"] == "Login", "the edit itself must still be applied"


def test_an_ordinary_settings_save_cannot_replace_the_stored_reference() -> None:
    from core.application.registry.workflow_secret_defaults import preserve_secret_references

    stored = {
        "callable": {
            "expected_inputs": [
                {
                    "name": "login",
                    "type": "secret",
                    "default_secret_ref": "workflow-input:v1:owned:4",
                }
            ]
        }
    }
    incoming = {
        "callable": {
            "expected_inputs": [
                {
                    "name": "login",
                    "type": "secret",
                    "default_secret_ref": "workflow-input:v1:other-workflow:9",
                }
            ]
        }
    }

    row = preserve_secret_references(incoming, stored)["callable"]["expected_inputs"][0]

    assert row["default_secret_ref"] == "workflow-input:v1:owned:4"


def test_an_ordinary_patch_cannot_replace_the_stored_reference() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    stored = {
        "workflow_id": "orders",
        "settings": {
            "callable": {
                "expected_inputs": [
                    {
                        "name": "login",
                        "type": "secret",
                        "default_secret_ref": "workflow-input:v1:owned:4",
                    }
                ]
            }
        },
    }
    updates = {
        "settings": {
            "callable": {
                "expected_inputs": [
                    {
                        "name": "login",
                        "type": "secret",
                        "default_secret_ref": "workflow-input:v1:other-workflow:9",
                    }
                ]
            }
        }
    }

    sanitized = WorkflowPortabilityService(object()).public_patch_payload(stored, updates)

    row = sanitized["settings"]["callable"]["expected_inputs"][0]
    assert row["default_secret_ref"] == "workflow-input:v1:owned:4"


def test_public_create_cannot_mint_a_secret_reference() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    payload = {
        "workflow_id": "attacker-workflow",
        "settings": {
            "callable": {
                "expected_inputs": [
                    {
                        "name": "login",
                        "type": "secret",
                        "default_secret_ref": "workflow-input:v1:victim:4",
                        "default_configured": True,
                    }
                ]
            }
        },
    }

    sanitized = WorkflowPortabilityService(object()).public_create_payload(payload)

    row = sanitized["settings"]["callable"]["expected_inputs"][0]
    assert "default_secret_ref" not in row
    assert "default_configured" not in row


def test_public_workflow_and_history_projections_drop_secret_authority() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    workflow = {
        "workflow_id": "orders",
        "settings": {
            "callable": {
                "expected_inputs": [
                    {
                        "name": "login",
                        "type": "secret",
                        "default_secret_ref": "workflow-input:v1:owned:4",
                    }
                ]
            }
        },
    }
    service = WorkflowPortabilityService(object())

    detail = service.redact_protected_workflow(workflow)
    history = service.redact_workflow_history_entry(
        {"entity_id": "orders", "snapshot": workflow}, workflow_id="orders"
    )

    assert "default_secret_ref" not in json.dumps(detail)
    assert "default_secret_ref" not in json.dumps(history)
    assert detail["settings"]["callable"]["expected_inputs"][0]["default_configured"] is True
    assert (
        history["snapshot"]["settings"]["callable"]["expected_inputs"][0]["default_configured"]
        is True
    )


def test_a_field_that_stops_being_a_secret_does_not_keep_a_reference() -> None:
    from core.application.registry.workflow_secret_defaults import preserve_secret_references

    stored = {
        "callable": {
            "expected_inputs": [
                {"name": "login", "type": "secret", "default_secret_ref": "workflow:o:input:login"}
            ]
        }
    }
    incoming = {"callable": {"expected_inputs": [{"name": "login", "type": "string"}]}}

    row = preserve_secret_references(incoming, stored)["callable"]["expected_inputs"][0]

    assert "default_secret_ref" not in row


def _ordinary_workflow_update(
    client,
    *,
    method: str,
    expected_inputs: list[dict],
    enabled: bool = False,
):
    if method == "put":
        return client.put(
            "/api/workflows/orders",
            json={
                "workflow_id": "orders",
                "display_name": "Orders",
                "enabled": enabled,
                "settings": {"callable": {"expected_inputs": expected_inputs}},
                "expected_revision": 3,
            },
        )
    return client.patch(
        "/api/workflows/orders",
        json={
            "enabled": enabled,
            "settings": {"callable": {"expected_inputs": expected_inputs}},
            "expected_revision": 3,
        },
    )


@pytest.mark.parametrize("method", ["put", "patch"])
def test_ordinary_update_deletes_ciphertexts_when_secret_field_changes_type(
    monkeypatch, workflow_db, method
) -> None:
    from core.application.management.scope import DEFAULT_ACCOUNT_ID
    from core.application.registry.workflow_secret_defaults import (
        secret_default_ref_key,
        secret_default_ref_owner_prefix,
    )
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["account_id"] = DEFAULT_ACCOUNT_ID
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )

    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method=method,
        expected_inputs=[{"name": "login", "type": "string"}],
    )

    assert response.status_code == 200, response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    owner_prefix = secret_default_ref_owner_prefix("orders", "login")
    cleanup_calls = [
        call
        for call in cur.execute.call_args_list
        if "DELETE FROM secret_refs" in str(call.args[0])
    ]
    assert [call.args[1] for call in cleanup_calls] == [
        (DEFAULT_ACCOUNT_ID, owner_prefix, owner_prefix, "", "")
    ]
    saved_field = workflow_db.save_workflow.call_args.args[0]["settings"]["callable"][
        "expected_inputs"
    ][0]
    assert saved_field["type"] == "string"
    assert "default_secret_ref" not in saved_field


def test_full_update_deletes_ciphertexts_when_secret_field_is_removed(
    monkeypatch, workflow_db
) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )

    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method="put",
        expected_inputs=[],
    )

    assert response.status_code == 200, response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    assert any(
        "DELETE FROM secret_refs" in str(call.args[0]) for call in cur.execute.call_args_list
    )


def test_ordinary_update_keeps_ciphertexts_for_a_secret_field_that_remains(
    monkeypatch, workflow_db
) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )

    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method="put",
        expected_inputs=[{"name": "login", "type": "secret", "label": "Login"}],
    )

    assert response.status_code == 200, response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    assert not any(
        "DELETE FROM secret_refs" in str(call.args[0]) for call in cur.execute.call_args_list
    )
    saved_field = workflow_db.save_workflow.call_args.args[0]["settings"]["callable"][
        "expected_inputs"
    ][0]
    assert saved_field["default_secret_ref"] == current_ref


def test_stale_ordinary_update_deletes_no_ciphertexts(monkeypatch, workflow_db) -> None:
    from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    workflow_db.save_workflow.side_effect = WorkflowRevisionConflictError(
        workflow_id="orders", expected_revision=3
    )
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )

    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method="put",
        expected_inputs=[],
    )

    assert response.status_code == 409
    cur = workflow_db._transaction.return_value.__enter__.return_value
    cur.execute.assert_not_called()


@pytest.mark.parametrize("method", ["put", "patch"])
def test_validation_callback_failure_is_not_a_secret_storage_dependency(
    monkeypatch, workflow_db, method
) -> None:
    from core.entrypoints.admin import workflows_routes_write as write_routes

    def fail_validation(*_args, **_kwargs):
        raise RuntimeError("private validation callback failure")

    monkeypatch.setattr(write_routes, "_get_workflow_validation", fail_validation)
    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method=method,
        expected_inputs=[],
        enabled=True,
    )
    assert response.status_code == 500
    assert "workflow_secret_storage_unavailable" not in response.text
    assert "private validation callback failure" not in response.text
    cur = workflow_db._transaction.return_value.__enter__.return_value
    cur.execute.assert_not_called()


def test_validation_failure_deletes_no_ciphertexts(monkeypatch, workflow_db) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": False, "errors": [{"code": "invalid"}], "warnings": []},
    )
    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method="put",
        expected_inputs=[],
        enabled=True,
    )

    assert response.status_code == 400
    cur = workflow_db._transaction.return_value.__enter__.return_value
    cur.execute.assert_not_called()


def test_cleanup_failure_returns_typed_error_and_uses_the_save_transaction(
    monkeypatch, workflow_db
) -> None:
    from core.application.registry.workflow_secret_defaults import secret_default_ref_key
    from core.entrypoints.admin import workflows_routes_write as write_routes

    current_ref = secret_default_ref_key("orders", "login", version_token="3")
    workflow_db.get_workflow.return_value["settings"]["callable"]["expected_inputs"] = [
        {"name": "login", "type": "secret", "default_secret_ref": current_ref}
    ]
    monkeypatch.setattr(
        write_routes,
        "_get_workflow_validation",
        lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )
    cur = workflow_db._transaction.return_value.__enter__.return_value
    cur.execute.side_effect = RuntimeError("private cleanup failure")

    response = _ordinary_workflow_update(
        _secret_route_client(monkeypatch, workflow_db),
        method="put",
        expected_inputs=[],
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "workflow_secret_storage_unavailable",
        "message": "Workflow secret storage is unavailable.",
    }
    workflow_db.save_workflow.assert_called_once()
    assert cur.execute.called


def test_a_new_secret_field_starts_unconfigured() -> None:
    from core.application.registry.workflow_secret_defaults import preserve_secret_references

    incoming = {"callable": {"expected_inputs": [{"name": "token", "type": "secret"}]}}

    row = preserve_secret_references(incoming, {})["callable"]["expected_inputs"][0]

    assert not row.get("default_secret_ref")


def test_history_diff_and_restore_projections_remove_secret_reference_authority() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    ref = "workflow-input:v1:owned:history"
    service = WorkflowPortabilityService(object())
    diff = {
        "changes": [
            {
                "path": "settings.callable.expected_inputs[0].default_secret_ref",
                "change": "changed",
                "from": ref + "-old",
                "to": ref,
            },
            {"path": "display_name", "change": "changed", "from": "A", "to": "B"},
        ]
    }
    restored = {
        "workflow_id": "orders",
        "settings": {
            "callable": {
                "expected_inputs": [{"name": "login", "type": "secret", "default_secret_ref": ref}]
            }
        },
    }

    redacted_diff = service.redact_workflow_history_payload(diff)
    redacted_restore = service.redact_workflow_history_payload(restored)

    assert redacted_diff["changes"] == [diff["changes"][1]]
    assert "default_secret_ref" not in json.dumps(redacted_restore)
    assert (
        redacted_restore["settings"]["callable"]["expected_inputs"][0]["default_configured"] is True
    )
