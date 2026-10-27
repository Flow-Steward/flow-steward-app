"""Neutral registry fixture for the live E2E and UI suites.

Flow Steward 1.0 ships an empty built-in registry catalog: no agents, skills or
workflows are seeded. The live suites still need one runnable agent, skill and
workflow to exercise generic behaviour (task submission, approvals, retries,
scheduling, registry reads, search), so this module provisions a small neutral
set through the public registry API at session start.

The entities are created idempotently: an existing entity is left as it is,
a missing one is created. The ids are deliberately domain-neutral and do not
start with a generated-test prefix (``e2e_``/``test_``), so suites that audit
"product" workflows (import/export, entry/exit markers) see them too.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import requests

NEUTRAL_AGENT_ID = "writer"
NEUTRAL_AGENT_ROLE_TYPE = "researcher"
NEUTRAL_SKILL_ID = "draft_summary"
NEUTRAL_SKILL_OUTPUT = "summary_doc"
#: Workflow that pauses on an Approval step after the draft step.
NEUTRAL_WORKFLOW_ID = "weekly_report"
#: Same shape without the Approval step: runs straight to Stop Success.
NEUTRAL_AUTO_WORKFLOW_ID = "weekly_digest"
NEUTRAL_WORKFLOW_IDS = (NEUTRAL_WORKFLOW_ID, NEUTRAL_AUTO_WORKFLOW_ID)

RequestFn = Callable[..., requests.Response]

_NEUTRAL_SKILL: dict[str, Any] = {
    "skill_id": NEUTRAL_SKILL_ID,
    "display_name": "Draft Summary",
    "description": "Draft a short plain-language summary of the requested topic.",
    "enabled": True,
    "category": "analysis",
    "skill_type": "secondary",
    "instructions": ["Write a concise summary of the topic in a few sentences."],
    "expected_inputs": [
        {
            "name": "topic",
            "type": "text",
            "description": "Topic or request to summarise",
            "required": True,
        }
    ],
    "expected_outputs": [
        {
            "name": NEUTRAL_SKILL_OUTPUT,
            "type": "text",
            "description": "Short summary of the topic",
            "required": True,
        }
    ],
    "tags": ["e2e-fixture"],
}

_NEUTRAL_AGENT: dict[str, Any] = {
    "agent_id": NEUTRAL_AGENT_ID,
    "display_name": "Writer",
    "description": "Drafts short summaries for the neutral E2E workflows.",
    "role_type": NEUTRAL_AGENT_ROLE_TYPE,
    "enabled": True,
    "prompts": {
        "system": "You write short, factual summaries of the requested topic.",
    },
    "skills": [
        {
            "skill_id": NEUTRAL_SKILL_ID,
            "skill_prompt": "Summarise the topic in a few sentences.",
            "sort_order": 0,
        }
    ],
    "tags": ["e2e-fixture"],
}

#: Entry step of both neutral workflows.
NEUTRAL_START_STEP_ID = "workflow_start"
#: The agent step that drafts the summary.
NEUTRAL_DRAFT_STEP_ID = "draft"
#: The Approval step of the gated workflow, between the draft and the end.
NEUTRAL_APPROVAL_STEP_ID = "review"
NEUTRAL_SUCCESS_STEP_ID = "workflow_success"
NEUTRAL_REJECTED_STEP_ID = "workflow_rejected"

_START_STEP: dict[str, Any] = {
    "step_id": NEUTRAL_START_STEP_ID,
    "display_name": "Start workflow",
    "step_kind": "set_variable",
    "override": {"set_variable": {"target": "state.workflow_started", "value": True}},
}

_DRAFT_STEP: dict[str, Any] = {
    "step_id": NEUTRAL_DRAFT_STEP_ID,
    "display_name": "Draft summary",
    "step_kind": "agent",
    # An agent step runs the agent's primary skill, here NEUTRAL_SKILL_ID.
    "agent_id": NEUTRAL_AGENT_ID,
}

_APPROVAL_STEP: dict[str, Any] = {
    "step_id": NEUTRAL_APPROVAL_STEP_ID,
    "display_name": "Review draft",
    "step_kind": "approval",
    "override": {
        "approval": {
            "message": "Review the draft summary before it is finalised.",
            "show_outputs": [
                f"message.step.{NEUTRAL_DRAFT_STEP_ID}.artifacts.{NEUTRAL_SKILL_OUTPUT}"
            ],
        }
    },
}

_SUCCESS_STEP: dict[str, Any] = {
    "step_id": NEUTRAL_SUCCESS_STEP_ID,
    "display_name": "Complete workflow",
    "step_kind": "stop_success",
    "override": {"stop_success": {"message": "Workflow completed successfully."}},
}

_REJECTED_STEP: dict[str, Any] = {
    "step_id": NEUTRAL_REJECTED_STEP_ID,
    "display_name": "Draft rejected",
    "step_kind": "stop_fail",
    "override": {"stop_fail": {"message": "The reviewer rejected the draft."}},
}


def _edge(source: str, target: str, outcome: str = "success") -> dict[str, Any]:
    return {
        "source": {"kind": "step", "id": source},
        "outcome": {"name": outcome},
        "target": {"kind": "step", "id": target},
        "rework": False,
    }


def _definition_operations(*, gated: bool) -> list[dict[str, Any]]:
    """The change set that builds a neutral workflow: steps, entry, then edges.

    Both workflows run start -> draft -> end. The gated one pauses on an
    Approval step after the draft; a rejection ends the run as failed.
    """
    steps = [_START_STEP, _DRAFT_STEP]
    edges = [_edge(NEUTRAL_START_STEP_ID, NEUTRAL_DRAFT_STEP_ID)]
    if gated:
        steps += [_APPROVAL_STEP, _SUCCESS_STEP, _REJECTED_STEP]
        edges += [
            _edge(NEUTRAL_DRAFT_STEP_ID, NEUTRAL_APPROVAL_STEP_ID),
            _edge(NEUTRAL_APPROVAL_STEP_ID, NEUTRAL_SUCCESS_STEP_ID, "approved"),
            _edge(NEUTRAL_APPROVAL_STEP_ID, NEUTRAL_REJECTED_STEP_ID, "rejected"),
        ]
    else:
        steps.append(_SUCCESS_STEP)
        edges.append(_edge(NEUTRAL_DRAFT_STEP_ID, NEUTRAL_SUCCESS_STEP_ID))
    operations: list[dict[str, Any]] = [
        {"op": "create_step", "step": copy.deepcopy(step)} for step in steps
    ]
    operations.append({"op": "set_entry", "target": {"kind": "step", "id": NEUTRAL_START_STEP_ID}})
    operations.extend({"op": "connect", "edge": edge} for edge in edges)
    return operations


def _workflow_payload(workflow_id: str, display_name: str) -> dict:
    """Metadata only: a new workflow starts empty and disabled (steps come via /changes)."""
    return {
        "workflow_id": workflow_id,
        "display_name": display_name,
        "description": "Neutral E2E fixture workflow: drafts a short summary of the task.",
        "enabled": False,
        "tags": ["e2e-fixture"],
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["mcp", "schedule"],
                "input_mode": "passthrough",
                "strict_inputs": False,
                "expected_inputs": [{"name": "topic", "type": "paragraph", "required": False}],
                "output_mode": "none",
            }
        },
    }


#: (workflow_id, display_name, gated) for each neutral workflow.
_NEUTRAL_WORKFLOWS = (
    (NEUTRAL_WORKFLOW_ID, "Weekly Report", True),
    (NEUTRAL_AUTO_WORKFLOW_ID, "Weekly Digest", False),
)


def neutral_workflow_payloads() -> list[dict[str, Any]]:
    """Return the metadata create payloads for the neutral workflows (fresh copies)."""
    return [_workflow_payload(workflow_id, name) for workflow_id, name, _ in _NEUTRAL_WORKFLOWS]


def neutral_workflow_operations(workflow_id: str) -> list[dict[str, Any]]:
    """Return the change set that builds the definition of *workflow_id*."""
    for neutral_id, _, gated in _NEUTRAL_WORKFLOWS:
        if neutral_id == workflow_id:
            return _definition_operations(gated=gated)
    raise KeyError(workflow_id)


def neutral_skill_payload() -> dict[str, Any]:
    return copy.deepcopy(_NEUTRAL_SKILL)


def neutral_agent_payload() -> dict[str, Any]:
    return copy.deepcopy(_NEUTRAL_AGENT)


def _ensure_entity(
    request: RequestFn,
    base_url: str,
    *,
    collection: str,
    entity_id: str,
    payload: dict[str, Any],
) -> None:
    probe = request("GET", f"{base_url}/api/{collection}/{entity_id}")
    if probe.status_code == 200:
        return
    if probe.status_code in (401, 403):
        # A non-admin lane cannot provision registry entities; the admin lane does.
        return
    if probe.status_code != 404:
        raise AssertionError(
            f"Could not read neutral {collection} fixture {entity_id!r}: "
            f"{probe.status_code} {probe.text[:300]}"
        )
    created = request("POST", f"{base_url}/api/{collection}", json=payload)
    if created.status_code in (200, 201):
        return
    if created.status_code == 409:
        # Another worker created it between the probe and the create.
        return
    if created.status_code in (401, 403):
        return
    raise AssertionError(
        f"Could not create neutral {collection} fixture {entity_id!r}: "
        f"{created.status_code} {created.text[:500]}"
    )


def _ensure_workflow(request: RequestFn, base_url: str, payload: dict[str, Any]) -> None:
    """Create a neutral workflow, build its definition and enable it.

    A workflow is created metadata-only and disabled, its steps and graph come
    in one change set at its revision, and only then is it enabled. A workflow
    that exists without steps (an earlier run stopped half-way) is completed;
    one that already has steps is left as it is.
    """
    workflow_id = str(payload["workflow_id"])
    path = f"{base_url}/api/workflows/{workflow_id}"
    _ensure_entity(
        request, base_url, collection="workflows", entity_id=workflow_id, payload=payload
    )
    current = request("GET", path)
    if current.status_code != 200:
        # A lane that cannot read or create registry entities leaves it to the admin lane.
        return
    workflow = current.json() or {}
    if not workflow.get("steps"):
        changed = request(
            "POST",
            f"{path}/changes",
            json={
                "expected_revision": workflow.get("revision"),
                "operations": neutral_workflow_operations(workflow_id),
            },
        )
        if changed.status_code in (401, 403):
            return
        if changed.status_code != 200 or not (changed.json() or {}).get("valid", True):
            raise AssertionError(
                f"Could not build neutral workflow {workflow_id!r}: "
                f"{changed.status_code} {changed.text[:500]}"
            )
        workflow = request("GET", path).json() or {}
    if workflow.get("enabled"):
        return
    enabled = request(
        "PATCH",
        path,
        json={"enabled": True, "expected_revision": workflow.get("revision")},
    )
    if enabled.status_code == 200 or enabled.status_code in (401, 403):
        return
    raise AssertionError(
        f"Could not enable neutral workflow {workflow_id!r}: "
        f"{enabled.status_code} {enabled.text[:500]}"
    )


def ensure_neutral_registry(request: RequestFn, base_url: str) -> None:
    """Create the neutral skill, agent and workflows when they are missing.

    ``request`` is called as ``request(method, url, json=...)`` and must already
    carry authentication and CSRF headers. Order matters: the agent links the
    skill, and the workflows reference both.
    """
    base = str(base_url or "").rstrip("/")
    _ensure_entity(
        request,
        base,
        collection="skills",
        entity_id=NEUTRAL_SKILL_ID,
        payload=neutral_skill_payload(),
    )
    _ensure_entity(
        request,
        base,
        collection="agents",
        entity_id=NEUTRAL_AGENT_ID,
        payload=neutral_agent_payload(),
    )
    for workflow in neutral_workflow_payloads():
        _ensure_workflow(request, base, workflow)


def ensure_project_links(request: RequestFn, base_url: str, project_id: str) -> None:
    """Link the neutral workflows to ``project_id`` when they are not linked yet.

    Existing links and the operator default are kept. A lane without the right
    to change project workflows leaves the project as it is.
    """
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return
    base = str(base_url or "").rstrip("/")
    path = f"{base}/api/projects/{normalized_project_id}/workflows"
    current = request("GET", path)
    if current.status_code != 200:
        return
    body = current.json() or {}
    rows = body.get("project_workflows") if isinstance(body, dict) else []
    linked_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in rows or []
        if isinstance(row, dict)
        and bool(row.get("enabled", True))
        and not row.get("deleted_at")
        and str(row.get("workflow_id") or "").strip()
    ]
    missing = [workflow_id for workflow_id in NEUTRAL_WORKFLOW_IDS if workflow_id not in linked_ids]
    if not missing:
        return
    current_default = str(body.get("operator_default_workflow_id") or "").strip()
    default_id = current_default if current_default in linked_ids else NEUTRAL_WORKFLOW_ID
    updated = request(
        "PUT",
        path,
        json={
            "workflow_ids": [*linked_ids, *missing],
            "operator_default_workflow_id": default_id,
        },
    )
    if updated.status_code == 200 or updated.status_code in (401, 403):
        return
    # A stale link to a workflow that no longer exists rejects the union; fall
    # back to the neutral set alone.
    request(
        "PUT",
        path,
        json={
            "workflow_ids": list(NEUTRAL_WORKFLOW_IDS),
            "operator_default_workflow_id": NEUTRAL_WORKFLOW_ID,
        },
    )
