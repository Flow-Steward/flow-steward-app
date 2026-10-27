"""Registry persistence for skills and their public contracts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from core.application.registry.db_store_common import (
    BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL,
    BUILDER_METADATA_JSONB_S_VALUE,
    BUILDER_METADATA_JSONB_VALUE,
    DELETED_AT_IS_NULL_VALUE,
    SQL_AND,
    SQL_DELETED_AT_IS_NULL,
    SQL_ENABLED_TRUE,
    _nullable_scope_sql,
    _system_registry_account_id,
    json,
    normalize_builder_metadata,
    psycopg_runtime,
)


class RegistryDBStoreSkillsMixin:
    # Connection and revision operations are supplied by RegistryDBStore's
    # existing sibling components; these declarations add no runtime fallback.
    _column_exists: Callable[[str, str], bool]
    _cursor: Callable[[], AbstractContextManager[Any]]
    _transaction: Callable[[], AbstractContextManager[Any]]
    _log_change_cur: Callable[[Any, str, str, str, dict, str], None]
    _trigger_public_contract_projection_sync: Callable[[], None]
    _exists: Callable[[str, str, str], bool]
    _build_contract_schema: Callable[[list[dict]], dict]

    def get_skill(self, skill_id: str) -> dict | None:
        with self._cursor() as cur:
            conditions = ["skill_id = %s"]
            params: list[Any] = [skill_id]
            if self._column_exists("registry_skills", "deleted_at"):
                conditions.append(SQL_DELETED_AT_IS_NULL)
            where_sql = psycopg_runtime.sql.SQL(SQL_AND).join(
                psycopg_runtime.sql.SQL(condition) for condition in conditions
            )
            cur.execute(
                psycopg_runtime.sql.SQL("SELECT * FROM registry_skills WHERE {}").format(where_sql),
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                return None
            cap = dict(row)
            # Attach inputs and outputs.
            cur.execute(
                """SELECT i.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                          dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                   FROM registry_skill_inputs i
                   LEFT JOIN registry_data_contracts dc ON dc.contract_id = i.contract_id
                   WHERE i.skill_id = %s
                   ORDER BY i.sort_order""",
                (skill_id,),
            )
            cap["_inputs"] = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """SELECT o.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                          dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                   FROM registry_skill_outputs o
                   LEFT JOIN registry_data_contracts dc ON dc.contract_id = o.contract_id
                   WHERE o.skill_id = %s
                   ORDER BY o.sort_order""",
                (skill_id,),
            )
            cap["_outputs"] = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT agent_id FROM registry_agent_skills WHERE skill_id = %s ORDER BY agent_id",
                (skill_id,),
            )
            using_agents = [str(r.get("agent_id") or "").strip() for r in cur.fetchall()]
            using_agents = [agent_id for agent_id in using_agents if agent_id]
            cap["using_agents"] = using_agents
            cap["_agents"] = using_agents
            cap["agents"] = using_agents
            return cap

    def list_skills(
        self,
        enabled_only: bool = False,
        include_trashed: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        with self._cursor() as cur:
            conditions: list[str] = []
            if not include_trashed and self._column_exists("registry_skills", "deleted_at"):
                conditions.append(SQL_DELETED_AT_IS_NULL)
            if enabled_only:
                conditions.append(SQL_ENABLED_TRUE)
            normalized_account_id = str(account_id or "").strip()
            normalized_project_id = str(project_id or "").strip()
            if self._column_exists("registry_skills", "account_id") and normalized_account_id:
                conditions.append(
                    f"({_nullable_scope_sql('account_id')} OR account_id = %(account_id)s)"
                )
            if self._column_exists("registry_skills", "project_id"):
                if normalized_project_id:
                    conditions.append(
                        f"({_nullable_scope_sql('project_id')} OR project_id = %(project_id)s)"
                    )
                else:
                    conditions.append(_nullable_scope_sql("project_id"))
            where_sql = (
                psycopg_runtime.sql.SQL("WHERE {}").format(
                    psycopg_runtime.sql.SQL(SQL_AND).join(
                        psycopg_runtime.sql.SQL(condition) for condition in conditions
                    )
                )
                if conditions
                else psycopg_runtime.sql.SQL("")
            )
            cur.execute(
                psycopg_runtime.sql.SQL(
                    "SELECT * FROM registry_skills {} ORDER BY skill_id"
                ).format(where_sql),
                {
                    "account_id": normalized_account_id or None,
                    "project_id": normalized_project_id or None,
                },
            )
            caps = [dict(r) for r in cur.fetchall()]
            cap_ids = [c["skill_id"] for c in caps]
            if cap_ids:
                cur.execute(
                    """SELECT i.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                              dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                       FROM registry_skill_inputs i
                       LEFT JOIN registry_data_contracts dc ON dc.contract_id = i.contract_id
                       WHERE i.skill_id = ANY(%s)
                       ORDER BY i.skill_id, i.sort_order""",
                    (cap_ids,),
                )
                inputs_by_cap: dict[str, list] = {}
                for r in cur.fetchall():
                    inputs_by_cap.setdefault(r["skill_id"], []).append(dict(r))
                cur.execute(
                    """SELECT o.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                              dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                       FROM registry_skill_outputs o
                       LEFT JOIN registry_data_contracts dc ON dc.contract_id = o.contract_id
                       WHERE o.skill_id = ANY(%s)
                       ORDER BY o.skill_id, o.sort_order""",
                    (cap_ids,),
                )
                outputs_by_cap: dict[str, list] = {}
                for r in cur.fetchall():
                    outputs_by_cap.setdefault(r["skill_id"], []).append(dict(r))
                for c in caps:
                    c["_inputs"] = inputs_by_cap.get(c["skill_id"], [])
                    c["_outputs"] = outputs_by_cap.get(c["skill_id"], [])
            return caps

    def list_skills_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        with self._cursor() as cur:
            has_deleted_at = self._column_exists("registry_skills", "deleted_at")
            has_builder_metadata = self._column_exists("registry_skills", "builder_metadata_jsonb")
            has_account_id = self._column_exists("registry_skills", "account_id")
            has_project_id = self._column_exists("registry_skills", "project_id")
            has_scope_kind = self._column_exists("registry_skills", "scope_kind")
            has_source_install_id = self._column_exists("registry_skills", "source_install_id")
            has_availability_status = self._column_exists("registry_skills", "availability_status")
            has_new_runs_blocked = self._column_exists("registry_skills", "new_runs_blocked")
            has_visible_in_catalog = self._column_exists("registry_skills", "visible_in_catalog")
            normalized_account_id = str(account_id or "").strip()
            normalized_project_id = str(project_id or "").strip()
            scoped_conditions: list[str] = []
            if has_account_id and normalized_account_id:
                scoped_conditions.append(
                    f"({_nullable_scope_sql('account_id')} OR account_id = %(account_id)s)"
                )
            if has_project_id:
                if normalized_project_id:
                    scoped_conditions.append(
                        f"({_nullable_scope_sql('project_id')} OR project_id = %(project_id)s)"
                    )
                else:
                    scoped_conditions.append(_nullable_scope_sql("project_id"))
            active_clause = " AND deleted_at IS NULL" if has_deleted_at else ""
            if enabled_only:
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """SELECT
                           skill_id,
                           display_name,
                           LEFT(COALESCE(description, ''), 160) AS description_preview,
                           enabled,
                           category,
                           COALESCE(array_length(instructions, 1), 0) AS instruction_count
                           {account_id_column}
                           {project_id_column}
                           {scope_kind_column}
                           {source_install_id_column}
                           {availability_status_column}
                           {new_runs_blocked_column}
                           {visible_in_catalog_column}
                           {builder_metadata_column}
                       FROM registry_skills
                       WHERE enabled = true{active_clause}{scope_clause}
                       ORDER BY skill_id"""
                    ).format(
                        account_id_column=psycopg_runtime.sql.SQL(
                            ", account_id" if has_account_id else ""
                        ),
                        project_id_column=psycopg_runtime.sql.SQL(
                            ", project_id" if has_project_id else ""
                        ),
                        scope_kind_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(scope_kind, 'system') AS scope_kind"
                            if has_scope_kind
                            else ""
                        ),
                        source_install_id_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(source_install_id, '') AS source_install_id"
                            if has_source_install_id
                            else ""
                        ),
                        availability_status_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(availability_status, 'available') AS availability_status"
                            if has_availability_status
                            else ""
                        ),
                        new_runs_blocked_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(new_runs_blocked, false) AS new_runs_blocked"
                            if has_new_runs_blocked
                            else ""
                        ),
                        visible_in_catalog_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(visible_in_catalog, true) AS visible_in_catalog"
                            if has_visible_in_catalog
                            else ""
                        ),
                        builder_metadata_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb"
                            if has_builder_metadata
                            else ""
                        ),
                        active_clause=psycopg_runtime.sql.SQL(active_clause),
                        scope_clause=(
                            psycopg_runtime.sql.SQL(" AND {}").format(
                                psycopg_runtime.sql.SQL(SQL_AND).join(
                                    psycopg_runtime.sql.SQL(condition)
                                    for condition in scoped_conditions
                                )
                            )
                            if scoped_conditions
                            else psycopg_runtime.sql.SQL("")
                        ),
                    ),
                    {
                        "account_id": normalized_account_id or None,
                        "project_id": normalized_project_id or None,
                    },
                )
            else:
                conditions: list[str] = []
                if has_deleted_at:
                    conditions.append(DELETED_AT_IS_NULL_VALUE)
                conditions.extend(scoped_conditions)
                where_sql = (
                    psycopg_runtime.sql.SQL("WHERE {}").format(
                        psycopg_runtime.sql.SQL(SQL_AND).join(
                            psycopg_runtime.sql.SQL(condition) for condition in conditions
                        )
                    )
                    if conditions
                    else psycopg_runtime.sql.SQL("")
                )
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """SELECT
                           skill_id,
                           display_name,
                           LEFT(COALESCE(description, ''), 160) AS description_preview,
                           enabled,
                           category,
                           COALESCE(array_length(instructions, 1), 0) AS instruction_count
                           {account_id_column}
                           {project_id_column}
                           {scope_kind_column}
                           {source_install_id_column}
                           {availability_status_column}
                           {new_runs_blocked_column}
                           {visible_in_catalog_column}
                           {builder_metadata_column}
                       FROM registry_skills
                       {where_sql}
                       ORDER BY skill_id"""
                    ).format(
                        account_id_column=psycopg_runtime.sql.SQL(
                            ", account_id" if has_account_id else ""
                        ),
                        project_id_column=psycopg_runtime.sql.SQL(
                            ", project_id" if has_project_id else ""
                        ),
                        scope_kind_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(scope_kind, 'system') AS scope_kind"
                            if has_scope_kind
                            else ""
                        ),
                        source_install_id_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(source_install_id, '') AS source_install_id"
                            if has_source_install_id
                            else ""
                        ),
                        availability_status_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(availability_status, 'available') AS availability_status"
                            if has_availability_status
                            else ""
                        ),
                        new_runs_blocked_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(new_runs_blocked, false) AS new_runs_blocked"
                            if has_new_runs_blocked
                            else ""
                        ),
                        visible_in_catalog_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(visible_in_catalog, true) AS visible_in_catalog"
                            if has_visible_in_catalog
                            else ""
                        ),
                        builder_metadata_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb"
                            if has_builder_metadata
                            else ""
                        ),
                        where_sql=where_sql,
                    ),
                    {
                        "account_id": normalized_account_id or None,
                        "project_id": normalized_project_id or None,
                    },
                )
            return [dict(r) for r in cur.fetchall()]

    def save_skill(self, data: dict, author: str = "admin") -> None:
        skill_id = data["skill_id"]
        is_new = not self._exists("registry_skills", "skill_id", skill_id)
        expected_inputs = data.get("expected_inputs")
        if expected_inputs is None:
            expected_inputs = data.get("_inputs", [])
        expected_outputs = data.get("expected_outputs")
        if expected_outputs is None:
            expected_outputs = data.get("_outputs", [])
        input_schema = self._build_contract_schema(expected_inputs)
        output_schema = self._build_contract_schema(expected_outputs)
        has_skill_type = self._column_exists("registry_skills", "skill_type")
        has_supported_roles = self._column_exists("registry_skills", "supported_role_types")
        has_builder_metadata = self._column_exists("registry_skills", "builder_metadata_jsonb")
        has_deleted_at = self._column_exists("registry_skills", "deleted_at")
        has_deleted_by = self._column_exists("registry_skills", "deleted_by")
        builder_metadata = normalize_builder_metadata(data.get("builder_metadata_jsonb"))
        clear_deleted_assignments = psycopg_runtime.sql.SQL("")
        if has_deleted_at:
            clear_deleted_assignments += psycopg_runtime.sql.SQL(", deleted_at = NULL")
        if has_deleted_by:
            clear_deleted_assignments += psycopg_runtime.sql.SQL(", deleted_by = NULL")

        with self._transaction() as cur:
            payload = {
                "skill_id": skill_id,
                "version": data.get("version", "1.0.0"),
                "display_name": data.get("display_name", skill_id),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "category": data.get("category", ""),
                "instructions": data.get("instructions", []),
                "prompt_template": data.get("prompt_template", ""),
                "examples": json.dumps(data.get("examples", [])),
                "tags": data.get("tags", []),
                "input_schema": json.dumps(input_schema),
                "output_schema": json.dumps(output_schema),
                "builder_metadata_jsonb": json.dumps(builder_metadata),
                "skill_type": str(data.get("skill_type") or "secondary").strip() or "secondary",
                "supported_role_types": list(data.get("supported_role_types") or []),
            }
            optional_columns = [
                ("account_id", _system_registry_account_id(data)),
                ("project_id", str(data.get("project_id") or "").strip() or None),
                ("scope_kind", str(data.get("scope_kind") or "system").strip() or "system"),
                ("source_install_id", str(data.get("source_install_id") or "").strip() or None),
                (
                    "availability_status",
                    str(data.get("availability_status") or "available").strip() or "available",
                ),
                (
                    "availability_reason_code",
                    str(data.get("availability_reason_code") or "none").strip() or "none",
                ),
                (
                    "availability_reason_text",
                    str(data.get("availability_reason_text") or "").strip(),
                ),
                ("new_runs_blocked", bool(data.get("new_runs_blocked", False))),
                ("visible_in_catalog", bool(data.get("visible_in_catalog", True))),
            ]
            optional_column_names = [
                column_name
                for column_name, _ in optional_columns
                if self._column_exists("registry_skills", column_name)
            ]
            for column_name, column_value in optional_columns:
                if column_name in optional_column_names:
                    payload[column_name] = column_value
            if has_skill_type and has_supported_roles:
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """INSERT INTO registry_skills
                       (skill_id, version, display_name, description, enabled,
                        category, instructions, prompt_template, examples, tags, input_schema, output_schema,
                        skill_type, supported_role_types{builder_column}{optional_columns})
                       VALUES (%(skill_id)s, %(version)s, %(display_name)s, %(description)s, %(enabled)s,
                               %(category)s, %(instructions)s, %(prompt_template)s, %(examples)s, %(tags)s, %(input_schema)s, %(output_schema)s,
                               %(skill_type)s, %(supported_role_types)s{builder_value}{optional_values})
                       ON CONFLICT (skill_id) DO UPDATE SET
                           version = EXCLUDED.version,
                           display_name = EXCLUDED.display_name,
                           description = EXCLUDED.description,
                           enabled = EXCLUDED.enabled,
                           category = EXCLUDED.category,
                           instructions = EXCLUDED.instructions,
                           prompt_template = EXCLUDED.prompt_template,
                           examples = EXCLUDED.examples,
                           tags = EXCLUDED.tags,
                           input_schema = EXCLUDED.input_schema,
                           output_schema = EXCLUDED.output_schema,
                           skill_type = EXCLUDED.skill_type,
                           supported_role_types = EXCLUDED.supported_role_types{builder_update}{optional_updates}{clear_deleted_assignments}"""
                    ).format(
                        clear_deleted_assignments=clear_deleted_assignments,
                        builder_column=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_value=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_S_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_update=psycopg_runtime.sql.SQL(
                            BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL
                        )
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        optional_columns=psycopg_runtime.sql.SQL(
                            "".join(f", {column}" for column in optional_column_names)
                        ),
                        optional_values=psycopg_runtime.sql.SQL(
                            "".join(f", %({column})s" for column in optional_column_names)
                        ),
                        optional_updates=psycopg_runtime.sql.SQL(
                            "".join(
                                f", {column} = EXCLUDED.{column}"
                                for column in optional_column_names
                            )
                        ),
                    ),
                    payload,
                )
            elif has_skill_type:
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """INSERT INTO registry_skills
                       (skill_id, version, display_name, description, enabled,
                        category, instructions, prompt_template, examples, tags, input_schema, output_schema,
                        skill_type{builder_column}{optional_columns})
                       VALUES (%(skill_id)s, %(version)s, %(display_name)s, %(description)s, %(enabled)s,
                               %(category)s, %(instructions)s, %(prompt_template)s, %(examples)s, %(tags)s, %(input_schema)s, %(output_schema)s,
                               %(skill_type)s{builder_value}{optional_values})
                       ON CONFLICT (skill_id) DO UPDATE SET
                           version = EXCLUDED.version,
                           display_name = EXCLUDED.display_name,
                           description = EXCLUDED.description,
                           enabled = EXCLUDED.enabled,
                           category = EXCLUDED.category,
                           instructions = EXCLUDED.instructions,
                           prompt_template = EXCLUDED.prompt_template,
                           examples = EXCLUDED.examples,
                           tags = EXCLUDED.tags,
                           input_schema = EXCLUDED.input_schema,
                           output_schema = EXCLUDED.output_schema,
                           skill_type = EXCLUDED.skill_type{builder_update}{optional_updates}{clear_deleted_assignments}"""
                    ).format(
                        clear_deleted_assignments=clear_deleted_assignments,
                        builder_column=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_value=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_S_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_update=psycopg_runtime.sql.SQL(
                            BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL
                        )
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        optional_columns=psycopg_runtime.sql.SQL(
                            "".join(f", {column}" for column in optional_column_names)
                        ),
                        optional_values=psycopg_runtime.sql.SQL(
                            "".join(f", %({column})s" for column in optional_column_names)
                        ),
                        optional_updates=psycopg_runtime.sql.SQL(
                            "".join(
                                f", {column} = EXCLUDED.{column}"
                                for column in optional_column_names
                            )
                        ),
                    ),
                    payload,
                )
            else:
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """INSERT INTO registry_skills
                       (skill_id, version, display_name, description, enabled,
                        category, instructions, prompt_template, examples, tags, input_schema, output_schema{builder_column}{optional_columns})
                       VALUES (%(skill_id)s, %(version)s, %(display_name)s, %(description)s, %(enabled)s,
                               %(category)s, %(instructions)s, %(prompt_template)s, %(examples)s, %(tags)s, %(input_schema)s, %(output_schema)s{builder_value}{optional_values})
                       ON CONFLICT (skill_id) DO UPDATE SET
                           version = EXCLUDED.version,
                           display_name = EXCLUDED.display_name,
                           description = EXCLUDED.description,
                           enabled = EXCLUDED.enabled,
                           category = EXCLUDED.category,
                           instructions = EXCLUDED.instructions,
                           prompt_template = EXCLUDED.prompt_template,
                           examples = EXCLUDED.examples,
                           tags = EXCLUDED.tags,
                           input_schema = EXCLUDED.input_schema,
                           output_schema = EXCLUDED.output_schema{builder_update}{optional_updates}{clear_deleted_assignments}"""
                    ).format(
                        clear_deleted_assignments=clear_deleted_assignments,
                        builder_column=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_value=psycopg_runtime.sql.SQL(BUILDER_METADATA_JSONB_S_VALUE)
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        builder_update=psycopg_runtime.sql.SQL(
                            BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL
                        )
                        if has_builder_metadata
                        else psycopg_runtime.sql.SQL(""),
                        optional_columns=psycopg_runtime.sql.SQL(
                            "".join(f", {column}" for column in optional_column_names)
                        ),
                        optional_values=psycopg_runtime.sql.SQL(
                            "".join(f", %({column})s" for column in optional_column_names)
                        ),
                        optional_updates=psycopg_runtime.sql.SQL(
                            "".join(
                                f", {column} = EXCLUDED.{column}"
                                for column in optional_column_names
                            )
                        ),
                    ),
                    payload,
                )
            # Sync inputs
            cur.execute("DELETE FROM registry_skill_inputs WHERE skill_id = %s", (skill_id,))
            for i, inp in enumerate(expected_inputs):
                cur.execute(
                    """INSERT INTO registry_skill_inputs
                       (skill_id, name, type, description, required, sort_order, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        skill_id,
                        inp.get("name", ""),
                        inp.get("type", "text"),
                        inp.get("description", ""),
                        inp.get("required", False),
                        i,
                        inp.get("contract_id") or None,
                    ),
                )
            # Sync outputs
            cur.execute("DELETE FROM registry_skill_outputs WHERE skill_id = %s", (skill_id,))
            for i, out in enumerate(expected_outputs):
                cur.execute(
                    """INSERT INTO registry_skill_outputs
                       (skill_id, name, type, description, sort_order, required, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        skill_id,
                        out.get("name", ""),
                        out.get("type", "text"),
                        out.get("description", ""),
                        i,
                        out.get("required", True),
                        out.get("contract_id") or None,
                    ),
                )
            self._log_change_cur(
                cur,
                "skill",
                skill_id,
                "create" if is_new else "update",
                data,
                author,
            )
        self._trigger_public_contract_projection_sync()

    def delete_skill(self, skill_id: str, author: str = "admin") -> None:
        snapshot = self.get_skill(skill_id)
        if not snapshot:
            raise KeyError(f"Skill '{skill_id}' not found")
        has_deleted_at = self._column_exists("registry_skills", "deleted_at")
        has_deleted_by = self._column_exists("registry_skills", "deleted_by")
        with self._transaction() as cur:
            if has_deleted_at and has_deleted_by:
                cur.execute(
                    "UPDATE registry_skills SET deleted_at = NOW(), deleted_by = %s WHERE skill_id = %s AND deleted_at IS NULL",
                    (author, skill_id),
                )
            elif has_deleted_at:
                cur.execute(
                    "UPDATE registry_skills SET deleted_at = NOW() WHERE skill_id = %s AND deleted_at IS NULL",
                    (skill_id,),
                )
            else:
                cur.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))
            self._log_change_cur(cur, "skill", skill_id, "delete", snapshot, author)
        self._trigger_public_contract_projection_sync()

    def toggle_skill(self, skill_id: str) -> bool:
        snapshot = self.get_skill(skill_id)
        if not snapshot:
            raise KeyError(f"Skill '{skill_id}' not found")
        with self._transaction() as cur:
            cur.execute(
                "UPDATE registry_skills SET enabled = NOT enabled WHERE skill_id = %s RETURNING enabled",
                (skill_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Skill '{skill_id}' not found")
            enabled = row["enabled"]
            snapshot = dict(snapshot)
            snapshot["enabled"] = enabled
            self._log_change_cur(cur, "skill", skill_id, "update", snapshot, "admin")
        self._trigger_public_contract_projection_sync()
        return enabled

    def get_agents_using_skill(self, skill_id: str) -> list[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT agent_id FROM registry_agent_skills WHERE skill_id = %s ORDER BY agent_id",
                (skill_id,),
            )
            return [r["agent_id"] for r in cur.fetchall()]

    def get_skill_dependents(self, skill_id: str, *, limit: int = 26) -> list[dict]:
        bounded_limit = max(1, min(int(limit), 100))
        with self._cursor() as cur:
            cur.execute(
                """SELECT dependency_kind, dependency_id
                   FROM (
                       SELECT 'agent'::text AS dependency_kind,
                              a.agent_id::text AS dependency_id
                       FROM registry_agent_skills relation
                       JOIN registry_agents a ON a.agent_id = relation.agent_id
                       WHERE relation.skill_id = %s

                       UNION

                       SELECT 'workflow_step'::text AS dependency_kind,
                              CONCAT(s.owner_workflow_id, '/', s.step_id)::text
                                  AS dependency_id
                       FROM registry_workflow_steps s
                       WHERE s.skill_id = %s

                       UNION

                       SELECT 'published_block'::text AS dependency_kind,
                              block_key::text AS dependency_id
                       FROM registry_published_block_contracts
                       WHERE source_kind = 'agent'
                         AND split_part(source_ref, '::', 2) = %s
                   ) dependencies
                   ORDER BY dependency_kind, dependency_id
                   LIMIT %s""",
                (skill_id, skill_id, skill_id, bounded_limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_skill_inputs(self, skill_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT i.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                          dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                   FROM registry_skill_inputs i
                   LEFT JOIN registry_data_contracts dc ON dc.contract_id = i.contract_id
                   WHERE i.skill_id = %s
                   ORDER BY i.sort_order""",
                (skill_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_skill_inputs(self, skill_id: str, inputs: list[dict]) -> None:
        snapshot = self.get_skill(skill_id)
        if not snapshot:
            raise KeyError(f"Skill '{skill_id}' not found")
        schema = self._build_contract_schema(inputs)
        with self._transaction() as cur:
            cur.execute("DELETE FROM registry_skill_inputs WHERE skill_id = %s", (skill_id,))
            for i, inp in enumerate(inputs):
                cur.execute(
                    """INSERT INTO registry_skill_inputs
                       (skill_id, name, type, description, required, sort_order, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        skill_id,
                        inp["name"],
                        inp.get("type", "text"),
                        inp.get("description", ""),
                        inp.get("required", False),
                        i,
                        inp.get("contract_id") or None,
                    ),
                )
            cur.execute(
                "UPDATE registry_skills SET input_schema = %s WHERE skill_id = %s",
                (json.dumps(schema), skill_id),
            )
            snapshot = dict(snapshot)
            snapshot["expected_inputs"] = list(inputs)
            snapshot["_inputs"] = list(inputs)
            snapshot["input_schema"] = schema
            self._log_change_cur(cur, "skill", skill_id, "update", snapshot, "admin")
        self._trigger_public_contract_projection_sync()

    def _projected_skill_outputs(self, skill_id: str) -> list[dict]:
        if not str(skill_id or "").startswith("extension."):
            return []

        from core.application.registry.projected_artifact_rows import projected_artifact_rows

        projected = next(
            (
                row
                for row in projected_artifact_rows(self, "skill", "skill_id")
                if str(row.get("skill_id") or "") == skill_id
            ),
            None,
        )
        if projected is None:
            return []
        return [
            dict(output)
            for output in projected.get("expected_outputs") or []
            if isinstance(output, dict) and str(output.get("name") or "").strip()
        ]

    def get_skill_outputs(self, skill_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT o.*, dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                          dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                   FROM registry_skill_outputs o
                   LEFT JOIN registry_data_contracts dc ON dc.contract_id = o.contract_id
                   WHERE o.skill_id = %s
                   ORDER BY o.sort_order""",
                (skill_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows or self._projected_skill_outputs(skill_id)

    def get_skill_contract(self, skill_id: str) -> dict:
        """Return the output contract for a skill.

        Returns a dict with:
          required_fields: list of field names that MUST be present and non-empty
          optional_fields: list of field names that are tracked but not enforced
          all_fields: list of {name, type, required, description} dicts

        Returns empty contract (no required_fields) if skill has no outputs defined.
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT o.name, o.type, o.description, o.required, o.contract_id,
                          dc.display_name AS contract_display_name, dc.kind AS contract_kind,
                          dc.schema_json AS contract_schema, dc.enabled AS contract_enabled
                   FROM registry_skill_outputs o
                   LEFT JOIN registry_data_contracts dc ON dc.contract_id = o.contract_id
                   WHERE o.skill_id = %s
                   ORDER BY o.sort_order""",
                (skill_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows = rows or self._projected_skill_outputs(skill_id)
        return {
            "skill_id": skill_id,
            "required_fields": [r["name"] for r in rows if r.get("required", True)],
            "optional_fields": [r["name"] for r in rows if not r.get("required", True)],
            "all_fields": rows,
        }

    def set_skill_outputs(self, skill_id: str, outputs: list[dict]) -> None:
        snapshot = self.get_skill(skill_id)
        if not snapshot:
            raise KeyError(f"Skill '{skill_id}' not found")
        schema = self._build_contract_schema(outputs)
        with self._transaction() as cur:
            cur.execute("DELETE FROM registry_skill_outputs WHERE skill_id = %s", (skill_id,))
            for i, out in enumerate(outputs):
                cur.execute(
                    """INSERT INTO registry_skill_outputs
                       (skill_id, name, type, description, sort_order, required, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        skill_id,
                        out["name"],
                        out.get("type", "text"),
                        out.get("description", ""),
                        i,
                        out.get("required", True),
                        out.get("contract_id") or None,
                    ),
                )
            cur.execute(
                "UPDATE registry_skills SET output_schema = %s WHERE skill_id = %s",
                (json.dumps(schema), skill_id),
            )
            snapshot = dict(snapshot)
            snapshot["expected_outputs"] = list(outputs)
            snapshot["_outputs"] = list(outputs)
            snapshot["output_schema"] = schema
            self._log_change_cur(cur, "skill", skill_id, "update", snapshot, "admin")
        self._trigger_public_contract_projection_sync()
