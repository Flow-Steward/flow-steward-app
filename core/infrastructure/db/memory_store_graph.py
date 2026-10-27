# mypy: ignore-errors
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.memory.models import (
    EntityLinkInput,
    MemoryEdge,
    MemoryEdgeInput,
    MemoryEntity,
    MemoryEntityAlias,
    ScopeId,
)
from core.infrastructure.db.memory_store_common import (
    VALUE_AND_SQL,
    _db_account_id,
    _db_scope_id,
    _db_tenant_id,
    _json_dumps,
    _json_loads,
    _row_to_alias,
    _row_to_edge,
    _row_to_entity,
)


class MemoryGraphStoreMixin:
    def create_or_get_entity(
        self,
        *,
        account_id: str,
        entity_type: str,
        canonical_name: str,
        normalized_name: str,
        created_by: str,
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryEntity:
        existing = self._fetchone(
            """
            SELECT *
            FROM memory_entities
            WHERE account_id = %s AND entity_type = %s AND normalized_name = %s
              AND COALESCE(project_id::text, '') = COALESCE(%s, '')
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            [_db_account_id(account_id), entity_type, normalized_name, _db_scope_id(project_id)],
        )
        if existing is not None:
            return _row_to_entity(existing)
        row = self._fetchone(
            """
            INSERT INTO memory_entities (
                account_id, tenant_id, project_id, entity_type, canonical_name,
                normalized_name, metadata_jsonb, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            [
                _db_account_id(account_id),
                _db_tenant_id(tenant_id),
                _db_scope_id(project_id),
                entity_type,
                canonical_name,
                normalized_name,
                _json_dumps(metadata),
                created_by,
            ],
        )
        self._maybe_commit()
        if row is None:
            raise RuntimeError("Entity insert did not return a row")
        return _row_to_entity(row)

    def create_alias(
        self,
        *,
        entity_id: str,
        alias: str,
        normalized_alias: str,
        created_by: str,
        language_code: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> MemoryEntityAlias:
        existing = self._fetchone(
            """
            SELECT *
            FROM memory_entity_aliases
            WHERE entity_id = %s
              AND normalized_alias = %s
              AND COALESCE(language_code, '') = COALESCE(%s, '')
            LIMIT 1
            """,
            [entity_id, normalized_alias, language_code],
        )
        if existing is not None:
            return _row_to_alias(existing)
        row = self._fetchone(
            """
            INSERT INTO memory_entity_aliases (
                entity_id, alias, normalized_alias, language_code,
                provenance_jsonb, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            [
                entity_id,
                alias,
                normalized_alias,
                language_code,
                _json_dumps(provenance),
                created_by,
            ],
        )
        self._maybe_commit()
        if row is None:
            raise RuntimeError("Alias insert did not return a row")
        return _row_to_alias(row)

    def lookup_entity_by_alias(
        self,
        *,
        account_id: str,
        normalized_alias: str,
        language_code: str = "",
        project_id: ScopeId | None = None,
    ) -> MemoryEntity | None:
        conditions = [
            "e.account_id = %s",
            "a.normalized_alias = %s",
        ]
        params: list[Any] = [_db_account_id(account_id), normalized_alias]
        if language_code:
            conditions.append("COALESCE(a.language_code, '') = %s")
            params.append(language_code)
        if project_id is not None:
            conditions.append("(e.project_id = %s OR e.project_id IS NULL)")
            params.append(_db_scope_id(project_id))
        row = self._fetchone(
            f"""
            SELECT e.*
            FROM memory_entity_aliases a
            JOIN memory_entities e ON e.id = a.entity_id
            WHERE {VALUE_AND_SQL.join(conditions)}
            ORDER BY e.updated_at DESC, e.id DESC
            LIMIT 1
            """,
            params,
        )
        return _row_to_entity(row) if row else None

    def link_record_entity(self, payload: EntityLinkInput) -> dict[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO memory_record_entities (
                record_id, entity_id, mention_text, mention_role, confidence, provenance_jsonb
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id, entity_id, mention_role, mention_text) DO NOTHING
            RETURNING record_id, entity_id, mention_text, mention_role, confidence, provenance_jsonb
            """,
            [
                payload.record_id,
                payload.entity_id,
                payload.mention_text,
                payload.mention_role,
                payload.confidence,
                _json_dumps(payload.provenance),
            ],
        )
        self._maybe_commit()
        return row or {
            "record_id": payload.record_id,
            "entity_id": payload.entity_id,
            "mention_text": payload.mention_text,
            "mention_role": payload.mention_role,
            "confidence": payload.confidence,
            "provenance_jsonb": dict(payload.provenance or {}),
        }

    def create_edge(self, payload: MemoryEdgeInput) -> MemoryEdge:
        row = self._fetchone(
            """
            INSERT INTO memory_edges (
                account_id, tenant_id, project_id, workflow_id, job_id, session_id,
                edge_type, from_record_id, to_record_id, from_entity_id, to_entity_id,
                confidence, provenance_jsonb, metadata_jsonb, valid_from, valid_to, created_by
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            RETURNING *
            """,
            [
                _db_account_id(payload.account_id),
                _db_tenant_id(payload.tenant_id),
                _db_scope_id(payload.project_id),
                payload.workflow_id,
                _db_scope_id(payload.job_id),
                payload.session_id,
                payload.edge_type,
                payload.from_record_id,
                payload.to_record_id,
                payload.from_entity_id,
                payload.to_entity_id,
                payload.confidence,
                _json_dumps(payload.provenance),
                _json_dumps(payload.metadata),
                payload.valid_from,
                payload.valid_to,
                payload.created_by,
            ],
        )
        self._maybe_commit()
        if row is None:
            raise RuntimeError("Edge insert did not return a row")
        return _row_to_edge(row)

    def _record_join_conditions(
        self,
        alias: str,
        *,
        account_id: str,
        actor_id: str = "",
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        include_private_user: bool = False,
        lifecycle_statuses: Sequence[str] = ("active", "candidate"),
        only_safe: bool = True,
    ) -> tuple[str, list[Any]]:
        conditions = [f"{alias}.account_id = %s"]
        params: list[Any] = [_db_account_id(account_id)]
        if tenant_id:
            conditions.append(f"({alias}.tenant_id = %s OR {alias}.tenant_id IS NULL)")
            params.append(_db_tenant_id(tenant_id))
        if project_id is not None:
            conditions.append(f"({alias}.project_id = %s OR {alias}.project_id IS NULL)")
            params.append(_db_scope_id(project_id))
        if lifecycle_statuses:
            conditions.append(f"{alias}.lifecycle_status = ANY(%s)")
            params.append(list(lifecycle_statuses))
        if include_private_user and actor_id:
            conditions.append(
                f"(({alias}.visibility = 'private_user' AND {alias}.created_by = %s) "
                f"OR {alias}.visibility <> 'private_user')"
            )
            params.append(actor_id)
        else:
            conditions.append(f"{alias}.visibility <> 'private_user'")
        if only_safe:
            conditions.append(f"COALESCE({alias}.metadata_jsonb->>'unsafe', 'false') <> 'true'")
            conditions.append(f"COALESCE({alias}.metadata_jsonb->>'sensitive', 'false') <> 'true'")
        return VALUE_AND_SQL.join(conditions), params

    def _entity_join_conditions(
        self,
        alias: str,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
    ) -> tuple[str, list[Any]]:
        conditions = [f"{alias}.account_id = %s"]
        params: list[Any] = [_db_account_id(account_id)]
        if tenant_id:
            conditions.append(f"({alias}.tenant_id = %s OR {alias}.tenant_id IS NULL)")
            params.append(_db_tenant_id(tenant_id))
        if project_id is not None:
            conditions.append(f"({alias}.project_id = %s OR {alias}.project_id IS NULL)")
            params.append(_db_scope_id(project_id))
        return VALUE_AND_SQL.join(conditions), params

    def context_neighbors(
        self,
        *,
        account_id: str,
        actor_id: str = "",
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        record_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        include_private_user: bool = False,
        lifecycle_statuses: Sequence[str] = ("active", "candidate"),
        only_safe: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = ["e.account_id = %s"]
        where_params: list[Any] = [_db_account_id(account_id)]
        if tenant_id:
            conditions.append("(e.tenant_id = %s OR e.tenant_id IS NULL)")
            where_params.append(_db_tenant_id(tenant_id))
        if project_id is not None:
            conditions.append("(e.project_id = %s OR e.project_id IS NULL)")
            where_params.append(_db_scope_id(project_id))
        if record_ids:
            conditions.append("(e.from_record_id = ANY(%s) OR e.to_record_id = ANY(%s))")
            where_params.extend([list(record_ids), list(record_ids)])
        if entity_ids:
            conditions.append("(e.from_entity_id = ANY(%s) OR e.to_entity_id = ANY(%s))")
            where_params.extend([list(entity_ids), list(entity_ids)])
        fr_conditions, fr_params = self._record_join_conditions(
            "fr",
            account_id=account_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            project_id=project_id,
            include_private_user=include_private_user,
            lifecycle_statuses=lifecycle_statuses,
            only_safe=only_safe,
        )
        tr_conditions, tr_params = self._record_join_conditions(
            "tr",
            account_id=account_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            project_id=project_id,
            include_private_user=include_private_user,
            lifecycle_statuses=lifecycle_statuses,
            only_safe=only_safe,
        )
        fe_conditions, fe_params = self._entity_join_conditions(
            "fe",
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        te_conditions, te_params = self._entity_join_conditions(
            "te",
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        rows = self._fetchall(
            f"""
            SELECT
                e.id AS edge_id,
                e.edge_type,
                e.confidence,
                e.provenance_jsonb,
                e.metadata_jsonb,
                e.valid_from,
                e.valid_to,
                CASE WHEN fr.id IS NOT NULL THEN e.from_record_id END AS from_record_id,
                CASE WHEN tr.id IS NOT NULL THEN e.to_record_id END AS to_record_id,
                CASE WHEN fe.id IS NOT NULL THEN e.from_entity_id END AS from_entity_id,
                CASE WHEN te.id IS NOT NULL THEN e.to_entity_id END AS to_entity_id,
                fr.title AS from_record_title,
                tr.title AS to_record_title,
                fe.canonical_name AS from_entity_name,
                te.canonical_name AS to_entity_name
            FROM memory_edges e
            LEFT JOIN memory_records fr ON fr.id = e.from_record_id AND {fr_conditions}
            LEFT JOIN memory_records tr ON tr.id = e.to_record_id AND {tr_conditions}
            LEFT JOIN memory_entities fe ON fe.id = e.from_entity_id AND {fe_conditions}
            LEFT JOIN memory_entities te ON te.id = e.to_entity_id AND {te_conditions}
            WHERE {VALUE_AND_SQL.join(conditions)}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT %s
            """,
            [
                *fr_params,
                *tr_params,
                *fe_params,
                *te_params,
                *where_params,
                max(1, min(int(limit or 20), 100)),
            ],
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            normalized.append(
                {
                    **row,
                    "provenance_jsonb": dict(_json_loads(row.get("provenance_jsonb"), default={})),
                    "metadata_jsonb": dict(_json_loads(row.get("metadata_jsonb"), default={})),
                }
            )
        return normalized
