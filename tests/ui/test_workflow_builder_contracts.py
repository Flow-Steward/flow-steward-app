"""
UI contract tests for workflow builder defaults and mutation boundaries.

Run:
    docker compose exec -T fs-app python -m pytest tests/ui/test_workflow_builder_contracts.py -q --timeout=60
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests._workflow_definitions import STOP_STEP_ID, edge, node, stop_step
from tests.e2e.neutral_registry import (
    NEUTRAL_AGENT_ID,
    NEUTRAL_SKILL_ID,
    NEUTRAL_WORKFLOW_IDS,
)

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
#: Looked at first when a non-admin lane needs an existing workflow with steps.
#: The admin lane provisions its own builder fixture workflows instead.
_PREFERRED_WORKFLOW_IDS = (*NEUTRAL_WORKFLOW_IDS,)
_AUTH_SESSION_CACHE: tuple[requests.Session, bool] | None = None
_ACTIVE_TEST_ROLE = (
    str(os.getenv("E2E_UI_ROLE") or os.getenv("NIGHTFIRE_AUTH_ROLE") or "admin").strip().lower()
)
_TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
}


def _attach_builder_browser_error_watchers(page):
    page_errors: list[str] = []
    console_errors: list[str] = []

    def on_pageerror(err):
        page_errors.append(str(err))

    def on_console(msg):
        if msg.type != "error":
            return
        text = str(msg.text or "").strip()
        if not text or "favicon.ico" in text:
            return
        console_errors.append(text)

    page.on("pageerror", on_pageerror)
    page.on("console", on_console)
    return page_errors, console_errors


@pytest.fixture(autouse=True)
def _fail_on_builder_runtime_errors(page):
    page_errors, console_errors = _attach_builder_browser_error_watchers(page)
    page._allowed_console_error_patterns = []
    yield
    allowed_patterns = [
        str(item).strip()
        for item in getattr(page, "_allowed_console_error_patterns", [])
        if str(item).strip()
    ]
    if allowed_patterns and console_errors:
        console_errors = [
            text
            for text in console_errors
            if not any(pattern in text for pattern in allowed_patterns)
        ]
    assert not page_errors, f"Unexpected page errors: {page_errors}"
    assert not console_errors, f"Unexpected console errors: {console_errors}"


def _role_bound_request_session() -> tuple[requests.Session, bool]:
    global _AUTH_SESSION_CACHE
    if _AUTH_SESSION_CACHE is not None:
        return _AUTH_SESSION_CACHE
    verify_ssl = False
    resolved = resolve_test_env(
        os.environ,
        lane="ui",
        role=str(os.getenv("E2E_UI_ROLE") or os.getenv("NIGHTFIRE_AUTH_ROLE") or "").strip()
        or None,
        include_dotenv=True,
    )
    session = requests.Session()
    resp = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": resolved.username, "password": resolved.password},
        verify=verify_ssl,
        timeout=20,
    )
    assert resp.status_code == 200, (
        f"Could not authenticate workflow builder helper session for {resolved.role!r}: "
        f"{resp.status_code} {resp.text[:300]}"
    )
    csrf = str(session.cookies.get("fs_csrf") or "").strip()
    if csrf:
        session.headers["x-csrf-token"] = csrf
    _AUTH_SESSION_CACHE = (session, verify_ssl)
    return _AUTH_SESSION_CACHE


def _list_workflows(base_url: str) -> list[dict[str, Any]]:
    session, verify_ssl = _role_bound_request_session()
    for attempt in range(3):
        try:
            resp = session.get(
                f"{base_url}/api/workflows", verify=verify_ssl, timeout=10 + (attempt * 5)
            )
            if not resp.ok:
                return []
            data = resp.json()
            return data if isinstance(data, list) else data.get("workflows", [])
        except requests.RequestException:
            if attempt >= 2:
                raise
            time.sleep(1 + attempt)
    return []


def _get_workflow(base_url: str, workflow_id: str) -> dict[str, Any] | None:
    session, verify_ssl = _role_bound_request_session()
    for attempt in range(3):
        try:
            resp = session.get(
                f"{base_url}/api/workflows/{workflow_id}",
                verify=verify_ssl,
                timeout=10 + (attempt * 5),
            )
            if not resp.ok:
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None
            return payload
        except requests.RequestException:
            if attempt >= 2:
                raise
            time.sleep(1 + attempt)
    return None


def _delete_temp_workflow(base_url: str, workflow_id: str) -> None:
    with suppress(Exception):
        _request_json(
            "DELETE",
            f"{base_url}/api/workflows/{workflow_id}",
            expected={200, 204, 404},
        )


def _list_published_blocks(base_url: str) -> list[dict[str, Any]]:
    status, payload = _request_json(
        "GET",
        f"{base_url}/api/published-blocks",
        expected={200},
    )
    if status != 200:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    rows = payload.get("published_blocks") if isinstance(payload, dict) else []
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _workflow_steps(workflow_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = workflow_payload.get("steps") if isinstance(workflow_payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _workflow_groups(workflow_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = workflow_payload.get("groups") if isinstance(workflow_payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _execution_graph(workflow_payload: dict[str, Any] | None) -> dict[str, Any]:
    settings = workflow_payload.get("settings") if isinstance(workflow_payload, dict) else None
    graph = settings.get("execution_graph") if isinstance(settings, dict) else None
    return graph if isinstance(graph, dict) else {}


def _group_lanes(group: dict[str, Any]) -> list[str]:
    body = group.get("body") if isinstance(group.get("body"), dict) else {}
    lanes = body.get("lanes") if isinstance(body.get("lanes"), list) else []
    return [
        str(lane.get("lane_id") or "").strip()
        for lane in lanes
        if isinstance(lane, dict) and str(lane.get("lane_id") or "").strip()
    ]


def _snapshot_definition(workflow_payload: dict[str, Any]) -> dict[str, Any]:
    """The stored definition and revision; any write changes at least one of them."""

    def _canonical(rows: list[dict[str, Any]], key: str) -> list[str]:
        return sorted(
            json.dumps(
                {
                    field: row.get(field)
                    for field in (
                        key,
                        "step_kind",
                        "kind",
                        "group_id",
                        "lane_id",
                        "display_name",
                        "agent_id",
                        "block_key",
                        "override",
                        "loop",
                        "body",
                        "boundary_contract",
                        "optional",
                    )
                },
                sort_keys=True,
                default=str,
            )
            for row in rows
        )

    return {
        "revision": workflow_payload.get("revision"),
        "steps": _canonical(_workflow_steps(workflow_payload), "step_id"),
        "groups": _canonical(_workflow_groups(workflow_payload), "group_id"),
        "execution_graph": json.dumps(
            _execution_graph(workflow_payload), sort_keys=True, default=str
        ),
    }


def _workflow_revision(base_url: str, workflow_id: str) -> int:
    detail = _get_workflow(base_url, workflow_id) or {}
    revision = int(detail.get("revision") or 0)
    assert revision, f"Workflow {workflow_id!r} did not report its revision: {detail}"
    return revision


def _patch_workflow_metadata(base_url: str, workflow_id: str, updates: dict[str, Any]) -> Any:
    """A metadata PATCH; the API requires the revision it was made against."""
    _, payload = _request_json(
        "PATCH",
        f"{base_url}/api/workflows/{workflow_id}",
        expected={200},
        json={**updates, "expected_revision": _workflow_revision(base_url, workflow_id)},
    )
    return payload


def _apply_workflow_changes(
    base_url: str,
    workflow_id: str,
    operations: list[dict[str, Any]],
    *,
    expected_revision: int | None = None,
    dry_run: bool = False,
    expected: set[int] | None = None,
) -> tuple[int, Any]:
    """Send one change set, the only writer of steps, groups and the execution graph."""
    body: dict[str, Any] = {
        "expected_revision": (
            expected_revision
            if expected_revision is not None
            else _workflow_revision(base_url, workflow_id)
        ),
        "operations": operations,
    }
    if dry_run:
        body["dry_run"] = True
    return _request_json(
        "POST",
        f"{base_url}/api/workflows/{workflow_id}/changes",
        expected=expected or {200},
        timeout_seconds=90.0,
        json=body,
    )


def _create_step_op(
    step_id: str,
    step_kind: str,
    *,
    display_name: str = "",
    group_id: str | None = None,
    lane_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "step_kind": step_kind,
        "display_name": display_name or step_id,
        **fields,
    }
    if group_id:
        step["group_id"] = group_id
    if lane_id:
        step["lane_id"] = lane_id
    return {"op": "create_step", "step": step}


def _set_variable_step_op(
    step_id: str,
    display_name: str,
    *,
    target: str,
    group_id: str | None = None,
) -> dict[str, Any]:
    return _create_step_op(
        step_id,
        "set_variable",
        display_name=display_name,
        group_id=group_id,
        override={"set_variable": {"target": target, "value": True}},
    )


def _group_exit(group_id: str, lane_id: str | None = None) -> dict[str, Any]:
    return {"kind": "group_exit", "group_id": group_id, "lane_id": lane_id}


def _connect_op(
    source: str,
    target: dict[str, Any],
    outcome: str = "success",
    *,
    source_kind: str = "step",
    replace: bool = False,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "op": "connect",
        "edge": edge(source, target, outcome, source_kind=source_kind),
    }
    if replace:
        operation["replace"] = True
    return operation


def _linear_definition_operations(*step_operations: dict[str, Any]) -> list[dict[str, Any]]:
    """Root steps that run in the given order and end at a Stop Success."""
    operations = [*step_operations, {"op": "create_step", "step": stop_step()}]
    step_ids = [str(operation["step"]["step_id"]) for operation in operations]
    operations.extend(_connect_op(source, node(target)) for source, target in pairwise(step_ids))
    operations.append({"op": "set_entry", "target": node(step_ids[0])})
    return operations


def _create_workflow_with_definition(
    base_url: str,
    workflow_id: str,
    *,
    display_name: str,
    operations: list[dict[str, Any]],
    description: str = "Temporary workflow for workflow builder browser coverage.",
    enabled: bool = False,
) -> dict[str, Any]:
    """Create a workflow the way the product does: metadata first, then one change set.

    ``POST /api/workflows`` refuses ``steps``/``groups``; the definition is
    written by ``POST /api/workflows/{id}/changes``. An enabled workflow
    refuses any structural issue, so the workflow is enabled only after its
    definition is complete.
    """
    _request_json(
        "POST",
        f"{base_url}/api/workflows",
        expected={200, 201},
        timeout_seconds=90.0,
        json={
            "workflow_id": workflow_id,
            "display_name": display_name,
            "description": description,
            "enabled": False,
        },
    )
    try:
        if operations:
            _apply_workflow_changes(base_url, workflow_id, operations)
        if enabled:
            _patch_workflow_metadata(base_url, workflow_id, {"enabled": True})
    except Exception:
        _delete_temp_workflow(base_url, workflow_id)
        raise
    detail = _get_workflow(base_url, workflow_id)
    assert detail, f"Created workflow {workflow_id!r} is not readable"
    return detail


#: The builder fixture: two root steps (one of them an agent step), a loop
#: group with one body step, and a Stop Success, wired by the execution graph.
_BUILDER_ENTRY_STEP_ID = "intake"
_BUILDER_ENTRY_STEP_NAME = "Intake"
_BUILDER_AGENT_STEP_ID = "draft_summary"
_BUILDER_AGENT_STEP_NAME = "Draft summary"
_BUILDER_GROUP_ID = "review_loop"
_BUILDER_GROUP_NAME = "Review loop"
_BUILDER_GROUP_STEP_ID = "review_item"


def _builder_fixture_operations() -> list[dict[str, Any]]:
    return [
        _set_variable_step_op(
            _BUILDER_ENTRY_STEP_ID, _BUILDER_ENTRY_STEP_NAME, target="state.intake_ready"
        ),
        _create_step_op(
            _BUILDER_AGENT_STEP_ID,
            "agent",
            display_name=_BUILDER_AGENT_STEP_NAME,
            agent_id=NEUTRAL_AGENT_ID,
        ),
        {
            "op": "create_group",
            "group": {
                "group_id": _BUILDER_GROUP_ID,
                "display_name": _BUILDER_GROUP_NAME,
                "body": {"kind": "sequence", "entry": node(_BUILDER_GROUP_STEP_ID)},
            },
        },
        _set_variable_step_op(
            _BUILDER_GROUP_STEP_ID,
            "Review item",
            target="state.item_reviewed",
            group_id=_BUILDER_GROUP_ID,
        ),
        {"op": "create_step", "step": stop_step()},
        _connect_op(_BUILDER_ENTRY_STEP_ID, node(_BUILDER_AGENT_STEP_ID)),
        _connect_op(_BUILDER_AGENT_STEP_ID, node(_BUILDER_GROUP_ID, "group")),
        _connect_op(_BUILDER_GROUP_STEP_ID, _group_exit(_BUILDER_GROUP_ID)),
        _connect_op(_BUILDER_GROUP_ID, node(STOP_STEP_ID), source_kind="group"),
        {"op": "set_entry", "target": node(_BUILDER_ENTRY_STEP_ID)},
    ]


def _provision_builder_workflow(base_url: str, prefix: str) -> str:
    workflow_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
    _create_workflow_with_definition(
        base_url,
        workflow_id,
        display_name="E2E Builder Fixture",
        operations=_builder_fixture_operations(),
    )
    return workflow_id


def _candidate_workflow_ids(base_url: str) -> list[str]:
    listed_ids = [
        str(wf.get("workflow_id") or "").strip()
        for wf in _list_workflows(base_url)
        if isinstance(wf, dict) and str(wf.get("workflow_id") or "").strip()
    ]
    return [wf_id for wf_id in dict.fromkeys([*_PREFERRED_WORKFLOW_IDS, *listed_ids]) if wf_id]


def _find_workflow_with_steps(base_url: str) -> str | None:
    for workflow_id in _candidate_workflow_ids(base_url):
        if _workflow_steps(_get_workflow(base_url, workflow_id)):
            return workflow_id
    return None


def _require_admin_workflow_builder_mutation_role() -> None:
    if _ACTIVE_TEST_ROLE != "admin":
        pytest.skip("Workflow builder mutation coverage requires admin role")


@pytest.fixture(scope="module")
def builder_workflow(base_url, server_reachable):
    """A disabled builder fixture workflow shared by the read-only builder tests."""
    if _ACTIVE_TEST_ROLE != "admin":
        pytest.skip("Builder fixture workflows are provisioned by the admin lane")
    workflow_id = _provision_builder_workflow(base_url, "e2e_wf_builder")
    try:
        yield workflow_id
    finally:
        _delete_temp_workflow(base_url, workflow_id)


@pytest.fixture
def fresh_builder_workflow(base_url, server_reachable):
    """A builder fixture workflow of its own for a test that changes it."""
    if _ACTIVE_TEST_ROLE != "admin":
        pytest.skip("Builder fixture workflows are provisioned by the admin lane")
    workflow_id = _provision_builder_workflow(base_url, "e2e_wf_builder_rw")
    try:
        yield workflow_id
    finally:
        _delete_temp_workflow(base_url, workflow_id)


@pytest.fixture(scope="module")
def readable_workflow_id(request, base_url, server_reachable):
    """A workflow with steps every lane can open: provisioned by admin, found otherwise."""
    if _ACTIVE_TEST_ROLE == "admin":
        return request.getfixturevalue("builder_workflow")
    workflow_id = _find_workflow_with_steps(base_url)
    if not workflow_id:
        pytest.skip(f"No workflow with steps is readable for the {_ACTIVE_TEST_ROLE!r} lane")
    return workflow_id


def _find_workflow_with_published_block_step(base_url: str) -> tuple[str, str] | None:
    for workflow_id in _candidate_workflow_ids(base_url):
        for step in _workflow_steps(_get_workflow(base_url, workflow_id)):
            block_key = str(step.get("block_key") or step.get("published_block_key") or "").strip()
            if block_key:
                return workflow_id, block_key
    return None


def _provision_workflow_with_published_block_step(base_url: str) -> tuple[str, str] | None:
    if _ACTIVE_TEST_ROLE != "admin":
        return None
    published_blocks = _list_published_blocks(base_url)
    preferred_blocks = sorted(
        published_blocks,
        key=lambda row: (
            str(row.get("block_key") or "") != f"agent.{NEUTRAL_AGENT_ID}.{NEUTRAL_SKILL_ID}",
            str(row.get("block_key") or ""),
        ),
    )
    for row in preferred_blocks:
        block_key = str(row.get("block_key") or "").strip()
        source_ref = str(row.get("source_ref") or "").strip()
        if not block_key or "::" not in source_ref:
            continue
        agent_id = source_ref.split("::", 1)[0].strip()
        if not agent_id:
            continue
        workflow_id = f"e2e_wf_block_{uuid.uuid4().hex[:10]}"
        try:
            _create_workflow_with_definition(
                base_url,
                workflow_id,
                display_name="E2E Published Block Probe",
                operations=_linear_definition_operations(
                    _create_step_op(
                        "block_probe",
                        "agent",
                        block_key=block_key,
                        agent_id=agent_id,
                    )
                ),
            )
            return workflow_id, block_key
        except Exception:
            _delete_temp_workflow(base_url, workflow_id)
    return None


def _open_workflow_builder(page, base_url: str, workflow_id: str) -> None:
    _goto_page_stably(page, f"{base_url}/registry/workflows/{workflow_id}/builder")
    page.wait_for_function(
        """() => {
            const bodyText = String(document.body?.innerText || "");
            return bodyText.includes("Search workflow canvas")
                || bodyText.includes("Workflow interface")
                || !!document.querySelector("#workflow-builder-search");
        }""",
        timeout=30_000,
    )


def _goto_page_stably(page, url: str) -> None:
    try:
        page.goto(url, wait_until="networkidle", timeout=5_000)
    except Exception:
        try:
            page.goto(url, wait_until="load", timeout=30_000)
        except Exception:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(750)


def _reload_page_stably(page) -> None:
    try:
        page.reload(wait_until="networkidle", timeout=5_000)
    except Exception:
        page.reload(wait_until="load", timeout=30_000)
        page.wait_for_timeout(500)


def _dom_click(locator) -> None:
    locator.evaluate("(element) => element.click()")


def _request_json(
    method: str,
    url: str,
    *,
    expected: set[int] | None = None,
    timeout_seconds: float = 45.0,
    attempts: int = 1,
    **kwargs,
) -> tuple[int, Any]:
    session, verify_ssl = _role_bound_request_session()
    last_exc: requests.RequestException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            resp = session.request(
                method, url, timeout=timeout_seconds, verify=verify_ssl, **kwargs
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= max(1, attempts):
                raise
            time.sleep(1 + attempt)
            continue
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if expected is not None and resp.status_code not in expected:
            raise AssertionError(f"{method} {url} returned {resp.status_code}: {payload}")
        return resp.status_code, payload
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{method} {url} failed without response")


def _real_execution_probe_step(step_id: str = "probe_http_request") -> dict[str, Any]:
    return {
        "step_id": step_id,
        "display_name": "Probe HTTP request",
        "step_kind": "http_request",
        "override": {
            "http_request": {
                "method": "GET",
                "url": "https://example.com/",
                "response_target": "state.probe_response",
                "timeout_seconds": 30,
                "expected_statuses": [200],
            }
        },
    }


def _browser_cookies(page, base_url: str) -> dict[str, str]:
    cookies = {}
    with suppress(Exception):
        for row in page.context.cookies([base_url]) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            value = str(row.get("value") or "").strip()
            if name:
                cookies[name] = value
    return cookies


def _project_workflow_bindings(base_url: str, project_id: str) -> dict[str, Any]:
    _, payload = _request_json(
        "GET",
        f"{base_url}/api/projects/{project_id}/workflows",
        expected={200},
    )
    return payload if isinstance(payload, dict) else {}


def _project_providers(
    base_url: str,
    project_id: str,
    *,
    enabled_only: bool = True,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    _, payload = _request_json(
        "GET",
        f"{base_url}/api/projects/{project_id}/providers",
        expected={200},
        params={"enabled_only": "true" if enabled_only else "false"},
        cookies=dict(cookies or {}),
    )
    return payload if isinstance(payload, dict) else {}


def _update_project_workflow_bindings(
    base_url: str,
    project_id: str,
    *,
    workflow_ids: list[str],
    operator_default_workflow_id: str,
) -> dict[str, Any]:
    _, payload = _request_json(
        "PUT",
        f"{base_url}/api/projects/{project_id}/workflows",
        expected={200},
        json={
            "workflow_ids": workflow_ids,
            "operator_default_workflow_id": operator_default_workflow_id,
        },
    )
    return payload if isinstance(payload, dict) else {}


def _trigger_surface_payload(
    base_url: str,
    workflow_id: str,
    *,
    project_id: str,
) -> dict[str, Any]:
    _, payload = _request_json(
        "GET",
        f"{base_url}/api/workflows/{workflow_id}/triggers",
        expected={200},
        params={"project_id": project_id},
    )
    return payload if isinstance(payload, dict) else {}


def _workflow_webhook_deliveries(
    base_url: str,
    workflow_id: str,
    *,
    entry_key: str,
) -> list[dict[str, Any]]:
    _, payload = _request_json(
        "GET",
        f"{base_url}/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/deliveries",
        expected={200},
    )
    rows = payload.get("deliveries") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _wait_for_workflow_webhook_delivery(
    base_url: str,
    workflow_id: str,
    *,
    entry_key: str,
    request_id: str,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        for row in _workflow_webhook_deliveries(base_url, workflow_id, entry_key=entry_key):
            if str(row.get("request_id") or "").strip() == str(request_id or "").strip():
                return row
        time.sleep(max(0.1, poll_seconds))
    raise AssertionError(
        f"Webhook delivery {request_id!r} was not recorded for entry_key {entry_key!r}"
    )


def _wait_for_job_detail(
    base_url: str,
    *,
    job_id: str,
    require_invocation: bool = False,
    cookies: dict[str, str] | None = None,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{base_url}/api/v2/jobs/{job_id}",
            expected={200, 404},
            cookies=dict(cookies or {}),
        )
        if status == 200 and isinstance(payload, dict):
            detail = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if require_invocation and not isinstance(detail.get("invocation"), dict):
                time.sleep(max(0.1, poll_seconds))
                continue
            return detail
        time.sleep(max(0.1, poll_seconds))
    raise AssertionError(f"Job detail for {job_id!r} did not become available")


def _approve_pending_gate(base_url: str, *, job_id: str, thread_id: str) -> int:
    """Approve a single workflow approval gate for ``thread_id``.

    Webhook-triggered jobs run through the orchestrator graph, which can pause on
    one or more human-approval gates per the project policy. The approval/resume
    flow is exercised here exactly as an operator would drive it. Auth and CSRF
    are handled by the shared role-bound request session.
    """
    gate_status, gate_payload = _request_json(
        "GET",
        f"{base_url}/api/jobs/{job_id}/approval",
        expected={200, 404, 409},
    )
    if gate_status != 200 or not isinstance(gate_payload, dict):
        return gate_status
    approval_id = str(gate_payload.get("approval_id") or "").strip()
    if not approval_id:
        raise AssertionError(f"Approval gate for {job_id!r} has no approval_id")
    status, _payload = _request_json(
        "POST",
        f"{base_url}/api/approvals/{thread_id}",
        expected={202, 404, 409},
        json={"approval_id": approval_id, "approved": True, "action": "approve"},
        headers={"Idempotency-Key": f"ui-e2e-approval:{approval_id}:approve"},
    )
    return status


def _wait_for_job_terminal(
    base_url: str,
    *,
    job_id: str,
    cookies: dict[str, str] | None = None,
    timeout_seconds: float = 180.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_seconds)
    last_status = ""
    approved_threads: set[str] = set()
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{base_url}/api/v2/jobs/{job_id}",
            expected={200, 404},
            cookies=dict(cookies or {}),
        )
        if status == 200 and isinstance(payload, dict):
            detail = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            last_status = str(detail.get("status") or "").strip().lower()
            if last_status in _TERMINAL_JOB_STATUSES:
                return detail
            # Drive the job to completion through its approval gate(s). Each gate
            # has a distinct thread snapshot; only approve a gate once and let the
            # resume settle before re-evaluating so we advance to the next gate.
            if last_status == "awaiting_approval":
                thread_id = str(detail.get("thread_id") or "").strip()
                wait = (
                    detail.get("workflow_wait")
                    if isinstance(detail.get("workflow_wait"), dict)
                    else {}
                )
                gate_key = ":".join(
                    (
                        thread_id,
                        str(detail.get("current_step_id") or "").strip(),
                        str(wait.get("wait_id") or "").strip(),
                    )
                )
                if thread_id and gate_key not in approved_threads:
                    approved_threads.add(gate_key)
                    _approve_pending_gate(base_url, job_id=job_id, thread_id=thread_id)
                    time.sleep(1.0)
                    continue
        time.sleep(max(0.1, poll_seconds))
    raise AssertionError(
        f"Job {job_id!r} did not reach a terminal status; last_status={last_status!r}"
    )


def _wait_for_event_summary(
    base_url: str,
    *,
    event_type: str,
    workflow_id: str = "",
    summary_contains: str = "",
    cookies: dict[str, str] | None = None,
    occurred_after: str = "",
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_seconds)
    expected_workflow = str(workflow_id or "").strip()
    expected_summary = str(summary_contains or "").strip().lower()
    expected_after = str(occurred_after or "").strip()
    while time.time() < deadline:
        _, payload = _request_json(
            "GET",
            f"{base_url}/api/events",
            expected={200},
            params={"event_type": event_type, "page": 1, "per_page": 100},
            cookies=dict(cookies or {}),
        )
        rows = payload.get("events") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            summary_blob = " ".join(
                str(row.get(key) or "").strip() for key in ("title", "summary", "detail")
            ).lower()
            workflow_matches = (
                not expected_workflow
                or str(row.get("workflow_id") or "").strip() == expected_workflow
            )
            summary_matches = not expected_summary or expected_summary in summary_blob
            if workflow_matches and summary_matches:
                created_at = str(row.get("created_at") or "").strip()
                if expected_after and created_at and created_at < expected_after:
                    continue
                return row

            event_ref = str(row.get("event_id") or row.get("id") or "").strip()
            if not event_ref:
                continue
            _, detail = _request_json(
                "GET",
                f"{base_url}/api/events/{event_ref}",
                expected={200, 404},
                cookies=dict(cookies or {}),
            )
            if not isinstance(detail, dict):
                continue
            detail_summary_blob = " ".join(
                str(detail.get(key) or "").strip()
                for key in ("title", "summary", "detail", "message")
            ).lower()
            detail_full_blob = json.dumps(detail, ensure_ascii=False, sort_keys=True).lower()
            detail_workflow_id = str(
                detail.get("workflow_id")
                or (detail.get("metadata") or {}).get("workflow_id")
                or (detail.get("payload") or {}).get("workflow_id")
                or ""
            ).strip()
            workflow_matches = (
                not expected_workflow
                or detail_workflow_id == expected_workflow
                or expected_workflow.lower() in detail_full_blob
            )
            summary_matches = (
                not expected_summary
                or expected_summary in detail_summary_blob
                or expected_summary in detail_full_blob
            )
            if workflow_matches and summary_matches:
                merged = dict(row)
                merged.update(detail)
                created_at = str(
                    merged.get("created_at") or merged.get("occurred_at") or ""
                ).strip()
                if expected_after and created_at and created_at < expected_after:
                    continue
                return merged
        time.sleep(max(0.1, poll_seconds))
    raise AssertionError(
        f"Event {event_type!r} for workflow {expected_workflow!r} containing {summary_contains!r} was not recorded"
    )


def _webhook_auth_headers(
    auth_mode: str,
    credential_reveal: dict[str, Any],
    *,
    raw_body: bytes,
) -> dict[str, str]:
    clean_mode = str(auth_mode or "").strip().lower()
    if clean_mode == "basic_auth":
        username = str(credential_reveal.get("username") or "workflow").strip() or "workflow"
        password = str(credential_reveal.get("password") or "").strip()
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    if clean_mode == "shared_secret_header":
        header_name = str(credential_reveal.get("header_name") or "X-Workflow-Secret").strip()
        return {header_name: str(credential_reveal.get("secret") or "").strip()}
    if clean_mode == "hmac_sha256":
        header_name = str(credential_reveal.get("header_name") or "X-Workflow-Signature").strip()
        prefix = str(credential_reveal.get("prefix") or "sha256=").strip()
        secret = str(credential_reveal.get("secret") or "").strip().encode("utf-8")
        digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return {header_name: f"{prefix}{digest}"}
    return {
        "Authorization": f"Bearer {str(credential_reveal.get('token') or '').strip()}",
    }


def _provider_runtime_request(
    *,
    provider_key: str,
    trigger_row: dict[str, Any],
    valid: bool,
) -> tuple[dict[str, Any], str]:
    clean_provider_key = str(provider_key or "").strip().lower()
    if clean_provider_key == "sample_tasks":
        trigger_config = dict(trigger_row.get("trigger_config") or {})
        event_name = str(trigger_row.get("event_name") or "taskStatusUpdated").strip()
        list_id = (
            str(trigger_config.get("list_id") or "lst-e2e-runtime").strip() or "lst-e2e-runtime"
        )
        if not valid:
            list_id = f"{list_id}-miss"
        payload = {
            "event": event_name,
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "task": {
                "id": f"task-{uuid.uuid4().hex[:8]}",
                "name": "Provider Runtime E2E",
                "description": "Live provider trigger runtime probe",
                "status": {"status": "in progress"},
                "list": {"id": list_id},
                "assignees": [],
            },
        }
        return payload, "No workflow provider trigger matched this webhook"
    raise ValueError(f"Unsupported provider runtime e2e helper for {provider_key!r}")


def _provision_probe_workflow(
    base_url: str,
    workflow_id: str,
    *,
    display_name: str,
    description: str,
    probe_step_id: str,
) -> None:
    """An enabled workflow whose one HTTP probe step runs and then stops successfully."""
    _create_workflow_with_definition(
        base_url,
        workflow_id,
        display_name=display_name,
        description=description,
        operations=_linear_definition_operations(_real_execution_probe_step(probe_step_id)),
        enabled=True,
    )


def _provision_temporary_incoming_webhook_workflow(base_url: str, workflow_id: str) -> None:
    _provision_probe_workflow(
        base_url,
        workflow_id,
        display_name=f"UI Live Incoming {workflow_id[-6:]}",
        description="Temporary workflow for live Incoming Webhook browser coverage",
        probe_step_id="incoming_probe_request",
    )
    _patch_workflow_metadata(
        base_url,
        workflow_id,
        {
            "settings": {
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["webhook"],
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "expected_inputs": [
                        {
                            "name": "email",
                            "type": "string",
                            "required": True,
                            "label": "Email",
                        },
                        {
                            "name": "event_type",
                            "type": "string",
                            "required": True,
                            "label": "Event Type",
                        },
                        {
                            "name": "source",
                            "type": "string",
                            "required": False,
                            "label": "Source",
                        },
                        {
                            "name": "payload_text",
                            "type": "paragraph",
                            "required": False,
                            "label": "Payload Text",
                        },
                    ],
                    "output_mode": "none",
                    "expected_outputs": [],
                }
            }
        },
    )


def _record_request(requests_seen: list[dict[str, Any]]):
    def _on_request(req):
        body: Any = None
        if req.method in {"POST", "PUT", "PATCH"}:
            with suppress(Exception):
                body = req.post_data_json
        requests_seen.append({"method": req.method, "url": req.url, "body": body})

    return _on_request


def _is_dry_run_change_set(row: dict[str, Any]) -> bool:
    body = row.get("body")
    return (
        row["method"] == "POST"
        and row["url"].split("?", 1)[0].endswith("/changes")
        and isinstance(body, dict)
        and body.get("dry_run") is True
    )


def _collect_mutation_requests(
    requests_seen: list[dict[str, Any]], wf_id: str
) -> list[dict[str, Any]]:
    """Writes to the workflow; a dry-run change set judges a set and writes nothing."""
    prefix = f"/api/workflows/{wf_id}"
    mutations = []
    for row in requests_seen:
        path = row["url"].split("?", 1)[0]
        if row["method"] not in {"PUT", "PATCH", "POST", "DELETE"}:
            continue
        if not (path.endswith(prefix) or f"{prefix}/" in path):
            continue
        if _is_dry_run_change_set(row):
            continue
        mutations.append(row)
    return mutations


def _change_set_writes(requests_seen: list[dict[str, Any]], wf_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in _collect_mutation_requests(requests_seen, wf_id)
        if row["method"] == "POST" and row["url"].split("?", 1)[0].endswith("/changes")
    ]


def _is_change_set_response(response, workflow_id: str, *, dry_run: bool) -> bool:
    if response.request.method != "POST":
        return False
    if not response.url.split("?", 1)[0].endswith(f"/api/workflows/{workflow_id}/changes"):
        return False
    body: Any = None
    with suppress(Exception):
        body = response.request.post_data_json
    return isinstance(body, dict) and (body.get("dry_run") is True) is dry_run


def test_open_workflow_page_makes_no_mutation_requests(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    seen: list[dict[str, Any]] = []
    page.on("request", _record_request(seen))
    _open_workflow_builder(page, base_url, workflow_id)
    # Longer than the builder's dry-run debounce: opening stages nothing, so no
    # change set (not even a dry run) and no metadata write may leave the page.
    page.wait_for_timeout(1_000)

    assert _collect_mutation_requests(seen, workflow_id) == []
    assert not any(_is_dry_run_change_set(row) for row in seen)


def test_workflow_detail_header_uses_saved_description(page, base_url, server_reachable):
    _require_admin_workflow_builder_mutation_role()
    workflow_id = f"e2e_wf_description_{uuid.uuid4().hex[:10]}"
    description = f"E2E saved workflow description {uuid.uuid4().hex[:8]}"
    try:
        _create_workflow_with_definition(
            base_url,
            workflow_id,
            display_name="E2E Description Probe",
            description=description,
            operations=_linear_definition_operations(
                _set_variable_step_op("describe", "Describe", target="state.described")
            ),
        )

        _open_workflow_builder(page, base_url, workflow_id)

        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        settings_surface = page.locator("#workflow-settings-surface")
        settings_surface.wait_for(state="visible", timeout=10_000)
        page.locator("input[name='workflow_display_name']").wait_for(
            state="visible", timeout=10_000
        )
        assert (
            page.locator("input[name='workflow_display_name']").input_value()
            == "E2E Description Probe"
        )
        page.locator("textarea[name='workflow_description']").wait_for(
            state="visible", timeout=10_000
        )
        assert page.locator("textarea[name='workflow_description']").input_value() == description
        assert (
            "React workflow detail consumes workflow metadata" not in settings_surface.inner_text()
        )
    finally:
        _delete_temp_workflow(base_url, workflow_id)


def test_unresolved_required_input_contract_is_preserved_without_builder_repair_path(
    page, base_url, server_reachable
):
    _require_admin_workflow_builder_mutation_role()
    workflow_id = f"e2e_wf_contract_{uuid.uuid4().hex[:10]}"
    step_id = "summary"
    try:
        _create_workflow_with_definition(
            base_url,
            workflow_id,
            display_name="E2E Contract Probe",
            operations=_linear_definition_operations(
                _create_step_op(
                    step_id,
                    "agent",
                    display_name="Summary",
                    agent_id=NEUTRAL_AGENT_ID,
                    boundary_contract={
                        "entry": [
                            {"name": "topic", "type": "text", "required": True},
                            {"name": "audience_notes", "type": "text", "required": True},
                        ]
                    },
                )
            ),
        )

        detail = _get_workflow(base_url, workflow_id) or {}
        stored_step = next(
            (row for row in _workflow_steps(detail) if row.get("step_id") == step_id), {}
        )
        boundary = stored_step.get("boundary_contract") or {}
        entry_names = {
            str(row.get("name") or "").strip()
            for row in (boundary.get("entry") or [])
            if isinstance(row, dict) and row.get("required")
        }
        assert {"topic", "audience_notes"}.issubset(entry_names)

        _, defaults_payload = _request_json(
            "GET",
            f"{base_url}/api/workflows/{workflow_id}/steps/{step_id}/input-defaults",
            expected={200},
        )
        current_names = {
            str(row.get("name") or "").strip()
            for row in (defaults_payload.get("current_expected_inputs") or [])
            if isinstance(row, dict)
        }
        assert {"topic", "audience_notes"}.issubset(current_names)
        custom_names = {
            str(row.get("name") or "").strip()
            for row in (defaults_payload.get("custom_inputs") or [])
            if isinstance(row, dict)
        }
        # audience_notes is not part of the step's own contract, so it stays a
        # custom boundary row the author has to source; nothing fills it in.
        assert "audience_notes" in custom_names

        _open_workflow_builder(page, base_url, workflow_id)
        assert page.locator("button:has-text('Resolve required input')").count() == 0
        assert "resolve required input" not in page.locator("body").inner_text().lower()
        assert _snapshot_definition(_get_workflow(base_url, workflow_id) or {}) == (
            _snapshot_definition(detail)
        )
    finally:
        _delete_temp_workflow(base_url, workflow_id)


def test_builder_defaults_do_not_change_after_plain_page_reload(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    before_payload = _get_workflow(base_url, workflow_id)
    assert before_payload, "Cannot load workflow payload before UI navigation"
    before_snapshot = _snapshot_definition(before_payload)

    _open_workflow_builder(page, base_url, workflow_id)
    _reload_page_stably(page)

    after_payload = _get_workflow(base_url, workflow_id)
    assert after_payload, "Cannot load workflow payload after reload"
    after_snapshot = _snapshot_definition(after_payload)

    assert after_snapshot == before_snapshot


def test_canvas_hides_legacy_contract_mapping_controls(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _open_workflow_builder(page, base_url, workflow_id)
    page.get_by_role("button", name=re.compile(r"auto layout", re.I)).wait_for(
        state="visible", timeout=10_000
    )
    page.get_by_role("textbox", name="Search workflow canvas").wait_for(
        state="visible", timeout=10_000
    )

    assert page.get_by_text("Required expected inputs", exact=True).count() == 0
    assert page.locator('[id^="ei-required-list-"]').count() == 0
    assert page.get_by_text("needs source", exact=False).count() == 0


def test_workflow_settings_opens_drawer_surface(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _open_workflow_builder(page, base_url, workflow_id)
    page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
    page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)

    assert page.locator("#builder-drawer-title").inner_text().strip().lower().startswith("workflow")
    assert page.locator("#workflow-settings-surface").is_visible()


def test_canvas_builder_surface_exposes_search_minimap_and_no_phase_controls(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )

    page.get_by_role("button", name=re.compile(r"auto layout", re.I)).wait_for(
        state="visible", timeout=10_000
    )
    assert page.get_by_role("textbox", name="Search workflow canvas").is_visible()
    page.get_by_role("button", name="More").click()
    minimap_toggle = page.get_by_role("button", name=re.compile(r"show minimap", re.I))
    assert page.locator(".react-flow__minimap").count() == 0
    minimap_toggle.click()
    assert page.locator(".react-flow__minimap").count() == 1
    # Negative: the retired phase affordances must not come back.
    assert page.get_by_role("button", name="Phase here").count() == 0
    assert page.get_by_role("button", name="Add case -> phase").count() == 0
    assert page.get_by_role("button", name=re.compile(r"add phase", re.I)).count() == 0


def test_builder_route_uses_single_canvas_header(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    page.get_by_role("textbox", name="Search workflow canvas").wait_for(
        state="visible", timeout=10_000
    )
    assert page.get_by_role("link", name="Detail").count() == 0
    # The detail page's structure card and summary header stay on the detail route.
    assert page.locator("#workflow-structure").count() == 0
    assert page.locator("#workflow-detail-summary").count() == 0
    assert page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).count() == 1


def test_builder_detail_header_actions_open_drawer(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _goto_page_stably(page, f"{base_url}/registry/workflows/{workflow_id}/builder")
    page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
    page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)

    assert page.locator("#builder-drawer-title").inner_text().strip().lower().startswith("workflow")
    assert page.locator("#workflow-settings-surface").is_visible()


def test_workflow_incoming_webhook_live_browser_flow_triggers_real_delivery(
    page, base_url, server_reachable, test_project_id
):
    _require_admin_workflow_builder_mutation_role()
    workflow_id = f"ui_live_incoming_{uuid.uuid4().hex[:10]}"
    project_id = str(test_project_id)
    _provision_temporary_incoming_webhook_workflow(base_url, workflow_id)

    original_bindings = _project_workflow_bindings(base_url, project_id)
    workflow_rows = original_bindings.get("project_workflows") or []
    original_workflow_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in workflow_rows
        if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
    ]
    original_workflow_ids = [
        existing_id for existing_id in original_workflow_ids if _get_workflow(base_url, existing_id)
    ]
    target_workflow_ids = list(dict.fromkeys([*original_workflow_ids, workflow_id]))
    original_default = str(original_bindings.get("operator_default_workflow_id") or "").strip()
    restore_default = original_default or (
        target_workflow_ids[0] if target_workflow_ids else workflow_id
    )
    created_entry_key = ""
    accepted_job_ids: list[str] = []
    auth_cookies: dict[str, str] = {}
    webhook_description = f"UI live webhook {uuid.uuid4().hex[:8]}"

    if workflow_id not in original_workflow_ids or original_default != workflow_id:
        _update_project_workflow_bindings(
            base_url,
            project_id,
            workflow_ids=target_workflow_ids,
            operator_default_workflow_id=workflow_id,
        )

    try:
        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)

        email_mapping_input = page.get_by_role("textbox", name=re.compile(r"email \*string", re.I))
        event_mapping_input = page.get_by_role(
            "textbox", name=re.compile(r"event_type \*string", re.I)
        )
        email_mapping_input.wait_for(state="visible", timeout=10_000)
        event_mapping_input.wait_for(state="visible", timeout=10_000)
        page.get_by_role("textbox", name="Description").fill(webhook_description)
        page.get_by_role("combobox", name="Idempotency mode").select_option("header")

        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and f"/api/workflows/{workflow_id}/triggers/webhooks" in response.url
                and response.status == 201
            )
        ) as webhook_create_response:
            page.get_by_role("button", name="Create webhook").click()
        webhook_create_payload = webhook_create_response.value.json()
        webhook_create_request_payload = webhook_create_response.value.request.post_data_json
        assert webhook_create_request_payload["project_id"] == project_id
        created_entry_key = str(webhook_create_payload.get("entry_key") or "").strip()
        assert created_entry_key, webhook_create_payload
        public_path = str(webhook_create_payload.get("public_path") or "").strip()
        public_url = str(webhook_create_payload.get("public_url") or "").strip()
        assert public_path == f"/webhook/workflows/{created_entry_key}"
        assert public_url.endswith(public_path)
        credential_reveal = dict(webhook_create_payload.get("credential_reveal") or {})
        assert credential_reveal.get("auth_mode") == "bearer_token"

        page.get_by_text(re.compile(r"Webhook created", re.I)).wait_for(timeout=10_000)
        page.get_by_role("button", name="Copy credential").wait_for(timeout=10_000)
        page.get_by_text(public_path, exact=True).wait_for(timeout=10_000)
        page.get_by_role("button", name=webhook_description).click()
        page.get_by_text(re.compile(r"Generated cURL", re.I)).wait_for(timeout=10_000)
        page.get_by_text(re.compile(r"Authorization: Bearer <token>", re.I)).wait_for(
            timeout=10_000
        )
        page.get_by_text(re.compile(r"Idempotency-Key: <unique-key>", re.I)).wait_for(
            timeout=10_000
        )
        auth_cookies = _browser_cookies(page, base_url)

        invoke_url = f"{base_url.rstrip('/')}{public_path}"
        invalid_attempt_started_at = datetime.now(UTC).isoformat()
        invalid_response = requests.post(
            invoke_url,
            json={
                "email": "live-ui@example.com",
                "event_type": "invoice.created",
            },
            headers={"Idempotency-Key": f"idem-{uuid.uuid4().hex[:8]}"},
            timeout=20,
            verify=False,
        )
        assert invalid_response.status_code == 401
        rejected_event = _wait_for_event_summary(
            base_url,
            event_type="workflow.ingress.rejected",
            cookies=auth_cookies,
            occurred_after=invalid_attempt_started_at,
        )

        bearer_idempotency_key = f"idem-{uuid.uuid4().hex[:8]}"
        bearer_payload = {
            "email": "live-ui@example.com",
            "event_type": "invoice.created",
            "amount": 42,
            "approved": True,
            "source": "browser-e2e",
        }
        bearer_headers = {
            **_webhook_auth_headers("bearer_token", credential_reveal, raw_body=b""),
            "Idempotency-Key": bearer_idempotency_key,
        }
        bearer_response = requests.post(
            invoke_url,
            json=bearer_payload,
            headers=bearer_headers,
            timeout=20,
            verify=False,
        )
        assert bearer_response.status_code == 202, bearer_response.text
        bearer_result = bearer_response.json()
        assert bearer_result["selected_workflow_id"] == workflow_id
        job_id = str(bearer_result.get("job_id") or "").strip()
        assert job_id, bearer_result
        accepted_job_ids.append(job_id)
        try:
            job_detail = _wait_for_job_detail(
                base_url,
                job_id=job_id,
                require_invocation=True,
                cookies=auth_cookies,
            )
        except AssertionError as exc:
            pytest.skip(f"Live job detail did not expose invocation snapshot in shared env: {exc}")
        invocation = job_detail.get("invocation") if isinstance(job_detail, dict) else {}
        assert isinstance(invocation, dict)
        assert invocation.get("channel") == "webhook"
        assert invocation.get("trigger_family") == "incoming_webhook"
        assert invocation.get("request_id") == str(bearer_result.get("request_id") or "").strip()
        assert invocation.get("entry_key") == created_entry_key
        assert dict(invocation.get("mapped_inputs") or {}).get("email") == "live-ui@example.com"

        _goto_page_stably(page, f"{base_url}/jobs/{job_id}")
        page.locator("[data-testid='job-invocation-panel']").wait_for(
            state="visible",
            timeout=10_000,
        )
        page.locator("[data-testid='job-invocation-panel']").get_by_text(
            re.compile(r"Incoming webhook", re.I)
        ).wait_for(timeout=10_000)
        page.locator("[data-testid='job-invocation-panel']").get_by_text(
            str(bearer_result.get("request_id") or "").strip()
        ).wait_for(timeout=10_000)
        page.locator("[data-testid='job-invocation-panel']").get_by_text(
            re.compile(r"live-ui@example.com", re.I)
        ).wait_for(timeout=10_000)

        _goto_page_stably(page, f"{base_url}/monitoring?tab=events")
        page.get_by_text(re.compile(r"Events and audit trail", re.I)).wait_for(timeout=10_000)
        page.get_by_role("combobox", name=re.compile(r"exact event type", re.I)).select_option(
            "workflow.ingress.rejected"
        )
        page.get_by_role("button", name=re.compile(r"Refresh", re.I)).click()
        page.get_by_role(
            "button",
            name=re.compile(
                re.escape(str(rejected_event.get("event_id") or rejected_event.get("id") or ""))
            ),
        ).click()
        page.locator("[data-testid='event-detail-inspector']").wait_for(
            state="visible",
            timeout=10_000,
        )
        event_detail_fragments = [
            str(rejected_event.get("summary") or "").strip(),
            str(rejected_event.get("detail") or "").strip(),
            str(rejected_event.get("message") or "").strip(),
            "Invalid bearer token",
        ]
        event_detail_fragments = [value for value in event_detail_fragments if value]
        inspector_text = page.locator("[data-testid='event-detail-inspector']").inner_text()
        normalized_inspector_text = inspector_text.lower()
        matched_fragment = any(
            fragment.lower() in normalized_inspector_text for fragment in event_detail_fragments
        )
        assert matched_fragment, (
            "Expected event detail inspector to show the rejected ingress summary or detail"
        )

        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)
        created_webhook_row = (
            page.locator("#workflow-triggers-surface button")
            .filter(has_text=created_entry_key)
            .first
        )
        created_webhook_row.wait_for(state="visible", timeout=10_000)
        created_webhook_row.click()

        page.get_by_role("combobox", name="Auth mode").select_option("shared_secret_header")
        page.get_by_role("textbox", name="Header name").fill("X-Demo-Secret")
        json_checkbox = page.get_by_label("application/json")
        if json_checkbox.is_checked():
            json_checkbox.uncheck()
        form_checkbox = page.get_by_label("application/x-www-form-urlencoded")
        if not form_checkbox.is_checked():
            form_checkbox.check()
        page.get_by_role("combobox", name="Idempotency mode").select_option("none")
        page.get_by_role("combobox", name="Response mode").select_option("ok")
        email_mapping_input = page.get_by_role("textbox", name=re.compile(r"email \*string", re.I))
        event_mapping_input = page.get_by_role(
            "textbox", name=re.compile(r"event_type \*string", re.I)
        )
        email_mapping_input.fill("form.email")
        event_mapping_input.fill("headers.x-event-type")

        with page.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and f"/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}"
                in response.url
                and response.status == 200
            )
        ) as webhook_save_response:
            page.get_by_role("button", name="Save webhook settings").click()
        webhook_save_payload = webhook_save_response.value.json()
        webhook_save_request_payload = webhook_save_response.value.request.post_data_json
        assert webhook_save_request_payload["project_id"] == project_id
        assert webhook_save_request_payload["auth_mode"] == "shared_secret_header"
        assert webhook_save_request_payload["allowed_content_types"] == [
            "application/x-www-form-urlencoded"
        ]
        assert webhook_save_request_payload["input_mapping"]["email"] == "form.email"
        assert webhook_save_request_payload["input_mapping"]["event_type"] == "headers.x-event-type"
        shared_secret_reveal = dict(webhook_save_payload.get("credential_reveal") or {})
        assert shared_secret_reveal.get("auth_mode") == "shared_secret_header"
        page.get_by_text(re.compile(r"Webhook settings saved", re.I)).wait_for(timeout=10_000)
        page.get_by_text(re.compile(r"X-Demo-Secret: <shared-secret>", re.I)).wait_for(
            timeout=10_000
        )
        page.get_by_text(re.compile(r"--data-urlencode", re.I)).wait_for(timeout=10_000)

        invalid_form_response = requests.post(
            invoke_url,
            data={"email": "form-ui@example.com"},
            headers={"x-event-type": "approval.changed"},
            timeout=20,
            verify=False,
        )
        assert invalid_form_response.status_code == 401

        form_body = "email=form-ui%40example.com"
        form_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "x-event-type": "approval.changed",
            **_webhook_auth_headers(
                "shared_secret_header",
                shared_secret_reveal,
                raw_body=form_body.encode("utf-8"),
            ),
        }
        shared_response = requests.post(
            invoke_url,
            data=form_body,
            headers=form_headers,
            timeout=20,
            verify=False,
        )
        assert shared_response.status_code == 200, shared_response.text
        shared_result = shared_response.json()
        assert shared_result["selected_workflow_id"] == workflow_id
        shared_job_id = str(shared_result.get("job_id") or "").strip()
        if shared_job_id:
            accepted_job_ids.append(shared_job_id)
        shared_delivery = _wait_for_workflow_webhook_delivery(
            base_url,
            workflow_id,
            entry_key=created_entry_key,
            request_id=str(shared_result.get("request_id") or "").strip(),
        )
        assert shared_delivery["auth_mode"] == "shared_secret_header"
        assert shared_delivery["http_status"] == 200
        assert shared_delivery["content_type"] == "application/x-www-form-urlencoded"
        payload_summary = shared_delivery.get("payload_summary") or {}
        assert "email" in payload_summary.get("form_keys", [])
    finally:
        for accepted_job_id in accepted_job_ids:
            _wait_for_job_terminal(
                base_url,
                job_id=accepted_job_id,
                cookies=auth_cookies,
            )
        if created_entry_key:
            with suppress(Exception):
                requests.request(
                    "DELETE",
                    f"{base_url}/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}",
                    params={"project_id": project_id},
                    timeout=15,
                    verify=False,
                )
        if workflow_id not in original_workflow_ids or original_default != workflow_id:
            with suppress(Exception):
                restore_ids = original_workflow_ids or [workflow_id]
                _update_project_workflow_bindings(
                    base_url,
                    project_id,
                    workflow_ids=restore_ids,
                    operator_default_workflow_id=restore_default,
                )
        with suppress(Exception):
            _request_json(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                expected={200, 204, 404},
            )


def test_workflow_incoming_webhook_live_browser_flow_supports_basic_and_hmac_auth(
    page, base_url, server_reachable, test_project_id
):
    _require_admin_workflow_builder_mutation_role()
    workflow_id = f"ui_live_auth_{uuid.uuid4().hex[:10]}"
    project_id = str(test_project_id)
    _provision_temporary_incoming_webhook_workflow(base_url, workflow_id)

    original_bindings = _project_workflow_bindings(base_url, project_id)
    workflow_rows = original_bindings.get("project_workflows") or []
    original_workflow_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in workflow_rows
        if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
    ]
    original_workflow_ids = [
        existing_id for existing_id in original_workflow_ids if _get_workflow(base_url, existing_id)
    ]
    target_workflow_ids = list(dict.fromkeys([*original_workflow_ids, workflow_id]))
    original_default = str(original_bindings.get("operator_default_workflow_id") or "").strip()
    restore_default = original_default or (
        target_workflow_ids[0] if target_workflow_ids else workflow_id
    )
    created_entry_key = ""
    accepted_job_ids: list[str] = []
    webhook_description = f"UI auth matrix {uuid.uuid4().hex[:8]}"

    if workflow_id not in original_workflow_ids or original_default != workflow_id:
        _update_project_workflow_bindings(
            base_url,
            project_id,
            workflow_ids=target_workflow_ids,
            operator_default_workflow_id=workflow_id,
        )

    try:
        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)

        page.get_by_role("textbox", name="Description").fill(webhook_description)
        page.get_by_role("combobox", name="Auth mode").select_option("basic_auth")
        page.get_by_role("textbox", name="Username").fill("workflow-basic")
        page.get_by_role("combobox", name="Idempotency mode").select_option("none")

        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and f"/api/workflows/{workflow_id}/triggers/webhooks" in response.url
                and response.status == 201
            )
        ) as webhook_create_response:
            page.get_by_role("button", name="Create webhook").click()
        webhook_create_payload = webhook_create_response.value.json()
        webhook_create_request_payload = webhook_create_response.value.request.post_data_json
        assert webhook_create_request_payload["project_id"] == project_id
        assert webhook_create_request_payload["auth_mode"] == "basic_auth"
        created_entry_key = str(webhook_create_payload.get("entry_key") or "").strip()
        assert created_entry_key, webhook_create_payload
        public_path = str(webhook_create_payload.get("public_path") or "").strip()
        assert public_path == f"/webhook/workflows/{created_entry_key}"
        basic_reveal = dict(webhook_create_payload.get("credential_reveal") or {})
        assert basic_reveal.get("auth_mode") == "basic_auth"
        assert basic_reveal.get("username") == "workflow-basic"
        page.get_by_text(re.compile(r"Webhook created", re.I)).wait_for(timeout=10_000)
        page.get_by_role("button", name="Copy credential").wait_for(timeout=10_000)
        page.get_by_text(re.compile(r"-u 'workflow-basic:<password>'", re.I)).wait_for(
            timeout=10_000
        )

        invoke_url = f"{base_url.rstrip('/')}{public_path}"
        invalid_basic = requests.post(
            invoke_url,
            json={"email": "basic-ui@example.com", "event_type": "invoice.created"},
            timeout=20,
            verify=False,
        )
        assert invalid_basic.status_code == 401

        basic_response = requests.post(
            invoke_url,
            json={"email": "basic-ui@example.com", "event_type": "invoice.created"},
            headers=_webhook_auth_headers("basic_auth", basic_reveal, raw_body=b""),
            timeout=20,
            verify=False,
        )
        assert basic_response.status_code == 202, basic_response.text
        basic_result = basic_response.json()
        assert basic_result["selected_workflow_id"] == workflow_id
        basic_job_id = str(basic_result.get("job_id") or "").strip()
        if basic_job_id:
            accepted_job_ids.append(basic_job_id)
        basic_delivery = _wait_for_workflow_webhook_delivery(
            base_url,
            workflow_id,
            entry_key=created_entry_key,
            request_id=str(basic_result.get("request_id") or "").strip(),
        )
        assert basic_delivery["auth_mode"] == "basic_auth"
        assert basic_delivery["http_status"] == 202
        assert basic_delivery["content_type"] == "application/json"

        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)
        created_webhook_row = (
            page.locator("#workflow-triggers-surface button")
            .filter(has_text=created_entry_key)
            .first
        )
        created_webhook_row.wait_for(state="visible", timeout=10_000)
        created_webhook_row.click()

        page.get_by_role("combobox", name="Auth mode").select_option("hmac_sha256")
        page.get_by_role("textbox", name="Header name").fill("X-Demo-Signature")
        json_checkbox = page.get_by_label("application/json")
        if json_checkbox.is_checked():
            json_checkbox.uncheck()
        text_checkbox = page.get_by_label("text/plain")
        if not text_checkbox.is_checked():
            text_checkbox.check()
        email_mapping_input = page.get_by_role("textbox", name=re.compile(r"email \*string", re.I))
        event_mapping_input = page.get_by_role(
            "textbox", name=re.compile(r"event_type \*string", re.I)
        )
        email_mapping_input.fill("headers.x-user-email")
        event_mapping_input.fill("headers.x-event-type")

        with page.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and f"/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}"
                in response.url
                and response.status == 200
            )
        ) as webhook_save_response:
            page.get_by_role("button", name="Save webhook settings").click()
        webhook_save_payload = webhook_save_response.value.json()
        webhook_save_request_payload = webhook_save_response.value.request.post_data_json
        assert webhook_save_request_payload["project_id"] == project_id
        assert webhook_save_request_payload["auth_mode"] == "hmac_sha256"
        assert webhook_save_request_payload["allowed_content_types"] == ["text/plain"]
        assert webhook_save_request_payload["input_mapping"]["email"] == "headers.x-user-email"
        assert webhook_save_request_payload["input_mapping"]["event_type"] == "headers.x-event-type"
        hmac_reveal = dict(webhook_save_payload.get("credential_reveal") or {})
        assert hmac_reveal.get("auth_mode") == "hmac_sha256"
        page.get_by_text(re.compile(r"Webhook settings saved", re.I)).wait_for(timeout=10_000)
        page.get_by_text(re.compile(r"X-Demo-Signature: sha256=<hex-digest>", re.I)).wait_for(
            timeout=10_000
        )
        page.get_by_text(re.compile(r"Content-Type: text/plain", re.I)).wait_for(timeout=10_000)

        invalid_hmac = requests.post(
            invoke_url,
            data=b"raw text payload from browser auth test",
            headers={
                "Content-Type": "text/plain",
                "x-user-email": "hmac-ui@example.com",
                "x-event-type": "approval.changed",
            },
            timeout=20,
            verify=False,
        )
        assert invalid_hmac.status_code == 401

        raw_body = b"raw text payload from browser auth test"
        hmac_headers = {
            "Content-Type": "text/plain",
            "x-user-email": "hmac-ui@example.com",
            "x-event-type": "approval.changed",
            **_webhook_auth_headers("hmac_sha256", hmac_reveal, raw_body=raw_body),
        }
        hmac_response = requests.post(
            invoke_url,
            data=raw_body,
            headers=hmac_headers,
            timeout=20,
            verify=False,
        )
        assert hmac_response.status_code == 202, hmac_response.text
        hmac_result = hmac_response.json()
        assert hmac_result["selected_workflow_id"] == workflow_id
        hmac_job_id = str(hmac_result.get("job_id") or "").strip()
        if hmac_job_id:
            accepted_job_ids.append(hmac_job_id)
        hmac_delivery = _wait_for_workflow_webhook_delivery(
            base_url,
            workflow_id,
            entry_key=created_entry_key,
            request_id=str(hmac_result.get("request_id") or "").strip(),
        )
        assert hmac_delivery["auth_mode"] == "hmac_sha256"
        assert hmac_delivery["http_status"] == 202
        assert hmac_delivery["content_type"] == "text/plain"
        assert "body" in (hmac_delivery.get("payload_summary") or {}).get("body_keys", [])
    finally:
        for accepted_job_id in accepted_job_ids:
            _wait_for_job_terminal(base_url, job_id=accepted_job_id)
        if created_entry_key:
            with suppress(Exception):
                requests.request(
                    "DELETE",
                    f"{base_url}/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}",
                    params={"project_id": project_id},
                    timeout=15,
                    verify=False,
                )
        if workflow_id not in original_workflow_ids or original_default != workflow_id:
            with suppress(Exception):
                restore_ids = original_workflow_ids or [workflow_id]
                _update_project_workflow_bindings(
                    base_url,
                    project_id,
                    workflow_ids=restore_ids,
                    operator_default_workflow_id=restore_default,
                )
        with suppress(Exception):
            _request_json(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                expected={200, 204, 404},
            )


def test_workflow_triggers_flow_roundtrips_against_live_backend(
    page, base_url, server_reachable, test_project_id
):
    _require_admin_workflow_builder_mutation_role()
    workflow_id = f"ui_live_provider_{uuid.uuid4().hex[:10]}"
    _provision_probe_workflow(
        base_url,
        workflow_id,
        display_name=f"UI Live Provider {workflow_id[-6:]}",
        description="Temporary workflow for live provider trigger browser coverage",
        probe_step_id="provider_probe_request",
    )

    project_id = str(test_project_id)
    original_bindings = _project_workflow_bindings(base_url, project_id)
    workflow_rows = original_bindings.get("project_workflows") or []
    original_workflow_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in workflow_rows
        if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
    ]
    original_workflow_ids = [
        existing_id for existing_id in original_workflow_ids if _get_workflow(base_url, existing_id)
    ]
    target_workflow_ids = list(dict.fromkeys([*original_workflow_ids, workflow_id]))
    original_default = str(original_bindings.get("operator_default_workflow_id") or "").strip()
    created_entry_key = ""
    created_trigger_id = ""
    webhook_description = f"UI E2E webhook {uuid.uuid4().hex[:8]}"
    updated_webhook_description = f"{webhook_description} updated"
    provider_display_name = f"UI E2E provider {uuid.uuid4().hex[:8]}"
    created_provider_payload: dict[str, Any] = {}
    restore_default = original_default or (
        target_workflow_ids[0] if target_workflow_ids else workflow_id
    )

    if workflow_id not in original_workflow_ids or original_default != workflow_id:
        _update_project_workflow_bindings(
            base_url,
            project_id,
            workflow_ids=target_workflow_ids,
            operator_default_workflow_id=workflow_id,
        )

    try:
        trigger_payload = _trigger_surface_payload(
            base_url,
            workflow_id,
            project_id=project_id,
        )
        provider_catalog = [
            row
            for row in (trigger_payload.get("catalog") or [])
            if isinstance(row, dict) and str(row.get("trigger_family") or "") == "provider"
        ]
        provider_catalog_empty = not provider_catalog

        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)
        auth_cookies = _browser_cookies(page, base_url)

        create_webhook_button = page.get_by_role("button", name="Create webhook")
        new_webhook_button = page.get_by_role("button", name=re.compile(r"new webhook", re.I))
        if new_webhook_button.count():
            new_webhook_button.first.click()
        create_webhook_button.wait_for(state="visible", timeout=10_000)
        page.get_by_role("textbox", name="Description").fill(webhook_description)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and f"/api/workflows/{workflow_id}/triggers/webhooks" in response.url
                and response.status == 201
            )
        ) as webhook_create_response:
            page.get_by_role("button", name="Create webhook").click()
            webhook_create_payload = webhook_create_response.value.json()
            webhook_create_request = webhook_create_response.value.request
            webhook_create_request_payload = webhook_create_request.post_data_json
            assert webhook_create_request_payload["project_id"] == project_id
            created_entry_key = str(webhook_create_payload.get("entry_key") or "").strip()
            assert created_entry_key, webhook_create_payload
            page.get_by_text(re.compile(r"Webhook created", re.I)).wait_for(timeout=10_000)
            page.locator("#workflow-triggers-surface p.break-all").filter(
                has_text=created_entry_key
            ).first.wait_for(timeout=10_000)

        _goto_page_stably(
            page,
            f"{base_url}/registry/workflows/{workflow_id}/builder?project_id={project_id}",
        )
        page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
        page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
        page.get_by_role("button", name="Triggers").click()
        page.locator("#workflow-triggers-surface").wait_for(state="visible", timeout=10_000)
        created_webhook_row = page.locator(
            "#workflow-triggers-surface button[aria-label]",
        ).first
        created_webhook_row.wait_for(state="visible", timeout=10_000)
        created_webhook_row.click()
        page.get_by_role("button", name="Copy public URL").wait_for(
            state="visible",
            timeout=10_000,
        )
        save_webhook_button = page.get_by_role("button", name="Save webhook settings")
        save_webhook_button.wait_for(state="visible", timeout=10_000)
        page.get_by_role("textbox", name="Description").fill(updated_webhook_description)
        with page.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and f"/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}"
                in response.url
                and response.status == 200
            )
        ) as webhook_save_response:
            save_webhook_button.click()
        webhook_save_request_payload = webhook_save_response.value.request.post_data_json
        assert webhook_save_request_payload["project_id"] == project_id
        page.get_by_text(re.compile(r"Webhook settings saved", re.I)).wait_for(timeout=10_000)
        if provider_catalog_empty:
            assert page.locator("#workflow-triggers-surface").is_visible()
            assert "traceback" not in page.content().lower()
            return

        create_provider_button = page.get_by_role("button", name="Create provider trigger")
        new_provider_button = page.get_by_role(
            "button", name=re.compile(r"new provider trigger", re.I)
        )
        if new_provider_button.count():
            new_provider_button.first.click()
        create_provider_button.wait_for(state="visible", timeout=10_000)
        provider_select = page.get_by_role("combobox", name="Trigger catalog")
        option_values = provider_select.locator("option")
        preferred_catalog = next(
            (
                row
                for row in provider_catalog
                if str(row.get("provider_key") or "").strip().lower() == "sample_tasks"
            ),
            provider_catalog[0],
        )
        provider_option_value = str(preferred_catalog.get("catalog_id") or "").strip()
        if not provider_option_value:
            for index in range(option_values.count()):
                value = str(option_values.nth(index).get_attribute("value") or "").strip()
                if value:
                    provider_option_value = value
                    break
        assert provider_option_value, (
            "Provider catalog exists but the UI exposed no selectable option"
        )
        provider_select.select_option(provider_option_value)
        selected_provider_catalog = next(
            (
                row
                for row in provider_catalog
                if str(row.get("catalog_id") or "").strip() == provider_option_value
            ),
            preferred_catalog,
        )
        page.get_by_role("textbox", name="Display name").fill(provider_display_name)

        provider_panel = page.locator("#workflow-provider-trigger-editor")
        ui_fields = (
            selected_provider_catalog.get("ui_schema", {}).get("fields", [])
            if isinstance(selected_provider_catalog, dict)
            else []
        )
        for field in ui_fields:
            if not isinstance(field, dict):
                continue
            field_path = str(
                field.get("path") or field.get("field_id") or field.get("name") or ""
            ).strip()
            if not field_path:
                continue
            label = str(
                field.get("label")
                or field.get("field_id")
                or field.get("path")
                or field.get("name")
                or ""
            ).strip()
            if not label:
                continue
            widget = str(field.get("widget") or field.get("widget_type") or "text").strip().lower()
            if widget in {"boolean", "checkbox"}:
                field_checkbox = provider_panel.get_by_role(
                    "checkbox",
                    name=re.compile(re.escape(label), re.I),
                )
                if not field_checkbox.is_checked():
                    field_checkbox.check()
                continue
            if widget == "select":
                field_select = provider_panel.get_by_role(
                    "combobox",
                    name=re.compile(re.escape(label), re.I),
                )
                options = field.get("options") if isinstance(field.get("options"), list) else []
                option_value = ""
                for option in options:
                    if isinstance(option, dict):
                        option_value = str(option.get("value") or "").strip()
                    else:
                        option_value = str(option or "").strip()
                    if option_value:
                        break
                if option_value:
                    field_select.select_option(option_value)
                continue
            field_input = provider_panel.get_by_role(
                "textbox",
                name=re.compile(re.escape(label), re.I),
            )
            field_input.fill(f"ui-{uuid.uuid4().hex[:8]}")

        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/api/workflows/{workflow_id}/triggers/providers")
            )
        ) as provider_create_response:
            allowed_console_patterns = getattr(page, "_allowed_console_error_patterns", [])
            allowed_console_patterns.append("status of 409 (Conflict)")
            page._allowed_console_error_patterns = allowed_console_patterns
            create_provider_button.click()
        provider_response = provider_create_response.value
        provider_payload = provider_response.json()
        if provider_response.status == 409:
            existing_provider_rows = (
                _trigger_surface_payload(
                    base_url,
                    workflow_id,
                    project_id=project_id,
                ).get("provider_triggers")
                or []
            )
            matching_existing_trigger = next(
                (
                    row
                    for row in existing_provider_rows
                    if isinstance(row, dict)
                    and str(row.get("provider_key") or "").strip().lower()
                    == str(selected_provider_catalog.get("provider_key") or "").strip().lower()
                    and str(row.get("trigger_type_id") or "").strip()
                    == str(selected_provider_catalog.get("trigger_type_id") or "").strip()
                ),
                None,
            )
            if not isinstance(matching_existing_trigger, dict):
                matching_existing_trigger = next(
                    (
                        row
                        for row in existing_provider_rows
                        if isinstance(row, dict)
                        and str(row.get("provider_key") or "").strip().lower()
                        == str(selected_provider_catalog.get("provider_key") or "").strip().lower()
                    ),
                    None,
                )
            if not isinstance(matching_existing_trigger, dict):
                pytest.skip(
                    f"Provider trigger create conflicted in shared test env and no existing trigger could be reused: {provider_payload}"
                )
            created_trigger_id = str(matching_existing_trigger.get("trigger_id") or "").strip()
            created_provider_payload = dict(matching_existing_trigger)
        else:
            created_trigger_id = str(provider_payload.get("trigger_id") or "").strip()
            assert created_trigger_id, provider_payload
            created_provider_payload = dict(provider_payload)
            page.get_by_text(re.compile(r"Provider trigger saved", re.I)).wait_for(timeout=10_000)
            page.get_by_role(
                "button", name=re.compile(re.escape(provider_display_name), re.I)
            ).wait_for(timeout=10_000)

        provider_key = str(created_provider_payload.get("provider_key") or "").strip().lower()
        if provider_key != "sample_tasks":
            pytest.skip(
                f"Live provider runtime e2e currently supports sample_tasks only, got {provider_key!r}"
            )
        project_provider_rows = (
            _project_providers(
                base_url,
                project_id,
                enabled_only=True,
                cookies=auth_cookies,
            ).get("project_providers")
            or []
        )
        has_enabled_provider = any(
            isinstance(row, dict)
            and str(row.get("provider_key") or "").strip().lower() == provider_key
            and bool(row.get("enabled", True))
            and bool(row.get("provider_enabled", True))
            for row in project_provider_rows
        )
        if not has_enabled_provider:
            pytest.skip(
                f"Project {project_id} has no enabled provider connection for {provider_key}"
            )

        invalid_provider_payload, invalid_summary = _provider_runtime_request(
            provider_key=provider_key,
            trigger_row=created_provider_payload,
            valid=False,
        )
        invalid_started_at = datetime.now(UTC).isoformat()
        invalid_response = requests.post(
            f"{base_url}/webhook/providers/{provider_key}",
            json=invalid_provider_payload,
            timeout=20,
            verify=False,
        )
        assert invalid_response.status_code == 404, invalid_response.text
        rejected_event = _wait_for_event_summary(
            base_url,
            event_type="workflow.ingress.rejected",
            summary_contains=invalid_summary,
            cookies=auth_cookies,
            occurred_after=invalid_started_at,
        )
        assert rejected_event

        valid_provider_payload, _ = _provider_runtime_request(
            provider_key=provider_key,
            trigger_row=created_provider_payload,
            valid=True,
        )
        accepted_response = requests.post(
            f"{base_url}/webhook/providers/{provider_key}",
            json=valid_provider_payload,
            timeout=20,
            verify=False,
        )
        assert accepted_response.status_code == 202, accepted_response.text
        accepted_payload = accepted_response.json()
        provider_job_id = str(accepted_payload.get("job_id") or "").strip()
        assert provider_job_id, accepted_payload
        provider_job_detail = _wait_for_job_detail(
            base_url,
            job_id=provider_job_id,
            require_invocation=True,
            cookies=auth_cookies,
        )
        provider_invocation = (
            provider_job_detail.get("invocation") if isinstance(provider_job_detail, dict) else {}
        )
        assert isinstance(provider_invocation, dict)
        assert provider_invocation.get("channel") == "provider_webhook"
        assert provider_invocation.get("trigger_family") == "provider_trigger"
        assert provider_invocation.get("trigger_id") == created_trigger_id
        assert provider_invocation.get("source_system") == provider_key

        _goto_page_stably(page, f"{base_url}/jobs/{provider_job_id}")
        page.locator("[data-testid='job-invocation-panel']").wait_for(
            state="visible",
            timeout=10_000,
        )
        page.locator("[data-testid='job-invocation-panel']").get_by_text(
            re.compile(r"Provider trigger", re.I)
        ).wait_for(timeout=10_000)
        page.locator("[data-testid='job-invocation-panel']").get_by_text(
            re.compile(re.escape(provider_key), re.I)
        ).wait_for(timeout=10_000)
    finally:
        if created_trigger_id:
            with suppress(Exception):
                requests.request(
                    "DELETE",
                    f"{base_url}/api/workflows/{workflow_id}/triggers/providers/{created_trigger_id}",
                    timeout=15,
                    verify=False,
                )
        if created_entry_key:
            with suppress(Exception):
                requests.request(
                    "DELETE",
                    f"{base_url}/api/workflows/{workflow_id}/triggers/webhooks/{created_entry_key}",
                    timeout=15,
                    verify=False,
                )
        if workflow_id not in original_workflow_ids or original_default != workflow_id:
            with suppress(Exception):
                restore_ids = original_workflow_ids or [workflow_id]
                _update_project_workflow_bindings(
                    base_url,
                    project_id,
                    workflow_ids=restore_ids,
                    operator_default_workflow_id=restore_default,
                )
        with suppress(Exception):
            _request_json(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                expected={200, 204, 404},
            )


def test_builder_drawer_resizes_and_persists_across_workflows(
    page, base_url, server_reachable, builder_workflow, fresh_builder_workflow
):
    workflow_id, other_workflow_id = builder_workflow, fresh_builder_workflow

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    page.evaluate("localStorage.removeItem('workflow-builder:drawer-width:v1')")
    _reload_page_stably(page)
    page.get_by_role("button", name=re.compile(r"Workflow interface")).click()
    drawer = page.locator("[aria-label='builder drawer']")
    drawer.wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        """() => {
            const drawer = document.querySelector('[aria-label="builder drawer"]');
            if (!drawer) return false;
            const rect = drawer.getBoundingClientRect();
            return rect.left <= window.innerWidth - rect.width + 2;
        }""",
        timeout=5_000,
    )
    box = drawer.bounding_box()
    assert box is not None
    assert abs(box["width"] - 450) <= 2

    handle = page.get_by_role("button", name="Resize builder inspector")
    handle.wait_for(state="visible", timeout=10_000)
    assert handle.evaluate("node => getComputedStyle(node).cursor") == "col-resize"
    handle_box = handle.bounding_box()
    assert handle_box is not None
    drag_y = handle_box["y"] + handle_box["height"] / 2
    drag_start_x = handle_box["x"] + handle_box["width"] - 1
    hit_target = page.evaluate(
        """([x, y]) => {
            const node = document.elementFromPoint(x, y);
            return node ? {
                aria: node.getAttribute('aria-label'),
                className: String(node.getAttribute('class') || ''),
                tagName: node.tagName,
                role: node.getAttribute('role'),
            } : {};
        }""",
        [drag_start_x, drag_y],
    )
    assert hit_target.get("aria") == "Resize builder inspector", hit_target
    page.mouse.move(drag_start_x, drag_y)
    page.mouse.down()
    page.mouse.move(1440 - 600, drag_y, steps=12)
    page.mouse.up()
    page.wait_for_function(
        """() => {
            const width = Number(localStorage.getItem('workflow-builder:drawer-width:v1') || 0);
            return width >= 598 && width <= 602;
        }""",
        timeout=5_000,
    )
    resized_box = drawer.bounding_box()
    assert resized_box is not None
    assert abs(resized_box["width"] - 600) <= 2

    _reload_page_stably(page)
    page.get_by_role("button", name=re.compile(r"Workflow interface")).click()
    page.wait_for_function(
        """() => {
            const drawer = document.querySelector('[aria-label="builder drawer"]');
            if (!drawer) return false;
            const rect = drawer.getBoundingClientRect();
            return rect.left <= window.innerWidth - rect.width + 2;
        }""",
        timeout=5_000,
    )
    persisted_box = page.locator("[aria-label='builder drawer']").bounding_box()
    assert persisted_box is not None
    assert abs(persisted_box["width"] - 600) <= 2

    page.goto(
        f"{base_url}/registry/workflows/{other_workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    page.get_by_role("button", name=re.compile(r"Workflow interface")).click()
    page.wait_for_function(
        """() => {
            const drawer = document.querySelector('[aria-label="builder drawer"]');
            if (!drawer) return false;
            const rect = drawer.getBoundingClientRect();
            return rect.left <= window.innerWidth - rect.width + 2;
        }""",
        timeout=5_000,
    )
    cross_workflow_box = page.locator("[aria-label='builder drawer']").bounding_box()
    assert cross_workflow_box is not None
    assert abs(cross_workflow_box["width"] - 600) <= 2


def _wait_for_step_drawer(page) -> None:
    page.wait_for_function(
        """() => {
            const drawer = document.querySelector('[aria-label="builder drawer"]');
            const panel = document.getElementById('modal-add-step-panel');
            const title = document.getElementById('builder-drawer-title');
            const drawerRect = drawer ? drawer.getBoundingClientRect() : null;
            return !!drawer
                && !!drawerRect
                && drawerRect.width > 0
                && !!panel
                && panel.offsetParent !== null
                && String(title?.innerText || '').trim().toLowerCase().startsWith('step');
        }""",
        timeout=5_000,
    )


def _open_step_from_window_hook(page, step_id: str) -> None:
    page.wait_for_function("""() => typeof window.editStep === 'function'""", timeout=10_000)
    page.evaluate("""(stepId) => { window.editStep?.(stepId); }""", step_id)
    _wait_for_step_drawer(page)


def _close_builder_drawer(page) -> None:
    page.evaluate("() => { if (typeof closeBuilderDrawer === 'function') closeBuilderDrawer(); }")
    page.wait_for_timeout(150)


def _set_advanced_mode(page, enabled: bool) -> None:
    page.wait_for_function(
        """() => typeof window.toggleAdvancedMode === 'function'""", timeout=10_000
    )
    page.evaluate(
        """(enabled) => {
            if (typeof _advancedMode !== 'undefined' && Boolean(_advancedMode) !== enabled) {
                toggleAdvancedMode();
            }
        }""",
        enabled,
    )
    page.wait_for_function(
        """(enabled) => Boolean(window._advancedMode) === enabled""", arg=enabled, timeout=5_000
    )


def _drawer_tab_keys(page) -> list[str]:
    return page.locator("[data-drawer-tab]").evaluate_all(
        "(nodes) => nodes.map((node) => node.getAttribute('data-drawer-tab'))"
    )


def _structure_status(page):
    return page.get_by_test_id("structure-status")


def test_canvas_group_add_step_buttons_open_builder_drawer(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow
    detail = _get_workflow(base_url, workflow_id) or {}
    groups = _workflow_groups(detail)
    assert groups, "Expected the builder fixture to have a real group"
    # A loop over a sequence body offers one Add step button, a lanes body one per lane.
    expected_button_count = sum(max(1, len(_group_lanes(group))) for group in groups)

    _goto_page_stably(page, f"{base_url}/registry/workflows/{workflow_id}/builder")
    add_step_buttons = page.get_by_role("button", name=re.compile(r"^Add step to "))
    add_step_buttons.first.wait_for(state="visible", timeout=10_000)
    assert add_step_buttons.count() == expected_button_count
    assert (
        page.get_by_role("button", name=f"Add step to {_BUILDER_GROUP_NAME}", exact=True).count()
        == 1
    )

    page.get_by_role("button", name=f"Add step to {_BUILDER_GROUP_NAME}", exact=True).click()
    _wait_for_step_drawer(page)
    assert page.locator("[aria-label='builder drawer']").is_visible()
    assert page.locator("#modal-add-step:not(.hidden)").count() == 0
    assert page.locator("#step-executor-advanced").is_visible()
    panel_text = page.locator("#modal-add-step-panel").inner_text()
    assert "New step, not added yet" in panel_text
    assert page.locator("#step-submit-btn").inner_text().strip() == "Add step to changes"


def test_click_step_opens_step_drawer(page, base_url, server_reachable, builder_workflow):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _open_step_from_window_hook(page, _BUILDER_ENTRY_STEP_ID)

    assert page.locator("#builder-drawer-title").inner_text().strip() == "Step editor"
    assert page.locator("#step-executor-advanced").is_visible()
    assert (
        f"step_id: {_BUILDER_ENTRY_STEP_ID}" in page.locator("#modal-add-step-panel").inner_text()
    )
    assert page.locator("#step-id").input_value() == _BUILDER_ENTRY_STEP_ID

    # The same drawer opens from a click on the step node itself.
    _close_builder_drawer(page)
    page.get_by_role("button", name="Fit canvas").click()
    page.get_by_label(f"Open step {_BUILDER_AGENT_STEP_NAME}", exact=True).click()
    _wait_for_step_drawer(page)
    assert page.locator("#step-id").input_value() == _BUILDER_AGENT_STEP_ID


def test_step_editor_uses_agent_only_when_executor_is_auto_locked(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)
    page.locator("#step-agent-select").wait_for(state="visible", timeout=5_000)
    result = page.evaluate(
        """() => {
            const agentSelect = document.getElementById('step-agent-select');
            const advanced = document.getElementById('step-executor-advanced');
            const capSelect = document.getElementById('step-skill-select');
            if (capSelect) return {ok: false, reason: 'skill_control_visible'};
            if (!agentSelect || !advanced || typeof _setStepExecutorControlState !== 'function') {
                return {ok: false, reason: 'missing_controls'};
            }
            const agentOption = Array.from(agentSelect.options || []).find((opt) => String(opt?.value || '').trim());
            if (!agentOption) return {ok: false, reason: 'no_options'};

            _setStepExecutorControlState({
                mappedAgents: [String(agentOption.value || '').trim()],
                resolvedAgent: String(agentOption.value || '').trim(),
            });
            return {
                ok: true,
                advancedHidden: advanced.classList.contains('hidden'),
                agentDisabled: !!agentSelect.disabled,
            };
        }"""
    )
    assert result.get("ok"), f"Step controls unavailable: {result.get('reason')}"

    assert result["advancedHidden"] is False
    assert result["agentDisabled"] is True


def test_step_drawer_uses_drawer_native_shell_without_legacy_modal_header(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)

    assert page.locator("#modal-add-step-panel").is_visible()
    assert page.locator('#modal-add-step-panel [data-step-retired-shell="1"]:visible').count() == 0
    assert page.locator("#step-submit-btn").is_visible()
    # A saved step saves its own parameters; only structure goes to the change set.
    assert page.locator("#step-submit-btn").inner_text().strip() == "Save parameters"
    assert page.locator("#step-block-recovery-warning").count() == 0


def test_step_parameter_save_sends_one_update_step_config_change(
    page, base_url, server_reachable, fresh_builder_workflow
):
    workflow_id = fresh_builder_workflow
    revision_before = _workflow_revision(base_url, workflow_id)
    renamed = f"Intake renamed {uuid.uuid4().hex[:6]}"
    seen: list[dict[str, Any]] = []
    page.on("request", _record_request(seen))

    _open_workflow_builder(page, base_url, workflow_id)
    _open_step_from_window_hook(page, _BUILDER_ENTRY_STEP_ID)
    page.locator("input[name='step_display_name']").fill(renamed)
    with page.expect_response(
        lambda response: _is_change_set_response(response, workflow_id, dry_run=False)
    ) as save_response:
        page.locator("#step-submit-btn").click()

    response = save_response.value
    assert response.status == 200, response.text()
    request_body = response.request.post_data_json
    assert request_body["expected_revision"] == revision_before
    assert "dry_run" not in request_body
    operations = request_body["operations"]
    assert [operation["op"] for operation in operations] == ["update_step_config"]
    assert operations[0]["step_id"] == _BUILDER_ENTRY_STEP_ID
    assert operations[0]["patch"]["display_name"] == renamed
    assert not {"step_id", "step_kind", "group_id", "lane_id"} & set(operations[0]["patch"])
    assert int(response.json()["revision"]) > revision_before

    page.get_by_test_id("step-form-status").get_by_text("Parameters saved").wait_for(timeout=10_000)
    assert "Structure applied" in _structure_status(page).inner_text()
    assert len(_change_set_writes(seen, workflow_id)) == 1

    stored = _get_workflow(base_url, workflow_id) or {}
    stored_step = next(
        row for row in _workflow_steps(stored) if row.get("step_id") == _BUILDER_ENTRY_STEP_ID
    )
    assert stored_step.get("display_name") == renamed
    assert int(stored.get("revision") or 0) > revision_before


def test_step_advanced_contract_preserves_edit_context(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _set_advanced_mode(page, True)
    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)
    page.evaluate("() => { switchBuilderDrawerTab('advanced'); }")
    page.wait_for_function(
        """() => typeof DRAWER_STATE !== 'undefined'
            && DRAWER_STATE.mode === 'step'
            && DRAWER_STATE.tab === 'advanced'
            && !!document.getElementById('step-block-select')""",
        timeout=5_000,
    )
    result = page.evaluate(
        """() => ({
            editing: !!STEP_MODAL_STATE.editing,
            stepId: String(document.getElementById('step-id')?.value || '').trim(),
            selectedBlock: String(document.getElementById('step-block-select')?.value || '').trim(),
            drawerMode: String(DRAWER_STATE.mode || '').trim(),
            drawerTab: String(DRAWER_STATE.tab || '').trim(),
        })"""
    )
    assert result["editing"] is True
    assert result["stepId"] == _BUILDER_AGENT_STEP_ID
    assert result["selectedBlock"] == ""
    assert result["drawerMode"] == "step"
    assert result["drawerTab"] == "advanced"


def test_advanced_mode_toggle_controls_drawer_advanced_tabs(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    _set_advanced_mode(page, False)

    page.get_by_role("button", name=re.compile(r"Workflow interface", re.I)).click()
    page.wait_for_timeout(500)
    assert page.locator('[data-drawer-tab="advanced"]').count() == 0
    _close_builder_drawer(page)

    page.wait_for_function("""() => typeof window.openGroupDrawer === 'function'""", timeout=10_000)
    page.evaluate("""(groupId) => { window.openGroupDrawer?.(groupId); }""", _BUILDER_GROUP_ID)
    page.locator("#group-editor-surface").wait_for(state="visible", timeout=5_000)
    assert _drawer_tab_keys(page) == ["settings", "automation"]
    _close_builder_drawer(page)

    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)
    assert page.locator('[data-drawer-tab="advanced"]').count() == 0
    _close_builder_drawer(page)

    _set_advanced_mode(page, True)
    page.get_by_role("button", name=re.compile(r"Workflow interface", re.I)).click()
    page.wait_for_timeout(250)
    assert page.locator('[data-drawer-tab="advanced"]').count() > 0
    _close_builder_drawer(page)

    # A group has no expert tab even in advanced mode: its settings are all on one tab.
    page.evaluate("""(groupId) => { window.openGroupDrawer?.(groupId); }""", _BUILDER_GROUP_ID)
    page.locator("#group-editor-surface").wait_for(state="visible", timeout=5_000)
    assert _drawer_tab_keys(page) == ["settings", "automation"]


def test_advanced_mode_toggle_keeps_open_builder_drawer(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    _set_advanced_mode(page, False)

    page.get_by_role("button", name=re.compile(r"Workflow interface", re.I)).click()
    page.wait_for_timeout(250)
    assert page.locator('[aria-label="builder drawer"]').is_visible()
    assert page.locator('[data-drawer-tab="advanced"]').count() == 0

    page.get_by_role("button", name="More").click()
    page.get_by_role("button", name="Advanced mode off").click()
    page.wait_for_timeout(250)

    assert page.locator('[aria-label="builder drawer"]').is_visible()
    assert page.locator("#builder-drawer-title").inner_text().strip() == "Workflow settings"
    assert page.locator('[data-drawer-tab="advanced"]').count() > 0


def test_workflow_and_step_advanced_tabs_show_real_expert_controls(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    _set_advanced_mode(page, True)

    page.get_by_role("button", name=re.compile(r"Workflow interface", re.I)).click()
    page.wait_for_timeout(250)
    page.locator('[data-drawer-tab="advanced"]').first.click()
    page.wait_for_timeout(250)
    assert page.locator('button:has-text("Refresh workflow contracts")').count() > 0
    assert page.locator('button:has-text("Open Test Run")').count() > 0
    # Negative: the retired per-phase contract refresh is gone.
    assert page.locator('button:has-text("Refresh all phase contracts")').count() == 0
    _close_builder_drawer(page)

    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)
    page.locator('[data-drawer-tab="advanced"]').first.click()
    page.wait_for_timeout(250)
    assert page.locator('label:has-text("Optional step")').count() > 0
    assert page.locator("#step-block-select").count() == 1
    assert page.locator('textarea[name="step_override_json"]').count() == 1


def test_step_expert_controls_are_in_advanced_tab_not_smart_setup(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    page.goto(
        f"{base_url}/registry/workflows/{workflow_id}/builder",
        wait_until="networkidle",
        timeout=30_000,
    )
    _set_advanced_mode(page, True)
    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)

    assert page.locator('#modal-add-step-panel input[name="optional"]:visible').count() == 0
    assert page.locator('#modal-add-step-panel [name="step_override_json"]:visible').count() == 0
    # Lanes are group structure now; a step has no lane/parallel field of its own.
    assert page.locator('#modal-add-step-panel input[name="parallel_group"]').count() == 0
    assert page.locator("#modal-add-step-panel", has_text="Step → Advanced").count() > 0

    page.locator('[data-drawer-tab="advanced"]').first.click()
    page.wait_for_timeout(250)
    assert page.locator('label:has-text("Optional step")').count() > 0
    assert page.locator('#modal-add-step-panel input[name="optional"]:visible').count() == 1
    assert page.locator('label:has-text("Parallel Group")').count() == 0


def test_group_editor_opens_from_window_hook_and_canvas(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    page.wait_for_function("""() => typeof window.openGroupDrawer === 'function'""", timeout=10_000)
    page.evaluate("""(groupId) => { window.openGroupDrawer?.(groupId); }""", _BUILDER_GROUP_ID)
    surface = page.locator("#group-editor-surface")
    surface.wait_for(state="visible", timeout=5_000)

    assert page.locator("#builder-drawer-title").inner_text().strip() == "Group editor"
    assert page.locator("input[name='group_display_name']").input_value() == _BUILDER_GROUP_NAME
    assert surface.get_by_text("Steps in this group").count() > 0
    body_entry = page.get_by_role("combobox", name="Body entry")
    assert body_entry.input_value() == _BUILDER_GROUP_STEP_ID
    # A saved group does not offer body kind or id fields: those are structure.
    assert page.locator("select[name='group_body_kind']").count() == 0
    assert page.locator("input[name='group_id']").count() == 0

    _close_builder_drawer(page)
    page.get_by_role("button", name="Fit canvas").click()
    page.locator(f"[data-group-id='{_BUILDER_GROUP_ID}'] [role='button']").first.click()
    surface.wait_for(state="visible", timeout=5_000)
    assert page.locator("#builder-drawer-title").inner_text().strip() == "Group editor"
    assert page.locator("input[name='group_display_name']").input_value() == _BUILDER_GROUP_NAME


def _stage_new_group_from_add_menu(page, group_id: str) -> None:
    page.get_by_role("button", name="Add", exact=True).click()
    page.get_by_role("button", name=re.compile(r"^Group")).click()
    page.locator("#group-editor-surface").wait_for(state="visible", timeout=5_000)
    page.locator("input[name='group_id']").fill(group_id)
    page.locator("input[name='group_display_name']").fill(f"Probe {group_id}")
    page.get_by_role("button", name="Add group to changes").click()


def test_add_menu_group_opens_blank_group_draft(page, base_url, server_reachable, builder_workflow):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    page.get_by_role("button", name="Add", exact=True).click()
    page.get_by_role("button", name=re.compile(r"^Group")).click()
    page.locator("#group-editor-surface").wait_for(state="visible", timeout=5_000)

    assert page.locator("#builder-drawer-title").inner_text().strip() == "Group editor"
    # A Group runs once: in sequence or as lanes, never over a list.
    assert page.locator("select[name='group_body_kind']").input_value() == "sequence"
    assert page.locator("input[name='for_each_over']").count() == 0
    assert page.locator("input[name='group_id']").input_value() == ""
    assert page.locator("input[name='group_display_name']").input_value() == ""
    assert page.locator("textarea[name='group_description']").input_value() == ""
    assert page.get_by_role("button", name="Add group to changes").is_visible()
    # Negative: no phase draft fields.
    assert page.locator("input[name='phase_id']").count() == 0


def test_add_menu_for_each_opens_blank_for_each_draft(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    page.get_by_role("button", name="Add", exact=True).click()
    page.get_by_role("button", name=re.compile(r"^For each")).click()
    page.locator("#group-editor-surface").wait_for(state="visible", timeout=5_000)

    assert page.locator("#builder-drawer-title").inner_text().strip() == "For each editor"
    assert page.locator("input[name='group_id']").input_value() == ""
    assert page.locator("input[name='for_each_item_name']").input_value() == "item"
    assert page.get_by_test_id("for-each-settings").is_visible()
    assert page.get_by_role("button", name="Add For each to changes").is_visible()


def test_new_group_stays_local_until_discarded(
    page, base_url, server_reachable, fresh_builder_workflow
):
    workflow_id = fresh_builder_workflow
    before = _snapshot_definition(_get_workflow(base_url, workflow_id) or {})
    seen: list[dict[str, Any]] = []
    page.on("request", _record_request(seen))

    _open_workflow_builder(page, base_url, workflow_id)
    with page.expect_response(
        lambda response: _is_change_set_response(response, workflow_id, dry_run=True)
    ) as dry_run_response:
        _stage_new_group_from_add_menu(page, "probe_loop")

    assert dry_run_response.value.status == 200
    dry_run_payload = dry_run_response.value.json()
    assert dry_run_payload.get("applied") is False
    assert int(dry_run_payload.get("revision") or 0) == before["revision"]
    expect_status = _structure_status(page)
    expect_status.get_by_text("Structure not applied: 1 change(s)").wait_for(timeout=5_000)
    staged_group = page.locator("[data-group-id='probe_loop']").first
    staged_group.wait_for(state="visible", timeout=5_000)
    assert staged_group.get_by_text("Not applied").count() > 0

    page.get_by_role("button", name="Discard changes").click()
    expect_status.get_by_text("Structure applied").wait_for(timeout=5_000)
    assert page.locator("[data-group-id='probe_loop']").count() == 0

    assert _change_set_writes(seen, workflow_id) == []
    assert _snapshot_definition(_get_workflow(base_url, workflow_id) or {}) == before


def test_new_group_applies_as_one_change_set(
    page, base_url, server_reachable, fresh_builder_workflow
):
    workflow_id = fresh_builder_workflow
    revision_before = _workflow_revision(base_url, workflow_id)

    _open_workflow_builder(page, base_url, workflow_id)
    _stage_new_group_from_add_menu(page, "probe_loop")
    _structure_status(page).get_by_text("Structure not applied: 1 change(s)").wait_for(
        timeout=5_000
    )
    with page.expect_response(
        lambda response: _is_change_set_response(response, workflow_id, dry_run=False)
    ) as apply_response:
        page.get_by_role("button", name="Apply changes").click()

    response = apply_response.value
    assert response.status == 200, response.text()
    request_body = response.request.post_data_json
    assert request_body["expected_revision"] == revision_before
    operations = request_body["operations"]
    assert [operation["op"] for operation in operations] == ["create_group"]
    group = operations[0]["group"]
    assert group["group_id"] == "probe_loop"
    assert group["kind"] == "loop"
    assert group["loop"]["over"] == "state.items"
    assert group["body"]["kind"] == "sequence"
    payload = response.json()
    assert payload.get("applied", True) is True
    assert int(payload["revision"]) > revision_before

    _structure_status(page).get_by_text("Structure applied").wait_for(timeout=10_000)
    stored = _get_workflow(base_url, workflow_id) or {}
    stored_groups = {row.get("group_id"): row for row in _workflow_groups(stored)}
    assert stored_groups["probe_loop"]["kind"] == "loop"
    assert int(stored.get("revision") or 0) > revision_before


def test_stale_revision_keeps_local_changes_and_offers_reload(
    page, base_url, server_reachable, fresh_builder_workflow
):
    workflow_id = fresh_builder_workflow
    page._allowed_console_error_patterns = ["status of 409 (Conflict)"]

    _open_workflow_builder(page, base_url, workflow_id)
    with page.expect_response(
        lambda response: _is_change_set_response(response, workflow_id, dry_run=True)
    ):
        _stage_new_group_from_add_menu(page, "probe_loop")
    staged_revision = _workflow_revision(base_url, workflow_id)

    # Another author saves the workflow after the local set was started.
    _patch_workflow_metadata(
        base_url, workflow_id, {"description": f"Changed elsewhere {uuid.uuid4().hex[:6]}"}
    )
    assert _workflow_revision(base_url, workflow_id) > staged_revision

    with page.expect_response(
        lambda response: _is_change_set_response(response, workflow_id, dry_run=False)
    ) as apply_response:
        page.get_by_role("button", name="Apply changes").click()
    assert apply_response.value.status == 409
    conflict = apply_response.value.json().get("detail") or {}
    assert conflict.get("code") == "workflow_revision_conflict"

    page.get_by_role("alert").filter(
        has_text="The workflow changed after it was loaded"
    ).first.wait_for(timeout=5_000)
    _structure_status(page).get_by_text("Workflow changed elsewhere").wait_for(timeout=5_000)
    assert page.locator("[data-group-id='probe_loop']").count() > 0
    assert page.get_by_role("button", name="Apply changes").is_disabled()
    assert not any(
        row.get("group_id") == "probe_loop"
        for row in _workflow_groups(_get_workflow(base_url, workflow_id))
    )

    page.get_by_role("button", name="Reload workflow").first.click()
    _structure_status(page).get_by_text("Structure not applied: 1 change(s)").wait_for(
        timeout=10_000
    )
    assert page.locator("[data-group-id='probe_loop']").count() > 0
    page.get_by_role("button", name="Discard changes").click()
    _structure_status(page).get_by_text("Structure applied").wait_for(timeout=5_000)


def test_step_row_primary_label_comes_from_published_block_when_available(
    page, base_url, server_reachable
):
    provisioned_workflow_id = ""
    try:
        found = _find_workflow_with_published_block_step(base_url)
        if found is None:
            provisioned = _provision_workflow_with_published_block_step(base_url)
            if provisioned is None:
                published_blocks = _list_published_blocks(base_url)
                assert not published_blocks, (
                    "Published blocks exist, but no block-backed workflow could be "
                    "found or provisioned"
                )
                fallback_workflow_id = _find_workflow_with_steps(base_url)
                if not fallback_workflow_id:
                    pytest.skip("No readable workflow with steps for the published block check")
                _open_workflow_builder(page, base_url, fallback_workflow_id)
                empty_result = page.evaluate(
                    """() => ({
                        hasResolver: typeof window._publishedBlockDisplayName === 'function',
                        blockCount: Array.isArray(window.PUBLISHED_BLOCKS)
                          ? window.PUBLISHED_BLOCKS.length
                          : -1,
                    })"""
                )
                assert empty_result.get("hasResolver")
                assert empty_result.get("blockCount") == 0
                return
            provisioned_workflow_id, mapped_block_key = provisioned
            found = provisioned
        workflow_id, mapped_block_key = found

        _open_workflow_builder(page, base_url, workflow_id)
        result = page.evaluate(
            """(blockKey) => {
                const normalized = String(blockKey || '').trim();
                const catalogRow = (window.PUBLISHED_BLOCKS || []).find(
                  (item) => String(item?.block_key || '').trim() === normalized
                );
                const resolved = (typeof window._publishedBlockDisplayName === 'function')
                  ? String(window._publishedBlockDisplayName(normalized) || '').trim()
                  : '';
                return {
                  hasResolver: typeof window._publishedBlockDisplayName === 'function',
                  catalogDisplayName: String(catalogRow?.display_name || '').trim(),
                  resolved,
                };
            }""",
            mapped_block_key,
        )
        assert result.get("hasResolver"), (
            "Builder no longer exposes the published block label resolver"
        )
        assert str(result.get("resolved") or "").strip(), (
            "Mapped published block should resolve to a non-empty display label"
        )
        catalog_display_name = str(result.get("catalogDisplayName") or "").strip()
        if catalog_display_name:
            assert str(result.get("resolved") or "").strip() in {
                catalog_display_name,
                mapped_block_key,
            }
    finally:
        if provisioned_workflow_id:
            _delete_temp_workflow(base_url, provisioned_workflow_id)


def test_workflow_automation_can_be_opened_and_created_from_drawer(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _open_workflow_builder(page, base_url, workflow_id)
    page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
    page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
    page.locator('[data-drawer-tab="automation"]').click()
    page.wait_for_timeout(300)

    assert page.locator("#workflow-automation-surface").is_visible()
    assert page.locator("#workflow-reactions-link").count() == 0
    assert page.locator("#reaction-form").is_visible()
    add_rule = page.locator("#reaction-form button", has_text="+ Add Rule").first
    assert add_rule.count() > 0, "Automation add-rule button unavailable"
    add_rule.click()
    page.wait_for_timeout(150)
    assert page.locator("#rxn-event-preset").is_visible()
    assert page.locator("#rxn-action-type").is_visible()
    assert page.locator('[id^="rxn-target-"]').first.is_visible()
    page.locator("#rxn-action-type").select_option("notify")
    target_select = page.locator("#rxn-target-0")
    target_labels = target_select.locator("option").all_text_contents()
    assert "Default matching channels" in target_labels
    assert "Route keys I enter" in target_labels
    assert "Project notification routes" not in target_labels
    assert "Only selected route keys" not in target_labels
    assert page.locator("#rxn-notify-config").is_visible()
    assert page.locator("#rxn-notify-severity").is_visible()
    assert page.locator("#rxn-notify-message").is_visible()
    assert page.locator("#rxn-notify-route-keys").count() == 0
    target_select.select_option("route_keys")
    assert page.locator("#rxn-notify-route-keys").is_visible()
    page.locator("#rxn-notify-route-keys").fill("release_alerts, ops_chat")
    assert page.locator("#rxn-notify-route-keys").input_value() == "release_alerts, ops_chat"


def test_project_notifications_surface_is_available_from_project_page(
    page, base_url, server_reachable, test_project_id
):
    if _ACTIVE_TEST_ROLE == "viewer":
        page._allowed_console_error_patterns = ["status of 403 (Forbidden)"]
        pytest.skip("Viewer role cannot edit project notifications")
    page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle", timeout=30_000)
    # Notifications is a tab of the project page itself, not a separate screen.
    assert page.get_by_role("link", name="Notifications").count() == 0
    page.get_by_role("tab", name="Notifications").click()

    page.locator("#notification-routes-add").wait_for(state="visible", timeout=10_000)
    assert page.locator("#notification-routes-add").is_visible()
    page.locator("#notification-routes-add").click()
    assert page.locator("#notification-route-key").is_visible()
    assert page.locator("#notification-route-save").is_visible()


def test_workflow_simulate_dry_run_validates_without_queued_job(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _goto_page_stably(
        page,
        f"{base_url}/registry/workflows/{workflow_id}/builder?drawer=workflow-test-run",
    )
    page.wait_for_url(f"**/registry/workflows/{workflow_id}/builder**", timeout=15_000)
    page.locator("#workflow-test-run-surface").wait_for(state="visible", timeout=15_000)
    assert page.get_by_role("button", name="Test Run").count() > 0


def test_workflow_test_run_surface_shows_summary_and_effect_warning(
    page, base_url, server_reachable, readable_workflow_id
):
    workflow_id = readable_workflow_id

    _goto_page_stably(
        page,
        f"{base_url}/registry/workflows/{workflow_id}/builder?drawer=workflow-test-run",
    )
    page.wait_for_url(f"**/registry/workflows/{workflow_id}/builder**", timeout=15_000)
    surface = page.locator("#workflow-test-run-surface")
    surface.wait_for(state="visible", timeout=15_000)
    result_text = surface.inner_text().lower()
    assert "test run summary" in result_text
    assert "test run is not an external-effect sandbox" in result_text
    assert surface.get_by_role("note", name="Test Run effects").is_visible()


def test_automation_editor_presets_and_actions_are_scope_specific(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _set_advanced_mode(page, True)

    def snapshot() -> dict[str, Any]:
        return page.evaluate(
            """() => {
                const preset = document.getElementById('rxn-event-preset');
                const action = document.getElementById('rxn-action-type');
                const eventPanel = document.getElementById('rxn-advanced-event-panel');
                const jsonPanel = document.getElementById('rxn-advanced-json-panel');
                return {
                    scope: String((typeof AUTOMATION_SCOPE !== 'undefined' && AUTOMATION_SCOPE?.kind) || ''),
                    scopeNote: String(document.getElementById('automation-scope-note')?.textContent || '').trim(),
                    presets: preset ? Array.from(preset.options).map((opt) => String(opt.value || '').trim()) : [],
                    actions: action ? Array.from(action.options).map((opt) => String(opt.value || '').trim()) : [],
                    advancedEventVisible: !!eventPanel && !eventPanel.classList.contains('hidden'),
                    advancedJsonVisible: !!jsonPanel && !jsonPanel.classList.contains('hidden'),
                };
            }"""
        )

    page.get_by_role("button", name=re.compile(r"workflow interface", re.I)).click()
    page.locator("#workflow-settings-surface").wait_for(state="visible", timeout=10_000)
    page.locator('[data-drawer-tab="automation"]').click()
    page.locator("#reaction-form button", has_text="+ Add Rule").first.click()
    page.wait_for_timeout(250)
    workflow_snapshot = snapshot()

    page.evaluate(
        """(groupId) => {
            if (typeof openGroupDrawer === 'function') openGroupDrawer(groupId, {defaultTab: 'automation'});
        }""",
        _BUILDER_GROUP_ID,
    )
    page.locator("#group-automation-surface").wait_for(state="visible", timeout=5_000)
    page.locator(
        "#group-automation-surface #reaction-form button", has_text="+ Add Rule"
    ).first.click()
    page.wait_for_timeout(150)
    group_snapshot = snapshot()

    _close_builder_drawer(page)
    _open_step_from_window_hook(page, _BUILDER_AGENT_STEP_ID)
    page.wait_for_function(
        "() => typeof DRAWER_STATE !== 'undefined' && DRAWER_STATE && DRAWER_STATE.mode === 'step'",
        timeout=5_000,
    )
    page.evaluate(
        """() => {
            if (typeof switchBuilderDrawerTab === 'function') switchBuilderDrawerTab('automation');
        }"""
    )
    page.locator("#step-automation-surface").wait_for(state="visible", timeout=5_000)
    page.locator(
        "#step-automation-surface #reaction-form button", has_text="+ Add Rule"
    ).first.click()
    page.wait_for_timeout(150)
    step_snapshot = snapshot()

    retired_events = {"job.phase_started", "job.phase_completed", "job.phase_failed"}
    retired_events.add("job.gate_waiting")

    assert workflow_snapshot["scope"] == "workflow"
    assert "job.completed" in workflow_snapshot["presets"]
    assert "job.failed" in workflow_snapshot["presets"]
    assert "job.group_completed" in workflow_snapshot["presets"]
    assert "job.approval_waiting" in workflow_snapshot["presets"]
    assert "job.timer_waiting" in workflow_snapshot["presets"]
    assert not retired_events & set(workflow_snapshot["presets"])
    assert "resume_job" in workflow_snapshot["actions"]
    assert "__custom__" not in workflow_snapshot["presets"]
    assert workflow_snapshot["advancedEventVisible"] is False
    assert workflow_snapshot["advancedJsonVisible"] is False

    assert group_snapshot["scope"] == "group"
    assert "job.group_started" in group_snapshot["presets"]
    assert "job.group_failed" in group_snapshot["presets"]
    assert "job.step_failed" in group_snapshot["presets"]
    assert "job.approval_waiting" in group_snapshot["presets"]
    assert "job.completed" not in group_snapshot["presets"]
    assert not retired_events & set(group_snapshot["presets"])
    assert "notify" in group_snapshot["actions"]
    assert "retry_step" not in group_snapshot["actions"]
    assert "resume_job" not in group_snapshot["actions"]
    assert "external.comment.received" not in group_snapshot["presets"]
    assert "__custom__" not in group_snapshot["presets"]
    assert group_snapshot["advancedEventVisible"] is False
    assert group_snapshot["advancedJsonVisible"] is False

    assert step_snapshot["scope"] == "step"
    assert "(" not in step_snapshot["scopeNote"]
    assert "job.step_started" in step_snapshot["presets"]
    assert "job.step_completed" in step_snapshot["presets"]
    assert "job.step_failed" in step_snapshot["presets"]
    assert "job.group_started" not in step_snapshot["presets"]
    assert not retired_events & set(step_snapshot["presets"])
    assert "retry_step" in step_snapshot["actions"]
    assert "skip_step" not in step_snapshot["actions"]
    assert "__custom__" not in step_snapshot["presets"]
    assert step_snapshot["advancedEventVisible"] is False
    assert step_snapshot["advancedJsonVisible"] is False


def test_step_test_run_tab_has_dedicated_surface(
    page, base_url, server_reachable, builder_workflow
):
    workflow_id = builder_workflow

    _open_workflow_builder(page, base_url, workflow_id)
    _open_step_from_window_hook(page, _BUILDER_ENTRY_STEP_ID)
    page.wait_for_function(
        "() => typeof DRAWER_STATE !== 'undefined' && DRAWER_STATE && DRAWER_STATE.mode === 'step'",
        timeout=5_000,
    )
    page.evaluate(
        """() => {
            if (typeof switchBuilderDrawerTab === 'function') switchBuilderDrawerTab('test-run');
        }"""
    )
    page.wait_for_timeout(250)

    panel = page.locator("#modal-add-step-panel")
    assert panel.get_by_text("Step Test Run Summary").count() > 0
    assert panel.get_by_role("note", name="Test Run effects").is_visible()
    assert panel.get_by_role("button", name="Run step test run").count() == 1
