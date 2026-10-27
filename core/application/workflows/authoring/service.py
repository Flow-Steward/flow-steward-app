"""Application facade for the six Developer Toolkit authoring operations."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from core.application.management.errors import ServiceValidationError, safe_operator_exception
from core.application.management.scope import ScopeContext
from core.application.workflows.authoring.catalog import (
    CapabilityDescriptionResult,
    CapabilitySearchResult,
    WorkflowAuthoringCatalog,
    WorkflowExamplesResult,
)
from core.application.workflows.authoring.drafts import (
    DraftSaveResult,
    DraftValidationResult,
    WorkflowAuthoringDrafts,
)
from core.application.workflows.authoring.schema import (
    AuthoringSchemaResult,
    WorkflowAuthoringSchema,
)
from core.application.workflows.knowledge_projection import WorkflowKnowledgeBackend
from core.application.workflows.workflow_portability import WorkflowPortabilityError

logger = logging.getLogger(__name__)


class WorkflowAuthoringService:
    """Share scoped read services and one preparation path across transports."""

    def __init__(
        self,
        db: Any,
        *,
        project_store: Any | None = None,
        project_connection_store: Any | None = None,
        workflow_knowledge_backend: WorkflowKnowledgeBackend | None = None,
        get_install_store: Callable[[], Any] | None = None,
        creation_authorizer: Callable[[ScopeContext], None] | None = None,
        transaction_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        after_save: Callable[[], None] | None = None,
    ) -> None:
        self.after_save = after_save
        self.catalog = WorkflowAuthoringCatalog(
            db,
            project_store=project_store,
            project_connection_store=project_connection_store,
            workflow_knowledge_backend=workflow_knowledge_backend,
        )
        self.schemas = WorkflowAuthoringSchema()
        self.drafts = WorkflowAuthoringDrafts(
            db,
            project_store=project_store,
            project_connection_store=project_connection_store,
            get_install_store=get_install_store,
            creation_authorizer=creation_authorizer,
            transaction_factory=transaction_factory,
        )

    def capability_search(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        query: str,
        kinds: Sequence[str] | None = None,
        limit: int = 12,
    ) -> CapabilitySearchResult:
        return self.catalog.search(
            scope=scope, project_id=project_id, query=query, kinds=kinds, limit=limit
        )

    def capability_describe(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        ref: str,
        operation_id: str = "",
    ) -> CapabilityDescriptionResult:
        return self.catalog.describe(
            scope=scope, project_id=project_id, ref=ref, operation_id=operation_id
        )

    def workflow_examples_search(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        query: str,
        limit: int = 5,
    ) -> WorkflowExamplesResult:
        return self.catalog.examples(scope=scope, project_id=project_id, query=query, limit=limit)

    def workflow_authoring_schema(self, *, step_kind: str | None = None) -> AuthoringSchemaResult:
        return self.schemas.get(step_kind=step_kind)

    def workflow_draft_validate(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> DraftValidationResult:
        return self.drafts.validate(
            scope=scope, project_id=project_id, bundle_yaml=bundle_yaml, resolutions=resolutions
        )

    def workflow_draft_save(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        bundle_yaml: str,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> DraftSaveResult:
        try:
            result = self.drafts.save(
                scope=scope, project_id=project_id, bundle_yaml=bundle_yaml, resolutions=resolutions
            )
        except WorkflowPortabilityError as exc:
            raise ServiceValidationError(
                "Workflow draft validation failed",
                status_code=422,
                payload={
                    "code": exc.code,
                    "message": "Workflow draft validation failed",
                    "issues": self.drafts._safe_error_issues(exc),
                },
            ) from exc
        if self.after_save is not None:
            try:
                self.after_save()
            except Exception as exc:
                # Creation has committed. Cache failure must not imply rollback.
                logger.warning(
                    "Authoring workflow cache refresh failed: %s", safe_operator_exception(exc)
                )
                result["issues"].append(
                    {
                        "code": "authoring.cache_refresh_failed",
                        "path": "workflow",
                        "message": "Draft was created, but the workflow cache could not be refreshed.",
                        "severity": "warning",
                        "blocking": False,
                        "repair_guidance": "Refresh the editor using editor_url; the disabled draft already exists.",
                    }
                )
        return result
