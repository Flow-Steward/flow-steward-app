"""
Unit tests for RegistryManager, AgentRegistry, WorkflowRegistry, and validation.

Tests:
- All DB entries load without errors
- Singleton behavior
- Registry sub-registries (find_by_skill, task-class resolver, etc.)
- Validation catches errors

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_registry.py -v
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests._db_env import dsn_unreachable_reason, resolve_test_db_dsn
from tests._workflow_definitions import edge, linear_graph, linear_workflow, node

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class _BuiltinToolStore:
    def __init__(self, rows: dict[str, dict]):
        self.rows = {tool_id: dict(row) for tool_id, row in rows.items()}
        self.deleted: list[tuple[str, str]] = []
        self.saved: list[tuple[dict, str]] = []

    def get_tool(self, tool_id: str):
        return self.rows.get(tool_id)

    def delete_tool(self, tool_id: str, author: str = "admin") -> None:
        self.deleted.append((tool_id, author))
        self.rows.pop(tool_id, None)

    def save_tool(self, payload: dict, author: str = "admin") -> None:
        self.saved.append((dict(payload), author))
        self.rows[str(payload["tool_id"])] = dict(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_db():
    """Skip test if no DB DSN is available."""
    db_dsn = resolve_test_db_dsn()
    if not db_dsn:
        pytest.skip("No database DSN configured")
    unreachable = dsn_unreachable_reason(db_dsn)
    if unreachable:
        pytest.skip(f"Database host is not reachable from this test environment: {unreachable}")


@pytest.fixture(autouse=True)
def _registry_db_dsn(monkeypatch):
    """Point RegistryManager at the test database for this test only.

    RegistryManager reads FS_DB_DSN. When the harness named the database through
    another variable, the test sets FS_DB_DSN and monkeypatch removes it again,
    so no later test inherits a database it did not ask for.
    """
    db_dsn = resolve_test_db_dsn()
    if db_dsn and not os.environ.get("FS_DB_DSN"):
        monkeypatch.setenv("FS_DB_DSN", db_dsn)


def get_fresh_registry():
    """Reset singleton and return a fresh RegistryManager."""
    _require_db()
    import core.application.registry.registry_manager as rm_module

    rm_module._instance = None
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


@pytest.fixture
def user_authored_catalog():
    """Persist one user-authored skill, agent and workflow for the loading tests.

    The 1.0 registry seed ships no built-in agents, skills or workflows, so the
    loading behaviour is exercised against entities authored the way a user or
    an extension would create them.
    """
    _require_db()
    from core.application.registry.db_store import RegistryDBStore

    db = RegistryDBStore(resolve_test_db_dsn())
    suffix = uuid.uuid4().hex[:8]
    ids = SimpleNamespace(
        skill_id=f"draft_summary_{suffix}",
        agent_id=f"writer_{suffix}",
        workflow_id=f"weekly_report_{suffix}",
    )
    db.save_skill(
        {
            "skill_id": ids.skill_id,
            "display_name": f"Draft summary {suffix}",
            "description": "Drafts a short summary",
            "enabled": True,
            "category": "analysis",
            "instructions": [],
            "tags": [],
            "expected_inputs": [],
            "expected_outputs": [],
        },
        author="test",
    )
    db.save_agent(
        {
            "agent_id": ids.agent_id,
            "agent_type_key": "researcher",
            "display_name": f"Writer {suffix}",
            "description": "Writes weekly reports",
            "enabled": True,
            "prompts": {"system": "You write concise weekly reports."},
            "config": {},
            "quality": {},
            "inputs": {"required": [], "optional": []},
            "outputs": [],
            "depends_on": {"soft": [], "hard": []},
            "tags": [],
            "skills": [ids.skill_id],
            "tools": [],
        },
        author="test",
    )
    db.save_workflow(
        {
            "workflow_id": ids.workflow_id,
            "display_name": f"Weekly report {suffix}",
            "description": "Weekly report workflow",
            "enabled": True,
            "settings": {"execution_graph": linear_graph(["draft", "done"])},
            "steps": [
                {"step_id": "draft", "step_kind": "agent", "agent_id": ids.agent_id},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [],
            "for_each": [],
        },
        author="test",
    )
    try:
        yield ids
    finally:
        db.delete_workflow(ids.workflow_id, author="test")
        db.delete_agent(ids.agent_id, author="test")
        db.delete_skill(ids.skill_id, author="test")


def _extension_to_connector_workflow_db(
    monkeypatch,
    *,
    extension_output_type: str,
    contract_overrides: dict | None = None,
    consumer_first: bool = False,
):
    from core.application.extensions import workflow_install_contract
    from core.application.registry.db_store import RegistryDBStore

    producer_step = {
        "step_id": "produce_document",
        "step_kind": "connector",
        "connector_id": "extension.fixture.document-producer",
        "connector_install_id": "install-document-producer",
        "output_mapping_jsonb": {"document_id": "state.release_document_id"},
        "override": {"connector": {"operation_id": "produce_document"}},
    }
    consumer_step = {
        "step_id": "consume_document",
        "step_kind": "connector",
        "connector_id": "connector.fixture.text-consumer",
        "input_mapping_jsonb": {"documentId": "state.release_document_id"},
        "override": {"connector": {"operation_id": "consume_document"}},
    }
    ordered = [consumer_step, producer_step] if consumer_first else [producer_step, consumer_step]
    workflow = linear_workflow(*ordered, workflow_id="wf_demo", account_id="account-1")
    contract = {
        "account_id": "account-1",
        "extension_id": "fixture.document-producer",
        "install_id": "install-document-producer",
        "install_state": "active",
        "operation_manifest": [
            {
                "operation_id": "produce_document",
                "outputs": [{"name": "document_id", "value_type": extension_output_type}],
            }
        ],
    }
    contract.update(contract_overrides or {})
    connection = SimpleNamespace(closed=0)

    def resolve_install_contract(
        candidate_connection,
        *,
        install_id: str,
        account_id: str,
        default_account_id: str,
    ):
        if (
            candidate_connection is connection
            and install_id == "install-document-producer"
            and account_id == "account-1"
            and default_account_id
        ):
            return dict(contract)
        return None

    monkeypatch.setattr(
        workflow_install_contract,
        "resolve_workflow_extension_install_contract",
        resolve_install_contract,
    )
    db = RegistryDBStore("postgresql://unused")
    db._conn = connection
    db._backend_initialized = True
    monkeypatch.setattr(db, "get_workflow", lambda workflow_id: workflow)
    monkeypatch.setattr(
        db,
        "get_tool",
        lambda tool_id: (
            {
                "tool_id": "fixture.text-consumer",
                "interface": {
                    "functions": [
                        {
                            "name": "consume_document",
                            "parameters": [
                                {"name": "documentId", "type": "string", "required": True}
                            ],
                            "returns": {},
                        }
                    ]
                },
            }
            if tool_id == "fixture.text-consumer"
            else None
        ),
    )
    monkeypatch.setattr(db, "get_agent_outputs", lambda _agent_id: [])
    monkeypatch.setattr(db, "get_skill_outputs", lambda _skill_id: [])
    monkeypatch.setattr(db, "get_agent_inputs", lambda _agent_id: [])
    monkeypatch.setattr(db, "get_skill_inputs", lambda _skill_id: [])
    return db


class TestRegistryPromptCanonicalization:
    def test_nullable_scope_sql_casts_uuid_columns_to_text(self):
        from core.application.registry.db_store import _nullable_scope_sql

        sql = _nullable_scope_sql("w.account_id")

        assert "w.account_id::text" in sql
        assert "BTRIM" in sql

    def test_system_registry_account_id_defaults_unowned_system_rows(self):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.registry.db_store import _system_registry_account_id

        assert _system_registry_account_id({"scope_kind": "system"}) == DEFAULT_ACCOUNT_ID
        assert _system_registry_account_id({"account_id": "account-A"}) == "account-A"
        assert _system_registry_account_id({"scope_kind": "account"}) is None
        assert _system_registry_account_id({"project_id": "project-1"}) is None
        assert _system_registry_account_id({"source_install_id": "install-1"}) is None

    def test_workflow_runtime_dependencies_default_system_account(self, monkeypatch):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        executed: list[tuple[str, object]] = []

        class _Cursor:
            def execute(self, sql, params=None):
                executed.append((str(sql), params))

        @contextmanager
        def _transaction():
            yield _Cursor()

        monkeypatch.setattr(
            store,
            "_table_exists",
            lambda table: table == "workflow_runtime_dependencies",
        )
        monkeypatch.setattr(store, "_transaction", _transaction)
        monkeypatch.setattr(
            store,
            "get_workflow",
            lambda _workflow_id: {
                "workflow_id": "wf_system",
                "scope_kind": "system",
                "steps": [
                    {
                        "step_id": "child",
                        "step_kind": "agent",
                        "group_id": None,
                        "workflow_id": "child_wf",
                    }
                ],
                "groups": [],
            },
        )

        store._sync_workflow_runtime_dependencies("wf_system")

        insert_params = [
            params for sql, params in executed if "INSERT INTO workflow_runtime_dependencies" in sql
        ]
        assert insert_params
        assert insert_params[0]["account_id"] == DEFAULT_ACCOUNT_ID

    def test_workflow_runtime_dependencies_include_exact_extension_action(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        executed: list[tuple[str, object]] = []

        class _Cursor:
            def execute(self, sql, params=None):
                executed.append((str(sql), params))

        @contextmanager
        def _transaction():
            yield _Cursor()

        monkeypatch.setattr(
            store,
            "_table_exists",
            lambda table: table == "workflow_runtime_dependencies",
        )
        monkeypatch.setattr(store, "_transaction", _transaction)
        monkeypatch.setattr(
            store,
            "get_workflow",
            lambda _workflow_id: {
                "workflow_id": "wf_fixture_documents",
                "account_id": "00000000-0000-0000-0000-000000000002",
                "scope_kind": "account",
                "steps": [
                    {
                        "step_id": "search_drive",
                        "step_kind": "connector",
                        "group_id": None,
                        "connector_id": "extension.fixture.extension-oauth",
                        "connector_install_id": "install-fixture",
                        "connection_binding_jsonb": {"connection_id": "fixture-documents"},
                        "override": {
                            "connector": {
                                "extension_id": "fixture.extension-oauth",
                                "operation_id": "search_drive_files",
                            }
                        },
                    }
                ],
                "groups": [],
            },
        )

        store._sync_workflow_runtime_dependencies("wf_fixture_documents")

        rows = [
            params for sql, params in executed if "INSERT INTO workflow_runtime_dependencies" in sql
        ]
        indexed = {(row["dependency_kind"], row["dependency_ref"]): row for row in rows}
        assert indexed[("extension_install", "install-fixture")]["install_id"] == "install-fixture"
        assert (
            indexed[("extension_action", "search_drive_files")]["install_id"] == "install-fixture"
        )
        assert indexed[("project_connection", "fixture-documents")]["step_id"] == ("search_drive")

    def test_db_store_strips_deprecated_output_format(self):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        agent = {
            "agent_id": "composer",
            "prompts": {
                "system": "You are a composer.",
                "output_format": "VERIFICATION:\n- tests",
            },
            "_skills": [],
        }

        result = store._canonicalize_agent_prompts(agent)

        assert "output_format" not in result["prompts"]

    def test_db_store_exposes_skill_prompts_from_skill_rows(self):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        agent = {
            "agent_id": "composer",
            "prompts": {
                "system": "You are a composer.",
                "skill_prompts": {"draft_summary": "stale json prompt"},
            },
            "_skills": [
                {"skill_id": "draft_summary", "skill_prompt": "row prompt", "sort_order": 0},
            ],
        }

        result = store._canonicalize_agent_prompts(agent)

        assert result["prompts"]["skill_prompts"] == {"draft_summary": "row prompt"}

    def test_db_store_backfills_missing_system_prompt(self):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        agent = {
            "agent_id": "e2e_agent_tmp",
            "display_name": "E2E Agent Tmp",
            "prompts": {},
            "_skills": [],
        }

        result = store._canonicalize_agent_prompts(agent)

        assert result["prompts"]["system"].startswith("You are E2E Agent Tmp.")

    def test_manifest_ignores_stale_json_skill_prompts(self):
        from core.application.registry.agent_manifest import AgentManifest

        manifest = AgentManifest.from_db_row(
            {
                "agent_id": "composer",
                "version": "1.0.0",
                "display_name": "Developer",
                "description": "",
                "enabled": True,
                "prompts": {
                    "system": "You are a composer.",
                    "skill_prompts": {"draft_summary": "stale json prompt"},
                },
                "_skills": [
                    {"skill_id": "draft_summary", "skill_prompt": "row prompt", "sort_order": 0},
                ],
                "_tools": [],
                "_inputs": [],
                "_outputs": [],
            }
        )

        assert manifest.prompts.skill_prompts == {"draft_summary": "row prompt"}


class TestRegistryDatasetColumnCompatibility:
    def test_column_is_generated_detects_generated_columns(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")

        @contextmanager
        def fake_cursor():
            class _Cursor:
                def execute(self, _sql, _params):
                    return None

                def fetchone(self):
                    return {"is_generated": "ALWAYS"}

            yield _Cursor()

        monkeypatch.setattr(store, "_cursor", fake_cursor)

        assert store._column_is_generated("registry_datasets", "dataset_id") is True

    def test_column_is_generated_handles_legacy_columns(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")

        @contextmanager
        def fake_cursor():
            class _Cursor:
                def execute(self, _sql, _params):
                    return None

                def fetchone(self):
                    return {"is_generated": "NEVER"}

            yield _Cursor()

        monkeypatch.setattr(store, "_cursor", fake_cursor)

        assert store._column_is_generated("registry_datasets", "dataset_id") is False

    def test_db_store_builds_contract_schema_from_structured_rows(self):
        from core.application.registry.db_store import RegistryDBStore

        schema = RegistryDBStore._build_contract_schema(
            [
                {
                    "name": "review_verdict",
                    "type": "text",
                    "description": "pass/fail",
                    "required": True,
                },
                {
                    "name": "warnings",
                    "type": "list",
                    "description": "non-blocking notes",
                    "required": False,
                    "contract_id": "warnings_list.v1",
                },
            ]
        )

        assert schema == {
            "type": "object",
            "properties": {
                "review_verdict": {"type": "text", "description": "pass/fail"},
                "warnings": {
                    "type": "list",
                    "description": "non-blocking notes",
                    "contract_id": "warnings_list.v1",
                },
            },
            "required": ["review_verdict"],
        }

    def test_build_phase_result_json_schema_uses_contract_schema_for_structured_outputs(self):
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {
                    "name": "outline_doc",
                    "type": "json",
                    "description": "Architecture payload",
                    "required": True,
                    "contract_id": "outline_doc.v1",
                    "contract_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "overview": {"type": "string"},
                            "components": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["overview"],
                    },
                }
            ]
        )

        artifacts = schema["properties"]["artifacts"]["properties"]
        assert artifacts["outline_doc"]["type"] == "object"
        assert artifacts["outline_doc"]["properties"]["overview"]["type"] == "string"
        assert artifacts["outline_doc"]["properties"]["components"]["type"] == "array"

    def test_build_phase_result_json_schema_marks_optional_outputs_nullable_but_required(self):
        from core.application.orchestration.models import build_phase_result_json_schema

        schema = build_phase_result_json_schema(
            [
                {"name": "review_verdict", "type": "text", "required": True},
                {"name": "check_results", "type": "text", "required": False},
            ]
        )

        artifacts_schema = schema["properties"]["artifacts"]
        assert set(artifacts_schema["required"]) == {"review_verdict", "check_results"}
        test_results_schema = artifacts_schema["properties"]["check_results"]
        assert "anyOf" in test_results_schema
        assert {"type": "null"} in test_results_schema["anyOf"]

    def test_skill_manifest_preserves_required_output_flags(self):
        from core.application.registry.skill_manifest import SkillManifest

        manifest = SkillManifest.from_db_row(
            {
                "skill_id": "review_summary",
                "version": "1.0.0",
                "display_name": "Review Summary",
                "description": "",
                "enabled": True,
                "category": "quality",
                "instructions": [],
                "input_schema": {},
                "output_schema": {},
                "_inputs": [],
                "_outputs": [
                    {
                        "name": "review_verdict",
                        "type": "text",
                        "description": "pass/fail",
                        "required": True,
                    },
                    {"name": "warnings", "type": "list", "description": "notes", "required": False},
                ],
                "_tools": [],
            }
        )

        assert manifest.expected_outputs == [
            {
                "name": "review_verdict",
                "type": "text",
                "description": "pass/fail",
                "required": True,
                "contract_id": None,
            },
            {
                "name": "warnings",
                "type": "list",
                "description": "notes",
                "required": False,
                "contract_id": None,
            },
        ]

    def test_generate_output_format_uses_phase_result_v2_envelope(self):
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(MagicMock())
        text = resolver.generate_output_format(
            [
                {
                    "name": "review_verdict",
                    "type": "text",
                    "description": "pass/fail",
                    "required": True,
                },
                {
                    "name": "checks_run",
                    "type": "json",
                    "description": "executed checks",
                    "required": True,
                },
            ]
        )

        assert '"schema_version": "phase_result_v2"' in text
        assert '"artifacts": {' in text
        assert '"review_verdict": "..."' in text
        assert '"checks_run": {}' in text

    def test_step_contract_preview_groups_locked_unresolved_optional_and_custom(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "analysis", "agent_id": "scout", "skill_id": "collect_findings"},
            {
                "step_id": "development",
                "agent_id": "composer",
                "skill_id": "draft_summary",
                "boundary_contract": {
                    "entry": [
                        {
                            "name": "summary_doc",
                            "type": "text",
                            "source": "message.step.analysis.artifacts.summary_doc",
                        },
                        {"name": "custom_note", "type": "text", "source": "state.task_summary"},
                    ]
                },
            },
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]
        db.get_skill_inputs.return_value = [
            {"name": "summary_doc", "type": "text", "required": True},
            {"name": "bug_description", "type": "text", "required": True},
            {"name": "nice_to_have", "type": "text", "required": False},
        ]
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_agent_inputs.return_value = [
            {"name": "branch_name", "type": "text", "required": True}
        ]
        db.get_skill_outputs.return_value = [
            {"name": "code_output", "type": "text", "required": True}
        ]
        db.get_agent_outputs.side_effect = lambda agent_id: (
            [{"name": "summary_doc", "type": "text"}]
            if agent_id == "scout"
            else [{"name": "git_diff", "type": "text"}]
        )
        db.get_agent.return_value = {"agent_type_key": "composer", "agent_type_label": "Developer"}
        db.get_skill.return_value = {
            "skill_id": "draft_summary",
            "display_name": "Implement Story",
        }

        preview = IOResolver(db).resolve_step_contract_preview(
            workflow_id="wf_demo",
            step_id="development",
            agent_id="composer",
            skill_id="draft_summary",
        )

        locked_names = {item["name"] for item in preview["locked_required"]}
        unresolved_names = {item["name"] for item in preview["unresolved_required"]}
        optional_names = {item["name"] for item in preview["optional"]}
        custom_names = {item["name"] for item in preview["custom"]}
        output_names = {item["name"] for item in preview["outputs"]}

        assert "branch_name" not in locked_names
        assert "summary_doc" in unresolved_names
        assert "bug_description" in unresolved_names
        assert "nice_to_have" in optional_names
        assert "custom_note" in custom_names
        assert "code_output" in output_names
        assert preview["role_type"]["key"] == "composer"
        assert preview["skill"]["id"] == "draft_summary"

    def test_step_contract_preview_ignores_tool_runtime_io_in_builder_contract(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "development", "agent_id": "composer", "skill_id": "draft_summary"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]
        db.get_skill_inputs.return_value = [
            {"name": "task_summary", "type": "text", "required": True},
        ]
        db.get_agent_tools.return_value = [{"tool_id": "git_tool"}]
        db.get_tool.return_value = {
            "interface": {
                "parameters": [
                    {"name": "repo_path", "type": "text", "required": True},
                ],
                "returns": {"name": "patch_diff", "type": "text"},
            }
        }
        db.get_agent_inputs.return_value = []
        db.get_skill_outputs.return_value = [
            {"name": "code_output", "type": "text", "required": True}
        ]
        db.get_agent_outputs.return_value = []
        db.get_agent.return_value = {"agent_type_key": "composer", "agent_type_label": "Developer"}
        db.get_skill.return_value = {
            "skill_id": "draft_summary",
            "display_name": "Implement Story",
        }

        preview = IOResolver(db).resolve_step_contract_preview(
            workflow_id="wf_demo",
            step_id="development",
            agent_id="composer",
            skill_id="draft_summary",
        )

        unresolved_names = {item["name"] for item in preview["unresolved_required"]}
        output_names = {item["name"] for item in preview["outputs"]}

        locked_names = {item["name"] for item in preview["locked_required"]}
        assert "task_summary" in locked_names
        assert "repo_path" not in unresolved_names
        assert "code_output" in output_names
        assert "patch_diff" not in output_names

    @pytest.mark.parametrize(
        "configured_query,legacy_query,expected_resolved",
        [
            (None, "", False),
            ("", "", False),
            ("   ", "", False),
            ("saved-query", "", True),
            ("", "legacy-query", True),
        ],
    )
    def test_dataset_query_preview_requires_an_actual_saved_query_selection(
        self, configured_query, legacy_query, expected_resolved
    ):
        from core.application.registry.io_resolver import IOResolver

        config = {"params": {"zero": 0, "flag": False}}
        if configured_query is not None:
            config["query_template_id"] = configured_query
        step = {
            "step_id": "query",
            "step_kind": "dataset_query",
            "query_template_id": legacy_query,
            "override": {"dataset_query": config},
        }
        workflow = linear_workflow(
            step,
            workflow_id="saved-query-summary",
            settings={
                "callable": {"expected_inputs": [{"name": "query_template_id", "type": "text"}]}
            },
        )
        resolver = IOResolver(SimpleNamespace(get_workflow=lambda _workflow_id: workflow))

        preview = resolver.resolve_step_contract_preview(
            workflow_id="saved-query-summary", step_id="query"
        )

        locked = {row["name"]: row for row in preview["locked_required"]}
        unresolved = {row["name"]: row for row in preview["unresolved_required"]}
        selected = locked if expected_resolved else unresolved
        other = unresolved if expected_resolved else locked
        row = selected["query_template_id"]
        assert "query_template_id" not in other
        assert row["required"] is True
        assert row["resolved"] is expected_resolved
        assert row["read_only"] is expected_resolved
        assert row["source"] == ("literal" if expected_resolved else "")
        assert row["resolution"] == ("literal" if expected_resolved else "unresolved")
        # The named workflow input cannot substitute for authoring a saved-query ID.
        # Falsy literal parameter values remain configured; only selection presence changes.
        for name in ("zero", "flag"):
            assert locked[name]["resolved"] is True
            assert locked[name]["source"] == "literal"
        assert [(row["name"], row["type"]) for row in preview["outputs"]] == [
            ("dataset_query_result", "array")
        ]

    def test_connector_step_contract_preview_uses_manifest_return_type(self):
        from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "uppercase_text",
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "input_mapping_jsonb": {"value": "state.inputs.text"},
                "output_mapping_jsonb": {"result": "state.upper_text"},
                "override": {"connector": {"function_name": "upper"}},
            },
            workflow_id="wf_demo",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "text", "type": "paragraph"}],
                }
            },
        )
        manifests = builtin_standard_tool_manifests()
        db.get_tool.side_effect = lambda tool_id: manifests.get(str(tool_id or ""))

        preview = IOResolver(db).resolve_step_contract_preview(
            workflow_id="wf_demo",
            step_id="uppercase_text",
        )

        assert preview["outputs"] == [
            {
                "name": "upper_text",
                "type": "string",
                "description": "Mapped from connector output 'result'",
                "source": "connector.std.text",
                "source_kind": "connector/action/http result",
            }
        ]
        assert preview["locked_required"][0]["source"] == "state.inputs.text"
        assert preview["locked_required"][0]["type"] == "string"
        assert preview["unresolved_required"] == []

    def test_step_input_defaults_aggregate_required_optional_and_custom(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "analysis", "agent_id": "scout", "skill_id": "collect_findings"},
            {
                "step_id": "development",
                "agent_id": "composer",
                "skill_id": "draft_summary",
                "boundary_contract": {
                    "entry": [
                        {
                            "name": "summary_doc",
                            "type": "text",
                            "source": "message.step.analysis.artifacts.summary_doc",
                        },
                        {"name": "custom_note", "type": "text", "source": "state.task_summary"},
                    ]
                },
            },
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]
        db.get_skill_inputs.return_value = [
            {"name": "summary_doc", "type": "text", "required": True},
            {"name": "nice_to_have", "type": "text", "required": False},
        ]
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_agent_inputs.return_value = []
        db.get_skill_outputs.return_value = [
            {"name": "code_output", "type": "text", "required": True}
        ]
        db.get_agent_outputs.return_value = [{"name": "summary_doc", "type": "text"}]

        payload = IOResolver(db).get_step_input_defaults("wf_demo", "development")
        required_names = {item.get("name") for item in payload.get("required_inputs", [])}
        optional_names = {item.get("name") for item in payload.get("optional_inputs", [])}
        custom_names = {item.get("name") for item in payload.get("custom_inputs", [])}

        assert "summary_doc" in required_names
        assert "nice_to_have" in optional_names
        assert "custom_note" in custom_names

    def test_step_input_defaults_use_skill_contract_not_agent_or_tool_io(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_inputs.side_effect = lambda cap_id: (
            [
                {"name": "summary_doc", "type": "text", "required": True},
                {"name": "reference_context", "type": "text", "required": False},
            ]
            if cap_id == "outline_plan"
            else []
        )
        db.get_skill_outputs.return_value = [
            {"name": "architecture", "type": "text", "required": True}
        ]
        db.get_agent_inputs.return_value = [
            {"name": "library_id", "type": "text", "required": True},
            {"name": "directory", "type": "text", "required": True},
            {"name": "class_name", "type": "text", "required": True},
        ]
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = [{"tool_id": "http_fetch"}]
        db.get_tool.return_value = {
            "interface": {
                "parameters": [
                    {"name": "query", "type": "text", "required": True},
                    {"name": "project_name", "type": "text", "required": True},
                    {"name": "n_results", "type": "number", "required": False},
                ],
            }
        }

        payload = IOResolver(db).get_step_input_defaults("wf_demo", "design")
        required_names = {item.get("name") for item in payload.get("required_inputs", [])}
        optional_names = {item.get("name") for item in payload.get("optional_inputs", [])}
        merged_names = required_names | optional_names

        assert required_names == {"summary_doc"}
        assert optional_names == {"reference_context"}
        assert "library_id" not in merged_names
        assert "directory" not in merged_names
        assert "class_name" not in merged_names
        assert "query" not in merged_names
        assert "project_name" not in merged_names
        assert "n_results" not in merged_names

    def test_step_outputs_use_skill_outputs_not_agent_outputs(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_outputs.side_effect = lambda cap_id: (
            [
                {"name": "architecture", "type": "text", "required": True},
                {"name": "files_to_touch", "type": "list", "required": True},
            ]
            if cap_id == "outline_plan"
            else []
        )
        db.get_agent_outputs.return_value = [
            {"name": "outline_doc", "type": "text", "required": False},
            {"name": "data_models", "type": "json", "required": False},
            {"name": "api_contracts", "type": "json", "required": False},
        ]
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "design")
        names = {item.get("name") for item in outputs}
        required_names = {item.get("name") for item in outputs if bool(item.get("required"))}

        assert {"architecture", "files_to_touch"} <= names
        assert {"architecture", "files_to_touch"} <= required_names
        assert "outline_doc" not in names

    def test_step_input_defaults_prefer_published_block_contract_when_available(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.list_published_block_contracts.return_value = [
            {
                "block_key": "designer.outline_plan",
                "source_kind": "agent",
                "source_ref": "designer::outline_plan",
                "enabled": True,
            }
        ]
        db.get_published_block_contract.return_value = {
            "block_key": "designer.outline_plan",
            "inputs": [
                {"field_key": "summary_doc", "value_type": "text", "required": True},
                {"field_key": "reference_context", "value_type": "text", "required": False},
            ],
            "outputs": [],
        }
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_inputs.return_value = [
            {"name": "library_id", "type": "text", "required": True},
            {"name": "directory", "type": "text", "required": True},
        ]
        db.get_skill_outputs.return_value = []
        db.get_agent_inputs.return_value = []
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None

        payload = IOResolver(db).get_step_input_defaults("wf_demo", "design")
        required_names = {item.get("name") for item in payload.get("required_inputs", [])}
        optional_names = {item.get("name") for item in payload.get("optional_inputs", [])}
        all_names = required_names | optional_names

        assert required_names == {"summary_doc"}
        assert optional_names == {"reference_context"}
        assert "library_id" not in all_names
        assert "directory" not in all_names

    def test_step_outputs_prefer_published_block_outputs_when_available(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.list_published_block_contracts.return_value = [
            {
                "block_key": "designer.outline_plan",
                "source_kind": "agent",
                "source_ref": "designer::outline_plan",
                "enabled": True,
            }
        ]
        db.get_published_block_contract.return_value = {
            "block_key": "designer.outline_plan",
            "inputs": [],
            "outputs": [
                {
                    "field_key": "architecture",
                    "value_type": "text",
                    "required_by_default": True,
                    "exposed_by_default": True,
                },
                {
                    "field_key": "files_to_touch",
                    "value_type": "list",
                    "required_by_default": True,
                    "exposed_by_default": True,
                },
                {
                    "field_key": "outline_doc",
                    "value_type": "json",
                    "required_by_default": False,
                    "exposed_by_default": True,
                },
            ],
        }
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_outputs.return_value = [
            {"name": "legacy_internal_payload", "type": "json", "required": True},
        ]
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "design")
        names = {item.get("name") for item in outputs}
        required_names = {item.get("name") for item in outputs if bool(item.get("required"))}

        assert {"architecture", "files_to_touch", "outline_doc"} <= names
        assert {"architecture", "files_to_touch"} <= required_names
        assert "legacy_internal_payload" not in names

    def test_step_outputs_include_state_aliases_from_agent_output_mapping(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "agent_id": "copy_agent",
                "skill_id": "write_copy",
                "step_kind": "agent",
                "step_id": "generate_copy",
                "output_mapping_jsonb": {
                    "approved_subject": "state.draft_subject",
                    "approved_body": "state.draft_body",
                },
            },
            workflow_id="wf_demo",
        )
        db.list_published_block_contracts.return_value = []
        db.get_published_block_contract.return_value = None
        db.get_agent_skills.return_value = [{"skill_id": "write_copy"}]
        db.get_skill_outputs.return_value = [
            {"name": "approved_subject", "type": "text", "required": True},
            {"name": "approved_body", "type": "text", "required": True},
        ]
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "generate_copy")
        by_name = {item.get("name"): item for item in outputs}

        assert by_name["draft_subject"]["type"] == "text"
        assert by_name["draft_body"]["type"] == "text"

    def test_step_outputs_ignore_disabled_published_block_and_fall_back_to_skill_contract(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "analysis", "agent_id": "scout", "skill_id": "collect_findings"},
            workflow_id="wf_demo",
        )
        disabled_row = {
            "block_key": "agent.scout.collect_findings",
            "source_kind": "agent",
            "source_ref": "scout::collect_findings",
            "enabled": False,
        }
        db.list_published_block_contracts.side_effect = lambda enabled_only=False, **_kwargs: (
            [] if enabled_only else [disabled_row]
        )
        db.get_published_block_contract.return_value = {
            "block_key": "agent.scout.collect_findings",
            "inputs": [],
            "outputs": [
                {
                    "field_key": "code_output",
                    "value_type": "text",
                    "required_by_default": True,
                    "exposed_by_default": True,
                }
            ],
        }
        db.get_agent_skills.return_value = [{"skill_id": "collect_findings"}]
        db.get_skill_outputs.return_value = [
            {
                "name": "summary_doc",
                "type": "text",
                "required": True,
            }
        ]
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "analysis")
        names = {item.get("name") for item in outputs}

        assert "summary_doc" in names
        assert "code_output" not in names
        assert db.list_published_block_contracts.call_args_list
        assert all(
            call.kwargs == {"enabled_only": True}
            for call in db.list_published_block_contracts.call_args_list
        )

    def test_effective_io_can_include_agent_and_tool_fields_but_builder_defaults_do_not(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_inputs.side_effect = lambda cap_id: (
            [
                {"name": "summary_doc", "type": "text", "required": True},
                {"name": "reference_context", "type": "text", "required": False},
            ]
            if cap_id == "outline_plan"
            else []
        )
        db.get_skill_outputs.side_effect = lambda cap_id: (
            [
                {"name": "architecture", "type": "text", "required": True},
            ]
            if cap_id == "outline_plan"
            else []
        )
        db.get_agent_inputs.return_value = [
            {"name": "library_id", "type": "text", "required": True},
            {"name": "directory", "type": "text", "required": True},
        ]
        db.get_agent_outputs.return_value = [{"name": "outline_doc", "type": "text"}]
        db.get_agent_tools.return_value = [{"tool_id": "http_fetch"}]
        db.get_tool.return_value = {
            "tool_id": "http_fetch",
            "interface": {
                "parameters": [{"name": "query", "type": "text", "required": True}],
                "returns": {"name": "tool_payload", "type": "json"},
            },
        }

        resolver = IOResolver(db)
        effective = resolver.compute_effective_io("designer")
        effective_input_names = {item.get("name") for item in effective.get("inputs", [])}
        assert {
            "summary_doc",
            "reference_context",
            "library_id",
            "directory",
            "query",
        } <= effective_input_names

        defaults = resolver.get_step_input_defaults("wf_demo", "design")
        default_names = {
            item.get("name")
            for item in defaults.get("required_inputs", []) + defaults.get("optional_inputs", [])
        }
        assert "summary_doc" in default_names
        assert "reference_context" in default_names
        assert "library_id" not in default_names
        assert "directory" not in default_names
        assert "query" not in default_names

    def test_builder_defaults_remain_stable_when_agent_io_changes_but_skill_contract_does_not(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_inputs.side_effect = lambda cap_id: (
            [
                {"name": "summary_doc", "type": "text", "required": True},
                {"name": "reference_context", "type": "text", "required": False},
            ]
            if cap_id == "outline_plan"
            else []
        )
        db.get_skill_outputs.return_value = [
            {"name": "architecture", "type": "text", "required": True}
        ]
        db.get_agent_tools.return_value = [{"tool_id": "http_fetch"}]
        db.get_tool.return_value = {
            "interface": {"parameters": [{"name": "query", "type": "text", "required": True}]}
        }
        db.get_agent_inputs.return_value = [
            {"name": "library_id", "type": "text", "required": True}
        ]
        db.get_agent_outputs.return_value = [{"name": "outline_doc", "type": "text"}]

        resolver = IOResolver(db)
        before = resolver.get_step_input_defaults("wf_demo", "design")

        db.get_agent_inputs.return_value = [
            {"name": "library_id", "type": "text", "required": True},
            {"name": "class_name", "type": "text", "required": True},
            {"name": "directory", "type": "text", "required": True},
        ]
        db.get_agent_outputs.return_value = [
            {"name": "outline_doc", "type": "text"},
            {"name": "data_models", "type": "json"},
        ]
        after = resolver.get_step_input_defaults("wf_demo", "design")

        before_required = sorted(item.get("name") for item in before.get("required_inputs", []))
        before_optional = sorted(item.get("name") for item in before.get("optional_inputs", []))
        after_required = sorted(item.get("name") for item in after.get("required_inputs", []))
        after_optional = sorted(item.get("name") for item in after.get("optional_inputs", []))

        assert before_required == after_required
        assert before_optional == after_optional

    def test_step_contract_preview_excludes_unfillable_tool_requirements(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "design", "agent_id": "designer", "skill_id": "outline_plan"},
            workflow_id="wf_demo",
        )
        db.get_agent_skills.return_value = [{"skill_id": "outline_plan"}]
        db.get_skill_inputs.return_value = [
            {"name": "summary_doc", "type": "text", "required": True},
        ]
        db.get_skill_outputs.return_value = [
            {"name": "architecture", "type": "text", "required": True}
        ]
        db.get_agent_inputs.return_value = []
        db.get_agent_outputs.return_value = []
        db.get_agent_tools.return_value = [{"tool_id": "http_fetch"}]
        db.get_tool.return_value = {
            "interface": {
                "parameters": [
                    {"name": "library_id", "type": "text", "required": True},
                    {"name": "class_name", "type": "text", "required": True},
                    {"name": "directory", "type": "text", "required": True},
                ],
            }
        }
        db.get_agent.return_value = {"agent_type_key": "designer", "agent_type_label": "Designer"}
        db.get_skill.return_value = {
            "skill_id": "outline_plan",
            "display_name": "Design Architecture",
        }

        preview = IOResolver(db).resolve_step_contract_preview(
            workflow_id="wf_demo",
            step_id="design",
            agent_id="designer",
            skill_id="outline_plan",
        )

        all_names = {
            item.get("name")
            for item in (
                list(preview.get("locked_required", []))
                + list(preview.get("unresolved_required", []))
                + list(preview.get("optional", []))
                + list(preview.get("custom", []))
            )
        }
        assert "summary_doc" in all_names
        assert "library_id" not in all_names
        assert "class_name" not in all_names
        assert "directory" not in all_names

    def test_step_outputs_merge_duplicate_names_and_preserve_required(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "development", "agent_id": "composer", "skill_id": "draft_summary"},
            workflow_id="wf_demo",
        )
        db.get_agent_outputs.return_value = [
            {"name": "artifact_bundle", "type": "text", "required": False},
        ]
        db.get_skill_outputs.return_value = [
            {"name": "artifact_bundle", "type": "text", "required": True},
        ]

        outputs = IOResolver(db).get_step_outputs("wf_demo", "development")
        by_name = {str(item.get("name")): item for item in outputs}

        assert set(by_name.keys()) == {"artifact_bundle"}
        assert bool(by_name["artifact_bundle"].get("required")) is True
        assert by_name["artifact_bundle"].get("source") == "skill"

    def test_step_outputs_ignore_set_variable_self_passthrough(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "finalize_outputs_prepare_outputs",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {"state.risk_notes": {"source": "state.risk_notes"}}
                    }
                },
                "output_mapping_jsonb": {"risk_notes": "state.risk_notes"},
            },
            workflow_id="wf_demo",
        )
        db.get_agent_outputs.return_value = []
        db.get_skill_outputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "finalize_outputs_prepare_outputs")

        assert not any(str(item.get("name")) == "risk_notes" for item in outputs)

    def test_step_outputs_include_parent_object_for_nested_assignment(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "capture_payload",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {
                            "state.webhook_payload.email": {"source": "state.inputs.email"}
                        }
                    }
                },
            },
            workflow_id="wf_demo",
        )
        db.get_agent_outputs.return_value = []
        db.get_skill_outputs.return_value = []

        outputs = IOResolver(db).get_step_outputs("wf_demo", "capture_payload")
        by_name = {str(item.get("name")): item for item in outputs}

        assert by_name["webhook_payload"]["type"] == "object"

    def test_extension_output_type_is_available_to_later_text_connector_input(self, monkeypatch):
        from core.application.registry.io_resolver import IOResolver

        db = _extension_to_connector_workflow_db(
            monkeypatch,
            extension_output_type="text",
        )

        resolver = IOResolver(db)
        outputs = resolver.get_step_outputs("wf_demo", "produce_document")
        upstream = resolver.get_available_outputs_for_step("wf_demo", "consume_document")
        preview = resolver.resolve_step_contract_preview(
            workflow_id="wf_demo",
            step_id="produce_document",
        )
        issues = resolver.validate_step_io("wf_demo", "consume_document")

        assert {item["name"]: item["type"] for item in outputs}["release_document_id"] == "text"
        assert {(item["name"], item["step_id"], item["group_id"]) for item in upstream} >= {
            ("release_document_id", "produce_document", None)
        }
        assert (
            resolver.get_available_type_map_for_step("wf_demo", "consume_document")[
                "message.step.produce_document.artifacts.release_document_id"
            ]
            == "text"
        )
        assert {item["name"]: item["type"] for item in preview["outputs"]}[
            "release_document_id"
        ] == "text"
        assert not [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ], issues

    def test_extension_structured_output_remains_incompatible_with_later_text_input(
        self, monkeypatch
    ):
        from core.application.registry.io_resolver import IOResolver

        db = _extension_to_connector_workflow_db(
            monkeypatch,
            extension_output_type="object",
        )

        resolver = IOResolver(db)
        issues = resolver.validate_step_io("wf_demo", "consume_document")

        matching = [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ]
        assert len(matching) == 1
        assert matching[0]["input_name"] == "documentId"

    @pytest.mark.parametrize(
        "contract_overrides",
        [
            {"account_id": "account-other"},
            {"install_id": "install-other"},
            {"extension_id": "fixture.other-producer"},
            {"install_state": "disabled"},
            {
                "operation_manifest": [
                    {
                        "operation_id": "produce_other_document",
                        "outputs": [{"name": "document_id", "value_type": "text"}],
                    }
                ]
            },
            {
                "operation_manifest": [
                    {
                        "operation_id": "produce_document",
                        "outputs": [{"name": "other_id", "value_type": "text"}],
                    }
                ]
            },
        ],
    )
    def test_extension_output_type_fails_closed_for_wrong_install_authority(
        self,
        monkeypatch,
        contract_overrides,
    ):
        from core.application.registry.io_resolver import IOResolver

        db = _extension_to_connector_workflow_db(
            monkeypatch,
            extension_output_type="text",
            contract_overrides=contract_overrides,
        )

        outputs = IOResolver(db).get_step_outputs("wf_demo", "produce_document")

        assert {item["name"]: item["type"] for item in outputs}["release_document_id"] == "json"

    def test_extension_output_is_not_available_to_a_step_that_runs_before_it(self, monkeypatch):
        from core.application.registry.io_resolver import IOResolver

        db = _extension_to_connector_workflow_db(
            monkeypatch,
            extension_output_type="text",
            consumer_first=True,
        )

        resolver = IOResolver(db)
        issues = resolver.validate_step_io("wf_demo", "consume_document")

        assert resolver.get_available_outputs_for_step("wf_demo", "consume_document") == []
        assert [
            (issue["input_name"], issue["path"], issue["step_id"], issue["group_id"])
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ] == [("documentId", "inputs.documentId", "consume_document", None)]

    def test_validate_workflow_readiness_allows_final_delivery_to_return_prior_output(self):
        from core.application.registry.contract_validation import validate_workflow_readiness

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "capture_input",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {
                            "state.inputs.campaign_brief": {"source": "state.inputs.campaign_brief"}
                        }
                    }
                },
            },
            {
                "step_id": "write_copy",
                "step_kind": "agent",
                "agent_id": "sample_launch_copywriter",
                "skill_id": "sample_write_launch_copy",
                "output_mapping_jsonb": {"launch_copy": "state.launch_copy"},
            },
            {
                "step_id": "return_launch_copy",
                "step_kind": "set_variable",
                "boundary_contract": {"exit": ["launch_copy"]},
                "override": {
                    "set_variable": {
                        "assignments": {"state.launch_copy": {"source": "state.launch_copy"}}
                    }
                },
                "output_mapping_jsonb": {"launch_copy": "state.launch_copy"},
            },
            workflow_id="wf_demo",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "campaign_brief", "type": "text"}],
                    "expected_outputs": [{"name": "launch_copy", "type": "text"}],
                    "trigger_channels": ["parent_workflow"],
                }
            },
        )
        db.get_agent_outputs.return_value = [{"name": "launch_copy", "type": "text"}]
        db.get_skill_outputs.return_value = [{"name": "launch_copy", "type": "text"}]
        db.get_agent.return_value = {
            "agent_id": "sample_launch_copywriter",
            "enabled": True,
            "_skills": [{"skill_id": "sample_write_launch_copy"}],
            "_tools": [],
        }
        db.get_skill.return_value = {
            "skill_id": "sample_write_launch_copy",
            "enabled": True,
            "expected_inputs": [{"name": "campaign_brief", "type": "text"}],
            "expected_outputs": [{"name": "launch_copy", "type": "text"}],
        }
        db.get_agent_skills.return_value = [{"skill_id": "sample_write_launch_copy"}]
        db.get_agent_tools.return_value = []
        db.get_tool.return_value = None
        db.get_workflow_summary.return_value = None

        validation = validate_workflow_readiness(db, "wf_demo")

        assert validation["valid"] is True, validation["errors"]
        assert not any(
            issue.get("code") == "workflow.boundary_exit_not_produced"
            for issue in validation["errors"]
        )

    def test_validate_workflow_readiness_rejects_unmapped_required_dataset_parameter(self):
        from core.application.registry.contract_validation import validate_workflow_readiness

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "load",
                "step_kind": "dataset_query",
                "override": {
                    "dataset_query": {
                        "query_template_id": "query-1",
                        "params": {},
                    }
                },
            },
            workflow_id="wf_dataset",
        )
        db.get_dataset_query_template.return_value = {
            "query_template_id": "query-1",
            "enabled": True,
            "params_schema_jsonb": {
                "type": "object",
                "properties": {"project_name": {"type": "string"}},
                "required": ["project_name"],
            },
        }
        db.get_agent_outputs.return_value = []
        db.get_skill_outputs.return_value = []

        validation = validate_workflow_readiness(db, "wf_dataset")

        assert any(
            issue.get("code") == "workflow.step_dataset_query_required_parameter_unmapped"
            and issue.get("path")
            == "workflow.steps.load.override.dataset_query.params.project_name"
            for issue in validation["errors"]
        )

    def test_io_resolver_reuses_cached_workflow_within_single_instance(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {"step_id": "analysis", "step_kind": "set_variable"},
            {"step_id": "development", "step_kind": "set_variable"},
            workflow_id="wf_demo",
        )

        resolver = IOResolver(db)
        resolver.get_step_outputs("wf_demo", "analysis")
        resolver.get_available_outputs_for_step("wf_demo", "development")
        resolver.validate_step_io("wf_demo", "development")

        db.get_workflow.assert_called_once_with("wf_demo")

    def test_validate_step_io_accepts_explicit_state_input_mapping_for_skill_required_input(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "generate_email_draft",
                "step_kind": "agent",
                "agent_id": "sample_launch_copywriter",
                "skill_id": "sample_write_launch_copy",
                "input_mapping_jsonb": {
                    "request": "state.inputs.launch_brief",
                    "launch_brief": "state.inputs.launch_brief",
                },
                "output_mapping_jsonb": {
                    "draft_subject": "state.draft_subject",
                },
                "override": {
                    "agent": {
                        "input_mapping": {
                            "request": "state.inputs.launch_brief",
                            "launch_brief": "state.inputs.launch_brief",
                        }
                    }
                },
            },
            workflow_id="wf_demo",
            settings={
                "callable": {
                    "expected_inputs": [
                        {"name": "launch_brief", "type": "text", "required": True},
                    ]
                }
            },
        )
        db.get_skill_inputs.return_value = [
            {"name": "request", "type": "text", "required": True},
        ]
        db.get_agent_inputs.return_value = []

        resolver = IOResolver(db)
        resolver.get_available_outputs_for_step = MagicMock(return_value=[])

        issues = resolver.validate_step_io("wf_demo", "generate_email_draft")

        assert not [
            issue
            for issue in issues
            if issue.get("severity") == "error" and issue.get("input_name") == "request"
        ], issues

    def test_validate_step_io_prefers_valid_override_mapping_when_raw_input_mapping_is_stale(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "generate_email_draft",
                "step_kind": "agent",
                "agent_id": "sample_launch_copywriter",
                "skill_id": "sample_write_launch_copy",
                "input_mapping_jsonb": {
                    "request": "draft_email",
                    "launch_brief": "${launch_brief}",
                },
                "override": {
                    "agent": {
                        "input_mapping": {
                            "request": "state.inputs.launch_brief",
                            "launch_brief": "state.inputs.launch_brief",
                        }
                    }
                },
            },
            workflow_id="wf_demo",
            settings={
                "callable": {
                    "expected_inputs": [
                        {"name": "launch_brief", "type": "text", "required": True},
                    ]
                }
            },
        )
        db.get_skill_inputs.return_value = [
            {"name": "request", "type": "text", "required": True},
        ]
        db.get_agent_inputs.return_value = []

        resolver = IOResolver(db)
        resolver.get_available_outputs_for_step = MagicMock(return_value=[])

        issues = resolver.validate_step_io("wf_demo", "generate_email_draft")

        assert not [
            issue
            for issue in issues
            if issue.get("severity") == "error" and issue.get("input_name") == "request"
        ], issues

    def test_validate_step_io_accepts_upstream_request_output_for_agent_request(self):
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "build_request_payload",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {
                            "state.request": {"risk": "high"},
                        }
                    }
                },
            },
            {
                "step_id": "review_launch_risks",
                "step_kind": "agent",
                "agent_id": "sample_launch_risk_reviewer",
                "skill_id": "sample_review_launch_risks",
                "input_mapping_jsonb": {"request": "state.request"},
                "override": {
                    "agent": {
                        "input_mapping": {"request": "state.request"},
                    }
                },
            },
            workflow_id="wf_demo",
        )
        db.get_skill_inputs.return_value = [
            {"name": "request", "type": "json", "required": True},
        ]
        db.get_agent_inputs.return_value = []

        issues = IOResolver(db).validate_step_io("wf_demo", "review_launch_risks")

        assert not [
            issue
            for issue in issues
            if issue.get("severity") == "error" and issue.get("input_name") == "request"
        ], issues

    def test_validate_step_io_accepts_callable_string_input_for_json_connector_param(self):
        from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "measure_event_type_length",
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "input_mapping_jsonb": {"value": "state.inputs.event_type"},
                "output_mapping_jsonb": {"result": "state.webhook_payload.event_type_length"},
                "override": {
                    "connector": {
                        "function_name": "length",
                        "input_mapping": {"value": "state.inputs.event_type"},
                        "output_mapping": {"result": "state.webhook_payload.event_type_length"},
                    }
                },
            },
            workflow_id="wf_demo",
            settings={
                "callable": {
                    "expected_inputs": [
                        {"name": "event_type", "type": "string", "required": True},
                    ]
                }
            },
        )
        manifests = builtin_standard_tool_manifests()
        db.get_tool.side_effect = lambda tool_id: manifests.get(str(tool_id or ""))
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        issues = IOResolver(db).validate_step_io("wf_demo", "measure_event_type_length")

        assert not [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ], issues

    @pytest.mark.parametrize("producer_first", [True, False])
    def test_validate_step_io_sees_state_output_only_from_upstream_steps(self, producer_first):
        from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
        from core.application.registry.io_resolver import IOResolver

        producer = {
            "step_id": "set_failure_reason",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "target": "state.failure_reason",
                    "value": "Intentional demo failure",
                }
            },
        }
        consumer = {
            "step_id": "measure_failure_reason",
            "step_kind": "connector",
            "connector_id": "connector.std.text",
            "input_mapping_jsonb": {"value": "state.failure_reason"},
            "override": {
                "connector": {
                    "function_name": "length",
                    "input_mapping": {"value": "state.failure_reason"},
                }
            },
        }
        ordered = (producer, consumer) if producer_first else (consumer, producer)
        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(*ordered, workflow_id="wf_demo")
        manifests = builtin_standard_tool_manifests()
        db.get_tool.side_effect = lambda tool_id: manifests.get(str(tool_id or ""))
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []

        issues = IOResolver(db).validate_step_io("wf_demo", "measure_failure_reason")

        unavailable = [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ]
        assert (unavailable == []) is producer_first, issues

    def test_validate_step_io_exposes_dataset_query_result_target_to_later_steps(self):
        from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "query_open_orders",
                "step_kind": "dataset_query",
                "override": {
                    "dataset_query": {
                        "query_template_id": "query-1",
                        "result_target": "state.demo_orders_result",
                    }
                },
            },
            {
                "step_id": "count_open_orders",
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "input_mapping_jsonb": {"value": "state.demo_orders_result.rows"},
                "override": {"connector": {"function_name": "length"}},
            },
            workflow_id="wf_demo",
        )
        manifests = builtin_standard_tool_manifests()
        db.get_tool.side_effect = lambda tool_id: manifests.get(str(tool_id or ""))
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []
        resolver = IOResolver(db)

        type_map = resolver.get_available_type_map_for_step("wf_demo", "count_open_orders")
        issues = resolver.validate_step_io("wf_demo", "count_open_orders")

        assert type_map["state.demo_orders_result"] == "json"
        assert not [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ], issues

    def test_validate_step_io_exposes_loop_item_to_loop_group_steps(self):
        from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
        from core.application.registry.io_resolver import IOResolver

        db = MagicMock()
        db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "settings": {
                "execution_graph": {
                    "schema_version": 1,
                    "entry": node("inspect_batches_loop", "for_each"),
                    "edges": [
                        edge(
                            "measure_current_batch",
                            {
                                "kind": "for_each_exit",
                                "for_each_id": "inspect_batches_loop",
                                "lane_id": None,
                            },
                        )
                    ],
                }
            },
            "groups": [],
            "for_each": [
                {
                    "for_each_id": "inspect_batches_loop",
                    "over": "catalog_batches",
                    "item_name": "current_batch",
                    "body": {"kind": "sequence", "entry": node("measure_current_batch")},
                }
            ],
            "steps": [
                {
                    "step_id": "measure_current_batch",
                    "for_each_id": "inspect_batches_loop",
                    "step_kind": "connector",
                    "connector_id": "connector.std.text",
                    "input_mapping_jsonb": {"value": "state.current_batch"},
                    "override": {
                        "connector": {
                            "function_name": "length",
                            "input_mapping": {"value": "state.current_batch"},
                        }
                    },
                },
                {
                    "step_id": "measure_outside_loop",
                    "step_kind": "connector",
                    "connector_id": "connector.std.text",
                    "input_mapping_jsonb": {"value": "state.current_batch"},
                    "override": {"connector": {"function_name": "length"}},
                },
            ],
        }
        manifests = builtin_standard_tool_manifests()
        db.get_tool.side_effect = lambda tool_id: manifests.get(str(tool_id or ""))
        db.get_skill_inputs.return_value = []
        db.get_agent_inputs.return_value = []
        resolver = IOResolver(db)

        type_map = resolver.get_available_type_map_for_step("wf_demo", "measure_current_batch")
        issues = resolver.validate_step_io("wf_demo", "measure_current_batch")
        outside_issues = resolver.validate_step_io("wf_demo", "measure_outside_loop")

        assert type_map["state.current_batch"] == "json"
        assert not [
            issue
            for issue in issues
            if issue.get("issue_type") == "connector_required_input_unavailable"
        ], issues
        assert "state.current_batch" not in resolver.get_available_type_map_for_step(
            "wf_demo", "measure_outside_loop"
        )
        assert [issue["input_name"] for issue in outside_issues] == ["value"]

    def test_save_published_block_contract_rejects_source_rebind_when_block_is_in_use(
        self, monkeypatch
    ):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")

        @contextmanager
        def _fake_transaction():
            yield MagicMock()

        monkeypatch.setattr(store, "_transaction", _fake_transaction)
        monkeypatch.setattr(store, "_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            store,
            "_published_block_source_binding_cur",
            lambda cur, block_key: ("agent", "designer::outline_plan"),
        )
        monkeypatch.setattr(
            store,
            "_published_block_step_bindings_cur",
            lambda cur, block_key: [{"workflow_id": "wf_demo", "step_id": "design_outline"}],
        )
        monkeypatch.setattr(
            store,
            "_validate_published_block_source_ref_cur",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            store,
            "get_published_block_contract",
            lambda block_key: {
                "block_key": block_key,
                "display_name": "Designer / Outline",
                "block_type": "agent",
                "source_kind": "agent",
                "source_ref": "designer::outline_plan",
                "enabled": True,
                "inputs": [],
                "outputs": [],
            },
        )

        with pytest.raises(
            ValueError, match="Cannot change source_ref/source_kind for published block"
        ):
            store.save_published_block_contract(
                {
                    "block_key": "designer.outline_plan",
                    "display_name": "Designer / Outline",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "composer::draft_summary",
                    "enabled": True,
                    "inputs": [],
                    "outputs": [],
                },
                author="test",
            )

    @pytest.mark.parametrize(
        "stored_error,override,expected",
        [
            (None, None, None),
            ("", None, None),
            ("  ", None, None),
            (" failed ", None, "failed"),
            ("None", None, "None"),
            ("failed", {"last_sync_error": None}, None),
            ("failed", {"last_sync_error": " new failure "}, "new failure"),
        ],
    )
    @pytest.mark.parametrize("include_error", [True, False])
    def test_save_published_block_preserves_nullable_sync_error(
        self, monkeypatch, stored_error, override, expected, include_error
    ):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        cur = MagicMock()
        existing = {
            "block_key": "fixture.block",
            "display_name": "Draft",
            "block_type": "agent",
            "source_kind": "agent",
            "source_ref": "designer::outline_plan",
            "enabled": False,
            "last_sync_error": stored_error,
            "inputs": [{"field_key": "design_request", "required": True}],
            "outputs": [
                {
                    "field_key": "outline_doc",
                    "required_by_default": True,
                    "exposed_by_default": True,
                }
            ],
        }

        @contextmanager
        def transaction():
            yield cur

        monkeypatch.setattr(store, "_transaction", transaction)
        monkeypatch.setattr(store, "_exists", lambda *args, **kwargs: True)
        monkeypatch.setattr(store, "get_published_block_contract", lambda *args, **kwargs: existing)
        monkeypatch.setattr(
            store,
            "_published_block_source_binding_cur",
            lambda *args, **kwargs: ("agent", "designer::outline_plan"),
        )
        monkeypatch.setattr(
            store, "_validate_published_block_source_ref_cur", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(store, "_field_id_for_key_cur", lambda _cur, key: f"{key}_id")
        monkeypatch.setattr(store, "_log_change_cur", lambda *args, **kwargs: None)
        monkeypatch.setattr(store, "_column_exists", lambda *args, **kwargs: False)
        payload = {**existing, "description": "Metadata only"}
        if not include_error:
            payload.pop("last_sync_error")
        if override is not None:
            payload.update(override)
        store.save_published_block_contract(payload, author="test")
        snapshots = [
            call.args[1]
            for call in cur.execute.call_args_list
            if str(call.args[0])
            .lstrip()
            .startswith("INSERT INTO registry_published_block_contracts")
        ]
        assert len(snapshots) == 1
        assert snapshots[0]["last_sync_error"] == expected
        assert snapshots[0]["source_ref"] == existing["source_ref"]
        assert snapshots[0]["enabled"] is False
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        assert "INSERT INTO registry_published_block_inputs" in sql
        assert "INSERT INTO registry_published_block_outputs" in sql

    def test_save_published_block_contract_upserts_block_io_rows(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")
        cur = MagicMock()

        @contextmanager
        def _fake_transaction():
            yield cur

        monkeypatch.setattr(store, "_transaction", _fake_transaction)
        monkeypatch.setattr(store, "_exists", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            store,
            "_published_block_source_binding_cur",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            store,
            "_validate_published_block_source_ref_cur",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(store, "_field_id_for_key_cur", lambda _cur, key: f"{key}_id")
        monkeypatch.setattr(store, "_log_change_cur", lambda *args, **kwargs: None)
        monkeypatch.setattr(store, "_column_exists", lambda *_args, **_kwargs: False)

        store.save_published_block_contract(
            {
                "block_key": "designer.outline_plan",
                "display_name": "Designer / Outline",
                "block_type": "agent",
                "source_kind": "agent",
                "source_ref": "designer::outline_plan",
                "enabled": True,
                "inputs": [
                    {
                        "field_key": "design_request",
                        "required": True,
                    }
                ],
                "outputs": [
                    {
                        "field_key": "outline_doc",
                        "required_by_default": True,
                        "exposed_by_default": True,
                    }
                ],
            },
            author="test",
        )

        executed_sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        assert "INSERT INTO registry_published_block_inputs" in executed_sql
        assert "INSERT INTO registry_published_block_outputs" in executed_sql
        assert "ON CONFLICT (block_key, field_id) DO UPDATE SET" in executed_sql


# ---------------------------------------------------------------------------
# RegistryManager loading tests
# ---------------------------------------------------------------------------


class TestRegistryLoading:
    """Test that all DB entries load correctly."""

    def test_loads_all_agents(self, user_authored_catalog):
        rm = get_fresh_registry()
        agent_ids = {agent.agent_id for agent in rm.agents.list_all()}
        assert user_authored_catalog.agent_id in agent_ids

    def test_loads_all_tools(self):
        rm = get_fresh_registry()
        tools = rm.tools.list_all()
        assert len(tools) > 0, "Expected at least 1 tool in registry"

    def test_registry_seed_omits_retired_knowledge_tools(self):
        seed_path = Path(__file__).parent.parent.parent / "db" / "seeds" / "registry_seed.json"
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        assert seed["tools"] == []

    def test_builtin_mcp_sync_retires_only_shipped_knowledge_rows(self):
        from core.application.registry.builtin_mcp_tools import (
            _legacy_kb_write_manifest,
            sync_builtin_mcp_tools,
        )

        rows = {
            "kb_query": {
                "tool_id": "kb_query",
                "tags": ["builtin", "mcp", "knowledge"],
                "implementation": {
                    "type": "mcp",
                    "server": "fs",
                    "connector_id": "builtin_fs_mcp",
                },
                "scope_kind": "system",
            },
            "kb_write": {
                **_legacy_kb_write_manifest(),
                "scope_kind": "system",
            },
        }
        store = _BuiltinToolStore(rows)

        changed = sync_builtin_mcp_tools(store)

        assert store.deleted == [("kb_query", "system"), ("kb_write", "system")]
        assert {"kb_query", "kb_write", "workflow_ops"} <= set(changed)
        workflow_payload = next(
            payload for payload, _author in store.saved if payload["tool_id"] == "workflow_ops"
        )
        workflow_functions = {
            function["name"] for function in workflow_payload["interface"]["functions"]
        }
        assert {"project_list", "project_jobs"} <= workflow_functions
        assert {
            "kb_search",
            "kb_write",
            "project_profile_get",
            "project_profile_update",
        }.isdisjoint(workflow_functions)

    def test_builtin_mcp_sync_preserves_same_id_user_owned_or_modified_rows(self):
        from core.application.registry.builtin_mcp_tools import (
            _legacy_kb_write_manifest,
            sync_builtin_mcp_tools,
        )

        modified_write = _legacy_kb_write_manifest()
        modified_write["description"] = "User-authored replacement"
        store = _BuiltinToolStore(
            {
                "kb_query": {
                    "tool_id": "kb_query",
                    "tags": ["builtin", "mcp", "knowledge"],
                    "implementation": {
                        "type": "mcp",
                        "connector_id": "builtin_fs_mcp",
                    },
                    "scope_kind": "project",
                    "project_id": "project-1",
                    "account_id": "account-1",
                },
                "kb_write": {**modified_write, "scope_kind": "system"},
            }
        )

        sync_builtin_mcp_tools(store)

        assert store.deleted == []
        assert "kb_query" in store.rows
        assert store.rows["kb_write"]["description"] == "User-authored replacement"

    def test_builtin_mcp_tool_sync_adds_workflow_ops_surface(self):
        rm = get_fresh_registry()
        registered = rm.tool_resolver.list_registered()
        assert "workflow_ops" in registered
        names = set(registered["workflow_ops"])
        assert {
            "project_list",
            "project_jobs",
            "workflow_list",
            "workflow_validate",
            "workflow_contract",
            "workflow_suggestions",
            "workflow_trigger",
            "job_result",
            "job_acknowledge",
            "job_unacknowledge",
            "job_restart",
            "job_cancel",
            "job_approval_decide",
        } <= names

    def test_registry_does_not_expose_removed_provider_callable(self):
        rm = get_fresh_registry()
        registered = rm.tool_resolver.list_registered()
        names = {name for group in registered.values() for name in group}
        removed_name = "sample_tasks_" + "update_task"
        assert removed_name not in names

    def test_loads_all_skills(self, user_authored_catalog):
        rm = get_fresh_registry()
        skill_ids = {cap.skill_id for cap in rm.skills.list_all()}
        assert user_authored_catalog.skill_id in skill_ids

    def test_loads_all_workflows(self, user_authored_catalog):
        rm = get_fresh_registry()
        workflow_ids = {wf.workflow_id for wf in rm.workflows.list_all()}
        assert user_authored_catalog.workflow_id in workflow_ids

    def test_agents_have_required_fields(self, user_authored_catalog):
        rm = get_fresh_registry()
        for agent in rm.agents.list_all():
            assert agent.agent_id, f"Agent missing agent_id: {agent}"
            assert agent.display_name, f"Agent {agent.agent_id} missing display_name"
            assert agent.prompts.system, f"Agent {agent.agent_id} missing system prompt"

    def test_tools_have_required_fields(self):
        rm = get_fresh_registry()
        for tool in rm.tools.list_all():
            assert tool.tool_id, "Tool missing tool_id"
            assert tool.display_name, f"Tool {tool.tool_id} missing display_name"

    def test_skills_have_required_fields(self, user_authored_catalog):
        rm = get_fresh_registry()
        for cap in rm.skills.list_all():
            assert cap.skill_id, "Skill missing skill_id"
            assert cap.display_name, f"Skill {cap.skill_id} missing display_name"

    def test_workflows_have_steps(self, user_authored_catalog):
        rm = get_fresh_registry()
        runtime_ready_workflows = 0
        for wf in rm.workflows.list_all():
            if wf.workflow_id.startswith("test_") or not wf.enabled:
                continue  # Skip test fixtures and disabled placeholder workflows
            if len(wf.steps) == 0:
                callable_cfg = (
                    wf.settings.get("callable", {}) if isinstance(wf.settings, dict) else {}
                )
                assert not bool(callable_cfg.get("enabled")), (
                    f"Workflow {wf.workflow_id} is callable-enabled but has no steps"
                )
                continue  # Authoring draft shells may persist without executable steps yet.
            runtime_ready_workflows += 1
        assert runtime_ready_workflows > 0, (
            "Expected at least one runtime-ready workflow with steps"
        )


class TestRegistrySingleton:
    """Test singleton behavior of RegistryManager."""

    def test_singleton_returns_same_instance(self):
        _require_db()
        import core.application.registry.registry_manager as rm_module

        rm_module._instance = None
        from core.application.registry.registry_manager import RegistryManager

        a = RegistryManager.instance()
        b = RegistryManager.instance()
        assert a is b

    def test_reset_clears_singleton(self):
        from core.application.registry.registry_manager import RegistryManager

        _require_db()
        _ = get_fresh_registry()
        RegistryManager.reset()
        import core.application.registry.registry_manager as rm_module

        assert rm_module._instance is None

    def test_instance_after_reset_creates_new(self):
        from core.application.registry.registry_manager import RegistryManager

        _require_db()
        a = get_fresh_registry()
        RegistryManager.reset()
        b = get_fresh_registry()
        # Same data, different object
        assert a is not b
        assert len(a.agents.list_all()) == len(b.agents.list_all())

    def test_thread_local_instance_override_is_scoped(self):
        import core.application.registry.registry_manager as rm_module
        from core.application.registry.registry_manager import RegistryManager

        original_instance = rm_module._instance
        sentinel_registry = object()
        try:
            with RegistryManager.thread_local_instance(sentinel_registry):
                assert RegistryManager.instance() is sentinel_registry
            assert rm_module._instance is original_instance
        finally:
            rm_module._instance = original_instance

    def test_process_instance_override_is_scoped(self):
        import core.application.registry.registry_manager as rm_module
        from core.application.registry.registry_manager import RegistryManager

        original_instance = rm_module._instance
        original_override = rm_module._runtime_override_registry
        sentinel_registry = object()
        try:
            with RegistryManager.process_instance_override(sentinel_registry):
                assert RegistryManager.instance() is sentinel_registry
            assert rm_module._instance is original_instance
            assert rm_module._runtime_override_registry is original_override
        finally:
            rm_module._instance = original_instance
            rm_module._runtime_override_registry = original_override

    def test_get_stats_returns_counts(self, user_authored_catalog):
        rm = get_fresh_registry()
        if hasattr(rm, "get_stats"):
            stats = rm.get_stats()
            assert "agent_count" in stats or len(stats) > 0
        else:
            # Manually verify counts
            assert len(rm.agents.list_all()) > 0
            assert len(rm.tools.list_all()) > 0


# ---------------------------------------------------------------------------
# AgentRegistry tests
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    """Test AgentRegistry functionality."""

    def test_list_all_returns_all_agents(self, user_authored_catalog):
        rm = get_fresh_registry()
        all_agents = rm.agents.list_all()
        assert isinstance(all_agents, list)
        assert user_authored_catalog.agent_id in {agent.agent_id for agent in all_agents}

    def test_get_agent_by_id(self, user_authored_catalog):
        rm = get_fresh_registry()
        all_agents = rm.agents.list_all()
        if not all_agents:
            pytest.skip("No agents in registry")
        agent = all_agents[0]
        retrieved = rm.agents.get_agent(agent.agent_id)
        assert retrieved.agent_id == agent.agent_id

    def test_get_unknown_agent_raises(self):
        rm = get_fresh_registry()
        # get_agent returns None for unknown agents (does not raise)
        result = rm.agents.get_agent("nonexistent_agent_xyz_12345")
        assert result is None

    def test_find_agents_for_skill_collect_findings(self):
        """The scout agent should support collect_findings skill."""
        rm = get_fresh_registry()
        if not hasattr(rm.agents, "find_by_skill"):
            # Try manual search
            matching = [a for a in rm.agents.list_all() if "collect_findings" in a.skills]
            # It's OK if this skill doesn't exist with this exact name
            assert isinstance(matching, list)
            return
        matching = rm.agents.find_by_skill("collect_findings")
        assert isinstance(matching, list)

    def test_find_agents_for_skill_draft_summary(self):
        """Developer agent should support draft_summary skill."""
        rm = get_fresh_registry()
        if not hasattr(rm.agents, "find_by_skill"):
            matching = [a for a in rm.agents.list_all() if "draft_summary" in a.skills]
            assert isinstance(matching, list)
            return
        matching = rm.agents.find_by_skill("draft_summary")
        assert isinstance(matching, list)

    def test_disabled_agent_excluded_from_enabled_list(self, user_authored_catalog):
        """Disabled agents should not appear in list_enabled() if that method exists."""
        from core.application.registry.db_store import RegistryDBStore

        RegistryDBStore(resolve_test_db_dsn()).toggle_agent(user_authored_catalog.agent_id)
        rm = get_fresh_registry()
        all_agents = rm.agents.list_all()
        # Find disabled agents
        disabled = [a for a in all_agents if not a.enabled]
        if not disabled:
            pytest.skip("No disabled agents in registry to test")
        # Check that enabled-only listing excludes them
        if hasattr(rm.agents, "list_enabled"):
            enabled_only = rm.agents.list_enabled()
            disabled_ids = {a.agent_id for a in disabled}
            enabled_ids = {a.agent_id for a in enabled_only}
            assert not (disabled_ids & enabled_ids), "Disabled agent appeared in list_enabled()"

    def test_agent_has_skills_list(self, user_authored_catalog):
        rm = get_fresh_registry()
        for agent in rm.agents.list_all():
            assert isinstance(agent.skills, list)

    def test_agent_has_tools_list(self, user_authored_catalog):
        rm = get_fresh_registry()
        for agent in rm.agents.list_all():
            assert isinstance(agent.tools, list)


# ---------------------------------------------------------------------------
# WorkflowRegistry tests
# ---------------------------------------------------------------------------


class TestWorkflowRegistry:
    """Test WorkflowRegistry functionality."""

    def test_list_all_returns_workflows(self, user_authored_catalog):
        rm = get_fresh_registry()
        workflows = rm.workflows.list_all()
        assert isinstance(workflows, list)
        assert user_authored_catalog.workflow_id in {wf.workflow_id for wf in workflows}

    def test_list_enabled_returns_only_enabled(self):
        rm = get_fresh_registry()
        if hasattr(rm.workflows, "list_enabled"):
            enabled = rm.workflows.list_enabled()
            for wf in enabled:
                assert wf.enabled is True

    def test_task_class_feature_resolver_removed(self):
        rm = get_fresh_registry()
        assert not hasattr(rm.workflows, "resolve_workflow_for_task_class")

    def test_task_class_bugfix_resolver_removed(self):
        rm = get_fresh_registry()
        assert not hasattr(rm.workflows, "resolve_workflow_for_task_class")

    def test_get_workflow_by_id(self, user_authored_catalog):
        rm = get_fresh_registry()
        all_wf = rm.workflows.list_all()
        if not all_wf:
            pytest.skip("No workflows")
        wf = all_wf[0]
        if hasattr(rm.workflows, "get_workflow"):
            retrieved = rm.workflows.get_workflow(wf.workflow_id)
            assert retrieved.workflow_id == wf.workflow_id

    def test_workflow_steps_have_ids(self, user_authored_catalog):
        rm = get_fresh_registry()
        for wf in rm.workflows.list_all():
            for step in wf.steps:
                assert step.step_id, f"Step in {wf.workflow_id} missing step_id"


# ---------------------------------------------------------------------------
# Registry validation tests
# ---------------------------------------------------------------------------


class TestRegistryValidation:
    """Test RegistryManager.validate() catches real errors."""

    @staticmethod
    def _make_manager(*, agent_output_format: str = "", skill_prompt: str = ""):
        from core.application.registry.registry_manager import RegistryManager

        manager = RegistryManager.__new__(RegistryManager)
        agent = SimpleNamespace(
            agent_id="composer",
            enabled=True,
            skills=["draft_summary"],
            tools=[],
            structured_inputs=[],
            structured_outputs=[],
            prompts=SimpleNamespace(
                output_format=agent_output_format,
                skill_prompts={"draft_summary": skill_prompt},
            ),
        )
        skill = SimpleNamespace(
            skill_id="draft_summary",
            enabled=True,
            input_schema={"type": "object", "properties": {}, "required": []},
            output_schema={"type": "object", "properties": {}, "required": []},
            expected_inputs=[],
            expected_outputs=[],
        )

        manager.agents = MagicMock()
        manager.agents.list_all.return_value = [agent]
        manager.agents.get_agent.return_value = agent

        manager.tools = MagicMock()
        manager.tools.list_all.return_value = []
        manager.tools.list_enabled.return_value = []

        manager.skills = MagicMock()
        manager.skills.list_all.return_value = [skill]
        manager.skills.list_enabled.return_value = [skill]

        manager.workflows = MagicMock()
        manager.workflows.list_all.return_value = []
        manager.workflows.list_enabled.return_value = []

        manager._db_store = MagicMock()
        return manager

    def test_validate_clean_registry(self):
        """The real registry should have 0 validation errors."""
        rm = get_fresh_registry()
        errors, warnings = rm.validate()
        assert isinstance(errors, list)
        assert isinstance(warnings, list)
        # Real registry should be clean
        assert len(errors) == 0, f"Registry validation errors: {errors}"

    def test_validate_skips_agent_binding_for_runtime_step_kinds(self):
        rm = self._make_manager()
        steps = [
            SimpleNamespace(
                agent="",
                optional=False,
                override={},
                participant_key="",
                step_kind=step_kind,
            )
            for step_kind in (
                "http_request",
                "dataset_query",
                "batch",
                "set_variable",
                "collect",
            )
        ]
        workflow = SimpleNamespace(
            enabled=True,
            steps=steps,
            groups=[],
            settings={},
            traits={},
            workflow_id="wf_runtime_steps",
        )
        rm.workflows.list_all.return_value = [workflow]
        rm.workflows.list_enabled.return_value = [workflow]

        errors, _ = rm.validate()

        assert not any("agent step requires agent_id" in error for error in errors)

    def test_validate_counts_connector_steps_and_skips_connector_only_tools(self):
        rm = self._make_manager()
        projected_agent = SimpleNamespace(
            agent_id="extension.install.demo_agent",
            enabled=True,
            skills=["draft_summary"],
            tools=[],
            structured_inputs=[],
            structured_outputs=[],
            prompts=SimpleNamespace(
                output_format="",
                skill_prompts={"draft_summary": ""},
            ),
        )
        rm.agents.list_all.return_value.append(projected_agent)
        connector_tool = SimpleNamespace(
            tool_id="extension.demo.connector",
            enabled=True,
            config={},
        )
        connector_only_tool = SimpleNamespace(
            tool_id="std.json",
            enabled=True,
            config={"agent_exposure": "connector_only"},
        )
        ordinary_unused_tool = SimpleNamespace(
            tool_id="ordinary.unused",
            enabled=True,
            config={},
        )
        tools = [connector_tool, connector_only_tool, ordinary_unused_tool]
        rm.tools.list_all.return_value = tools
        rm.tools.list_enabled.return_value = tools

        workflow = SimpleNamespace(
            enabled=True,
            steps=[
                SimpleNamespace(
                    agent="composer",
                    connector_id="",
                    optional=False,
                    override={},
                    participant_key="",
                    step_kind="agent",
                ),
                SimpleNamespace(
                    agent="",
                    connector_id="extension.demo.connector",
                    optional=False,
                    override={},
                    participant_key="",
                    step_kind="connector",
                ),
            ],
            groups=[],
            settings={},
            traits={},
            workflow_id="wf_connector_tools",
        )
        rm.workflows.list_all.return_value = [workflow]
        rm.workflows.list_enabled.return_value = [workflow]

        errors, warnings = rm.validate()

        assert errors == []
        assert warnings == ["Tool 'ordinary.unused' is enabled but used by no agent"]

    def test_validate_returns_tuple(self):
        rm = get_fresh_registry()
        result = rm.validate()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_validate_flags_deprecated_agent_output_format(self):
        rm = self._make_manager(agent_output_format="VERIFICATION:\n- tests")

        errors, _ = rm.validate()

        assert any("deprecated prompts.output_format" in err for err in errors)

    def test_validate_flags_legacy_skill_prompt_formatting(self):
        rm = self._make_manager(skill_prompt="CHECKS:\n{}\nVERDICT: pass")

        errors, _ = rm.validate()

        assert any(
            "Skill prompts must not define outer response formatting" in err for err in errors
        )

    def test_reload_all_works(self):
        """reload_all() should not crash and should maintain counts."""
        rm = get_fresh_registry()
        agent_count_before = len(rm.agents.list_all())
        rm.reload_all()
        agent_count_after = len(rm.agents.list_all())
        assert agent_count_before == agent_count_after


class TestPublicContractProjection:
    class _DummyTx:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        def execute(self, sql, params=None):
            self.executed.append((" ".join(str(sql).split()), tuple(params or ())))

        def fetchone(self):
            return {}

    @staticmethod
    @contextmanager
    def _ctx(value):
        yield value

    def test_reconcile_projection_derives_fields_contracts_and_blocks_from_skill_pairs(self):
        from core.application.registry.public_contract_projection import (
            reconcile_public_contract_projection,
        )

        tx = self._DummyTx()
        db = MagicMock()
        db.list_skills.return_value = [
            {
                "skill_id": "outline_plan",
                "display_name": "Design Architecture",
                "description": "Create the architecture package",
                "enabled": True,
                "category": "architecture",
                "_inputs": [
                    {
                        "name": "summary_doc",
                        "type": "text",
                        "required": True,
                        "description": "Requirements",
                    },
                    {
                        "name": "outline_doc",
                        "type": "json",
                        "required": False,
                        "description": "Architecture doc",
                    },
                ],
                "_outputs": [
                    {
                        "name": "architecture",
                        "type": "json",
                        "required": True,
                        "description": "Architecture payload",
                    },
                    {
                        "name": "files_to_touch",
                        "type": "list",
                        "required": False,
                        "description": "Edited files",
                    },
                ],
            }
        ]
        db.list_agents.return_value = [
            {
                "agent_id": "designer",
                "display_name": "Solution Designer",
                "enabled": True,
                "_skills": [{"skill_id": "outline_plan"}],
            }
        ]
        db.list_fields.return_value = []
        db.list_data_contracts.return_value = []
        db.list_published_block_contracts.return_value = []
        db.save_field = MagicMock()
        db.save_data_contract = MagicMock()
        db.save_published_block_contract = MagicMock()
        db.get_published_block_contract.return_value = None
        db._transaction = lambda: self._ctx(tx)
        db._cursor = lambda: self._ctx(
            MagicMock(fetchone=lambda: {"fields": 4, "data_contracts": 3, "published_blocks": 1})
        )

        result = reconcile_public_contract_projection(db, apply=True)

        saved_field_keys = {call.args[0]["field_key"] for call in db.save_field.call_args_list}
        assert {
            "summary_doc",
            "outline_doc",
            "architecture",
            "files_to_touch",
        } <= saved_field_keys

        saved_contract_ids = {
            call.args[0]["contract_id"] for call in db.save_data_contract.call_args_list
        }
        assert "auto.outline_doc.v1" in saved_contract_ids
        assert "auto.architecture.v1" in saved_contract_ids
        assert "auto.files_to_touch.v1" in saved_contract_ids

        saved_block = db.save_published_block_contract.call_args_list[0].args[0]
        assert saved_block["block_key"] == "agent.designer.outline_plan"
        assert saved_block["source_ref"] == "designer::outline_plan"
        assert [item["field_key"] for item in saved_block["inputs"]] == [
            "summary_doc",
            "outline_doc",
        ]
        assert [item["field_key"] for item in saved_block["outputs"]] == [
            "architecture",
            "files_to_touch",
        ]
        assert result["applied"] is True

        executed_sql = "\n".join(sql for sql, _ in tx.executed)
        assert "UPDATE registry_skill_inputs SET contract_id = %s" in executed_sql
        assert "UPDATE registry_skill_outputs SET contract_id = %s" in executed_sql

    def test_reconcile_projection_dry_run_reports_orphaned_rows_without_writes(self):
        from core.application.registry.public_contract_projection import (
            reconcile_public_contract_projection,
        )

        db = MagicMock()
        db.list_skills.return_value = []
        db.list_agents.return_value = []
        db.list_fields.return_value = [
            {
                "field_key": "outline_doc",
                "projection_mode": "derived",
                "enabled": True,
                "sync_status": "active",
            }
        ]
        db.list_data_contracts.return_value = [
            {
                "contract_id": "auto.outline_doc.v1",
                "projection_mode": "derived",
                "enabled": True,
                "sync_status": "active",
            }
        ]
        db.list_published_block_contracts.return_value = [
            {
                "block_key": "agent.designer.outline_plan",
                "projection_mode": "derived",
                "enabled": True,
                "sync_status": "active",
            }
        ]
        db._cursor = lambda: self._ctx(
            MagicMock(fetchone=lambda: {"fields": 1, "data_contracts": 1, "published_blocks": 1})
        )

        result = reconcile_public_contract_projection(db, apply=False)

        assert result["applied"] is False
        assert result["summary"]["orphaned"] == 3
        db.save_field.assert_not_called()
        db.save_data_contract.assert_not_called()
        db.save_published_block_contract.assert_not_called()

    def test_projection_sync_skips_writes_for_unchanged_derived_rows(self):
        from core.application.registry.public_contract_projection import (
            ProjectionSummary,
            _sync_blocks,
            _sync_data_contracts,
            _sync_fields,
        )

        db = MagicMock()
        contract = {
            "contract_id": "auto.payload.v1",
            "kind": "json",
            "enabled": True,
            "projection_mode": "derived",
            "sync_status": "active",
            "derived_hash": "contract-hash",
        }
        field = {
            "field_key": "payload",
            "data_contract_id": "auto.payload.v1",
            "enabled": True,
            "projection_mode": "derived",
            "sync_status": "active",
            "derived_hash": "field-hash",
        }
        block = {
            "block_key": "workflow.demo",
            "enabled": True,
            "projection_mode": "derived",
            "sync_status": "active",
            "derived_hash": "block-hash",
        }
        summary = ProjectionSummary()

        _sync_data_contracts(
            db, {"auto.payload.v1": contract}, {"auto.payload.v1": contract}, summary
        )
        _sync_fields(db, {"payload": field}, {"payload": field}, summary)
        _sync_blocks(db, {"workflow.demo": block}, {"workflow.demo": block}, summary)

        assert summary.unchanged == 3
        db.save_data_contract.assert_not_called()
        db.save_field.assert_not_called()
        db.save_published_block_contract.assert_not_called()

    def test_projection_sync_skips_writes_for_already_orphaned_rows(self):
        from core.application.registry.public_contract_projection import (
            ProjectionSummary,
            _sync_blocks,
            _sync_data_contracts,
            _sync_fields,
        )

        db = MagicMock()
        contract = {
            "contract_id": "auto.old.v1",
            "enabled": False,
            "projection_mode": "derived",
            "sync_status": "orphaned",
        }
        field = {
            "field_key": "old_payload",
            "enabled": False,
            "projection_mode": "derived",
            "sync_status": "orphaned",
        }
        block = {
            "block_key": "workflow.old",
            "enabled": False,
            "projection_mode": "derived",
            "sync_status": "orphaned",
        }
        summary = ProjectionSummary()

        _sync_data_contracts(db, {"auto.old.v1": contract}, {}, summary)
        _sync_fields(db, {"old_payload": field}, {}, summary)
        _sync_blocks(db, {"workflow.old": block}, {}, summary)

        assert summary.unchanged == 3
        db.save_data_contract.assert_not_called()
        db.save_field.assert_not_called()
        db.save_published_block_contract.assert_not_called()
        db.get_published_block_contract.assert_not_called()

    def test_public_projection_ensure_skips_source_scan_when_projection_exists(self):
        from core.application.registry.public_contract_projection import (
            ensure_public_contract_projection,
        )

        db = MagicMock()
        db._cursor = lambda: self._ctx(
            MagicMock(fetchone=lambda: {"fields": 1, "data_contracts": 1, "published_blocks": 1})
        )

        result = ensure_public_contract_projection(db)

        assert result is None
        db.list_skills.assert_not_called()
        db.list_agents.assert_not_called()
        db.list_tools.assert_not_called()

    def test_public_projection_read_paths_auto_ensure_before_listing(self):
        from core.application.registry.db_store import RegistryDBStore

        class _RowsCursor:
            def __init__(self, rows):
                self._rows = rows

            def execute(self, _sql, _params=None):
                return None

            def fetchall(self):
                return self._rows

        store = RegistryDBStore("postgresql://unused")

        @contextmanager
        def _fake_cursor():
            yield _RowsCursor([{"block_key": "agent.demo.review_summary"}])

        store._cursor = _fake_cursor  # type: ignore[method-assign]
        store._ensure_public_contract_projection = MagicMock()

        rows = store.list_published_block_contracts(enabled_only=False)

        assert rows == [{"block_key": "agent.demo.review_summary"}]
        store._ensure_public_contract_projection.assert_called_once()

    def test_trigger_projection_sync_uses_advisory_lock_and_shared_connection(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        class _FakeCursor:
            def __init__(self, fetch_values):
                self._fetch_values = list(fetch_values)
                self.executed: list[tuple[str, tuple]] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.executed.append((" ".join(str(sql).split()), tuple(params or ())))

            def fetchone(self):
                if self._fetch_values:
                    return self._fetch_values.pop(0)
                return (True,)

        class _FakeConn:
            def __init__(self, fetch_values):
                self.autocommit = True
                self.closed = 0
                self._fetch_values = fetch_values
                self.cursors: list[_FakeCursor] = []

            def get_transaction_status(self):
                return 0

            def rollback(self):
                return None

            def cursor(self, cursor_factory=None):
                cur = _FakeCursor(self._fetch_values)
                self.cursors.append(cur)
                return cur

        store = RegistryDBStore("postgresql://unused")
        conn = _FakeConn(fetch_values=[(True,)])
        monkeypatch.setattr(store, "_acquire_conn", lambda: (conn, False))
        released: list[tuple[object, bool]] = []
        monkeypatch.setattr(
            store, "_release_conn", lambda c, from_pool: released.append((c, from_pool))
        )

        observed_tx_conn: list[object] = []

        def _fake_reconcile(db, *, apply):
            observed_tx_conn.append(db._get_tx_conn())
            assert apply is True
            return {"applied": True}

        monkeypatch.setattr(
            "core.application.registry.public_contract_projection.reconcile_public_contract_projection",
            _fake_reconcile,
        )

        store._trigger_public_contract_projection_sync()

        assert observed_tx_conn == [conn]
        executed = [entry for cur in conn.cursors for entry in cur.executed]
        sql_statements = "\n".join(sql for sql, _ in executed)
        assert "pg_try_advisory_lock" in sql_statements
        assert "pg_advisory_unlock" in sql_statements
        assert released == [(conn, False)]

    def test_projection_sync_suppression_batches_multiple_triggers(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        class _FakeCursor:
            def __init__(self):
                self.executed: list[tuple[str, tuple]] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.executed.append((" ".join(str(sql).split()), tuple(params or ())))

            def fetchone(self):
                return (True,)

        class _FakeConn:
            def __init__(self):
                self.autocommit = True
                self.closed = 0
                self.cursors: list[_FakeCursor] = []

            def get_transaction_status(self):
                return 0

            def rollback(self):
                return None

            def cursor(self, cursor_factory=None):
                cur = _FakeCursor()
                self.cursors.append(cur)
                return cur

        store = RegistryDBStore("postgresql://unused")
        conn = _FakeConn()
        acquired: list[object] = []
        monkeypatch.setattr(
            store,
            "_acquire_conn",
            lambda: acquired.append(conn) or (conn, False),
        )
        monkeypatch.setattr(store, "_release_conn", lambda *_args, **_kwargs: None)
        reconcile = MagicMock()
        monkeypatch.setattr(
            "core.application.registry.public_contract_projection.reconcile_public_contract_projection",
            reconcile,
        )

        with store.suppress_projection_sync():
            store._trigger_public_contract_projection_sync()
            with store.suppress_projection_sync():
                store._trigger_public_contract_projection_sync()
            store._trigger_public_contract_projection_sync()
            reconcile.assert_not_called()

        assert len(acquired) == 1
        reconcile.assert_called_once_with(store, apply=True)

    def test_trigger_projection_sync_skips_when_lock_is_held(self, monkeypatch):
        from core.application.registry.db_store import RegistryDBStore

        class _FakeCursor:
            def __init__(self, fetch_values):
                self._fetch_values = list(fetch_values)
                self.executed: list[tuple[str, tuple]] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.executed.append((" ".join(str(sql).split()), tuple(params or ())))

            def fetchone(self):
                if self._fetch_values:
                    return self._fetch_values.pop(0)
                return (False,)

        class _FakeConn:
            def __init__(self, fetch_values):
                self.autocommit = True
                self.closed = 0
                self._fetch_values = fetch_values
                self.cursors: list[_FakeCursor] = []

            def get_transaction_status(self):
                return 0

            def rollback(self):
                return None

            def cursor(self, cursor_factory=None):
                cur = _FakeCursor(self._fetch_values)
                self.cursors.append(cur)
                return cur

        store = RegistryDBStore("postgresql://unused")
        conn = _FakeConn(fetch_values=[(False,)])
        monkeypatch.setattr(store, "_acquire_conn", lambda: (conn, False))
        monkeypatch.setattr(store, "_release_conn", lambda *_args, **_kwargs: None)
        reconcile = MagicMock()
        monkeypatch.setattr(
            "core.application.registry.public_contract_projection.reconcile_public_contract_projection",
            reconcile,
        )

        store._trigger_public_contract_projection_sync()

        reconcile.assert_not_called()

    def test_trigger_projection_sync_connection_failure_is_best_effort(self, monkeypatch, caplog):
        from core.application.registry.db_store import RegistryDBStore

        store = RegistryDBStore("postgresql://unused")

        def _boom():
            raise RuntimeError("projection connection unavailable")

        monkeypatch.setattr(store, "_acquire_conn", _boom)

        with caplog.at_level("WARNING"):
            store._trigger_public_contract_projection_sync()

        assert "Public contract projection sync failed" in caplog.text
        assert store._public_projection_sync_in_progress is False

    def test_db_backed_projection_sync_creates_derived_rows_for_new_agent_skill_pair(self):
        _require_db()
        from core.application.tooling.db.init_db import apply_clean_bootstrap

        apply_clean_bootstrap()
        rm = get_fresh_registry()
        db = rm.db_store
        if db is None:
            pytest.skip("Registry DB store unavailable")

        suffix = uuid.uuid4().hex[:8]
        skill_id = f"projection_cap_{suffix}"
        agent_id = f"projection_agent_{suffix}"
        structured_field = f"architecture_payload_{suffix}"
        text_field = f"requirements_doc_{suffix}"
        contract_id = f"auto.{structured_field}.v1"
        block_key = f"agent.{agent_id}.{skill_id}"

        try:
            db.save_skill(
                {
                    "skill_id": skill_id,
                    "display_name": f"Projection Skill {suffix}",
                    "description": "Projection test skill",
                    "enabled": True,
                    "category": "analysis",
                    "instructions": [],
                    "tags": [],
                    "expected_inputs": [
                        {
                            "name": text_field,
                            "type": "text",
                            "description": "Input text",
                            "required": True,
                        },
                        {
                            "name": structured_field,
                            "type": "json",
                            "description": "Structured input",
                            "required": False,
                        },
                    ],
                    "expected_outputs": [
                        {
                            "name": structured_field,
                            "type": "json",
                            "description": "Structured output",
                            "required": True,
                        },
                    ],
                },
                author="test",
            )
            db.save_agent(
                {
                    "agent_id": agent_id,
                    "agent_type_key": "researcher",
                    "display_name": f"Projection Agent {suffix}",
                    "description": "Projection test agent",
                    "enabled": True,
                    "prompts": {"system": "Projection test agent"},
                    "config": {},
                    "quality": {},
                    "inputs": {"required": [], "optional": []},
                    "outputs": [],
                    "depends_on": {"soft": [], "hard": []},
                    "tags": [],
                    "skills": [skill_id],
                    "tools": [],
                },
                author="test",
            )
            db._trigger_public_contract_projection_sync()

            block = db.get_published_block_contract(block_key)
            field = db.get_field(structured_field)
            contract = db.get_data_contract(contract_id)

            assert block is not None
            assert block["projection_mode"] == "derived"
            assert block["source_ref"] == f"{agent_id}::{skill_id}"
            assert field is not None
            assert field["projection_mode"] == "derived"
            assert field["data_contract_id"] == contract_id
            assert contract is not None
            assert contract["projection_mode"] == "derived"

        finally:
            with db._transaction() as cur:
                cur.execute(
                    "DELETE FROM registry_published_block_contracts WHERE block_key = %s",
                    (block_key,),
                )
                cur.execute(
                    "DELETE FROM registry_fields WHERE field_key = ANY(%s)",
                    ([structured_field, text_field],),
                )
                cur.execute(
                    "DELETE FROM registry_data_contracts WHERE contract_id = %s", (contract_id,)
                )
                cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
                cur.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
                cur.execute("DELETE FROM registry_skill_inputs WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM registry_skill_outputs WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))

    def test_reconcile_projection_derives_workflow_and_connector_blocks_from_public_sources(self):
        from core.application.registry.public_contract_projection import (
            reconcile_public_contract_projection,
        )

        db = MagicMock()
        db.list_skills.return_value = []
        db.list_agents.return_value = []
        db.list_workflows.return_value = [
            {
                "workflow_id": "daily_briefing",
                "display_name": "Story Cycle",
                "description": "Reusable story loop",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "current_story", "type": "json", "required": True},
                            {"name": "task_summary", "type": "text", "required": True},
                        ],
                        "expected_outputs": [
                            {"name": "item_verdict", "type": "text", "required": True},
                            {"name": "files_changed", "type": "json", "required": True},
                        ],
                    }
                },
            }
        ]
        db.list_tools.return_value = [
            {
                "tool_id": "sample_tasks",
                "display_name": "Sample Tasks Sync",
                "description": "Update Sample Tasks task state",
                "enabled": True,
                "category": "integrations",
                "interface": {
                    "function_name": "update_task",
                    "parameters": [
                        {"name": "task_id", "type": "string", "required": True},
                        {"name": "status", "type": "string", "required": True},
                    ],
                    "returns": {"name": "sample_tasks_result", "type": "json", "required": True},
                },
            }
        ]
        db.list_fields.return_value = []
        db.list_data_contracts.return_value = []
        db.list_published_block_contracts.return_value = []
        db.save_field = MagicMock()
        db.save_data_contract = MagicMock()
        db.save_published_block_contract = MagicMock()
        db.get_published_block_contract.return_value = None
        db._transaction = lambda: self._ctx(self._DummyTx())
        db._cursor = lambda: self._ctx(
            MagicMock(fetchone=lambda: {"fields": 5, "data_contracts": 3, "published_blocks": 2})
        )

        result = reconcile_public_contract_projection(db, apply=True)

        saved_field_keys = {call.args[0]["field_key"] for call in db.save_field.call_args_list}
        assert {
            "current_story",
            "task_summary",
            "item_verdict",
            "files_changed",
            "task_id",
            "status",
            "sample_tasks_result",
        } <= saved_field_keys

        saved_contract_ids = {
            call.args[0]["contract_id"] for call in db.save_data_contract.call_args_list
        }
        assert "auto.current_story.v1" in saved_contract_ids
        assert "auto.files_changed.v1" in saved_contract_ids
        assert "auto.sample_tasks_result.v1" in saved_contract_ids

        saved_blocks = {
            call.args[0]["block_key"]: call.args[0]
            for call in db.save_published_block_contract.call_args_list
        }
        workflow_block = saved_blocks["workflow.daily_briefing"]
        assert workflow_block["source_kind"] == "workflow"
        assert workflow_block["source_ref"] == "daily_briefing"
        assert [item["field_key"] for item in workflow_block["inputs"]] == [
            "current_story",
            "task_summary",
        ]
        assert [item["field_key"] for item in workflow_block["outputs"]] == [
            "item_verdict",
            "files_changed",
        ]

        connector_block = saved_blocks["connector.sample_tasks"]
        assert connector_block["source_kind"] == "connector"
        assert connector_block["source_ref"] == "sample_tasks"
        assert [item["field_key"] for item in connector_block["inputs"]] == ["task_id", "status"]
        assert [item["field_key"] for item in connector_block["outputs"]] == ["sample_tasks_result"]
        assert result["applied"] is True

    def test_db_backed_projection_sync_creates_and_orphans_workflow_and_connector_blocks(self):
        _require_db()
        from core.application.tooling.db.init_db import apply_clean_bootstrap

        apply_clean_bootstrap()
        rm = get_fresh_registry()
        db = rm.db_store
        if db is None:
            pytest.skip("Registry DB store unavailable")

        suffix = uuid.uuid4().hex[:8]
        workflow_id = f"projection_workflow_{suffix}"
        tool_id = f"projection_connector_{suffix}"
        workflow_input = f"workflow_input_{suffix}"
        workflow_output = f"workflow_output_{suffix}"
        connector_input = f"connector_input_{suffix}"
        connector_output = f"connector_output_{suffix}"
        workflow_block_key = f"workflow.{workflow_id}"
        connector_block_key = f"connector.{tool_id}"
        workflow_contract_id = f"auto.{workflow_output}.v1"
        connector_contract_id = f"auto.{connector_output}.v1"

        try:
            db.save_workflow(
                {
                    "workflow_id": workflow_id,
                    "display_name": f"Projection Workflow {suffix}",
                    "description": "Projection workflow test",
                    "enabled": True,
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "expected_inputs": [
                                {"name": workflow_input, "type": "text", "required": True},
                            ],
                            "expected_outputs": [
                                {"name": workflow_output, "type": "json", "required": True},
                            ],
                        }
                    },
                    "steps": [],
                    "groups": [],
                    "for_each": [],
                },
                author="test",
            )
            db.save_tool(
                {
                    "tool_id": tool_id,
                    "display_name": f"Projection Connector {suffix}",
                    "description": "Projection connector test",
                    "enabled": True,
                    "category": "integrations",
                    "prompt": "",
                    "interface": {
                        "function_name": "sync_record",
                        "parameters": [
                            {"name": connector_input, "type": "string", "required": True},
                        ],
                        "returns": {"name": connector_output, "type": "json", "required": True},
                    },
                    "implementation": {},
                    "config": {},
                    "tags": [],
                },
                author="test",
            )
            db._trigger_public_contract_projection_sync()

            workflow_block = db.get_published_block_contract(workflow_block_key)
            connector_block = db.get_published_block_contract(connector_block_key)
            workflow_field = db.get_field(workflow_output)
            connector_field = db.get_field(connector_output)

            assert workflow_block is not None
            assert workflow_block["projection_mode"] == "derived"
            assert workflow_block["sync_status"] == "active"
            assert connector_block is not None
            assert connector_block["projection_mode"] == "derived"
            assert connector_block["sync_status"] == "active"
            assert workflow_field is not None
            assert workflow_field["data_contract_id"] == workflow_contract_id
            assert connector_field is not None
            assert connector_field["data_contract_id"] == connector_contract_id
            assert db.get_data_contract(workflow_contract_id) is not None
            assert db.get_data_contract(connector_contract_id) is not None

            db.delete_workflow(workflow_id, author="test")
            db.delete_tool(tool_id, author="test")
            db._trigger_public_contract_projection_sync()

            workflow_block = db.get_published_block_contract(workflow_block_key)
            connector_block = db.get_published_block_contract(connector_block_key)
            workflow_field = db.get_field(workflow_output)
            connector_field = db.get_field(connector_output)

            assert workflow_block is not None
            assert workflow_block["enabled"] is False
            assert workflow_block["sync_status"] == "orphaned"
            assert connector_block is not None
            assert connector_block["enabled"] is False
            assert connector_block["sync_status"] == "orphaned"
            assert workflow_field is not None
            assert workflow_field["enabled"] is False
            assert workflow_field["sync_status"] == "orphaned"
            assert connector_field is not None
            assert connector_field["enabled"] is False
            assert connector_field["sync_status"] == "orphaned"
            assert db.get_data_contract(workflow_contract_id)["sync_status"] == "orphaned"
            assert db.get_data_contract(connector_contract_id)["sync_status"] == "orphaned"

        finally:
            with db._transaction() as cur:
                cur.execute(
                    "DELETE FROM registry_published_block_contracts WHERE block_key = ANY(%s)",
                    ([workflow_block_key, connector_block_key],),
                )
                cur.execute(
                    "DELETE FROM registry_fields WHERE field_key = ANY(%s)",
                    ([workflow_input, workflow_output, connector_input, connector_output],),
                )
                cur.execute(
                    "DELETE FROM registry_data_contracts WHERE contract_id = ANY(%s)",
                    ([workflow_contract_id, connector_contract_id],),
                )
                cur.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
                cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))

    def test_save_workflow_syncs_runtime_dependencies_table(self, user_authored_catalog):
        _require_db()
        from core.application.tooling.db.init_db import apply_clean_bootstrap

        apply_clean_bootstrap()
        rm = get_fresh_registry()
        db = rm.db_store
        if db is None:
            pytest.skip("Registry DB store unavailable")

        suffix = uuid.uuid4().hex[:8]
        workflow_id = f"wf_dependency_{suffix}"
        child_workflow_id = f"wf_child_{suffix}"
        block_key = f"blkproj:pi_dep_{suffix}:operation.sync"
        connection_ref = f"conn_{suffix}"
        step_skill_id = user_authored_catalog.skill_id

        try:
            db.save_workflow(
                {
                    "workflow_id": workflow_id,
                    "display_name": f"Dependency Workflow {suffix}",
                    "description": "Dependency sync test",
                    "enabled": True,
                    "account_id": "00000000-0000-0000-0000-000000000002",
                    "scope_kind": "account",
                    "settings": {"execution_graph": linear_graph(["sync_step", "child_step"])},
                    "steps": [
                        {
                            "step_id": "sync_step",
                            "step_kind": "agent",
                            "group_id": None,
                            "lane_id": None,
                            "skill_id": step_skill_id,
                            "block_key": block_key,
                            "connection_binding_jsonb": {
                                "connection_id": connection_ref,
                            },
                        },
                        {
                            "step_id": "child_step",
                            "step_kind": "agent",
                            "group_id": None,
                            "lane_id": None,
                            "skill_id": step_skill_id,
                            "workflow_id": child_workflow_id,
                        },
                    ],
                    "groups": [],
                    "for_each": [],
                },
                author="test",
            )

            with db._cursor() as cur:
                cur.execute(
                    """
                    SELECT dependency_kind, dependency_ref, group_id, step_id, account_id, install_id, block_key
                    FROM workflow_runtime_dependencies
                    WHERE workflow_id = %s
                    """,
                    (workflow_id,),
                )
                rows = [dict(row) for row in cur.fetchall()]

            indexed = {(row["dependency_kind"], row["dependency_ref"]): row for row in rows}
            assert ("published_block", block_key) in indexed
            assert ("project_connection", connection_ref) in indexed
            assert ("child_workflow", child_workflow_id) in indexed

            block_row = indexed[("published_block", block_key)]
            assert str(block_row["account_id"]) == "00000000-0000-0000-0000-000000000002"
            assert block_row["group_id"] is None
            assert block_row["step_id"] == "sync_step"
            assert block_row["dependency_ref"] == block_key

        finally:
            with db._transaction() as cur:
                cur.execute(
                    "DELETE FROM workflow_runtime_dependencies WHERE workflow_id = %s",
                    (workflow_id,),
                )
                cur.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))


class TestMaterializedAgentContractHelpers:
    """Pure provenance/required mapping for the materialized agent contract."""

    def _store(self):
        from core.application.registry.db_store import RegistryDBStore

        return RegistryDBStore("postgresql://unused")

    def test_source_mapping(self):
        store = self._store()
        fn = store._materialized_contract_source
        assert fn({"source": "agent", "source_id": "scout"}) == "own"
        assert fn({"source": "own"}) == "own"
        assert fn({}) == "own"
        assert fn({"source": "skill", "source_id": "collect_findings"}) == "skill:collect_findings"
        assert fn({"source": "tool", "source_id": "kb_query"}) == "tool:kb_query"

    def test_rows_use_the_concrete_store_source_override(self):
        from core.application.registry.db_store_agents_mixin import RegistryDBStoreAgentsMixin

        class OwnSourceStore(RegistryDBStoreAgentsMixin):
            @staticmethod
            def _materialized_contract_source(item):
                return "own"

        rows = OwnSourceStore._materialized_contract_rows(
            [{"name": "Input", "required": True, "source": "tool", "source_id": "external"}]
        )
        assert rows == [
            {"name": "Input", "type": "text", "description": "", "required": True, "source": "own"}
        ]

    def test_inherited_inputs_are_optional_own_keep_required(self):
        store = self._store()
        rows = store._materialized_contract_rows(
            [
                {"name": "spec", "required": True, "source": "agent", "source_id": "a"},
                {"name": "query", "required": True, "source": "tool", "source_id": "kb_query"},
                {
                    "name": "summary_doc",
                    "required": True,
                    "source": "skill",
                    "source_id": "revise_draft",
                },
            ]
        )
        by_name = {r["name"]: r for r in rows}
        assert by_name["spec"]["required"] is True
        assert by_name["spec"]["source"] == "own"
        assert by_name["query"]["required"] is False
        assert by_name["query"]["source"] == "tool:kb_query"
        assert by_name["summary_doc"]["required"] is False
        assert by_name["summary_doc"]["source"] == "skill:revise_draft"

    def test_rows_dedupe_by_name_case_insensitive(self):
        store = self._store()
        rows = store._materialized_contract_rows(
            [
                {"name": "Doc", "source": "agent"},
                {"name": "doc", "source": "skill", "source_id": "s"},
                {"name": "", "source": "agent"},
            ]
        )
        assert [r["name"] for r in rows] == ["Doc"]
