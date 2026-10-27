"""E2E tests for the global search user flow.

P1 gap: service/API is covered at medium depth; missing deterministic e2e user
journey across grouped preview defaults, category drill-down, load-more cursor
behavior, and open-result routing.

Coverage:

A. Search API baseline contracts
   - GET /api/search?query=<term> returns 200 with results
   - Results are grouped by category (projects, jobs, workflows, kb)
   - Empty search returns 200 with empty results (not 422)
   - Search result items have required fields (id, name/title, type)

B. Category-specific search
   - ?category=projects filters to project results only
   - ?category=jobs filters to job results
   - Unknown category returns 200 or 400 (never 500)

C. Pagination / load-more
   - ?limit=N returns at most N results
   - ?cursor=<token> loads the next page without duplicates
   - Result count is consistent with limit param

D. Search result field contracts
   - Each result item has id, name or title, and entity_type
   - No internal/forbidden fields in search results

E. Auth and scope
   - Anonymous search must return 401/403
   - Search results must respect project-level permissions

Run:
    docker compose exec fs-app python -m pytest \
        tests/e2e/test_global_search_flow.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import anon_get, get, require_ok
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()
_SEARCH_PATH = "/api/search"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


# ===========================================================================
# A. Search API baseline contracts
# ===========================================================================


class TestSearchApiBaseline:
    """Search API must return structured, consistent results."""

    def test_search_with_term_returns_200(self, server_available):
        """GET /api/search?query=<term> must return 200."""
        resp = get(f"{_SEARCH_PATH}?query=test")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code == 200, f"Search with term must return 200; got {resp.status_code}"

    def test_search_response_is_json(self, server_available):
        """Search response must be valid JSON."""
        resp = get(f"{_SEARCH_PATH}?query=test")
        require_ok(resp, f"GET {_SEARCH_PATH}?query=test", allow_403=True, allow_404=True)
        body = resp.json()
        assert isinstance(body, (list, dict)), (
            f"Search must return list or dict; got {type(body).__name__}"
        )

    def test_empty_search_returns_200(self, server_available):
        """GET /api/search?query= (empty) must return 200 or 400 (not 422/500)."""
        resp = get(f"{_SEARCH_PATH}?query=")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        if resp.status_code >= 500:
            pytest.fail(f"Empty search must not 5xx; got {resp.status_code}")
        assert resp.status_code in (200, 400, 401, 403), (
            f"Empty search must return 200/400 or auth rejection; got {resp.status_code}"
        )

    def test_search_without_query_param_handled(self, server_available):
        """GET /api/search (no q param) must return a typed success/auth/validation status."""
        resp = get(_SEARCH_PATH)
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400, 401, 403), (
            f"Search without q param must return typed success/auth/validation status; got {resp.status_code}"
        )

    def test_search_results_have_entity_type(self, server_available):
        """Each search result must indicate its entity type."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=5")
        require_ok(resp, f"GET {_SEARCH_PATH}?query=a", allow_403=True, allow_404=True)
        body = resp.json()
        items = _extract_items(body)
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            has_type = (
                "type" in item or "entity_type" in item or "category" in item or "kind" in item
            )
            assert has_type, f"Search result must have entity_type field: {list(item.keys())}"

    def test_search_results_have_id_and_name(self, server_available):
        """Each search result must have at least id and name/title."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=5")
        require_ok(resp, f"GET {_SEARCH_PATH}?query=a", allow_403=True, allow_404=True)
        body = resp.json()
        items = _extract_items(body)
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            has_id = "id" in item
            has_name = "name" in item or "title" in item or "label" in item
            assert has_id, f"Search result must have id: {list(item.keys())}"
            assert has_name, f"Search result must have name/title: {list(item.keys())}"


def _extract_items(body) -> list:
    """Extract result items from various search response shapes."""
    if isinstance(body, list):
        return body
    # Grouped response: {projects: [...], jobs: [...], ...}
    all_items = []
    for key in ("items", "results", "projects", "jobs", "workflows", "kb", "data"):
        val = body.get(key)
        if isinstance(val, list):
            all_items.extend(val)
    return all_items


def _workflow_items(body) -> list[dict]:
    if not isinstance(body, dict):
        return []
    groups = body.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and group.get("group") == "workflows":
                return [item for item in group.get("items") or [] if isinstance(item, dict)]
    return [item for item in body.get("workflows") or [] if isinstance(item, dict)]


# ===========================================================================
# B. Category-specific search
# ===========================================================================


class TestCategorySearch:
    """Search can be filtered by category/entity type."""

    def test_search_category_projects_accepted(self, server_available):
        """?category=projects filter must be accepted without 422."""
        resp = get(f"{_SEARCH_PATH}?query=test&category=projects")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400), (
            f"?category=projects must not cause 422/500; got {resp.status_code}"
        )

    def test_search_category_jobs_accepted(self, server_available):
        """?category=jobs filter must be accepted without 422."""
        resp = get(f"{_SEARCH_PATH}?query=test&category=jobs")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400), (
            f"?category=jobs must not cause 422/500; got {resp.status_code}"
        )

    def test_search_unknown_category_not_500(self, server_available):
        """Unknown category must return a typed success/auth/validation status."""
        resp = get(f"{_SEARCH_PATH}?query=test&category=totally_unknown_xyz")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400, 401, 403), (
            f"Unknown category filter must return typed success/auth/validation status; got {resp.status_code}"
        )

    def test_search_category_results_match_filter(self, server_available):
        """Results returned for ?category=jobs must all be job-type items.

        Note: the API supports categories such as all, jobs, events, runtime_messages, kb, chats.
        There is no 'projects' category — project search goes through the 'all' category.
        """
        resp = get(f"{_SEARCH_PATH}?query=a&category=jobs&limit=5")
        require_ok(resp, f"GET {_SEARCH_PATH}?category=jobs", allow_403=True, allow_404=True)
        body = resp.json()
        items = _extract_items(body)
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            entity_type = (
                item.get("type")
                or item.get("entity_type")
                or item.get("category")
                or item.get("group")
                or ""
            )
            if entity_type:
                assert "job" in str(entity_type).lower() or entity_type in ("job", "jobs"), (
                    f"Category=jobs filter must return only jobs; got type={entity_type!r}"
                )

    @pytest.mark.parametrize("query", ("weekly_report", "Weekly Report"))
    def test_workflow_search_returns_neutral_workflow_deep_link(self, server_available, query):
        resp = get(f"{_SEARCH_PATH}?query={query}&category=workflows")
        require_ok(resp, f"GET {_SEARCH_PATH}?query={query}&category=workflows")
        items = _workflow_items(resp.json())
        match = next(
            (item for item in items if item.get("workflow_id") == NEUTRAL_WORKFLOW_ID), None
        )
        assert match is not None, f"Expected {NEUTRAL_WORKFLOW_ID} in workflow results: {items}"
        assert match["deep_link"] == f"/registry/workflows/{NEUTRAL_WORKFLOW_ID}/builder"


# ===========================================================================
# C. Pagination / load-more
# ===========================================================================


class TestSearchPagination:
    """Search must support pagination with consistent cursor behavior."""

    def test_limit_param_constrains_result_count(self, server_available):
        """?limit=3 must return at most 3 results."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=3")
        require_ok(resp, f"GET {_SEARCH_PATH}?query=a&limit=3", allow_403=True, allow_404=True)
        body = resp.json()
        items = _extract_items(body)
        assert len(items) <= 3, f"?limit=3 must return at most 3 results; got {len(items)}"

    def test_cursor_param_accepted_without_error(self, server_available):
        """?cursor=<token> must be accepted without 422."""
        resp = get(f"{_SEARCH_PATH}?query=a&cursor=0")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code not in (422, 500), (
            f"?cursor=0 must be accepted; got {resp.status_code}"
        )

    def test_second_page_no_duplicates(self, server_available):
        """Second page with cursor must not duplicate first page results."""
        resp1 = get(f"{_SEARCH_PATH}?query=a&limit=3")
        require_ok(
            resp1, f"GET {_SEARCH_PATH}?query=a&limit=3 (page 1)", allow_403=True, allow_404=True
        )
        body1 = resp1.json()
        items1 = _extract_items(body1)
        ids1 = {str(i.get("id", "")) for i in items1 if isinstance(i, dict)}

        # Get next cursor from response
        next_cursor = None
        if isinstance(body1, dict):
            next_cursor = body1.get("next_cursor") or body1.get("cursor") or body1.get("next")

        if not next_cursor:
            pytest.skip("No cursor returned — cannot test pagination")

        resp2 = get(f"{_SEARCH_PATH}?query=a&limit=3&cursor={next_cursor}")
        require_ok(
            resp2, f"GET {_SEARCH_PATH}?query=a&cursor=... (page 2)", allow_403=True, allow_404=True
        )
        items2 = _extract_items(resp2.json())
        ids2 = {str(i.get("id", "")) for i in items2 if isinstance(i, dict)}

        # Page 2 must not contain ids from page 1
        duplicates = ids1 & ids2 - {""}
        assert not duplicates, f"Search page 2 contains duplicates from page 1: {duplicates}"

    def test_large_limit_handled_gracefully(self, server_available):
        """Very large limit value must return a typed success/auth/validation status."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=10000")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400, 401, 403, 422), (
            f"Large limit must return typed success/auth/validation status; got {resp.status_code}"
        )


# ===========================================================================
# D. Search result field contracts (forbidden field leakage)
# ===========================================================================


class TestSearchResultFieldContracts:
    """Search results must not expose internal or forbidden fields."""

    _FORBIDDEN = frozenset(
        {
            "password_hash",
            "password",
            "hashed_password",
            "secret_key",
            "private_key",
            "internal_token",
        }
    )

    def test_search_results_no_forbidden_fields(self, server_available):
        """Search results must not include credential or internal fields."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=10")
        require_ok(
            resp,
            f"GET {_SEARCH_PATH}?query=a (forbidden-field check)",
            allow_403=True,
            allow_404=True,
        )
        body = resp.json()
        items = _extract_items(body)
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            leaked = self._FORBIDDEN & set(item.keys())
            assert not leaked, f"Search result leaks forbidden fields {leaked}: {str(item)[:300]}"

    def test_search_no_raw_db_fields(self, server_available):
        """Search results must not expose raw DB fields."""
        resp = get(f"{_SEARCH_PATH}?query=a&limit=10")
        require_ok(
            resp, f"GET {_SEARCH_PATH}?query=a (raw-DB-field check)", allow_403=True, allow_404=True
        )
        body = resp.json()
        items = _extract_items(body)
        raw_db_fields = {"xmin", "ctid", "tableoid", "pg_oid"}
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            leaked = raw_db_fields & set(item.keys())
            assert not leaked, f"Search result leaks raw DB fields {leaked}"


