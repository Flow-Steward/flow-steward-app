"""Strict UI/API contracts for registry workflow endpoints.

A workflow definition is ``steps`` (each may carry ``group_id``/``lane_id``),
real ``groups`` and ``settings.execution_graph``. Metadata saves never carry
the definition; ``POST /api/workflows/{workflow_id}/changes`` is its only writer.
"""

from __future__ import annotations

import os
import time
import uuid
from itertools import pairwise
from typing import Any

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests._workflow_definitions import STOP_STEP_ID, edge, node, stop_step
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
_AUTH_SESSION_CACHE: tuple[requests.Session, bool] | None = None

#: Root steps of the temporary workflow, in execution order, before the Stop.
_TEMP_STEP_IDS = ("analysis", "review")


def _workflow_request(method: str, url: str, *, requester=None, **kwargs):
    method_name = method.upper()
    call = requester
    if call is None:
        session, verify_ssl = _role_bound_request_session()
        kwargs.setdefault("verify", verify_ssl)
        if method_name in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(kwargs.get("headers") or {})
            csrf = str(session.cookies.get("fs_csrf") or "").strip()
            if csrf and "x-csrf-token" not in {
                str(key).lower(): value for key, value in headers.items()
            }:
                headers["x-csrf-token"] = csrf
            kwargs["headers"] = headers
        call = session.request
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return call(method, url, timeout=10 + (attempt * 5), **kwargs)
        except requests.exceptions.ReadTimeout as exc:
            last_error = exc
            if method_name not in {"GET", "HEAD", "OPTIONS"}:
                raise
            if attempt >= 2:
                raise
            time.sleep(1 + attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{method} {url} failed without a response")


def _workflow_steps(payload: dict | None) -> list[dict]:
    rows = payload.get("steps") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _workflow_groups(payload: dict | None) -> list[dict]:
    rows = payload.get("groups") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _execution_graph(payload: dict | None) -> dict:
    settings = payload.get("settings") if isinstance(payload, dict) else None
    graph = settings.get("execution_graph") if isinstance(settings, dict) else None
    return graph if isinstance(graph, dict) else {}


def _step_ids(payload: dict | None) -> list[str]:
    return [
        str(step.get("step_id") or "").strip()
        for step in _workflow_steps(payload)
        if str(step.get("step_id") or "").strip()
    ]


def _edge_targets(payload: dict | None) -> dict[tuple[str, str, str], dict]:
    """``(source kind, source id, outcome) -> target`` of the stored graph."""
    targets: dict[tuple[str, str, str], dict] = {}
    for row in _execution_graph(payload).get("edges") or []:
        if not isinstance(row, dict):
            continue
        source = row.get("source") or {}
        outcome = row.get("outcome") or {}
        targets[
            (
                str(source.get("kind") or ""),
                str(source.get("id") or ""),
                str(outcome.get("name") or ""),
            )
        ] = row.get("target") or {}
    return targets


def _execution_order(payload: dict | None) -> list[str]:
    """Root step ids from the entry along success edges (no groups, no cycles)."""
    graph = _execution_graph(payload)
    targets = _edge_targets(payload)
    order: list[str] = []
    current = graph.get("entry") or {}
    while isinstance(current, dict) and current.get("kind") == "step":
        step_id = str(current.get("id") or "")
        if not step_id or step_id in order:
            break
        order.append(step_id)
        current = targets.get(("step", step_id, "success"))
    return order


def _workflow_list_rows(payload) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("workflows", [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _csrf_headers(session: requests.Session | None) -> dict[str, str]:
    if session is None:
        return {}
    token = str(session.cookies.get("fs_csrf") or "").strip()
    return {"x-csrf-token": token} if token else {}


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
        f"Could not authenticate registry workflow helper session for {resolved.role!r}: "
        f"{resp.status_code} {resp.text[:300]}"
    )
    _AUTH_SESSION_CACHE = (session, verify_ssl)
    return _AUTH_SESSION_CACHE


def _assert_registry_status(
    resp: requests.Response,
    active_test_role: str,
    *,
    admin_statuses: set[int] | None = None,
) -> None:
    if active_test_role == "admin":
        if admin_statuses is None:
            assert resp.ok, (
                f"Expected successful admin response, got {resp.status_code}: {resp.text[:300]}"
            )
            return
        assert resp.status_code in admin_statuses, (
            f"Expected admin status in {sorted(admin_statuses)}, got {resp.status_code}: {resp.text[:300]}"
        )
        return
    if active_test_role == "operator":
        assert resp.status_code in (200, 404, 405), (
            f"Expected operator-safe response, got {resp.status_code}: {resp.text[:300]}"
        )
        return
    assert resp.status_code == 403, f"Non-admin access must be forbidden, got {resp.status_code}"


def _get_detail(base_url: str, workflow_id: str, session, verify_ssl) -> dict:
    resp = _workflow_request(
        "GET",
        f"{base_url}/api/workflows/{workflow_id}",
        requester=session.request if session else None,
        verify=verify_ssl,
    )
    assert resp.status_code == 200, f"workflow read failed: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def _apply_changes(
    base_url: str,
    workflow_id: str,
    operations: list[dict[str, Any]],
    session,
    verify_ssl,
    *,
    expected_revision: int | None = None,
    dry_run: bool = False,
) -> requests.Response:
    body: dict[str, Any] = {
        "expected_revision": (
            expected_revision
            if expected_revision is not None
            else int(_get_detail(base_url, workflow_id, session, verify_ssl).get("revision") or 1)
        ),
        "operations": operations,
    }
    if dry_run:
        body["dry_run"] = True
    return _workflow_request(
        "POST",
        f"{base_url}/api/workflows/{workflow_id}/changes",
        requester=session.request if session else None,
        verify=verify_ssl,
        headers=_csrf_headers(session),
        json=body,
    )


def _set_variable_step(step_id: str, *, group_id: str | None = None, value: Any = True) -> dict:
    step: dict[str, Any] = {
        "step_id": step_id,
        "step_kind": "set_variable",
        "display_name": step_id.replace("_", " ").title(),
        "override": {"set_variable": {"target": f"state.{step_id}", "value": value}},
    }
    if group_id:
        step["group_id"] = group_id
    return step


def _connect(source: str, target: dict, *, source_kind: str = "step", replace: bool = False):
    operation: dict[str, Any] = {
        "op": "connect",
        "edge": edge(source, target, source_kind=source_kind),
    }
    if replace:
        operation["replace"] = True
    return operation


def _temp_definition_operations() -> list[dict[str, Any]]:
    """analysis -> review -> done, each a step of the workflow root."""
    step_ids = [*_TEMP_STEP_IDS, STOP_STEP_ID]
    return [
        *({"op": "create_step", "step": _set_variable_step(step_id)} for step_id in _TEMP_STEP_IDS),
        {"op": "create_step", "step": stop_step()},
        *(_connect(source, node(target)) for source, target in pairwise(step_ids)),
        {"op": "set_entry", "target": node(_TEMP_STEP_IDS[0])},
    ]


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url, ui_auth_session):
    session, verify_ssl = ui_auth_session
    resp = _workflow_request(
        "GET",
        f"{base_url}/api/workflows",
        requester=session.request if session else None,
        verify=verify_ssl,
    )
    assert resp.status_code != 404, "Registry mode is not enabled for workflow endpoints"


