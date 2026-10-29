"""Connector artifact/dataset grant policy, preparation and cleanup."""

from __future__ import annotations

import logging
from typing import Any

from core.application.orchestration.connector_mapping import (
    _get_path,
    _to_object,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes

logger = logging.getLogger(__name__)

_ARTIFACT_PLANE_SINGLETON: Any | None = None

_EXTENSION_ARTIFACT_MAX_BYTES_ENV = "FS_EXTENSION_ARTIFACT_MAX_BYTES"

_DEFAULT_EXTENSION_ARTIFACT_LIMIT_BYTES = 200 * 1024 * 1024

_MIN_EXTENSION_ARTIFACT_LIMIT_BYTES = 1024 * 1024

_MAX_EXTENSION_ARTIFACT_LIMIT_BYTES = 10 * 1024 * 1024 * 1024


def _shared_artifact_plane_from_state(state: dict[str, Any]) -> Any | None:
    plane = state.get("_artifact_plane") or state.get("artifact_plane")
    if plane is not None:
        return plane
    try:
        from core.infrastructure.runtime import http_app_request_helpers

        plane = getattr(http_app_request_helpers, "_artifact_plane", None)
        if plane is not None:
            return plane
    except Exception:
        pass
    global _ARTIFACT_PLANE_SINGLETON
    if _ARTIFACT_PLANE_SINGLETON is not None:
        return _ARTIFACT_PLANE_SINGLETON
    try:
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import create_storage_adapter
        from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

        _ARTIFACT_PLANE_SINGLETON = build_artifact_storage(
            environ=dict(_runtime_env),
            create_storage_adapter=create_storage_adapter,
            artifact_plane_factory=ArtifactDatasetPlane,
        )
    except Exception:
        _ARTIFACT_PLANE_SINGLETON = None
    return _ARTIFACT_PLANE_SINGLETON


def _extension_contract_rows(extension: dict[str, Any], section: str) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if isinstance(extension.get(section), list):
        rows.extend(extension.get(section) or [])
    compiled = _to_object(extension.get("compiled_manifest"))
    if isinstance(compiled.get(section), list):
        rows.extend(compiled.get(section) or [])
    for key in ("extension_contract_v2", "contract_v2"):
        contract = _to_object(extension.get(key)) or _to_object(compiled.get(key))
        normalized = _to_object(contract.get("normalized_contract")) or contract
        value = normalized.get(section)
        if isinstance(value, list):
            rows.extend(value)
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = _to_object(row)
        if payload:
            normalized_rows.append(payload)
    return normalized_rows


def _artifact_policy_for_operation(
    extension: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        return {}
    for row in _extension_contract_rows(extension, "artifact_policies"):
        if str(row.get("operation_id") or "").strip() == normalized_operation_id:
            return row
    return {}


def _policy_items(policy: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in keys:
        value = policy.get(key)
        if isinstance(value, list):
            rows.extend(_to_object(item) for item in value)
    return [row for row in rows if row]


def _policy_kind(item: dict[str, Any]) -> str:
    value = (
        str(
            item.get("kind")
            or item.get("grant_kind")
            or item.get("handle_kind")
            or item.get("value_type")
            or item.get("type")
            or ""
        )
        .strip()
        .lower()
    )
    return "dataset" if value in {"dataset", "datasets", "dataset_handle"} else "artifact"


def _policy_max_size(item: dict[str, Any]) -> int | None:
    env_name = str(item.get("max_size_env") or "").strip()
    if env_name == _EXTENSION_ARTIFACT_MAX_BYTES_ENV:
        return clamp_size_limit_bytes(
            _runtime_env.get("FS_EXTENSION_ARTIFACT_MAX_BYTES"),
            default=_DEFAULT_EXTENSION_ARTIFACT_LIMIT_BYTES,
            minimum=_MIN_EXTENSION_ARTIFACT_LIMIT_BYTES,
            maximum=_MAX_EXTENSION_ARTIFACT_LIMIT_BYTES,
        )
    raw_default = (
        item.get("max_size_bytes") or item.get("max_bytes") or item.get("size_limit_bytes")
    )
    if raw_default is None:
        return None
    try:
        default = int(raw_default)
    except (TypeError, ValueError):
        return None
    if default <= 0:
        return None
    return default


def _project_state_value(
    project_state: dict[str, Any],
    reference: dict[str, Any],
) -> Any:
    namespace = str(reference.get("namespace") or "").strip()
    state_key = str(reference.get("state_key") or reference.get("key") or "").strip()
    if not namespace or not state_key:
        return None
    for item in project_state.get("items") or []:
        row = _to_object(item)
        if (
            str(row.get("namespace") or "").strip() == namespace
            and str(row.get("state_key") or "").strip() == state_key
        ):
            value = row.get("value")
            field = str(reference.get("field") or "").strip()
            return _get_path(_to_object(value), field) if field else value
    return None


def _resolve_policy_handle(
    item: dict[str, Any],
    *,
    input_payload: dict[str, Any],
    project_state: dict[str, Any],
) -> Any:
    state_ref = _to_object(item.get("from_project_state"))
    if state_ref:
        return _project_state_value(project_state, state_ref)
    for key in ("from_input", "input_path", "path"):
        path = str(item.get(key) or "").strip()
        if path:
            return _get_path(input_payload, path)
    field_name = str(
        item.get("field") or item.get("input") or item.get("name") or item.get("binding_key") or ""
    ).strip()
    if field_name:
        return input_payload.get(field_name)
    return item.get("value")


def _policy_input_grant_rows(
    item: dict[str, Any],
    *,
    kind: str,
    input_payload: dict[str, Any],
    project_state: dict[str, Any],
) -> list[dict[str, Any]]:
    value = _resolve_policy_handle(
        item,
        input_payload=input_payload,
        project_state=project_state,
    )
    if value is None or value == "" or value == []:
        return []
    values = value if isinstance(value, list) else [value]
    base_binding_key = str(item.get("binding_key") or item.get("name") or "").strip()
    rows: list[dict[str, Any]] = []
    for raw_value in values:
        raw_row = _to_object(raw_value)
        binding_key = base_binding_key
        if raw_row:
            handle = (
                raw_row.get(f"{kind}_handle")
                or raw_row.get(f"{kind}_id")
                or raw_row.get("handle")
                or raw_row.get("id")
                or raw_row.get("value")
            )
            binding_key = str(
                raw_row.get("binding_key") or raw_row.get("name") or binding_key
            ).strip()
        else:
            handle = raw_value
        if not handle:
            raise ValueError(
                f"missing_{kind}_input:{item.get('name') or item.get('binding_key') or ''!s}"
            )
        row = {f"{kind}_handle": handle, "binding_key": binding_key}
        content_type = str(item.get("content_type") or "").strip()
        if content_type:
            row["content_type"] = content_type
        max_size = _policy_max_size(item)
        if max_size is not None:
            row["max_size_bytes"] = max_size
        rows.append(row)
    return rows


def _grant_requests_from_policy(
    policy: dict[str, Any],
    *,
    input_payload: dict[str, Any],
    project_state: dict[str, Any],
) -> dict[str, list[Any]]:
    requests = {
        "artifact_inputs": [],
        "artifact_outputs": [],
        "artifact_output_specs": [],
        "dataset_inputs": [],
        "dataset_outputs": [],
        "dataset_output_specs": [],
    }
    for item in _policy_items(policy, "inputs", "read_grants", "reads"):
        kind = _policy_kind(item)
        rows = _policy_input_grant_rows(
            item,
            kind=kind,
            input_payload=input_payload,
            project_state=project_state,
        )
        if not rows and item.get("required") is False:
            continue
        if not rows:
            raise ValueError(
                f"missing_{kind}_input:{item.get('name') or item.get('binding_key') or ''!s}"
            )
        requests[f"{kind}_inputs"].extend(rows)
    for item in _policy_items(policy, "outputs", "write_grants", "writes"):
        kind = _policy_kind(item)
        handle = _resolve_policy_handle(
            item,
            input_payload=input_payload,
            project_state=project_state,
        )
        binding_key = str(item.get("binding_key") or item.get("name") or "").strip()
        if handle:
            requests[f"{kind}_outputs"].append(
                {
                    f"{kind}_handle": handle,
                    "binding_key": binding_key,
                }
            )
            continue
        spec = {
            "binding_key": binding_key,
            "name": str(item.get("name") or binding_key).strip(),
            "run_id": item.get("run_id"),
            "step_id": item.get("step_id"),
            "filename": item.get("filename") or item.get("name") or "output",
            "content_type": item.get("content_type"),
            "retention_class": item.get("retention_class") or item.get("retention"),
            "max_size_bytes": _policy_max_size(item),
            "schema": _to_object(item.get("schema")),
        }
        requests[f"{kind}_output_specs"].append(spec)
    return requests


def _prepare_grants_for_extension_action(
    *,
    extension: dict[str, Any],
    action_id: str,
    input_payload: dict[str, Any],
    project_state: dict[str, Any],
    runtime_context: dict[str, Any],
    runtime_scope: Any,
    state: dict[str, Any],
    before_materialize_outputs: Any = None,
    include_output_grants: bool = True,
) -> tuple[Any | None, Any | None]:
    policy = _artifact_policy_for_operation(extension, action_id)
    if not policy:
        return None, None
    requests = _grant_requests_from_policy(
        policy,
        input_payload=input_payload,
        project_state=project_state,
    )
    if not include_output_grants:
        for key in (
            "artifact_outputs",
            "artifact_output_specs",
            "dataset_outputs",
            "dataset_output_specs",
        ):
            requests[key] = []
    from core.application.extensions import runtime_grants
    from core.application.extensions.host_contract import (
        EXECUTION_SCOPE_FIELDS,
        normalize_execution_scope,
    )

    if not any(requests.values()):
        return None, runtime_grants.PreparedExtensionGrants()
    plane = _shared_artifact_plane_from_state(state)
    if plane is None:
        raise RuntimeError("artifact_dataset_grants_unavailable")

    audit = {
        key: value
        for key, value in runtime_context.items()
        if key in {"account_id", "project_id", "workflow_id", "job_id", "step_id", "actor_id"}
    }
    if str(runtime_context.get("step_id") or "").strip():
        # A step's grants are bound to where it runs: its immediate Group or
        # For each business id, explicit null for the other owner (both null at
        # the root — root, not a wildcard), and its logical execution. Which
        # artifacts it may read is decided above by the step's mappings; this
        # only pins the grant to its consumer. A context without both owner
        # keys is refused here, before any grant is issued.
        audit.update(
            normalize_execution_scope(
                {
                    field: runtime_context[field]
                    for field in EXECUTION_SCOPE_FIELDS
                    if field in runtime_context
                }
            )
        )
    prepared = runtime_grants.prepare_extension_grants(
        plane=plane,
        scope=runtime_scope,
        audit=audit,
        extension_id=str(extension.get("extension_key") or extension.get("extension_id") or ""),
        before_materialize_outputs=before_materialize_outputs,
        installed_extension=extension,
        operation_id=action_id,
        **requests,
    )
    return plane, prepared


def _has_prepared_output_grants(prepared_grants: Any) -> bool:
    return bool(
        prepared_grants is not None
        and (
            list(getattr(prepared_grants, "artifact_outputs", None) or [])
            or list(getattr(prepared_grants, "dataset_outputs", None) or [])
        )
    )


def _cleanup_prepared_output_grants(grant_plane: Any, prepared_grants: Any) -> None:
    if grant_plane is None or prepared_grants is None:
        return
    try:
        from core.application.extensions import runtime_grants

        runtime_grants.cleanup_prepared_extension_grants(grant_plane, prepared_grants)
    except Exception as exc:
        logger.warning("failed to cleanup prepared extension output grants: %s", exc)
