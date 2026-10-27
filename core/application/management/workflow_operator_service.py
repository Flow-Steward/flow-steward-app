# mypy: ignore-errors
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.registry_manager import RegistryManager
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows import workflow_entrypoint_repository
from core.application.workflows.workflow_availability_service import WorkflowAvailabilityService

from .errors import ServiceNotFoundError, ServiceScopeNotFoundError, ServiceUnavailableError
from .query_models import (
    ProjectWorkflowCatalogView,
    ProjectWorkflowEntrypointsView,
    WorkflowCatalogItemView,
    WorkflowContractResultView,
    WorkflowEntrypointView,
    WorkflowSuggestionsResultView,
    WorkflowValidationIssueView,
    WorkflowValidationResultView,
)
from .workflow_operator_authoring import WorkflowOperatorAuthoringMixin
from .workflow_operator_hooks import WorkflowOperatorHooksMixin

logger = logging.getLogger(__name__)

_WORKFLOW_STEP_PATH_RE = re.compile(r"workflow\.steps\.([^.\[]+)(?:\.(.+))?$")


class _WorkflowUnavailableError(ServiceNotFoundError):
    """Keep read-catalog availability distinct while preserving concealed 404s."""

    def __init__(self, workflow_id: str, availability_status: str) -> None:
        super().__init__(f"Workflow '{workflow_id}' not available")
        self.availability_status = availability_status or "unavailable"


