"""
Extended UI tests for Registry pages: agents, tools, skills, workflows,
guardrails, data contracts, and sidebar navigation.

Tests:
- Agents: page loads, grid/list, search, detail navigation
- Tools: page loads, grid/list, detail navigation
- Skills: page loads, grid/list
- Workflows: page loads, grid/list, builder steps, workflow builder canvas
- Guardrails: page loads
- Data Contracts: page loads
- Sidebar: all registry links present, active state highlighting

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_registry_pages_extended.py -v --timeout=60
"""

from __future__ import annotations

import json
import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _json_list(payload):
    """Extract a list from an API response payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (
            "items",
            "rows",
            "data",
            "agents",
            "tools",
            "skills",
            "workflows",
            "guardrails",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _assert_tab_labels_present(page, labels: list[str]) -> None:
    if labels:
        page.locator("main button, [role='tab'], button").filter(has_text=labels[0]).first.wait_for(
            state="visible",
            timeout=20_000,
        )
    content = page.content().lower()
    missing: list[str] = []
    for label in labels:
        if page.locator(f"button:has-text('{label}')").count() > 0:
            continue
        if label.lower() not in content:
            missing.append(label)
    assert not missing, f"Missing expected detail tabs: {missing}"


def _object_from_maybe_json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def _return_contract_keys(value) -> list[str]:
    row = _object_from_maybe_json(value)
    properties = _object_from_maybe_json(row.get("properties"))
    if properties:
        return [str(key).strip() for key in properties if str(key).strip()]
    name = str(row.get("name") or "").strip()
    return [name] if name else []


def _first_tool_action_contract(base_url: str, ui_auth_session):
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/tools", verify=verify_ssl, timeout=15)
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    for tool in _json_list(resp.json()):
        if not isinstance(tool, dict):
            continue
        tool_id = str(tool.get("tool_id") or tool.get("id") or "").strip()
        if not tool_id:
            continue
        try:
            detail_resp = _get(
                f"{base_url}/api/tools/{tool_id}",
                verify=verify_ssl,
                timeout=15,
            )
        except requests.RequestException:
            continue
        if not detail_resp.ok:
            continue
        detail = _object_from_maybe_json(detail_resp.json())
        interface = _object_from_maybe_json(detail.get("interface"))
        for function in interface.get("functions") or interface.get("operations") or []:
            if not isinstance(function, dict):
                continue
            function_name = str(
                function.get("display_name")
                or function.get("name")
                or function.get("operation_id")
                or ""
            ).strip()
            parameters = [
                str(row.get("name") or "").strip()
                for row in (function.get("parameters") or [])
                if isinstance(row, dict) and str(row.get("name") or "").strip()
            ]
            return_keys = _return_contract_keys(function.get("returns") or function.get("outputs"))
            if function_name and (parameters or return_keys):
                return {
                    "tool_id": tool_id,
                    "function_name": function_name,
                    "parameter": parameters[0] if parameters else "",
                    "return_key": return_keys[0] if return_keys else "",
                }
    return None


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url, ui_auth_session):
    """Skip if not running in registry mode."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/agents", verify=verify_ssl, timeout=5)
        if resp.status_code == 404:
            pytest.skip("Registry mode not enabled (admin API not found)")
    except Exception as e:
        pytest.skip(f"Registry API not reachable: {e}")


@pytest.fixture(scope="module")
def first_agent_id(base_url, registry_mode_enabled, ui_auth_session):
    """Get the first agent ID from the registry, or None."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/agents", verify=verify_ssl, timeout=5)
    if not resp.ok:
        return None
    agents = _json_list(resp.json())
    if not agents:
        return None
    return agents[0].get("agent_id") or agents[0].get("id")


@pytest.fixture(scope="module")
def first_tool_id(base_url, registry_mode_enabled, ui_auth_session):
    """Get the first tool ID from the registry, or None."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/tools", verify=verify_ssl, timeout=5)
    if not resp.ok:
        return None
    tools = _json_list(resp.json())
    if not tools:
        return None
    return tools[0].get("tool_id") or tools[0].get("id")


@pytest.fixture(scope="module")
def first_workflow_id(base_url, registry_mode_enabled, ui_auth_session):
    """Get the first workflow ID from the registry, or None."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/workflows", verify=verify_ssl, timeout=5)
    if not resp.ok:
        return None
    workflows = _json_list(resp.json())
    if not workflows:
        return None
    return workflows[0].get("workflow_id") or workflows[0].get("id")


@pytest.fixture(scope="module")
def first_skill_id(base_url, registry_mode_enabled, ui_auth_session):
    """Get the first skill ID from the registry, or None."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/skills", verify=verify_ssl, timeout=5)
    if not resp.ok:
        return None
    skills = _json_list(resp.json())
    if not skills:
        return None
    return skills[0].get("skill_id") or skills[0].get("id")


