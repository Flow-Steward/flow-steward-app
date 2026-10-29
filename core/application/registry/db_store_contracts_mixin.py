# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .db_store_common import *  # NOSONAR


class RegistryDBStoreContractsMixin:
    @staticmethod
    def _default_system_prompt(agent: dict) -> str:
        """Generate a minimal safe system prompt for agents missing one."""
        display_name = str(agent.get("display_name") or agent.get("agent_id") or "Agent").strip()
        if not display_name:
            display_name = "Agent"
        return (
            f"You are {display_name}. "
            "Follow assigned workflow steps and return complete, reliable results."
        )

    @staticmethod
    def _canonicalize_agent_prompts(agent: dict) -> dict:
        """Expose skill prompts from the junction table as the API shape."""
        prompts = agent.get("prompts") or {}
        if isinstance(prompts, str):
            prompts = json.loads(prompts)
        if not isinstance(prompts, dict):
            prompts = {}
        prompts = strip_agent_output_format(prompts)
        prompts["skill_prompts"] = {
            str(cap.get("skill_id") or ""): cap.get("skill_prompt") or ""
            for cap in agent.get("_skills", [])
            if str(cap.get("skill_id") or "").strip()
        }
        prompts["local_instructions"] = list(prompts.get("local_instructions") or [])
        if not str(prompts.get("system") or "").strip():
            prompts["system"] = RegistryDBStoreContractsMixin._default_system_prompt(agent)
        agent["prompts"] = prompts
        return agent

    @staticmethod
    def _apply_agent_composition_fields(agent: dict) -> dict:
        raw_skills = agent.get("_skills")
        skills: list[Any] = raw_skills if isinstance(raw_skills, list) else []
        ordered_skill_ids: list[str] = []
        for item in skills:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "").strip()
            if skill_id:
                ordered_skill_ids.append(skill_id)
        if "base_skill" not in agent or not str(agent.get("base_skill") or "").strip():
            agent["base_skill"] = ordered_skill_ids[0] if ordered_skill_ids else ""
        if "secondary_skills" not in agent or not isinstance(agent.get("secondary_skills"), list):
            agent["secondary_skills"] = ordered_skill_ids[1:] if len(ordered_skill_ids) > 1 else []
        return agent

    @staticmethod
    def _normalize_agent_skill_ids(raw_caps: list | None) -> list[str]:
        normalized: list[str] = []
        for item in raw_caps or []:
            if isinstance(item, dict):
                cap_id = str(item.get("skill_id") or "").strip()
            else:
                cap_id = str(item or "").strip()
            if cap_id:
                normalized.append(cap_id)
        return normalized

    @staticmethod
    def _compose_agent_skill_ids(data: dict) -> list[str]:
        explicit = RegistryDBStoreContractsMixin._normalize_agent_skill_ids(data.get("skills", []))
        if explicit:
            return explicit
        base_skill = str(data.get("base_skill") or "").strip()
        secondary = RegistryDBStoreContractsMixin._normalize_agent_skill_ids(
            data.get("secondary_skills", [])
        )
        ordered: list[str] = []
        if base_skill:
            ordered.append(base_skill)
        ordered.extend(secondary)
        deduped: list[str] = []
        for skill_id in ordered:
            if skill_id and skill_id not in deduped:
                deduped.append(skill_id)
        return deduped

    @staticmethod
    def _build_contract_schema(items: list[dict]) -> dict:
        """Derive JSON-schema-like contract metadata from structured I/O rows."""
        normalized = [item for item in (items or []) if str(item.get("name") or "").strip()]
        properties = {}
        for item in normalized:
            prop = {
                "type": item.get("type", "text"),
                "description": item.get("description", ""),
            }
            contract_id = str(item.get("contract_id") or "").strip()
            if contract_id:
                prop["contract_id"] = contract_id
            properties[item["name"]] = prop
        required = [item["name"] for item in normalized if item.get("required", False)]
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def get_data_contract(self, contract_id: str, include_trashed: bool = False) -> dict | None:
        clauses = ["contract_id = %s"]
        params: list[Any] = [contract_id]
        if not include_trashed and self._column_exists("registry_data_contracts", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        where_sql = SQL_AND.join(clauses)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM registry_data_contracts WHERE {where_sql}",
                tuple(params),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_data_contracts(
        self,
        enabled_only: bool = False,
        *,
        ensure_projection: bool = True,
        include_trashed: bool = False,
    ) -> list[dict]:
        if ensure_projection:
            self._ensure_public_contract_projection()
        clauses: list[str] = []
        if enabled_only:
            clauses.append(SQL_ENABLED_TRUE)
        if not include_trashed and self._column_exists("registry_data_contracts", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM registry_data_contracts {where_sql} ORDER BY contract_id")
            return [dict(r) for r in cur.fetchall()]

    def save_data_contract(self, data: dict, author: str = "admin") -> None:
        contract_id = str(data.get("contract_id") or "").strip()
        if not contract_id:
            raise ValueError("contract_id is required")
        is_new = not self._exists("registry_data_contracts", "contract_id", contract_id)
        existing = self.get_data_contract(contract_id) if not is_new else {}
        existing_map: dict[str, Any] = existing if isinstance(existing, dict) else {}
        schema = data["schema"] if "schema" in data else data.get("schema_json", {})
        if schema is None:
            schema = {}
        last_sync_error = data.get("last_sync_error", existing_map.get("last_sync_error"))
        snapshot = {
            "contract_id": contract_id,
            "version": data.get("version", "1.0.0"),
            "display_name": data.get("display_name", contract_id),
            "description": data.get("description", ""),
            "kind": data.get("kind", "json"),
            "schema_json": schema,
            "enabled": bool(data.get("enabled", True)),
            "tags": list(data.get("tags") or []),
            "projection_mode": str(
                data["projection_mode"]
                if "projection_mode" in data
                else existing_map.get("projection_mode", "manual")
            ).strip()
            or "manual",
            "derived_from_kind": str(
                data["derived_from_kind"]
                if "derived_from_kind" in data
                else existing_map.get("derived_from_kind", "")
            ).strip(),
            "derived_from_ref": str(
                data["derived_from_ref"]
                if "derived_from_ref" in data
                else existing_map.get("derived_from_ref", "")
            ).strip(),
            "derived_hash": str(
                data["derived_hash"]
                if "derived_hash" in data
                else existing_map.get("derived_hash", "")
            ).strip(),
            "sync_status": str(
                data["sync_status"]
                if "sync_status" in data
                else existing_map.get("sync_status", "active")
            ).strip()
            or "active",
            "last_synced_at": (
                data["last_synced_at"]
                if "last_synced_at" in data
                else existing_map.get("last_synced_at")
            ),
            "last_sync_error": (
                None if last_sync_error is None else str(last_sync_error).strip() or None
            ),
        }
        if snapshot["projection_mode"] == "derived" and snapshot["last_synced_at"] is None:
            snapshot["last_synced_at"] = datetime.now(UTC)
        with self._transaction() as cur:
            cur.execute(
                """INSERT INTO registry_data_contracts
                   (contract_id, version, display_name, description, kind, schema_json, enabled, tags,
                    projection_mode, derived_from_kind, derived_from_ref, derived_hash,
                    sync_status, last_synced_at, last_sync_error)
                   VALUES (%(contract_id)s, %(version)s, %(display_name)s, %(description)s,
                           %(kind)s, %(schema_json)s, %(enabled)s, %(tags)s,
                           %(projection_mode)s, %(derived_from_kind)s, %(derived_from_ref)s, %(derived_hash)s,
                           %(sync_status)s, %(last_synced_at)s, %(last_sync_error)s)
                   ON CONFLICT (contract_id) DO UPDATE SET
                       version = EXCLUDED.version,
                       display_name = EXCLUDED.display_name,
                       description = EXCLUDED.description,
                       kind = EXCLUDED.kind,
                       schema_json = EXCLUDED.schema_json,
                       enabled = EXCLUDED.enabled,
                       tags = EXCLUDED.tags,
                       projection_mode = EXCLUDED.projection_mode,
                       derived_from_kind = EXCLUDED.derived_from_kind,
                       derived_from_ref = EXCLUDED.derived_from_ref,
                       derived_hash = EXCLUDED.derived_hash,
                       sync_status = EXCLUDED.sync_status,
                       last_synced_at = EXCLUDED.last_synced_at,
                       last_sync_error = EXCLUDED.last_sync_error""",
                {
                    **snapshot,
                    "schema_json": json.dumps(snapshot["schema_json"]),
                },
            )
            self._log_change_cur(
                cur,
                "data_contract",
                contract_id,
                "create" if is_new else "update",
                snapshot,
                author,
            )

    def toggle_data_contract(self, contract_id: str) -> bool:
        has_deleted_at = self._column_exists("registry_data_contracts", "deleted_at")
        with self._cursor() as cur:
            if has_deleted_at:
                cur.execute(
                    "UPDATE registry_data_contracts SET enabled = NOT enabled "
                    "WHERE contract_id = %s AND deleted_at IS NULL RETURNING enabled",
                    (contract_id,),
                )
            else:
                cur.execute(
                    "UPDATE registry_data_contracts SET enabled = NOT enabled WHERE contract_id = %s RETURNING enabled",
                    (contract_id,),
                )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Data contract '{contract_id}' not found")
            return bool(row["enabled"])

    def delete_data_contract(self, contract_id: str, author: str = "admin") -> None:
        snapshot = self.get_data_contract(contract_id)
        if not snapshot:
            raise KeyError(f"Data contract '{contract_id}' not found")
        has_deleted_at = self._column_exists("registry_data_contracts", "deleted_at")
        has_deleted_by = self._column_exists("registry_data_contracts", "deleted_by")
        with self._transaction() as cur:
            if has_deleted_at and has_deleted_by:
                cur.execute(
                    "UPDATE registry_data_contracts SET deleted_at = NOW(), deleted_by = %s "
                    "WHERE contract_id = %s AND deleted_at IS NULL",
                    (author, contract_id),
                )
            elif has_deleted_at:
                cur.execute(
                    "UPDATE registry_data_contracts SET deleted_at = NOW() "
                    "WHERE contract_id = %s AND deleted_at IS NULL",
                    (contract_id,),
                )
            else:
                cur.execute(
                    "DELETE FROM registry_data_contracts WHERE contract_id = %s", (contract_id,)
                )
            self._log_change_cur(cur, "data_contract", contract_id, "delete", snapshot, author)

    @staticmethod
    def _normalize_workspace_row(row: dict | None) -> dict | None:
        if not row:
            return None
        payload = dict(row)
        value = payload.get("metadata_jsonb")
        if isinstance(value, str):
            try:
                payload["metadata_jsonb"] = json.loads(value)
            except Exception:
                payload["metadata_jsonb"] = {}
        elif not isinstance(value, dict):
            payload["metadata_jsonb"] = {}
        payload["id"] = str(payload.get("id") or "").strip()
        payload["tenant_id"] = str(payload.get("tenant_id") or "").strip()
        payload["account_id"] = str(payload.get("account_id") or "").strip()
        payload["workspace_key"] = str(payload.get("workspace_key") or "").strip()
        payload["display_name"] = str(payload.get("display_name") or "").strip()
        payload["status"] = str(payload.get("status") or "active").strip() or "active"
        payload["projects_count"] = int(payload.get("projects_count") or 0)
        payload["jobs_count"] = int(payload.get("jobs_count") or 0)
        return payload

    def list_workspaces(
        self,
        *,
        account_id: str,
        tenant_id: str,
        search: str = "",
        status: str = "",
        include_archived: bool = False,
    ) -> list[dict]:
        if not self._table_exists("workspaces"):
            return []
        clauses = ["w.account_id = %s", "w.tenant_id = %s"]
        params: list[Any] = [account_id, tenant_id]
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            if normalized_status in {"trashed", "deleted", "archived"}:
                clauses.append("w.deleted_at IS NOT NULL")
            else:
                clauses.append(W_DELETED_AT_IS_NULL_VALUE)
        elif not include_archived:
            clauses.append(W_DELETED_AT_IS_NULL_VALUE)
        normalized_search = str(search or "").strip()
        if normalized_search:
            like = f"%{normalized_search}%"
            clauses.append(
                "(w.display_name ILIKE %s OR w.workspace_key ILIKE %s OR "
                "COALESCE(w.description, '') ILIKE %s)"
            )
            params.extend([like, like, like])
        where_sql = f"WHERE {SQL_AND.join(clauses)}"
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    w.*,
                    COUNT(DISTINCT p.id) AS projects_count,
                    COUNT(DISTINCT j.id) AS jobs_count
                FROM workspaces w
                LEFT JOIN projects p ON p.workspace_id = w.id AND p.deleted_at IS NULL
                LEFT JOIN jobs j ON j.workspace_id = w.id
                {where_sql}
                GROUP BY w.id
                ORDER BY w.updated_at DESC, w.display_name
                """,
                tuple(params),
            )
            return [
                normalized
                for row in cur.fetchall()
                if (normalized := self._normalize_workspace_row(dict(row))) is not None
            ]

    def get_workspace(
        self,
        workspace_id: str,
        *,
        account_id: str,
        tenant_id: str,
        include_archived: bool = False,
    ) -> dict | None:
        rows = self.list_workspaces(
            account_id=account_id,
            tenant_id=tenant_id,
            search="",
            status="" if include_archived else "active",
            include_archived=include_archived,
        )
        normalized_id = str(workspace_id or "").strip()
        return next((row for row in rows if str(row.get("id") or "") == normalized_id), None)

    def save_workspace(
        self,
        payload: dict,
        *,
        account_id: str,
        tenant_id: str,
        author: str = "admin",
    ) -> dict:
        if not self._table_exists("workspaces"):
            raise RuntimeError("Workspace table is not available")
        workspace_id = str(payload.get("id") or "").strip()
        workspace_key = str(payload.get("workspace_key") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        if not workspace_key:
            raise ValueError("workspace_key is required")
        if not display_name:
            raise ValueError("display_name is required")
        status = "active"
        description = str(payload.get("description") or "").strip()
        metadata = payload.get("metadata_jsonb") or payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata_jsonb must be an object")

        with self._transaction() as cur:
            if workspace_id:
                cur.execute(
                    """
                    INSERT INTO workspaces (
                        id, tenant_id, account_id, workspace_key, display_name,
                        description, status, metadata_jsonb, deleted_at, deleted_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, NULL, NULL)
                    ON CONFLICT (id) DO UPDATE SET
                        workspace_key = EXCLUDED.workspace_key,
                        display_name = EXCLUDED.display_name,
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        metadata_jsonb = EXCLUDED.metadata_jsonb,
                        deleted_at = NULL,
                        deleted_by = NULL,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        workspace_id,
                        tenant_id,
                        account_id,
                        workspace_key,
                        display_name,
                        description,
                        status,
                        json.dumps(metadata),
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO workspaces (
                        tenant_id, account_id, workspace_key, display_name,
                        description, status, metadata_jsonb
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, account_id, workspace_key) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        description = EXCLUDED.description,
                        status = EXCLUDED.status,
                        metadata_jsonb = EXCLUDED.metadata_jsonb,
                        deleted_at = NULL,
                        deleted_by = NULL,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        account_id,
                        workspace_key,
                        display_name,
                        description,
                        status,
                        json.dumps(metadata),
                    ),
                )
            row = dict(cur.fetchone() or {})
            self._log_change_cur(
                cur,
                "workspace",
                str(row.get("id") or workspace_id or workspace_key),
                "upsert",
                row,
                author,
            )
        saved = self.get_workspace(
            str(row.get("id") or workspace_id),
            account_id=account_id,
            tenant_id=tenant_id,
            include_archived=True,
        )
        return saved or self._normalize_workspace_row(row) or {}

    def create_workspace(
        self,
        payload: dict,
        *,
        account_id: str,
        tenant_id: str,
        author: str = "admin",
    ) -> dict | None:
        """Insert a workspace without changing an existing workspace on key conflict."""
        if not self._table_exists("workspaces"):
            raise RuntimeError("Workspace table is not available")
        workspace_key = str(payload.get("workspace_key") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        if not workspace_key:
            raise ValueError("workspace_key is required")
        if not display_name:
            raise ValueError("display_name is required")
        description = str(payload.get("description") or "").strip()
        metadata = payload.get("metadata_jsonb") or payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata_jsonb must be an object")

        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO workspaces (
                    tenant_id, account_id, workspace_key, display_name,
                    description, status, metadata_jsonb
                )
                VALUES (%s, %s, %s, %s, %s, 'active', %s::jsonb)
                ON CONFLICT (tenant_id, account_id, workspace_key) DO NOTHING
                RETURNING *
                """,
                (
                    tenant_id,
                    account_id,
                    workspace_key,
                    display_name,
                    description,
                    json.dumps(metadata),
                ),
            )
            row = dict(cur.fetchone() or {})
            if not row:
                return None
            self._log_change_cur(
                cur,
                "workspace",
                str(row.get("id") or workspace_key),
                "create",
                row,
                author,
            )
        saved = self.get_workspace(
            str(row.get("id") or ""),
            account_id=account_id,
            tenant_id=tenant_id,
            include_archived=True,
        )
        return saved or self._normalize_workspace_row(row)

    def set_workspace_archived(
        self,
        workspace_id: str,
        *,
        archived: bool,
        account_id: str,
        tenant_id: str,
        author: str = "admin",
    ) -> dict:
        with self._transaction() as cur:
            cur.execute(
                """
                UPDATE workspaces
                   SET status = 'active',
                       deleted_at = CASE WHEN %s THEN now() ELSE NULL END,
                       deleted_by = CASE WHEN %s THEN %s ELSE NULL END,
                       updated_at = now()
                 WHERE id = %s
                   AND account_id = %s
                   AND tenant_id = %s
                RETURNING *
                """,
                (archived, archived, author, workspace_id, account_id, tenant_id),
            )
            row = dict(cur.fetchone() or {})
            if not row:
                raise KeyError(workspace_id)
            self._log_change_cur(
                cur,
                "workspace",
                str(row.get("id") or workspace_id),
                "trash" if archived else "restore",
                row,
                author,
            )
        return self._normalize_workspace_row(row) or {}

    def list_workspace_projects(
        self, workspace_id: str, *, account_id: str, tenant_id: str
    ) -> list[dict]:
        if not self._table_exists("projects"):
            return []
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT p.*, COUNT(j.id) AS job_count
                  FROM projects p
                  LEFT JOIN jobs j ON j.project_id = p.id
                 WHERE p.workspace_id = %s
                   AND p.account_id = %s
                   AND COALESCE(p.tenant_id, %s) = %s
                   AND p.deleted_at IS NULL
                 GROUP BY p.id
                 ORDER BY p.name
                """,
                (workspace_id, account_id, tenant_id, tenant_id),
            )
            return [dict(row) for row in cur.fetchall()]


__all__ = [
    "RegistryDBStoreContractsMixin",
]
