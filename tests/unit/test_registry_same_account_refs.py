"""Every reference a workflow's steps make stays in the workflow's account (ADR 0020, C4c).

The child of an Invoke step, a hook target, and the agents, skills and published
blocks a step names are the step's own account's, or platform objects (agents,
skills and blocks without an account). Code outside a request or a job (the
scheduler, retention, cleanup tools, the in-memory workflow registry) never
resolves another account's workflow in the default account. The database side is
proven in ``tests/db/test_registry_same_account_refs_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import core.infrastructure.runtime.control.scans_scheduled_jobs as scheduled_mod
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry import account_references
from core.application.registry.account_references import (
    foreign_shared_references,
    shared_object_in_account,
    step_shared_reference_ids,
    workflow_in_account,
)
from core.application.registry.workflow_account import (
    WorkflowAccountMissingError,
    bound_workflow_account,
    workflow_account_scope,
)

ACCOUNT_A = "11111111-1111-4111-8111-111111111111"
ACCOUNT_B = "22222222-2222-4222-8222-222222222222"


# --- the rule -----------------------------------------------------------------------


def test_a_workflow_row_of_another_account_is_not_the_steps_workflow() -> None:
    assert workflow_in_account({"account_id": ACCOUNT_B}, ACCOUNT_B)
    assert not workflow_in_account({"account_id": ACCOUNT_A}, ACCOUNT_B)
    # A row from an account-scoped read (a projection, a manifest) names no account.
    assert workflow_in_account({"workflow_id": "acme.child"}, ACCOUNT_B)
    with workflow_account_scope(ACCOUNT_B):
        assert not workflow_in_account({"account_id": ACCOUNT_A})
    # Nothing bound and no account named: refused, never the default account (C5).
    with pytest.raises(WorkflowAccountMissingError):
        workflow_in_account({"account_id": DEFAULT_ACCOUNT_ID})


def test_platform_objects_are_shared_and_owned_ones_are_not() -> None:
    assert shared_object_in_account({"agent_id": "acme.agent", "account_id": None}, ACCOUNT_B)
    assert shared_object_in_account({"account_id": ACCOUNT_B}, ACCOUNT_B)
    assert not shared_object_in_account({"account_id": ACCOUNT_A}, ACCOUNT_B)
    assert shared_object_in_account(SimpleNamespace(account_id=""), ACCOUNT_A)


def test_a_step_names_its_agent_skill_and_published_blocks() -> None:
    ids = step_shared_reference_ids(
        {
            "agent_id": "acme.agent",
            "skill_id": "acme.skill",
            "block_key": "acme.block",
            "override": {"published_block_key": "acme.other_block"},
        }
    )
    assert ids == {
        "agent": {"acme.agent"},
        "skill": {"acme.skill"},
        "published block": {"acme.block", "acme.other_block"},
    }
    assert step_shared_reference_ids({"step_kind": "set_variable"}) == {
        "agent": set(),
        "skill": set(),
        "published block": set(),
    }


class _ForeignCursor:
    """Answers the ownership query: ``foreign`` ids belong to another account."""

    def __init__(self, foreign: dict[str, set[str]]) -> None:
        self.foreign = foreign
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self._rows: list[dict[str, str]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.queries.append((sql, params))
        ids, _owner = params
        for _kind, table, key in account_references.STEP_SHARED_REFERENCES:
            if f"FROM {table} " in sql:
                self._rows = [{key: ref} for ref in ids if ref in self.foreign.get(table, set())]

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows


def test_saving_a_step_refuses_another_accounts_agent_skill_or_block() -> None:
    cur = _ForeignCursor(
        {
            "registry_agents": {"acme.a_agent"},
            "registry_skills": {"acme.a_skill"},
            "registry_published_block_contracts": {"acme.a_block"},
        }
    )
    messages = foreign_shared_references(
        cur,
        account_id=ACCOUNT_B,
        steps=[
            {"step_id": "one", "agent_id": "acme.a_agent", "skill_id": "acme.platform_skill"},
            {"step_id": "two", "skill_id": "acme.a_skill", "block_key": "acme.a_block"},
        ],
    )
    assert messages == [
        "Agent 'acme.a_agent' not found",
        "Skill 'acme.a_skill' not found",
        "Published block 'acme.a_block' not found",
    ]
    # The query names the step's account: a name is foreign only when no row of it
    # is the account's own or a platform one (a block key is unique per owner).
    for sql, (_ids, owner) in cur.queries:
        assert "NOT EXISTS (" in sql
        assert "(v.account_id IS NULL OR v.account_id::text = %s)" in sql
        assert owner == ACCOUNT_B


def test_a_step_without_shared_references_reads_nothing() -> None:
    cur = _ForeignCursor({})
    assert foreign_shared_references(cur, account_id=ACCOUNT_B, steps=[{"step_id": "x"}]) == []
    assert cur.queries == []


# --- the in-memory workflow registry ---------------------------------------------------


class _AccountStore:
    """A store whose workflows depend on the bound account, as the real one's do."""

    def __init__(self) -> None:
        self.rows = {
            DEFAULT_ACCOUNT_ID: {"workflow_id": "acme.review", "display_name": "Default review"},
            ACCOUNT_B: {"workflow_id": "acme.review", "display_name": "B review"},
        }

    def _row(self) -> dict | None:
        return self.rows.get(bound_workflow_account() or DEFAULT_ACCOUNT_ID)

    def list_workflows(self) -> list[dict]:
        row = self._row()
        return [dict(row)] if row else []

    def get_workflow(self, workflow_id: str) -> dict | None:
        row = self._row()
        return dict(row) if row and row["workflow_id"] == workflow_id else None

    def list_project_workflows(self, enabled_only: bool = True) -> list[dict]:
        return []


