from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.application.memory.models import (
    EvidenceRef,
    MemoryEdge,
    MemoryEntity,
    MemoryEntityAlias,
    MemoryRecord,
    ScopeId,
)
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID

VALUE_AND_SQL = " AND "


_SEARCH_VECTOR_SQL = "to_tsvector('simple', concat_ws(' ', coalesce(title, ''), coalesce(summary, ''), coalesce(content, '')))"


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {})


def _json_loads(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _evidence_refs(value: Any) -> list[EvidenceRef]:
    refs = _json_loads(value, default=[])
    output: list[EvidenceRef] = []
    if not isinstance(refs, list):
        return output
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        output.append(
            EvidenceRef(
                ref_type=str(ref.get("ref_type") or ref.get("type") or "").strip() or "source",
                ref_id=str(ref.get("ref_id") or ref.get("id") or "").strip(),
                label=str(ref.get("label") or "").strip(),
                uri=str(ref.get("uri") or "").strip(),
                metadata=dict(ref.get("metadata") or {})
                if isinstance(ref.get("metadata"), Mapping)
                else {},
            )
        )
    return output


def _scope_id(value: Any) -> ScopeId | None:
    if value in (None, ""):
        return None
    normalized = str(value or "").strip()
    return normalized or None


def _db_scope_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _db_account_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized == "acc_default":
        return DEFAULT_ACCOUNT_ID
    return normalized


def _db_tenant_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized or DEFAULT_TENANT_ID


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _row_to_record(row: Mapping[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=str(row.get("id") or ""),
        account_id=str(row.get("account_id") or ""),
        tenant_id=str(row.get("tenant_id") or "").strip() or None,
        project_id=_scope_id(row.get("project_id")),
        workflow_id=str(row.get("workflow_id") or "").strip() or None,
        job_id=_scope_id(row.get("job_id")),
        session_id=str(row.get("session_id") or "").strip() or None,
        memory_type=str(row.get("memory_type") or ""),
        visibility=str(row.get("visibility") or ""),
        lifecycle_status=str(row.get("lifecycle_status") or ""),
        title=str(row.get("title") or ""),
        summary=str(row.get("summary") or ""),
        content=str(row.get("content") or ""),
        source_type=str(row.get("source_type") or ""),
        source_id=str(row.get("source_id") or ""),
        source_trust=str(row.get("source_trust") or ""),
        evidence_refs=_evidence_refs(row.get("evidence_refs_jsonb")),
        confidence=_maybe_float(row.get("confidence")),
        importance=float(row.get("importance") or 0.0),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        supersedes_id=str(row.get("supersedes_id") or "").strip() or None,
        retention_policy=str(row.get("retention_policy") or "").strip() or None,
        expires_at=row.get("expires_at"),
        tags=[
            str(item).strip()
            for item in (_json_loads(row.get("tags_jsonb"), default=[]))
            if str(item).strip()
        ],
        metadata=(
            dict(_json_loads(row.get("metadata_jsonb"), default={}))
            if isinstance(_json_loads(row.get("metadata_jsonb"), default={}), Mapping)
            else {}
        ),
        created_by=str(row.get("created_by") or ""),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_entity(row: Mapping[str, Any]) -> MemoryEntity:
    return MemoryEntity(
        id=str(row.get("id") or ""),
        account_id=str(row.get("account_id") or ""),
        tenant_id=str(row.get("tenant_id") or "").strip() or None,
        project_id=_scope_id(row.get("project_id")),
        entity_type=str(row.get("entity_type") or ""),
        canonical_name=str(row.get("canonical_name") or ""),
        normalized_name=str(row.get("normalized_name") or ""),
        created_by=str(row.get("created_by") or ""),
        metadata=dict(_json_loads(row.get("metadata_jsonb"), default={})),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_alias(row: Mapping[str, Any]) -> MemoryEntityAlias:
    return MemoryEntityAlias(
        id=str(row.get("id") or ""),
        entity_id=str(row.get("entity_id") or ""),
        alias=str(row.get("alias") or ""),
        normalized_alias=str(row.get("normalized_alias") or ""),
        language_code=str(row.get("language_code") or ""),
        created_by=str(row.get("created_by") or ""),
        provenance=dict(_json_loads(row.get("provenance_jsonb"), default={})),
        created_at=row.get("created_at"),
    )


def _row_to_edge(row: Mapping[str, Any]) -> MemoryEdge:
    return MemoryEdge(
        id=str(row.get("id") or ""),
        account_id=str(row.get("account_id") or ""),
        tenant_id=str(row.get("tenant_id") or "").strip() or None,
        project_id=_scope_id(row.get("project_id")),
        workflow_id=str(row.get("workflow_id") or "").strip() or None,
        job_id=_scope_id(row.get("job_id")),
        session_id=str(row.get("session_id") or "").strip() or None,
        edge_type=str(row.get("edge_type") or ""),
        from_record_id=str(row.get("from_record_id") or "").strip() or None,
        to_record_id=str(row.get("to_record_id") or "").strip() or None,
        from_entity_id=str(row.get("from_entity_id") or "").strip() or None,
        to_entity_id=str(row.get("to_entity_id") or "").strip() or None,
        created_by=str(row.get("created_by") or ""),
        confidence=_maybe_float(row.get("confidence")),
        provenance=dict(_json_loads(row.get("provenance_jsonb"), default={})),
        metadata=dict(_json_loads(row.get("metadata_jsonb"), default={})),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        created_at=row.get("created_at"),
    )
