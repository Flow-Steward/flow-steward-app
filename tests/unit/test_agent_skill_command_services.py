"""Agent and skill command services own the registry rules the admin routes used to hold."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.application.management import command_services_agent_rules as rules
from core.application.management.command_services_agent import AgentCommandService
from core.application.management.command_services_skill import SkillCommandService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import normalize_scope
from core.entrypoints.admin._service_errors import raise_service_http_error

ACCOUNT_A = "account-A"
ACCOUNT_B = "account-B"
VALID = {"valid": True, "errors": [], "warnings": []}
INVALID = {"valid": False, "errors": [{"message": "Missing output"}], "warnings": []}


def _scope(account_id: str = ACCOUNT_A):
    return normalize_scope(account_id=account_id, actor_id="operator")


def _agent_service(db, *, validation=VALID, reload_registry=None):
    return AgentCommandService(
        get_db=lambda: db,
        reload_registry=reload_registry or MagicMock(),
        get_agent_validation=MagicMock(return_value=validation),
        validate_skill_compatibility=MagicMock(return_value=VALID),
    )


def _skill_service(db, *, validation=VALID, reload_registry=None):
    return SkillCommandService(
        get_db=lambda: db,
        reload_registry=reload_registry or MagicMock(),
        get_skill_validation=MagicMock(return_value=validation),
    )


def _db_with_agent(**agent):
    db = MagicMock()
    row = {"agent_id": "writer", "account_id": ACCOUNT_A, "enabled": False, **agent}
    db.get_agent.side_effect = lambda agent_id: dict(row) if agent_id == row["agent_id"] else None
    return db


def _db_with_skill(**skill):
    db = MagicMock()
    row = {"skill_id": "draft", "account_id": ACCOUNT_A, "enabled": False, **skill}
    db.get_skill.side_effect = lambda skill_id: dict(row) if skill_id == row["skill_id"] else None
    return db


# --------------------------------------------------------------------------- #
# Agents                                                                      #
# --------------------------------------------------------------------------- #


def test_create_agent_stamps_transport_account_under_the_requested_id():
    db = MagicMock()
    db.get_agent.return_value = None
    reload_registry = MagicMock()
    service = _agent_service(db, reload_registry=reload_registry)

    result = service.create_agent(
        {"agent_id": "writer", "display_name": "Writer", "skills": [{"skill_id": "draft"}]},
        scope=_scope(),
    )

    saved = db.save_agent.call_args.args[0]
    assert result.status == "created"
    assert result.agent_id == saved["agent_id"] == "writer"
    assert db.save_agent.call_args.kwargs["create_only"] is True
    assert saved["account_id"] == ACCOUNT_A
    assert saved["skills"] == ["draft"]
    assert saved["base_skill"] == "draft"
    reload_registry.assert_called_once_with()


def test_create_agent_refuses_a_foreign_account_and_standard_tool_actions():
    db = MagicMock()
    db.get_agent.return_value = None
    service = _agent_service(db)

    with pytest.raises(ServiceForbiddenError):
        service.create_agent(
            {"agent_id": "writer", "account_id": ACCOUNT_B, "display_name": "W"},
            scope=_scope(),
        )
    with pytest.raises(ServiceValidationError, match="Standard Tool Actions"):
        service.create_agent(
            {"agent_id": "writer", "display_name": "W", "tools": ["std.http_request"]},
            scope=_scope(),
        )
    db.save_agent.assert_not_called()


def test_enabled_save_with_invalid_readiness_is_a_contract_error():
    db = MagicMock()
    db.get_agent.return_value = None
    service = _agent_service(db, validation=INVALID)

    with pytest.raises(ServiceValidationError) as refused:
        service.create_agent(
            {"agent_id": "writer", "display_name": "W", "enabled": True}, scope=_scope()
        )

    assert refused.value.payload["action"] == "save"
    assert refused.value.payload["entity_type"] == "agent"


def test_foreign_account_agent_is_not_found_for_every_mutation():
    db = _db_with_agent(account_id=ACCOUNT_B, enabled=True)
    service = _agent_service(db)
    scope = _scope(ACCOUNT_A)

    for call in (
        lambda: service.patch_agent("writer", {"description": "x"}, scope=scope),
        lambda: service.toggle_agent("writer", scope=scope),
        lambda: service.delete_agent("writer", scope=scope),
        lambda: service.restore_agent_revision("writer", "7", scope=scope),
        lambda: service.add_agent_tool("writer", "lookup", scope=scope),
        lambda: service.set_agent_own_io("writer", "inputs", [], scope=scope),
    ):
        with pytest.raises(ServiceNotFoundError, match="Agent 'writer' not found"):
            call()
    db.save_agent.assert_not_called()
    db.toggle_agent.assert_not_called()
    db.delete_agent.assert_not_called()


@pytest.mark.parametrize("route", ["patch", "toggle"])
def test_patch_and_toggle_share_the_disable_dependency_rule(route):
    db = _db_with_agent(enabled=True)
    service = _agent_service(db)

    with (
        patch.object(rules, "enabled_workflow_dependencies", return_value=["daily_import"]),
        pytest.raises(ServiceConflictError) as refused,
    ):
        if route == "patch":
            service.patch_agent("writer", {"enabled": False}, scope=_scope())
        else:
            service.toggle_agent("writer", scope=_scope())

    assert refused.value.payload == {
        "error": "Active workflow dependency",
        "message": (
            "Agent 'writer' cannot be disabled while enabled workflows depend on it: "
            "daily_import. Disable or update those workflows first."
        ),
        "workflow_ids": ["daily_import"],
    }
    db.save_agent.assert_not_called()
    db.toggle_agent.assert_not_called()


def test_patch_without_enabled_does_not_consult_workflow_dependencies():
    db = _db_with_agent(enabled=True, config={"provider": "openai", "model": "m", "x": 1})
    service = _agent_service(db)

    with patch.object(rules, "enabled_workflow_dependencies") as dependencies:
        result = service.patch_agent(
            "writer", {"config": {"provider": "", "model": ""}}, scope=_scope()
        )

    dependencies.assert_not_called()
    merged = db.save_agent.call_args.args[0]
    assert merged["config"] == {"x": 1}
    assert db.save_agent.call_args.kwargs == {"author": "operator", "sync_relationships": False}
    assert result.status == "updated"


def test_patch_with_no_fields_is_no_change():
    db = _db_with_agent()
    result = _agent_service(db).patch_agent("writer", {}, scope=_scope())

    assert result.model_dump(exclude_none=True) == {
        "status": "no_change",
        "agent_id": "writer",
        "enabled": False,
    }
    db.save_agent.assert_not_called()


def _stateful_db(kind: str, rows: dict | None = None):
    """A store that keeps what it saves, with the real store's ``enabled`` default."""
    db = MagicMock()
    stored = dict(rows or {})

    def save(data, author="admin", **kwargs):
        stored[data[f"{kind}_id"]] = {**data, "enabled": data.get("enabled", True)}

    getattr(db, f"save_{kind}").side_effect = save
    getattr(db, f"get_{kind}").side_effect = lambda entity_id: (
        dict(stored[entity_id]) if entity_id in stored else None
    )
    return db


