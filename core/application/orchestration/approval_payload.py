"""Shared approval identity and payload resolution, independent of graph execution."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.active_step import ActiveStep, active_step
from core.application.orchestration.task_envelope import TEST_RUN_SOURCES
from core.application.workflows.workflow_step_kinds import STEP_KIND_APPROVAL

APPROVAL_NODE = "human_approval_gate"
RECOVERY_AUTONOMY = "autonomy"
RECOVERY_REWORK_BUDGET = "rework_budget"
GATE_KIND_APPROVAL = "approval"
GATE_KIND_AUTONOMY = "autonomy_gate"
GATE_KIND_RECOVERY = "recovery"
DEFAULT_APPROVAL_MESSAGE = "Please review and approve this step before proceeding."


def _is_meaningful_content(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return False
        return cleaned.lower() not in {"{}", "[]", "null", "(no content)"}
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _content_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        if not value:
            return ""
        try:
            return json.dumps(value, indent=2)
        except Exception:
            return str(value).strip()
    return str(value).strip()


def _extract_artifact_text(artifacts: dict, keys: tuple[str, ...]) -> str:
    if not isinstance(artifacts, dict) or not keys:
        return ""

    containers: list[dict] = []

    def _push(container: object) -> None:
        if not isinstance(container, dict):
            return
        containers.append(container)
        nested = container.get("artifacts")
        if isinstance(nested, dict):
            containers.append(nested)

    _push(artifacts)
    _push(artifacts.get("phase_result_v2"))

    raw_output = artifacts.get("raw_output")
    if isinstance(raw_output, str) and raw_output.strip().startswith("{"):
        try:
            parsed = json.loads(raw_output)
            _push(parsed)
        except Exception:
            pass

    for container in containers:
        for key in keys:
            for candidate in (key, key.lower(), key.upper()):
                if candidate not in container:
                    continue
                value = container.get(candidate)
                if not _is_meaningful_content(value):
                    continue
                text = _content_to_text(value)
                if _is_meaningful_content(text):
                    return text
    return ""


def _latest_message_artifact_text(
    state: Mapping[str, Any],
    *,
    step_execution_id: str = "",
    keys: tuple[str, ...],
) -> str:
    """The first meaningful artifact text of the latest messages, newest first."""
    from core.application.orchestration.agent_messages import get_state_messages

    for msg in reversed(get_state_messages(state)):
        if step_execution_id and str(msg.get("step_execution_id") or "") != step_execution_id:
            continue
        artifacts = msg.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        text = _extract_artifact_text(artifacts, keys)
        if _is_meaningful_content(text):
            return text
    return ""


def _latest_message_artifacts(
    state: Mapping[str, Any],
    *,
    exclude_agent: str = APPROVAL_NODE,
) -> dict:
    """Artifacts of the latest result before this gate: what the reviewer judges."""
    from core.application.orchestration.agent_messages import get_state_messages

    for msg in reversed(get_state_messages(state)):
        if exclude_agent and str(msg.get("from_agent") or "") == exclude_agent:
            continue
        artifacts = msg.get("artifacts")
        if isinstance(artifacts, dict):
            return artifacts
    return {}


def _pick_gate_content(*candidates: object) -> str:
    for candidate in candidates:
        if not _is_meaningful_content(candidate):
            continue
        text = _content_to_text(candidate)
        if _is_meaningful_content(text):
            return text
    return ""


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _stage_label(current: ActiveStep | None, fallback: str) -> str:
    if current is None:
        return fallback
    label = str(current.step.get("display_name") or "").strip()
    return label or current.step_id.replace("_", " ").title() or fallback


def _container_label(state: Mapping[str, Any], kind: str, container_id: object) -> str | None:
    """The immediate container's display name for notifications; none at the root.

    A For each step has its own For each label; ``group_label`` is never filled
    with a Group it does not belong to.
    """
    identifier = str(container_id or "").strip()
    if not identifier:
        return None
    try:
        from core.application.orchestration.run_definition import load_run_definition

        definition = load_run_definition(state)
        item = (
            definition.group(identifier)
            if kind == "group"
            else definition.for_each_item(identifier)
        )
        label = str(item.get("display_name") or "").strip()
    except Exception:
        label = ""
    return label or identifier.replace("_", " ").replace("-", " ").title()


def _group_label(state: Mapping[str, Any], group_id: object) -> str | None:
    return _container_label(state, "group", group_id)


def _for_each_label(state: Mapping[str, Any], for_each_id: object) -> str | None:
    return _container_label(state, "for_each", for_each_id)


def _approval_config(current: ActiveStep) -> dict[str, Any]:
    override = current.step.get("override")
    config = override.get(STEP_KIND_APPROVAL) if isinstance(override, Mapping) else None
    return dict(config) if isinstance(config, Mapping) else {}


def _string_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        if key and key not in values:
            values.append(key)
    return values


def _override_targets(config: Mapping[str, Any]) -> list[str]:
    mapping = config.get("response_mapping")
    if not isinstance(mapping, Mapping):
        return []
    return _string_list(
        mapping.get("override_content_targets")
        or mapping.get("override_content")
        or mapping.get("writeback")
    )


@dataclass
class _Gate:
    kind: str
    current: ActiveStep | None
    payload: dict[str, Any]
    allowed_actions: list[str]
    pending: dict[str, Any] = field(default_factory=dict)
    override_targets: list[str] = field(default_factory=list)
    approval_id: str = ""

    @property
    def authored(self) -> bool:
        return self.kind == GATE_KIND_APPROVAL

    @property
    def step_id(self) -> str:
        if self.current is not None:
            return self.current.step_id
        return str(self.pending.get("step_id") or "")

    def allows(self, action: str) -> bool:
        return action in self.allowed_actions


def _autonomy_copy(stage: str, detail: Mapping[str, Any]) -> tuple[str, str]:
    stage_text = str(stage or "").strip().lower() or "next"
    target = str(detail.get("target_node") or "").strip().replace("_", " ")
    mode = str(detail.get("mode") or "").strip().lower()
    confidence = _float_or_none(detail.get("confidence"))
    threshold = _float_or_none(detail.get("min_confidence"))

    intro = f"Flow Steward wants to start the {stage_text} step."
    if target:
        intro = f"Flow Steward wants to start the {stage_text} step with the {target} agent."
    lines = [intro]
    if mode == "manual":
        lines.append(
            "This project is configured to pause for human approval before this step runs."
        )
    else:
        lines.append(
            "This run is configured to pause when Flow Steward is not confident enough to continue automatically."
        )
    if confidence is not None and threshold is not None:
        lines.append(
            f"Flow Steward estimated confidence at {confidence:.2f}, below the {threshold:.2f} threshold."
        )
    lines.append(
        "Approve to continue with the step, or reject to stop and send feedback back to Flow Steward."
    )
    return (
        "Flow Steward paused before starting the next step and needs your decision.",
        "\n\n".join(lines),
    )


def _recovery_gate(
    state: Mapping[str, Any], pending: dict[str, Any], current: ActiveStep | None
) -> _Gate:
    kind = str(pending.get("kind") or "")
    reason = str(pending.get("reason") or "").strip()
    detail = pending.get("detail")
    detail = dict(detail) if isinstance(detail, Mapping) else {}
    allowed_actions = _string_list(pending.get("allowed_actions")) or ["approve", "reject"]
    step_execution_id = str(pending.get("step_execution_id") or "")
    stage = _stage_label(current, "Escalation Review")
    technical_details = _pick_gate_content(
        reason,
        _latest_message_artifact_text(
            state,
            step_execution_id=step_execution_id,
            keys=("_contract_errors", "_schema_errors", "blocked_reason", "raw_output", "summary"),
        ),
        state.get("task_summary", ""),
    )
    payload: dict[str, Any] = {
        "stage": stage,
        "task_summary": state.get("task_summary", ""),
        "allowed_actions": list(allowed_actions),
        "request_id": str(pending.get("request_id") or ""),
        "recovery_kind": kind,
        "step_id": str(pending.get("step_id") or ""),
        "step_execution_id": step_execution_id,
        "group_id": pending.get("group_id") or (current.group_id if current is not None else None),
        "for_each_id": pending.get("for_each_id")
        or (current.for_each_id if current is not None else None),
        "lane_id": pending.get("lane_id"),
        "technical_details": technical_details,
    }
    payload["group_label"] = _group_label(state, payload["group_id"])
    payload["for_each_label"] = _for_each_label(state, payload["for_each_id"])
    if kind == RECOVERY_AUTONOMY:
        message, content = _autonomy_copy(stage, detail)
        payload["gate_kind"] = GATE_KIND_AUTONOMY
        payload["gate_meta"] = {
            "step_id": payload["step_id"],
            "step_execution_id": step_execution_id,
            "target_agent": detail.get("target_node"),
            "mode": detail.get("mode"),
            "confidence": detail.get("confidence"),
            "min_confidence": detail.get("min_confidence"),
            "reason": reason,
        }
    elif kind == RECOVERY_REWORK_BUDGET:
        rework = detail.get("rework")
        rework = rework if isinstance(rework, Mapping) else {}
        message = (
            "The rework budget of this run is exhausted. Approve to continue with "
            "additional budget, or abort the run."
        )
        content = technical_details
        payload["gate_kind"] = GATE_KIND_RECOVERY
        payload.update(
            {
                "rework_budget_exhausted": True,
                "rework_exhausted": list(detail.get("exhausted") or []),
                "rework_blocked_edge": list(detail.get("blocked_edge") or []),
                "rework_retry_attempt": int(rework.get("used") or 0),
            }
        )
    else:
        message = (
            "This step failed. Approve to continue past the error on the step's "
            "success path, or reject to send it back for rework with your comment."
        )
        content = technical_details
        payload["gate_kind"] = GATE_KIND_RECOVERY
    payload["message"] = message
    payload["content"] = content
    return _Gate(
        kind=payload["gate_kind"],
        current=current,
        payload=payload,
        allowed_actions=allowed_actions,
        pending=pending,
    )


def _show_output_value(
    state: Mapping[str, Any], artifacts: Mapping[str, Any], selector: str
) -> object:
    """One show_outputs selector: a message selector, a state path or an output key."""
    root = selector.removeprefix("state.").split(".", 1)[0].split("[", 1)[0]
    if root == "external_effect_resolution_ref":
        return None
    if selector.startswith("message."):
        from core.application.orchestration.agent_messages import (
            MessageSelectorError,
            resolve_message_selector,
        )

        try:
            return resolve_message_selector(selector, state)
        except MessageSelectorError:
            return None
    if selector.startswith("state."):
        from core.application.orchestration.state_paths import (
            resolve_state_path_with_workflow_data,
        )

        value, found = resolve_state_path_with_workflow_data(dict(state), selector)
        return value if found else None
    if selector in artifacts:
        return artifacts.get(selector)
    if selector in state:
        return state.get(selector)
    text = _extract_artifact_text(dict(artifacts), (selector, selector.upper()))
    return text if _is_meaningful_content(text) else None


def _authored_gate(state: Mapping[str, Any], current: ActiveStep) -> _Gate:
    config = _approval_config(current)
    artifacts = _latest_message_artifacts(state)
    shown: dict[str, object] = {}
    for selector in _string_list(config.get("show_outputs")):
        value = _show_output_value(state, artifacts, selector)
        if _is_meaningful_content(value):
            shown[selector] = value
    serialized = ""
    if len(shown) == 1:
        serialized = _content_to_text(next(iter(shown.values())))
    elif shown:
        serialized = json.dumps(shown, indent=2, ensure_ascii=False, default=str)
    content = _pick_gate_content(
        serialized,
        _extract_artifact_text(artifacts, ("raw_output", "summary", "output", "OUTPUT")),
        state.get("task_summary", ""),
    )
    approval_id = approval_id_for_execution(current.step_execution_id)
    allowed_actions = ["approve", "reject"]
    payload: dict[str, Any] = {
        "stage": _stage_label(current, "Approval"),
        "message": str(config.get("message") or "").strip() or DEFAULT_APPROVAL_MESSAGE,
        "content": content,
        "task_summary": state.get("task_summary", ""),
        "allowed_actions": list(allowed_actions),
        "approval_id": approval_id,
        "gate_kind": GATE_KIND_APPROVAL,
        **current.message_identity(),
        "group_label": _group_label(state, current.group_id),
        "for_each_label": _for_each_label(state, current.for_each_id),
    }
    if shown:
        payload["gate_meta"] = {"show_outputs": list(shown)}
    return _Gate(
        kind=GATE_KIND_APPROVAL,
        current=current,
        payload=payload,
        allowed_actions=allowed_actions,
        override_targets=_override_targets(config),
        approval_id=approval_id,
    )


class NoPendingApprovalGate(ValueError):
    """The pinned current step is not an Approval and no recovery is pending."""


def _resolve_gate(state: Mapping[str, Any]) -> _Gate:
    pending = state.get("recovery_pending")
    current = active_step(state)
    if isinstance(pending, Mapping) and pending:
        return _recovery_gate(state, dict(pending), current)
    if current is None:
        raise ValueError("The current approval step authority is unavailable")
    if current.step_kind == STEP_KIND_APPROVAL:
        return _authored_gate(state, current)
    raise NoPendingApprovalGate(
        "human_approval_gate requires a pending recovery request or an active Approval step"
    )


def state_based_approval_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """Project a real test-run pause from its pinned gate, without an interrupt."""
    if (
        str(state.get("task_source") or "").strip().lower() not in TEST_RUN_SOURCES
        or state.get("status") != "awaiting_approval"
        or not state.get("needs_user_approval")
    ):
        return {}
    gate = _resolve_gate(state)
    payload = dict(gate.payload)
    if not payload.get("approval_id"):
        request_id = str(gate.pending.get("request_id") or "")
        if not request_id:
            return {}
        payload["approval_id"] = f"recovery:{request_id}"
    return payload


def approval_id_for_execution(step_execution_id: str) -> str:
    """The approval id of one logical entry into an Approval step.

    Derived from the execution id, so a re-executed node (resume, restart)
    reports the same approval and a new entry reports a new one.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(step_execution_id)))
