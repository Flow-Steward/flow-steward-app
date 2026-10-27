"""Scoped local discovery and live descriptions without turn shortlists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict
from urllib.parse import quote

from core.application.extensions.connection_viability import project_connection_is_viable
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.guards import (
    require_default_workflow_catalog_account,
    require_project_access,
    require_workflow_access,
)
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.workflows.discovery.capability_contracts import (
    tool_expected_inputs,
    tool_expected_outputs,
    tool_operations,
)
from core.application.workflows.discovery.capability_discovery import (
    WorkflowCapabilityDiscovery,
)
from core.application.workflows.discovery.scoped_capability_catalog import (
    ScopedCapabilityCatalog,
    ScopedCapabilityRef,
)
from core.application.workflows.knowledge_projection import WorkflowKnowledgeBackend
from core.application.workflows.workflow_portability import WorkflowPortabilityService

MAX_RESULTS = 50
DISCOVERY_CANDIDATE_LIMIT = 100
MAX_QUERY_LENGTH = 2000
CATALOG_KINDS = frozenset({"workflow", "agent", "skill", "tool", "dataset"})


class CapabilitySearchResult(TypedDict):
    capabilities: list[dict[str, Any]]
    no_match: bool
    workflow_knowledge: dict[str, Any]


class CapabilityDescriptionResult(TypedDict, total=False):
    ref: str
    type: str
    id: str
    display_name: str
    description: str
    metadata: dict[str, Any]
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    step_kind: str
    operations: list[dict[str, Any]]
    required_connection_types: list[str]
    connections: list[dict[str, Any]]
    setup_url: str
    portability: dict[str, Any]
    bundle_yaml: str
    schema_snapshot: dict[str, Any]
    workflow_id: str
    why_matched: str


class WorkflowExamplesResult(TypedDict):
    examples: list[CapabilityDescriptionResult]
    no_match: bool
    workflow_knowledge: dict[str, Any]


class WorkflowAuthoringCatalog:
    def __init__(
        self,
        db: Any,
        *,
        project_store: Any | None = None,
        project_connection_store: Any | None = None,
        workflow_knowledge_backend: WorkflowKnowledgeBackend | None = None,
    ) -> None:
        self.db = db
        self.project_store = project_store or db
        self.connection_store = project_connection_store or db
        self.visibility = ScopedCapabilityCatalog(db)
        self.discovery = WorkflowCapabilityDiscovery(
            db, workflow_knowledge_backend=workflow_knowledge_backend
        )
        self.portability = WorkflowPortabilityService(db)

    def project_scope(
        self, scope: ScopeContext | Mapping[str, Any], project_id: str
    ) -> ScopeContext:
        resolved = normalize_scope(scope, project_id=project_id)
        require_default_workflow_catalog_account(resolved)
        if not resolved.project_id:
            raise ServiceValidationError("Project is required")
        getter = getattr(self.project_store, "get_project", None)
        if callable(getter):
            project = getter(resolved.project_id)
        else:
            getter = getattr(self.project_store, "_get_project_scope", None)
            project = (
                getter(resolved.project_id, account_id=resolved.account_id)
                if callable(getter)
                else None
            )
        if not isinstance(project, dict):
            raise ServiceNotFoundError("Project not found")
        require_project_access(
            resolved, {**project, "id": project.get("id") or project.get("project_id")}
        )
        return resolved

    def visible_row(
        self, scope: ScopeContext, project_id: str, kind: str, identifier: str
    ) -> dict[str, Any]:
        try:
            if kind == "workflow":
                require_workflow_access(scope, identifier)
            if kind == "dataset":
                getter = getattr(self.db, "get_dataset", None)
                row = getter(identifier) if callable(getter) else None
                if not isinstance(row, dict) or not self.visibility._row_visible(
                    {**row, "id": identifier},
                    capability_type=kind,
                    scope=scope,
                    project_id=project_id,
                ):
                    raise ValueError("Dataset not visible")
                return row
            return self.visibility.assert_capability_visible(
                scope=scope,
                project_id=project_id,
                capability_ref=ScopedCapabilityRef(kind, identifier),
            )
        except (ValueError, ServiceForbiddenError) as exc:
            raise ServiceNotFoundError("Capability not found in this project") from exc

    def search(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        query: str,
        kinds: Sequence[str] | None = None,
        limit: int = 12,
    ) -> CapabilitySearchResult:
        resolved = self.project_scope(scope, project_id)
        selected = set(kinds) if kinds else set(CATALOG_KINDS)
        if (
            selected - CATALOG_KINDS
            or not 1 <= limit <= MAX_RESULTS
            or len(query) > MAX_QUERY_LENGTH
        ):
            raise ServiceValidationError("Invalid search kinds, query or limit")
        discovered = self.discovery.discover(
            workflow_id="",
            request_text=query,
            candidate_kinds=frozenset(selected),
            scope=resolved.model_dump(),
            project_id=project_id,
            candidate_limit=DISCOVERY_CANDIDATE_LIMIT,
        )
        capabilities = []
        for candidate in discovered["candidates"]:
            if candidate.type not in selected:
                continue
            try:
                row = self.visible_row(resolved, project_id, candidate.type, candidate.id)
            except ServiceNotFoundError:
                continue
            # Graph match reasons and stale labels can carry private workflow structure.
            # Publish live public metadata, never raw graph provenance.
            capabilities.append(
                {
                    "ref": f"{candidate.type}:{candidate.id}",
                    "id": candidate.id,
                    "type": candidate.type,
                    "operation_id": candidate.operation_id,
                    "display_name": str(row.get("display_name") or candidate.id),
                    "summary": str(row.get("description") or row.get("description_preview") or ""),
                    "why_matched": "Matched the local catalog or indexed workflow relationships.",
                    "confidence": candidate.confidence,
                    "metadata": self._public_metadata(row),
                }
            )
            if len(capabilities) >= limit:
                break
        status = dict(discovered.get("workflow_knowledge") or {})
        status.pop("error", None)
        return {
            "capabilities": capabilities,
            "no_match": not capabilities,
            "workflow_knowledge": status,
        }

    def describe(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        ref: str,
        operation_id: str = "",
    ) -> CapabilityDescriptionResult:
        resolved = self.project_scope(scope, project_id)
        kind, separator, identifier = ref.partition(":")
        if not separator or not identifier or kind not in CATALOG_KINDS:
            raise ServiceValidationError("Use a returned kind:id capability reference")
        row = self.visible_row(resolved, project_id, kind, identifier)
        result: CapabilityDescriptionResult = {
            "ref": ref,
            "type": kind,
            "id": identifier,
            "display_name": str(row.get("display_name") or identifier),
            "description": str(row.get("description") or ""),
            "metadata": self._public_metadata(row),
        }
        if kind == "workflow":
            contract = (row.get("settings") or {}).get("callable") or {}
            result["inputs"] = contract.get("expected_inputs") or []
            result["outputs"] = contract.get("expected_outputs") or []
            result["portability"] = self.portability.export_policy_for_workflow(row)
            result["bundle_yaml"] = self.portability.export_yaml(identifier, include_children=False)
            result["step_kind"] = "invoke_workflow"
        elif kind == "tool":
            operations = tool_operations(row)
            if operation_id:
                operations = [
                    operation
                    for operation in operations
                    if operation_id in {operation["operation_id"], operation["function_name"]}
                ]
                if not operations:
                    raise ServiceNotFoundError("Operation not found")
            result["operations"] = operations
            result["step_kind"] = "connector"
            if operation_id or len(operations) == 1:
                selected_id = operations[0]["operation_id"] if operations else operation_id
                result["inputs"] = tool_expected_inputs(row, operation_id=selected_id)
                result["outputs"] = tool_expected_outputs(row, operation_id=selected_id)
            required_types = sorted(
                {
                    kind
                    for operation in operations
                    for kind in operation.get("connection_type_ids", [])
                }
            )
            result["required_connection_types"] = required_types
            result["setup_url"] = f"/projects/{quote(project_id, safe='')}?tab=connections"
            result["connections"] = (
                self._connections(resolved, project_id, required_types) if required_types else []
            )
        elif kind in {"agent", "skill"}:
            for name in ("inputs", "outputs"):
                getter = getattr(self.db, f"get_{kind}_{name}", None)
                if row.get("source_install_id"):
                    result[name] = row.get(f"expected_{name}", row.get(f"_{name}", []))
                else:
                    result[name] = (
                        getter(identifier) if callable(getter) else row.get(f"expected_{name}", [])
                    )
            result["step_kind"] = "agent"
        else:
            result["schema_snapshot"] = row.get("schema_snapshot_jsonb") or {}
            result["step_kind"] = "dataset_query"
        return result

    def examples(
        self,
        *,
        scope: ScopeContext | Mapping[str, Any],
        project_id: str,
        query: str,
        limit: int = 5,
    ) -> WorkflowExamplesResult:
        found = self.search(
            scope=scope, project_id=project_id, query=query, kinds=["workflow"], limit=limit
        )
        examples: list[CapabilityDescriptionResult] = []
        for candidate in found["capabilities"]:
            detail = self.describe(scope=scope, project_id=project_id, ref=candidate["ref"])
            examples.append(
                {**detail, "workflow_id": candidate["id"], "why_matched": candidate["why_matched"]}
            )
        return {
            "examples": examples,
            "no_match": not examples,
            "workflow_knowledge": found["workflow_knowledge"],
        }

    def _connections(
        self, scope: ScopeContext, project_id: str, required_types: list[str]
    ) -> list[dict[str, Any]]:
        getter = getattr(self.connection_store, "list_project_connections", None)
        rows = getter(project_id=project_id, enabled_only=False) if callable(getter) else []
        permitted = []
        for row in rows or []:
            if not isinstance(row, dict) or str(row.get("project_id") or "") != project_id:
                continue
            if row.get("account_id") and row["account_id"] != scope.account_id:
                continue
            connection_type = str(row.get("connection_type_id") or row.get("connection_type") or "")
            if connection_type not in required_types:
                continue
            permitted.append(
                {
                    "connection_id": row["connection_id"],
                    "connection_type": connection_type,
                    "enabled": bool(row.get("enabled")),
                    "ready": project_connection_is_viable(row),
                    "health_status": row.get("health_status") or "unknown",
                    "runtime_status": row.get("runtime_status") or "unknown",
                    "setup_url": f"/projects/{quote(project_id, safe='')}?tab=connections",
                }
            )
        return permitted

    @staticmethod
    def _public_metadata(row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("builder_metadata_jsonb") or {}
        return {
            key: metadata[key]
            for key in (
                "purpose",
                "use_cases",
                "anti_use_cases",
                "composition_role",
                "determinism_level",
                "safe_for_auto_use",
            )
            if key in metadata
        }
