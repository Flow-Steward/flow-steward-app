from __future__ import annotations

from typing import Any

from .command_context import ExtensionCommandContext
from .command_install import ExtensionCommandInstall
from .service_support import _entitlement_status_value, _normalize_text


class ExtensionLifecycleRecoveryOperations:
    """Rollback and release-cleanup operations for extension lifecycle commands."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        install: ExtensionCommandInstall,
    ) -> None:
        self._context = context
        self._install = install

    def rollback_install_release_failure(
        self,
        *,
        install_store: Any,
        runtime_store: Any,
        extension_id: str,
        account_id: str,
        failed_install_id: str,
        previous: dict[str, Any] | None,
        previous_release: Any | None = None,
        previous_entitlement: Any | None = None,
        previous_projections: list[Any] | None = None,
    ) -> None:
        normalized_install_id = _normalize_text(failed_install_id)
        self._install.remove_dataset_execution_bridges(install_id=normalized_install_id)
        if normalized_install_id and hasattr(runtime_store, "delete_projections_for_install"):
            runtime_store.delete_projections_for_install(normalized_install_id)
        if normalized_install_id and hasattr(runtime_store, "delete_entitlement"):
            runtime_store.delete_entitlement(normalized_install_id)
        if previous is None:
            if hasattr(install_store, "uninstall"):
                install_store.uninstall(extension_id=extension_id, account_id=account_id)
            return
        restored = install_store.install(
            **{key: value for key, value in previous.items() if key != "state"}
        )
        if previous_entitlement is not None and hasattr(runtime_store, "upsert_entitlement"):
            runtime_store.upsert_entitlement(
                install_id=restored.install_id,
                account_id=restored.account_id,
                status=_entitlement_status_value(previous_entitlement),
                trial_ends_at=getattr(previous_entitlement, "trial_ends_at", None),
                grace_ends_at=getattr(previous_entitlement, "grace_ends_at", None),
                suspended_reason=_normalize_text(
                    getattr(previous_entitlement, "suspended_reason", "")
                ),
                metadata=dict(getattr(previous_entitlement, "metadata", {}) or {}),
                updated_by=_normalize_text(getattr(previous_entitlement, "updated_by", "")),
            )
        if previous_release is not None:
            self._install.project_release_for_install(
                install_record=restored,
                release=previous_release,
                account_id=account_id,
            )
        for projection in previous_projections or []:
            if hasattr(runtime_store, "upsert_projection"):
                runtime_store.upsert_projection(projection)
        if previous_release is not None:
            self._install.materialize_dataset_execution_bridges(
                install_record=restored,
                release=previous_release,
                account_id=account_id,
            )
        previous_state = _normalize_text(previous.get("state"))
        if previous_state and previous_state != "active" and hasattr(install_store, "set_state"):
            install_store.set_state(
                extension_id=extension_id,
                account_id=account_id,
                state=previous_state,
            )
        if normalized_install_id and normalized_install_id != _normalize_text(
            getattr(restored, "install_id", "")
        ):
            if hasattr(runtime_store, "delete_projections_for_install"):
                runtime_store.delete_projections_for_install(normalized_install_id)
            if hasattr(runtime_store, "delete_entitlement"):
                runtime_store.delete_entitlement(normalized_install_id)

    @staticmethod
    def remove_obsolete_install_projections(
        *,
        runtime_store: Any,
        account_id: str,
        install_id: str,
        release_id: str,
    ) -> None:
        if not hasattr(runtime_store, "list_projections") or not hasattr(
            runtime_store, "delete_projections_for_install"
        ):
            return
        desired_rows = [
            row
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
            if _normalize_text(getattr(row, "install_id", "")) == _normalize_text(install_id)
            and _normalize_text(getattr(row, "release_id", "")) == _normalize_text(release_id)
        ]
        desired_keys = {
            (
                _normalize_text(getattr(row, "artifact_kind", "")),
                _normalize_text(getattr(row, "artifact_id", "")),
            )
            for row in desired_rows
        }
        all_rows = [
            row
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
            if _normalize_text(getattr(row, "install_id", "")) == _normalize_text(install_id)
        ]
        obsolete_rows = [
            row
            for row in all_rows
            if (
                _normalize_text(getattr(row, "artifact_kind", "")),
                _normalize_text(getattr(row, "artifact_id", "")),
            )
            not in desired_keys
        ]
        if not obsolete_rows:
            return
        runtime_store.delete_projections_for_install(install_id)
        for row in desired_rows:
            runtime_store.upsert_projection(row)

    def cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None:
        normalized_release_id = _normalize_text(release_id)
        if not normalized_release_id or not hasattr(install_store, "list_all_installs"):
            return
        remaining = [
            row
            for row in install_store.list_all_installs(extension_id)
            if _normalize_text(getattr(row, "release_id", "")) == normalized_release_id
        ]
        if not remaining:
            self.cleanup_release_artifacts(normalized_release_id)

    def cleanup_release_artifacts(self, release_id: str) -> None:
        runtime_store = self._context.runtime_store()
        if hasattr(runtime_store, "delete_release"):
            runtime_store.delete_release(release_id)


__all__ = ["ExtensionLifecycleRecoveryOperations"]
