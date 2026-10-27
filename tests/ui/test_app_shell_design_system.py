"""Authenticated acceptance gate for the Design System application shell."""

from __future__ import annotations

import re

import pytest

pytestmark = [pytest.mark.ui]

ACCEPTANCE_WIDTHS = (
    (768, "collapsed", 65),
    (1024, "expanded", 261),
    (1280, "expanded", 261),
    (1440, "expanded", 261),
    (1920, "expanded", 261),
    (2560, "expanded", 261),
)


@pytest.fixture(scope="module", autouse=True)
def _require_admin_authenticated_lane(active_test_role):
    """Keep the representative protected routes in the existing admin UI lane."""
    if active_test_role != "admin":
        pytest.skip("The App Shell acceptance gate requires the admin-authenticated UI lane")


def _attach_runtime_watchers(page):
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def on_console(message):
        text = str(message.text or "").strip()
        if message.type == "error" and text and "favicon.ico" not in text:
            console_errors.append(text)

    page.on("console", on_console)
    return page_errors, console_errors


def _assert_clean_runtime(page_errors: list[str], console_errors: list[str]):
    assert not page_errors, f"Unexpected page errors: {page_errors}"
    assert not console_errors, f"Unexpected console errors: {console_errors}"


def _goto_shell(page, url: str):
    response = page.goto(url, wait_until="domcontentloaded")
    assert response is not None, (
        f"Expected a successful authenticated response for {url}, got {(response.status if response else 'no response')}"
    )
    assert response.ok, (
        f"Expected a successful authenticated response for {url}, got {(response.status if response else 'no response')}"
    )
    layout = page.get_by_test_id("app-shell-layout")
    layout.wait_for(state="visible")
    return layout


@pytest.mark.parametrize(("width", "rail_state", "rail_width"), ACCEPTANCE_WIDTHS)
def test_shell_geometry_and_breakpoint_default(
    page, server_reachable, base_url, width, rail_state, rail_width
):
    page.set_viewport_size({"width": width, "height": 900})
    page_errors, console_errors = _attach_runtime_watchers(page)
    layout = _goto_shell(page, f"{base_url}/projects")

    sidebar = page.get_by_test_id("app-shell-sidebar-frame")
    header = page.get_by_test_id("app-shell-header-frame")
    main = page.locator("main.fs-shell-main")

    assert layout.get_attribute("data-rail-state") == rail_state
    sidebar_box = sidebar.bounding_box()
    header_box = header.bounding_box()
    main_box = main.bounding_box()
    assert sidebar_box
    assert header_box
    assert main_box
    assert round(sidebar_box["width"]) == rail_width
    assert round(header_box["x"]) == rail_width
    assert round(main_box["x"]) >= rail_width
    assert round(header_box["height"]) == 64
    assert round(sidebar_box["y"]) == 0
    assert round(sidebar_box["height"]) == 900
    _assert_clean_runtime(page_errors, console_errors)


def test_explicit_rail_preference_wins_and_persists(page, server_reachable, base_url):
    page.set_viewport_size({"width": 1440, "height": 900})
    layout = _goto_shell(page, f"{base_url}/projects")
    page.evaluate("localStorage.setItem('fsb-rail', 'collapsed')")
    page.reload(wait_until="domcontentloaded")
    layout.wait_for(state="visible")
    assert layout.get_attribute("data-rail-state") == "collapsed"

    # The rail edge shares the name; Header's PanelLeft is the control under test.
    page.get_by_role("banner").get_by_role("button", name="Toggle sidebar").click()
    assert layout.get_attribute("data-rail-state") == "expanded"
    assert page.evaluate("localStorage.getItem('fsb-rail')") == "expanded"

    page.reload(wait_until="domcontentloaded")
    layout.wait_for(state="visible")
    assert layout.get_attribute("data-rail-state") == "expanded"


@pytest.mark.parametrize(
    "path",
    (
        "/projects",
        "/jobs",
        "/registry/workflows",
        "/settings",
    ),
)
def test_shell_wraps_representative_routes_without_runtime_errors(
    page, server_reachable, base_url, path
):
    page_errors, console_errors = _attach_runtime_watchers(page)
    _goto_shell(page, f"{base_url}{path}")

    assert page.get_by_role("banner").is_visible()
    current = page.locator("nav[aria-label='Primary'] [aria-current='page']")
    assert current.count() == 1
    _assert_clean_runtime(page_errors, console_errors)


def test_keyboard_search_and_menu_dismissal(page, server_reachable, base_url):
    _goto_shell(page, f"{base_url}/registry/workflows")
    page.keyboard.press("Control+K")
    dialog = page.get_by_role("dialog", name="Global search")
    dialog.wait_for(state="visible")
    combobox = dialog.get_by_role("combobox", name="Global search")
    assert combobox.evaluate("element => element === document.activeElement")

    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")

    workspace = page.get_by_role("button", name="Workspace")
    workspace.click()
    menu = page.get_by_role("menu", name="Workspace menu")
    menu.wait_for(state="visible")
    page.keyboard.press("Escape")
    menu.wait_for(state="hidden")
    assert workspace.evaluate("element => element === document.activeElement")


def test_header_does_not_duplicate_sidebar_controls(page, server_reachable, base_url):
    _goto_shell(page, f"{base_url}/projects")
    header = page.get_by_role("banner")
    assert header.get_by_role("button", name="Workspace").count() == 0
    assert header.get_by_role("button", name="Account menu").count() == 0
    assert header.get_by_role("button", name=re.compile("theme", re.I)).count() == 0
    assert header.get_by_role("button", name="Open approvals").count() == 1
    assert header.get_by_role("button", name="Ask AI").count() == 1
