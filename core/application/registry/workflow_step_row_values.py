"""Column values written for one workflow step row.

The workflow store upserts steps, Groups and For each containers in
``db_store_workflows_mixin``; this module turns one step payload into the
ordered column tuple of the step upsert.
"""

from __future__ import annotations

import json
from typing import Any

from core.application.registry.step_skill_resolution import skill_is_host_owned


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _step_row_values(store, step: dict, *, step_id: str, index: int) -> tuple:
    """Column values of one step, in the order of the step upsert."""
    step_kind = str(step.get("step_kind") or "agent").strip() or "agent"
    raw_agent_id = step.get("agent_id")
    if raw_agent_id is None:
        raw_agent_id = step.get("agent", "")
    raw_skill_id = step.get("skill_id")
    if raw_skill_id is None:
        raw_skill_id = step.get("skill", "")
    step_skill_id = str(raw_skill_id or "").strip() or None
    # An extension's skill is advertised through the same catalog as the
    # host's own, but it lives in extension_registry_projections and this
    # column's foreign key points at registry_skills. Writing it there turns
    # an ordinary authoring request into an empty 500. Nothing is lost by
    # leaving the column empty because the effective skill is derived from
    # the selected agent.
    extension_skill_selected = bool(step_skill_id and not skill_is_host_owned(store, step_skill_id))
    if step_kind != "agent" or extension_skill_selected:
        step_skill_id = None
    return (
        step_kind,
        str(raw_agent_id or "").strip(),
        step_skill_id,
        index,
        bool(step.get("optional", False)),
        step.get("disabled") is True,
        json.dumps(step.get("context_inject")) if step.get("context_inject") else None,
        json.dumps(step.get("override")) if step.get("override") else None,
        str(step.get("participant_key") or "").strip(),
        str(step.get("connector_id") or "").strip() or None,
        str(step.get("connector_install_id") or "").strip(),
        str(step.get("workflow_id") or step.get("child_workflow_id") or "").strip() or None,
        str(step.get("block_key") or "").strip(),
        str(step.get("display_name") or step.get("name") or step_id).strip(),
        json.dumps(step.get("input_mapping_jsonb") or step.get("input_mapping") or {}),
        json.dumps(step.get("output_mapping_jsonb") or step.get("output_mapping") or {}),
        json.dumps(step.get("connection_binding_jsonb") or step.get("connection_binding") or {}),
        json.dumps(step.get("error_policy_jsonb") or step.get("error_policy") or {}),
        _json_or_none(step.get("boundary_contract", step.get("boundary_contract_jsonb"))),
    )
