"""Shared E2E test utilities.

Centralises HTTP wrappers, env-var helpers, polling, and cleanup
so individual test modules stay thin and consistent.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from itertools import pairwise
from typing import Any

import pytest
import requests

from core.application.tooling.testing.runtime_env import (
    resolve_base_url,
    resolve_test_env,
    resolve_ws_url,
)
from tests.e2e.conftest import e2e_request
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID, NEUTRAL_WORKFLOW_IDS

# ── Environment ──────────────────────────────────────────────────────────────

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
WS_BASE_URL = resolve_ws_url(os.environ, include_dotenv=True)
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()
_BACKEND_ENV = resolve_test_env(os.environ, lane="backend", role=BACKEND_ROLE, include_dotenv=True)
PROJECT_ID = str(_BACKEND_ENV.project_id or "").strip()
#: Workflows every E2E project is linked to. The built-in catalog is empty in
#: 1.0, so these are the neutral fixture workflows from ``neutral_registry``.
CANONICAL_WORKFLOW_IDS = NEUTRAL_WORKFLOW_IDS
CANONICAL_E2E_PROJECT_NAME_PREFIXES = ("Test Project Backend", "Test Project UI")


def env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def unique_id(prefix: str = "e2e") -> str:
    """Return a unique, lowercase, regex-safe identifier like ``e2e_abc12345``."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _e2e_test_runtime_provider_config() -> dict[str, str]:
    # API credentials and model selection are account-level Assistant Settings,
    # never per-project environment configuration. E2E projects must use a
    # prepared account with a verified saved provider.
    return {}


def _ensure_e2e_project_runtime_provider(project_id: str | int) -> None:
    provider_config = _e2e_test_runtime_provider_config()
    if not provider_config:
        return

    try:
        project_resp = e2e_request(
            "GET",
            f"{BASE_URL}/api/projects/{project_id!s}",
            verify=VERIFY_SSL,
            timeout=20,
        )
        if project_resp.status_code != 200:
            return
        project_row = project_resp.json() or {}
        project_name = str(project_row.get("name") or "").strip()
        if not project_name.startswith(CANONICAL_E2E_PROJECT_NAME_PREFIXES):
            return

        agents_config = (
            dict(project_row.get("agents_config"))
            if isinstance(project_row.get("agents_config"), dict)
            else {}
        )
        changed = False
        for key, value in provider_config.items():
            if str(agents_config.get(key) or "").strip() != value:
                agents_config[key] = value
                changed = True
        if changed:
            payload = {
                "name": project_name,
                "workspace_id": str(project_row.get("workspace_id") or "").strip(),
                "default_provider": str(agents_config.get("default_provider") or "").strip(),
                "default_model": str(agents_config.get("default_model") or "").strip(),
            }
            if not payload["workspace_id"]:
                return
            update_resp = e2e_request(
                "PUT",
                f"{BASE_URL}/api/projects/{project_id!s}",
                json=payload,
                verify=VERIFY_SSL,
                timeout=30,
            )
            if update_resp.status_code != 200:
                return
    except Exception:
        return


# ── Anonymous cookies placeholder (empty — no auth) ──────────────────────────
# Used by SSE and RBAC tests that need a token-free session.
# DO NOT add auth credentials here.
COOKIES: dict[str, str] = {}


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def _anon_session() -> requests.Session:
    """Return a fresh, unauthenticated Session that bypasses the e2e auth monkeypatch.

    The conftest e2e_authenticated_requests fixture patches ``requests.get``,
    ``requests.post``, etc. at the module level.  A ``Session`` object's
    instance method (``session.get``) is NOT affected by that patch, so calls
    made through this session are genuinely unauthenticated.
    """
    s = requests.Session()
    s.verify = VERIFY_SSL
    return s


def anon_get(path: str, **kwargs: Any) -> requests.Response:
    """Unauthenticated GET — bypasses the e2e auth monkeypatch."""
    kwargs.setdefault("timeout", 10)
    kwargs.setdefault("allow_redirects", False)
    return _anon_session().get(f"{BASE_URL}{path}", **kwargs)


