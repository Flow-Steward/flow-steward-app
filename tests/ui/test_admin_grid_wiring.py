"""
Behavioral UI tests for admin page platform-grid wiring.

Verifies that:
- Admin pages render platform-grid / platform-drawer components (migration complete)
- Shadow DOM action buttons are accessible and clickable
- platform-action event handlers wire to correct navigation / API calls
- Token revoke, dashboard job actions, project/user row actions all fire

Run (requires live server + Playwright):
    pytest -q tests/ui/test_admin_grid_wiring.py --timeout=60
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


@pytest.fixture(autouse=True)
def _admin_only_suite(active_test_role):
    if active_test_role != "admin":
        pytest.skip("Admin grid wiring suite is admin-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_grid(page, grid_id: str, timeout: int = 10_000):
    """Wait for a platform-grid element to be attached to the DOM."""
    page.wait_for_selector(f"#{grid_id}", state="attached", timeout=timeout)


def _grid_row_count(page, grid_id: str) -> int:
    """Count rows rendered inside a platform-grid's shadow DOM."""
    return page.evaluate(
        """(gridId) => {
            const el = document.getElementById(gridId);
            if (!el || !el.shadowRoot) return 0;
            return el.shadowRoot.querySelectorAll('tbody tr').length;
        }""",
        grid_id,
    )


def _grid_action_buttons(page, grid_id: str, action_id: str) -> int:
    """Count action buttons with a given data-row-action in grid shadow DOM."""
    return page.evaluate(
        """([gridId, actionId]) => {
            const el = document.getElementById(gridId);
            if (!el || !el.shadowRoot) return 0;
            return el.shadowRoot.querySelectorAll(
                `button[data-row-action="${actionId}"]`
            ).length;
        }""",
        [grid_id, action_id],
    )


def _fire_grid_action(page, grid_id: str, action_id: str, row_index: int = 0) -> bool:
    """Click the nth action button matching action_id in the grid shadow DOM.
    Returns True if the button was found and clicked."""
    return page.evaluate(
        """([gridId, actionId, idx]) => {
            const el = document.getElementById(gridId);
            if (!el || !el.shadowRoot) return false;
            const btns = el.shadowRoot.querySelectorAll(
                `button[data-row-action="${actionId}"]`
            );
            if (!btns[idx]) return false;
            btns[idx].click();
            return true;
        }""",
        [grid_id, action_id, row_index],
    )


def _drawer_is_open(page, drawer_id: str) -> bool:
    """Check if a platform-drawer has the 'open' class (visible)."""
    return page.evaluate(
        """(drawerId) => {
            const el = document.getElementById(drawerId);
            return el ? el.classList.contains('open') : false;
        }""",
        drawer_id,
    )


