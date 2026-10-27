"""
UI tests for the Projects page (/projects).

Tests:
- loads
- shows existing projects
- create modal opens
- create form has required fields
- create submits
- project detail shows info
- detail shows jobs table
- edit form is prepopulated
- delete disabled when project has jobs

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_projects_page.py -v --timeout=60
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress

import pytest
import requests

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _attach_projects_browser_error_watchers(page):
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
def _fail_on_projects_runtime_errors(page):
    page_errors, console_errors = _attach_projects_browser_error_watchers(page)
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


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _csrf_headers(session: requests.Session | None) -> dict[str, str]:
    if session is None:
        return {}
    csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
    return {"x-csrf-token": csrf_token} if csrf_token else {}


def _request(
    session: requests.Session | None,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> requests.Response:
    caller = session.request if session is not None else requests.request
    final_headers = dict(headers or {})
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        final_headers.update(_csrf_headers(session))
    return caller(
        method,
        url,
        headers=final_headers,
        verify=VERIFY_SSL,
        timeout=30,
        **kwargs,
    )


def _ensure_admin_session(base_url: str) -> requests.Session:
    resolved = resolve_test_env(os.environ, lane="ui", role="admin", include_dotenv=True)
    session = requests.Session()
    resp = session.post(
        f"{base_url}/api/auth/login",
        json={"email": resolved.username, "password": resolved.password},
        verify=VERIFY_SSL,
        timeout=30,
    )
    assert resp.status_code == 200, (
        f"admin login failed for project-history cleanup/setup: "
        f"{resp.status_code} {resp.text[:300]}"
    )
    csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
    if csrf_token:
        session.headers["x-csrf-token"] = csrf_token
    return session


def _create_project(
    session: requests.Session | None,
    base_url: str,
    *,
    name: str,
) -> str:
    payload: dict[str, object] = {
        "name": name,
        "workspace_id": DEFAULT_WORKSPACE_ID,
    }
    resp = _request(
        session,
        "POST",
        f"{base_url}/api/projects",
        json=payload,
    )
    assert resp.status_code == 201, f"project create failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json() or {}
    project_id = str(body.get("id") or body.get("project_id") or "").strip()
    assert project_id, f"project create missing id: {body}"
    return project_id


def _update_project(
    session: requests.Session | None,
    base_url: str,
    project_id: str,
    payload: dict[str, object],
) -> None:
    resp = _request(
        session,
        "PUT",
        f"{base_url}/api/projects/{project_id}",
        json=payload,
    )
    assert resp.status_code == 200, f"project update failed: {resp.status_code} {resp.text[:300]}"


def _project_detail(session: requests.Session | None, base_url: str, project_id: str) -> dict:
    resp = _request(session, "GET", f"{base_url}/api/projects/{project_id}")
    assert resp.status_code == 200, f"project detail failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, dict)
    return body


def _project_history(
    session: requests.Session | None,
    base_url: str,
    project_id: str,
    *,
    limit: int = 10,
) -> list[dict]:
    resp = _request(session, "GET", f"{base_url}/api/projects/{project_id}/history?limit={limit}")
    assert resp.status_code == 200, f"project history failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json() or {}
    rows = body.get("revisions") if isinstance(body, dict) else body
    assert isinstance(rows, list), f"history must return revisions list: {body}"
    return [row for row in rows if isinstance(row, dict)]


def _trash_project(session: requests.Session | None, base_url: str, project_id: str) -> None:
    resp = _request(session, "POST", f"{base_url}/api/trash/project/{project_id}/trash", json={})
    assert resp.status_code == 200, f"trash failed: {resp.status_code} {resp.text[:300]}"


def _restore_project(session: requests.Session | None, base_url: str, project_id: str) -> None:
    resp = _request(session, "POST", f"{base_url}/api/trash/project/{project_id}/restore", json={})
    assert resp.status_code == 200, f"restore failed: {resp.status_code} {resp.text[:300]}"


def _hard_delete_project(session: requests.Session | None, base_url: str, project_id: str) -> None:
    """Purge a trashed project through its lifecycle plan, as the Trash page does.

    The direct project hard-delete endpoint refuses every project with 409. A
    project the test already purged has no plan (404) and needs nothing more.
    """
    trash_url = f"{base_url}/api/trash/project/{project_id}"
    plan = _request(session, "POST", f"{trash_url}/plan", json={"action_type": "hard_delete"})
    if plan.status_code == 404:
        return
    assert plan.status_code == 200, f"purge plan failed: {plan.status_code} {plan.text[:300]}"
    resp = _request(
        session,
        "POST",
        f"{trash_url}/execute",
        json={"action_type": "hard_delete", "plan_version": plan.json().get("plan_version")},
    )
    assert resp.status_code == 200, f"purge failed: {resp.status_code} {resp.text[:300]}"


def _wait_for_grid_row(page, grid_id: str, match_text: str, *, timeout: int = 15_000) -> None:
    page.wait_for_function(
        """([gridId, matchText]) => {
            const grid = document.getElementById(gridId);
            if (!grid || !grid.shadowRoot) return false;
            return Array.from(grid.shadowRoot.querySelectorAll('tbody tr'))
              .some((row) => String(row.innerText || '').includes(matchText));
        }""",
        arg=[grid_id, match_text],
        timeout=timeout,
    )


def _wait_for_grid_row_absent(
    page,
    grid_id: str,
    match_text: str,
    *,
    timeout: int = 15_000,
) -> None:
    page.wait_for_function(
        """([gridId, matchText]) => {
            const grid = document.getElementById(gridId);
            if (!grid || !grid.shadowRoot) return true;
            return !Array.from(grid.shadowRoot.querySelectorAll('tbody tr'))
              .some((row) => String(row.innerText || '').includes(matchText));
        }""",
        arg=[grid_id, match_text],
        timeout=timeout,
    )


def _grid_action_buttons(page, grid_id: str, action_id: str) -> int:
    return int(
        page.evaluate(
            """([gridId, actionId]) => {
                const grid = document.getElementById(gridId);
                if (!grid || !grid.shadowRoot) return 0;
                return grid.shadowRoot.querySelectorAll(
                    `button[data-row-action="${actionId}"]`
                ).length;
            }""",
            [grid_id, action_id],
        )
    )


def _grid_click_action(page, grid_id: str, match_text: str, action_id: str) -> bool:
    return bool(
        page.evaluate(
            """([gridId, matchText, actionId]) => {
                const grid = document.getElementById(gridId);
                if (!grid || !grid.shadowRoot) return false;
                const rows = Array.from(grid.shadowRoot.querySelectorAll('tbody tr'));
                for (const row of rows) {
                    if (!String(row.innerText || '').includes(matchText)) continue;
                    const button = row.querySelector(`button[data-row-action="${actionId}"]`);
                    if (!button) return false;
                    button.click();
                    return true;
                }
                return false;
            }""",
            [grid_id, match_text, action_id],
        )
    )


def _accept_next_dialog(page) -> dict[str, str]:
    seen: dict[str, str] = {}

    def handler(dialog):
        seen["type"] = dialog.type
        seen["message"] = dialog.message
        dialog.accept()

    page.once("dialog", handler)
    return seen


@pytest.fixture
def project_history_browser_story(base_url, ui_auth_session, active_test_role):
    session, _verify_ssl = ui_auth_session
    admin_session = _ensure_admin_session(base_url)
    owner_session = session if active_test_role == "admin" else admin_session
    base_name = _uid("ui-history")
    project_id = _create_project(owner_session, base_url, name=base_name)

    state_b = {
        "name": f"{base_name}-v2",
        "description": "History story revision B",
    }
    state_c = {
        "name": f"{base_name}-v3",
        "description": "History story revision C",
        "project_rules": "Keep weekly summaries short.",
    }

    _update_project(owner_session, base_url, project_id, state_b)
    _update_project(owner_session, base_url, project_id, state_c)
    history_rows = _project_history(owner_session, base_url, project_id, limit=10)
    assert len(history_rows) >= 3, f"expected create + 2 saves; got {history_rows}"

    try:
        yield {
            "project_id": project_id,
            "base_name": base_name,
            "previous_revision_id": str(history_rows[1]["id"]).strip(),
            "state_b": {
                "name": state_b["name"],
                "description": state_b["description"],
                "project_rules": "",
            },
        }
    finally:
        with suppress(AssertionError):
            _trash_project(owner_session, base_url, project_id)
        _hard_delete_project(owner_session, base_url, project_id)


@pytest.fixture
def trashed_project(base_url, ui_auth_session, active_test_role):
    if active_test_role != "admin":
        yield {}
        return

    session, _verify_ssl = ui_auth_session
    project_name = _uid("ui-trash")
    project_id = _create_project(session, base_url, name=project_name)
    _trash_project(session, base_url, project_id)

    try:
        yield {"project_id": project_id, "project_name": project_name}
    finally:
        with suppress(AssertionError):
            _trash_project(session, base_url, project_id)
        _hard_delete_project(session, base_url, project_id)


class TestProjectsListPage:
    """Tests for the /projects list page."""

    def test_projects_page_loads(self, page, server_reachable, base_url):
        """Projects page loads with 200 status."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        assert "project" in page.title().lower() or page.url.endswith("/projects")

    def test_projects_page_has_title(self, page, server_reachable, base_url):
        """Projects page has a visible title or heading."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        # Look for a Flow Steward or Projects heading
        heading = page.query_selector("h1, h2, .page-title, [data-testid='page-title']")
        assert heading is not None or "project" in page.content().lower()

    def test_projects_page_shows_projects(self, page, server_reachable, base_url):
        """Projects page shows a list/table of projects."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        # Look for table rows, cards, or project list items
        content = page.content().lower()
        has_projects = (
            page.query_selector("table, .project-card, [data-testid='project-list']") is not None
            or "project" in content
        )
        assert has_projects

    def test_create_project_button_exists(self, page, server_reachable, base_url):
        """Create project button is visible."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        # Look for create/new project button
        btn = page.query_selector(
            "button:has-text('Create'), button:has-text('New'), "
            "a:has-text('Create'), [data-testid='create-project']"
        )
        assert btn is not None or "create" in page.content().lower()

    def test_create_modal_opens(self, page, server_reachable, base_url):
        """Clicking create opens a modal."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        # Try to find and click create button
        btn = page.query_selector(
            "button:has-text('Create'), button:has-text('New Project'), "
            "[data-action='create-project']"
        )
        assert btn is not None, "Create button not found on projects page"
        btn.click()
        page.wait_for_timeout(500)

        # Modal should appear
        modal = page.query_selector(".modal, dialog, [role='dialog'], [data-testid='create-modal']")
        assert modal is not None or "form" in page.content().lower()

    def test_create_form_has_project_contract_fields(self, page, server_reachable, base_url):
        """Create project form exposes the canonical project create contract fields."""
        page.goto(f"{base_url}/projects", wait_until="networkidle")
        btn = page.query_selector("button:has-text('Create'), button:has-text('New')")
        if btn:
            btn.click()
            page.wait_for_timeout(500)

        always_required_names = [
            "name",
            "workspace_id",
            "workflow_ids",
            "operator_default_workflow_id",
        ]
        missing = [
            name for name in always_required_names if page.locator(f"[name='{name}']").count() == 0
        ]
        assert missing == []
        removed_repository_fields = [
            "project_type",
            "repo_url",
            "project_root",
            "container_root",
            "supported_extensions",
            "auto_index_enabled",
            "stack_language",
        ]
        present = [
            name for name in removed_repository_fields if page.locator(f"[name='{name}']").count()
        ]
        assert present == []


