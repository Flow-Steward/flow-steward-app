"""E2E tests for Registry deep features.

Covers: REG-04 to REG-41 gaps — agent tools, effective tools, I/O,
template vars, output-format generation, performance, history/diff/restore
for all entities, workflow simulate/test-run and step outputs/variables/IO,
guardrail agent scope, registry reload/stats, agent types, prompt preview,
workflow reactions, changelog generic history.

Fixture strategy: ``registry_entities`` seeds one disposable entity of each
type at module scope with a subsequent update (to generate a history entry)
and tears down on module completion.  Routes confirmed mounted in
admin/routes/* are tested as real contracts — a 404 from a mounted route is
a test failure, not a skip.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import (
    PAYLOAD_INPUT_WORKFLOW_SETTINGS,
    PROJECT_ID,
    E2ECleanup,
    apply_workflow_changes,
    get,
    linear_step_operations,
    patch_json,
    payload_transform_step,
    post_json,
    put_json,
    unique_id,
)
from tests.e2e.neutral_registry import NEUTRAL_AGENT_CATEGORY, NEUTRAL_AGENT_ID

pytestmark = [pytest.mark.e2e]


# ── Module fixtures ────────────────────────────────────────────────────────────

#: The seeded workflow's definition: a transform of the callable ``payload``
#: input (a workflow needs one execution step), a Set Variable, then a Stop Success.
#: The step-scoped IO routes are asked about the second step.
_SEED_STEP_ID = "record_payload"
_SEED_READER_ID = "read_started"
_SEED_WORKFLOW_STEPS = (
    payload_transform_step(_SEED_STEP_ID, "Record payload"),
    {
        "step_id": _SEED_READER_ID,
        "step_kind": "set_variable",
        "display_name": "Mark copied",
        "override": {"set_variable": {"target": "state.copied", "value": True}},
    },
    {
        "step_id": "done",
        "step_kind": "stop_success",
        "display_name": "Done",
        "override": {"stop_success": {"message": "done"}},
    },
)


#: The seeded guardrail: a disabled deterministic rule scoped to the neutral agent.
_SEED_GUARDRAIL = {
    "mode": "deterministic",
    "rule_type": "forbidden_patterns",
    "enabled": False,
    "config": {"patterns": ["e2e_forbidden_marker"]},
    "agents": [NEUTRAL_AGENT_ID],
}


@pytest.fixture(scope="module")
def server_available():
    resp = get("/health", timeout=5)
    if resp.status_code >= 500:
        pytest.fail(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def registry_entities(server_available):
    """Seed one disposable entity of each type for this module's tests.

    Creates agent, tool, skill, workflow, and guardrail with unique IDs,
    updates each once (to generate a history entry), and tears down on
    module completion.

    Fails the module when the registry backend is unavailable or the
    seed payload is no longer valid.
    """
    uid = unique_id("e2deep")
    created: dict = {
        "agent_id": None,
        "tool_id": None,
        "skill_id": None,
        "workflow_id": None,
        "guardrail_id": None,
    }
    cleanup = E2ECleanup()

    # Agent
    a_id = f"{uid}a"
    resp = post_json(
        "/api/agents",
        {
            "agent_id": a_id,
            "display_name": f"E2E Deep Agent {uid}",
            "category": NEUTRAL_AGENT_CATEGORY,
            "enabled": False,
        },
    )
    if resp.status_code in (200, 201):
        created["agent_id"] = a_id
        cleanup.add("DELETE", f"/api/agents/{a_id}")
        put_json(
            f"/api/agents/{a_id}",
            {
                "agent_id": a_id,
                "display_name": f"E2E Deep Agent {uid} v2",
                "category": NEUTRAL_AGENT_CATEGORY,
                "enabled": False,
                "description": "e2e-deep-fixture",
            },
        )

    # Tool
    t_id = f"{uid}t"
    resp = post_json(
        "/api/tools",
        {
            "tool_id": t_id,
            "display_name": f"E2E Deep Tool {uid}",
            "enabled": False,
        },
    )
    if resp.status_code in (200, 201):
        created["tool_id"] = t_id
        cleanup.add("DELETE", f"/api/tools/{t_id}")
        put_json(
            f"/api/tools/{t_id}",
            {
                "tool_id": t_id,
                "display_name": f"E2E Deep Tool {uid} v2",
                "enabled": False,
                "description": "e2e-deep-fixture",
            },
        )

    # Skill
    s_id = f"{uid}s"
    resp = post_json(
        "/api/skills",
        {
            "skill_id": s_id,
            "display_name": f"E2E Deep Skill {uid}",
            "enabled": True,
            "expected_outputs": [{"name": "result", "type": "text", "required": True}],
        },
    )
    if resp.status_code in (200, 201):
        created["skill_id"] = s_id
        cleanup.add("DELETE", f"/api/skills/{s_id}")
        put_json(
            f"/api/skills/{s_id}",
            {
                "skill_id": s_id,
                "display_name": f"E2E Deep Skill {uid} v2",
                "enabled": True,
                "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                "description": "e2e-deep-fixture",
            },
        )

    if created["agent_id"] and created["skill_id"]:
        put_json(
            f"/api/agents/{created['agent_id']}",
            {
                "agent_id": created["agent_id"],
                "display_name": f"E2E Deep Agent {uid} v3",
                "category": NEUTRAL_AGENT_CATEGORY,
                "enabled": True,
                "skills": [{"skill_id": created["skill_id"]}],
                "description": "e2e-deep-fixture-skill-link",
            },
        )

    # Workflow
    w_id = f"{uid}w"
    resp = post_json(
        "/api/workflows",
        {
            "workflow_id": w_id,
            "display_name": f"E2E Deep Workflow {uid}",
            "enabled": False,
            "settings": PAYLOAD_INPUT_WORKFLOW_SETTINGS,
        },
    )
    if resp.status_code in (200, 201):
        created["workflow_id"] = w_id
        cleanup.add("DELETE", f"/api/workflows/{w_id}")
        seeded = apply_workflow_changes(w_id, linear_step_operations(*_SEED_WORKFLOW_STEPS))
        assert seeded["valid"] is True, seeded["issues"]
        update_resp = patch_json(
            f"/api/workflows/{w_id}",
            {
                "display_name": f"E2E Deep Workflow {uid} v2",
                "description": "e2e-deep-fixture",
                "expected_revision": get(f"/api/workflows/{w_id}").json().get("revision"),
            },
        )
        assert update_resp.status_code == 200, (
            f"workflow fixture update failed: {update_resp.status_code} {update_resp.text[:300]}"
        )

    # Guardrail
    resp = post_json(
        "/api/guardrails",
        {
            "display_name": f"E2E Deep Guardrail {uid}",
            **_SEED_GUARDRAIL,
        },
    )
    if resp.status_code in (200, 201):
        body = resp.json()
        gid = body.get("id") or body.get("guardrail_id")
        if gid:
            created["guardrail_id"] = str(gid).strip()
            cleanup.add("DELETE", f"/api/guardrails/{gid}")

    if not any(v is not None for v in created.values()):
        pytest.fail(
            "Registry DB not available — all entity creates returned non-2xx. "
            "Ensure the registry routes are mounted and the DB is seeded."
        )

    yield created
    cleanup.run()


# ── Agent Tools ────────────────────────────────────────────────────────────────


class TestAgentTools:
    def test_agent_direct_tools(self, server_available, registry_entities):
        """GET /api/agents/{id}/tools returns a list."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/tools")
        assert resp.status_code == 200, (
            f"Agent tools endpoint returned {resp.status_code} for seeded agent {aid}. "
            "Route is mounted in admin/routes/agents.py — 404 means it is not reachable."
        )
        data = resp.json()
        tools = data if isinstance(data, list) else (data or {}).get("tools") or []
        assert isinstance(tools, list), f"tools should be a list: {data}"

    def test_agent_effective_tools(self, server_available, registry_entities):
        """GET /api/agents/{id}/effective-tools returns a list."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/effective-tools")
        assert resp.status_code == 200, (
            f"effective-tools returned {resp.status_code} for seeded agent {aid}. "
            "Route is mounted in admin/routes/agents.py."
        )
        data = resp.json()
        tools = data if isinstance(data, list) else (data or {}).get("tools") or []
        assert isinstance(tools, list)


# ── Agent I/O ──────────────────────────────────────────────────────────────────


class TestAgentIO:
    def test_agent_inputs(self, server_available, registry_entities):
        """GET /api/agents/{id}/inputs returns inputs schema."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/inputs")
        assert resp.status_code == 200, (
            f"agent inputs returned {resp.status_code} — route mounted in admin/routes/agents.py"
        )
        data = resp.json()
        inputs = data if isinstance(data, list) else (data or {}).get("inputs") or []
        assert isinstance(inputs, list)

    def test_agent_outputs(self, server_available, registry_entities):
        """GET /api/agents/{id}/outputs returns outputs schema."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/outputs")
        assert resp.status_code == 200, (
            f"agent outputs returned {resp.status_code} — route mounted in admin/routes/agents.py"
        )
        data = resp.json()
        outputs = data if isinstance(data, list) else (data or {}).get("outputs") or []
        assert isinstance(outputs, list)

    def test_agent_effective_io(self, server_available, registry_entities):
        """GET /api/agents/{id}/effective-io returns merged I/O."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/effective-io")
        assert resp.status_code == 200, (
            f"effective-io returned {resp.status_code} — route mounted in admin/routes/agents.py"
        )
        data = resp.json()
        assert isinstance(data, dict), f"effective-io body should be a dict: {data}"

    def test_agent_template_variables(self, server_available, registry_entities):
        """GET /api/agents/{id}/template-variables returns variable list."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/template-variables")
        assert resp.status_code == 200, (
            f"template-variables returned {resp.status_code} — route mounted in admin/routes/agents.py"
        )
        data = resp.json()
        variables = data if isinstance(data, list) else (data or {}).get("variables") or []
        assert isinstance(variables, list)

    def test_agent_generate_output_format(self, server_available, registry_entities):
        """POST /api/agents/{id}/generate-output-format returns format proposal."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = post_json(f"/api/agents/{aid}/generate-output-format", {})
        assert resp.status_code in (200, 400, 422), (
            f"generate-output-format returned {resp.status_code} — route mounted in admin/routes/agents.py"
        )


