from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import suppress

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


def _seed_credentials() -> dict[str, str]:
    resolved = resolve_test_env(
        os.environ,
        lane="ui",
        role=os.getenv("NIGHTFIRE_AUTH_ROLE") or os.getenv("E2E_UI_ROLE"),
        include_dotenv=True,
    )
    return {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
    }


def _json_or_empty(resp: requests.Response) -> dict:
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {}


@pytest.fixture(scope="session")
def nightfire_session() -> tuple[requests.Session, dict[str, str] | None]:
    session = requests.Session()
    creds = _seed_credentials()

    try:
        login_resp = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": creds["username"], "password": creds["password"]},
            verify=VERIFY_SSL,
            timeout=20,
        )
    except requests.RequestException as exc:
        pytest.skip(f"Nightfire auth endpoint unavailable: {exc}")
    if login_resp.status_code == 200:
        csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
        if csrf_token:
            session.headers["x-csrf-token"] = csrf_token
        return session, creds

    if login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
        return session, None

    pytest.fail(
        "Nightfire UI lane requires the seeded credentials from .env; "
        f"login for {creds['username']!r} returned {login_resp.status_code}: {login_resp.text[:200]}"
    )


def _login_browser_if_needed(page, *, base_url: str, creds: dict[str, str] | None) -> None:
    page.goto(f"{base_url}/dashboard", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")

    if "/login" not in page.url and page.query_selector("#login-form") is None:
        return

    if not creds:
        pytest.skip(
            "Nightfire browser smoke requires local auth credentials when /login is enforced"
        )

    toggle = page.query_selector("#local-login-toggle button")
    if toggle is not None:
        try:
            toggle.click()
            page.wait_for_timeout(200)
        except Exception:
            pass

    email = page.query_selector("#email")
    password = page.query_selector("#password")
    if email is None or password is None:
        pytest.skip("Login form is not available for unattended browser auth")

    email.fill(creds["username"])
    password.fill(creds["password"])
    with page.expect_navigation(wait_until="networkidle", timeout=15_000):
        page.click("#login-btn")
    assert "/login" not in page.url


def _find_project_by_name(
    session: requests.Session,
    name: str,
    *,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> dict | None:
    clean_name = str(name or "").strip()
    if not clean_name:
        return None

    deadline = time.time() + max(timeout_seconds, poll_interval)
    last_error: requests.RequestException | None = None
    while time.time() < deadline:
        try:
            resp = session.get(
                f"{BASE_URL}/api/projects",
                params={"search": clean_name, "per_page": 200},
                verify=VERIFY_SSL,
                timeout=20,
            )
            resp.raise_for_status()
            body = _json_or_empty(resp)
            projects = body.get("projects") if isinstance(body.get("projects"), list) else []
            for item in projects:
                if isinstance(item, dict) and str(item.get("name") or "").strip() == clean_name:
                    return item
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(poll_interval)

    if last_error is not None:
        raise AssertionError(
            f"Timed out looking up project {clean_name!r} via /api/projects"
        ) from last_error
    return None


def _wait_for_url_contains(page, expected_fragment: str, *, timeout_ms: int = 15_000) -> None:
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if expected_fragment in page.url:
            return
        page.wait_for_timeout(100)
    raise AssertionError(
        f"Timed out waiting for URL containing {expected_fragment!r}; got {page.url!r}"
    )


def _wait_for_grid_row(page, grid_id: str, match_text: str, *, timeout_ms: int = 15_000) -> None:
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
        timeout=timeout_ms,
    )


def _set_grid_search(page, grid_id: str, query: str, *, timeout_ms: int = 10_000) -> None:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_error: PlaywrightError | None = None
    while time.time() < deadline:
        try:
            page.wait_for_function(
                """(gridId) => {
                    const grid = document.getElementById(gridId);
                    return !!(grid && grid.shadowRoot && grid.shadowRoot.querySelector('.search input'));
                }""",
                arg=grid_id,
                timeout=min(timeout_ms, 2_000),
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

    if last_error is not None:
        raise last_error
    raise AssertionError(f"Timed out setting search for grid {grid_id!r}")


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


def _close_global_search_results(page) -> None:
    for _ in range(3):
        overlay = page.locator("[data-testid='global-search-results']")
        if overlay.count() == 0:
            return
        with suppress(Exception):
            page.locator(
                "[data-testid='global-search-results'] button[aria-label='Close search results']"
            ).click(force=True, timeout=1_000)
        with suppress(Exception):
            page.keyboard.press("Escape")
        with suppress(Exception):
            overlay.wait_for(state="detached", timeout=1_000)
        page.wait_for_timeout(100)


def _first_workflow_id(session: requests.Session) -> str:
    resp = session.get(f"{BASE_URL}/api/workflows", verify=VERIFY_SSL, timeout=20)
    resp.raise_for_status()
    body = _json_or_empty(resp)
    workflows = body if isinstance(body, list) else body.get("workflows", [])
    if not isinstance(workflows, list) or not workflows:
        pytest.skip("No workflows available for nightfire workflow clone smoke")
    first = workflows[0] if isinstance(workflows[0], dict) else {}
    workflow_id = str(first.get("workflow_id") or first.get("id") or "").strip()
    if not workflow_id:
        pytest.skip("Workflow list response did not include a workflow_id")
    return workflow_id


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
    checkboxes = page.get_by_role("checkbox", name=re.compile(r"^Toggle "))
    try:
        checkboxes.first.wait_for(state="visible", timeout=10_000)
    except PlaywrightError:
        pytest.skip("Project edit requires at least one enabled workflow in the catalog")
    if page.get_by_role("checkbox", checked=True, name=re.compile(r"^Toggle ")).count() == 0:
        checkboxes.first.check()


def _extensions_tab_available(page) -> bool:
    return page.locator("button:has-text('Extensions'), [data-tab='extensions']").count() > 0


def _extensions_hub_available(page) -> bool:
    return page.locator("[data-ext-tab='installed'], #extensions-installed-content").count() > 0


class TestNightfireRegression:
    def test_browser_login_form_accepts_seeded_credentials(
        self,
        page,
        server_reachable,
        base_url,
        nightfire_session,
    ):
        _session, creds = nightfire_session
        if not creds:
            pytest.skip("Browser login smoke requires seeded local auth credentials")

        page.context.clear_cookies()
        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        toggle = page.query_selector("#local-login-toggle button")
        if toggle is not None:
            try:
                toggle.click()
                page.wait_for_timeout(200)
            except Exception:
                pass

        page.fill("#email", creds["username"])
        page.fill("#password", creds["password"])
        with page.expect_navigation(wait_until="networkidle", timeout=15_000):
            page.click("#login-btn")

        assert "/login" not in page.url
        assert "/dashboard" in page.url or page.locator("h1").count() > 0

    def test_extensions_surface_lists_cutover_integrations(
        self,
        page,
        server_reachable,
        base_url,
        nightfire_session,
        active_test_role,
    ):
        session, creds = nightfire_session
        _login_browser_if_needed(page, base_url=base_url, creds=creds)

        extensions_resp = session.get(
            f"{base_url}/api/extensions",
            params={"enabled_only": "false"},
            verify=VERIFY_SSL,
            timeout=20,
        )
        if active_test_role == "viewer":
            assert extensions_resp.status_code == 403
            page.goto(f"{base_url}/settings", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            assert "settings" in page.content().lower()
            return

        assert extensions_resp.ok
        payload = extensions_resp.json() or {}
        assert isinstance(payload.get("extensions") or [], list)

        page.goto(f"{base_url}/settings", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)

        body = page.content().lower()
        assert "settings" in body
        if _extensions_tab_available(page):
            page.click("button:has-text('Extensions'), [data-tab='extensions']", timeout=5_000)
            page.wait_for_selector("#extensions-content:not(.hidden)", timeout=15_000)
        else:
            page.goto(f"{base_url}/extensions", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            if _extensions_hub_available(page):
                if page.locator("[data-ext-tab='installed']").count() > 0:
                    page.click("[data-ext-tab='installed']")
                page.wait_for_selector("#extensions-installed-content:not(.hidden)", timeout=15_000)
            else:
                assert "/extensions" in page.url

    def test_project_create_edit_delete_roundtrip(
        self,
        page,
        server_reachable,
        base_url,
        test_project_id,
        nightfire_session,
        active_test_role,
    ):
        if active_test_role == "viewer":
            pytest.skip("Project CRUD roundtrip requires write role")
        session, creds = nightfire_session
        _login_browser_if_needed(page, base_url=base_url, creds=creds)

        created_project_id: str | None = None
        project_name = f"nightfire-ui-{uuid.uuid4().hex[:8]}"
        project_rules_marker = f"nightfire regression marker {uuid.uuid4().hex[:8]}"
        updated_project_name = f"{project_name}-updated"
        try:
            page.goto(f"{base_url}/projects", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.click("button:has-text('New Project')")
            page.wait_for_timeout(300)

            workspace_select = page.locator("#modal-create select[name='workspace_id']").first
            workspace_select.wait_for(state="visible", timeout=10_000)
            workspace_select.select_option(index=1)
            page.fill("#modal-create input[name='name']", project_name)
            assert page.locator("#modal-create input[name='project_root']").count() == 0
            assert page.locator("#modal-create input[name='container_root']").count() == 0
            _select_first_create_workflow(page)

            page.click("#modal-create button[type='submit']")
            page.wait_for_url("**/projects/*", timeout=10_000)
            created_project_id = page.url.rstrip("/").rsplit("/", 1)[-1]
            assert created_project_id

            page.goto(f"{base_url}/projects/{created_project_id}", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_function(
                """([expectedName]) => {
                    const nameInput = document.querySelector("input[name='name']");
                    const workspaceInput = document.querySelector("input[name='workspace_id']");
                    return (
                        !!nameInput &&
                        String(nameInput.value || "").trim() === expectedName &&
                        !!workspaceInput &&
                        String(workspaceInput.value || "").trim().length > 0
                    );
                }""",
                arg=[project_name],
                timeout=10_000,
            )
            page.fill("input[name='name']", updated_project_name)
            if page.locator("textarea[name='description']").count() > 0:
                page.fill("textarea[name='description']", project_rules_marker)
            _ensure_first_edit_workflow_selected(page)
            with page.expect_response(
                lambda resp: (
                    f"/api/projects/{created_project_id}" in resp.url
                    and resp.request.method == "PUT"
                ),
                timeout=15_000,
            ) as update_response:
                page.locator(
                    "button:has-text('Save changes'), button:has-text('Save Project')"
                ).first.click()
            response_body = update_response.value.text()
            assert update_response.value.ok, (
                f"Project update failed with {update_response.value.status}: {response_body}"
            )

            page.wait_for_timeout(500)
            content = page.content().lower()
            project_resp = session.get(
                f"{base_url}/api/projects/{created_project_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert project_resp.ok
            project_payload = project_resp.json()
            assert project_payload.get("name") == updated_project_name
            assert (
                project_rules_marker.lower() in content
                or project_payload.get("description") == project_rules_marker
            )
            if page.locator("textarea[name='description']").count() > 0:
                assert (
                    project_rules_marker
                    in page.locator("textarea[name='description']").input_value()
                )

            page.goto(f"{base_url}/projects", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            session.delete(
                f"{base_url}/api/projects/{created_project_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            created_project_id = None
        finally:
            if created_project_id:
                session.delete(
                    f"{base_url}/api/projects/{created_project_id}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )

    def test_workflow_clone_delete_roundtrip(
        self,
        page,
        server_reachable,
        base_url,
        nightfire_session,
        active_test_role,
    ):
        if active_test_role != "admin":
            pytest.skip("Workflow clone/delete roundtrip is admin-only")
        session, creds = nightfire_session
        _login_browser_if_needed(page, base_url=base_url, creds=creds)

        source_workflow_id = str(
            os.getenv("NIGHTFIRE_SOURCE_WORKFLOW_ID", "") or ""
        ).strip() or _first_workflow_id(session)
        clone_workflow_id = f"nightfire_clone_{uuid.uuid4().hex[:8]}"
        clone_exists = False

        try:
            page.goto(f"{base_url}/registry/workflows", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            try:
                _set_grid_search(page, "workflows-catalog-grid", source_workflow_id)
                _wait_for_grid_row(page, "workflows-catalog-grid", source_workflow_id)
            except Exception:
                pass

            page.once("dialog", lambda dialog: dialog.accept(clone_workflow_id))
            clicked = _grid_click_action(
                page, "workflows-catalog-grid", source_workflow_id, "clone"
            )
            if clicked:
                _wait_for_url_contains(page, f"/registry/workflows/{clone_workflow_id}")
            else:
                clone_resp = session.post(
                    f"{base_url}/api/workflows/{source_workflow_id}/clone",
                    json={"workflow_id": clone_workflow_id},
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                assert clone_resp.ok, (
                    f"Workflow clone action is not available and API fallback failed: "
                    f"{clone_resp.status_code}"
                )
                page.goto(
                    f"{base_url}/registry/workflows/{clone_workflow_id}",
                    wait_until="domcontentloaded",
                )
                page.wait_for_load_state("networkidle")

            page.wait_for_load_state("networkidle")
            clone_exists = True
            verify_clone_resp = session.get(
                f"{base_url}/api/workflows/{clone_workflow_id}",
                verify=VERIFY_SSL,
                timeout=20,
            )
            assert verify_clone_resp.ok, (
                f"Cloned workflow {clone_workflow_id!r} was not available via API: "
                f"{verify_clone_resp.status_code}"
            )

            page.goto(
                f"{base_url}/registry/workflows/{clone_workflow_id}",
                wait_until="domcontentloaded",
            )
            page.wait_for_load_state("networkidle")
            _close_global_search_results(page)
            delete_button = page.locator("[data-workflow-header-action='delete']").first
            if delete_button.count() > 0:
                page.once("dialog", lambda dialog: dialog.accept("DELETE"))
                delete_button.click()
            else:
                delete_resp = session.delete(
                    f"{base_url}/api/workflows/{clone_workflow_id}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                assert delete_resp.ok, (
                    f"Workflow delete fallback failed: "
                    f"{delete_resp.status_code} {delete_resp.text[:300]}"
                )
            delete_deadline = time.time() + 30
            deleted = False
            while time.time() < delete_deadline:
                verify_resp = session.get(
                    f"{base_url}/api/workflows/{clone_workflow_id}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
                if verify_resp.status_code == 404:
                    deleted = True
                    break
                page.wait_for_timeout(500)
            assert deleted, f"Workflow {clone_workflow_id} was not deleted via UI flow"
            if not page.url.endswith("/registry/workflows"):
                with suppress(Exception):
                    page.wait_for_url("**/registry/workflows", timeout=5_000)

            clone_exists = False
        finally:
            if clone_exists:
                session.delete(
                    f"{base_url}/api/workflows/{clone_workflow_id}",
                    verify=VERIFY_SSL,
                    timeout=20,
                )
