from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .workflow_entrypoints_common import (
    APPLICATION_JSON_VALUE,
    _isoformat,
    _normalize_response_mode,
)
from .workflow_entrypoints_registry import (
    _absolute_url_for_request,
    _absolute_url_source,
    _effective_entrypoint_auth_mode,
    _entrypoint_project_binding_id,
    _entrypoint_readiness,
    _normalize_allowed_content_types,
    _normalize_limit,
    _normalize_mapping,
    _normalize_source_restrictions,
    _public_auth_config,
    _public_workflow_webhook_path,
)


def _serialize_workflow_entrypoint(
    entrypoint: dict[str, Any], request: Request | None = None
) -> dict[str, Any]:
    entry_key = str(entrypoint.get("entry_key") or "").strip()
    public_path = _public_workflow_webhook_path(entry_key)
    auth_mode = _effective_entrypoint_auth_mode(entrypoint)
    auth_config = _public_auth_config(entrypoint)
    readiness = _entrypoint_readiness(entrypoint)
    return {
        "entrypoint_id": str(entrypoint.get("entrypoint_id") or "").strip() or None,
        "workflow_id": str(entrypoint.get("workflow_id") or "").strip(),
        "project_id": _entrypoint_project_binding_id(entrypoint) or None,
        "entry_kind": str(entrypoint.get("entry_kind") or "webhook").strip() or "webhook",
        "entry_key": entry_key,
        "enabled": bool(entrypoint.get("enabled", True)),
        "allowed_methods": ["POST"],
        "auth_mode": auth_mode,
        "auth_config": auth_config,
        "source_restrictions": _normalize_source_restrictions(
            entrypoint.get("source_restrictions_jsonb")
        ),
        "allowed_content_types": _normalize_allowed_content_types(
            entrypoint.get("allowed_content_types_jsonb")
        ),
        "max_body_bytes": _normalize_limit(
            entrypoint.get("max_body_bytes"), default=46137344, minimum=1, maximum=104857600
        ),
        "max_file_bytes": _normalize_limit(
            entrypoint.get("max_file_bytes"), default=10485760, minimum=1
        ),
        "max_files": _normalize_limit(entrypoint.get("max_files"), default=4, minimum=0),
        "idempotency_mode": str(entrypoint.get("idempotency_mode") or "none").strip() or "none",
        "response_mode": _normalize_response_mode(entrypoint.get("response_mode")),
        "input_mapping": _normalize_mapping(entrypoint.get("input_mapping_jsonb")),
        "description": str(entrypoint.get("description") or "").strip(),
        "requires_secret": bool(str(entrypoint.get("secret_ref") or "").strip()),
        "secret_ref": str(entrypoint.get("secret_ref") or "").strip() or None,
        "public_path": public_path,
        "public_url": _absolute_url_for_request(request, public_path),
        "public_url_source": _absolute_url_source(request),
        "readiness": readiness,
        "created_at": _isoformat(entrypoint.get("created_at")),
        "updated_at": _isoformat(entrypoint.get("updated_at")),
    }


