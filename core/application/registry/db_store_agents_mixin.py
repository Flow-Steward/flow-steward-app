# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.registry.agent_manifest import refuse_agent_phases
from core.application.registry.catalog_categories import catalog_category
from core.application.registry.db_store_agent_contract_mixin import (
    RegistryDBStoreAgentContractMixin,
)

from .db_store_common import *  # NOSONAR

#: Columns a pre-existing database may still hold that agents no longer have.
_REMOVED_AGENT_COLUMNS = ("phases",)


class AgentRelationshipConflictError(RuntimeError):
    """The relationship snapshot changed after the editor loaded it."""


class AgentRelationshipValidationError(ValueError):
    """A proposed atomic relationship snapshot violates the agent contract."""

    def __init__(self, validation: dict):
        super().__init__("Agent relationship contract validation failed")
        self.validation = validation


class AgentRelationshipReadinessError(AgentRelationshipValidationError):
    """An in-scope relationship was rejected before its transaction committed."""


class RegistryDBStoreAgentsMixin(RegistryDBStoreAgentContractMixin):
    @staticmethod
    def _agent_row(row: Any) -> dict:
        """An agent row without the columns agents no longer have.

        A database created before agents lost ``phases`` still has the column
        (schema changes carry no migration before 1.0). Reading it back into the
        agent would put a field the save path now refuses into every update.
        """
        agent = dict(row)
        for column in _REMOVED_AGENT_COLUMNS:
            agent.pop(column, None)
        return agent

    @staticmethod
    def _lock_agent_relationship_parent(cur, agent_id: str) -> dict:
        """Serialize every relationship writer on the owning agent row."""
        cur.execute(
            "SELECT agent_id, account_id FROM registry_agents WHERE agent_id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Agent '{agent_id}' not found")
        return dict(row)

    @staticmethod
    def _require_relationship_capabilities_in_scope(
        cur,
        *,
        account_id: str,
        table_name: str,
        id_column: str,
        values: list[str],
    ) -> None:
        if not values:
            return
        if account_id:
            cur.execute(
                f"""SELECT {id_column} FROM {table_name}
                    WHERE {id_column} = ANY(%s)
                      AND deleted_at IS NULL
                      AND (account_id IS NULL OR account_id = %s)""",
                (values, account_id),
            )
        else:
            cur.execute(
                f"""SELECT {id_column} FROM {table_name}
                    WHERE {id_column} = ANY(%s)
                      AND deleted_at IS NULL
                      AND account_id IS NULL""",
                (values,),
            )
        visible = {str(row.get(id_column) or "") for row in cur.fetchall()}
        missing = [value for value in values if value not in visible]
        if missing:
            raise AgentRelationshipValidationError(
                {
                    "valid": False,
                    "errors": [
                        {
                            "message": "Skill or tool is outside the agent account scope",
                            "ids": missing,
                        }
                    ],
                    "warnings": [],
                }
            )

    def get_agent(self, agent_id: str) -> dict | None:
        with self._cursor() as cur:
            conditions = ["a.agent_id = %s"]
            params: list[Any] = [agent_id]
            if self._column_exists("registry_agents", "deleted_at"):
                conditions.append(A_DELETED_AT_IS_NULL_VALUE)
            where_sql = SQL_AND.join(conditions)
            cur.execute(
                f"""SELECT a.*
                   FROM registry_agents a
                   WHERE {where_sql}""",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                return None
            agent = self._agent_row(row)
            # Attach skills
            cur.execute(
                """SELECT skill_id, skill_prompt, sort_order
                   FROM registry_agent_skills
                   WHERE agent_id = %s ORDER BY sort_order""",
                (agent_id,),
            )
            agent["_skills"] = [dict(r) for r in cur.fetchall()]
            agent["skills"] = agent["_skills"]
            # Attach tools
            cur.execute(
                """SELECT tool_id, sort_order
                   FROM registry_agent_tools
                   WHERE agent_id = %s ORDER BY sort_order""",
                (agent_id,),
            )
            agent["_tools"] = [dict(r) for r in cur.fetchall()]
            agent["tools"] = agent["_tools"]
            # Attach structured I/O
            cur.execute(
                "SELECT * FROM registry_agent_inputs WHERE agent_id = %s ORDER BY sort_order",
                (agent_id,),
            )
            agent["_inputs"] = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT * FROM registry_agent_outputs WHERE agent_id = %s ORDER BY sort_order",
                (agent_id,),
            )
            agent["_outputs"] = [dict(r) for r in cur.fetchall()]
            self._canonicalize_agent_prompts(agent)
            self._apply_agent_composition_fields(agent)
            return agent

    def list_agents(self, enabled_only: bool = False, include_trashed: bool = False) -> list[dict]:
        with self._cursor() as cur:
            conditions: list[str] = []
            if not include_trashed and self._column_exists("registry_agents", "deleted_at"):
                conditions.append(A_DELETED_AT_IS_NULL_VALUE)
            if enabled_only:
                conditions.append(SQL_AGENT_ENABLED_TRUE)
            where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
            cur.execute(
                f"""SELECT a.*
                    FROM registry_agents a
                    {where_sql}
                    ORDER BY a.agent_id"""
            )
            agents = [self._agent_row(r) for r in cur.fetchall()]

            # Batch-fetch skills and tools for all agents
            agent_ids = [a["agent_id"] for a in agents]
            if agent_ids:
                cur.execute(
                    """SELECT agent_id, skill_id, skill_prompt, sort_order
                       FROM registry_agent_skills
                       WHERE agent_id = ANY(%s)
                       ORDER BY agent_id, sort_order""",
                    (agent_ids,),
                )
                caps_by_agent: dict[str, list] = {}
                for r in cur.fetchall():
                    caps_by_agent.setdefault(r["agent_id"], []).append(dict(r))

                cur.execute(
                    """SELECT agent_id, tool_id, sort_order
                       FROM registry_agent_tools
                       WHERE agent_id = ANY(%s)
                       ORDER BY agent_id, sort_order""",
                    (agent_ids,),
                )
                tools_by_agent: dict[str, list] = {}
                for r in cur.fetchall():
                    tools_by_agent.setdefault(r["agent_id"], []).append(dict(r))

                for a in agents:
                    a["_skills"] = caps_by_agent.get(a["agent_id"], [])
                    a["skills"] = a["_skills"]
                    a["_tools"] = tools_by_agent.get(a["agent_id"], [])
                    a["tools"] = a["_tools"]
                    self._canonicalize_agent_prompts(a)
                    self._apply_agent_composition_fields(a)
            return agents

    def list_agents_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        has_builder_metadata = self._column_exists("registry_agents", "builder_metadata_jsonb")
        has_account_id = self._column_exists("registry_agents", "account_id")
        has_project_id = self._column_exists("registry_agents", "project_id")
        has_scope_kind = self._column_exists("registry_agents", "scope_kind")
        has_source_install_id = self._column_exists("registry_agents", "source_install_id")
        has_availability_status = self._column_exists("registry_agents", "availability_status")
        has_new_runs_blocked = self._column_exists("registry_agents", "new_runs_blocked")
        has_visible_in_catalog = self._column_exists("registry_agents", "visible_in_catalog")
        conditions: list[str] = []
        if self._column_exists("registry_agents", "deleted_at"):
            conditions.append(A_DELETED_AT_IS_NULL_VALUE)
        if enabled_only:
            conditions.append(SQL_AGENT_ENABLED_TRUE)
        normalized_account_id = str(account_id or "").strip()
        normalized_project_id = str(project_id or "").strip()
        if has_account_id and normalized_account_id:
            conditions.append(
                f"({_nullable_scope_sql('a.account_id')} OR a.account_id = %(account_id)s)"
            )
        if has_project_id:
            if normalized_project_id:
                conditions.append(
                    f"({_nullable_scope_sql('a.project_id')} OR a.project_id = %(project_id)s)"
                )
            else:
                conditions.append(_nullable_scope_sql("a.project_id"))
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        builder_select = (
            ", COALESCE(a.builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb"
            if has_builder_metadata
            else ""
        )
        with self._cursor() as cur:
            cur.execute(
                f"""SELECT
                        a.agent_id,
                        a.display_name,
                        a.enabled,
                        LEFT(COALESCE(a.description, ''), 160) AS description_preview,
                        COALESCE(a.category, '') AS category,
                        COALESCE(a.config->>'model', '') AS model,
                        COALESCE(cap_summary.skill_names, '') AS skill_names,
                        COALESCE(cap_summary.skill_count, 0) AS skill_count,
                        COALESCE(tool_summary.tool_count, 0) AS tool_count
                        {", a.account_id" if has_account_id else ""}
                        {", a.project_id" if has_project_id else ""}
                        {", COALESCE(a.scope_kind, 'system') AS scope_kind" if has_scope_kind else ""}
                        {", COALESCE(a.source_install_id, '') AS source_install_id" if has_source_install_id else ""}
                        {", COALESCE(a.availability_status, 'available') AS availability_status" if has_availability_status else ""}
                        {", COALESCE(a.new_runs_blocked, false) AS new_runs_blocked" if has_new_runs_blocked else ""}
                        {", COALESCE(a.visible_in_catalog, true) AS visible_in_catalog" if has_visible_in_catalog else ""}
                        {builder_select}
                    FROM registry_agents a
                    LEFT JOIN (
                        SELECT
                            ac.agent_id,
                            string_agg(
                                COALESCE(c.display_name, ac.skill_id),
                                ', '
                                ORDER BY ac.sort_order, COALESCE(c.display_name, ac.skill_id)
                            ) AS skill_names,
                            COUNT(*)::INT AS skill_count
                        FROM registry_agent_skills ac
                        LEFT JOIN registry_skills c ON c.skill_id = ac.skill_id
                        GROUP BY ac.agent_id
                    ) cap_summary ON cap_summary.agent_id = a.agent_id
                    LEFT JOIN (
                        SELECT agent_id, COUNT(*)::INT AS tool_count
                        FROM registry_agent_tools
                        GROUP BY agent_id
                    ) tool_summary ON tool_summary.agent_id = a.agent_id
                    {where_sql}
                    ORDER BY a.agent_id""",
                {
                    "account_id": normalized_account_id or None,
                    "project_id": normalized_project_id or None,
                },
            )
            return [dict(r) for r in cur.fetchall()]

    def save_agent(
        self,
        data: dict,
        author: str = "admin",
        *,
        sync_relationships: bool = True,
    ) -> None:
        agent_id = data["agent_id"]
        refuse_agent_phases(data)
        is_new = not self._exists("registry_agents", "agent_id", agent_id)
        prompts = strip_agent_output_format(data.get("prompts") or {})
        cap_prompts = dict(prompts.get("skill_prompts") or {})
        prompts.pop("skill_prompts", None)
        if not str(prompts.get("system") or "").strip():
            prompts["system"] = self._default_system_prompt(data)
        skills = self._compose_agent_skill_ids(data)
        if "local_instructions" in data and "local_instructions" not in prompts:
            prompts["local_instructions"] = list(data.get("local_instructions") or [])
        builder_metadata = normalize_builder_metadata(data.get("builder_metadata_jsonb"))

        with self._transaction() as cur:
            payload = {
                "agent_id": agent_id,
                "version": data.get("version", "1.0.0"),
                "display_name": data.get("display_name", agent_id),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "prompts": json.dumps(prompts),
                "config": json.dumps(data.get("config", {})),
                "quality": json.dumps(data.get("quality", {})),
                "inputs": json.dumps(data.get("inputs", {"required": [], "optional": []})),
                "outputs": data.get("outputs", []),
                "depends_on": json.dumps(data.get("depends_on", {"soft": [], "hard": []})),
                "tags": data.get("tags", []),
                "builder_metadata_jsonb": json.dumps(builder_metadata),
            }
            columns = [
                "agent_id",
                "version",
                "display_name",
                "description",
                "enabled",
                "prompts",
                "config",
                "quality",
                "inputs",
                "outputs",
                "depends_on",
                "tags",
            ]
            updates = [
                VERSION_EXCLUDED_VERSION_SQL,
                DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
                DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
                ENABLED_EXCLUDED_ENABLED_SQL,
                "prompts = EXCLUDED.prompts",
                "config = EXCLUDED.config",
                "quality = EXCLUDED.quality",
                "inputs = EXCLUDED.inputs",
                "outputs = EXCLUDED.outputs",
                "depends_on = EXCLUDED.depends_on",
                TAGS_EXCLUDED_TAGS_SQL,
            ]
            optional_columns = [
                ("builder_metadata_jsonb", payload.get("builder_metadata_jsonb")),
                ("category", catalog_category(data.get("category"))),
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
            for column_name, column_value in optional_columns:
                if not self._column_exists("registry_agents", column_name):
                    continue
                columns.append(column_name)
                updates.append(f"{column_name} = EXCLUDED.{column_name}")
                payload[column_name] = column_value
            cur.execute(
                f"""INSERT INTO registry_agents ({", ".join(columns)})
                    VALUES ({", ".join(f"%({column})s" for column in columns)})
                    ON CONFLICT (agent_id) DO UPDATE SET
                        {", ".join(updates)}""",
                payload,
            )
            if sync_relationships:
                agent = self._lock_agent_relationship_parent(cur, agent_id)
                relationship_account_id = str(agent.get("account_id") or "")
                caps = self._unique_relationship_ids(skills)
                tools = self._unique_relationship_ids(data.get("tools", []))
                self._require_relationship_capabilities_in_scope(
                    cur,
                    account_id=relationship_account_id,
                    table_name="registry_skills",
                    id_column="skill_id",
                    values=caps,
                )
                self._require_relationship_capabilities_in_scope(
                    cur,
                    account_id=relationship_account_id,
                    table_name="registry_tools",
                    id_column="tool_id",
                    values=tools,
                )
                cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
                for i, cap_id in enumerate(caps):
                    cur.execute(
                        """INSERT INTO registry_agent_skills
                           (agent_id, skill_id, sort_order, skill_prompt)
                           VALUES (%s, %s, %s, %s)""",
                        (agent_id, cap_id, i, cap_prompts.get(cap_id, "")),
                    )
                cur.execute("DELETE FROM registry_agent_tools WHERE agent_id = %s", (agent_id,))
                for i, tool_id in enumerate(tools):
                    cur.execute(
                        """INSERT INTO registry_agent_tools (agent_id, tool_id, sort_order)
                           VALUES (%s, %s, %s)""",
                        (agent_id, tool_id, i),
                    )
                changelog_snapshot = data
            else:
                # PATCH does not own relationships. Materialize them while
                # holding the same parent lock used by every relationship
                # writer so the revision remains safe to restore later.
                self._lock_agent_relationship_parent(cur, agent_id)
                cur.execute(
                    """SELECT skill_id, skill_prompt, sort_order
                       FROM registry_agent_skills
                       WHERE agent_id = %s ORDER BY sort_order, skill_id""",
                    (agent_id,),
                )
                skill_rows = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """SELECT tool_id, sort_order
                       FROM registry_agent_tools
                       WHERE agent_id = %s ORDER BY sort_order, tool_id""",
                    (agent_id,),
                )
                tool_rows = [dict(row) for row in cur.fetchall()]
                skill_ids = [str(row.get("skill_id") or "") for row in skill_rows]
                changelog_snapshot = dict(data)
                changelog_snapshot["_skills"] = skill_rows
                changelog_snapshot["skills"] = skill_rows
                changelog_snapshot["_tools"] = tool_rows
                changelog_snapshot["tools"] = tool_rows
                changelog_snapshot["base_skill"] = skill_ids[0] if skill_ids else ""
                changelog_snapshot["secondary_skills"] = skill_ids[1:]
                snapshot_prompts = dict(prompts)
                snapshot_prompts["skill_prompts"] = {
                    str(row.get("skill_id") or ""): str(row.get("skill_prompt") or "")
                    for row in skill_rows
                    if str(row.get("skill_id") or "").strip()
                }
                changelog_snapshot["prompts"] = snapshot_prompts
            if sync_relationships and ("structured_inputs" in data or "structured_outputs" in data):
                # The public own-contract writers reuse this active transaction.
                # Materialization then derives inherited skill/tool I/O from the
                # clone's own durable relationship rows before the changelog is saved.
                if "structured_inputs" in data:
                    self.set_agent_inputs(agent_id, list(data.get("structured_inputs") or []))
                if "structured_outputs" in data:
                    self.set_agent_outputs(agent_id, list(data.get("structured_outputs") or []))
                self.materialize_agent_contract(agent_id)
            self._log_change_cur(
                cur,
                "agent",
                agent_id,
                "create" if is_new else "update",
                changelog_snapshot,
                author,
            )
        self._trigger_public_contract_projection_sync()

    def delete_agent(self, agent_id: str, author: str = "admin") -> None:
        snapshot = self.get_agent(agent_id)
        if not snapshot:
            raise KeyError(f"Agent '{agent_id}' not found")
        has_deleted_at = self._column_exists("registry_agents", "deleted_at")
        has_deleted_by = self._column_exists("registry_agents", "deleted_by")
        with self._transaction() as cur:
            if has_deleted_at and has_deleted_by:
                cur.execute(
                    "UPDATE registry_agents SET deleted_at = NOW(), deleted_by = %s WHERE agent_id = %s AND deleted_at IS NULL",
                    (author, agent_id),
                )
            elif has_deleted_at:
                cur.execute(
                    "UPDATE registry_agents SET deleted_at = NOW() WHERE agent_id = %s AND deleted_at IS NULL",
                    (agent_id,),
                )
            else:
                cur.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
            self._log_change_cur(cur, "agent", agent_id, "delete", snapshot, author)
        self._trigger_public_contract_projection_sync()

    def toggle_agent(self, agent_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE registry_agents SET enabled = NOT enabled
                   WHERE agent_id = %s RETURNING enabled""",
                (agent_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Agent '{agent_id}' not found")
            enabled = row["enabled"]
        self._trigger_public_contract_projection_sync()
        return enabled

    def get_agent_skills(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT ac.skill_id, ac.sort_order, ac.skill_prompt,
                          c.display_name, c.category, c.description, c.enabled
                   FROM registry_agent_skills ac
                   JOIN registry_skills c ON c.skill_id = ac.skill_id
                   WHERE ac.agent_id = %s ORDER BY ac.sort_order""",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_agent_skill_pairs(
        self,
        agent_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict]:
        """Return valid agent+skill pairs for quick workflow step creation."""
        where = []
        params: list = []
        if agent_id:
            where.append("ac.agent_id = %s")
            params.append(agent_id)
        if enabled_only:
            where.append(SQL_AGENT_ENABLED_TRUE)
            where.append("c.enabled = true")

        where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ac.agent_id,
                    a.display_name AS agent_display_name,
                    ac.skill_id,
                    c.display_name AS skill_display_name,
                    ac.sort_order
                FROM registry_agent_skills ac
                JOIN registry_agents a ON a.agent_id = ac.agent_id
                JOIN registry_skills c ON c.skill_id = ac.skill_id
                {where_sql}
                ORDER BY ac.agent_id, ac.sort_order, ac.skill_id
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            row["pair_id"] = f"{row['agent_id']}::{row['skill_id']}"
        return rows

    def add_agent_skill(
        self,
        agent_id: str,
        skill_id: str,
        prompt: str = "",
        *,
        validate_skill_compatibility=None,
        validate_agent=None,
    ) -> None:
        """Commit a relationship only after supplied contract checks pass.

        As with atomic relationship replacement, projection sync happens after
        commit, never between inserting the mapping and validating it.
        """
        with self._transaction() as cur:
            agent = self._lock_agent_relationship_parent(cur, agent_id)
            self._require_relationship_capabilities_in_scope(
                cur,
                account_id=str(agent.get("account_id") or ""),
                table_name="registry_skills",
                id_column="skill_id",
                values=[skill_id],
            )
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM registry_agent_skills WHERE agent_id = %s",
                (agent_id,),
            )
            next_order = cur.fetchone()["next"]
            cur.execute(
                """INSERT INTO registry_agent_skills (agent_id, skill_id, sort_order, skill_prompt)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                   RETURNING agent_id""",
                (agent_id, skill_id, next_order, prompt),
            )
            if cur.fetchone() is None:
                raise AgentRelationshipConflictError("Skill already assigned")
            if callable(validate_skill_compatibility):
                validation = validate_skill_compatibility(self, agent_id, skill_id)
                if not validation.get("valid", False):
                    raise AgentRelationshipReadinessError(validation)
            if callable(validate_agent):
                validation = validate_agent(self, agent_id)
                if not validation.get("valid", False):
                    raise AgentRelationshipReadinessError(validation)
            if callable(validate_skill_compatibility) or callable(validate_agent):
                self.materialize_agent_contract(agent_id)
        self._trigger_public_contract_projection_sync()

    def remove_agent_skill(self, agent_id: str, skill_id: str) -> None:
        with self._transaction() as cur:
            self._lock_agent_relationship_parent(cur, agent_id)
            cur.execute(
                "DELETE FROM registry_agent_skills WHERE agent_id = %s AND skill_id = %s",
                (agent_id, skill_id),
            )
        self._trigger_public_contract_projection_sync()

    def reorder_agent_skills(self, agent_id: str, ordered_ids: list[str]) -> None:
        with self._transaction() as cur:
            self._lock_agent_relationship_parent(cur, agent_id)
            for i, cap_id in enumerate(ordered_ids):
                cur.execute(
                    """UPDATE registry_agent_skills SET sort_order = %s
                       WHERE agent_id = %s AND skill_id = %s""",
                    (i, agent_id, cap_id),
                )

    def update_skill_prompt(self, agent_id: str, skill_id: str, prompt: str) -> None:
        with self._transaction() as cur:
            self._lock_agent_relationship_parent(cur, agent_id)
            cur.execute(
                """UPDATE registry_agent_skills SET skill_prompt = %s
                   WHERE agent_id = %s AND skill_id = %s""",
                (prompt, agent_id, skill_id),
            )

    def get_agent_tools(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT at.tool_id, at.sort_order,
                          t.display_name, t.category, t.enabled
                   FROM registry_agent_tools at
                   JOIN registry_tools t ON t.tool_id = at.tool_id
                   WHERE at.agent_id = %s ORDER BY at.sort_order""",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def add_agent_tool(self, agent_id: str, tool_id: str) -> None:
        with self._transaction() as cur:
            agent = self._lock_agent_relationship_parent(cur, agent_id)
            self._require_relationship_capabilities_in_scope(
                cur,
                account_id=str(agent.get("account_id") or ""),
                table_name="registry_tools",
                id_column="tool_id",
                values=[tool_id],
            )
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM registry_agent_tools WHERE agent_id = %s",
                (agent_id,),
            )
            next_order = cur.fetchone()["next"]
            cur.execute(
                """INSERT INTO registry_agent_tools (agent_id, tool_id, sort_order)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                   RETURNING agent_id""",
                (agent_id, tool_id, next_order),
            )
            if cur.fetchone() is None:
                raise AgentRelationshipConflictError("Tool already assigned")

    def remove_agent_tool(self, agent_id: str, tool_id: str) -> None:
        with self._transaction() as cur:
            self._lock_agent_relationship_parent(cur, agent_id)
            cur.execute(
                "DELETE FROM registry_agent_tools WHERE agent_id = %s AND tool_id = %s",
                (agent_id, tool_id),
            )

    @staticmethod
    def _unique_relationship_ids(values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
        )

    def replace_agent_relationships(
        self,
        agent_id: str,
        *,
        expected_skill_ids: list[str],
        expected_tool_ids: list[str],
        skill_ids: list[str],
        tool_ids: list[str],
        validate_skill_compatibility=None,
        validate_agent=None,
    ) -> dict:
        """CAS-replace skills and tools in one transaction.

        Existing per-skill prompts are retained for skills that remain assigned.
        The expected lists are the optimistic concurrency token: an order or
        membership change made by another editor rejects the whole operation.
        """
        expected_skills = self._unique_relationship_ids(expected_skill_ids)
        expected_tools = self._unique_relationship_ids(expected_tool_ids)
        desired_skills = self._unique_relationship_ids(skill_ids)
        desired_tools = self._unique_relationship_ids(tool_ids)
        with self._transaction() as cur:
            agent = self._lock_agent_relationship_parent(cur, agent_id)
            account_id = str(agent.get("account_id") or "")
            self._require_relationship_capabilities_in_scope(
                cur,
                account_id=account_id,
                table_name="registry_skills",
                id_column="skill_id",
                values=desired_skills,
            )
            self._require_relationship_capabilities_in_scope(
                cur,
                account_id=account_id,
                table_name="registry_tools",
                id_column="tool_id",
                values=desired_tools,
            )
            cur.execute(
                """SELECT skill_id, skill_prompt FROM registry_agent_skills
                   WHERE agent_id = %s ORDER BY sort_order, skill_id""",
                (agent_id,),
            )
            skill_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """SELECT tool_id FROM registry_agent_tools
                   WHERE agent_id = %s ORDER BY sort_order, tool_id""",
                (agent_id,),
            )
            tool_rows = [dict(row) for row in cur.fetchall()]
            current_skills = [str(row.get("skill_id") or "") for row in skill_rows]
            current_tools = [str(row.get("tool_id") or "") for row in tool_rows]
            if current_skills != expected_skills or current_tools != expected_tools:
                raise AgentRelationshipConflictError(
                    "Agent relationships changed; reload and retry"
                )
            if current_skills == desired_skills and current_tools == desired_tools:
                return {
                    "status": "no_change",
                    "skills": self.get_agent_skills(agent_id),
                    "tools": self.get_agent_tools(agent_id),
                }

            prompts = {
                str(row.get("skill_id") or ""): str(row.get("skill_prompt") or "")
                for row in skill_rows
            }
            cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
            for sort_order, skill_id in enumerate(desired_skills):
                cur.execute(
                    """INSERT INTO registry_agent_skills
                       (agent_id, skill_id, sort_order, skill_prompt)
                       VALUES (%s, %s, %s, %s)""",
                    (agent_id, skill_id, sort_order, prompts.get(skill_id, "")),
                )
            cur.execute("DELETE FROM registry_agent_tools WHERE agent_id = %s", (agent_id,))
            for sort_order, tool_id in enumerate(desired_tools):
                cur.execute(
                    """INSERT INTO registry_agent_tools (agent_id, tool_id, sort_order)
                       VALUES (%s, %s, %s)""",
                    (agent_id, tool_id, sort_order),
                )

            if callable(validate_skill_compatibility):
                for skill_id in desired_skills:
                    pair_validation = validate_skill_compatibility(self, agent_id, skill_id)
                    if not pair_validation.get("valid", False):
                        raise AgentRelationshipValidationError(pair_validation)
            if callable(validate_agent):
                agent_validation = validate_agent(self, agent_id)
                if not agent_validation.get("valid", False):
                    raise AgentRelationshipValidationError(agent_validation)
            self.materialize_agent_contract(agent_id)
            result = {
                "status": "updated",
                "skills": self.get_agent_skills(agent_id),
                "tools": self.get_agent_tools(agent_id),
            }
        self._trigger_public_contract_projection_sync()
        return result

    def get_agent_inputs(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM registry_agent_inputs WHERE agent_id = %s ORDER BY sort_order",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_agent_own_inputs(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM registry_agent_inputs
                   WHERE agent_id = %s AND (source = 'own' OR source IS NULL)
                   ORDER BY sort_order""",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_agent_inputs(self, agent_id: str, inputs: list[dict]) -> None:
        """Replace the agent-authored (own) inputs, leaving inherited rows intact.

        Callers should follow with materialize_agent_contract() to refresh the
        inherited rows against the new own set.
        """
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM registry_agent_inputs "
                "WHERE agent_id = %s AND (source = 'own' OR source IS NULL)",
                (agent_id,),
            )
            for i, inp in enumerate(inputs):
                cur.execute(
                    """INSERT INTO registry_agent_inputs
                       (agent_id, name, type, description, required, sort_order, source)
                       VALUES (%s, %s, %s, %s, %s, %s, 'own')""",
                    (
                        agent_id,
                        inp["name"],
                        inp.get("type", "text"),
                        inp.get("description", ""),
                        inp.get("required", False),
                        i,
                    ),
                )

    def get_agent_outputs(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM registry_agent_outputs WHERE agent_id = %s ORDER BY sort_order",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_agent_own_outputs(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM registry_agent_outputs
                   WHERE agent_id = %s AND (source = 'own' OR source IS NULL)
                   ORDER BY sort_order""",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_agent_outputs(self, agent_id: str, outputs: list[dict]) -> None:
        """Replace the agent-authored (own) outputs, leaving inherited rows intact."""
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM registry_agent_outputs "
                "WHERE agent_id = %s AND (source = 'own' OR source IS NULL)",
                (agent_id,),
            )
            for i, out in enumerate(outputs):
                cur.execute(
                    """INSERT INTO registry_agent_outputs
                       (agent_id, name, type, description, required, sort_order, source)
                       VALUES (%s, %s, %s, %s, %s, %s, 'own')""",
                    (
                        agent_id,
                        out["name"],
                        out.get("type", "text"),
                        out.get("description", ""),
                        out.get("required", True),
                        i,
                    ),
                )

    def materialize_agent_contract(self, agent_id: str) -> None:
        """Rebuild registry_agent_inputs/outputs for one agent from its effective I/O."""
        aid = str(agent_id or "").strip()
        if not aid:
            return
        self.ensure_builder_registry_schema()
        from core.application.registry.io_resolver import IOResolver

        effective = IOResolver(self).compute_effective_io(aid, own_only=True)
        inputs = self._materialized_contract_rows(effective.get("inputs"))
        outputs = self._materialized_contract_rows(effective.get("outputs"), output_contract=True)
        with self._transaction() as cur:
            cur.execute("DELETE FROM registry_agent_inputs WHERE agent_id = %s", (aid,))
            for i, row in enumerate(inputs):
                cur.execute(
                    """INSERT INTO registry_agent_inputs
                       (agent_id, name, type, description, required, sort_order, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        aid,
                        row["name"],
                        row["type"],
                        row["description"],
                        row["required"],
                        i,
                        row["source"],
                    ),
                )
            cur.execute("DELETE FROM registry_agent_outputs WHERE agent_id = %s", (aid,))
            for i, row in enumerate(outputs):
                cur.execute(
                    """INSERT INTO registry_agent_outputs
                       (agent_id, name, type, description, required, sort_order, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        aid,
                        row["name"],
                        row["type"],
                        row["description"],
                        row["required"],
                        i,
                        row["source"],
                    ),
                )

    def materialize_agents_for_skill(self, skill_id: str) -> None:
        sid = str(skill_id or "").strip()
        if not sid:
            return
        for agent_id in self.get_agents_using_skill(sid) or []:
            self.materialize_agent_contract(agent_id)

    def materialize_agents_for_tool(self, tool_id: str) -> None:
        tid = str(tool_id or "").strip()
        if not tid:
            return
        for agent_id in self.get_agents_using_tool(tid) or []:
            self.materialize_agent_contract(agent_id)

    def materialize_all_agent_contracts(self) -> int:
        """Backfill: rebuild the materialized contract for every agent."""
        count = 0
        for agent in self.list_agents(include_trashed=True) or []:
            agent_id = str((agent or {}).get("agent_id") or "").strip()
            if not agent_id:
                continue
            self.materialize_agent_contract(agent_id)
            count += 1
        return count

    def ensure_agent_contracts_materialized(self) -> None:
        """One-time self-heal so a fresh database has materialized contracts.

        If the contract tables are empty (new install, or first start after this
        feature shipped), backfill every agent. After the first run the save-time
        triggers keep contracts fresh, so the existence probe short-circuits on
        every later startup. Best-effort: never block registry init on failure.
        """
        try:
            self.ensure_builder_registry_schema()
            with self._cursor() as cur:
                cur.execute("SELECT 1 FROM registry_agent_outputs LIMIT 1")
                if cur.fetchone():
                    return
                cur.execute("SELECT 1 FROM registry_agent_inputs LIMIT 1")
                if cur.fetchone():
                    return
            materialized = self.materialize_all_agent_contracts()
            if materialized:
                logger.info("Materialized agent contracts for %d agents", materialized)
        except Exception:  # pragma: no cover - self-heal must not break startup
            logger.warning("Agent contract self-heal skipped", exc_info=True)

    def get_effective_tools(self, agent_id: str) -> list[dict]:
        """All tools assigned directly to the agent."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT DISTINCT t.tool_id, t.display_name, t.category, t.enabled,
                          'direct' AS source
                   FROM registry_tools t
                   JOIN registry_agent_tools at ON at.tool_id = t.tool_id
                   WHERE at.agent_id = %s
                   ORDER BY t.tool_id""",
                (agent_id,),
            )
            return [dict(r) for r in cur.fetchall()]


__all__ = [
    "RegistryDBStoreAgentsMixin",
]