# ── Agent Performance ──────────────────────────────────────────────────────────


class TestAgentPerformance:
    def test_agent_performance(self, server_available, registry_entities):
        """GET /api/agents/{id}/performance returns performance data."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/agents/{aid}/performance")
        # 404 is acceptable here: means no performance data yet (not "not mounted").
        assert resp.status_code in (200, 404), (
            f"agent performance returned {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict), f"performance body should be a dict: {data}"


# ── Catalog categories ───────────────────────────────────────────────────────


class TestCatalogCategories:
    def test_catalog_categories_list(self, server_available):
        """GET /api/catalog-categories returns the shared category list."""
        resp = get("/api/catalog-categories")
        assert resp.status_code == 200, f"catalog-categories returned {resp.status_code}"
        ids = [row["id"] for row in resp.json()]
        assert "other" in ids and "marketing" in ids


# ── History/Diff for All Entity Types ─────────────────────────────────────────


class TestEntityHistory:
    @pytest.mark.parametrize(
        "entity_key,entity_type",
        [
            ("agent_id", "agents"),
            ("tool_id", "tools"),
            ("skill_id", "skills"),
        ],
    )
    def test_entity_history(self, server_available, registry_entities, entity_key, entity_type):
        """GET /api/{entity_type}/{id}/history returns a history list."""
        eid = registry_entities[entity_key]
        if not eid:
            pytest.fail(f"Test {entity_type[:-1]} not seeded")
        resp = get(f"/api/{entity_type}/{eid}/history")
        assert resp.status_code == 200, (
            f"{entity_type} history returned {resp.status_code} for seeded entity {eid}. "
            "Route is mounted — 404 means it is unreachable."
        )
        data = resp.json()
        entries = data if isinstance(data, list) else (data or {}).get("entries") or []
        assert isinstance(entries, list)

    def test_workflow_history(self, server_available, registry_entities):
        """GET /api/workflows/{id}/history returns a history list."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}/history")
        assert resp.status_code == 200, (
            f"workflow history returned {resp.status_code} for seeded workflow {wid}."
        )
        data = resp.json()
        entries = data if isinstance(data, list) else (data or {}).get("entries") or []
        assert isinstance(entries, list)

    def test_guardrail_history(self, server_available, registry_entities):
        """GET /api/guardrails/{id}/history returns a history list."""
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")
        resp = get(f"/api/guardrails/{gid}/history")
        assert resp.status_code == 200, f"guardrail history returned {resp.status_code}"
        data = resp.json()
        entries = data if isinstance(data, list) else (data or {}).get("entries") or []
        assert isinstance(entries, list)

    @pytest.mark.parametrize(
        "entity_key,entity_type",
        [
            ("agent_id", "agents"),
            ("tool_id", "tools"),
        ],
    )
    def test_entity_diff(self, server_available, registry_entities, entity_key, entity_type):
        """GET /api/{entity_type}/{id}/diff returns diff payload."""
        eid = registry_entities[entity_key]
        if not eid:
            pytest.fail(f"Test {entity_type[:-1]} not seeded")
        resp = get(f"/api/{entity_type}/{eid}/diff")
        assert resp.status_code in (200, 400), (
            f"{entity_type} diff returned {resp.status_code} — "
            "route is mounted; 400 = no revisions to diff (acceptable for new entity), "
            "404 = route unreachable (not acceptable)."
        )
        if resp.status_code == 200:
            assert isinstance(resp.json(), dict)


