"""Public API-specific OpenAPI contract adjustments."""

from __future__ import annotations

from typing import Any


def apply_public_token_approval_contract(schema: dict[str, Any]) -> None:
    """Expose the strict token approval contract instead of browser compatibility fields."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components["TokenApprovalContentV1"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "title": {"type": "string"},
            "reason": {"type": "string"},
            "description": {"type": "string"},
        },
    }
    components["TokenApprovalGateV1"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["job_id", "thread_id", "task_summary", "approval_id", "allowed_actions"],
        "properties": {
            "job_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "task_summary": {"type": "string"},
            "project_id": {"type": ["string", "null"]},
            "approval_id": {"type": "string"},
            "interrupt_id": {"type": "string"},
            "message": {"type": "string"},
            "stage": {"type": "string"},
            "gate_kind": {"type": ["string", "null"], "maxLength": 128},
            "recovery_kind": {"type": ["string", "null"], "maxLength": 128},
            "approve_label": {"type": ["string", "null"], "maxLength": 255},
            "reject_label": {"type": ["string", "null"], "maxLength": 255},
            "comment_required_on_approve": {"type": "boolean"},
            "comment_required_on_reject": {"type": "boolean"},
            "override_content_supported": {"type": "boolean"},
            "step_id": {"type": "string"},
            "step_execution_id": {"type": "string"},
            "group_id": {"type": ["string", "null"]},
            "for_each_id": {"type": ["string", "null"]},
            "expires_at": {"type": ["string", "null"], "format": "date-time"},
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
            "content": {"$ref": "#/components/schemas/TokenApprovalContentV1"},
        },
    }

    paths = schema.get("paths") or {}
    decision = (paths.get("/api/approvals/{thread_id}") or {}).get("post")
    if isinstance(decision, dict):
        decision["parameters"] = [
            {
                "name": "thread_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Workflow thread waiting on an approval decision.",
            },
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "maxLength": 255},
                "description": "Required opaque retry key scoped to the current approval gate.",
            },
        ]
        body = (decision.get("requestBody") or {}).get("content") or {}
        json_body = body.get("application/json") if isinstance(body, dict) else None
        if isinstance(json_body, dict):
            decision_schema = dict(json_body.get("schema") or {})
            decision_schema["required"] = ["approval_id"]
            decision_properties = dict(decision_schema.get("properties") or {})
            decision_properties.pop("override_content", None)
            decision_schema["properties"] = decision_properties
            decision_schema["anyOf"] = [
                {"required": ["approved"]},
                {"required": ["action"]},
            ]
            decision_schema.pop("oneOf", None)
            json_body["schema"] = decision_schema
        decision["requestBody"] = {"required": True, "content": body}

    gate = (paths.get("/api/jobs/{job_id}/approval") or {}).get("get")
    if isinstance(gate, dict):
        response = (gate.get("responses") or {}).get("200")
        if isinstance(response, dict):
            response.setdefault("content", {}).setdefault("application/json", {})["schema"] = {
                "$ref": "#/components/schemas/TokenApprovalGateV1"
            }


__all__ = ["apply_public_token_approval_contract"]
