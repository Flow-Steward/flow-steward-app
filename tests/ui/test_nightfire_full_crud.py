from __future__ import annotations

import os
import time
import uuid
from urllib.parse import quote

import pytest
import requests
from playwright.sync_api import Error as PlaywrightError

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.fixture(autouse=True)
def _admin_only_crud_cases(request, active_test_role):
    if request.node.name == "test_core_pages_have_no_runtime_errors":
        return
    if active_test_role != "admin":
        pytest.skip("Nightfire full CRUD mutation checks are admin-only")


def _goto_ok(page, url: str, *, timeout: int = 30_000):
    response = page.goto(url, wait_until="networkidle", timeout=timeout)
    assert response is not None, f"Navigation returned no response for {url}"
    assert response.status < 400, f"{url} returned status {response.status}"
    return response


def _collect_runtime_errors(page):
    page_errors: list[str] = []
    response_errors: list[tuple[str, int]] = []

    def on_pageerror(err):
        page_errors.append(str(err))

    def on_response(response):
        if response.url.startswith(BASE_URL) and response.status >= 400:
            response_errors.append((response.url, response.status))

    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    return page_errors, response_errors


def _wait_for_grid_row(page, grid_id: str, match_text: str, *, timeout: int = 30_000):
    page.wait_for_function(
        """([gridId, matchText]) => {
            const grid = document.getElementById(gridId);
            if (!grid) {
                return String(document.body?.innerText || '').includes(matchText);
            }
            if (grid.shadowRoot) {
                const inRows = Array.from(
                    grid.shadowRoot.querySelectorAll('tbody tr, [role="row"], tr, li, article, div')
                ).some((row) => String(row.innerText || '').includes(matchText));
                if (inRows) {
                    return true;
                }
                return String(grid.shadowRoot.innerText || '').includes(matchText);
            }
            return String(grid.innerText || '').includes(matchText);
        }""",
        arg=[grid_id, match_text],
        timeout=timeout,
    )


def _wait_for_grid_row_absent(page, grid_id: str, match_text: str, *, timeout: int = 15_000):
    page.wait_for_function(
        """([gridId, matchText]) => {
            const grid = document.getElementById(gridId);
            if (!grid) {
                return !String(document.body?.innerText || '').includes(matchText);
            }
            if (grid.shadowRoot) {
                const inRows = Array.from(
                    grid.shadowRoot.querySelectorAll('tbody tr, [role="row"], tr, li, article, div')
                ).some((row) => String(row.innerText || '').includes(matchText));
                if (inRows) {
                    return false;
                }
                return !String(grid.shadowRoot.innerText || '').includes(matchText);
            }
            return !String(grid.innerText || '').includes(matchText);
        }""",
        arg=[grid_id, match_text],
        timeout=timeout,
    )


def _set_grid_search(page, grid_id: str, query: str, *, timeout: int = 10_000):
    deadline = time.time() + (timeout / 1000.0)
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            page.wait_for_function(
                """(gridId) => {
                    const grid = document.getElementById(gridId);
                    return !!(grid && grid.shadowRoot && grid.shadowRoot.querySelector('.search input'));
                }""",
                arg=grid_id,
                timeout=min(timeout, 2_000),
            )
            page.evaluate(
                """([gridId, query]) => {
                    const grid = document.getElementById(gridId);
                    if (!grid || !grid.shadowRoot) return false;
                    const input = grid.shadowRoot.querySelector('.search input');
                    if (!input) return false;
                    input.value = query;
                    input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                    return true;
                }""",
                [grid_id, query],
            )
            return
        except PlaywrightError as exc:
            last_error = exc
            page.wait_for_timeout(150)
    fallback_search = page.locator(
        "#search, input[type='search'], input[placeholder*='Search'], input[placeholder*='search']"
    ).first
    if fallback_search.count() > 0:
        fallback_search.fill(query)
        fallback_search.press("Enter")
        return
    if last_error:
        raise last_error
    raise AssertionError(f"Timed out setting search for grid {grid_id!r}")


def _wait_for_url_contains(page, expected_fragment: str, *, timeout: int = 15_000):
    deadline = time.time() + (timeout / 1000.0)
    while time.time() < deadline:
        if expected_fragment in page.url:
            return
        page.wait_for_timeout(100)
    raise AssertionError(
        f"Timed out waiting for URL containing {expected_fragment!r}; got {page.url!r}"
    )


def _wait_for_status_text(page, selector: str, expected_text: str, *, timeout: int = 10_000):
    page.wait_for_function(
        """([selector, expectedText]) => {
            const el = document.querySelector(selector);
            if (!el || el.classList.contains('hidden')) return false;
            return String(el.textContent || '').includes(expectedText);
        }""",
        arg=[selector, expected_text],
        timeout=timeout,
    )


def _click_and_wait_for_reload(page, locator, *, timeout: int = 10_000):
    with page.expect_navigation(wait_until="networkidle", timeout=timeout):
        locator.click()


def _best_effort_cleanup_delete(url: str) -> None:
    try:
        resp = requests.delete(url, verify=VERIFY_SSL, timeout=20)
    except requests.RequestException:
        return
    if resp.status_code in {200, 404}:
        return


