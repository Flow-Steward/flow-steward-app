# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .db_store_common import *  # NOSONAR


class RegistryDBStoreGuardrailsMixin:
    _GUARDRAIL_OWNERSHIP_COLUMNS = (
        "account_id",
        "created_by",
        "updated_by",
        "deleted_by",
    )

    def _require_guardrail_ownership_schema(self) -> None:
        missing = [
            column
            for column in self._GUARDRAIL_OWNERSHIP_COLUMNS
            if not self._column_exists("registry_guardrails", column)
        ]
        missing.extend(
            f"registry_changelog.{column}"
            for column in ("account_id", "actor_id", "actor_type")
            if not self._column_exists("registry_changelog", column)
        )
        if missing:
            raise RuntimeError(
                "Guardrail ownership migration is required; missing: " + ", ".join(missing)
            )

    def get_step_kind_profile(self, step_kind_key: str) -> dict | None:
        if not self._table_exists("registry_step_kind_profiles"):
            return None
        normalized_key = str(step_kind_key or "").strip()
        if not normalized_key:
            return None
        with self._cursor() as cur:
            cur.execute(
                """SELECT *
                   FROM registry_step_kind_profiles
                   WHERE step_kind_key = %s""",
                (normalized_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_step_kind_profiles(self, enabled_only: bool = False) -> list[dict]:
        if not self._table_exists("registry_step_kind_profiles"):
            return []
        clauses: list[str] = []
        if enabled_only:
            clauses.append("enabled = true")
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""SELECT *
                    FROM registry_step_kind_profiles
                    {where_sql}
                    ORDER BY step_kind_key""",
            )
            return [dict(row) for row in cur.fetchall()]

    def upsert_step_kind_profile(self, data: dict, author: str = "admin") -> None:
        self.ensure_builder_registry_schema()
        step_kind_key = str(data.get("step_kind_key") or "").strip()
        if not step_kind_key:
            raise ValueError("step_kind_key is required")
        payload = {
            "step_kind_key": step_kind_key,
            "display_name": str(data.get("display_name") or step_kind_key).strip() or step_kind_key,
            "description": str(data.get("description") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
            "builder_metadata_jsonb": json.dumps(
                normalize_builder_metadata(data.get("builder_metadata_jsonb"))
            ),
            "config_schema_jsonb": json.dumps(data.get("config_schema_jsonb") or {}),
        }
        is_new = self.get_step_kind_profile(step_kind_key) is None
        with self._transaction() as cur:
            cur.execute(
                """INSERT INTO registry_step_kind_profiles
                   (step_kind_key, display_name, description, enabled, builder_metadata_jsonb, config_schema_jsonb)
                   VALUES (%(step_kind_key)s, %(display_name)s, %(description)s, %(enabled)s, %(builder_metadata_jsonb)s, %(config_schema_jsonb)s)
                   ON CONFLICT (step_kind_key) DO UPDATE SET
                       display_name = EXCLUDED.display_name,
                       description = EXCLUDED.description,
                       enabled = EXCLUDED.enabled,
                       builder_metadata_jsonb = EXCLUDED.builder_metadata_jsonb,
                       config_schema_jsonb = EXCLUDED.config_schema_jsonb,
                       updated_at = NOW()""",
                payload,
            )
            self._log_change_cur(
                cur,
                "step_kind_profile",
                step_kind_key,
                "create" if is_new else "update",
                {
                    **data,
                    "builder_metadata_jsonb": normalize_builder_metadata(
                        data.get("builder_metadata_jsonb")
                    ),
                },
                author,
            )

    def _guardrail_account_clause(
        self,
        account_id: str | None,
        clauses: list[str],
        params: list[Any],
    ) -> None:
        """Restrict a guardrail query to one account when the column exists.

        RC7-006: guardrails were the one mutable registry root with neither an
        account column nor the require_default_registry_account guard that
        covers the other global roots, so a foreign-account admin could reach a
        first-party row. The column check keeps this usable on a database that
        has not yet run the additive migration; once it has, every query is
        account-qualified.
        """
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError("account_id is required for guardrail access")
        self._require_guardrail_ownership_schema()
        clauses.append("account_id::text = %s")
        params.append(normalized_account_id)

    def guardrail_assignable_agent_ids(
        self, candidate_ids: list[str], *, account_id: str
    ) -> set[str]:
        """Return durable, nondeleted candidates owned by the resolved account.

        Disabled agents remain valid scope targets. This read does not replace
        the writer's transactional ownership check or the assignment FK.
        """
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError("account_id is required for guardrail assignment")
        normalized_ids = list(dict.fromkeys(str(value or "").strip() for value in candidate_ids))
        normalized_ids = [value for value in normalized_ids if value]
        if not normalized_ids:
            return set()
        with self._cursor() as cur:
            cur.execute(
                """SELECT agent_id FROM registry_agents
                   WHERE agent_id = ANY(%s)
                     AND account_id::text = %s
                     AND deleted_at IS NULL""",
                (normalized_ids, normalized_account_id),
            )
            return {str(row["agent_id"]) for row in cur.fetchall()}

    def get_guardrail(
        self,
        guardrail_id: str,
        include_trashed: bool = False,
        *,
        account_id: str | None = None,
    ) -> dict | None:
        """Get a guardrail by ID, including agent scope."""
        clauses = ["id::text = %s"]
        normalized_guardrail_id = str(guardrail_id or "").strip()
        if not normalized_guardrail_id:
            return None
        params: list[Any] = [normalized_guardrail_id]
        self._guardrail_account_clause(account_id, clauses, params)
        if not include_trashed and self._column_exists("registry_guardrails", "deleted_at"):
            clauses.append(SQL_DELETED_AT_IS_NULL)
        where_sql = SQL_AND.join(clauses)
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM registry_guardrails WHERE {where_sql}", tuple(params))
            row = cur.fetchone()
            if not row:
                return None
            g = dict(row)
            cur.execute(
                """SELECT ga.agent_id
                     FROM registry_guardrail_agents ga
                     JOIN registry_guardrails g ON g.id = ga.guardrail_id
                    WHERE ga.guardrail_id::text = %s
                      AND g.account_id::text = %s""",
                (normalized_guardrail_id, str(account_id).strip()),
            )
            g["_agents"] = [r["agent_id"] for r in cur.fetchall()]
            return g

    def list_guardrails(
        self,
        project_id: str | None = None,
        enabled_only: bool = False,
        include_trashed: bool = False,
        *,
        account_id: str | None = None,
    ) -> list[dict]:
        """List all guardrails, optionally filtered by project and/or enabled status."""
        with self._cursor() as cur:
            clauses = []
            params: list = []
            self._guardrail_account_clause(account_id, clauses, params)
            if project_id is not None:
                clauses.append("(project_id IS NULL OR project_id = %s)")
                params.append(project_id)
            if enabled_only:
                clauses.append(SQL_ENABLED_TRUE)
            if not include_trashed and self._column_exists("registry_guardrails", "deleted_at"):
                clauses.append(SQL_DELETED_AT_IS_NULL)
            where = "WHERE " + SQL_AND.join(clauses) if clauses else ""
            cur.execute(
                f"SELECT * FROM registry_guardrails {where} ORDER BY sort_order, id",
                params,
            )
            guardrails = [dict(r) for r in cur.fetchall()]

            # Batch-fetch agent scopes
            gids = [str(g.get("id") or "").strip() for g in guardrails]
            gids = [gid for gid in gids if gid]
            if gids:
                cur.execute(
                    """SELECT ga.guardrail_id, ga.agent_id
                         FROM registry_guardrail_agents ga
                         JOIN registry_guardrails g ON g.id = ga.guardrail_id
                        WHERE ga.guardrail_id::text = ANY(%s)
                          AND g.account_id::text = %s""",
                    (gids, str(account_id).strip()),
                )
                agents_by_gid: dict[Any, list] = {}
                for r in cur.fetchall():
                    agents_by_gid.setdefault(str(r["guardrail_id"]), []).append(r["agent_id"])
                for g in guardrails:
                    g["_agents"] = agents_by_gid.get(str(g.get("id") or ""), [])
            else:
                for g in guardrails:
                    g["_agents"] = []
            return guardrails

    def _extension_runtime_store(self) -> Any | None:
        try:
            from core.application.extensions.service_support import (
                get_default_extension_runtime_store,
            )
            from core.infrastructure.db.platform_stores import PlatformIdentityStore

            dsn = str(self.dsn or "").strip()
            if not dsn:
                return None
            return get_default_extension_runtime_store(
                get_logger=lambda: PlatformIdentityStore(dsn)
            )
        except Exception as exc:
            logger.debug("registry guardrail extension runtime store unavailable: %s", exc)
            return None

    @staticmethod
    def _normalize_extension_guardrail_row(row: Any) -> dict[str, Any]:
        if hasattr(row, "model_dump"):
            projection = row.model_dump(mode="json")
        elif isinstance(row, dict):
            projection = dict(row)
        else:
            projection = {}
        snapshot = projection.get("projection")
        snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        guardrail_id = str(
            snapshot.get("id")
            or snapshot.get("guardrail_id")
            or projection.get("artifact_id")
            or ""
        ).strip()
        if not guardrail_id:
            return {}
        snapshot["id"] = guardrail_id
        snapshot["guardrail_id"] = guardrail_id
        snapshot["source_install_id"] = str(
            snapshot.get("source_install_id") or projection.get("install_id") or ""
        ).strip()
        snapshot["source_release_id"] = str(
            snapshot.get("source_release_id") or projection.get("release_id") or ""
        ).strip()
        snapshot["projection_mode"] = (
            str(snapshot.get("projection_mode") or "derived").strip() or "derived"
        )
        snapshot["enabled"] = bool(snapshot.get("enabled", projection.get("is_active", True)))
        raw_agents = snapshot.get("_agents")
        if not isinstance(raw_agents, list):
            raw_agents = snapshot.get("agents")
        snapshot["_agents"] = [
            str(item or "").strip() for item in (raw_agents or []) if str(item or "").strip()
        ]
        return snapshot

    def _extension_guardrails_for_agent(
        self,
        agent_id: str,
        project_id: str | None = None,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> list[dict]:
        runtime_store = self._extension_runtime_store()
        if runtime_store is None or not hasattr(runtime_store, "list_projections"):
            return []
        normalized_account_id = _normalize_extension_runtime_account_id(account_id)
        if not normalized_account_id:
            logger.debug(
                "Skipping Extension runtime guardrail lookup for non-UUID account scope '%s'",
                account_id,
            )
            return []
        try:
            rows = runtime_store.list_projections(
                account_id=normalized_account_id,
                artifact_kind="guardrail",
                only_active=True,
            )
        except Exception as exc:
            logger.warning("Extension runtime guardrail lookup failed: %s", exc)
            return []

        normalized_project_id = str(project_id or "").strip()
        normalized_agent_id = str(agent_id or "").strip()
        matched: list[dict] = []
        for row in rows or []:
            snapshot = self._normalize_extension_guardrail_row(row)
            if not snapshot or not bool(snapshot.get("enabled", True)):
                continue
            row_project_id = str(snapshot.get("project_id") or "").strip()
            if row_project_id and row_project_id != normalized_project_id:
                continue
            scoped_agents = [
                str(item or "").strip()
                for item in (snapshot.get("_agents") or [])
                if str(item or "").strip()
            ]
            if normalized_agent_id not in scoped_agents:
                continue
            matched.append(snapshot)
        return matched

    def list_guardrails_for_agent(
        self,
        agent_id: str,
        project_id: str | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> list[dict]:
        """Load guardrail rules applicable to a specific agent + project.

        Returns rules where:
        - enabled = true
        - (project_id IS NULL OR project_id = given project_id)
        - agent_id is explicitly assigned to the guardrail
        """
        include_deleted_filter = self._column_exists("registry_guardrails", "deleted_at")
        with self._cursor() as cur:
            query_lines = [
                "SELECT g.*",
                "FROM registry_guardrails g",
                "WHERE g.enabled = true",
                "  AND (g.project_id IS NULL OR g.project_id = %s)",
            ]
            if include_deleted_filter:
                query_lines.append("  AND g.deleted_at IS NULL")
            # RC7-006: this is the runtime evaluation path. It already received
            # account_id and passed it to the extension lookup below, while the
            # first-party query itself was unscoped - so a guardrail owned by
            # another account could take part in evaluating this agent.
            runtime_params: list[Any] = [project_id]
            self._require_guardrail_ownership_schema()
            query_lines.append("  AND g.account_id::text = %s")
            runtime_params.append(str(account_id).strip())
            query_lines.extend(
                [
                    "  AND EXISTS (",
                    "      SELECT 1 FROM registry_guardrail_agents ga",
                    "      WHERE ga.guardrail_id = g.id AND ga.agent_id = %s",
                    "  )",
                    "ORDER BY g.sort_order, g.id",
                ]
            )
            runtime_params.append(agent_id)
            cur.execute("\n".join(query_lines), tuple(runtime_params))
            registry_rows = [dict(r) for r in cur.fetchall()]

        seen_ids = {
            str(row.get("id") or row.get("guardrail_id") or "").strip()
            for row in registry_rows
            if str(row.get("id") or row.get("guardrail_id") or "").strip()
        }
        merged_rows = list(registry_rows)
        for row in self._extension_guardrails_for_agent(
            agent_id,
            project_id,
            account_id=account_id,
        ):
            guardrail_id = str(row.get("id") or row.get("guardrail_id") or "").strip()
            if not guardrail_id or guardrail_id in seen_ids:
                continue
            merged_rows.append(row)
            seen_ids.add(guardrail_id)
        return merged_rows

    def save_guardrail(
        self,
        data: dict,
        author: str = "admin",
        *,
        account_id: str,
        actor_id: str,
        actor_type: str,
        create_only: bool = False,
    ) -> str:
        """Create or update a guardrail rule. Returns the guardrail id.

        *create_only* creates the rule under its given id or raises; an existing
        rule with that id, in any account or in Trash, is never updated.
        """
        self._require_guardrail_ownership_schema()
        data = dict(data or {})
        normalized_account_id = str(account_id or "").strip()
        normalized_actor_id = str(actor_id or "").strip()
        normalized_actor_type = str(actor_type or "").strip()
        if not normalized_account_id or not normalized_actor_id or not normalized_actor_type:
            raise ValueError("account_id, actor_id, and actor_type are required")
        agents = list(
            dict.fromkeys(str(agent_id or "").strip() for agent_id in (data.get("agents") or []))
        )
        agents = [agent_id for agent_id in agents if agent_id]
        if not agents:
            raise ValueError("guardrail agents must contain at least one agent_id")

        gid = str(data.get("id") or "").strip()
        if gid:
            try:
                gid = str(uuid.UUID(gid))
            except (TypeError, ValueError) as exc:
                raise ValueError("guardrail id must be a UUID") from exc
        is_new = not gid
        created = is_new

        with self._transaction() as cur:
            cur.execute(
                """SELECT agent_id FROM registry_agents
                    WHERE agent_id = ANY(%s) AND account_id::text = %s""",
                (agents, normalized_account_id),
            )
            owned_agent_ids = {str(row["agent_id"]) for row in cur.fetchall()}
            missing_agent_ids = sorted(set(agents) - owned_agent_ids)
            if missing_agent_ids:
                raise ValueError(
                    "guardrail agents are not available in the owning account: "
                    + ", ".join(missing_agent_ids)
                )
            if create_only:
                if is_new:
                    raise ValueError("a create-only guardrail needs its id")
                cur.execute(
                    """
                    INSERT INTO registry_guardrails
                    (id, display_name, description, enabled, mode, rule_type,
                     severity, config, prompt_text, agent_prompt, project_id, sort_order,
                     account_id, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                """,
                    (
                        gid,
                        data.get("display_name", UNNAMED_RULE_VALUE),
                        data.get("description", ""),
                        data.get("enabled", True),
                        data["mode"],
                        data["rule_type"],
                        data.get("severity", "error"),
                        json.dumps(data.get("config", {})),
                        data.get("prompt_text", ""),
                        data.get("agent_prompt", ""),
                        data.get("project_id"),
                        data.get("sort_order", 0),
                        normalized_account_id,
                        normalized_actor_id,
                        normalized_actor_id,
                    ),
                )
                created_row = cur.fetchone()
                if created_row is None:
                    raise RegistryCreateConflictError("guardrail", gid)
                gid = created_row["id"]
                created = True
            elif is_new:
                cur.execute(
                    """
                    INSERT INTO registry_guardrails
                    (display_name, description, enabled, mode, rule_type,
                     severity, config, prompt_text, agent_prompt, project_id, sort_order,
                     account_id, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """,
                    (
                        data.get("display_name", UNNAMED_RULE_VALUE),
                        data.get("description", ""),
                        data.get("enabled", True),
                        data["mode"],
                        data["rule_type"],
                        data.get("severity", "error"),
                        json.dumps(data.get("config", {})),
                        data.get("prompt_text", ""),
                        data.get("agent_prompt", ""),
                        data.get("project_id"),
                        data.get("sort_order", 0),
                        normalized_account_id,
                        normalized_actor_id,
                        normalized_actor_id,
                    ),
                )
                gid = cur.fetchone()["id"]
            else:
                cur.execute(
                    """
                    UPDATE registry_guardrails SET
                        display_name = %s, description = %s, enabled = %s,
                        mode = %s, rule_type = %s, severity = %s,
                        config = %s, prompt_text = %s, agent_prompt = %s,
                        project_id = %s, sort_order = %s, updated_by = %s
                    WHERE id::text = %s AND account_id::text = %s
                """,
                    (
                        data.get("display_name", UNNAMED_RULE_VALUE),
                        data.get("description", ""),
                        data.get("enabled", True),
                        data["mode"],
                        data["rule_type"],
                        data.get("severity", "error"),
                        json.dumps(data.get("config", {})),
                        data.get("prompt_text", ""),
                        data.get("agent_prompt", ""),
                        data.get("project_id"),
                        data.get("sort_order", 0),
                        normalized_actor_id,
                        str(gid),
                        normalized_account_id,
                    ),
                )
                if int(cur.rowcount or 0) == 0:
                    cur.execute(
                        "SELECT 1 FROM registry_guardrails WHERE id::text = %s",
                        (str(gid),),
                    )
                    if cur.fetchone():
                        raise KeyError(f"Guardrail {gid} not found")
                    cur.execute(
                        """
                        INSERT INTO registry_guardrails
                        (id, display_name, description, enabled, mode, rule_type,
                         severity, config, prompt_text, agent_prompt, project_id, sort_order,
                         account_id, created_by, updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """,
                        (
                            gid,
                            data.get("display_name", UNNAMED_RULE_VALUE),
                            data.get("description", ""),
                            data.get("enabled", True),
                            data["mode"],
                            data["rule_type"],
                            data.get("severity", "error"),
                            json.dumps(data.get("config", {})),
                            data.get("prompt_text", ""),
                            data.get("agent_prompt", ""),
                            data.get("project_id"),
                            data.get("sort_order", 0),
                            normalized_account_id,
                            normalized_actor_id,
                            normalized_actor_id,
                        ),
                    )
                    gid = cur.fetchone()["id"]
                    created = True

            # Sync agent scope
            cur.execute(
                "DELETE FROM registry_guardrail_agents WHERE guardrail_id::text = %s",
                (str(gid),),
            )
            for aid in agents:
                cur.execute(
                    "INSERT INTO registry_guardrail_agents (guardrail_id, agent_id) VALUES (%s, %s)",
                    (gid, aid),
                )

            self._log_change_cur(
                cur,
                "guardrail",
                str(gid),
                "create" if created else "update",
                data,
                author,
                account_id=normalized_account_id,
                actor_id=normalized_actor_id,
                actor_type=normalized_actor_type,
            )

        if not str(gid or "").strip():
            raise ValueError("Failed to persist guardrail id")
        return str(gid)

    def delete_guardrail(
        self,
        guardrail_id: str,
        author: str = "admin",
        *,
        account_id: str,
        actor_id: str,
        actor_type: str,
    ) -> bool:
        """Delete a guardrail rule."""
        self._require_guardrail_ownership_schema()
        snapshot = self.get_guardrail(guardrail_id, account_id=account_id)
        if not snapshot:
            return False
        has_deleted_at = self._column_exists("registry_guardrails", "deleted_at")
        has_deleted_by = self._column_exists("registry_guardrails", "deleted_by")
        with self._transaction() as cur:
            if has_deleted_at and has_deleted_by:
                cur.execute(
                    "UPDATE registry_guardrails SET deleted_at = NOW(), deleted_by = %s "
                    "WHERE id::text = %s AND account_id::text = %s "
                    "AND deleted_at IS NULL RETURNING id",
                    (actor_id, guardrail_id, account_id),
                )
            elif has_deleted_at:
                cur.execute(
                    "UPDATE registry_guardrails SET deleted_at = NOW() "
                    "WHERE id::text = %s AND account_id::text = %s "
                    "AND deleted_at IS NULL RETURNING id",
                    (guardrail_id, account_id),
                )
            else:
                cur.execute(
                    "DELETE FROM registry_guardrails WHERE id::text = %s "
                    "AND account_id::text = %s RETURNING id",
                    (guardrail_id, account_id),
                )
            deleted = cur.fetchone()
            if deleted:
                self._log_change_cur(
                    cur,
                    "guardrail",
                    str(guardrail_id),
                    "delete",
                    snapshot,
                    author,
                    account_id=account_id,
                    actor_id=actor_id,
                    actor_type=actor_type,
                )
                return True
            return False

    def toggle_guardrail(
        self,
        guardrail_id: str,
        *,
        account_id: str,
        actor_id: str,
        actor_type: str,
    ) -> bool | None:
        """Toggle enabled/disabled. Returns new state or None if not found."""
        self._require_guardrail_ownership_schema()
        snapshot = self.get_guardrail(guardrail_id, account_id=account_id)
        if not snapshot:
            return None
        has_deleted_at = self._column_exists("registry_guardrails", "deleted_at")
        with self._transaction() as cur:
            current_query = (
                "SELECT * FROM registry_guardrails "
                "WHERE id::text = %s AND account_id::text = %s "
                "AND deleted_at IS NULL FOR UPDATE"
                if has_deleted_at
                else "SELECT * FROM registry_guardrails "
                "WHERE id::text = %s AND account_id::text = %s FOR UPDATE"
            )
            cur.execute(
                current_query,
                (guardrail_id, account_id),
            )
            current = cur.fetchone()
            if not current:
                return None
            snapshot = {**snapshot, **current}
            if not current["enabled"]:
                from .guardrail_config_validation import validate_deterministic_guardrail_config

                validate_deterministic_guardrail_config(
                    enabled=True,
                    mode=current["mode"],
                    rule_type=current["rule_type"],
                    config=current["config"],
                )
            if has_deleted_at:
                cur.execute(
                    "UPDATE registry_guardrails SET enabled = NOT enabled, updated_by = %s "
                    "WHERE id::text = %s AND account_id::text = %s "
                    "AND deleted_at IS NULL RETURNING enabled",
                    (actor_id, guardrail_id, account_id),
                )
            else:
                cur.execute(
                    "UPDATE registry_guardrails SET enabled = NOT enabled, updated_by = %s "
                    "WHERE id::text = %s AND account_id::text = %s RETURNING enabled",
                    (actor_id, guardrail_id, account_id),
                )
            row = cur.fetchone()
            if not row:
                return None
            enabled = bool(row["enabled"])
            self._log_change_cur(
                cur,
                "guardrail",
                str(guardrail_id),
                "toggle",
                {**snapshot, "enabled": enabled},
                actor_id,
                account_id=account_id,
                actor_id=actor_id,
                actor_type=actor_type,
            )
            return enabled

    def set_guardrail_agents(
        self,
        guardrail_id: str,
        agent_ids: list[str],
        *,
        account_id: str,
        actor_id: str,
        actor_type: str,
    ) -> None:
        """Replace the agent scope for a guardrail."""
        self._require_guardrail_ownership_schema()
        normalized_agent_ids = list(
            dict.fromkeys(str(agent_id or "").strip() for agent_id in (agent_ids or []))
        )
        normalized_agent_ids = [agent_id for agent_id in normalized_agent_ids if agent_id]
        if not normalized_agent_ids:
            raise ValueError("guardrail agents must contain at least one agent_id")

        current = self.get_guardrail(guardrail_id, account_id=account_id)
        if not current:
            raise KeyError(f"Guardrail {guardrail_id} not found")
        with self._transaction() as cur:
            cur.execute(
                """SELECT agent_id FROM registry_agents
                    WHERE agent_id = ANY(%s) AND account_id::text = %s""",
                (normalized_agent_ids, account_id),
            )
            owned_agent_ids = {str(row["agent_id"]) for row in cur.fetchall()}
            missing_agent_ids = sorted(set(normalized_agent_ids) - owned_agent_ids)
            if missing_agent_ids:
                raise ValueError(
                    "guardrail agents are not available in the owning account: "
                    + ", ".join(missing_agent_ids)
                )
            cur.execute(
                """DELETE FROM registry_guardrail_agents ga
                    USING registry_guardrails g
                    WHERE ga.guardrail_id = g.id
                      AND ga.guardrail_id::text = %s
                      AND g.account_id::text = %s""",
                (str(guardrail_id), account_id),
            )
            for aid in normalized_agent_ids:
                cur.execute(
                    """INSERT INTO registry_guardrail_agents (guardrail_id, agent_id)
                       SELECT id, %s FROM registry_guardrails
                        WHERE id::text = %s AND account_id::text = %s""",
                    (aid, guardrail_id, account_id),
                )
            cur.execute(
                """UPDATE registry_guardrails SET updated_by = %s
                    WHERE id::text = %s AND account_id::text = %s""",
                (actor_id, guardrail_id, account_id),
            )
            self._log_change_cur(
                cur,
                "guardrail",
                str(guardrail_id),
                "update_agents",
                {**current, "_agents": normalized_agent_ids},
                actor_id,
                account_id=account_id,
                actor_id=actor_id,
                actor_type=actor_type,
            )


__all__ = [
    "RegistryDBStoreGuardrailsMixin",
]
