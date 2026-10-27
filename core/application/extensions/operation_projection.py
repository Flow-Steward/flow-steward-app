from __future__ import annotations

import copy
import json
import re
from typing import Any
from uuid import UUID

from .extension_descriptor import is_llm_secret_field_name
from .external_effects import EXTERNAL_EFFECT_STATUSES
from .operation_context import _ActionExecutionContext

_MESSAGE_EXTENSION_ACTION_FAILED = "Extension action failed"

_AUDIT_REDACTED = "<redacted>"


_AUDIT_SENSITIVE_KEY_TOKENS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)


_AUDIT_SENSITIVE_CONTAINERS = {"config", "connectionconfig", "providerconfig"}


_AUDIT_SENSITIVE_MATERIAL_KEYS = {"cert", "certificate", "dsn", "key", "keymaterial"}


_AUDIT_SENSITIVE_MATERIAL_SUFFIXES = (
    "certificate",
    "cert",
    "dsn",
    "keymaterial",
    "privatekey",
    "publickey",
    "signingkey",
    "encryptionkey",
)


#: The manifest owns the audit policy; Core applies declared fields generically.
_AUDIT_POLICY_KEY = "audit"


_AUDIT_REDACT_FIELDS_KEY = "redact_result_fields"


_AUDIT_FAILURE_PROJECTION_KEY = "failure_projection"


_AUDIT_KEEP_ROW_FIELDS_KEY = "keep_row_fields"


_AUDIT_MAX_DECLARED_FIELDS = 64


#: A damaged declared policy must never disable protection silently.
_AUDIT_FAIL_CLOSED_KEY = "fail_closed"


_CLIENT_ACTION_OUTPUT_KEYS = frozenset({"grant_outputs", "produced_artifacts", "produced_datasets"})


_CLIENT_ACTION_CODE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")


_CLIENT_ACTION_STATUSES = frozenset({"failed", "succeeded"})


_CLIENT_ACTION_ERROR_MAX_LENGTH = 512


_ABSOLUTE_POSIX_PATH_RE = re.compile(r"(?:^|[\s(\"'])/(?!/)[^\s]*")


_ABSOLUTE_WINDOWS_PATH_RE = re.compile(r"(?:^|[\s(\"'])(?:[a-zA-Z]:[\\/]|\\\\)")


_LOCAL_FILE_URL_RE = re.compile(r"(?:^|\s)file://", re.IGNORECASE)


def _normalized_audit_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _redact_action_audit_value(value: Any, *, parent_key: str = "") -> Any:
    normalized_key = _normalized_audit_key(parent_key)
    if (
        normalized_key in _AUDIT_SENSITIVE_CONTAINERS
        or normalized_key in _AUDIT_SENSITIVE_MATERIAL_KEYS
        or any(token in normalized_key for token in _AUDIT_SENSITIVE_KEY_TOKENS)
        or any(normalized_key.endswith(suffix) for suffix in _AUDIT_SENSITIVE_MATERIAL_SUFFIXES)
        or is_llm_secret_field_name(parent_key)
    ):
        return _AUDIT_REDACTED
    if isinstance(value, dict):
        return {
            str(key): _redact_action_audit_value(raw, parent_key=str(key))
            for key, raw in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_action_audit_value(item, parent_key=parent_key) for item in value]
    return copy.deepcopy(value)


def _redact_action_audit_fields(value: Any, *, field_names: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                _AUDIT_REDACTED
                if _normalized_audit_key(key) in field_names
                else _redact_action_audit_fields(raw, field_names=field_names)
            )
            for key, raw in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_action_audit_fields(item, field_names=field_names) for item in value]
    return copy.deepcopy(value)


def _safe_action_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    code = value.strip()
    return code if _CLIENT_ACTION_CODE_RE.fullmatch(code) else ""


def _safe_action_error(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    message = value.strip()
    if not message or len(message) > _CLIENT_ACTION_ERROR_MAX_LENGTH:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in message):
        return ""
    if (
        _ABSOLUTE_POSIX_PATH_RE.search(message)
        or _ABSOLUTE_WINDOWS_PATH_RE.search(message)
        or _LOCAL_FILE_URL_RE.search(message)
    ):
        return ""
    return message


def _json_safe_copy(value: Any) -> tuple[bool, Any]:
    try:
        return True, json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        return False, None


def _canonical_action_status(value: Any) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in _CLIENT_ACTION_STATUSES else "failed"


