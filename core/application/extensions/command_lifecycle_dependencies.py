from __future__ import annotations

from typing import Any

from core.application.management.errors import ServiceValidationError

from .command_install import ExtensionCommandInstall
from .service_support import _normalize_text


class ExtensionLifecycleDependencyOperations:
    """Dependency policy and projection preservation for lifecycle mutations."""

    def __init__(self, *, install: ExtensionCommandInstall) -> None:
        self._install = install

    def reject_required_dependents(
        self,
        *,
        extension_id: str,
        account_id: str,
        action: str,
        install_store: Any,
        runtime_store: Any,
    ) -> None:
        blockers = self.required_dependents(
            extension_id=extension_id,
            account_id=account_id,
            install_store=install_store,
            runtime_store=runtime_store,
        )
        if blockers:
            raise ServiceValidationError(
                f"Cannot {action} extension '{extension_id}' while required dependents are active",
                payload={"dependents": blockers},
            )

    def required_dependents(
        self,
        *,
        extension_id: str,
        account_id: str,
        install_store: Any,
        runtime_store: Any,
    ) -> list[dict[str, Any]]:
        if not hasattr(install_store, "list_for_account"):
            return []
        target_install = (
            install_store.get(extension_id=extension_id, account_id=account_id)
            if hasattr(install_store, "get")
            else None
        )
        target_visibility = _normalize_text(getattr(target_install, "visibility", "")).lower()
        if target_visibility == "global" and hasattr(install_store, "list_active_installs"):
            active_installs = install_store.list_active_installs()
        else:
            active_installs = install_store.list_for_account(
                account_id,
                include_global=False,
                include_inactive_owned=False,
            )
        release_by_id = self._releases_for_installs(
            active_installs=active_installs,
            runtime_store=runtime_store,
        )
        blockers: list[dict[str, Any]] = []
        for install in active_installs:
            blockers.extend(
                self._install_blockers(
                    install=install,
                    extension_id=extension_id,
                    account_id=account_id,
                    release_by_id=release_by_id,
                )
            )
        return blockers

    @staticmethod
    def _releases_for_installs(
        *,
        active_installs: list[Any],
        runtime_store: Any,
    ) -> dict[str, Any]:
        release_ids = [
            _normalize_text(getattr(row, "release_id", ""))
            for row in active_installs
            if _normalize_text(getattr(row, "release_id", ""))
        ]
        if release_ids and hasattr(runtime_store, "list_releases"):
            return {
                _normalize_text(getattr(row, "release_id", "")): row
                for row in (runtime_store.list_releases(release_ids=release_ids) or [])
            }
        if hasattr(runtime_store, "get_release"):
            return {release_id: runtime_store.get_release(release_id) for release_id in release_ids}
        return {}

    def _install_blockers(
        self,
        *,
        install: Any,
        extension_id: str,
        account_id: str,
        release_by_id: dict[str, Any],
    ) -> list[dict[str, Any]]:
        dependent_id = _normalize_text(getattr(install, "extension_id", ""))
        dependent_account_id = _normalize_text(getattr(install, "account_id", ""))
        if not dependent_id or (
            dependent_id == extension_id and dependent_account_id == _normalize_text(account_id)
        ):
            return []
        release = release_by_id.get(_normalize_text(getattr(install, "release_id", "")))
        descriptor = dict(getattr(release, "descriptor", {}) or {}) if release else {}
        blockers: list[dict[str, Any]] = []
        for dependency in self._install.extension_dependency_specs(descriptor):
            if not bool(dependency.get("required", True)):
                continue
            if _normalize_text(dependency.get("extension_id")) == extension_id:
                blockers.append(
                    {
                        "extension_id": dependent_id,
                        "install_id": _normalize_text(getattr(install, "install_id", "")),
                        "account_id": dependent_account_id,
                        "reason": _normalize_text(dependency.get("reason")),
                    }
                )
        return blockers

    def preserve_required_dependents(
        self,
        *,
        extension_id: str,
        account_id: str,
        install_store: Any,
        runtime_store: Any,
    ) -> None:
        dependents = self.required_dependents(
            extension_id=extension_id,
            account_id=account_id,
            install_store=install_store,
            runtime_store=runtime_store,
        )
        if (
            not dependents
            or not hasattr(runtime_store, "list_projections")
            or not hasattr(runtime_store, "upsert_projection")
        ):
            return
        availability = self._dependency_availability(extension_id)
        by_account: dict[str, set[str]] = {}
        for dependent in dependents:
            dependent_account = _normalize_text(dependent.get("account_id")) or account_id
            dependent_install_id = _normalize_text(dependent.get("install_id"))
            if dependent_install_id:
                by_account.setdefault(dependent_account, set()).add(dependent_install_id)
        for dependent_account, install_ids in by_account.items():
            self._mark_projections_unavailable(
                runtime_store=runtime_store,
                dependent_account=dependent_account,
                install_ids=install_ids,
                availability=availability,
            )

    @staticmethod
    def _dependency_availability(extension_id: str) -> dict[str, Any]:
        return {
            "availability_status": "unavailable",
            "availability_reason_code": "required_extension_uninstalled",
            "availability_reason_text": f"Missing required extension: {extension_id}",
            "missing_extensions": [extension_id],
            "new_runs_blocked": True,
            "visible_in_catalog": True,
        }

    @staticmethod
    def _mark_projections_unavailable(
        *,
        runtime_store: Any,
        dependent_account: str,
        install_ids: set[str],
        availability: dict[str, Any],
    ) -> None:
        rows = runtime_store.list_projections(
            account_id=dependent_account,
            only_active=False,
        )
        for row in rows:
            if _normalize_text(getattr(row, "install_id", "")) not in install_ids:
                continue
            projection = dict(getattr(row, "projection", {}) or {})
            projection["extension_dependency_availability"] = dict(availability)
            runtime_store.upsert_projection(
                row.model_copy(update={"projection": projection})
                if hasattr(row, "model_copy")
                else {**dict(row), "projection": projection}
            )


__all__ = ["ExtensionLifecycleDependencyOperations"]
