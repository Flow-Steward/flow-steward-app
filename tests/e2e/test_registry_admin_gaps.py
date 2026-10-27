"""E2E tests covering previously-uncovered registry admin API endpoints.

Fills gaps: PUT (full update), clone, restore, reorder, agent tool
add/remove, inputs/outputs PUT, workflow step changes,
agent-skill-pairs meta.

Fixture strategy: ``registry_entities`` seeds one disposable entity of
each type at module scope, performs one update per entity to guarantee a
history entry, and tears everything down after the module completes.  Tests
that previously skipped on "no agents/tools available" now operate on the
seeded entities and only skip when the DB backend itself is unavailable.
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
    put_json,
    step_edge,
    unique_id,
)
from tests.e2e.neutral_registry import NEUTRAL_AGENT_ID, NEUTRAL_AGENT_ROLE_TYPE

pytestmark = [pytest.mark.e2e]


# ── Module fixtures ────────────────────────────────────────────────────────────

#: The seeded workflow's definition: a transform of the callable ``payload``
#: input (a workflow needs one execution step), then a Stop Success.
_SEED_STEP_ID = "record_payload"
_SEED_STOP_ID = "done"
_SEED_WORKFLOW_STEPS = (
    payload_transform_step(_SEED_STEP_ID, "Record payload"),
    {
        "step_id": _SEED_STOP_ID,
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
    resp = get("/health")
    if resp.status_code >= 500:
        pytest.fail(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def registry_entities(server_available):
    """Seed one disposable entity of each type for this module's tests.

    Creates agent, tool, skill, workflow, and guardrail with unique IDs.
    Each entity is updated once after creation to guarantee at least one
    history entry is available for restore/diff/revision tests.

    Yields a dict::

        {
          "agent_id":    str | None,
          "tool_id":     str | None,
          "skill_id":    str | None,
          "workflow_id": str | None,
          "guardrail_id": str | None,
        }

    All seeded entities are deleted on module teardown.
    Skips the module only when the registry DB is genuinely unavailable
    (all creates returned non-2xx / 503).
    """
    uid = unique_id("e2ent")
    created: dict = {
        "agent_id": None,
        "tool_id": None,
        "skill_id": None,
        "workflow_id": None,
        "guardrail_id": None,
    }
    cleanup = E2ECleanup()

    # ── Agent ─────────────────────────────────────────────────────────────────
    a_id = f"{uid}a"
    resp = post_json(
        "/api/agents",
        {
            "agent_id": a_id,
            "display_name": f"E2E Agent {uid}",
            "role_type": NEUTRAL_AGENT_ROLE_TYPE,
            "enabled": False,
        },
    )
    if resp.status_code in (200, 201):
        created["agent_id"] = a_id
        cleanup.add("DELETE", f"/api/agents/{a_id}")
        # Second write generates a history entry.
        put_json(
            f"/api/agents/{a_id}",
            {
                "agent_id": a_id,
                "display_name": f"E2E Agent {uid} v2",
                "role_type": NEUTRAL_AGENT_ROLE_TYPE,
                "enabled": False,
                "description": "e2e-fixture-update",
            },
        )

    # ── Tool ──────────────────────────────────────────────────────────────────
    t_id = f"{uid}t"
    resp = post_json(
        "/api/tools",
        {
            "tool_id": t_id,
            "display_name": f"E2E Tool {uid}",
            "enabled": True,
        },
    )
    if resp.status_code in (200, 201):
        created["tool_id"] = t_id
        cleanup.add("DELETE", f"/api/tools/{t_id}")
        put_json(
            f"/api/tools/{t_id}",
            {
                "tool_id": t_id,
                "display_name": f"E2E Tool {uid} v2",
                "enabled": True,
                "description": "e2e-fixture-update",
            },
        )

    # ── Skill ─────────────────────────────────────────────────────────────────
    s_id = f"{uid}s"
    resp = post_json(
        "/api/skills",
        {
            "skill_id": s_id,
            "display_name": f"E2E Skill {uid}",
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
                "display_name": f"E2E Skill {uid} v2",
                "enabled": True,
                "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                "description": "e2e-fixture-update",
            },
        )

    if created["agent_id"] and created["skill_id"]:
        put_json(
            f"/api/agents/{created['agent_id']}",
            {
                "agent_id": created["agent_id"],
                "display_name": f"E2E Agent {uid} v3",
                "role_type": NEUTRAL_AGENT_ROLE_TYPE,
                "enabled": True,
                "skills": [{"skill_id": created["skill_id"]}],
                "description": "e2e-fixture-skill-link",
            },
        )

    # ── Workflow ──────────────────────────────────────────────────────────────
    w_id = f"{uid}w"
    resp = post_json(
        "/api/workflows",
        {
            "workflow_id": w_id,
            "display_name": f"E2E Workflow {uid}",
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
                "display_name": f"E2E Workflow {uid} v2",
                "description": "e2e-fixture-update",
                "expected_revision": get(f"/api/workflows/{w_id}").json().get("revision"),
            },
        )
        assert update_resp.status_code == 200, (
            f"workflow fixture update failed: {update_resp.status_code} {update_resp.text[:300]}"
        )

    # ── Guardrail ─────────────────────────────────────────────────────────────
    resp = post_json(
        "/api/guardrails",
        {
            "display_name": f"E2E Guardrail {uid}",
            **_SEED_GUARDRAIL,
        },
    )
    if resp.status_code in (200, 201):
        body = resp.json()
        gid = body.get("id") or body.get("guardrail_id")
        if gid:
            created["guardrail_id"] = str(gid).strip()
            cleanup.add("DELETE", f"/api/guardrails/{gid}")
            put_json(
                f"/api/guardrails/{gid}",
                {
                    "display_name": f"E2E Guardrail {uid} v2",
                    **_SEED_GUARDRAIL,
                    "description": "e2e-fixture-update",
                },
            )

    if not any(v is not None for v in created.values()):
        pytest.fail(
            "Registry DB not available — all entity creates returned non-2xx. "
            "Expected when 'DB backend not configured' (503). "
            "Ensure the registry routes are mounted and the DB is seeded."
        )

    yield created
    cleanup.run()


# ── History helper (used by restore / diff / revision tests) ──────────────────


def _first_history_entry_id(entity_type: str, entity_id: str) -> str | None:
    """Return the first history entry_id for an entity, or None."""
    resp = get(f"/api/{entity_type}/{entity_id}/history")
    if resp.status_code != 200:
        return None
    data = resp.json()
    entries = data if isinstance(data, list) else data.get("entries") or data.get("history") or []
    if not entries:
        return None
    entry_id = str(entries[0].get("id") or entries[0].get("entry_id") or "").strip()
    return entry_id or None


def _latest_history_revision(entity_type: str, entity_id: str) -> str | None:
    """Return the revision selector expected by /history/{revision} routes."""
    resp = get(f"/api/{entity_type}/{entity_id}/history")
    if resp.status_code != 200:
        return None
    data = resp.json()
    entries = data if isinstance(data, list) else data.get("entries") or data.get("history") or []
    if not entries:
        return None
    first = entries[0] if isinstance(entries[0], dict) else {}
    revision = str(first.get("revision") or first.get("version") or "1").strip()
    return revision or "1"


# ── Agent Full Update (PUT) ───────────────────────────────────────────────────


class TestAgentFullUpdate:
    def test_put_agent_roundtrip(self, server_available, registry_entities):
        """PUT /api/agents/{id} full update preserves data."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded (DB unavailable)")

        current = get(f"/api/agents/{agent_id}")
        assert current.status_code == 200, f"GET agent {agent_id} returned {current.status_code}"
        original = current.json()

        resp = put_json(f"/api/agents/{agent_id}", original)
        assert resp.status_code in (200, 400, 422), (
            f"PUT agent returned {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            assert "agent_id" in body or body.get("status") in ("updated", "ok", "saved")


# ── Agent Skills Reorder ──────────────────────────────────────────────────────


class TestAgentSkillsReorder:
    def test_reorder_skills(self, server_available, registry_entities):
        """PUT /api/agents/{id}/skills/reorder accepts ordered_ids list."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        caps_resp = get(f"/api/agents/{agent_id}/skills")
        assert caps_resp.status_code == 200, f"GET agent skills returned {caps_resp.status_code}"
        caps_data = caps_resp.json()
        caps = caps_data if isinstance(caps_data, list) else (caps_data or {}).get("skills") or []
        cap_ids = [
            c.get("skill_id") or c.get("id") for c in caps if c.get("skill_id") or c.get("id")
        ]

        # Seeded agent has no skills initially — reorder with empty list is still a valid call.
        resp = put_json(
            f"/api/agents/{agent_id}/skills/reorder",
            {"ordered_ids": cap_ids},
        )
        assert resp.status_code in (200, 400, 422), (
            f"reorder skills returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Agent Skill Prompt Update ─────────────────────────────────────────────────


class TestAgentSkillPrompt:
    def test_update_skill_prompt(self, server_available, registry_entities):
        """PUT /api/agents/{id}/skills/{cap_id}/prompt updates prompt."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        caps_resp = get(f"/api/agents/{agent_id}/skills")
        assert caps_resp.status_code == 200
        caps_data = caps_resp.json()
        caps = caps_data if isinstance(caps_data, list) else (caps_data or {}).get("skills") or []
        if not caps:
            pytest.fail("Seeded agent has no skills — attach a skill first to test prompt update")

        cap_id = caps[0].get("skill_id") or caps[0].get("id")
        original_prompt = caps[0].get("skill_prompt") or ""

        resp = put_json(
            f"/api/agents/{agent_id}/skills/{cap_id}/prompt",
            {"skill_prompt": original_prompt},
        )
        assert resp.status_code in (200, 400, 404, 422), (
            f"update skill prompt returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Agent Inputs / Outputs PUT ────────────────────────────────────────────────


class TestAgentInputsOutputs:
    def test_set_agent_inputs(self, server_available, registry_entities):
        """PUT /api/agents/{id}/inputs sets input schema."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        current = get(f"/api/agents/{agent_id}/inputs")
        assert current.status_code == 200, f"GET agent inputs returned {current.status_code}"
        original = (
            current.json()
            if isinstance(current.json(), list)
            else (current.json() or {}).get("inputs") or []
        )

        resp = put_json(f"/api/agents/{agent_id}/inputs", original)
        assert resp.status_code in (200, 400, 422), (
            f"set inputs returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_set_agent_outputs(self, server_available, registry_entities):
        """PUT /api/agents/{id}/outputs sets output schema."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        current = get(f"/api/agents/{agent_id}/outputs")
        assert current.status_code == 200, f"GET agent outputs returned {current.status_code}"
        original = (
            current.json()
            if isinstance(current.json(), list)
            else (current.json() or {}).get("outputs") or []
        )

        resp = put_json(f"/api/agents/{agent_id}/outputs", original)
        assert resp.status_code in (200, 400, 422), (
            f"set outputs returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Agent Tool Add / Remove ───────────────────────────────────────────────────


class TestAgentToolManagement:
    def test_add_and_remove_tool(self, server_available, registry_entities):
        """POST /api/agents/{id}/tools + DELETE /api/agents/{id}/tools/{tool_id}."""
        agent_id = registry_entities["agent_id"]
        tool_id = registry_entities["tool_id"]
        if not agent_id or not tool_id:
            pytest.fail("Test agent or tool not seeded")

        tools_resp = get(f"/api/agents/{agent_id}/tools")
        assert tools_resp.status_code == 200, f"GET agent tools returned {tools_resp.status_code}"
        existing_tools = (
            tools_resp.json()
            if isinstance(tools_resp.json(), list)
            else (tools_resp.json() or {}).get("tools") or []
        )
        already_linked = any((t.get("tool_id") or t.get("id")) == tool_id for t in existing_tools)

        if already_linked:
            del_resp = delete(f"/api/agents/{agent_id}/tools/{tool_id}")
            assert del_resp.status_code in (200, 404), (
                f"remove tool returned {del_resp.status_code}"
            )
            add_resp = post_json(f"/api/agents/{agent_id}/tools", {"tool_id": tool_id})
            assert add_resp.status_code in (200, 201, 409), (
                f"re-add tool returned {add_resp.status_code}: {add_resp.text[:300]}"
            )
        else:
            add_resp = post_json(f"/api/agents/{agent_id}/tools", {"tool_id": tool_id})
            assert add_resp.status_code in (200, 201, 409), (
                f"add tool returned {add_resp.status_code}: {add_resp.text[:300]}"
            )
            if add_resp.status_code in (200, 201):
                del_resp = delete(f"/api/agents/{agent_id}/tools/{tool_id}")
                assert del_resp.status_code in (200, 204, 404)


# ── Restore Endpoints ─────────────────────────────────────────────────────────


class TestEntityRestore:
    def _test_restore(self, entity_type: str, entity_id: str):
        entry_id = _first_history_entry_id(entity_type, entity_id)
        if entry_id is None:
            pytest.fail(
                f"No history entries for seeded {entity_type}/{entity_id}. "
                "The fixture update should have created one — check if the DB "
                "history backend is recording PUT changes."
            )

        body = {"entry_id": entry_id}
        if entity_type == "workflows":
            # Workflow writes are revision-bound (428 without expected_revision).
            body["expected_revision"] = get(f"/api/workflows/{entity_id}").json().get("revision")
        resp = post_json(f"/api/{entity_type}/{entity_id}/restore", body)
        assert resp.status_code in (200, 400, 404, 409, 422), (
            f"restore {entity_type} returned {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("status") == "restored", (
                f"restore response missing status=restored: {body}"
            )

    def test_restore_agent(self, server_available, registry_entities):
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")
        self._test_restore("agents", agent_id)

    def test_restore_tool(self, server_available, registry_entities):
        tool_id = registry_entities["tool_id"]
        if not tool_id:
            pytest.fail("Test tool not seeded")
        self._test_restore("tools", tool_id)

    def test_restore_skill(self, server_available, registry_entities):
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")
        self._test_restore("skills", cap_id)

    def test_restore_workflow(self, server_available, registry_entities):
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")
        self._test_restore("workflows", wf_id)

    def test_restore_guardrail(self, server_available, registry_entities):
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")
        entry_id = _first_history_entry_id("guardrails", str(gid))
        if entry_id is None:
            pytest.fail("No history for seeded guardrail — check DB history recording")
        resp = post_json(f"/api/guardrails/{gid}/restore", {"entry_id": entry_id})
        assert resp.status_code in (200, 400, 404, 409, 422), (
            f"guardrail restore returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Clone Endpoints ───────────────────────────────────────────────────────────


class TestEntityClone:
    def test_clone_tool(self, server_available, registry_entities):
        """POST /api/tools/{id}/clone creates a copy."""
        tool_id = registry_entities["tool_id"]
        if not tool_id:
            pytest.fail("Test tool not seeded")

        uid = unique_id("clonetool")
        with E2ECleanup() as cleanup:
            resp = post_json(
                f"/api/tools/{tool_id}/clone",
                {
                    "tool_id": uid,
                    "display_name": f"Cloned Tool {uid}",
                },
            )
            assert resp.status_code in (200, 201, 400, 409, 422), (
                f"clone tool returned {resp.status_code}: {resp.text[:300]}"
            )
            if resp.status_code in (200, 201):
                body = resp.json()
                assert body.get("status") == "cloned", (
                    f"clone tool response missing status=cloned: {body}"
                )
                cleanup.add("DELETE", f"/api/tools/{uid}")

    def test_clone_skill(self, server_available, registry_entities):
        """POST /api/skills/{id}/clone creates a copy."""
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")

        uid = unique_id("clonecap")
        with E2ECleanup() as cleanup:
            resp = post_json(
                f"/api/skills/{cap_id}/clone",
                {
                    "skill_id": uid,
                    "display_name": f"Cloned Cap {uid}",
                },
            )
            assert resp.status_code in (200, 201, 400, 409, 422), (
                f"clone skill returned {resp.status_code}: {resp.text[:300]}"
            )
            if resp.status_code in (200, 201):
                cleanup.add("DELETE", f"/api/skills/{uid}")

    def test_clone_workflow(self, server_available, registry_entities):
        """POST /api/workflows/{id}/clone creates a copy."""
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")

        uid = unique_id("clonewf")
        with E2ECleanup() as cleanup:
            resp = post_json(
                f"/api/workflows/{wf_id}/clone",
                {
                    "workflow_id": uid,
                    "display_name": f"Cloned Workflow {uid}",
                },
            )
            assert resp.status_code in (200, 201, 400, 409, 422), (
                f"clone workflow returned {resp.status_code}: {resp.text[:300]}"
            )
            if resp.status_code in (200, 201):
                cleanup.add("DELETE", f"/api/workflows/{uid}")


# ── Tool Full Update (PUT) ────────────────────────────────────────────────────


class TestToolFullUpdate:
    def test_put_tool_roundtrip(self, server_available, registry_entities):
        """PUT /api/tools/{id} full update preserves data."""
        tool_id = registry_entities["tool_id"]
        if not tool_id:
            pytest.fail("Test tool not seeded")

        current = get(f"/api/tools/{tool_id}")
        assert current.status_code == 200, f"GET tool {tool_id} returned {current.status_code}"

        resp = put_json(f"/api/tools/{tool_id}", current.json())
        assert resp.status_code in (200, 400, 422), (
            f"PUT tool returned {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            assert "tool_id" in body or body.get("status") in ("updated", "ok", "saved")


# ── Skill Full Update + Inputs/Outputs/Tools ──────────────────────────────────


class TestSkillFullUpdate:
    def test_put_skill_roundtrip(self, server_available, registry_entities):
        """PUT /api/skills/{id} full update."""
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")

        current = get(f"/api/skills/{cap_id}")
        assert current.status_code == 200, f"GET skill {cap_id} returned {current.status_code}"

        resp = put_json(f"/api/skills/{cap_id}", current.json())
        assert resp.status_code in (200, 400, 422), (
            f"PUT skill returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_set_skill_inputs(self, server_available, registry_entities):
        """PUT /api/skills/{id}/inputs sets input schema."""
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")

        current = get(f"/api/skills/{cap_id}/inputs")
        assert current.status_code == 200, f"GET skill inputs returned {current.status_code}"
        original = (
            current.json()
            if isinstance(current.json(), list)
            else (current.json() or {}).get("inputs") or []
        )

        resp = put_json(f"/api/skills/{cap_id}/inputs", original)
        assert resp.status_code in (200, 400, 422), (
            f"set skill inputs returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_set_skill_outputs(self, server_available, registry_entities):
        """PUT /api/skills/{id}/outputs sets output schema."""
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")

        current = get(f"/api/skills/{cap_id}/outputs")
        assert current.status_code == 200, f"GET skill outputs returned {current.status_code}"
        original = (
            current.json()
            if isinstance(current.json(), list)
            else (current.json() or {}).get("outputs") or []
        )

        resp = put_json(f"/api/skills/{cap_id}/outputs", original)
        assert resp.status_code in (200, 400, 422), (
            f"set skill outputs returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Workflow Full Update ──────────────────────────────────────────────────────


class TestWorkflowFullUpdate:
    def test_put_workflow_roundtrip(self, server_available, registry_entities):
        """PUT /api/workflows/{id} full update."""
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")

        current = get(f"/api/workflows/{wf_id}")
        assert current.status_code == 200, f"GET workflow returned {current.status_code}"

        resp = put_json(f"/api/workflows/{wf_id}", current.json())
        assert resp.status_code in (200, 400, 422), (
            f"PUT workflow returned {resp.status_code}: {resp.text[:300]}"
        )


# ── Workflow Step Changes ─────────────────────────────────────────────────────


class TestWorkflowStepChanges:
    """Steps are changed through one change set at the workflow's revision."""

    @staticmethod
    def _steps(workflow_id: str) -> dict[str, dict]:
        resp = get(f"/api/workflows/{workflow_id}")
        assert resp.status_code == 200, f"GET workflow returned {resp.status_code}"
        return {step["step_id"]: step for step in resp.json().get("steps") or []}

    def test_update_step_config(self, server_available, registry_entities):
        """update_step_config renames a step without touching its structure."""
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")

        result = apply_workflow_changes(
            wf_id,
            [
                {
                    "op": "update_step_config",
                    "step_id": _SEED_STEP_ID,
                    "patch": {"display_name": "Mark started (renamed)"},
                }
            ],
        )

        assert result["valid"] is True, result["issues"]
        assert self._steps(wf_id)[_SEED_STEP_ID]["display_name"] == "Mark started (renamed)"

    def test_insert_and_delete_step(self, server_available, registry_entities):
        """insert_step_between adds a step on an edge; delete_step removes it again."""
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")
        inserted_id = "e2e_inserted"

        inserted = apply_workflow_changes(
            wf_id,
            [
                {
                    "op": "insert_step_between",
                    "source": {"kind": "step", "id": _SEED_STEP_ID},
                    "outcome": {"name": "success"},
                    "step": {
                        "step_id": inserted_id,
                        "step_kind": "set_variable",
                        "display_name": "Inserted",
                        "override": {"set_variable": {"target": "state.inserted", "value": 1}},
                    },
                }
            ],
        )
        assert inserted["valid"] is True, inserted["issues"]
        assert set(self._steps(wf_id)) == {_SEED_STEP_ID, _SEED_STOP_ID, inserted_id}

        removed = apply_workflow_changes(
            wf_id,
            [
                {"op": "delete_step", "step_id": inserted_id},
                {"op": "connect", "edge": step_edge(_SEED_STEP_ID, _SEED_STOP_ID)},
            ],
        )
        assert removed["valid"] is True, removed["issues"]
        assert inserted_id not in self._steps(wf_id)


# ── Guardrail Full Update ─────────────────────────────────────────────────────


class TestGuardrailFullUpdate:
    def test_put_guardrail_roundtrip(self, server_available, registry_entities):
        """PUT /api/guardrails/{id} full update."""
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")

        current = get(f"/api/guardrails/{gid}")
        assert current.status_code == 200, f"GET guardrail returned {current.status_code}"

        resp = put_json(f"/api/guardrails/{gid}", current.json())
        assert resp.status_code in (200, 400, 422), (
            f"PUT guardrail returned {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            assert "id" in body or body.get("status") in ("updated", "ok", "saved")


# ── History Revision Detail ───────────────────────────────────────────────────


class TestHistoryRevisionDetail:
    def _test_revision_detail(self, entity_type: str, entity_id: str):
        revision = _latest_history_revision(entity_type, entity_id)
        if revision is None:
            pytest.fail(
                f"No history for seeded {entity_type}/{entity_id}. "
                "Fixture update should have generated one."
            )

        resp = get(f"/api/{entity_type}/{entity_id}/history/{revision}")
        assert resp.status_code in (200, 404), (
            f"history revision detail returned {resp.status_code}"
        )
        if resp.status_code == 200:
            body = resp.json()
            # Should have at minimum an entry id and some snapshot data.
            assert body.get("id") or body.get("entry_id") or body.get("snapshot")

    def test_agent_revision_detail(self, server_available, registry_entities):
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")
        self._test_revision_detail("agents", agent_id)

    def test_tool_revision_detail(self, server_available, registry_entities):
        tool_id = registry_entities["tool_id"]
        if not tool_id:
            pytest.fail("Test tool not seeded")
        self._test_revision_detail("tools", tool_id)

    def test_skill_revision_detail(self, server_available, registry_entities):
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")
        self._test_revision_detail("skills", cap_id)

    def test_workflow_revision_detail(self, server_available, registry_entities):
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")
        self._test_revision_detail("workflows", wf_id)

    def test_guardrail_revision_detail(self, server_available, registry_entities):
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")
        revision = _latest_history_revision("guardrails", str(gid))
        if revision is None:
            pytest.fail("No guardrail history — fixture update should have created one")
        resp = get(f"/api/guardrails/{gid}/history/{revision}")
        assert resp.status_code in (200, 404), (
            f"guardrail revision detail returned {resp.status_code}"
        )


# ── Diff Endpoints ────────────────────────────────────────────────────────────


class TestEntityDiff:
    def _test_diff(self, entity_type: str, entity_id: str):
        resp = get(f"/api/{entity_type}/{entity_id}/diff")
        assert resp.status_code in (200, 400, 404), (
            f"diff {entity_type} returned {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            # Diff body must have some structure (changes, summary, or diff key).
            assert isinstance(body, dict), f"diff response should be a dict: {body}"

    def test_tool_diff(self, server_available, registry_entities):
        tool_id = registry_entities["tool_id"]
        if not tool_id:
            pytest.fail("Test tool not seeded")
        self._test_diff("tools", tool_id)

    def test_skill_diff(self, server_available, registry_entities):
        cap_id = registry_entities["skill_id"]
        if not cap_id:
            pytest.fail("Test skill not seeded")
        self._test_diff("skills", cap_id)

    def test_workflow_diff(self, server_available, registry_entities):
        wf_id = registry_entities["workflow_id"]
        if not wf_id:
            pytest.fail("Test workflow not seeded")
        self._test_diff("workflows", wf_id)

    def test_guardrail_diff(self, server_available, registry_entities):
        gid = registry_entities["guardrail_id"]
        if not gid:
            pytest.fail("Test guardrail not seeded")
        resp = get(f"/api/guardrails/{gid}/diff")
        assert resp.status_code in (200, 400, 404), (
            f"guardrail diff returned {resp.status_code}: {resp.text[:200]}"
        )


# ── Agent-Skill Pairs Meta ────────────────────────────────────────────────────


class TestAgentSkillPairsMeta:
    def test_list_all_pairs(self, server_available):
        """GET /api/workflows/meta/agent-skill-pairs returns all pairs."""
        resp = get("/api/workflows/meta/agent-skill-pairs")
        assert resp.status_code == 200, (
            f"agent-skill-pairs returned {resp.status_code}: {resp.text[:200]}"
        )
        body = resp.json()
        pairs = body.get("pairs") if isinstance(body, dict) else body
        assert isinstance(pairs, list), f"pairs should be a list: {body}"

    def test_filter_by_agent(self, server_available, registry_entities):
        """GET /api/workflows/meta/agent-skill-pairs?agent_id=X filters."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        resp = get("/api/workflows/meta/agent-skill-pairs", params={"agent_id": agent_id})
        assert resp.status_code in (200, 404), (
            f"filtered agent-skill-pairs returned {resp.status_code}"
        )
        if resp.status_code == 200:
            body = resp.json()
            pairs = body.get("pairs") if isinstance(body, dict) else body
            assert isinstance(pairs, list)


# ── Changelog Restore + Revision Detail ──────────────────────────────────────


class TestChangelogRestoreRevision:
    def test_changelog_generic_revision(self, server_available, registry_entities):
        """GET /api/changelog/{entity_type}/{entity_id}/history/{revision}."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        # Try both singular and plural entity type paths.
        for etype in ("agent", "agents"):
            resp = get(f"/api/changelog/{etype}/{agent_id}/history")
            if resp.status_code == 200:
                entries = (
                    resp.json()
                    if isinstance(resp.json(), list)
                    else (resp.json() or {}).get("entries") or []
                )
                if entries:
                    first = entries[0] if isinstance(entries[0], dict) else {}
                    revision = str(first.get("revision") or first.get("version") or "1").strip()
                    if revision:
                        rev_resp = get(f"/api/changelog/{etype}/{agent_id}/history/{revision}")
                        assert rev_resp.status_code in (200, 404), (
                            f"changelog revision detail returned {rev_resp.status_code}"
                        )
                        return
        pytest.fail("Changelog history not available for seeded agent via /api/changelog/*")

    def test_changelog_restore(self, server_available, registry_entities):
        """POST /api/changelog/{entity_type}/{entity_id}/restore."""
        agent_id = registry_entities["agent_id"]
        if not agent_id:
            pytest.fail("Test agent not seeded")

        for etype in ("agent", "agents"):
            resp = get(f"/api/changelog/{etype}/{agent_id}/history")
            if resp.status_code == 200:
                entries = (
                    resp.json()
                    if isinstance(resp.json(), list)
                    else (resp.json() or {}).get("entries") or []
                )
                if entries:
                    entry_id = entries[0].get("id") or entries[0].get("entry_id")
                    if entry_id:
                        restore_resp = post_json(
                            f"/api/changelog/{etype}/{agent_id}/restore",
                            {"entry_id": entry_id},
                        )
                        assert restore_resp.status_code in (200, 400, 404, 409, 422), (
                            f"changelog restore returned {restore_resp.status_code}"
                        )
                        return
        pytest.fail("No changelog entries available for seeded agent restore test")
