from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext
from core.application.registry.builder_metadata import normalize_builder_metadata
from core.application.registry.db_store import RegistryDBStore


class _FailingChangelogRegistryDBStore(RegistryDBStore):
    def _log_change_cur(self, *args, **kwargs):
        super()._log_change_cur(*args, **kwargs)
        raise RuntimeError("injected failure after relationship inserts")


def _seed_dependencies(
    store: RegistryDBStore, suffix: str, disposable_db_conn
) -> tuple[str, str, str, str]:
    global_skill_id = f"clone_global_skill_{suffix}"
    account_skill_id = f"clone_account_skill_{suffix}"
    global_tool_id = f"clone_global_tool_{suffix}"
    account_tool_id = f"clone_account_tool_{suffix}"
    store.save_skill(
        {"skill_id": global_skill_id, "display_name": "Shared skill", "enabled": False},
        author="projected_clone_test",
    )
    store.save_skill(
        {
            "skill_id": account_skill_id,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "display_name": "Account skill",
            "enabled": False,
        },
        author="projected_clone_test",
    )
    for tool_id, account_id in (
        (global_tool_id, None),
        (account_tool_id, DEFAULT_ACCOUNT_ID),
    ):
        store.save_tool(
            {
                "tool_id": tool_id,
                "account_id": account_id,
                "scope_kind": "account" if account_id else "system",
                "display_name": tool_id,
                "enabled": False,
                "implementation": {"type": "internal", "module": "tests.clone_fixture"},
                "interface": {"function_name": "run", "arguments": {}},
            },
            author="projected_clone_test",
        )
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_skills SET account_id = NULL WHERE skill_id = %s", (global_skill_id,)
        )
        cur.execute(
            "UPDATE registry_tools SET account_id = NULL WHERE tool_id = %s", (global_tool_id,)
        )
    disposable_db_conn.commit()
    return global_skill_id, account_skill_id, global_tool_id, account_tool_id


def _cleanup_rows(conn, agent_ids: list[str], skill_ids: list[str], tool_ids: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM registry_changelog WHERE entity_id = ANY(%s)",
            (agent_ids + skill_ids + tool_ids,),
        )
        cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = ANY(%s)", (agent_ids,))
        cur.execute("DELETE FROM registry_agent_tools WHERE agent_id = ANY(%s)", (agent_ids,))
        cur.execute("DELETE FROM registry_agent_inputs WHERE agent_id = ANY(%s)", (agent_ids,))
        cur.execute("DELETE FROM registry_agent_outputs WHERE agent_id = ANY(%s)", (agent_ids,))
        cur.execute("DELETE FROM registry_agents WHERE agent_id = ANY(%s)", (agent_ids,))
        cur.execute("DELETE FROM registry_skills WHERE skill_id = ANY(%s)", (skill_ids,))
        cur.execute("DELETE FROM registry_tools WHERE tool_id = ANY(%s)", (tool_ids,))
    conn.commit()