def test_agent_mutations_report_the_persisted_enabled_state_not_null():
    """Live check 2026-10-31 (F8): create/update/set_enabled answered enabled=null."""
    db = _stateful_db("agent")
    service = _agent_service(db)

    created = service.create_agent(
        {"agent_id": "writer", "display_name": "Writer", "enabled": False}, scope=_scope()
    )
    enabled = service.patch_agent("writer", {"enabled": True}, scope=_scope())
    renamed = service.patch_agent("writer", {"description": "Writes"}, scope=_scope())
    replaced = service.update_agent(
        "writer", {"display_name": "Writer", "enabled": False}, scope=_scope()
    )
    unchanged = service.patch_agent("writer", {}, scope=_scope())

    assert (created.status, created.enabled) == ("created", False)
    assert (enabled.status, enabled.enabled) == ("updated", True)
    assert (renamed.status, renamed.enabled) == ("updated", True)
    assert (replaced.status, replaced.enabled) == ("updated", False)
    assert (unchanged.status, unchanged.enabled) == ("no_change", False)


def test_skill_mutations_report_the_persisted_enabled_state_not_null():
    db = _stateful_db("skill")
    service = _skill_service(db)

    created = service.create_skill(
        {"skill_id": "draft", "display_name": "Draft", "enabled": False}, scope=_scope()
    )
    enabled = service.patch_skill("draft", {"enabled": True}, scope=_scope())
    replaced = service.update_skill(
        "draft",
        {"display_name": "Draft", "enabled": False},
        provided_fields={"display_name", "enabled"},
        scope=_scope(),
    )
    unchanged = service.patch_skill("draft", {}, scope=_scope())

    assert (created.status, created.enabled) == ("created", False)
    assert (enabled.status, enabled.enabled) == ("updated", True)
    assert (replaced.status, replaced.enabled) == ("updated", False)
    assert (unchanged.status, unchanged.enabled) == ("no_change", False)


