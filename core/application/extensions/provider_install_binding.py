"""Resolve a provider runtime through its project-bound extension install."""

from __future__ import annotations

from typing import Any, Protocol


class InstallBoundProviderStore(Protocol):
    """Persistence reads required to resolve one project provider binding."""

    def get_project_connection(
        self,
        *,
        project_id: str,
        extension_id: str,
        enabled_only: bool,
        connection_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def get_extension_by_install_id(
        self,
        install_id: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any] | None: ...


def extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = extension if isinstance(extension, dict) else {}
    compiled = payload.get("compiled_manifest")
    manifest = compiled if isinstance(compiled, dict) else {}
    return str(
        payload.get("extension_key")
        or payload.get("extension_id")
        or manifest.get("extension_id")
        or ""
    ).strip()


def resolve_install_bound_provider_extension(
    store: InstallBoundProviderStore,
    *,
    extension: dict[str, Any],
    connection_id: str,
    project_id: str,
    account_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve and verify the exact enabled install authorized for a project."""

    extension_id = extension_runtime_id(extension)
    getter = getattr(store, "get_project_connection", None)
    loader = getattr(store, "get_extension_by_install_id", None)
    if not extension_id or not callable(getter) or not callable(loader):
        raise RuntimeError("Install-bound provider runtime is not available")
    lookup: dict[str, Any] = {
        "project_id": project_id,
        "extension_id": extension_id,
        "enabled_only": True,
    }
    if connection_id:
        lookup["connection_id"] = connection_id
    raw_connection = getter(**lookup)
    connection = dict(raw_connection) if isinstance(raw_connection, dict) else {}
    install_id = str(connection.get("extension_install_id") or "").strip()
    if not connection or not install_id:
        raise RuntimeError("Provider connection install scope is missing")
    if str(connection.get("project_id") or "").strip() != str(project_id).strip():
        raise RuntimeError("Provider connection project scope mismatch")
    if str(connection.get("extension_id") or "").strip() != extension_id:
        raise RuntimeError("Provider connection extension scope mismatch")
    connection_account_id = str(connection.get("account_id") or "").strip()
    if account_id and connection_account_id != account_id:
        raise RuntimeError("Provider connection account scope mismatch")
    raw_resolved = loader(install_id, account_id=account_id or None)
    resolved = dict(raw_resolved) if isinstance(raw_resolved, dict) else {}
    resolved_install_id = str(
        resolved.get("install_id") or resolved.get("extension_install_id") or ""
    ).strip()
    if resolved_install_id != install_id:
        raise RuntimeError("Provider extension install identity mismatch")
    if extension_runtime_id(resolved) != extension_id:
        raise RuntimeError("Provider extension identity mismatch")
    return resolved, connection


__all__ = [
    "InstallBoundProviderStore",
    "extension_runtime_id",
    "resolve_install_bound_provider_extension",
]