@pytest.mark.integration
def test_projected_agent_clone_persists_complete_content_and_ordered_owned_relations(
    disposable_db, disposable_db_conn
):
    from core.application.management import command_services

    assert hasattr(command_services, "AgentCommandService"), "Agent clone policy service is missing"
    from core.application.management import command_services_agent as agent_clone_module

    suffix = uuid.uuid4().hex
    source_id = f"extension.clone-{suffix}.source_agent"
    target_id = f"clone_agent_{suffix}"
    store = RegistryDBStore(disposable_db.dsn)
    skill_ids: tuple[str, str] | None = None
    tool_ids: tuple[str, str] | None = None
    try:
        global_skill, account_skill, global_tool, account_tool = _seed_dependencies(
            store, suffix, disposable_db_conn
        )
        skill_ids = (global_skill, account_skill)
        tool_ids = (global_tool, account_tool)
        assert store.get_skill(global_skill)["account_id"] is None
        assert store.get_tool(global_tool)["account_id"] is None
        store.set_skill_inputs(
            global_skill,
            [
                {
                    "name": "inherited_context",
                    "type": "object",
                    "description": "From skill",
                    "required": True,
                }
            ],
        )
        store.set_skill_outputs(
            global_skill,
            [
                {
                    "name": "inherited_audit",
                    "type": "text",
                    "description": "Skill result",
                    "required": True,
                }
            ],
        )
        source = {
            "agent_id": source_id,
            "account_id": DEFAULT_ACCOUNT_ID,
            "source_install_id": f"clone-{suffix}",
            "display_name": "Projected source",
            "description": "Complete copy source",
            "prompts": {
                "system": "Keep system",
                "context_template": "Keep context",
                "skill_prompts": {
                    global_skill: "First prompt",
                    account_skill: "Second prompt",
                },
            },
            "skills": [
                {"skill_id": global_skill, "skill_prompt": "First prompt", "sort_order": 0},
                {"skill_id": account_skill, "skill_prompt": "Second prompt", "sort_order": 1},
            ],
            "tools": [global_tool, account_tool],
            "inputs": {"required": [{"name": "topic", "type": "text"}], "optional": []},
            "outputs": ["summary"],
            "structured_inputs": [
                {
                    "name": "topic",
                    "type": "text",
                    "description": "Authored input",
                    "required": True,
                },
                {
                    "name": "notes",
                    "type": "text",
                    "description": "Optional notes",
                    "required": False,
                },
            ],
            "structured_outputs": [
                {
                    "name": "summary",
                    "type": "text",
                    "description": "Authored output",
                    "required": True,
                }
            ],
            "depends_on": {"soft": [], "hard": []},
            "config": {"model": "fixture-model"},
            "quality": {"determinism": "high"},
            "builder_metadata_jsonb": {"purpose": "review"},
            "category": "marketing",
            "enabled": True,
            "version": "2.3.0",
            "tags": ["catalog", "review"],
        }
        store.save_agent(source, author="projected_clone_test")
        service = agent_clone_module.AgentCommandService(
            get_db=lambda: store, reload_registry=lambda: None
        )

        def resolve_catalog_row(_db, artifact_kind, _id_key, artifact_id, _fallback, **_kwargs):
            if artifact_kind == "agent":
                return store.get_agent(source_id)
            if artifact_kind == "skill":
                return store.get_skill(artifact_id)
            return None

        with patch.object(
            agent_clone_module,
            "projection_aware_catalog_row",
            side_effect=resolve_catalog_row,
        ):
            service.clone_agent(
                source_id,
                target_agent_id=target_id,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="clone_test"),
            )

        saved = store.get_agent(target_id)
        assert saved is not None
        assert str(saved["account_id"]) == DEFAULT_ACCOUNT_ID
        assert saved["scope_kind"] == "account"
        assert saved["enabled"] is False
        assert saved["display_name"] == "Projected source (Copy)"
        assert saved["category"] == source["category"]
        assert saved["description"] == source["description"]
        assert saved["prompts"]["system"] == "Keep system"
        assert saved["prompts"]["context_template"] == "Keep context"
        assert saved["prompts"]["skill_prompts"] == {
            global_skill: "First prompt",
            account_skill: "Second prompt",
        }
        assert [row["skill_id"] for row in saved["skills"]] == [global_skill, account_skill]
        assert [row["skill_prompt"] for row in saved["skills"]] == ["First prompt", "Second prompt"]
        assert [row["tool_id"] for row in saved["tools"]] == [global_tool, account_tool]
        assert saved["inputs"] == source["inputs"]
        assert saved["outputs"] == source["outputs"]
        own_inputs = store.get_agent_own_inputs(target_id)
        own_outputs = store.get_agent_own_outputs(target_id)
        assert [
            (row["name"], row["type"], row["description"], row["required"]) for row in own_inputs
        ] == [
            ("topic", "text", "Authored input", True),
            ("notes", "text", "Optional notes", False),
        ]
        assert [
            (row["name"], row["type"], row["description"], row["required"]) for row in own_outputs
        ] == [("summary", "text", "Authored output", True)]
        all_inputs = store.get_agent_inputs(target_id)
        all_outputs = store.get_agent_outputs(target_id)
        assert [(row["name"], row["source"]) for row in all_inputs] == [
            ("inherited_context", f"skill:{global_skill}"),
            ("topic", "own"),
            ("notes", "own"),
        ]
        assert [(row["name"], row["source"]) for row in all_outputs] == [
            ("inherited_audit", f"skill:{global_skill}"),
            ("summary", "own"),
        ]
        assert saved["config"] == source["config"]
        assert saved["quality"] == source["quality"]
        assert saved["builder_metadata_jsonb"] == normalize_builder_metadata(
            source["builder_metadata_jsonb"]
        )
        assert saved["version"] == source["version"]
        assert saved["tags"] == source["tags"]
        assert not saved.get("source_install_id")
    finally:
        if skill_ids and tool_ids:
            _cleanup_rows(
                disposable_db_conn, [source_id, target_id], list(skill_ids), list(tool_ids)
            )
        if store._pool is not None:
            store._pool.closeall()


