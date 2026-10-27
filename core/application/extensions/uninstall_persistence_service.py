from __future__ import annotations

from pathlib import Path
from typing import Any

from core.application.management.errors import ServiceConflictError, ServiceUnavailableError

from .binding_cleanup_service import ExtensionCleanupService
from .binding_context import ExtensionLifecycleContext
from .managed_release_service import ManagedReleaseService
from .oauth_lifecycle import record_extension_oauth_uninstall_audit
from .oauth_uninstall_lifecycle import GenericOAuthUninstallLifecycle
from .provider_registration import disable_provider_row_cur
from .uninstall_cleanup_queries import ExtensionUninstallCleanupQueries
from .uninstall_models import RemovalTransactionState, UninstallContext, UninstallTarget
from .uninstall_plan_service import ExtensionUninstallPlanService


class ExtensionUninstallPersistenceService:
    """Owns transactional uninstall writes while orchestration selects the lifecycle path."""

    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        cleanup: ExtensionCleanupService,
        managed_release: ManagedReleaseService,
        plan: ExtensionUninstallPlanService,
    ) -> None:
        self._context = context
        self._cleanup = cleanup
        self._managed_release = managed_release
        self._plan = plan
        self._queries = ExtensionUninstallCleanupQueries()
        self._generic_oauth = GenericOAuthUninstallLifecycle(
            lifecycle_transaction=context.lifecycle_transaction
        )

    def remove_installed_target(
        self,
        context: UninstallContext,
        target: UninstallTarget,
    ) -> tuple[Any, list[str], RemovalTransactionState]:
        if getattr(context.bl, "conn", None) is None:
            raise ServiceUnavailableError("Extension lifecycle storage is not available")
        bundle_root = (
            Path(str(target.extension.get("bundle_root") or "").strip())
            if target.extension.get("bundle_root")
            else None
        )
        removed_data: list[str] = []
        oauth_revocations: list[dict[str, Any]] = []
        prepared_oauth = self._generic_oauth.prepare(
            context, target, validate_locked_plan=self._validate_locked_plan
        )
        extension_oauth_outcome = self._generic_oauth.execute(prepared_oauth)
        with self._context.lifecycle_transaction(context.bl) as transaction_conn:
            if transaction_conn is None:
                raise ServiceUnavailableError("Extension lifecycle transaction is unavailable")
            with transaction_conn.cursor() as cur:
                state = self._prepare_removal_transaction(context, target, cur, bundle_root)
                self._queries.cleanup_install_dependencies(
                    context=context,
                    target=target,
                    state=state,
                    cur=cur,
                    removed_data=removed_data,
                    oauth_revocations=oauth_revocations,
                )
                self._queries.cleanup_projection_dependencies(
                    context=context,
                    target=target,
                    state=state,
                    cur=cur,
                    removed_data=removed_data,
                )
                self._queries.cleanup_runtime_records(
                    context=context, state=state, cur=cur, removed_data=removed_data
                )
                if prepared_oauth.owns_oauth:
                    record_extension_oauth_uninstall_audit(
                        context.bl,
                        extension_id=context.extension_id,
                        account_id=context.account_id,
                        install_id=str(
                            context.confirmed_plan.get("install_id") or target.install_id
                        ),
                        plan_revision=context.plan_revision,
                        actor_type=str(context.resolved_scope.principal_type or "system_actor"),
                        actor_id=str(context.resolved_scope.actor_id or "system"),
                        outcome=extension_oauth_outcome,
                    )
                    removed_data.append(f"oauth_remote_revocation_{extension_oauth_outcome}")
                # Same transaction as the removal: the extension and its provider
                # row go together, or the whole thing rolls back.
                if disable_provider_row_cur(cur, target.extension):
                    removed_data.append("provider_registration")
        removed_data.extend(state.file_notes)
        self._execute_oauth_revocations(oauth_revocations)
        return state.cleanup_intent, removed_data, state

    @staticmethod
    def _execute_oauth_revocations(revocations: list[dict[str, Any]]) -> None:
        if not revocations:
            return
        from core.application.auth.oauth_client import execute_prepared_oauth_revocation

        for revocation in revocations:
            try:
                execute_prepared_oauth_revocation(revocation)
            except Exception:
                # Local uninstall is already committed; remote revoke is best-effort.
                continue

    def _validate_locked_plan(self, context: UninstallContext, transaction_conn: Any) -> None:
        if transaction_conn is None:
            raise ServiceUnavailableError("Extension lifecycle transaction is unavailable")
        with transaction_conn.cursor() as cur:
            self._plan.lock_extension_install_for_uninstall(
                cur,
                account_id=context.account_id,
                install_id=str(context.confirmed_plan.get("install_id") or ""),
            )
            self._plan.lock_extension_uninstall_inventory(context.bl, cur)
            locked = self._plan.preview_extension_uninstall(
                context.extension_id, scope=context.scope
            )
        if str(locked.get("revision") or "") != context.plan_revision:
            self._raise_stale_plan(locked)
        context.confirmed_plan = locked

    def _prepare_removal_transaction(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        cur: Any,
        bundle_root: Path | None,
    ) -> RemovalTransactionState:
        self._plan.lock_extension_install_for_uninstall(
            cur,
            account_id=context.account_id,
            install_id=str(context.confirmed_plan.get("install_id") or target.install_id),
        )
        self._plan.lock_extension_uninstall_inventory(context.bl, cur)
        locked = self._plan.preview_extension_uninstall(context.extension_id, scope=context.scope)
        if str(locked.get("revision") or "") != context.plan_revision:
            self._raise_stale_plan(locked)
        install_id = str(locked.get("install_id") or target.install_id).strip()
        release_id = str(locked.get("release_id") or target.release_id).strip()
        remove_shared = self._is_last_runtime_install(
            context.bl, cur, context.extension_id, install_id
        )
        cleanup_intent, file_notes = self._prepare_bundle_cleanup(
            context, bundle_root, remove_shared
        )
        affected_persisted = self._persist_affected_state(context, cur, locked)
        context.confirmed_plan = locked
        return RemovalTransactionState(
            install_id=install_id,
            release_id=release_id,
            remove_shared_runtime_package=remove_shared,
            affected_state_persisted=affected_persisted,
            cleanup_intent=cleanup_intent,
            file_notes=file_notes,
        )

    @staticmethod
    def _raise_stale_plan(locked: dict[str, Any]) -> None:
        raise ServiceConflictError(
            "Extension uninstall impact changed; refresh the plan before confirming",
            payload={"stale_plan": True, "current_plan": locked},
        )

    @staticmethod
    def _is_last_runtime_install(bl: Any, cur: Any, extension_id: str, install_id: str) -> bool:
        if not getattr(bl, "_table_exists", lambda *_args: False)("extension_installs"):
            return True
        cur.execute(
            "SELECT 1 FROM extension_installs WHERE extension_id = %s AND install_id <> %s LIMIT 1",
            (extension_id, install_id),
        )
        return cur.fetchone() is None

    def _prepare_bundle_cleanup(
        self,
        context: UninstallContext,
        bundle_root: Path | None,
        remove_shared: bool,
    ) -> tuple[Any, tuple[str, ...]]:
        """Choose the files to delete with the install; never require them to exist.

        Uninstall is the only way out of an install, so it works whatever is
        left on disk: files lost with a volume, removed by hand, or never
        finished by a crashed install. Only paths inside the managed extensions
        root are considered — the extension's folder there (a managed release
        link or a plain folder left by an older build) and its releases.
        """
        if not remove_shared:
            return None, ()
        active_root = self._managed_release.managed_simple_extension_root(context.extension_id)
        roots: list[Path] = []
        notes: list[str] = []
        del bundle_root  # the recorded path may be stale; the managed layout is the truth
        if active_root.is_symlink():
            # A managed release link, dangling or not: the link goes, and the
            # release it names goes with the releases below.
            roots.append(active_root)
        elif active_root.is_dir():
            if (active_root / ".git").exists():
                # A developer's checkout placed in the extensions root is theirs.
                notes.append("extension_folder_kept_git_checkout")
            else:
                # A plain folder (older build, leftover cache): left in place, the
                # next scan would register it again.
                roots.append(active_root)
        else:
            notes.append("extension_files_already_missing")
        roots.extend(self._release_roots(context.extension_id, active_root))
        cleanup_intent = self._cleanup.prepare_extension_cleanup(
            extension_id=context.extension_id,
            account_id=context.account_id,
            plan_revision=context.plan_revision,
            bundle_roots=roots,
        )
        return cleanup_intent, tuple(notes)

    def _release_roots(self, extension_id: str, active_root: Path) -> list[Path]:
        """Every immutable release of this extension that still exists on disk."""
        releases_parent = active_root.parent / ".releases" / active_root.name
        if releases_parent.is_symlink() or not releases_parent.is_dir():
            return []
        return [
            self._managed_release.validated_managed_simple_extension_release_directory(
                extension_id=extension_id,
                release_root=candidate,
            )
            for candidate in sorted(releases_parent.iterdir())
            if candidate.is_dir() and not candidate.is_symlink()
        ]

    def _persist_affected_state(
        self,
        context: UninstallContext,
        cur: Any,
        plan: dict[str, Any],
    ) -> bool:
        if not plan or not getattr(context.bl, "_table_exists", lambda *_args: False)(
            "extension_registry_projections"
        ):
            return False
        self._plan.apply_confirmed_uninstall_affected_state_cur(
            plan=plan, cur=cur, account_id=context.account_id
        )
        return True


__all__ = ["ExtensionUninstallPersistenceService"]
