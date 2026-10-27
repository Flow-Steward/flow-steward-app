# mypy: ignore-errors
from __future__ import annotations

import logging
from collections.abc import (
    Mapping,
)
from typing import (
    Any,
)

from ..knowledge_projection import (
    WorkflowKnowledgeBackend,
)
from .capability_discovery_helpers import (
    _MIN_CANDIDATE_CONFIDENCE,
    _lexical_fallback_enabled,
    _lexical_hydrate_limit,
    _merge_candidates,
    _prune_catalog_workflow_candidates,
    _tokens,
)
from .contract_fields import (
    _text,
)
from .models import (
    CapabilityCandidate,
)
from .scoped_capability_catalog import (
    ScopedCapabilityCatalog,
)

logger = logging.getLogger(__name__)


class CapabilityDiscoveryCoreMixin:
    def __init__(
        self,
        db: Any,
        *,
        workflow_knowledge_backend: WorkflowKnowledgeBackend | None = None,
    ) -> None:
        self.db = db
        self._workflow_knowledge_backend = workflow_knowledge_backend
        self._synced_scopes: dict[tuple[str, str, str], str] = {}
        self._scoped_catalog = ScopedCapabilityCatalog(db)

    def discover(
        self,
        *,
        workflow_id: str,
        request_text: str,
        candidate_limit: int = 12,
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        enable_workflow_knowledge_search: bool = True,
        candidate_kinds: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        request_tokens = _tokens(request_text)

        graph_candidates: list[CapabilityCandidate] = []
        workflow_knowledge: dict[str, Any] = {
            "enabled": bool(self._workflow_knowledge_backend is not None)
            and bool(enable_workflow_knowledge_search),
            "lexical_fallback_used": False,
            "lexical_fallback_blocked": False,
        }
        if self._workflow_knowledge_backend is not None and enable_workflow_knowledge_search:
            try:
                graph_candidates = self._workflow_knowledge_candidates(
                    workflow_id=workflow_id,
                    request_text=request_text,
                    scope=scope,
                    project_id=project_id,
                    candidate_limit=max(8, candidate_limit * 2),
                )
                workflow_knowledge.update(
                    getattr(self, "_last_workflow_knowledge_status", {}) or {}
                )
            except Exception as exc:
                logger.warning("Workflow knowledge discovery fallback to lexical path: %s", exc)
                workflow_knowledge.update(
                    {
                        "available": False,
                        "freshness": "unavailable",
                        "reason": "backend_error",
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    }
                )

        allow_lexical = True
        blocking_freshness = _text(workflow_knowledge.get("freshness")).lower()
        if workflow_knowledge.get("enabled") and blocking_freshness in {
            "empty",
            "stale",
            "unavailable",
        }:
            if _lexical_fallback_enabled():
                workflow_knowledge["lexical_fallback_used"] = True
            else:
                workflow_knowledge["lexical_fallback_blocked"] = True
                allow_lexical = False
        lexical_candidates = (
            self._lexical_candidates(
                workflow_id=workflow_id,
                request_tokens=request_tokens,
                scope=scope,
                project_id=project_id,
                candidate_limit=candidate_limit,
            )
            if allow_lexical
            else []
        )
        candidates = _merge_candidates(graph_candidates, lexical_candidates)
        candidates = _prune_catalog_workflow_candidates(
            request_tokens=request_tokens,
            candidates=candidates,
        )
        candidates = [
            candidate
            for candidate in candidates
            if float(candidate.confidence or 0.0) >= _MIN_CANDIDATE_CONFIDENCE
        ]
        if candidate_kinds is not None:
            candidates = [
                candidate for candidate in candidates if candidate.type in candidate_kinds
            ]
        return {
            "candidates": candidates[:candidate_limit],
            "workflow_knowledge": workflow_knowledge,
        }

    def _lexical_candidates(
        self,
        *,
        workflow_id: str,
        request_tokens: set[str],
        scope: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        candidate_limit: int = 12,
    ) -> list[CapabilityCandidate]:
        candidates: list[CapabilityCandidate] = []
        hydrate_limit = _lexical_hydrate_limit(candidate_limit)
        candidates.extend(
            self._workflow_candidates(
                workflow_id,
                request_tokens,
                scope=scope,
                project_id=project_id,
                hydrate_limit=hydrate_limit,
            )
        )
        candidates.extend(
            self._agent_candidates(
                request_tokens,
                scope=scope,
                project_id=project_id,
                hydrate_limit=hydrate_limit,
            )
        )
        candidates.extend(
            self._skill_candidates(
                request_tokens,
                scope=scope,
                project_id=project_id,
                hydrate_limit=hydrate_limit,
            )
        )
        candidates.extend(
            self._tool_candidates(
                request_tokens,
                scope=scope,
                project_id=project_id,
                hydrate_limit=hydrate_limit,
            )
        )
        candidates.extend(
            self._dataset_candidates(
                request_tokens,
                scope=scope,
                project_id=project_id,
                hydrate_limit=hydrate_limit,
            )
        )
        return candidates


__all__ = [name for name in globals() if not name.startswith("__")]
