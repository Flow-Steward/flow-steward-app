from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

import pytest

from core.application.registry.db_store_workflows_mixin import (
    RegistryDBStoreWorkflowsMixin,
)


class _Cursor:
    def __init__(self, *, mutation_rowcount: int = 1, toggle_row: dict | None = None) -> None:
        self.mutation_rowcount = mutation_rowcount
        self.toggle_row = toggle_row
        self.last_statement = ""
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0

    def execute(self, statement: str, params: Any = ()) -> None:
        self.last_statement = " ".join(str(statement).split())
        normalized_params = tuple(params or ())
        self.executions.append((self.last_statement, normalized_params))
        if self.last_statement.startswith(("UPDATE registry_workflows SET deleted_at",)):
            self.rowcount = self.mutation_rowcount
        else:
            self.rowcount = 1

    def fetchone(self) -> dict[str, Any] | None:
        statement = self.last_statement
        if statement.startswith("SELECT * FROM registry_workflows"):
            return {
                "workflow_id": "wf-demo",
                "account_id": "account-1",
                "deleted_by": None,
                "revision": 1,
            }
        if statement.startswith("UPDATE registry_workflows SET enabled"):
            self.rowcount = 1 if self.toggle_row else 0
            return self.toggle_row
        raise AssertionError(f"Unexpected fetchone after: {statement}")

    def fetchall(self) -> list[dict[str, Any]]:
        if any(
            table in self.last_statement
            for table in (
                "registry_workflow_groups",
                "registry_workflow_for_each",
                "registry_workflow_steps",
                "workflow_entrypoints",
            )
        ):
            return []
        raise AssertionError(f"Unexpected fetchall after: {self.last_statement}")


class _WorkflowStore(RegistryDBStoreWorkflowsMixin):
    def __init__(self, toggle_row: dict | None, *, mutation_rowcount: int = 1) -> None:
        self.cursor = _Cursor(
            toggle_row=toggle_row,
            mutation_rowcount=mutation_rowcount,
        )
        self.conn = object()

    @contextmanager
    def _transaction(self):
        yield self.cursor

    def _trigger_public_contract_projection_sync(self) -> None:
        return None

    def get_workflow(self, _workflow_id: str, include_trashed: bool = False) -> dict:
        _ = include_trashed
        return {"workflow_id": "wf-demo", "account_id": "account-1"}

    def _column_exists(self, table: str, column: str) -> bool:
        return table == "registry_workflows" and column in {"deleted_at", "deleted_by"}

    def _log_change_cur(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def test_toggle_bumps_revision_in_the_same_successful_update() -> None:
    store = _WorkflowStore({"enabled": True})

    assert store.toggle_workflow("wf-demo") is True

    assert store.cursor.executions == [
        (
            "UPDATE registry_workflows SET enabled = NOT enabled, revision = revision + 1 WHERE workflow_id = %s RETURNING enabled",
            ("wf-demo",),
        )
    ]


def test_missing_toggle_row_has_no_committed_revision_change() -> None:
    store = _WorkflowStore(None)

    with pytest.raises(KeyError, match="wf-demo"):
        store.toggle_workflow("wf-demo")

    assert store.cursor.rowcount == 0


def test_soft_delete_bumps_revision_in_the_same_successful_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.application.workflows.workflow_trigger_lifecycle."
        "assert_workflow_has_no_nonterminal_jobs",
        lambda *_args, **_kwargs: None,
    )
    store = _WorkflowStore(None)

    store.delete_workflow("wf-demo", author="operator")

    delete_sql = next(
        sql
        for sql, _params in store.cursor.executions
        if sql.startswith("UPDATE registry_workflows")
    )
    assert re.search(r"deleted_at\s*=\s*now\(\)", delete_sql, re.I)
    assert re.search(r"revision\s*=\s*revision\s*\+\s*1", delete_sql)


def test_zero_row_soft_delete_fails_without_a_committed_revision_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.application.workflows.workflow_trigger_lifecycle."
        "assert_workflow_has_no_nonterminal_jobs",
        lambda *_args, **_kwargs: None,
    )
    store = _WorkflowStore(None, mutation_rowcount=0)

    with pytest.raises(KeyError, match="wf-demo"):
        store.delete_workflow("wf-demo", author="operator")

    assert store.cursor.rowcount == 0
