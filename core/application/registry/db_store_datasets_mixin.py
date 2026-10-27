# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .db_store_common import *  # NOSONAR


class RegistryDBStoreDatasetsMixin:
    def save_workspace_data_connection_with_secrets(
        self,
        data: dict,
        *,
        secret_values: dict[str, str],
        author: str = "admin",
    ) -> dict:
        """Persist credentials and their owning connection in one transaction."""
        from core.application.management.project_connection_secrets import (
            save_connection_secrets,
        )

        payload = dict(data)
        existing_refs = self._json_object(payload.get("secret_refs_jsonb")).get("refs") or {}
        if not isinstance(existing_refs, dict):
            existing_refs = {}
        with self._transaction():
            if secret_values:
                saved = save_connection_secrets(
                    self._get_tx_conn(),
                    account_id=str(payload.get("account_id") or "").strip(),
                    connection_id=str(payload.get("connection_id") or "").strip(),
                    secrets=secret_values,
                )
                saved_refs = self._json_object(saved).get("refs") or {}
                if not isinstance(saved_refs, dict):
                    saved_refs = {}
                payload["secret_refs_jsonb"] = {"refs": {**existing_refs, **saved_refs}}
            else:
                payload["secret_refs_jsonb"] = {"refs": existing_refs} if existing_refs else {}
            return self.save_workspace_data_connection(payload, author=author)

    def get_extension_registry_projection(
        self,
        *,
        account_id: str,
        install_id: str,
        artifact_kind: str,
        artifact_id: str,
    ) -> dict | None:
        if not self._table_exists("extension_registry_projections"):
            return None
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT projection_id::text AS projection_id,
                       account_id::text AS account_id,
                       install_id,
                       release_id,
                       extension_id,
                       artifact_kind,
                       artifact_id,
                       artifact_ref,
                       projection_jsonb,
                       is_active,
                       availability_status,
                       availability_reason_code,
                       availability_reason_text,
                       new_runs_blocked,
                       visible_in_catalog
                  FROM extension_registry_projections
                 WHERE account_id::text = %s
                   AND install_id = %s
                   AND artifact_kind = %s
                   AND artifact_id = %s
                   AND is_active = true
                   AND EXISTS (
                       SELECT 1
                         FROM extension_installs AS install
                        WHERE install.install_id = extension_registry_projections.install_id
                          AND install.account_id = extension_registry_projections.account_id
                          AND install.release_id = extension_registry_projections.release_id
                          AND install.state = 'active'
                   )
                 LIMIT 1
                """,
                (account_id, install_id, artifact_kind, artifact_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        payload = dict(row)
        payload["projection"] = self._json_object(payload.pop("projection_jsonb", {}))
        return payload

    @staticmethod
    def _normalize_dataset_project_ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        project_ids: list[str] = []
        for item in value:
            parsed = str(item or "").strip()
            if parsed and parsed not in project_ids:
                project_ids.append(parsed)
        return project_ids

    def _dataset_connection_workspace_map(self, connection_ids: set[str]) -> dict[str, str]:
        if not connection_ids or not self._table_exists("workspace_data_connections"):
            return {}
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT connection_id::text AS connection_id, workspace_id::text AS workspace_id
                  FROM workspace_data_connections
                 WHERE connection_id::text = ANY(%s)
                   AND deleted_at IS NULL
                """,
                (sorted(connection_ids),),
            )
            return {
                str(row.get("connection_id") or "").strip(): str(
                    row.get("workspace_id") or ""
                ).strip()
                for row in cur.fetchall()
                if str(row.get("connection_id") or "").strip()
                and str(row.get("workspace_id") or "").strip()
            }

    def _dataset_project_workspace_map(self, project_ids: set[str]) -> dict[str, str]:
        if not project_ids or not self._table_exists("projects"):
            return {}
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS project_id, workspace_id::text AS workspace_id
                  FROM projects
                 WHERE id::text = ANY(%s)
                   AND deleted_at IS NULL
                """,
                (sorted(project_ids),),
            )
            return {
                str(row.get("project_id") or "").strip(): str(row.get("workspace_id") or "").strip()
                for row in cur.fetchall()
                if str(row.get("project_id") or "").strip()
            }

    def _dataset_workspace_scope_map(self, workspace_ids: set[str]) -> dict[str, dict[str, str]]:
        if not workspace_ids or not self._table_exists("workspaces"):
            return {}
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    id::text AS workspace_id,
                    COALESCE(account_id::text, '') AS account_id,
                    COALESCE(tenant_id::text, '') AS tenant_id
                  FROM workspaces
                 WHERE id::text = ANY(%s)
                   AND deleted_at IS NULL
                """,
                (sorted(workspace_ids),),
            )
            return {
                str(row.get("workspace_id") or "").strip(): {
                    "account_id": str(row.get("account_id") or "").strip(),
                    "tenant_id": str(row.get("tenant_id") or "").strip(),
                }
                for row in cur.fetchall()
                if str(row.get("workspace_id") or "").strip()
            }

    def _get_project_scope(
        self,
        project_id: str,
        *,
        include_trashed: bool = False,
        account_id: str | None = None,
    ) -> dict[str, str] | None:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id or not self._table_exists("projects"):
            return None
        normalized_account_id = str(account_id or "").strip() or None
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    id::text AS project_id,
                    workspace_id::text AS workspace_id,
                    COALESCE(tenant_id::text, '') AS tenant_id,
                    COALESCE(account_id::text, '') AS account_id,
                    COALESCE(name, '') AS project_name
                  FROM projects
                 WHERE id::text = %s
                   AND (%s OR deleted_at IS NULL)
                   AND (NULLIF(%s, '') IS NULL OR account_id::text = %s)
                 LIMIT 1
                """,
                (
                    normalized_project_id,
                    bool(include_trashed),
                    normalized_account_id or "",
                    normalized_account_id or "",
                ),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _normalize_dataset_row(
        self,
        row: dict | None,
        *,
        connection_workspace_map: dict[str, str] | None = None,
        project_workspace_map: dict[str, str] | None = None,
        workspace_scope_map: dict[str, dict[str, str]] | None = None,
    ) -> dict | None:
        if not row:
            return None
        payload = dict(row)
        for key in ("schema_snapshot_jsonb", "connection_schema_jsonb", "metadata_jsonb"):
            value = payload.get(key)
            if isinstance(value, str):
                try:
                    payload[key] = json.loads(value)
                except Exception:
                    payload[key] = {}
            elif not isinstance(value, dict):
                payload[key] = {}
        payload["project_ids"] = type(self)._normalize_dataset_project_ids(
            payload.get("project_ids")
        )
        metadata_jsonb: dict[str, Any] = (
            payload.get("metadata_jsonb") if isinstance(payload.get("metadata_jsonb"), dict) else {}
        )
        workspace_id = str(
            payload.get("workspace_id") or metadata_jsonb.get("workspace_id") or ""
        ).strip()
        resolution_status = "explicit" if workspace_id else ""
        if not workspace_id:
            connection_id = str(metadata_jsonb.get("connection_id") or "").strip()
            derived_workspace_id = (
                str((connection_workspace_map or {}).get(connection_id) or "").strip()
                if connection_id
                else ""
            )
            if derived_workspace_id:
                workspace_id = derived_workspace_id
                resolution_status = "derived_from_connection"
            else:
                project_workspace_ids = sorted(
                    {
                        str((project_workspace_map or {}).get(project_id) or "").strip()
                        for project_id in payload["project_ids"]
                        if str((project_workspace_map or {}).get(project_id) or "").strip()
                    }
                )
                if len(project_workspace_ids) == 1:
                    workspace_id = project_workspace_ids[0]
                    resolution_status = "derived_from_projects"
                else:
                    resolution_status = "unresolved"
        availability_mode = (
            str(payload.get("availability_mode") or metadata_jsonb.get("availability_mode") or "")
            .strip()
            .lower()
        )
        if availability_mode not in {
            "all_workspace_projects",
            "specific_projects_only",
        }:
            availability_mode = "specific_projects_only"
        payload["workspace_id"] = workspace_id
        workspace_scope = (workspace_scope_map or {}).get(workspace_id) or {}
        payload["account_id"] = str(
            payload.get("account_id")
            or metadata_jsonb.get("account_id")
            or workspace_scope.get("account_id")
            or ""
        ).strip()
        payload["tenant_id"] = str(
            payload.get("tenant_id") or workspace_scope.get("tenant_id") or ""
        ).strip()
        payload["availability_mode"] = availability_mode
        payload["workspace_resolution_status"] = resolution_status or (
            "resolved" if workspace_id else "unresolved"
        )
        payload["workspace_unresolved"] = not bool(workspace_id)
        return payload

    @staticmethod
    def _json_object(value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _bool_value(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return bool(value)

    @classmethod
    def _dataset_connectors_from_descriptor_row(cls, row: dict) -> list[dict]:
        descriptor = cls._json_object(row.get("descriptor_jsonb"))
        contract = cls._json_object(descriptor.get("extension_contract_v2_normalized"))
        if not contract:
            runtime = cls._json_object(descriptor.get("runtime"))
            contract = cls._json_object(runtime.get("extension_contract_v2_normalized"))
        if not contract:
            contract = cls._json_object(descriptor.get("extension_contract_v2"))
        if not contract:
            runtime = cls._json_object(descriptor.get("runtime"))
            contract = cls._json_object(runtime.get("extension_contract_v2"))
        connectors = contract.get("dataset_connectors")
        if not isinstance(connectors, list):
            return []

        install_id = str(row.get("install_id") or "").strip()
        release_id = str(row.get("release_id") or "").strip()
        extension_id = str(row.get("extension_id") or "").strip()
        account_id = str(row.get("account_id") or "").strip()
        projected: list[dict] = []
        for connector in connectors:
            if not isinstance(connector, dict):
                continue
            local_connector_id = str(connector.get("connector_id") or "").strip()
            if not local_connector_id:
                continue
            raw_connector_catalog = connector.get("connector_catalog")
            connector_catalog = (
                raw_connector_catalog if isinstance(raw_connector_catalog, list) else []
            )
            connector_id = (
                f"extension.{install_id}.{local_connector_id}"
                if install_id
                else f"extension.{extension_id}.{local_connector_id}"
            )
            projected.append(
                {
                    "connector_id": connector_id,
                    "local_connector_id": local_connector_id,
                    "display_name": str(
                        connector.get("display_name") or connector.get("name") or local_connector_id
                    ).strip(),
                    "description": str(connector.get("description") or "").strip(),
                    "connector_kind": "extension",
                    "runtime_kind": str(
                        connector.get("runtime_kind")
                        or row.get("runtime_mode")
                        or "local_extension"
                    ).strip()
                    or "local_extension",
                    "query_language": str(connector.get("query_language") or "sql").strip()
                    or "sql",
                    "connection_schema": cls._json_object(connector.get("connection_schema")),
                    "secret_schema": cls._json_object(connector.get("secret_schema")),
                    "capabilities": cls._json_object(connector.get("capabilities")),
                    "safety_profile": cls._json_object(connector.get("safety_profile")),
                    "preview_support": cls._bool_value(connector.get("preview_support")),
                    "schema_refresh_support": cls._bool_value(
                        connector.get("schema_refresh_support")
                    ),
                    "runtime_adapter_ref": str(connector.get("runtime_adapter_ref") or "").strip(),
                    "connector_catalog": list(connector_catalog),
                    "source_install_id": install_id,
                    "source_release_id": release_id,
                    "extension_id": extension_id,
                    "account_id": account_id,
                }
            )
        return projected

    def _dataset_project_ids(self, dataset_id: str) -> list[str]:
        if not self._table_exists("project_datasets"):
            return []
        with self._cursor() as cur:
            cur.execute(
                "SELECT project_id FROM project_datasets WHERE dataset_id = %s ORDER BY project_id",
                (dataset_id,),
            )
            return [
                str(row["project_id"])
                for row in cur.fetchall()
                if row.get("project_id") is not None
            ]

    def get_dataset(self, dataset_id: str, include_trashed: bool = False) -> dict | None:
        if not self._table_exists("registry_datasets"):
            return None
        clauses = ["dataset_id = %s"]
        params: list[Any] = [dataset_id]
        if not include_trashed and self._column_exists("registry_datasets", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        where_sql = SQL_AND.join(clauses)
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    d.*,
                    COALESCE(pd_agg.project_ids, '{{}}'::uuid[]) AS project_ids
                FROM registry_datasets d
                LEFT JOIN LATERAL (
                    SELECT ARRAY_AGG(pd.project_id ORDER BY pd.project_id) AS project_ids
                    FROM project_datasets pd
                    WHERE pd.dataset_id = d.dataset_id
                ) pd_agg ON TRUE
                WHERE {where_sql}
                LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                return None
            raw_row = dict(row)
            metadata = self._json_object(raw_row.get("metadata_jsonb"))
            connection_id = str(metadata.get("connection_id") or "").strip()
            project_ids = self._normalize_dataset_project_ids(raw_row.get("project_ids"))
            connection_workspace_map = self._dataset_connection_workspace_map(
                {connection_id} if connection_id else set()
            )
            project_workspace_map = self._dataset_project_workspace_map(set(project_ids))
            workspace_ids = {
                str(raw_row.get("workspace_id") or metadata.get("workspace_id") or "").strip()
            }
            workspace_ids.update(
                str(item or "").strip() for item in connection_workspace_map.values()
            )
            workspace_ids.update(str(item or "").strip() for item in project_workspace_map.values())
            return self._normalize_dataset_row(
                raw_row,
                connection_workspace_map=connection_workspace_map,
                project_workspace_map=project_workspace_map,
                workspace_scope_map=self._dataset_workspace_scope_map(
                    {item for item in workspace_ids if item}
                ),
            )

    def list_datasets(
        self,
        enabled_only: bool = False,
        *,
        project_id: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict]:
        if not self._table_exists("registry_datasets"):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append(f"d.{SQL_ENABLED_TRUE}")
        if not include_trashed and self._column_exists("registry_datasets", "deleted_at"):
            clauses.append(f"d.{SQL_DELETED_AT_IS_NULL}")
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    d.*,
                    COALESCE(pd_agg.project_ids, '{{}}'::uuid[]) AS project_ids
                FROM registry_datasets d
                LEFT JOIN LATERAL (
                    SELECT ARRAY_AGG(pd.project_id ORDER BY pd.project_id) AS project_ids
                    FROM project_datasets pd
                    WHERE pd.dataset_id = d.dataset_id
                ) pd_agg ON TRUE
                {where_sql}
                ORDER BY d.display_name, d.dataset_id
                """,
                tuple(params),
            )
            rows = [dict(row) for row in cur.fetchall()]
        connection_ids: set[str] = set()
        project_ids: set[str] = set()
        for row in rows:
            metadata = self._json_object(row.get("metadata_jsonb"))
            connection_id = str(metadata.get("connection_id") or "").strip()
            if connection_id:
                connection_ids.add(connection_id)
            project_ids.update(self._normalize_dataset_project_ids(row.get("project_ids")))
        connection_workspace_map = self._dataset_connection_workspace_map(connection_ids)
        project_workspace_map = self._dataset_project_workspace_map(project_ids)
        workspace_ids = {
            str(
                row.get("workspace_id")
                or self._json_object(row.get("metadata_jsonb")).get("workspace_id")
                or ""
            ).strip()
            for row in rows
        }
        workspace_ids.update(str(item or "").strip() for item in connection_workspace_map.values())
        workspace_ids.update(str(item or "").strip() for item in project_workspace_map.values())
        workspace_scope_map = self._dataset_workspace_scope_map(
            {item for item in workspace_ids if item}
        )
        normalized_rows = [
            normalized
            for row in rows
            if (
                normalized := self._normalize_dataset_row(
                    row,
                    connection_workspace_map=connection_workspace_map,
                    project_workspace_map=project_workspace_map,
                    workspace_scope_map=workspace_scope_map,
                )
            )
            is not None
        ]
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return normalized_rows
        project_scope = self._get_project_scope(normalized_project_id)
        if not project_scope:
            return []
        project_workspace_id = str(project_scope.get("workspace_id") or "").strip()
        return [
            row
            for row in normalized_rows
            if str(row.get("workspace_id") or "").strip() == project_workspace_id
            and (
                str(row.get("availability_mode") or "specific_projects_only")
                == "all_workspace_projects"
                or normalized_project_id
                in self._normalize_dataset_project_ids(row.get("project_ids"))
            )
        ]

    def list_dataset_connectors(self) -> list[dict]:
        if not self._table_exists("extension_installs") or not self._table_exists(
            "extension_releases"
        ):
            return []
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    pi.install_id,
                    pi.extension_id,
                    COALESCE(pi.account_id::text, '') AS account_id,
                    pi.release_id,
                    pi.runtime_mode,
                    pr.descriptor_jsonb
                FROM extension_installs pi
                JOIN extension_releases pr ON pr.release_id = pi.release_id
                WHERE pi.state = 'active'
                ORDER BY pi.extension_id, pi.install_id
                """
            )
            rows = [dict(row) for row in cur.fetchall()]

        connectors: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            for connector in self._dataset_connectors_from_descriptor_row(row):
                connector_id = str(connector.get("connector_id") or "").strip()
                if not connector_id or connector_id in seen:
                    continue
                connectors.append(connector)
                seen.add(connector_id)
        connectors.sort(
            key=lambda item: (
                str(item.get("display_name") or "").lower(),
                str(item.get("connector_id") or ""),
            )
        )
        return connectors

    def list_workspace_data_connections(
        self,
        *,
        workspace_id: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict]:
        if not self._table_exists("workspace_data_connections"):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        normalized_workspace_id = str(workspace_id or "").strip()
        if normalized_workspace_id:
            clauses.append("c.workspace_id = %s")
            params.append(normalized_workspace_id)
        if not include_trashed and self._column_exists("workspace_data_connections", "deleted_at"):
            clauses.append(f"c.{SQL_DELETED_AT_IS_NULL}")
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        linked_dataset_join = (
            """
            LEFT JOIN (
                SELECT
                    metadata_jsonb->>'connection_id' AS connection_id,
                    COUNT(*) AS linked_dataset_count
                FROM registry_datasets
                WHERE metadata_jsonb ? 'connection_id'
                  AND deleted_at IS NULL
                GROUP BY metadata_jsonb->>'connection_id'
            ) ds ON ds.connection_id = c.connection_id::text
            """
            if self._table_exists("registry_datasets")
            else ""
        )
        linked_dataset_select = (
            "COALESCE(ds.linked_dataset_count, 0) AS linked_dataset_count"
            if self._table_exists("registry_datasets")
            else "0 AS linked_dataset_count"
        )
        workspace_join = (
            "LEFT JOIN workspaces w ON w.id = c.workspace_id"
            if self._table_exists("workspaces")
            else ""
        )
        workspace_select = (
            "COALESCE(w.display_name, '') AS workspace_display_name"
            if self._table_exists("workspaces")
            else "'' AS workspace_display_name"
        )
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    c.*,
                    {workspace_select},
                    {linked_dataset_select}
                FROM workspace_data_connections c
                {workspace_join}
                {linked_dataset_join}
                {where_sql}
                ORDER BY c.updated_at DESC, c.display_name, c.connection_id
                """,
                tuple(params),
            )
            return [
                self._normalize_workspace_data_connection_row(dict(row)) for row in cur.fetchall()
            ]

    def get_workspace_data_connection(
        self,
        connection_id: str,
        *,
        include_trashed: bool = False,
    ) -> dict | None:
        normalized_connection_id = str(connection_id or "").strip()
        if not normalized_connection_id:
            return None
        rows = self.list_workspace_data_connections(include_trashed=include_trashed)
        return next(
            (
                row
                for row in rows
                if str(row.get("connection_id") or "").strip() == normalized_connection_id
            ),
            None,
        )

    def save_workspace_data_connection(self, data: dict, author: str = "admin") -> dict:
        if not self._table_exists("workspace_data_connections"):
            raise RuntimeError("Workspace data connection table is not available")
        connection_id = str(data.get("connection_id") or "").strip()
        workspace_id = str(data.get("workspace_id") or "").strip()
        display_name = str(data.get("display_name") or "").strip()
        tenant_id = str(data.get("tenant_id") or "").strip()
        account_id = str(data.get("account_id") or "").strip()
        if not connection_id:
            raise ValueError("connection_id is required")
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not account_id:
            raise ValueError("account_id is required")
        if not display_name:
            raise ValueError("display_name is required")

        snapshot = {
            "connection_id": connection_id,
            "tenant_id": tenant_id,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "display_name": display_name,
            "description": str(data.get("description") or "").strip(),
            "connector_ref_jsonb": self._json_object(data.get("connector_ref_jsonb")),
            "source_kind": str(data.get("source_kind") or "").strip(),
            "environment": str(data.get("environment") or "dev").strip() or "dev",
            "tags": list(data.get("tags") or []),
            "access_mode": "read_only",
            "enabled": bool(data.get("enabled", True)),
            "health_status": str(data.get("health_status") or "unknown").strip() or "unknown",
            "last_checked_at": data.get("last_checked_at"),
            "last_schema_refresh_at": data.get("last_schema_refresh_at"),
            "last_error": str(data.get("last_error") or "").strip(),
            "connection_config_jsonb": self._json_object(data.get("connection_config_jsonb")),
            "secret_refs_jsonb": self._json_object(data.get("secret_refs_jsonb")),
            "metadata_jsonb": self._json_object(data.get("metadata_jsonb")),
        }
        is_new = not self.get_workspace_data_connection(connection_id, include_trashed=True)
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO workspace_data_connections (
                    connection_id, tenant_id, account_id, workspace_id, display_name,
                    description, connector_ref_jsonb, source_kind, environment, tags,
                    access_mode, enabled, health_status, last_checked_at,
                    last_schema_refresh_at, last_error, connection_config_jsonb,
                    secret_refs_jsonb, metadata_jsonb, deleted_at
                )
                VALUES (
                    %(connection_id)s, %(tenant_id)s, %(account_id)s, %(workspace_id)s,
                    %(display_name)s, %(description)s, %(connector_ref_jsonb)s,
                    %(source_kind)s, %(environment)s, %(tags)s, %(access_mode)s,
                    %(enabled)s, %(health_status)s, %(last_checked_at)s,
                    %(last_schema_refresh_at)s, %(last_error)s, %(connection_config_jsonb)s,
                    %(secret_refs_jsonb)s, %(metadata_jsonb)s, NULL
                )
                ON CONFLICT (connection_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    account_id = EXCLUDED.account_id,
                    workspace_id = EXCLUDED.workspace_id,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    connector_ref_jsonb = EXCLUDED.connector_ref_jsonb,
                    source_kind = EXCLUDED.source_kind,
                    environment = EXCLUDED.environment,
                    tags = EXCLUDED.tags,
                    access_mode = EXCLUDED.access_mode,
                    enabled = EXCLUDED.enabled,
                    health_status = EXCLUDED.health_status,
                    last_checked_at = EXCLUDED.last_checked_at,
                    last_schema_refresh_at = EXCLUDED.last_schema_refresh_at,
                    last_error = EXCLUDED.last_error,
                    connection_config_jsonb = EXCLUDED.connection_config_jsonb,
                    secret_refs_jsonb = EXCLUDED.secret_refs_jsonb,
                    metadata_jsonb = EXCLUDED.metadata_jsonb,
                    deleted_at = NULL,
                    updated_at = now()
                """,
                {
                    **snapshot,
                    "connector_ref_jsonb": json.dumps(snapshot["connector_ref_jsonb"]),
                    "connection_config_jsonb": json.dumps(snapshot["connection_config_jsonb"]),
                    "secret_refs_jsonb": json.dumps(snapshot["secret_refs_jsonb"]),
                    "metadata_jsonb": json.dumps(snapshot["metadata_jsonb"]),
                },
            )
            self._log_change_cur(
                cur,
                "workspace_data_connection",
                connection_id,
                "create" if is_new else "update",
                snapshot,
                author,
            )

        row = self.get_workspace_data_connection(connection_id, include_trashed=True)
        return row or dict(snapshot)

    def toggle_workspace_data_connection(self, connection_id: str) -> bool:
        if not self._table_exists("workspace_data_connections"):
            raise KeyError(f"Data connection '{connection_id}' not found")
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE workspace_data_connections
                   SET enabled = NOT enabled, updated_at = now()
                 WHERE connection_id = %s
                   AND deleted_at IS NULL
                RETURNING enabled
                """,
                (str(connection_id),),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Data connection '{connection_id}' not found")
            return bool(row["enabled"])

    def delete_workspace_data_connection(self, connection_id: str, author: str = "admin") -> None:
        snapshot = self.get_workspace_data_connection(connection_id)
        if not snapshot:
            raise KeyError(f"Data connection '{connection_id}' not found")
        if int(snapshot.get("linked_dataset_count") or 0) > 0:
            raise ValueError("Data connection has linked datasets and cannot be deleted")
        with self._transaction() as cur:
            cur.execute(
                """
                UPDATE workspace_data_connections
                   SET deleted_at = now(), updated_at = now()
                 WHERE connection_id = %s
                   AND deleted_at IS NULL
                """,
                (str(connection_id),),
            )
            if not cur.rowcount:
                raise KeyError(f"Data connection '{connection_id}' not found")
            self._log_change_cur(
                cur,
                "workspace_data_connection",
                str(connection_id),
                "delete",
                snapshot,
                author,
            )

    @staticmethod
    def _normalize_workspace_data_connection_row(row: dict) -> dict:
        for field_name in (
            "connector_ref_jsonb",
            "connection_config_jsonb",
            "secret_refs_jsonb",
            "metadata_jsonb",
        ):
            value = row.get(field_name)
            if isinstance(value, str):
                try:
                    row[field_name] = json.loads(value)
                except Exception:
                    row[field_name] = {}
            elif not isinstance(value, dict):
                row[field_name] = {}
        row["connection_id"] = str(row.get("connection_id") or "").strip()
        row["workspace_id"] = str(row.get("workspace_id") or "").strip()
        row["tenant_id"] = str(row.get("tenant_id") or "").strip()
        row["account_id"] = str(row.get("account_id") or "").strip()
        row["display_name"] = str(row.get("display_name") or row["connection_id"]).strip()
        row["description"] = str(row.get("description") or "").strip()
        row["source_kind"] = str(row.get("source_kind") or "").strip()
        row["environment"] = str(row.get("environment") or "dev").strip() or "dev"
        row["access_mode"] = str(row.get("access_mode") or "read_only").strip() or "read_only"
        row["health_status"] = str(row.get("health_status") or "unknown").strip() or "unknown"
        row["last_error"] = str(row.get("last_error") or "").strip()
        row["workspace_display_name"] = str(row.get("workspace_display_name") or "").strip()
        row["linked_dataset_count"] = int(row.get("linked_dataset_count") or 0)
        row["enabled"] = bool(row.get("enabled", True))
        if not isinstance(row.get("tags"), list):
            row["tags"] = []
        return row


__all__ = [
    "RegistryDBStoreDatasetsMixin",
]
