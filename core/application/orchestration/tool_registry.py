"""Runtime tools supplied by enabled project ToolProvider bindings."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _resolve_tool_provider_extension(binding: dict) -> tuple[dict | None, str]:
    provider_key = str(binding.get("provider_key") or "").strip()
    implementation_kind = str(binding.get("implementation_kind") or "builtin").strip().lower()
    if implementation_kind != "extension":
        return (
            None,
            f"tool provider '{provider_key}' implementation_kind '{implementation_kind}' is unsupported",
        )

    provider_config = _as_dict(binding.get("provider_config"))
    extension_key = (
        str(provider_config.get("extension_key") or "").strip()
        or str(binding.get("integration_key") or "").strip()
        or provider_key
    )
    if not extension_key:
        return None, f"tool provider '{provider_key}' is missing extension_key"

    try:
        from core.infrastructure.config import CONFIG
        from core.infrastructure.db.platform_stores import (
            PlatformIdentityStore,
            resolve_observability_dsn,
        )
    except Exception as exc:
        return None, f"tool provider '{provider_key}' runtime unavailable: {exc}"

    dsn = resolve_observability_dsn(
        CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""
    )
    if not dsn:
        return None, f"tool provider '{provider_key}' cannot resolve observability DSN"
    try:
        bl = PlatformIdentityStore(dsn)
        extension = bl.get_extension(extension_key) if hasattr(bl, "get_extension") else None
    except Exception as exc:
        return None, f"tool provider '{provider_key}' extension lookup failed: {exc}"
    if not extension:
        return None, f"tool provider '{provider_key}' extension '{extension_key}' not found"
    if not bool(extension.get("enabled")):
        return None, f"tool provider '{provider_key}' extension '{extension_key}' is disabled"
    if str(extension.get("kind") or "").strip() != "tool_provider":
        return None, (
            f"tool provider '{provider_key}' extension '{extension_key}' kind "
            f"'{extension.get('kind')}' does not match tool_provider"
        )
    return extension, ""


def _build_tool_provider_manifest(binding: dict, spec: dict):
    from core.application.registry.tool_manifest import ToolManifest

    provider_key = str(binding.get("provider_key") or "").strip()
    tool_name = str(spec.get("name") or "").strip()
    description = (
        str(spec.get("description") or "").strip()
        or f"Invoke '{tool_name}' from provider '{provider_key}'"
    )
    extension_tool_name = str(spec.get("tool_name") or "").strip() or tool_name
    timeout_seconds = int(spec.get("timeout_seconds") or 30)
    static_arguments = _as_dict(spec.get("static_arguments"))
    provider_config = _as_dict(binding.get("provider_config"))
    extension_key = (
        str(provider_config.get("extension_key") or "").strip()
        or str(binding.get("integration_key") or "").strip()
        or provider_key
    )
    return ToolManifest.model_validate(
        {
            "tool_id": f"provider.{provider_key}.{tool_name}",
            "display_name": tool_name,
            "description": description,
            "enabled": True,
            "interface": {
                "function_name": tool_name,
                "parameters": [
                    {
                        "name": "arguments_json",
                        "type": "string",
                        "required": False,
                        "description": "JSON object text with tool arguments.",
                    }
                ],
                "returns": {"name": "result", "type": "string", "required": False},
            },
            "implementation": {
                "kind": "extension",
                "extension_key": extension_key,
                "tool_name": extension_tool_name,
                "action": "invoke_tool",
                "timeout_seconds": timeout_seconds,
                "provider_key": provider_key,
            },
            "config": {"static_arguments": static_arguments},
            "tags": ["provider", "extension"],
        }
    )


def build_project_provider_tools(project_config: dict) -> list:
    from core.application.orchestration.tools.tool_resolver import (
        ToolResolver,
        build_manifest_dispatch_tools,
    )

    bindings = _as_list(project_config.get("project_providers"))
    if not bindings:
        return []

    tools = []
    seen_names: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        if str(binding.get("provider_type_key") or "").strip().lower() != "tool_provider":
            continue
        if not bool(binding.get("enabled", True)) or not bool(
            binding.get("provider_enabled", True)
        ):
            continue

        binding_config = _as_dict(binding.get("binding_config"))
        provider_config = _as_dict(binding.get("provider_config"))
        specs = _as_list(binding_config.get("tools")) or _as_list(provider_config.get("tools"))
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            if name in seen_names:
                logger.warning("tool_registry: duplicate ToolProvider tool name '%s' skipped", name)
                continue
            try:
                manifest = _build_tool_provider_manifest(binding, spec)
                resolver = ToolResolver()
                resolver.register_from_manifest(manifest)
                resolved_tools = resolver.resolve(
                    [manifest.tool_id]
                ) or build_manifest_dispatch_tools(manifest)
                if not resolved_tools:
                    raise RuntimeError("provider tool did not resolve to a runtime callable")
                tool_fn = resolved_tools[0]
            except Exception as exc:
                logger.warning(
                    "tool_registry: failed to build ToolProvider tool '%s' for provider '%s': %s",
                    name,
                    str(binding.get("provider_key") or ""),
                    exc,
                )
                continue
            tools.append(tool_fn)
            seen_names.add(name)
            logger.debug(
                "tool_registry: registered ToolProvider tool '%s' (provider=%s)",
                name,
                str(binding.get("provider_key") or ""),
            )
    return tools
