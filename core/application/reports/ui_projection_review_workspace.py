"""Review workspace projection helpers for job detail UI."""

from __future__ import annotations

from typing import Any

from .ui_projection_common import (
    _AUTONOMY_GATE_SENTINEL,
    _normalize_dict,
    _normalize_role,
    _visible_product_label,
)


def _approval_workspace_title(approval_gate: dict[str, Any], *, fallback: str) -> str:
    for source in (
        approval_gate.get("title"),
        _normalize_dict(approval_gate.get("gate_meta")).get("title"),
    ):
        title = str(source or "").strip()
        if title:
            return title
    return fallback


def _is_autonomy_gate_payload(
    approval_gate: dict[str, Any], *, content: str, technical: str
) -> bool:
    gate_kind = str(approval_gate.get("gate_kind") or "").strip().lower()
    if gate_kind == "autonomy_gate":
        return True
    haystacks = [content, technical, str(approval_gate.get("message") or "").strip()]
    return any(_AUTONOMY_GATE_SENTINEL in text for text in haystacks if text)


def _parse_autonomy_gate_meta(approval_gate: dict[str, Any], raw_text: str) -> dict[str, Any]:
    meta = _normalize_dict(approval_gate.get("gate_meta"))
    parsed: dict[str, Any] = dict(meta)

    for line in str(raw_text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or ":" not in stripped:
            continue
        key, value = stripped[2:].split(":", 1)
        normalized_key = key.strip().lower().replace(" ", "_")
        parsed.setdefault(normalized_key, value.strip())

    if "target_agent" not in parsed and parsed.get("agent"):
        parsed["target_agent"] = parsed.get("agent")
    if "min_confidence" not in parsed and parsed.get("threshold"):
        parsed["min_confidence"] = parsed.get("threshold")
    return parsed


def _autonomy_gate_title(stage: str) -> str:
    normalized = str(stage or "").strip().lower()
    if not normalized:
        return "Approve the next step"
    return f"Approve the {normalized} step"


def _override_copy(stage: str, *, autonomy_gate: bool = False) -> dict[str, str]:
    normalized = str(stage or "").strip() or "current step"
    normalized_lower = normalized.lower()
    if autonomy_gate:
        return {
            "label": "Provide content instead",
            "field_label": f"Content to use instead of running {normalized_lower}",
            "placeholder": "Paste the content Flow Steward should continue with.",
            "help": "Flow Steward will skip the current step and continue with this text.",
        }
    return {
        "label": "Override and continue",
        "field_label": "Override content",
        "placeholder": "Paste the replacement content Flow Steward should continue with.",
        "help": "Used only when you choose the override action.",
    }


def _override_supported(approval_gate: dict[str, Any], *, is_mutation_allowed: bool) -> bool:
    if not is_mutation_allowed:
        return False
    if approval_gate.get("override_content_supported") is True:
        return True
    gate_meta = _normalize_dict(approval_gate.get("gate_meta"))
    response_mapping = _normalize_dict(gate_meta.get("response_mapping"))
    if response_mapping:
        for key in ("override_content_targets", "override_content", "writeback"):
            value = response_mapping.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, list) and any(str(item or "").strip() for item in value):
                return True
    for key in ("override_content_targets", "show_outputs"):
        value = approval_gate.get(key) or gate_meta.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


def _autonomy_gate_intro(meta: dict[str, Any], *, override_supported: bool) -> str:
    mode = str(meta.get("mode") or "").strip().lower()
    if mode == "manual":
        intro = "Flow Steward paused because this project is configured to require a human decision before this step runs."
    else:
        intro = "Flow Steward paused because this run requires a human decision when confidence is too low to continue automatically."
    if override_supported:
        return (
            intro
            + " You can also provide the content Flow Steward should use instead of running the step."
        )
    return intro


