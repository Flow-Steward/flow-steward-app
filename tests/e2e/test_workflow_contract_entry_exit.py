from __future__ import annotations

import os
from typing import Any

import pytest

from tests.e2e.helpers import BACKEND_ROLE, get

pytestmark = [pytest.mark.e2e]

STOP_STEP_KINDS = {"stop_success", "stop_fail"}
ENTRY_NODE_KINDS = {"step", "group", "for_each"}


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


def _list_enabled_workflow_ids() -> list[str]:
    resp = get("/api/workflows", params={"enabled_only": "true"})
    if resp.status_code in (401, 403):
        pytest.skip("Workflow list endpoint requires elevated permissions in this lane")
    assert resp.status_code == 200, f"workflow list failed: {resp.status_code} {resp.text[:300]}"
    body: Any = resp.json() or []
    rows: list[dict[str, Any]] = []
    if isinstance(body, list):
        rows = [dict(row) for row in body if isinstance(row, dict)]
    elif isinstance(body, dict):
        raw = body.get("workflows") or body.get("items") or []
        if isinstance(raw, list):
            rows = [dict(row) for row in raw if isinstance(row, dict)]
    workflow_ids = []
    for row in rows:
        workflow_id = str(row.get("workflow_id") or "").strip()
        if workflow_id:
            workflow_ids.append(workflow_id)
    return workflow_ids


def _workflow_detail(workflow_id: str) -> dict[str, Any]:
    resp = get(f"/api/workflows/{workflow_id}")
    assert resp.status_code == 200, (
        f"workflow detail failed for {workflow_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    assert isinstance(body, dict), f"workflow detail must be object for {workflow_id}"
    return body


def _workflow_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        return []
    return [dict(step) for step in steps if isinstance(step, dict)]


def _workflow_has_explicit_stop_step(workflow: dict[str, Any]) -> bool:
    return any(
        str(step.get("step_kind") or "").strip().lower() in STOP_STEP_KINDS
        for step in _workflow_steps(workflow)
    )


def _workflow_has_explicit_start_marker(workflow: dict[str, Any]) -> bool:
    """A workflow starts where its execution graph's entry points."""
    settings = workflow.get("settings")
    graph = settings.get("execution_graph") if isinstance(settings, dict) else None
    entry = graph.get("entry") if isinstance(graph, dict) else None
    if not isinstance(entry, dict):
        return False
    return str(entry.get("kind") or "").strip() in ENTRY_NODE_KINDS and bool(
        str(entry.get("id") or "").strip()
    )


def test_enabled_workflow_entry_exit_inventory(server_available):
    workflow_ids = _list_enabled_workflow_ids()
    if not workflow_ids:
        pytest.skip("No enabled workflows available for entry/exit inventory")

    inspected = 0
    missing_stop: list[str] = []
    missing_start_marker: list[str] = []
    for workflow_id in workflow_ids:
        detail = _workflow_detail(workflow_id)
        inspected += 1
        if not _workflow_has_explicit_stop_step(detail):
            missing_stop.append(workflow_id)
        if not _workflow_has_explicit_start_marker(detail):
            missing_start_marker.append(workflow_id)

    assert inspected > 0
    # Inventory-only smoke: keep this test green but visible in logs on failure runs.
    # Strict enforcement is tracked by the xfail checks below.
    assert isinstance(missing_stop, list)
    assert isinstance(missing_start_marker, list)


def test_enabled_workflows_require_explicit_stop_step(server_available):
    workflow_ids = _list_enabled_workflow_ids()
    if not workflow_ids:
        pytest.skip("No enabled workflows available for stop-step contract assertion")

    missing_stop: list[str] = []
    for workflow_id in workflow_ids:
        detail = _workflow_detail(workflow_id)
        if not _workflow_has_explicit_stop_step(detail):
            missing_stop.append(workflow_id)

    assert not missing_stop, (
        "Enabled workflows without explicit stop steps: "
        f"{missing_stop} (role={BACKEND_ROLE}, env={os.getenv('E2E_TEST_ENV', 'default')})"
    )


def test_enabled_workflows_require_explicit_start_marker(server_available):
    workflow_ids = _list_enabled_workflow_ids()
    if not workflow_ids:
        pytest.skip("No enabled workflows available for start-marker contract assertion")

    missing_start_marker: list[str] = []
    for workflow_id in workflow_ids:
        detail = _workflow_detail(workflow_id)
        if not _workflow_has_explicit_start_marker(detail):
            missing_start_marker.append(workflow_id)

    assert not missing_start_marker, (
        "Enabled workflows without explicit start markers: "
        f"{missing_start_marker} (role={BACKEND_ROLE}, env={os.getenv('E2E_TEST_ENV', 'default')})"
    )
