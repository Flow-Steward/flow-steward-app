# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.extensions.projected_dataset_bridge import assert_projection_bridge_mutable

from .db_store_common import *  # NOSONAR


class RegistryDBStoreDatasetCatalogMixin:
    @staticmethod
    def _is_project_scoped_projection_dataset(row: dict) -> bool:
        metadata = row.get("metadata_jsonb") or row.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        bridge = metadata.get("projection_bridge")
        return (
            isinstance(bridge, dict)
            and bool(bridge.get("immutable"))
            and str(metadata.get("connection_resolution") or "").strip() == "project_scope"
        )

    def set_project_datasets(
        self, project_id: str, dataset_ids: list[str], author: str = "admin"
    ) -> list[dict]:
        if not self._table_exists("registry_datasets") or not self._table_exists(
            "project_datasets"
        ):
            raise RuntimeError("Dataset registry table is not available")

        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        normalized_dataset_ids: list[str] = []
        for item in dataset_ids:
            dataset_id = str(item or "").strip()
            if dataset_id and dataset_id not in normalized_dataset_ids:
                normalized_dataset_ids.append(dataset_id)

        project_scope = self._get_project_scope(normalized_project_id)
        if not project_scope:
            raise KeyError(f"Unknown project '{normalized_project_id}'")
        project_workspace_id = str(project_scope.get("workspace_id") or "").strip()
        available = self.list_datasets(enabled_only=False)
        by_id = {
            str(row.get("dataset_id") or "").strip(): row
            for row in available
            if str(row.get("dataset_id") or "").strip()
        }
        missing = [dataset_id for dataset_id in normalized_dataset_ids if dataset_id not in by_id]
        if missing:
            raise KeyError(f"Unknown dataset(s): {', '.join(missing)}")
        incompatible = [
            dataset_id
            for dataset_id in normalized_dataset_ids
            if str(by_id[dataset_id].get("workspace_id") or "").strip() != project_workspace_id
            and not self._is_project_scoped_projection_dataset(by_id[dataset_id])
        ]
        if incompatible:
            raise ValueError(
                f"Dataset(s) belong to a different workspace: {', '.join(incompatible)}"
            )

        wanted = set(normalized_dataset_ids)
        changed_dataset_ids: list[str] = []
        with self._transaction() as cur:
            cur.execute(
                "SELECT dataset_id FROM project_datasets WHERE project_id = %s",
                (normalized_project_id,),
            )
            previous = {str(row.get("dataset_id") or "").strip() for row in cur.fetchall()}

            cur.execute(
                "DELETE FROM project_datasets WHERE project_id = %s", (normalized_project_id,)
            )
            for dataset_id in normalized_dataset_ids:
                if (
                    str(by_id[dataset_id].get("availability_mode") or "")
                    == "all_workspace_projects"
                ):
                    continue
                cur.execute(
                    """
                    INSERT INTO project_datasets (dataset_id, project_id)
                    VALUES (%s, %s)
                    ON CONFLICT (dataset_id, project_id) DO NOTHING
                    """,
                    (dataset_id, normalized_project_id),
                )

            changed_dataset_ids = sorted(previous.symmetric_difference(wanted))
            for dataset_id in changed_dataset_ids:
                snapshot = dict(by_id.get(dataset_id) or {"dataset_id": dataset_id})
                project_ids = set(self._normalize_dataset_project_ids(snapshot.get("project_ids")))
                project_ids.discard(normalized_project_id)
                if dataset_id in wanted:
                    project_ids.add(normalized_project_id)
                snapshot["project_ids"] = sorted(project_ids)
                self._log_change_cur(
                    cur, "dataset", dataset_id, "project_assignment", snapshot, author
                )

        return self.list_datasets(enabled_only=False, project_id=normalized_project_id)

    def list_project_dataset_catalog(
        self,
        project_id: str,
        *,
        include_trashed: bool = False,
        account_id: str | None = None,
    ) -> dict:
        normalized_project_id = str(project_id or "").strip()
        project_scope = self._get_project_scope(
            normalized_project_id,
            include_trashed=include_trashed,
            account_id=account_id,
        )
        if not project_scope:
            raise KeyError(f"Unknown project '{normalized_project_id}'")
        project_workspace_id = str(project_scope.get("workspace_id") or "").strip()
        available_rows = [
            row
            for row in self.list_datasets(enabled_only=False)
            if (
                str(row.get("workspace_id") or "").strip() == project_workspace_id
                and not bool(row.get("workspace_unresolved"))
            )
            or (
                self._is_project_scoped_projection_dataset(row)
                and str(row.get("account_id") or "").strip()
                == str(project_scope.get("account_id") or "").strip()
            )
        ]
        explicit_dataset_ids = sorted(
            {
                str(row.get("dataset_id") or "").strip()
                for row in available_rows
                if normalized_project_id
                in self._normalize_dataset_project_ids(row.get("project_ids"))
            }
        )
        effective_dataset_ids = sorted(
            {
                str(row.get("dataset_id") or "").strip()
                for row in available_rows
                if str(row.get("availability_mode") or "") == "all_workspace_projects"
                or normalized_project_id
                in self._normalize_dataset_project_ids(row.get("project_ids"))
            }
        )
        return {
            "project_id": normalized_project_id,
            "workspace_id": project_workspace_id,
            "dataset_ids": effective_dataset_ids,
            "explicit_dataset_ids": explicit_dataset_ids,
            "available_datasets": available_rows,
        }

    def list_dataset_project_catalog(self, dataset_id: str) -> dict:
        row = self.get_dataset(dataset_id)
        if not row:
            raise KeyError(f"Unknown dataset '{dataset_id}'")
        workspace_id = str(row.get("workspace_id") or "").strip()
        projects: list[dict] = []
        if workspace_id and self._table_exists("projects"):
            with self._cursor() as cur:
                cur.execute(
                    """
                    SELECT p.*, COUNT(j.id) AS job_count
                      FROM projects p
                      LEFT JOIN jobs j ON j.project_id = p.id
                     WHERE p.workspace_id::text = %s
                       AND p.deleted_at IS NULL
                     GROUP BY p.id
                     ORDER BY p.name
                    """,
                    (workspace_id,),
                )
                projects = [dict(project) for project in cur.fetchall()]
        explicit_project_ids = self._normalize_dataset_project_ids(row.get("project_ids"))
        effective_project_ids = (
            [str(project.get("id") or "").strip() for project in projects]
            if str(row.get("availability_mode") or "") == "all_workspace_projects"
            else explicit_project_ids
        )
        return {
            "dataset_id": str(row.get("dataset_id") or "").strip(),
            "workspace_id": workspace_id,
            "availability_mode": str(row.get("availability_mode") or "specific_projects_only"),
            "project_ids": effective_project_ids,
            "explicit_project_ids": explicit_project_ids,
            "available_projects": projects,
        }

    def save_dataset(self, data: dict, author: str = "admin") -> None:
        dataset_id = str(data.get("dataset_id") or "").strip()
        if not dataset_id:
            raise ValueError("dataset_id is required")
        if not self._table_exists("registry_datasets"):
            raise RuntimeError("Dataset registry table is not available")
        is_new = not self.get_dataset(dataset_id, include_trashed=True)
        existing = self.get_dataset(dataset_id, include_trashed=True) if not is_new else {}
        existing_map: dict[str, Any] = existing if isinstance(existing, dict) else {}
        assert_projection_bridge_mutable(existing_map, author=author)
        snapshot = {
            "dataset_id": dataset_id,
            "dataset_key": str(
                data.get("dataset_key") or existing_map.get("dataset_key") or dataset_id
            ).strip()
            or dataset_id,
            "workspace_id": str(
                data.get("workspace_id") or existing_map.get("workspace_id") or ""
            ).strip()
            or None,
            "display_name": data.get("display_name") or dataset_id,
            "description": data.get("description") or "",
            "connector_kind": data.get("connector_kind")
            or existing_map.get("connector_kind")
            or "extension",
            "connector_id": data.get("connector_id") or existing_map.get("connector_id") or "",
            "runtime_kind": data.get("runtime_kind")
            or existing_map.get("runtime_kind")
            or "local_extension",
            "query_language": data.get("query_language")
            or existing_map.get("query_language")
            or "sql",
            "availability_mode": str(
                data.get("availability_mode")
                or existing_map.get("availability_mode")
                or "specific_projects_only"
            ).strip()
            or "specific_projects_only",
            "read_only": bool(data.get("read_only", existing_map.get("read_only", True))),
            "enabled": bool(data.get("enabled", existing_map.get("enabled", True))),
            "schema_snapshot_jsonb": data.get("schema_snapshot_jsonb")
            or data.get("schema_snapshot")
            or existing_map.get("schema_snapshot_jsonb")
            or {},
            "connection_schema_jsonb": data.get("connection_schema_jsonb")
            or data.get("connection_schema")
            or existing_map.get("connection_schema_jsonb")
            or {},
            "metadata_jsonb": data.get("metadata_jsonb")
            or data.get("metadata")
            or existing_map.get("metadata_jsonb")
            or {},
            "tags": list(data.get("tags", existing_map.get("tags")) or []),
        }
        project_ids_provided = "project_ids" in data
        project_ids = self._normalize_dataset_project_ids(data.get("project_ids"))
        supports_workspace_id = self._column_exists("registry_datasets", "workspace_id")
        supports_availability_mode = self._column_exists("registry_datasets", "availability_mode")
        has_generated_dataset_id = self._column_is_generated("registry_datasets", "dataset_id")

        with self._transaction() as cur:
            insert_columns = [
                "dataset_key",
                "display_name",
                "description",
                "connector_kind",
                "connector_id",
                "runtime_kind",
                "query_language",
                "read_only",
                "enabled",
                "schema_snapshot_jsonb",
                "connection_schema_jsonb",
                "metadata_jsonb",
                "tags",
                "deleted_at",
            ]
            if not has_generated_dataset_id:
                insert_columns.insert(0, "dataset_id")
            if supports_workspace_id:
                insert_columns.insert(2 if not has_generated_dataset_id else 1, "workspace_id")
            if supports_availability_mode:
                if has_generated_dataset_id:
                    insert_columns.insert(8 if supports_workspace_id else 7, "availability_mode")
                else:
                    insert_columns.insert(9 if supports_workspace_id else 8, "availability_mode")
            excluded_updates = [
                "dataset_key = EXCLUDED.dataset_key",
                DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
                DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
                "connector_kind = EXCLUDED.connector_kind",
                "connector_id = EXCLUDED.connector_id",
                "runtime_kind = EXCLUDED.runtime_kind",
                "query_language = EXCLUDED.query_language",
                "read_only = EXCLUDED.read_only",
                ENABLED_EXCLUDED_ENABLED_SQL,
                "schema_snapshot_jsonb = EXCLUDED.schema_snapshot_jsonb",
                "connection_schema_jsonb = EXCLUDED.connection_schema_jsonb",
                "metadata_jsonb = EXCLUDED.metadata_jsonb",
                TAGS_EXCLUDED_TAGS_SQL,
                DELETED_AT_NULL_SQL,
                "updated_at = now()",
            ]
            if supports_workspace_id:
                excluded_updates.insert(1, "workspace_id = EXCLUDED.workspace_id")
            if supports_availability_mode:
                excluded_updates.insert(
                    8 if supports_workspace_id else 7,
                    "availability_mode = EXCLUDED.availability_mode",
                )
            insert_identifiers = psycopg_runtime.sql.SQL(", ").join(
                psycopg_runtime.sql.Identifier(column) for column in insert_columns
            )
            insert_placeholders = psycopg_runtime.sql.SQL(", ").join(
                [
                    *(psycopg_runtime.sql.Placeholder(column) for column in insert_columns[:-1]),
                    psycopg_runtime.sql.SQL("NULL"),
                ]
            )
            update_assignments = psycopg_runtime.sql.SQL(", ").join(
                psycopg_runtime.sql.SQL(fragment) for fragment in excluded_updates
            )
            cur.execute(
                psycopg_runtime.sql.SQL(
                    """
                INSERT INTO registry_datasets ({insert_columns})
                VALUES ({insert_placeholders})
                ON CONFLICT (dataset_id) DO UPDATE SET
                    {update_assignments}
                """
                ).format(
                    insert_columns=insert_identifiers,
                    insert_placeholders=insert_placeholders,
                    update_assignments=update_assignments,
                ),
                {
                    **snapshot,
                    "schema_snapshot_jsonb": json.dumps(snapshot["schema_snapshot_jsonb"]),
                    "connection_schema_jsonb": json.dumps(snapshot["connection_schema_jsonb"]),
                    "metadata_jsonb": json.dumps(snapshot["metadata_jsonb"]),
                },
            )
            if project_ids_provided and self._table_exists("project_datasets"):
                cur.execute("DELETE FROM project_datasets WHERE dataset_id = %s", (dataset_id,))
                for project_id in project_ids:
                    if snapshot["availability_mode"] == "all_workspace_projects":
                        break
                    cur.execute(
                        """
                        INSERT INTO project_datasets (dataset_id, project_id)
                        VALUES (%s, %s)
                        ON CONFLICT (dataset_id, project_id) DO NOTHING
                        """,
                        (dataset_id, project_id),
                    )
            self._log_change_cur(
                cur, "dataset", dataset_id, "create" if is_new else "update", snapshot, author
            )

    def toggle_dataset(self, dataset_id: str) -> bool:
        if not self._table_exists("registry_datasets"):
            raise KeyError(f"Dataset '{dataset_id}' not found")
        assert_projection_bridge_mutable(
            self.get_dataset(dataset_id, include_trashed=True), author="admin"
        )
        has_deleted_at = self._column_exists("registry_datasets", "deleted_at")
        with self._cursor() as cur:
            if has_deleted_at:
                cur.execute(
                    "UPDATE registry_datasets SET enabled = NOT enabled, updated_at = now() "
                    "WHERE dataset_id = %s AND deleted_at IS NULL RETURNING enabled",
                    (dataset_id,),
                )
            else:
                cur.execute(
                    "UPDATE registry_datasets SET enabled = NOT enabled, updated_at = now() "
                    "WHERE dataset_id = %s RETURNING enabled",
                    (dataset_id,),
                )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Dataset '{dataset_id}' not found")
            return bool(row["enabled"])

    def delete_dataset(self, dataset_id: str, author: str = "admin") -> None:
        snapshot = self.get_dataset(dataset_id)
        if not snapshot:
            raise KeyError(f"Dataset '{dataset_id}' not found")
        assert_projection_bridge_mutable(snapshot, author=author)
        has_deleted_at = self._column_exists("registry_datasets", "deleted_at")
        with self._transaction() as cur:
            if has_deleted_at:
                cur.execute(
                    "UPDATE registry_datasets SET deleted_at = NOW(), updated_at = now() "
                    "WHERE dataset_id = %s AND deleted_at IS NULL",
                    (dataset_id,),
                )
            else:
                cur.execute("DELETE FROM registry_datasets WHERE dataset_id = %s", (dataset_id,))
            self._log_change_cur(cur, "dataset", dataset_id, "delete", snapshot, author)

    def purge_dataset(self, dataset_id: str) -> None:
        """Physically remove one host-owned dataset bridge and its assignments."""
        with self._transaction() as cur:
            if self._table_exists("project_datasets"):
                cur.execute("DELETE FROM project_datasets WHERE dataset_id = %s", (dataset_id,))
            cur.execute("DELETE FROM registry_datasets WHERE dataset_id = %s", (dataset_id,))

    def list_dataset_query_templates(
        self, dataset_id: str | None = None, include_trashed: bool = False
    ) -> list[dict]:
        if not self._table_exists("dataset_query_templates"):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if dataset_id:
            clauses.append("dataset_id = %s")
            params.append(str(dataset_id))
        if not include_trashed and self._column_exists("dataset_query_templates", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM dataset_query_templates
                  {where_sql}
                 ORDER BY updated_at DESC, display_name, query_template_id
                """,
                tuple(params),
            )
            return [self._normalize_dataset_query_template_row(dict(row)) for row in cur.fetchall()]

    def get_dataset_query_template(
        self, query_template_id: str, include_trashed: bool = False
    ) -> dict | None:
        if not self._table_exists("dataset_query_templates"):
            return None
        clauses = ["query_template_id = %s"]
        params: list[Any] = [str(query_template_id)]
        if not include_trashed and self._column_exists("dataset_query_templates", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM dataset_query_templates
                 WHERE {SQL_AND.join(clauses)}
                 LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            return self._normalize_dataset_query_template_row(dict(row)) if row else None

    def save_dataset_query_template(self, data: dict, author: str = "admin") -> dict:
        if not self._table_exists("dataset_query_templates"):
            raise RuntimeError("Dataset query template table is not available")
        has_query_language_column = self._column_exists("dataset_query_templates", "query_language")
        has_tenant_id_column = self._column_exists("dataset_query_templates", "tenant_id")
        has_account_id_column = self._column_exists("dataset_query_templates", "account_id")
        has_workspace_id_column = self._column_exists("dataset_query_templates", "workspace_id")
        params_schema_column = (
            "params_schema_jsonb"
            if self._column_exists("dataset_query_templates", "params_schema_jsonb")
            else "param_schema_jsonb"
        )
        query_template_id = str(data.get("query_template_id") or "").strip()
        dataset_id = str(data.get("dataset_id") or "").strip()
        if not query_template_id:
            raise ValueError("query_template_id is required")
        if not dataset_id:
            raise ValueError("dataset_id is required")
        dataset_row = self.get_dataset(dataset_id, include_trashed=True) or {}
        from core.application.registry.saved_query_contract import (
            validate_saved_query_contract,
        )

        validate_saved_query_contract(data, dataset=dataset_row)
        existing = self.get_dataset_query_template(query_template_id, include_trashed=True) or {}
        assert_projection_bridge_mutable(existing, author=author)
        snapshot = {
            "query_template_id": query_template_id,
            "dataset_id": dataset_id,
            "tenant_id": str(
                data.get("tenant_id")
                or existing.get("tenant_id")
                or dataset_row.get("tenant_id")
                or "00000000-0000-0000-0000-000000000001"
            ).strip(),
            "account_id": str(
                data.get("account_id")
                or existing.get("account_id")
                or dataset_row.get("account_id")
                or "00000000-0000-0000-0000-000000000002"
            ).strip(),
            "workspace_id": str(
                data.get("workspace_id")
                or existing.get("workspace_id")
                or dataset_row.get("workspace_id")
                or "00000000-0000-0000-0000-000000000003"
            ).strip(),
            "display_name": str(data.get("display_name") or query_template_id).strip(),
            "description": str(data.get("description") or "").strip(),
            "query_language": str(data.get("query_language") or "sql").strip() or "sql",
            "query_definition_jsonb": data.get("query_definition_jsonb")
            or data.get("query_definition")
            or existing.get("query_definition_jsonb")
            or {},
            "params_schema_jsonb": data.get("params_schema_jsonb")
            or data.get("param_schema_jsonb")
            or data.get("params_schema")
            or data.get("param_schema")
            or existing.get("params_schema_jsonb")
            or existing.get("param_schema_jsonb")
            or {},
            "output_schema_jsonb": data.get("output_schema_jsonb")
            or data.get("output_schema")
            or existing.get("output_schema_jsonb")
            or {},
            "validation_result_jsonb": data.get("validation_result_jsonb")
            or data.get("validation_result")
            or existing.get("validation_result_jsonb")
            or {},
            "enabled": bool(data.get("enabled", existing.get("enabled", True))),
            "version": int(existing.get("version") or 1) + 1 if existing else 1,
            "created_by": str(data.get("created_by") or existing.get("created_by") or "").strip(),
            "approved_by": str(
                data.get("approved_by") or existing.get("approved_by") or ""
            ).strip(),
        }
        with self._transaction() as cur:
            insert_columns = [
                "query_template_id",
                "dataset_id",
                "display_name",
                "description",
                "query_definition_jsonb",
                params_schema_column,
                "output_schema_jsonb",
                "validation_result_jsonb",
                "enabled",
                "version",
                "created_by",
                "approved_by",
                "deleted_at",
            ]
            if has_tenant_id_column:
                insert_columns.insert(1, "tenant_id")
            if has_account_id_column:
                insert_columns.insert(2 if has_tenant_id_column else 1, "account_id")
            if has_workspace_id_column:
                workspace_insert_index = sum((has_tenant_id_column, has_account_id_column)) + 1
                insert_columns.insert(workspace_insert_index, "workspace_id")
            if has_query_language_column:
                scope_column_count = sum(
                    (has_tenant_id_column, has_account_id_column, has_workspace_id_column)
                )
                insert_columns.insert(scope_column_count + 4, "query_language")
            insert_identifiers = psycopg_runtime.sql.SQL(", ").join(
                psycopg_runtime.sql.Identifier(column) for column in insert_columns
            )
            insert_placeholders = psycopg_runtime.sql.SQL(", ").join(
                [
                    *(psycopg_runtime.sql.Placeholder(column) for column in insert_columns[:-1]),
                    psycopg_runtime.sql.SQL("NULL"),
                ]
            )
            update_assignments = [
                "dataset_id = EXCLUDED.dataset_id",
                DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
                DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
                "query_definition_jsonb = EXCLUDED.query_definition_jsonb",
                f"{params_schema_column} = EXCLUDED.{params_schema_column}",
                "output_schema_jsonb = EXCLUDED.output_schema_jsonb",
                "validation_result_jsonb = EXCLUDED.validation_result_jsonb",
                ENABLED_EXCLUDED_ENABLED_SQL,
                VERSION_EXCLUDED_VERSION_SQL,
                "approved_by = EXCLUDED.approved_by",
                DELETED_AT_NULL_SQL,
                "updated_at = now()",
            ]
            if has_tenant_id_column:
                update_assignments.insert(1, "tenant_id = EXCLUDED.tenant_id")
            if has_account_id_column:
                update_assignments.insert(
                    2 if has_tenant_id_column else 1,
                    "account_id = EXCLUDED.account_id",
                )
            if has_workspace_id_column:
                workspace_update_index = sum((has_tenant_id_column, has_account_id_column)) + 1
                update_assignments.insert(
                    workspace_update_index,
                    "workspace_id = EXCLUDED.workspace_id",
                )
            if has_query_language_column:
                scope_assignment_count = sum(
                    (has_tenant_id_column, has_account_id_column, has_workspace_id_column)
                )
                update_assignments.insert(
                    scope_assignment_count + 3,
                    "query_language = EXCLUDED.query_language",
                )
            update_sql = psycopg_runtime.sql.SQL(", ").join(
                psycopg_runtime.sql.SQL(fragment) for fragment in update_assignments
            )
            cur.execute(
                psycopg_runtime.sql.SQL(
                    """
                INSERT INTO dataset_query_templates ({insert_columns})
                VALUES ({insert_placeholders})
                ON CONFLICT (query_template_id) DO UPDATE SET
                    {update_sql}
                RETURNING *
                """
                ).format(
                    insert_columns=insert_identifiers,
                    insert_placeholders=insert_placeholders,
                    update_sql=update_sql,
                ),
                {
                    **snapshot,
                    "query_definition_jsonb": json.dumps(snapshot["query_definition_jsonb"]),
                    "params_schema_jsonb": json.dumps(snapshot["params_schema_jsonb"]),
                    "output_schema_jsonb": json.dumps(snapshot["output_schema_jsonb"]),
                    "validation_result_jsonb": json.dumps(snapshot["validation_result_jsonb"]),
                },
            )
            row = cur.fetchone()
            return self._normalize_dataset_query_template_row(dict(row)) if row else snapshot

    @staticmethod
    def _normalize_dataset_query_template_row(row: dict) -> dict:
        if "params_schema_jsonb" not in row and "param_schema_jsonb" in row:
            row["params_schema_jsonb"] = row.get("param_schema_jsonb")
        if "param_schema_jsonb" not in row and "params_schema_jsonb" in row:
            row["param_schema_jsonb"] = row.get("params_schema_jsonb")
        return row

    def toggle_dataset_query_template(self, query_template_id: str) -> bool:
        if not self._table_exists("dataset_query_templates"):
            raise KeyError(f"Saved query '{query_template_id}' not found")
        assert_projection_bridge_mutable(
            self.get_dataset_query_template(query_template_id, include_trashed=True), author="admin"
        )
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE dataset_query_templates
                   SET enabled = NOT enabled, updated_at = now()
                 WHERE query_template_id = %s
                   AND deleted_at IS NULL
                RETURNING enabled
                """,
                (str(query_template_id),),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Saved query '{query_template_id}' not found")
            return bool(row["enabled"])

    def delete_dataset_query_template(self, query_template_id: str) -> None:
        if not self._table_exists("dataset_query_templates"):
            raise KeyError(f"Saved query '{query_template_id}' not found")
        assert_projection_bridge_mutable(
            self.get_dataset_query_template(query_template_id, include_trashed=True), author="admin"
        )
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE dataset_query_templates
                   SET deleted_at = now(), updated_at = now()
                 WHERE query_template_id = %s
                   AND deleted_at IS NULL
                """,
                (str(query_template_id),),
            )
            if not cur.rowcount:
                raise KeyError(f"Saved query '{query_template_id}' not found")

    def purge_dataset_query_template(self, query_template_id: str) -> None:
        """Physically remove one host-owned saved-query bridge."""
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM dataset_query_templates WHERE query_template_id = %s",
                (str(query_template_id),),
            )


__all__ = [
    "RegistryDBStoreDatasetCatalogMixin",
]