def _goto_or_skip_not_found(page, url: str, *, timeout: int = 20_000):
    """Open a URL and skip this test if the page is not implemented (404)."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            resp = page.goto(
                url, wait_until="domcontentloaded", timeout=timeout if attempt == 0 else timeout * 2
            )
            break
        except PlaywrightTimeoutError as exc:
            last_error = exc
            if attempt == 1:
                raise
            page.wait_for_timeout(500)
    else:
        if last_error:
            raise last_error
        raise AssertionError(f"Failed to navigate to {url}")
    if resp and resp.status == 404:
        pytest.skip(f"Page not implemented: {url}")
    return resp


def _seed_jobs_grid_with_bulk_actions(page) -> bool:
    """Inject deterministic local rows into jobs-grid with bulk actions enabled."""
    return page.evaluate(
        """() => {
            const grid = document.getElementById('jobs-grid');
            if (!grid) return false;
            const items = Array.from({ length: 3 }, (_value, index) => ({
                job_id: String(index + 1),
                summary: `Job ${index + 1}`,
            }));
            grid.config = {
                component_id: 'jobs-grid-bulk-test',
                id_field: 'job_id',
                columns: [
                    { key: 'job_id', label: '#', type: 'text' },
                    { key: 'summary', label: 'Summary', type: 'text' },
                ],
                bulk_actions: [
                    { action_id: 'bulk_cancel', label: 'Cancel Selected', variant: 'danger' },
                    { action_id: 'bulk_delete', label: 'Delete Selected', variant: 'ghost' },
                ],
                pagination: { page_size_default: 10, page_size_options: [10, 25] },
                empty_state: { title: 'No jobs found' },
            };
            grid.setData({ items, total: items.length });
            return !!grid.shadowRoot;
        }"""
    )


# ---------------------------------------------------------------------------
# Dashboard — attention board grid wiring
# ---------------------------------------------------------------------------


class TestDashboardGridWiring:
    """Dashboard renders either the chat-first command center or attention board.

    No System Health panel, no Activity Feed, no Blocked/Done/Acknowledged lanes.
    """

    @staticmethod
    def _is_ai_shell(page) -> bool:
        return page.query_selector("[data-testid='cmd-center']") is not None

    def test_dashboard_renders_lane_grids(self, page, server_reachable, base_url):
        """Dashboard renders attention lanes or command center."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        if self._is_ai_shell(page):
            el = page.query_selector("[data-testid='cmd-center']")
            assert el is not None, "AI shell dashboard must have cmd-center"
        else:
            for lane_id in ["lane-needs-attention", "lane-working"]:
                el = page.query_selector(f"#{lane_id}")
                assert el is not None, f"Dashboard must have <platform-grid id='{lane_id}'>"

    def test_dashboard_no_retired_lanes(self, page, server_reachable, base_url):
        """Dashboard must not render retired Blocked, Done, or Acknowledged lanes."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        for stale_id in ["lane-blocked", "lane-done", "lane-acknowledged"]:
            el = page.query_selector(f"#{stale_id}")
            assert el is None, f"Dashboard must NOT have retired <platform-grid id='{stale_id}'>"

    def test_dashboard_kpi_strip_renders(self, page, server_reachable, base_url):
        """Dashboard renders KPI strip or command center."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        if self._is_ai_shell(page):
            el = page.query_selector("[data-testid='cmd-center']")
            assert el is not None, "AI shell dashboard must have cmd-center"
        else:
            el = page.query_selector("#kpi-strip")
            assert el is not None, "Dashboard must have <platform-stats-row id='kpi-strip'>"

    def test_dashboard_no_health_panel(self, page, server_reachable, base_url):
        """Dashboard must NOT render System Health panel (moved to Settings)."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        el = page.query_selector("#health-panel")
        assert el is None, "Dashboard must NOT have health-panel (moved to Settings)"

    def test_dashboard_no_activity_log(self, page, server_reachable, base_url):
        """Dashboard must NOT render Activity Feed (removed — duplicates attention board)."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        el = page.query_selector("#activity-log")
        assert el is None, "Dashboard must NOT have activity-log (removed)"

    def test_dashboard_recent_completions_exists(self, page, server_reachable, base_url):
        """Dashboard renders recent completions or command center."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        if self._is_ai_shell(page):
            el = page.query_selector("[data-testid='cmd-center']")
            assert el is not None, "AI shell dashboard must have cmd-center"
        else:
            el = page.query_selector("#recent-completions")
            assert el is not None, "Dashboard must have #recent-completions section"


# ---------------------------------------------------------------------------
# Projects page — platform-grid wiring
# ---------------------------------------------------------------------------


class TestProjectsGridWiring:
    def test_projects_renders_platform_grid(self, page, server_reachable, base_url):
        """Projects page renders a platform-grid for project listing."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        _wait_for_grid(page, "projects-grid")
        el = page.query_selector("#projects-grid")
        assert el is not None, "Projects must have <platform-grid id='projects-grid'>"

    def test_projects_grid_has_shadow_root(self, page, server_reachable, base_url):
        """Projects platform-grid has a shadow root (component rendered)."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)  # allow JS to configure grid
        has_shadow = page.evaluate(
            """() => {
                const el = document.getElementById('projects-grid');
                return el ? el.shadowRoot !== null : false;
            }"""
        )
        assert has_shadow, "projects-grid must have an attached shadow root"

    def test_projects_grid_action_buttons_use_correct_id(self, page, server_reachable, base_url):
        """Grid action buttons use data-row-action attribute (not onclick)."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)
        view_count = _grid_action_buttons(page, "projects-grid", "view")
        edit_count = _grid_action_buttons(page, "projects-grid", "edit")
        # At least one of view or edit should exist if there are any projects
        row_count = _grid_row_count(page, "projects-grid")
        if row_count > 0:
            assert view_count > 0 or edit_count > 0, (
                "projects-grid must render view/edit action buttons via data-row-action"
            )

    def test_projects_grid_view_action_navigates(self, page, server_reachable, base_url):
        """Clicking 'view' action in projects-grid navigates to project detail."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)
        row_count = _grid_row_count(page, "projects-grid")
        if row_count == 0:
            pytest.skip("No projects in grid to test navigation")

        # Intercept navigation — we just verify that clicking 'view' starts navigation
        with page.expect_navigation(timeout=15000, wait_until="commit"):
            clicked = _fire_grid_action(page, "projects-grid", "view", row_index=0)
        assert clicked, "view button must exist and be clickable"
        assert "/projects/" in page.url, (
            f"view action should navigate to /projects/<id>, got: {page.url}"
        )


# ---------------------------------------------------------------------------
# Jobs page — platform-grid wiring
# ---------------------------------------------------------------------------


class TestJobsGridWiring:
    def test_jobs_page_renders_grid(self, page, server_reachable, base_url):
        """Jobs page renders platform-grid for table view."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)
        el = page.query_selector("#jobs-grid")
        assert el is not None, "Jobs page must have <platform-grid id='jobs-grid'>"

    def test_jobs_page_uses_single_grid_surface(self, page, server_reachable, base_url):
        """Jobs page exposes the current single-grid surface and no stale board-lane grids."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        table_view = page.query_selector("#jobs-table-view")
        assert table_view is not None, "Jobs page must keep the current #jobs-table-view container"
        stale_lane_ids = [
            lane_id
            for lane_id in ["board-needs-attention", "board-running", "board-blocked", "board-done"]
            if page.query_selector(f"#{lane_id}") is not None
        ]
        assert not stale_lane_ids, (
            f"Jobs page should not render stale board-lane grids: {stale_lane_ids}"
        )

    def test_jobs_grid_has_shadow_root(self, page, server_reachable, base_url):
        """Jobs platform-grid has a shadow root after JS initialization."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(2000)
        has_shadow = page.evaluate(
            """() => {
                const el = document.getElementById('jobs-grid');
                return el ? el.shadowRoot !== null : false;
            }"""
        )
        assert has_shadow, "jobs-grid must have an attached shadow root"

    def test_jobs_grid_action_buttons_use_data_attribute(self, page, server_reachable, base_url):
        """Jobs grid action buttons use data-row-action (not inline onclick)."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(2000)
        row_count = _grid_row_count(page, "jobs-grid")
        if row_count == 0:
            pytest.skip("No jobs rendered in grid")
        view_count = _grid_action_buttons(page, "jobs-grid", "view")
        assert view_count > 0, "jobs-grid must render 'view' action buttons via data-row-action"

    def test_jobs_grid_no_inline_onclick_in_shadow(self, page, server_reachable, base_url):
        """Jobs grid shadow DOM must not contain inline onclick attributes."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(2000)
        has_onclick = page.evaluate(
            """() => {
                const el = document.getElementById('jobs-grid');
                if (!el || !el.shadowRoot) return false;
                const buttons = el.shadowRoot.querySelectorAll('[onclick]');
                return buttons.length > 0;
            }"""
        )
        assert not has_onclick, (
            "jobs-grid shadow DOM must not use inline onclick — actions use data-row-action"
        )

    def test_jobs_grid_client_side_pagination_and_page_size_work(
        self, page, server_reachable, base_url
    ):
        """Jobs grid must paginate local setData payloads without getting stuck in loading state."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid) return false;
                const items = Array.from({ length: 35 }, (_value, index) => ({
                    job_id: String(index + 1),
                    summary: `Job ${index + 1}`,
                }));
                grid.config = {
                    component_id: 'jobs-grid-local-pagination-test',
                    id_field: 'job_id',
                    columns: [
                        { key: 'job_id', label: '#', type: 'text' },
                        { key: 'summary', label: 'Summary', type: 'text' },
                    ],
                    pagination: { page_size_default: 10, page_size_options: [10, 25, 50] },
                    empty_state: { title: 'No jobs found' },
                };
                grid.setData({ items, total: items.length });
                return !!grid.shadowRoot;
            }"""
        )
        assert configured, "jobs-grid must be configurable for client-side pagination checks"

        def read_grid_state():
            return page.evaluate(
                """() => {
                    const grid = document.getElementById('jobs-grid');
                    if (!grid || !grid.shadowRoot) return null;
                    const rowEls = Array.from(grid.shadowRoot.querySelectorAll('tbody tr'));
                    const firstRow = rowEls[0] ? rowEls[0].querySelector('td')?.textContent?.trim() || '' : '';
                    const footer = grid.shadowRoot.querySelector('.footer-left span')?.textContent?.trim() || '';
                    const pageInfo = grid.shadowRoot.querySelector('.page-info')?.textContent?.trim() || '';
                    const loading = !!grid.shadowRoot.querySelector('.loading-overlay');
                    return {
                        rowCount: rowEls.length,
                        firstRow,
                        footer,
                        pageInfo,
                        loading,
                    };
                }"""
            )

        initial = read_grid_state()
        assert initial["rowCount"] == 10
        assert initial["firstRow"] == "1"
        assert initial["footer"] == "1-10 of 35"
        assert initial["pageInfo"] == "Page 1 of 4"
        assert not initial["loading"], (
            "Local grid pagination must not show Loading... on initial render"
        )

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const next = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('button[data-page="next"]')
                    : null;
                if (next) next.click();
            }"""
        )
        page.wait_for_timeout(150)

        second_page = read_grid_state()
        assert second_page["rowCount"] == 10
        assert second_page["firstRow"] == "11"
        assert second_page["footer"] == "11-20 of 35"
        assert second_page["pageInfo"] == "Page 2 of 4"
        assert not second_page["loading"], "Changing page must not leave the grid in loading state"

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const select = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('[data-role="page-size"]')
                    : null;
                if (!select) return;
                select.value = '25';
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(150)

        resized = read_grid_state()
        assert resized["rowCount"] == 25
        assert resized["firstRow"] == "1"
        assert resized["footer"] == "1-25 of 35"
        assert resized["pageInfo"] == "Page 1 of 2"
        assert not resized["loading"], (
            "Changing items per page must re-render locally, not enter loading"
        )

    def test_jobs_grid_bulk_selection_ui_and_select_all_work(
        self, page, server_reachable, base_url
    ):
        """Jobs grid must show row selection UI, bulk bar, and select-all behavior when bulk actions exist."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = _seed_jobs_grid_with_bulk_actions(page)
        assert configured, "jobs-grid must support deterministic bulk-selection setup"

        def read_bulk_state():
            return page.evaluate(
                """() => {
                    const grid = document.getElementById('jobs-grid');
                    if (!grid || !grid.shadowRoot) return null;
                    const rowCheckboxes = Array.from(
                        grid.shadowRoot.querySelectorAll('.row-checkbox[data-row-id]')
                    );
                    const bulkButtons = Array.from(
                        grid.shadowRoot.querySelectorAll('.bulk-bar [data-bulk-action]')
                    ).map(btn => btn.textContent.trim());
                    return {
                        rowCheckboxCount: rowCheckboxes.length,
                        checkedRowCount: rowCheckboxes.filter(cb => cb.checked).length,
                        hasSelectAll: !!grid.shadowRoot.querySelector('[data-role="select-all"]'),
                        hasBulkBar: !!grid.shadowRoot.querySelector('.bulk-bar'),
                        bulkButtons,
                    };
                }"""
            )

        initial = read_bulk_state()
        assert initial["rowCheckboxCount"] == 3
        assert initial["hasSelectAll"], "jobs-grid must render a header select-all checkbox"
        assert not initial["hasBulkBar"], (
            "bulk bar must stay hidden until at least one row is selected"
        )

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const row = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="1"]')
                    : null;
                if (row) row.click();
            }"""
        )
        page.wait_for_timeout(150)

        selected = read_bulk_state()
        assert selected["checkedRowCount"] == 1
        assert selected["hasBulkBar"], "selecting a row must reveal the bulk action bar"
        assert selected["bulkButtons"] == ["Cancel Selected", "Delete Selected"]

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const selectAll = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('[data-role="select-all"]')
                    : null;
                if (selectAll) selectAll.click();
            }"""
        )
        page.wait_for_timeout(150)

        select_all = read_bulk_state()
        assert select_all["checkedRowCount"] == 3, "select-all must select the whole visible page"
        assert select_all["hasBulkBar"], "bulk bar must remain visible after select-all"

    def test_jobs_grid_bulk_action_emits_scope_and_item_ids(self, page, server_reachable, base_url):
        """Jobs grid bulk action must emit scope='bulk' with selected item_ids."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = _seed_jobs_grid_with_bulk_actions(page)
        assert configured, "jobs-grid must support deterministic bulk-event setup"

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                window.__jobsBulkEvents = [];
                window.confirm = () => false;
                grid.addEventListener('platform-action', (e) => {
                    const detail = e.detail || {};
                    if (detail.scope === 'bulk') {
                        window.__jobsBulkEvents.push({
                            action_id: detail.action_id,
                            scope: detail.scope,
                            item_ids: Array.isArray(detail.item_ids) ? detail.item_ids.slice() : [],
                        });
                    }
                });
            }"""
        )

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="1"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const second = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="2"]')
                    : null;
                if (second) second.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const bulk = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('[data-bulk-action="bulk_cancel"]')
                    : null;
                if (bulk) bulk.click();
            }"""
        )
        page.wait_for_timeout(150)

        event = page.evaluate("() => window.__jobsBulkEvents && window.__jobsBulkEvents[0]")
        assert event is not None, "clicking a jobs bulk action must emit a bulk event"
        assert event["action_id"] == "bulk_cancel"
        assert event["scope"] == "bulk"
        assert sorted(event["item_ids"]) == ["1", "2"]

    def test_jobs_grid_numeric_ids_preserve_checked_state(self, page, server_reachable, base_url):
        """Jobs grid selection must stay visually checked when row ids are numeric."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid) return false;
                grid.config = {
                    component_id: 'jobs-grid-numeric-selection-test',
                    id_field: 'job_id',
                    columns: [
                        { key: 'job_id', label: '#', type: 'text' },
                        { key: 'summary', label: 'Summary', type: 'text' },
                    ],
                    bulk_actions: [
                        { action_id: 'bulk_cancel', label: 'Cancel Selected', variant: 'danger' },
                    ],
                    pagination: { page_size_default: 10 },
                    empty_state: { title: 'No jobs found' },
                };
                grid.setData({
                    items: [
                        { job_id: 101, summary: 'First' },
                        { job_id: 102, summary: 'Second' },
                    ],
                    total: 2,
                });
                return !!grid.shadowRoot;
            }"""
        )
        assert configured, "jobs-grid must be configurable for numeric selection checks"

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="101"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                const rowCheckboxes = Array.from(grid.shadowRoot.querySelectorAll('.row-checkbox[data-row-id]'));
                return {
                    checkedRowCount: rowCheckboxes.filter(cb => cb.checked).length,
                    bulkCount: grid.shadowRoot.querySelector('.bulk-count')?.textContent?.trim() || '',
                };
            }"""
        )
        assert state["checkedRowCount"] == 1, (
            "Selecting a numeric-id row must leave its checkbox checked after re-render"
        )
        assert state["bulkCount"] == "1 selected"

    def test_jobs_grid_setdata_prunes_selection_for_removed_rows(
        self, page, server_reachable, base_url
    ):
        """Jobs grid must drop stale selection when setData removes previously selected rows."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid) return false;
                grid.config = {
                    component_id: 'jobs-grid-selection-prune-test',
                    id_field: 'job_id',
                    columns: [
                        { key: 'job_id', label: '#', type: 'text' },
                        { key: 'summary', label: 'Summary', type: 'text' },
                    ],
                    bulk_actions: [
                        { action_id: 'bulk_cancel', label: 'Cancel Selected', variant: 'danger' },
                    ],
                    pagination: { page_size_default: 10 },
                    empty_state: { title: 'No jobs found' },
                };
                grid.setData({
                    items: [
                        { job_id: 201, summary: 'First' },
                        { job_id: 202, summary: 'Second' },
                    ],
                    total: 2,
                });
                return !!grid.shadowRoot;
            }"""
        )
        assert configured, "jobs-grid must be configurable for selection prune checks"

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="201"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                grid.setData({
                    items: [
                        { job_id: 202, summary: 'Second' },
                    ],
                    total: 1,
                });
            }"""
        )
        page.wait_for_timeout(150)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                return {
                    checkedRowCount: grid.shadowRoot.querySelectorAll('.row-checkbox[data-row-id]:checked').length,
                    hasBulkBar: !!grid.shadowRoot.querySelector('.bulk-bar'),
                };
            }"""
        )
        assert state["checkedRowCount"] == 0, (
            "Selection for removed rows must be pruned when new data no longer contains them"
        )
        assert not state["hasBulkBar"], (
            "Bulk bar must disappear after setData removes the selected rows"
        )

    def test_jobs_page_bulk_action_clears_selection_after_completion(
        self, page, server_reachable, base_url
    ):
        """Jobs page bulk actions must clear selection after the action finishes."""
        page.route(
            "**/api/jobs/301/cancel",
            lambda route: route.fulfill(status=200, json={"ok": True}),
        )
        page.route(
            "**/api/jobs?*",
            lambda route: route.fulfill(
                status=200,
                json={"jobs": [], "pagination": {"page": 1, "per_page": 200, "total": 0}},
            ),
        )
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || typeof grid.setData !== 'function') return false;
                grid.setData([
                    { job_id: 301, project_id: 1, project_name: 'Demo', task_summary: 'First job', display_summary: 'First job', status: 'awaiting_approval' },
                    { job_id: 302, project_id: 1, project_name: 'Demo', task_summary: 'Second job', display_summary: 'Second job', status: 'awaiting_approval' },
                ]);
                window.confirm = () => true;
                window.alert = () => {};
                return true;
            }"""
        )
        assert configured, "jobs grid must be available for bulk selection reset checks"

        page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="301"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        with page.expect_response(
            lambda response: response.request.method == "POST"
            and "/api/jobs/301/cancel" in response.url
            and response.status == 200,
            timeout=10_000,
        ):
            page.evaluate(
                """() => {
                const grid = document.getElementById('jobs-grid');
                grid.dispatchEvent(new CustomEvent('platform-action', {
                    bubbles: true,
                    detail: {scope: 'bulk', action_id: 'bulk_cancel', item_ids: ['301']},
                }));
            }"""
            )
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return false;
                return grid.shadowRoot.querySelectorAll('.row-checkbox[data-row-id]:checked').length === 0;
            }""",
            timeout=5000,
        )

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                return {
                    checkedRowCount: grid.shadowRoot.querySelectorAll('.row-checkbox[data-row-id]:checked').length,
                    hasBulkBar: !!grid.shadowRoot.querySelector('.bulk-bar'),
                };
            }"""
        )
        assert state["checkedRowCount"] == 0, "Bulk action completion must clear checked rows"
        assert not state["hasBulkBar"], "Bulk bar must disappear after a jobs bulk action finishes"

    def test_jobs_page_bootstrap_rows_render_when_api_refresh_fails(
        self, page, server_reachable, base_url
    ):
        """Jobs page should render server-bootstrapped rows even if /api/jobs fails in the browser."""
        page.route("**/api/jobs*", lambda route: route.abort())
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                const status = document.getElementById('filter-status');
                return {
                    rowCount: grid && grid.shadowRoot ? grid.shadowRoot.querySelectorAll('tbody tr').length : 0,
                    options: status ? Array.from(status.options).map(o => o.value) : [],
                    errorText: document.getElementById('jobs-load-error')?.textContent?.trim() || '',
                };
            }"""
        )

        assert state["rowCount"] > 0, (
            "Jobs page must keep rendering the server bootstrap rows when the API refresh fails"
        )
        assert len(state["options"]) > 1, (
            "Jobs page status filter must still show live status options from the bootstrap rows"
        )
        assert "Could not refresh jobs. Showing last available data." in state["errorText"], (
            "Jobs page must explain that it preserved bootstrap rows after a refresh failure"
        )

    def test_jobs_page_status_filter_matches_exact_status_values(
        self, page, server_reachable, base_url
    ):
        """Jobs page status filter must match concrete statuses like failed/awaiting_approval."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                if (typeof _rebuildStatusFilter !== 'function' || typeof renderJobs !== 'function') return false;
                window._allJobs = [
                    { job_id: '1', project_id: 1, project_name: 'Demo', task_summary: 'Failed job', status: 'failed' },
                    { job_id: '2', project_id: 1, project_name: 'Demo', task_summary: 'Approval job', status: 'awaiting_approval' },
                    { job_id: '3', project_id: 1, project_name: 'Demo', task_summary: 'Running job', status: 'in_progress' },
                ];
                _rebuildStatusFilter();
                const filter = document.getElementById('filter-status');
                if (!filter) return false;
                filter.value = 'failed';
                renderJobs();
                return true;
            }"""
        )
        assert configured, "Jobs page must expose the exact-status filter helpers"

        page.wait_for_timeout(150)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                const rows = Array.from(grid.shadowRoot.querySelectorAll('tbody tr'));
                const texts = rows.map((row) => row.textContent || '');
                const options = Array.from(document.getElementById('filter-status').options).map((opt) => ({
                    value: opt.value,
                    label: opt.textContent || '',
                }));
                return { rowCount: rows.length, texts, options };
            }"""
        )
        assert state is not None
        assert state["rowCount"] == 1, "Filtering by failed should keep only failed rows"
        assert any("Failed" in text for text in state["texts"]), (
            "Filtered grid should still show the failed job"
        )
        assert any(opt["value"] == "awaiting_approval" for opt in state["options"]), (
            "Status filter should include concrete live statuses like awaiting_approval"
        )

    def test_jobs_page_workflow_filter_matches_exact_workflow_values(
        self, page, server_reachable, base_url
    ):
        """Jobs page workflow filter must combine cleanly with status filtering."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                if (typeof _syncJobFiltersAndGrid !== 'function' || typeof renderJobs !== 'function') return false;
                window._workflowDisplayNames = {
                    weekly_digest: 'Weekly Digest',
                    weekly_report: 'Weekly Report',
                };
                window._allJobs = [
                    { job_id: '1', project_id: 1, project_name: 'Demo', task_summary: 'Weekly Digest completed', status: 'completed', selected_workflow: 'weekly_digest' },
                    { job_id: '2', project_id: 1, project_name: 'Demo', task_summary: 'Weekly Report failed', status: 'failed', selected_workflow: 'weekly_report' },
                    { job_id: '3', project_id: 1, project_name: 'Demo', task_summary: 'Weekly Report completed', status: 'completed', selected_workflow: 'weekly_report' },
                ];
                _syncJobFiltersAndGrid();
                const workflow = document.getElementById('filter-workflow');
                const status = document.getElementById('filter-status');
                if (!workflow || !status) return false;
                workflow.value = 'weekly_report';
                status.value = 'completed';
                renderJobs();
                return true;
            }"""
        )
        assert configured, "Jobs page must expose workflow filter helpers"

        page.wait_for_timeout(150)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                const rows = Array.from(grid.shadowRoot.querySelectorAll('tbody tr'));
                const texts = rows.map((row) => row.textContent || '');
                const options = Array.from(document.getElementById('filter-workflow').options).map((opt) => ({
                    value: opt.value,
                    label: opt.textContent || '',
                }));
                return { rowCount: rows.length, texts, options };
            }"""
        )
        assert state is not None
        assert state["rowCount"] == 1, (
            "Combined workflow+status filters should keep only matching rows"
        )
        assert any("Weekly Report completed" in text for text in state["texts"]), (
            "Workflow filter should keep the completed job from the requested workflow"
        )
        assert any(opt["value"] == "weekly_digest" for opt in state["options"]), (
            "Workflow filter should include weekly_digest when it appears in live rows"
        )
        assert any(opt["value"] == "weekly_report" for opt in state["options"]), (
            "Workflow filter should include weekly_report when it appears in live rows"
        )

    def test_jobs_page_workflow_filter_lists_registry_workflows_without_matching_rows(
        self, page, server_reachable, base_url
    ):
        """Jobs workflow filter should show all known workflows, not only those present in loaded jobs."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                if (typeof _syncJobFiltersAndGrid !== 'function') return false;
                window._workflowDisplayNames = {
                    weekly_digest: 'Weekly Digest',
                    weekly_report: 'Weekly Report',
                    nightly_regression: 'Nightly Regression',
                };
                window._allJobs = [
                    { job_id: '1', project_id: 1, project_name: 'Demo', task_summary: 'Weekly Digest completed', status: 'completed', selected_workflow: 'weekly_digest' },
                ];
                _syncJobFiltersAndGrid();
                return true;
            }"""
        )
        assert configured, (
            "Jobs page must be able to rebuild workflow options from registry metadata"
        )

        state = page.evaluate(
            """() => {
                return Array.from(document.getElementById('filter-workflow').options).map((opt) => ({
                    value: opt.value,
                    label: opt.textContent || '',
                }));
            }"""
        )
        values = [opt["value"] for opt in state]
        assert "weekly_digest" in values
        assert "weekly_report" in values
        assert "nightly_regression" in values, (
            "Workflow filter should include registry workflows even when no current job uses them"
        )

    def test_jobs_page_grid_shows_workflow_column_instead_of_phase(
        self, page, server_reachable, base_url
    ):
        """Jobs grid should display the selected workflow in the table header/body instead of phase."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                if (typeof renderJobs !== 'function') return false;
                window._workflowDisplayNames = {
                    weekly_digest: 'Weekly Digest',
                };
                window._allJobs = [
                    {
                        job_id: '1',
                        project_id: 1,
                        project_name: 'Demo',
                        task_summary: 'Weekly Digest completed',
                        status: 'completed',
                        selected_workflow: 'weekly_digest',
                        current_phase: 'draft',
                        current_agent: 'writer',
                    },
                ];
                renderJobs();
                return true;
            }"""
        )
        assert configured, "Jobs page must expose renderJobs for deterministic column checks"

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                const headers = Array.from(grid.shadowRoot.querySelectorAll('thead th')).map((th) => (
                    th.textContent || ''
                ).trim());
                const rowText = grid.shadowRoot.querySelector('tbody tr')?.textContent || '';
                return { headers, rowText };
            }"""
        )
        assert state is not None
        assert any("WORKFLOW" in header.upper() for header in state["headers"]), (
            "Jobs grid header should include Workflow"
        )
        assert "Weekly Digest" in state["rowText"], (
            "Jobs grid row should render the workflow display name"
        )

    def test_jobs_page_grid_has_phase_and_agent_columns(self, page, server_reachable, base_url):
        """Jobs grid includes Phase and Agent columns alongside Workflow."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                return Array.from(grid.shadowRoot.querySelectorAll('thead th')).map((th) => (
                    th.textContent || ''
                ).trim());
            }"""
        )
        assert state is not None
        assert any("PHASE" in header.upper() for header in state), (
            "Jobs grid header should include Phase"
        )
        assert any("AGENT" in header.upper() for header in state), (
            "Jobs grid header should include Agent"
        )

    def test_jobs_page_grid_uses_duration_column_label(self, page, server_reachable, base_url):
        """Jobs grid should label elapsed runtime as Duration."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        state = page.evaluate(
            """() => {
                const grid = document.getElementById('jobs-grid');
                if (!grid || !grid.shadowRoot) return null;
                return Array.from(grid.shadowRoot.querySelectorAll('thead th')).map((th) => (
                    th.textContent || ''
                ).trim());
            }"""
        )
        assert state is not None
        assert any("DURATION" in header.upper() for header in state), (
            "Jobs grid header should include Duration"
        )