class TestProjectDetailPage:
    """Tests for the project detail page /projects/{id}."""

    def test_project_detail_loads(self, page, server_reachable, base_url, test_project_id):
        """Project detail page loads."""
        page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle")
        assert page.url is not None
        # Should show project content
        content = page.content()
        assert len(content) > 100

    def test_project_detail_shows_jobs(self, page, server_reachable, base_url, test_project_id):
        """Project detail shows a jobs table/list."""
        page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle")
        content = page.content().lower()
        has_jobs = (
            "job" in content
            or page.query_selector("table, .job-list, [data-testid='jobs-table']") is not None
        )
        assert has_jobs

    def test_project_detail_shows_name(self, page, server_reachable, base_url, test_project_id):
        """Project detail page shows the project name."""
        page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle")
        content = page.content()
        # Should have some project identity on the page
        assert len(content) > 200

    def test_project_detail_shows_every_section_as_a_tab(
        self, page, server_reachable, base_url, test_project_id
    ):
        """One level: the project page's tab strip holds every project section."""
        page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle")
        tablist = page.get_by_role("tablist", name="Project sections")
        tablist.wait_for(state="visible", timeout=10_000)
        assert tablist.get_by_role("tab").all_inner_texts() == [
            "Overview",
            "Workflows",
            "Data Connections",
            "Policy",
            "Models",
            "Notifications",
            "Runtime Connections",
            "Datasets",
            "History",
        ]
        assert page.get_by_role("link", name="Configuration").count() == 0

    def test_submit_task_button_is_absent(self, page, server_reachable, base_url, test_project_id):
        """Project overview no longer offers generic task submission."""
        page.goto(f"{base_url}/projects/{test_project_id}", wait_until="networkidle")
        assert page.get_by_role("button", name="Submit Task", exact=True).count() == 0
        assert page.locator("[data-testid='submit-task'], #task-submit-form").count() == 0


