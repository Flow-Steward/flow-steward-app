# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_validation import _as_dict, _issue, _normalize_string_list


def _operation_result_fields(operation_payload: dict[str, Any]) -> set[str]:
    payload = _as_dict(operation_payload)
    contract = _as_dict(payload.get("contract"))
    fields = _normalize_string_list(payload.get("result_fields"))
    fields.extend(_normalize_string_list(contract.get("result_fields")))
    return {str(item or "").strip() for item in fields if str(item or "").strip()}


def _normalize_error_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return "".join(ch for ch in raw.replace("-", "_") if ch.isalnum() or ch == "_")


def _operation_error_codes(operation_payload: dict[str, Any]) -> set[str]:
    payload = _as_dict(operation_payload)
    contract = _as_dict(payload.get("contract"))
    values = _normalize_string_list(payload.get("error_codes"))
    values.extend(_normalize_string_list(contract.get("error_codes")))
    return {_normalize_error_code(item) for item in values if _normalize_error_code(item)}


def _validate_task_ingress_profile(
    *,
    issues: list[dict],
    manifest_path: str,
    manifest_version: int,
    kind: str,
    features: list[str],
    query_definitions: list[dict],
    action_definitions: list[dict],
) -> None:
    if manifest_version < 2:
        return

    normalized_kind = str(kind or "").strip().lower()
    feature_set = {str(item or "").strip().lower() for item in features if str(item or "").strip()}
    declares_task_ingress = TASK_INGRESS_FEATURE_ID in feature_set

    if normalized_kind == "task_provider" and not declares_task_ingress:
        issues.append(
            _issue(
                severity="error",
                code="task_ingress_feature_required",
                message=(
                    "Task provider manifests must declare the 'task_ingress' feature "
                    "to opt into the canonical ingress contract"
                ),
                file_path=manifest_path,
                field_path="features",
            )
        )
        return

    if declares_task_ingress and normalized_kind not in {"", "task_provider"}:
        issues.append(
            _issue(
                severity="error",
                code="task_ingress_kind_mismatch",
                message=(
                    "The 'task_ingress' feature is only valid for kind='task_provider' manifests"
                ),
                file_path=manifest_path,
                field_path="kind",
            )
        )
        return

    if not declares_task_ingress:
        return

    allowed_error_codes = {_normalize_error_code(code) for code in TASK_INGRESS_ERROR_CODES}
    query_index: dict[str, tuple[int, dict[str, Any]]] = {}
    for idx, raw_query in enumerate(query_definitions):
        query_payload = _as_dict(raw_query)
        query_id = str(query_payload.get("query_id") or "").strip()
        if query_id and query_id not in query_index:
            query_index[query_id] = (idx, query_payload)

    action_index: dict[str, tuple[int, dict[str, Any]]] = {}
    for idx, raw_action in enumerate(action_definitions):
        action_payload = _as_dict(raw_action)
        action_id = str(action_payload.get("action_id") or "").strip()
        if action_id and action_id not in action_index:
            action_index[action_id] = (idx, action_payload)

    for action_id, (action_idx, action_payload) in action_index.items():
        declared_error_codes = _operation_error_codes(action_payload)
        for error_code in sorted(declared_error_codes):
            if error_code in allowed_error_codes:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_unknown_error_code",
                    message=(
                        f"Action '{action_id}' declares unsupported error code '{error_code}'"
                    ),
                    file_path=manifest_path,
                    field_path=f"action_definitions[{action_idx}].error_codes",
                    hint=(
                        "Use only codes from TASK_INGRESS_ERROR_CODES catalog "
                        "(normalize with snake_case)."
                    ),
                )
            )

    for query_id, (query_idx, query_payload) in query_index.items():
        declared_error_codes = _operation_error_codes(query_payload)
        for error_code in sorted(declared_error_codes):
            if error_code in allowed_error_codes:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_unknown_error_code",
                    message=(f"Query '{query_id}' declares unsupported error code '{error_code}'"),
                    file_path=manifest_path,
                    field_path=f"query_definitions[{query_idx}].error_codes",
                    hint=(
                        "Use only codes from TASK_INGRESS_ERROR_CODES catalog "
                        "(normalize with snake_case)."
                    ),
                )
            )

    for action_id in TASK_INGRESS_REQUIRED_ACTION_IDS:
        if action_id not in action_index:
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_action",
                    message=f"Task ingress profile requires action_id '{action_id}'",
                    file_path=manifest_path,
                    field_path="action_definitions",
                )
            )
            continue

        action_idx, action_payload = action_index[action_id]
        result_fields = _operation_result_fields(action_payload)
        required_fields = TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS.get(action_id) or ()
        for required_field in required_fields:
            if required_field in result_fields:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_action_result_field",
                    message=(
                        f"Action '{action_id}' must declare result_fields containing "
                        f"'{required_field}' for task ingress contract"
                    ),
                    file_path=manifest_path,
                    field_path=f"action_definitions[{action_idx}].result_fields",
                )
            )
        declared_error_codes = _operation_error_codes(action_payload)
        required_error_codes = TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES.get(action_id) or ()
        for required_error_code in required_error_codes:
            normalized_required_code = _normalize_error_code(required_error_code)
            if normalized_required_code in declared_error_codes:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_action_error_code",
                    message=(
                        f"Action '{action_id}' must declare error_codes containing "
                        f"'{normalized_required_code}' for task ingress contract"
                    ),
                    file_path=manifest_path,
                    field_path=f"action_definitions[{action_idx}].error_codes",
                )
            )

    for query_id in TASK_INGRESS_REQUIRED_QUERY_IDS:
        if query_id not in query_index:
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_query",
                    message=f"Task ingress profile requires query_id '{query_id}'",
                    file_path=manifest_path,
                    field_path="query_definitions",
                )
            )
            continue

        query_idx, query_payload = query_index[query_id]
        result_fields = _operation_result_fields(query_payload)
        required_fields = TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS.get(query_id) or ()
        for required_field in required_fields:
            if required_field in result_fields:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_query_result_field",
                    message=(
                        f"Query '{query_id}' must declare result_fields containing "
                        f"'{required_field}' for task ingress contract"
                    ),
                    file_path=manifest_path,
                    field_path=f"query_definitions[{query_idx}].result_fields",
                )
            )
        declared_error_codes = _operation_error_codes(query_payload)
        required_error_codes = TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES.get(query_id) or ()
        for required_error_code in required_error_codes:
            normalized_required_code = _normalize_error_code(required_error_code)
            if normalized_required_code in declared_error_codes:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="task_ingress_missing_query_error_code",
                    message=(
                        f"Query '{query_id}' must declare error_codes containing "
                        f"'{normalized_required_code}' for task ingress contract"
                    ),
                    file_path=manifest_path,
                    field_path=f"query_definitions[{query_idx}].error_codes",
                )
            )