# ---------------------------------------------------------------------------
# Events page — platform-grid + platform-drawer wiring
# ---------------------------------------------------------------------------


class TestEventsGridWiring:
    def test_events_page_renders_grid(self, page, server_reachable, base_url):
        """Monitoring events tab renders platform-grid for event table."""
        _goto_or_skip_not_found(page, f"{base_url}/monitoring?tab=events")
        el = page.query_selector("#events-grid")
        assert el is not None, "Monitoring events tab must have <platform-grid id='events-grid'>"

    def test_events_page_renders_drawer(self, page, server_reachable, base_url):
        """Monitoring events tab opens the event detail drawer after selecting an event."""
        _goto_or_skip_not_found(page, f"{base_url}/monitoring?tab=events")
        event_buttons = page.locator(
            "xpath=//*[@id='events-grid']/following-sibling::div[contains(@class, 'grid')]//button"
        )
        if event_buttons.count() == 0:
            pytest.skip("No events available to open the detail drawer")
        event_buttons.nth(0).click()
        page.wait_for_selector("#event-drawer", timeout=10_000)
        el = page.query_selector("#event-drawer")
        assert el is not None, "Selecting an event must open <platform-drawer id='event-drawer'>"

    def test_events_drawer_is_initially_closed(self, page, server_reachable, base_url):
        """Event detail drawer starts closed (no 'open' class)."""
        page.goto(f"{base_url}/monitoring?tab=events", wait_until="networkidle", timeout=20_000)
        assert not _drawer_is_open(page, "event-drawer"), (
            "event-drawer must be closed on initial page load"
        )

    def test_events_no_bespoke_table_in_dom(self, page, server_reachable, base_url):
        """Monitoring events tab must not contain the old bespoke <table id='events-tbody'>."""
        page.goto(f"{base_url}/monitoring?tab=events", wait_until="networkidle", timeout=20_000)
        old_tbody = page.query_selector("#events-tbody")
        assert old_tbody is None, (
            "events-tbody must not exist — events use platform-grid not bespoke table"
        )