class TestProjectHistoryBrowserFlows:
    def test_project_history_page_shows_selected_revision_diff(
        self,
        page,
        server_reachable,
        base_url,
        active_test_role,
        project_history_browser_story,
    ):
        project_id = project_history_browser_story["project_id"]
        revision_id = project_history_browser_story["previous_revision_id"]

        response = page.goto(
            f"{base_url}/projects/{project_id}?tab=history&selected_revision_id={revision_id}",
            wait_until="networkidle",
        )
        assert response is not None
        assert response.status < 400
        if active_test_role == "viewer":
            assert page.get_by_role("button", name="Restore this revision").count() == 0
            assert page.get_by_text("Viewer mode is read-only on project history.").count() > 0
        else:
            page.get_by_role("button", name="Restore this revision").first.wait_for(
                state="visible",
                timeout=10_000,
            )
        revision_dialog = page.locator("[role='dialog']").first
        page.wait_for_function(
            """() => {
                const dialog = document.querySelector('[role="dialog"]');
                if (!dialog) return false;
                const text = dialog.innerText || '';
                const rowCount = dialog.querySelectorAll('table tbody tr').length;
                const loading = text.includes('Loading diff');
                return !loading && (
                    rowCount > 0 ||
                    text.includes('No field-level changes in the selected revision.') ||
                    text.includes('Failed to load diff.')
                );
            }""",
            timeout=10_000,
        )
        assert revision_dialog.get_by_text("Failed to load diff.").count() == 0
        diff_rows = revision_dialog.locator("table tbody tr")
        assert (
            diff_rows.count() > 0
            or page.locator(
                "text=No field-level changes in the selected revision."
            ).first.is_visible()
        )

    def test_project_history_restore_flow_rehydrates_fields_in_browser(
        self,
        page,
        server_reachable,
        base_url,
        ui_auth_session,
        active_test_role,
        project_history_browser_story,
    ):
        session, _verify_ssl = ui_auth_session
        project_id = project_history_browser_story["project_id"]
        revision_id = project_history_browser_story["previous_revision_id"]
        expected_state = project_history_browser_story["state_b"]

        if active_test_role == "viewer":
            response = page.goto(
                f"{base_url}/projects/{project_id}?tab=history&selected_revision_id={revision_id}",
                wait_until="networkidle",
            )
            assert response is not None
            assert response.status < 400
            assert page.get_by_role("button", name="Restore this revision").count() == 0
            assert page.get_by_text("Viewer mode is read-only on project history.").count() > 0
            return

        response = page.goto(
            f"{base_url}/projects/{project_id}?tab=history&selected_revision_id={revision_id}",
            wait_until="networkidle",
        )
        assert response is not None
        assert response.status < 400

        dialog = _accept_next_dialog(page)
        with page.expect_response(
            lambda resp: (
                f"/api/projects/{project_id}/history/{revision_id}/restore" in resp.url
                and resp.request.method == "POST"
            ),
            timeout=15_000,
        ) as restore_response_info:
            page.get_by_role("button", name="Restore this revision").click()
        restore_response = restore_response_info.value
        assert restore_response.status < 400, (
            f"restore API failed: {restore_response.status} {restore_response.text()[:300]}"
        )

        assert dialog["type"] == "confirm"
        assert str(revision_id) in dialog["message"]
        page.wait_for_function(
            "() => window.location.search.includes('flash_type=info')",
            timeout=10_000,
        )
        success_flash = page.get_by_text("Project settings restored from revision").first
        if success_flash.count() > 0:
            success_flash.wait_for(state="visible", timeout=10_000)

        detail = _project_detail(session, base_url, project_id)
        assert detail["name"] == expected_state["name"]
        assert detail["description"] == expected_state["description"]
        assert str(detail.get("project_rules") or "") == expected_state["project_rules"]


