from __future__ import annotations

import logging
from datetime import UTC, datetime

from .audit import MemoryAuditService
from .entities import EntityResolutionService
from .exceptions import MemoryNotFoundError, MemoryPolicyError
from .graphs import ContextGraphBackend
from .models import (
    EntityLinkInput,
    MemoryEdge,
    MemoryEdgeInput,
    MemoryRecord,
    MemoryRecordInput,
    MemoryRepository,
    VectorBackend,
)
from .policy import enforce_human_confirmation, enforce_memory_safety, validate_record_input

logger = logging.getLogger(__name__)


class MemoryWriter:
    def __init__(
        self,
        *,
        repository: MemoryRepository,
        audit_service: MemoryAuditService | None = None,
        entity_resolution: EntityResolutionService | None = None,
        context_graph_backend: ContextGraphBackend | None = None,
        vector_backend: VectorBackend | None = None,
    ) -> None:
        self._repository = repository
        self._audit = audit_service or MemoryAuditService(repository=repository)
        self._entities = entity_resolution or EntityResolutionService(repository=repository)
        self._context_graph_backend = context_graph_backend
        self._vector_backend = vector_backend

    def _index_record_after_commit(self, record: MemoryRecord) -> None:
        if self._vector_backend is None:
            return
        vector_backend = self._vector_backend
        if str(getattr(vector_backend, "co_location_group", "") or "") == "postgres":
            return

        def _index_external_record() -> None:
            try:
                vector_backend.index_record(record, repository=self._repository)
            except Exception as exc:
                logger.warning(
                    "External memory vector indexing failed for record %s: %s",
                    record.id,
                    exc,
                )
                self._repository.record_audit_event(
                    account_id=record.account_id,
                    actor_id=record.created_by or "system",
                    action_type="memory_vector_index",
                    project_id=record.project_id,
                    job_id=record.job_id,
                    task_id=str(record.metadata.get("task_id") or ""),
                    task_summary=str(record.metadata.get("task_summary") or ""),
                    status="failed",
                    payload={
                        "record_id": record.id,
                        "backend_group": str(
                            getattr(vector_backend, "co_location_group", "") or ""
                        ),
                    },
                    error=str(exc),
                    meta={"retry": "backfill_required"},
                )

        self._repository.add_post_commit_hook(_index_external_record)

    def _index_record_in_transaction(self, record: MemoryRecord) -> None:
        if self._vector_backend is None:
            return
        if str(getattr(self._vector_backend, "co_location_group", "") or "") != "postgres":
            return
        self._vector_backend.index_record(record, repository=self._repository)

    def create_candidate(self, payload: MemoryRecordInput) -> MemoryRecord:
        payload.lifecycle_status = "candidate"
        validate_record_input(payload)
        enforce_human_confirmation(payload)
        try:
            enforce_memory_safety(payload)
        except MemoryPolicyError as exc:
            diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
            self._audit.record(
                account_id=payload.account_id,
                actor_id=payload.created_by,
                action_type="memory_write_candidate",
                project_id=payload.project_id,
                job_id=payload.job_id,
                task_id=str(payload.metadata.get("task_id") or ""),
                task_summary=str(payload.metadata.get("task_summary") or payload.title or ""),
                status="rejected",
                payload={
                    "memory_type": payload.memory_type,
                    "source_type": payload.source_type,
                    "policy_category": diagnostics.get("category", "unknown"),
                    "policy_rule_id": diagnostics.get("rule_id", "unknown"),
                    "source_field": diagnostics.get("source_field", "unknown"),
                    "matched": diagnostics.get("matched", "unknown"),
                },
                error=str(exc),
            )
            raise
        with self._repository.transaction():
            record = self._repository.upsert_record(payload)
            self._index_record_in_transaction(record)
            self._audit.record(
                account_id=record.account_id,
                actor_id=payload.created_by,
                action_type="memory_write_candidate",
                project_id=record.project_id,
                job_id=record.job_id,
                status="succeeded",
                payload={"memory_type": record.memory_type, "source_type": record.source_type},
                result={"record_id": record.id, "lifecycle_status": record.lifecycle_status},
            )
            self._index_record_after_commit(record)
            return record

    def promote(self, record_id: str, *, actor_id: str, force: bool = False) -> MemoryRecord:
        record = self._repository.get_record(record_id)
        if record is None:
            raise MemoryNotFoundError(f"Memory record {record_id} not found")
        if not force and record.source_trust not in {"human_approved", "canonical_doc"}:
            self._audit.record(
                account_id=record.account_id,
                actor_id=actor_id,
                action_type="memory_promote",
                project_id=record.project_id,
                job_id=record.job_id,
                status="rejected",
                payload={"record_id": record_id, "source_trust": record.source_trust},
                error="Promotion requires human approval or canonical source trust",
            )
            raise MemoryPolicyError(
                "Memory record promotion requires human-approved or canonical-doc source trust"
            )
        with self._repository.transaction():
            updated = self._repository.update_record_status(record_id, lifecycle_status="active")
            if updated is None:
                raise MemoryNotFoundError(f"Memory record {record_id} not found")
            self._audit.record(
                account_id=updated.account_id,
                actor_id=actor_id,
                action_type="memory_promote",
                project_id=updated.project_id,
                job_id=updated.job_id,
                status="succeeded",
                payload={"record_id": record_id},
                result={"lifecycle_status": updated.lifecycle_status},
            )
            return updated

    def reject(self, record_id: str, *, actor_id: str) -> MemoryRecord:
        with self._repository.transaction():
            updated = self._repository.update_record_status(record_id, lifecycle_status="rejected")
            if updated is None:
                raise MemoryNotFoundError(f"Memory record {record_id} not found")
            self._audit.record(
                account_id=updated.account_id,
                actor_id=actor_id,
                action_type="memory_reject",
                project_id=updated.project_id,
                job_id=updated.job_id,
                status="succeeded",
                payload={"record_id": record_id},
                result={"lifecycle_status": updated.lifecycle_status},
            )
            return updated

    def supersede(
        self,
        record_id: str,
        replacement: MemoryRecordInput,
        *,
        actor_id: str,
    ) -> MemoryRecord:
        replacement.supersedes_id = record_id
        replacement.lifecycle_status = (
            "active"
            if replacement.source_trust in {"canonical_doc", "human_approved"}
            else "candidate"
        )
        validate_record_input(replacement)
        enforce_human_confirmation(replacement)
        enforce_memory_safety(replacement)
        with self._repository.transaction():
            prior = self._repository.update_record_status(
                record_id,
                lifecycle_status="superseded",
                valid_to=datetime.now(UTC),
            )
            if prior is None:
                raise MemoryNotFoundError(f"Memory record {record_id} not found")
            record = self._repository.upsert_record(replacement)
            self._index_record_in_transaction(record)
            self._audit.record(
                account_id=record.account_id,
                actor_id=actor_id,
                action_type="memory_supersede",
                project_id=record.project_id,
                job_id=record.job_id,
                status="succeeded",
                payload={"prior_record_id": record_id},
                result={"record_id": record.id, "lifecycle_status": record.lifecycle_status},
            )
            self._index_record_after_commit(record)
            return record

    def expire(self, record_id: str, *, actor_id: str) -> MemoryRecord:
        with self._repository.transaction():
            updated = self._repository.update_record_status(
                record_id,
                lifecycle_status="expired",
                expires_at=datetime.now(UTC),
            )
            if updated is None:
                raise MemoryNotFoundError(f"Memory record {record_id} not found")
            self._audit.record(
                account_id=updated.account_id,
                actor_id=actor_id,
                action_type="memory_expire",
                project_id=updated.project_id,
                job_id=updated.job_id,
                status="succeeded",
                payload={"record_id": record_id},
                result={"lifecycle_status": updated.lifecycle_status},
            )
            return updated

    def link_entity(self, payload: EntityLinkInput, *, actor_id: str) -> dict[str, object]:
        backend = self._context_graph_backend
        with self._repository.transaction():
            if backend is not None:
                result = backend.link_record_entity(payload)
            else:
                result = self._repository.link_record_entity(payload)
            record = self._repository.get_record(payload.record_id)
            account_id = record.account_id if record else ""
            project_id = record.project_id if record else None
            job_id = record.job_id if record else None
            self._audit.record(
                account_id=account_id,
                actor_id=actor_id,
                action_type="memory_link_entity",
                project_id=project_id,
                job_id=job_id,
                status="succeeded",
                payload={"record_id": payload.record_id, "entity_id": payload.entity_id},
            )
            return result

    def link_edge(self, payload: MemoryEdgeInput, *, actor_id: str) -> MemoryEdge:
        backend = self._context_graph_backend
        with self._repository.transaction():
            edge = (
                backend.create_edge(payload)
                if backend is not None
                else self._repository.create_edge(payload)
            )
            self._audit.record(
                account_id=payload.account_id,
                actor_id=actor_id,
                action_type="memory_link_edge",
                project_id=payload.project_id,
                job_id=payload.job_id,
                status="succeeded",
                payload={
                    "edge_type": payload.edge_type,
                    "from_record_id": payload.from_record_id,
                    "to_record_id": payload.to_record_id,
                },
                result={"edge_id": edge.id},
            )
            return edge
