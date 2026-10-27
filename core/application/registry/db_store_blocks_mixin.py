# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .db_store_common import *  # NOSONAR


class RegistryDBStoreBlocksMixin:
    def get_field(self, field_key: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM registry_fields WHERE field_key = %s",
                (field_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_fields(
        self,
        enabled_only: bool = False,
        value_type: str | None = None,
        search: str | None = None,
        *,
        ensure_projection: bool = True,
    ) -> list[dict]:
        if ensure_projection:
            self._ensure_public_contract_projection()
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append(SQL_ENABLED_TRUE)
        if value_type:
            clauses.append("value_type = %s")
            params.append(str(value_type).strip())
        if search:
            needle = f"%{str(search).strip()}%"
            clauses.append("(field_key ILIKE %s OR display_name ILIKE %s)")
            params.extend([needle, needle])
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""SELECT *
                    FROM registry_fields
                    {where_sql}
                    ORDER BY field_key""",
                tuple(params),
            )
            return [dict(r) for r in cur.fetchall()]

    def save_field(self, data: dict, author: str = "admin") -> None:
        field_key = str(data.get("field_key") or "").strip()
        if not field_key:
            raise ValueError("field_key is required")
        is_new = not self._exists("registry_fields", "field_key", field_key)
        existing = self.get_field(field_key) if not is_new else {}
        existing_map: dict[str, Any] = existing if isinstance(existing, dict) else {}
        last_sync_error = data.get("last_sync_error", existing_map.get("last_sync_error"))
        snapshot = {
            "field_key": field_key,
            "display_name": str(data.get("display_name") or field_key).strip(),
            "description": str(data.get("description") or "").strip(),
            "value_type": str(data.get("value_type") or "text").strip() or "text",
            "data_contract_id": str(data.get("data_contract_id") or "").strip() or None,
            "created_from_kind": str(data.get("created_from_kind") or "manual").strip() or "manual",
            "created_from_ref": str(data.get("created_from_ref") or "").strip(),
            "source_hints_json": data.get("source_hints_json") or data.get("source_hints") or {},
            "manual_allowed": bool(data.get("manual_allowed", True)),
            "enabled": bool(data.get("enabled", True)),
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
            if snapshot["data_contract_id"]:
                cur.execute(
                    "SELECT contract_id FROM registry_data_contracts "
                    "WHERE contract_id = %s FOR KEY SHARE",
                    (snapshot["data_contract_id"],),
                )
                if cur.fetchone() is None:
                    raise ValueError("Referenced data contract does not exist")
            cur.execute(
                """INSERT INTO registry_fields
                   (field_key, display_name, description, value_type, data_contract_id,
                    created_from_kind, created_from_ref, source_hints_json, manual_allowed, enabled,
                    projection_mode, derived_from_kind, derived_from_ref, derived_hash,
                    sync_status, last_synced_at, last_sync_error)
                   VALUES
                   (%(field_key)s, %(display_name)s, %(description)s, %(value_type)s, %(data_contract_id)s,
                    %(created_from_kind)s, %(created_from_ref)s, %(source_hints_json)s, %(manual_allowed)s, %(enabled)s,
                    %(projection_mode)s, %(derived_from_kind)s, %(derived_from_ref)s, %(derived_hash)s,
                    %(sync_status)s, %(last_synced_at)s, %(last_sync_error)s)
                   ON CONFLICT (field_key) DO UPDATE SET
                      display_name = EXCLUDED.display_name,
                      description = EXCLUDED.description,
                      value_type = EXCLUDED.value_type,
                      data_contract_id = EXCLUDED.data_contract_id,
                      created_from_kind = EXCLUDED.created_from_kind,
                      created_from_ref = EXCLUDED.created_from_ref,
                      source_hints_json = EXCLUDED.source_hints_json,
                      manual_allowed = EXCLUDED.manual_allowed,
                      enabled = EXCLUDED.enabled,
                      projection_mode = EXCLUDED.projection_mode,
                      derived_from_kind = EXCLUDED.derived_from_kind,
                      derived_from_ref = EXCLUDED.derived_from_ref,
                      derived_hash = EXCLUDED.derived_hash,
                      sync_status = EXCLUDED.sync_status,
                      last_synced_at = EXCLUDED.last_synced_at,
                      last_sync_error = EXCLUDED.last_sync_error""",
                {
                    **snapshot,
                    "source_hints_json": json.dumps(snapshot["source_hints_json"]),
                },
            )
            self._log_change_cur(
                cur,
                "field",
                field_key,
                "create" if is_new else "update",
                snapshot,
                author,
            )

    def toggle_field(self, field_key: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE registry_fields SET enabled = NOT enabled WHERE field_key = %s RETURNING enabled",
                (field_key,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Field '{field_key}' not found")
            return bool(row["enabled"])

    def delete_field(self, field_key: str, author: str = "admin") -> None:
        snapshot = self.get_field(field_key)
        if not snapshot:
            raise KeyError(f"Field '{field_key}' not found")
        with self._transaction() as cur:
            cur.execute("DELETE FROM registry_fields WHERE field_key = %s", (field_key,))
            self._log_change_cur(cur, "field", field_key, "delete", snapshot, author)

    def _field_id_for_key_cur(self, cur, field_key: str) -> str | None:
        normalized = str(field_key or "").strip()
        if not normalized:
            return None
        cur.execute("SELECT field_id FROM registry_fields WHERE field_key = %s", (normalized,))
        row = cur.fetchone()
        if not row:
            return None
        return str(row["field_id"]).strip()

    def _block_inputs_cur(self, cur, block_key: str) -> list[dict]:
        cur.execute(
            """SELECT bi.id, bi.block_key, bi.field_id, bi.required, bi.multiple_allowed,
                      bi.manual_allowed_override, bi.default_source_hint, bi.notes, bi.sort_order,
                      f.field_key, f.display_name AS field_display_name, f.description AS field_description,
                      f.value_type, f.data_contract_id
               FROM registry_published_block_inputs bi
               JOIN registry_fields f ON f.field_id = bi.field_id
               WHERE bi.block_key = %s
               ORDER BY bi.sort_order, bi.id""",
            (block_key,),
        )
        return [dict(r) for r in cur.fetchall()]

    def _block_outputs_cur(self, cur, block_key: str) -> list[dict]:
        cur.execute(
            """SELECT bo.id, bo.block_key, bo.field_id, bo.required_by_default, bo.exposed_by_default,
                      bo.notes, bo.sort_order,
                      f.field_key, f.display_name AS field_display_name, f.description AS field_description,
                      f.value_type, f.data_contract_id
               FROM registry_published_block_outputs bo
               JOIN registry_fields f ON f.field_id = bo.field_id
               WHERE bo.block_key = %s
               ORDER BY bo.sort_order, bo.id""",
            (block_key,),
        )
        return [dict(r) for r in cur.fetchall()]

    def _published_block_source_binding_cur(self, cur, block_key: str) -> tuple[str, str] | None:
        cur.execute(
            """SELECT source_kind, source_ref
               FROM registry_published_block_contracts
               WHERE block_key = %s""",
            (block_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (
            str(row.get("source_kind") or "").strip(),
            str(row.get("source_ref") or "").strip(),
        )

    def _published_block_step_bindings_cur(self, cur, block_key: str) -> list[dict]:
        """Return workflow steps that reference this block via override keys."""
        cur.execute(
            """SELECT w.workflow_id, s.step_id
               FROM registry_workflow_steps s
               JOIN registry_workflows w ON w.workflow_id = s.owner_workflow_id
               WHERE COALESCE(s.override->>'published_block_key', '') = %s
                  OR COALESCE(s.override->>'block_key', '') = %s
               ORDER BY w.workflow_id, s.sort_order, s.step_id""",
            (block_key, block_key),
        )
        return [dict(r) for r in cur.fetchall()]

    def _validate_published_block_source_ref_cur(
        self,
        cur,
        *,
        source_kind: str,
        source_ref: str,
        block_key: str = "",
    ) -> None:
        kind = str(source_kind or "").strip().lower()
        ref = str(source_ref or "").strip()
        if not kind or not ref:
            raise ValueError("source_kind and source_ref are required")

        if kind == "agent":
            if "::" in ref:
                agent_id, skill_id = ref.split("::", 1)
                agent_id = str(agent_id or "").strip()
                skill_id = str(skill_id or "").strip()
                if not agent_id or not skill_id:
                    raise ValueError("agent source_ref must be 'agent_id::skill_id'")
                cur.execute("SELECT 1 AS ok FROM registry_agents WHERE agent_id = %s", (agent_id,))
                if not cur.fetchone():
                    raise ValueError(f"source_ref references unknown agent '{agent_id}'")
                cur.execute(
                    "SELECT 1 AS ok FROM registry_skills WHERE skill_id = %s",
                    (skill_id,),
                )
                if not cur.fetchone():
                    raise ValueError(f"source_ref references unknown skill '{skill_id}'")
                cur.execute(
                    """SELECT 1 AS ok
                       FROM registry_agent_skills
                       WHERE agent_id = %s AND skill_id = %s""",
                    (agent_id, skill_id),
                )
                if not cur.fetchone():
                    raise ValueError(
                        f"source_ref '{ref}' is invalid: agent '{agent_id}' is not mapped to skill '{skill_id}'"
                    )
                return

            # Transitional forms: either agent_id or skill_id must resolve.
            cur.execute("SELECT 1 AS ok FROM registry_agents WHERE agent_id = %s", (ref,))
            if cur.fetchone():
                return
            cur.execute("SELECT 1 AS ok FROM registry_skills WHERE skill_id = %s", (ref,))
            if cur.fetchone():
                return
            raise ValueError(
                f"source_ref '{ref}' does not reference an existing agent_id or skill_id"
            )

        if kind == "workflow":
            cur.execute("SELECT 1 AS ok FROM registry_workflows WHERE workflow_id = %s", (ref,))
            if cur.fetchone():
                return
            raise ValueError(f"source_ref references unknown workflow '{ref}'")

        if kind == "connector":
            cur.execute("SELECT 1 AS ok FROM registry_tools WHERE tool_id = %s", (ref,))
            if cur.fetchone():
                return
            raise ValueError(f"source_ref references unknown connector/tool '{ref}'")

        raise ValueError(f"Unsupported source_kind '{kind}' for block '{block_key}'")

    def get_published_block_contract(
        self, block_key: str, *, account_id: str | None = None
    ) -> dict | None:
        """Fetch a published-block contract by key.

        When ``account_id`` is provided, an account-owned block (a row with a
        non-empty ``account_id``) is only returned to a caller in that same
        account; platform-global blocks (NULL ``account_id``, ``scope_kind`` =
        'system') remain visible to everyone. Callers that already hold the
        request scope should pass it so cross-account block contracts are not
        resolved. Fails closed: a mismatched owner yields ``None``.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM registry_published_block_contracts WHERE block_key = %s",
                (block_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            contract = dict(row)
            scope_account_id = str(account_id or "").strip()
            if scope_account_id:
                row_account_id = str(contract.get("account_id") or "").strip()
                if row_account_id and row_account_id != scope_account_id:
                    return None
            contract["_inputs"] = self._block_inputs_cur(cur, block_key)
            contract["inputs"] = contract["_inputs"]
            contract["_outputs"] = self._block_outputs_cur(cur, block_key)
            contract["outputs"] = contract["_outputs"]
            return contract

    def list_published_block_contracts(
        self,
        enabled_only: bool = False,
        block_type: str | None = None,
        source_kind: str | None = None,
        *,
        ensure_projection: bool = True,
    ) -> list[dict]:
        if ensure_projection:
            self._ensure_public_contract_projection()
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append(SQL_ENABLED_TRUE)
        if block_type:
            clauses.append("block_type = %s")
            params.append(str(block_type).strip())
        if source_kind:
            clauses.append("source_kind = %s")
            params.append(str(source_kind).strip())
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""SELECT *
                    FROM registry_published_block_contracts
                    {where_sql}
                    ORDER BY block_key""",
                tuple(params),
            )
            return [dict(r) for r in cur.fetchall()]

    def save_published_block_contract(self, data: dict, author: str = "admin") -> None:
        block_key = str(data.get("block_key") or "").strip()
        if not block_key:
            raise ValueError("block_key is required")
        is_new = not self._exists("registry_published_block_contracts", "block_key", block_key)
        existing = self.get_published_block_contract(block_key) if not is_new else {}
        existing_map: dict[str, Any] = existing if isinstance(existing, dict) else {}
        last_sync_error = data.get("last_sync_error", existing_map.get("last_sync_error"))
        scope_payload = dict(existing_map)
        scope_payload.update(data)
        source_install_id = str(scope_payload.get("source_install_id") or "").strip()

        snapshot = {
            "block_key": block_key,
            "display_name": str(data.get("display_name") or block_key).strip(),
            "block_type": str(data.get("block_type") or "agent").strip() or "agent",
            "source_kind": str(data.get("source_kind") or "").strip(),
            "source_ref": str(data.get("source_ref") or "").strip(),
            "description": str(data.get("description") or "").strip(),
            "family": str(data.get("family") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
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
        snapshot.update(
            {
                "account_id": _system_registry_account_id(
                    scope_payload,
                    source_install_id=source_install_id,
                ),
                "scope_kind": str(scope_payload.get("scope_kind") or "system").strip() or "system",
                "source_install_id": source_install_id or None,
                "source_release_id": str(scope_payload.get("source_release_id") or "").strip()
                or None,
                "source_projection_id": scope_payload.get("source_projection_id"),
                "local_block_key": str(
                    scope_payload.get("local_block_key")
                    or existing_map.get("local_block_key")
                    or ""
                ).strip(),
                "availability_status": str(
                    scope_payload.get("availability_status") or "available"
                ).strip()
                or "available",
                "availability_reason_code": str(
                    scope_payload.get("availability_reason_code") or "none"
                ).strip()
                or "none",
                "availability_reason_text": str(
                    scope_payload.get("availability_reason_text") or ""
                ).strip(),
                "new_runs_blocked": bool(scope_payload.get("new_runs_blocked", False)),
                "visible_in_catalog": bool(scope_payload.get("visible_in_catalog", True)),
            }
        )
        if snapshot["projection_mode"] == "derived" and snapshot["last_synced_at"] is None:
            snapshot["last_synced_at"] = datetime.now(UTC)
        if not snapshot["source_kind"] or not snapshot["source_ref"]:
            raise ValueError("source_kind and source_ref are required")

        raw_inputs = list(data.get("inputs") or [])
        raw_outputs = list(data.get("outputs") or [])

        with self._transaction() as cur:
            existing_binding = self._published_block_source_binding_cur(cur, block_key)
            if existing_binding:
                prev_source_kind, prev_source_ref = existing_binding
                source_binding_changed = (
                    prev_source_kind != snapshot["source_kind"]
                    or prev_source_ref != snapshot["source_ref"]
                )
                if source_binding_changed:
                    step_bindings = self._published_block_step_bindings_cur(cur, block_key)
                    if step_bindings:
                        preview = ", ".join(
                            f"{str(item.get('workflow_id') or '').strip()}#"
                            f"{str(item.get('step_id') or '').strip()}"
                            for item in step_bindings[:3]
                        )
                        if len(step_bindings) > 3:
                            preview = f"{preview}, +{len(step_bindings) - 3} more"
                        raise ValueError(
                            f"Cannot change source_ref/source_kind for published block '{block_key}' "
                            f"while it is referenced by workflow steps ({len(step_bindings)}): {preview}. "
                            "Create a new block_key instead."
                        )
            skip_source_ref_validation = snapshot["projection_mode"] == "derived" and snapshot[
                "sync_status"
            ] in {"orphaned", "stale"}
            if not skip_source_ref_validation:
                self._validate_published_block_source_ref_cur(
                    cur,
                    source_kind=str(snapshot["source_kind"]),
                    source_ref=str(snapshot["source_ref"]),
                    block_key=block_key,
                )
            columns = [
                "block_key",
                "display_name",
                "block_type",
                "source_kind",
                "source_ref",
                "description",
                "family",
                "enabled",
                "projection_mode",
                "derived_from_kind",
                "derived_from_ref",
                "derived_hash",
                "sync_status",
                "last_synced_at",
                "last_sync_error",
            ]
            updates = [
                DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
                "block_type = EXCLUDED.block_type",
                "source_kind = EXCLUDED.source_kind",
                "source_ref = EXCLUDED.source_ref",
                DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
                "family = EXCLUDED.family",
                ENABLED_EXCLUDED_ENABLED_SQL,
                "projection_mode = EXCLUDED.projection_mode",
                "derived_from_kind = EXCLUDED.derived_from_kind",
                "derived_from_ref = EXCLUDED.derived_from_ref",
                "derived_hash = EXCLUDED.derived_hash",
                "sync_status = EXCLUDED.sync_status",
                "last_synced_at = EXCLUDED.last_synced_at",
                "last_sync_error = EXCLUDED.last_sync_error",
            ]
            optional_columns = [
                ("account_id", snapshot.get("account_id")),
                ("scope_kind", snapshot.get("scope_kind")),
                ("source_install_id", snapshot.get("source_install_id")),
                ("source_release_id", snapshot.get("source_release_id")),
                ("source_projection_id", snapshot.get("source_projection_id")),
                ("local_block_key", snapshot.get("local_block_key")),
                ("availability_status", snapshot.get("availability_status")),
                ("availability_reason_code", snapshot.get("availability_reason_code")),
                ("availability_reason_text", snapshot.get("availability_reason_text")),
                ("new_runs_blocked", snapshot.get("new_runs_blocked")),
                ("visible_in_catalog", snapshot.get("visible_in_catalog")),
            ]
            for column_name, _column_value in optional_columns:
                if not self._column_exists("registry_published_block_contracts", column_name):
                    continue
                columns.append(column_name)
                updates.append(f"{column_name} = EXCLUDED.{column_name}")
            cur.execute(
                f"""INSERT INTO registry_published_block_contracts ({", ".join(columns)})
                   VALUES ({", ".join(f"%({column})s" for column in columns)})
                   ON CONFLICT (block_key) DO UPDATE SET
                      {", ".join(updates)}""",
                snapshot,
            )

            normalized_inputs: list[dict] = []
            seen_input_ids: set[str] = set()
            for idx, item in enumerate(raw_inputs):
                if not isinstance(item, dict):
                    continue
                field_key = str(item.get("field_key") or "").strip()
                field_id = str(item.get("field_id") or "").strip() or None
                resolved_field_id = (
                    self._field_id_for_key_cur(cur, field_key) if field_key else None
                )
                if (
                    field_id is not None
                    and resolved_field_id is not None
                    and field_id != resolved_field_id
                ):
                    raise ValueError(
                        f"inputs[{idx}] field_id '{field_id}' does not match field_key '{field_key}'"
                    )
                if resolved_field_id is not None:
                    field_id = resolved_field_id
                elif field_id is not None:
                    cur.execute(
                        "SELECT 1 AS ok FROM registry_fields WHERE field_id::text = %s", (field_id,)
                    )
                    if not cur.fetchone():
                        raise ValueError(f"inputs[{idx}] references unknown field_id '{field_id}'")
                if field_id is None:
                    if not field_key:
                        raise ValueError(
                            f"inputs[{idx}] references unknown field_key '{field_key}'"
                        )
                    field_id = self._field_id_for_key_cur(cur, field_key)
                    if field_id is None:
                        raise ValueError(
                            f"inputs[{idx}] references unknown field_key '{field_key}'"
                        )
                if field_id in seen_input_ids:
                    raise ValueError(
                        f"inputs[{idx}] duplicates field_id '{field_id}' in block '{block_key}'"
                    )
                seen_input_ids.add(field_id)
                normalized_inputs.append(
                    {
                        "field_id": field_id,
                        "required": bool(item.get("required", False)),
                        "multiple_allowed": bool(item.get("multiple_allowed", False)),
                        "manual_allowed_override": item.get("manual_allowed_override"),
                        "default_source_hint": str(item.get("default_source_hint") or "").strip(),
                        "notes": str(item.get("notes") or "").strip(),
                        "sort_order": int(item.get("sort_order", idx)),
                    }
                )

            normalized_outputs: list[dict] = []
            seen_output_ids: set[str] = set()
            for idx, item in enumerate(raw_outputs):
                if not isinstance(item, dict):
                    continue
                field_key = str(item.get("field_key") or "").strip()
                field_id = str(item.get("field_id") or "").strip() or None
                resolved_field_id = (
                    self._field_id_for_key_cur(cur, field_key) if field_key else None
                )
                if (
                    field_id is not None
                    and resolved_field_id is not None
                    and field_id != resolved_field_id
                ):
                    raise ValueError(
                        f"outputs[{idx}] field_id '{field_id}' does not match field_key '{field_key}'"
                    )
                if resolved_field_id is not None:
                    field_id = resolved_field_id
                elif field_id is not None:
                    cur.execute(
                        "SELECT 1 AS ok FROM registry_fields WHERE field_id::text = %s", (field_id,)
                    )
                    if not cur.fetchone():
                        raise ValueError(f"outputs[{idx}] references unknown field_id '{field_id}'")
                if field_id is None:
                    if not field_key:
                        raise ValueError(
                            f"outputs[{idx}] references unknown field_key '{field_key}'"
                        )
                    field_id = self._field_id_for_key_cur(cur, field_key)
                    if field_id is None:
                        raise ValueError(
                            f"outputs[{idx}] references unknown field_key '{field_key}'"
                        )
                if field_id in seen_output_ids:
                    raise ValueError(
                        f"outputs[{idx}] duplicates field_id '{field_id}' in block '{block_key}'"
                    )
                seen_output_ids.add(field_id)
                normalized_outputs.append(
                    {
                        "field_id": field_id,
                        "required_by_default": bool(item.get("required_by_default", False)),
                        "exposed_by_default": bool(item.get("exposed_by_default", True)),
                        "notes": str(item.get("notes") or "").strip(),
                        "sort_order": int(item.get("sort_order", idx)),
                    }
                )

            cur.execute(
                "DELETE FROM registry_published_block_inputs WHERE block_key = %s",
                (block_key,),
            )
            for row in normalized_inputs:
                cur.execute(
                    """INSERT INTO registry_published_block_inputs
                       (block_key, field_id, required, multiple_allowed, manual_allowed_override,
                        default_source_hint, notes, sort_order)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (block_key, field_id) DO UPDATE SET
                          required = EXCLUDED.required,
                          multiple_allowed = EXCLUDED.multiple_allowed,
                          manual_allowed_override = EXCLUDED.manual_allowed_override,
                          default_source_hint = EXCLUDED.default_source_hint,
                          notes = EXCLUDED.notes,
                          sort_order = EXCLUDED.sort_order""",
                    (
                        block_key,
                        row["field_id"],
                        row["required"],
                        row["multiple_allowed"],
                        row["manual_allowed_override"],
                        row["default_source_hint"],
                        row["notes"],
                        row["sort_order"],
                    ),
                )

            cur.execute(
                "DELETE FROM registry_published_block_outputs WHERE block_key = %s",
                (block_key,),
            )
            for row in normalized_outputs:
                cur.execute(
                    """INSERT INTO registry_published_block_outputs
                       (block_key, field_id, required_by_default, exposed_by_default, notes, sort_order)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (block_key, field_id) DO UPDATE SET
                          required_by_default = EXCLUDED.required_by_default,
                          exposed_by_default = EXCLUDED.exposed_by_default,
                          notes = EXCLUDED.notes,
                          sort_order = EXCLUDED.sort_order""",
                    (
                        block_key,
                        row["field_id"],
                        row["required_by_default"],
                        row["exposed_by_default"],
                        row["notes"],
                        row["sort_order"],
                    ),
                )

            self._log_change_cur(
                cur,
                "published_block",
                block_key,
                "create" if is_new else "update",
                {
                    **snapshot,
                    "inputs": normalized_inputs,
                    "outputs": normalized_outputs,
                },
                author,
            )

    def toggle_published_block_contract(self, block_key: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE registry_published_block_contracts
                   SET enabled = NOT enabled
                   WHERE block_key = %s
                   RETURNING enabled""",
                (block_key,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Published block '{block_key}' not found")
            return bool(row["enabled"])

    def delete_published_block_contract(self, block_key: str, author: str = "admin") -> None:
        snapshot = self.get_published_block_contract(block_key)
        if not snapshot:
            raise KeyError(f"Published block '{block_key}' not found")
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM registry_published_block_contracts WHERE block_key = %s",
                (block_key,),
            )
            self._log_change_cur(cur, "published_block", block_key, "delete", snapshot, author)


__all__ = [
    "RegistryDBStoreBlocksMixin",
]