def _validate_feature_profiles(
    *,
    issues: list[dict],
    manifest_path: str,
    kind: str,
    features: list[str],
    supported_events: list[str],
    action_definitions: list[dict],
) -> None:
    normalized_kind = str(kind or "").strip().lower()
    feature_set = {str(item or "").strip().lower() for item in features if str(item or "").strip()}
    supported_event_set = {
        str(item or "").strip() for item in supported_events if str(item or "").strip()
    }

    action_index: dict[str, tuple[int, dict[str, Any]]] = {}
    for idx, raw_action in enumerate(action_definitions):
        action_payload = _as_dict(raw_action)
        action_id = str(action_payload.get("action_id") or "").strip()
        if action_id and action_id not in action_index:
            action_index[action_id] = (idx, action_payload)

    if normalized_kind == "notification_provider":
        for required_feature in NOTIFICATION_PROVIDER_REQUIRED_FEATURES:
            if required_feature in feature_set:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="notification_provider_missing_feature",
                    message=(
                        f"Notification provider manifests must declare feature '{required_feature}'"
                    ),
                    file_path=manifest_path,
                    field_path="features",
                )
            )

    if normalized_kind == "tool_provider":
        for required_feature in TOOL_PROVIDER_REQUIRED_FEATURES:
            if required_feature in feature_set:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="tool_provider_missing_feature",
                    message=(f"Tool provider manifests must declare feature '{required_feature}'"),
                    file_path=manifest_path,
                    field_path="features",
                )
            )

    declares_ingress_event = INGRESS_EVENT_FEATURE_ID in feature_set
    if not declares_ingress_event:
        return

    if normalized_kind not in {"", "notification_provider"}:
        issues.append(
            _issue(
                severity="error",
                code="ingress_event_kind_mismatch",
                message=("Feature 'ingress_event' is only valid for kind='notification_provider'"),
                file_path=manifest_path,
                field_path="kind",
            )
        )
        return

    if INGRESS_EVENT_REQUIRED_ACTION_ID not in action_index:
        issues.append(
            _issue(
                severity="error",
                code="ingress_event_missing_action",
                message=(
                    f"Feature 'ingress_event' requires action_id "
                    f"'{INGRESS_EVENT_REQUIRED_ACTION_ID}'"
                ),
                file_path=manifest_path,
                field_path="action_definitions",
            )
        )
    else:
        action_idx, action_payload = action_index[INGRESS_EVENT_REQUIRED_ACTION_ID]
        result_fields = _operation_result_fields(action_payload)
        for required_field in INGRESS_EVENT_REQUIRED_ACTION_RESULT_FIELDS:
            if required_field in result_fields:
                continue
            issues.append(
                _issue(
                    severity="error",
                    code="ingress_event_missing_action_result_field",
                    message=(
                        f"Action '{INGRESS_EVENT_REQUIRED_ACTION_ID}' must declare "
                        f"result_fields containing '{required_field}'"
                    ),
                    file_path=manifest_path,
                    field_path=f"action_definitions[{action_idx}].result_fields",
                )
            )

    for required_event in INGRESS_EVENT_REQUIRED_EVENT_TYPES:
        if required_event in supported_event_set:
            continue
        issues.append(
            _issue(
                severity="error",
                code="ingress_event_missing_supported_event",
                message=(
                    f"Feature 'ingress_event' requires supported_events to include "
                    f"'{required_event}'"
                ),
                file_path=manifest_path,
                field_path="supported_events",
            )
        )

    for recommended_event in INGRESS_EVENT_RECOMMENDED_EVENT_TYPES:
        if recommended_event in supported_event_set:
            continue
        issues.append(
            _issue(
                severity="warning",
                code="ingress_event_missing_recommended_event",
                message=(
                    f"Consider adding '{recommended_event}' to supported_events for richer "
                    "interaction telemetry."
                ),
                file_path=manifest_path,
                field_path="supported_events",
            )
        )


__all__ = [
    "_operation_result_fields",
    "_normalize_error_code",
    "_operation_error_codes",
    "_validate_task_ingress_profile",
    "_validate_feature_profiles",
]