def _client_action_invocation(
    invocation: dict[str, Any], *, host_external_effect: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Project an extension invocation to the closed public action response."""
    status = _canonical_action_status(invocation.get("status"))
    raw_response = invocation.get("response")
    response: dict[str, Any] = {}
    if isinstance(raw_response, dict):
        if isinstance(raw_response.get("ok"), bool):
            response["ok"] = raw_response["ok"]
        error_code = _safe_action_code(raw_response.get("error_code"))
        if error_code:
            response["error_code"] = error_code
        error = _safe_action_error(raw_response.get("error"))
        if error:
            response["error"] = error
        definitely_no_external_effect = raw_response.get("definitely_no_external_effect")
        if isinstance(definitely_no_external_effect, bool):
            response["definitely_no_external_effect"] = definitely_no_external_effect
        if "result" in raw_response:
            result_is_safe, result = _json_safe_copy(raw_response["result"])
            if result_is_safe:
                response["result"] = result
    if isinstance(host_external_effect, dict):
        effect_id = host_external_effect.get("effect_id")
        claim_version = host_external_effect.get("claim_version")
        effect_status = host_external_effect.get("status")
        operation_id = host_external_effect.get("operation_id")
        if (
            isinstance(effect_id, (str, UUID))
            and str(effect_id).strip()
            and isinstance(claim_version, int)
            and not isinstance(claim_version, bool)
            and claim_version > 0
            and effect_status in EXTERNAL_EFFECT_STATUSES
            and isinstance(operation_id, str)
            and operation_id.strip()
        ):
            response["external_effect"] = {
                "effect_id": str(effect_id),
                "claim_version": claim_version,
                "status": effect_status,
                "operation_id": operation_id,
            }
    raw_errors = invocation.get("errors")
    errors = (
        [
            {"code": code}
            for item in raw_errors
            if isinstance(item, dict) and (code := _safe_action_code(item.get("code")))
        ]
        if isinstance(raw_errors, list)
        else []
    )
    raw_detail = str(invocation.get("detail") or "").strip()
    detail = _MESSAGE_EXTENSION_ACTION_FAILED
    if status == "succeeded":
        detail = ""
    elif _CLIENT_ACTION_CODE_RE.fullmatch(raw_detail):
        detail = raw_detail
    projected = {
        "status": status,
        "detail": detail,
        "response": response,
        "errors": errors,
    }
    for key in _CLIENT_ACTION_OUTPUT_KEYS:
        if key not in invocation:
            continue
        output_is_safe, output = _json_safe_copy(invocation[key])
        if output_is_safe:
            projected[key] = output
    from core.application.orchestration.retry_policy import normalize_retry_signal

    projected.update(normalize_retry_signal(invocation))
    projected.update(normalize_retry_signal(raw_response))
    return projected


def _safe_audit_code(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        return ""
    if not all(
        character.islower() or character.isdigit() or character in "._-" for character in value
    ):
        return ""
    return value


def _project_failure_status_row(value: Any, *, keep_row_fields: frozenset[str]) -> dict[str, Any]:
    """Keep a failed row's protocol facts, plus the identifiers the action declares.

    Core owns `status`, `code` and `error_code` — they are its own vocabulary. A row
    identifier is the extension's, so it is named in the manifest and admitted only
    as a positive integer or a safe code, never as free text.
    """
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for row_field in sorted(keep_row_fields):
        raw = value.get(row_field)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            projected[row_field] = raw
        elif code := _safe_audit_code(raw):
            projected[row_field] = code
    status = value.get("status")
    if isinstance(status, str) and status in EXTERNAL_EFFECT_STATUSES:
        projected["status"] = status
    for key in ("code", "error_code"):
        code = _safe_audit_code(value.get(key))
        if code:
            projected[key] = code
    return projected


def _project_failure_errors(value: Any, *, keep_row_fields: frozenset[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        projected
        for item in value
        if (projected := _project_failure_status_row(item, keep_row_fields=keep_row_fields))
    ]


def _project_failure_result(value: Any, *, keep_row_fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    results = value.get("results")
    if isinstance(results, list):
        projected_results = [
            row
            for item in results
            if (row := _project_failure_status_row(item, keep_row_fields=keep_row_fields))
        ]
        if projected_results:
            projected["results"] = projected_results
    external_effect_status = value.get("external_effect_status")
    if isinstance(external_effect_status, str) and external_effect_status in (
        EXTERNAL_EFFECT_STATUSES
    ):
        projected["external_effect_status"] = external_effect_status
    definitely_no_external_effect = value.get("definitely_no_external_effect")
    if isinstance(definitely_no_external_effect, bool):
        projected["definitely_no_external_effect"] = definitely_no_external_effect
    return projected


def _project_failed_invocation(value: Any, *, keep_row_fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "failed"}
    projected: dict[str, Any] = {"status": "failed"}
    response = value.get("response")
    if isinstance(response, dict):
        projected_response: dict[str, Any] = {}
        if isinstance(response.get("ok"), bool):
            projected_response["ok"] = response["ok"]
        error_code = _safe_audit_code(response.get("error_code"))
        if error_code:
            projected_response["error_code"] = error_code
        errors = _project_failure_errors(response.get("errors"), keep_row_fields=keep_row_fields)
        if errors:
            projected_response["errors"] = errors
        result = _project_failure_result(response.get("result"), keep_row_fields=keep_row_fields)
        if result:
            projected_response["result"] = result
        external_effect_status = response.get("external_effect_status")
        if isinstance(external_effect_status, str) and external_effect_status in (
            EXTERNAL_EFFECT_STATUSES
        ):
            projected_response["external_effect_status"] = external_effect_status
        definitely_no_external_effect = response.get("definitely_no_external_effect")
        if isinstance(definitely_no_external_effect, bool):
            projected_response["definitely_no_external_effect"] = definitely_no_external_effect
        if projected_response:
            projected["response"] = projected_response
    errors = _project_failure_errors(value.get("errors"), keep_row_fields=keep_row_fields)
    if errors:
        projected["errors"] = errors
    from core.application.orchestration.retry_policy import normalize_retry_signal

    projected.update(normalize_retry_signal(value))
    projected.update(normalize_retry_signal(response))
    return projected


def contract_action_input_names(extension: dict[str, Any], action_id: str) -> frozenset[str] | None:
    """Return the input names an extension contract declares for ``action_id``.

    ``None`` means the contract does not describe the action, so callers must not
    filter or augment the payload. Shared with the workflow connector runtime so both
    call sites agree on which operations expect a ``connection_ref`` input.
    """
    compiled_manifest = extension.get("compiled_manifest")
    compiled_manifest = dict(compiled_manifest) if isinstance(compiled_manifest, dict) else {}
    runtime = extension.get("runtime")
    runtime = dict(runtime) if isinstance(runtime, dict) else {}
    candidates = (
        extension.get("extension_contract_v2_normalized"),
        runtime.get("extension_contract_v2_normalized"),
        compiled_manifest.get("extension_contract_v2_normalized"),
        dict(compiled_manifest.get("runtime") or {}).get("extension_contract_v2_normalized")
        if isinstance(compiled_manifest.get("runtime"), dict)
        else None,
    )
    operation_candidates = (
        extension.get("operation_manifest"),
        runtime.get("operation_manifest"),
        compiled_manifest.get("operation_manifest"),
        *(item.get("operation_manifest") for item in candidates if isinstance(item, dict)),
    )
    for operations in operation_candidates:
        for raw_operation in operations if isinstance(operations, list) else []:
            operation = raw_operation if isinstance(raw_operation, dict) else {}
            if str(operation.get("operation_id") or "").strip() != action_id:
                continue
            raw_inputs = operation.get("inputs") or operation.get("public_inputs")
            inputs = raw_inputs if isinstance(raw_inputs, list) else []
            return frozenset(
                str(item.get("name") or "").strip()
                for item in inputs
                if isinstance(item, dict)
                if str(item.get("name") or "").strip()
            )
    manifest_version = extension.get("manifest_version") or compiled_manifest.get(
        "manifest_version"
    )
    if str(manifest_version or "").strip() != "2":
        return None
    action_definitions = extension.get("action_definitions")
    for raw_action in action_definitions if isinstance(action_definitions, list) else []:
        action = raw_action if isinstance(raw_action, dict) else {}
        if str(action.get("action_id") or "").strip() != action_id:
            continue
        parameters = action.get("parameters")
        if not isinstance(parameters, list):
            return None
        return frozenset(
            str(item.get("name") or "").strip()
            for item in parameters
            if isinstance(item, dict)
            if str(item.get("name") or "").strip()
        )
    return None


def _declared_field_names(value: Any) -> frozenset[str]:
    """Normalise a declared list of field names, ignoring anything unusable."""
    if not isinstance(value, list):
        return frozenset()
    names = {
        normalized
        for item in value[:_AUDIT_MAX_DECLARED_FIELDS]
        if (normalized := _normalized_audit_key(item))
    }
    return frozenset(names)


def action_audit_policy(extension: dict[str, Any], action_id: str) -> dict[str, Any]:
    """Return the audit policy an extension declares for one action.

    The policy is the extension's, not Core's: Core neither knows which extension
    handles sensitive material nor what that material is called. An action declares

    ```yaml
    audit:
      redact_result_fields: [payload, note]        # never written to the audit record
      failure_projection: {keep_row_fields: [uid]}   # a failure keeps codes only
    ```

    and Core applies it. An absent or malformed block means "no policy", which is
    the same answer Core gives for an extension it has never seen.
    """
    wanted = str(action_id or "").strip()
    if not wanted:
        return {}
    compiled_manifest = extension.get("compiled_manifest")
    compiled_manifest = compiled_manifest if isinstance(compiled_manifest, dict) else {}
    runtime = extension.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    for definitions in (
        extension.get("action_definitions"),
        runtime.get("action_definitions"),
        compiled_manifest.get("action_definitions"),
    ):
        for raw_action in definitions if isinstance(definitions, list) else []:
            action = raw_action if isinstance(raw_action, dict) else {}
            if str(action.get("action_id") or "").strip() != wanted:
                continue
            declared = action.get(_AUDIT_POLICY_KEY)
            if declared is None:
                return {}
            if not isinstance(declared, dict) or not declared:
                return {_AUDIT_FAIL_CLOSED_KEY: True}
            if set(declared) - {_AUDIT_REDACT_FIELDS_KEY, _AUDIT_FAILURE_PROJECTION_KEY}:
                return {_AUDIT_FAIL_CLOSED_KEY: True}

            policy: dict[str, Any] = {}
            if _AUDIT_REDACT_FIELDS_KEY in declared:
                redact = _declared_field_names(declared.get(_AUDIT_REDACT_FIELDS_KEY))
                if not redact:
                    return {_AUDIT_FAIL_CLOSED_KEY: True}
                policy[_AUDIT_REDACT_FIELDS_KEY] = redact
            if _AUDIT_FAILURE_PROJECTION_KEY in declared:
                projection = declared.get(_AUDIT_FAILURE_PROJECTION_KEY)
                if not isinstance(projection, dict) or set(projection) - {
                    _AUDIT_KEEP_ROW_FIELDS_KEY
                }:
                    return {_AUDIT_FAIL_CLOSED_KEY: True}
                keep_rows = projection.get(_AUDIT_KEEP_ROW_FIELDS_KEY, [])
                if not isinstance(keep_rows, list):
                    return {_AUDIT_FAIL_CLOSED_KEY: True}
                policy[_AUDIT_FAILURE_PROJECTION_KEY] = frozenset(
                    str(item or "").strip()
                    for item in keep_rows[:_AUDIT_MAX_DECLARED_FIELDS]
                    if isinstance(item, str) and item.strip()
                )
            return policy or {_AUDIT_FAIL_CLOSED_KEY: True}
    return {}


def _redact_action_audit_payload(
    context: _ActionExecutionContext,
    value: Any,
    *,
    invocation: bool = False,
) -> Any:
    redacted = _redact_action_audit_value(value)
    policy = action_audit_policy(context.extension, context.action_id)
    if not policy:
        return redacted
    if policy.get(_AUDIT_FAIL_CLOSED_KEY):
        # The action says it handles something sensitive but no longer says what.
        # Keep the protocol facts and nothing else, whatever the outcome was.
        return _project_failed_invocation(redacted, keep_row_fields=frozenset())

    keep_row_fields = policy.get(_AUDIT_FAILURE_PROJECTION_KEY)
    if (
        keep_row_fields is not None
        and invocation
        and str((value or {}).get("status") or "").strip().lower() != "succeeded"
    ):
        return _project_failed_invocation(redacted, keep_row_fields=keep_row_fields)

    field_names = policy.get(_AUDIT_REDACT_FIELDS_KEY) or frozenset()
    if not field_names:
        return redacted
    return _redact_action_audit_fields(redacted, field_names=field_names)
