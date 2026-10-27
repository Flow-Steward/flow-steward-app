from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceNotFoundError, ServiceUnavailableError
from core.application.management.scope import ScopeContext

from .binding_context import ExtensionLifecycleContext
from .extension_state_service import ExtensionStateService
from .lifecycle_ports import (
    ERR_DATABASE_NOT_CONFIGURED,
    require_global_extension_lifecycle_scope,
    service_scope,
)
from .uninstall_inventory_repository import (
    ExtensionUninstallInventoryRepository,
    dependency_has_available_alternative_install,
)
from .uninstall_inventory_repository import (
    install_visibility as _install_visibility,
)
from .uninstall_plan_presentation import (
    PlanSubject,
    descriptor_display_name,
    present_uninstall_plan,
)


def _dependency_has_available_alternative_install(
    *,
    install_store: Any,
    account_id: str,
    removed_install_id: str,
    dependency: dict[str, Any],
) -> bool:
    return dependency_has_available_alternative_install(
        install_store=install_store,
        account_id=account_id,
        removed_install_id=removed_install_id,
        dependency=dependency,
    )


def _extension_display_name(runtime_store: Any, release_id: str) -> str:
    """The name the extension's descriptor gives it, or "" when there is none."""
    if not release_id or not hasattr(runtime_store, "get_release"):
        return ""
    release = runtime_store.get_release(release_id)
    return descriptor_display_name(getattr(release, "descriptor", None) if release else None)