def anon_post(path: str, payload: dict | None = None, **kwargs: Any) -> requests.Response:
    """Unauthenticated POST — bypasses the e2e auth monkeypatch."""
    kwargs.setdefault("timeout", 10)
    kwargs.setdefault("allow_redirects", False)
    return _anon_session().post(f"{BASE_URL}{path}", json=payload, **kwargs)


def get(path: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 20)
    return e2e_request("GET", f"{BASE_URL}{path}", **kwargs)


def post_json(path: str, payload: dict | None = None, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 30)
    return e2e_request("POST", f"{BASE_URL}{path}", json=payload, **kwargs)


def put_json(path: str, payload: dict | list | None = None, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", int(os.getenv("E2E_PUT_TIMEOUT", "30")))
    attempts = max(1, int(os.getenv("E2E_PUT_RETRY_ATTEMPTS", "1")))
    for attempt in range(attempts):
        try:
            return e2e_request("PUT", f"{BASE_URL}{path}", json=payload, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt >= attempts - 1:
                raise
            import time as _time

            _time.sleep(min(1.0 * (attempt + 1), 3.0))
    raise RuntimeError("unreachable: put_json retry loop exhausted")


def patch_json(path: str, payload: dict | None = None, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 30)
    return e2e_request("PATCH", f"{BASE_URL}{path}", json=payload, **kwargs)


def delete(path: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 20)
    return e2e_request("DELETE", f"{BASE_URL}{path}", **kwargs)


def require_ok(
    resp: requests.Response,
    context: str,
    *,
    allow_403: bool = False,
    allow_404: bool = False,
) -> bool:
    """Assert a response is actionable for an e2e contract test.

    - 200 → return True (proceed)
    - 401 → pytest.skip (no auth configured in this environment)
    - 403 → pytest.skip if allow_403=True (admin-only endpoint), else pytest.fail
    - 404 → pytest.skip if allow_404=True, else pytest.fail
    - 5xx → always pytest.fail (server crash is a test failure, not a skip)
    - other 4xx → pytest.fail (unexpected error)
    """
    code = resp.status_code
    if code == 200:
        return True
    if code == 401:
        pytest.skip(f"{context}: not authenticated (401) — configure E2E credentials to run")
    if code == 403:
        if allow_403:
            pytest.skip(f"{context}: 403 Forbidden — skipped (admin-only endpoint)")
        pytest.fail(
            f"{context}: unexpected 403 Forbidden for an endpoint accessible to authenticated users"
        )
    if code == 404 and allow_404:
        pytest.skip(f"{context}: 404 Not Found — resource absent in this environment")
    if code >= 500:
        pytest.fail(f"{context}: server error {code} — must never be 5xx")
    pytest.fail(f"{context}: unexpected {code} — expected 200")
    return False  # unreachable


def step_edge(source: str, target: str, outcome: str = "success") -> dict[str, Any]:
    """One execution-graph edge from step *source* to step *target*."""
    return {
        "source": {"kind": "step", "id": source},
        "outcome": {"name": outcome},
        "target": {"kind": "step", "id": target},
        "rework": False,
    }


#: Workflow settings that declare one callable ``payload`` object input. A
#: workflow needs one real execution step, and a transform reads only a
#: declared input: ``state.inputs`` itself is not a transform source.
PAYLOAD_INPUT_WORKFLOW_SETTINGS: dict[str, Any] = {
    "callable": {
        "enabled": True,
        "trigger_channels": ["ui", "api", "mcp"],
        "input_mode": "fields",
        "strict_inputs": True,
        "expected_inputs": [{"name": "payload", "type": "object", "required": True}],
        "output_mode": "none",
        "expected_outputs": [],
    }
}


def payload_transform_step(step_id: str, display_name: str) -> dict[str, Any]:
    """A transform that copies ``payload.marker`` into state; no model provider runs."""
    return {
        "step_id": step_id,
        "step_kind": "transform",
        "display_name": display_name,
        "override": {
            "transform": {
                "input": {"source": "state.inputs.payload"},
                "result_target": f"state.{step_id}",
                "fields": [{"from": "marker", "to": "marker", "type": "text"}],
            }
        },
    }


def linear_step_operations(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    """Change operations that create *steps* and run them in the given order."""
    step_ids = [str(step["step_id"]) for step in steps]
    operations: list[dict[str, Any]] = [{"op": "create_step", "step": dict(step)} for step in steps]
    operations.append({"op": "set_entry", "target": {"kind": "step", "id": step_ids[0]}})
    operations.extend(
        {"op": "connect", "edge": step_edge(source, target)}
        for source, target in pairwise(step_ids)
    )
    return operations


def apply_workflow_changes(workflow_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply one change set at the workflow's current revision, the way the editor does.

    Steps, groups and the execution graph are authored only through
    ``/api/workflows/{id}/changes``; a workflow payload that carries them as
    phases is refused.
    """
    current = get(f"/api/workflows/{workflow_id}")
    assert current.status_code == 200, (
        f"reading workflow {workflow_id} failed: {current.status_code} {current.text[:300]}"
    )
    resp = post_json(
        f"/api/workflows/{workflow_id}/changes",
        {"expected_revision": current.json().get("revision"), "operations": operations},
    )
    assert resp.status_code == 200, (
        f"change set for {workflow_id} failed: {resp.status_code} {resp.text[:300]}"
    )
    return resp.json()


def purge_trashed_project(project_id: str | int) -> requests.Response:
    """Hard-delete a trashed project the way the product does: plan, then execute it.

    The direct project hard-delete endpoint refuses every project with 409; a
    purge names the plan revision it was shown. Returns the execute response.
    """
    plan = post_json(f"/api/trash/project/{project_id}/plan", {"action_type": "hard_delete"})
    assert plan.status_code == 200, (
        f"project purge plan failed: {plan.status_code} {plan.text[:300]}"
    )
    return post_json(
        f"/api/trash/project/{project_id}/execute",
        {"action_type": "hard_delete", "plan_version": plan.json().get("plan_version")},
    )


def post_form(path: str, data: dict | None = None, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("allow_redirects", False)
    return e2e_request("POST", f"{BASE_URL}{path}", data=data, **kwargs)


def ensure_canonical_project_workflows(
    project_id: str | int,
    *,
    workflow_ids: tuple[str, ...] = CANONICAL_WORKFLOW_IDS,
    operator_default_workflow_id: str = NEUTRAL_WORKFLOW_ID,
) -> dict:
    """Ensure the shared E2E project is linked to the neutral fixture workflows."""

    def _skip_if_unresolved_contracts(workflow_ids_to_check: tuple[str, ...]) -> None:
        if not env_flag("E2E_ENFORCE_WORKFLOW_READINESS", True):
            return
        unresolved: dict[str, list[str]] = {}
        for workflow_id in workflow_ids_to_check:
            try:
                validation_resp = e2e_request(
                    "GET",
                    f"{BASE_URL}/api/workflows/{workflow_id}/contract-validation",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
            ) as exc:
                pytest.skip(
                    "Neutral project workflow readiness check timed out; "
                    f"skipping orchestration-dependent E2E checks ({workflow_id}: {exc})"
                )
            if validation_resp.status_code != 200:
                continue
            payload = validation_resp.json() or {}
            validation = payload.get("validation") if isinstance(payload, dict) else {}
            errors = (validation or {}).get("errors") if isinstance(validation, dict) else []
            if not isinstance(errors, list):
                continue
            messages = [
                str((issue or {}).get("message") or "").strip()
                for issue in errors
                if isinstance(issue, dict) and str((issue or {}).get("message") or "").strip()
            ]
            if messages:
                unresolved[workflow_id] = messages
        if unresolved:
            preview = "; ".join(
                f"{workflow_id}: {messages[0]}"
                for workflow_id, messages in sorted(unresolved.items())
            )
            pytest.skip(
                "Neutral project workflows are linked but not contract-ready; "
                f"skipping orchestration-dependent E2E checks ({preview[:500]})"
            )

    project_id_str = str(project_id or "").strip()
    if not project_id_str:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")

    _ensure_e2e_project_runtime_provider(project_id_str)

    current = e2e_request(
        "GET",
        f"{BASE_URL}/api/projects/{project_id_str}/workflows",
        verify=VERIFY_SSL,
        timeout=20,
    )
    if current.status_code == 404:
        pytest.skip("Project workflows endpoint not implemented")
    if current.status_code == 503:
        pytest.skip("Database not configured")
    if current.status_code == 403 and BACKEND_ROLE != "admin":
        return {}
    assert current.status_code == 200, (
        f"Could not read project workflows: {current.status_code} {current.text[:300]}"
    )

    body = current.json() or {}
    rows = body.get("project_workflows") or []
    enabled_ids = {
        str(row.get("workflow_id") or "").strip()
        for row in rows
        if isinstance(row, dict)
        and bool(row.get("enabled"))
        and not row.get("deleted_at")
        and str(row.get("workflow_id") or "").strip()
    }
    current_default = str(body.get("operator_default_workflow_id") or "").strip()
    required_ids = tuple(
        dict.fromkeys(
            str(workflow_id or "").strip()
            for workflow_id in workflow_ids
            if str(workflow_id or "").strip()
        )
    )
    desired_default = (
        operator_default_workflow_id
        if operator_default_workflow_id in required_ids
        else required_ids[0]
    )
    if enabled_ids.issuperset(required_ids) and current_default == desired_default:
        _skip_if_unresolved_contracts(required_ids)
        return body

    update = e2e_request(
        "PUT",
        f"{BASE_URL}/api/projects/{project_id_str}/workflows",
        json={
            "workflow_ids": list(required_ids),
            "operator_default_workflow_id": desired_default,
        },
        verify=VERIFY_SSL,
        timeout=30,
    )
    if update.status_code == 404:
        pytest.skip("Project workflows update endpoint not implemented")
    if update.status_code == 503:
        pytest.skip("Database not configured")
    if update.status_code == 403 and BACKEND_ROLE != "admin":
        pytest.skip("Project workflows update requires admin role")
    assert update.status_code == 200, (
        f"Could not update project workflows: {update.status_code} {update.text[:300]}"
    )
    linked = update.json() or {}

    _skip_if_unresolved_contracts(required_ids)

    return linked


# ── Polling ──────────────────────────────────────────────────────────────────


def poll_until(
    check: Callable[[], bool],
    *,
    timeout: float = 30,
    interval: float = 2,
    description: str = "condition",
) -> None:
    """Block until *check()* returns ``True`` or *timeout* seconds elapse."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(interval)
    raise TimeoutError(f"poll_until timed out waiting for {description} after {timeout}s")


# ── Cleanup tracker ──────────────────────────────────────────────────────────


class E2ECleanup:
    """Collects (method, path) tuples and calls them in reverse on cleanup().

    Usage::

        cleanup = E2ECleanup()
        cleanup.add("DELETE", "/api/agents/e2e_test_123")
        # ... test body ...
        cleanup.run()          # called in finally / fixture teardown
    """

    def __init__(self) -> None:
        self._actions: list[tuple[str, str, dict | None]] = []

    def add(self, method: str, path: str, json_body: dict | None = None) -> None:
        self._actions.append((method.upper(), path, json_body))

    def run(self) -> None:
        for method, path, body in reversed(self._actions):
            try:
                if method == "DELETE":
                    delete(path)
                elif method == "POST":
                    post_json(path, body)
                elif method == "PUT":
                    put_json(path, body)
                elif method == "PATCH":
                    patch_json(path, body)
            except Exception:
                pass
        self._actions.clear()

    def __enter__(self) -> E2ECleanup:
        return self

    def __exit__(self, *exc: object) -> None:
        self.run()


# ── Job seed helpers ─────────────────────────────────────────────────────────

# Cache of seeded test jobs keyed by project_id.  Populated lazily by
# ensure_test_job() so we only create one job per E2E test session even
# when multiple modules call the helper.
_seeded_jobs: dict[str, dict] = {}


def ensure_test_job(project_id: str | int | None = None) -> dict:
    """Return an existing job or start the neutral workflow via ``POST /api/workflow-runs``.

    Uses the authenticated ``e2e_request`` helper so that the call works
    regardless of auth settings.  The job is created with ``test_mode=True``
    so the worker short-circuits instead of running the full graph.

    Returns a dict with at least ``{"id": <uuid>, ...}`` — the raw job row
    as returned by the jobs-list API.

    Raises ``pytest.skip`` only when the server or DB is genuinely
    unavailable (never just because "no jobs exist").
    """
    pid = str(project_id or PROJECT_ID or "").strip()
    if not pid:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set — cannot seed test job")

    # Fast path: already seeded in this session.
    if pid in _seeded_jobs:
        cached = _seeded_jobs[pid]
        cached_id = str(cached.get("id") or cached.get("job_id") or "").strip()
        if cached_id:
            cached_resp = e2e_request(
                "GET",
                f"{BASE_URL}/api/jobs/{cached_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            if cached_resp.status_code == 200:
                cached_data = cached_resp.json()
                refreshed = (
                    cached_data.get("job", cached_data)
                    if isinstance(cached_data, dict)
                    else cached_data
                )
                _seeded_jobs[pid] = refreshed
                return refreshed
            if cached_resp.status_code != 404:
                return cached
        _seeded_jobs.pop(pid, None)

    # Check if there's already a job we can reuse.
    existing = _find_existing_job(pid)
    if existing:
        _seeded_jobs[pid] = existing
        return existing

    ensure_canonical_project_workflows(pid, workflow_ids=(NEUTRAL_WORKFLOW_ID,))

    # Start the fixture workflow; correlation identity is owned by the server.
    resp = e2e_request(
        "POST",
        f"{BASE_URL}/api/workflow-runs",
        json={
            "project_id": pid,
            "workflow_id": NEUTRAL_WORKFLOW_ID,
            "test_mode": True,
        },
        verify=VERIFY_SSL,
        timeout=30,
    )
    if resp.status_code == 503:
        pytest.skip("Database not configured — cannot seed test job")
    assert resp.status_code == 200, (
        f"Failed to create seed job: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json()
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Seed job response missing job_id: {body}"

    # Fetch the full job row so callers get the same shape as _find_existing_job.
    job_resp = e2e_request(
        "GET",
        f"{BASE_URL}/api/jobs/{job_id}",
        verify=VERIFY_SSL,
        timeout=20,
    )
    if job_resp.status_code == 200:
        job_data = job_resp.json()
        # Normalise — some endpoints wrap in {"job": {...}}
        job = job_data.get("job", job_data) if isinstance(job_data, dict) else job_data
    else:
        # Fallback: synthesise minimal dict from the creation response.
        job = {"id": job_id, "job_id": job_id, "status": "pending"}

    _seeded_jobs[pid] = job
    return job


def _find_existing_job(project_id: str | int) -> dict | None:
    """Return the first job for *project_id*, or ``None``."""
    resp = e2e_request(
        "GET",
        f"{BASE_URL}/api/projects/{project_id}/jobs",
        params={"limit": 1},
        verify=VERIFY_SSL,
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    jobs = data if isinstance(data, list) else data.get("jobs") or []
    return jobs[0] if jobs else None


# ── Job helpers ──────────────────────────────────────────────────────────────


def cancel_job(job_id: str | int) -> None:
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return
    with contextlib.suppress(Exception):
        requests.post(
            f"{BASE_URL}/api/jobs/{normalized_job_id}/cancel",
            headers={"accept": "application/json"},
            verify=VERIFY_SSL,
            timeout=20,
            allow_redirects=False,
        )
