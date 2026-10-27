"""Producer evidence omitted by the editor's flattened variable type map."""

from collections import defaultdict

from core.application.orchestration.state import OrchestratorState

_STATE_PREFIX = "state."

_TRANSFERABLE_RUN_FIELDS = frozenset(
    {
        "inputs",
        "task_id",
        "task_summary",
        "task_source",
        "project_name",
        "effort_hint",
        "task_type",
        "risk_level",
        "kb_context",
        "initial_requirements",
    }
)


def is_runtime_context_root(root: str) -> bool:
    """Runtime-owned evolving state is not an ordinary frozen data input."""
    return root in OrchestratorState.__annotations__ and root not in _TRANSFERABLE_RUN_FIELDS


def validate_source_producers(workflow, group_id, resolver, plan):
    """Refuse message fallbacks and conflicting types before freezing inputs.

    The editor combines state and message outputs for suggestions. Extraction
    must retain the provenance instead: a message artifact is not a state value.
    All outside producers are considered conservatively, including rework paths.
    """
    types = defaultdict(set)
    message_only = set()
    workflow_id = workflow["workflow_id"]
    nested = {
        item.get("for_each_id")
        for item in workflow.get("for_each") or []
        if item.get("group_id") == group_id
    }
    for step in workflow.get("steps") or []:
        if step.get("group_id") == group_id or step.get("for_each_id") in nested:
            continue
        step_id = step["step_id"]
        state_rows = resolver.get_step_state_outputs(workflow_id, step_id)
        state_names = {str(row.get("name") or "").removeprefix(_STATE_PREFIX) for row in state_rows}
        for row in state_rows:
            name = str(row.get("name") or "").removeprefix(_STATE_PREFIX)
            kind = str(row.get("type") or "")
            types[name].add(
                {"text": "string", "paragraph": "string", "json": "object"}.get(kind, kind)
            )
        for row in resolver.get_step_outputs(workflow_id, step_id):
            name = str(row.get("name") or "").removeprefix(_STATE_PREFIX)
            if name not in state_names:
                message_only.add(name.split(".", 1)[0])
    for alias, selector in plan.input_mapping.items():
        name = selector.removeprefix(_STATE_PREFIX)
        path = f"input_mapping.{alias}"
        if name.split(".", 1)[0] in message_only:
            plan.refuse(
                "message_state_fallback",
                path,
                f"{selector} also names a message-only output; publish an explicit state value before conversion.",
            )
        if len(types[name]) > 1:
            plan.refuse(
                "ambiguous_input_type",
                path,
                f"{selector} has conflicting producer types; normalize them before conversion.",
            )