@pytest.fixture(scope="module")
def first_workflow_id(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    """Return one workflow that already has steps."""
    if active_test_role != "admin":
        return NEUTRAL_WORKFLOW_ID
    session, verify_ssl = ui_auth_session
    resp = _workflow_request(
        "GET",
        f"{base_url}/api/workflows",
        requester=session.request if session else None,
        verify=verify_ssl,
    )
    assert resp.status_code == 200, (
        f"Cannot fetch workflows list: {resp.status_code} {resp.text[:300]}"
    )
    workflows = _workflow_list_rows(resp.json())
    for row in workflows:
        workflow_id = str(row.get("workflow_id") or row.get("id") or "").strip()
        if not workflow_id:
            continue
        detail_resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/{workflow_id}",
            requester=session.request if session else None,
            verify=verify_ssl,
        )
        if detail_resp.status_code != 200:
            continue
        if _workflow_steps(detail_resp.json()):
            return workflow_id
    pytest.fail("No workflow with steps found in registry")


@pytest.fixture(scope="module")
def first_valid_workflow_id(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    """Return one workflow whose simulate/test-run validation is clean."""
    if active_test_role != "admin":
        return NEUTRAL_WORKFLOW_ID
    session, verify_ssl = ui_auth_session
    list_resp = _workflow_request(
        "GET",
        f"{base_url}/api/workflows",
        requester=session.request if session else None,
        verify=verify_ssl,
    )
    assert list_resp.status_code == 200, (
        f"Cannot fetch workflows list for simulate contract: {list_resp.status_code} {list_resp.text[:300]}"
    )
    workflows = _workflow_list_rows(list_resp.json())
    for row in workflows:
        workflow_id = str(row.get("workflow_id") or row.get("id") or "").strip()
        if not workflow_id:
            continue
        validation_resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/{workflow_id}/contract-validation",
            requester=session.request if session else None,
            verify=verify_ssl,
        )
        if validation_resp.status_code != 200:
            continue
        validation = (validation_resp.json() or {}).get("validation") or {}
        if isinstance(validation, dict) and bool(validation.get("valid")):
            return workflow_id
    pytest.fail("No validation-clean workflow found in registry")