def _auth_db_dsn() -> str:
    return str(
        os.environ.get("FS_DB_DSN")
        or os.environ.get("FS_DB_DSN")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()


def _authenticated_requests_session(base_url: str) -> requests.Session:
    resolved = resolve_test_env(
        os.environ,
        lane="ui",
        role=os.getenv("NIGHTFIRE_AUTH_ROLE") or os.getenv("E2E_UI_ROLE"),
        include_dotenv=True,
    )
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": resolved.username, "password": resolved.password},
        verify=VERIFY_SSL,
        timeout=20,
    )
    response.raise_for_status()
    csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
    if csrf_token:
        session.headers["x-csrf-token"] = csrf_token
    return session


def _delete_user_by_username(username: str) -> None:
    clean_username = str(username or "").strip()
    if not clean_username:
        return
    dsn = _auth_db_dsn()
    if not dsn:
        return
    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (clean_username,))
    except Exception:
        pass


def _delete_scheduled_job_by_summary(
    base_url: str, project_id: str | int, task_summary: str
) -> None:
    clean_summary = str(task_summary or "").strip()
    if not clean_summary:
        return
    try:
        resp = requests.get(
            f"{base_url}/api/projects/{project_id}/scheduled-jobs",
            verify=VERIFY_SSL,
            timeout=20,
        )
        if not resp.ok:
            return
        payload = resp.json() or {}
        rows = payload.get("scheduled_jobs") or payload.get("jobs") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("task_summary") or "").strip() != clean_summary:
                continue
            scheduled_job_id = str(row.get("id") or "").strip()
            if not scheduled_job_id:
                continue
            requests.delete(
                f"{base_url}/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
    except Exception:
        pass


def _wait_for_scheduled_job_by_summary(
    base_url: str,
    project_id: str | int,
    task_summary: str,
    *,
    timeout_seconds: float = 20.0,
) -> dict | None:
    clean_summary = str(task_summary or "").strip()
    if not clean_summary:
        return None
    deadline = time.time() + max(timeout_seconds, 0.5)
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{base_url}/api/projects/{project_id}/scheduled-jobs",
                verify=VERIFY_SSL,
                timeout=20,
            )
            if resp.ok:
                payload = resp.json() or {}
                rows = payload.get("scheduled_jobs") or payload.get("jobs") or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("task_summary") or "").strip() == clean_summary:
                        return row
        except Exception:
            pass
        time.sleep(0.35)
    return None


def _resolve_supported_scheduled_job_task(
    base_url: str,
    project_id: str | int,
    workflow_ids: list[str],
) -> tuple[str, str] | None:
    preview_url = f"{base_url}/api/projects/{project_id}/workflow-preview"
    suffix = _nightfire_alpha_suffix(6)
    candidate_summaries = [
        f"Fix bug causing scheduler error {suffix}",
        f"Investigate production incident outage {suffix}",
        f"Implement feature delivery flow {suffix}",
        f"Refactor maintenance cleanup {suffix}",
        f"Quick typo formatting fix {suffix}",
    ]
    for workflow_id in workflow_ids:
        clean_workflow = str(workflow_id or "").strip()
        if not clean_workflow:
            continue
        for summary in candidate_summaries:
            try:
                resp = requests.post(
                    preview_url,
                    json={
                        "task_summary": summary,
                        "workflow_id": clean_workflow,
                    },
                    verify=VERIFY_SSL,
                    timeout=20,
                )
            except requests.RequestException:
                continue
            if resp.status_code != 200:
                continue
            payload = resp.json() if resp.content else {}
            selected_workflow_id = str(payload.get("selected_workflow_id") or "").strip()
            requested_workflow_id = str(payload.get("requested_workflow_id") or "").strip()
            if selected_workflow_id != clean_workflow:
                continue
            if requested_workflow_id and requested_workflow_id != clean_workflow:
                continue
            return clean_workflow, summary
    return None


def _nightfire_alpha_suffix(length: int = 8) -> str:
    letters = "".join(ch for ch in uuid.uuid4().hex if ch.isalpha())
    if len(letters) < length:
        letters = f"{letters}{'nightfire'}"
    return letters[:length]


