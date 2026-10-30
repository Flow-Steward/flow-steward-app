"""Workflow portability tests for direct agent-step execution model."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# B06: reserved identities and atomic import refusals use real canonical PG.
from functools import partial as _b06_partial
from types import SimpleNamespace as _B06Namespace

from core.application.auth.config import DEFAULT_ACCOUNT_ID as _B06_ACCOUNT
from core.application.orchestration.graph.builder import (
    _collect_directly_referenced_agents,
    _validate_graph_contracts,
)
from core.application.orchestration.helpers import (
    get_agent_from_step,
    get_step_participant_key,
    resolve_step_agent,
)
from core.application.registry.db_store import RegistryDBStore as _B06RegistryDBStore
from core.application.registry.db_store_workflows_mixin import (
    WorkflowCreateConflictError as _B06WorkflowCreateConflictError,
)
from core.application.registry.workflow_manifest import (
    StepConfig,
    WorkflowManifest,
    WorkflowPublicContract,
)
from core.application.workflows.workflow_definition_service import (
    create_workflow_with_definition as _b06_create_definition,
)
from core.application.workflows.workflow_portability import (
    SCHEMA_VERSION,
    TOP_LEVEL_KEYS,
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.entrypoints.admin.workflows_models import WorkflowImportRequest
from core.infrastructure.runtime.entrypoint_db_adapters import (
    create_integration_store as _b06_create_integration_store,
)
from core.infrastructure.runtime.entrypoint_db_adapters import (
    create_project_store as _b06_create_project_store,
)
from core.infrastructure.runtime.entrypoint_db_adapters import (
    workflow_import_transaction as _b06_import_transaction,
)
from tests._workflow_definitions import edge, linear_workflow, node


def test_workflow_import_request_normalizes_blank_optional_target() -> None:
    request = WorkflowImportRequest(bundle_yaml="bundle", target_workflow_id="")

    assert request.target_workflow_id is None


def _make_agent(agent_id: str, skills: list[str], enabled: bool = True):
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.skills = skills
    agent.enabled = enabled
    agent.display_name = agent_id
    agent.config = {}
    return agent


def _make_registry(*agents):
    registry = MagicMock()
    registry.agents.list_all.return_value = list(agents)
    by_id = {a.agent_id: a for a in agents}
    registry.agents.get = lambda aid: by_id.get(aid)
    registry.tools = MagicMock()
    return registry


def test_scoped_graph_validation_ignores_unrelated_corrupt_agent_and_skill() -> None:
    good_agent = _make_agent("good-agent", ["good-skill"])
    bad_agent = _make_agent("bad-agent", ["missing-skill"])
    good_skill = MagicMock(
        skill_id="good-skill",
        enabled=True,
        input_schema={"type": "object", "properties": {"request": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
    )
    bad_skill = MagicMock(
        skill_id="bad-skill",
        enabled=True,
        input_schema={},
        output_schema={},
    )
    step = MagicMock(agent="good-agent", skill="good-skill")
    selected_workflow = MagicMock(
        workflow_id="selected-workflow",
        enabled=True,
        steps=[step],
    )
    registry = MagicMock()
    registry.db_store = MagicMock()
    registry.agents.list_all.return_value = [good_agent, bad_agent]
    registry.skills.list_all.return_value = [good_skill, bad_skill]
    registry.workflows.list_all.return_value = [selected_workflow]

    def validate_agent(_db, agent_id):
        if agent_id == "bad-agent":
            return {
                "valid": False,
                "errors": [{"message": "unknown skill", "path": "agent.skills[0]"}],
                "warnings": [],
            }
        return {"valid": True, "errors": [], "warnings": []}

    with (
        patch(
            "core.application.registry.contract_validation.validate_agent_readiness",
            side_effect=validate_agent,
        ) as validate_agent_mock,
        patch(
            "core.application.registry.contract_validation.validate_skill_readiness",
            return_value={"valid": True, "errors": [], "warnings": []},
        ) as validate_skill_mock,
        patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value={"valid": True, "errors": [], "warnings": []},
        ),
    ):
        errors = _validate_graph_contracts(registry, workflow_ids=["selected-workflow"])

    assert errors == []
    validate_agent_mock.assert_called_once_with(registry.db_store, "good-agent")
    validate_skill_mock.assert_called_once_with(registry.db_store, "good-skill")


class _PortabilityDB:
    def __init__(
        self,
        *,
        workflows=None,
        agents=None,
        skills=None,
        tools=None,
        agent_tools=None,
        blocks=None,
        installs=None,
        connections=None,
        history=None,
        fail_save_workflow_ids=None,
        fail_binding_workflow_ids=None,
    ):
        self.workflows = dict(workflows or {})
        self.agents = dict(agents or {})
        self.skills = dict(skills or {})
        self.tools = dict(tools or {})
        self.agent_tools = dict(agent_tools or {})
        self.blocks = dict(blocks or {})
        self.installs = dict(installs or {})
        self.connections = dict(connections or {})
        self.history = [dict(row) for row in list(history or []) if isinstance(row, dict)]
        self.save_calls = []
        self.project_workflow_bindings = []
        self.fail_save_workflow_ids = set(fail_save_workflow_ids or [])
        self.fail_binding_workflow_ids = set(fail_binding_workflow_ids or [])

    @contextmanager
    def workflow_import_transaction(self):
        workflows_before = deepcopy(self.workflows)
        connections_before = deepcopy(self.connections)
        bindings_before = deepcopy(self.project_workflow_bindings)
        try:
            yield
        except Exception:
            self.workflows = workflows_before
            self.connections = connections_before
            self.project_workflow_bindings = bindings_before
            raise

    def get_workflow(self, workflow_id, *, include_trashed=False):
        row = self.workflows.get(str(workflow_id or "").strip())
        if not isinstance(row, dict):
            return None
        if not include_trashed and row.get("deleted_at") is not None:
            return None
        return dict(row)

    def get_agent(self, agent_id):
        row = self.agents.get(str(agent_id or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def get_skill(self, skill_id):
        row = self.skills.get(str(skill_id or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def get_tool(self, tool_id):
        row = self.tools.get(str(tool_id or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def get_agent_tools(self, agent_id):
        return list(self.agent_tools.get(str(agent_id or "").strip(), []))

    def get_published_block_contract(self, block_key):
        row = self.blocks.get(str(block_key or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def get_extension_by_install_id(self, install_id):
        row = self.installs.get(str(install_id or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def get_project_connection(self, connection_ref):
        row = self.connections.get(str(connection_ref or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def list_agents_summary(self, enabled_only=False):
        return [
            dict(row)
            for row in self.agents.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_skills_summary(self, enabled_only=False):
        return [
            dict(row)
            for row in self.skills.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_tools_summary(self, enabled_only=False):
        return [
            dict(row)
            for row in self.tools.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_workflows_summary(
        self,
        enabled_only=False,
    ):
        _ = enabled_only
        return [
            dict(row)
            for row in self.workflows.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_published_block_contracts(self, enabled_only=False, **kwargs):
        _ = kwargs
        return [
            dict(row)
            for row in self.blocks.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_extensions(self, enabled_only=False):
        return [
            dict(row)
            for row in self.installs.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def list_project_connections(self, project_id, enabled_only=False):
        _ = project_id
        return [
            dict(row)
            for row in self.connections.values()
            if not enabled_only or bool(row.get("enabled", True))
        ]

    def upsert_project_connection(self, **kwargs):
        connection_id = (
            kwargs.get("connection_id")
            or f"conn_{kwargs.get('project_id')}_{kwargs.get('extension_id') or kwargs.get('extension_install_id')}"
        )
        row = {
            "connection_id": str(connection_id),
            "project_id": kwargs.get("project_id"),
            "extension_id": kwargs.get("extension_id"),
            "extension_install_id": kwargs.get("extension_install_id"),
            "enabled": kwargs.get("enabled", False),
            "connection_config": kwargs.get("connection_config") or {},
            "secret_refs": kwargs.get("secret_refs") or [],
        }
        self.connections[row["connection_id"]] = row
        return dict(row)

    def get_changelog_entry(self, entry_id, *, entity_type=None, entity_id=None):
        for row in self.history:
            if int(row.get("id") or 0) != int(entry_id):
                continue
            if entity_type and row.get("entity_type") != entity_type:
                continue
            if entity_id and row.get("entity_id") != entity_id:
                continue
            return dict(row)
        return None

    def get_entity_history(self, entity_type, entity_id, limit=50):
        rows = [
            dict(row)
            for row in self.history
            if row.get("entity_type") == entity_type and row.get("entity_id") == entity_id
        ]
        return rows[: int(limit)]

    def save_workflow(self, data, author="admin", *, create_only=False, expected_revision=None):
        del create_only, expected_revision
        payload = dict(data)
        self.save_calls.append({"data": payload, "author": author})
        if payload["workflow_id"] in self.fail_save_workflow_ids:
            raise RuntimeError(f"save failed for {payload['workflow_id']}")
        self.workflows[payload["workflow_id"]] = payload

    def add_project_workflow_binding(self, project_id, workflow_id):
        if workflow_id in self.fail_binding_workflow_ids:
            raise RuntimeError(f"binding failed for {workflow_id}")
        self.project_workflow_bindings.append((project_id, workflow_id))


def _manual_workflow(workflow_id="manual_wf"):
    return linear_workflow(
        {
            "step_id": "plan",
            "step_kind": "agent",
            "participant_key": "owner",
            "agent_id": "scout",
            "skill_id": "collect_findings",
            "block_key": "blkproj:inst-crm:operation.sync",
            "workflow_id": "child_wf",
            "connection_binding_jsonb": {"connection_id": "crm-prod"},
            "override": {
                "add_tools": ["search_docs"],
                "connector_install_id": "inst-crm",
            },
        },
        workflow_id=workflow_id,
        version="1.0.0",
        display_name="Manual Workflow",
        description="Portable workflow",
        projection_mode="manual",
        trigger={"confidence_threshold": 0.4},
        settings={
            "participants": [{"participant_key": "owner", "agent_id": "scout"}],
            "notifications": {"api_token": "super-secret-token"},
        },
        tags=["portable"],
    )


def _mailer_workflow(workflow_id="mailer_wf"):
    binding = {"connection_id": "conn_author"}
    return linear_workflow(
        {
            "step_id": "send",
            "step_kind": "connector",
            "connector_id": "extension.acme.mailer",
            "connector_install_id": "inst-crm",
            "connection_binding_jsonb": dict(binding),
            "override": {
                "connector": {
                    "extension_id": "acme.mailer",
                    "operation_id": "send_email",
                    "connection_binding": dict(binding),
                }
            },
        },
        workflow_id=workflow_id,
        version="1.0.0",
        display_name="Mailer Workflow",
        projection_mode="manual",
    )


def _resolved_db(*, workflows=None):
    workflow_rows = {"child_wf": {"workflow_id": "child_wf", "display_name": "Child"}}
    workflow_rows.update(workflows or {})
    return _PortabilityDB(
        workflows=workflow_rows,
        agents={"scout": {"agent_id": "scout", "display_name": "Scout"}},
        skills={
            "collect_findings": {
                "skill_id": "collect_findings",
                "display_name": "Analyze Requirements",
            }
        },
        tools={"search_docs": {"tool_id": "search_docs", "display_name": "Search Docs"}},
        agent_tools={"scout": [{"tool_id": "search_docs"}]},
        blocks={
            "blkproj:inst-crm:operation.sync": {
                "block_key": "blkproj:inst-crm:operation.sync",
                "display_name": "Sync",
            }
        },
        installs={"inst-crm": {"install_id": "inst-crm", "status": "active"}},
        connections={"crm-prod": {"connection_id": "crm-prod", "enabled": True}},
    )


_DEFINITION_KINDS = (
    ("agents", "agent", "agent_id"),
    ("skills", "skill", "skill_id"),
    ("data_contracts", "data_contract", "contract_id"),
    ("guardrails", "guardrail", "id"),
)


def _reuse_definitions(bundle: str | dict) -> list[dict]:
    """A `use_existing` resolution for every definition the bundle carries.

    `_resolved_db()` already holds the agent and skill a default export carries, so
    importing such a bundle there is a conflict until each one is reused.
    """
    parsed = yaml.safe_load(bundle) if isinstance(bundle, str) else bundle
    definitions = parsed.get("definitions") or {}
    return [
        {"dependency_key": f"{kind}:{entry[id_field]}", "action": "use_existing"}
        for section, kind, id_field in _DEFINITION_KINDS
        for entry in definitions.get(section) or []
    ]


class TestWorkflowPortabilityService:
    @pytest.mark.parametrize(
        "flag",
        (
            "allow_full_export",
            "full_export_allowed",
            "allow_export",
            "export_allowed",
            "allow_fork",
            "fork_allowed",
        ),
    )
    @pytest.mark.parametrize(
        "location",
        (
            "extension_policy",
            "export_policy",
            "source_policy",
            "settings.extension_policy",
            "settings.export_policy",
            "settings.workflow_policy",
            "settings.portability",
        ),
    )
    def test_installed_source_stays_immutable_with_licensed_export_and_fork(self, flag, location):
        workflow = {
            **_manual_workflow("licensed_installed"),
            "source_install_id": "trusted-install",
            "projection_mode": "derived",
        }
        if location.startswith("settings."):
            workflow["settings"] = {location.split(".", 1)[1]: {flag: True}}
        else:
            workflow[location] = {flag: True}
        db = _PortabilityDB(workflows={"licensed_installed": workflow})
        service = WorkflowPortabilityService(db)

        policy = service.export_policy("licensed_installed")

        assert policy["protected"] is False
        assert policy["full_export_allowed"] is True
        assert policy["clone_allowed"] is True
        assert policy["step_test_run_allowed"] is True
        assert policy["structure_change_allowed"] is False
        with pytest.raises(WorkflowPortabilityError) as raised:
            service.assert_structure_change_allowed("licensed_installed", action="edit")
        assert raised.value.status_code == 403
        assert raised.value.code == "workflow_internal_structure_protected"

    def test_licensed_installed_source_metadata_restore_and_delete_are_refused(self):
        workflow = {
            **_manual_workflow("licensed_installed"),
            "source_install_id": "trusted-install",
            "projection_mode": "derived",
            "settings": {"portability": {"allow_full_export": True}},
        }
        history = [
            {
                "id": 17,
                "entity_type": "workflow",
                "entity_id": "licensed_installed",
                "snapshot": workflow,
            }
        ]
        service = WorkflowPortabilityService(
            _PortabilityDB(workflows={"licensed_installed": workflow}, history=history)
        )

        for operation, code in (
            (
                lambda: service.public_update_payload(workflow, {"display_name": "Forged"}),
                "workflow_update_protected",
            ),
            (
                lambda: service.public_patch_payload(workflow, {"display_name": "Forged"}),
                "workflow_protected_metadata_immutable",
            ),
            (
                lambda: service.public_patch_payload(
                    workflow, {"builder_metadata_jsonb": {"purpose": "Forged"}}
                ),
                "workflow_protected_metadata_immutable",
            ),
            (
                lambda: service.assert_restore_allowed("licensed_installed", entry_id="17"),
                "workflow_internal_structure_protected",
            ),
            (
                lambda: service.assert_delete_allowed("licensed_installed"),
                "workflow_internal_structure_protected",
            ),
        ):
            with pytest.raises(WorkflowPortabilityError) as raised:
                operation()
            assert raised.value.status_code == 403
            assert raised.value.code == code

        assert service.public_patch_payload(workflow, {"enabled": False}) == {"enabled": False}
        service.assert_history_diff_allowed(
            "licensed_installed", from_entry_id="17", to_entry_id="17"
        )

    def test_licensed_installed_source_exports_to_an_independent_editable_copy(self):
        workflow = _manual_workflow("licensed_installed")
        workflow.update(source_install_id="trusted-install", projection_mode="derived")
        workflow["settings"]["portability"] = {"allow_full_export": True}
        source_db = _resolved_db(workflows={"licensed_installed": workflow})
        source_db.installs["trusted-install"] = {
            "install_id": "trusted-install",
            "status": "active",
        }
        source_service = WorkflowPortabilityService(source_db)

        source_service.assert_clone_allowed("licensed_installed")
        bundle = source_service.export_yaml("licensed_installed")
        exported = yaml.safe_load(bundle)["workflows"][0]
        assert exported["steps"]
        assert exported.get("portability_kind") != "extension_workflow_ref"

        copy_db = _resolved_db()
        copy_db.installs["trusted-install"] = {"install_id": "trusted-install", "status": "active"}
        WorkflowPortabilityService(copy_db).apply_import(
            bundle,
            target_workflow_id="independent_copy",
            resolutions=[
                *_reuse_definitions(bundle),
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "map_existing",
                    "target_connection_id": "crm-prod",
                },
            ],
        )
        copied = copy_db.workflows["independent_copy"]
        assert copied["source_install_id"] is None
        assert copied["projection_mode"] == "manual"
        assert (
            WorkflowPortabilityService(copy_db).export_policy("independent_copy")[
                "structure_change_allowed"
            ]
            is True
        )
        assert source_db.workflows["licensed_installed"] == workflow

    def test_manual_workflow_export_policy_allows_full_export(self):
        db = _PortabilityDB(workflows={"manual_wf": _manual_workflow()})
        policy = WorkflowPortabilityService(db).export_policy("manual_wf")

        assert policy["full_export_allowed"] is True
        assert policy["reference_export_allowed"] is False
        assert policy["clone_allowed"] is True
        assert policy["structure_read_allowed"] is True
        assert policy["structure_change_allowed"] is True
        assert policy["step_test_run_allowed"] is True

    def test_extension_owned_export_policy_protects_by_default(self):
        db = _PortabilityDB(
            workflows={
                "wf_ext": {
                    **_manual_workflow("wf_ext"),
                    "source_install_id": "inst-ext",
                    "source_release_id": "rel-1",
                    "projection_mode": "derived",
                }
            }
        )
        policy = WorkflowPortabilityService(db).export_policy("wf_ext")

        assert policy["extension_owned"] is True
        assert policy["protected"] is True
        assert policy["full_export_allowed"] is False
        assert policy["reference_export_allowed"] is True
        assert policy["clone_allowed"] is False

    def test_deleted_protected_workflow_history_diff_blocks_from_snapshot(self):
        db = _PortabilityDB(
            history=[
                {
                    "id": 7,
                    "entity_type": "workflow",
                    "entity_id": "wf_ext",
                    "snapshot": {
                        **_manual_workflow("wf_ext"),
                        "source_install_id": "inst-ext",
                        "projection_mode": "derived",
                    },
                }
            ]
        )

        try:
            WorkflowPortabilityService(db).assert_history_diff_allowed("wf_ext")
        except WorkflowPortabilityError as exc:
            assert exc.status_code == 403
            assert exc.code == "workflow_internal_structure_protected"
            assert exc.errors[0]["entry_id"] == "7"
            assert exc.errors[0]["action"] == "workflow_history_diff"
        else:
            raise AssertionError("expected protected history diff to be blocked")

    def test_deleted_protected_workflow_restore_blocks_from_snapshot(self):
        db = _PortabilityDB(
            history=[
                {
                    "id": 11,
                    "entity_type": "workflow",
                    "entity_id": "wf_ext",
                    "snapshot": {
                        **_manual_workflow("wf_ext"),
                        "source_install_id": "inst-ext",
                        "projection_mode": "derived",
                    },
                }
            ]
        )

        try:
            WorkflowPortabilityService(db).assert_restore_allowed("wf_ext", entry_id=11)
        except WorkflowPortabilityError as exc:
            assert exc.status_code == 403
            assert exc.code == "workflow_internal_structure_protected"
            assert exc.errors[0]["entry_id"] == "11"
            assert exc.errors[0]["action"] == "workflow_history_restore"
        else:
            raise AssertionError("expected protected history restore to be blocked")

    def test_deleted_manual_workflow_cannot_be_resurrected_from_history(self):
        db = _PortabilityDB(
            history=[
                {
                    "id": 12,
                    "entity_type": "workflow",
                    "entity_id": "wf_deleted",
                    "snapshot": _manual_workflow("wf_deleted"),
                }
            ]
        )

        try:
            WorkflowPortabilityService(db).assert_restore_allowed("wf_deleted", entry_id=12)
        except WorkflowPortabilityError as exc:
            assert exc.status_code == 409
            assert exc.code == "workflow_deleted_restore_forbidden"
        else:
            raise AssertionError("expected deleted workflow history restore to be blocked")

    def test_manual_export_yaml_has_required_shape_and_no_secret_values(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle_text = WorkflowPortabilityService(db).export_yaml("manual_wf")
        bundle = yaml.safe_load(bundle_text)

        # The default export carries the agent and skill it uses, between the
        # dependencies and the import hints.
        assert list(bundle.keys()) == [*TOP_LEVEL_KEYS[:-1], "definitions", TOP_LEVEL_KEYS[-1]]
        assert TOP_LEVEL_KEYS[-1] == "import_hints"
        assert bundle["schema_version"] == SCHEMA_VERSION
        assert bundle["kind"] == "workflow_bundle"
        workflow = bundle["workflows"][0]
        assert list(workflow.keys()) == [
            "workflow_id",
            "version",
            "display_name",
            "description",
            "trigger",
            "settings",
            "participants",
            "tags",
            "steps",
            "groups",
            "for_each",
            "execution_graph",
        ]
        assert workflow["workflow_id"] == "manual_wf"
        assert "enabled" not in workflow
        assert workflow["steps"][0]["step_id"] == "plan"
        assert "super-secret-token" not in bundle_text
        assert workflow["settings"]["notifications"]["api_token"] == "[redacted]"

    def test_export_yaml_keeps_template_words_and_token_counters(self):
        workflow = _manual_workflow("text_wf")
        template = "{% for token in lower.split() %}{{ token }}{% endfor %}"
        workflow["description"] = "the token is required; a password is a word here"
        workflow["steps"][0]["override"] = {
            "template": {"source": template},
            "config": {"max_tokens": 4000, "access_token": "super-secret-token"},
        }
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"text_wf": workflow})
        ).export_yaml("text_wf")
        exported = yaml.safe_load(bundle_text)["workflows"][0]

        assert exported["description"] == "the token is required; a password is a word here"
        assert exported["steps"][0]["override"]["template"]["source"] == template
        assert exported["steps"][0]["override"]["config"]["max_tokens"] == 4000
        assert exported["steps"][0]["override"]["config"]["access_token"] == "[redacted]"
        assert "super-secret-token" not in bundle_text

    def test_for_each_container_survives_export_and_import_round_trip(self):
        workflow = _manual_workflow("loop_source")
        workflow["steps"] = [
            {
                "step_id": "seen",
                "step_kind": "set_variable",
                "group_id": None,
                "for_each_id": "iterate",
                "lane_id": None,
                "override": {"set_variable": {"target": "state.seen", "value": True}},
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ]
        workflow["for_each"] = [
            {
                "for_each_id": "iterate",
                "group_id": None,
                "over": "state.items",
                "item_name": "item",
                "index_name": "index",
                "body": {"kind": "sequence", "entry": {"kind": "step", "id": "seen"}},
            }
        ]
        workflow["settings"] = {
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "for_each", "id": "iterate"},
                "edges": [
                    {
                        "source": {"kind": "step", "id": "seen"},
                        "outcome": {"name": "success"},
                        "target": {
                            "kind": "for_each_exit",
                            "for_each_id": "iterate",
                            "lane_id": None,
                        },
                        "rework": False,
                    },
                    {
                        "source": {"kind": "for_each", "id": "iterate"},
                        "outcome": {"name": "success"},
                        "target": {"kind": "step", "id": "done"},
                        "rework": False,
                    },
                ],
            }
        }
        exported = WorkflowPortabilityService(
            _resolved_db(workflows={"loop_source": workflow})
        ).export_yaml("loop_source")
        exported_workflow = yaml.safe_load(exported)["workflows"][0]

        assert exported_workflow["for_each"] == [
            {
                "for_each_id": "iterate",
                "group_id": None,
                "display_name": "iterate",
                "over": "state.items",
                "item_name": "item",
                "index_name": "index",
                "body": {"kind": "sequence", "entry": {"kind": "step", "id": "seen"}},
            }
        ]
        assert exported_workflow["steps"][0]["for_each_id"] == "iterate"

        import_db = _resolved_db()
        result = WorkflowPortabilityService(import_db).apply_import(
            exported,
            target_workflow_id="loop_imported",
        )
        imported = import_db.workflows["loop_imported"]

        assert result["workflow_id"] == "loop_imported"
        assert imported["for_each"][0]["over"] == "state.items"
        assert imported["steps"][0]["for_each_id"] == "iterate"

    def test_a_disabled_step_survives_export_and_import_round_trip(self):
        workflow = _manual_workflow("disabled_source")

        def set_variable(step_id: str, **extra) -> dict:
            return {
                "step_id": step_id,
                "step_kind": "set_variable",
                "group_id": None,
                "for_each_id": None,
                "lane_id": None,
                "override": {"set_variable": {"target": f"state.{step_id}", "value": True}},
                **extra,
            }

        workflow["steps"] = [
            set_variable("skipped", disabled=True),
            set_variable("runs", disabled=False),
            {"step_id": "done", "step_kind": "stop_success"},
        ]

        def success(source: str, target: str) -> dict:
            return {
                "source": {"kind": "step", "id": source},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": target},
                "rework": False,
            }

        workflow["settings"] = {
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "step", "id": "skipped"},
                "edges": [success("skipped", "runs"), success("runs", "done")],
            }
        }
        exported = WorkflowPortabilityService(
            _resolved_db(workflows={"disabled_source": workflow})
        ).export_yaml("disabled_source")
        steps = {
            step["step_id"]: step for step in yaml.safe_load(exported)["workflows"][0]["steps"]
        }

        assert steps["skipped"]["disabled"] is True
        assert "disabled" not in steps["runs"], "an enabled step is the default"

        import_db = _resolved_db()
        WorkflowPortabilityService(import_db).apply_import(
            exported, target_workflow_id="disabled_imported"
        )
        imported = {
            step["step_id"]: step for step in import_db.workflows["disabled_imported"]["steps"]
        }

        assert imported["skipped"]["disabled"] is True
        assert imported["runs"].get("disabled", False) is False

    def test_protected_export_yaml_is_reference_only(self):
        db = _PortabilityDB(
            workflows={
                "wf_ext": {
                    **_manual_workflow("wf_ext"),
                    "source_install_id": "inst-ext",
                    "source_release_id": "rel-1",
                    "local_workflow_id": "local_sync",
                    "projection_mode": "derived",
                    "runtime_mode": "remote",
                }
            }
        )
        bundle_text = WorkflowPortabilityService(db).export_yaml("wf_ext")
        bundle = yaml.safe_load(bundle_text)
        workflow = bundle["workflows"][0]

        assert workflow["portability_kind"] == "extension_workflow_ref"
        assert workflow["source_install_id"] == "inst-ext"
        assert workflow["source_release_id"] == "rel-1"
        assert workflow["local_workflow_id"] == "local_sync"
        assert workflow["requires_extension"] is True
        assert "steps" not in workflow
        assert "execution_graph" not in bundle_text

    def test_reference_only_protected_bundle_blocks_import_preflight(self):
        bundle_text = WorkflowPortabilityService(
            _PortabilityDB(
                workflows={
                    "wf_ext": {
                        **_manual_workflow("wf_ext"),
                        "source_install_id": "inst-ext",
                        "projection_mode": "derived",
                    }
                }
            )
        ).export_yaml("wf_ext")

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(bundle_text)

        assert preflight["can_apply"] is False
        protected = next(
            item
            for item in preflight["dependencies"]
            if item["dependency_type"] == "extension_workflow_ref"
        )
        assert protected["status"] == "protected_reference"
        assert protected["blocking"] is True
        try:
            WorkflowPortabilityService(_resolved_db()).apply_import(bundle_text)
        except WorkflowPortabilityError as exc:
            assert exc.code == "workflow_import_preflight_blocked"
        else:
            raise AssertionError("Expected reference-only bundle import to be blocked")

    @pytest.mark.parametrize("step_kind", ["agent", ""])
    def test_import_rejects_agent_invoke_wrapper_without_any_persistence(self, step_kind):
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        step = bundle["workflows"][0]["steps"][0]
        step["override"] = {"invoke_workflow": {"workflow_id": "child_wf"}}
        resolutions = [
            *_reuse_definitions(bundle),
            {
                "dependency_key": "project_connection:crm-prod",
                "action": "map_existing",
                "target_connection_id": "crm-prod",
            },
        ]
        step["step_kind"] = "invoke_workflow"
        assert (
            WorkflowPortabilityService(_resolved_db()).preflight_import(
                yaml.safe_dump(bundle, sort_keys=False),
                resolutions=resolutions,
            )["can_apply"]
            is True
        )
        if step_kind:
            step["step_kind"] = step_kind
        else:
            step.pop("step_kind", None)
        bundle_text = yaml.safe_dump(bundle, sort_keys=False)
        target_db = _resolved_db()
        before = deepcopy(target_db.workflows)
        history_before = deepcopy(target_db.history)
        service = WorkflowPortabilityService(target_db)

        preflight = service.preflight_import(bundle_text, resolutions=resolutions)

        assert preflight["can_apply"] is False
        issues = [
            item["metadata"]
            for item in preflight["dependencies"]
            if item.get("metadata", {}).get("code") == "workflow.step_override_key_unknown"
        ]
        assert [item["path"] for item in issues] == [
            "workflows[0].steps.plan.override.invoke_workflow"
        ]
        with pytest.raises(WorkflowPortabilityError) as caught:
            service.apply_import(
                bundle_text, owner_project_id="project-target", resolutions=resolutions
            )
        # A definition the structural validator rejects is refused as topology; either
        # way nothing is written.
        assert caught.value.code in {
            "workflow_import_preflight_blocked",
            "workflow_import_topology_invalid",
        }
        assert target_db.save_calls == []
        assert target_db.workflows == before
        assert target_db.history == history_before
        assert target_db.project_workflow_bindings == []

    def test_import_preflight_blocks_unknown_or_foreign_step_override_before_save(self):
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        step = bundle["workflows"][0]["steps"][0]
        step["override"]["totally_bogus_key"] = True
        step["override"]["http_request"] = {
            "method": "POST",
            "url": "https://example.invalid",
        }
        bundle_text = yaml.safe_dump(bundle, sort_keys=False)
        target_db = _resolved_db()

        preflight = WorkflowPortabilityService(target_db).preflight_import(bundle_text)

        assert preflight["can_apply"] is False
        issues = [
            item
            for item in preflight["dependencies"]
            if item.get("metadata", {}).get("code") == "workflow.step_override_key_unknown"
        ]
        assert {item["metadata"]["path"] for item in issues} == {
            "workflows[0].steps.plan.override.http_request",
            "workflows[0].steps.plan.override.totally_bogus_key",
        }

        try:
            WorkflowPortabilityService(target_db).apply_import(bundle_text)
        except WorkflowPortabilityError as exc:
            assert exc.code == "workflow_import_preflight_blocked"
        else:
            raise AssertionError("Expected unknown override key import to be blocked")
        assert target_db.save_calls == []

    @pytest.mark.parametrize(
        ("step_kind", "override", "expected_path"),
        [
            (
                "http_request",
                {
                    "http_request": {
                        "method": "GET",
                        "url": (
                            "https://example.invalid/items/{{ state.rows[abc].sku | default:'x' }}"
                        ),
                        "response_mode": "inline",
                        "response_target": "state.response",
                        "timeout_seconds": 5,
                        "expected_statuses": [200],
                    }
                },
                "workflows[0].steps.plan.override.http_request.url",
            ),
            (
                "invoke_workflow",
                {
                    "invoke_workflow": {
                        "workflow_id": "child-workflow",
                        "input_mapping": {"sku": "state.rows[abc].sku"},
                        "output_mapping": {"result": "state.rows[abc].result"},
                    }
                },
                "workflows[0].steps.plan.override.invoke_workflow.input_mapping.sku",
            ),
            (
                "dataset_query",
                {
                    "dataset_query": {
                        "query_template_id": "saved-query",
                        "params": {"sku": "state.rows[abc].sku"},
                        "result_target": "state.rows[abc].result",
                    }
                },
                "workflows[0].steps.plan.override.dataset_query.params.sku",
            ),
            (
                "invoke_workflow",
                {
                    "invoke_workflow": {
                        "workflow_id": "child-workflow",
                        "input_mapping": {"sku": "state.rows[0].sku | default('x')"},
                    }
                },
                "workflows[0].steps.plan.override.invoke_workflow.input_mapping.sku",
            ),
            (
                "dataset_query",
                {
                    "dataset_query": {
                        "query_template_id": "saved-query",
                        "params": {"sku": "state.rows[0].sku | default('x')"},
                    }
                },
                "workflows[0].steps.plan.override.dataset_query.params.sku",
            ),
        ],
    )
    def test_import_preflight_blocks_malformed_state_selector_before_writes(
        self,
        step_kind,
        override,
        expected_path,
    ):
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        step = bundle["workflows"][0]["steps"][0]
        step["step_kind"] = step_kind
        step["override"] = override
        bundle_text = yaml.safe_dump(bundle, sort_keys=False)
        target_db = _resolved_db()

        preflight = WorkflowPortabilityService(target_db).preflight_import(bundle_text)

        issues = [
            item
            for item in preflight["dependencies"]
            if item.get("metadata", {}).get("code") == "workflow.state_selector_invalid"
        ]
        assert preflight["can_apply"] is False
        assert [item["metadata"]["path"] for item in issues] == [expected_path]
        _raises_callable_727_1 = WorkflowPortabilityService(target_db).apply_import
        with pytest.raises(WorkflowPortabilityError) as excinfo:
            _raises_callable_727_1(bundle_text)
        assert excinfo.value.code == "workflow_import_preflight_blocked"
        assert target_db.save_calls == []

    def test_conflict_preflight_blocks_without_target_resolution(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle_text = WorkflowPortabilityService(db).export_yaml("manual_wf")

        preflight = WorkflowPortabilityService(db).preflight_import(bundle_text)

        assert preflight["can_apply"] is False
        conflict = next(
            item
            for item in preflight["dependencies"]
            if item["metadata"].get("code") == "workflow_id_conflict"
        )
        assert conflict["status"] == "conflict"
        assert conflict["blocking"] is True

    def test_missing_workflow_id_preflight_blocks_with_specific_message(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle_text = WorkflowPortabilityService(db).export_yaml("manual_wf")
        bundle = yaml.safe_load(bundle_text)
        bundle["workflows"][0].pop("workflow_id")

        preflight = WorkflowPortabilityService(db).preflight_import(
            yaml.safe_dump(bundle, sort_keys=False),
            target_workflow_id="manual_wf_imported",
        )

        missing = next(
            item
            for item in preflight["dependencies"]
            if item["metadata"].get("code") == "workflow_id_missing"
        )
        assert preflight["can_apply"] is False
        assert missing["dependency_type"] == "workflow"
        assert missing["status"] == "blocked"
        assert missing["blocking"] is True
        assert missing["resolution_actions"] == ["abort"]
        assert missing["message"] == "Workflow definition is missing workflow_id."

    def test_malformed_yaml_preflight_returns_structured_parse_error(self):
        try:
            WorkflowPortabilityService(_resolved_db()).preflight_import(
                "schema_version: [\nkind: workflow_bundle\n"
            )
        except WorkflowPortabilityError as exc:
            assert exc.code == "bundle_yaml_invalid"
            assert str(exc) == "YAML bundle could not be parsed."
            assert exc.errors
        else:
            raise AssertionError("Expected malformed YAML to be rejected")

    def test_missing_dependency_preflight_requires_mapping(self):
        db = _PortabilityDB(workflows={})
        # Subject: an agent/skill the file only names; a default export carries them.
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf", reference_only=True)

        preflight = WorkflowPortabilityService(db).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
        )

        statuses = {
            (item["dependency_type"], item["ref"]): item["status"]
            for item in preflight["dependencies"]
        }
        assert statuses[("agent", "scout")] == "requires_mapping"
        assert statuses[("skill", "collect_findings")] == "requires_mapping"
        assert statuses[("tool", "search_docs")] == "requires_mapping"
        assert statuses[("extension", "inst-crm")] == "requires_extension_install"
        assert statuses[("project_connection", "crm-prod")] == "requires_project_connection"

    def test_invalid_none_child_workflow_ref_blocks_with_clear_message(self):
        workflow = _manual_workflow()
        workflow["steps"][0]["step_kind"] = "invoke_workflow"
        workflow["steps"][0]["workflow_id"] = "None"
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": workflow})
        ).export_yaml("manual_wf")

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
        )

        child = next(
            item
            for item in preflight["dependencies"]
            if item["dependency_type"] == "child_workflow" and item["ref"] == "None"
        )
        assert child["status"] == "blocked"
        assert child["blocking"] is True
        assert child["label"] == "Missing workflow id"
        assert child["resolution_actions"] == ["abort"]
        assert "without a usable id" in child["message"]

    def test_agent_step_none_workflow_id_is_not_exported_as_child_dependency(self):
        workflow = _manual_workflow()
        step = workflow["steps"][0]
        step["step_kind"] = "agent"
        step["workflow_id"] = "None"
        step["connector_id"] = "None"

        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": workflow})
        ).export_yaml("manual_wf")
        bundle = yaml.safe_load(bundle_text)

        exported_step = bundle["workflows"][0]["steps"][0]
        child_dependencies = bundle["dependencies"]["child_workflows"]
        assert "workflow_id" not in exported_step
        assert "connector_id" not in exported_step
        assert all(item["workflow_id"] != "None" for item in child_dependencies)

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
        )

        assert all(
            not (item["dependency_type"] == "child_workflow" and item["ref"] == "None")
            for item in preflight["dependencies"]
        )

    def test_export_records_how_the_workflow_starts_and_the_platform_version(self, monkeypatch):
        """The catalog could only ever say "manual": schedules, webhooks and provider
        triggers live outside the workflow row, so the export never mentioned them.

        The export records the kinds only. A cron expression, a webhook key or a
        project id belongs to the author's installation, not to the package.
        """
        from core.application.workflows import workflow_portability_export as export_module
        from core.infrastructure.config.version import APP_VERSION

        calls = []

        def _kinds(bl, *, workflow_id, account_id):
            calls.append((workflow_id, account_id))
            return [
                {"kind": "schedule"},
                {"kind": "webhook"},
                {"kind": "provider", "provider_key": "acme", "trigger_type_id": "acme.new_mail"},
            ]

        monkeypatch.setattr(export_module, "list_workflow_trigger_kinds", _kinds)

        class _Store:
            conn = object()

        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(
            WorkflowPortabilityService(
                db, extension_install_store=_Store(), account_id="acc_alpha"
            ).export_yaml("manual_wf")
        )

        assert bundle["metadata"]["triggers"] == [
            {"kind": "schedule"},
            {"kind": "webhook"},
            {"kind": "provider", "provider_key": "acme", "trigger_type_id": "acme.new_mail"},
        ]
        # The product version, which catalog compatibility is checked against.
        assert bundle["metadata"]["flow_steward_version"] == APP_VERSION
        assert calls == [("manual_wf", "acc_alpha")]

    def test_an_export_without_the_trigger_store_records_no_triggers(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(db).export_yaml("manual_wf"))

        assert bundle["metadata"]["triggers"] == []

    def test_a_bundle_with_trigger_metadata_still_imports(self):
        """Older and newer installs read the same file: `metadata` is informational."""
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(db).export_yaml("manual_wf"))
        bundle["metadata"]["triggers"] = [{"kind": "schedule"}]
        bundle["metadata"]["flow_steward_version"] = "1.0.0"

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            yaml.safe_dump(bundle, sort_keys=False)
        )

        assert preflight["kind"] == "workflow_import_preflight"

    def test_import_dependency_options_are_typed_registry_candidates(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})

        payload = WorkflowPortabilityService(db).import_dependency_options(project_id=17)

        assert payload["kind"] == "workflow_import_dependency_options"
        options = payload["options"]
        assert options["agent"][0]["value"] == "scout"
        assert options["skill"][0]["value"] == "collect_findings"
        assert options["tool"][0]["value"] == "search_docs"
        assert options["child_workflow"][0]["value"] == "child_wf"
        assert options["published_block"][0]["value"] == "blkproj:inst-crm:operation.sync"
        assert options["extension"][0]["value"] == "inst-crm"
        assert options["project_connection"][0]["value"] == "crm-prod"

    def test_extension_options_come_from_the_store_that_owns_installs(self):
        """The import wizard offered "No extension installs available" on every install.

        Extension candidates were listed from the registry store the admin routes pass as
        `db` — RegistryDBStore, which has no `list_extensions` — so the list was always
        empty, in the manual import and in the catalog dialog alike. The routes now hand
        in install rows the way they hand in project connections, and the wizard target
        is the install id the resolver matches, not the bundle's extension id.
        """
        payload = WorkflowPortabilityService(
            _RegistryStoreWithoutInstallLookup()
        ).import_dependency_options(
            account_id="acc_alpha",
            extension_install_rows=[
                {
                    "install_id": "196694e6-3d1c-4bf5-8573-dab99698dc81",
                    "extension_id": "acme.mailbox",
                    "display_name": "Acme Mailbox",
                    "description": "acme.mailbox · 0.1.0 · active",
                }
            ],
        )

        assert [option["value"] for option in payload["options"]["extension"]] == [
            "196694e6-3d1c-4bf5-8573-dab99698dc81"
        ]
        assert payload["options"]["extension"][0]["label"] == "Acme Mailbox"

    def test_install_option_rows_describe_installs_not_bundles(self):
        from core.application.extensions.extension_catalog_install import install_option_rows

        class _Record:
            def __init__(self, install_id, extension_id, version, state):
                self.install_id = install_id
                self.extension_id = extension_id
                self.version = version
                self.state = state

        class _Store:
            def list_for_account(self, account_id, **kwargs):
                self.call = (account_id, kwargs)
                return [_Record("inst-1", "acme.mailbox", "0.1.0", "active")]

        store = _Store()
        rows = install_option_rows(
            store,
            account_id="acc_alpha",
            bundle_rows=[{"extension_id": "acme.mailbox", "display_name": "Generic IMAP"}],
        )

        assert rows == [
            {
                "install_id": "inst-1",
                "extension_id": "acme.mailbox",
                "display_name": "Generic IMAP",
                "description": "acme.mailbox · 0.1.0 · active",
            }
        ]
        assert store.call == (
            "acc_alpha",
            {"include_global": True, "include_inactive_owned": True},
        )

    def test_an_exported_extension_keeps_its_portable_id_through_preflight(self):
        """Import read `install_id` and dropped the `extension_id` exported beside it."""
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        [exported] = bundle["dependencies"]["extensions"]
        exported["extension_id"] = "acme.mailbox"
        exported["version"] = "0.1.0"

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            yaml.safe_dump(bundle, sort_keys=False)
        )

        [row] = [
            item
            for item in preflight["dependencies"]
            if item["dependency_type"] == "extension" and item["ref"] == exported["install_id"]
        ]
        assert row["metadata"]["extension_id"] == "acme.mailbox"

    def test_import_dependency_options_accept_connection_rows_from_the_owning_store(self):
        db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        db.list_project_connections = None

        payload = WorkflowPortabilityService(db).import_dependency_options(
            project_id=17,
            project_connection_rows=[
                {
                    "connection_id": "supplier-feed",
                    "display_name": "Supplier SFTP",
                    "enabled": True,
                }
            ],
        )

        assert payload["options"]["project_connection"] == [
            {
                "value": "supplier-feed",
                "label": "Supplier SFTP",
                "enabled": True,
            }
        ]

    def test_project_connection_resolution_uses_the_owning_store(self):
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        registry_db = _resolved_db()
        registry_db.get_project_connection = None
        integration_store = _resolved_db()

        preflight = WorkflowPortabilityService(
            registry_db,
            project_connection_store=integration_store,
        ).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            resolutions=[
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "map_existing",
                    "target_connection_id": "crm-prod",
                }
            ],
        )

        connection = next(
            item
            for item in preflight["dependencies"]
            if item["dependency_type"] == "project_connection"
        )
        assert connection["status"] == "resolved"

    def test_a_mapped_connection_is_rewritten_in_every_copy_a_step_carries(self):
        """The builder keeps the binding twice: top-level and inside the connector override.

        Import rewrote only the top-level copy. Runtime reads that one first, so runs
        worked, but the override kept the author's connection id, and the next export
        carried the stale id to the next installation.
        """
        service = WorkflowPortabilityService(_resolved_db())
        step = {
            "step_id": "read_mailbox",
            "step_kind": "connector",
            "connection_binding_jsonb": {"connection_id": "conn_author"},
            "override": {
                "connector": {
                    "extension_id": "acme.mailbox",
                    "connection_binding": {"connection_id": "conn_author"},
                }
            },
        }
        index = {
            ("project_connection", "conn_author"): {
                "action": "map_existing",
                "target_ref": "conn_local",
            }
        }

        rewritten = service._rewrite_step(step, index)

        assert rewritten["connection_binding_jsonb"] == {"connection_id": "conn_local"}
        assert rewritten["override"]["connector"]["connection_binding"] == {
            "connection_id": "conn_local"
        }
        assert rewritten["override"]["connector"]["extension_id"] == "acme.mailbox"

    def test_extension_resolution_requires_installed_extension(self):
        db = _PortabilityDB(workflows={})
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")

        preflight = WorkflowPortabilityService(db).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            resolutions=[
                {
                    "dependency_key": "extension:inst-crm",
                    "action": "install_extension_required",
                }
            ],
        )

        extension = next(
            item
            for item in preflight["dependencies"]
            if item["dependency_type"] == "extension" and item["ref"] == "inst-crm"
        )
        assert extension["status"] == "requires_extension_install"
        assert extension["blocking"] is True

    def test_project_connection_placeholder_resolution_requires_creation_metadata(self):
        db = _resolved_db()
        db.connections.pop("crm-prod")
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")

        missing_preflight = WorkflowPortabilityService(db).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            resolutions=[
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "create_connection_placeholder",
                }
            ],
        )
        missing_connection = next(
            item
            for item in missing_preflight["dependencies"]
            if item["dependency_type"] == "project_connection" and item["ref"] == "crm-prod"
        )
        assert missing_connection["status"] == "requires_project_connection"

        resolved_preflight = WorkflowPortabilityService(db).preflight_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            resolutions=[
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "create_connection_placeholder",
                    "project_id": "17",
                    "extension_id": "inst-crm",
                }
            ],
        )

        resolved_connection = next(
            item
            for item in resolved_preflight["dependencies"]
            if item["dependency_type"] == "project_connection" and item["ref"] == "crm-prod"
        )
        assert resolved_connection["status"] == "resolved"

    def test_a_connection_resolves_only_to_one_of_the_extension_its_step_calls(self):
        """A mailer step's connection could be mapped to a Docling connection.

        The dependency did not say which extension it belongs to, so every connection
        of the project was offered and accepted; the run then failed on the wrong one.
        """
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"mailer_wf": _mailer_workflow()})
        ).export_yaml("mailer_wf")
        db = _resolved_db()
        db.connections = {
            "conn_mail": {"connection_id": "conn_mail", "extension_id": "acme.mailer"},
            "conn_docs": {"connection_id": "conn_docs", "extension_id": "acme.docs"},
        }
        service = WorkflowPortabilityService(db)

        def connection_row(resolution):
            preflight = service.preflight_import(
                bundle_text, target_workflow_id="mailer_imported", resolutions=[resolution]
            )
            return next(
                item
                for item in preflight["dependencies"]
                if item["dependency_key"] == "project_connection:conn_author"
            )

        def mapped(target):
            return {
                "dependency_key": "project_connection:conn_author",
                "action": "map_existing",
                "target_connection_id": target,
            }

        def placeholder(extension_id):
            return {
                "dependency_key": "project_connection:conn_author",
                "action": "create_connection_placeholder",
                "project_id": "17",
                "extension_id": extension_id,
            }

        assert connection_row(mapped("conn_mail"))["metadata"]["extension_id"] == "acme.mailer"
        wrong = connection_row(mapped("conn_docs"))
        assert wrong["status"] == "requires_project_connection"
        assert "acme.docs" in wrong["message"] and "acme.mailer" in wrong["message"]
        assert connection_row(mapped("conn_mail"))["status"] == "resolved"
        assert connection_row(placeholder("acme.docs"))["status"] == "requires_project_connection"
        assert connection_row(placeholder("acme.mailer"))["status"] == "resolved"

        with pytest.raises(WorkflowPortabilityError) as refused:
            service.apply_import(
                bundle_text,
                target_workflow_id="mailer_imported",
                author="tester",
                resolutions=[*_reuse_definitions(bundle_text), mapped("conn_docs")],
            )
        assert refused.value.code == "workflow_import_preflight_blocked"
        assert db.save_calls == []
        # The same import with the mailer's own connection goes through.
        service.apply_import(
            bundle_text,
            target_workflow_id="mailer_imported",
            author="tester",
            resolutions=[*_reuse_definitions(bundle_text), mapped("conn_mail")],
        )
        assert len(db.save_calls) == 1

    def test_import_apply_saves_disabled_draft_and_uses_registry_save_path(self):
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()
        result = WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            author="tester",
            # project_connection deps no longer auto-resolve against a same-named
            # existing connection (security hardening); the operator must map them
            # explicitly. Map crm-prod to the existing connection in _resolved_db.
            resolutions=[
                *_reuse_definitions(bundle_text),
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "map_existing",
                    "target_connection_id": "crm-prod",
                },
            ],
        )

        assert result["workflow_id"] == "manual_wf_imported"
        assert len(db.save_calls) == 1
        call = db.save_calls[0]
        saved = call["data"]
        assert call["author"] == "tester"
        assert saved["workflow_id"] == "manual_wf_imported"
        assert saved["enabled"] is False
        assert saved["projection_mode"] == "manual"
        assert saved["source_install_id"] is None
        assert saved["settings"]["portability"]["import_state"] == "draft"
        assert saved["steps"][0]["workflow_id"] == "child_wf"
        assert saved["steps"][0]["block_key"] == "blkproj:inst-crm:operation.sync"

    def test_import_rolls_back_connection_placeholder_when_workflow_save_fails(self):
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()
        db.connections.pop("crm-prod")
        db.fail_save_workflow_ids.add("manual_wf_imported")

        _raises_callable_1034_1 = WorkflowPortabilityService(db).apply_import
        with pytest.raises(RuntimeError, match="save failed for manual_wf_imported"):
            _raises_callable_1034_1(
                bundle_text,
                target_workflow_id="manual_wf_imported",
                resolutions=[
                    *_reuse_definitions(bundle_text),
                    {
                        "dependency_key": "project_connection:crm-prod",
                        "action": "create_connection_placeholder",
                        "project_id": "17",
                        "extension_id": "inst-crm",
                    },
                ],
            )

        assert "manual_wf_imported" not in db.workflows
        assert db.connections == {}

    def test_import_fails_closed_without_atomic_transaction(self):
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()
        db.workflow_import_transaction = None

        _raises_callable_1058_1 = WorkflowPortabilityService(db).apply_import
        with pytest.raises(WorkflowPortabilityError) as exc_info:
            _raises_callable_1058_1(
                bundle_text,
                target_workflow_id="manual_wf_imported",
                resolutions=[
                    *_reuse_definitions(bundle_text),
                    {
                        "dependency_key": "project_connection:crm-prod",
                        "action": "map_existing",
                        "target_connection_id": "crm-prod",
                    },
                ],
            )

        assert exc_info.value.code == "workflow_import_transaction_unavailable"
        assert exc_info.value.status_code == 503
        assert "manual_wf_imported" not in db.workflows

    def test_import_rolls_back_all_workflows_when_later_save_fails(self):
        bundle = yaml.safe_load(
            WorkflowPortabilityService(
                _resolved_db(workflows={"first": _manual_workflow("first")})
            ).export_yaml("first")
        )
        second = deepcopy(bundle["workflows"][0])
        second["workflow_id"] = "second"
        bundle["workflows"].append(second)
        db = _resolved_db()
        db.fail_save_workflow_ids.add("second")

        _raises_callable_1087_1 = WorkflowPortabilityService(db).apply_import
        _raises_input_1087_2 = yaml.safe_dump(bundle, sort_keys=False)
        with pytest.raises(RuntimeError, match="save failed for second"):
            _raises_callable_1087_1(
                _raises_input_1087_2,
                resolutions=[
                    *_reuse_definitions(bundle),
                    {
                        "dependency_key": "project_connection:crm-prod",
                        "action": "map_existing",
                        "target_connection_id": "crm-prod",
                    },
                ],
            )

        assert "first" not in db.workflows
        assert "second" not in db.workflows

    def test_import_rolls_back_placeholder_and_workflows_when_later_binding_fails(self):
        bundle = yaml.safe_load(
            WorkflowPortabilityService(
                _resolved_db(workflows={"first": _manual_workflow("first")})
            ).export_yaml("first")
        )
        second = deepcopy(bundle["workflows"][0])
        second["workflow_id"] = "second"
        bundle["workflows"].append(second)
        db = _resolved_db()
        db.connections.pop("crm-prod")
        db.fail_binding_workflow_ids.add("second")

        _raises_callable_1115_1 = WorkflowPortabilityService(db).apply_import
        _raises_input_1115_2 = yaml.safe_dump(bundle, sort_keys=False)
        with pytest.raises(RuntimeError, match="binding failed for second"):
            _raises_callable_1115_1(
                _raises_input_1115_2,
                owner_project_id="17",
                resolutions=[
                    *_reuse_definitions(bundle),
                    {
                        "dependency_key": "project_connection:crm-prod",
                        "action": "create_connection_placeholder",
                        "project_id": "17",
                        "extension_id": "inst-crm",
                    },
                ],
            )

        assert "first" not in db.workflows
        assert "second" not in db.workflows
        assert db.connections == {}
        assert db.project_workflow_bindings == []

    def test_import_commits_workflow_and_placeholder_together(self):
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()
        db.connections.pop("crm-prod")

        result = WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            owner_project_id="17",
            resolutions=[
                *_reuse_definitions(bundle_text),
                {
                    "dependency_key": "project_connection:crm-prod",
                    "action": "create_connection_placeholder",
                    "project_id": "17",
                    "extension_id": "inst-crm",
                },
            ],
        )

        placeholder_id = "conn_17_inst-crm"
        assert result["workflow_id"] == "manual_wf_imported"
        assert result["project_id"] == "17"
        assert placeholder_id in db.connections
        assert db.project_workflow_bindings == [("17", "manual_wf_imported")]
        assert db.workflows["manual_wf_imported"]["steps"][0]["connection_binding_jsonb"] == {
            "connection_id": placeholder_id
        }

    def test_import_apply_blocks_unresolved_dependencies(self):
        db = _PortabilityDB(workflows={})
        # Subject: an agent the file only names; a default export would carry it.
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf", reference_only=True)

        try:
            WorkflowPortabilityService(db).apply_import(
                bundle_text,
                target_workflow_id="manual_wf_imported",
            )
        except WorkflowPortabilityError as exc:
            assert exc.code == "workflow_import_preflight_blocked"
            assert any(
                item["status"] == "requires_mapping"
                for item in exc.errors
                if item["dependency_type"] == "agent"
            )
        else:
            raise AssertionError("Expected unresolved dependencies to block import apply")

    def test_import_apply_rejects_raw_mcp_connector_binding(self):
        workflow = _manual_workflow()
        workflow["steps"][0] = {
            "step_id": "plan",
            "step_kind": "connector",
            "display_name": "Project list",
            "connector_id": "mcp_c134570ca411419d",
            "override": {
                "connector": {
                    "operation_id": "fs_browser_full.project_list",
                }
            },
        }
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": workflow})
        ).export_yaml("manual_wf")
        db = _resolved_db()

        try:
            WorkflowPortabilityService(db).apply_import(
                bundle_text,
                target_workflow_id="manual_wf_imported",
                author="tester",
                resolutions=_reuse_definitions(bundle_text),
            )
        except WorkflowPortabilityError as exc:
            assert exc.code == "workflow_raw_mcp_connector_unsupported"
            assert any(item["connector_id"] == "mcp_c134570ca411419d" for item in exc.errors)
        else:
            raise AssertionError("Expected raw MCP connector binding to block import apply")

        assert db.save_calls == []

    def _import_resolutions(self, bundle_text):
        return [
            *_reuse_definitions(bundle_text),
            {
                "dependency_key": "project_connection:crm-prod",
                "action": "map_existing",
                "target_connection_id": "crm-prod",
            },
        ]

    def test_import_sanitizes_forbidden_trigger_keys_before_persist(self):
        # FS-DEEP-CAND-014 negative: a bundle carrying forbidden routing keys and
        # an out-of-range confidence_threshold must be sanitized at import time,
        # exactly as authoring create/update does, before reaching save_workflow.
        workflow = _manual_workflow()
        workflow["trigger"] = {
            "confidence_threshold": 5.0,
            "task_types": ["classification"],
            "complexity": "high",
            "keep_me": "ok",
        }
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": workflow})
        ).export_yaml("manual_wf")
        db = _resolved_db()

        WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            author="tester",
            resolutions=self._import_resolutions(bundle_text),
        )

        assert len(db.save_calls) == 1
        saved_trigger = db.save_calls[0]["data"]["trigger"]
        assert "task_types" not in saved_trigger
        assert "complexity" not in saved_trigger
        assert saved_trigger["confidence_threshold"] == 1.0
        assert saved_trigger["keep_me"] == "ok"

    def test_import_clean_trigger_is_preserved(self):
        # FS-DEEP-CAND-014 positive: a clean trigger imports unchanged.
        workflow = _manual_workflow()
        workflow["trigger"] = {"confidence_threshold": 0.4, "mode": "manual"}
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": workflow})
        ).export_yaml("manual_wf")
        db = _resolved_db()

        WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            author="tester",
            resolutions=self._import_resolutions(bundle_text),
        )

        saved_trigger = db.save_calls[0]["data"]["trigger"]
        assert saved_trigger == {"confidence_threshold": 0.4, "mode": "manual"}

    def test_import_binds_persisted_row_to_caller_account(self):
        # FS-DEEP-CAND-013 negative: an account-scoped caller's import must not
        # persist an unowned, system-scope row. The caller account is stamped and
        # scope_kind becomes "account" instead of defaulting to NULL/"system".
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()

        WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            author="tester",
            owner_account_id="acct-caller",
            owner_project_id="proj-caller",
            resolutions=self._import_resolutions(bundle_text),
        )

        saved = db.save_calls[0]["data"]
        assert saved["account_id"] == "acct-caller"
        assert saved["scope_kind"] == "account"
        assert saved["project_id"] == "proj-caller"

    def test_import_without_owner_account_does_not_invent_scope(self):
        # FS-DEEP-CAND-013 positive: when no caller account is supplied (e.g. a
        # legacy system-scoped admin path), import does not force account binding,
        # preserving the prior default behavior.
        bundle_text = WorkflowPortabilityService(
            _resolved_db(workflows={"manual_wf": _manual_workflow()})
        ).export_yaml("manual_wf")
        db = _resolved_db()

        WorkflowPortabilityService(db).apply_import(
            bundle_text,
            target_workflow_id="manual_wf_imported",
            author="tester",
            resolutions=self._import_resolutions(bundle_text),
        )

        saved = db.save_calls[0]["data"]
        assert "account_id" not in saved or not saved.get("account_id")
        assert "scope_kind" not in saved


class TestStepConfig:
    def test_agent_step_from_dict(self):
        step = StepConfig.from_dict(
            {
                "step_kind": "agent",
                "participant_key": "implementation",
                "agent": "composer",
                "skill": "draft_summary",
            }
        )
        assert step.step_kind == "agent"
        assert step.participant_key == "implementation"
        assert step.agent == "composer"
        assert step.skill == ""

    def test_explicit_invoke_step_extracts_child_id_from_override(self):
        step = StepConfig.from_dict(
            {
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": "daily_briefing"}},
            }
        )
        assert step.step_kind == "invoke_workflow"
        assert step.workflow_id == "daily_briefing"

    @pytest.mark.parametrize("step_kind", ["agent", ""])
    def test_agent_wrapper_does_not_promote_kind_or_extract_child_id(self, step_kind):
        raw = {
            "agent": "coordinator",
            "override": {
                "invoke_workflow": {"workflow_id": "daily_briefing"},
            },
        }
        if step_kind:
            raw["step_kind"] = step_kind
        step = StepConfig.from_dict(raw)
        assert step.step_kind == "agent"
        assert step.is_agent_step is True
        assert step.is_invoke_workflow_step is False
        assert step.workflow_id == ""

    def test_step_io_mappings_preserved_from_dict(self):
        step = StepConfig.from_dict(
            {
                "step_kind": "connector",
                "connector_id": "connector.demo",
                "input_mapping_jsonb": {"limit": "literal:10"},
                "output_mapping_jsonb": {"rows": "state.catalog_rows"},
            }
        )
        assert step.input_mapping_jsonb == {"limit": "literal:10"}
        assert step.output_mapping_jsonb == {"rows": "state.catalog_rows"}


class TestStepHelpers:
    def test_get_agent_from_step(self):
        assert get_agent_from_step({"agent": "composer"}) == "composer"

    def test_get_step_participant_key(self):
        assert get_step_participant_key({"participant_key": "proofreader"}) == "proofreader"


class TestResolveStepAgent:
    def test_concrete_agent_returned(self):
        registry = _make_registry(_make_agent("composer", ["draft_summary"]))
        assert resolve_step_agent({"agent": "composer"}, registry) == "composer"

    def test_missing_agent_returns_empty(self):
        registry = _make_registry(_make_agent("composer", ["draft_summary"]))
        assert resolve_step_agent({"participant_key": "implementation"}, registry) == ""


class TestHubRouting:
    def test_routes_direct_agent_step(self):
        from core.application.orchestration.step_hub_support import executor_node_for_step

        step = {"step_id": "dev", "agent": "composer", "skill_id": "draft_summary"}
        assert executor_node_for_step(step) == "composer"

    def test_routes_invoke_step(self):
        from core.application.orchestration.step_hub_support import executor_node_for_step

        step = {
            "step_id": "story_loop",
            "step_kind": "invoke_workflow",
            "skill_id": "compose_sections",
            "override": {"invoke_workflow": {"workflow_id": "daily_briefing"}},
        }
        assert executor_node_for_step(step) == "invoke_workflow_step"

    @pytest.mark.parametrize("step_kind", ["agent", ""])
    def test_agent_wrapper_routes_to_agent(self, step_kind):
        from core.application.orchestration.step_hub_support import executor_node_for_step

        step = {
            "agent": "coordinator",
            "override": {
                "invoke_workflow": {"workflow_id": "daily_briefing"},
            },
        }
        if step_kind:
            step["step_kind"] = step_kind
        assert executor_node_for_step(step) == "coordinator"


class TestWorkflowPublicContract:
    def test_from_settings_dict_with_io(self):
        contract = WorkflowPublicContract.from_settings(
            {
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                    "expected_outputs": [{"name": "report", "type": "text"}],
                }
            }
        )
        assert contract.callable
        assert contract.expected_inputs[0]["name"] == "request"
        assert contract.expected_inputs[0]["type"] == "paragraph"
        assert contract.expected_outputs[0]["name"] == "report"


class TestGraphTopology:
    def test_collect_directly_referenced_agents(self):
        wf = WorkflowManifest(
            workflow_id="wf",
            version="1.0.0",
            display_name="wf",
            description="",
            enabled=True,
            trigger={},
            steps=[StepConfig(step_id="dev", agent="composer")],
        )
        registry = MagicMock()
        registry.workflows.list_all.return_value = [wf]
        referenced = _collect_directly_referenced_agents(registry)
        assert referenced == {"composer"}

    def test_validate_graph_contracts_rejects_missing_agent(self):
        skill = MagicMock()
        skill.skill_id = "draft_summary"
        skill.enabled = True
        skill.input_schema = {"type": "object", "properties": {}, "required": []}
        skill.output_schema = {"type": "object", "properties": {}, "required": []}

        wf = WorkflowManifest(
            workflow_id="wf",
            version="1.0.0",
            display_name="wf",
            description="",
            enabled=True,
            trigger={},
            steps=[StepConfig(step_id="dev", agent="")],
        )

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.skills.list_all.return_value = [skill]
        registry.workflows.list_all.return_value = [wf]

        with (
            patch(
                "core.application.registry.contract_validation.validate_agent_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.application.registry.contract_validation.validate_skill_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={
                    "valid": False,
                    "errors": [
                        {
                            "message": "missing agent_id or participant_key",
                            "path": "workflow.steps.dev",
                        }
                    ],
                    "warnings": [],
                },
            ),
        ):
            errors = _validate_graph_contracts(registry)
        assert any("missing agent_id or participant_key" in err for err in errors)


class TestExecuteStepNode:
    def test_executes_resolved_agent(self):
        from core.application.orchestration.graph.nodes import execute_step_node
        from tests._step_run_state import step_run_state

        state = step_run_state(
            {"step_id": "dev", "agent": "composer", "skill_id": "draft_summary"},
            orchestrator_journal=[],
        )

        registry = _make_registry(_make_agent("composer", ["draft_summary"]))

        def _node_fn(_state):
            return {"agent_messages": [{"from_agent": "composer", "status": "pass"}]}

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.orchestration.graph.nodes.load_agent_implementation",
                return_value=object(),
            ),
            patch(
                "core.application.orchestration.graph.nodes.wrap_agent_as_node",
                return_value=_node_fn,
            ),
        ):
            result = execute_step_node(state)

        assert isinstance(result, dict)
        assert result.get("agent_messages")
        assert result.get("orchestrator_journal")
        assert result["orchestrator_journal"][0]["event"] == "step_agent_resolved"

    def test_returns_fail_message_when_agent_missing(self):
        from core.application.orchestration.graph.nodes import execute_step_node
        from tests._step_run_state import step_run_state

        state = step_run_state({"step_id": "dev", "skill_id": "draft_summary"})
        registry = _make_registry()

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            update = execute_step_node(state)

        assert isinstance(update, dict)
        assert update.get("agent_messages")
        assert update["agent_messages"][-1]["status"] == "fail"
        assert update["agent_messages"][-1]["artifacts"]["error"] == "missing_agent_binding"


class TestImportRunsTheSameContractValidatorAsSave:
    """F31 regression: import must not create a workflow that cannot be saved.

    A bundle whose `for_each` used non-`state.` collect paths passed
    `POST /api/workflows/import/preflight` with `ok: true, blocking_count: 0` and
    imported successfully — then `PUT /api/workflows/{id}` rejected the identical
    definition with `workflow.step_for_each_collect_target_invalid`. Preflight only
    ran the step-config-key check; the control-flow step contract that the save path
    already applies is now run over the same definition.
    """

    @staticmethod
    def _bundle_with_invalid_for_each() -> str:
        """A bundle of the removed format: a loop Group the final validator refuses."""
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        bundle["workflows"][0]["groups"] = [
            {
                "group_id": "rows",
                "kind": "loop",
                "loop": {"over": "state.rows", "item_name": "row"},
                "body": {"kind": "sequence", "entry": {"kind": "step", "id": "plan"}},
            }
        ]
        return yaml.safe_dump(bundle, sort_keys=False)

    def test_preflight_blocks_a_definition_the_save_path_would_reject(self):
        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            self._bundle_with_invalid_for_each()
        )

        assert preflight["can_apply"] is False
        codes = {str(item.get("code") or "") for item in preflight["topology_issues"]}
        assert "workflow.group.loop_removed" in codes

    def test_apply_refuses_the_same_definition(self):
        _raises_callable_1622_1 = WorkflowPortabilityService(_resolved_db()).apply_import
        _raises_input_1622_2 = self._bundle_with_invalid_for_each()
        with pytest.raises(WorkflowPortabilityError) as excinfo:
            _raises_callable_1622_1(_raises_input_1622_2)

        assert excinfo.value.code in {
            "workflow_import_preflight_blocked",
            "workflow_import_topology_invalid",
        }

    def test_a_valid_definition_still_imports(self):
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle_text = WorkflowPortabilityService(source_db).export_yaml("manual_wf")

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(bundle_text)

        codes = {
            str((item.get("metadata") or {}).get("code") or "")
            for item in preflight["dependencies"]
        }
        assert "workflow.group.loop_removed" not in codes


class TestImportBoundaryContracts:
    @pytest.mark.parametrize("inputs", [["message"], [None], {}, "message"])
    def test_malformed_boundary_entry_blocks_import_before_writes(self, inputs):
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        bundle["workflows"][0]["steps"][0]["boundary_contract"] = {"entry": inputs}
        text = yaml.safe_dump(bundle, sort_keys=False)
        target = _resolved_db()
        service = WorkflowPortabilityService(target)
        preflight = service.preflight_import(text)
        assert preflight["can_apply"] is False
        issues = [item.get("metadata") or {} for item in preflight["dependencies"]]
        expected_code = (
            "contract.item_not_object" if isinstance(inputs, list) else "contract.items_not_list"
        )
        matching = [issue for issue in issues if issue.get("code") == expected_code]
        assert matching
        assert "steps.plan.boundary_contract.entry" in matching[0]["path"]
        before = deepcopy(target.workflows)
        with pytest.raises(WorkflowPortabilityError) as exc_info:
            service.apply_import(text)
        assert exc_info.value.code in {
            "workflow_import_preflight_blocked",
            "workflow_import_topology_invalid",
        }
        assert target.workflows == before


class TestImportPreflightTopology:
    """FLOW-001: preflight checked dependencies and never the shape of the graph.

    A bundle whose execution graph no valid workflow can have — a missing target,
    a cycle without rework, an edge out of a Stop — passed preflight and was stored
    as a disabled draft. The definition validator rejected it later, which is too
    late: the import had already reported success.
    """

    @staticmethod
    def _bundle_with(steps: list[dict], edges: list[dict], entry: str = "plan") -> str:
        source_db = _resolved_db(workflows={"manual_wf": _manual_workflow()})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        workflow = bundle["workflows"][0]
        workflow["steps"] = steps
        workflow["execution_graph"] = {
            "schema_version": 1,
            "entry": node(entry),
            "edges": edges,
        }
        return yaml.safe_dump(bundle, sort_keys=False)

    @classmethod
    def _missing_target(cls) -> str:
        return cls._bundle_with(
            [
                {"step_id": "plan", "step_kind": "set_variable"},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            [edge("plan", node("nowhere_at_all"))],
        )

    @classmethod
    def _edge_out_of_stop(cls) -> str:
        return cls._bundle_with(
            [
                {"step_id": "plan", "step_kind": "set_variable"},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            [edge("plan", node("done")), edge("done", node("plan"))],
        )

    @classmethod
    def _cycle_without_rework(cls) -> str:
        return cls._bundle_with(
            [
                {"step_id": "plan", "step_kind": "set_variable"},
                {"step_id": "again", "step_kind": "set_variable"},
            ],
            [edge("plan", node("again")), edge("again", node("plan"))],
        )

    def test_preflight_reports_a_missing_target(self):
        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            self._missing_target()
        )

        assert preflight["can_apply"] is False
        assert preflight["ok"] is False
        issues = preflight["topology_issues"]
        assert any(item["code"] == "workflow.graph.target_unknown" for item in issues)
        assert all(item["workflow_id"] == "manual_wf" for item in issues)

    def test_preflight_reports_an_edge_out_of_a_stop(self):
        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            self._edge_out_of_stop()
        )

        assert preflight["can_apply"] is False
        assert any(
            item["code"] == "workflow.graph.outcome_unsupported" and item["blocking"]
            for item in preflight["topology_issues"]
        )

    def test_preflight_blocks_a_cycle_without_rework(self):
        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(
            self._cycle_without_rework()
        )

        issue = next(
            item
            for item in preflight["topology_issues"]
            if item["code"] == "workflow.graph.cycle_without_rework"
        )
        assert issue["blocking"] is True
        assert preflight["can_apply"] is False

    def test_apply_refuses_and_persists_nothing(self):
        db = _resolved_db()

        service = WorkflowPortabilityService(db)
        bundle_text = self._missing_target()
        with pytest.raises(WorkflowPortabilityError) as excinfo:
            service.apply_import(bundle_text)

        assert excinfo.value.code == "workflow_import_topology_invalid"
        assert excinfo.value.errors, "the caller gets the same typed codes preflight reported"
        assert not db.save_calls, "an invalid import must not be stored, not even disabled"

    def test_an_unconnected_draft_still_imports(self):
        """A draft whose steps are not all connected yet is a normal import.

        Readiness enforces a runnable graph before the workflow is enabled or run;
        preflight reports the gap and lets it through.
        """
        bundle_text = self._bundle_with(
            [
                {"step_id": "plan", "step_kind": "set_variable"},
                {"step_id": "later", "step_kind": "set_variable"},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            [edge("plan", node("done"))],
        )

        preflight = WorkflowPortabilityService(_resolved_db()).preflight_import(bundle_text)

        reported = {item["code"] for item in preflight["topology_issues"]}
        assert {"workflow.graph.outcome_missing", "workflow.graph.unreachable"} <= reported
        assert all(item["blocking"] is False for item in preflight["topology_issues"])

    def test_a_phase_based_v1_bundle_is_refused_before_any_write(self):
        bundle = yaml.safe_load(
            WorkflowPortabilityService(
                _resolved_db(workflows={"manual_wf": _manual_workflow()})
            ).export_yaml("manual_wf")
        )
        bundle["schema_version"] = "platform.workflow_bundle.v1"
        db = _resolved_db()

        service = WorkflowPortabilityService(db)
        bundle_text = yaml.safe_dump(bundle)
        with pytest.raises(WorkflowPortabilityError) as excinfo:
            service.apply_import(bundle_text)

        assert excinfo.value.code == "bundle_schema_unsupported"
        assert excinfo.value.errors[0]["required_schema_version"] == "platform.workflow_bundle.v2"
        assert not db.save_calls


class _RegistryStoreWithoutInstallLookup:
    """What the admin routes actually hand the portability service.

    RegistryDBStore defines none of `get_extension_by_install_id`,
    `get_extension_install` or `get_install` — verified by execution, not by reading.
    """


class _AccountScopedInstallStore:
    """The integration store, which owns the lookup and honours the account filter."""

    def __init__(self, rows: dict[tuple[str, str], dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str | None]] = []

    def get_extension_by_install_id(self, install_id, *, account_id=None):
        self.calls.append((install_id, account_id))
        if account_id is None:
            # The real lookup skips its account filter here and would answer across
            # tenants. The service must never call it this way.
            raise AssertionError("install lookup was called without an account")
        return self.rows.get((install_id, account_id))


class TestExtensionInstallPreflight:
    """AC17-R67: an extension dependency could never resolve, on any deployment.

    The preflight probed three method names on the *workflow* store, and the store the
    admin routes pass has none of them. `_call_db_get` swallowed that into the same
    `None` a genuinely missing row produces, so an active local install, a disabled one
    and a foreign identifier were indistinguishable — all three reported
    `requires_extension_install`, and the log only ever said
    "Required extension install is missing."
    """

    ACCOUNT = "00000000-0000-0000-0000-000000000002"
    INSTALL = "edb03429-963f-4ccd-95c3-3a08ef1f60e2"

    def _service(self, *, account_id, rows=None):
        store = _AccountScopedInstallStore(
            rows if rows is not None else {(self.INSTALL, self.ACCOUNT): {"id": self.INSTALL}}
        )
        service = WorkflowPortabilityService(
            _RegistryStoreWithoutInstallLookup(),
            extension_install_store=store,
            account_id=account_id,
        )
        return service, store

    def test_resolves_an_install_owned_by_the_requesting_account(self):
        service, store = self._service(account_id=self.ACCOUNT)

        assert service._install_exists(self.INSTALL) is True
        assert store.calls == [(self.INSTALL, self.ACCOUNT)]

    def test_does_not_resolve_an_install_owned_by_another_account(self):
        service, _store = self._service(account_id="11111111-1111-1111-1111-111111111111")

        assert service._install_exists(self.INSTALL) is False

    def test_refuses_to_probe_at_all_without_an_account(self):
        # Asking the real lookup with account_id=None disables its account filter, turning
        # preflight into a cross-tenant existence oracle. Staying blocked is the safe way
        # to be wrong.
        service, store = self._service(account_id="")

        assert service._install_exists(self.INSTALL) is False
        assert store.calls == []

    def test_still_reports_a_genuinely_absent_install_as_missing(self):
        service, _store = self._service(account_id=self.ACCOUNT, rows={})

        assert service._install_exists("9204647a-11ef-4b71-a638-23efe1e69cc7") is False

    def test_the_store_the_admin_routes_pass_cannot_answer_on_its_own(self):
        # Without the install store wired in, this is the pre-fix behaviour, and it is why
        # every extension dependency stayed blocking.
        service = WorkflowPortabilityService(_RegistryStoreWithoutInstallLookup())

        assert service._install_exists(self.INSTALL) is False


class _OperatorInstallStore:
    """The account-scoped lookup an export can use, including disabled installs."""

    def __init__(self, rows: dict[tuple[str, str], dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def get_extension_install_for_operator(self, install_id, *, account_id):
        self.calls.append((install_id, account_id))
        return self.rows.get((install_id, account_id))


class TestPortableExtensionIdentity:
    """AC17-R60: an export named a dependency by an identifier only one machine can read.

    The owner exported a workflow on one machine and imported it on his server. The package
    carried `install_id: 9204647a-…` and, in the field meant for a human name, the same UUID.
    The import screen showed a UUID as the title, `extension / <same UUID>` as the subtitle,
    and an empty target list. The supervisor searched that id across five local installations
    and found it on none of them — it is unique to one installation on one machine.
    """

    ACCOUNT = "00000000-0000-0000-0000-000000000002"
    INSTALL = "9204647a-11ef-4b71-a638-23efe1e69cc7"

    def _service(self, rows):
        store = _OperatorInstallStore(rows)
        return WorkflowPortabilityService(
            _RegistryStoreWithoutInstallLookup(),
            extension_install_store=store,
            account_id=self.ACCOUNT,
        ), store

    def _item(self):
        return {
            "dependency_type": "extension",
            "ref": self.INSTALL,
            "required": True,
            "sources": ["image_processing.initialize_phase.connector_install_id"],
        }

    def test_export_carries_the_extension_identity_not_only_the_install_id(self):
        service, store = self._service(
            {
                (self.INSTALL, self.ACCOUNT): {
                    "extension_id": "acme.documents",
                    "install_id": self.INSTALL,
                    "state": "disabled",
                    "descriptor_jsonb": {"display_name": "Acme Documents", "version": "1.4.0"},
                }
            }
        )

        payload = service._dependency_snapshot(self._item())

        assert payload["extension_id"] == "acme.documents"
        assert payload["version"] == "1.4.0"
        assert payload["display_name"] == "Acme Documents"
        # the install id stays, as a local hint rather than as the identity
        assert payload["install_id"] == self.INSTALL
        assert store.calls == [(self.INSTALL, self.ACCOUNT)]

    def test_the_install_manifest_names_the_extension_when_there_is_no_descriptor(self):
        """The operator lookup returns the install's `manifest`, not `descriptor_jsonb`.

        Reading only the descriptor, a live export wrote the extension id where the human
        name belongs (`display_name: <extension id>`) and no version at all, so the
        catalog could show nothing better than a code name.
        """
        service, _store = self._service(
            {
                (self.INSTALL, self.ACCOUNT): {
                    "extension_id": "acme.documents",
                    "install_id": self.INSTALL,
                    "manifest": {
                        "extension_id": "acme.documents",
                        "display_name": "Acme Documents",
                        "version": "0.1.0",
                    },
                }
            }
        )

        payload = service._dependency_snapshot(self._item())

        assert payload["display_name"] == "Acme Documents"
        assert payload["version"] == "0.1.0"

    def test_a_disabled_install_is_still_described(self):
        """A fresh installation arrives with every extension disabled (AC17-R66).

        An active-only lookup would erase exactly the dependencies a new install has.
        """
        service, _store = self._service(
            {
                (self.INSTALL, self.ACCOUNT): {
                    "extension_id": "acme.storefront",
                    "state": "disabled",
                    "descriptor_jsonb": {"version": "2.0.0"},
                }
            }
        )

        payload = service._dependency_snapshot(self._item())

        assert payload["extension_id"] == "acme.storefront"
        assert payload["display_name"] == "acme.storefront"

    def test_the_human_field_never_repeats_the_opaque_identifier(self):
        """With nothing resolvable the name must not silently become the UUID again."""
        service, _store = self._service({})

        payload = service._dependency_snapshot(self._item())

        assert "extension_id" not in payload
        assert payload["install_id"] == self.INSTALL

    def test_other_dependency_types_are_untouched(self):
        service, store = self._service({})

        payload = service._dependency_snapshot(
            {"dependency_type": "agent", "ref": "scout", "required": True, "sources": []}
        )

        assert payload["agent_id"] == "scout"
        assert "extension_id" not in payload
        assert store.calls == []


class TestSourceExtensionIdentityInThePolicy:
    """B13: a protected workflow's header named its extension by an install UUID.

    The policy now carries the extension's id, display name and version, read
    from the install in the account that owns the workflow row.
    """

    ACCOUNT = "00000000-0000-0000-0000-000000000002"
    INSTALL = "4f054cc7-e9c4-44f4-9e44-6d90fc58f255"

    def _workflow(self, **overrides):
        return {
            **_manual_workflow("wf_ext"),
            "account_id": self.ACCOUNT,
            "source_install_id": self.INSTALL,
            "source_release_id": "pr_f576e7757790789ebbc24ba5",
            "projection_mode": "derived",
            **overrides,
        }

    def _service(self, rows, workflow=None):
        store = _OperatorInstallStore(rows)
        db = _PortabilityDB(workflows={"wf_ext": workflow or self._workflow()})
        return WorkflowPortabilityService(db, extension_install_store=store), store

    def _install(self):
        return {
            (self.INSTALL, self.ACCOUNT): {
                "extension_id": "acme.hook-sample",
                "install_id": self.INSTALL,
                "manifest": {
                    "extension_id": "acme.hook-sample",
                    "display_name": "Hook Sample",
                    "version": "1.0.1",
                },
            }
        }

    def test_policy_names_the_extension_and_its_version(self):
        service, store = self._service(self._install())

        policy = service.export_policy("wf_ext")

        assert policy["source_extension_id"] == "acme.hook-sample"
        assert policy["source_extension_name"] == "Hook Sample"
        assert policy["source_extension_version"] == "1.0.1"
        # The lookup is confined to the account that owns the workflow.
        assert store.calls == [(self.INSTALL, self.ACCOUNT)]

    def test_an_install_of_another_account_is_not_read(self):
        service, store = self._service(
            self._install(),
            workflow=self._workflow(account_id="11111111-1111-1111-1111-111111111111"),
        )

        policy = service.export_policy("wf_ext")

        assert policy["source_extension_id"] == ""
        assert policy["source_extension_name"] == ""
        assert policy["source_extension_version"] == ""
        assert store.calls == [(self.INSTALL, "11111111-1111-1111-1111-111111111111")]

    def test_no_human_field_holds_the_install_id(self):
        service, _store = self._service({})

        policy = service.export_policy("wf_ext")

        assert self.INSTALL not in (
            policy["source_extension_id"],
            policy["source_extension_name"],
            policy["source_extension_version"],
        )

    def test_one_lookup_per_install_for_a_whole_list(self):
        service, store = self._service(self._install())

        for _ in range(3):
            service.redact_protected_workflow(self._workflow())

        assert store.calls == [(self.INSTALL, self.ACCOUNT)]

    def test_a_manual_workflow_looks_nothing_up(self):
        store = _OperatorInstallStore(self._install())
        db = _PortabilityDB(workflows={"manual_wf": _manual_workflow()})

        policy = WorkflowPortabilityService(db, extension_install_store=store).export_policy(
            "manual_wf"
        )

        assert policy["source_extension_name"] == ""
        assert store.calls == []


class TestCatalogLogicalExtensionReference:
    """A catalog template names a connector by logical id; import must map it.

    The template bundle cannot carry a local install id — the same file is
    published to every installation. It carries a stable logical `extension_id`
    instead, and the catalog create adapter turns that into an ordinary
    portability resolution so the existing rewrite path performs the
    substitution. Nothing about the runtime workflow format changes, and manual
    YAML import (which never builds the adapter) is untouched.
    """

    LOGICAL_EXTENSION_ID = "acme_crm"
    LOCAL_INSTALL_ID = "inst_local_crm"

    class _Install:
        def __init__(self, extension_id, version, install_id, state="active"):
            self.extension_id = extension_id
            self.version = version
            self.install_id = install_id
            self.state = state

        def model_dump(self):
            return {
                "extension_id": self.extension_id,
                "version": self.version,
                "install_id": self.install_id,
                "state": self.state,
            }

    class _InstallStore:
        def __init__(self, *records):
            self.records = list(records)

        def list_for_account(self, account_id, **_kwargs):
            self.account_id = account_id
            return list(self.records)

    @classmethod
    def _catalog_source_workflow(cls) -> dict:
        """A publishable template: one connector reference, nothing account-local.

        A real template cannot ship a block key, child workflow or project
        connection that only exists on the authoring installation, so those are
        stripped here rather than left to block the import for unrelated
        reasons.
        """
        workflow = deepcopy(_manual_workflow())
        for step in workflow["steps"]:
            if step.get("step_kind") != "agent":
                continue
            for account_local_field in (
                "block_key",
                "workflow_id",
                "connection_binding_jsonb",
            ):
                step.pop(account_local_field, None)
            step["override"] = {"connector_install_id": cls.LOGICAL_EXTENSION_ID}
        return workflow

    @classmethod
    def _catalog_bundle_yaml(cls) -> str:
        """Export a real bundle that names its connector by logical id."""
        source_workflow = cls._catalog_source_workflow()
        source_db = _resolved_db(workflows={"manual_wf": source_workflow})
        bundle = yaml.safe_load(WorkflowPortabilityService(source_db).export_yaml("manual_wf"))
        bundle["workflows"][0]["workflow_id"] = "catalog_wf"
        return yaml.safe_dump(bundle, sort_keys=False)

    @classmethod
    def _service(cls, store):
        from core.application.workflows.workflow_template_catalog_create import (
            InstallAwareWorkflowDb,
            WorkflowTemplateCatalogCreateService,
        )

        target_db = _resolved_db()
        # The production workflow store cannot answer install lookups; the
        # catalog path supplies that one answer from the install store.
        portability = WorkflowPortabilityService(
            InstallAwareWorkflowDb(
                target_db,
                get_install_store=lambda: store,
                account_id="acc_7",
            )
        )
        bundle_yaml = cls._catalog_bundle_yaml()
        service = WorkflowTemplateCatalogCreateService(
            portability=portability,
            get_install_store=lambda: store,
            load_index=lambda: _StubCatalogIndex(),
            fetch_artifact=lambda *_args, **_kwargs: bundle_yaml.encode("utf-8"),
        )
        return service, target_db

    def test_an_installed_extension_is_substituted_into_the_saved_workflow(self):
        store = self._InstallStore(
            self._Install(self.LOGICAL_EXTENSION_ID, "1.2.0", self.LOCAL_INSTALL_ID)
        )
        service, target_db = self._service(store)

        result = service.create(
            "tpl",
            author="alice",
            owner_account_id="acc_7",
            resolutions=_reuse_definitions(self._catalog_bundle_yaml()),
        )

        assert result["workflow_id"] == "catalog_wf"
        saved = target_db.save_calls[-1]["data"]
        overrides = [step.get("override", {}) for step in saved.get("steps", [])]
        assert any(
            override.get("connector_install_id") == self.LOCAL_INSTALL_ID for override in overrides
        ), "the logical extension id was not rewritten to the local install id"
        assert all(
            override.get("connector_install_id") != self.LOGICAL_EXTENSION_ID
            for override in overrides
        ), "the temporary logical reference survived into the stored workflow"

    def test_an_exported_workflow_is_mapped_by_its_portable_extension_id(self):
        """A plain export names its extension by the author's install UUID.

        AC17-R60 made the export carry `extension_id` beside that UUID, but the import
        dropped the field and the catalog looked the UUID up as a logical id, so a
        workflow exported on one machine could never be created from the catalog on
        another. Only the logical id is portable; it must be what is resolved.
        """
        foreign_install_id = "a5ed0a41-daab-43fe-be29-050dafef3f1a"
        bundle = yaml.safe_load(
            self._catalog_bundle_yaml().replace(self.LOGICAL_EXTENSION_ID, foreign_install_id)
        )
        for row in bundle["dependencies"]["extensions"]:
            row["extension_id"] = self.LOGICAL_EXTENSION_ID
        store = self._InstallStore(
            self._Install(self.LOGICAL_EXTENSION_ID, "1.2.0", self.LOCAL_INSTALL_ID)
        )
        service, target_db = self._service(store)
        exported_yaml = yaml.safe_dump(bundle, sort_keys=False)
        service._fetch_artifact = lambda *_args, **_kwargs: exported_yaml.encode("utf-8")

        result = service.create(
            "tpl",
            author="alice",
            owner_account_id="acc_7",
            resolutions=_reuse_definitions(exported_yaml),
        )

        saved = target_db.save_calls[-1]["data"]
        install_ids = {
            step.get("override", {}).get("connector_install_id") for step in saved.get("steps", [])
        }
        assert self.LOCAL_INSTALL_ID in install_ids
        assert foreign_install_id not in install_ids
        assert [row["extension_id"] for row in result["catalog_extension_resolutions"]] == [
            self.LOGICAL_EXTENSION_ID
        ]

    def test_a_missing_extension_blocks_create_and_writes_nothing(self):
        store = self._InstallStore()
        service, target_db = self._service(store)

        with pytest.raises(WorkflowPortabilityError) as raised:
            service.create(
                "tpl",
                author="alice",
                owner_account_id="acc_7",
                resolutions=_reuse_definitions(self._catalog_bundle_yaml()),
            )

        assert raised.value.code == "workflow_import_preflight_blocked"
        assert target_db.save_calls == []

    def test_an_existing_workflow_id_is_never_overwritten(self):
        """Catalog create reuses the import conflict rule rather than replacing."""
        store = self._InstallStore(
            self._Install(self.LOGICAL_EXTENSION_ID, "1.2.0", self.LOCAL_INSTALL_ID)
        )
        service, target_db = self._service(store)
        existing = {"workflow_id": "catalog_wf", "display_name": "Mine", "enabled": True}
        target_db.workflows["catalog_wf"] = dict(existing)

        with pytest.raises(WorkflowPortabilityError) as raised:
            service.create(
                "tpl",
                author="alice",
                owner_account_id="acc_7",
                resolutions=_reuse_definitions(self._catalog_bundle_yaml()),
            )

        assert raised.value.code in {
            "workflow_id_conflict",
            "workflow_import_preflight_blocked",
        }
        assert target_db.workflows["catalog_wf"] == existing
        assert target_db.save_calls == []

    def test_a_renamed_target_creates_a_second_independent_draft(self):
        store = self._InstallStore(
            self._Install(self.LOGICAL_EXTENSION_ID, "1.2.0", self.LOCAL_INSTALL_ID)
        )
        service, target_db = self._service(store)
        existing = {"workflow_id": "catalog_wf", "display_name": "Mine", "enabled": True}
        target_db.workflows["catalog_wf"] = dict(existing)

        result = service.create(
            "tpl",
            target_workflow_id="catalog_wf_copy",
            author="alice",
            owner_account_id="acc_7",
            resolutions=_reuse_definitions(self._catalog_bundle_yaml()),
        )

        assert result["workflow_id"] == "catalog_wf_copy"
        assert target_db.workflows["catalog_wf"] == existing
        assert target_db.workflows["catalog_wf_copy"]["enabled"] is False

    def test_manual_import_of_the_same_bundle_still_blocks(self):
        """The adapter is what maps logical ids; plain import must not start to."""
        store = self._InstallStore(
            self._Install(self.LOGICAL_EXTENSION_ID, "1.2.0", self.LOCAL_INSTALL_ID)
        )
        _service, target_db = self._service(store)

        preflight = WorkflowPortabilityService(target_db).preflight_import(
            self._catalog_bundle_yaml()
        )

        blocked = [
            row
            for row in preflight["dependencies"]
            if row["dependency_type"] == "extension" and row["blocking"]
        ]
        assert blocked, "manual import must still report the unresolved extension"


class TestStepNamingItsExtensionWithoutInstall:
    """A connector step that names its extension by package id and carries no install.

    Live on 2026-10-15: a File Transfer workflow imported with preflight ok/can_apply,
    then its test run failed the suppressed upload with connection_unavailable and a
    real run with external_effect_install_identity_required. Preflight never looked at
    connector_id, so the step had no extension dependency at all.
    """

    EXTENSION_ID = "example.file-transfer"
    INSTALL_ID = "inst-file-transfer"
    ACCOUNT = "acc_7"

    _Install = TestCatalogLogicalExtensionReference._Install
    _InstallStore = TestCatalogLogicalExtensionReference._InstallStore

    @classmethod
    def _bundle_yaml(cls, connector_id: str, *, tools: dict | None = None) -> str:
        workflow = linear_workflow(
            {
                "step_id": "upload",
                "step_kind": "connector",
                "connector_id": connector_id,
                "input_mapping_jsonb": {"remote_path": "literal:/out/report.csv"},
            },
            workflow_id="upload_wf",
            version="1.0.0",
            display_name="Upload",
            projection_mode="manual",
        )
        source_db = _resolved_db(workflows={"upload_wf": workflow})
        source_db.tools.update(tools or {})
        return WorkflowPortabilityService(source_db).export_yaml("upload_wf")

    @classmethod
    def _portability(cls, store, *, tools: dict | None = None):
        from core.application.workflows.workflow_template_catalog_create import (
            InstallAwareWorkflowDb,
        )

        target_db = _resolved_db()
        target_db.tools.update(tools or {})
        portability = WorkflowPortabilityService(
            InstallAwareWorkflowDb(
                target_db, get_install_store=lambda: store, account_id=cls.ACCOUNT
            )
        )
        return portability, target_db

    @classmethod
    def _resolve(cls, portability, store, bundle_yaml):
        from core.application.workflows.workflow_template_catalog_create import (
            resolve_portable_extensions,
        )

        return resolve_portable_extensions(
            portability,
            bundle_yaml,
            target_workflow_id="",
            resolutions=[],
            account_id=cls.ACCOUNT,
            get_install_store=lambda: store,
        )

    def _extension_rows(self, preflight: dict) -> list[dict]:
        return [
            row
            for row in preflight["dependencies"]
            if row["dependency_type"] == "extension" and row["ref"] == self.EXTENSION_ID
        ]

    @pytest.mark.parametrize(
        "connector_id",
        [f"{EXTENSION_ID}.upload_file", f"extension.{EXTENSION_ID}"],
    )
    def test_import_binds_the_step_to_the_accounts_install(self, connector_id):
        store = self._InstallStore(self._Install(self.EXTENSION_ID, "0.2.0", self.INSTALL_ID))
        portability, target_db = self._portability(store)
        bundle_yaml = self._bundle_yaml(connector_id)

        unresolved = portability.preflight_import(bundle_yaml)
        merged, outcomes, _discovery = self._resolve(portability, store, bundle_yaml)
        resolved = portability.preflight_import(bundle_yaml, resolutions=merged)
        portability.apply_import(bundle_yaml, resolutions=merged, owner_account_id=self.ACCOUNT)

        [row] = self._extension_rows(unresolved)
        assert unresolved["can_apply"] is False
        assert row["status"] == "requires_extension_install"
        assert "without an install" in row["message"]
        assert [outcome["install_id"] for outcome in outcomes] == [self.INSTALL_ID]
        assert resolved["can_apply"] is True, resolved["dependencies"]
        [saved_step] = [
            step
            for step in target_db.save_calls[-1]["data"]["steps"]
            if step["step_id"] == "upload"
        ]
        assert saved_step["connector_install_id"] == self.INSTALL_ID
        assert saved_step["connector_id"] == connector_id

    def test_import_blocks_when_the_account_has_no_install(self):
        store = self._InstallStore()
        portability, target_db = self._portability(store)
        bundle_yaml = self._bundle_yaml(f"{self.EXTENSION_ID}.upload_file")

        merged, _outcomes, discovery = self._resolve(portability, store, bundle_yaml)

        assert discovery["can_apply"] is False
        assert self._extension_rows(discovery)[0]["blocking"] is True
        with pytest.raises(WorkflowPortabilityError) as raised:
            portability.apply_import(bundle_yaml, resolutions=merged, owner_account_id=self.ACCOUNT)
        assert raised.value.code == "workflow_import_preflight_blocked"
        assert target_db.save_calls == []

    def test_a_registry_tool_with_a_dotted_id_is_not_taken_for_an_extension(self):
        tools = {"acme.tools.lookup": {"tool_id": "acme.tools.lookup", "display_name": "Lookup"}}
        portability, _target_db = self._portability(self._InstallStore(), tools=tools)

        preflight = portability.preflight_import(
            self._bundle_yaml("acme.tools.lookup", tools=tools)
        )

        assert not [
            row for row in preflight["dependencies"] if row["dependency_type"] == "extension"
        ]


class _StubCatalogIndex:
    """The one catalog entry the create adapter looks up in these tests."""

    def item(self, item_id):
        from core.application.workflows.workflow_template_catalog import (
            WorkflowTemplateCatalogItem,
        )

        return WorkflowTemplateCatalogItem(
            item_id=str(item_id),
            version="1.0.0",
            name="Catalog template",
            summary="A template that names its connector logically.",
            description="",
            categories=(),
            tags=(),
            integrations=(),
            trigger_types=(),
            setup_requirements=(),
            workflow_yaml_url="https://catalog.example.test/tpl-1.0.0.yaml",
            workflow_yaml_bytes=1,
            workflow_yaml_sha256="a" * 64,
            publication_status="published",
            verification_status="verified",
            bundle_schema="platform.workflow_bundle.v2",
        )


def test_extension_dependency_can_map_to_existing_install_without_installing():
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    db = _PortabilityDB(
        installs={
            "installed": {"install_id": "installed", "enabled": True, "account_id": "account"}
        }
    )
    parsed = {
        "schema_version": SCHEMA_VERSION,
        "kind": "workflow_bundle",
        "metadata": {},
        "workflows": [{"workflow_id": "draft", "steps": [], "groups": []}],
        "dependencies": {},
        "import_hints": {},
    }
    parsed["dependencies"] = {"extensions": [{"install_id": "portable"}]}
    store = _AccountScopedInstallStore({("installed", "account"): db.installs["installed"]})
    result = WorkflowPortabilityService(
        db, account_id="account", extension_install_store=store
    ).preflight_import(
        yaml.safe_dump(parsed),
        resolutions=[
            {
                "dependency_type": "extension",
                "ref": "portable",
                "action": "map_existing",
                "target_ref": "installed",
            }
        ],
    )
    assert result["can_apply"] is True
    assert db.save_calls == []


def test_portability_mapping_rewrites_connector_ref_consumed_by_persistence():
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    db = _PortabilityDB(tools={"installed_tool": {"tool_id": "installed_tool", "enabled": True}})
    portability = WorkflowPortabilityService(db)
    workflow = {
        "workflow_id": "mapped",
        "steps": [
            {
                "step_id": "call",
                "step_kind": "connector",
                "connector_id": "source_tool",
                "override": {"connector": {"operation_id": "send"}},
            }
        ],
        "groups": [],
    }
    resolutions = [
        {
            "dependency_type": "tool",
            "ref": "source_tool",
            "action": "map_existing",
            "target_ref": "installed_tool",
        }
    ]
    payload = portability._import_payload(
        workflow, "mapped", portability._resolution_index(resolutions)
    )
    # The bulk persistence path reads this column, not a Builder proposal ref.
    assert payload["steps"][0]["connector_id"] == "installed_tool"


@pytest.mark.parametrize(
    "kind,ref",
    [
        ("agent", "extension.install-copy.copywriter"),
        ("skill", "extension.install-copy.write_copy"),
    ],
)
def test_projection_dependency_lookup_uses_explicit_account(kind, ref):
    from tests.unit.test_workflow_authoring_catalog import CatalogDB, install_projection_store

    db = CatalogDB()
    store = install_projection_store(db, account_id="account-projected")
    service = WorkflowPortabilityService(db, account_id=" account-projected ")
    dependency = {"dependency_type": kind, "ref": ref, "required": True}
    result = service._classify_dependency(dependency, {})
    assert result["status"] == "resolved"
    assert service._existing_row_for_dependency(kind, ref)["account_id"] == "account-projected"
    assert set(store.reads) == {"account-projected"}
    for account in ["foreign", ""]:
        denied = WorkflowPortabilityService(db, account_id=account)
        assert denied._classify_dependency(dependency, {})["status"] == "requires_mapping"
        assert denied._existing_row_for_dependency(kind, ref) is None
    store.active = False
    assert service._classify_dependency(dependency, {})["status"] == "requires_mapping"


@pytest.mark.parametrize(
    "marker",
    [
        {"source_install_id": "install-copy"},
        {"metadata_jsonb": {"projection_bridge": {"install_id": "install-copy"}}},
        {"validation_result_jsonb": {"projection_bridge": {"install_id": "install-copy"}}},
    ],
)
@pytest.mark.parametrize("kind", ["agent", "skill"])
def test_projection_bridge_dependency_requires_actual_account_authority(marker, kind):
    from tests.unit.test_workflow_authoring_catalog import CatalogDB

    db = CatalogDB()
    identifier = "stored-bridge"
    getattr(db, f"{kind}s")[identifier] = {f"{kind}_id": identifier, **marker}
    service = WorkflowPortabilityService(db)
    assert service._dependency_exists(kind, identifier) is False
    assert service._existing_row_for_dependency(kind, identifier) is None


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled": False},
        {"visible_in_catalog": False},
        {"deleted_at": "2026-10-01"},
        {"new_runs_blocked": True},
        {"availability_status": "unavailable"},
        {"extension_dependency_availability": {"new_runs_blocked": True}},
    ],
)
def test_projection_dependencies_preserve_availability_denials(updates):
    from tests.unit.test_workflow_authoring_catalog import (
        PROJECTED_AGENT,
        PROJECTED_SKILL,
        CatalogDB,
        install_projection_store,
    )

    db = CatalogDB()
    store = install_projection_store(db, account_id="account-projected")
    for row in store.rows:
        row["projection"].update(updates)
    service = WorkflowPortabilityService(db, account_id="account-projected")
    for kind, ref in [("agent", PROJECTED_AGENT), ("skill", PROJECTED_SKILL)]:
        assert (
            service._classify_dependency({"dependency_type": kind, "ref": ref}, {})["status"]
            == "requires_mapping"
        )
        assert service._existing_row_for_dependency(kind, ref) is None


def _mode_import_workflow(workflow_id, *, has_hook=False, child_id=None):
    from tests.unit.test_workflow_hook_contract import declaration

    if has_hook:
        hook = declaration(
            input_schema={"type": "object", "properties": {}},
            input_mapping={},
            mutable_fields=[],
            additional_outputs={},
        )
        steps = [
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": hook}},
            }
        ]
    elif child_id:
        steps = [
            {
                "step_id": "call",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": child_id}},
            }
        ]
    else:
        steps = []
    payload = linear_workflow(*steps, workflow_id=workflow_id, enabled=False)
    payload["execution_graph"] = payload["settings"].pop("execution_graph")
    return payload


def _mode_import_bundle(workflows):
    return yaml.safe_dump(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "workflow_bundle",
            "metadata": {"source_workflow_id": workflows[0]["workflow_id"]},
            "workflows": workflows,
            "dependencies": {},
            "import_hints": {},
        }
    )


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("location", ["root", "renamed_root", "created_carried_child"])
def test_imported_hook_definition_requires_development(monkeypatch, mode, location):
    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    workflows = (
        [
            _mode_import_workflow("root", child_id="child"),
            _mode_import_workflow("child", has_hook=True),
        ]
        if location == "created_carried_child"
        else [_mode_import_workflow("root", has_hook=True)]
    )
    text = _mode_import_bundle(workflows)
    db = _PortabilityDB()
    before = deepcopy((db.workflows, db.connections, db.project_workflow_bindings))
    portability = WorkflowPortabilityService(db)
    target = "renamed" if location == "renamed_root" else ""
    preflight = portability.preflight_import(text, target_workflow_id=target)
    if mode == "production":
        assert preflight["can_apply"] is False, preflight
        issues = [
            issue
            for issue in preflight["topology_issues"]
            if issue.get("code") == "workflow.hook.declaration_requires_development"
        ]
        assert issues
        assert all(
            issue["blocking"] and issue["severity"] == "error" and issue["path"] for issue in issues
        )
        with pytest.raises(WorkflowPortabilityError) as raised:
            portability.apply_import(text, target_workflow_id=target, owner_project_id="project")
        assert raised.value.code == "workflow_import_topology_invalid"
        assert raised.value.status_code == 400
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            for issue in raised.value.errors
        )
        assert db.save_calls == []
        workflows_before, connections_before, bindings_before = before
        assert db.workflows == workflows_before
        assert db.connections == connections_before
        assert db.project_workflow_bindings == bindings_before
    else:
        assert preflight["can_apply"] is True, preflight
        portability.apply_import(text, target_workflow_id=target, owner_project_id="project")
        assert set(db.workflows) == (
            {"root", "child"} if location == "created_carried_child" else {target or "root"}
        )
        assert all(row["enabled"] is False for row in db.workflows.values())
        assert len(db.save_calls) == len(workflows)


def test_production_may_reuse_existing_hook_owner_without_creating_a_declaration(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    child = _mode_import_workflow("child", has_hook=True)
    db = _PortabilityDB(workflows={"child": deepcopy(child)})
    before = deepcopy(db.workflows["child"])
    text = _mode_import_bundle([_mode_import_workflow("root", child_id="child"), child])
    resolutions = [
        {
            "dependency_type": "workflow",
            "ref": "child",
            "action": "use_existing",
            "target_ref": "child",
        }
    ]
    portability = WorkflowPortabilityService(db)
    preflight = portability.preflight_import(text, resolutions=resolutions)
    assert preflight["can_apply"] is True, preflight
    portability.apply_import(text, resolutions=resolutions, owner_project_id="project")
    assert [call["data"]["workflow_id"] for call in db.save_calls] == ["root"]
    assert db.workflows["child"] == before


@pytest.mark.parametrize("mode", ["production", "development"])
def test_development_never_unlocks_protected_core_authoring(monkeypatch, mode):
    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    protected = _mode_import_workflow("protected", has_hook=True)
    protected.update(
        source_install_id="trusted-install",
        source_release_id="release-1",
        projection_mode="derived",
    )
    db = _PortabilityDB(workflows={"protected": protected})
    portability = WorkflowPortabilityService(db)
    for operation, code in (
        (
            lambda: portability.assert_structure_change_allowed("protected", action="edit"),
            "workflow_internal_structure_protected",
        ),
        (lambda: portability.assert_clone_allowed("protected"), "workflow_clone_protected"),
        (
            lambda: portability.assert_restore_allowed("protected", entry_id="11"),
            "workflow_internal_structure_protected",
        ),
    ):
        with pytest.raises(WorkflowPortabilityError) as raised:
            operation()
        assert raised.value.code == code
        assert raised.value.status_code == 403
    assert db.save_calls == []
    assert db.workflows == {"protected": protected}


def _b06_workflow(workflow_id, *, child_id=None):
    steps = (
        [{"step_id": "call_child", "step_kind": "invoke_workflow", "workflow_id": child_id}]
        if child_id
        else []
    )
    return linear_workflow(
        *steps,
        workflow_id=workflow_id,
        enabled=False,
        account_id=_B06_ACCOUNT,
        scope_kind="account",
        projection_mode="manual",
        version="1.0.0",
    )


def _b06_save(store, workflow_id, *, child_id=None, author="B06 fixture"):
    payload = _b06_workflow(workflow_id, child_id=child_id)
    payload["execution_graph"] = payload["settings"].pop("execution_graph")
    return _b06_create_definition(store, payload, author=author)


def _b06_bundle(root_id, *, child_id=None):
    workflows = {root_id: _b06_workflow(root_id, child_id=child_id)}
    if child_id:
        workflows[child_id] = _b06_workflow(child_id)
    # Only the export source is an existing lightweight bundle fixture. Every
    # target read/write/refusal below uses RegistryDBStore and real PostgreSQL.
    return WorkflowPortabilityService(_PortabilityDB(workflows=workflows)).export_yaml(
        root_id, include_children=bool(child_id)
    )


def _b06_stored(observer, workflow_id):
    row = observer.execute(
        "SELECT to_jsonb(w) FROM registry_workflows w WHERE workflow_id=%s", (workflow_id,)
    ).fetchone()
    steps = observer.execute(
        "SELECT to_jsonb(s) FROM registry_workflow_steps s "
        "WHERE owner_workflow_id=%s ORDER BY step_id",
        (workflow_id,),
    ).fetchall()
    return (row[0] if row else None, [item[0] for item in steps])


@pytest.fixture
def b06_import_audit(audit_db):
    store, observer, base_id, project_id = audit_db
    competitor = _B06RegistryDBStore(store.dsn)
    allocated = {}
    owned_stores = []

    def allocate(label):
        assert label not in allocated
        allocated[label] = f"{base_id}_{label}"
        return allocated[label]

    try:
        integration = _b06_create_integration_store(store.dsn)
        owned_stores.append(integration)
        projects = _b06_create_project_store(store.dsn)
        owned_stores.append(projects)
        service = WorkflowPortabilityService(
            store,
            project_connection_store=integration,
            extension_install_store=integration,
            account_id=_B06_ACCOUNT,
            import_transaction_factory=_b06_partial(_b06_import_transaction, store, integration),
        )
        yield _B06Namespace(
            store=store,
            observer=observer,
            competitor=competitor,
            integration=integration,
            projects=projects,
            service=service,
            allocate=allocate,
            project_id=str(project_id),
            account_id=_B06_ACCOUNT,
        )
    finally:
        # Explicit fixture IDs only; never remove another suite's rows or pools.
        observer.rollback()
        ids = list(allocated.values())
        if ids:
            observer.execute(
                "DELETE FROM project_workflows WHERE project_id=%s AND workflow_id=ANY(%s)",
                (project_id, ids),
            )
            observer.execute(
                "DELETE FROM registry_changelog WHERE entity_type='workflow' AND entity_id=ANY(%s)",
                (ids,),
            )
            observer.execute("DELETE FROM registry_workflows WHERE workflow_id=ANY(%s)", (ids,))
        for owned in owned_stores:
            direct = getattr(owned, "_direct_conn", None)
            if direct is not None:
                direct.close()
        if competitor._pool is None and competitor._conn is not None:
            competitor._conn.close()


def _b06_compete_before_create(monkeypatch, audit, target_id, *, before_create=None):
    original = audit.store.save_workflow
    race = {"committed": None, "refused": None, "calls": 0}

    def save(data, author="admin", *, create_only=False, expected_revision=None):
        if data["workflow_id"] == target_id and create_only and not race["calls"]:
            race["calls"] += 1
            assert audit.competitor is not audit.store
            assert audit.store._get_tx_conn() is not None
            assert audit.competitor._get_tx_conn() is None
            if before_create is not None:
                before_create()
            _b06_save(audit.competitor, target_id, author="B06 competing writer")
            race["committed"] = _b06_stored(audit.observer, target_id)
            assert race["committed"][0] is not None
        try:
            return original(
                data, author=author, create_only=create_only, expected_revision=expected_revision
            )
        except _B06WorkflowCreateConflictError as exc:
            # Record the real INSERT rowcount refusal; do not synthesize it.
            race["refused"] = exc
            raise

    monkeypatch.setattr(audit.store, "save_workflow", save)
    return race


def test_explicit_occupied_root_target_cannot_be_verified_by_free_rename(b06_import_audit):
    audit = b06_import_audit
    source = audit.allocate("explicit_source")
    occupied = audit.allocate("explicit_occupied")
    free = audit.allocate("explicit_free")
    _b06_save(audit.store, source)
    _b06_save(audit.store, occupied)
    before_source = _b06_stored(audit.observer, source)
    before_occupied = _b06_stored(audit.observer, occupied)
    bundle = _b06_bundle(source)
    resolutions = [
        {
            "dependency_key": f"workflow:{source}",
            "dependency_type": "workflow",
            "ref": source,
            "action": "rename_workflow",
            "target_workflow_id": free,
        }
    ]

    preflight = audit.service.preflight_import(
        bundle, target_workflow_id=occupied, resolutions=resolutions
    )
    assert preflight["can_apply"] is False
    conflict = next(item for item in preflight["dependencies"] if item["ref"] == source)
    assert conflict["status"] == "conflict"
    assert conflict["blocking"] is True
    assert conflict["metadata"]["target_workflow_id"] == occupied

    with pytest.raises(WorkflowPortabilityError) as refusal:
        audit.service.apply_import(
            bundle,
            target_workflow_id=occupied,
            resolutions=resolutions,
            owner_account_id=audit.account_id,
            owner_project_id=audit.project_id,
        )
    assert refusal.value.code == "workflow_import_preflight_blocked"
    assert _b06_stored(audit.observer, source) == before_source
    assert _b06_stored(audit.observer, occupied) == before_occupied
    assert _b06_stored(audit.observer, free) == (None, [])


def test_trashed_workflow_identity_requires_explicit_fresh_rename(b06_import_audit):
    audit = b06_import_audit
    reserved = audit.allocate("reserved")
    active = audit.allocate("active")
    fresh = audit.allocate("fresh")
    _b06_save(audit.store, reserved)
    _b06_save(audit.store, active)
    audit.store.delete_workflow(reserved, author="B06 fixture trash")
    before = _b06_stored(audit.observer, reserved)
    assert before[0]["deleted_at"] is not None
    assert audit.store.get_workflow(reserved) is None
    bundle = _b06_bundle(reserved)
    assert yaml.safe_load(bundle)["import_hints"]["conflict_policy"] == "rename_workflow"
    assert audit.service.preflight_import(bundle, target_workflow_id=active)["can_apply"] is False

    preflight = audit.service.preflight_import(bundle)
    assert preflight["can_apply"] is False
    conflict = next(item for item in preflight["dependencies"] if item["ref"] == reserved)
    assert conflict["status"] == "conflict"
    assert "reserved" in conflict["message"].lower()
    assert "trash" in conflict["message"].lower()
    assert "fresh" in conflict["message"].lower()
    assert "rename_workflow" in conflict["resolution_actions"]
    with pytest.raises(WorkflowPortabilityError) as refusal:
        audit.service.apply_import(
            bundle, owner_account_id=audit.account_id, owner_project_id=audit.project_id
        )
    assert refusal.value.status_code == 400
    assert refusal.value.code == "workflow_import_preflight_blocked"
    assert _b06_stored(audit.observer, reserved) == before

    result = audit.service.apply_import(
        bundle,
        target_workflow_id=fresh,
        owner_account_id=audit.account_id,
        owner_project_id=audit.project_id,
    )
    assert result["workflow_id"] == fresh
    imported = audit.store.get_workflow(fresh)
    assert imported["enabled"] is False
    assert str(imported["account_id"]) == audit.account_id
    assert _b06_stored(audit.observer, reserved) == before
    assert audit.observer.execute(
        "SELECT enabled FROM project_workflows WHERE project_id=%s AND workflow_id=%s",
        (audit.project_id, fresh),
    ).fetchone() == (True,)


@pytest.mark.parametrize("trashed", [False, True], ids=["active", "trashed"])
def test_carried_workflow_reuse_requires_active_identity(b06_import_audit, trashed):
    audit = b06_import_audit
    root = audit.allocate("reuse_root")
    child = audit.allocate("reuse_child")
    _b06_save(audit.store, child)
    if trashed:
        audit.store.delete_workflow(child, author="B06 fixture trash")
    before = _b06_stored(audit.observer, child)
    bundle = _b06_bundle(root, child_id=child)
    resolutions = [
        {
            "dependency_key": f"workflow:{child}",
            "action": "use_existing",
            "target_ref": child,
        }
    ]
    preflight = audit.service.preflight_import(bundle, resolutions=resolutions)
    if trashed:
        assert preflight["can_apply"] is False
        with pytest.raises(WorkflowPortabilityError) as refusal:
            audit.service.apply_import(
                bundle,
                resolutions=resolutions,
                owner_account_id=audit.account_id,
                owner_project_id=audit.project_id,
            )
        assert refusal.value.status_code == 400
        assert refusal.value.code == "workflow_import_preflight_blocked"
        assert audit.store.get_workflow(root) is None
    else:
        assert preflight["can_apply"] is True
        result = audit.service.apply_import(
            bundle,
            resolutions=resolutions,
            owner_account_id=audit.account_id,
            owner_project_id=audit.project_id,
        )
        assert result["reused_workflows"] == [{"workflow_id": child, "source_workflow_id": child}]
        assert audit.store.get_workflow(root)["steps"][0]["workflow_id"] == child
    assert _b06_stored(audit.observer, child) == before


def test_later_atomic_import_conflict_rolls_back_earlier_carried_workflow_and_binding(
    b06_import_audit, monkeypatch
):
    audit = b06_import_audit
    root = audit.allocate("rollback_root")
    child = audit.allocate("rollback_child")
    bundle = _b06_bundle(root, child_id=child)
    assert audit.service.preflight_import(bundle)["can_apply"] is True
    seen = []

    def assert_child_is_inside_transaction():
        with audit.store._get_tx_conn().cursor() as cursor:
            cursor.execute("SELECT 1 FROM registry_workflows WHERE workflow_id=%s", (child,))
            assert cursor.fetchone() is not None
            cursor.execute(
                "SELECT 1 FROM project_workflows WHERE project_id=%s AND workflow_id=%s",
                (audit.project_id, child),
            )
            assert cursor.fetchone() is not None
        assert _b06_stored(audit.observer, child) == (None, [])
        seen.append(child)

    race = _b06_compete_before_create(
        monkeypatch, audit, root, before_create=assert_child_is_inside_transaction
    )
    with pytest.raises(WorkflowPortabilityError) as refusal:
        audit.service.apply_import(
            bundle, owner_account_id=audit.account_id, owner_project_id=audit.project_id
        )
    assert refusal.value.status_code == 409
    assert refusal.value.code == "workflow_id_conflict"
    assert seen == [child]
    assert race["calls"] == 1
    assert isinstance(race["refused"], _B06WorkflowCreateConflictError)
    assert _b06_stored(audit.observer, child) == (None, [])
    assert _b06_stored(audit.observer, root) == race["committed"]
    assert (
        audit.observer.execute(
            "SELECT workflow_id FROM project_workflows WHERE project_id=%s AND workflow_id=ANY(%s)",
            (audit.project_id, [root, child]),
        ).fetchall()
        == []
    )