def _autonomy_gate_content(stage: str, meta: dict[str, Any]) -> str:
    stage_text = str(stage or "").strip().lower() or "next"
    agent = str(meta.get("target_agent") or "").strip().replace("_", " ")
    confidence_raw = meta.get("confidence")
    try:
        confidence = float(str(confidence_raw)) if confidence_raw not in (None, "") else None
    except Exception:
        confidence = None
    threshold_raw = meta.get("min_confidence")
    try:
        threshold = float(str(threshold_raw)) if threshold_raw not in (None, "") else None
    except Exception:
        threshold = None
    lines = [
        (
            f"Flow Steward wants to start the {stage_text} step with the {agent} agent."
            if agent
            else f"Flow Steward wants to start the {stage_text} step."
        )
    ]
    if confidence is not None and threshold is not None:
        lines.append(
            f"It estimated confidence at {confidence:.2f}, below the required {threshold:.2f} threshold."
        )
    lines.append(
        "Approve to let Flow Steward continue, or reject to stop and send feedback back to the workflow."
    )
    return "\n\n".join(lines)


def _build_review_workspace(
    *,
    approval_gate: dict[str, Any],
    user_role: str,
) -> dict[str, Any] | None:
    if not approval_gate:
        return None
    stage = str(approval_gate.get("stage") or "").strip() or "Review"
    message = _visible_product_label(approval_gate.get("message")).strip()
    content = _visible_product_label(approval_gate.get("content")).strip()
    technical_details = _visible_product_label(approval_gate.get("technical_details")).strip()
    thread_id = str(approval_gate.get("thread_id") or "").strip()
    role = _normalize_role(user_role)
    is_mutation_allowed = role in {"operator", "admin"}
    override_supported = _override_supported(approval_gate, is_mutation_allowed=is_mutation_allowed)

    if stage == "Orchestrator":
        return {
            "kind": "clarifying_answer",
            "title": _approval_workspace_title(
                approval_gate,
                fallback="Clarification required",
            ),
            "stage_label": stage,
            "intro": message or "Flow Steward needs an answer before it can continue.",
            "thread_id": thread_id,
            "question": message or content,
            "content": content,
            "read_only": not is_mutation_allowed,
            "submit_label": "Send answer",
        }

    if _is_autonomy_gate_payload(approval_gate, content=content, technical=technical_details):
        meta = _parse_autonomy_gate_meta(approval_gate, technical_details or content)
        override_copy = _override_copy(stage, autonomy_gate=True)
        return {
            "kind": "autonomy_gate",
            "title": _approval_workspace_title(
                approval_gate,
                fallback=_autonomy_gate_title(stage),
            ),
            "stage_label": stage,
            "intro": _autonomy_gate_intro(meta, override_supported=override_supported),
            "thread_id": thread_id,
            "content": _autonomy_gate_content(stage, meta),
            "content_is_present": True,
            "details_title": "Decision details",
            "details": technical_details or content,
            "comment_required_on_reject": True,
            "override_content_supported": override_supported,
            "explicit_override_action": override_supported,
            "override_label": override_copy["label"],
            "override_textarea_label": override_copy["field_label"],
            "override_placeholder": override_copy["placeholder"],
            "override_help": override_copy["help"],
            "read_only": not is_mutation_allowed,
            "approve_label": "Approve and continue",
            "reject_label": "Reject",
        }

    override_copy = _override_copy(stage, autonomy_gate=False)
    return {
        "kind": "document_review",
        "title": _approval_workspace_title(
            approval_gate,
            fallback="Review workspace",
        ),
        "stage_label": stage,
        "intro": message or "Review the latest Flow Steward output before continuing.",
        "thread_id": thread_id,
        "content": content,
        "content_is_present": bool(content),
        "comment_required_on_reject": True,
        "override_content_supported": override_supported,
        "explicit_override_action": override_supported,
        "override_label": override_copy["label"],
        "override_textarea_label": override_copy["field_label"],
        "override_placeholder": override_copy["placeholder"],
        "override_help": override_copy["help"],
        "read_only": not is_mutation_allowed,
        "approve_label": "Approve",
        "reject_label": "Reject",
    }
