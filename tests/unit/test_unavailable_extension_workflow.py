"""An extension workflow whose required extension is missing is listed, never hidden.

It used to vanish: the projection row was dropped, so the Workflows list showed
one workflow fewer and its page answered 404, with nothing saying why. It now
stays listed and readable by id, disabled and marked unavailable with a reason
that names the missing extension and what to do; a run is refused with that
reason as ``workflow_not_runnable``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.extensions.projection_dependency_availability import (
    missing_dependency_reason,
    resolve_projection_dependency_availability,
)

INSTALL_ID = "install-showcase"
DEPENDENCY = "acme.required-warehouse"
WORKFLOW_ID = projection_bridge_id(INSTALL_ID, "demo_dataset_open_orders")
_ROWS = "core.application.registry.projected_artifact_rows"


def _projection(kind: str, local_id: str, id_key: str) -> dict[str, Any]:
    return {
        "artifact_kind": kind,
        "artifact_id": local_id,
        "install_id": INSTALL_ID,
        "release_id": "pr_showcase",
        "projection": {
            id_key: local_id,
            "display_name": "Demo: Dataset Open Orders",
            "enabled": True,
            "settings": {},
            "steps": [],
            "dependencies": {
                "extensions": [
                    {
                        "extension_id": DEPENDENCY,
                        "required": True,
                        "reason": "It runs the dataset through MCP Toolbox for PostgreSQL.",
                    }
                ]
            },
        },
    }


class _Store:
    def __init__(self, *, anchor: bool) -> None:
        self.extension_runtime_store = SimpleNamespace(
            list_projections=lambda **_kwargs: [
                _projection("workflow", "demo_dataset_open_orders", "workflow_id"),
                _projection("agent", "demo_agent", "agent_id"),
            ]
        )
        self._anchor = anchor

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        if not self._anchor or workflow_id != WORKFLOW_ID:
            return None
        return {
            "workflow_id": WORKFLOW_ID,
            "projection_mode": "derived",
            "source_install_id": INSTALL_ID,
            "enabled": True,
        }


@pytest.fixture
def nothing_else_installed():
    with (
        patch(
            f"{_ROWS}.active_extension_ids_for_account",
            return_value={"acme.sample-workflows"},
        ),
        patch(f"{_ROWS}.active_extension_versions_for_account", return_value={}),
    ):
        yield


def test_the_reason_names_the_missing_extension_and_what_to_do() -> None:
    text = missing_dependency_reason(
        [DEPENDENCY],
        [{"extension_id": DEPENDENCY, "min_version": "1.1.0", "reason": "It runs the query."}],
    )

    assert text == (
        f"Needs the extension {DEPENDENCY} 1.1.0 or later, which this account does not have "
        "active. Install it from Extensions > Catalog, or turn it on if it is installed. "
        "It runs the query."
    )


def test_the_resolved_availability_carries_that_reason() -> None:
    availability = resolve_projection_dependency_availability(
        _projection("workflow", "demo", "workflow_id")["projection"],
        active_extension_ids=set(),
    )

    assert availability["new_runs_blocked"] is True
    assert availability["missing_extensions"] == [DEPENDENCY]
    assert availability["availability_reason_text"].startswith(
        f"Needs the extension {DEPENDENCY}, which this account does not have active."
    )


def test_a_blocked_workflow_projection_stays_listed_disabled_with_its_reason(
    nothing_else_installed: None,
) -> None:
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    rows = projected_artifact_rows(_Store(anchor=True), "workflow", "workflow_id")

    assert [row["workflow_id"] for row in rows] == [WORKFLOW_ID]
    row = rows[0]
    assert row["enabled"] is False
    assert row["availability_status"] == "unavailable"
    assert row["availability_reason_code"] == "required_extension_uninstalled"
    assert row["missing_extensions"] == [DEPENDENCY]
    assert row["new_runs_blocked"] is True
    assert DEPENDENCY in row["availability_reason_text"]


def test_a_blocked_agent_projection_is_still_left_out(nothing_else_installed: None) -> None:
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    assert projected_artifact_rows(_Store(anchor=True), "agent", "agent_id") == []


def test_the_list_keeps_the_blocked_workflow_next_to_its_anchor(
    nothing_else_installed: None,
) -> None:
    from core.application.registry.projected_artifact_rows import projection_aware_catalog_rows

    store = _Store(anchor=True)
    rows = projection_aware_catalog_rows(
        store,
        "workflow",
        "workflow_id",
        [{"workflow_id": "manual", "enabled": True}, store.get_workflow(WORKFLOW_ID)],
    )

    by_id = {row["workflow_id"]: row for row in rows}
    assert sorted(by_id) == sorted(["manual", WORKFLOW_ID])
    assert by_id[WORKFLOW_ID]["availability_status"] == "unavailable"
    assert (
        projection_aware_catalog_rows(
            store, "workflow", "workflow_id", [store.get_workflow(WORKFLOW_ID)], enabled_only=True
        )
        == []
    )


@pytest.mark.parametrize("anchor", [True, False])
def test_the_blocked_workflow_resolves_by_id(nothing_else_installed: None, anchor: bool) -> None:
    from core.application.registry.workflow_lookup import get_workflow_row

    row = get_workflow_row(_Store(anchor=anchor), WORKFLOW_ID)

    assert row is not None
    assert row["workflow_id"] == WORKFLOW_ID
    assert row["availability_status"] == "unavailable"
    assert DEPENDENCY in row["availability_reason_text"]


def test_a_run_of_the_blocked_workflow_is_refused_as_not_runnable(monkeypatch) -> None:
    from core.application.jobs import workflow_definition_snapshot_repository as snapshots
    from core.application.management.command_services_common import _workflow_not_runnable_error

    class _Cursor:
        def execute(self, *_args: Any) -> None:
            return None

        def fetchone(self) -> dict[str, Any]:
            return {
                "id": 7,
                "workflow_id": WORKFLOW_ID,
                "revision": 1,
                "settings": {},
                "source_install_id": INSTALL_ID,
                "projection_mode": "derived",
                "local_workflow_id": "demo_dataset_open_orders",
            }

    reason = missing_dependency_reason([DEPENDENCY], [{"extension_id": DEPENDENCY}])
    monkeypatch.setattr(snapshots, "load_workflow_definition_rows", lambda _cur, _ids: {})
    repository = snapshots.WorkflowDefinitionSnapshotRepository(conn=None)
    monkeypatch.setattr(
        repository,
        "_projected_definition",
        lambda _workflow, *, account_id: {
            "workflow_id": WORKFLOW_ID,
            "steps": [{"step_id": "load"}],
            "new_runs_blocked": True,
            "availability_reason_text": reason,
        },
    )

    cursor = _Cursor()
    with pytest.raises(snapshots.WorkflowDefinitionPinError) as refused:
        repository._workflow_row(cursor, WORKFLOW_ID, account_id="acc")

    assert reason in str(refused.value)
    assert refused.value.issues[0]["code"] == "workflow.extension_dependency_missing"
    conflict = _workflow_not_runnable_error(refused.value)
    assert conflict is not None
    assert conflict.payload["code"] == "workflow_not_runnable"


_DETAIL = {
    "extension_id": DEPENDENCY,
    "display_name": "Required Warehouse",
    "state": "not_installed",
    "min_version": "",
    "max_version_exclusive": "",
}


def test_the_listed_row_carries_which_extension_is_missing_and_its_state(
    nothing_else_installed: None,
) -> None:
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    with patch(
        "core.application.extensions.projection_dependency_availability."
        "extension_installs_for_account",
        return_value={},
    ):
        [row] = projected_artifact_rows(_Store(anchor=True), "workflow", "workflow_id")

    assert row["missing_extension_details"] == [_DETAIL]


def test_the_export_policy_passes_the_structured_reason_on() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    policy = WorkflowPortabilityService(SimpleNamespace()).export_policy_for_workflow(
        {
            "workflow_id": WORKFLOW_ID,
            "source_install_id": INSTALL_ID,
            "projection_mode": "derived",
            "availability_status": "unavailable",
            "availability_reason_text": "Needs the extension acme.required-warehouse.",
            "missing_extension_details": [_DETAIL],
        }
    )
    available = WorkflowPortabilityService(SimpleNamespace()).export_policy_for_workflow(
        {"workflow_id": "manual", "missing_extension_details": [_DETAIL]}
    )

    assert policy["missing_extension_details"] == [_DETAIL]
    assert "missing_extension_details" not in available


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"auth_user": {"roles": ["admin"]}}, True),
        ({"auth_user": {"roles": ["operator"]}}, False),
        ({"auth_principal": SimpleNamespace(roles=["Admin"])}, True),
        ({}, False),
    ],
)
def test_an_unavailable_workflow_says_whether_this_viewer_can_turn_the_extension_on(
    state: dict[str, Any], expected: bool
) -> None:
    from core.entrypoints.admin.workflows_scope import _with_viewer_extension_permission

    request = SimpleNamespace(state=SimpleNamespace(**state))
    payload = _with_viewer_extension_permission(
        {
            "missing_extension_details": [_DETAIL],
            "portability": {"missing_extension_details": [_DETAIL]},
        },
        request,
    )
    untouched = _with_viewer_extension_permission({"workflow_id": "manual"}, request)

    assert payload["viewer_can_manage_extensions"] is expected
    assert payload["portability"]["viewer_can_manage_extensions"] is expected
    assert "viewer_can_manage_extensions" not in untouched