# ── Tool Agents ────────────────────────────────────────────────────────────────


class TestToolAgents:
    def test_tool_agents_list(self, server_available, registry_entities):
        """GET /api/tools/{id}/agents returns agents that use this tool."""
        tid = registry_entities["tool_id"]
        if not tid:
            pytest.fail("Test tool not seeded")
        resp = get(f"/api/tools/{tid}/agents")
        assert resp.status_code == 200, (
            f"tool agents returned {resp.status_code} for seeded tool {tid}. "
            "Route is mounted in admin/routes/tools.py."
        )
        data = resp.json()
        agents = data if isinstance(data, list) else (data or {}).get("agents") or []
        assert isinstance(agents, list)


# ── Skill Agents ───────────────────────────────────────────────────────────────


class TestSkillDetails:
    def test_skill_agents(self, server_available, registry_entities):
        """GET /api/skills/{id}/agents returns agents that use this skill."""
        cid = registry_entities["skill_id"]
        if not cid:
            pytest.fail("Test skill not seeded")
        resp = get(f"/api/skills/{cid}/agents")
        assert resp.status_code == 200, (
            f"skill agents returned {resp.status_code} for seeded skill {cid}. "
            "Route is mounted in admin/routes/skills.py."
        )
        data = resp.json()
        agents = data if isinstance(data, list) else (data or {}).get("agents") or []
        assert isinstance(agents, list)


