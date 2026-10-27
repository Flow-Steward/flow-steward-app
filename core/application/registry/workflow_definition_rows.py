"""A workflow definition as stored rows: steps, Groups and For each containers.

The registry keeps steps, run-once Groups and For each containers in their own
tables, linked by row UUIDs; readers and writers speak business ids. A save
carries all three collections or none (ADR 0038).
"""

from __future__ import annotations

from typing import NamedTuple

from core.application.workflows.workflow_hook_placement import hook_placement_issues


class WorkflowDefinitionRows(NamedTuple):
    """The stored definition of one workflow, in business ids."""

    steps: list[dict]
    groups: list[dict]
    for_each: list[dict]

    @classmethod
    def empty(cls) -> WorkflowDefinitionRows:
        return cls([], [], [])


def load_workflow_definition_rows(
    cur, workflow_ids: list[str], *, derive_skill=None
) -> dict[str, WorkflowDefinitionRows]:
    """Steps, Groups and For each containers per workflow, read through *cur*.

    A step's ``group_id``/``for_each_id`` and a For each's parent ``group_id``
    are business ids, never the row UUIDs the foreign keys use, so every
    reader speaks the definition's ids. *derive_skill* fills an empty step
    skill from its agent for display; a run pins the definition without it,
    because the skill is derived at dispatch.
    """
    normalized_ids = [
        str(workflow_id or "").strip()
        for workflow_id in (workflow_ids or [])
        if str(workflow_id or "").strip()
    ]
    if not normalized_ids:
        return {}
    definitions: dict[str, WorkflowDefinitionRows] = {
        workflow_id: WorkflowDefinitionRows.empty() for workflow_id in normalized_ids
    }
    cur.execute(
        """SELECT * FROM registry_workflow_groups
           WHERE workflow_id = ANY(%s)
           ORDER BY workflow_id, sort_order, group_id""",
        (normalized_ids,),
    )
    for row in cur.fetchall():
        group = dict(row)
        group["body"] = group.get("body_jsonb") or {}
        group["error_policy"] = group.get("error_policy_jsonb") or {}
        group["boundary_contract"] = group.get("boundary_contract_jsonb")
        definitions[str(group["workflow_id"]).strip()].groups.append(group)
    cur.execute(
        """SELECT f.*, g.group_id AS group_key
           FROM registry_workflow_for_each f
           LEFT JOIN registry_workflow_groups g ON g.id = f.group_id
           WHERE f.workflow_id = ANY(%s)
           ORDER BY f.workflow_id, f.sort_order, f.for_each_id""",
        (normalized_ids,),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["group_id"] = item.pop("group_key", None)
        item["over"] = item.get("over_selector") or ""
        item["body"] = item.get("body_jsonb") or {}
        item["error_policy"] = item.get("error_policy_jsonb") or {}
        item["boundary_contract"] = item.get("boundary_contract_jsonb")
        definitions[str(item["workflow_id"]).strip()].for_each.append(item)
    cur.execute(
        """SELECT s.*, g.group_id AS group_key, f.for_each_id AS for_each_key,
                  a.display_name AS agent_display_name, c.display_name AS skill_display_name
           FROM registry_workflow_steps s
           LEFT JOIN registry_workflow_groups g ON g.id = s.group_id
           LEFT JOIN registry_workflow_for_each f ON f.id = s.for_each_id
           LEFT JOIN registry_agents a ON a.agent_id = s.agent_id
           LEFT JOIN registry_skills c ON c.skill_id = s.skill_id
           WHERE s.owner_workflow_id = ANY(%s)
           ORDER BY s.owner_workflow_id, s.sort_order, s.step_id""",
        (normalized_ids,),
    )
    for row in cur.fetchall():
        step = dict(row)
        step["group_id"] = step.pop("group_key", None)
        step["for_each_id"] = step.pop("for_each_key", None)
        step["error_policy"] = step.get("error_policy_jsonb") or {}
        step["boundary_contract"] = step.get("boundary_contract_jsonb")
        # The column holds only host-owned skills, because its foreign key
        # points at registry_skills. A step whose agent's skill is
        # extension-projected therefore reads back empty, which looks to an
        # author like the skill was lost. It was not: the runtime runs the
        # agent's own skill, so report that one here.
        if derive_skill is not None and not str(step.get("skill_id") or "").strip():
            derived = derive_skill(step.get("agent_id") or "")
            if derived:
                step["skill_id"] = derived
        definitions[str(step["owner_workflow_id"]).strip()].steps.append(step)
    return definitions


_LEGACY_DEFINITION_KEYS = ("phases", "_phases")
_DEFINITION_COLLECTIONS = (
    ("steps", "step_id"),
    ("groups", "group_id"),
    ("for_each", "for_each_id"),
)


def _definition_payload(data: dict) -> WorkflowDefinitionRows | None:
    """The definition a save carries, or ``None`` for a metadata-only save.

    Steps, Groups and For each containers are one definition, so a save carries
    all three or none. A phase-based payload is refused rather than read:
    silently dropping it would erase the workflow's definition.
    """
    for key in _LEGACY_DEFINITION_KEYS:
        if key in data:
            raise ValueError(
                f"Workflow payload key '{key}' belongs to the phase-based format; "
                "send 'steps', 'groups' and 'for_each'"
            )
    present = [key for key, _ in _DEFINITION_COLLECTIONS if key in data]
    if not present:
        return None
    if len(present) != len(_DEFINITION_COLLECTIONS):
        raise ValueError(
            "A workflow definition save carries 'steps', 'groups' and 'for_each' together"
        )
    collections: list[list[dict]] = []
    for key, id_key in _DEFINITION_COLLECTIONS:
        raw = data.get(key)
        if not isinstance(raw, list):
            raise ValueError(f"Workflow '{key}' must be a list")
        rows = [dict(row) for row in raw if isinstance(row, dict)]
        # Rows are upserted on their ids, so a repeated id would silently merge
        # two authored nodes into one row instead of failing.
        ids = [str(row.get(id_key) or "").strip() for row in rows]
        repeated = sorted({item for item in ids if item and ids.count(item) > 1})
        if repeated:
            raise ValueError(f"Workflow {key} id(s) used twice: {', '.join(repeated)}")
        collections.append(rows)
    if issues := hook_placement_issues(data):
        raise ValueError(
            "; ".join(f"{issue['code']} at {issue['path']}: {issue['message']}" for issue in issues)
        )
    return WorkflowDefinitionRows(*collections)


_DEFINITION_STEP_FIELDS = (
    "step_id",
    "step_kind",
    "group_id",
    "for_each_id",
    "lane_id",
    "block_key",
    "display_name",
    "participant_key",
    "agent_id",
    "skill_id",
    "connector_id",
    "connector_install_id",
    "workflow_id",
    "input_mapping_jsonb",
    "output_mapping_jsonb",
    "connection_binding_jsonb",
    "error_policy",
    "boundary_contract",
    "optional",
    "disabled",
    "context_inject",
    "override",
)
_DEFINITION_GROUP_FIELDS = (
    "group_id",
    "display_name",
    "description",
    "body",
    "error_policy",
    "boundary_contract",
)
_DEFINITION_FOR_EACH_FIELDS = (
    "for_each_id",
    "group_id",
    "display_name",
    "description",
    "over",
    "item_name",
    "index_name",
    "body",
    "error_policy",
    "boundary_contract",
)


def _definition_step_payload(step: dict) -> dict:
    """A loaded step reduced to its definition fields, ready to save elsewhere."""
    return {key: step.get(key) for key in _DEFINITION_STEP_FIELDS}


def _definition_group_payload(group: dict) -> dict:
    return {key: group.get(key) for key in _DEFINITION_GROUP_FIELDS}


def _definition_for_each_payload(item: dict) -> dict:
    return {key: item.get(key) for key in _DEFINITION_FOR_EACH_FIELDS}


__all__ = [
    "WorkflowDefinitionRows",
    "load_workflow_definition_rows",
]