@pytest.mark.integration
def test_projected_agent_clone_rolls_back_agent_relationships_and_changelog(
    disposable_db, disposable_db_conn
):
    from core.application.management import command_services

    assert hasattr(command_services, "AgentCommandService"), "Agent clone policy service is missing"
    from core.application.management import command_services_agent as agent_clone_module

    suffix = uuid.uuid4().hex
    source_id = f"extension.rollback-{suffix}.source_agent"
    target_id = f"rollback_agent_{suffix}"
    store = RegistryDBStore(disposable_db.dsn)
    failing_store = None
    skill_ids: tuple[str, str] | None = None
    tool_ids: tuple[str, str] | None = None
    try:
        global_skill, account_skill, global_tool, account_tool = _seed_dependencies(
            store, suffix, disposable_db_conn
        )
        skill_ids = (global_skill, account_skill)
        tool_ids = (global_tool, account_tool)
        source = {
            "agent_id": source_id,
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Rollback source",
            "category": "marketing",
            "prompts": {
                "system": "Source prompt",
                "skill_prompts": {
                    global_skill: "First rollback prompt",
                    account_skill: "Second rollback prompt",
                },
            },
            "skills": [
                {"skill_id": global_skill, "skill_prompt": "First rollback prompt"},
                {"skill_id": account_skill, "skill_prompt": "Second rollback prompt"},
            ],
            "tools": [global_tool, account_tool],
            "inputs": {"required": [], "optional": []},
            "outputs": [],
            "structured_inputs": [
                {
                    "name": "rollback_input",
                    "type": "text",
                    "description": "Rollback",
                    "required": True,
                }
            ],
            "structured_outputs": [
                {
                    "name": "rollback_output",
                    "type": "json",
                    "description": "Rollback",
                    "required": True,
                }
            ],
            "depends_on": {"soft": [], "hard": []},
        }
        store.save_agent(source, author="projected_clone_test")
        failing_store = _FailingChangelogRegistryDBStore(disposable_db.dsn)
        service = agent_clone_module.AgentCommandService(
            get_db=lambda: failing_store, reload_registry=lambda: None
        )
        with (
            patch.object(
                agent_clone_module,
                "projection_aware_catalog_row",
                side_effect=lambda *_args, **_kwargs: store.get_agent(source_id),
            ),
            pytest.raises(ServiceError, match="Failed to clone agent"),
        ):
            service.clone_agent(
                source_id,
                target_agent_id=target_id,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        with disposable_db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM registry_agents WHERE agent_id = %s", (target_id,))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_skills WHERE agent_id = %s", (target_id,)
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_tools WHERE agent_id = %s", (target_id,)
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_inputs WHERE agent_id = %s", (target_id,)
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_outputs WHERE agent_id = %s", (target_id,)
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_changelog WHERE entity_type = 'agent' AND entity_id = %s",
                (target_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        if skill_ids and tool_ids:
            _cleanup_rows(
                disposable_db_conn, [source_id, target_id], list(skill_ids), list(tool_ids)
            )
        for candidate in (store, failing_store):
            if candidate is not None and candidate._pool is not None:
                candidate._pool.closeall()
