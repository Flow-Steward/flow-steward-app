"""Project-owned settings revision persistence operations."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import project_settings_store
from core.infrastructure.db.platform_logger_common import (
    SQL_AND,
    SQL_DELETED_AT_IS_NULL,
    SQL_ID_EQ,
    SQL_PROJECT_ID_EQ,
    _as_dict,
    _as_list,
    _json_clone,
    _normalize_string_list,
)


class PlatformProjectSettingsHistoryMixin:
    """Expose the project history contract on responsibility-owned stores."""

    conn: Any
    _table_exists: Any
    _column_exists: Any
    get_project: Any
    get_project_workflows: Any
    get_project_policy: Any
    get_project_provider_bindings: Any
    update_project: Any
    save_project_provider_binding: Any

    def get_project_settings_snapshot(self, project_id: str) -> dict:
        return project_settings_store.get_project_settings_snapshot(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            get_project=self.get_project,
            get_project_workflows=self.get_project_workflows,
            get_project_policy=self.get_project_policy,
            get_project_provider_bindings=self.get_project_provider_bindings,
            list_project_data_connections=getattr(self, "list_project_data_connections", None),
            as_dict=_as_dict,
            json_clone=_json_clone,
        )

    def create_project_settings_revision(
        self,
        project_id: str,
        *,
        author: str = "system",
        action: str = "save",
        source: str = "ui",
        snapshot: dict | None = None,
    ) -> str | None:
        return project_settings_store.create_project_settings_revision(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            as_dict=_as_dict,
            get_project_settings_snapshot=self.get_project_settings_snapshot,
            author=author,
            action=action,
            source=source,
            snapshot=snapshot,
        )

    def list_project_settings_revisions(
        self,
        project_id: str,
        limit: int = 50,
        *,
        include_trashed: bool = False,
    ) -> list[dict]:
        return project_settings_store.list_project_settings_revisions(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            sql_project_id_eq=SQL_PROJECT_ID_EQ,
            sql_deleted_at_is_null=SQL_DELETED_AT_IS_NULL,
            sql_and=SQL_AND,
            limit=limit,
            include_trashed=include_trashed,
        )

    def get_project_settings_revision(
        self,
        project_id: str,
        revision_id: str,
        *,
        include_trashed: bool = False,
    ) -> dict | None:
        return project_settings_store.get_project_settings_revision(
            project_id,
            revision_id,
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            as_dict=_as_dict,
            sql_project_id_eq=SQL_PROJECT_ID_EQ,
            sql_id_eq=SQL_ID_EQ,
            sql_deleted_at_is_null=SQL_DELETED_AT_IS_NULL,
            sql_and=SQL_AND,
            include_trashed=include_trashed,
        )

    def restore_project_settings_revision(
        self,
        project_id: str,
        revision_id: str,
        *,
        author: str = "system",
    ) -> dict[str, Any]:
        return project_settings_store.restore_project_settings_revision(
            project_id,
            revision_id,
            as_dict=_as_dict,
            as_list=_as_list,
            normalize_string_list=_normalize_string_list,
            get_project_settings_revision=self.get_project_settings_revision,
            get_project=self.get_project,
            update_project=self.update_project,
            set_project_data_connections=getattr(self, "set_project_data_connections", None),
            get_project_provider_bindings=self.get_project_provider_bindings,
            save_project_provider_binding=self.save_project_provider_binding,
            create_project_settings_revision=self.create_project_settings_revision,
            author=author,
        )
