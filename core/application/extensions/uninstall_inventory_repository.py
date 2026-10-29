from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID

from .oauth_uninstall_inventory import collect_oauth_configuration_inventory
from .semver import compare_semver
from .uninstall_connection_inventory import collect_uninstall_connection_rows as _connection_rows
from .uninstall_descriptor_dependencies import collect_descriptor_only_dependents
from .uninstall_inventory_queries import (
    artifact_display_name,
    collect_database_inventory,
    finalize_inventory,
    uninstall_inventory_row,
)
from .uninstall_llm_model_dependents import collect_llm_model_dependents
from .uninstall_projected_skill_dependents import collect_projected_skill_dependents
from .uninstall_workflow_dependents import collect_workflow_dependents


def install_visibility(record: Any) -> str:
    visibility = getattr(record, "visibility", "account")
    return str(getattr(visibility, "value", visibility) or "account").strip().lower()


def dependency_has_available_alternative_install(
    *,
    install_store: Any,
    account_id: str,
    removed_install_id: str,
    dependency: dict[str, Any],
) -> bool:
    extension_id = str(dependency.get("extension_id") or "").strip()
    if not extension_id or not hasattr(install_store, "list_for_account"):
        return False
    try:
        candidates = install_store.list_for_account(
            str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
            include_global=True,
            include_inactive_owned=False,
        )
    except Exception:
        return False
    minimum = str(dependency.get("min_version") or "").strip()
    maximum = str(dependency.get("max_version_exclusive") or "").strip()
    for candidate in candidates:
        if _candidate_satisfies_dependency(
            candidate,
            extension_id=extension_id,
            removed_install_id=removed_install_id,
            minimum=minimum,
            maximum=maximum,
        ):
            return True
    return False


def _candidate_satisfies_dependency(
    candidate: Any,
    *,
    extension_id: str,
    removed_install_id: str,
    minimum: str,
    maximum: str,
) -> bool:
    if str(getattr(candidate, "install_id", "") or "").strip() == removed_install_id:
        return False
    if str(getattr(candidate, "extension_id", "") or "").strip() != extension_id:
        return False
    version = str(getattr(candidate, "version", "") or "").strip()
    try:
        return not (
            (minimum and compare_semver(version, minimum, field_name="extension version") < 0)
            or (maximum and compare_semver(version, maximum, field_name="extension version") >= 0)
        )
    except ValueError:
        return False


