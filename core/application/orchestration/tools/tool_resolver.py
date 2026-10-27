# mypy: ignore-errors
"""
ToolResolver — resolves typed tool manifests into runtime tool callables.

Callers request tools by typed identity and the resolver hides backend-specific
execution details behind one runtime interface.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any

from pydantic import BaseModel

from core.application.management.mcp_connector_common import (
    MCP_OUTPUT_PROJECTION_CONFIG_KEY,
    MCP_OUTPUT_PROJECTION_WHOLE_OBJECT_RESULT,
    _project_mcp_output_contract,
)
from core.application.management.mcp_connector_http import mcp_tool_transport_headers
from core.application.management.mcp_connector_protocol import McpToolInvocationError
from core.application.orchestration.agents.common_tool_runtime import (
    MCP_CANONICAL_TOOL_ID_METADATA_KEY,
    MCP_PROJECTED_TOOL_METADATA_KEY,
)
from core.application.orchestration.tools.tool_manifest_contracts import (
    _assert_mcp_connector_row_account,
    _assert_mcp_connector_row_lineage,
    _build_args_schema,
    _expected_mcp_connector_lineage,
    _json_values_equal,
    _manifest_requires_runtime_context,
    _mcp_tool_is_allowlisted,
    _normalize_mcp_tool_result,
    _operation_description,
    _tool_interface_is_implicit_invoke,
)
from core.application.orchestration.tools.tool_manifest_contracts import (
    provider_tool_alias as provider_tool_alias,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

try:
    from langchain_core.tools import BaseTool as LCBaseTool
    from langchain_core.tools import StructuredTool
except Exception:  # pragma: no cover - optional in lightweight test envs
    LCBaseTool = ()
    StructuredTool = None

logger = logging.getLogger(__name__)

_DEFAULT_MCP_SERVER = "fs"
_DEFAULT_MCP_CONNECTOR = "builtin_fs_mcp"
_BUILTIN_MCP_CONNECTORS: dict[str, dict[str, str]] = {
    _DEFAULT_MCP_CONNECTOR: {"server": _DEFAULT_MCP_SERVER},
}

_INTERNAL_TOOL_MODULE_FALLBACKS: dict[str, str] = {}


_OBSERVABILITY_DSN_ENV_NAMES = (
    "FS_OBSERVABILITY_DSN",
    "FS_DB_DSN",
    "DATABASE_URL",
    "POSTGRES_DSN",
)


class _FallbackTool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        invoke_fn,
        metadata: dict[str, Any] | None = None,
    ):
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self._invoke_fn = invoke_fn
        self.metadata = dict(metadata or {})

    def invoke(self, payload: dict | None = None):
        validated = self.args_schema.model_validate(payload or {})
        return self._invoke_fn(**validated.model_dump(exclude_unset=True, exclude_none=True))


def _observability_dsn_env_candidate() -> str:
    for name in _OBSERVABILITY_DSN_ENV_NAMES:
        value = _runtime_env.get(name, "").strip()
        if value:
            return value
    return ""


def _resolve_observability_dsn_with_preimport_env(
    resolve_observability_dsn,
    preimport_dsn: str,
) -> str:
    return (
        resolve_observability_dsn(preimport_dsn) if preimport_dsn else resolve_observability_dsn()
    )


#: Model providers accept a tool name of `^[a-zA-Z0-9_-]+$` and reject anything
#: else outright, before the agent takes a single action.
#: Providers bound a function name; keep aliases comfortably inside it.
#: A name that already looks like a derived alias — `<something>_<8 hex>` — must
#: not be handed through unchanged, or it can be the alias of a different id.


def _resolve_extension_record(
    manifest,
    *,
    runtime_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    implementation_payload = manifest.implementation.to_dict()
    extension_key = str(
        implementation_payload.get("extension_key")
        or implementation_payload.get("extension_id")
        or (getattr(manifest, "config", {}) or {}).get("extension_key")
        or ""
    ).strip()
    if not extension_key:
        raise RuntimeError(f"Tool '{manifest.tool_id}' is missing implementation.extension_key")

    preimport_dsn = _observability_dsn_env_candidate()
    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
    from core.infrastructure.db.platform_stores import PlatformIdentityStore
    from core.infrastructure.extension_runtime.extension_runtime import (
        normalize_extension_row_for_runtime,
    )

    normalized_runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    candidate_logger = normalized_runtime_context.get("logger")
    bl = candidate_logger if hasattr(candidate_logger, "get_extension") else None
    if bl is None:
        dsn = _resolve_observability_dsn_with_preimport_env(
            resolve_observability_dsn,
            preimport_dsn,
        )
        if not dsn:
            raise RuntimeError(f"Tool '{manifest.tool_id}' cannot resolve observability DSN")
        bl = PlatformIdentityStore.get_shared_instance(dsn)
    # FS-DEEP-CAND-060: resolve the tool-provider extension within the caller's
    # account scope. Without account_id the store returns the first matching
    # extension_key across all accounts, letting a scoped caller execute another
    # account's extension. Pass the caller account so cross-account keys are
    # invisible (get_extension returns None -> "not found").
    caller_account_id = str(normalized_runtime_context.get("account_id") or "").strip()
    if hasattr(bl, "get_extension"):
        if caller_account_id:
            extension = bl.get_extension(extension_key, account_id=caller_account_id)
        else:
            extension = bl.get_extension(extension_key)
    else:
        extension = None
    if not extension:
        raise RuntimeError(f"Tool '{manifest.tool_id}' extension '{extension_key}' not found")
    normalized = normalize_extension_row_for_runtime(extension)
    if not bool(normalized.get("enabled")):
        raise RuntimeError(f"Tool '{manifest.tool_id}' extension '{extension_key}' is disabled")
    return normalized, bl


def _invoke_tool_callable(tool_callable: Any, arguments: dict[str, Any]) -> Any:
    if hasattr(tool_callable, "invoke"):
        result = tool_callable.invoke(arguments)
    else:
        result = tool_callable(**arguments)
    if asyncio.iscoroutine(result):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(result)
        raise RuntimeError("Async tool execution requires an async runtime")
    return result


def _resolve_class_tools(module_path: str, class_name: str, config: dict[str, Any]) -> list[Any]:
    from core.application.orchestration.tools.base import BaseTool as ProjectBaseTool

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    if not issubclass(cls, ProjectBaseTool):
        raise TypeError(f"{class_name} does not extend tools.base.BaseTool")
    instance = cls(config=config)
    return list(instance.get_tools() or [])


def _resolve_module_tools(module_path: str, manifest) -> list[Any]:
    mod = importlib.import_module(module_path)
    resolved: list[Any] = []
    function_names = list(getattr(manifest.interface, "all_function_names", []) or [])
    if function_names:
        for function_name in function_names:
            tool_callable = getattr(mod, function_name, None)
            if tool_callable and ToolResolver._is_langchain_tool(tool_callable):
                resolved.append(tool_callable)
    if resolved:
        return resolved
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        attr = getattr(mod, attr_name)
        if ToolResolver._is_langchain_tool(attr):
            resolved.append(attr)
    return resolved


def _resolve_mcp_tools(manifest) -> list[Any]:
    import core.application.orchestration.tools.mcp_tools as mcp_mod

    # All MCP tools must resolve through an explicit connector identity.
    impl_dict = (
        manifest.implementation.to_dict() if hasattr(manifest.implementation, "to_dict") else {}
    )
    connector_id = str(
        impl_dict.get("connector_id") or getattr(manifest.implementation, "connector_id", "") or ""
    ).strip()
    if not connector_id:
        logger.warning(
            "MCP tool '%s' has no connector_id; refusing runtime resolution",
            getattr(manifest, "tool_id", "<unknown>"),
        )
        return []

    try:
        _validate_mcp_connector_tool_access(connector_id, manifest)
    except Exception as exc:
        logger.warning(
            "MCP connector validation failed for tool '%s' connector '%s': %s",
            manifest.tool_id,
            connector_id,
            exc,
        )
        return []  # refuse to resolve tools from disabled/invalid connectors

    resolved: list[Any] = []
    for function_name in getattr(manifest.interface, "all_function_names", []) or []:
        tool_callable = getattr(mcp_mod, function_name, None)
        if tool_callable and ToolResolver._is_langchain_tool(tool_callable):
            resolved.append(tool_callable)
    return resolved


def _load_mcp_connector_row(
    connector_id: str,
    *,
    manifest=None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    trusted_lineage = tuple(
        str(normalized_runtime_context.get(key) or "").strip()
        for key in ("account_id", "project_id", "tenant_id")
    )
    if any(not value for value in trusted_lineage):
        raise RuntimeError("MCP connector runtime requires trusted account/project/tenant lineage")
    expected_account_id, expected_project_id, expected_tenant_id = _expected_mcp_connector_lineage(
        manifest,
        normalized_runtime_context,
        require=True,
    )
    candidate_logger = normalized_runtime_context.get("logger")
    bl = candidate_logger if hasattr(candidate_logger, "cursor") else None
    if bl is None:
        preimport_dsn = _observability_dsn_env_candidate()
        from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        dsn = _resolve_observability_dsn_with_preimport_env(
            resolve_observability_dsn,
            preimport_dsn,
        )
        if not dsn:
            raise RuntimeError("MCP connector runtime DB DSN is not configured")
        bl = PlatformIdentityStore.get_shared_instance(dsn)

    from core.application.management.mcp_connector_services import McpConnectorStore

    row = McpConnectorStore().get_connector_row(
        bl,
        connector_id,
        account_id=expected_account_id,
        project_id=expected_project_id,
        tenant_id=expected_tenant_id,
    )
    if not row:
        raise RuntimeError(f"MCP connector '{connector_id}' not found")
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
    return row


def _invoke_remote_mcp_tool(
    manifest,
    *,
    function_name: str,
    arguments: dict[str, Any],
    runtime_context: dict[str, Any] | None = None,
) -> Any:
    implementation_payload = (
        manifest.implementation.to_dict() if hasattr(manifest.implementation, "to_dict") else {}
    )
    connector_id = str(
        implementation_payload.get("connector_id")
        or getattr(manifest.implementation, "connector_id", "")
        or ""
    ).strip()
    remote_tool_name = str(
        implementation_payload.get("remote_tool_name")
        or (getattr(manifest, "config", {}) or {}).get("remote_tool_name")
        or ""
    )
    if not connector_id:
        raise RuntimeError(f"Tool '{manifest.tool_id}' is missing implementation.connector_id")
    if not remote_tool_name.strip():
        raise RuntimeError(f"Tool '{manifest.tool_id}' is missing implementation.remote_tool_name")

    connector_row = _load_mcp_connector_row(
        connector_id,
        manifest=manifest,
        runtime_context=runtime_context,
    )
    if not bool(connector_row.get("enabled", True)):
        raise RuntimeError(f"MCP connector '{connector_id}' is disabled")

    if not _mcp_tool_is_allowlisted(
        connector_row.get("allowlisted_tools"),
        manifest=manifest,
        function_name=function_name,
    ):
        raise RuntimeError(
            f"Tool function '{function_name}' is not allowlisted for connector '{connector_id}'"
        )

    request_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": remote_tool_name,
            "arguments": dict(arguments or {}),
        },
    }
    try:
        from core.application.auth.mcp_connector_oauth import mcp_connector_rpc_post

        normalized_runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
        candidate_logger = normalized_runtime_context.get("logger")
        bl = candidate_logger if hasattr(candidate_logger, "cursor") else None
        if bl is None:
            preimport_dsn = _observability_dsn_env_candidate()
            from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
            from core.infrastructure.db.platform_stores import PlatformIdentityStore

            dsn = _resolve_observability_dsn_with_preimport_env(
                resolve_observability_dsn,
                preimport_dsn,
            )
            if not dsn:
                raise RuntimeError("MCP connector runtime DB DSN is not configured")
            bl = PlatformIdentityStore.get_shared_instance(dsn)
        trusted_input_schema: dict[str, Any] = {}
        trusted_output_schema: dict[str, Any] = {}
        discovered_tools = connector_row.get("discovered_tools_jsonb")
        if isinstance(discovered_tools, list):
            for discovered_tool in discovered_tools:
                if not isinstance(discovered_tool, dict):
                    continue
                discovered_name = str(
                    discovered_tool.get("remote_tool_name") or discovered_tool.get("name") or ""
                )
                if discovered_name != remote_tool_name:
                    continue
                candidate_schema = discovered_tool.get("input_schema")
                if not isinstance(candidate_schema, dict):
                    candidate_schema = discovered_tool.get("inputSchema")
                if isinstance(candidate_schema, dict):
                    trusted_input_schema = candidate_schema
                candidate_output_schema = discovered_tool.get("output_schema")
                if not isinstance(candidate_output_schema, dict):
                    candidate_output_schema = discovered_tool.get("outputSchema")
                if isinstance(candidate_output_schema, dict):
                    trusted_output_schema = candidate_output_schema
                break
        generated_transport_headers = mcp_tool_transport_headers(
            trusted_input_schema,
            dict(arguments or {}),
        )
        rpc_kwargs = {
            "connector_row": connector_row,
            "payload": request_payload,
            "timeout_seconds": 60.0,
            "runtime_context": normalized_runtime_context,
        }
        if generated_transport_headers:
            rpc_kwargs["generated_transport_headers"] = generated_transport_headers
        payload = mcp_connector_rpc_post(bl, **rpc_kwargs)
    except Exception as exc:
        logger.exception("MCP projected tool call failed for connector %s", connector_id)
        raise RuntimeError("MCP tool call failed; verify the connector and retry") from exc

    raw_result = payload.get("result")
    normalized_result = _normalize_mcp_tool_result(
        raw_result,
        runtime_context=normalized_runtime_context,
    )
    config = getattr(manifest, "config", {})
    config = config if isinstance(config, dict) else {}
    configured_output_schema = config.get("output_schema")
    configured_output_schema = (
        configured_output_schema if isinstance(configured_output_schema, dict) else {}
    )
    _projected_schema, derived_projection = _project_mcp_output_contract(trusted_output_schema)
    stored_projection = str(config.get(MCP_OUTPUT_PROJECTION_CONFIG_KEY) or "").strip()
    if (
        stored_projection == MCP_OUTPUT_PROJECTION_WHOLE_OBJECT_RESULT
        and stored_projection == derived_projection
        and _json_values_equal(configured_output_schema, trusted_output_schema)
    ):
        if not isinstance(raw_result, dict) or not isinstance(
            raw_result.get("structuredContent"), dict
        ):
            raise McpToolInvocationError("MCP tool did not return its structured object output")
        return {"result": normalized_result}
    return normalized_result


def _validate_mcp_connector_tool_access(connector_id: str, manifest) -> None:
    """Validate that an MCP connector allows this tool.

    Checks:
    1. Connector exists
    2. Connector is enabled
    3. The canonical projected tool ID (or a supported legacy identity) is in the
       connector's allowlisted_tools when the allowlist is non-empty
    """
    builtin = _BUILTIN_MCP_CONNECTORS.get(connector_id)
    if builtin is not None:
        expected_server = str(builtin.get("server") or "").strip()
        actual_server = str(getattr(manifest.implementation, "server", "") or "").strip()
        if actual_server != expected_server:
            raise RuntimeError(
                f"MCP tool server '{actual_server}' does not match builtin connector '{connector_id}'"
            )
        return

    # Lightweight validation — read connector row directly.
    # Fail-open vs fail-closed behaviour when the DB is unavailable:
    #   - Default (dev/test): fail-open — allow the tool through so local dev works without a
    #     fully-wired DB.  Controlled by FS_MCP_ALLOWLIST_FAIL_CLOSED=false (default).
    #   - Production: set FS_MCP_ALLOWLIST_FAIL_CLOSED=true to fail closed — the resolver
    #     will raise RuntimeError when the DB is unreachable rather than silently allowing the
    #     call through.  This prevents allowlist bypass on DB-outage in hardened deployments.
    _fail_closed = str(
        _runtime_env.get("FS_MCP_ALLOWLIST_FAIL_CLOSED", "false")
    ).strip().lower() in (
        "1",
        "true",
        "yes",
    )
    expected_lineage = _expected_mcp_connector_lineage(manifest, None, require=True)
    expected_account_id, expected_project_id, expected_tenant_id = expected_lineage
    try:
        preimport_dsn = _observability_dsn_env_candidate()
        from core.application.management.mcp_connector_services import McpConnectorStore
        from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        dsn = _resolve_observability_dsn_with_preimport_env(
            resolve_observability_dsn,
            preimport_dsn,
        )
        if not dsn:
            if _fail_closed:
                raise RuntimeError(
                    f"MCP allowlist validation failed for connector '{connector_id}': "
                    "no DB DSN configured and FS_MCP_ALLOWLIST_FAIL_CLOSED=true"
                )
            return  # no DB available — skip validation (fail-open, dev/test mode)
        bl = PlatformIdentityStore.get_shared_instance(dsn)
        row = McpConnectorStore().get_connector_row(
            bl,
            connector_id,
            account_id=expected_account_id,
            project_id=expected_project_id,
            tenant_id=expected_tenant_id,
        )
    except RuntimeError:
        raise  # propagate fail-closed errors
    except Exception as _exc:
        if _fail_closed:
            raise RuntimeError(
                f"MCP allowlist validation for connector '{connector_id}' failed (DB unavailable): {_exc}"
            ) from _exc
        return  # DB unavailable — allow tool through (fail-open, dev/test mode)

    if not row:
        raise RuntimeError(f"MCP connector '{connector_id}' not found")
    _assert_mcp_connector_row_lineage(
        row,
        connector_id=connector_id,
        expected_lineage=expected_lineage,
    )
    if not row.get("enabled", True):
        raise RuntimeError(f"MCP connector '{connector_id}' is disabled")

    function_names = list(getattr(manifest.interface, "all_function_names", []) or [])
    for fn in function_names:
        if not _mcp_tool_is_allowlisted(
            row.get("allowlisted_tools"),
            manifest=manifest,
            function_name=fn,
        ):
            raise RuntimeError(f"Tool function '{fn}' not in connector '{connector_id}' allowlist")


def resolve_backend_callables(manifest) -> list[Any]:
    implementation = manifest.implementation
    if implementation.type == "native":
        return []
    if implementation.class_name:
        return _resolve_class_tools(
            implementation.module or "", implementation.class_name, manifest.config
        )
    if implementation.type == "internal":
        module_path = str(
            implementation.module or ""
        ).strip() or _INTERNAL_TOOL_MODULE_FALLBACKS.get(
            str(getattr(manifest, "tool_id", "") or "").strip()
        )
        if module_path:
            return _resolve_module_tools(module_path, manifest)
    if implementation.type == "mcp":
        return _resolve_mcp_tools(manifest)
    if implementation.type == "extension":
        return []
    return []


def invoke_manifest_tool(
    manifest,
    *,
    function_name: str,
    arguments: dict[str, Any],
    runtime_context: dict[str, Any] | None = None,
) -> Any:
    implementation = manifest.implementation
    if implementation.type == "extension":
        normalized_runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
        extension, bl = _resolve_extension_record(
            manifest,
            runtime_context=normalized_runtime_context,
        )
        implementation_payload = implementation.to_dict()
        action = str(implementation_payload.get("action") or "invoke_tool").strip() or "invoke_tool"
        extension_tool_name = (
            str(implementation_payload.get("tool_name") or function_name).strip() or function_name
        )
        timeout_seconds = int(
            implementation_payload.get("timeout_seconds")
            or (manifest.config or {}).get("timeout_seconds")
            or 30
        )
        manifest_config = getattr(manifest, "config", {}) or {}
        runtime_arguments = dict(manifest_config.get("static_arguments") or {})
        supplied_arguments = dict(arguments or {})
        raw_arguments_json = str(supplied_arguments.pop("arguments_json", "") or "").strip()
        if raw_arguments_json:
            try:
                parsed = json.loads(raw_arguments_json)
                if isinstance(parsed, dict):
                    runtime_arguments.update(parsed)
                else:
                    runtime_arguments["value"] = parsed
            except Exception:
                runtime_arguments["text"] = raw_arguments_json
        runtime_arguments.update(supplied_arguments)
        request_payload = dict(implementation_payload.get("request_payload") or {})
        provider_key = str(implementation_payload.get("provider_key") or "").strip()
        if action == "invoke_tool":
            request_payload.update(
                {
                    "action": "invoke_tool",
                    "tool_name": extension_tool_name,
                    "arguments": runtime_arguments,
                }
            )
        else:
            request_payload.setdefault("action", action)
            request_payload.setdefault("arguments", runtime_arguments)
        if provider_key:
            request_payload.setdefault("provider", {"provider_key": provider_key})

        from core.infrastructure.extension_runtime.extension_invocation_audit import (
            log_host_extension_invocation,
        )
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

        invocation = run_extension_invocation(
            extension,
            request_payload=request_payload,
            timeout_seconds=timeout_seconds,
            runtime_context=runtime_context,
        )
        logged_invocation = dict(invocation) if isinstance(invocation, dict) else {}
        response = logged_invocation.get("response")
        if isinstance(response, dict) and response and response.get("ok") is False:
            logged_invocation["status"] = "failed"
            logged_invocation["detail"] = str(
                response.get("error")
                or logged_invocation.get("detail")
                or "Extension returned ok=false"
            ).strip()
        log_host_extension_invocation(
            bl=bl,
            extension=extension,
            extension_id=str(
                implementation_payload.get("extension_key")
                or implementation_payload.get("extension_id")
                or ""
            ).strip(),
            invocation_type=f"tool:{manifest.tool_id}:{function_name}",
            request=request_payload,
            invocation=logged_invocation,
            runtime_context=normalized_runtime_context,
            request_context={
                "tool_id": str(getattr(manifest, "tool_id", "") or "").strip(),
                "function_name": function_name,
                **(
                    {}
                    if str(normalized_runtime_context.get("source") or "").strip()
                    else {"source": "tool_resolver"}
                ),
            },
        )
        if invocation.get("status") != "succeeded":
            raise RuntimeError(
                str(invocation.get("detail") or "Extension invocation failed").strip()
            )

        # Host contract v1.1 — process commands/events from extension response
        response = invocation.get("response")
        if isinstance(response, dict) and response:
            try:
                from core.infrastructure.db.extension_response import (
                    filter_valid_commands,
                    filter_valid_events,
                    normalize_extension_response,
                )
                from core.infrastructure.extension_runtime.extension_runtime import (
                    emit_extension_event,
                )

                env = normalize_extension_response(response)
                compiled = extension.get("compiled_manifest") or {}
                supported_events = list(
                    compiled.get("supported_events") or extension.get("supported_events") or []
                )
                for cmd in filter_valid_commands(list(env.commands or [])):
                    logger.info("Tool extension returned command: %s", cmd.get("command_type"))
                for evt in filter_valid_events(
                    list(env.events or []), declared_event_types=supported_events
                ):
                    try:
                        emit_extension_event(
                            extension,
                            event_type=str(evt.get("event_type") or "").strip(),
                            correlation_id=str(evt.get("correlation_id") or "").strip(),
                            summary=str(evt.get("summary") or "").strip(),
                            severity=str(evt.get("severity") or "info").strip(),
                            payload=dict(evt.get("payload") or {}),
                        )
                    except Exception as exc:
                        logger.warning("Tool extension event emission failed: %s", exc)
            except Exception as exc:
                logger.debug("Extension response normalization skipped: %s", exc)

        if isinstance(response, dict):
            if not bool(response.get("ok", True)):
                raise RuntimeError(
                    str(response.get("error") or "Extension returned ok=false").strip()
                )
            if "result" in response:
                return response.get("result")
            if "value" in response:
                return response.get("value")
        return response

    if implementation.type == "mcp":
        implementation_payload = (
            implementation.to_dict() if hasattr(implementation, "to_dict") else {}
        )
        remote_tool_name = str(
            implementation_payload.get("remote_tool_name")
            or (getattr(manifest, "config", {}) or {}).get("remote_tool_name")
            or ""
        ).strip()
        if remote_tool_name:
            return _invoke_remote_mcp_tool(
                manifest,
                function_name=function_name,
                arguments=arguments,
                runtime_context=runtime_context,
            )

    resolved = list(resolve_backend_callables(manifest) or [])
    for tool_callable in resolved:
        if str(getattr(tool_callable, "name", "") or "").strip() == function_name:
            return _invoke_tool_callable(tool_callable, dict(arguments or {}))
    raise RuntimeError(
        f"Tool '{manifest.tool_id}' function '{function_name}' is not runtime-resolvable"
    )


def build_manifest_dispatch_tools(
    manifest,
    *,
    runtime_context: dict[str, Any] | None = None,
) -> list[Any]:
    if _tool_interface_is_implicit_invoke(manifest):
        resolved = list(resolve_backend_callables(manifest) or [])
        if resolved:
            return resolved
    operations = list(getattr(manifest.interface, "operations", []) or [])
    if not operations:
        return []
    projected_mcp = _manifest_requires_runtime_context(manifest)
    resolved: list[Any] = []
    for operation in operations:
        function_name = str((operation or {}).get("name") or "").strip()
        if not function_name:
            continue
        args_schema = _build_args_schema(manifest, operation)
        description = _operation_description(manifest, function_name)
        tool_metadata = (
            {
                MCP_PROJECTED_TOOL_METADATA_KEY: True,
                MCP_CANONICAL_TOOL_ID_METADATA_KEY: function_name,
            }
            if projected_mcp
            else {}
        )

        optional_parameter_names = {
            str((parameter or {}).get("name") or "").strip()
            for parameter in operation.get("parameters") or []
            if not bool((parameter or {}).get("required", True))
        }

        def _runner(
            _function_name=function_name,
            _optional_names=optional_parameter_names,
            _runtime_context=runtime_context,
            **kwargs,
        ):
            try:
                arguments = {
                    key: value
                    for key, value in dict(kwargs or {}).items()
                    if not (key in _optional_names and value is None)
                }
                invocation_context = (
                    dict(_runtime_context) if isinstance(_runtime_context, dict) else None
                )
                result = invoke_manifest_tool(
                    manifest,
                    function_name=_function_name,
                    arguments=arguments,
                    runtime_context=invocation_context,
                )
                if isinstance(result, (dict, list)):
                    return json.dumps(result, ensure_ascii=True)
                return "" if result is None else str(result)
            except Exception as exc:
                return f"[ERROR] {exc}"

        # The provider is given an alias it can accept; the runner above still
        # calls, authorises and audits under the canonical id.
        provider_name = provider_tool_alias(function_name)
        if StructuredTool is not None:
            resolved.append(
                StructuredTool.from_function(
                    func=_runner,
                    name=provider_name,
                    description=description,
                    args_schema=args_schema,
                    metadata=tool_metadata,
                )
            )
        else:
            resolved.append(
                _FallbackTool(
                    name=provider_name,
                    description=description,
                    args_schema=args_schema,
                    invoke_fn=_runner,
                    metadata=tool_metadata,
                )
            )
    return resolved


class ToolResolver:
    """Resolves typed tool manifests to LangChain-compatible tool callables."""

    def __init__(self):
        self._cache: dict[str, list[Any]] = {}
        self._manifests: dict[str, Any] = {}
        self._errors: list[str] = []

    def register_from_manifest(self, manifest) -> None:
        tool_id = manifest.tool_id
        impl = manifest.implementation

        if not manifest.enabled:
            logger.debug("ToolResolver: skipping disabled tool '%s'", tool_id)
            return
        config = getattr(manifest, "config", {}) or {}
        if (
            isinstance(config, dict)
            and str(config.get("agent_exposure") or "").strip().lower() == "connector_only"
        ):
            logger.debug("ToolResolver: skipping connector-only tool '%s'", tool_id)
            return

        try:
            if impl.type == "native":
                logger.debug(
                    "ToolResolver: skipping native tool '%s' (injected at runtime)", tool_id
                )
                return

            if impl.type == "mcp" and _manifest_requires_runtime_context(manifest):
                # Projected connector tools dispatch through the connector's
                # persisted HTTP/OAuth runtime. Importing the local Flow Steward
                # MCP SDK here couples registration to an unrelated optional
                # client package and can make otherwise valid remote tools vanish.
                pass
            elif impl.type in {"internal", "mcp"} or impl.class_name:
                resolve_backend_callables(manifest)
            elif impl.type == "extension":
                implementation_payload = impl.to_dict()
                extension_key = str(
                    implementation_payload.get("extension_key")
                    or implementation_payload.get("extension_id")
                    or (manifest.config or {}).get("extension_key")
                    or ""
                ).strip()
                if not extension_key:
                    raise RuntimeError("extension tools must declare implementation.extension_key")
            else:
                logger.debug(
                    "ToolResolver: unsupported impl type '%s' for tool '%s'", impl.type, tool_id
                )
                return

            tools = build_manifest_dispatch_tools(manifest)
            if tools:
                self._cache[tool_id] = tools
                self._manifests[tool_id] = manifest
                logger.info(
                    "ToolResolver: registered '%s' → %d callable(s): %s",
                    tool_id,
                    len(tools),
                    [getattr(tool, "name", "?") for tool in tools],
                )
            else:
                self._errors.append(f"Tool '{tool_id}': resolved to 0 callables")
        except Exception as exc:
            self._errors.append(f"Tool '{tool_id}': {exc}")
            logger.exception("ToolResolver: failed to resolve '%s': %s", tool_id, exc)

    def _resolve_registered(
        self,
        tool_ids: list[str],
        *,
        runtime_context: dict[str, Any] | None = None,
    ) -> list[Any]:
        result: list[Any] = []
        seen_names: set[str] = set()
        for tool_id in tool_ids:
            manifest = self._manifests.get(tool_id)
            if isinstance(runtime_context, dict) and manifest is not None:
                callables = build_manifest_dispatch_tools(
                    manifest,
                    runtime_context=runtime_context,
                )
            elif manifest is not None and _manifest_requires_runtime_context(manifest):
                logger.warning(
                    "ToolResolver: refusing contextless resolve for MCP runtime tool '%s'",
                    tool_id,
                )
                continue
            else:
                callables = self._cache.get(tool_id, [])
            if not callables:
                logger.debug("ToolResolver: tool_id '%s' has no registered callables", tool_id)
                continue
            for tool_callable in callables:
                name = getattr(tool_callable, "name", str(tool_callable))
                if name not in seen_names:
                    seen_names.add(name)
                    result.append(tool_callable)
        return result

    def resolve(self, tool_ids: list[str]) -> list[Any]:
        return self._resolve_registered(tool_ids)

    def resolve_runtime(
        self,
        tool_ids: list[str],
        *,
        runtime_context: dict[str, Any],
    ) -> list[Any]:
        return self._resolve_registered(tool_ids, runtime_context=runtime_context)

    def resolve_catalog(
        self,
        catalog_entries: list[dict[str, Any]],
        *,
        runtime_context: dict[str, Any] | None = None,
    ) -> list[Any]:
        tool_ids = [
            str((entry or {}).get("tool_id") or "").strip()
            for entry in catalog_entries or []
            if str((entry or {}).get("tool_id") or "").strip()
        ]
        if isinstance(runtime_context, dict):
            return self._resolve_registered(tool_ids, runtime_context=runtime_context)
        return self.resolve(tool_ids)

    def get_errors(self) -> list[str]:
        return list(self._errors)

    def list_registered(self) -> dict[str, list[str]]:
        return {
            tool_id: [getattr(tool, "name", "?") for tool in tools]
            for tool_id, tools in self._cache.items()
        }

    @staticmethod
    def _is_langchain_tool(obj) -> bool:
        return (LCBaseTool and isinstance(obj, LCBaseTool)) or (
            callable(obj)
            and hasattr(obj, "name")
            and hasattr(obj, "description")
            and hasattr(obj, "args_schema")
        )
