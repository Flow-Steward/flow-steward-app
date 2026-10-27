"""Resolve extension action/query declarations and their audit policies."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .extension_runtime_common import ACTION_ID_REQUIRED_MESSAGE, QUERY_ID_REQUIRED_MESSAGE
from .extension_runtime_manifest import _resolve_object_ref
from .extension_runtime_validation import _as_dict, _as_list, _issue, _normalize_string_list


def _normalize_query_or_action_items(
    *,
    items_value: Any,
    item_key: str,
    ref_field_path: str,
    bundle_root: Path,
    manifest_path: Path,
    issues: list[dict],
) -> list[dict]:
    if isinstance(items_value, list):
        rows = items_value
    elif isinstance(items_value, dict):
        rows = _as_list(items_value.get(item_key))
    else:
        rows = []

    normalized: list[dict] = []
    for idx, item in enumerate(rows):
        field_path = f"{item_key}[{idx}]"
        if isinstance(item, dict) and str(item.get("ref") or "").strip():
            loaded, _ = _resolve_object_ref(
                bundle_root=bundle_root,
                parent_path=manifest_path,
                ref_text=str(item.get("ref") or "").strip(),
                issues=issues,
                field_path=f"{ref_field_path}.{field_path}",
            )
            if isinstance(loaded, dict):
                normalized.append(dict(loaded))
            elif loaded is not None:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_ref_item",
                        message="Referenced item must resolve to an object",
                        file_path=str(manifest_path),
                        field_path=f"{ref_field_path}.{field_path}",
                    )
                )
            continue
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_item",
                    message="Item must be an object",
                    file_path=str(manifest_path),
                    field_path=f"{ref_field_path}.{field_path}",
                )
            )
            continue
        normalized.append(dict(item))
    return normalized


def _resolve_query_manifest(
    *,
    bundle_root: Path,
    manifest_path: Path,
    ref_text: str,
    issues: list[dict],
) -> list[dict]:
    if not ref_text:
        return []
    data, query_manifest_path = _resolve_object_ref(
        bundle_root=bundle_root,
        parent_path=manifest_path,
        ref_text=ref_text,
        issues=issues,
        field_path="query_manifest",
    )
    if data is None:
        return []
    rows = _normalize_query_or_action_items(
        items_value=data,
        item_key="queries",
        ref_field_path="query_manifest",
        bundle_root=bundle_root,
        manifest_path=Path(query_manifest_path) if query_manifest_path else manifest_path,
        issues=issues,
    )

    seen: set[str] = set()
    normalized_queries: list[dict] = []
    for idx, row in enumerate(rows):
        query_id = str(row.get("query_id") or row.get("id") or "").strip()
        if not query_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_query_id",
                    message=QUERY_ID_REQUIRED_MESSAGE,
                    file_path=query_manifest_path,
                    field_path=f"queries[{idx}].query_id",
                )
            )
            continue
        if query_id in seen:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_query_id",
                    message=f"Duplicate query_id '{query_id}'",
                    file_path=query_manifest_path,
                    field_path=f"queries[{idx}].query_id",
                )
            )
            continue
        seen.add(query_id)
        normalized_queries.append(
            {
                "query_id": query_id,
                "title": str(row.get("title") or query_id).strip(),
                "description": str(row.get("description") or "").strip(),
                "required_scopes": _normalize_string_list(row.get("required_scopes")),
                "result_fields": _normalize_string_list(row.get("result_fields")),
                "error_codes": _normalize_string_list(row.get("error_codes")),
                "contract": _as_dict(row.get("contract")),
                "handler": _as_dict(row.get("handler") or row.get("runtime") or {}),
            }
        )
    return normalized_queries


def _as_string_list(value: Any) -> list[str]:
    """One declared name as a list, so singular and plural share a normalizer."""
    text = str(value or "").strip()
    return [text] if text else []


def _resolve_action_manifest(
    *,
    bundle_root: Path,
    manifest_path: Path,
    ref_text: str,
    issues: list[dict],
) -> list[dict]:
    if not ref_text:
        return []
    data, action_manifest_path = _resolve_object_ref(
        bundle_root=bundle_root,
        parent_path=manifest_path,
        ref_text=ref_text,
        issues=issues,
        field_path="action_manifest",
    )
    if data is None:
        return []
    rows = _normalize_query_or_action_items(
        items_value=data,
        item_key="actions",
        ref_field_path="action_manifest",
        bundle_root=bundle_root,
        manifest_path=Path(action_manifest_path) if action_manifest_path else manifest_path,
        issues=issues,
    )

    seen: set[str] = set()
    normalized_actions: list[dict] = []
    for idx, row in enumerate(rows):
        action_id = str(row.get("action_id") or row.get("id") or "").strip()
        if not action_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_action_id",
                    message=ACTION_ID_REQUIRED_MESSAGE,
                    file_path=action_manifest_path,
                    field_path=f"actions[{idx}].action_id",
                )
            )
            continue
        if action_id in seen:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_action_id",
                    message=f"Duplicate action_id '{action_id}'",
                    file_path=action_manifest_path,
                    field_path=f"actions[{idx}].action_id",
                )
            )
            continue
        seen.add(action_id)

        workflow_visible = row.get("workflow_visible")
        if not isinstance(workflow_visible, bool):
            issues.append(
                _issue(
                    severity="error",
                    code="workflow_visibility_required",
                    message="Every action must declare workflow_visible as an explicit boolean",
                    file_path=action_manifest_path,
                    field_path=f"actions[{idx}].workflow_visible",
                )
            )
            workflow_visible = False

        audit_policy = _normalize_action_audit_policy(
            row.get("audit"),
            issues=issues,
            action_manifest_path=action_manifest_path,
            field_path=f"actions[{idx}].audit",
        )
        normalized_actions.append(
            {
                "action_id": action_id,
                "title": str(row.get("title") or action_id).strip(),
                "description": str(row.get("description") or "").strip(),
                "required_scopes": _normalize_string_list(row.get("required_scopes")),
                "result_fields": _normalize_string_list(row.get("result_fields")),
                "error_codes": _normalize_string_list(row.get("error_codes")),
                "parameters": [
                    dict(item) for item in row.get("parameters", []) if isinstance(item, dict)
                ],
                "input_schema": _as_dict(row.get("input_schema")),
                "workflow_visible": workflow_visible,
                "contract": _as_dict(row.get("contract")),
                "handler": _as_dict(row.get("handler") or row.get("runtime") or {}),
                "command_type": str(row.get("command_type") or "").strip(),
                "mutates_platform": bool(row.get("mutates_platform", False)),
                # Carried through compilation deliberately: the runtime reads the
                # policy from the compiled action, so dropping it here would turn
                # a declared protection off with nothing to notice.
                **({"audit": audit_policy} if audit_policy else {}),
            }
        )
    return normalized_actions


#: An action's audit policy. Flow Steward applies it without knowing the extension,
#: so the shape is checked here, once, at compile time — a policy that reaches the
#: runtime malformed would silently protect nothing.
_AUDIT_POLICY_KEYS = frozenset({"redact_result_fields", "failure_projection"})
_AUDIT_FAILURE_PROJECTION_KEYS = frozenset({"keep_row_fields"})
_AUDIT_MAX_FIELDS = 64
_AUDIT_MAX_FIELD_LENGTH = 64
_AUDIT_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _normalize_audit_field_names(
    value: Any,
    *,
    issues: list[dict],
    action_manifest_path: str,
    field_path: str,
    allow_empty: bool,
) -> list[str] | None:
    """Return the declared field names, or None when the declaration is unusable."""

    def _reject(message: str) -> None:
        issues.append(
            _issue(
                severity="error",
                code="invalid_action_audit_policy",
                message=message,
                file_path=action_manifest_path,
                field_path=field_path,
            )
        )

    if not isinstance(value, list):
        _reject(f"{field_path} must be a list of field names")
        return None
    if len(value) > _AUDIT_MAX_FIELDS:
        _reject(
            f"{field_path} declares {len(value)} names; at most {_AUDIT_MAX_FIELDS} are allowed"
        )
        return None
    if not value and not allow_empty:
        _reject(f"{field_path} must name at least one field")
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _reject(f"{field_path} must contain only strings, got {type(item).__name__}")
            return None
        name = item.strip()
        if not name or len(name) > _AUDIT_MAX_FIELD_LENGTH:
            _reject(f"{field_path} entry {item!r} must be 1-{_AUDIT_MAX_FIELD_LENGTH} characters")
            return None
        if not _AUDIT_FIELD_NAME.match(name):
            _reject(f"{field_path} entry {name!r} is not a field name")
            return None
        if name in names:
            _reject(f"{field_path} names {name!r} more than once")
            return None
        names.append(name)
    return names


def _normalize_action_audit_policy(
    value: Any,
    *,
    issues: list[dict],
    action_manifest_path: str,
    field_path: str,
) -> dict | None:
    """Validate one action's declared audit policy.

    An absent policy is legal and means "no policy". A policy that is present but
    malformed is an error that fails validation and compilation, because the
    alternative — dropping it — turns a declared protection off without saying so.
    """

    def _reject(message: str) -> None:
        issues.append(
            _issue(
                severity="error",
                code="invalid_action_audit_policy",
                message=message,
                file_path=action_manifest_path,
                field_path=field_path,
            )
        )

    if value is None:
        return None
    if not isinstance(value, dict):
        _reject(f"{field_path} must be a mapping")
        return None
    unknown = sorted(set(value) - _AUDIT_POLICY_KEYS)
    if unknown:
        _reject(f"{field_path} has unknown keys: {', '.join(unknown)}")
        return None
    if not value:
        _reject(f"{field_path} declares nothing; remove it or declare a policy")
        return None

    policy: dict[str, Any] = {}
    if "redact_result_fields" in value:
        redacted = _normalize_audit_field_names(
            value["redact_result_fields"],
            issues=issues,
            action_manifest_path=action_manifest_path,
            field_path=f"{field_path}.redact_result_fields",
            allow_empty=False,
        )
        if redacted is None:
            return None
        policy["redact_result_fields"] = redacted
    if "failure_projection" in value:
        projection = value["failure_projection"]
        projection_path = f"{field_path}.failure_projection"
        if not isinstance(projection, dict):
            _reject(f"{projection_path} must be a mapping")
            return None
        unknown_projection = sorted(set(projection) - _AUDIT_FAILURE_PROJECTION_KEYS)
        if unknown_projection:
            _reject(f"{projection_path} has unknown keys: {', '.join(unknown_projection)}")
            return None
        keep_rows = _normalize_audit_field_names(
            projection.get("keep_row_fields", []),
            issues=issues,
            action_manifest_path=action_manifest_path,
            field_path=f"{projection_path}.keep_row_fields",
            allow_empty=True,
        )
        if keep_rows is None:
            return None
        policy["failure_projection"] = {"keep_row_fields": keep_rows}
    return policy