class ExtensionUninstallPlanService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        state: ExtensionStateService,
        inventory: ExtensionUninstallInventoryRepository,
        get_logger: Callable[[], Any],
    ) -> None:
        self._context = context
        self._state = state
        self._inventory = inventory
        self._get_logger = get_logger

    def lock_extension_uninstall_inventory(self, bl: Any, cur: Any) -> None:
        self._lock_extension_uninstall_inventory(bl, cur)

    @staticmethod
    def lock_extension_install_for_uninstall(cur: Any, *, account_id: str, install_id: str) -> bool:
        """Lock the owning install before any provider/inventory mutation locks."""
        normalized_install_id = str(install_id or "").strip()
        if not normalized_install_id:
            return False
        cur.execute(
            """
            SELECT install_id
              FROM extension_installs
             WHERE account_id::text = %s AND install_id = %s
             FOR UPDATE
            """,
            (str(account_id), normalized_install_id),
        )
        return cur.fetchone() is not None

    @staticmethod
    def _lock_extension_uninstall_inventory(bl: Any, cur: Any) -> None:
        """Prevent inventory writes between confirmation revalidation and deletion."""
        tables = (
            "extension_bundles",
            "extension_versions",
            "enabled_extensions",
            "extension_installs",
            "extension_releases",
            "extension_entitlements",
            "extension_usage_events",
            "extension_invocation_logs",
            "extension_registry_projections",
            "extension_artifact_snapshots",
            "project_connections",
            "workspace_data_connections",
            "extension_callback_registrations",
            "extension_project_state",
            "external_effects",
            "llm_provider_sources",
            "secret_refs",
            "project_workflows",
            "project_datasets",
            "registry_datasets",
            "dataset_query_templates",
            "registry_workflows",
        )
        existing = [
            table for table in tables if getattr(bl, "_table_exists", lambda *_args: False)(table)
        ]
        if existing:
            cur.execute("LOCK TABLE " + ", ".join(existing) + " IN SHARE ROW EXCLUSIVE MODE")

    def apply_confirmed_uninstall_affected_state_cur(
        self,
        *,
        plan: dict[str, Any] | None,
        cur: Any,
        account_id: str,
    ) -> None:
        if not plan:
            return
        extension_id = str(plan.get("extension_id") or "").strip()
        for item in plan.get("affected") or []:
            if isinstance(item, dict) and item.get("action") == "preserve_and_disable":
                self._persist_affected_projection(
                    cur=cur, item=item, extension_id=extension_id, account_id=account_id
                )

    @staticmethod
    def _persist_affected_projection(
        *,
        cur: Any,
        item: dict[str, Any],
        extension_id: str,
        account_id: str,
    ) -> None:
        reason = (
            str(item.get("reason") or "").strip() or f"Missing required extension: {extension_id}"
        )
        availability = {
            "availability_status": "unavailable",
            "availability_reason_code": "required_extension_uninstalled",
            "availability_reason_text": reason,
            "missing_extensions": [extension_id],
            "new_runs_blocked": True,
            "visible_in_catalog": True,
        }
        cur.execute(
            "UPDATE extension_registry_projections SET projection_jsonb = jsonb_set(COALESCE(projection_jsonb, '{}'::jsonb), '{extension_dependency_availability}', %s::jsonb, true), updated_at = now() WHERE account_id = %s AND install_id = %s AND artifact_kind = %s AND artifact_id = %s",
            (
                json.dumps(availability),
                str(item.get("owner_account_id") or account_id).strip(),
                str(item.get("owner_install_id") or "").strip(),
                str(item.get("kind") or "").strip(),
                str(item.get("id") or "").strip(),
            ),
        )

    def apply_confirmed_uninstall_affected_state(
        self,
        *,
        plan: dict[str, Any] | None,
        runtime_store: Any,
        account_id: str,
    ) -> None:
        if (
            not plan
            or not hasattr(runtime_store, "list_projections")
            or not hasattr(runtime_store, "upsert_projection")
        ):
            return
        affected = self._affected_projection_keys(plan)
        extension_id = str(plan.get("extension_id") or "").strip()
        for row in runtime_store.list_projections(account_id=account_id, only_active=False):
            key = (
                str(getattr(row, "install_id", "") or "").strip(),
                str(getattr(row, "artifact_kind", "") or "").strip(),
                str(getattr(row, "artifact_id", "") or "").strip(),
            )
            if key in affected:
                self._update_affected_projection(runtime_store, row, affected[key], extension_id)

    @staticmethod
    def _affected_projection_keys(plan: dict[str, Any]) -> dict[tuple[str, str, str], str]:
        return {
            (
                str(item.get("owner_install_id") or "").strip(),
                str(item.get("kind") or "").strip(),
                str(item.get("id") or "").strip(),
            ): str(item.get("reason") or "").strip()
            for item in plan.get("affected") or []
            if isinstance(item, dict) and item.get("action") == "preserve_and_disable"
        }

    @staticmethod
    def _update_affected_projection(
        runtime_store: Any,
        row: Any,
        reason: str,
        extension_id: str,
    ) -> None:
        projection = dict(getattr(row, "projection", {}) or {})
        projection["extension_dependency_availability"] = {
            "availability_status": "unavailable",
            "availability_reason_code": "required_extension_uninstalled",
            "availability_reason_text": reason or f"Missing required extension: {extension_id}",
            "missing_extensions": [extension_id],
            "new_runs_blocked": True,
            "visible_in_catalog": True,
        }
        runtime_store.upsert_projection(row.model_copy(update={"projection": projection}))

    def preview_extension_uninstall(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved_scope = service_scope(scope)
        require_global_extension_lifecycle_scope(resolved_scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        install = self._state.current_extension_install_record(
            bl, extension_id=extension_id, scope=resolved_scope
        )
        if install is None:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        install_id = str(getattr(install, "install_id", "") or "").strip()
        release_id = str(getattr(install, "release_id", "") or "").strip()
        include_consumers = _install_visibility(install) == "global"
        runtime_store = self._context.runtime_store(bl)
        account_id = resolved_scope.account_id or DEFAULT_ACCOUNT_ID
        subject = PlanSubject(
            extension_id=extension_id,
            name=_extension_display_name(runtime_store, release_id),
            version=str(getattr(install, "version", "") or "").strip(),
        )
        remove, retained = self._inventory.collect(
            bl=bl,
            extension_id=extension_id,
            install_id=install_id,
            release_id=release_id,
            account_id=account_id,
            include_consumer_accounts=include_consumers,
            runtime_store=runtime_store,
        )
        affected = self._inventory.collect_affected(
            bl=bl,
            extension_id=extension_id,
            install_id=install_id,
            account_id=account_id,
            include_consumer_accounts=include_consumers,
            runtime_store=runtime_store,
            install_store=self._context.install_store(bl),
            extension_name=subject.name,
        )
        remove, affected, retained = present_uninstall_plan(
            bl=bl, subject=subject, remove=remove, affected=affected, retained=retained
        )
        return self._canonical_uninstall_plan(
            extension_id=extension_id,
            extension_name=subject.name or extension_id,
            extension_version=subject.version,
            install_id=install_id,
            release_id=release_id,
            remove=remove,
            affected=affected,
            retained=retained,
        )

    @staticmethod
    def _canonical_uninstall_plan(
        *,
        extension_id: str,
        extension_name: str,
        extension_version: str,
        install_id: str,
        release_id: str,
        remove: list[dict[str, Any]],
        affected: list[dict[str, Any]],
        retained: list[dict[str, Any]],
    ) -> dict[str, Any]:
        canonical = {
            "extension_id": extension_id,
            "extension_name": extension_name,
            "extension_version": extension_version,
            "install_id": install_id,
            "release_id": release_id,
            "remove": remove,
            "affected": affected,
            "retained": retained,
        }
        revision = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {**canonical, "revision": revision}


__all__ = ["ExtensionUninstallPlanService"]
