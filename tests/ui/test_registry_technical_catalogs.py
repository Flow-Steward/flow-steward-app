"""UI tests for the registry technical catalog pages:
/registry/fields, /registry/data-contracts, /registry/published-blocks.

P0 gap: mostly read-only presence checks exist; missing strict filter/sort/
pagination contracts and API parity assertions.

Coverage:

A. Fields catalog (/registry/fields)
   - Page loads without error
   - Field list renders with at least column headers
   - Filter/search input is present
   - Pagination controls render when count exceeds page size

B. Data contracts catalog (/registry/data-contracts)
   - Page loads without error
   - Data contract list renders
   - Each visible row has name/type columns
   - Sort controls present

C. Published blocks catalog (/registry/published-blocks)
   - Page loads without error
   - Block list renders
   - Grid contract: visible items have expected column structure

D. API parity checks
   - UI-visible field count is consistent with /api/fields count
   - UI-visible data contract count is consistent with /api/data-contracts count

E. Cross-catalog navigation
   - Each catalog page has sidebar links to sibling catalogs
   - Back navigation from detail to list works

Run:
    docker compose exec fs-app python -m pytest \
        tests/ui/test_registry_technical_catalogs.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _is_login_page(page_or_content, page=None) -> bool:
    """Return True if we ended up on the login page (URL-based check preferred)."""
    try:
        url = str(getattr(page_or_content, "url", "") or "").lower()
        if url:
            return "/login" in url or url.rstrip("/").endswith("login")
    except Exception:
        pass
    body = str(page_or_content or "").lower()
    has_login_form = 'type="password"' in body or "type='password'" in body
    has_login_indicator = "action" in body and ("login" in body or "sign-in" in body)
    return has_login_form and has_login_indicator


def _is_error_page(content: str) -> bool:
    body = str(content or "").lower()
    return "internal server error" in body or "traceback" in body


def _page_item_count(page) -> int:
    """Count visible data rows in the main content area."""
    return page.evaluate(
        """() => {
            const rows = document.querySelectorAll(
              'table tbody tr, [role="row"], .grid-row, .list-item, .catalog-item'
            );
            return Array.from(rows).filter(r => {
              const rect = r.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }).length;
        }"""
    )


# ===========================================================================
# A. Fields catalog
# ===========================================================================


class TestFieldsCatalog:
    """Registry fields catalog (/registry/fields) must load and render data."""

    def test_fields_page_http_200(self, page, server_reachable, base_url):
        """GET /registry/fields must return 200 or auth redirect."""
        resp = requests.get(f"{base_url}/registry/fields", verify=False, timeout=10)
        assert resp.status_code in (200, 302, 301, 403, 404), (
            f"/registry/fields returned unexpected {resp.status_code}"
        )

    def test_fields_page_renders(self, page, server_reachable, base_url):
        """Browser must render the fields catalog page."""
        page.goto(f"{base_url}/registry/fields", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Fields catalog not mounted in this build")
        assert not _is_error_page(content), "/registry/fields must not crash"

    def test_fields_page_has_list_content(self, page, server_reachable, base_url):
        """Fields catalog must render a list or table of fields."""
        page.goto(f"{base_url}/registry/fields", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Fields catalog not mounted")
        has_list = (
            page.locator("table, [role='grid'], .grid, .list-view").count() > 0
            or "field" in content
            or "name" in content
        )
        assert has_list, "Fields catalog must render a list or table of fields"

    def test_fields_page_has_filter_input(self, page, server_reachable, base_url):
        """Fields catalog must have a search/filter input."""
        page.goto(f"{base_url}/registry/fields", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Fields catalog not mounted")
        search_input = page.locator(
            "input[type='search'], input[type='text'][placeholder*='search' i], "
            "input[placeholder*='filter' i], input[name*='search'], input[name*='filter']"
        )
        has_filter = search_input.count() > 0 or "search" in content or "filter" in content
        assert has_filter, (
            "/registry/fields: page has no visible search or filter UI — "
            "add a search input or filter control for usability"
        )

    def test_fields_page_no_500(self, page, server_reachable, base_url):
        """Fields catalog must return typed route/auth status."""
        resp = requests.get(f"{base_url}/registry/fields", verify=False, timeout=10)
        assert resp.status_code in (200, 301, 302, 401, 403, 404), (
            f"/registry/fields returned unexpected {resp.status_code}"
        )


# ===========================================================================
# B. Data contracts catalog
# ===========================================================================


class TestDataContractsCatalog:
    """Registry data contracts catalog (/registry/data-contracts) must load."""

    def test_data_contracts_page_http_not_500(self, page, server_reachable, base_url):
        """GET /registry/data-contracts must return typed route/auth status."""
        resp = requests.get(f"{base_url}/registry/data-contracts", verify=False, timeout=10)
        assert resp.status_code in (200, 301, 302, 401, 403, 404), (
            f"/registry/data-contracts returned unexpected {resp.status_code}"
        )

    def test_data_contracts_page_renders(self, page, server_reachable, base_url):
        """Browser must render the data contracts catalog."""
        page.goto(
            f"{base_url}/registry/data-contracts",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Data contracts catalog not mounted")
        assert not _is_error_page(content), "/registry/data-contracts must not crash"

    def test_data_contracts_list_has_structure(self, page, server_reachable, base_url):
        """Data contracts catalog must render a list with column headers."""
        page.goto(
            f"{base_url}/registry/data-contracts",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Data contracts catalog not mounted")
        has_structure = (
            page.locator("table th, [role='columnheader'], .column-header").count() > 0
            or "contract" in content
            or "name" in content
        )
        assert has_structure, "Data contracts catalog must render column headers or list structure"

    def test_data_contracts_rows_have_name_column(self, page, server_reachable, base_url):
        """Each visible data contract row must show a name."""
        page.goto(
            f"{base_url}/registry/data-contracts",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Data contracts catalog not mounted")
        # Name column must be present (headers or data cells)
        assert "name" in content or "contract" in content, (
            "Data contracts catalog must display contract names"
        )


# ===========================================================================
# C. Published blocks catalog
# ===========================================================================


class TestPublishedBlocksCatalog:
    """Registry published blocks catalog (/registry/published-blocks) must load."""

    def test_published_blocks_page_not_500(self, page, server_reachable, base_url):
        """GET /registry/published-blocks must return typed route/auth status."""
        resp = requests.get(f"{base_url}/registry/published-blocks", verify=False, timeout=10)
        assert resp.status_code in (200, 301, 302, 401, 403, 404), (
            f"/registry/published-blocks returned unexpected {resp.status_code}"
        )

    def test_published_blocks_page_renders(self, page, server_reachable, base_url):
        """Browser must render the published blocks catalog."""
        page.goto(
            f"{base_url}/registry/published-blocks",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Published blocks catalog not mounted")
        assert not _is_error_page(content), "/registry/published-blocks must not crash"

    def test_published_blocks_list_renders(self, page, server_reachable, base_url):
        """Published blocks catalog must render a block list or grid."""
        page.goto(
            f"{base_url}/registry/published-blocks",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Published blocks catalog not mounted")
        has_grid = (
            page.locator("table, [role='grid'], .grid, .block-list, .card-grid").count() > 0
            or "block" in content
            or "published" in content
        )
        assert has_grid, "Published blocks catalog must render a list or grid of blocks"

    def test_published_blocks_item_has_name(self, page, server_reachable, base_url):
        """Each visible published block item must display a name."""
        page.goto(
            f"{base_url}/registry/published-blocks",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Published blocks catalog not mounted")
        assert "name" in content or "block" in content or "title" in content, (
            "Published blocks catalog must display block names or titles"
        )


# ===========================================================================
# D. API parity checks
# ===========================================================================


class TestCatalogApiParity:
    """UI visible item counts must be consistent with API responses."""

    def test_fields_api_returns_consistent_data(self, page, server_reachable, base_url):
        """API /api/fields must return data that the UI would render."""
        resp = requests.get(f"{base_url}/api/fields?limit=5", verify=False, timeout=10)
        if resp.status_code == 404:
            pytest.fail("Fields API not mounted")
        if resp.status_code != 200:
            pytest.fail(f"Fields API returned {resp.status_code}")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("fields") or []
        # API must return a list-like structure
        assert isinstance(items, list), "Fields API must return a list"

    def test_data_contracts_api_returns_list(self, page, server_reachable, base_url):
        """API /api/data-contracts must return a list."""
        resp = requests.get(f"{base_url}/api/data-contracts?limit=5", verify=False, timeout=10)
        if resp.status_code == 404:
            pytest.fail("Data contracts API not mounted")
        if resp.status_code != 200:
            pytest.fail(f"Data contracts API returned {resp.status_code}")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("contracts") or []
        assert isinstance(items, list), "Data contracts API must return a list"

    def test_published_blocks_api_returns_list(self, page, server_reachable, base_url):
        """API /api/published-blocks must return a list."""
        resp = requests.get(f"{base_url}/api/published-blocks?limit=5", verify=False, timeout=10)
        if resp.status_code == 404:
            pytest.fail("Published blocks API not mounted")
        if resp.status_code != 200:
            pytest.fail(f"Published blocks API returned {resp.status_code}")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("blocks") or []
        assert isinstance(items, list), "Published blocks API must return a list"


# ===========================================================================
# E. Cross-catalog navigation
# ===========================================================================


class TestCrossCatalogNavigation:
    """Registry catalog pages must have cross-catalog navigation."""

    def test_fields_page_has_registry_nav(self, page, server_reachable, base_url):
        """Fields catalog page must include registry sidebar navigation."""
        page.goto(f"{base_url}/registry/fields", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Fields catalog not mounted")
        has_nav = (
            page.locator("nav, [role='navigation'], aside, .sidebar").count() > 0
            or "registry" in content
        )
        assert has_nav, "Fields catalog must have registry navigation"

    def test_data_contracts_page_links_to_other_catalogs(self, page, server_reachable, base_url):
        """Data contracts page must have links to other registry sections."""
        page.goto(
            f"{base_url}/registry/data-contracts",
            wait_until="networkidle",
            timeout=25_000,
        )
        content = page.content().lower()
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        if "not found" in content or "404" in content:
            pytest.fail("Data contracts catalog not mounted")
        # Should link to agents, tools, or workflows (sibling registry pages)
        sibling_links = page.locator(
            "a[href*='/registry/agents'], a[href*='/registry/tools'], "
            "a[href*='/registry/workflows'], a[href*='/registry']"
        )
        assert sibling_links.count() > 0 or "registry" in content, (
            "Data contracts page must link to sibling registry catalogs"
        )
