from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import ExtensionArchiveUninstallResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

from .binding_cleanup_service import ExtensionCleanupService
from .binding_context import ExtensionLifecycleContext
from .binding_projection_service import ExtensionBindingProjectionService
from .extension_state_service import ExtensionStateService
from .lifecycle_ports import (
    ERR_DATABASE_NOT_CONFIGURED,
    ERR_EXTENSION_NOT_FOUND,
    require_global_extension_lifecycle_scope,
    service_scope,
)
from .managed_release_service import ManagedReleaseService
from .uninstall_models import UninstallContext, UninstallTarget
from .uninstall_persistence_service import ExtensionUninstallPersistenceService
from .uninstall_plan_service import (
    ExtensionUninstallPlanService,
    _install_visibility,
)
from .uninstall_target_removal import remove_resolved_uninstall_target


class ExtensionUninstallService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        cleanup: ExtensionCleanupService,
        projection: ExtensionBindingProjectionService,
        managed_release: ManagedReleaseService,
        plan: ExtensionUninstallPlanService,
        persistence: ExtensionUninstallPersistenceService,
        state: ExtensionStateService,
        get_logger: Callable[[], Any],
        require_archive_mutation_available: Callable[[], None],
    ) -> None:
        self._context = context
        self._cleanup = cleanup
        self._projection = projection
        self._managed_release = managed_release
        self._plan = plan
        self._persistence = persistence
        self._state = state
        self._get_logger = get_logger
        self._require_archive_mutation_available = require_archive_mutation_available

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        plan_revision: str = "",
    ) -> ExtensionArchiveUninstallResultView:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        with self._context.update_lock(bl=bl, extension_id=extension_id):
            return self._uninstall_extension_locked(
                extension_id,
                scope=scope,
                plan_revision=plan_revision,
            )

    def _uninstall_extension_locked(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        plan_revision: str = "",
    ) -> ExtensionArchiveUninstallResultView:
        context = self._prepare_uninstall_context(
            extension_id=extension_id,
            scope=scope,
            plan_revision=plan_revision,
        )
        target_or_result = self._resolve_uninstall_target(context)
        if isinstance(target_or_result, ExtensionArchiveUninstallResultView):
            return target_or_result
        return self._persist_resolved_uninstall(context, target_or_result)

    def _prepare_uninstall_context(
        self,
        *,
        extension_id: str,
        scope: ScopeContext | None,
        plan_revision: str,
    ) -> UninstallContext:
        resolved_scope = service_scope(scope)
        require_global_extension_lifecycle_scope(resolved_scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        normalized_plan_revision = str(plan_revision or "").strip()
        if not normalized_plan_revision:
            raise ServiceValidationError(
                "Extension uninstall requires a confirmed impact plan revision"
            )
        self._cleanup.reconcile_extension_cleanup(
            bl=bl,
            discard_installed_intents=True,
        )
        confirmed_plan = self._plan.preview_extension_uninstall(extension_id, scope=scope)
        self._require_matching_uninstall_plan(confirmed_plan, normalized_plan_revision)
        self._reject_required_dependents_in_plan(confirmed_plan, extension_id=extension_id)
        runtime_store = self._context.runtime_store(bl)
        return UninstallContext(
            extension_id=extension_id,
            scope=scope,
            resolved_scope=resolved_scope,
            plan_revision=normalized_plan_revision,
            account_id=resolved_scope.account_id or DEFAULT_ACCOUNT_ID,
            bl=bl,
            runtime_store=runtime_store,
            confirmed_plan=confirmed_plan,
        )

    @staticmethod
    def _require_matching_uninstall_plan(plan: dict[str, Any], plan_revision: str) -> None:
        if str(plan.get("revision") or "") == plan_revision:
            return
        raise ServiceConflictError(
            "Extension uninstall impact changed; refresh the plan before confirming",
            payload={"stale_plan": True, "current_plan": plan},
        )

    @staticmethod
    def _reject_required_dependents_in_plan(plan: dict[str, Any], *, extension_id: str) -> None:
        # "stops_working" rows (agents and projects routed to a removed LLM
        # provider's models) are listed for the operator, not dependents that
        # block the uninstall.
        affected_rows = [
            row
            for row in plan.get("affected") or []
            if not (isinstance(row, dict) and row.get("action") == "stops_working")
        ]
        dependents = [
            dict(row)
            for row in affected_rows
            if isinstance(row, dict) and row.get("action") == "preserve_and_disable"
        ]
        workflows_can_be_preserved = (
            bool(dependents)
            and len(dependents) == len(affected_rows)
            and all(row.get("kind") == "workflow" for row in dependents)
        )
        if workflows_can_be_preserved:
            return
        if dependents:
            raise ServiceValidationError(
                f"Cannot uninstall extension '{extension_id}' while required dependents are active",
                payload={"dependents": dependents},
            )

    def _validate_locked_plan(self, context: UninstallContext, transaction_conn: Any) -> None:
        if transaction_conn is None:
            raise ServiceUnavailableError("Extension lifecycle transaction is unavailable")
        install_id = str(context.confirmed_plan.get("install_id") or "").strip()
        if not install_id:
            raise ServiceValidationError(
                "Confirmed extension uninstall plan is missing a usable install_id"
            )
        with transaction_conn.cursor() as cur:
            self._plan.lock_extension_install_for_uninstall(
                cur,
                account_id=context.account_id,
                install_id=install_id,
            )
            self._plan.lock_extension_uninstall_inventory(context.bl, cur)
            locked = self._plan.preview_extension_uninstall(
                context.extension_id,
                scope=context.scope,
            )
        self._require_matching_uninstall_plan(locked, context.plan_revision)
        self._reject_required_dependents_in_plan(locked, extension_id=context.extension_id)
        context.confirmed_plan = locked

    def _complete_with_affected_state(
        self,
        context: UninstallContext,
        result: ExtensionArchiveUninstallResultView,
    ) -> ExtensionArchiveUninstallResultView:
        if result.state == "uninstalled":
            self._delete_runtime_install_records(context)
        self._plan.apply_confirmed_uninstall_affected_state(
            plan=context.confirmed_plan,
            runtime_store=context.runtime_store,
            account_id=context.account_id,
        )
        return result

    @staticmethod
    def _delete_runtime_install_records(
        context: UninstallContext,
        install_id: str = "",
    ) -> None:
        resolved_install_id = str(
            install_id or context.confirmed_plan.get("install_id") or ""
        ).strip()
        if resolved_install_id and hasattr(context.runtime_store, "delete_projections_for_install"):
            context.runtime_store.delete_projections_for_install(resolved_install_id)
        if resolved_install_id and hasattr(context.runtime_store, "delete_entitlement"):
            context.runtime_store.delete_entitlement(resolved_install_id)

    def _reconcile_committed_bundle_cleanup(
        self,
        context: UninstallContext,
        cleanup_intent: Any,
        removed_data: list[str],
    ) -> ExtensionArchiveUninstallResultView:
        cleanup = self._cleanup.reconcile_extension_cleanup(
            bl=context.bl,
            intent_path=cleanup_intent,
            committed_extension_id=context.extension_id,
        )
        if cleanup.completed:
            removed_data.append("bundle_files")
        if cleanup.pending:
            removed_data.append("bundle_cleanup_pending")
        return ExtensionArchiveUninstallResultView(
            extension_id=context.extension_id,
            state="uninstalled",
            removed_data=removed_data,
        )

    def _uninstall_simple_artifact(
        self,
        context: UninstallContext,
        *,
        bundle_root: Path,
        invoke_runtime_uninstall: bool,
    ) -> ExtensionArchiveUninstallResultView:
        self._require_archive_mutation_available()
        removed_data = [
            "install_runtime_records",
            *(["entitlement_records", "registry_projections"] if invoke_runtime_uninstall else []),
            "release_runtime_records",
            "bundle_records",
        ]
        cleanup_intent = None
        active_root = self._managed_release.managed_simple_extension_root(context.extension_id)
        # A plain folder left at the managed root goes too, or the next scan
        # registers it again; a developer's git checkout there is left alone.
        if bundle_root != active_root or active_root.is_symlink():
            cleanup_roots = [bundle_root]
        elif active_root.is_dir() and not (active_root / ".git").exists():
            cleanup_roots = [active_root]
        else:
            cleanup_roots = []
        with self._context.lifecycle_transaction(context.bl) as transaction_conn:
            self._validate_locked_plan(context, transaction_conn)
            cleanup_intent = self._cleanup.prepare_extension_cleanup(
                extension_id=context.extension_id,
                account_id=context.account_id,
                plan_revision=context.plan_revision,
                bundle_roots=cleanup_roots,
            )
            if invoke_runtime_uninstall:
                self._context.command_service(context.bl).uninstall_extension(
                    context.extension_id,
                    scope=context.scope,
                )
            self._cleanup.purge_simple_extension_scan_state(
                bl=context.bl,
                extension_id=context.extension_id,
            )
            self._complete_with_affected_state(
                context,
                ExtensionArchiveUninstallResultView(
                    extension_id=context.extension_id,
                    state="uninstalled",
                    removed_data=removed_data,
                ),
            )
        return self._reconcile_committed_bundle_cleanup(context, cleanup_intent, removed_data)

    def _uninstall_runtime_only_extension(
        self,
        context: UninstallContext,
    ) -> ExtensionArchiveUninstallResultView:
        with self._context.lifecycle_transaction(context.bl) as transaction_conn:
            self._validate_locked_plan(context, transaction_conn)
            self._context.command_service(context.bl).uninstall_extension(
                context.extension_id,
                scope=context.scope,
            )
            return self._complete_with_affected_state(
                context,
                ExtensionArchiveUninstallResultView(
                    extension_id=context.extension_id,
                    state="uninstalled",
                    removed_data=[
                        "install_runtime_records",
                        "entitlement_records",
                        "registry_projections",
                        "release_runtime_records",
                    ],
                ),
            )

    def _resolve_uninstall_target(
        self,
        context: UninstallContext,
    ) -> UninstallTarget | ExtensionArchiveUninstallResultView:
        bl = context.bl
        extension_id = context.extension_id
        if not hasattr(bl, "get_extension"):
            raise ServiceUnavailableError("Extension lifecycle is not available")
        current_install = None
        with contextlib.suppress(Exception):
            current_install = self._state.current_extension_install_record(
                bl,
                extension_id=extension_id,
                scope=context.resolved_scope,
            )
        extension = bl.get_extension(extension_id)
        if (
            not extension
            and current_install is not None
            and (
                getattr(context.runtime_store, "_conn", None) is not None
                or str(getattr(current_install, "runtime_mode", "") or "").strip() != "remote_https"
            )
        ):
            extension = {
                "extension_key": extension_id,
                "install_id": str(getattr(current_install, "install_id", "") or ""),
                "release_id": str(getattr(current_install, "release_id", "") or ""),
                "version": str(getattr(current_install, "version", "") or ""),
                "descriptor_source": str(getattr(current_install, "descriptor_source", "") or ""),
            }
        if not extension:
            resolved = self._resolve_runtime_only_target(context)
            if isinstance(resolved, ExtensionArchiveUninstallResultView):
                return resolved
            extension = resolved

        extension = dict(extension)
        install_descriptor_source = str(
            getattr(current_install, "descriptor_source", "") or ""
        ).strip()
        if install_descriptor_source:
            extension["descriptor_source"] = install_descriptor_source
        install_id = str(context.confirmed_plan.get("install_id") or "").strip()
        if not install_id:
            install_id = str(getattr(current_install, "install_id", "") or "").strip()
        if not install_id:
            install_id = str(extension.get("install_id") or "").strip()
        release_id = str(getattr(current_install, "release_id", "") or "").strip()
        if not release_id:
            release_id = str(extension.get("release_id") or "").strip()
        include_consumer_accounts = _install_visibility(current_install) == "global"
        return UninstallTarget(
            extension=extension,
            install_id=install_id,
            release_id=release_id,
            include_consumer_accounts=include_consumer_accounts,
        )

    def _resolve_runtime_only_target(
        self,
        context: UninstallContext,
    ) -> dict[str, Any] | ExtensionArchiveUninstallResultView:
        runtime_extension, detail = self._projection.runtime_extension_detail(
            bl=context.bl, extension_id=context.extension_id, scope=context.scope
        )
        if runtime_extension is None or detail is None:
            if not self._cleanup.has_simple_extension_artifact(
                bl=context.bl, extension_id=context.extension_id
            ):
                raise ServiceNotFoundError(ERR_EXTENSION_NOT_FOUND)
            return self._resolve_orphan_artifact(context)
        descriptor_source = str(
            getattr(detail, "descriptor_source", "")
            or runtime_extension.get("descriptor_source")
            or ""
        ).strip()
        if descriptor_source == "extension_scan":
            scan_result = self._resolve_scanned_runtime_target(context, runtime_extension, detail)
            if scan_result is not None:
                return scan_result
        if getattr(context.runtime_store, "_conn", None) is None:
            return self._uninstall_runtime_only_extension(context)
        return {
            **runtime_extension,
            "extension_key": context.extension_id,
            "install_id": str(getattr(detail, "install_id", "") or ""),
            "release_id": str(getattr(detail, "release_id", "") or ""),
        }

    def _resolve_orphan_artifact(
        self,
        context: UninstallContext,
    ) -> ExtensionArchiveUninstallResultView:
        root = self._managed_release.managed_simple_extension_root(context.extension_id)
        return self._uninstall_simple_artifact(
            context, bundle_root=root, invoke_runtime_uninstall=False
        )

    def _resolve_scanned_runtime_target(
        self,
        context: UninstallContext,
        runtime_extension: dict[str, Any],
        detail: Any,
    ) -> dict[str, Any] | ExtensionArchiveUninstallResultView | None:
        root = self._managed_release.managed_simple_extension_root(context.extension_id)
        if getattr(context.runtime_store, "_conn", None) is None:
            return self._uninstall_simple_artifact(
                context, bundle_root=root, invoke_runtime_uninstall=True
            )
        return {
            **runtime_extension,
            "extension_key": context.extension_id,
            "bundle_root": str(root),
            "install_id": str(getattr(detail, "install_id", "") or ""),
        }

    def _persist_resolved_uninstall(
        self,
        context: UninstallContext,
        target: UninstallTarget,
    ) -> ExtensionArchiveUninstallResultView:
        """Remove the extension, its provider row going with it or neither.

        The provider row is disabled inside the same transaction that commits the
        removal, in `remove_installed_target`. That
        is the only way the two can be atomic: disabling it around the outside and
        restoring on failure produced the opposite half-state — a later step
        failing after the removal had already committed put an enabled provider on
        an extension that was gone.

        Nothing after those commits needs unwinding: if the fallback store
        uninstall or the affected-state write fails, the extension is removed and
        its provider is disabled, which is consistent.
        """
        return remove_resolved_uninstall_target(
            context,
            target,
            persistence=self._persistence,
            reconcile_committed_bundle_cleanup=self._reconcile_committed_bundle_cleanup,
            delete_runtime_install_records=self._delete_runtime_install_records,
            install_store=self._context.install_store(context.bl),
            plan=self._plan,
            require_archive_mutation_available=self._require_archive_mutation_available,
        )


__all__ = ["ExtensionUninstallService"]
