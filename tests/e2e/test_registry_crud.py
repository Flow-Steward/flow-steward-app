"""E2E tests for Registry CRUD surfaces (REG-02 through REG-07).

Covers create/read/update/toggle/clone/history/delete lifecycle
for agents, tools, skills, workflows, and guardrails.
All entities use ``e2e_`` prefixed IDs and are cleaned up after tests.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import (
    PAYLOAD_INPUT_WORKFLOW_SETTINGS,
    E2ECleanup,
    apply_workflow_changes,
    delete,
    get,
    linear_step_operations,
    patch_json,
    payload_transform_step,
    post_json,
    unique_id,
)
from tests.e2e.neutral_registry import NEUTRAL_AGENT_CATEGORY, NEUTRAL_AGENT_ID

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def registry_available(server_available):
    """Skip if registry admin routes are not mounted."""
    resp = get("/api/agents")
    if resp.status_code in (404, 503):
        pytest.skip("Registry admin routes not available")


# ── Agents ───────────────────────────────────────────────────────────────────


class TestAgentCRUD:
    def test_agent_crud_lifecycle(self, registry_available):
        agent_id = unique_id("e2e_agent")
        with E2ECleanup() as cleanup:
            # Create
            create_resp = post_json(
                "/api/agents",
                {
                    "agent_id": agent_id,
                    "display_name": f"E2E Agent {agent_id}",
                    "category": NEUTRAL_AGENT_CATEGORY,
                    "description": "Temporary E2E test agent",
                    "enabled": False,
                },
            )
            assert create_resp.status_code in (200, 201), (
                f"agent create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            cleanup.add("DELETE", f"/api/agents/{agent_id}")

            # Read
            read_resp = get(f"/api/agents/{agent_id}")
            assert read_resp.status_code == 200
            agent = read_resp.json()
            a = agent.get("agent") or agent
            assert a.get("agent_id") == agent_id or a.get("id") == agent_id

            # List — find it
            list_resp = get("/api/agents")
            assert list_resp.status_code == 200
            agents_list = (
                list_resp.json()
                if isinstance(list_resp.json(), list)
                else list_resp.json().get("agents", [])
            )
            found = [a for a in agents_list if (a.get("agent_id") or a.get("id")) == agent_id]
            assert found, f"Agent {agent_id} not found in list"

            # Patch
            patch_resp = patch_json(
                f"/api/agents/{agent_id}",
                {
                    "description": "Updated by E2E test",
                },
            )
            assert patch_resp.status_code == 200

            # Toggle — may return 400 if contract validation fails (no skills).
            # That's expected: the toggle endpoint works, but enabling is guarded.
            toggle_resp = patch_json(f"/api/agents/{agent_id}/toggle")
            assert toggle_resp.status_code in (200, 400)
            if toggle_resp.status_code == 200:
                assert "enabled" in toggle_resp.json()

            # History
            history_resp = get(f"/api/agents/{agent_id}/history")
            if history_resp.status_code == 200:
                history = history_resp.json()
                entries = (
                    history
                    if isinstance(history, list)
                    else history.get("entries") or history.get("history") or []
                )
                assert isinstance(entries, list)

            # Delete
            del_resp = delete(f"/api/agents/{agent_id}")
            assert del_resp.status_code == 200
            cleanup._actions.clear()

            # Verify gone
            gone_resp = get(f"/api/agents/{agent_id}")
            assert gone_resp.status_code == 404

    def test_agent_clone(self, registry_available):
        agent_id = unique_id("e2e_clone_src")
        clone_id = unique_id("e2e_clone_dst")
        with E2ECleanup() as cleanup:
            post_json(
                "/api/agents",
                {
                    "agent_id": agent_id,
                    "display_name": "Clone Source",
                    "category": NEUTRAL_AGENT_CATEGORY,
                    "enabled": False,
                },
            )
            cleanup.add("DELETE", f"/api/agents/{agent_id}")

            clone_resp = post_json(
                f"/api/agents/{agent_id}/clone",
                {
                    "agent_id": clone_id,
                    "display_name": "Clone Target",
                },
            )
            if clone_resp.status_code in (200, 201):
                cleanup.add("DELETE", f"/api/agents/{clone_id}")
                body = clone_resp.json()
                assert body.get("status") == "cloned"

    def test_agent_contract_validation(self, registry_available):
        agent_id = unique_id("e2e_validate")
        with E2ECleanup() as cleanup:
            post_json(
                "/api/agents",
                {
                    "agent_id": agent_id,
                    "display_name": "Validation Target",
                    "category": NEUTRAL_AGENT_CATEGORY,
                    "enabled": False,
                },
            )
            cleanup.add("DELETE", f"/api/agents/{agent_id}")

            resp = get(f"/api/agents/{agent_id}/contract-validation")
            assert resp.status_code == 200
            body = resp.json()
            assert "validation" in body


# ── Tools ────────────────────────────────────────────────────────────────────


class TestToolCRUD:
    def test_tool_crud_lifecycle(self, registry_available):
        tool_id = unique_id("e2e_tool")
        with E2ECleanup() as cleanup:
            create_resp = post_json(
                "/api/tools",
                {
                    "tool_id": tool_id,
                    "display_name": f"E2E Tool {tool_id}",
                    "description": "Temporary E2E test tool",
                    "enabled": False,
                },
            )
            assert create_resp.status_code in (200, 201), (
                f"tool create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            cleanup.add("DELETE", f"/api/tools/{tool_id}")

            # Read
            read_resp = get(f"/api/tools/{tool_id}")
            assert read_resp.status_code == 200

            # Patch
            patch_resp = patch_json(
                f"/api/tools/{tool_id}",
                {
                    "description": "Updated by E2E",
                },
            )
            assert patch_resp.status_code == 200

            # Toggle — may 400 if contract validation blocks enabling
            toggle_resp = patch_json(f"/api/tools/{tool_id}/toggle")
            assert toggle_resp.status_code in (200, 400)
            if toggle_resp.status_code == 200:
                assert "enabled" in toggle_resp.json()

            # Delete
            del_resp = delete(f"/api/tools/{tool_id}")
            assert del_resp.status_code == 200
            cleanup._actions.clear()

            # Verify gone
            assert get(f"/api/tools/{tool_id}").status_code == 404

    def test_tool_health_endpoint(self, registry_available):
        resp = get("/api/tools/health")
        # Health may return 200 or 503 depending on tool resolver state.
        assert resp.status_code in (200, 503)


# ── Skills ─────────────────────────────────────────────────────────────


class TestSkillCRUD:
    def test_skill_crud_lifecycle(self, registry_available):
        cap_id = unique_id("e2e_cap")
        with E2ECleanup() as cleanup:
            create_resp = post_json(
                "/api/skills",
                {
                    "skill_id": cap_id,
                    "display_name": f"E2E Skill {cap_id}",
                    "description": "Temporary E2E test skill",
                    "enabled": False,
                },
            )
            assert create_resp.status_code in (200, 201), (
                f"skill create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            cleanup.add("DELETE", f"/api/skills/{cap_id}")

            # Read
            read_resp = get(f"/api/skills/{cap_id}")
            assert read_resp.status_code == 200

            # Patch
            patch_resp = patch_json(
                f"/api/skills/{cap_id}",
                {
                    "description": "Updated by E2E",
                },
            )
            assert patch_resp.status_code == 200

            # Toggle — may 400 (contract requires outputs to enable)
            toggle_resp = patch_json(f"/api/skills/{cap_id}/toggle")
            assert toggle_resp.status_code in (200, 400)
            if toggle_resp.status_code == 200:
                assert "enabled" in toggle_resp.json()

            # I/O management
            inputs_resp = get(f"/api/skills/{cap_id}/inputs")
            assert inputs_resp.status_code == 200

            outputs_resp = get(f"/api/skills/{cap_id}/outputs")
            assert outputs_resp.status_code == 200

            # Delete
            del_resp = delete(f"/api/skills/{cap_id}")
            assert del_resp.status_code == 200
            cleanup._actions.clear()

            assert get(f"/api/skills/{cap_id}").status_code == 404


# ── Workflows ────────────────────────────────────────────────────────────────


class TestWorkflowCRUD:
    def test_workflow_crud_lifecycle(self, registry_available):
        wf_id = unique_id("e2e_wf")
        with E2ECleanup() as cleanup:
            create_resp = post_json(
                "/api/workflows",
                {
                    "workflow_id": wf_id,
                    "display_name": f"E2E Workflow {wf_id}",
                    "description": "Temporary E2E test workflow",
                    "enabled": False,
                    "settings": PAYLOAD_INPUT_WORKFLOW_SETTINGS,
                },
            )
            assert create_resp.status_code in (200, 201), (
                f"workflow create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            cleanup.add("DELETE", f"/api/workflows/{wf_id}")

            # Read
            read_resp = get(f"/api/workflows/{wf_id}")
            assert read_resp.status_code == 200

            # Patch
            patch_resp = patch_json(
                f"/api/workflows/{wf_id}",
                {
                    "description": "Updated by E2E",
                    "expected_revision": read_resp.json().get("revision"),
                },
            )
            assert patch_resp.status_code == 200, (
                f"workflow patch failed: {patch_resp.status_code} {patch_resp.text[:300]}"
            )

            # Toggle — may 400 (a workflow without a runnable definition cannot be enabled)
            toggle_resp = patch_json(f"/api/workflows/{wf_id}/toggle")
            assert toggle_resp.status_code in (200, 400)

            # Steps and graph: one change set, the way every client authors them
            change = apply_workflow_changes(
                wf_id,
                linear_step_operations(
                    payload_transform_step("record_payload", "Record payload"),
                    {
                        "step_id": "done",
                        "step_kind": "stop_success",
                        "display_name": "Done",
                        "override": {"stop_success": {"message": "done"}},
                    },
                ),
            )
            assert change["valid"] is True, change["issues"]
            built = get(f"/api/workflows/{wf_id}").json()
            assert [step["step_id"] for step in built["steps"]] == ["record_payload", "done"]
            assert "phases" not in built

            # DAG
            dag_resp = get(f"/api/workflows/{wf_id}/dag")
            if dag_resp.status_code == 200:
                dag = dag_resp.json()
                assert "nodes" in dag or "edges" in dag

            # Contract validation
            val_resp = get(f"/api/workflows/{wf_id}/contract-validation")
            assert val_resp.status_code == 200

            # Delete
            del_resp = delete(f"/api/workflows/{wf_id}")
            assert del_resp.status_code == 200
            cleanup._actions.clear()

            assert get(f"/api/workflows/{wf_id}").status_code == 404


# ── Guardrails ───────────────────────────────────────────────────────────────


class TestGuardrailCRUD:
    def test_guardrail_crud_lifecycle(self, registry_available):
        with E2ECleanup() as cleanup:
            create_resp = post_json(
                "/api/guardrails",
                {
                    "display_name": f"E2E Guardrail {unique_id()}",
                    "mode": "deterministic",
                    "rule_type": "forbidden_patterns",
                    "description": "Temporary E2E guardrail",
                    "enabled": False,
                    "severity": "warning",
                    "config": {"patterns": ["e2e_forbidden_marker"]},
                    "agents": [NEUTRAL_AGENT_ID],
                },
            )
            assert create_resp.status_code in (200, 201), (
                f"guardrail create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            body = create_resp.json()
            guardrail_id = body.get("id") or body.get("guardrail_id")
            assert guardrail_id, f"No guardrail id in response: {body}"
            cleanup.add("DELETE", f"/api/guardrails/{guardrail_id}")

            # Read
            read_resp = get(f"/api/guardrails/{guardrail_id}")
            assert read_resp.status_code == 200

            # Patch
            patch_resp = patch_json(
                f"/api/guardrails/{guardrail_id}",
                {
                    "description": "Updated by E2E",
                },
            )
            assert patch_resp.status_code == 200

            # Toggle
            toggle_resp = patch_json(f"/api/guardrails/{guardrail_id}/toggle")
            assert toggle_resp.status_code == 200
            toggle_body = toggle_resp.json()
            assert "enabled" in toggle_body

            # List
            list_resp = get("/api/guardrails")
            assert list_resp.status_code == 200
            guardrails = (
                list_resp.json()
                if isinstance(list_resp.json(), list)
                else list_resp.json().get("guardrails", [])
            )
            found = [
                g
                for g in guardrails
                if g.get("id") == guardrail_id or g.get("guardrail_id") == guardrail_id
            ]
            assert found, f"Guardrail {guardrail_id} not in list"

            # Delete
            del_resp = delete(f"/api/guardrails/{guardrail_id}")
            assert del_resp.status_code == 200
            cleanup._actions.clear()


# ── Changelog & Cross-cutting ────────────────────────────────────────────────


class TestRegistryChangelog:
    def test_changelog_list(self, registry_available):
        resp = get("/api/changelog")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (list, dict))

    def test_changelog_stats(self, registry_available):
        resp = get("/api/changelog/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)

    def test_changelog_search(self, registry_available):
        resp = get("/api/changelog/search", params={"q": "e2e"})
        assert resp.status_code == 200

    def test_registry_validate(self, registry_available):
        resp = get("/api/changelog/validate")
        assert resp.status_code == 200

    def test_registry_pages_load(self, registry_available):
        for path in (
            "/registry",
            "/registry/agents",
            "/registry/tools",
            "/registry/skills",
            "/registry/workflows",
        ):
            resp = get(path)
            assert resp.status_code == 200, f"Registry page {path} failed: {resp.status_code}"


# ── Agent-Skill relationship ────────────────────────────────────────────


class TestAgentSkillRelationship:
    def test_agent_skill_link_unlink(self, registry_available):
        """Link an existing enabled skill to a new agent, then unlink."""
        agent_id = unique_id("e2e_rel_agent")
        with E2ECleanup() as cleanup:
            # Create agent
            create_resp = post_json(
                "/api/agents",
                {
                    "agent_id": agent_id,
                    "display_name": "Relationship Agent",
                    "category": NEUTRAL_AGENT_CATEGORY,
                    "enabled": True,
                },
            )
            if create_resp.status_code not in (200, 201):
                pytest.skip(
                    f"Cannot create agent: {create_resp.status_code} {create_resp.text[:200]}"
                )
            cleanup.add("DELETE", f"/api/agents/{agent_id}")

            # Find an existing enabled skill to link
            caps_resp = get("/api/skills")
            caps_list = (
                caps_resp.json()
                if isinstance(caps_resp.json(), list)
                else caps_resp.json().get("skills", [])
            )
            enabled_caps = [c for c in caps_list if c.get("enabled")]
            if not enabled_caps:
                pytest.skip("No enabled skills to test linking")

            cap_id = enabled_caps[0].get("skill_id") or enabled_caps[0].get("id")

            # Link
            link_resp = post_json(
                f"/api/agents/{agent_id}/skills",
                {
                    "skill_id": cap_id,
                },
            )
            # May fail with 400/404 if contract validation blocks — that's an acceptable skip.
            if link_resp.status_code in (400, 404):
                pytest.skip(f"Cannot link skill: {link_resp.status_code} {link_resp.text[:200]}")
            assert link_resp.status_code == 200

            # Verify linked
            caps_resp = get(f"/api/agents/{agent_id}/skills")
            assert caps_resp.status_code == 200
            caps = (
                caps_resp.json()
                if isinstance(caps_resp.json(), list)
                else (caps_resp.json() or {}).get("skills", [])
            )
            cap_ids = [c.get("skill_id") or c.get("id") for c in caps]
            assert cap_id in cap_ids

            # Unlink
            unlink_resp = delete(f"/api/agents/{agent_id}/skills/{cap_id}")
            assert unlink_resp.status_code == 200