def test_the_in_memory_registry_never_answers_another_account_with_default_workflows() -> None:
    from core.application.registry.workflow_registry import WorkflowRegistry

    store = _AccountStore()
    with workflow_account_scope(ACCOUNT_B):
        # A reload under another account still caches the default account's workflows.
        registry = WorkflowRegistry(store)

    assert registry.get_workflow("acme.review").display_name == "Default review"
    with workflow_account_scope(ACCOUNT_B):
        assert registry.get_workflow("acme.review").display_name == "B review"
        assert [w.display_name for w in registry.list_all()] == ["B review"]
    with workflow_account_scope("33333333-3333-4333-8333-333333333333"):
        assert registry.get_workflow("acme.review") is None
        assert registry.list_enabled() == []
    assert [w.display_name for w in registry.list_all()] == ["Default review"]


# --- Invoke children ----------------------------------------------------------------


class _InvokeDB:
    """A parent of account B; the child id exists only in account A."""

    def __init__(self, *, child_account: str) -> None:
        self.child_account = child_account

    def get_workflow(self, workflow_id: str) -> dict | None:
        owner = bound_workflow_account() or DEFAULT_ACCOUNT_ID
        if workflow_id == "acme.parent" and owner == ACCOUNT_B:
            return {"workflow_id": "acme.parent", "account_id": ACCOUNT_B, "steps": []}
        if workflow_id == "acme.child":
            # A store that ignored the account would hand back the other account's row.
            return {
                "workflow_id": "acme.child",
                "account_id": self.child_account,
                "enabled": True,
                "settings": {"callable": {"enabled": True}},
                "steps": [],
            }
        return None


def _invoke_step() -> dict:
    return {
        "step_id": "call",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"workflow_id": "acme.child"}},
    }


def test_an_invoke_child_of_another_account_is_an_unknown_workflow() -> None:
    from core.application.registry.contract_validation_invoke import (
        validate_invoke_workflow_step,
    )

    with workflow_account_scope(ACCOUNT_B):
        result = validate_invoke_workflow_step(
            _InvokeDB(child_account=ACCOUNT_A),
            workflow_id="acme.parent",
            step=_invoke_step(),
            path_prefix="workflow.steps.call",
        )
    codes = [issue["code"] for issue in result["errors"]]
    assert "invoke.child_workflow_missing" in codes
    assert all(ACCOUNT_A not in str(issue) for issue in result["errors"])


def test_an_invoke_child_of_the_same_account_is_found() -> None:
    from core.application.registry.contract_validation_invoke import (
        validate_invoke_workflow_step,
    )

    with workflow_account_scope(ACCOUNT_B):
        result = validate_invoke_workflow_step(
            _InvokeDB(child_account=ACCOUNT_B),
            workflow_id="acme.parent",
            step=_invoke_step(),
            path_prefix="workflow.steps.call",
        )
    assert "invoke.child_workflow_missing" not in [issue["code"] for issue in result["errors"]]


# --- agents, skills and blocks at readiness -------------------------------------------