def test_toggle_enable_requires_readiness_with_enable_action():
    db = _db_with_agent(enabled=False)
    service = _agent_service(db, validation=INVALID)

    with pytest.raises(ServiceValidationError) as refused:
        service.toggle_agent("writer", scope=_scope())

    assert refused.value.payload["action"] == "enable"
    db.toggle_agent.assert_not_called()


def test_toggle_flips_and_reloads():
    db = _db_with_agent(enabled=False)
    db.toggle_agent.return_value = True
    reload_registry = MagicMock()

    result = _agent_service(db, reload_registry=reload_registry).toggle_agent(
        "writer", scope=_scope()
    )

    assert (result.status, result.enabled) == ("toggled", True)
    reload_registry.assert_called_once_with()


def test_delete_agent_maps_store_failures():
    db = _db_with_agent()
    service = _agent_service(db)

    db.delete_agent.side_effect = RuntimeError("update violates foreign key constraint")
    with pytest.raises(ServiceConflictError, match="referenced by workflow steps"):
        service.delete_agent("writer", scope=_scope())
    db.delete_agent.side_effect = KeyError("Agent 'writer' not found")
    with pytest.raises(ServiceNotFoundError):
        service.delete_agent("writer", scope=_scope())
    db.delete_agent.side_effect = RuntimeError("boom")
    with pytest.raises(ServiceError) as failed:
        service.delete_agent("writer", scope=_scope())
    assert failed.value.status_code == 500


def test_restore_attributes_the_actor_and_returns_the_snapshot():
    db = _db_with_agent()
    db.restore_entity_revision.return_value = {"restored": {"agent_id": "writer"}}

    result = _agent_service(db).restore_agent_revision("writer", 12, scope=_scope())

    assert db.restore_entity_revision.call_args.kwargs["author"] == "operator_restore"
    assert result.restored_from_entry_id == "12"
    assert result.validation == VALID
    assert result.snapshot == {"agent_id": "writer"}


def test_add_agent_tool_rolls_back_an_invalid_mapping():
    db = _db_with_agent()
    service = _agent_service(db, validation=INVALID)

    with pytest.raises(ServiceValidationError) as refused:
        service.add_agent_tool("writer", " lookup ", scope=_scope())

    db.add_agent_tool.assert_called_once_with("writer", "lookup")
    db.remove_agent_tool.assert_called_once_with("writer", "lookup")
    assert refused.value.payload["action"] == "save_tool_mapping"


def test_contract_items_go_to_the_primary_skill():
    db = _db_with_agent(skills=[{"skill_id": "draft"}])
    db.get_skill_outputs.return_value = [{"name": "notes"}]

    rows = _agent_service(db).set_agent_contract_items(
        "writer", "outputs", [{"name": "notes", "type": "text"}], scope=_scope()
    )

    db.set_skill_outputs.assert_called_once()
    db.materialize_agents_for_skill.assert_called_once_with("draft")
    db.set_agent_outputs.assert_not_called()
    assert rows == [{"name": "notes"}]


