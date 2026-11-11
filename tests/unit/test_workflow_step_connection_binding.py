"""A connector step's two copies of its connection binding never disagree after a write."""

from __future__ import annotations

from typing import Any

from core.application.workflows.workflow_step_connection_binding import (
    sync_step_connection_binding,
)


def _step(column: Any = None, connector_binding: Any = None) -> dict[str, Any]:
    connector: dict[str, Any] = {"operation_id": "export"}
    if connector_binding is not None:
        connector["connection_binding"] = connector_binding
    step: dict[str, Any] = {"step_kind": "connector", "override": {"connector": connector}}
    if column is not None:
        step["connection_binding_jsonb"] = column
    return step


def test_a_write_that_names_the_column_rewrites_the_connector_copy() -> None:
    step = _step(
        column={"connection_id": "conn_new"}, connector_binding={"connection_id": "conn_old"}
    )

    sync_step_connection_binding(step, {"connection_binding_jsonb": {"connection_id": "conn_new"}})

    assert step["override"]["connector"]["connection_binding"] == {"connection_id": "conn_new"}


def test_a_write_of_only_the_connector_copy_rewrites_the_column() -> None:
    # The case that broke: an override-only write left the column on a deleted connection,
    # which runs, readiness and saves kept using while the editor showed the new one.
    step = _step(
        column={"connection_id": "conn_deleted"}, connector_binding={"connection_id": "conn_new"}
    )
    written = {"override": {"connector": {"connection_binding": {"connection_id": "conn_new"}}}}

    sync_step_connection_binding(step, written)

    assert step["connection_binding_jsonb"] == {"connection_id": "conn_new"}


def test_clearing_the_column_clears_both() -> None:
    step = _step(column={}, connector_binding={"connection_id": "conn_old"})

    sync_step_connection_binding(step, {"connection_binding_jsonb": {}})

    assert step["connection_binding_jsonb"] == {}
    assert step["override"]["connector"]["connection_binding"] == {}


def test_a_write_that_names_neither_or_a_step_without_a_connector_is_left_alone() -> None:
    step = _step(column={"connection_id": "a"}, connector_binding={"connection_id": "b"})
    sync_step_connection_binding(step, {"display_name": "Export"})
    assert step["connection_binding_jsonb"] == {"connection_id": "a"}
    assert step["override"]["connector"]["connection_binding"] == {"connection_id": "b"}

    agent: dict[str, Any] = {"step_kind": "agent", "override": {"agent": {}}}
    sync_step_connection_binding(agent, {"connection_binding_jsonb": {"connection_id": "x"}})
    assert "connection_binding_jsonb" not in agent