# ---------------------------------------------------------------------------
# Processes page — platform-grid wiring
# ---------------------------------------------------------------------------


class TestProcessesGridWiring:
    def test_processes_page_renders_jobs_grid(self, page, server_reachable, base_url):
        """Monitoring processes tab renders platform-grid for active jobs."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)
        el = page.query_selector("#proc-jobs-grid")
        assert el is not None, "Monitoring processes tab must have <platform-grid id='proc-jobs-grid'>"

    def test_processes_page_renders_workers_grid(self, page, server_reachable, base_url):
        """Monitoring processes tab renders platform-grid for worker subprocesses."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        el = page.query_selector("#proc-workers-grid")
        assert el is not None, "Monitoring processes tab must have <platform-grid id='proc-workers-grid'>"

    def test_processes_no_bespoke_jobs_table(self, page, server_reachable, base_url):
        """Monitoring processes tab must not contain the old bespoke #jobs-table."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        old_table = page.query_selector("#jobs-table")
        assert old_table is None, (
            "#jobs-table must not exist — processes use platform-grid not bespoke table"
        )

    def test_processes_no_bespoke_orphans_table(self, page, server_reachable, base_url):
        """Monitoring processes tab must not contain the old bespoke #orphans-table."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        old_table = page.query_selector("#orphans-table")
        assert old_table is None, (
            "#orphans-table must not exist — processes use platform-grid not bespoke table"
        )

    def test_processes_jobs_grid_bulk_action_routes_selected_ids(
        self, page, server_reachable, base_url
    ):
        """Processes jobs grid bulk cancel must route selected item ids to the page handler."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('proc-jobs-grid');
                if (!grid || !grid.shadowRoot || !grid.config) return false;
                window.__procJobBulkCalls = [];
                bulkProcessJobAction = (action, itemIds) => {
                    window.__procJobBulkCalls.push({
                        action,
                        item_ids: Array.isArray(itemIds) ? itemIds.slice() : [],
                    });
                };
                grid.setData({
                    items: [
                        { id: '101', project_name: 'Demo', display_summary: 'First', task_summary: 'First job', status: 'in_progress' },
                        { id: '102', project_name: 'Demo', display_summary: 'Second', task_summary: 'Second job', status: 'pending' },
                    ],
                    total: 2,
                });
                return true;
            }"""
        )
        assert configured, "proc-jobs-grid must be initialized before verifying bulk actions"

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-jobs-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="101"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-jobs-grid');
                const second = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="102"]')
                    : null;
                if (second) second.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-jobs-grid');
                const bulk = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('[data-bulk-action="bulk_cancel"]')
                    : null;
                if (bulk) bulk.click();
            }"""
        )
        page.wait_for_timeout(150)

        event = page.evaluate("() => window.__procJobBulkCalls && window.__procJobBulkCalls[0]")
        assert event is not None, "clicking the jobs bulk action must call bulkProcessJobAction"
        assert event["action"] == "bulk_cancel"
        assert sorted(event["item_ids"]) == ["101", "102"]

    def test_processes_workers_grid_bulk_action_routes_selected_ids(
        self, page, server_reachable, base_url
    ):
        """Processes workers grid bulk kill must route selected pids to the page handler."""
        page.goto(f"{base_url}/monitoring?tab=processes", wait_until="networkidle", timeout=20_000)
        page.wait_for_timeout(1500)

        configured = page.evaluate(
            """() => {
                const grid = document.getElementById('proc-workers-grid');
                if (!grid || !grid.shadowRoot || !grid.config) return false;
                window.__procWorkerBulkCalls = [];
                bulkWorkerAction = (action, itemIds) => {
                    window.__procWorkerBulkCalls.push({
                        action,
                        item_ids: Array.isArray(itemIds) ? itemIds.slice() : [],
                    });
                };
                grid.setData({
                    items: [
                        { pid: '2001', cmdline: 'headless_shell --foo', _rss_label: '10 MB' },
                        { pid: '2002', cmdline: 'headless_shell --bar', _rss_label: '12 MB' },
                    ],
                    total: 2,
                });
                return true;
            }"""
        )
        assert configured, "proc-workers-grid must be initialized before verifying bulk actions"

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-workers-grid');
                const first = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="2001"]')
                    : null;
                if (first) first.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-workers-grid');
                const second = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('.row-checkbox[data-row-id="2002"]')
                    : null;
                if (second) second.click();
            }"""
        )
        page.wait_for_timeout(150)

        page.evaluate(
            """() => {
                const grid = document.getElementById('proc-workers-grid');
                const bulk = grid && grid.shadowRoot
                    ? grid.shadowRoot.querySelector('[data-bulk-action="bulk_kill"]')
                    : null;
                if (bulk) bulk.click();
            }"""
        )
        page.wait_for_timeout(150)

        event = page.evaluate(
            "() => window.__procWorkerBulkCalls && window.__procWorkerBulkCalls[0]"
        )
        assert event is not None, "clicking the workers bulk action must call bulkWorkerAction"
        assert event["action"] == "bulk_kill"
        assert sorted(event["item_ids"]) == ["2001", "2002"]