@pytest.fixture
def temp_valid_workflow(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    """Create a deterministic valid workflow: metadata, then one change set."""
    if active_test_role != "admin":
        yield ""
        return
    session, verify_ssl = ui_auth_session
    workflow_id = ""
    create_resp = None
    for _ in range(5):
        workflow_id = f"test_ui_workflow_{uuid.uuid4().hex[:12]}"
        create_resp = _workflow_request(
            "POST",
            f"{base_url}/api/workflows",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={
                "workflow_id": workflow_id,
                "display_name": "Test UI Workflow",
                "description": "Created by strict UI registry tests",
                "enabled": False,
            },
        )
        if create_resp.status_code != 409:
            break
    assert create_resp is not None
    assert create_resp.status_code == 200, (
        f"workflow create failed: {create_resp.status_code} {create_resp.text[:300]}"
    )
    create_body = create_resp.json()
    assert create_body["status"] == "created"
    assert create_body["workflow_id"] == workflow_id
    assert create_body["validation"]["valid"] is True
    try:
        changes_resp = _apply_changes(
            base_url, workflow_id, _temp_definition_operations(), session, verify_ssl
        )
        assert changes_resp.status_code == 200, (
            f"workflow definition change set failed: {changes_resp.status_code} "
            f"{changes_resp.text[:500]}"
        )
        changes_body = changes_resp.json()
        assert changes_body["applied"] is True
        error_issues = [
            issue
            for issue in changes_body.get("issues") or []
            if str(issue.get("severity") or "error").lower() == "error"
        ]
        assert error_issues == [], error_issues
        yield workflow_id
    finally:
        _workflow_request(
            "DELETE",
            f"{base_url}/api/workflows/{workflow_id}",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
        )


class TestRegistryWorkflowsList:
    """Workflow list API."""

    def test_workflows_api_returns_list(self, base_url, registry_mode_enabled, active_test_role):
        resp = _workflow_request("GET", f"{base_url}/api/workflows", verify=False)
        if active_test_role == "viewer":
            assert resp.status_code == 403
            return
        assert resp.status_code == 200
        workflows = _workflow_list_rows(resp.json())
        assert isinstance(workflows, list)
        assert workflows, "registry should expose at least one workflow in seeded environments"

    def test_workflows_have_required_fields(
        self, base_url, registry_mode_enabled, active_test_role
    ):
        resp = _workflow_request("GET", f"{base_url}/api/workflows", verify=False)
        _assert_registry_status(resp, active_test_role, admin_statuses={200})
        if active_test_role != "admin":
            return
        workflows = _workflow_list_rows(resp.json())
        assert workflows
        first = workflows[0]
        assert str(first.get("workflow_id") or "").strip()
        assert isinstance(first.get("enabled"), bool)


class TestRegistryWorkflowDetail:
    """Workflow detail API."""

    def test_workflow_detail_returns_data(
        self, base_url, registry_mode_enabled, first_workflow_id, active_test_role
    ):
        resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/{first_workflow_id}",
            verify=False,
        )
        if active_test_role == "viewer":
            assert resp.status_code == 403
            return
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data["workflow_id"] == first_workflow_id

    def test_workflow_detail_has_steps_groups_and_execution_graph(
        self, base_url, registry_mode_enabled, first_workflow_id, active_test_role
    ):
        resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/{first_workflow_id}",
            verify=False,
        )
        if active_test_role == "viewer":
            assert resp.status_code == 403
            return
        assert resp.status_code == 200
        detail = resp.json()
        steps = _workflow_steps(detail)
        assert steps, "workflow detail should include at least one step"
        assert all(str(step.get("step_id") or "").strip() for step in steps)
        assert all(str(step.get("step_kind") or "").strip() for step in steps)
        assert isinstance(detail.get("groups"), list)
        group_ids = {str(group.get("group_id") or "") for group in _workflow_groups(detail)}
        assert all(
            step.get("group_id") in (None, "") or step.get("group_id") in group_ids
            for step in steps
        )
        graph = _execution_graph(detail)
        assert graph.get("schema_version") == 1
        assert isinstance(graph.get("edges"), list)
        assert isinstance(graph.get("entry"), dict)
        assert int(detail.get("revision") or 0) >= 1
        # Negative: the phase model is gone from the read model.
        assert "phases" not in detail
        assert "_phases" not in detail

    def test_workflow_not_found_returns_404(
        self, base_url, registry_mode_enabled, active_test_role
    ):
        resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/nonexistent_workflow_xyz",
            verify=False,
        )
        if active_test_role == "viewer":
            assert resp.status_code == 403
            return
        assert resp.status_code == 404


