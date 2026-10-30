"""Browser acceptance for the workflow builder's edit session (Tasks 4-7).

A real browser against a deployed candidate: Undo/Redo keys on the ordinary
user path, the session through reload and Apply, two tabs, the conflict
choices with a repeated 409, and a normal leave.

Strict prerequisites, never a skip: an explicit ``APP_BASE_URL``, the
candidate's build SHA in ``FS_SMOKE_CANDIDATE_COMMIT`` (checked against
``/health/live``), Playwright with Chromium, and an authenticated admin
session. Run it through the gate:

    APP_BASE_URL=https://<candidate> FS_SMOKE_CANDIDATE_COMMIT=<sha> \\
      ./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests \\
      --test-paths tests/ui/test_workflow_edit_session.py

The keys are pressed with Control (the Linux runner); ⌘Z/⌘Y on macOS must be
checked by hand on the same candidate and reported separately.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests

from tests.ui.conftest import _csrf_headers

pytestmark = [pytest.mark.ui]

UNDO = "Control+KeyZ"
REDO = "Control+Shift+KeyZ"
REDO_Y = "Control+KeyY"


@pytest.fixture(scope="module")
def candidate() -> dict[str, str]:
    base_url = str(os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
    expected_sha = str(os.environ.get("FS_SMOKE_CANDIDATE_COMMIT") or "").strip()
    if not base_url:
        pytest.fail("APP_BASE_URL must name the deployed candidate explicitly")
    if not expected_sha:
        pytest.fail("FS_SMOKE_CANDIDATE_COMMIT must name the candidate build being accepted")
    live = requests.get(f"{base_url}/health/live", timeout=15, verify=False)
    if live.status_code != 200:
        pytest.fail(f"Candidate {base_url} is not live: HTTP {live.status_code}")
    build_sha = str(live.json().get("build_sha") or "")
    if not build_sha or not (
        build_sha.startswith(expected_sha) or expected_sha.startswith(build_sha)
    ):
        pytest.fail(f"Candidate runs {build_sha or 'an unknown build'}, not {expected_sha}")
    return {"base_url": base_url, "build_sha": build_sha}


@pytest.fixture(scope="module")
def api(candidate, ui_auth_session) -> requests.Session:
    session, _verify = ui_auth_session
    if session is None:
        pytest.fail("No authenticated admin session for the candidate")
    return session


@pytest.fixture(scope="module")
def chromium() -> Iterator[Any]:
    # Playwright is required: a missing install fails the fixture with ImportError.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture
def browser_context(chromium, candidate, api) -> Iterator[Any]:
    context = chromium.new_context(
        ignore_https_errors=True, viewport={"width": 1440, "height": 900}
    )
    context.add_cookies(
        [
            {
                "name": item.name,
                "value": item.value,
                "url": candidate["base_url"],
                "secure": candidate["base_url"].startswith("https://"),
                "httpOnly": item.name != "fs_csrf",
            }
            for item in api.cookies
        ]
    )
    yield context
    context.close()


def _post(api: requests.Session, base_url: str, path: str, body: dict) -> dict:
    response = api.post(
        f"{base_url}{path}", json=body, headers=_csrf_headers(api), timeout=30, verify=False
    )
    assert response.status_code < 300, f"{path}: {response.status_code} {response.text}"
    return response.json()


def _revision(api: requests.Session, base_url: str, workflow_id: str) -> int:
    response = api.get(f"{base_url}/api/workflows/{workflow_id}", timeout=30, verify=False)
    assert response.status_code == 200, response.text
    return int(response.json()["revision"])


@pytest.fixture
def workflow(api, candidate) -> Iterator[str]:
    base_url = candidate["base_url"]
    workflow_id = f"edit_session_{uuid.uuid4().hex[:10]}"
    _post(
        api,
        base_url,
        "/api/workflows",
        {"workflow_id": workflow_id, "display_name": "Edit session acceptance", "enabled": False},
    )
    _post(
        api,
        base_url,
        f"/api/workflows/{workflow_id}/changes",
        {
            "expected_revision": _revision(api, base_url, workflow_id),
            "operations": [
                {
                    "op": "create_step",
                    "step": {
                        "step_id": "done",
                        "step_kind": "stop_success",
                        "display_name": "Done",
                    },
                }
            ],
        },
    )
    yield workflow_id
    api.delete(
        f"{base_url}/api/workflows/{workflow_id}",
        headers=_csrf_headers(api),
        timeout=30,
        verify=False,
    )


def _open_builder(context, base_url: str, workflow_id: str):
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.goto(f"{base_url}/registry/workflows/{workflow_id}/builder", wait_until="domcontentloaded")
    page.get_by_role("region", name="Workflow canvas").wait_for()
    return page


def _status(page) -> str:
    return page.get_by_test_id("structure-status").inner_text()


def _expect_unapplied(page, expected: bool) -> None:
    pattern = re.compile("not applied", re.IGNORECASE)
    if expected:
        page.get_by_test_id("structure-status").filter(has_text=pattern).wait_for()
    else:
        page.wait_for_function(
            "() => !/not applied/i.test(document.querySelector('[data-testid=structure-status]')?.textContent || '')"
        )


def _add_stop_step_from_the_palette(page) -> None:
    """The ordinary path: Add step, a palette choice, the drawer's button keeps the focus."""
    page.get_by_role("button", name="Add step").first.click()
    page.get_by_role("option", name=re.compile(r"Stop · success")).first.click()
    update = page.get_by_role("button", name="Update new step")
    if update.count():
        update.click()
    _expect_unapplied(page, True)