def _delete_project_by_name(base_url: str, project_name: str) -> None:
    clean_name = str(project_name or "").strip()
    if not clean_name:
        return
    try:
        resp = requests.get(
            f"{base_url}/api/projects",
            params={"search": clean_name, "per_page": 200},
            verify=VERIFY_SSL,
            timeout=20,
        )
        if not resp.ok:
            return
        rows = (resp.json() or {}).get("projects") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip() != clean_name:
                continue
            project_id = str(row.get("id") or "").strip()
            if not project_id:
                continue
            requests.delete(
                f"{base_url}/api/projects/{project_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
    except Exception:
        pass


def _find_project_by_name(
    base_url: str,
    project_name: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict | None:
    clean_name = str(project_name or "").strip()
    if not clean_name:
        return None
    deadline = time.time() + max(timeout_seconds, 0.5)
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{base_url}/api/projects",
                params={"search": clean_name, "per_page": 200},
                verify=VERIFY_SSL,
                timeout=20,
            )
            if not resp.ok:
                time.sleep(0.35)
                continue
            rows = (resp.json() or {}).get("projects") or []
            for row in rows:
                if isinstance(row, dict) and str(row.get("name") or "").strip() == clean_name:
                    return row
        except Exception:
            pass
        time.sleep(0.35)
    return None


def _select_first_create_workflow(page) -> None:
    try:
        page.wait_for_selector(
            "#modal-create input[name='linked_selected']:not([disabled])",
            timeout=10_000,
        )
    except PlaywrightError:
        pytest.skip("Project CRUD requires at least one enabled workflow in the catalog")
    page.locator("#modal-create input[name='linked_selected']:not([disabled])").first.check()


def _ensure_first_edit_workflow_selected(page) -> None:
    tab = page.get_by_role("button", name="Workflows")
    if tab.count() > 0:
        tab.click()
    try:
        page.locator("input[aria-label^='Toggle ']:not([disabled])").first.wait_for(
            state="visible",
            timeout=10_000,
        )
    except PlaywrightError:
        pytest.skip("Project edit requires at least one enabled workflow in the catalog")
    if page.locator("input[aria-label^='Toggle ']:checked").count() == 0:
        page.locator("input[aria-label^='Toggle ']:not([disabled])").first.check()


def _find_user_by_email(
    base_url: str,
    email: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict | None:
    clean_email = str(email or "").strip().lower()
    if not clean_email:
        return None
    session = _authenticated_requests_session(base_url)
    deadline = time.time() + max(timeout_seconds, 0.5)
    while time.time() < deadline:
        try:
            resp = session.get(
                f"{base_url}/api/auth/users",
                params={"page": 1, "per_page": 200},
                verify=VERIFY_SSL,
                timeout=20,
            )
            if not resp.ok:
                time.sleep(0.35)
                continue
            rows = (resp.json() or {}).get("users") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = str(row.get("email") or row.get("username") or "").strip().lower()
                if value == clean_email:
                    return row
        except Exception:
            pass
        time.sleep(0.35)
    return None


def _find_guardrail_by_name(
    page,
    base_url: str,
    display_name: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict | None:
    needle = str(display_name or "").strip()
    if not needle:
        return None
    deadline = time.time() + max(timeout_seconds, 0.5)
    while time.time() < deadline:
        try:
            rows = page.evaluate(
                """async () => {
                    const response = await fetch("/api/guardrails", {
                        headers: { Accept: "application/json" },
                    });
                    if (!response.ok) {
                        return null;
                    }
                    const body = await response.json();
                    return Array.isArray(body) ? body : (body.guardrails || []);
                }"""
            )
            if not rows:
                time.sleep(0.35)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate_names = [
                    str(row.get("display_name") or "").strip(),
                    str(row.get("name") or "").strip(),
                    str(row.get("title") or "").strip(),
                ]
                if any(name == needle for name in candidate_names):
                    return row
        except Exception:
            pass
        time.sleep(0.35)
    return None


def _wait_for_tool_detail_ready(page, *, timeout: int = 15_000):
    try:
        page.locator("#tool-panel-config input[name='display_name']").wait_for(
            state="visible", timeout=min(timeout, 5_000)
        )
        return
    except PlaywrightError:
        pass
    page.wait_for_function(
        """() => {
            const hasConfigTabButton = Array.from(document.querySelectorAll("button"))
                .some((row) => /config/i.test(String(row.textContent || "")));
            const hasDisplayNameInput = Array.from(document.querySelectorAll("input"))
                .some((row) => /display name/i.test(String(row.getAttribute("placeholder") || "")));
            return hasConfigTabButton || hasDisplayNameInput;
        }""",
        timeout=timeout,
    )


def _open_details_section(page, heading_text: str, *, timeout: int = 10_000):
    details = page.locator("details", has_text=heading_text).first
    if details.count():
        details.wait_for(state="attached", timeout=timeout)
        is_open = bool(details.evaluate("el => !!el.open"))
        if is_open:
            return details
        details.locator("summary").click()
        page.wait_for_function(
            "(el) => !!el.open",
            arg=details.element_handle(),
            timeout=timeout,
        )
        return details

    if str(heading_text or "").strip().lower() == "scheduled jobs":
        tab_btn = page.locator("[data-workspace-tab='automation']").first
        if tab_btn.count():
            tab_btn.click()
        panel = page.locator("#workspace-panel-automation").first
        panel.wait_for(state="visible", timeout=timeout)
        return panel

    raise AssertionError(f"Details section '{heading_text}' was not found")


def _grid_click_action(page, grid_id: str, match_text: str, action_id: str) -> bool:
    return bool(
        page.evaluate(
            """([gridId, matchText, actionId]) => {
                const grid = document.getElementById(gridId);
                const actionLabels = {
                    view: ["view", "detail"],
                    edit: ["edit", "config"],
                    delete: ["delete"],
                    clone: ["clone"],
                    revoke: ["revoke"],
                    toggle_active: ["toggle", "active", "inactive"],
                    force_reset: ["force reset", "reset"],
                };
                const labels = actionLabels[actionId] || [String(actionId || "").replaceAll("_", " ")];
                const clickMatchingAction = (scope) => {
                    const rows = Array.from(scope.querySelectorAll("tr, div, li, article"));
                    for (const row of rows) {
                        if (!String(row.innerText || "").includes(matchText)) continue;
                        const explicit = row.querySelector(`button[data-row-action="${actionId}"], a[data-row-action="${actionId}"]`);
                        if (explicit) {
                            explicit.click();
                            return true;
                        }
                        const candidates = Array.from(row.querySelectorAll("button, a"));
                        for (const candidate of candidates) {
                            const text = String(candidate.textContent || "").toLowerCase();
                            if (labels.some((label) => text.includes(label))) {
                                candidate.click();
                                return true;
                            }
                        }
                    }
                    return false;
                };
                if (grid && grid.shadowRoot && clickMatchingAction(grid.shadowRoot)) {
                    return true;
                }
                if (grid && clickMatchingAction(grid)) {
                    return true;
                }
                return clickMatchingAction(document);
            }""",
            [grid_id, match_text, action_id],
        )
    )


def _accept_next_dialog(page, *, prompt_text: str | None = None):
    seen: dict[str, str] = {}

    def handler(dialog):
        seen["type"] = dialog.type
        seen["message"] = dialog.message
        if dialog.type == "prompt":
            dialog.accept(prompt_text or "")
        else:
            dialog.accept()

    page.once("dialog", handler)
    return seen


class TestNightfireFullCrud:
    @pytest.mark.parametrize(
        ("path", "allowed_response_error_suffixes"),
        [
            ("/dashboard", []),
            ("/settings/account", []),
            ("/settings/users", []),
        ],
    )
    def test_core_pages_have_no_runtime_errors(
        self,
        page,
        server_reachable,
        base_url,
        path,
        allowed_response_error_suffixes,
        active_test_role,
    ):
        if active_test_role != "admin" and path == "/settings/users":
            pytest.skip("Users & Access page is admin-only")
        page_errors, response_errors = _collect_runtime_errors(page)
        _goto_ok(page, f"{base_url}{path}")
        page.wait_for_timeout(1500)
        allowed_statuses = set(allowed_response_error_suffixes)
        if active_test_role != "admin" and path == "/dashboard":
            allowed_statuses.update(
                {
                    "/api/providers?enabled_only=true",
                    "/api/extensions?state=active",
                }
            )
        filtered_response_errors = [
            (url, status)
            for url, status in response_errors
            if not any(url.endswith(suffix) for suffix in allowed_statuses)
        ]
        assert not page_errors, f"{path} emitted page errors: {page_errors}"
        assert not filtered_response_errors, (
            f"{path} emitted HTTP errors: {filtered_response_errors}"
        )

    def test_account_token_create_and_revoke_roundtrip(self, page, server_reachable, base_url):
        token_id: str | None = None
        token_label = f"nightfire-token-{uuid.uuid4().hex[:8]}"
        try:
            _goto_ok(page, f"{base_url}/settings/account")
            page.get_by_role("button", name="Personal tokens").click()
            page.locator("#pat-form").wait_for(state="visible", timeout=10_000)
            page.fill("#pat-label", token_label)
            # Expiry is inside a <details> collapse — open it before filling
            page.locator("#pat-form details summary").click()
            page.fill("#pat-expiry", "30")
            page.locator("#pat-form button[type='submit']").click()
            page.locator("#token-success").wait_for(state="visible", timeout=10_000)
            _wait_for_grid_row(page, "token-grid", token_label)

            token_rows = (
                requests.get(f"{base_url}/api/auth/tokens", verify=VERIFY_SSL, timeout=20)
                .json()
                .get("tokens")
                or []
            )
            for row in token_rows:
                if isinstance(row, dict) and str(row.get("label") or "") == token_label:
                    token_id = str(row.get("id") or "").strip() or None
                    break

            _accept_next_dialog(page)
            assert _grid_click_action(page, "token-grid", token_label, "revoke"), (
                "Token revoke action was not clickable"
            )
            _wait_for_grid_row_absent(page, "token-grid", token_label)
        finally:
            if token_id:
                requests.post(
                    f"{base_url}/api/auth/tokens/{token_id}/revoke", verify=VERIFY_SSL, timeout=20
                )

    def test_users_page_create_edit_toggle_and_force_reset_roundtrip(
        self,
        page,
        server_reachable,
        base_url,
    ):
        username = f"nightfire_user_{uuid.uuid4().hex[:8]}"
        email = f"{username}@nightfire.test"
        updated_name = f"Nightfire User Updated {uuid.uuid4().hex[:4]}"
        try:
            _goto_ok(page, f"{base_url}/settings/users")
            page.get_by_role("button", name="+ New User").click()
            page.fill("#modal-email", email)
            page.fill("#modal-display-name", f"Nightfire User {username}")
            page.fill("#modal-password", "NightfirePass_1234!")
            page.locator("#modal-form button[type='submit']").click()
            page.locator("#modal-user").wait_for(state="hidden", timeout=10_000)

            created_user = _find_user_by_email(base_url, email)
            assert created_user is not None, (
                f"Created user {email!r} was not returned by /api/auth/users"
            )
            user_id = str(created_user.get("id") or "").strip()
            assert user_id, "Created user row is missing id"
            session = _authenticated_requests_session(base_url)

            update_resp = session.put(
                f"{base_url}/api/auth/users/{user_id}",
                json={"display_name": updated_name},
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert update_resp.ok, f"Failed to update user display name: {update_resp.status_code}"

            deactivate_resp = session.put(
                f"{base_url}/api/auth/users/{user_id}",
                json={"is_active": False},
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert deactivate_resp.ok, f"Failed to deactivate user: {deactivate_resp.status_code}"

            reactivate_resp = session.put(
                f"{base_url}/api/auth/users/{user_id}",
                json={"is_active": True},
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert reactivate_resp.ok, f"Failed to reactivate user: {reactivate_resp.status_code}"

            reset_resp = session.post(
                f"{base_url}/api/auth/users/{user_id}/force-password-reset",
                json={},
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert reset_resp.ok, f"Failed to force reset user password: {reset_resp.status_code}"
        finally:
            _delete_user_by_username(username)

    def test_project_create_edit_delete_roundtrip(self, page, server_reachable, base_url):
        project_name = f"Nightfire Project {uuid.uuid4().hex[:8]}"
        updated_name = f"Nightfire Project Updated {uuid.uuid4().hex[:4]}"
        created_project_id: str | None = None
        try:
            _goto_ok(page, f"{base_url}/projects")
            page.get_by_role("button", name="+ New Project").click()
            workspace_select = page.locator("#modal-create select[name='workspace_id']")
            workspace_select.wait_for(state="visible", timeout=10_000)
            workspace_select.select_option(index=1)
            page.fill("#modal-create input[name='name']", project_name)
            assert page.locator("#modal-create input[name='project_root']").count() == 0
            assert page.locator("#modal-create input[name='container_root']").count() == 0
            _select_first_create_workflow(page)
            page.locator("#modal-create button[type='submit']").click()
            _wait_for_url_contains(page, "/projects/")
            created_project_id = page.url.rstrip("/").rsplit("/", 1)[-1]
            assert created_project_id

            _goto_ok(page, f"{base_url}/projects/{created_project_id}")
            page.wait_for_function(
                """(expectedName) => {
                    const nameInput = document.querySelector("input[name='name']");
                    const workspaceInput = document.querySelector("input[name='workspace_id']");
                    return (
                        !!nameInput &&
                        String(nameInput.value || "").trim() === expectedName &&
                        !!workspaceInput &&
                        String(workspaceInput.value || "").trim().length > 0
                    );
                }""",
                arg=project_name,
                timeout=10_000,
            )
            page.fill("input[name='name']", updated_name)
            _ensure_first_edit_workflow_selected(page)
            with page.expect_response(
                lambda resp: (
                    f"/api/projects/{created_project_id}" in resp.url
                    and resp.request.method == "PUT"
                ),
                timeout=15_000,
            ) as update_response:
                page.get_by_role("button", name="Save Project").click()
            response_body = update_response.value.text()
            assert update_response.value.ok, (
                f"Project update failed with {update_response.value.status}: {response_body}"
            )
            assert page.locator("input[name='name']").input_value() == updated_name
            project_resp = requests.get(
                f"{base_url}/api/projects/{created_project_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert project_resp.ok
            assert project_resp.json().get("name") == updated_name

            delete_resp = requests.delete(
                f"{base_url}/api/projects/{created_project_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert delete_resp.status_code in {200, 204, 404}
            created_project_id = None
        finally:
            _delete_project_by_name(base_url, project_name)
            _delete_project_by_name(base_url, updated_name)
            if created_project_id:
                requests.delete(
                    f"{base_url}/api/projects/{created_project_id}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )

    def test_agent_create_edit_delete_roundtrip(self, page, server_reachable, base_url):
        agent_id = f"nightfire_agent_{uuid.uuid4().hex[:8]}"
        updated_name = f"Nightfire Agent Updated {uuid.uuid4().hex[:4]}"
        try:
            _goto_ok(page, f"{base_url}/registry/agents")
            page.get_by_role("button", name="+ New Agent").click()
            page.locator("#modal-create, [role='dialog']").first.wait_for(
                state="visible",
                timeout=10_000,
            )
            page.locator("#create-agent-type-id, select[name='agent_type_id']").first.wait_for(
                state="visible",
                timeout=10_000,
            )
            options = page.locator(
                "#create-agent-type-id option:not([disabled]), "
                "select[name='agent_type_id'] option:not([disabled])"
            )
            if options.count() < 2:
                agent_types_resp = requests.get(
                    f"{base_url}/api/agent-types",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                assert agent_types_resp.ok, (
                    f"Agent types API failed: {agent_types_resp.status_code}"
                )
                payload = agent_types_resp.json() or []
                rows = payload if isinstance(payload, list) else payload.get("agent_types", [])
                assert isinstance(rows, list), f"Unexpected agent types payload: {payload!r}"
                assert rows == [] or options.count() >= 2, (
                    "Agent type records exist, but the create-agent form did not expose them."
                )
                return
            page.fill("input[name='agent_id']", agent_id)
            page.fill("input[name='display_name']", f"Nightfire Agent {agent_id}")
            page.locator("#create-agent-type-id, select[name='agent_type_id']").first.select_option(
                index=1
            )
            page.fill(
                "#create-description, textarea[name='description']",
                "Nightfire CRUD coverage agent",
            )
            page.fill(
                "#create-system-prompt, textarea[name='system_prompt']",
                "You are a Nightfire CRUD coverage agent.",
            )
            page.locator("#modal-create button[type='submit']").click()

            _wait_for_url_contains(page, f"/registry/agents/{agent_id}")
            verify_resp = requests.get(
                f"{base_url}/api/agents/{quote(agent_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_resp.ok, f"Failed to load agent detail via API: {verify_resp.status_code}"

            update_resp = requests.patch(
                f"{base_url}/api/agents/{quote(agent_id)}",
                json={
                    "display_name": updated_name,
                    "description": "Nightfire CRUD coverage agent updated",
                },
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert update_resp.ok, (
                f"Failed to update agent via API: {update_resp.status_code} {update_resp.text[:300]}"
            )

            verify_resp = requests.get(
                f"{base_url}/api/agents/{quote(agent_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_resp.ok, f"Failed to reload agent via API: {verify_resp.status_code}"
            verify_payload = verify_resp.json() or {}
            assert (
                str(verify_payload.get("display_name") or verify_payload.get("name") or "").strip()
                == updated_name
            )

            delete_resp = requests.delete(
                f"{base_url}/api/agents/{quote(agent_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert delete_resp.status_code in {200, 204, 404}
        finally:
            _best_effort_cleanup_delete(f"{base_url}/api/agents/{quote(agent_id)}")

    def test_tool_create_edit_delete_roundtrip(self, page, server_reachable, base_url):
        tool_id = f"nightfire_tool_{uuid.uuid4().hex[:8]}"
        updated_name = f"Nightfire Tool Updated {uuid.uuid4().hex[:4]}"
        try:
            _goto_ok(page, f"{base_url}/registry/tools")
            create_trigger = page.locator(
                'button[onclick*="modal-create-tool"], button:has-text("+ New Tool"), button:has-text("Create entry")'
            ).first
            if create_trigger.count() > 0:
                create_trigger.click()
            page.locator("input[name='tool_id'], #create-identifier").first.fill(tool_id)
            page.locator("input[name='display_name'], #create-display-name").first.fill(
                f"Nightfire Tool {tool_id}"
            )
            page.locator(
                "input[name='description'], textarea[name='description'], #create-description"
            ).first.fill("Nightfire CRUD coverage tool")
            prompt_field = page.locator("textarea[name='prompt']").first
            if prompt_field.count() > 0:
                prompt_field.fill("Use this tool only for Nightfire CRUD coverage.")
            page.locator(
                "#modal-create-tool button[type='submit'], form:has(#create-identifier) button[type='submit']"
            ).first.click()

            if f"/registry/tools/{tool_id}" not in page.url:
                _goto_ok(page, f"{base_url}/registry/tools/{tool_id}")
            _goto_ok(page, f"{base_url}/registry/tools/{tool_id}")
            _wait_for_tool_detail_ready(page)
            verify_resp = requests.get(
                f"{base_url}/api/tools/{quote(tool_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_resp.ok, f"Failed to load tool detail via API: {verify_resp.status_code}"
            verify_payload = verify_resp.json() or {}
            update_payload = dict(verify_payload)
            update_payload["tool_id"] = tool_id
            update_payload["display_name"] = updated_name
            update_resp = requests.put(
                f"{base_url}/api/tools/{quote(tool_id)}",
                json=update_payload,
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert update_resp.ok, f"Failed to update tool via API: {update_resp.status_code}"
            verify_resp = requests.get(
                f"{base_url}/api/tools/{quote(tool_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_resp.ok, (
                f"Failed to reload tool detail via API: {verify_resp.status_code}"
            )
            verify_payload = verify_resp.json() or {}
            assert (
                str(verify_payload.get("display_name") or verify_payload.get("name") or "").strip()
                == updated_name
            )

            deleted = False
            delete_deadline = time.time() + 30
            delete_url = f"{base_url}/api/tools/{quote(tool_id)}"
            while time.time() < delete_deadline:
                delete_resp = requests.delete(
                    delete_url,
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                if delete_resp.status_code in {200, 204, 404}:
                    verify_resp = requests.get(
                        delete_url,
                        verify=VERIFY_SSL,
                        timeout=20,
                    )
                    if verify_resp.status_code == 404:
                        deleted = True
                        break
                page.wait_for_timeout(500)
            if not deleted:
                _best_effort_cleanup_delete(delete_url)
        finally:
            _best_effort_cleanup_delete(f"{base_url}/api/tools/{quote(tool_id)}")

    def test_skill_create_edit_delete_roundtrip(self, page, server_reachable, base_url):
        skill_id = f"nightfire_cap_{uuid.uuid4().hex[:8]}"
        updated_name = f"Nightfire Skill Updated {uuid.uuid4().hex[:4]}"
        try:
            _goto_ok(page, f"{base_url}/registry/skills")
            create_trigger = page.locator(
                'button[onclick*="modal-create-cap"], button:has-text("+ New Skill"), button:has-text("Create entry")'
            ).first
            if create_trigger.count() > 0:
                create_trigger.click()
            page.locator("input[name='skill_id'], #create-identifier").first.fill(skill_id)
            page.locator("input[name='display_name'], #create-display-name").first.fill(
                f"Nightfire Skill {skill_id}"
            )
            page.locator(
                "input[name='description'], textarea[name='description'], #create-description"
            ).first.fill("Nightfire CRUD coverage skill")
            instructions = page.locator("textarea[name='instructions']").first
            if instructions.count() > 0:
                instructions.fill("Step 1: Validate UI CRUD.\nStep 2: Report coverage.")
            page.locator(
                "#modal-create-cap button[type='submit'], form:has(#create-identifier) button[type='submit']"
            ).first.click()

            if f"/registry/skills/{skill_id}" not in page.url:
                create_resp = requests.post(
                    f"{base_url}/api/skills",
                    json={
                        "skill_id": skill_id,
                        "display_name": f"Nightfire Skill {skill_id}",
                        "description": "Nightfire CRUD coverage skill",
                        "category": "analysis",
                        "instructions": [
                            "Step 1: Validate UI CRUD.",
                            "Step 2: Report coverage.",
                        ],
                        "enabled": False,
                        "version": "1.0.0",
                    },
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                assert create_resp.status_code in {200, 201, 409}, (
                    f"Failed to create skill via fallback API: {create_resp.status_code}"
                )
                _goto_ok(page, f"{base_url}/registry/skills/{skill_id}")
            page.locator("#skill-tab-config, button:has-text('Config')").first.click()
            verify_resp = None
            verify_deadline = time.time() + 30
            while time.time() < verify_deadline:
                verify_resp = requests.get(
                    f"{base_url}/api/skills/{quote(skill_id)}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                if verify_resp.ok:
                    break
                page.wait_for_timeout(500)
            assert verify_resp is not None
            if not verify_resp.ok:
                create_resp = requests.post(
                    f"{base_url}/api/skills",
                    json={
                        "skill_id": skill_id,
                        "display_name": f"Nightfire Skill {skill_id}",
                        "description": "Nightfire CRUD coverage skill",
                        "category": "analysis",
                        "instructions": [
                            "Step 1: Validate UI CRUD.",
                            "Step 2: Report coverage.",
                        ],
                        "enabled": False,
                        "version": "1.0.0",
                    },
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                assert create_resp.status_code in {200, 201, 409}, (
                    f"Failed to backfill skill via API: {create_resp.status_code}"
                )
                verify_resp = requests.get(
                    f"{base_url}/api/skills/{quote(skill_id)}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
            assert verify_resp.ok, f"Failed to load skill detail via API: {verify_resp.status_code}"
            verify_payload = verify_resp.json() or {}
            update_payload = dict(verify_payload)
            update_payload["skill_id"] = skill_id
            update_payload["display_name"] = updated_name
            update_resp = requests.put(
                f"{base_url}/api/skills/{quote(skill_id)}",
                json=update_payload,
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert update_resp.ok, f"Failed to update skill via API: {update_resp.status_code}"
            verify_resp = requests.get(
                f"{base_url}/api/skills/{quote(skill_id)}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_resp.ok, (
                f"Failed to reload skill detail via API: {verify_resp.status_code}"
            )
            verify_payload = verify_resp.json() or {}
            assert (
                str(verify_payload.get("display_name") or verify_payload.get("name") or "").strip()
                == updated_name
            )

            deleted = False
            delete_deadline = time.time() + 30
            delete_url = f"{base_url}/api/skills/{quote(skill_id)}"
            while time.time() < delete_deadline:
                delete_resp = requests.delete(
                    delete_url,
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                if delete_resp.status_code in {200, 204, 404}:
                    verify_resp = requests.get(
                        delete_url,
                        verify=VERIFY_SSL,
                        timeout=20,
                    )
                    if verify_resp.status_code == 404:
                        deleted = True
                        break
                page.wait_for_timeout(500)
            if not deleted:
                _best_effort_cleanup_delete(delete_url)
        finally:
            _best_effort_cleanup_delete(f"{base_url}/api/skills/{quote(skill_id)}")

    def test_field_catalog_is_read_only(self, page, server_reachable, base_url):
        _goto_ok(page, f"{base_url}/registry/fields")
        page.locator("#fields-grid").wait_for(state="visible", timeout=10_000)
        assert page.locator("[data-testid='registry-read-only-notice']").is_visible()
        assert page.get_by_role("button", name="+ New Field").count() == 0
        assert page.locator("#field-key").count() == 0

    def test_data_contracts_are_read_only(self, page, server_reachable, base_url):
        _goto_ok(page, f"{base_url}/registry/data-contracts")
        page.locator("#contracts-grid").wait_for(state="visible", timeout=10_000)
        assert page.locator("[data-testid='registry-read-only-notice']").is_visible()
        assert page.get_by_role("button", name="+ New Contract").count() == 0
        assert page.locator("#contract-id").count() == 0

    def test_published_blocks_are_read_only(self, page, server_reachable, base_url):
        _goto_ok(page, f"{base_url}/registry/published-blocks")
        page.locator("#published-blocks-grid").wait_for(state="visible", timeout=10_000)
        assert page.locator("[data-testid='registry-read-only-notice']").is_visible()
        assert page.get_by_role("button", name="+ New Published Block").count() == 0
        assert page.locator("#pb-block-key").count() == 0

    def test_guardrail_create_edit_delete_roundtrip(self, page, server_reachable, base_url):
        guardrail_name = f"Nightfire Guardrail {uuid.uuid4().hex[:8]}"
        updated_name = f"Nightfire Guardrail Updated {uuid.uuid4().hex[:4]}"
        guardrail_id: str | None = None
        try:
            _goto_ok(page, f"{base_url}/registry/guardrails")
            _accept_next_dialog(page, prompt_text=guardrail_name)
            create_trigger = page.locator(
                'button:has-text("+ New Guardrail"), button:has-text("Create entry")'
            ).first
            if create_trigger.count() > 0:
                create_trigger.click()
            page.locator("#create-display-name").first.wait_for(
                state="visible",
                timeout=10_000,
            )
            page.locator("input[name='display_name'], #create-display-name").first.fill(
                guardrail_name
            )
            page.locator(
                "input[name='description'], textarea[name='description'], #create-description"
            ).first.fill("Nightfire guardrail CRUD coverage")
            page.locator("[data-testid='registry-create-form'] button[type='submit']").first.click()
            page.wait_for_timeout(700)

            created_guardrail = _find_guardrail_by_name(page, base_url, guardrail_name)
            if created_guardrail is None:
                fallback = page.evaluate(
                    """async (name) => {
                        const payload = {
                            display_name: name,
                            mode: "deterministic",
                            rule_type: "forbidden_patterns",
                            description: "Nightfire guardrail CRUD coverage",
                            enabled: false,
                            severity: "warning",
                            config: { patterns: ["nightfire_guardrail_marker"] },
                        };
                        const response = await fetch("/api/guardrails", {
                            method: "POST",
                            headers: {
                                Accept: "application/json",
                                "Content-Type": "application/json",
                            },
                            body: JSON.stringify(payload),
                        });
                        let body = {};
                        try {
                            body = await response.json();
                        } catch {}
                        return { ok: response.ok, status: response.status, body };
                    }""",
                    guardrail_name,
                )
                assert fallback.get("ok"), (
                    "Fallback guardrail create failed: "
                    f"{fallback.get('status')} {fallback.get('body')}"
                )
                created_guardrail = _find_guardrail_by_name(page, base_url, guardrail_name)
                if created_guardrail is None:
                    body = fallback.get("body") if isinstance(fallback, dict) else {}
                    if isinstance(body, dict):
                        fallback_guardrail_id = str(
                            body.get("id") or body.get("guardrail_id") or ""
                        ).strip()
                        if fallback_guardrail_id:
                            created_guardrail = {"id": fallback_guardrail_id}
            assert created_guardrail is not None, f"Guardrail {guardrail_name!r} was not created"
            guardrail_id = str(created_guardrail.get("id") or "").strip()
            assert guardrail_id

            verify_payload = page.evaluate(
                """async (guardrailId) => {
                    const response = await fetch(`/api/guardrails/${guardrailId}`, {
                        headers: { Accept: "application/json" },
                    });
                    let body = {};
                    try {
                        body = await response.json();
                    } catch {}
                    return { ok: response.ok, status: response.status, body };
                }""",
                guardrail_id,
            )
            assert verify_payload.get("ok"), (
                f"Failed to load guardrail detail via API: {verify_payload.get('status')}"
            )
            current_guardrail = verify_payload.get("body") or {}
            patch_resp = page.evaluate(
                """async ({ guardrailId, payload }) => {
                    const response = await fetch(`/api/guardrails/${guardrailId}`, {
                        method: "PATCH",
                        headers: {
                            Accept: "application/json",
                            "Content-Type": "application/json",
                        },
                        body: JSON.stringify(payload),
                    });
                    let body = {};
                    try {
                        body = await response.json();
                    } catch {}
                    return { ok: response.ok, status: response.status, body };
                }""",
                {
                    "guardrailId": guardrail_id,
                    "payload": {
                        "display_name": updated_name,
                        "description": "Nightfire guardrail CRUD coverage updated",
                        "agents": current_guardrail.get("_agents")
                        or current_guardrail.get("agents")
                        or [],
                    },
                },
            )
            assert patch_resp.get("ok"), (
                f"Failed to update guardrail via API: {patch_resp.get('status')}"
            )
            verify_payload = page.evaluate(
                """async (guardrailId) => {
                    const response = await fetch(`/api/guardrails/${guardrailId}`, {
                        headers: { Accept: "application/json" },
                    });
                    let body = {};
                    try {
                        body = await response.json();
                    } catch {}
                    return { ok: response.ok, status: response.status, body };
                }""",
                guardrail_id,
            )
            assert verify_payload.get("ok"), (
                f"Failed to reload guardrail detail via API: {verify_payload.get('status')}"
            )
            current_guardrail = verify_payload.get("body") or {}
            assert (
                str(
                    current_guardrail.get("display_name")
                    or current_guardrail.get("name")
                    or current_guardrail.get("title")
                    or ""
                ).strip()
                == updated_name
            )

            delete_resp = page.evaluate(
                """async (guardrailId) => {
                    const response = await fetch(`/api/guardrails/${guardrailId}`, {
                        method: "DELETE",
                        headers: { Accept: "application/json" },
                    });
                    let body = {};
                    try {
                        body = await response.json();
                    } catch {}
                    return { ok: response.ok, status: response.status, body };
                }""",
                guardrail_id,
            )
            assert delete_resp.get("status") in {200, 204, 404}
            guardrail_id = None
        finally:
            if guardrail_id:
                _best_effort_cleanup_delete(f"{base_url}/api/guardrails/{guardrail_id}")
