from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from .audit import MemoryAuditService
from .graphs import ContextGraphBackend
from .models import (
    DEFAULT_CONTEXT_CATEGORIES,
    ContextAssemblyRequest,
    ContextAssemblyResult,
    MemorySearchResult,
    approximate_token_count,
)
from .policy import MemoryPolicyResolver
from .retrieval import MemoryRetrievalService, record_to_citation

_MEMORY_CAPABILITIES = frozenset({"kb.search", "memory.search", "memory.context"})


def _result_payload(result: MemorySearchResult) -> dict[str, Any]:
    record = result.record
    citation = record_to_citation(record, snippet=result.citation.snippet)
    return {
        "record_id": record.id,
        "memory_type": record.memory_type,
        "title": record.title,
        "summary": record.summary,
        "content": record.content,
        "visibility": record.visibility,
        "lifecycle_status": record.lifecycle_status,
        "source_type": record.source_type,
        "source_id": record.source_id,
        "source_trust": record.source_trust,
        "importance": record.importance,
        "confidence": record.confidence,
        "project_id": record.project_id,
        "workflow_id": record.workflow_id,
        "job_id": record.job_id,
        "session_id": record.session_id,
        "tags": list(record.tags or []),
        "metadata": dict(record.metadata or {}),
        "citation": asdict(citation),
        "token_estimate": record.token_estimate,
    }


def _trim_payloads(
    payloads: Iterable[dict[str, Any]], *, budget: int
) -> tuple[list[dict[str, Any]], int]:
    remaining = max(0, int(budget or 0))
    selected: list[dict[str, Any]] = []
    consumed = 0
    for payload in payloads:
        estimate = int(payload.get("token_estimate") or approximate_token_count(payload))
        if selected and remaining - estimate < 0:
            break
        selected.append(payload)
        consumed += estimate
        remaining -= estimate
        if remaining <= 0:
            break
    return selected, consumed


def _filter_categories_for_capabilities(
    categories: Iterable[str],
    *,
    allowed_capabilities: Iterable[str] = (),
) -> tuple[str, ...]:
    requested = tuple(categories)
    normalized_capabilities = {
        str(value or "").strip().lower()
        for value in allowed_capabilities
        if str(value or "").strip()
    }
    if not normalized_capabilities:
        return requested

    memory_allowed = bool(normalized_capabilities & _MEMORY_CAPABILITIES)
    memory_categories = {
        "project_profile_context",
        "project_memory",
        "workflow_memory",
        "procedural_memory",
        "docs_memory",
        "graph_context",
    }
    filtered: list[str] = []
    for category in requested:
        if category in memory_categories and not memory_allowed:
            continue
        filtered.append(category)
    return tuple(filtered)


