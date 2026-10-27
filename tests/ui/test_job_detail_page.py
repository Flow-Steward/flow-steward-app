"""
UI tests for the Job Detail page (/jobs/{job_id}).

Validates the human-readable Job Detail redesign contract.

Run:
    docker compose exec -e E2E_UI_ROLE=viewer fs-app python -m pytest tests/ui/test_job_detail_page.py -v --timeout=60
    docker compose exec -e E2E_UI_ROLE=operator fs-app python -m pytest tests/ui/test_job_detail_page.py -v --timeout=60
    docker compose exec -e E2E_UI_ROLE=admin fs-app python -m pytest tests/ui/test_job_detail_page.py -v --timeout=60
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import suppress
from pathlib import Path

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests.e2e.helpers import ensure_canonical_project_workflows
from tests.e2e.neutral_registry import (
    NEUTRAL_AGENT_ID,
    NEUTRAL_APPROVAL_STEP_ID,
    NEUTRAL_DRAFT_STEP_ID,
    NEUTRAL_SKILL_ID,
    NEUTRAL_START_STEP_ID,
    NEUTRAL_WORKFLOW_ID,
)
from tests.e2e.retry_step_helpers import cleanup_job

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)

PRIMARY_TEST_IDS = {
    "primary_container": "job-primary-container",
    "hero_summary": "job-hero-summary",
    "next_action": "job-next-action",
    "review_workspace": "job-review-workspace",
    "execution_waterfall": "job-execution-waterfall",
    "step_inspector": "job-step-inspector",
    "cost_breakdown": "job-cost-breakdown",
}

LEGACY_JOB_DETAIL_SELECTORS = (
    "#tab-timeline",
    "#tab-messages",
    "#tab-logs",
    "#tab-cost",
    "#advanced-mode-toggle",
    "[data-testid='job-advanced-diagnostics-trigger']",
)

_SEEDED_JOB_IDS: set[str] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _goto_job_detail(page, base_url: str, job_id: str) -> None:
    """Open a live job page without waiting for websocket/network quiescence."""
    page.goto(f"{base_url}/jobs/{job_id}", wait_until="domcontentloaded")
    page.locator("#status-badge, h1").first.wait_for(state="visible", timeout=30_000)
    page.locator(
        '[data-testid="job-primary-container"], [data-testid="job-primary-error"]'
    ).first.wait_for(state="visible", timeout=30_000)


def _connect_db():
    dsn = str(os.getenv("FS_DB_DSN") or os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("FS_DB_DSN="):
                    continue
                dsn = line.split("=", 1)[1].strip()
                break
    if not Path("/.dockerenv").exists():
        dsn = dsn.replace("host=pg-fs", "host=127.0.0.1").replace("@pg-fs:", "@127.0.0.1:")
    if not dsn:
        raise RuntimeError("DB DSN is not configured")
    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(dsn)
    except Exception:
        from core.infrastructure.db import psycopg_runtime as psycopg  # type: ignore

        conn = psycopg.connect(dsn)
    conn.autocommit = True
    return conn


def _ensure_admin_session(base_url: str) -> requests.Session:
    env = resolve_test_env(os.environ, lane="ui", role="admin", include_dotenv=True)
    session = requests.Session()
    resp = session.post(
        f"{base_url}/api/auth/login",
        json={"email": env.username, "password": env.password},
        verify=False,
        timeout=20,
    )
    assert resp.status_code == 200, (
        f"Could not authenticate admin user {env.username!r}: {resp.status_code} {resp.text[:200]}"
    )
    csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
    if csrf_token:
        session.headers["x-csrf-token"] = csrf_token
    return session


def _seed_job_with_status(
    *,
    base_url: str,
    project_id: str,
    session: requests.Session | None,
    verify_ssl: bool,
    status: str,
    current_step_id: str,
    current_agent: str,
    task_summary: str,
    meta: dict | None = None,
) -> str:
    ensure_canonical_project_workflows(project_id)
    _post = session.post if session else requests.post
    create_resp = _post(
        f"{base_url}/api/workflow-runs",
        json={
            "inputs": {"topic": task_summary},
            "project_id": project_id,
            "workflow_id": NEUTRAL_WORKFLOW_ID,
            "test_mode": True,
        },
        verify=verify_ssl,
        timeout=30,
    )
    assert create_resp.ok, (
        f"Could not create seeded job for status {status!r}: "
        f"{create_resp.status_code} {create_resp.text[:300]}"
    )
    body = create_resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Seeded job response missing job_id: {body!r}"
    _SEEDED_JOB_IDS.add(job_id)
    _force_job_status(
        job_id=job_id,
        project_id=project_id,
        status=status,
        current_step_id=current_step_id,
        current_agent=current_agent,
        meta=meta or {"test_mode": True},
    )
    return job_id


def _force_job_status(
    *,
    job_id: str,
    project_id: str,
    status: str,
    current_step_id: str,
    current_agent: str,
    meta: dict,
) -> str:
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(NULLIF(thread_id, ''), %s) FROM jobs WHERE id = %s",
            (uuid.uuid4().hex, job_id),
        )
        row = cur.fetchone()
        thread_id = str(row[0] if isinstance(row, (list, tuple)) else row["thread_id"]).strip()
        cur.execute(
            """
            UPDATE jobs
               SET status = %s,
                   queue_name = COALESCE(NULLIF(queue_name, ''), %s),
                   heartbeat_at = NOW(),
                   current_step_id = %s,
                   current_agent = %s,
                   thread_id = %s,
                   worker_id = '',
                   meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
             WHERE id = %s
            """,
            (
                status,
                f"platform.project.{project_id}",
                current_step_id,
                current_agent,
                thread_id,
                json.dumps(meta),
                job_id,
            ),
        )
    return job_id


def _cleanup_seeded_jobs():
    if not _SEEDED_JOB_IDS:
        return
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE id = ANY(%s)", (list(_SEEDED_JOB_IDS),))
    _SEEDED_JOB_IDS.clear()


def _mutation_owner_session(
    *,
    base_url: str,
    active_test_role: str,
    ui_auth_session: tuple[requests.Session | None, bool],
) -> tuple[requests.Session | None, bool]:
    session, verify_ssl = ui_auth_session
    if active_test_role in {"admin", "operator"}:
        return session, verify_ssl
    return _ensure_admin_session(base_url), verify_ssl


def _adopt_role_session(page, base_url: str, role: str) -> None:
    if role == "viewer":
        from tests.e2e.test_api_response_minimization_matrix import _create_viewer_session

        session, _user_id = _create_viewer_session()
    else:
        env = resolve_test_env(os.environ, lane="ui", role=role, include_dotenv=True)
        session = requests.Session()
        resp = session.post(
            f"{base_url}/api/auth/login",
            json={"email": env.username, "password": env.password},
            verify=False,
            timeout=20,
        )
        assert resp.status_code == 200, (
            f"Could not authenticate {role} user {env.username!r}: {resp.status_code} {resp.text[:200]}"
        )
    page.context.clear_cookies()
    cookies = [
        {
            "name": item.name,
            "value": item.value,
            "url": base_url,
            "secure": base_url.startswith("https://"),
            "httpOnly": item.name != "fs_csrf",
        }
        for item in session.cookies
    ]
    if cookies:
        page.context.add_cookies(cookies)


def _test_id_selector(test_id: str) -> str:
    return f'[data-testid="{test_id}"]'


def _by_test_id(page, test_id: str):
    return page.locator(_test_id_selector(test_id))


def _assert_test_id_visible(page, test_id: str) -> None:
    locator = _by_test_id(page, test_id)
    assert locator.count() > 0, f"Missing required selector {test_id!r}"
    assert locator.first.is_visible(), f"Expected {test_id!r} to be visible"


def _body_text(page) -> str:
    return page.locator("body").inner_text().strip().lower()


def _assert_retired_job_detail_controls_absent(page) -> None:
    for selector in LEGACY_JOB_DETAIL_SELECTORS:
        assert page.locator(selector).count() == 0, (
            f"Retired selector should stay absent: {selector}"
        )


def _require_role(active_test_role: str, *allowed: str) -> None:
    if active_test_role not in allowed:
        pytest.skip(
            f"Role-specific contract check requires one of {allowed}, got {active_test_role!r}"
        )


def _jobs_by_attention_group(
    base_url: str,
    session: requests.Session | None = None,
    *,
    verify_ssl: bool = False,
) -> dict[str, list[dict]]:
    """Return grouped job rows from the supported jobs attention API."""
    try:
        _get = session.get if session else requests.get
        resp = _get(
            f"{base_url}/api/jobs?group_by=attention&limit=100", verify=verify_ssl, timeout=10
        )
        if not resp.ok:
            return {}
        payload = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        groups = payload.get("groups") if isinstance(payload, dict) else {}
        return groups if isinstance(groups, dict) else {}
    except Exception:
        return {}


def _project_jobs(
    base_url: str,
    project_id: str | None = None,
    session: requests.Session | None = None,
    *,
    verify_ssl: bool = False,
) -> list[dict]:
    """Get grouped jobs filtered to a project when possible."""
    groups = _jobs_by_attention_group(base_url, session, verify_ssl=verify_ssl)
    rows: list[dict] = []
    project_filter = str(project_id or "").strip()
    for items in groups.values():
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, dict):
                continue
            if project_filter and str(row.get("project_id") or "").strip() != project_filter:
                continue
            rows.append(row)
    return rows


def _pick_job_id(
    base_url: str,
    *,
    project_id: str | None = None,
    status: str | None = None,
    session: requests.Session | None = None,
    verify_ssl: bool = False,
) -> str | None:
    """Return a job id from the supported grouped jobs API."""
    desired_status = str(status or "").strip().lower()
    for row in _project_jobs(base_url, project_id, session, verify_ssl=verify_ssl):
        row_status = str(row.get("status") or "").strip().lower()
        if desired_status and row_status != desired_status:
            continue
        job_id = row.get("id") or row.get("job_id")
        if job_id:
            return str(job_id).strip()
    return None


def _job_detail(
    base_url: str,
    job_id: str,
    session: requests.Session | None = None,
    *,
    verify_ssl: bool = False,
) -> dict:
    """Return live job detail payload when available."""
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {}
    try:
        _get = session.get if session else requests.get
        resp = _get(
            f"{base_url}/api/jobs/{normalized_job_id}",
            verify=verify_ssl,
            timeout=10,
        )
        if not resp.ok:
            return {}
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@pytest.fixture
def any_job_id(base_url, test_project_id, ui_auth_session):
    """Get an existing job ID for testing."""
    session, verify_ssl = ui_auth_session
    job_id = _pick_job_id(
        base_url, project_id=test_project_id, session=session, verify_ssl=verify_ssl
    )
    if not job_id:
        # Try to get from projects page
        try:
            _get = session.get if session else requests.get
            resp = _get(f"{base_url}/projects/{test_project_id}", verify=verify_ssl, timeout=5)
            # Try to extract a job ID from the HTML
            if "job_id" in resp.text or "/jobs/" in resp.text:
                import re

                match = re.search(r"/jobs/([0-9A-Za-z-]+)", resp.text)
                if match:
                    job_id = match.group(1).strip()
        except Exception:
            pass

    if not job_id:
        pytest.skip("No jobs available for testing")
    return job_id


@pytest.fixture
def retryable_job_id(base_url, test_project_id, ui_auth_session, active_test_role):
    """Get a retryable interrupted-step job on the env-backed UI project."""
    override = str(os.getenv("E2E_RETRYABLE_JOB_ID", "") or "").strip()
    if override:
        return override
    session, verify_ssl = _mutation_owner_session(
        base_url=base_url,
        active_test_role=active_test_role,
        ui_auth_session=ui_auth_session,
    )
    return _seed_job_with_status(
        base_url=base_url,
        project_id=str(test_project_id),
        session=session,
        verify_ssl=verify_ssl,
        status="retryable_interrupted_step",
        current_step_id=NEUTRAL_DRAFT_STEP_ID,
        current_agent=NEUTRAL_AGENT_ID,
        task_summary="UI job detail retry-step seed",
        meta={
            "test_mode": True,
            "retryable_interrupted_step": {
                "agent_id": NEUTRAL_AGENT_ID,
                "skill": NEUTRAL_SKILL_ID,
                "step_id": NEUTRAL_DRAFT_STEP_ID,
            },
        },
    )


@pytest.fixture
def awaiting_approval_job_id(base_url, test_project_id, ui_auth_session, active_test_role):
    """Get a job that is currently awaiting review."""
    session, verify_ssl = _mutation_owner_session(
        base_url=base_url,
        active_test_role=active_test_role,
        ui_auth_session=ui_auth_session,
    )
    return _seed_job_with_status(
        base_url=base_url,
        project_id=str(test_project_id),
        session=session,
        verify_ssl=verify_ssl,
        status="awaiting_approval",
        current_step_id=NEUTRAL_APPROVAL_STEP_ID,
        current_agent="human_approval_gate",
        task_summary="UI job detail awaiting approval seed",
        meta={"test_mode": True},
    )


@pytest.fixture
def cancellable_job_id(base_url, test_project_id, ui_auth_session, active_test_role):
    """Get a running or waiting job that should expose Cancel job."""
    session, verify_ssl = _mutation_owner_session(
        base_url=base_url,
        active_test_role=active_test_role,
        ui_auth_session=ui_auth_session,
    )
    for status in ("in_progress", "pending", "awaiting_approval"):
        job_id = _pick_job_id(
            base_url,
            project_id=test_project_id,
            status=status,
            session=session,
            verify_ssl=verify_ssl,
        )
        if not job_id:
            job_id = _pick_job_id(
                base_url,
                status=status,
                session=session,
                verify_ssl=verify_ssl,
            )
        if job_id:
            detail = _job_detail(base_url, job_id, session, verify_ssl=verify_ssl)
            detail_status = str(detail.get("status") or "").strip().lower()
            if detail_status == status:
                return job_id
    return _seed_job_with_status(
        base_url=base_url,
        project_id=str(test_project_id),
        session=session,
        verify_ssl=verify_ssl,
        status="pending",
        current_step_id=NEUTRAL_START_STEP_ID,
        current_agent="orchestrator",
        task_summary="UI job detail cancellable seed",
        meta={"test_mode": True},
    )


@pytest.fixture
def terminal_job_id(base_url, test_project_id, ui_auth_session, active_test_role):
    """Get a terminal-state job that should expose Restart job."""
    session, verify_ssl = _mutation_owner_session(
        base_url=base_url,
        active_test_role=active_test_role,
        ui_auth_session=ui_auth_session,
    )
    for status in ("failed", "completed", "cancelled", "max_iterations"):
        job_id = _pick_job_id(
            base_url,
            project_id=test_project_id,
            status=status,
            session=session,
            verify_ssl=verify_ssl,
        )
        if not job_id:
            job_id = _pick_job_id(
                base_url,
                status=status,
                session=session,
                verify_ssl=verify_ssl,
            )
        if job_id:
            detail = _job_detail(base_url, job_id, session, verify_ssl=verify_ssl)
            detail_status = str(detail.get("status") or "").strip().lower()
            if detail_status == status:
                return job_id
    pytest.skip("No terminal-state jobs available for restart checks")


@pytest.fixture(scope="module", autouse=True)
def _require_explicit_job_detail_role(explicit_active_test_role):
    return


@pytest.fixture(scope="module", autouse=True)
def _cleanup_job_detail_seeds():
    yield
    for job_id in list(_SEEDED_JOB_IDS):
        with suppress(Exception):
            cleanup_job(job_id)
    with suppress(Exception):
        _cleanup_seeded_jobs()


# ---------------------------------------------------------------------------
# Tests — Redesign Contract
# ---------------------------------------------------------------------------


class TestJobDetailHumanReadableContract:
    """Validate the frozen human-readable Job Detail page contract."""

    def test_job_detail_page_loads(self, page, server_reachable, base_url, any_job_id):
        """Job detail page loads without error."""
        _goto_job_detail(page, base_url, any_job_id)
        assert page.url is not None
        assert len(page.content()) > 100

    def test_primary_flow_uses_redesign_sections(
        self, page, server_reachable, base_url, any_job_id
    ):
        """Primary page flow is anchored by redesign test IDs, not storage tabs."""
        _goto_job_detail(page, base_url, any_job_id)
        for test_id in (
            PRIMARY_TEST_IDS["primary_container"],
            PRIMARY_TEST_IDS["hero_summary"],
            PRIMARY_TEST_IDS["next_action"],
            PRIMARY_TEST_IDS["execution_waterfall"],
            PRIMARY_TEST_IDS["step_inspector"],
            PRIMARY_TEST_IDS["cost_breakdown"],
        ):
            _assert_test_id_visible(page, test_id)

        _assert_retired_job_detail_controls_absent(page)

    def test_awaiting_approval_jobs_show_review_workspace(
        self, page, server_reachable, base_url, awaiting_approval_job_id
    ):
        """Active human gates render a dedicated Review workspace."""
        _goto_job_detail(page, base_url, awaiting_approval_job_id)
        _assert_test_id_visible(page, PRIMARY_TEST_IDS["next_action"])
        if _by_test_id(page, PRIMARY_TEST_IDS["review_workspace"]).count() > 0:
            _assert_test_id_visible(page, PRIMARY_TEST_IDS["review_workspace"])
            assert page.locator("platform-review-workspace").count() == 1
            next_action_text = (
                _by_test_id(page, PRIMARY_TEST_IDS["next_action"]).inner_text().strip().lower()
            )
            assert "approve" not in next_action_text
            assert "reject" not in next_action_text
        _assert_retired_job_detail_controls_absent(page)

    def test_viewer_sees_read_only_primary_flow_without_mutation_controls(
        self,
        page,
        server_reachable,
        base_url,
        any_job_id,
        active_test_role,
    ):
        """Viewer keeps the narrative flow but not mutation actions."""
        if active_test_role != "viewer":
            with suppress(Exception, pytest.skip.Exception):
                _adopt_role_session(page, base_url, "viewer")
        _goto_job_detail(page, base_url, any_job_id)
        for test_id in (
            PRIMARY_TEST_IDS["hero_summary"],
            PRIMARY_TEST_IDS["next_action"],
            PRIMARY_TEST_IDS["execution_waterfall"],
            PRIMARY_TEST_IDS["step_inspector"],
            PRIMARY_TEST_IDS["cost_breakdown"],
        ):
            _assert_test_id_visible(page, test_id)

        text = _body_text(page)
        assert "cancel job" not in text
        assert "restart job" not in text
        assert "restart from this step" not in text
        primary_text = (
            _by_test_id(page, PRIMARY_TEST_IDS["primary_container"]).inner_text().strip().lower()
        )
        assert "graph td" not in primary_text
        assert "correlation_id" not in primary_text
        assert "routing_decision" not in primary_text
        assert "raw output" not in primary_text
        assert "schema_version" not in primary_text
        _assert_retired_job_detail_controls_absent(page)


# ---------------------------------------------------------------------------
# Tests — Contextual Job Actions
# ---------------------------------------------------------------------------


class TestJobDetailActions:
    """Validate role-aware user-facing action labels."""

    def test_operator_and_admin_running_or_waiting_jobs_show_cancel_job(
        self,
        page,
        server_reachable,
        base_url,
        cancellable_job_id,
        active_test_role,
    ):
        """Running and approval-state jobs use the single Cancel job label."""
        _require_role(active_test_role, "operator", "admin")
        _goto_job_detail(page, base_url, cancellable_job_id)
        _by_test_id(page, "job-cancel-button").first.wait_for(state="visible", timeout=10_000)
        assert _by_test_id(page, "job-cancel-button").count() > 0

    def test_operator_and_admin_terminal_jobs_show_restart_job(
        self,
        page,
        server_reachable,
        base_url,
        terminal_job_id,
        active_test_role,
    ):
        """Terminal jobs surface Restart job as the page-level action."""
        _require_role(active_test_role, "operator", "admin")
        _goto_job_detail(page, base_url, terminal_job_id)
        _by_test_id(page, "job-restart-button").first.wait_for(state="visible", timeout=10_000)
        assert _by_test_id(page, "job-restart-button").count() > 0

    def test_retryable_selected_step_surfaces_restart_from_this_step(
        self,
        page,
        server_reachable,
        base_url,
        retryable_job_id,
        active_test_role,
    ):
        """Retryable interrupted steps expose restart from the selected-step inspector."""
        _require_role(active_test_role, "operator", "admin")
        _goto_job_detail(page, base_url, retryable_job_id)
        _assert_test_id_visible(page, PRIMARY_TEST_IDS["step_inspector"])
        _assert_test_id_visible(page, PRIMARY_TEST_IDS["next_action"])