# ---------------------------------------------------------------------------
# Account page — token revoke wiring
# ---------------------------------------------------------------------------


class TestAccountGridWiring:
    def test_account_page_renders_token_grid(self, page, server_reachable, base_url):
        """Account page renders platform-grid for API tokens."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/account")
        page.wait_for_timeout(1500)
        el = page.query_selector("#token-grid")
        assert el is not None, "Account page must have <platform-grid id='token-grid'>"

    def test_account_page_renders_kv_card(self, page, server_reachable, base_url):
        """Account page renders platform-kv-card for profile info."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/account")
        el = page.query_selector("#profile-card")
        assert el is not None, "Account page must have <platform-kv-card id='profile-card'>"

    def test_account_page_bootstraps_token_grid_on_initial_load(
        self, page, server_reachable, base_url
    ):
        """Account page binds the initial grid query and renders rows or empty state without manual refresh."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/account")
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('token-grid');
                if (!grid || !grid.shadowRoot || !grid._tokensQueryBound) return false;
                return (
                    grid.shadowRoot.querySelectorAll('tbody tr').length > 0
                    || !!grid.shadowRoot.querySelector('.empty-state, [data-empty-state], .bk-empty')
                );
            }""",
            timeout=10_000,
        )

    def test_token_grid_revoke_button_uses_data_attribute(self, page, server_reachable, base_url):
        """Token grid revoke button uses data-row-action, not inline onclick."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/account")
        page.wait_for_timeout(2000)
        row_count = _grid_row_count(page, "token-grid")
        if row_count == 0:
            pytest.skip("No tokens in token-grid to test")
        revoke_count = _grid_action_buttons(page, "token-grid", "revoke")
        assert revoke_count > 0, (
            "token-grid must render 'revoke' buttons via data-row-action[revoke]"
        )

    def test_token_grid_revoke_action_uses_target_not_row(self, page, server_reachable, base_url):
        """Account token grid handler reads platform-action target.id and calls revoke API."""
        token_id = "ui-test-token"
        revoke_seen: list[str] = []

        def handle_revoke(route):
            revoke_seen.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok": true}',
            )

        page.route(f"**/api/auth/tokens/{token_id}/revoke", handle_revoke)
        _goto_or_skip_not_found(page, f"{base_url}/settings/account")
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('token-grid');
                return !!(grid && grid._tokensQueryBound);
            }""",
            timeout=10_000,
        )

        dialog_seen: dict[str, str] = {}

        def accept_dialog(dialog):
            dialog_seen["type"] = dialog.type
            dialog_seen["message"] = dialog.message
            dialog.accept()

        page.once("dialog", accept_dialog)
        with page.expect_response(
            lambda resp: f"/api/auth/tokens/{token_id}/revoke" in resp.url
            and resp.request.method == "POST",
            timeout=10_000,
        ) as revoke_response_info:
            dispatched = page.evaluate(
                """(tokenId) => {
                    const grid = document.getElementById('token-grid');
                    if (!grid) return false;
                    grid.dispatchEvent(
                        new CustomEvent('platform-action', {
                            bubbles: true,
                            detail: {
                                action_id: 'revoke',
                                target: { id: tokenId, label: 'UI token' },
                                row: { id: 'wrong-row-id' },
                            },
                        })
                    );
                    return true;
                }""",
                token_id,
            )

        assert dispatched is True
        assert revoke_response_info.value.status == 200
        assert revoke_seen, "token-grid revoke action must call the revoke API"
        assert dialog_seen["type"] == "confirm"
        assert "Revoke this token" in dialog_seen["message"]
        assert not any("wrong-row-id" in url for url in revoke_seen), (
            "Account page must use platform-action target.id, not row.id"
        )


