# ruff: noqa: F401
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import deque
from collections.abc import Mapping
from typing import Any

from core.application.management.scope import normalize_scope
from core.application.registry.builder_metadata import (
    builder_metadata_summary,
    normalize_builder_metadata,
)
from core.application.workflows.capability_visibility import (
    capability_visible_by_policy,
)
from core.application.workflows.knowledge_projection import (
    WorkflowKnowledgeBackend,
    WorkflowKnowledgeHit,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
    tool_node_id,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .capability_contracts import (
    contract_field_names,
    tool_expected_inputs,
    tool_expected_outputs,
    tool_operations,
)
from .models import CapabilityCandidate
from .scoped_capability_catalog import (
    ScopedCapabilityCatalog,
    ScopedCapabilityRef,
)

logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_NULL_LIKE_TEXT = frozenset({"none", "null", "undefined"})
_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "automation",
        "build",
        "by",
        "create",
        "flow",
        "for",
        "from",
        "get",
        "gets",
        "getting",
        "in",
        "into",
        "make",
        "new",
        "of",
        "or",
        "phase",
        "phases",
        "return",
        "returns",
        "returning",
        "step",
        "steps",
        "that",
        "the",
        "to",
        "use",
        "using",
        "via",
        "with",
        "workflow",
        "workflows",
    }
)
_TOKEN_ALIASES = {
    "http": {"api", "request", "url", "get", "post"},
    "workflow": {"flow", "automation", "pipeline"},
    "update": {"refine", "extend", "modify", "also"},
    "return": {"output", "result", "response"},
}
_CAPABILITY_NODE_KINDS = frozenset({"workflow", "agent", "skill", "tool"})
_MIN_CANDIDATE_CONFIDENCE = 0.24
_WORKFLOW_KNOWLEDGE_SYNC_CACHE: dict[tuple[str, str, str, str], str] = {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULL_LIKE_TEXT else text


def _scope_value(scope: Mapping[str, Any] | Any, key: str) -> str:
    if isinstance(scope, Mapping):
        return _text(scope.get(key))
    return _text(getattr(scope, key, ""))


def _workflow_knowledge_backend_cache_namespace(backend: WorkflowKnowledgeBackend | None) -> str:
    if backend is None:
        return ""
    return "|".join(
        part
        for part in (
            f"{backend.__class__.__module__}.{backend.__class__.__qualname__}",
            _text(getattr(backend, "_dsn", "")),
            _text(getattr(backend, "graph_name", "")),
        )
        if part
    )


def _workflow_knowledge_backend_sync_digest(
    backend: WorkflowKnowledgeBackend | None,
    *,
    account_id: str,
    tenant_id: str | None = None,
    project_id: str | None,
) -> str:
    if backend is None:
        return ""
    getter = getattr(backend, "get_scope_sync_digest", None)
    if not callable(getter):
        return ""
    try:
        return _text(getter(account_id=account_id, tenant_id=tenant_id, project_id=project_id))
    except TypeError:
        try:
            return _text(getter(account_id=account_id, project_id=project_id))
        except Exception:
            logger.debug("Workflow knowledge backend sync digest lookup failed", exc_info=True)
            return ""
    except Exception:
        logger.debug("Workflow knowledge backend sync digest lookup failed", exc_info=True)
        return ""


def _lexical_fallback_enabled() -> bool:
    raw = _text(_runtime_env.get("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "true")).lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    return [_text(item) for item in items if _text(item)]


def _builder_metadata(value: Any) -> dict[str, Any]:
    return normalize_builder_metadata(value)


def _builder_summary(value: Any) -> str:
    return builder_metadata_summary(_builder_metadata(value))


def _builder_match_fields(value: Any) -> list[str]:
    metadata = _builder_metadata(value)
    return [
        _text(metadata.get("purpose")),
        " ".join(_string_list(metadata.get("use_cases"))),
        " ".join(_string_list(metadata.get("anti_use_cases"))),
        _text(metadata.get("composition_role")),
        _text(metadata.get("determinism_level")),
        "safe_for_auto_use" if metadata.get("safe_for_auto_use") else "",
    ]


def _combined_summary(*values: Any) -> str:
    parts = [_text(value) for value in values if _text(value)]
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return " ".join(seen).strip()


def _dataset_column_names(schema_snapshot: Any) -> list[str]:
    """Column names from a dataset schema snapshot — the fields a dataset_query exposes."""
    if not isinstance(schema_snapshot, dict):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for resource in schema_snapshot.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        for column in resource.get("columns") or []:
            if not isinstance(column, dict):
                continue
            name = _text(column.get("name"))
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    return names


def _candidate_contract_field_names(detail: dict[str, Any] | None, *, kind: str) -> list[str]:
    if not isinstance(detail, dict):
        return []
    rows = detail.get(f"expected_{kind}s")
    if not isinstance(rows, list):
        rows = detail.get(f"_{kind}s")
    return contract_field_names(rows if isinstance(rows, list) else [])


def _workflow_contract_field_names(detail: dict[str, Any] | None, *, kind: str) -> list[str]:
    if not isinstance(detail, dict):
        return []
    raw_settings = detail.get("settings")
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    raw_callable_settings = settings.get("callable")
    callable_settings = raw_callable_settings if isinstance(raw_callable_settings, dict) else {}
    if isinstance(callable_settings, dict):
        names = contract_field_names(callable_settings.get(f"expected_{kind}s") or [])
        if names:
            return names
    if kind == "input":
        names = contract_field_names(
            (detail.get("interface_contract") or {}).get("expected_inputs") or []
        )
        if names:
            return names
    else:
        names = contract_field_names(
            (detail.get("output_contract") or {}).get("expected_outputs") or []
        )
        if names:
            return names
    return _candidate_contract_field_names(detail, kind=kind)


def _tokens(value: Any) -> set[str]:
    base = {
        token
        for token in _TOKEN_RE.findall(_text(value).lower())
        if token and token not in _TOKEN_STOPWORDS
    }
    expanded = set(base)
    for token in base:
        expanded.update(_TOKEN_ALIASES.get(token, set()))
    return expanded


def _search_query_text(value: Any) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(_text(value).lower()):
        if token in _TOKEN_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if tokens:
        return " ".join(tokens)
    return _text(value)


def _lexical_hydrate_limit(candidate_limit: int) -> int:
    bounded_limit = max(1, int(candidate_limit or 0))
    return max(8, bounded_limit)


def _visible_capability(row: dict[str, Any]) -> bool:
    capability_type = _text(row.get("type") or row.get("capability_type")).lower()
    if not capability_type:
        if _text(row.get("workflow_id")):
            capability_type = "workflow"
        elif _text(row.get("agent_id")):
            capability_type = "agent"
        elif _text(row.get("skill_id")):
            capability_type = "skill"
        elif _text(row.get("tool_id")):
            capability_type = "tool"
    capability_id = _text(row.get(f"{capability_type}_id") if capability_type else "") or _text(
        row.get("workflow_id")
        or row.get("agent_id")
        or row.get("skill_id")
        or row.get("tool_id")
        or row.get("id")
    )
    if not capability_visible_by_policy(
        capability_id,
        capability=row,
    ):
        return False
    workflow_id = _text(row.get("workflow_id"))
    if workflow_id and "step_count" in row:
        try:
            if int(row.get("step_count") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _visible_capability_node(node: WorkflowKnowledgeNode | None) -> bool:
    if node is None:
        return False
    for key in ("workflow_id", "agent_id", "skill_id", "tool_id"):
        capability_id = node.metadata.get(key)
        if capability_id and not capability_visible_by_policy(
            capability_id,
            capability=node.metadata,
        ):
            return False
    return capability_visible_by_policy(node.id, capability=node.metadata)


def _json_safe_catalog_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            _text(key): _json_safe_catalog_value(item)
            for key, item in sorted(value.items(), key=lambda item: _text(item[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_catalog_value(item) for item in value]
    return str(value)


def _score_match(request_tokens: set[str], *fields: Any) -> float:
    haystack = set()
    for field in fields:
        haystack |= _tokens(field)
    if not request_tokens or not haystack:
        return 0.0
    overlap = request_tokens & haystack
    if not overlap:
        return 0.0
    return min(0.99, len(overlap) / max(1, min(len(request_tokens), 6)))


def _workflow_candidate_request_overlap(
    request_tokens: set[str],
    candidate: CapabilityCandidate,
) -> float:
    return _score_match(
        request_tokens,
        candidate.id,
        candidate.display_name,
        candidate.capability_summary,
        candidate.skill_names,
        candidate.input_names,
        candidate.output_names,
    )


def _workflow_candidate_identity_overlap(
    request_tokens: set[str],
    candidate: CapabilityCandidate,
) -> float:
    return _score_match(
        request_tokens,
        candidate.id,
        candidate.display_name,
        candidate.skill_names,
        candidate.input_names,
        candidate.output_names,
    )


def _prune_catalog_workflow_candidates(
    *,
    request_tokens: set[str],
    candidates: list[CapabilityCandidate],
) -> list[CapabilityCandidate]:
    workflow_candidates = [
        candidate for candidate in candidates if _text(candidate.type).lower() == "workflow"
    ]
    if not workflow_candidates:
        return list(candidates)

    kept_workflow_ids = {
        _text(candidate.id)
        for candidate in workflow_candidates
        if _workflow_candidate_identity_overlap(request_tokens, candidate) >= 0.18
        or _workflow_candidate_request_overlap(request_tokens, candidate) >= 0.45
        or float(candidate.confidence or 0.0) >= 0.72
    }
    has_non_workflow_capability = any(
        _text(candidate.type).lower() in {"agent", "skill", "tool", "block"}
        for candidate in candidates
    )
    if not kept_workflow_ids and not has_non_workflow_capability:
        top_candidate = max(
            workflow_candidates,
            key=lambda candidate: float(candidate.confidence or 0.0),
            default=None,
        )
        if top_candidate is not None and _text(top_candidate.id):
            kept_workflow_ids.add(_text(top_candidate.id))

    return [
        candidate
        for candidate in candidates
        if _text(candidate.type).lower() != "workflow" or _text(candidate.id) in kept_workflow_ids
    ]


def _recommended_use(capability_type: str) -> str:
    normalized = _text(capability_type).lower()
    if normalized == "workflow":
        return "Adapt this workflow pattern or invoke it directly if the request aligns closely."
    if normalized == "agent":
        return "Reuse this agent when reasoning-heavy execution is the core of the request."
    if normalized == "skill":
        return "Use this skill when an existing agent can be paired with it cleanly."
    if normalized == "tool":
        return (
            "Compose this into deterministic workflow steps when direct reasoning is not required."
        )
    return ""


def _build_candidate(
    *,
    capability_id: str,
    capability_type: str,
    display_name: str,
    composition_role: str = "",
    operation_id: str = "",
    function_name: str = "",
    operation_display_name: str = "",
    operation_summary: str = "",
    why_matched: str,
    recommended_use: str,
    confidence: float,
    capability_summary: str = "",
    step_kinds: list[str] | None = None,
    input_names: list[str] | None = None,
    input_contracts: list[dict[str, Any]] | None = None,
    output_names: list[str] | None = None,
    skill_names: list[str] | None = None,
    semantic_roles: list[str] | None = None,
    anti_use_cases: list[str] | None = None,
) -> CapabilityCandidate:
    return CapabilityCandidate(
        id=capability_id,
        type=capability_type,
        display_name=display_name,
        composition_role=_text(composition_role),
        operation_id=_text(operation_id),
        function_name=_text(function_name),
        operation_display_name=_text(operation_display_name),
        operation_summary=_text(operation_summary),
        why_matched=why_matched,
        recommended_use=recommended_use,
        confidence=round(max(0.0, min(confidence, 0.99)), 3),
        capability_summary=_text(capability_summary),
        step_kinds=list(step_kinds or []),
        input_names=list(input_names or []),
        input_contracts=[dict(row) for row in (input_contracts or []) if isinstance(row, dict)],
        output_names=list(output_names or []),
        skill_names=list(skill_names or []),
        # Structured eligibility metadata (Create #10).
        semantic_roles=list(semantic_roles or []),
        anti_use_cases=list(anti_use_cases or []),
    )


def _candidate_key(candidate: CapabilityCandidate) -> tuple[str, str, str]:
    return (
        _text(candidate.type).lower(),
        _text(candidate.id),
        _text(candidate.function_name or candidate.operation_id).lower(),
    )


def _tool_operation_display_name(
    tool_display_name: str,
    operation_display_name: str,
    *,
    multi_operation: bool,
) -> str:
    if not multi_operation:
        return tool_display_name
    normalized_operation = _text(operation_display_name)
    if not normalized_operation:
        return tool_display_name
    return f"{tool_display_name} / {normalized_operation}"


def _merge_candidates(
    *groups: list[CapabilityCandidate],
) -> list[CapabilityCandidate]:
    merged: dict[tuple[str, str, str], CapabilityCandidate] = {}
    for group in groups:
        for candidate in group:
            candidate_id = _text(candidate.id)
            if not candidate_id:
                continue
            key = _candidate_key(candidate)
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            if float(candidate.confidence or 0.0) > float(existing.confidence or 0.0):
                if (
                    existing.why_matched
                    and existing.why_matched != candidate.why_matched
                    and existing.why_matched not in candidate.why_matched
                ):
                    candidate.why_matched = (
                        f"{candidate.why_matched} {existing.why_matched}".strip()
                    )
                merged[key] = candidate
                continue
            if (
                candidate.why_matched
                and candidate.why_matched != existing.why_matched
                and candidate.why_matched not in existing.why_matched
            ):
                existing.why_matched = f"{existing.why_matched} {candidate.why_matched}".strip()
    return sorted(
        merged.values(),
        key=lambda item: (-float(item.confidence or 0.0), item.type, item.display_name),
    )


def _metadata_id(node: WorkflowKnowledgeNode, key: str, prefix: str) -> str:
    metadata_value = _text(node.metadata.get(key))
    if metadata_value:
        return metadata_value
    node_id = _text(node.id)
    if node_id.startswith(prefix):
        return node_id[len(prefix) :]
    return ""


__all__ = [name for name in globals() if not name.startswith("__")]