# ── Workflow Advanced ──────────────────────────────────────────────────────────


class TestWorkflowAdvanced:
    def test_workflow_simulate(self, server_available, registry_entities):
        """POST /api/workflows/{id}/simulate returns simulation result."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = post_json(
            f"/api/workflows/{wid}/simulate",
            {"task_summary": "Fix a bug in the login page"},
        )
        assert resp.status_code in (200, 400, 422), (
            f"workflow simulate returned {resp.status_code} — "
            "route is mounted in admin/routes/workflows.py"
        )

    def test_workflow_test_run(self, server_available, registry_entities):
        """POST /api/workflows/{id}/test-run accepts a dry-run probe of the seeded workflow."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        if not PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")
        validation = get(f"/api/workflows/{wid}/contract-validation").json().get("validation")
        assert validation["valid"] is True, validation
        resp = post_json(
            f"/api/workflows/{wid}/test-run",
            {
                "project_id": str(PROJECT_ID),
                "task_summary": "E2E test run probe",
                "dry_run": True,
                "task_envelope": {"inputs": {"payload": {"marker": "e2e"}}},
            },
        )
        assert resp.status_code == 200, (
            f"workflow test-run dry-run failed: {resp.status_code} {resp.text[:300]}"
        )
        payload = resp.json()
        assert payload["status"] == "dry_run"
        assert payload["dry_run"] is True
        assert payload["would_execute_ephemerally"] is True
        assert payload["workflow_id"] == wid
        assert str(payload["project_id"]) == str(PROJECT_ID)
        assert "execution_id" not in payload

    def test_workflow_step_available_outputs(self, server_available, registry_entities):
        """GET /api/workflows/{id}/steps/{step_id}/available-outputs is a list."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}/steps/{_SEED_READER_ID}/available-outputs")
        assert resp.status_code == 200, (
            f"available-outputs returned {resp.status_code}: {resp.text[:300]}"
        )
        assert isinstance(resp.json(), list)

    def test_workflow_step_available_variables(self, server_available, registry_entities):
        """GET /api/workflows/{id}/steps/{step_id}/available-variables groups sources."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}/steps/{_SEED_READER_ID}/available-variables")
        assert resp.status_code == 200, (
            f"available-variables returned {resp.status_code}: {resp.text[:300]}"
        )
        data = resp.json()
        assert isinstance(data.get("state"), list)
        assert isinstance(data.get("dotted_path"), list)

    def test_workflow_step_io_validation(self, server_available, registry_entities):
        """GET /api/workflows/{id}/steps/{step_id}/io-validation is a list of issues."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}/steps/{_SEED_READER_ID}/io-validation")
        assert resp.status_code == 200, (
            f"io-validation returned {resp.status_code}: {resp.text[:300]}"
        )
        assert isinstance(resp.json(), list)

    def test_workflow_steps_are_read_from_the_workflow(self, server_available, registry_entities):
        """GET /api/workflows/{id} carries the steps the change set built."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}")
        assert resp.status_code == 200
        step_ids = {step["step_id"] for step in resp.json().get("steps") or []}
        assert step_ids == {step["step_id"] for step in _SEED_WORKFLOW_STEPS}


