"""Shared workflow step-kind constants and helpers."""

from __future__ import annotations

from collections.abc import Mapping

STEP_KIND_AGENT = "agent"
STEP_KIND_CONNECTOR = "connector"
STEP_KIND_INVOKE_WORKFLOW = "invoke_workflow"
STEP_KIND_IF = "if"
STEP_KIND_SWITCH = "switch"
STEP_KIND_STOP_SUCCESS = "stop_success"
STEP_KIND_STOP_FAIL = "stop_fail"
STEP_KIND_SET_VARIABLE = "set_variable"
#: The removed inline iteration step (ADR 0038). Kept only so the runtime
#: pieces still being moved to the For each container compile; no definition
#: can contain it.
STEP_KIND_FOR_EACH = "for_each"
STEP_KIND_BATCH = "batch"
STEP_KIND_HTTP_REQUEST = "http_request"
STEP_KIND_DATASET_QUERY = "dataset_query"
STEP_KIND_TRANSFORM = "transform"
STEP_KIND_TABULAR_FEED = "tabular_feed"
STEP_KIND_FILE_FROM_BASE64 = "file_from_base64"
STEP_KIND_TEMPLATE = "template"
#: Graph-owned kinds of the execution_graph contract. They join
#: SUPPORTED_STEP_KINDS together with their runtime dispatch.
STEP_KIND_APPROVAL = "approval"
STEP_KIND_WAIT = "wait"
#: Loop controls of a sequence For each body (ADR 0038). Break and Continue end
#: the current item or the whole For each; Collect adds to an existing variable.
STEP_KIND_BREAK = "break"
STEP_KIND_CONTINUE = "continue"
STEP_KIND_COLLECT = "collect"
FOR_EACH_TERMINAL_STEP_KINDS = frozenset({STEP_KIND_BREAK, STEP_KIND_CONTINUE})
FOR_EACH_ONLY_STEP_KINDS = frozenset({STEP_KIND_BREAK, STEP_KIND_CONTINUE, STEP_KIND_COLLECT})

CONTROL_FLOW_STEP_KINDS: set[str] = {
    STEP_KIND_IF,
    STEP_KIND_SWITCH,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_BATCH,
    STEP_KIND_BREAK,
    STEP_KIND_CONTINUE,
    STEP_KIND_COLLECT,
}

SUPPORTED_STEP_KINDS: set[str] = {
    STEP_KIND_AGENT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_TRANSFORM,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_TEMPLATE,
    STEP_KIND_APPROVAL,
    STEP_KIND_WAIT,
    *CONTROL_FLOW_STEP_KINDS,
}


def normalize_step_kind(
    value: object,
    *,
    default: str = STEP_KIND_AGENT,
    preserve_unknown: bool = False,
) -> str:
    normalized = str(value or "").strip().lower()
    # Canonical alias for dataset-query labels used in authored workflow payloads.
    if normalized == "dataset":
        normalized = STEP_KIND_DATASET_QUERY
    if normalized in SUPPORTED_STEP_KINDS:
        return normalized
    if preserve_unknown and normalized:
        return normalized
    return default


def is_control_flow_step_kind(value: object) -> bool:
    return normalize_step_kind(value, default="") in CONTROL_FLOW_STEP_KINDS


def step_requires_agent(value: object) -> bool:
    return normalize_step_kind(value) == STEP_KIND_AGENT


#: Runtime actors that run a step kind themselves. A step reported by one of
#: these involved no agent; the label names the kind of step for a reader.
STEP_KIND_RUNTIME_ACTOR_LABELS: dict[str, str] = {
    "connector_step": "Extension action",
    "http_request_step": "HTTP request",
    "dataset_query_step": "Dataset query",
    "transform_step": "Transform",
    "tabular_feed_step": "Tabular feed",
    "file_from_base64_step": "Save file from base64",
    "template_step": "Template",
    "invoke_workflow": "Run another workflow",
    "control_flow_step": "Control flow",
}


def runtime_actor_for_step(step: Mapping[str, object] | None, *, default: str = "") -> str:
    """Resolve runtime message actor for a normalized workflow step payload."""
    if not isinstance(step, Mapping):
        return str(default or "").strip()
    explicit = str(step.get("agent") or step.get("agent_id") or "").strip()
    if explicit:
        return explicit
    step_kind = normalize_step_kind(step.get("step_kind"), default="")
    if step_kind == STEP_KIND_CONNECTOR:
        return "connector_step"
    if step_kind == STEP_KIND_HTTP_REQUEST:
        return "http_request_step"
    if step_kind == STEP_KIND_DATASET_QUERY:
        return "dataset_query_step"
    if step_kind == STEP_KIND_TRANSFORM:
        return "transform_step"
    if step_kind == STEP_KIND_TABULAR_FEED:
        return "tabular_feed_step"
    if step_kind == STEP_KIND_FILE_FROM_BASE64:
        return "file_from_base64_step"
    if step_kind == STEP_KIND_TEMPLATE:
        return "template_step"
    if step_kind == STEP_KIND_INVOKE_WORKFLOW:
        return "invoke_workflow"
    if is_control_flow_step_kind(step_kind):
        return "control_flow_step"
    return str(default or "").strip()