@pytest.fixture(scope="module")
def first_guardrail_id(base_url, registry_mode_enabled, ui_auth_session):
    """Get the first guardrail ID from the registry, or None."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/guardrails", verify=verify_ssl, timeout=5)
    if not resp.ok:
        return None
    rows = _json_list(resp.json())
    if not rows:
        return None
    return rows[0].get("id") or rows[0].get("guardrail_id")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class TestRegistryAgentsPageExtended:
    """Extended tests for the /registry/agents page."""

    def test_agents_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/agents returns 200."""
        resp = requests.get(f"{base_url}/registry/agents", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_agents_page_has_grid_or_list(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Agents page renders agent cards, a grid, or a table."""
        page.goto(f"{base_url}/registry/agents", wait_until="networkidle", timeout=20_000)
        elements = page.locator(
            "platform-grid, table, .agent-card, .card, [data-testid='agents-list'], .registry-list"
        )
        content = page.content().lower()
        assert elements.count() > 0 or "agent" in content

    def test_agents_page_has_search(self, page, server_reachable, registry_mode_enabled, base_url):
        """Agents page has a search input."""
        page.goto(f"{base_url}/registry/agents", wait_until="networkidle", timeout=20_000)
        search = page.locator(
            "input[type='search'], input[placeholder*='search'], "
            "input[placeholder*='Search'], input[placeholder*='filter'], "
            "[data-testid='search']"
        )
        content = page.content().lower()
        assert search.count() > 0 or "search" in content

    def test_agent_detail_page_loads(
        self, page, server_reachable, registry_mode_enabled, base_url, first_agent_id
    ):
        """Clicking an agent navigates to its detail page."""
        if first_agent_id is None:
            pytest.skip("No agents in registry")
        page.goto(
            f"{base_url}/registry/agents/{first_agent_id}",
            wait_until="networkidle",
            timeout=20_000,
        )
        content = page.content()
        assert len(content) > 200
        assert page.url.endswith(f"/agents/{first_agent_id}") or "agent" in content.lower()

    def test_agent_detail_has_entity_specific_tabs(
        self, page, server_reachable, registry_mode_enabled, base_url, first_agent_id
    ):
        """Agent detail preserves prompts/skills/tools/config/io/preview/changelog tabs."""
        if first_agent_id is None:
            pytest.skip("No agents in registry")
        page.goto(
            f"{base_url}/registry/agents/{first_agent_id}",
            wait_until="networkidle",
            timeout=20_000,
        )
        _assert_tab_labels_present(
            page,
            ["Prompts", "Skills", "Tools", "Config", "Builder", "I/O", "Preview", "Changelog"],
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestRegistryToolsPageExtended:
    """Extended tests for the /registry/tools page."""

    def test_tools_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/tools returns 200."""
        resp = requests.get(f"{base_url}/registry/tools", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_tools_page_has_grid_or_list(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Tools page renders tool cards, a grid, or a table."""
        page.goto(f"{base_url}/registry/tools", wait_until="networkidle", timeout=20_000)
        elements = page.locator(
            "platform-grid, table, .tool-card, .card, [data-testid='tools-list'], .registry-list"
        )
        content = page.content().lower()
        assert elements.count() > 0 or "tool" in content

    def test_tool_detail_page_loads(
        self, page, server_reachable, registry_mode_enabled, base_url, first_tool_id
    ):
        """Tool detail page loads for the first tool."""
        if first_tool_id is None:
            pytest.skip("No tools in registry")
        page.goto(
            f"{base_url}/registry/tools/{first_tool_id}",
            wait_until="networkidle",
            timeout=20_000,
        )
        content = page.content()
        assert len(content) > 200

    def test_tool_detail_has_entity_specific_tabs(
        self, page, server_reachable, registry_mode_enabled, base_url, first_tool_id
    ):
        """Tool detail preserves config/actions/prompt/agents/changelog tabs."""
        if first_tool_id is None:
            pytest.skip("No tools in registry")
        page.goto(
            f"{base_url}/registry/tools/{first_tool_id}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.get_by_role("tab", name="Config", exact=True).wait_for(state="visible", timeout=20_000)
        _assert_tab_labels_present(
            page, ["Config", "Actions", "Prompt", "Agents Using", "Changelog"]
        )

    def test_tool_detail_actions_tab_shows_function_contracts(
        self, page, server_reachable, registry_mode_enabled, base_url, ui_auth_session
    ):
        """Tool detail Actions tab exposes function inputs and returned fields."""
        contract = _first_tool_action_contract(base_url, ui_auth_session)
        if not contract:
            pytest.skip("No tool action contract available in registry")
        page.goto(
            f"{base_url}/registry/tools/{contract['tool_id']}",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        page.get_by_role("tab", name="Actions").wait_for(state="visible", timeout=10_000)
        page.get_by_role("tab", name="Actions").click()
        page.get_by_role("heading", name=contract["function_name"]).wait_for(
            state="visible", timeout=10_000
        )
        if contract["parameter"]:
            page.locator("main").get_by_text(contract["parameter"], exact=True).first.wait_for(
                state="visible", timeout=10_000
            )
        if contract["return_key"]:
            page.locator("main").get_by_text(contract["return_key"], exact=True).first.wait_for(
                state="visible", timeout=10_000
            )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class TestRegistrySkillsPageExtended:
    """Extended tests for the /registry/skills page."""

    def test_skills_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/skills returns 200."""
        resp = requests.get(f"{base_url}/registry/skills", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_skills_page_has_grid_or_list(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Skills page renders cards, a grid, or a table."""
        page.goto(f"{base_url}/registry/skills", wait_until="networkidle", timeout=20_000)
        elements = page.locator(
            "platform-grid, table, .skill-card, .card, [data-testid='skills-list'], .registry-list"
        )
        content = page.content().lower()
        assert elements.count() > 0 or "capabilit" in content

    def test_skill_detail_has_entity_specific_tabs(
        self, page, server_reachable, registry_mode_enabled, base_url, first_skill_id
    ):
        """Skill detail preserves config/instructions/io/agents/changelog tabs."""
        if first_skill_id is None:
            pytest.skip("No skills in registry")
        page.goto(
            f"{base_url}/registry/skills/{first_skill_id}",
            wait_until="networkidle",
            timeout=20_000,
        )
        _assert_tab_labels_present(
            page,
            ["Config", "Builder", "Instructions", "I/O", "Agents", "Changelog"],
        )


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


class TestRegistryWorkflowsPageExtended:
    """Extended tests for the /registry/workflows page."""

    def test_workflows_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/workflows returns 200."""
        resp = requests.get(f"{base_url}/registry/workflows", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_workflows_page_has_grid_or_list(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Workflows page renders workflow cards, a grid, or a table."""
        page.goto(f"{base_url}/registry/workflows", wait_until="networkidle", timeout=20_000)
        elements = page.locator(
            "platform-grid, table, .workflow-card, .card, "
            "[data-testid='workflows-list'], .registry-list"
        )
        content = page.content().lower()
        assert elements.count() > 0 or "workflow" in content

    def test_workflow_builder_shows_steps(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Workflow overview links to the builder, which renders each step as a canvas node."""
        page.goto(
            f"{base_url}/registry/workflows/{NEUTRAL_WORKFLOW_ID}",
            wait_until="networkidle",
            timeout=20_000,
        )
        page.get_by_role("link", name="Open builder").first.wait_for(
            state="visible", timeout=20_000
        )

        page.goto(
            f"{base_url}/registry/workflows/{NEUTRAL_WORKFLOW_ID}/builder",
            wait_until="networkidle",
            timeout=20_000,
        )
        nodes = page.locator(".react-flow__node")
        nodes.first.wait_for(state="visible", timeout=20_000)
        assert nodes.filter(has_text="Draft summary").count() == 1
        assert nodes.filter(has_text="Review draft").count() == 1
        # Negative: the builder has no phase sections any more.
        assert page.locator(".phase, .phase-card, [data-testid='phases']").count() == 0

    def test_workflow_builder_renders_canvas(
        self, page, server_reachable, registry_mode_enabled, base_url, first_workflow_id
    ):
        """The builder route of any workflow renders the visual canvas."""
        if first_workflow_id is None:
            pytest.skip("No workflows in registry")
        page.goto(
            f"{base_url}/registry/workflows/{first_workflow_id}/builder",
            wait_until="networkidle",
            timeout=20_000,
        )
        page.locator(".react-flow").first.wait_for(state="visible", timeout=20_000)
        assert page.get_by_role("textbox", name="Search workflow canvas").is_visible()


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


class TestRegistryGuardrailsPage:
    """Tests for the /registry/guardrails page."""

    def test_guardrails_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/guardrails returns 200."""
        resp = requests.get(f"{base_url}/registry/guardrails", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_guardrail_detail_has_entity_specific_tabs(
        self, page, server_reachable, registry_mode_enabled, base_url, first_guardrail_id
    ):
        """Guardrail detail preserves config/scope/changelog tabs."""
        if first_guardrail_id is None:
            pytest.skip("No guardrails in registry")
        page.goto(
            f"{base_url}/registry/guardrails/{first_guardrail_id}",
            wait_until="networkidle",
            timeout=20_000,
        )
        _assert_tab_labels_present(page, ["Config", "Agent Scope", "Changelog"])


# ---------------------------------------------------------------------------
# Data Contracts
# ---------------------------------------------------------------------------


class TestRegistryDataContractsPage:
    """Tests for the /registry/data-contracts page."""

    def test_data_contracts_page_loads(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """/registry/data-contracts returns 200."""
        resp = requests.get(f"{base_url}/registry/data-contracts", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_data_contracts_page_restores_contract_specific_grid_shape(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Data contracts page should expose contract-centric filters/columns, not generic fallback rows."""
        page.goto(f"{base_url}/registry/data-contracts", wait_until="networkidle", timeout=20_000)
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('contracts-grid');
                return !!(grid && grid.config && Array.isArray(grid.config.columns) && grid.config.columns.length);
            }""",
            timeout=10_000,
        )
        config = page.evaluate(
            """() => {
                const grid = document.getElementById('contracts-grid');
                if (!grid || !grid.config) return { columns: [], filters: [] };
                return {
                    columns: Array.isArray(grid.config.columns) ? grid.config.columns.map((c) => c.key) : [],
                    filters: Array.isArray(grid.config.filters_schema?.fields)
                        ? grid.config.filters_schema.fields.map((f) => f.filter_id)
                        : [],
                };
            }"""
        )
        assert "contract_id" in (config.get("columns") or [])
        assert "_projection_label" in (config.get("columns") or [])
        assert "_sync_label" in (config.get("columns") or [])
        assert "kind" in (config.get("filters") or [])
        assert "_projection_label" in (config.get("filters") or [])
        assert "_sync_label" in (config.get("filters") or [])
        assert (
            page.locator(
                "text=Data contracts are generated and synchronized from registry definitions."
            ).count()
            > 0
        )
        assert page.locator("a[href='/registry/fields']").count() > 0
        assert page.locator("a[href='/registry/published-blocks']").count() > 0


class TestRegistryFieldsPage:
    """Tests for the /registry/fields page."""

    def test_fields_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/fields returns 200."""
        resp = requests.get(f"{base_url}/registry/fields", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_fields_page_restores_field_contract_grid_shape(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Fields page should show field-specific filters/columns and read-only guidance."""
        page.goto(f"{base_url}/registry/fields", wait_until="networkidle", timeout=20_000)
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('fields-grid');
                return !!(grid && grid.config && Array.isArray(grid.config.columns) && grid.config.columns.length);
            }""",
            timeout=10_000,
        )
        config = page.evaluate(
            """() => {
                const grid = document.getElementById('fields-grid');
                if (!grid || !grid.config) return { columns: [], filters: [] };
                return {
                    columns: Array.isArray(grid.config.columns) ? grid.config.columns.map((c) => c.key) : [],
                    filters: Array.isArray(grid.config.filters_schema?.fields)
                        ? grid.config.filters_schema.fields.map((f) => f.filter_id)
                        : [],
                };
            }"""
        )
        assert "field_key" in (config.get("columns") or [])
        assert "_manual_label" in (config.get("columns") or [])
        assert "_contract_label" in (config.get("columns") or [])
        assert "value_type" in (config.get("filters") or [])
        assert "_contract_label" in (config.get("filters") or [])
        assert "_source_label" in (config.get("filters") or [])
        assert (
            page.locator(
                "text=Fields are derived from workflow, skill, and published block contracts."
            ).count()
            > 0
        )
        assert page.locator("a[href='/registry/data-contracts']").count() > 0
        assert page.locator("a[href='/registry/published-blocks']").count() > 0


class TestRegistryPublishedBlocksPage:
    """Tests for the /registry/published-blocks page."""

    def test_published_blocks_page_loads(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """/registry/published-blocks returns 200."""
        resp = requests.get(f"{base_url}/registry/published-blocks", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_published_blocks_page_restores_contract_grid_shape(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Published blocks page should render block-specific filters/columns and cross-links."""
        page.goto(f"{base_url}/registry/published-blocks", wait_until="networkidle", timeout=20_000)
        page.wait_for_function(
            """() => {
                const grid = document.getElementById('published-blocks-grid');
                return !!(grid && grid.config && Array.isArray(grid.config.columns) && grid.config.columns.length);
            }""",
            timeout=10_000,
        )
        config = page.evaluate(
            """() => {
                const grid = document.getElementById('published-blocks-grid');
                if (!grid || !grid.config) return { columns: [], filters: [] };
                return {
                    columns: Array.isArray(grid.config.columns) ? grid.config.columns.map((c) => c.key) : [],
                    filters: Array.isArray(grid.config.filters_schema?.fields)
                        ? grid.config.filters_schema.fields.map((f) => f.filter_id)
                        : [],
                };
            }"""
        )
        assert "block_key" in (config.get("columns") or [])
        assert "_io_summary" in (config.get("columns") or [])
        assert "block_type" in (config.get("columns") or [])
        assert "block_type" in (config.get("filters") or [])
        assert "source_kind" in (config.get("filters") or [])
        assert "_family_label" in (config.get("filters") or [])
        assert (
            page.locator("text=Published blocks are runtime-facing contract snapshots.").count() > 0
        )
        assert page.locator("a[href='/registry/fields']").count() > 0
        assert page.locator("a[href='/registry/data-contracts']").count() > 0


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------


class TestRegistryChangelogPage:
    """Tests for the /registry/changelog page."""

    def test_changelog_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """/registry/changelog returns 200."""
        resp = requests.get(f"{base_url}/registry/changelog", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_changelog_page_has_search_and_restore_controls(
        self, page, server_reachable, registry_mode_enabled, base_url
    ):
        """Changelog page exposes cross-entity search and restore controls."""
        page.goto(f"{base_url}/registry/changelog", wait_until="networkidle", timeout=20_000)
        search = page.locator(
            "input[type='search'], input[placeholder*='Search registry entities']"
        )
        assert search.count() > 0
        page.wait_for_function(
            """() => {
                const body = document.body?.innerText || "";
                return body.includes("entry #") || body.includes("No changelog entries found");
            }""",
            timeout=10_000,
        )
        entry_buttons = page.locator("button").filter(has_text="entry #")
        if entry_buttons.count() > 0:
            entry_buttons.first.click()
            restore_button = page.locator("button:has-text('Restore selected revision')")
            restore_button.first.wait_for(state="visible", timeout=10_000)
            assert restore_button.count() > 0
            return
        assert page.locator("text=No changelog entries found with current filters.").count() > 0


# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------


class TestSidebarNavigation:
    """Sidebar navigation tests for registry links."""

    def test_sidebar_has_all_registry_links(self, page, server_reachable, base_url):
        """Sidebar contains links to agents, tools, workflows, and skills."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        expected_paths = [
            "/registry/agents",
            "/registry/tools",
            "/registry/workflows",
            "/registry/skills",
        ]
        links = page.evaluate(
            """() => {
                const hrefs = [];
                for (const el of document.querySelectorAll('#sidebar a[href], nav a[href], aside a[href]')) {
                    const href = String(el.getAttribute('href') || '').trim();
                    if (href.startsWith('/')) hrefs.push(href);
                }
                return hrefs;
            }"""
        )
        missing = [p for p in expected_paths if p not in (links or [])]
        assert not missing, f"Sidebar is missing registry links: {missing}"

    def test_sidebar_active_state(self, page, server_reachable, registry_mode_enabled, base_url):
        """Correct sidebar link is highlighted when visiting a registry page."""
        page.goto(f"{base_url}/registry/agents", wait_until="networkidle", timeout=20_000)
        agents_nav_links = page.locator("#sidebar a[href='/registry/agents']")
        if agents_nav_links.count() == 0:
            pytest.skip("Registry navigation link is not rendered in the current shell")
        # Look for an active/selected class on the agents sidebar link
        active_link = page.locator(
            "#sidebar a.active[href='/registry/agents'], "
            "nav a.active[href='/registry/agents'], "
            "a.sidebar-item.active[href='/registry/agents'], "
            "[aria-current='page'][href='/registry/agents']"
        )
        # Fallback: check if any link with 'agents' href has an active-like class
        any_active = page.locator(
            "a[href='/registry/agents'].active, "
            "a[href='/registry/agents'].selected, "
            "a[href='/registry/agents'][aria-current]"
        )
        assert active_link.count() > 0 or any_active.count() > 0
