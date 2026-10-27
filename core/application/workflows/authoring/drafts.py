"""Shared non-mutating preparation and transactional disabled create."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import quote

from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceUnavailableError,
)
from core.application.management.guards import require_workflow_access
from core.application.management.scope import ScopeContext
from core.application.registry.contract_validation_workflow_readiness import (
    validate_workflow_readiness,
)
from core.application.registry.db_store_workflows_mixin import WorkflowCreateConflictError
from core.application.workflows.authoring.catalog import WorkflowAuthoringCatalog
from core.application.workflows.authoring.draft_references import DraftReferences
from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING
from core.application.workflows.workflow_definition_service import (
    create_workflow_with_definition,
)
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.application.workflows.workflow_portability_common import HOOK_BINDINGS_KEY
from core.application.workflows.workflow_secret_default_service import (
    normalize_disabled_workflow_validation,
)
from core.application.workflows.workflow_template_catalog_create import (
    InstallAwareWorkflowDb,
    resolve_portable_extensions,
)

logger = logging.getLogger(__name__)
PERFORMED_CHECKS = (
    "resolution_policy",
    "bundle_schema",
    "dependency_preflight",
    "topology",
    "step_configuration",
    "state_selectors",
    "scope",
    "contracts",
    "readiness",
)
UNPERFORMED_CHECKS = ("runtime_execution", "external_effects", "business_intent")


class DraftValidationResult(TypedDict):
    workflow_id: str
    can_save: bool
    issues: list[dict[str, Any]]
    readiness: dict[str, Any]
    performed_checks: list[str]
    unperformed_checks: list[str]


class DraftSaveResult(DraftValidationResult):
    enabled: bool
    editor_url: str


@dataclass
class _PreparedDraft:
    payload: dict[str, Any]
    result: DraftValidationResult


class _DefinitionView:
    """Read-only candidate row overlay for the existing persisted contract validator."""

    def __init__(self, db: Any, payload: dict[str, Any], catalog: WorkflowAuthoringCatalog) -> None:
        self._db = db
        self._payload = payload
        self._catalog = catalog

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        if workflow_id == self._payload["workflow_id"]:
            # The draft carries its graph as a definition field; the stored row
            # keeps it in settings, which is where readiness reads it.
            candidate = deepcopy(self._payload)
            settings = dict(candidate.get("settings") or {})
            settings[EXECUTION_GRAPH_SETTING] = candidate.pop(EXECUTION_GRAPH_SETTING, None)
            candidate["settings"] = settings
            return candidate
        return self._db.get_workflow(workflow_id)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        getter = getattr(self._catalog.project_store, "get_project", None)
        if callable(getter):
            return getter(project_id)
        getter = getattr(self._catalog.project_store, "_get_project_scope", None)
        return getter(project_id) if callable(getter) else None

    def list_project_workflows(self, **kwargs: Any) -> list[dict[str, Any]]:
        getter = getattr(self._db, "list_project_workflows", None)
        rows = list(getter(**kwargs) or []) if callable(getter) else []
        if not kwargs.get("project_id") or str(kwargs["project_id"]) == self._payload["project_id"]:
            rows.append(
                {
                    "project_id": self._payload["project_id"],
                    "workflow_id": self._payload["workflow_id"],
                    "enabled": True,
                }
            )
        return rows

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("save_", "add_", "delete_", "upsert_", "create_", "toggle_")):
            raise AttributeError(name)
        return getattr(self._db, name)


class WorkflowAuthoringDrafts(DraftReferences):
    def __init__(
        self,
        db: Any,
        *,
        project_store: Any | None = None,
        project_connection_store: Any | None = None,
        get_install_store: Callable[[], Any] | None = None,
        creation_authorizer: Callable[[ScopeContext], None] | None = None,
        transaction_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.db = db
        self.catalog = WorkflowAuthoringCatalog(
            db, project_store=project_store, project_connection_store=project_connection_store
        )
        self.connection_store = project_connection_store or db
        self.get_install_store = get_install_store
        self.creation_authorizer = creation_authorizer
        self.transaction_factory = transaction_factory

    def validate(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> DraftValidationResult:
        resolved = self.catalog.project_scope(scope, project_id)
        checks = ["scope"]
        try:
            return self._prepare(
                resolved, project_id, bundle_yaml, resolutions or [], performed_checks=checks
            ).result
        except WorkflowPortabilityError as exc:
            return {
                "workflow_id": "",
                "can_save": False,
                "issues": self._safe_error_issues(exc),
                "readiness": {"valid": False},
                "performed_checks": checks,
                "unperformed_checks": [
                    *UNPERFORMED_CHECKS,
                    *(check for check in PERFORMED_CHECKS if check not in checks),
                ],
            }

    @staticmethod
    def _safe_error_issues(exc: WorkflowPortabilityError) -> list[dict[str, Any]]:
        # YAML parser exceptions include source excerpts; never publish those.
        details = [] if exc.code == "bundle_yaml_invalid" else exc.errors
        safe = []
        for detail in details:
            if not isinstance(detail, dict) or not detail.get("path"):
                continue
            safe.append(
                {
                    "code": str(detail.get("code") or exc.code)[:100],
                    "path": str(detail["path"])[:300],
                    "message": str(detail.get("message") or str(exc))[:500],
                    "severity": "error",
                    "blocking": True,
                    "repair_guidance": "Repair the identified field and validate again.",
                }
            )
        return safe or [
            {
                "code": exc.code,
                "path": "bundle_yaml",
                "message": str(exc),
                "severity": "error",
                "blocking": True,
                "repair_guidance": "Repair the bundle or dependency resolutions and validate again.",
            }
        ]

    def save(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> DraftSaveResult:
        resolved = self.catalog.project_scope(scope, project_id)
        if self.creation_authorizer is None:
            raise ServiceForbiddenError("Workflow creation authority is required")
        self.creation_authorizer(resolved)
        transaction = (
            self.transaction_factory
            or getattr(self.db, "workflow_import_transaction", None)
            or getattr(self.db, "_transaction", None)
        )
        if not callable(transaction):
            raise ServiceUnavailableError("Workflow creation transaction is unavailable")
        # Preparation and its live scope/reference checks are repeated inside the
        # same transaction that owns the create, association and normal history.
        with transaction():
            prepared = self._prepare(resolved, project_id, bundle_yaml, resolutions or [])
            workflow_id = prepared.payload["workflow_id"]
            require_workflow_access(resolved, workflow_id)
            if not prepared.result["can_save"]:
                if any(
                    issue.get("code") == "workflow_id_conflict"
                    for issue in prepared.result["issues"]
                ):
                    raise ServiceConflictError(
                        "Workflow already exists",
                        payload={"error": "workflow_id_conflict", "workflow_id": workflow_id},
                    )
                raise WorkflowPortabilityError(
                    "Workflow draft validation failed",
                    code="workflow_draft_invalid",
                    errors=prepared.result["issues"],
                )
            binding = getattr(self.db, "add_project_workflow_binding", None)
            if not callable(binding):
                raise ServiceUnavailableError("Workflow project association is unavailable")
            try:
                create_workflow_with_definition(
                    self.db, prepared.payload, author=resolved.actor_name
                )
            except WorkflowCreateConflictError as exc:
                logger.info(
                    "Workflow draft create conflict workflow_id=%s project_id=%s",
                    workflow_id,
                    project_id,
                )
                raise ServiceConflictError(
                    "Workflow already exists",
                    payload={"error": "workflow_id_conflict", "workflow_id": workflow_id},
                ) from exc
            binding(project_id, workflow_id)
        return {
            **prepared.result,
            "enabled": False,
            "editor_url": f"/registry/workflows/{quote(workflow_id, safe='')}/builder",
        }

    def _prepare(
        self,
        scope: ScopeContext,
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]],
        *,
        performed_checks: list[str] | None = None,
    ) -> _PreparedDraft:
        checks = performed_checks if performed_checks is not None else ["scope"]
        checks.append("resolution_policy")
        self._check_resolutions(resolutions)
        db = (
            InstallAwareWorkflowDb(
                self.db, get_install_store=self.get_install_store, account_id=scope.account_id
            )
            if self.get_install_store
            else self.db
        )
        portability = WorkflowPortabilityService(
            db, project_connection_store=self.connection_store, account_id=scope.account_id
        )
        checks.append("bundle_schema")
        bundle = portability.parse_bundle(bundle_yaml)
        if bundle.get(HOOK_BINDINGS_KEY):
            # A draft is one workflow; dropping its hook bindings would leave
            # every configured hook empty behind a successful save.
            raise WorkflowPortabilityError(
                "A draft cannot connect hooks; import the file to restore its hook bindings",
                code="authoring.hook_bindings_not_supported",
            )
        entries = bundle["workflows"]
        if (
            len(entries) != 1
            or not isinstance(entries[0], dict)
            or entries[0].get("portability_kind") == "extension_workflow_ref"
        ):
            raise WorkflowPortabilityError(
                "Provide exactly one importable workflow definition",
                code="authoring.single_definition_required",
            )
        workflow = entries[0]
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id:
            raise WorkflowPortabilityError(
                "Explicit workflow_id is required", code="workflow_id_missing"
            )
        require_workflow_access(scope, workflow_id)
        checks.append("step_configuration")
        self._check_definition_shapes(workflow)
        merged = list(resolutions)
        if self.get_install_store:
            merged, _, _ = resolve_portable_extensions(
                portability,
                bundle_yaml,
                target_workflow_id=workflow_id,
                resolutions=merged,
                account_id=scope.account_id,
                get_install_store=self.get_install_store,
            )
            merged = [
                {**row, "action": "map_existing"}
                if row.get("action") == "install_extension_required"
                else row
                for row in merged
            ]
        preflight = portability.preflight_import(
            bundle_yaml, target_workflow_id=workflow_id, resolutions=merged
        )
        checks.extend(["dependency_preflight", "topology", "state_selectors"])
        index = portability._resolution_index(merged)
        payload = portability._import_payload(
            workflow,
            workflow_id,
            index,
            owner_account_id=scope.account_id,
            owner_project_id=project_id,
        )
        # Match the bulk store's participants persistence.
        payload["settings"]["participants"] = deepcopy(payload["participants"])
        issues = self._dependency_issues(portability, scope, project_id, preflight, merged)
        issues.extend(
            {
                **item,
                "blocking": bool(item.get("blocking")),
                "repair_guidance": "Repair the workflow topology.",
            }
            for item in preflight.get("topology_issues", [])
        )
        issues.extend(self._step_scope_issues(portability, scope, project_id, payload))
        denied_reference = any(
            issue.get("code") == "authoring.reference_not_permitted" for issue in issues
        )
        readiness: dict[str, Any]
        if denied_reference:
            # Contract diagnostics can include private field names. Do not hydrate
            # any dependency contracts after reference visibility has failed.
            readiness = {"valid": False}
            normalized = {"errors": [], "warnings": []}
        else:
            checks.extend(["contracts", "readiness"])
            readiness = validate_workflow_readiness(
                _DefinitionView(db, payload, self.catalog), workflow_id
            )
            normalized = normalize_disabled_workflow_validation(readiness, final_enabled=False)
            # Disabled drafts may save before execution bindings are complete,
            # but their current readiness repair diagnostics remain visible.
            issues.extend(
                {
                    **{key: item[key] for key in ("code", "path", "message") if key in item},
                    "severity": "warning",
                    "blocking": False,
                    "repair_guidance": "Resolve this execution-readiness issue before enabling or running the workflow.",
                }
                for item in readiness.get("errors", [])
                if item not in normalized.get("errors", [])
            )
        nonblocking_topology = {
            item["code"]
            for item in preflight.get("topology_issues", [])
            if not item.get("blocking")
        }
        for severity in ("errors", "warnings"):
            for item in normalized.get(severity, []):
                issues.append(
                    {
                        **item,
                        "blocking": severity == "errors"
                        and item.get("code") not in nonblocking_topology,
                        "repair_guidance": "Repair the field using its current step or capability contract.",
                    }
                )
        result: DraftValidationResult = {
            "workflow_id": workflow_id,
            "can_save": bool(preflight["can_apply"])
            and not any(issue.get("blocking") for issue in issues),
            "issues": issues,
            "readiness": {"valid": bool(readiness.get("valid")), "execution_verified": False},
            "performed_checks": checks,
            "unperformed_checks": [
                *UNPERFORMED_CHECKS,
                *(check for check in PERFORMED_CHECKS if check not in checks),
            ],
        }
        return _PreparedDraft(payload, result)
