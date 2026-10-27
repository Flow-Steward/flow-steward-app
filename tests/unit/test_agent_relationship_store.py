from __future__ import annotations

from contextlib import contextmanager

import pytest

from core.application.registry.db_store_agents_mixin import (
    AgentRelationshipConflictError,
    AgentRelationshipValidationError,
    RegistryDBStoreAgentsMixin,
)
from core.application.registry.db_store_revisions_mixin import RegistryDBStoreRevisionsMixin


class _Cursor:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self._result = None

    def execute(self, sql, _params=None):
        statement = str(sql)
        self.sql.append(statement)
        self.calls.append((statement, _params))
        if "COALESCE(MAX(sort_order)" in statement:
            self._result = {"next": 2}
        elif "FROM registry_agents" in statement:
            self._result = {"agent_id": "agent-1", "account_id": "account-1"}
        elif "FROM registry_skills" in statement:
            self._result = [{"skill_id": value} for value in (_params or ([],))[0]]
        elif "FROM registry_tools" in statement:
            self._result = [{"tool_id": value} for value in (_params or ([],))[0]]
        elif "FROM registry_agent_skills" in statement:
            self._result = [
                {"skill_id": "base", "skill_prompt": "keep me"},
                {"skill_id": "old", "skill_prompt": ""},
            ]
        elif "FROM registry_agent_tools" in statement:
            self._result = [{"tool_id": "old-tool"}]
        elif (
            "INSERT INTO registry_agent_skills" in statement
            or "INSERT INTO registry_agent_tools" in statement
        ):
            self._result = {"agent_id": "agent-1"}
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class _Store(RegistryDBStoreAgentsMixin):
    def __init__(self) -> None:
        self.cursor = _Cursor()
        self.in_transaction = False
        self.rolled_back = False
        self.materialized = False
        self.projection_synced = False
        self.logged_snapshot = None

    @contextmanager
    def _transaction(self):
        self.in_transaction = True
        try:
            yield self.cursor
        except Exception:
            self.rolled_back = True
            raise
        finally:
            self.in_transaction = False

    @contextmanager
    def _cursor(self):
        yield self.cursor

    def get_agent_skills(self, _agent_id):
        assert self.in_transaction
        return [{"skill_id": "base"}, {"skill_id": "new"}]

    def get_agent_tools(self, _agent_id):
        assert self.in_transaction
        return [{"tool_id": "new-tool"}]

    def materialize_agent_contract(self, _agent_id):
        assert self.in_transaction
        self.materialized = True

    def _trigger_public_contract_projection_sync(self):
        assert not self.in_transaction
        self.projection_synced = True

    def _exists(self, *_args):
        return True

    def _column_exists(self, _table, column):
        return column == "account_id"

    def _resolve_agent_type_id_cur(self, _cur, _data):
        return "type-1"

    def _log_change_cur(self, _cur, _entity_type, _entity_id, _action, snapshot, _author):
        self.logged_snapshot = snapshot

    def _default_system_prompt(self, _data):
        return "system"

    def _compose_agent_skill_ids(self, data):
        return list(data.get("skills") or [])


def test_relationship_replace_mutates_and_validates_one_transaction():
    store = _Store()

    def validate_skill(_store, _agent_id, _skill_id):
        assert store.in_transaction
        return {"valid": True}

    def validate_agent(_store, _agent_id):
        assert store.in_transaction
        return {"valid": True}

    result = store.replace_agent_relationships(
        "agent-1",
        expected_skill_ids=["base", "old"],
        expected_tool_ids=["old-tool"],
        skill_ids=["base", "new"],
        tool_ids=["new-tool"],
        validate_skill_compatibility=validate_skill,
        validate_agent=validate_agent,
    )

    assert result["status"] == "updated"
    assert store.materialized is True
    assert store.projection_synced is True
    assert any("DELETE FROM registry_agent_skills" in sql for sql in store.cursor.sql)
    assert any("DELETE FROM registry_agent_tools" in sql for sql in store.cursor.sql)


@pytest.mark.parametrize("invalid_owner", ["pair", "agent"])
def test_single_skill_add_rejects_without_projection_side_effect(invalid_owner):
    store = _Store()

    def validate_pair(_store, _agent_id, _skill_id):
        assert store.in_transaction
        return {"valid": invalid_owner != "pair", "errors": ["invalid pair"]}

    def validate_agent(_store, _agent_id):
        assert store.in_transaction
        return {"valid": invalid_owner != "agent", "errors": ["invalid agent"]}

    with pytest.raises(AgentRelationshipValidationError):
        store.add_agent_skill(
            "agent-1",
            "draft-skill",
            "draft prompt",
            validate_skill_compatibility=validate_pair,
            validate_agent=validate_agent,
        )
    assert store.rolled_back
    assert not store.materialized
    assert not store.projection_synced
    assert not any("DELETE FROM registry_agent_skills" in sql for sql in store.cursor.sql)


def test_single_skill_add_commits_before_publishing_projection():
    store = _Store()

    def valid(*_args):
        assert store.in_transaction
        return {"valid": True}

    store.add_agent_skill(
        "agent-1",
        "valid-skill",
        "valid prompt",
        validate_skill_compatibility=valid,
        validate_agent=valid,
    )
    assert store.materialized
    assert store.projection_synced
    assert not store.rolled_back


def test_relationship_replace_rolls_back_both_sets_on_validation_failure():
    store = _Store()

    with pytest.raises(AgentRelationshipValidationError):
        store.replace_agent_relationships(
            "agent-1",
            expected_skill_ids=["base", "old"],
            expected_tool_ids=["old-tool"],
            skill_ids=["base", "new"],
            tool_ids=["new-tool"],
            validate_agent=lambda *_args: {"valid": False, "errors": ["invalid"]},
        )

    assert store.rolled_back is True
    assert store.projection_synced is False


