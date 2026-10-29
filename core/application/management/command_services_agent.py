from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.management.command_models import AgentMutationResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
)
from core.application.management.scope import ScopeContext
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import projection_aware_catalog_row

from .command_services_common import _service_scope
from .command_services_tool import _tool_row_in_scope


class AgentCommandService:
    """Own explicit agent clone policy and delegate persistence to the registry store."""

    def __init__(
        self,
        *,
        get_db: Callable[[], Any],
        reload_registry: Callable[[], None],
    ):
        self._get_db = get_db
        self._reload_registry = reload_registry

    @staticmethod
    def _row_in_scope(row: Any, scope: ScopeContext) -> bool:
        if not isinstance(row, Mapping) or row.get("deleted_at"):
            return False
        return _tool_row_in_scope(dict(row), scope)

    @staticmethod
    def _project_scoped(row: Mapping[str, Any]) -> bool:
        return bool(
            str(row.get("project_id") or "").strip()
            or str(row.get("scope_kind") or "").strip() == "project"
        )

    @classmethod
    def _account_clone_dependency_in_scope(cls, row: Any, scope: ScopeContext) -> bool:
        # Durable getters and the existing relationship transaction own liveness
        # and account checks. Install ownership stays on the referenced row;
        # referencing it does not transfer or materialize its runtime.
        return (
            cls._row_in_scope(row, scope)
            and isinstance(row, Mapping)
            and not cls._project_scoped(row)
        )

    @staticmethod
    def _ids(rows: Any, id_key: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for row in rows or []:
            value = row.get(id_key) if isinstance(row, Mapping) else row
            normalized = str(value or "").strip()
            if normalized and normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result

    @staticmethod
    def _skill_prompts(source: Mapping[str, Any], skills: list[str]) -> dict[str, Any]:
        prompts_value = source.get("prompts") or {}
        prompts = dict(prompts_value) if isinstance(prompts_value, Mapping) else {}
        prompts_value = prompts.get("skill_prompts") or {}
        source_skill_prompts = dict(prompts_value) if isinstance(prompts_value, Mapping) else {}
        skill_prompts = dict(source_skill_prompts)
        explicit_assignment_prompts: set[str] = set()

        def has_prompt_text(value: Any) -> bool:
            return bool(str(value or "").strip())

        assignments = source.get("skills")
        if assignments is None:
            assignments = source.get("_skills") or []
        for assignment in assignments or []:
            if not isinstance(assignment, Mapping):
                continue
            skill_id = str(assignment.get("skill_id") or "").strip()
            if skill_id in skills and "skill_prompt" in assignment:
                prompt = str(assignment.get("skill_prompt") or "")
                if has_prompt_text(prompt):
                    skill_prompts[skill_id] = prompt
                    explicit_assignment_prompts.add(skill_id)
                elif skill_id not in skill_prompts or not has_prompt_text(skill_prompts[skill_id]):
                    skill_prompts[skill_id] = prompt

        install_id = str(source.get("source_install_id") or "").strip()
        if install_id:
            for local_skill_id, prompt in source_skill_prompts.items():
                try:
                    projected_skill_id = projection_bridge_id(install_id, local_skill_id)
                except ValueError:
                    continue
                if projected_skill_id not in skills:
                    continue
                if projected_skill_id not in explicit_assignment_prompts:
                    canonical_prompt = source_skill_prompts.get(projected_skill_id)
                    if has_prompt_text(canonical_prompt):
                        skill_prompts[projected_skill_id] = canonical_prompt
                    elif has_prompt_text(prompt) or not has_prompt_text(
                        skill_prompts.get(projected_skill_id)
                    ):
                        skill_prompts[projected_skill_id] = prompt
                if local_skill_id != projected_skill_id:
                    skill_prompts.pop(local_skill_id, None)
        if skill_prompts:
            prompts["skill_prompts"] = skill_prompts
        return prompts

    @staticmethod
    def _resolve_clone_agent_type(db: Any, source: Mapping[str, Any], override: str | None) -> str:
        """Preserve a registered source type or require an explicit supported override."""
        list_types = getattr(db, "list_agent_types", None)
        if not callable(list_types):
            all_types: list[dict[str, Any]] = []
            enabled_types: list[dict[str, Any]] = []
        else:
            all_rows = list_types(enabled_only=False)
            enabled_rows = list_types(enabled_only=True)
            all_types = (
                [dict(row) for row in all_rows if isinstance(row, Mapping)]
                if isinstance(all_rows, list)
                else []
            )
            enabled_types = (
                [dict(row) for row in enabled_rows if isinstance(row, Mapping)]
                if isinstance(enabled_rows, list)
                else []
            )

        def find_type(value: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
            candidate = str(value or "").strip()
            if not candidate:
                return None
            return next(
                (
                    row
                    for row in rows
                    if candidate
                    in {
                        str(row.get("id") or "").strip(),
                        str(row.get("key") or row.get("agent_type_key") or "").strip(),
                    }
                ),
                None,
            )

        if override:
            selected = find_type(override, enabled_types)
            if selected is None:
                message = "Choose an enabled agent type from the existing catalog for this copy."
                raise ServiceConflictError(
                    message,
                    payload={"error": message, "code": "agent_clone_type_invalid"},
                )
        else:
            source_type = next(
                (
                    str(source.get(key) or "").strip()
                    for key in ("agent_type_key", "agent_type_id", "role_type")
                    if str(source.get(key) or "").strip()
                ),
                "",
            )
            selected = find_type(source_type, all_types)
            if selected is None:
                message = "Select an existing agent type for the cloned agent."
                raise ServiceConflictError(
                    message,
                    payload={"error": message, "code": "agent_clone_type_required"},
                )

        return str(
            selected.get("key") or selected.get("agent_type_key") or selected.get("id") or ""
        ).strip()

    @staticmethod
    def _own_io_rows(
        db: Any,
        source: Mapping[str, Any],
        *,
        getter_name: str,
        source_key: str,
        manifest_key: str,
    ) -> list[dict[str, Any]]:
        """Read authored I/O without turning inherited skill/tool rows into agent-owned rows."""
        projected = bool(str(source.get("source_install_id") or "").strip())
        if projected:
            # The install projection is the selected content for extension-owned
            # rows. It can be virtual-only or differ from a stale durable anchor;
            # even an explicit empty manifest list must win over that anchor.
            if manifest_key in source and isinstance(source.get(manifest_key), list):
                rows = source[manifest_key]
            elif source_key in source and isinstance(source.get(source_key), list):
                rows = source[source_key]
            else:
                rows = []
            return [
                dict(row)
                for row in rows
                if isinstance(row, Mapping)
                and str(row.get("source") or "own").strip().lower() in {"", "own", "agent"}
            ]

        getter = getattr(db, getter_name, None)
        if callable(getter):
            rows = getter(str(source.get("agent_id") or ""))
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
        rows = source.get(source_key)
        if not isinstance(rows, list):
            rows = source.get(manifest_key)
        return [
            dict(row)
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, Mapping)
            and str(row.get("source") or "own").strip().lower() in {"", "own", "agent"}
        ]

    def clone_agent(
        self,
        source_agent_id: str,
        *,
        target_agent_id: str | None = None,
        display_name: str | None = None,
        agent_type_id: str | None = None,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved = _service_scope(scope)
        db = self._get_db()
        source = projection_aware_catalog_row(
            db,
            "agent",
            "agent_id",
            source_agent_id,
            db.get_agent(source_agent_id),
            account_id=resolved.account_id,
        )
        if source is None or not self._row_in_scope(source, resolved):
            raise ServiceNotFoundError(f"Agent '{source_agent_id}' not found")
        if self._project_scoped(source):
            message = (
                "Project-scoped agents cannot be cloned into an account-scoped copy. "
                "Project-scoped agents cannot be cloned with the current clone operation."
            )
            raise ServiceConflictError(
                message,
                payload={
                    "error": message,
                    "code": "agent_clone_project_scope_unsupported",
                    "agent_id": source_agent_id,
                },
            )

        raw_skills = source.get("skills")
        if raw_skills is None:
            raw_skills = source.get("_skills") or []
        raw_tools = source.get("tools")
        if raw_tools is None:
            raw_tools = source.get("_tools") or []
        skill_ids = self._ids(raw_skills, "skill_id")
        tool_ids = self._ids(raw_tools, "tool_id")
        own_inputs = self._own_io_rows(
            db,
            source,
            getter_name="get_agent_own_inputs",
            source_key="_inputs",
            manifest_key="structured_inputs",
        )
        own_outputs = self._own_io_rows(
            db,
            source,
            getter_name="get_agent_own_outputs",
            source_key="_outputs",
            manifest_key="structured_outputs",
        )
        skill_dependencies: dict[str, dict[str, Any]] = {}
        for skill_id in skill_ids:
            dependency = projection_aware_catalog_row(
                db,
                "skill",
                "skill_id",
                skill_id,
                db.get_skill(skill_id),
                account_id=resolved.account_id,
            )
            if dependency is not None and self._account_clone_dependency_in_scope(
                dependency, resolved
            ):
                skill_dependencies[skill_id] = dict(dependency)
        missing_skills = [skill_id for skill_id in skill_ids if skill_id not in skill_dependencies]
        projected_skill_dependencies = [
            dependency
            for dependency in skill_dependencies.values()
            if str(dependency.get("source_install_id") or "").strip()
        ]
        missing_tools = [
            tool_id
            for tool_id in tool_ids
            if tool_id.startswith("std.")
            or not self._account_clone_dependency_in_scope(db.get_tool(tool_id), resolved)
        ]
        depends_on = source.get("depends_on") or {"soft": [], "hard": []}
        dependency_ids = self._ids(
            [*(depends_on.get("soft") or []), *(depends_on.get("hard") or [])]
            if isinstance(depends_on, Mapping)
            else [],
            "agent_id",
        )
        missing_agent_dependencies = [
            dependency_id
            for dependency_id in dependency_ids
            if not self._account_clone_dependency_in_scope(db.get_agent(dependency_id), resolved)
        ]
        if missing_skills or missing_tools or missing_agent_dependencies:
            message = (
                "This agent uses dependencies that are unavailable in the destination account scope. "
                "Make each dependency available as a saved, account-visible artifact, or create a manual agent."
            )
            raise ServiceConflictError(
                message,
                payload={
                    "error": message,
                    "code": "agent_clone_dependencies_unavailable",
                    "skill_ids": missing_skills,
                    "tool_ids": missing_tools,
                    "agent_ids": missing_agent_dependencies,
                },
            )

        resolved_agent_type_id = self._resolve_clone_agent_type(db, source, agent_type_id)

        requested_id = str(target_agent_id or f"{source_agent_id}_copy").strip()
        resolved_target_id = unique_text_key(
            requested_id,
            lambda candidate: db.get_agent(candidate) is not None,
        )
        clone_data = {
            "agent_id": resolved_target_id,
            "account_id": resolved.account_id,
            "scope_kind": "account",
            "display_name": display_name or f"{source.get('display_name', source_agent_id)} (Copy)",
            "description": source.get("description", ""),
            "agent_type_id": resolved_agent_type_id,
            "prompts": self._skill_prompts(source, skill_ids),
            "skills": skill_ids,
            "tools": tool_ids,
            "inputs": source.get("inputs") or {"required": [], "optional": []},
            "outputs": list(source.get("outputs") or []),
            "structured_inputs": own_inputs,
            "structured_outputs": own_outputs,
            "depends_on": depends_on,
            "config": dict(source.get("config") or {}),
            "quality": dict(source.get("quality") or {}),
            "builder_metadata_jsonb": dict(source.get("builder_metadata_jsonb") or {}),
            "category": source.get("category", ""),
            "enabled": False,
            "version": source.get("version", "1.0.0"),
            "tags": list(source.get("tags") or []),
        }
        try:
            # Anchor creation and the durable clone relation must commit or roll
            # back together. RegistryDBStore nested transactions reuse this outer
            # transaction, including the agent contract materialization.
            transaction = getattr(db, "_transaction", None)
            if projected_skill_dependencies and callable(transaction):
                with transaction():
                    for dependency in projected_skill_dependencies:
                        anchor_data = {
                            **dependency,
                            "source_release_id": dependency.get("source_release_id")
                            or dependency.get("release_id"),
                        }
                        db.ensure_projected_skill_anchor(
                            anchor_data, account_id=resolved.account_id
                        )
                    db.save_agent(clone_data, author=author)
            else:
                db.save_agent(clone_data, author=author)
        except Exception as exc:
            raise ServiceError("Failed to clone agent") from exc
        self._reload_registry()
        return AgentMutationResultView(
            status="cloned",
            agent_id=resolved_target_id,
            source_agent_id=source_agent_id,
        ).model_dump(mode="json")
