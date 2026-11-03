"""The extension release a connector step's input goes to, fixed before any value is read.

A connector step used to choose its extension only when it dispatched, after its
secret inputs were resolved. A catalog secret may reach only code a person approved
(ADR 0055), so the step now selects its receiver first: the installed extension
record, captured to its immutable release directory (`InvocationReleaseSelection`).
The same captured record is what the step dispatches to, so an update that
publishes another release between the check and the call cannot change the code
that receives the value. Capturing an already captured record yields the same
directory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PinnedReceiver:
    install_id: str
    release_id: str
    descriptor_sha256: str
    extension_id: str
    #: The captured record, its bundle root resolved to the release directory. It
    #: is what the step dispatches; the path never leaves the host.
    runtime_extension: dict[str, Any]
    logger: Any = None

    def target(self, install_id: str) -> tuple[dict[str, Any], Any, str, str]:
        """(extension, logger, extension id, install id) to dispatch to, as captured."""
        return (
            self.runtime_extension,
            self.logger,
            self.extension_id,
            install_id or self.install_id,
        )

    def identity(self) -> dict[str, str]:
        """What an approval records and a read is compared with: the canonical release.

        Portable across hosts: a filesystem root differs between installations of
        one release and is not part of it.
        """
        return {
            "kind": "extension",
            "install_id": self.install_id,
            "release_id": self.release_id,
            "descriptor_sha256": self.descriptor_sha256,
        }


def _payload(value: object) -> dict[str, Any]:
    """A manifest part as a mapping, through its `to_dict` when it has one."""
    to_dict = getattr(value, "to_dict", None)
    return _mapping(to_dict() if callable(to_dict) else value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def step_target(step: Any) -> dict[str, str]:
    """The fields of a connector step that select its extension action."""
    payload = _mapping(step)
    connector = _mapping(_mapping(payload.get("override")).get("connector"))
    return {
        "connector_id": _text(payload.get("connector_id")),
        "extension_id": _text(connector.get("extension_id")),
        "connector_install_id": _text(payload.get("connector_install_id")),
        "action_id": _text(connector.get("action_id")),
        "operation_id": _text(connector.get("operation_id")),
    }


def pin_extension_receiver(
    *,
    connector_id: str,
    extension_id: str,
    connector_install_id: str,
    action_id: str,
    operation_id: str,
    account_id: str,
    get_logger_only: Callable[[], Any] | None,
) -> PinnedReceiver | None:
    """The captured extension this step would dispatch to; None when it names none."""
    from core.application.extensions.invocation_release import InvocationReleaseSelection
    from core.application.orchestration.connector_effects import (
        _derive_extension_action,
        _resolved_install_id,
    )
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record,
    )
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        extension_runtime_availability_failure,
    )

    ext_id, op_id = _derive_extension_action(
        connector_id=connector_id,
        extension_id=extension_id,
        action_id=action_id,
        operation_id=operation_id,
    )
    install_id = _text(connector_install_id)
    candidates = [install_id] if install_id else ([ext_id] if ext_id else [])
    if not op_id or not _text(account_id):
        return None
    for candidate in candidates:
        extension, bl = resolve_extension_runtime_record(
            candidate, get_logger_only=get_logger_only, account_id=_text(account_id)
        )
        if not extension:
            continue
        if extension_runtime_availability_failure(extension) is not None:
            return None
        try:
            selection = InvocationReleaseSelection.capture(extension)
        except (OSError, ValueError):
            return None
        resolved_install = install_id or _resolved_install_id(extension)
        release = _canonical_release(bl, resolved_install)
        # The install must still run the release that was captured; a publication
        # in between leaves nothing pinned, so no catalog secret is read.
        if not release or release["release_id"] != selection.release_id:
            return None
        return PinnedReceiver(
            install_id=resolved_install,
            release_id=release["release_id"],
            descriptor_sha256=release["descriptor_sha256"],
            extension_id=_text(extension.get("extension_key") or extension.get("extension_id"))
            or candidate,
            runtime_extension=selection.runtime_extension(),
            logger=bl,
        )
    return None


@dataclass(frozen=True)
class PinnedTool:
    """A tool manifest selected before any value is read, and what implements it.

    A built-in tool runs host code: the host is the receiver, named by the tool id.
    A tool implemented by an extension carries that extension's captured release.
    """

    tool_id: str
    manifest: Any
    extension: PinnedReceiver | None = None
    #: A remote MCP tool's connector row, read once with trusted lineage; the call
    #: uses this row and never reads it again.
    mcp_row: dict[str, Any] | None = None

    def identity(self) -> dict[str, str]:
        # The executable manifest is part of every tool receiver: the same tool id
        # with another implementation, module or config is another receiver.
        manifest = {"tool_id": self.tool_id, "manifest_sha256": manifest_digest(self.manifest)}
        if self.extension is not None:
            return {**self.extension.identity(), **manifest, "kind": "tool_extension"}
        if self.mcp_row is not None:
            return {
                "kind": "mcp",
                **manifest,
                "connector_id": _text(self.mcp_row.get("connector_id")),
                "config_sha256": mcp_config_digest(self.mcp_row, self.manifest),
            }
        return {"kind": "host_tool", **manifest}

    def describe(self) -> dict[str, str]:
        """The identity, plus what a person needs to see to approve it."""
        _kind, payload = _implementation(self.manifest)
        if self.mcp_row is None:
            return {**self.identity(), "implementation": _text(payload.get("module"))}
        return {
            **self.identity(),
            "endpoint": _text(self.mcp_row.get("base_url")),
            "transport": _text(self.mcp_row.get("transport")),
            "remote_tool": _remote_tool_name(self.manifest, payload),
        }


def _remote_tool_name(manifest: Any, payload: dict[str, Any]) -> str:
    """As the MCP call resolves it: the implementation first, then the manifest config."""
    return _text(
        payload.get("remote_tool_name")
        or _mapping(getattr(manifest, "config", None)).get("remote_tool_name")
    )


def manifest_digest(manifest: Any) -> str:
    """A digest of what a tool manifest executes: implementation, config, interface.

    Its display name and description are not executable and are left out.
    """
    import hashlib
    import json

    kind, payload = _implementation(manifest)
    interface = getattr(manifest, "interface", None)
    interface_payload = _payload(interface)
    executable = {
        "kind": kind,
        "implementation": payload,
        "config": _mapping(getattr(manifest, "config", None)),
        "function_name": interface_payload.get("function_name")
        or _text(getattr(interface, "function_name", "")),
        "operations": interface_payload.get("operations")
        or list(getattr(interface, "operations", []) or []),
    }
    encoded = json.dumps(executable, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


#: The parts of an MCP connector that decide where a call goes and as whom.
_MCP_CONFIG_FIELDS = (
    "connector_id",
    "account_id",
    "project_id",
    "tenant_id",
    "transport",
    "base_url",
    "auth_mode",
    "oauth_credential_revision",
    "allowlisted_tools",
    "enabled",
)


def mcp_config_digest(row: dict[str, Any], manifest: Any) -> str:
    """A digest of the connector's endpoint, transport, auth and allowlist; no values."""
    import hashlib
    import json

    _kind, payload = _implementation(manifest)
    config = {field: row.get(field) for field in _MCP_CONFIG_FIELDS}
    # The whole reference map: a new header value or identity writes a new
    # generation there. References only; nothing is decrypted.
    config["header_secret_refs"] = row.get("header_secret_refs_jsonb") or {}
    config["remote_tool_name"] = _remote_tool_name(manifest, payload)
    encoded = json.dumps(config, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _implementation(manifest: Any) -> tuple[str, dict[str, Any]]:
    implementation = getattr(manifest, "implementation", None)
    payload = (
        _mapping(implementation.to_dict())
        if implementation is not None and hasattr(implementation, "to_dict")
        else _mapping(implementation)
    )
    kind = _text(
        getattr(implementation, "type", "")
        or getattr(implementation, "kind", "")
        or payload.get("type")
        or payload.get("kind")
    ).lower()
    return kind, payload


def pin_tool_receiver(
    *,
    connector_id: str,
    connector_install_id: str,
    lineage: dict[str, str],
    get_logger_only: Callable[[], Any] | None,
) -> PinnedTool | None:
    """The tool a connector step dispatches to, selected now; None when there is none.

    `lineage` is the trusted account, project and tenant of the run (or of the
    project an approval is made for): a remote MCP connector is project-scoped.
    """
    from core.application.orchestration.connector_tool_runtime import _tool_id_candidates
    from core.application.registry.registry_manager import RegistryManager

    try:
        tools = getattr(RegistryManager.instance(), "tools", None)
    except Exception:
        return None
    get_tool = getattr(tools, "get_tool", None)
    for tool_id in _tool_id_candidates(connector_id) if callable(get_tool) else []:
        manifest = get_tool(tool_id)
        if manifest is None or getattr(manifest, "enabled", True) is False:
            continue
        kind, payload = _implementation(manifest)
        if kind == "mcp":
            row = _mcp_row(manifest, payload, lineage)
            return PinnedTool(tool_id=tool_id, manifest=manifest, mcp_row=row) if row else None
        if kind != "extension":
            return PinnedTool(tool_id=tool_id, manifest=manifest)
        action = _text(payload.get("action")) or "invoke_tool"
        extension = pin_extension_receiver(
            connector_id=connector_id,
            extension_id=_text(payload.get("extension_key") or payload.get("extension_id")),
            connector_install_id=connector_install_id,
            action_id=action,
            operation_id=action,
            account_id=lineage.get("account_id", ""),
            get_logger_only=get_logger_only,
        )
        return (
            PinnedTool(tool_id=tool_id, manifest=manifest, extension=extension)
            if extension
            else None
        )
    return None


def _mcp_row(manifest: Any, payload: dict[str, Any], lineage: dict[str, str]) -> dict | None:
    """The MCP connector row the call will use, read with the run's trusted lineage."""
    from core.application.orchestration.tools.tool_resolver import _load_mcp_connector_row

    connector_id = _text(payload.get("connector_id"))
    if not connector_id or not all(
        lineage.get(k) for k in ("account_id", "project_id", "tenant_id")
    ):
        return None
    try:
        row = _load_mcp_connector_row(
            connector_id, manifest=manifest, runtime_context=dict(lineage)
        )
    except RuntimeError:
        return None
    return dict(row) if row and bool(row.get("enabled", True)) else None


def pin_step_receiver(
    *, lineage: dict[str, str], get_logger_only: Callable[[], Any] | None, **target: str
) -> PinnedReceiver | PinnedTool | None:
    """Whatever a connector step's input goes to, selected before any value is read."""
    from core.application.orchestration.connector_mapping import _TOKEN_EXTENSION

    if target["connector_id"].startswith(_TOKEN_EXTENSION) or target["extension_id"]:
        return pin_extension_receiver(
            **target, account_id=lineage.get("account_id", ""), get_logger_only=get_logger_only
        )
    return pin_tool_receiver(
        connector_id=target["connector_id"],
        connector_install_id=target["connector_install_id"],
        lineage=lineage,
        get_logger_only=get_logger_only,
    )


def _canonical_release(bl: Any, install_id: str) -> dict[str, str] | None:
    """The install's current release row: its id and descriptor digest."""
    from core.application.secrets.repository import install_release

    conn = getattr(bl, "conn", None)
    if conn is None or not _text(install_id):
        return None
    with conn.cursor() as cur:
        release = install_release(cur, install_id=install_id)
    if not _text(release.get("release_id")):
        return None
    return {
        "release_id": _text(release.get("release_id")),
        "descriptor_sha256": _text(release.get("descriptor_sha256")),
    }


def definition_receivers(definition: Any, *, lineage: dict[str, str]) -> list[dict[str, str]]:
    """The receiver of every connector step of a definition, for one project.

    Each entry names the project it holds for: an approval records the set, and
    a read must match the entry of its own job's project. A step whose receiver
    cannot be selected is listed as unpinned; no catalog secret reaches it.
    """
    from core.application.orchestration.connector_events import _shared_logger
    from core.application.secrets.workflow_gate import connector_steps

    receivers: list[dict[str, str]] = []
    for step in connector_steps(definition):
        target = step_target(step)
        pinned = pin_step_receiver(**target, lineage=lineage, get_logger_only=_shared_logger)
        entry = (
            (pinned.describe() if isinstance(pinned, PinnedTool) else pinned.identity())
            if pinned is not None
            else {"kind": "unpinned", "connector_id": target["connector_id"]}
        )
        entry = {
            "project_id": _text(lineage.get("project_id")),
            "step_id": _text(_mapping(step).get("step_id")),
            **entry,
        }
        if entry not in receivers:
            receivers.append(entry)
    return sorted(receivers, key=lambda row: (row["step_id"], row.get("install_id", "")))


__all__ = [
    "PinnedReceiver",
    "PinnedTool",
    "definition_receivers",
    "manifest_digest",
    "mcp_config_digest",
    "pin_extension_receiver",
    "pin_step_receiver",
    "pin_tool_receiver",
    "step_target",
]