def test_relationship_replace_rejects_stale_snapshot_before_mutation():
    store = _Store()

    with pytest.raises(AgentRelationshipConflictError):
        store.replace_agent_relationships(
            "agent-1",
            expected_skill_ids=["base"],
            expected_tool_ids=["old-tool"],
            skill_ids=["base", "new"],
            tool_ids=["new-tool"],
        )

    assert not any("DELETE FROM" in sql for sql in store.cursor.sql)


def test_relationship_replace_rejects_foreign_account_capability_before_mutation():
    store = _Store()
    original_execute = store.cursor.execute

    def execute(sql, params=None):
        original_execute(sql, params)
        if "FROM registry_skills" in str(sql):
            store.cursor._result = [{"skill_id": "base"}]

    store.cursor.execute = execute

    with pytest.raises(AgentRelationshipValidationError) as caught:
        store.replace_agent_relationships(
            "agent-1",
            expected_skill_ids=["base", "old"],
            expected_tool_ids=["old-tool"],
            skill_ids=["base", "foreign-skill"],
            tool_ids=["new-tool"],
        )

    assert "account scope" in str(caught.value.validation).lower()
    assert not any("DELETE FROM" in sql for sql in store.cursor.sql)


def test_full_agent_save_rejects_foreign_capabilities_before_relationship_delete():
    store = _Store()
    original_execute = store.cursor.execute

    def execute(sql, params=None):
        original_execute(sql, params)
        if "FROM registry_skills" in str(sql):
            store.cursor._result = []

    store.cursor.execute = execute

    with pytest.raises(AgentRelationshipValidationError):
        store.save_agent(
            {
                "agent_id": "agent-1",
                "display_name": "Agent",
                "account_id": "account-1",
                "skills": ["foreign-skill"],
                "tools": [],
            }
        )

    assert not any("DELETE FROM registry_agent_skills" in sql for sql in store.cursor.sql)


def test_agent_patch_save_does_not_rewrite_relationships_from_stale_snapshot():
    store = _Store()

    store.save_agent(
        {
            "agent_id": "agent-1",
            "display_name": "Agent",
            "account_id": "account-1",
            "skills": ["stale-skill"],
            "tools": ["stale-tool"],
        },
        sync_relationships=False,
    )

    assert not any(
        operation in sql
        for sql in store.cursor.sql
        for operation in (
            "DELETE FROM registry_agent_skills",
            "INSERT INTO registry_agent_skills",
            "UPDATE registry_agent_skills",
            "DELETE FROM registry_agent_tools",
            "INSERT INTO registry_agent_tools",
            "UPDATE registry_agent_tools",
        )
    )


def test_agent_save_persists_category_when_schema_column_is_available():
    store = _Store()
    store._column_exists = lambda table, column: (
        table == "registry_agents"
        and column
        in {
            "account_id",
            "category",
        }
    )

    store.save_agent(
        {
            "agent_id": "agent-1",
            "display_name": "Agent",
            "account_id": "account-1",
            "category": "quality",
        },
        sync_relationships=False,
    )

    insert_sql, insert_params = next(
        (sql, params) for sql, params in store.cursor.calls if "INSERT INTO registry_agents" in sql
    )
    assert "category" in insert_sql
    assert insert_params["category"] == "quality"


def test_agent_patch_revision_restores_materialized_post_save_relationships():
    store = _Store()

    store.save_agent(
        {
            "agent_id": "agent-1",
            "display_name": "Agent patched from stale view",
            "account_id": "account-1",
            "skills": ["stale-skill"],
            "tools": ["stale-tool"],
            "prompts": {"skill_prompts": {"stale-skill": "stale"}},
        },
        sync_relationships=False,
    )

    assert store.logged_snapshot is not None
    restored = RegistryDBStoreRevisionsMixin._normalize_agent_restore_payload(
        "agent-1", store.logged_snapshot
    )
    assert restored["skills"] == ["base", "old"]
    assert restored["tools"] == ["old-tool"]
    assert restored["prompts"]["skill_prompts"] == {
        "base": "keep me",
        "old": "",
    }


@pytest.mark.parametrize(
    "operation",
    [
        lambda store: store.add_agent_skill("agent-1", "new"),
        lambda store: store.remove_agent_skill("agent-1", "old"),
        lambda store: store.reorder_agent_skills("agent-1", ["old", "base"]),
        lambda store: store.update_skill_prompt("agent-1", "old", "prompt"),
        lambda store: store.add_agent_tool("agent-1", "new-tool"),
        lambda store: store.remove_agent_tool("agent-1", "old-tool"),
    ],
)
def test_every_relationship_writer_uses_parent_row_lock(operation):
    store = _Store()

    operation(store)

    assert any("FROM registry_agents" in sql and "FOR UPDATE" in sql for sql in store.cursor.sql)


def test_duplicate_agent_skill_is_reported_without_projection_or_delete():
    store = _Store()
    original_execute = store.cursor.execute

    def execute(sql, params=None):
        original_execute(sql, params)
        if "INSERT INTO registry_agent_skills" in str(sql):
            store.cursor._result = None

    store.cursor.execute = execute

    with pytest.raises(AgentRelationshipConflictError, match="already assigned"):
        store.add_agent_skill("agent-1", "new")

    assert store.projection_synced is False
    assert not any("DELETE FROM registry_agent_skills" in sql for sql in store.cursor.sql)