def _reload(page, *, expect_prompt: bool) -> None:
    prompts: list[str] = []

    def accept(dialog) -> None:
        prompts.append(dialog.type)
        dialog.accept()

    page.on("dialog", accept)
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("region", name="Workflow canvas").wait_for()
    page.remove_listener("dialog", accept)
    assert ("beforeunload" in prompts) is expect_prompt, prompts


def test_edit_session_through_keys_reload_apply_tabs_conflict_and_leave(
    browser_context, candidate, api, workflow
) -> None:
    base_url = candidate["base_url"]
    page = _open_builder(browser_context, base_url, workflow)

    # Task 4: the keys after the ordinary add path, focus left on the drawer's button.
    _add_stop_step_from_the_palette(page)
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    page.keyboard.press(REDO)
    _expect_unapplied(page, True)
    # A text field keeps its own Undo: the graph does not change.
    search = page.get_by_role("button", name=re.compile("Search canvas")).first
    search.click()
    field = page.get_by_role("textbox", name="Search workflow canvas")
    field.fill("typed")
    page.keyboard.press(UNDO)
    _expect_unapplied(page, True)
    page.keyboard.press("Escape")

    # Task 5/7: a dirty reload asks first, then restores the copy, history and Redo.
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    page.keyboard.press(REDO_Y)
    _expect_unapplied(page, True)
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    _add_stop_step_from_the_palette(page)  # a new edit after Undo ends Redo
    page.get_by_role("region", name="Workflow canvas").click(position={"x": 40, "y": 40})
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    page.keyboard.press(REDO)
    _expect_unapplied(page, True)
    _reload(page, expect_prompt=True)
    _expect_unapplied(page, True)
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    page.keyboard.press(REDO)
    _expect_unapplied(page, True)

    # Apply keeps the history: a clean reload has no prompt, Undo stages the reverse.
    revision_before = _revision(api, base_url, workflow)
    page.get_by_role("button", name="Apply changes").first.click()
    _expect_unapplied(page, False)
    assert _revision(api, base_url, workflow) == revision_before + 1
    _reload(page, expect_prompt=False)
    page.keyboard.press(UNDO)
    _expect_unapplied(page, True)
    assert _revision(api, base_url, workflow) == revision_before + 1  # Undo writes nothing
    page.keyboard.press(REDO)
    _expect_unapplied(page, False)

    # Two tabs: the second has its own session; its Apply makes the first conflict.
    page.keyboard.press(UNDO)
    _expect_unapplied(page, True)
    other = _open_builder(browser_context, base_url, workflow)
    _add_stop_step_from_the_palette(other)
    other.get_by_role("button", name="Apply changes").first.click()
    _expect_unapplied(other, False)
    page.bring_to_front()
    page.evaluate("() => window.dispatchEvent(new Event('focus'))")
    _reload(page, expect_prompt=True)
    dialog = page.get_by_role("dialog", name="Compare your copy with the server")
    dialog.wait_for()
    dialog.get_by_role("region", name="My changes").wait_for()
    dialog.get_by_role("button", name="Decide later").click()
    page.get_by_role("button", name="Compare versions").first.click()
    dialog.wait_for()
    dialog.get_by_role("button", name="Keep my version").click()
    # Someone saves again before the confirmation: 409 and a new comparison, no retry.
    _post(
        api,
        base_url,
        f"/api/workflows/{workflow}/changes",
        {
            "expected_revision": _revision(api, base_url, workflow),
            "operations": [
                {
                    "op": "update_step_config",
                    "step_id": "done",
                    "patch": {"display_name": "Changed again"},
                }
            ],
        },
    )
    revision_conflict = _revision(api, base_url, workflow)
    dialog.get_by_role("button", name="Replace with my version").click()
    dialog.get_by_text(re.compile("Another newer version was saved meanwhile")).wait_for()
    assert _revision(api, base_url, workflow) == revision_conflict
    dialog.get_by_role("button", name="Accept the server version").click()
    dialog.get_by_role("button", name="Discard my copy").click()
    dialog.wait_for(state="detached")
    _expect_unapplied(page, False)
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)  # a new base: the old history ended
    assert _revision(api, base_url, workflow) == revision_conflict

    # A normal leave ends the clean session; the next visit starts from the server.
    page.get_by_role("link", name="Back").first.click()
    page.wait_for_url(re.compile(r"/registry/workflows(?!/[^/]+/builder)"))
    page.goto(f"{base_url}/registry/workflows/{workflow}/builder", wait_until="domcontentloaded")
    page.get_by_role("region", name="Workflow canvas").wait_for()
    page.keyboard.press(UNDO)
    _expect_unapplied(page, False)
    other.close()
