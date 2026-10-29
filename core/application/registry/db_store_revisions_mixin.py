# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.registry.catalog_categories import known_catalog_category
from core.application.workflows import workflow_history_snapshot

from .db_store_common import *  # NOSONAR
from .db_store_workflows_mixin import WorkflowRevisionConflictError

WorkflowHistoryComparisonError = workflow_history_snapshot.WorkflowHistoryComparisonError

_SQL_ENTITY_ID = "entity_id = %s"
_SQL_ACCOUNT_ID = "account_id::text = %s"
#: A cross-entity changelog read leaves out the revisions of a hook's storage
#: workflow (``settings.hook_storage``): a place inside another workflow, never a
#: workflow a person browses. A revision snapshot carries the marker, and so does
#: the live row (a revision written before the marker needs the second test).
_SQL_NOT_HOOK_STORAGE_REVISION = (
    "NOT (entity_type = 'workflow' AND ("
    "COALESCE(snapshot->'settings', '{}'::jsonb) ? 'hook_storage' "
    "OR entity_id IN (SELECT hook_rw.workflow_id FROM registry_workflows hook_rw "
    "WHERE hook_rw.settings ? 'hook_storage')))"
)
#: The same marker, as a filter on a registry workflow row of a name search.
_SQL_NOT_HOOK_STORAGE_WORKFLOW = "NOT (settings ? 'hook_storage')"


