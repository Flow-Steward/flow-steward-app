"""Agent command service: the canonical owner of agent registry mutations.

Admin HTTP controllers only parse requests, call this service and map
``ServiceError`` subclasses to HTTP status codes; MCP and CLI adapters reuse it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.management.command_models import AgentMutationResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.registry.catalog_categories import known_catalog_category
from core.application.registry.contract_validation_entities import (
    validate_agent_readiness,
    validate_agent_skill_compatibility,
)
from core.application.registry.db_store_common import (
    RegistryCreateConflictError,
    RestoreValidationError,
)
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import (
    guardrail_assignable_catalog_rows,
    projected_catalog_row_matches,
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)

from .command_services_agent_relationships_mixin import AgentRelationshipCommandsMixin
from .command_services_agent_rules import (
    contract_validation_error,
    enforce_enabled_save_validation,
    normalize_agent_payload,
    reject_standard_tool_actions_for_agent,
    require_create_account_scope,
    require_disable_allowed,
    require_row_in_account_scope,
    row_in_account_scope,
    validate_agent_context_template,
    validate_agent_provider_model,
)
from .command_services_common import _service_scope
from .command_services_tool import _tool_row_in_scope
from .registry_name_conflicts import (
    effective_display_name,
    id_conflict_error,
    require_free_display_name,
    require_free_id,
)

_VIOLATES_FOREIGN_KEY = "violates foreign key"


def _agent_not_found(agent_id: str) -> str:
    return f"Agent '{agent_id}' not found"


def _default_agent_validation(db: Any, agent_id: str, **kwargs: Any) -> dict:
    return validate_agent_readiness(db, agent_id, **kwargs)


class AgentCommandService(AgentRelationshipCommandsMixin):
    """Own agent create/update/enable/delete/clone/restore, assignments and readiness.

    Persistence stays in the registry store; this service owns validation,
    account scope, dependency checks, transactions and registry reloads.
    """

    def __init__(
        self,
        *,
        get_db: Callable[[], Any],
        reload_registry: Callable[[], None],
        get_agent_validation: Callable[..., dict] | None = None,
        validate_skill_compatibility: Callable[[Any, str, str], dict] | None = None,
    ):
        self._get_db = get_db
        self._reload_registry = reload_registry
        self._get_agent_validation = get_agent_validation or _default_agent_validation
        self._skill_compatibility_validator = validate_skill_compatibility or (
            lambda db, agent_id, skill_id: validate_agent_skill_compatibility(
                db, agent_id, skill_id
            )
        )

    def _db(self) -> Any:
        return self._get_db()

    def _relationship_validator(self, db: Any, agent_id: str) -> dict:
        """Readiness callback for assignment and restore paths (no account argument)."""
        return self._get_agent_validation(db, agent_id)

    @staticmethod
    def _require_agent(db: Any, agent_id: str, scope: ScopeContext) -> dict[str, Any]:
        """Load a durable agent the caller may see, else a non-enumerating 404."""
        agent = db.get_agent(agent_id)
        if not agent:
            raise ServiceNotFoundError(_agent_not_found(agent_id))
        return require_row_in_account_scope(agent, scope, detail=_agent_not_found(agent_id))

    @staticmethod
    def _check_enabled_transition(
        db: Any, agent_id: str, *, current_enabled: bool, target_enabled: bool
    ) -> None:
        """The single enable/disable rule shared by ``PATCH {enabled}`` and ``toggle``."""
        if current_enabled and not target_enabled:
            require_disable_allowed(
                db,
                agent_ids=[agent_id],
                message=lambda workflows: (
                    f"Agent '{agent_id}' cannot be disabled while enabled workflows "
                    f"depend on it: {workflows}. Disable or update those "
                    "workflows first."
                ),
            )

    def _taken_agent_ids(self, db: Any) -> list[str]:
        """Every stored agent id, Trash included, for a suggested free id."""
        return [
            str(row.get("agent_id") or "")
            for row in db.list_agents(include_trashed=True) or []
            if isinstance(row, Mapping)
        ]

    @staticmethod
    def _persisted_enabled(db: Any, agent_id: str) -> bool | None:
        """The enabled state the store now holds; a mutation result reports it, not null."""
        row = db.get_agent(agent_id)
        return bool(row.get("enabled", False)) if isinstance(row, Mapping) else None

    def _save_agent(
        self,
        db: Any,
        data: dict[str, Any],
        *,
        agent_id: str,
        scope: ScopeContext,
        sync_relationships: bool = True,
        create: bool = False,
        previous: Mapping[str, Any] | None = None,
    ) -> dict:
        """Save under one transaction with the display-name check.

        *create* inserts without ever touching an existing row (a taken id is a
        conflict); *previous* is the stored agent, whose unchanged name is never
        refused.
        """
        try:
            with db._transaction():
                require_free_display_name(
                    db,
                    entity_type="agent",
                    display_name=effective_display_name(data, agent_id),
                    scope=scope,
                    entity_id=agent_id,
                    current_display_name=(
                        None if previous is None else effective_display_name(previous, agent_id)
                    ),
                )
                if create:
                    db.save_agent(data, author=scope.actor_name, create_only=True)
                elif sync_relationships:
                    db.save_agent(data, author=scope.actor_name)
                else:
                    db.save_agent(data, author=scope.actor_name, sync_relationships=False)
                validation = self._get_agent_validation(db, agent_id, account_id=scope.account_id)
                enforce_enabled_save_validation(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="save",
                    validation=validation,
                    final_enabled=bool(data.get("enabled", False)),
                )
        except RegistryCreateConflictError as exc:
            raise id_conflict_error(
                db,
                entity_type="agent",
                entity_id=agent_id,
                scope=scope,
                taken_ids=lambda: self._taken_agent_ids(db),
            ) from exc
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return validation

    # ------------------------------------------------------------------ #
    # Reads with projections and readiness                                #
    # ------------------------------------------------------------------ #

    def list_agents(
        self,
        *,
        scope: ScopeContext,
        enabled_only: bool = False,
        search: str | None = None,
        usage: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self._db()
        rows = projection_aware_catalog_rows(
            db,
            "agent",
            "agent_id",
            db.list_agents(enabled_only=enabled_only) or [],
            account_id=scope.account_id,
            enabled_only=enabled_only,
        )
        if search:
            canonical_by_id = {
                str(row.get("agent_id") or "").strip(): row
                for row in rows
                if isinstance(row, dict) and str(row.get("agent_id") or "").strip()
            }
            rows = [
                canonical_by_id[agent_id]
                for hit in (db.search_entities(search, entity_type="agent") or [])
                if isinstance(hit, dict)
                and (agent_id := str(hit.get("agent_id") or hit.get("entity_id") or "").strip())
                in canonical_by_id
            ]
            rows.extend(
                row
                for agent_id, row in canonical_by_id.items()
                if agent_id not in {str(item.get("agent_id") or "").strip() for item in rows}
                and projected_catalog_row_matches(
                    row,
                    search,
                    fields=("agent_id", "display_name", "description", "category"),
                )
            )
        rows = [row for row in (rows or []) if row_in_account_scope(row, scope)]
        if usage == "guardrail":
            return guardrail_assignable_catalog_rows(db, rows, account_id=scope.account_id)
        return rows

    def _projected_agent(self, db: Any, agent_id: str, scope: ScopeContext) -> dict[str, Any]:
        agent = projection_aware_catalog_row(
            db,
            "agent",
            "agent_id",
            agent_id,
            db.get_agent(agent_id),
            account_id=scope.account_id,
        )
        if not agent:
            raise ServiceNotFoundError(_agent_not_found(agent_id))
        return require_row_in_account_scope(agent, scope, detail=_agent_not_found(agent_id))

    def get_agent(self, agent_id: str, *, scope: ScopeContext) -> dict[str, Any]:
        """One agent (durable or extension-projected) with its skills and tools."""
        return self._projected_agent(self._db(), agent_id, scope)

    def validate_agent(self, agent_id: str, *, scope: ScopeContext) -> dict[str, Any]:
        """Readiness of one agent for enable/test/invoke."""
        db = self._db()
        self._projected_agent(db, agent_id, scope)
        validation = self._get_agent_validation(db, agent_id, account_id=scope.account_id)
        return {"agent_id": agent_id, "validation": validation}

    # ------------------------------------------------------------------ #
    # Create / replace / patch / enable / delete                          #
    # ------------------------------------------------------------------ #

    def create_agent(
        self, payload: Mapping[str, Any], *, scope: ScopeContext
    ) -> AgentMutationResultView:
        """Create an agent under exactly the requested id; a taken id is a conflict."""
        db = self._db()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            raise ServiceValidationError("agent_id is required")
        require_free_id(
            db,
            entity_type="agent",
            entity_id=agent_id,
            scope=scope,
            exists=lambda candidate: db.get_agent(candidate) is not None,
            taken_ids=lambda: self._taken_agent_ids(db),
        )
        data = dict(payload)
        data["agent_id"] = agent_id
        require_create_account_scope(data, scope)
        # Ownership is transport context, not client-controlled request data.
        data["account_id"] = scope.account_id
        reject_standard_tool_actions_for_agent(data.get("tools"))
        data = normalize_agent_payload(data)
        validate_agent_context_template(data.get("prompts"))
        validate_agent_provider_model(data, account_id=scope.account_id, conn=db.conn)
        validation = self._save_agent(db, data, agent_id=agent_id, scope=scope, create=True)
        self._reload_registry()
        return AgentMutationResultView(
            status="created",
            agent_id=agent_id,
            enabled=self._persisted_enabled(db, agent_id),
            validation=validation,
        )

    def update_agent(
        self, agent_id: str, payload: Mapping[str, Any], *, scope: ScopeContext
    ) -> AgentMutationResultView:
        """Full replacement of an agent (all fields)."""
        db = self._db()
        existing = db.get_agent(agent_id)
        if existing:
            require_row_in_account_scope(existing, scope, detail=_agent_not_found(agent_id))
        data = dict(payload)
        require_create_account_scope(data, scope)
        # A full replacement must not silently move an account-owned agent to the
        # registry store's default account when the payload omits ownership fields.
        data["account_id"] = (
            existing.get("account_id") if existing is not None else scope.account_id
        )
        reject_standard_tool_actions_for_agent(data.get("tools"))
        data = normalize_agent_payload(data)
        data["agent_id"] = agent_id
        validate_agent_context_template(data.get("prompts"))
        validate_agent_provider_model(data, account_id=scope.account_id, conn=db.conn)
        validation = self._save_agent(db, data, agent_id=agent_id, scope=scope, previous=existing)
        self._reload_registry()
        return AgentMutationResultView(
            status="updated",
            agent_id=agent_id,
            enabled=self._persisted_enabled(db, agent_id),
            validation=validation,
        )

    def patch_agent(
        self, agent_id: str, updates: Mapping[str, Any], *, scope: ScopeContext
    ) -> AgentMutationResultView:
        """Merge only the provided fields into the existing agent.

        ``updates`` holds the explicitly provided, non-null fields. ``config`` and
        ``prompts`` merge shallowly; an empty ``provider``/``model`` removes the
        route field so the agent inherits the project route.
        """
        db = self._db()
        existing = self._require_agent(db, agent_id, scope)
        updates = dict(updates)
        if not updates:
            return AgentMutationResultView(
                status="no_change",
                agent_id=agent_id,
                enabled=bool(existing.get("enabled", False)),
            )

        self._check_enabled_transition(
            db,
            agent_id,
            current_enabled=bool(existing.get("enabled", False)),
            target_enabled=updates.get("enabled") is not False,
        )

        merged = {**existing, **updates}
        merged["agent_id"] = agent_id

        # get_agent returns list-of-dicts but save_agent expects list-of-IDs.
        if merged.get("skills") and isinstance(merged["skills"][0], dict):
            merged["skills"] = [c["skill_id"] for c in merged["skills"] if isinstance(c, dict)]
        if merged.get("tools") and isinstance(merged["tools"][0], dict):
            merged["tools"] = [t["tool_id"] for t in merged["tools"] if isinstance(t, dict)]

        # Merge config (shallow) instead of replacing — preserves fields like
        # action_tools, hard_cap_multiplier that the Config tab doesn't send.
        if "config" in updates and "config" in existing:
            merged["config"] = {**(existing.get("config") or {}), **updates["config"]}
            # Explicit empty route fields mean "inherit the project route". Only
            # remove fields present in this patch so unrelated config is preserved.
            for route_field in ("provider", "model"):
                if (
                    route_field in updates["config"]
                    and not str(updates["config"].get(route_field) or "").strip()
                ):
                    merged["config"].pop(route_field, None)

        # Merge prompts (shallow) — preserves examples, context sections and
        # skill prompts on partial updates.
        if "prompts" in updates and "prompts" in existing:
            merged["prompts"] = {**(existing.get("prompts") or {}), **updates["prompts"]}
        if "local_instructions" in updates:
            merged_prompts = dict(merged.get("prompts") or {})
            merged_prompts["local_instructions"] = list(updates.get("local_instructions") or [])
            merged["prompts"] = merged_prompts
        merged = normalize_agent_payload(merged)
        validate_agent_context_template(merged.get("prompts"))
        validate_agent_provider_model(merged, account_id=scope.account_id, conn=db.conn)

        validation = self._save_agent(
            db,
            merged,
            agent_id=agent_id,
            scope=scope,
            sync_relationships=False,
            previous=existing,
        )
        self._reload_registry()
        return AgentMutationResultView(
            status="updated",
            agent_id=agent_id,
            enabled=self._persisted_enabled(db, agent_id),
            validation=validation,
        )

    def toggle_agent(self, agent_id: str, *, scope: ScopeContext) -> AgentMutationResultView:
        """Set ``enabled`` to the opposite of the current state.

        Uses the same enabled-transition rule as ``PATCH {"enabled": ...}``;
        enabling additionally requires a valid readiness result.
        """
        db = self._db()
        current = self._require_agent(db, agent_id, scope)
        current_enabled = bool(current.get("enabled", False))
        target_enabled = not current_enabled
        self._check_enabled_transition(
            db, agent_id, current_enabled=current_enabled, target_enabled=target_enabled
        )
        if target_enabled:
            validation = self._get_agent_validation(db, agent_id, account_id=scope.account_id)
            if not validation.get("valid", False):
                raise contract_validation_error(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="enable",
                    validation=validation,
                )
        try:
            new_state = db.toggle_agent(agent_id)
        except Exception as exc:
            raise ServiceNotFoundError(_agent_not_found(agent_id)) from exc
        self._reload_registry()
        return AgentMutationResultView(status="toggled", agent_id=agent_id, enabled=new_state)

    def delete_agent(self, agent_id: str, *, scope: ScopeContext) -> AgentMutationResultView:
        """Delete an agent. Fails while workflow steps reference it (FK RESTRICT)."""
        db = self._db()
        existing = db.get_agent(agent_id)
        if existing:
            require_row_in_account_scope(existing, scope, detail=_agent_not_found(agent_id))
        try:
            db.delete_agent(agent_id, author=scope.actor_name)
        except Exception as exc:
            if isinstance(exc, KeyError):
                raise ServiceNotFoundError(str(exc.args[0] if exc.args else exc)) from exc
            if _VIOLATES_FOREIGN_KEY in str(exc):
                raise ServiceConflictError(
                    f"Agent '{agent_id}' is referenced by workflow steps. "
                    "Remove from workflows first."
                ) from exc
            raise ServiceError(str(exc), status_code=500) from exc
        self._reload_registry()
        return AgentMutationResultView(status="deleted", agent_id=agent_id)

    def restore_agent_revision(
        self, agent_id: str, entry_id: str | int, *, scope: ScopeContext
    ) -> AgentMutationResultView:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        try:
            outcome = db.restore_entity_revision(
                entity_type="agent",
                entity_id=agent_id,
                entry_id=str(entry_id),
                author=f"{scope.actor_name}_restore",
                validate_restored=lambda _entity_type, _entity_id, _restored: (
                    self._relationship_validator(db, agent_id)
                ),
            )
        except RestoreValidationError as exc:
            validation = (
                exc.validation
                if isinstance(exc.validation, dict)
                else {"valid": False, "errors": [], "warnings": []}
            )
            raise contract_validation_error(
                entity_type="agent", entity_id=agent_id, action="restore", validation=validation
            ) from exc
        except KeyError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        validation = outcome.get("validation")
        if not isinstance(validation, dict):
            validation = self._relationship_validator(db, agent_id)
        self._reload_registry()
        return AgentMutationResultView(
            status="restored",
            agent_id=agent_id,
            restored_from_entry_id=str(entry_id),
            validation=validation,
            snapshot=outcome.get("restored"),
        )

    # ------------------------------------------------------------------ #
    # Clone                                                               #
    # ------------------------------------------------------------------ #

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
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved = _service_scope(scope)
        db = self._db()
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
            "category": known_catalog_category(source.get("category")),
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


__all__ = ["AgentCommandService"]