def _serialize_delivery(row: dict[str, Any]) -> dict[str, Any]:
    payload_value = row.get("payload")
    payload = payload_value if isinstance(payload_value, dict) else {}
    result_value = row.get("result")
    result = result_value if isinstance(result_value, dict) else {}
    meta_value = row.get("meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    validation_errors = meta.get("validation_errors")
    if not isinstance(validation_errors, list):
        validation_errors = []
    payload_summary = meta.get("payload_summary")
    if not isinstance(payload_summary, dict):
        payload_summary = {}
    http_status_value = meta.get("http_status")
    try:
        http_status = int(http_status_value) if http_status_value is not None else None
    except Exception:
        http_status = None
    return {
        "id": row.get("id"),
        "task_id": str(row.get("task_id") or "").strip() or None,
        "task_summary": str(row.get("task_summary") or "").strip(),
        "status": str(row.get("status") or "").strip(),
        "timestamp": row.get("timestamp"),
        "request_id": str(payload.get("request_id") or "").strip() or None,
        "idempotency_key_digest": str(payload.get("idempotency_key_digest") or "").strip() or None,
        "reason": str(payload.get("reason") or "").strip() or None,
        "job_id": str(result.get("job_id") or "").strip() or None,
        "deduplicated": bool(result.get("deduplicated", False)),
        "source_ip": str(meta.get("source_ip") or "").strip() or None,
        "auth_mode": str(meta.get("auth_mode") or "").strip() or None,
        "auth_outcome": str(meta.get("auth_outcome") or "").strip() or None,
        "auth_category": str(meta.get("auth_category") or "").strip() or None,
        "http_status": http_status,
        "content_type": str(meta.get("content_type") or "").strip() or None,
        "response_mode": str(meta.get("response_mode") or "").strip() or None,
        "validation_errors": validation_errors,
        "payload_summary": payload_summary,
        "error": str(row.get("error") or "").strip() or None,
        "result": result,
    }


def _serialize_provider_trigger(
    row: dict[str, Any],
    *,
    definition: dict[str, Any] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    payload = dict(row or {})
    catalog_definition = dict(definition or {})
    provider_key = str(payload.get("provider_key") or "").strip().lower()
    return {
        "trigger_id": str(payload.get("trigger_id") or "").strip(),
        "workflow_id": str(payload.get("workflow_id") or "").strip(),
        "project_id": str(payload.get("project_id") or "").strip() or None,
        "provider_key": provider_key,
        "trigger_type_id": str(payload.get("trigger_type_id") or "").strip(),
        "display_name": str(
            payload.get("display_name") or catalog_definition.get("display_name") or ""
        ).strip(),
        "description": str(catalog_definition.get("description") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
        "event_name": str(payload.get("event_name") or "").strip(),
        "trigger_config": dict(payload.get("trigger_config_jsonb") or {}),
        "readiness": {
            "status": str(payload.get("readiness_status") or "draft").strip() or "draft",
            "errors": list(payload.get("validation_errors_jsonb") or []),
        },
        "config_schema": dict(catalog_definition.get("config_schema") or {}),
        "ui_schema": dict(catalog_definition.get("ui_schema") or {}),
        "provider_ingress_url": (
            _absolute_url_for_request(request, f"/webhook/providers/{provider_key}")
            if request is not None and bool(catalog_definition.get("provider_ingress_url_required"))
            else None
        ),
        "created_at": _isoformat(payload.get("created_at")),
        "updated_at": _isoformat(payload.get("updated_at")),
    }


class WorkflowEntrypointCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    description: str = ""
    enabled: bool = True
    auth_mode: str = "bearer_token"
    auth_config: dict[str, Any] = Field(default_factory=dict)
    # Write-only: handed straight to project secret storage. Never serialized
    # back out, never stored in auth_config, never returned in credential_reveal.
    signing_secret: str | None = None
    source_restrictions: dict[str, Any] = Field(default_factory=dict)
    allowed_content_types: list[str] = Field(default_factory=lambda: [APPLICATION_JSON_VALUE])
    max_body_bytes: int = Field(default=46137344, ge=1, le=104857600)
    max_file_bytes: int = Field(default=10485760, ge=1, le=104857600)
    max_files: int = Field(default=4, ge=0, le=32)
    idempotency_mode: Literal["none", "header"] = "none"
    response_mode: Literal["accepted", "ok", "no_content"] = "accepted"
    input_mapping: dict[str, Any] = Field(default_factory=dict)

    @field_validator("project_id", mode="before")
    @classmethod
    def _require_project_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("project_id must not be blank")
        return normalized


class WorkflowEntrypointUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    description: str | None = None
    enabled: bool | None = None
    auth_mode: str | None = None
    auth_config: dict[str, Any] | None = None
    # Write-only replacement for an operator-supplied signing secret. Omitting it
    # (or sending null) leaves the stored credential untouched.
    signing_secret: str | None = None
    source_restrictions: dict[str, Any] | None = None
    allowed_content_types: list[str] | None = None
    max_body_bytes: int | None = Field(default=None, ge=1, le=104857600)
    max_file_bytes: int | None = Field(default=None, ge=1, le=104857600)
    max_files: int | None = Field(default=None, ge=0, le=32)
    idempotency_mode: Literal["none", "header"] | None = None
    response_mode: Literal["accepted", "ok", "no_content"] | None = None
    input_mapping: dict[str, Any] | None = None


class WorkflowEntrypointDryRunRequest(BaseModel):
    project_id: str | None = None
    content_type: str = APPLICATION_JSON_VALUE
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    form: dict[str, Any] = Field(default_factory=dict)
    files: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowProviderTriggerRequest(BaseModel):
    trigger_id: str | None = None
    project_id: str | None = None
    provider_key: str | None = None
    trigger_type_id: str
    display_name: str = ""
    enabled: bool | None = None
    trigger_config: dict[str, Any] = Field(default_factory=dict)


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