class RegistryDBStoreRevisionsMixin:
    def _log_change_cur(
        self,
        cur,
        entity_type: str,
        entity_id: str,
        action: str,
        snapshot: dict,
        author: str,
        *,
        account_id: str | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ) -> None:
        """Write changelog entry within an existing cursor/transaction."""
        if account_id is not None or actor_id is not None or actor_type is not None:
            if not all(str(value or "").strip() for value in (account_id, actor_id, actor_type)):
                raise ValueError("account_id, actor_id, and actor_type must be supplied together")
            cur.execute(
                """INSERT INTO registry_changelog
                   (entity_type, entity_id, action, snapshot, author,
                    account_id, actor_id, actor_type)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    entity_type,
                    entity_id,
                    action,
                    json.dumps(snapshot, default=str),
                    author,
                    account_id,
                    actor_id,
                    actor_type,
                ),
            )
            return
        cur.execute(
            """INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot, author)
               VALUES (%s, %s, %s, %s, %s)""",
            (entity_type, entity_id, action, json.dumps(snapshot, default=str), author),
        )

    def log_change(
        self, entity_type: str, entity_id: str, action: str, snapshot: dict, author: str = "admin"
    ) -> None:
        with self._cursor() as cur:
            self._log_change_cur(cur, entity_type, entity_id, action, snapshot, author)

    @staticmethod
    def _to_snapshot_dict(snapshot: Any) -> dict:
        if isinstance(snapshot, dict):
            return dict(snapshot)
        if isinstance(snapshot, str):
            try:
                parsed = json.loads(snapshot)
            except Exception:
                return {}
            if isinstance(parsed, dict):
                return dict(parsed)
        return {}

    @staticmethod
    def _history_revision_kind(entry: dict) -> str:
        action = str(entry.get("action") or "").strip().lower()
        return "lifecycle" if action in LIFECYCLE_PRESENTATION_ACTIONS else "content"

    def _project_history_entry(self, row: dict) -> dict:
        entry = dict(row)
        entry["revision_kind"] = self._history_revision_kind(entry)
        if str(entry.get("entity_type") or "").strip() == "workflow":
            # Restore rules read the definition validator; only history reads need them.
            from core.application.workflows import workflow_history

            snapshot = self._to_snapshot_dict(entry.get("snapshot"))
            # Replay policy follows the stored markers, never the presentation:
            # a Delete event shows as lifecycle but keeps its full snapshot.
            marker = str(entry.get("action") or "").strip().lower() in LIFECYCLE_CHANGELOG_ACTIONS
            reason = workflow_history.restore_unavailable_reason(snapshot, lifecycle=marker)
            lifecycle = entry["revision_kind"] == "lifecycle"
            entry["revision"] = None if lifecycle else workflow_history.history_revision(snapshot)
            entry["snapshot_kind"] = workflow_history.snapshot_kind(snapshot)
            entry["restorable"] = reason is None
            entry["restore_unavailable_reason"] = reason
        return entry

    @staticmethod
    def _normalize_agent_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["agent_id"] = entity_id

        skills = payload.get("skills")
        if not isinstance(skills, list):
            skills = payload.get("_skills")
        normalized_caps: list[str] = []
        cap_prompts: dict[str, str] = {}
        for item in skills or []:
            if isinstance(item, dict):
                skill_id = str(item.get("skill_id") or "").strip()
                skill_prompt = str(item.get("skill_prompt") or "")
            else:
                skill_id = str(item or "").strip()
                skill_prompt = ""
            if skill_id:
                normalized_caps.append(skill_id)
                if skill_prompt:
                    cap_prompts[skill_id] = skill_prompt
        payload["skills"] = normalized_caps

        tools = payload.get("tools")
        if not isinstance(tools, list):
            tools = payload.get("_tools")
        normalized_tools: list[str] = []
        for item in tools or []:
            if isinstance(item, dict):
                tool_id = str(item.get("tool_id") or "").strip()
            else:
                tool_id = str(item or "").strip()
            if tool_id:
                normalized_tools.append(tool_id)
        payload["tools"] = normalized_tools

        prompts = payload.get("prompts")
        if not isinstance(prompts, dict):
            prompts = {}
        if cap_prompts:
            existing = (
                prompts.get("skill_prompts")
                if isinstance(prompts.get("skill_prompts"), dict)
                else {}
            )
            prompts["skill_prompts"] = {**existing, **cap_prompts}
        payload["prompts"] = prompts

        for key in (
            "id",
            "created_at",
            "updated_at",
            "_skills",
            "_tools",
            "_inputs",
            "_outputs",
        ):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_tool_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["tool_id"] = entity_id
        for key in ("id", "created_at", "updated_at"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_skill_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["skill_id"] = entity_id
        if payload.get("expected_inputs") is None and isinstance(payload.get("_inputs"), list):
            payload["expected_inputs"] = payload.get("_inputs") or []
        if payload.get("expected_outputs") is None and isinstance(payload.get("_outputs"), list):
            payload["expected_outputs"] = payload.get("_outputs") or []
        for key in ("id", "created_at", "updated_at", "_inputs", "_outputs", "_tools"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_workflow_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["workflow_id"] = entity_id
        for key in ("id", "created_at", "updated_at"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_guardrail_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["id"] = str(entity_id or "").strip()
        if not payload["id"]:
            raise ValueError("guardrail id is required")
        if payload.get("agents") is None and isinstance(payload.get("_agents"), list):
            payload["agents"] = payload.get("_agents") or []
        for key in ("created_at", "updated_at", "_agents"):
            payload.pop(key, None)
        return payload

    def _normalize_restore_payload(self, entity_type: str, entity_id: str, snapshot: dict) -> dict:
        if entity_type in {"agent", "tool", "skill"}:
            # A revision saved before categories came from the shared list
            # restores under the default category rather than failing.
            snapshot = {**snapshot, "category": known_catalog_category(snapshot.get("category"))}
        if entity_type == "agent":
            return self._normalize_agent_restore_payload(entity_id, snapshot)
        if entity_type == "tool":
            return self._normalize_tool_restore_payload(entity_id, snapshot)
        if entity_type == "skill":
            return self._normalize_skill_restore_payload(entity_id, snapshot)
        if entity_type == "workflow":
            return self._normalize_workflow_restore_payload(entity_id, snapshot)
        if entity_type == "guardrail":
            return self._normalize_guardrail_restore_payload(entity_id, snapshot)
        raise ValueError(f"Unsupported entity type '{entity_type}'")

    def _get_entity_snapshot(self, entity_type: str, entity_id: str) -> dict | None:
        if entity_type == "agent":
            return self.get_agent(entity_id)
        if entity_type == "tool":
            return self.get_tool(entity_id)
        if entity_type == "skill":
            return self.get_skill(entity_id)
        if entity_type == "workflow":
            return self.get_workflow(entity_id)
        if entity_type == "guardrail":
            return self.get_guardrail(entity_id, account_id=DEFAULT_ACCOUNT_ID)
        return None

    def apply_entity_snapshot(
        self,
        entity_type: str,
        entity_id: str,
        snapshot: dict,
        author: str = "admin",
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        actor_id: str = "system",
        actor_type: str = "system",
        expected_revision: int | None = None,
    ) -> dict:
        if entity_type not in SUPPORTED_CHANGELOG_ENTITY_TYPES:
            raise ValueError(f"Unsupported entity type '{entity_type}'")
        normalized = self._normalize_restore_payload(
            entity_type, entity_id, self._to_snapshot_dict(snapshot)
        )
        if not normalized:
            raise ValueError("Revision snapshot is empty or invalid")
        if entity_type == "agent":
            self.save_agent(normalized, author=author)
            return self.get_agent(entity_id) or normalized
        if entity_type == "tool":
            normalized["account_id"] = str(account_id or DEFAULT_ACCOUNT_ID)
            self.save_tool(normalized, author=author)
            return self.get_tool(entity_id) or normalized
        if entity_type == "skill":
            self.save_skill(normalized, author=author)
            return self.get_skill(entity_id) or normalized
        if entity_type == "workflow":
            self.save_workflow(normalized, author=author, expected_revision=expected_revision)
            return self.get_workflow(entity_id) or normalized
        if entity_type == "guardrail":
            self.save_guardrail(
                normalized,
                author=author,
                account_id=account_id,
                actor_id=actor_id,
                actor_type=actor_type,
            )
            return self.get_guardrail(entity_id, account_id=account_id) or normalized
        raise ValueError(f"Unsupported entity type '{entity_type}'")

    def set_entity_builder_metadata(
        self,
        entity_type: str,
        entity_id: str,
        builder_metadata: dict[str, Any],
        *,
        author: str = "admin",
        only_if_empty: bool = False,
    ) -> bool:
        if entity_type not in {"agent", "tool", "skill", "workflow"}:
            raise ValueError(f"Unsupported builder metadata entity type '{entity_type}'")
        normalized_entity_id = str(entity_id or "").strip()
        if not normalized_entity_id:
            return False
        normalized_metadata = normalize_builder_metadata(builder_metadata)
        if not str(normalized_metadata.get("purpose") or "").strip():
            return False

        current_snapshot = self._get_entity_snapshot(entity_type, normalized_entity_id)
        if not isinstance(current_snapshot, dict) or not current_snapshot:
            return False
        current_metadata = normalize_builder_metadata(
            current_snapshot.get("builder_metadata_jsonb")
        )
        if only_if_empty and str(current_metadata.get("purpose") or "").strip():
            return False
        if current_metadata == normalized_metadata:
            return False
        if entity_type == "workflow":
            return self._save_workflow_builder_metadata(
                current_snapshot, normalized_metadata, author=author
            )

        table_name, id_column = {
            "agent": ("registry_agents", "agent_id"),
            "tool": ("registry_tools", "tool_id"),
            "skill": ("registry_skills", "skill_id"),
            "workflow": ("registry_workflows", "workflow_id"),
        }[entity_type]
        with self._transaction() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL(
                    """
                    UPDATE {table_name}
                       SET builder_metadata_jsonb = %s::jsonb,
                           updated_at = NOW()
                     WHERE {id_column} = %s
                    """
                ).format(
                    table_name=psycopg_runtime.sql.Identifier(table_name),
                    id_column=psycopg_runtime.sql.Identifier(id_column),
                ),
                (json.dumps(normalized_metadata), normalized_entity_id),
            )
            updated_snapshot = dict(current_snapshot)
            updated_snapshot["builder_metadata_jsonb"] = normalized_metadata
            self._log_change_cur(
                cur,
                entity_type,
                normalized_entity_id,
                "update",
                updated_snapshot,
                author,
            )
        return True

    def _save_workflow_builder_metadata(
        self, workflow: dict, builder_metadata: dict[str, Any], *, author: str
    ) -> bool:
        """Builder metadata is workflow content: one new revision with its full snapshot.

        The write is guarded by the revision just read, so an author's save in
        between wins and this backfill skips the workflow.
        """
        payload = dict(workflow, builder_metadata_jsonb=builder_metadata)
        try:
            self.save_workflow(
                payload, author=author, expected_revision=int(workflow.get("revision") or 1)
            )
        except WorkflowRevisionConflictError:
            return False
        return True

    def get_changelog_entry(
        self,
        entry_id: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        account_id: str | None = None,
    ) -> dict | None:
        with self._cursor() as cur:
            clauses = ["id::text = %s"]
            params: list[Any] = [str(entry_id)]
            if entity_type:
                clauses.append("entity_type = %s")
                params.append(entity_type)
            if entity_id:
                clauses.append(_SQL_ENTITY_ID)
                params.append(entity_id)
            if account_id:
                clauses.append(_SQL_ACCOUNT_ID)
                params.append(account_id)
            where = SQL_AND.join(clauses)
            cur.execute(f"SELECT * FROM registry_changelog WHERE {where} LIMIT 1", params)
            row = cur.fetchone()
            if not row:
                return None
            entry = self._project_history_entry(row)
            entry["snapshot"] = self._to_snapshot_dict(entry.get("snapshot"))
            return entry

    def get_entity_history(
        self,
        entity_type: str,
        entity_id: str,
        limit: int = 50,
        *,
        account_id: str | None = None,
    ) -> list[dict]:
        if entity_type not in SUPPORTED_CHANGELOG_ENTITY_TYPES:
            raise ValueError(f"Unsupported entity type '{entity_type}'")
        rows = self.get_changelog(
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
            account_id=account_id,
        )
        history: list[dict] = []
        for row in rows:
            entry = self._project_history_entry(row)
            entry["snapshot"] = self._to_snapshot_dict(entry.get("snapshot"))
            history.append(entry)
        return history

    def get_entity_revision(
        self,
        entity_type: str,
        entity_id: str,
        revision: int,
        *,
        account_id: str | None = None,
    ) -> dict | None:
        if revision < 1:
            return None
        if entity_type == "workflow":
            return self._get_workflow_revision_entry(entity_id, revision, account_id=account_id)
        history = self.get_entity_history(
            entity_type=entity_type,
            entity_id=entity_id,
            limit=max(200, revision),
            account_id=account_id,
        )
        if revision > len(history):
            return None
        return history[revision - 1]

    def _get_workflow_revision_entry(
        self, workflow_id: str, revision: int, *, account_id: str | None = None
    ) -> dict | None:
        """The entry that recorded *revision*, by the number it stored, not by position.

        Only entries written with a trusted number can match, so cleaning up
        older entries never moves a number onto another version.
        """
        clauses = [
            "entity_type = 'workflow'",
            _SQL_ENTITY_ID,
            "snapshot->>%s = %s",
            "snapshot->>'revision' = %s",
        ]
        params: list[Any] = [
            workflow_id,
            workflow_history_snapshot.HISTORY_SCHEMA_KEY,
            str(workflow_history_snapshot.HISTORY_SCHEMA_VERSION),
            str(int(revision)),
        ]
        if account_id:
            clauses.append(_SQL_ACCOUNT_ID)
            params.append(account_id)
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM registry_changelog WHERE "  # nosec B608 -- fixed clauses; values are bound.
                + SQL_AND.join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT 1",
                params,
            )
            row = cur.fetchone()
        if not row:
            return None
        entry = self._project_history_entry(row)
        entry["snapshot"] = self._to_snapshot_dict(entry.get("snapshot"))
        return entry

    @staticmethod
    def _flatten_snapshot(value: Any, prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(value, dict):
            for key in sorted(value.keys()):
                raw_child = value.get(key)
                child_key = str(key)
                child_path = f"{prefix}.{child_key}" if prefix else child_key
                out.update(RegistryDBStoreRevisionsMixin._flatten_snapshot(raw_child, child_path))
            if not value and prefix:
                out[prefix] = {}
            return out
        if isinstance(value, list):
            if not value and prefix:
                out[prefix] = []
                return out
            for idx, item in enumerate(value):
                child_path = f"{prefix}[{idx}]"
                out.update(RegistryDBStoreRevisionsMixin._flatten_snapshot(item, child_path))
            return out
        if prefix:
            out[prefix] = value
        return out

    @staticmethod
    def _jsonable(value: Any) -> Any:
        try:
            json.dumps(value, default=str)
            return value
        except Exception:
            return str(value)

    def _diff_snapshots(self, older: dict, newer: dict) -> list[dict]:
        old_flat = self._flatten_snapshot(self._to_snapshot_dict(older))
        new_flat = self._flatten_snapshot(self._to_snapshot_dict(newer))
        paths = sorted(set(old_flat.keys()) | set(new_flat.keys()))
        changes: list[dict] = []
        for path in paths:
            in_old = path in old_flat
            in_new = path in new_flat
            old_value = self._jsonable(old_flat.get(path))
            new_value = self._jsonable(new_flat.get(path))
            if in_old and not in_new:
                changes.append({"path": path, "change": "removed", "from": old_value, "to": None})
                continue
            if in_new and not in_old:
                changes.append({"path": path, "change": "added", "from": None, "to": new_value})
                continue
            if old_value != new_value:
                changes.append(
                    {"path": path, "change": "changed", "from": old_value, "to": new_value}
                )
        return changes

    @staticmethod
    def _diff_workflow_snapshots(
        older: dict | None, older_snapshot: dict, newer_snapshot: dict
    ) -> list[dict]:
        """Workflow definitions compared node by node, or refused when one side has none."""
        from core.application.workflows import workflow_history

        sides = [newer_snapshot] if older is None else [older_snapshot, newer_snapshot]
        for snapshot in sides:
            reason = workflow_history.comparison_unavailable_reason(snapshot)
            if reason:
                raise WorkflowHistoryComparisonError(reason)
        if older is None:
            return []
        return workflow_history.diff_workflow_snapshots(older_snapshot, newer_snapshot)

    def diff_entity_history(
        self,
        entity_type: str,
        entity_id: str,
        *,
        from_entry_id: str | None = None,
        to_entry_id: str | None = None,
        account_id: str | None = None,
    ) -> dict:
        history = (
            []
            if to_entry_id is not None and from_entry_id is not None
            else self.get_entity_history(
                entity_type=entity_type,
                entity_id=entity_id,
                limit=2,
                account_id=account_id,
            )
        )

        def _entry(entry_id: str) -> dict:
            # A named entry is read directly, so its age never hides it.
            entry = self.get_changelog_entry(
                str(entry_id).strip(),
                entity_type=entity_type,
                entity_id=entity_id,
                account_id=account_id,
            )
            if not entry:
                raise KeyError(f"Unknown revision id '{entry_id}'")
            return entry

        if to_entry_id is not None:
            newer = _entry(to_entry_id)
        elif history:
            newer = history[0]
        else:
            raise KeyError(f"No history for {entity_type}:{entity_id}")

        if from_entry_id is not None:
            older = _entry(from_entry_id)
        else:
            older = history[1] if len(history) > 1 else None

        lifecycle_comparison = any(
            self._history_revision_kind(entry) == "lifecycle"
            for entry in (older, newer)
            if entry is not None
        )
        result = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "from_entry_id": str((older or {}).get("id") or "").strip() or None,
            "to_entry_id": str((newer or {}).get("id") or "").strip() or None,
            "from_created_at": (older or {}).get("created_at"),
            "to_created_at": (newer or {}).get("created_at"),
            "comparison_kind": "lifecycle" if lifecycle_comparison else "content",
        }
        if lifecycle_comparison:
            # Audit markers are not full content snapshots. Comparing their
            # fields would invent content removals during a lifecycle transition.
            return {**result, "changes": [], "summary": {}}
        older_snapshot = self._to_snapshot_dict((older or {}).get("snapshot"))
        newer_snapshot = self._to_snapshot_dict((newer or {}).get("snapshot"))
        if entity_type == "workflow":
            changes = self._diff_workflow_snapshots(older, older_snapshot, newer_snapshot)
        else:
            changes = self._diff_snapshots(older_snapshot, newer_snapshot)
        return {
            **result,
            "changes": changes,
            "summary": {
                "total": len(changes),
                "added": sum(1 for item in changes if item.get("change") == "added"),
                "removed": sum(1 for item in changes if item.get("change") == "removed"),
                "changed": sum(1 for item in changes if item.get("change") == "changed"),
            },
        }

    def restore_entity_revision(
        self,
        entity_type: str,
        entity_id: str,
        *,
        entry_id: str,
        author: str = "admin",
        validate_restored=None,
        account_id: str | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
    ) -> dict:
        if entity_type == "workflow":
            # A workflow is judged and written by its definition service, never here.
            raise ValueError("A workflow revision is restored through restore_workflow_snapshot")
        entry = self.get_changelog_entry(
            str(entry_id),
            entity_type=entity_type,
            entity_id=entity_id,
            account_id=account_id,
        )
        if not entry:
            raise KeyError(f"Revision '{entry_id}' was not found for {entity_type}:{entity_id}")
        if str(entry.get("action") or "").strip().lower() in LIFECYCLE_CHANGELOG_ACTIONS:
            raise ValueError(
                "Selected entry records a lifecycle transition, not a content revision, "
                "and cannot be restored"
            )
        snapshot = self._to_snapshot_dict(entry.get("snapshot"))
        if not snapshot:
            raise ValueError("Selected revision does not contain a restorable snapshot")
        validation = None
        try:
            with self._transaction():
                if entity_type == "guardrail":
                    normalized = self._normalize_guardrail_restore_payload(entity_id, snapshot)
                    self.save_guardrail(
                        normalized,
                        author=author,
                        account_id=str(account_id or ""),
                        actor_id=str(actor_id or ""),
                        actor_type=str(actor_type or ""),
                    )
                    restored = self.get_guardrail(entity_id, account_id=account_id) or normalized
                else:
                    restored = self.apply_entity_snapshot(
                        entity_type,
                        entity_id,
                        snapshot,
                        author=author,
                        account_id=str(account_id or DEFAULT_ACCOUNT_ID),
                    )
                if callable(validate_restored):
                    validation = validate_restored(entity_type, entity_id, restored)
                    if isinstance(validation, dict) and not validation.get("valid", False):
                        raise RestoreValidationError(
                            f"Restored {entity_type}:{entity_id} failed validation",
                            validation=validation,
                        )
        except Exception as e:
            if isinstance(e, RestoreValidationError):
                raise
            raise ValueError(f"Failed to apply revision snapshot: {e}") from e
        return {
            "entry": entry,
            "restored": restored,
            "validation": validation,
        }

    def get_changelog(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
        *,
        account_id: str | None = None,
    ) -> list[dict]:
        with self._cursor() as cur:
            clauses = []
            params: list[Any] = []
            if entity_type:
                clauses.append("entity_type = %s")
                params.append(entity_type)
            if entity_id:
                clauses.append(_SQL_ENTITY_ID)
                params.append(entity_id)
            elif entity_type in (None, "", "workflow"):
                clauses.append(_SQL_NOT_HOOK_STORAGE_REVISION)
            if account_id:
                clauses.append(_SQL_ACCOUNT_ID)
                params.append(account_id)
            where = "WHERE " + SQL_AND.join(clauses) if clauses else ""
            params.append(limit)
            cur.execute(
                f"SELECT * FROM registry_changelog {where} ORDER BY created_at DESC, id DESC LIMIT %s",
                params,
            )
            return [self._project_history_entry(r) for r in cur.fetchall()]

    def search_entities(self, query: str, entity_type: str | None = None) -> list[dict]:
        """Search across all entity types by name/description."""
        results = []
        pattern = f"%{query}%"
        tables = {
            "agent": ("registry_agents", "agent_id", "display_name", "description"),
            "tool": ("registry_tools", "tool_id", "display_name", "description"),
            "skill": ("registry_skills", "skill_id", "display_name", "description"),
            "workflow": ("registry_workflows", "workflow_id", "display_name", "description"),
        }
        if entity_type:
            tables = {entity_type: tables[entity_type]}
        with self._cursor() as cur:
            for etype, (table, id_col, name_col, desc_col) in tables.items():
                clauses = [
                    f"({id_col} ILIKE %s OR {name_col} ILIKE %s OR COALESCE({desc_col}, '') ILIKE %s)"
                ]
                if self._column_exists(table, "deleted_at"):
                    clauses.append(SQL_DELETED_AT_IS_NULL)
                if etype == "workflow":
                    clauses.append(_SQL_NOT_HOOK_STORAGE_WORKFLOW)
                where_sql = SQL_AND.join(clauses)
                account_scope_select = (
                    ", account_id" if self._column_exists(table, "account_id") else ""
                )
                cur.execute(
                    f"""SELECT {id_col} AS entity_id, {name_col} AS display_name,
                               {desc_col} AS description, enabled{account_scope_select}
                        FROM {table}
                        WHERE {where_sql}
                        ORDER BY {id_col} LIMIT 20""",
                    (pattern, pattern, pattern),
                )
                for r in cur.fetchall():
                    results.append({"entity_type": etype, **dict(r)})
        return results

    def get_stats(self) -> dict:
        with self._cursor() as cur:
            stats = {}
            for etype, table in [
                ("agents", "registry_agents"),
                ("tools", "registry_tools"),
                ("skills", "registry_skills"),
                ("workflows", "registry_workflows"),
            ]:
                try:
                    cur.execute(
                        f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE enabled) AS enabled FROM {table}"
                    )
                    stats[etype] = dict(cur.fetchone())
                except Exception:
                    # Older or partially migrated schemas may temporarily miss a table.
                    stats[etype] = {"total": 0, "enabled": 0}
            # Guardrails stats (table may not exist yet if migration hasn't run)
            try:
                cur.execute(
                    "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE enabled) AS enabled FROM registry_guardrails"
                )
                stats["guardrails"] = dict(cur.fetchone())
            except Exception:
                stats["guardrails"] = {"total": 0, "enabled": 0}
            return stats

    def _exists(self, table: str, pk_col: str, pk_val: str) -> bool:
        with self._cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE {pk_col} = %s", (pk_val,))
            return cur.fetchone() is not None


__all__ = [
    "RegistryDBStoreRevisionsMixin",
    "WorkflowHistoryComparisonError",
]
