# mypy: ignore-errors
from __future__ import annotations

import logging
from collections.abc import (
    Mapping,
)
from typing import (
    Any,
)

from .capability_discovery_helpers import (
    _CAPABILITY_NODE_KINDS,
    _WORKFLOW_KNOWLEDGE_SYNC_CACHE,
    WorkflowKnowledgeHit,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
    _merge_candidates,
    _metadata_id,
    _scope_value,
    _search_query_text,
    _tool_operation_display_name,
    _visible_capability_node,
    _workflow_knowledge_backend_cache_namespace,
    _workflow_knowledge_backend_sync_digest,
    deque,
    normalize_scope,
    tool_node_id,
)
from .contract_fields import (
    _text,
)
from .models import (
    CapabilityCandidate,
)

logger = logging.getLogger(__name__)


class CapabilityDiscoveryKnowledgeMixin:
    def _workflow_knowledge_candidates(
        self,
        *,
        workflow_id: str,
        request_text: str,
        scope: Mapping[str, Any] | Any,
        project_id: str | None,
        candidate_limit: int,
    ) -> list[CapabilityCandidate]:
        normalized_scope = normalize_scope(scope)
        account_id = _scope_value(normalized_scope, "account_id")
        tenant_id = _scope_value(normalized_scope, "tenant_id")
        if not account_id or self._workflow_knowledge_backend is None:
            self._last_workflow_knowledge_status = {
                "available": False,
                "freshness": "not_configured",
                "reason": "missing_scope_or_backend",
            }
            return []
        project_scope = _text(project_id)
        search_query = _search_query_text(request_text)
        if not search_query:
            self._last_workflow_knowledge_status = {
                "available": True,
                "freshness": "not_needed",
                "reason": "empty_query",
            }
            return []
        sync_key = (account_id, tenant_id, project_scope)
        sync_cache_key = (
            _workflow_knowledge_backend_cache_namespace(self._workflow_knowledge_backend),
            account_id,
            tenant_id,
            project_scope,
        )
        catalog_digest = self._catalog_sync_digest(scope=normalized_scope, project_id=project_id)
        cached_digest = self._synced_scopes.get(sync_key) or _WORKFLOW_KNOWLEDGE_SYNC_CACHE.get(
            sync_cache_key
        )
        if not cached_digest:
            cached_digest = _workflow_knowledge_backend_sync_digest(
                self._workflow_knowledge_backend,
                account_id=account_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if cached_digest:
                _WORKFLOW_KNOWLEDGE_SYNC_CACHE[sync_cache_key] = cached_digest
        if cached_digest != catalog_digest:
            logger.info(
                "Skipping workflow knowledge sync in live request path for account/tenant/project scope %s/%s/%s.",
                account_id,
                tenant_id or "-",
                project_scope or "global",
            )
            self._last_workflow_knowledge_status = {
                "available": True,
                "freshness": "stale" if cached_digest else "empty",
                "reason": "catalog_digest_mismatch" if cached_digest else "missing_scope_digest",
                "catalog_digest": catalog_digest,
                "stored_digest": cached_digest,
                "scope": {
                    "account_id": account_id,
                    "tenant_id": tenant_id,
                    "project_id": project_scope,
                },
            }
            return []
        if cached_digest:
            self._synced_scopes[sync_key] = cached_digest
        self._last_workflow_knowledge_status = {
            "available": True,
            "freshness": "fresh",
            "reason": "catalog_digest_current",
            "catalog_digest": catalog_digest,
            "stored_digest": cached_digest,
            "scope": {
                "account_id": account_id,
                "tenant_id": tenant_id,
                "project_id": project_scope,
            },
        }
        hits = self._workflow_knowledge_backend.search(
            query=search_query,
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
            entity_kinds=(
                "workflow",
                "phase",
                "step",
                "reaction",
                "agent",
                "skill",
                "tool",
                "tool_action",
                "io_field",
                "step_kind",
                "phase_category",
                "agent_type",
                "skill_category",
                "tool_category",
            ),
            limit=max(12, candidate_limit),
        )
        if not hits:
            self._last_workflow_knowledge_status["reason"] = "no_search_hits"
            return []
        subgraph = self._workflow_knowledge_backend.fetch_subgraph(
            entity_ids=[hit.entity_id for hit in hits[:8]],
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
            depth=2,
            limit=160,
        )
        return self._graph_candidates_from_hits(
            hits=hits,
            subgraph=subgraph,
            current_workflow_id=workflow_id,
        )

    def _graph_candidates_from_hits(
        self,
        *,
        hits: list[WorkflowKnowledgeHit],
        subgraph: WorkflowKnowledgeProjection,
        current_workflow_id: str,
    ) -> list[CapabilityCandidate]:
        node_map = {node.id: node for node in (subgraph.nodes or [])}
        adjacency: dict[str, set[str]] = {}
        for edge in subgraph.edges or []:
            source_id = _text(edge.source_id)
            target_id = _text(edge.target_id)
            if not source_id or not target_id:
                continue
            adjacency.setdefault(source_id, set()).add(target_id)
            adjacency.setdefault(target_id, set()).add(source_id)

        candidates: list[CapabilityCandidate] = []
        for hit in hits:
            source_node = node_map.get(hit.entity_id)
            if source_node is None or not _visible_capability_node(source_node):
                continue
            candidates.extend(
                self._candidates_from_source_node(
                    source_node=source_node,
                    hit=hit,
                    node_map=node_map,
                    adjacency=adjacency,
                    current_workflow_id=current_workflow_id,
                )
            )
        return _merge_candidates(candidates)

    def _candidates_from_source_node(
        self,
        *,
        source_node: WorkflowKnowledgeNode,
        hit: WorkflowKnowledgeHit,
        node_map: dict[str, WorkflowKnowledgeNode],
        adjacency: dict[str, set[str]],
        current_workflow_id: str,
    ) -> list[CapabilityCandidate]:
        kind = _text(source_node.kind).lower()
        if kind == "workflow":
            if not _visible_capability_node(source_node):
                return []
            workflow_ref = _metadata_id(source_node, "workflow_id", "workflow:")
            if workflow_ref and workflow_ref != current_workflow_id:
                return [
                    self._candidate_from_capability(
                        capability_id=workflow_ref,
                        capability_type="workflow",
                        display_name=_text(source_node.label or workflow_ref) or workflow_ref,
                        confidence=min(0.99, float(hit.score or 0.0) / 100.0),
                        why_matched=(
                            f"Matched workflow graph semantics and structure for "
                            f"'{_text(source_node.label or workflow_ref)}'."
                        ),
                    )
                ]
            return []
        if kind == "agent":
            if not _visible_capability_node(source_node):
                return []
            agent_ref = _metadata_id(source_node, "agent_id", "agent:")
            if agent_ref:
                return [
                    self._candidate_from_capability(
                        capability_id=agent_ref,
                        capability_type="agent",
                        display_name=_text(source_node.label or agent_ref) or agent_ref,
                        confidence=min(0.99, float(hit.score or 0.0) / 100.0),
                        why_matched=(
                            f"Matched agent graph metadata for "
                            f"'{_text(source_node.label or agent_ref)}'."
                        ),
                    )
                ]
        if kind == "skill":
            if not _visible_capability_node(source_node):
                return []
            skill_ref = _metadata_id(source_node, "skill_id", "skill:")
            if skill_ref:
                return [
                    self._candidate_from_capability(
                        capability_id=skill_ref,
                        capability_type="skill",
                        display_name=_text(source_node.label or skill_ref) or skill_ref,
                        confidence=min(0.99, float(hit.score or 0.0) / 100.0),
                        why_matched=(
                            f"Matched skill graph metadata for "
                            f"'{_text(source_node.label or skill_ref)}'."
                        ),
                    )
                ]
        if kind == "tool":
            if not _visible_capability_node(source_node):
                return []
            tool_ref = _metadata_id(source_node, "tool_id", "tool:")
            if tool_ref:
                return [
                    self._candidate_from_capability(
                        capability_id=tool_ref,
                        capability_type="tool",
                        display_name=_text(source_node.label or tool_ref) or tool_ref,
                        confidence=min(0.99, float(hit.score or 0.0) / 100.0),
                        why_matched=(
                            f"Matched deterministic tool metadata for "
                            f"'{_text(source_node.label or tool_ref)}'."
                        ),
                    )
                ]
        if kind == "tool_action":
            tool_ref = _text(source_node.metadata.get("tool_id"))
            if tool_ref:
                tool_node = node_map.get(tool_node_id(tool_ref))
                if not _visible_capability_node(tool_node):
                    return []
                tool_label = _text((tool_node.label if tool_node else "") or tool_ref) or tool_ref
                operation_id = _text(source_node.metadata.get("operation_id"))
                action_label = _text(source_node.label or operation_id)
                return [
                    self._candidate_from_capability(
                        capability_id=tool_ref,
                        capability_type="tool",
                        display_name=_tool_operation_display_name(
                            tool_label,
                            action_label or operation_id,
                            multi_operation=True,
                        ),
                        operation_id=operation_id,
                        function_name=operation_id,
                        operation_display_name=action_label or operation_id,
                        operation_summary=_text(source_node.metadata.get("description")),
                        confidence=min(0.99, float(hit.score or 0.0) / 100.0),
                        why_matched=(
                            f"Matched tool action '{action_label}' within '{tool_label}'."
                        ),
                    )
                ]
        if kind == "step":
            step_candidates = self._step_backed_candidates(
                source_node=source_node,
                hit=hit,
                node_map=node_map,
                current_workflow_id=current_workflow_id,
            )
            if step_candidates:
                return step_candidates
        if kind in {"phase", "reaction"}:
            workflow_ref = _text(source_node.metadata.get("workflow_id"))
            workflow_node = node_map.get(f"workflow:{workflow_ref}") if workflow_ref else None
            if workflow_ref and not _visible_capability_node(workflow_node):
                return []
            if workflow_ref and workflow_ref != current_workflow_id:
                return [
                    self._candidate_from_capability(
                        capability_id=workflow_ref,
                        capability_type="workflow",
                        display_name=workflow_ref,
                        confidence=min(0.9, float(hit.score or 0.0) / 110.0),
                        why_matched=(
                            f"Matched workflow graph context around "
                            f"'{_text(source_node.label or workflow_ref)}'."
                        ),
                    )
                ]
        return self._neighbor_capability_candidates(
            source_node=source_node,
            hit=hit,
            node_map=node_map,
            adjacency=adjacency,
            current_workflow_id=current_workflow_id,
        )

    def _step_backed_candidates(
        self,
        *,
        source_node: WorkflowKnowledgeNode,
        hit: WorkflowKnowledgeHit,
        node_map: dict[str, WorkflowKnowledgeNode],
        current_workflow_id: str,
    ) -> list[CapabilityCandidate]:
        base_confidence = min(0.95, float(hit.score or 0.0) / 105.0)
        step_label = _text(source_node.label or source_node.metadata.get("step_graph_id"))
        tool_ref = _text(source_node.metadata.get("tool_id"))
        if tool_ref:
            tool_node = node_map.get(tool_node_id(tool_ref))
            if not _visible_capability_node(tool_node):
                return []
            tool_label = _text((tool_node.label if tool_node else "") or tool_ref) or tool_ref
            action_id = _text(source_node.metadata.get("tool_action_id"))
            why = f"Matched workflow step '{step_label}' that uses tool '{tool_label}'."
            if action_id:
                why = (
                    f"Matched workflow step '{step_label}' that uses action "
                    f"'{action_id}' from '{tool_label}'."
                )
            return [
                self._candidate_from_capability(
                    capability_id=tool_ref,
                    capability_type="tool",
                    display_name=tool_label,
                    confidence=base_confidence,
                    why_matched=why,
                )
            ]
        agent_ref = _text(source_node.metadata.get("agent_id"))
        if agent_ref:
            agent_node = node_map.get(f"agent:{agent_ref}")
            if not _visible_capability_node(agent_node):
                return []
            return [
                self._candidate_from_capability(
                    capability_id=agent_ref,
                    capability_type="agent",
                    display_name=agent_ref,
                    confidence=base_confidence,
                    why_matched=f"Matched workflow step '{step_label}' that uses agent '{agent_ref}'.",
                )
            ]
        skill_ref = _text(source_node.metadata.get("skill_id"))
        if skill_ref:
            skill_node = node_map.get(f"skill:{skill_ref}")
            if not _visible_capability_node(skill_node):
                return []
            return [
                self._candidate_from_capability(
                    capability_id=skill_ref,
                    capability_type="skill",
                    display_name=skill_ref,
                    confidence=base_confidence,
                    why_matched=f"Matched workflow step '{step_label}' that uses skill '{skill_ref}'.",
                )
            ]
        child_workflow_ref = _text(source_node.metadata.get("child_workflow_id"))
        if child_workflow_ref and child_workflow_ref != current_workflow_id:
            child_workflow_node = node_map.get(f"workflow:{child_workflow_ref}")
            if not _visible_capability_node(child_workflow_node):
                return []
            return [
                self._candidate_from_capability(
                    capability_id=child_workflow_ref,
                    capability_type="workflow",
                    display_name=child_workflow_ref,
                    confidence=base_confidence,
                    why_matched=(
                        f"Matched workflow step '{step_label}' that invokes workflow "
                        f"'{child_workflow_ref}'."
                    ),
                )
            ]
        workflow_ref = _text(source_node.metadata.get("workflow_id"))
        if workflow_ref and workflow_ref != current_workflow_id:
            workflow_node = node_map.get(f"workflow:{workflow_ref}")
            if not _visible_capability_node(workflow_node):
                return []
            return [
                self._candidate_from_capability(
                    capability_id=workflow_ref,
                    capability_type="workflow",
                    display_name=workflow_ref,
                    confidence=min(0.88, base_confidence),
                    why_matched=(
                        f"Matched workflow step '{step_label}' inside workflow '{workflow_ref}'."
                    ),
                )
            ]
        return []

    def _neighbor_capability_candidates(
        self,
        *,
        source_node: WorkflowKnowledgeNode,
        hit: WorkflowKnowledgeHit,
        node_map: dict[str, WorkflowKnowledgeNode],
        adjacency: dict[str, set[str]],
        current_workflow_id: str,
    ) -> list[CapabilityCandidate]:
        queue: deque[tuple[str, int]] = deque([(source_node.id, 0)])
        seen = {source_node.id}
        candidates: list[CapabilityCandidate] = []
        while queue:
            node_id, depth = queue.popleft()
            if depth >= 2:
                continue
            for neighbor_id in sorted(adjacency.get(node_id, set())):
                if neighbor_id in seen:
                    continue
                seen.add(neighbor_id)
                neighbor_node = node_map.get(neighbor_id)
                if neighbor_node is None or not _visible_capability_node(neighbor_node):
                    continue
                next_depth = depth + 1
                neighbor_kind = _text(neighbor_node.kind).lower()
                if neighbor_kind in _CAPABILITY_NODE_KINDS:
                    candidate = self._candidate_from_neighbor_capability(
                        source_node=source_node,
                        capability_node=neighbor_node,
                        hit_score=float(hit.score or 0.0),
                        hop_count=next_depth,
                        current_workflow_id=current_workflow_id,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                queue.append((neighbor_id, next_depth))
        return candidates[:4]

    def _candidate_from_neighbor_capability(
        self,
        *,
        source_node: WorkflowKnowledgeNode,
        capability_node: WorkflowKnowledgeNode,
        hit_score: float,
        hop_count: int,
        current_workflow_id: str,
    ) -> CapabilityCandidate | None:
        kind = _text(capability_node.kind).lower()
        if not _visible_capability_node(capability_node):
            return None
        base_confidence = min(0.92, hit_score / 100.0)
        if hop_count == 2:
            base_confidence *= 0.82
        label = _text(capability_node.label or capability_node.id)
        if kind == "workflow":
            workflow_ref = _metadata_id(capability_node, "workflow_id", "workflow:")
            if not workflow_ref or workflow_ref == current_workflow_id:
                return None
            return self._candidate_from_capability(
                capability_id=workflow_ref,
                capability_type="workflow",
                display_name=label or workflow_ref,
                confidence=base_confidence,
                why_matched=(
                    f"Matched graph-linked workflow context through '{_text(source_node.label)}'."
                ),
            )
        if kind == "agent":
            agent_ref = _metadata_id(capability_node, "agent_id", "agent:")
            if not agent_ref:
                return None
            return self._candidate_from_capability(
                capability_id=agent_ref,
                capability_type="agent",
                display_name=label or agent_ref,
                confidence=base_confidence,
                why_matched=(
                    f"Matched graph-linked agent context through '{_text(source_node.label)}'."
                ),
            )
        if kind == "skill":
            skill_ref = _metadata_id(capability_node, "skill_id", "skill:")
            if not skill_ref:
                return None
            return self._candidate_from_capability(
                capability_id=skill_ref,
                capability_type="skill",
                display_name=label or skill_ref,
                confidence=base_confidence,
                why_matched=(
                    f"Matched graph-linked skill context through '{_text(source_node.label)}'."
                ),
            )
        if kind == "tool":
            tool_ref = _metadata_id(capability_node, "tool_id", "tool:")
            if not tool_ref:
                return None
            return self._candidate_from_capability(
                capability_id=tool_ref,
                capability_type="tool",
                display_name=label or tool_ref,
                confidence=base_confidence,
                why_matched=(
                    f"Matched shared input/output graph context through '{_text(source_node.label)}'."
                ),
            )
        return None


__all__ = [name for name in globals() if not name.startswith("__")]