# --------------------------------------------------------------------------- #
# Skills                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ["patch", "toggle"])
def test_skill_patch_and_toggle_share_the_disable_dependency_rule(route):
    db = _db_with_skill(enabled=True)
    db.get_agents_using_skill.return_value = ["writer"]
    service = _skill_service(db)

    with (
        patch.object(rules, "enabled_workflow_dependencies", return_value=["daily"]) as deps,
        pytest.raises(ServiceConflictError) as refused,
    ):
        if route == "patch":
            service.patch_skill("draft", {"enabled": False}, scope=_scope())
        else:
            service.toggle_skill("draft", scope=_scope())

    deps.assert_called_once_with(db, agent_ids=["writer"])
    assert refused.value.payload["error"] == "Active workflow dependency"
    assert refused.value.payload["workflow_ids"] == ["daily"]
    db.save_skill.assert_not_called()
    db.toggle_skill.assert_not_called()


def test_skill_update_keeps_stored_io_that_the_request_omitted():
    db = _db_with_skill(_inputs=[{"name": "changelog", "type": "text"}])

    result = _skill_service(db).update_skill(
        "draft",
        {"display_name": "Draft", "expected_inputs": [], "expected_outputs": []},
        provided_fields={"display_name"},
        scope=_scope(),
    )

    saved = db.save_skill.call_args.args[0]
    assert saved["expected_inputs"] == [{"name": "changelog", "type": "text"}]
    assert saved["account_id"] == ACCOUNT_A
    db.materialize_agents_for_skill.assert_called_once_with("draft")
    assert result.status == "updated"


def test_skill_delete_reports_dependents_before_deleting():
    db = _db_with_skill()
    db.get_skill_dependents.return_value = [
        {"dependency_kind": "agent", "dependency_id": f"a{i}"} for i in range(26)
    ]

    with pytest.raises(ServiceConflictError) as refused:
        _skill_service(db).delete_skill("draft", scope=_scope())

    payload = refused.value.payload
    assert payload["error"] == "registry_dependency_conflict"
    assert len(payload["dependent_ids"]) == 25
    assert payload["dependent_ids_truncated"] is True
    db.delete_skill.assert_not_called()


def test_skill_clone_is_disabled_and_owned_by_the_caller():
    db = _db_with_skill(display_name="Draft", account_id=None)

    result = _skill_service(db).clone_skill("draft", scope=_scope())

    clone = db.save_skill.call_args.args[0]
    assert (clone["skill_id"], clone["enabled"], clone["account_id"]) == (
        "draft_copy",
        False,
        ACCOUNT_A,
    )
    assert clone["display_name"] == "Draft (Copy)"
    assert result.source_skill_id == "draft"


def test_skill_contract_items_reject_invalid_rows():
    db = _db_with_skill()

    with pytest.raises(ServiceValidationError) as refused:
        _skill_service(db).set_skill_contract_items(
            "draft", "inputs", [{"name": "", "type": "text"}], scope=_scope()
        )

    assert refused.value.payload["action"] == "save_inputs"
    db.set_skill_inputs.assert_not_called()


# --------------------------------------------------------------------------- #
# HTTP translation                                                            #
# --------------------------------------------------------------------------- #


def test_service_errors_keep_the_http_detail_shape():
    with pytest.raises(HTTPException) as plain:
        raise_service_http_error(ServiceNotFoundError("Agent 'x' not found"))
    assert (plain.value.status_code, plain.value.detail) == (404, "Agent 'x' not found")

    payload = {"error": "Active workflow dependency", "message": "m", "workflow_ids": ["w"]}
    with pytest.raises(HTTPException) as structured:
        raise_service_http_error(ServiceConflictError("m", payload=payload))
    assert (structured.value.status_code, structured.value.detail) == (409, payload)