class _SharedDB:
    def get_agent(self, agent_id: str) -> dict | None:
        return {
            "acme.a_agent": {"agent_id": "acme.a_agent", "account_id": ACCOUNT_A},
            "acme.platform_agent": {"agent_id": "acme.platform_agent", "account_id": None},
            "acme.b_agent": {"agent_id": "acme.b_agent", "account_id": ACCOUNT_B},
        }.get(agent_id)

    def get_skill(self, skill_id: str) -> dict | None:
        return {"skill_id": skill_id, "account_id": ACCOUNT_A} if skill_id else None

    def get_published_block_contract(self, block_key: str) -> dict | None:
        return {"block_key": block_key, "account_id": None}


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        ("acme.a_agent", ["Agent 'acme.a_agent' not found"]),
        ("acme.platform_agent", []),
        ("acme.b_agent", []),
    ],
)
def test_readiness_reports_another_accounts_agent_as_not_found(
    agent_id: str, expected: list[str]
) -> None:
    from core.application.registry.contract_validation_workflow_readiness import (
        _foreign_step_references,
    )

    step = {"step_id": "s", "agent_id": agent_id, "block_key": "acme.platform_block"}
    assert (
        _foreign_step_references(_SharedDB(), step=step, agent_id=agent_id, account_id=ACCOUNT_B)
        == expected
    )


def test_readiness_reports_another_accounts_skill() -> None:
    from core.application.registry.contract_validation_workflow_readiness import (
        _foreign_step_references,
    )

    step = {"step_id": "s", "agent_id": "acme.b_agent", "skill_id": "acme.a_skill"}
    assert _foreign_step_references(
        _SharedDB(), step=step, agent_id="acme.b_agent", account_id=ACCOUNT_B
    ) == ["Skill 'acme.a_skill' not found"]


# --- code outside a request: the scheduler -------------------------------------------


def _schedule(account_id: str) -> dict:
    now = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    return {
        "id": "schedule-1",
        "project_id": "project-1",
        "account_id": account_id,
        "workflow_id": "acme.review",
        "inputs": {},
        "cron_expr": "* * * * *",
        "next_run_at": now,
        "admission_allowed": True,
        "admission_workflow_revision": 1,
        "current_workflow_revision": 1,
    }


def test_a_schedule_resolves_and_launches_its_workflow_in_its_own_account() -> None:
    seen: dict[str, Any] = {}

    def materialize(workflow_id: str, inputs: dict) -> tuple[dict, list[str]]:
        seen["materialize_account"] = bound_workflow_account()
        return dict(inputs), []

    def enqueue(_bl, **kwargs: Any) -> dict:
        seen["enqueue_account"] = bound_workflow_account()
        seen["job_account"] = kwargs["account_id"]
        return {"status": "enqueued", "job_id": "job-1"}

    result = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(get_project_run_config=lambda _project_id: {"project_name": "Demo"}),
        _schedule(ACCOUNT_B),
        SimpleNamespace(),
        now_utc=datetime(2026, 11, 26, 10, 0, tzinfo=UTC),
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=enqueue,
        _materialize_scheduled_job_launch_fn=materialize,
    )

    assert result["status"] == "enqueued", result
    assert seen == {
        "materialize_account": ACCOUNT_B,
        "enqueue_account": ACCOUNT_B,
        "job_account": ACCOUNT_B,
    }
    # The binding ends with the schedule run.
    assert bound_workflow_account() is None


# --- code outside a request: retention ------------------------------------------------


class _RetentionCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (True,)

    def fetchall(self):
        return [("acme.review",)]


def test_workflow_trash_retention_lists_and_purges_one_account_at_a_time() -> None:
    from core.application.tooling.ops.retention_repository import (
        _delete_trashed_row,
        _list_trashed_entity_ids,
    )

    cur = _RetentionCursor()
    ids = _list_trashed_entity_ids(
        cur,
        cache={},
        table="registry_workflows",
        id_col="workflow_id",
        account_id=ACCOUNT_B,
        days=30,
    )
    assert ids == ["acme.review"]
    select_sql, select_params = cur.statements[-1]
    assert "WHERE account_id = %s" in select_sql
    assert select_params == (ACCOUNT_B, 30)

    assert (
        _delete_trashed_row(
            cur,
            table="registry_workflows",
            id_col="workflow_id",
            entity_id="acme.review",
            account_id=ACCOUNT_B,
        )
        == 1
    )
    delete_sql, delete_params = cur.statements[-1]
    assert delete_sql.startswith("DELETE FROM registry_workflows WHERE workflow_id = %s")
    assert "AND account_id = %s" in delete_sql
    assert delete_params == ("acme.review", ACCOUNT_B)
