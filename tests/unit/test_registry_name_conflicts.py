"""Agents, skills and workflows refuse taken identifiers and visible duplicate names.

One application rule (``registry_name_conflicts``) serves every writer: agent and
skill create/PUT/PATCH, workflow create/PUT/PATCH, the authoring draft save and
the group-to-child conversion. The conflict payload is the same everywhere and
names only what the caller may see.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from core.application.management import registry_name_conflicts as conflicts
from core.application.management.command_services_agent import AgentCommandService
from core.application.management.command_services_skill import SkillCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import normalize_scope
from core.application.registry.db_store_common import RegistryCreateConflictError
from core.application.workflows.workflow_where_used import WorkflowConsumer

ACCOUNT_A = "account-A"
ACCOUNT_B = "account-B"
VALID = {"valid": True, "errors": [], "warnings": []}


def _scope(account_id: str = ACCOUNT_A, **extra):
    return normalize_scope(account_id=account_id, actor_id="operator", **extra)


class _Cursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=None):
        self.db.statements.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class RegistryDB:
    """The registry-store slice the conflict rule reads, with a transaction log."""

    def __init__(self, *, agents=(), skills=(), workflows=()):
        self.agents = {row["agent_id"]: dict(row) for row in agents}
        self.skills = {row["skill_id"]: dict(row) for row in skills}
        self.workflows = {row["workflow_id"]: dict(row) for row in workflows}
        self.statements: list[tuple[str, object]] = []
        self.saved: list[tuple[str, dict, dict]] = []
        self.agents_using_skill: dict[str, list[str]] = {}
        self.depth = 0
        self.save_depths: list[int] = []
        self.conn = None

    @contextmanager
    def _transaction(self):
        self.depth += 1
        try:
            yield _Cursor(self)
        finally:
            self.depth -= 1

    def _has_active_transaction(self):
        return self.depth > 0

    def locks(self):
        return [params[0] for sql, params in self.statements if "pg_advisory_xact_lock" in sql]

    def get_agent(self, agent_id):
        row = self.agents.get(agent_id)
        return copy.deepcopy(row) if row else None

    def list_agents(self, enabled_only=False, include_trashed=False):
        return [copy.deepcopy(row) for row in self.agents.values()]

    def save_agent(self, data, author="admin", **kwargs):
        self.save_depths.append(self.depth)
        if kwargs.get("create_only") and data["agent_id"] in self.agents:
            raise RegistryCreateConflictError("agent", data["agent_id"])
        self.saved.append(("agent", copy.deepcopy(data), kwargs))
        self.agents[data["agent_id"]] = copy.deepcopy(data)

    def get_skill(self, skill_id):
        row = self.skills.get(skill_id)
        return copy.deepcopy(row) if row else None

    def list_skills(self, enabled_only=False, include_trashed=False, *, account_id=None):
        return [copy.deepcopy(row) for row in self.skills.values()]

    def save_skill(self, data, author="admin", **kwargs):
        self.save_depths.append(self.depth)
        if kwargs.get("create_only") and data["skill_id"] in self.skills:
            raise RegistryCreateConflictError("skill", data["skill_id"])
        self.saved.append(("skill", copy.deepcopy(data), kwargs))
        self.skills[data["skill_id"]] = copy.deepcopy(data)

    def get_agents_using_skill(self, skill_id):
        return list(self.agents_using_skill.get(skill_id, []))

    def materialize_agents_for_skill(self, skill_id):
        return None

    def get_workflow(self, workflow_id):
        row = self.workflows.get(workflow_id)
        return copy.deepcopy(row) if row else None

    def list_workflows(self, enabled_only=False, include_definitions=False):
        rows = [copy.deepcopy(row) for row in self.workflows.values()]
        return [row for row in rows if row.get("enabled")] if enabled_only else rows


def _agents(db):
    return AgentCommandService(
        get_db=lambda: db,
        reload_registry=MagicMock(),
        get_agent_validation=MagicMock(return_value=VALID),
        validate_skill_compatibility=MagicMock(return_value=VALID),
    )


def _skills(db):
    return SkillCommandService(
        get_db=lambda: db,
        reload_registry=MagicMock(),
        get_skill_validation=MagicMock(return_value=VALID),
    )


def _agent(agent_id, name, account_id=ACCOUNT_A, **extra):
    return {
        "agent_id": agent_id,
        "display_name": name,
        "account_id": account_id,
        "scope_kind": "account",
        "enabled": False,
        **extra,
    }


def _skill(skill_id, name, account_id=ACCOUNT_A, **extra):
    return {
        "skill_id": skill_id,
        "display_name": name,
        "account_id": account_id,
        "scope_kind": "account",
        "enabled": False,
        **extra,
    }


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Order Sync", "order sync"),
        ("  Order Sync  ", "Order Sync"),
        ("Order \t  Sync", "order sync"),
        ("ORDER\nSYNC", "Order Sync"),
        ("Straße", "STRASSE"),
    ],
)
def test_names_compare_case_insensitive_trimmed_and_collapsed(left, right):
    assert conflicts.normalize_display_name(left) == conflicts.normalize_display_name(right)


def test_distinct_names_and_scopes_do_not_share_a_lock_key():
    assert conflicts.normalize_display_name("Order Sync") != conflicts.normalize_display_name(
        "Order-Sync"
    )
    key = conflicts.display_name_lock_key
    assert key("a", "agent", "x") != key("b", "agent", "x")
    assert key("a", "agent", "x") != key("a", "skill", "x")
    assert key("a", "agent", "x") == "registry_display_name:a:agent:x"


def test_suggestion_skips_every_visible_variant_and_is_only_a_hint():
    taken = ["Writer", "writer 2", "WRITER  3"]
    assert conflicts.suggest_display_name(" Writer ", taken) == "Writer 4"


# --------------------------------------------------------------------------- #
# Agents                                                                      #
# --------------------------------------------------------------------------- #


def test_agent_create_refuses_a_visible_name_in_another_case_and_spacing():
    db = RegistryDB(agents=[_agent("writer", "Report Writer", enabled=True, updated_at="t1")])

    with pytest.raises(ServiceConflictError) as refused:
        _agents(db).create_agent(
            {"agent_id": "writer_two", "display_name": "  report   WRITER "}, scope=_scope()
        )

    payload = refused.value.payload
    assert refused.value.status_code == 409
    assert payload["code"] == conflicts.NAME_CONFLICT_CODE
    assert payload["entity_type"] == "agent"
    assert payload["field"] == "display_name"
    assert payload["display_name"] == "report WRITER"
    assert payload["matches"] == [
        {
            "id": "writer",
            "display_name": "Report Writer",
            "enabled": True,
            "updated_at": "t1",
            "origin": "account",
            "used_by": [],
            "used_by_truncated": False,
        }
    ]
    assert payload["suggested_display_name"] == "report WRITER 2"
    assert db.saved == []
    assert db.locks() == [f"registry_display_name:{ACCOUNT_A}:agent:report writer"]


def test_agent_name_of_another_account_is_neither_a_conflict_nor_revealed():
    db = RegistryDB(agents=[_agent("foreign", "Report Writer", account_id=ACCOUNT_B)])

    result = _agents(db).create_agent(
        {"agent_id": "writer", "display_name": "Report Writer"}, scope=_scope()
    )

    assert result.status == "created"
    assert db.saved[0][2] == {"create_only": True}


def test_system_and_extension_agents_count_as_visible_names(monkeypatch):
    db = RegistryDB(agents=[_agent("builtin", "Researcher", account_id="", scope_kind="system")])
    projected = {
        "agent_id": "ext.install.summarizer",
        "display_name": "Summarizer",
        "source_install_id": "install",
        "account_id": ACCOUNT_A,
        "enabled": True,
    }
    original = conflicts.projection_aware_catalog_rows
    monkeypatch.setattr(
        conflicts,
        "projection_aware_catalog_rows",
        lambda db_, kind, key, rows, **kw: (
            [*original(db_, kind, key, rows, **kw)] + ([projected] if kind == "agent" else [])
        ),
    )
    service = _agents(db)

    with pytest.raises(ServiceConflictError) as system:
        service.create_agent({"agent_id": "a1", "display_name": "researcher"}, scope=_scope())
    with pytest.raises(ServiceConflictError) as extension:
        service.create_agent({"agent_id": "a2", "display_name": "SUMMARIZER"}, scope=_scope())

    assert system.value.payload["matches"][0]["origin"] == "system"
    assert extension.value.payload["matches"][0]["origin"] == "extension"
    assert extension.value.payload["matches"][0]["id"] == "ext.install.summarizer"
    assert db.saved == []


def test_agent_patch_and_put_refuse_renaming_into_a_taken_name():
    db = RegistryDB(agents=[_agent("writer", "Writer"), _agent("editor", "Editor")])
    service = _agents(db)

    with pytest.raises(ServiceConflictError) as patched:
        service.patch_agent("editor", {"display_name": "WRITER"}, scope=_scope())
    with pytest.raises(ServiceConflictError) as replaced:
        service.update_agent("editor", {"display_name": " writer "}, scope=_scope())

    assert patched.value.payload["matches"][0]["id"] == "writer"
    assert replaced.value.payload["matches"][0]["id"] == "writer"
    assert db.saved == []


def test_existing_agent_duplicates_do_not_block_editing_other_fields():
    db = RegistryDB(agents=[_agent("writer", "Writer"), _agent("writer_old", "writer")])
    service = _agents(db)

    service.patch_agent("writer_old", {"description": "kept"}, scope=_scope())
    service.patch_agent("writer_old", {"display_name": "  WRITER "}, scope=_scope())
    service.update_agent(
        "writer", {"display_name": "Writer", "description": "replaced"}, scope=_scope()
    )

    assert [entry[1]["agent_id"] for entry in db.saved] == ["writer_old", "writer_old", "writer"]
    assert db.locks() == [], "an unchanged name takes no lock and is never refused"


def test_agent_rename_into_a_free_name_saves_under_the_name_lock():
    db = RegistryDB(agents=[_agent("writer", "Writer")])

    _agents(db).patch_agent("writer", {"display_name": "Reviewer"}, scope=_scope())

    assert db.saved[0][1]["display_name"] == "Reviewer"
    assert db.locks() == [f"registry_display_name:{ACCOUNT_A}:agent:reviewer"]
    assert db.save_depths == [1], "the lock and the save share one transaction"


def test_agent_create_refuses_a_taken_id_without_renaming_or_saving():
    db = RegistryDB(agents=[_agent("writer", "Writer", enabled=True)])

    with pytest.raises(ServiceConflictError) as refused:
        _agents(db).create_agent({"agent_id": "writer", "display_name": "Other"}, scope=_scope())

    payload = refused.value.payload
    assert payload["code"] == conflicts.ID_CONFLICT_CODE
    assert payload["field"] == "agent_id"
    assert payload["id"] == "writer"
    assert [match["id"] for match in payload["matches"]] == ["writer"]
    assert payload["suggested_id"] == "writer_1"
    assert db.saved == []


def test_an_id_held_by_another_account_conflicts_without_revealing_the_owner():
    db = RegistryDB(agents=[_agent("writer", "Secret name", account_id=ACCOUNT_B)])

    with pytest.raises(ServiceConflictError) as refused:
        _agents(db).create_agent({"agent_id": "writer", "display_name": "Mine"}, scope=_scope())

    payload = refused.value.payload
    assert payload["code"] == conflicts.ID_CONFLICT_CODE
    assert payload["matches"] == []
    assert "Secret name" not in str(payload)
    assert payload["suggested_id"] == "writer_1"


def test_an_id_taken_after_the_precheck_is_the_same_id_conflict():
    """The atomic insert, not the pre-check, decides: a lost race never overwrites."""
    db = RegistryDB()
    real_save = db.save_agent

    def racing_save(data, author="admin", **kwargs):
        db.agents[data["agent_id"]] = _agent(data["agent_id"], "Winner")
        return real_save(data, author, **kwargs)

    db.save_agent = racing_save

    with pytest.raises(ServiceConflictError) as refused:
        _agents(db).create_agent({"agent_id": "writer", "display_name": "Loser"}, scope=_scope())

    assert refused.value.payload["code"] == conflicts.ID_CONFLICT_CODE
    assert db.agents["writer"]["display_name"] == "Winner"


def test_agent_conflict_lists_only_where_used_rows_the_caller_may_see():
    db = RegistryDB(
        agents=[_agent("writer", "Writer")],
        workflows=[
            {
                "workflow_id": "mine",
                "display_name": "Mine",
                "account_id": ACCOUNT_A,
                "enabled": True,
                "steps": [{"step_id": "s", "agent_id": "writer"}],
            },
            {
                "workflow_id": "theirs",
                "display_name": "Theirs",
                "account_id": ACCOUNT_B,
                "enabled": True,
                "steps": [{"step_id": "s", "agent_id": "writer"}],
            },
            {
                "workflow_id": "restricted",
                "display_name": "Restricted project",
                "account_id": ACCOUNT_A,
                "project_id": "p2",
                "enabled": True,
                "steps": [{"step_id": "s", "agent_id": "writer"}],
            },
        ],
    )

    with pytest.raises(ServiceConflictError) as refused:
        _agents(db).create_agent(
            {"agent_id": "writer_2", "display_name": "writer"},
            scope=_scope(allowed_project_ids=["p1"]),
        )

    used_by = refused.value.payload["matches"][0]["used_by"]
    assert used_by == [
        {"entity_type": "workflow", "id": "mine", "display_name": "Mine", "enabled": True}
    ]


def test_agent_clone_still_suffixes_its_target_id():
    db = RegistryDB(agents=[_agent("writer", "Writer"), _agent("writer_copy", "Writer (Copy)")])

    result = _agents(db).clone_agent("writer", scope=_scope())

    assert result["agent_id"] == "writer_copy_1"


# --------------------------------------------------------------------------- #
# Skills                                                                      #
# --------------------------------------------------------------------------- #


def test_skill_create_refuses_a_visible_name_and_a_taken_id():
    db = RegistryDB(skills=[_skill("summary", "Summarize Report")])
    service = _skills(db)

    with pytest.raises(ServiceConflictError) as named:
        service.create_skill(
            {"skill_id": "summary_2", "display_name": "summarize report"}, scope=_scope()
        )
    with pytest.raises(ServiceConflictError) as keyed:
        service.create_skill({"skill_id": "summary", "display_name": "Other"}, scope=_scope())

    assert named.value.payload["code"] == conflicts.NAME_CONFLICT_CODE
    assert named.value.payload["entity_type"] == "skill"
    assert keyed.value.payload["code"] == conflicts.ID_CONFLICT_CODE
    assert keyed.value.payload["field"] == "skill_id"
    assert keyed.value.payload["suggested_id"] == "summary_1"
    assert db.saved == []


def test_skill_put_and_patch_refuse_a_taken_name_but_keep_existing_duplicates_editable():
    db = RegistryDB(
        skills=[
            _skill("summary", "Summary"),
            _skill("summary_old", "SUMMARY"),
            _skill("other", "Other"),
        ]
    )
    service = _skills(db)

    with pytest.raises(ServiceConflictError):
        service.patch_skill("summary_old", {"display_name": "other "}, scope=_scope())
    with pytest.raises(ServiceConflictError):
        service.update_skill(
            "summary_old",
            {"display_name": "OTHER"},
            provided_fields={"display_name"},
            scope=_scope(),
        )
    service.patch_skill("summary_old", {"description": "still editable"}, scope=_scope())

    assert [entry[1]["skill_id"] for entry in db.saved] == ["summary_old"]


def test_skill_conflict_lists_only_agents_the_caller_may_see():
    db = RegistryDB(
        skills=[_skill("summary", "Summary")],
        agents=[
            _agent("mine", "Mine", enabled=True),
            _agent("theirs", "Theirs", account_id=ACCOUNT_B),
        ],
    )
    db.agents_using_skill["summary"] = ["mine", "theirs"]

    with pytest.raises(ServiceConflictError) as refused:
        _skills(db).create_skill(
            {"skill_id": "summary_2", "display_name": "Summary"}, scope=_scope()
        )

    assert refused.value.payload["matches"][0]["used_by"] == [
        {"entity_type": "agent", "id": "mine", "display_name": "Mine", "enabled": True}
    ]


def test_skill_create_saves_create_only_under_the_requested_id():
    db = RegistryDB()

    result = _skills(db).create_skill({"skill_id": "summary", "display_name": "S"}, scope=_scope())

    assert result.skill_id == "summary"
    assert db.saved[0][2] == {"create_only": True}


# --------------------------------------------------------------------------- #
# Workflows                                                                   #
# --------------------------------------------------------------------------- #


def _workflow(workflow_id, name, account_id=ACCOUNT_A, **extra):
    return {
        "workflow_id": workflow_id,
        "display_name": name,
        "account_id": account_id,
        "enabled": False,
        **extra,
    }


def test_workflow_names_hide_hook_storage_and_foreign_or_restricted_rows():
    db = RegistryDB(
        workflows=[
            _workflow("hook_place", "Daily sync", settings={"hook_storage": {"hook_id": "h"}}),
            _workflow("foreign", "Daily sync", account_id=ACCOUNT_B),
            _workflow("other_project", "Daily sync", project_id="p2"),
        ]
    )

    with db._transaction():
        conflicts.require_free_display_name(
            db,
            entity_type="workflow",
            display_name="Daily Sync",
            scope=_scope(allowed_project_ids=["p1"]),
            entity_id="new_sync",
        )


def test_workflow_conflict_lists_only_visible_callers(monkeypatch):
    db = RegistryDB(
        workflows=[
            _workflow("child", "Child"),
            _workflow("parent", "Parent", enabled=True),
            _workflow("hidden_parent", "Hidden", project_id="p2"),
        ]
    )

    def consumers(cur, *, account_id, workflow_id):
        assert (account_id, workflow_id) == (ACCOUNT_A, "child")
        return [
            WorkflowConsumer(kind="invoke", caller_workflow_id="parent"),
            WorkflowConsumer(kind="hook", caller_workflow_id="hidden_parent"),
        ]

    monkeypatch.setattr(conflicts, "workflow_consumers", consumers)

    with pytest.raises(ServiceConflictError) as refused, db._transaction():
        conflicts.require_free_display_name(
            db,
            entity_type="workflow",
            display_name="CHILD",
            scope=_scope(allowed_project_ids=["p1"]),
            entity_id="child_2",
        )

    assert refused.value.payload["matches"][0]["used_by"] == [
        {
            "entity_type": "workflow",
            "id": "parent",
            "display_name": "Parent",
            "enabled": True,
            "relation": "invoke",
        }
    ]


def test_the_check_refuses_to_run_outside_the_saving_transaction():
    db = RegistryDB(agents=[_agent("writer", "Writer")])

    with pytest.raises(RuntimeError, match="inside the saving transaction"):
        conflicts.require_free_display_name(
            db, entity_type="agent", display_name="Writer", scope=_scope()
        )
