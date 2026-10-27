from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .uninstall_plan_presentation import descriptor_display_name


def collect_descriptor_only_dependents(
    *,
    extension_id: str,
    removed_install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    runtime_store: Any,
    install_store: Any,
    projected_install_ids: set[str],
    alternative_install_available: Callable[..., bool],
) -> list[dict[str, Any]]:
    if not hasattr(install_store, "list_for_account"):
        return []
    if include_consumer_accounts and hasattr(install_store, "list_active_installs"):
        active_installs = install_store.list_active_installs()
    else:
        active_installs = install_store.list_for_account(
            account_id,
            include_global=False,
            include_inactive_owned=False,
        )
    release_ids = [
        str(getattr(row, "release_id", "") or "").strip()
        for row in active_installs
        if str(getattr(row, "release_id", "") or "").strip()
    ]
    releases = (
        runtime_store.list_releases(release_ids=release_ids)
        if release_ids and hasattr(runtime_store, "list_releases")
        else []
    )
    release_by_id = {str(getattr(row, "release_id", "") or "").strip(): row for row in releases}
    affected: list[dict[str, Any]] = []
    for install in active_installs:
        install_id = str(getattr(install, "install_id", "") or "").strip()
        if install_id == removed_install_id or install_id in projected_install_ids:
            continue
        release = release_by_id.get(str(getattr(install, "release_id", "") or "").strip())
        descriptor = dict(getattr(release, "descriptor", {}) or {}) if release else {}
        dependencies = dict(descriptor.get("dependencies") or {}).get("extensions")
        if not isinstance(dependencies, list):
            continue
        dependency = next(
            (
                dict(item)
                for item in dependencies
                if isinstance(item, dict)
                and str(item.get("extension_id") or "").strip() == extension_id
                and bool(item.get("required", True))
                and not alternative_install_available(
                    install_store=install_store,
                    account_id=str(getattr(install, "account_id", "") or account_id),
                    removed_install_id=removed_install_id,
                    dependency=dict(item),
                )
            ),
            None,
        )
        if dependency is None:
            continue
        affected.append(
            {
                "kind": "extension_install",
                "id": str(getattr(install, "extension_id", "") or "").strip(),
                "display_name": descriptor_display_name(descriptor),
                "owner_install_id": install_id,
                "owner_account_id": str(getattr(install, "account_id", "") or account_id).strip(),
                "action": "preserve_and_disable",
                "reason": f"Missing required extension: {extension_id}",
            }
        )
    return affected


__all__ = ["collect_descriptor_only_dependents"]