# ── Workflow Reactions ─────────────────────────────────────────────────────────


class TestWorkflowReactions:
    def test_workflow_reactions_list(self, server_available, registry_entities):
        """GET /api/workflows/{id}/reactions returns reaction list."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/workflows/{wid}/reactions")
        assert resp.status_code == 200, (
            f"workflow reactions returned {resp.status_code} — "
            "route is mounted in admin/routes/workflows.py"
        )
        data = resp.json()
        reactions = data if isinstance(data, list) else (data or {}).get("reactions") or []
        assert isinstance(reactions, list)


# ── Guardrail Agent Scope ──────────────────────────────────────────────────────


class TestGuardrailAgentScope:
    def test_guardrail_agents_set(self, server_available, registry_entities):
        """PUT /api/guardrails/{id}/agents sets agent scope list."""
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")
        resp = put_json(f"/api/guardrails/{gid}/agents", {"agent_ids": []})
        assert resp.status_code == 422, (
            f"empty guardrail agent scope returned {resp.status_code} — "
            "an explicit assignment is required"
        )


# ── Registry Ops ───────────────────────────────────────────────────────────────


class TestRegistryOps:
    def test_registry_reload(self, server_available):
        """POST /api/registry/reload triggers a hot-reload."""
        resp = post_json("/api/registry/reload", {})
        assert resp.status_code == 200, (
            f"registry reload returned {resp.status_code}: {resp.text[:200]}"
        )
        body = resp.json()
        assert isinstance(body, dict), f"reload response should be a dict: {body}"

    def test_registry_stats(self, server_available):
        """GET /api/registry/stats returns count summary."""
        resp = get("/api/registry/stats")
        assert resp.status_code == 200, (
            f"registry stats returned {resp.status_code}: {resp.text[:200]}"
        )
        data = resp.json()
        assert isinstance(data, dict), f"stats should be a dict: {data}"
        # Should have at least one count-like key.
        assert any(k in data for k in ("agents", "tools", "skills", "workflows", "total")), (
            f"stats response missing expected count keys: {list(data.keys())}"
        )

    def test_graph_info(self, server_available):
        """GET /api/graph returns graph metadata."""
        resp = get("/api/graph")
        assert resp.status_code == 200, f"graph info returned {resp.status_code}: {resp.text[:200]}"

    def test_recent_runs(self, server_available):
        """GET /api/runs returns a list of recent runs."""
        resp = get("/api/runs")
        assert resp.status_code == 200, (
            f"runs endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
        data = resp.json()
        runs = data if isinstance(data, list) else (data or {}).get("runs") or []
        assert isinstance(runs, list)


# ── Prompt Preview ─────────────────────────────────────────────────────────────


class TestPromptPreview:
    def test_agent_preview(self, server_available, registry_entities):
        """GET /api/preview/agent/{id} returns prompt preview."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        resp = get(f"/api/preview/agent/{aid}")
        assert resp.status_code in (200, 400, 422), (
            f"agent preview returned {resp.status_code} — "
            "route is mounted; 400/422 acceptable if context is missing"
        )

    def test_workflow_preview(self, server_available, registry_entities):
        """GET /api/preview/workflow/{id} returns workflow preview."""
        wid = registry_entities["workflow_id"]
        if not wid:
            pytest.fail("Test workflow not seeded")
        resp = get(f"/api/preview/workflow/{wid}")
        assert resp.status_code == 200, (
            f"workflow preview returned {resp.status_code} — route is mounted"
        )
        data = resp.json()
        assert isinstance(data, dict), f"workflow preview body should be a dict: {data}"


# ── Changelog Generic ──────────────────────────────────────────────────────────


class TestChangelogGeneric:
    def test_changelog_generic_history(self, server_available, registry_entities):
        """GET /api/changelog/{entity_type}/{entity_id}/history returns history list."""
        aid = registry_entities["agent_id"]
        if not aid:
            pytest.fail("Test agent not seeded")
        # Try plural form first, then singular — both may be mounted.
        resp = get(f"/api/changelog/agents/{aid}/history")
        if resp.status_code == 404:
            resp = get(f"/api/changelog/agent/{aid}/history")
        assert resp.status_code in (200, 400), (
            f"changelog generic history returned {resp.status_code} — "
            "route is mounted in admin/routes/changelog.py"
        )
        if resp.status_code == 200:
            data = resp.json()
            entries = data if isinstance(data, list) else (data or {}).get("entries") or []
            assert isinstance(entries, list)