class ExtensionUninstallInventoryRepository:
    """Queries the complete revision-bearing uninstall inventory."""

    def collect(
        self,
        *,
        bl: Any,
        extension_id: str,
        install_id: str,
        release_id: str,
        account_id: str,
        include_consumer_accounts: bool,
        runtime_store: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        snapshots = (
            runtime_store.list_artifact_snapshots(release_id)
            if release_id and hasattr(runtime_store, "list_artifact_snapshots")
            else []
        )
        projections = self._install_projections(runtime_store, account_id, install_id)
        remove = self._collect_removals(
            bl=bl,
            extension_id=extension_id,
            install_id=install_id,
            release_id=release_id,
            account_id=account_id,
            include_consumer_accounts=include_consumer_accounts,
            projections=projections,
        )
        retained, shared_package, shared_release = collect_database_inventory(
            bl=bl,
            extension_id=extension_id,
            install_id=install_id,
            release_id=release_id,
            account_id=account_id,
            include_consumer_accounts=include_consumer_accounts,
            remove=remove,
        )
        return finalize_inventory(
            bl=bl,
            extension_id=extension_id,
            install_id=install_id,
            release_id=release_id,
            include_consumer_accounts=include_consumer_accounts,
            projections=projections,
            snapshots=snapshots,
            remove=remove,
            retained=retained,
            shared_runtime_package=shared_package,
            shared_release=shared_release,
        )

    @staticmethod
    def _install_projections(runtime_store: Any, account_id: str, install_id: str) -> list[Any]:
        if not hasattr(runtime_store, "list_projections"):
            return []
        return [
            row
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
            if str(getattr(row, "install_id", "") or "").strip() == install_id
        ]

    def _collect_removals(
        self,
        *,
        bl: Any,
        extension_id: str,
        install_id: str,
        release_id: str,
        account_id: str,
        include_consumer_accounts: bool,
        projections: list[Any],
    ) -> list[dict[str, Any]]:
        connections = _connection_rows(
            bl,
            account_id=account_id,
            extension_id=extension_id,
            install_id=install_id,
            include_consumer_accounts=include_consumer_accounts,
        )
        remove: list[dict[str, Any]] = [
            {"kind": "runtime_package", "id": extension_id, "count": 1},
            {"kind": "release", "id": release_id, "count": 1 if release_id else 0},
            {"kind": "projection", "id": install_id, "count": len(projections)},
        ]
        remove.extend(self._projection_items(projections))
        workspace_rows = self._workspace_connection_rows(bl, install_id)
        remove.extend(self._workspace_items(workspace_rows))
        remove.extend(self._workspace_dataset_items(bl, workspace_rows))
        remove.extend(self._connection_items(connections, install_id, include_consumer_accounts))
        remove.extend(
            collect_oauth_configuration_inventory(
                bl,
                account_id=account_id,
                extension_id=extension_id,
                install_id=install_id,
                include_consumer_accounts=include_consumer_accounts,
            )
        )
        return remove

    @staticmethod
    def _projection_items(projections: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in projections:
            artifact_kind = str(getattr(row, "artifact_kind", "") or "").strip()
            artifact_id = str(getattr(row, "artifact_id", "") or "").strip()
            items.append(
                {
                    "kind": "projection_record",
                    "id": str(getattr(row, "projection_id", "") or "").strip()
                    or f"{artifact_kind}:{artifact_id}",
                    "artifact_kind": artifact_kind,
                    "artifact_id": artifact_id,
                    "display_name": artifact_display_name(
                        getattr(row, "projection", None), artifact_id
                    ),
                    "count": 1,
                }
            )
        return items

    @staticmethod
    def _workspace_connection_rows(bl: Any, install_id: str) -> list[dict[str, Any]]:
        if not callable(getattr(bl, "cursor", None)) or not getattr(
            bl, "_table_exists", lambda *_args: False
        )("workspace_data_connections"):
            return []
        with bl.cursor() as cur:
            cur.execute(
                "SELECT connection_id, display_name FROM workspace_data_connections WHERE deleted_at IS NULL AND connector_ref_jsonb->>'install_id' = %s ORDER BY connection_id",
                (install_id,),
            )
            return [
                uninstall_inventory_row(row, ("connection_id", "display_name"))
                for row in cur.fetchall() or []
            ]

    @staticmethod
    def _workspace_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "kind": "workspace_data_connection",
                "id": str(row.get("connection_id") or "").strip(),
                "display_name": str(
                    row.get("display_name") or row.get("connection_id") or ""
                ).strip(),
                "count": 1,
            }
            for row in rows
            if str(row.get("connection_id") or "").strip()
        ]

    @staticmethod
    def _workspace_dataset_items(bl: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [
            str(row.get("connection_id") or "").strip()
            for row in rows
            if str(row.get("connection_id") or "").strip()
        ]
        if (
            not ids
            or not callable(getattr(bl, "cursor", None))
            or not getattr(bl, "_table_exists", lambda *_args: False)("registry_datasets")
        ):
            return []
        with bl.cursor() as cur:
            cur.execute(
                "SELECT dataset_id, display_name FROM registry_datasets WHERE metadata_jsonb->>'connection_id' = ANY(%s) ORDER BY dataset_id",
                (ids,),
            )
            return [
                {
                    "kind": "workspace_connection_dataset",
                    "id": str(row.get("dataset_id") if isinstance(row, dict) else row[0]),
                    "display_name": str(
                        row.get("display_name") if isinstance(row, dict) else row[1]
                    ),
                    "count": 1,
                }
                for row in cur.fetchall() or []
            ]

    @staticmethod
    def _connection_items(
        rows: list[dict[str, Any]],
        install_id: str,
        include_consumers: bool,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("connection_id") or "").strip()
        ]
        credential_count = sum(
            len(dict(row.get("secret_refs") or {}).get("refs") or {})
            for row in rows
            if isinstance(row.get("secret_refs"), dict)
        )
        if include_consumers:
            items = (
                [
                    {
                        "kind": "consumer_runtime_connections",
                        "id": install_id,
                        "display_name": "Consumer project connections",
                        "count": len(rows),
                    }
                ]
                if rows
                else []
            )
            if credential_count:
                items.append(
                    {
                        "kind": "consumer_connection_credential_refs",
                        "id": install_id,
                        "display_name": "Consumer connection credentials",
                        "count": credential_count,
                    }
                )
            return items
        items = [
            {
                "kind": "project_connection",
                "id": str(row.get("connection_id") or "").strip(),
                "display_name": str(
                    row.get("display_name") or row.get("connection_id") or ""
                ).strip(),
                **(
                    {"project_id": str(row.get("project_id") or "").strip()}
                    if str(row.get("project_id") or "").strip()
                    else {}
                ),
                "count": 1,
            }
            for row in rows
        ]
        items.extend(
            {
                "kind": "connection_credential_refs",
                "id": f"{str(row.get('connection_id') or '').strip()}:credentials",
                "display_name": str(row.get("display_name") or "Connection credentials"),
                "count": len(dict(row.get("secret_refs") or {}).get("refs") or {}),
            }
            for row in rows
            if isinstance(row.get("secret_refs"), dict)
            and bool(dict(row.get("secret_refs") or {}).get("refs"))
        )
        return items

    def collect_affected(
        self,
        *,
        bl: Any,
        extension_id: str,
        install_id: str,
        account_id: str,
        include_consumer_accounts: bool,
        runtime_store: Any,
        install_store: Any,
        extension_name: str = "",
    ) -> list[dict[str, Any]]:
        rows = self._projection_rows(
            bl=bl,
            account_id=account_id,
            include_consumer_accounts=include_consumer_accounts,
            runtime_store=runtime_store,
        )
        affected: list[dict[str, Any]] = []
        for row in rows:
            dependency = self._required_dependency(
                row=row,
                extension_id=extension_id,
                removed_install_id=install_id,
                account_id=account_id,
                install_store=install_store,
            )
            if row["install_id"] != install_id and dependency is not None:
                affected.append(
                    {
                        "kind": row["artifact_kind"] or "artifact",
                        "id": row["artifact_id"],
                        "display_name": artifact_display_name(row["projection"]),
                        "owner_install_id": row["install_id"],
                        "owner_account_id": row["account_id"],
                        "action": "preserve_and_disable",
                        "reason": f"Missing required extension: {extension_id}",
                    }
                )
        affected.extend(
            collect_descriptor_only_dependents(
                extension_id=extension_id,
                removed_install_id=install_id,
                account_id=account_id,
                include_consumer_accounts=include_consumer_accounts,
                runtime_store=runtime_store,
                install_store=install_store,
                projected_install_ids={
                    str(item.get("owner_install_id") or "").strip() for item in affected
                },
                alternative_install_available=dependency_has_available_alternative_install,
            )
        )
        affected.extend(
            collect_workflow_dependents(
                bl,
                extension_id=extension_id,
                extension_label=extension_name,
                install_id=install_id,
                account_id=account_id,
                include_consumer_accounts=include_consumer_accounts,
            )
        )
        affected.extend(
            collect_projected_skill_dependents(bl, install_id=install_id, account_id=account_id)
        )
        affected.extend(
            collect_llm_model_dependents(
                bl,
                install_id=install_id,
                account_id=account_id,
                include_consumer_accounts=include_consumer_accounts,
            )
        )
        return affected

    @staticmethod
    def _projection_rows(
        *,
        bl: Any,
        account_id: str,
        include_consumer_accounts: bool,
        runtime_store: Any,
    ) -> list[dict[str, Any]]:
        if (
            include_consumer_accounts
            and callable(getattr(bl, "cursor", None))
            and getattr(bl, "_table_exists", lambda *_args: False)("extension_registry_projections")
        ):
            with bl.cursor() as cur:
                cur.execute(
                    "SELECT account_id::text AS account_id, install_id, artifact_kind, artifact_id, projection_jsonb FROM extension_registry_projections"
                )
                raw_rows = cur.fetchall() or []
            return [
                {
                    "account_id": str(row.get("account_id") or "").strip(),
                    "install_id": str(row.get("install_id") or "").strip(),
                    "artifact_kind": str(row.get("artifact_kind") or "").strip(),
                    "artifact_id": str(row.get("artifact_id") or "").strip(),
                    "projection": dict(row.get("projection_jsonb") or {}),
                }
                for row in (
                    uninstall_inventory_row(
                        raw,
                        (
                            "account_id",
                            "install_id",
                            "artifact_kind",
                            "artifact_id",
                            "projection_jsonb",
                        ),
                    )
                    for raw in raw_rows
                )
            ]
        if not hasattr(runtime_store, "list_projections"):
            return []
        return [
            {
                "account_id": str(getattr(row, "account_id", "") or "").strip(),
                "install_id": str(getattr(row, "install_id", "") or "").strip(),
                "artifact_kind": str(getattr(row, "artifact_kind", "") or "").strip(),
                "artifact_id": str(getattr(row, "artifact_id", "") or "").strip(),
                "projection": dict(getattr(row, "projection", {}) or {}),
            }
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
        ]

    @staticmethod
    def _required_dependency(
        *,
        row: dict[str, Any],
        extension_id: str,
        removed_install_id: str,
        account_id: str,
        install_store: Any,
    ) -> dict[str, Any] | None:
        dependencies = dict(row["projection"].get("dependencies") or {}).get("extensions")
        if not isinstance(dependencies, list):
            return None
        return next(
            (
                dict(item)
                for item in dependencies
                if isinstance(item, dict)
                and str(item.get("extension_id") or "").strip() == extension_id
                and bool(item.get("required", True))
                and not dependency_has_available_alternative_install(
                    install_store=install_store,
                    account_id=row["account_id"] or account_id,
                    removed_install_id=removed_install_id,
                    dependency=dict(item),
                )
            ),
            None,
        )


__all__ = [
    "ExtensionUninstallInventoryRepository",
    "dependency_has_available_alternative_install",
    "install_visibility",
]