class ContextAssemblyService:
    def __init__(
        self,
        *,
        retrieval_service: MemoryRetrievalService,
        policy_resolver: MemoryPolicyResolver | None = None,
        audit_service: MemoryAuditService | None = None,
        context_graph_backend: ContextGraphBackend | None = None,
    ) -> None:
        self._retrieval = retrieval_service
        self._policy = policy_resolver or MemoryPolicyResolver()
        self._audit = audit_service
        self._context_graph_backend = context_graph_backend

    def _search(
        self,
        query: str,
        *,
        request: ContextAssemblyRequest,
        memory_types: Iterable[str],
        tags: Iterable[str] = (),
        source_types: Iterable[str] = (),
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        filters = self._policy.resolve_filters(
            account_id=request.account_id,
            actor_id=request.actor_id,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            session_id=request.session_id,
            job_id=request.job_id,
            memory_types=memory_types,
            source_types=source_types,
            tags=tags,
            lifecycle_statuses=("active", "candidate"),
            include_private_user=True,
            limit=limit,
        )
        return [_result_payload(item) for item in self._retrieval.search(query, filters=filters)]

    def assemble(self, request: ContextAssemblyRequest) -> ContextAssemblyResult:
        categories = _filter_categories_for_capabilities(
            request.requested_categories or DEFAULT_CONTEXT_CATEGORIES,
            allowed_capabilities=request.allowed_capabilities,
        )
        budget = max(0, int(request.token_budget or 0))
        query = str(request.intent or "").strip() or "*"
        result = ContextAssemblyResult()
        token_usage = 0

        if self._audit is not None:
            self._audit.record(
                account_id=request.account_id,
                actor_id=request.actor_id,
                action_type="memory_context_assembly",
                project_id=request.project_id,
                job_id=request.job_id,
                status="started",
                payload={
                    "intent": request.intent,
                    "requested_categories": list(categories),
                    "token_budget": budget,
                },
            )

        if (
            "project_profile_context" in categories
            and request.project_id is not None
            and budget > 0
        ):
            profile_record = self._retrieval.project_profile(
                account_id=request.account_id,
                project_id=request.project_id,
            )
            profile_results = []
            if profile_record is not None:
                profile_results.append(
                    _result_payload(
                        MemorySearchResult(
                            record=profile_record,
                            score=1.0,
                            citation=record_to_citation(profile_record),
                            matched_terms=[],
                        )
                    )
                )
            result.project_profile_context, consumed = _trim_payloads(
                profile_results, budget=budget
            )
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "project_memory" in categories and budget > 0:
            rows = self._search(
                query,
                request=request,
                memory_types=(
                    "lesson",
                    "fact",
                    "preference",
                    "project_rule",
                    "known_issue",
                    "decision",
                ),
                limit=6,
            )
            result.project_memory, consumed = _trim_payloads(rows, budget=budget)
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "workflow_memory" in categories and budget > 0:
            rows = self._search(
                query,
                request=request,
                memory_types=("workflow_pattern", "verification_result"),
                limit=4,
            )
            result.workflow_memory, consumed = _trim_payloads(rows, budget=budget)
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "procedural_memory" in categories and budget > 0:
            rows = self._search(
                query,
                request=request,
                memory_types=("procedural_playbook", "workflow_pattern"),
                limit=4,
            )
            result.procedural_memory, consumed = _trim_payloads(rows, budget=budget)
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "docs_memory" in categories and budget > 0:
            rows = self._search(
                query,
                request=request,
                memory_types=("docs_note", "extension_note"),
                limit=4,
            )
            result.docs_memory, consumed = _trim_payloads(rows, budget=budget)
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "graph_context" in categories and budget > 0 and self._context_graph_backend is not None:
            record_ids = [
                row.get("record_id")
                for group in (
                    result.project_profile_context,
                    result.project_memory,
                    result.workflow_memory,
                    result.procedural_memory,
                    result.docs_memory,
                )
                for row in group
                if row.get("record_id")
            ]
            neighbors = self._context_graph_backend.neighbors(
                account_id=request.account_id,
                actor_id=request.actor_id,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                record_ids=[str(item) for item in record_ids[:10]],
                include_private_user=True,
                lifecycle_statuses=("active", "candidate"),
                only_safe=True,
                limit=10,
            )
            prepared = []
            for row in neighbors:
                prepared.append(
                    {
                        **dict(row),
                        "citation": {
                            "source_type": "context_graph",
                            "source_id": str(row.get("edge_id") or row.get("id") or ""),
                            "source_trust": "tool_observed",
                            "title": str(row.get("edge_type") or "context edge"),
                            "provenance": {
                                "confidence": row.get("confidence"),
                                "edge_type": row.get("edge_type"),
                            },
                        },
                        "token_estimate": approximate_token_count(row),
                    }
                )
            result.graph_context, consumed = _trim_payloads(prepared, budget=budget)
            token_usage += consumed
            budget = max(0, budget - consumed)

        if "dataset_context" in categories:
            result.dataset_context = []

        citations = []
        for group in (
            result.project_profile_context,
            result.project_memory,
            result.workflow_memory,
            result.procedural_memory,
            result.docs_memory,
            result.graph_context,
            result.dataset_context,
        ):
            for item in group:
                citation = item.get("citation")
                if isinstance(citation, Mapping):
                    citations.append(dict(citation))
        result.citations = citations
        result.token_usage = token_usage

        if self._audit is not None:
            self._audit.record(
                account_id=request.account_id,
                actor_id=request.actor_id,
                action_type="memory_context_assembly",
                project_id=request.project_id,
                job_id=request.job_id,
                status="succeeded",
                result={
                    "token_usage": token_usage,
                    "categories": {
                        "project_profile_context": len(result.project_profile_context),
                        "project_memory": len(result.project_memory),
                        "workflow_memory": len(result.workflow_memory),
                        "procedural_memory": len(result.procedural_memory),
                        "docs_memory": len(result.docs_memory),
                        "graph_context": len(result.graph_context),
                        "dataset_context": len(result.dataset_context),
                    },
                },
            )

        return result
