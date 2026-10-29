"""Workflow authoring as shared operator commands for the CLI.

Every method delegates to the application service the HTTP route already uses:
the authoring facade for the schema and drafts, ``apply_workflow_changes`` for a
change set, ``WorkflowPortabilityService`` for export and import, and the job
store's workflow statistics. Reads need the workflow to be linked to the
project in the caller's scope; writes need deployment-admin authority, as the
admin-only HTTP routes do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from .errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)

_CONNECTION_ACTIONS = frozenset({"map_existing", "use_existing"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _portability_error(exc: Any) -> ServiceError:
    return ServiceError(str(exc), status_code=exc.status_code, payload=exc.detail())


def _persistence_error(exc: Exception) -> ServiceError:
    """A database refusal of the definition write, as the author can act on it."""
    message = str(exc)
    if "install_id_fkey" in message:
        return ServiceNotFoundError("Connector install not found")
    if "skill_id_fkey" in message:
        return ServiceNotFoundError("Skill not found")
    if "block_key_fkey" in message:
        return ServiceNotFoundError("Published block not found")
    return ServiceValidationError("Referenced workflow dependency not found")


logger = logging.getLogger(__name__)


class WorkflowOperatorAuthoringMixin:
    """Mixed into ``WorkflowOperatorService``; uses its scope and registry helpers."""

    _get_logger: Callable[[], Any]
    _registry_manager_provider: Callable[[], Any]
    _get_project_store: Callable[[], Any] | None
    _get_integration_store: Callable[[], Any] | None
    _get_install_store: Callable[[], Any] | None
    _get_job_store: Callable[[], Any] | None

    def _registry_db(self) -> Any:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _project_query(self) -> Any:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _require_project_workflow(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
        allow_disabled: bool = False,
    ) -> str:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _require_authoring_admin(self, scope: Any, action: str) -> None:
        """The HTTP request's own check, or the CLI's deployment-admin scope.

        An HTTP surface passes a request-bound authorizer: the caller must hold
        the administrator role and, with a token, ``admin:all`` — the check a
        draft save makes. Without one (the CLI), only a deployment-admin scope
        may author.
        """
        authorizer = getattr(self, "_authoring_authorizer", None)
        if authorizer is not None:
            authorizer(scope, action)
            return
        if scope is None or _text(getattr(scope, "principal_type", "")) != "admin":
            raise ServiceForbiddenError(f"{action} requires deployment-admin authority.")

    def _store(self, getter: Callable[[], Any] | None, name: str) -> Any:
        store = getter() if getter is not None else None
        if store is None:
            raise ServiceUnavailableError(f"{name} is unavailable")
        return store

    def _refresh_workflow_cache(self, *, reload_all: bool = False) -> bool:
        """Best effort: a committed change is never reported as failed by a cache.

        Returns False when the refresh failed, so a caller can say so.
        """
        registry = self._registry_manager_provider()
        refresh = getattr(registry, "reload_all" if reload_all else "refresh_workflow_cache", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.warning("Workflow cache refresh failed after committed mutation")
                return False
        return True

    def _authoring_service(self) -> Any:
        """The same facade ``/api/.../workflow-authoring`` builds, with CLI authority."""
        from core.application.workflows.authoring.service import WorkflowAuthoringService

        registry = self._registry_manager_provider()
        return WorkflowAuthoringService(
            self._registry_db(),
            project_store=self._store(self._get_project_store, "Project store"),
            project_connection_store=self._store(self._get_integration_store, "Integration store"),
            get_install_store=self._get_install_store,
            creation_authorizer=lambda scope: self._require_authoring_admin(
                scope, "Workflow creation"
            ),
            after_save=getattr(registry, "refresh_workflow_cache", None),
        )

    def _portability(self, *, account_id: str = "") -> Any:
        """The portability service as the admin workflow routes build it."""
        from core.application.workflows.workflow_portability import WorkflowPortabilityService
        from core.infrastructure.runtime.entrypoint_db_adapters import workflow_import_transaction

        db = self._registry_db()
        connection_store = self._get_integration_store() if self._get_integration_store else None
        return WorkflowPortabilityService(
            db,
            project_connection_store=connection_store,
            extension_install_store=connection_store,
            get_project_store=self._get_project_store,
            account_id=account_id,
            import_transaction_factory=(
                partial(workflow_import_transaction, db, connection_store)
                if connection_store is not None
                else None
            ),
        )

    def _scoped_workflow_row(
        self, project_id: Any, workflow_id: str, *, scope: Any
    ) -> tuple[str, dict[str, Any]]:
        from core.application.registry.workflow_lookup import get_workflow_row

        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        account_id = _text(getattr(scope, "account_id", ""))
        row = get_workflow_row(self._registry_db(), workflow_id, account_id=account_id)
        if not isinstance(row, dict):
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")
        if _text(row.get("account_id")) and _text(row.get("account_id")) != account_id:
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")
        return workflow_id, dict(row)

    # -- reads -------------------------------------------------------------

    def authoring_schema(self, *, step_kind: str | None = None, scope: Any = None) -> Any:
        del scope  # The schema is deployment-wide, as on GET /api/workflow-authoring/schema.
        return self._authoring_service().workflow_authoring_schema(step_kind=step_kind or None)

    def draft_validate(
        self,
        *,
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
        scope: Any,
    ) -> Any:
        return self._authoring_service().workflow_draft_validate(
            scope=scope, project_id=project_id, bundle_yaml=bundle_yaml, resolutions=resolutions
        )

    def workflow_definition(self, *, project_id: str, workflow_id: str, scope: Any) -> Any:
        """The definition ``GET /api/workflows/{workflow_id}`` returns."""
        from core.application.registry.boundary_contracts import (
            project_workflow_boundary_contracts,
        )
        from core.application.workflows.workflow_change_normalization import (
            normalize_workflow_participants,
        )

        _workflow_id, row = self._scoped_workflow_row(project_id, workflow_id, scope=scope)
        workflow = self._portability().redact_protected_workflow(row)
        settings = workflow.get("settings")
        if isinstance(settings, dict):
            workflow["participants"] = normalize_workflow_participants(settings.get("participants"))
        return project_workflow_boundary_contracts(workflow)

    def export_workflow(
        self,
        *,
        project_id: str,
        workflow_id: str,
        include_children: bool = False,
        reference_only: bool = False,
        scope: Any,
    ) -> dict[str, Any]:
        """The YAML bundle ``GET /api/workflows/{workflow_id}/export.yaml`` returns.

        Exported from the project, it also carries the project's bindings of the
        workflow's hooks, so an import elsewhere connects them again.
        """
        from core.application.workflows.workflow_portability import WorkflowPortabilityError

        workflow_id, row = self._scoped_workflow_row(project_id, workflow_id, scope=scope)
        account_id = _text(row.get("account_id")) or _text(getattr(scope, "account_id", ""))
        try:
            bundle_yaml = self._portability(account_id=account_id).export_yaml(
                workflow_id,
                include_children=include_children,
                project_id=_text(project_id),
                reference_only=reference_only,
            )
        except WorkflowPortabilityError as exc:
            raise _portability_error(exc) from exc
        return {
            "workflow_id": workflow_id,
            "filename": f"{workflow_id}.workflow.yaml",
            "include_children": bool(include_children),
            "bundle_yaml": bundle_yaml,
        }

    def workflow_stats(self, *, project_id: str, workflow_id: str, scope: Any) -> dict[str, Any]:
        """The statistics ``GET /api/workflows/{workflow_id}/stats`` returns."""
        workflow_id, _row = self._scoped_workflow_row(project_id, workflow_id, scope=scope)
        store = self._store(self._get_job_store, "Workflow statistics store")
        stats = store.get_workflow_stats(
            workflow_id=workflow_id, account_id=_text(getattr(scope, "account_id", ""))
        )
        return {"workflow_id": workflow_id, **dict(stats or {})}

    # -- writes ------------------------------------------------------------

    def draft_save(
        self,
        *,
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
        scope: Any,
    ) -> Any:
        self._require_authoring_admin(scope, "Workflow creation")
        return self._authoring_service().workflow_draft_save(
            scope=scope, project_id=project_id, bundle_yaml=bundle_yaml, resolutions=resolutions
        )

    def apply_changes(
        self,
        *,
        project_id: str,
        workflow_id: str,
        expected_revision: int,
        operations: list[dict[str, Any]],
        dry_run: bool = False,
        scope: Any,
    ) -> dict[str, Any]:
        """One atomic change set, as ``POST /api/workflows/{workflow_id}/changes``."""
        from core.application.registry.contract_validation import validate_workflow_readiness
        from core.application.registry.db_store_workflows_mixin import (
            WorkflowRevisionConflictError,
        )
        from core.application.workflows.workflow_change_authorization import (
            authorize_change_operations,
        )
        from core.application.workflows.workflow_change_normalization import (
            WorkflowChangeNormalizationError,
            normalize_change_operations,
        )
        from core.application.workflows.workflow_consumer_guard import (
            WorkflowConsumerConflictError,
        )
        from core.application.workflows.workflow_definition_service import (
            WorkflowDefinitionChangeError,
            apply_workflow_changes,
            readiness_from_validator,
        )
        from core.application.workflows.workflow_portability import WorkflowPortabilityError

        self._require_authoring_admin(scope, "Changing a workflow")
        workflow_id, workflow = self._scoped_workflow_row(project_id, workflow_id, scope=scope)
        if not _text(workflow.get("account_id")) and _text(workflow.get("scope_kind")) == "system":
            raise ServiceConflictError("platform_workflow_requires_clone")
        db = self._registry_db()
        try:
            self._portability().assert_structure_change_allowed_for(
                workflow, action="apply_changes"
            )
            normalized = normalize_change_operations(workflow, list(operations))
            authorize_change_operations(normalized, self._reference_access(db, scope, workflow))
            result = apply_workflow_changes(
                db,
                workflow_id,
                expected_revision=int(expected_revision),
                operations=normalized,
                author=_text(getattr(scope, "actor_name", "")) or "flow-steward-cli",
                readiness=readiness_from_validator(db, validate_workflow_readiness),
                dry_run=bool(dry_run),
            )
        except WorkflowPortabilityError as exc:
            raise _portability_error(exc) from exc
        except WorkflowChangeNormalizationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except WorkflowRevisionConflictError as exc:
            current = db.get_workflow(workflow_id) or {}
            raise ServiceConflictError(
                "The workflow changed after it was loaded. Reload before saving.",
                payload={
                    "code": "workflow_revision_conflict",
                    "workflow_id": exc.workflow_id,
                    "expected_revision": exc.expected_revision,
                    "revision": int(current.get("revision") or 0) or None,
                },
            ) from exc
        except WorkflowConsumerConflictError as exc:
            raise ServiceConflictError(
                str(exc),
                payload={
                    "code": "workflow_consumer_conflict",
                    "workflow_id": exc.workflow_id,
                    "consumers": exc.issues,
                },
            ) from exc
        except WorkflowDefinitionChangeError as exc:
            raise ServiceValidationError(
                str(exc), payload={"message": str(exc), "issues": list(exc.issues)}
            ) from exc
        except LookupError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        except ServiceError:
            raise
        except Exception as exc:
            if "violates foreign key" not in str(exc).lower():
                raise
            raise _persistence_error(exc) from exc
        if result.applied:
            # Simulation and preview read the registry; it must see the new revision.
            self._refresh_workflow_cache(reload_all=True)
        return {
            "workflow_id": workflow_id,
            "revision": result.revision,
            "valid": result.valid,
            "issues": result.issues,
            "workflow": result.workflow,
            "applied": result.applied,
            "blocking_issues": result.blocking_issues,
        }

    def _reference_access(self, db: Any, scope: Any, workflow: dict[str, Any]):
        """What the change set may reference: the workflow's account, linked projects."""
        from core.application.workflows.workflow_change_authorization import (
            store_reference_access,
        )

        account_id = _text(workflow.get("account_id")) or _text(getattr(scope, "account_id", ""))

        def project_visible(candidate: str) -> bool:
            try:
                self._project_query().get_workflow_bindings(candidate, scope=scope)
            except ServiceError:
                return False
            return True

        return store_reference_access(
            db,
            account_id=account_id,
            workflow_visible=lambda row: _text(row.get("account_id")) in {"", account_id},
            project_visible=project_visible,
        )

    def import_workflow(
        self,
        *,
        project_id: str,
        bundle_yaml: str,
        target_workflow_id: str | None = None,
        resolutions: list[dict[str, Any]] | None = None,
        scope: Any,
    ) -> dict[str, Any]:
        """Apply a bundle as disabled drafts, as ``POST /api/workflows/import/apply``."""
        from core.application.extensions.service_support import (
            get_default_extension_install_store,
        )
        from core.application.management.guards import require_default_workflow_catalog_account
        from core.application.workflows.workflow_portability import WorkflowPortabilityError
        from core.application.workflows.workflow_template_catalog_create import (
            resolve_portable_extensions,
        )

        self._require_authoring_admin(scope, "Importing a workflow")
        require_default_workflow_catalog_account(scope)
        project_id = _text(project_id)
        # The project must exist and be visible; its bindings are the visibility check.
        self._project_query().get_workflow_bindings(project_id, scope=scope)
        chosen = [dict(row) for row in resolutions or [] if isinstance(row, dict)]
        self._check_import_resolutions(project_id, chosen)
        account_id = _text(getattr(scope, "account_id", ""))
        service = self._portability(account_id=account_id)
        integration_store = self._get_integration_store() if self._get_integration_store else None
        try:
            try:
                install_store = get_default_extension_install_store(
                    get_logger=lambda: integration_store
                )
            except ServiceUnavailableError:
                merged = chosen
            else:
                merged, _outcomes, _discovery = resolve_portable_extensions(
                    service,
                    bundle_yaml,
                    target_workflow_id=_text(target_workflow_id),
                    resolutions=chosen,
                    account_id=account_id or "acc_default",
                    get_install_store=lambda: install_store,
                    target_project_id=project_id,
                )
            result = service.apply_import(
                bundle_yaml,
                target_workflow_id=_text(target_workflow_id),
                resolutions=merged,
                author=_text(getattr(scope, "actor_name", "")) or "flow-steward-cli",
                owner_account_id=account_id,
                owner_project_id=project_id,
            )
        except WorkflowPortabilityError as exc:
            raise _portability_error(exc) from exc
        if not self._refresh_workflow_cache(
            reload_all=bool((result.get("definitions") or {}).get("created"))
        ):
            from core.application.workflows.workflow_portability_common import (
                IMPORT_CACHE_REFRESH_WARNING,
            )

            result.setdefault("warnings", []).append(IMPORT_CACHE_REFRESH_WARNING)
        return result

    def _check_import_resolutions(self, project_id: str, resolutions: list[dict[str, Any]]) -> None:
        """A connection resolution stays inside the project the bundle is imported into."""
        for resolution in resolutions:
            dependency_type = _text(resolution.get("dependency_type") or resolution.get("type"))
            action = _text(resolution.get("action"))
            if not (
                action == "create_connection_placeholder"
                or dependency_type == "project_connection"
                or _text(resolution.get("dependency_key")).startswith("project_connection:")
            ):
                continue
            named_project = _text(resolution.get("project_id"))
            if named_project and named_project != project_id:
                raise ServiceForbiddenError("A connection resolution must use the import project.")
            connection_id = _text(
                resolution.get("target_ref")
                or resolution.get("target_id")
                or resolution.get("target_connection_id")
            )
            if action not in _CONNECTION_ACTIONS or not connection_id:
                continue
            if not named_project:
                raise ServiceValidationError(
                    "A project_connection resolution that maps an existing "
                    "connection must include project_id",
                    status_code=422,
                )
            store = self._store(self._get_integration_store, "Integration store")
            try:
                connection = store.get_project_connection(
                    project_id=project_id, connection_id=connection_id
                )
            except TypeError:
                connection = store.get_project_connection(connection_id=connection_id)
            if not isinstance(connection, dict) or _text(connection.get("project_id")) != (
                project_id
            ):
                raise ServiceForbiddenError("Access denied")


__all__ = ["WorkflowOperatorAuthoringMixin"]