# ---------------------------------------------------------------------------
# Users page — grid wiring
# ---------------------------------------------------------------------------


class TestUsersGridWiring:
    def test_users_page_renders_grid(self, page, server_reachable, base_url):
        """Users page renders platform-grid for user table."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/users")
        page.wait_for_timeout(1500)
        el = page.query_selector("#users-grid")
        assert el is not None, "Users page must have <platform-grid id='users-grid'>"

    def test_users_grid_has_shadow_root(self, page, server_reachable, base_url):
        """Users platform-grid has a shadow root after initialization."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/users")
        page.wait_for_timeout(2000)
        has_shadow = page.evaluate(
            """() => {
                const el = document.getElementById('users-grid');
                return el ? el.shadowRoot !== null : false;
            }"""
        )
        assert has_shadow, "users-grid must have an attached shadow root"

    def test_users_page_loads_real_user_rows(self, page, server_reachable, base_url):
        """Authenticated users page must fetch and render actual user rows on initial load."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/users")
        page.wait_for_timeout(2500)
        state = page.evaluate(
            """() => {
                const el = document.getElementById('users-grid');
                const rowCount = el && el.shadowRoot ? el.shadowRoot.querySelectorAll('tbody tr').length : 0;
                return {
                    usersLength: Array.isArray(window._users) ? window._users.length : -1,
                    rowCount,
                    errorBar: el && el.shadowRoot ? el.shadowRoot.querySelector('.error-bar')?.textContent?.trim() || '' : '',
                };
            }"""
        )
        assert state["errorBar"] == "", (
            f"users-grid must not show a load error on a healthy authenticated page: {state['errorBar']}"
        )
        assert state["usersLength"] > 0, (
            "users page must populate window._users from /api/auth/users on initial load"
        )
        assert state["rowCount"] > 0, (
            "users-grid must render at least one row for the authenticated admin session"
        )

    def test_users_grid_action_id_attribute(self, page, server_reachable, base_url):
        """Users grid action buttons use data-row-action attribute."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/users")
        page.wait_for_timeout(2000)
        row_count = _grid_row_count(page, "users-grid")
        if row_count == 0:
            pytest.skip("No users rendered in grid")
        edit_count = _grid_action_buttons(page, "users-grid", "edit")
        toggle_count = _grid_action_buttons(page, "users-grid", "toggle_active")
        assert edit_count > 0 or toggle_count > 0, (
            "users-grid must render edit/toggle_active buttons via data-row-action"
        )

    def test_users_grid_edit_action_opens_modal(self, page, server_reachable, base_url):
        """Clicking the users-grid Edit action must open the user modal."""
        _goto_or_skip_not_found(page, f"{base_url}/settings/users")
        page.wait_for_timeout(2500)
        row_count = _grid_row_count(page, "users-grid")
        if row_count == 0:
            pytest.skip("No users rendered in grid")

        clicked = page.evaluate(
            """() => {
                const el = document.getElementById('users-grid');
                const btn = el && el.shadowRoot
                    ? el.shadowRoot.querySelector('button[data-row-action="edit"]')
                    : null;
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        assert clicked, "users-grid must render a clickable Edit action"
        page.wait_for_timeout(300)
        modal_open = page.evaluate(
            """() => {
                const modal = document.getElementById('modal-user');
                return modal ? !modal.classList.contains('hidden') : false;
            }"""
        )
        assert modal_open, "users-grid Edit action must open the user modal"