class TestRegistryWorkflowSimulate:
    """Per-workflow simulate contracts."""

    @pytest.mark.parametrize(
        ("task_type", "title"),
        [
            ("feature", "Add user profile"),
            ("bugfix", "Fix null pointer exception in auth"),
            ("incident", "Investigate intermittent production outage"),
        ],
    )
    def test_simulate_returns_classification_and_step_plan(
        self,
        base_url,
        registry_mode_enabled,
        first_valid_workflow_id,
        active_test_role,
        task_type,
        title,
    ):
        resp = requests.post(
            f"{base_url}/api/workflows/{first_valid_workflow_id}/simulate",
            json={
                "task_envelope": {
                    "title": title,
                    "task_summary": title,
                    "task_type": task_type,
                }
            },
            verify=False,
            timeout=10,
        )
        if active_test_role != "admin":
            assert resp.status_code == 403
            return
        _assert_registry_status(resp, active_test_role, admin_statuses={200})
        payload = resp.json()
        assert payload["workflow_id"] == first_valid_workflow_id
        assert payload["validation"]["valid"] is True
        assert payload["classification"]["task_type"] == task_type
        assert isinstance(payload.get("steps"), list)
        assert payload["steps"]
        assert all(str(row.get("step_id") or "").strip() for row in payload["steps"])
        assert "phases" not in payload