class WorkflowOperatorService(WorkflowOperatorHooksMixin, WorkflowOperatorAuthoringMixin):
    """Shared workflow operator surfaces used by MCP, API, and CLI."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        project_query_service: Callable[[], Any] | None,
        registry_manager_provider: Callable[[], Any] | None = None,
        registry_db_provider: Callable[[], Any] | None = None,
        workflow_availability_service: Any | None = None,
        get_project_store: Callable[[], Any] | None = None,
        get_integration_store: Callable[[], Any] | None = None,
        get_install_store: Callable[[], Any] | None = None,
        get_job_store: Callable[[], Any] | None = None,
        authoring_authorizer: Callable[[Any, str], None] | None = None,
    ) -> None:
        self._get_logger = get_logger
        # An HTTP request's own authoring check; without one only the CLI's admin scope authors.
        self._authoring_authorizer = authoring_authorizer
        # Stores of the authoring commands; a surface that offers none leaves them unset.
        self._get_project_store = get_project_store
        self._get_integration_store = get_integration_store
        self._get_install_store = get_install_store
        self._get_job_store = get_job_store
        self._project_query_service = project_query_service
        self._registry_manager_provider = registry_manager_provider or RegistryManager.instance
        self._registry_db_provider = registry_db_provider
        self._workflow_availability_service = workflow_availability_service

    def _project_query(self):
        if self._project_query_service is None:
            raise RuntimeError("Project query service is unavailable")
        return self._project_query_service()

    def convert_group(self, *, project_id, scope, dry_run=True, preview_digest="", **arguments):
        """Deployment-admin command adapter to the same atomic authoring service."""
        from core.application.management.errors import ServiceError, ServiceForbiddenError
        from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
        from core.application.workflows.workflow_change_authorization import store_reference_access
        from core.application.workflows.workflow_group_extraction_service import (
            GroupExtractionError,
            GroupExtractionService,
        )
        from core.application.workflows.workflow_portability import WorkflowPortabilityError

        if scope is None or scope.principal_type != "admin":
            raise ServiceForbiddenError("Workflow conversion requires deployment-admin authority.")
        self._require_project_workflow(
            project_id, arguments["workflow_id"], scope=scope, allow_disabled=True
        )
        db = self._registry_db()
        reaction_store = self._get_logger()
        if reaction_store is None:
            raise ServiceUnavailableError("Workflow reaction validation is unavailable.")

        def project_visible(candidate):
            try:
                self._project_query().get_workflow_bindings(candidate, scope=scope)
                return True
            except ServiceError:
                return False

        service = GroupExtractionService(
            db,
            account_id=scope.account_id,
            reference_access=store_reference_access(
                db,
                account_id=scope.account_id,
                workflow_visible=lambda _row: True,
                project_visible=project_visible,
            ),
            project_visible=project_visible,
            reactions=lambda workflow_id, account_id: reaction_store.list_workflow_reactions(
                workflow_id=workflow_id, account_id=account_id, enabled_only=False
            ),
        )
        try:
            if dry_run:
                return service.preview(**arguments)
            return service.apply(
                **arguments, preview_digest=preview_digest, author=scope.actor_name
            )
        except WorkflowRevisionConflictError as exc:
            raise ServiceError(
                "Workflow revision changed; reload and preview again.", status_code=409
            ) from exc
        except GroupExtractionError as exc:
            raise ServiceError(
                str(exc),
                status_code=exc.status_code,
                payload={"message": str(exc), "issues": exc.issues},
            ) from exc
        except WorkflowPortabilityError as exc:
            raise ServiceError(str(exc), status_code=exc.status_code, payload=exc.detail()) from exc

    def _linked_project_workflow_ids(
        self,
        project_id: Any,
        *,
        scope: Any = None,
    ) -> set[str]:
        bindings = self._project_query().get_workflow_bindings(project_id, scope=scope)
        return {
            str(row.get("workflow_id") or "").strip()
            for row in (bindings.project_workflows or [])
            if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
        }

    def _require_project_workflow(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
        allow_disabled: bool = False,
    ) -> str:
        normalized_workflow_id = str(workflow_id or "").strip()
        allowed_workflow_ids = frozenset(getattr(scope, "allowed_workflow_ids", ()) or ())
        if allowed_workflow_ids and normalized_workflow_id not in allowed_workflow_ids:
            raise ServiceScopeNotFoundError("Workflow not linked to project")
        bindings = self._project_query().get_workflow_bindings(project_id, scope=scope)
        binding = next(
            (
                row
                for row in (bindings.project_workflows or [])
                if isinstance(row, dict)
                and str(row.get("workflow_id") or "").strip() == normalized_workflow_id
            ),
            None,
        )
        if binding is None:
            raise ServiceNotFoundError("Workflow not linked to project")
        availability_status = str(binding.get("availability_status") or "available").lower()
        disabled_draft = allow_disabled and (
            str(binding.get("availability_reason_code") or "").lower() == "workflow_disabled"
        )
        if not disabled_draft and (
            not bool(binding.get("enabled", True))
            or bool(binding.get("new_runs_blocked"))
            or availability_status != "available"
        ):
            raise ServiceNotFoundError("Workflow not available for project")
        return normalized_workflow_id

    def _registry_db(self):
        if self._registry_db_provider is not None:
            db_store = self._registry_db_provider()
            if db_store is None:
                raise ServiceUnavailableError("Workflow registry backend is unavailable")
            return db_store
        registry_manager = self._registry_manager_provider()
        db_store = getattr(registry_manager, "db_store", None)
        if db_store is None:
            raise ServiceUnavailableError("Workflow registry backend is unavailable")
        return db_store

    def _workflow_availability(self):
        if self._workflow_availability_service is None:
            self._workflow_availability_service = WorkflowAvailabilityService(
                get_logger=self._registry_db
            )
        return self._workflow_availability_service

    def _workflow_account_id(self, workflow_id: str) -> str:
        workflow = self._registry_db().get_workflow(workflow_id)
        if not isinstance(workflow, dict):
            return DEFAULT_ACCOUNT_ID
        return str(workflow.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID

    def _require_effectively_available_workflow(self, workflow_id: str) -> str:
        normalized_workflow_id = str(workflow_id or "").strip()
        availability = self._workflow_availability().get_workflow_availability(
            normalized_workflow_id,
            account_id=self._workflow_account_id(normalized_workflow_id),
        )
        if str(
            availability.get("availability_status") or ""
        ).strip().lower() != "available" or bool(availability.get("new_runs_blocked")):
            if availability.get("availability_reason_code") == "workflow_not_found":
                raise ServiceNotFoundError(f"Workflow '{normalized_workflow_id}' not found")
            raise _WorkflowUnavailableError(
                normalized_workflow_id,
                str(availability.get("availability_status") or "unavailable").strip().lower(),
            )
        return normalized_workflow_id

    def _workflow_validation_payload(
        self, workflow_id: str, *, require_available: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from core.application.registry.contract_validation import validate_workflow_readiness

        workflow_id = (
            self._require_effectively_available_workflow(workflow_id)
            if require_available
            else str(workflow_id or "").strip()
        )
        db = self._registry_db()
        workflow = db.get_workflow(workflow_id)
        if workflow is None:
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")
        try:
            validation = validate_workflow_readiness(db, workflow_id)
        except Exception as exc:
            logger.warning(
                "Workflow contract validation temporarily unavailable for '%s': %s",
                workflow_id,
                exc,
            )
            validation = {
                "valid": False,
                "errors": [
                    {
                        "code": "workflow.validation_temporarily_unavailable",
                        "path": "workflow",
                        "message": (
                            "Contract validation is temporarily unavailable due to high load. "
                            "Retry in a few seconds."
                        ),
                    }
                ],
                "warnings": [],
            }
        return dict(workflow), dict(validation or {})

    def _workflow_contract_payload(
        self, workflow_id: str, *, require_available: bool = True
    ) -> dict[str, Any]:
        """Project one workflow's public interface contract.

        `require_available` is what separates running from editing. An execution
        path must refuse a workflow that cannot accept new runs; a scoped
        diagnostic read or an author opening a disabled draft is not asking to
        run it. Existence and caller scope are still required either way.
        """
        workflow_id = (
            self._require_effectively_available_workflow(workflow_id)
            if require_available
            else str(workflow_id or "").strip()
        )
        registry_manager = self._registry_manager_provider()
        workflow_registry = getattr(registry_manager, "workflows", None)
        projected_workflow = (
            workflow_registry.get_workflow(workflow_id)
            if workflow_registry is not None
            and callable(getattr(workflow_registry, "get_workflow", None))
            else None
        )
        db_workflow = None
        if projected_workflow is None:
            db_workflow = self._registry_db().get_workflow(workflow_id)
        if projected_workflow is None and db_workflow is None:
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")

        if projected_workflow is not None:
            resolved_workflow_id = str(
                getattr(projected_workflow, "workflow_id", "") or workflow_id
            ).strip()
            display_name = str(
                getattr(projected_workflow, "display_name", "") or workflow_id
            ).strip()
            enabled = bool(getattr(projected_workflow, "enabled", False))
        else:
            workflow = db_workflow if isinstance(db_workflow, dict) else {}
            resolved_workflow_id = str(workflow.get("workflow_id") or workflow_id).strip()
            display_name = str(workflow.get("display_name") or workflow_id).strip()
            enabled = bool(workflow.get("enabled", False))

        service = WorkflowInterfaceService()
        interface = service.interface_from_definition(
            projected_workflow if projected_workflow is not None else db_workflow
        )
        return {
            "workflow_id": resolved_workflow_id,
            "display_name": display_name,
            "enabled": enabled,
            "interface": service.public_contract_payload(interface),
            "definition_issues": interface.definition_issues(),
        }

    def _workflow_validation_issues(
        self,
        workflow: dict[str, Any] | None,
        validation: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        workflow_obj = workflow if isinstance(workflow, dict) else {}
        step_ids = {
            str(step.get("step_id") or "").strip()
            for step in workflow_obj.get("steps") or []
            if isinstance(step, dict)
        }
        payload = validation if isinstance(validation, dict) else {}
        items: list[dict[str, Any]] = []

        for bucket_name, fallback_severity in (("errors", "error"), ("warnings", "warning")):
            for raw_issue in payload.get(bucket_name) or []:
                if not isinstance(raw_issue, dict):
                    continue
                path = str(raw_issue.get("path") or "").strip()
                field = ""
                step_id = ""
                match = _WORKFLOW_STEP_PATH_RE.match(path)
                if match and match.group(1) in step_ids:
                    step_id = match.group(1)
                    suffix = str(match.group(2) or "").strip()
                    if suffix:
                        field = suffix.split(".", 1)[0]
                items.append(
                    {
                        "type": str(raw_issue.get("code") or fallback_severity),
                        "code": str(raw_issue.get("code") or "").strip(),
                        "severity": str(raw_issue.get("severity") or fallback_severity).strip(),
                        "message": str(raw_issue.get("message") or "").strip(),
                        "path": path,
                        "step_id": step_id or None,
                        "field": field or None,
                    }
                )
        return items

    def _workflow_repair_suggestions(
        self,
        workflow_id: str,
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not issues:
            return []

        from core.application.registry.io_resolver import IOResolver

        db = self._registry_db()
        resolver = IOResolver(db)
        suggestions: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            step_id = str(issue.get("step_id") or "").strip()
            field = str(issue.get("field") or "").strip()
            path = str(issue.get("path") or "").strip()
            expected_input_name = ""
            if ".inputs." in path:
                expected_input_name = str(path.rsplit(".inputs.", 1)[-1]).strip()
            if step_id and (field == "boundary_contract" or expected_input_name):
                defaults = resolver.get_step_input_defaults(workflow_id, step_id)
                current_inputs = {
                    str(item.get("name") or "").strip()
                    for item in (defaults.get("current_expected_inputs") or [])
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                }
                for row in defaults.get("required_inputs") or []:
                    if not isinstance(row, dict):
                        continue
                    input_name = str(row.get("name") or "").strip()
                    if not input_name:
                        continue
                    if expected_input_name and input_name != expected_input_name:
                        continue
                    suggested_sources = [
                        str(item or "").strip()
                        for item in (row.get("suggested_sources") or [])
                        if str(item or "").strip()
                    ]
                    suggested_source = str(row.get("source") or "").strip()
                    if not suggested_source and suggested_sources:
                        suggested_source = suggested_sources[0]
                    suggested_value = {
                        "name": input_name,
                        "type": str(row.get("type") or "text").strip() or "text",
                        "source": suggested_source,
                        "required": True,
                    }
                    dedupe_key = (
                        "upsertExpectedInput",
                        step_id,
                        input_name,
                        str(suggested_value.get("source") or ""),
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    suggestions.append(
                        {
                            "action": "upsertExpectedInput",
                            "step_id": step_id,
                            "field": "boundary_contract.entry",
                            "input_name": input_name,
                            "suggested_value": suggested_value,
                            "reason": str(issue.get("message") or "").strip(),
                            "already_present": input_name in current_inputs,
                        }
                    )

        if suggestions:
            return suggestions

        return [
            {
                "action": "reviewIssue",
                "step_id": issue.get("step_id"),
                "field": issue.get("field"),
                "suggested_value": None,
                "reason": str(issue.get("message") or str(issue)).strip(),
            }
            for issue in issues
            if isinstance(issue, dict)
        ]

    def list_project_workflows(
        self,
        project_id: Any,
        *,
        enabled_only: bool = True,
        include_validation: bool = False,
        scope: Any = None,
    ) -> ProjectWorkflowCatalogView:
        bindings = self._project_query().get_workflow_bindings(project_id, scope=scope)
        workflows: list[WorkflowCatalogItemView] = []
        for row in bindings.project_workflows or []:
            if not isinstance(row, dict):
                continue
            new_runs_blocked = bool(row.get("new_runs_blocked"))
            effectively_enabled = bool(row.get("enabled")) and not new_runs_blocked
            if enabled_only and not effectively_enabled:
                continue
            workflow_id = str(row.get("workflow_id") or "").strip()
            if not workflow_id:
                continue
            item = WorkflowCatalogItemView(
                workflow_id=workflow_id,
                display_name=str(
                    row.get("workflow_display_name") or row.get("display_name") or workflow_id
                ).strip(),
                description=str(row.get("description") or "").strip(),
                enabled=effectively_enabled,
                availability_status=str(row.get("availability_status") or "").strip(),
                new_runs_blocked=new_runs_blocked,
            )
            if include_validation:
                try:
                    _workflow, validation = self._workflow_validation_payload(workflow_id)
                except _WorkflowUnavailableError as exc:
                    item.enabled = False
                    item.availability_status = exc.availability_status
                    item.new_runs_blocked = True
                    item.validation = {
                        "valid": False,
                        "errors": [
                            {
                                "code": "workflow.unavailable",
                                "path": "workflow",
                                "message": str(exc),
                                "severity": "error",
                            }
                        ],
                        "warnings": [],
                    }
                except ServiceNotFoundError as exc:
                    logger.warning(
                        "Project %s has stale workflow binding for missing workflow '%s'",
                        project_id,
                        workflow_id,
                    )
                    item.enabled = False
                    item.availability_status = "missing"
                    item.new_runs_blocked = True
                    item.validation = {
                        "valid": False,
                        "errors": [
                            {
                                "code": "workflow.not_found",
                                "path": "workflow",
                                "message": str(exc),
                                "severity": "error",
                            }
                        ],
                        "warnings": [],
                    }
                else:
                    item.validation = validation
            workflows.append(item)
        return ProjectWorkflowCatalogView(project_id=str(project_id), workflows=workflows)

    def list_project_entrypoints(
        self,
        project_id: Any,
        *,
        enabled_only: bool = True,
        scope: Any = None,
    ) -> ProjectWorkflowEntrypointsView:
        linked_workflow_ids = self._linked_project_workflow_ids(project_id, scope=scope)
        workflow_scope_restricted = bool(getattr(scope, "allowed_workflow_ids", frozenset()))
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        entrypoints = []
        for row in workflow_entrypoint_repository.list_project_workflow_entrypoints(
            bl,
            project_id=str(project_id),
            enabled_only=bool(enabled_only),
        ):
            workflow_id = str(row.get("workflow_id") or "").strip()
            if not workflow_id or (
                workflow_scope_restricted and workflow_id not in linked_workflow_ids
            ):
                continue
            allowed_methods = ["POST"]
            mapping = row.get("input_mapping_jsonb")
            mapping = mapping if isinstance(mapping, dict) else {}
            entrypoints.append(
                WorkflowEntrypointView(
                    entry_key=str(row.get("entry_key") or "").strip(),
                    workflow_id=workflow_id,
                    enabled=bool(row.get("enabled")),
                    allowed_methods=allowed_methods,
                    idempotency_mode=str(row.get("idempotency_mode") or "").strip(),
                    response_mode=str(row.get("response_mode") or "").strip(),
                    requires_secret=bool(str(row.get("secret_ref") or "").strip()),
                    input_mapping_keys=sorted(str(key) for key in mapping),
                )
            )
        return ProjectWorkflowEntrypointsView(project_id=str(project_id), entrypoints=entrypoints)

    def workflow_validate(
        self, workflow_id: str, *, require_available: bool = True
    ) -> WorkflowValidationResultView:
        workflow, validation = self._workflow_validation_payload(
            workflow_id, require_available=require_available
        )
        issues = self._workflow_validation_issues(workflow, validation)
        return WorkflowValidationResultView(
            workflow_id=str(workflow_id or "").strip(),
            validation=validation,
            issues=[WorkflowValidationIssueView(**issue) for issue in issues],
        )

    def workflow_contract(
        self, workflow_id: str, *, require_available: bool = True
    ) -> WorkflowContractResultView:
        return WorkflowContractResultView(
            **self._workflow_contract_payload(workflow_id, require_available=require_available)
        )

    def workflow_suggestions(self, workflow_id: str) -> WorkflowSuggestionsResultView:
        workflow, validation = self._workflow_validation_payload(workflow_id)
        issues = self._workflow_validation_issues(workflow, validation)
        return WorkflowSuggestionsResultView(
            workflow_id=str(workflow_id or "").strip(),
            suggestions=self._workflow_repair_suggestions(workflow_id, issues),
        )

    def project_workflow_validate(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
    ) -> WorkflowValidationResultView:
        return self.workflow_validate(
            self._require_project_workflow(
                project_id, workflow_id, scope=scope, allow_disabled=True
            ),
            require_available=False,
        )

    def project_workflow_contract(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
    ) -> WorkflowContractResultView:
        return self.workflow_contract(
            self._require_project_workflow(
                project_id, workflow_id, scope=scope, allow_disabled=True
            ),
            require_available=False,
        )

    def project_workflow_suggestions(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
    ) -> WorkflowSuggestionsResultView:
        return self.workflow_suggestions(
            self._require_project_workflow(project_id, workflow_id, scope=scope)
        )
