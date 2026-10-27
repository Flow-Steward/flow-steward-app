"""Tool manifest identities, parameter schemas, and MCP result contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from core.application.management.mcp_connector_protocol import (
    McpToolInvocationError,
    UnsupportedMcpResultTypeError,
)

_PARAMETER_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "text": str,
    "integer": int,
    "int": int,
    "number": float,
    "float": float,
    "boolean": bool,
    "bool": bool,
    "array": list[Any],
    "list": list[Any],
    "object": dict[str, Any],
    "json": dict[str, Any],
}


_MCP_TOOL_ERROR_FALLBACK = "MCP tool reported an invocation error"


_MCP_TOOL_ERROR_MAX_LENGTH = 2000


def _parameter_python_type(type_name: str) -> Any:
    normalized = str(type_name or "string").strip().lower()
    return _PARAMETER_TYPE_MAP.get(normalized, Any)


def _optional_parameter_type(annotation: Any) -> Any:
    if annotation is Any:
        return Any
    return annotation | None


def _manifest_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        return dict(payload) if isinstance(payload, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _manifest_owner_lineage(manifest) -> tuple[str, str, str]:
    implementation = getattr(manifest, "implementation", None)
    implementation_payload = _manifest_payload(implementation)
    config = getattr(manifest, "config", {}) or {}
    config_payload = config if isinstance(config, dict) else {}

    def _one(name: str) -> str:
        candidates = [
            implementation_payload.get(f"owner_{name}"),
            implementation_payload.get(f"owner_{name}_id"),
            getattr(implementation, f"owner_{name}", ""),
            getattr(implementation, f"owner_{name}_id", ""),
            config_payload.get(f"owner_{name}"),
            config_payload.get(f"owner_{name}_id"),
            getattr(manifest, f"{name}_id", "")
            if name != "tenant"
            else getattr(manifest, "tenant_id", ""),
        ]
        values: list[str] = []
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in values:
                values.append(value)
        if len(values) > 1:
            raise RuntimeError(f"MCP tool owner {name} metadata mismatch")
        return values[0] if values else ""

    return _one("account"), _one("project"), _one("tenant")


def _expected_mcp_connector_lineage(
    manifest,
    runtime_context: dict[str, Any] | None = None,
    *,
    require: bool,
) -> tuple[str, str, str]:
    context = runtime_context if isinstance(runtime_context, dict) else {}
    runtime = tuple(
        str(context.get(key) or "").strip() for key in ("account_id", "project_id", "tenant_id")
    )
    owner = _manifest_owner_lineage(manifest) if manifest is not None else ("", "", "")
    labels = ("account", "project", "tenant")
    for label, runtime_value, owner_value in zip(labels, runtime, owner, strict=True):
        if runtime_value and owner_value and runtime_value != owner_value:
            raise RuntimeError(
                f"MCP connector {label} mismatch: runtime value does not match tool owner"
            )
    expected = (
        runtime[0] or owner[0],
        runtime[1] or owner[1],
        runtime[2] or owner[2],
    )
    if require and any(not value for value in expected):
        raise RuntimeError("MCP connector runtime requires trusted account/project/tenant lineage")
    return expected


def _assert_mcp_connector_row_account(
    row: dict[str, Any],
    *,
    connector_id: str,
    expected_account_id: str,
) -> None:
    if not expected_account_id:
        return
    row_account_id = str(row.get("account_id") or "").strip()
    if row_account_id != expected_account_id:
        raise RuntimeError(f"MCP connector account mismatch for connector '{connector_id}'")


def _assert_mcp_connector_row_lineage(
    row: dict[str, Any],
    *,
    connector_id: str,
    expected_lineage: tuple[str, str, str],
) -> None:
    expected_account_id, expected_project_id, expected_tenant_id = expected_lineage
    _assert_mcp_connector_row_account(
        row,
        connector_id=connector_id,
        expected_account_id=expected_account_id,
    )
    if (
        str(row.get("project_id") or "").strip() != expected_project_id
        or str(row.get("tenant_id") or "").strip() != expected_tenant_id
    ):
        raise RuntimeError(f"MCP connector project mismatch for connector '{connector_id}'")


def _manifest_remote_mcp_tool_name(manifest) -> str:
    implementation = getattr(manifest, "implementation", None)
    implementation_payload = _manifest_payload(implementation)
    config = getattr(manifest, "config", {}) or {}
    config_payload = config if isinstance(config, dict) else {}
    return str(
        implementation_payload.get("remote_tool_name")
        or config_payload.get("remote_tool_name")
        or ""
    ).strip()


def _manifest_requires_runtime_context(manifest) -> bool:
    implementation = getattr(manifest, "implementation", None)
    implementation_payload = _manifest_payload(implementation)
    implementation_type = str(
        implementation_payload.get("type")
        or implementation_payload.get("kind")
        or getattr(implementation, "type", "")
        or getattr(implementation, "kind", "")
    ).strip()
    return implementation_type == "mcp" and bool(_manifest_remote_mcp_tool_name(manifest))


_PROVIDER_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_PROVIDER_TOOL_NAME = 64
_COLLIDES_WITH_DERIVED_ALIAS = re.compile(r"^.+_[0-9a-f]{8}$")


def provider_tool_alias(canonical_name: str) -> str:
    """Return the name a model provider will accept for ``canonical_name``.

    A projected MCP tool is identified by its dotted id — `<namespace>.<tool>` —
    which is the right identity for an allowlist and an audit trail and an
    impossible name for a provider function. Give the provider a sanitised alias
    and keep the canonical id for everything that decides or records anything.

    The alias is derived, not stored, so it is stable across runs; a digest is
    appended whenever sanitising changed the string, so two different canonical
    ids cannot collapse onto one provider name.
    """
    name = str(canonical_name or "").strip()
    if not name:
        return ""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    if _PROVIDER_SAFE_TOOL_NAME.match(name) and len(name) <= _MAX_PROVIDER_TOOL_NAME:
        # An already-safe id is its own alias — but only while no derived alias
        # could equal it. A canonical `a_b_2e7336dc` would otherwise collide with
        # the alias derived from `a.b`, and two different tools would answer to
        # one provider name.
        if not _COLLIDES_WITH_DERIVED_ALIAS.match(name):
            return name
        return f"{name}_{digest}"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "tool"
    budget = _MAX_PROVIDER_TOOL_NAME - len(digest) - 1
    if len(sanitized) > budget:
        sanitized = sanitized[:budget].rstrip("_") or "tool"
    return f"{sanitized}_{digest}"


def _operation_description(manifest, function_name: str) -> str:
    display_name = str(
        getattr(manifest, "display_name", "") or getattr(manifest, "tool_id", "")
    ).strip()
    description = str(getattr(manifest, "description", "") or "").strip()
    if description:
        return f"{display_name}: {description}" if display_name else description
    return display_name or function_name


def _build_args_schema(manifest, operation: dict[str, Any]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in operation.get("parameters") or []:
        if hasattr(parameter, "model_dump"):
            raw = parameter.model_dump(mode="python")
        else:
            raw = dict(parameter or {})
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        annotation = _parameter_python_type(str(raw.get("type") or "string"))
        required = bool(raw.get("required", True))
        default = raw.get("default")
        if not required:
            annotation = _optional_parameter_type(annotation)
        fields[name] = (annotation, ... if required and default is None else default)

    model_name = (
        f"{str(getattr(manifest, 'tool_id', 'tool')).replace('.', '_')}_"
        f"{str(operation.get('name') or 'invoke').replace('.', '_')}_Args"
    )
    model = create_model(model_name, **fields)
    model.model_config = ConfigDict(extra="forbid")
    return model


def _normalize_mcp_tool_result(
    result: Any,
    *,
    runtime_context: dict[str, Any] | None = None,
) -> Any:
    if not isinstance(result, dict):
        return result
    if "isError" in result and not isinstance(result.get("isError"), bool):
        raise McpToolInvocationError("MCP tool returned an invalid error indicator")
    if result.get("isError") is True:
        content = result.get("content")
        if not isinstance(content, list):
            content = []

        def _text_chunks():
            for item in content[:64]:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "text":
                    continue
                raw_text = item.get("text")
                if isinstance(raw_text, str):
                    yield raw_text

        error_message = _MCP_TOOL_ERROR_FALLBACK
        from core.application.orchestration.input_sanitization import (
            sanitize_external_error_text,
        )

        detail_budget = _MCP_TOOL_ERROR_MAX_LENGTH - len(error_message) - 2
        safe_detail = sanitize_external_error_text(
            _text_chunks(),
            max_chars=detail_budget,
            state=runtime_context,
        )
        if safe_detail:
            error_message = f"{error_message}: {safe_detail}"
        raise McpToolInvocationError(error_message)
    if "resultType" in result and result.get("resultType") != "complete":
        raise UnsupportedMcpResultTypeError("MCP tool returned an unsupported result type")
    if "structuredContent" in result:
        return result["structuredContent"]

    content = result.get("content")
    if isinstance(content, list):
        if any(
            not isinstance(item, dict) or str(item.get("type") or "").strip().lower() != "text"
            for item in content
        ):
            return content
        text_chunks: list[str] = []
        for item in content:
            text = str(item.get("text") or "")
            if text:
                text_chunks.append(text)
        if len(text_chunks) == 1:
            candidate = text_chunks[0].strip()
            if not candidate:
                return ""
            try:
                return json.loads(candidate)
            except Exception:
                return text_chunks[0]
        if text_chunks:
            return "\n".join(text_chunks)
    return result


def _normalized_mcp_tool_allowlist(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    return {str(item or "").strip() for item in (value or []) if str(item or "").strip()}


def _mcp_tool_is_allowlisted(
    allowlisted_tools: Any,
    *,
    manifest,
    function_name: str,
) -> bool:
    allowlisted = _normalized_mcp_tool_allowlist(allowlisted_tools)
    if not allowlisted:
        return False
    # New rows store the projected manifest ID; bare function/remote names are
    # retained only for connector rows written before projected IDs were canonical.
    accepted_identities = {
        str(getattr(manifest, "tool_id", "") or "").strip(),
        str(function_name or "").strip(),
        _manifest_remote_mcp_tool_name(manifest),
    }
    accepted_identities.discard("")
    return not allowlisted.isdisjoint(accepted_identities)


def _json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False


def _tool_interface_is_implicit_invoke(manifest) -> bool:
    interface = getattr(manifest, "interface", None)
    if interface is None:
        return True
    if getattr(interface, "function_name", None):
        return False
    if list(getattr(interface, "functions", []) or []):
        return False
    if list(getattr(interface, "parameters", []) or []):
        return False
    return not dict(getattr(interface, "returns", {}) or {})
