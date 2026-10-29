from __future__ import annotations

from typing import Any

from core.application.management.errors import ServiceConflictError

from .command_context import ExtensionCommandContext
from .extension_runtime import ExtensionRegistryProjectionRecord
from .service_support import _normalize_text


class ExtensionInstallProjectionService:
    """Publish install-owned projections and their registry bridges."""

    def __init__(self, *, context: ExtensionCommandContext) -> None:
        self._context = context

    def project_release(
        self,
        *,
        install_record: Any,
        release: Any,
        account_id: str,
    ) -> None:
        runtime_store = self._context.runtime_store()
        snapshots = (
            runtime_store.list_artifact_snapshots(release.release_id)
            if hasattr(runtime_store, "list_artifact_snapshots")
            else []
        )
        datasets = {
            _normalize_text(getattr(snapshot, "artifact_id", "")): dict(
                getattr(snapshot, "snapshot", {}) or {}
            )
            for snapshot in snapshots
            if _normalize_text(getattr(snapshot, "artifact_kind", "")) == "dataset"
        }
        from core.application.registry.saved_query_contract import (
            validate_saved_query_contract,
        )

        for snapshot in snapshots:
            if _normalize_text(getattr(snapshot, "artifact_kind", "")) != "saved_query":
                continue
            query = dict(getattr(snapshot, "snapshot", {}) or {})
            dataset_id = _normalize_text(query.get("dataset_id"))
            validate_saved_query_contract(query, dataset=datasets.get(dataset_id, {}))
        self._check_release_hooks(
            snapshots, install_record=install_record, release=release, account_id=account_id
        )
        for snapshot in snapshots:
            if not bool(getattr(snapshot, "publish", True)):
                continue
            runtime_store.upsert_projection(
                ExtensionRegistryProjectionRecord(
                    account_id=account_id,
                    install_id=install_record.install_id,
                    release_id=release.release_id,
                    extension_id=release.extension_id,
                    artifact_kind=snapshot.artifact_kind,
                    artifact_id=snapshot.artifact_id,
                    artifact_ref=snapshot.artifact_ref,
                    projection=dict(snapshot.snapshot or {}),
                    is_active=True,
                )
            )

    def _check_release_hooks(
        self, snapshots: list[Any], *, install_record: Any, release: Any, account_id: str
    ) -> None:
        """A release never breaks a bound hook; staged bindings activate with it."""
        from core.application.workflows.workflow_hook_release import (
            HookReleaseBlockedError,
            check_and_activate_release_hooks,
        )

        connection_provider = getattr(self._context, "persistence_connection", None)
        connection = connection_provider() if callable(connection_provider) else None
        if connection is None or not callable(getattr(connection, "cursor", None)):
            # In-memory lifecycle stores keep no bindings: nothing to check.
            return
        workflows = {
            _normalize_text(getattr(snapshot, "artifact_id", "")): dict(
                getattr(snapshot, "snapshot", {}) or {}
            )
            for snapshot in snapshots
            if _normalize_text(getattr(snapshot, "artifact_kind", "")) == "workflow"
        }
        try:
            check_and_activate_release_hooks(
                connection,
                account_id=account_id,
                install_id=install_record.install_id,
                release_id=release.release_id,
                workflows=workflows,
            )
        except HookReleaseBlockedError as exc:
            raise ServiceConflictError(str(exc), payload={"hooks": exc.issues}) from exc

    def materialize_dataset_bridges(
        self,
        *,
        install_record: Any,
        release: Any,
        account_id: str,
    ) -> None:
        registry_store = self._registry_store(
            required_methods=(
                "get_dataset",
                "save_dataset",
                "get_dataset_query_template",
                "save_dataset_query_template",
                "list_datasets",
                "list_dataset_query_templates",
                "purge_dataset",
                "purge_dataset_query_template",
            )
        )
        if registry_store is None:
            return
        runtime_store = self._context.runtime_store()
        projections = [
            row
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
            if _normalize_text(getattr(row, "install_id", ""))
            == _normalize_text(install_record.install_id)
            and _normalize_text(getattr(row, "artifact_kind", ""))
            in {"dataset", "saved_query", "skill"}
        ]
        self._materialize_with_transaction(
            registry_store=registry_store,
            install_record=install_record,
            release=release,
            account_id=account_id,
            projections=projections,
        )

    def _materialize_with_transaction(
        self,
        *,
        registry_store: Any,
        install_record: Any,
        release: Any,
        account_id: str,
        projections: list[Any],
    ) -> None:
        from core.application.extensions.projected_dataset_bridge import (
            materialize_projected_dataset_bridges,
        )

        def materialize() -> None:
            materialize_projected_dataset_bridges(
                registry_store,
                account_id=account_id,
                install_id=install_record.install_id,
                release_id=release.release_id,
                projections=projections,
            )
            from core.application.extensions.projected_skill_bridge import (
                materialize_projected_skill_anchors,
            )

            materialize_projected_skill_anchors(
                registry_store,
                account_id=account_id,
                install_id=install_record.install_id,
                release_id=release.release_id,
                projections=projections,
            )

        if self._run_with_lifecycle_connection(registry_store, materialize):
            return
        transaction = getattr(registry_store, "_transaction", None)
        if callable(transaction):
            with transaction():
                materialize()
            return
        materialize()

    def remove_dataset_bridges(self, *, install_id: str, account_id: str = "") -> None:
        normalized_install_id = _normalize_text(install_id)
        if not normalized_install_id:
            return
        registry_store = self._registry_store(
            required_methods=(
                "list_datasets",
                "list_dataset_query_templates",
                "purge_dataset",
                "purge_dataset_query_template",
            )
        )
        if registry_store is None:
            return
        from core.application.extensions.projected_dataset_bridge import (
            remove_projected_dataset_bridges,
        )

        def remove() -> None:
            remove_projected_dataset_bridges(registry_store, install_id=normalized_install_id)
            if account_id:
                from core.application.extensions.projected_skill_bridge import (
                    remove_projected_skill_anchors,
                )

                remove_projected_skill_anchors(
                    registry_store,
                    account_id=account_id,
                    install_id=normalized_install_id,
                )

        if not self._run_with_lifecycle_connection(registry_store, remove):
            remove()

    def deactivate_projected_skills(self, *, install_id: str, account_id: str) -> None:
        registry_store = self._registry_store(required_methods=())
        deactivate = getattr(registry_store, "deactivate_projected_skill_anchors", None)
        if not callable(deactivate):
            return

        def apply() -> None:
            deactivate(account_id=account_id, install_id=install_id)

        if not self._run_with_lifecycle_connection(registry_store, apply):
            apply()

    def _registry_store(self, *, required_methods: tuple[str, ...]) -> Any | None:
        logger = self._context.logger()
        registry_store = getattr(logger, "db_store", None) if logger is not None else None
        if registry_store is None:
            dsn = getattr(logger, "dsn", "") if logger is not None else ""
            if isinstance(dsn, str) and dsn.strip():
                from core.application.registry.db_store import RegistryDBStore

                registry_store = RegistryDBStore(dsn)
        if registry_store is None or not all(
            callable(getattr(registry_store, name, None)) for name in required_methods
        ):
            return None
        return registry_store

    def _run_with_lifecycle_connection(
        self,
        registry_store: Any,
        operation: Any,
    ) -> bool:
        lifecycle_connection = self._context.persistence_connection()
        set_tx_conn = getattr(registry_store, "_set_tx_conn", None)
        if not callable(set_tx_conn) or lifecycle_connection is None:
            return False
        from core.infrastructure.db.db_pool import active_transaction_connection

        set_tx_conn(active_transaction_connection(lifecycle_connection) or lifecycle_connection)
        try:
            operation()
        finally:
            set_tx_conn(None)
        return True