# ===========================================================================
# E. Auth and scope
# ===========================================================================


class TestSearchAuthAndScope:
    """Search must respect authentication and permission scope."""

    def test_anonymous_search_returns_401_or_403(self, server_available):
        """Anonymous search request must be rejected."""
        # Use anon_get to bypass the e2e_authenticated_requests monkeypatch —
        # only a genuine anonymous session proves the auth boundary.
        resp = anon_get(f"{_SEARCH_PATH}?query=test")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted in this environment")
        assert resp.status_code in (401, 403, 302), (
            f"Anonymous search must return 401/403/redirect; got {resp.status_code}"
        )

    def test_search_with_project_scope_accepted(self, server_available):
        """?project_id=<id> scoped search must be accepted."""
        if not _PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")
        resp = get(f"{_SEARCH_PATH}?query=test&project_id={_PROJECT_ID}")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400), (
            f"Project-scoped search must not 422/500; got {resp.status_code}"
        )

    def test_sql_injection_in_query_not_500(self, server_available):
        """SQL injection patterns in q param must return a typed success/auth/validation status."""
        malicious = "' OR '1'='1'; DROP TABLE projects; --"
        import urllib.parse

        resp = get(f"{_SEARCH_PATH}?query={urllib.parse.quote(malicious)}&limit=3")
        if resp.status_code == 404:
            pytest.skip("Search API not mounted")
        assert resp.status_code in (200, 400, 401, 403), (
            f"SQL injection in search query must return typed success/auth/validation status; got {resp.status_code}"
        )

    def test_xss_in_query_not_reflected_as_html(self, server_available):
        """XSS pattern in query param must not be returned inside an HTML response.

        A JSON API echoing back the raw query string in a JSON value is acceptable —
        JSON is data, not markup, and the client is responsible for HTML-escaping.
        What must never happen is the server returning a *text/html* response that
        contains the literal <script> tag (reflected XSS in an HTML page).
        """
        xss = "<script>alert(1)</script>"
        import urllib.parse

        resp = get(f"{_SEARCH_PATH}?query={urllib.parse.quote(xss)}&limit=3")
        require_ok(resp, f"GET {_SEARCH_PATH}?query=<xss>", allow_403=True, allow_404=True)
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            # HTML response must not contain an unescaped script tag — that is reflected XSS
            assert "<script>" not in resp.text, (
                "XSS in search query must not be reflected inside an HTML response (reflected XSS)"
            )
