from __future__ import annotations

from copy import copy
from typing import Any

MIN_MEMORY_VECTOR_SIMILARITY = 0.55
MIN_MEMORY_GLOBAL_SCORE = 0.2
MEMORY_GLOBAL_SOURCE_WEIGHT = 0.97
MIN_MEMORY_PRE_WEIGHT_SCORE = MIN_MEMORY_GLOBAL_SCORE / MEMORY_GLOBAL_SOURCE_WEIGHT


def _provenance(result: Any) -> dict[str, Any]:
    return dict(getattr(getattr(result, "citation", None), "provenance", {}) or {})


def memory_search_result_is_relevant(result: Any) -> bool:
    """Apply one evidence-aware predicate to unified memory search results."""
    provenance = _provenance(result)
    if provenance.get("retrieval_mode") != "postgres_unified":
        return True
    if bool(provenance.get("lexical_match")):
        return True
    try:
        if float(provenance.get("graph_score") or 0.0) > 0.0:
            return True
    except (TypeError, ValueError):
        pass
    vector_search_applied = bool(
        provenance.get("vector_search_applied", "vector_similarity" in provenance)
    )
    vector_similarity_available = bool(
        provenance.get(
            "vector_similarity_available",
            vector_search_applied and provenance.get("vector_similarity") is not None,
        )
    )
    if not vector_search_applied or not vector_similarity_available:
        return False
    try:
        similarity = provenance.get("vector_similarity")
        return similarity is not None and float(similarity) >= MIN_MEMORY_VECTOR_SIMILARITY
    except (TypeError, ValueError):
        return False


def memory_search_result_global_score(result: Any) -> float:
    """Normalize a memory result for the global-search semantic score contract."""
    try:
        score = max(0.0, min(float(getattr(result, "score", 0.0) or 0.0), 5.0) / 5.0)
    except (TypeError, ValueError):
        score = 0.0
    provenance = _provenance(result)
    if provenance.get("retrieval_mode") != "postgres_unified":
        return score
    if bool(provenance.get("lexical_match")):
        score = max(score, MIN_MEMORY_PRE_WEIGHT_SCORE)
    try:
        if float(provenance.get("graph_score") or 0.0) > 0.0:
            score = max(score, MIN_MEMORY_PRE_WEIGHT_SCORE)
    except (TypeError, ValueError):
        pass
    if bool(provenance.get("vector_similarity_available")):
        try:
            similarity = provenance.get("vector_similarity")
            if similarity is not None:
                score = max(score, max(0.0, min(float(similarity), 1.0)))
        except (TypeError, ValueError):
            pass
    return score


def memory_search_result_is_globally_visible(result: Any) -> bool:
    return memory_search_result_is_relevant(result) and (
        memory_search_result_global_score(result) >= MIN_MEMORY_GLOBAL_SCORE
    )


def relevant_memory_search_page(
    retrieval: Any,
    query: str,
    *,
    filters: Any,
    max_candidates: int = 5000,
) -> tuple[list[Any], int]:
    """Return one page and its total from the same bounded relevance decision."""
    offset = max(0, int(getattr(filters, "offset", 0) or 0))
    limit = max(1, int(getattr(filters, "limit", 10) or 10))
    candidate_limit = max(1, int(max_candidates))
    candidate_filters = copy(filters)
    candidate_filters.offset = 0
    candidate_filters.limit = candidate_limit
    candidates = retrieval.search(query, filters=candidate_filters)
    relevant = [result for result in candidates if memory_search_result_is_globally_visible(result)]
    return relevant[offset : offset + limit], len(relevant)


__all__ = [
    "MEMORY_GLOBAL_SOURCE_WEIGHT",
    "MIN_MEMORY_GLOBAL_SCORE",
    "MIN_MEMORY_PRE_WEIGHT_SCORE",
    "MIN_MEMORY_VECTOR_SIMILARITY",
    "memory_search_result_global_score",
    "memory_search_result_is_globally_visible",
    "memory_search_result_is_relevant",
    "relevant_memory_search_page",
]