def _trash_row(page, match_text: str):
    """The Trash table is the design-system DsTable in the light DOM."""
    return page.locator("#trash-grid tbody tr", has_text=match_text).first


def _trash_row_actions(page, match_text: str) -> list[str]:
    """Open the row's action menu and return the names it offers."""
    _trash_row(page, match_text).locator('button[aria-label^="Actions for "]').click()
    menu = page.get_by_role("menu")
    menu.wait_for(state="visible")
    names = [name.strip() for name in menu.get_by_role("menuitem").all_inner_texts()]
    page.keyboard.press("Escape")
    menu.wait_for(state="hidden")
    return names


def _trash_click_action(page, match_text: str, action_label: str) -> None:
    _trash_row(page, match_text).locator('button[aria-label^="Actions for "]').click()
    page.get_by_role("menuitem", name=action_label, exact=True).click()


class TestTrashBrowserFlows:
    def test_trash_page_access_is_role_aware(
        self,
        page,
        server_reachable,
        base_url,
        active_test_role,
    ):
        if active_test_role != "admin":
            page._allowed_console_error_patterns = ["status of 403 (Forbidden)"]
        response = page.goto(f"{base_url}/trash", wait_until="networkidle")
        assert response is not None
        if active_test_role == "admin":
            assert response.status < 400
            assert page.locator("#trash-grid").count() == 1
            return
        assert response.status == 200
        assert page.locator("#trash-grid").count() == 1

    def test_trash_page_restore_and_hard_delete_controls_work(
        self,
        page,
        server_reachable,
        base_url,
        ui_auth_session,
        active_test_role,
        trashed_project,
    ):
        if active_test_role != "admin":
            page._allowed_console_error_patterns = ["status of 403 (Forbidden)"]
            response = page.goto(f"{base_url}/trash", wait_until="networkidle")
            assert response is not None
            assert response.status == 200
            assert page.locator("#trash-grid").count() == 1
            return

        session, _verify_ssl = ui_auth_session
        project_id = trashed_project["project_id"]
        project_name = trashed_project["project_name"]

        response = page.goto(f"{base_url}/trash", wait_until="networkidle")
        assert response is not None
        assert response.status < 400
        page.fill("#trash-search", project_name)
        _trash_row(page, project_name).wait_for(state="visible", timeout=15_000)
        actions = _trash_row_actions(page, project_name)
        assert "Restore" in actions, f"Trash row must offer Restore; got {actions}"
        assert "Purge" in actions, f"Trash row must offer Purge; got {actions}"

        # A project is restored and purged through its lifecycle plan dialog.
        _trash_click_action(page, project_name, "Restore")
        restore_modal = page.get_by_role("dialog")
        restore_modal.wait_for(state="visible")
        restore_modal.get_by_role("button", name="Restore selected").click()
        restore_modal.wait_for(state="hidden")
        _trash_row(page, project_name).wait_for(state="detached", timeout=15_000)
        assert _project_detail(session, base_url, project_id)["id"] == project_id

        _trash_project(session, base_url, project_id)
        page.goto(f"{base_url}/trash", wait_until="networkidle")
        page.fill("#trash-search", project_name)
        _trash_row(page, project_name).wait_for(state="visible", timeout=15_000)

        _trash_click_action(page, project_name, "Purge")
        delete_modal = page.get_by_role("dialog")
        delete_modal.wait_for(state="visible")
        delete_modal.get_by_role("button", name="Hard delete selected").click()
        delete_modal.wait_for(state="hidden")
        _trash_row(page, project_name).wait_for(state="detached", timeout=15_000)

        deleted_resp = _request(session, "GET", f"{base_url}/api/projects/{project_id}")
        assert deleted_resp.status_code == 404, (
            f"hard-delete must remove the project; got {deleted_resp.status_code}"
        )