class TestRegistryWorkflowCreate:
    """Workflow creation contracts."""

    def test_create_workflow_without_steps_returns_valid_empty_definition(
        self, base_url, registry_mode_enabled, ui_auth_session, active_test_role
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = f"test_wf_no_steps_{uuid.uuid4().hex[:8]}"
        resp = _workflow_request(
            "POST",
            f"{base_url}/api/workflows",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={
                "workflow_id": workflow_id,
                "display_name": "Test",
            },
        )
        if active_test_role != "admin":
            assert resp.status_code == 403
            return
        try:
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["status"] == "created"
            assert payload["workflow_id"] == workflow_id
            assert payload["validation"]["valid"] is True
            assert payload["validation"]["errors"] == []
            detail = _get_detail(base_url, workflow_id, session, verify_ssl)
            assert _workflow_steps(detail) == []
            assert _workflow_groups(detail) == []
        finally:
            _workflow_request(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
            )

    def test_metadata_saves_refuse_definition_fields(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        if active_test_role != "admin":
            return
        workflow_id = f"test_wf_definition_{uuid.uuid4().hex[:8]}"
        create_resp = _workflow_request(
            "POST",
            f"{base_url}/api/workflows",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={
                "workflow_id": workflow_id,
                "display_name": "Definition In Metadata",
                "steps": [_set_variable_step("analysis")],
            },
        )
        try:
            assert create_resp.status_code in {400, 422}, create_resp.text[:300]
            assert "/changes" in create_resp.text
            missing = _workflow_request(
                "GET",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
            )
            assert missing.status_code == 404
        finally:
            _workflow_request(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
            )

        before = _get_detail(base_url, temp_valid_workflow, session, verify_ssl)
        for field, value in (
            ("groups", []),
            ("phases", []),  # Negative: the retired phase field is refused, not ignored.
        ):
            patch_resp = _workflow_request(
                "PATCH",
                f"{base_url}/api/workflows/{temp_valid_workflow}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
                json={field: value, "expected_revision": int(before["revision"])},
            )
            assert patch_resp.status_code in {400, 422}, (field, patch_resp.text[:300])
            assert "/changes" in patch_resp.text
        after = _get_detail(base_url, temp_valid_workflow, session, verify_ssl)
        assert after["revision"] == before["revision"]
        assert _step_ids(after) == _step_ids(before)

    def test_create_workflow_with_valid_data_persists_and_validates(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        if active_test_role != "admin":
            detail_resp = _workflow_request(
                "GET",
                f"{base_url}/api/workflows/{NEUTRAL_WORKFLOW_ID}",
                verify=False,
            )
            assert detail_resp.status_code == (403 if active_test_role == "viewer" else 200)
            return
        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        detail = _get_detail(base_url, temp_valid_workflow, session, verify_ssl)
        assert detail["workflow_id"] == temp_valid_workflow
        assert sorted(_step_ids(detail)) == sorted([*_TEMP_STEP_IDS, STOP_STEP_ID])
        assert _execution_order(detail) == [*_TEMP_STEP_IDS, STOP_STEP_ID]
        assert _workflow_groups(detail) == []

        validate_resp = (session or requests).post(
            f"{base_url}/api/workflows/{temp_valid_workflow}/validate",
            json={},
            headers=_csrf_headers(session),
            verify=verify_ssl,
            timeout=10,
        )
        assert validate_resp.status_code == 200
        issues = validate_resp.json()["issues"]
        error_issues = [
            issue
            for issue in issues
            if isinstance(issue, dict)
            and (
                str(issue.get("severity") or "").lower() == "error"
                or str(issue.get("code") or "").endswith(".error")
            )
        ]
        assert error_issues == []


class TestRegistryWorkflowMutations:
    """Strict mutation contracts: metadata saves and definition change sets."""

    def test_update_workflow_settings_persists_description_and_tags(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        before = (
            _get_detail(base_url, workflow_id, session, verify_ssl)
            if active_test_role == "admin"
            else {}
        )
        patch_resp = _workflow_request(
            "PATCH",
            f"{base_url}/api/workflows/{workflow_id}",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={
                "description": "Updated by reliability hardening regression coverage",
                "tags": ["release", "hardening"],
                "settings": {
                    "builder_notes": {"source": "ui-regression", "term": "Test Run"},
                    # Service-owned: a metadata save keeps the stored graph.
                    "execution_graph": {"schema_version": 1, "entry": None, "edges": []},
                },
                "expected_revision": int(before.get("revision") or 1),
            },
        )
        if active_test_role != "admin":
            assert patch_resp.status_code == 403
            return
        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        assert patch_resp.status_code == 200, (
            f"workflow patch failed: {patch_resp.status_code} {patch_resp.text[:300]}"
        )

        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        assert detail["description"] == "Updated by reliability hardening regression coverage"
        assert detail["tags"] == ["release", "hardening"]
        settings = detail.get("settings") or {}
        assert settings.get("builder_notes", {}).get("term") == "Test Run"
        assert _execution_graph(detail) == _execution_graph(before)
        assert _step_ids(detail) == _step_ids(before)

    def test_delete_workflow_removes_created_workflow(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = f"test_ui_workflow_delete_{uuid.uuid4().hex[:10]}"
        if active_test_role != "admin":
            create_resp = _workflow_request(
                "POST",
                f"{base_url}/api/workflows",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
                json={"workflow_id": workflow_id, "display_name": "Delete Probe", "enabled": False},
            )
            assert create_resp.status_code == 403
            return

        create_resp = _workflow_request(
            "POST",
            f"{base_url}/api/workflows",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={"workflow_id": workflow_id, "display_name": "Delete Probe", "enabled": False},
        )
        assert create_resp.status_code == 200, (
            f"workflow create for delete test failed: {create_resp.status_code} {create_resp.text[:300]}"
        )
        try:
            delete_resp = _workflow_request(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
            )
            assert delete_resp.status_code == 200, (
                f"workflow delete failed: {delete_resp.status_code} {delete_resp.text[:300]}"
            )
            body = delete_resp.json()
            assert body["status"] == "deleted"
            assert body["workflow_id"] == workflow_id

            detail_resp = _workflow_request(
                "GET",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
            )
            assert detail_resp.status_code == 404
        finally:
            _workflow_request(
                "DELETE",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
            )

    def test_change_set_creates_updates_and_deletes_group(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        group_id = f"delivery_{uuid.uuid4().hex[:6]}"
        body_step_id = f"{group_id}_item"
        create_operations = [
            {
                "op": "create_group",
                "group": {
                    "group_id": group_id,
                    "display_name": "Delivery",
                    "description": "Created by regression coverage",
                    "body": {"kind": "sequence", "entry": node(body_step_id)},
                },
            },
            {"op": "create_step", "step": _set_variable_step(body_step_id, group_id=group_id)},
            _connect(body_step_id, {"kind": "group_exit", "group_id": group_id, "lane_id": None}),
            # review -> group -> done replaces review -> done.
            _connect(_TEMP_STEP_IDS[-1], node(group_id, "group"), replace=True),
            _connect(group_id, node(STOP_STEP_ID), source_kind="group"),
        ]
        if active_test_role != "admin":
            denied = _apply_changes(
                base_url,
                workflow_id,
                create_operations,
                session,
                verify_ssl,
                expected_revision=1,
            )
            assert denied.status_code == 403
            return
        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        revision_before = int(_get_detail(base_url, workflow_id, session, verify_ssl)["revision"])

        create_resp = _apply_changes(
            base_url,
            workflow_id,
            create_operations,
            session,
            verify_ssl,
            expected_revision=revision_before,
        )
        assert create_resp.status_code == 200, (
            f"group change set failed: {create_resp.status_code} {create_resp.text[:500]}"
        )
        created_revision = int(create_resp.json()["revision"])
        assert created_revision > revision_before

        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        groups = {group.get("group_id"): group for group in _workflow_groups(detail)}
        # A Group runs its body once: it carries no kind and no loop source.
        assert "kind" not in groups[group_id]
        assert "loop" not in groups[group_id]
        assert groups[group_id]["display_name"] == "Delivery"
        body_step = next(
            step for step in _workflow_steps(detail) if step["step_id"] == body_step_id
        )
        assert body_step.get("group_id") == group_id
        targets = _edge_targets(detail)
        assert targets[("step", _TEMP_STEP_IDS[-1], "success")] == node(group_id, "group")
        assert targets[("group", group_id, "success")] == node(STOP_STEP_ID)

        update_resp = _apply_changes(
            base_url,
            workflow_id,
            [
                {
                    "op": "update_group_config",
                    "group_id": group_id,
                    "patch": {"display_name": "Delivery Updated"},
                }
            ],
            session,
            verify_ssl,
            expected_revision=created_revision,
        )
        assert update_resp.status_code == 200, update_resp.text[:500]
        updated_revision = int(update_resp.json()["revision"])
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        groups = {group.get("group_id"): group for group in _workflow_groups(detail)}
        assert groups[group_id]["display_name"] == "Delivery Updated"

        # A group's id is structure: a config change cannot rewrite it.
        refused = _apply_changes(
            base_url,
            workflow_id,
            [{"op": "update_group_config", "group_id": group_id, "patch": {"group_id": "renamed"}}],
            session,
            verify_ssl,
            expected_revision=updated_revision,
        )
        assert refused.status_code == 400, refused.text[:300]
        assert "workflow.change.structure_in_config" in refused.text

        delete_resp = _apply_changes(
            base_url,
            workflow_id,
            [
                {"op": "delete_group", "group_id": group_id, "with_contents": True},
                _connect(_TEMP_STEP_IDS[-1], node(STOP_STEP_ID)),
            ],
            session,
            verify_ssl,
            expected_revision=updated_revision,
        )
        assert delete_resp.status_code == 200, delete_resp.text[:500]
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        assert group_id not in {group.get("group_id") for group in _workflow_groups(detail)}
        assert body_step_id not in _step_ids(detail)
        assert _execution_order(detail) == [*_TEMP_STEP_IDS, STOP_STEP_ID]

    def test_change_set_rewires_execution_order(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        first, second = _TEMP_STEP_IDS
        operations = [
            {"op": "disconnect", "source": node(first), "outcome": {"name": "success"}},
            {"op": "set_entry", "target": node(second)},
            _connect(second, node(first), replace=True),
            _connect(first, node(STOP_STEP_ID)),
        ]
        if active_test_role != "admin":
            denied = _apply_changes(
                base_url, workflow_id, operations, session, verify_ssl, expected_revision=1
            )
            assert denied.status_code == 403
            return
        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        revision_before = int(_get_detail(base_url, workflow_id, session, verify_ssl)["revision"])

        dry_run = _apply_changes(
            base_url,
            workflow_id,
            operations,
            session,
            verify_ssl,
            expected_revision=revision_before,
            dry_run=True,
        )
        assert dry_run.status_code == 200, dry_run.text[:500]
        assert dry_run.json()["applied"] is False
        assert int(dry_run.json()["revision"]) == revision_before
        unchanged = _get_detail(base_url, workflow_id, session, verify_ssl)
        assert _execution_order(unchanged) == [first, second, STOP_STEP_ID]

        reorder_resp = _apply_changes(
            base_url,
            workflow_id,
            operations,
            session,
            verify_ssl,
            expected_revision=revision_before,
        )
        assert reorder_resp.status_code == 200, (
            f"execution order change failed: {reorder_resp.status_code} {reorder_resp.text[:300]}"
        )
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        assert _execution_order(detail) == [second, first, STOP_STEP_ID]
        assert int(detail["revision"]) > revision_before

        # The same set against the revision it was made for is now stale.
        stale = _apply_changes(
            base_url,
            workflow_id,
            operations,
            session,
            verify_ssl,
            expected_revision=revision_before,
        )
        assert stale.status_code == 409, stale.text[:300]
        stale_detail = stale.json().get("detail") or {}
        assert stale_detail.get("code") == "workflow_revision_conflict"
        assert int(stale_detail.get("revision") or 0) == int(detail["revision"])

    def test_step_create_update_and_delete_persist_through_changes(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        first, second = _TEMP_STEP_IDS
        new_step_id = f"set_release_flag_{uuid.uuid4().hex[:6]}"
        insert_operation = {
            "op": "insert_step_between",
            "source": node(first),
            "outcome": {"name": "success"},
            "step": {
                "step_id": new_step_id,
                "step_kind": "set_variable",
                "display_name": "Set release flag",
                "override": {"set_variable": {"target": "state.release_flag", "value": "ready"}},
            },
        }
        if active_test_role != "admin":
            detail_resp = _workflow_request(
                "GET",
                f"{base_url}/api/workflows/{workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
            )
            assert detail_resp.status_code in (200, 403)
            denied = _apply_changes(
                base_url,
                workflow_id,
                [insert_operation],
                session,
                verify_ssl,
                expected_revision=1,
            )
            assert denied.status_code == 403
            return

        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        create_resp = _apply_changes(base_url, workflow_id, [insert_operation], session, verify_ssl)
        assert create_resp.status_code == 200, (
            f"step create failed: {create_resp.status_code} {create_resp.text[:300]}"
        )
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        created_step = next(
            (row for row in _workflow_steps(detail) if row.get("step_id") == new_step_id),
            None,
        )
        assert isinstance(created_step, dict)
        assert created_step.get("display_name") == "Set release flag"
        assert created_step.get("group_id") in (None, "")
        assert _execution_order(detail) == [first, new_step_id, second, STOP_STEP_ID]

        update_resp = _apply_changes(
            base_url,
            workflow_id,
            [
                {
                    "op": "update_step_config",
                    "step_id": new_step_id,
                    "patch": {
                        "display_name": "Set release flag updated",
                        "optional": True,
                        "override": {
                            "set_variable": {
                                "target": "state.release_flag",
                                "value": "updated",
                            }
                        },
                    },
                }
            ],
            session,
            verify_ssl,
        )
        assert update_resp.status_code == 200, (
            f"step update failed: {update_resp.status_code} {update_resp.text[:300]}"
        )
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        updated_step = next(row for row in _workflow_steps(detail) if row["step_id"] == new_step_id)
        assert updated_step.get("display_name") == "Set release flag updated"
        assert bool(updated_step.get("optional")) is True
        assert updated_step["override"]["set_variable"]["value"] == "updated"
        # A config change never moves a step: placement is structure.
        assert _execution_order(detail) == [first, new_step_id, second, STOP_STEP_ID]

        structural_patch = _apply_changes(
            base_url,
            workflow_id,
            [
                {
                    "op": "update_step_config",
                    "step_id": new_step_id,
                    "patch": {"step_kind": "http_request"},
                }
            ],
            session,
            verify_ssl,
        )
        assert structural_patch.status_code == 400, structural_patch.text[:300]
        assert "workflow.change.structure_in_config" in structural_patch.text

        delete_resp = _apply_changes(
            base_url,
            workflow_id,
            [
                {"op": "delete_step", "step_id": new_step_id},
                _connect(first, node(second)),
            ],
            session,
            verify_ssl,
        )
        assert delete_resp.status_code == 200, (
            f"step delete failed: {delete_resp.status_code} {delete_resp.text[:300]}"
        )
        detail = _get_detail(base_url, workflow_id, session, verify_ssl)
        assert new_step_id not in _step_ids(detail)
        assert _execution_order(detail) == [first, second, STOP_STEP_ID]

    def test_workflow_export_import_round_trip_is_stable(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        target_workflow_id = f"test_ui_imported_{uuid.uuid4().hex[:10]}"
        if active_test_role != "admin":
            export_resp = _workflow_request(
                "GET",
                f"{base_url}/api/workflows/{workflow_id}/export.yaml",
                requester=session.request if session else None,
                verify=verify_ssl,
            )
            assert export_resp.status_code == 403
            return

        assert temp_valid_workflow, "Admin lane must provision a temporary workflow"
        export_resp = _workflow_request(
            "GET",
            f"{base_url}/api/workflows/{workflow_id}/export.yaml",
            requester=session.request if session else None,
            verify=verify_ssl,
        )
        assert export_resp.status_code == 200, (
            f"workflow export failed: {export_resp.status_code} {export_resp.text[:300]}"
        )
        bundle_yaml = export_resp.text
        assert "platform.workflow_bundle.v2" in bundle_yaml
        assert "execution_graph" in bundle_yaml

        preflight_resp = _workflow_request(
            "POST",
            f"{base_url}/api/workflows/import/preflight",
            requester=session.request if session else None,
            verify=verify_ssl,
            headers=_csrf_headers(session),
            json={"bundle_yaml": bundle_yaml, "target_workflow_id": target_workflow_id},
        )
        assert preflight_resp.status_code == 200, (
            f"workflow import preflight failed: {preflight_resp.status_code} {preflight_resp.text[:300]}"
        )
        preflight = preflight_resp.json()
        assert preflight["can_apply"] is True

        try:
            apply_resp = _workflow_request(
                "POST",
                f"{base_url}/api/workflows/import/apply",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
                json={
                    "bundle_yaml": bundle_yaml,
                    "target_workflow_id": target_workflow_id,
                    "resolutions": [],
                },
            )
            assert apply_resp.status_code == 200, (
                f"workflow import apply failed: {apply_resp.status_code} {apply_resp.text[:300]}"
            )
            apply_body = apply_resp.json()
            assert apply_body["status"] == "imported"
            assert apply_body["workflow_id"] == target_workflow_id

            imported = _get_detail(base_url, target_workflow_id, session, verify_ssl)
            source = _get_detail(base_url, workflow_id, session, verify_ssl)
            assert imported["workflow_id"] == target_workflow_id
            assert sorted(_step_ids(imported)) == sorted(_step_ids(source))
            assert _execution_order(imported) == _execution_order(source)
            assert _edge_targets(imported) == _edge_targets(source)
        finally:
            _workflow_request(
                "DELETE",
                f"{base_url}/api/workflows/{target_workflow_id}",
                requester=session.request if session else None,
                verify=verify_ssl,
                headers=_csrf_headers(session),
            )

    def test_phase_routes_are_retired(
        self,
        base_url,
        registry_mode_enabled,
        ui_auth_session,
        temp_valid_workflow,
        active_test_role,
    ):
        """Negative: the admin phase and phase-step routes no longer exist."""
        session, verify_ssl = ui_auth_session
        workflow_id = temp_valid_workflow or NEUTRAL_WORKFLOW_ID
        retired = (
            ("POST", f"/api/workflows/{workflow_id}/phases", {"display_name": "Delivery"}),
            ("PUT", f"/api/workflows/{workflow_id}/phases/reorder", {"ordered_phase_ids": []}),
            ("PATCH", f"/api/workflows/{workflow_id}/phases/analysis", {"display_name": "X"}),
            ("PUT", f"/api/workflows/{workflow_id}/phases/analysis", {"display_name": "X"}),
            ("DELETE", f"/api/workflows/{workflow_id}/phases/analysis", None),
            (
                "POST",
                f"/api/workflows/{workflow_id}/phases/analysis/steps",
                {"step_kind": "set_variable", "step_id": "x"},
            ),
        )
        allowed = {404, 405} if active_test_role == "admin" else {403, 404, 405}
        for method, path, body in retired:
            kwargs: dict[str, Any] = {
                "requester": session.request if session else None,
                "verify": verify_ssl,
                "headers": _csrf_headers(session),
            }
            if body is not None:
                kwargs["json"] = body
            resp = _workflow_request(method, f"{base_url}{path}", **kwargs)
            assert resp.status_code in allowed, (method, path, resp.status_code, resp.text[:200])
        if active_test_role == "admin":
            detail = _get_detail(base_url, workflow_id, session, verify_ssl)
            assert sorted(_step_ids(detail)) == sorted([*_TEMP_STEP_IDS, STOP_STEP_ID])
