"""Skill command service: the canonical owner of skill registry mutations.

Admin HTTP controllers only parse requests, call this service and map
``ServiceError`` subclasses to HTTP status codes; MCP and CLI adapters reuse it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any, Literal

from core.application.management.command_models import SkillMutationResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.registry.contract_item_validation import validate_contract_items
from core.application.registry.contract_validation_entities import validate_skill_readiness
from core.application.registry.db_store_common import RestoreValidationError
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import (
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)

from .command_services_agent_rules import (
    contract_items_error,
    contract_validation_error,
    enforce_enabled_save_validation,
    require_create_account_scope,
    require_disable_allowed,
    require_row_in_account_scope,
)

SKILL_EXPECTED_INPUTS_VALUE = "skill.expected_inputs"
SKILL_EXPECTED_OUTPUTS_VALUE = "skill.expected_outputs"
_DEPENDENT_IDS_LIMIT = 25

SkillIOKind = Literal["inputs", "outputs"]


def _skill_not_found(skill_id: str) -> str:
    return f"Skill '{skill_id}' not found"


def project_skill_contract_fields(skill: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose the stored I/O rows as the ``expected_inputs``/``expected_outputs`` fields."""
    payload = dict(skill or {})
    stored_inputs = payload.get("_inputs")
    stored_outputs = payload.get("_outputs")
    if isinstance(stored_inputs, list):
        payload["expected_inputs"] = list(stored_inputs)
    if isinstance(stored_outputs, list):
        payload["expected_outputs"] = list(stored_outputs)
    return payload


def _default_skill_validation(db: Any, skill_id: str) -> dict:
    return validate_skill_readiness(db, skill_id)


class SkillCommandService:
    """Own skill create/update/enable/delete/clone/restore, I/O contracts and readiness.

    Persistence stays in the registry store; this service owns validation,
    account scope, dependency checks, transactions and registry reloads.
    """

    def __init__(
        self,
        *,
        get_db: Callable[[], Any],
        reload_registry: Callable[[], None],
        get_skill_validation: Callable[[Any, str], dict] | None = None,
    ):
        self._get_db = get_db
        self._reload_registry = reload_registry
        self._get_skill_validation = get_skill_validation or _default_skill_validation

    def _db(self) -> Any:
        return self._get_db()

    @staticmethod
    def _require_skill(db: Any, skill_id: str, scope: ScopeContext) -> dict[str, Any]:
        """Load a durable skill the caller may see, else a non-enumerating 404."""
        skill = db.get_skill(skill_id)
        if not skill:
            raise ServiceNotFoundError(_skill_not_found(skill_id))
        return require_row_in_account_scope(skill, scope, detail=_skill_not_found(skill_id))

    @staticmethod
    def _check_enabled_transition(
        db: Any, skill_id: str, *, current_enabled: bool, target_enabled: bool
    ) -> None:
        """The single enable/disable rule shared by ``PATCH {enabled}`` and ``toggle``."""
        if current_enabled and not target_enabled:
            require_disable_allowed(
                db,
                agent_ids=db.get_agents_using_skill(skill_id),
                message=lambda workflows: (
                    f"Skill '{skill_id}' cannot be disabled while enabled workflows "
                    f"depend on an agent using it: {workflows}. Disable "
                    "or update those workflows first."
                ),
            )

    @staticmethod
    def _validate_expected_io(db: Any, skill_id: str, data: Mapping[str, Any]) -> None:
        _, input_issues = validate_contract_items(
            data.get("expected_inputs") or [],
            path_prefix=SKILL_EXPECTED_INPUTS_VALUE,
            default_required=False,
            db=db,
        )
        _, output_issues = validate_contract_items(
            data.get("expected_outputs") or [],
            path_prefix=SKILL_EXPECTED_OUTPUTS_VALUE,
            default_required=True,
            db=db,
        )
        if input_issues or output_issues:
            raise contract_items_error(
                entity_type="skill",
                entity_id=skill_id,
                action="save",
                issues=input_issues + output_issues,
            )

    def _save_skill(
        self, db: Any, data: dict[str, Any], *, skill_id: str, scope: ScopeContext
    ) -> dict:
        with db._transaction():
            db.save_skill(data, author=scope.actor_name)
            validation = self._get_skill_validation(db, skill_id)
            enforce_enabled_save_validation(
                entity_type="skill",
                entity_id=skill_id,
                action="save",
                validation=validation,
                final_enabled=bool(data.get("enabled", False)),
            )
        return validation

    # ------------------------------------------------------------------ #
    # Reads with projections and readiness                                #
    # ------------------------------------------------------------------ #

    def list_skills(
        self,
        *,
        scope: ScopeContext,
        enabled_only: bool = False,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self._db()
        rows = projection_aware_catalog_rows(
            db,
            "skill",
            "skill_id",
            db.list_skills(enabled_only=enabled_only, account_id=scope.account_id),
            account_id=scope.account_id,
            enabled_only=enabled_only,
        )
        if search:
            query = str(search).strip().casefold()
            if query:
                rows = [
                    row
                    for row in rows
                    if query
                    in " ".join(
                        str(row.get(field) or "")
                        for field in ("skill_id", "display_name", "description", "category")
                    ).casefold()
                ]
        return [project_skill_contract_fields(row) for row in rows]

    def get_skill(self, skill_id: str, *, scope: ScopeContext) -> dict[str, Any]:
        db = self._db()
        skill = projection_aware_catalog_row(
            db,
            "skill",
            "skill_id",
            skill_id,
            db.get_skill(skill_id),
            account_id=scope.account_id,
        )
        if not skill:
            raise ServiceNotFoundError(_skill_not_found(skill_id))
        require_row_in_account_scope(skill, scope, detail=_skill_not_found(skill_id))
        return project_skill_contract_fields(skill)

    def validate_skill(self, skill_id: str, *, scope: ScopeContext) -> dict[str, Any]:
        """Readiness of one skill for enable and agent assignment."""
        db = self._db()
        self._require_skill(db, skill_id, scope)
        return {"skill_id": skill_id, "validation": self._get_skill_validation(db, skill_id)}

    # ------------------------------------------------------------------ #
    # Create / replace / patch / enable / delete                          #
    # ------------------------------------------------------------------ #

    def create_skill(
        self, payload: Mapping[str, Any], *, scope: ScopeContext
    ) -> SkillMutationResultView:
        db = self._db()
        skill_id = unique_text_key(
            payload.get("skill_id"),
            lambda candidate: db.get_skill(candidate) is not None,
        )
        data = dict(payload)
        data["skill_id"] = skill_id
        require_create_account_scope(data, scope)
        # Registry ownership is transport context, not client-controlled data.
        # Otherwise non-default-account creates fall back to Main account.
        data["account_id"] = scope.account_id
        self._validate_expected_io(db, skill_id, data)
        validation = self._save_skill(db, data, skill_id=skill_id, scope=scope)
        self._reload_registry()
        return SkillMutationResultView(status="created", skill_id=skill_id, validation=validation)

    def update_skill(
        self,
        skill_id: str,
        payload: Mapping[str, Any],
        *,
        provided_fields: Collection[str],
        scope: ScopeContext,
    ) -> SkillMutationResultView:
        """Full replacement; I/O contracts omitted from the request keep their stored rows."""
        db = self._db()
        existing = db.get_skill(skill_id)
        if existing:
            require_row_in_account_scope(existing, scope, detail=_skill_not_found(skill_id))
        data = dict(payload)
        data["skill_id"] = skill_id
        require_create_account_scope(data, scope)
        # Full replacement must retain the row's authenticated account ownership.
        data["account_id"] = (
            existing.get("account_id") if existing is not None else scope.account_id
        )
        projected_existing = project_skill_contract_fields(existing or {})
        if "expected_inputs" not in provided_fields:
            data["expected_inputs"] = list(projected_existing.get("expected_inputs") or [])
        if "expected_outputs" not in provided_fields:
            data["expected_outputs"] = list(projected_existing.get("expected_outputs") or [])
        self._validate_expected_io(db, skill_id, data)
        validation = self._save_skill(db, data, skill_id=skill_id, scope=scope)
        self._reload_registry()
        db.materialize_agents_for_skill(skill_id)
        return SkillMutationResultView(status="updated", skill_id=skill_id, validation=validation)

    def patch_skill(
        self, skill_id: str, updates: Mapping[str, Any], *, scope: ScopeContext
    ) -> SkillMutationResultView:
        """Merge only the provided, non-null fields into the existing skill."""
        db = self._db()
        existing = self._require_skill(db, skill_id, scope)
        updates = dict(updates)
        if not updates:
            return SkillMutationResultView(status="no_change", skill_id=skill_id)
        self._check_enabled_transition(
            db,
            skill_id,
            current_enabled=bool(existing.get("enabled", False)),
            target_enabled=updates.get("enabled") is not False,
        )
        merged = {**existing, **updates, "skill_id": skill_id}
        validation = self._save_skill(db, merged, skill_id=skill_id, scope=scope)
        self._reload_registry()
        return SkillMutationResultView(status="updated", skill_id=skill_id, validation=validation)

    def toggle_skill(self, skill_id: str, *, scope: ScopeContext) -> SkillMutationResultView:
        """Set ``enabled`` to the opposite of the current state.

        Uses the same enabled-transition rule as ``PATCH {"enabled": ...}``;
        enabling additionally requires a valid readiness result.
        """
        db = self._db()
        current = self._require_skill(db, skill_id, scope)
        current_enabled = bool(current.get("enabled", False))
        target_enabled = not current_enabled
        self._check_enabled_transition(
            db, skill_id, current_enabled=current_enabled, target_enabled=target_enabled
        )
        if target_enabled:
            validation = self._get_skill_validation(db, skill_id)
            if not validation.get("valid", False):
                raise contract_validation_error(
                    entity_type="skill",
                    entity_id=skill_id,
                    action="enable",
                    validation=validation,
                )
        new_state = db.toggle_skill(skill_id)
        self._reload_registry()
        return SkillMutationResultView(status="toggled", skill_id=skill_id, enabled=new_state)

    def delete_skill(self, skill_id: str, *, scope: ScopeContext) -> SkillMutationResultView:
        db = self._db()
        existing = db.get_skill(skill_id)
        if existing:
            require_row_in_account_scope(existing, scope, detail=_skill_not_found(skill_id))
        dependency_lister = getattr(db, "get_skill_dependents", None)
        dependency_rows = (
            dependency_lister(skill_id, limit=_DEPENDENT_IDS_LIMIT + 1)
            if callable(dependency_lister)
            else []
        )
        if not isinstance(dependency_rows, list):
            dependency_rows = []
        dependent_ids = [
            f"{str(row.get('dependency_kind') or '').strip()}:"
            f"{str(row.get('dependency_id') or '').strip()}"
            for row in dependency_rows[:_DEPENDENT_IDS_LIMIT]
            if isinstance(row, dict)
            and str(row.get("dependency_kind") or "").strip()
            and str(row.get("dependency_id") or "").strip()
        ]
        if dependent_ids:
            raise ServiceConflictError(
                "registry_dependency_conflict",
                payload={
                    "error": "registry_dependency_conflict",
                    "entity_type": "registry_skill",
                    "entity_id": skill_id,
                    "dependent_ids": dependent_ids,
                    "action": "Remove or replace every listed dependency before trashing the skill.",
                    **(
                        {"dependent_ids_truncated": True}
                        if len(dependency_rows) > _DEPENDENT_IDS_LIMIT
                        else {}
                    ),
                },
            )
        try:
            db.delete_skill(skill_id, author=scope.actor_name)
        except KeyError as exc:
            raise ServiceNotFoundError(str(exc.args[0] if exc.args else exc)) from exc
        except Exception as exc:
            if "violates foreign key" in str(exc):
                raise ServiceConflictError(
                    f"Skill '{skill_id}' is still assigned to agents or workflow steps. "
                    "Remove associations first."
                ) from exc
            raise ServiceError(str(exc), status_code=500) from exc
        self._reload_registry()
        return SkillMutationResultView(status="deleted", skill_id=skill_id)

    def clone_skill(
        self,
        skill_id: str,
        *,
        target_skill_id: str | None = None,
        display_name: str | None = None,
        scope: ScopeContext,
    ) -> SkillMutationResultView:
        db = self._db()
        source = self._require_skill(db, skill_id, scope)
        target_id = unique_text_key(
            target_skill_id or f"{skill_id}_copy",
            lambda candidate: db.get_skill(candidate) is not None,
        )
        clone_data = {
            "skill_id": target_id,
            "account_id": scope.account_id,
            "display_name": display_name or f"{source.get('display_name', skill_id)} (Copy)",
            "description": source.get("description", ""),
            "category": source.get("category"),
            "skill_type": source.get("skill_type", "secondary"),
            "instructions": list(source.get("instructions") or []),
            "prompt_template": str(source.get("prompt_template") or ""),
            "expected_inputs": list(source.get("expected_inputs") or source.get("_inputs") or []),
            "expected_outputs": list(
                source.get("expected_outputs") or source.get("_outputs") or []
            ),
            "examples": list(source.get("examples") or []),
            "enabled": False,
            "version": source.get("version", "1.0.0"),
            "tags": list(source.get("tags") or []),
        }
        db.save_skill(clone_data, author=scope.actor_name)
        self._reload_registry()
        return SkillMutationResultView(
            status="cloned", skill_id=target_id, source_skill_id=skill_id
        )

    def restore_skill_revision(
        self, skill_id: str, entry_id: str | int, *, scope: ScopeContext
    ) -> SkillMutationResultView:
        db = self._db()
        self._require_skill(db, skill_id, scope)
        try:
            outcome = db.restore_entity_revision(
                entity_type="skill",
                entity_id=skill_id,
                entry_id=str(entry_id),
                author="admin_restore",
                validate_restored=lambda _entity_type, _entity_id, _restored: (
                    self._get_skill_validation(db, skill_id)
                ),
            )
        except RestoreValidationError as exc:
            validation = (
                exc.validation
                if isinstance(exc.validation, dict)
                else {"valid": False, "errors": [], "warnings": []}
            )
            raise contract_validation_error(
                entity_type="skill", entity_id=skill_id, action="restore", validation=validation
            ) from exc
        except KeyError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        validation = outcome.get("validation")
        if not isinstance(validation, dict):
            validation = self._get_skill_validation(db, skill_id)
        self._reload_registry()
        return SkillMutationResultView(
            status="restored",
            skill_id=skill_id,
            restored_from_entry_id=str(entry_id),
            validation=validation,
            snapshot=outcome.get("restored"),
        )

    # ------------------------------------------------------------------ #
    # I/O contracts                                                       #
    # ------------------------------------------------------------------ #

    def set_skill_contract_items(
        self,
        skill_id: str,
        kind: SkillIOKind,
        items: list[dict[str, Any]],
        *,
        scope: ScopeContext,
    ) -> list[dict[str, Any]]:
        """Replace the skill's inputs or outputs and rematerialize the agents using it."""
        db = self._db()
        self._require_skill(db, skill_id, scope)
        _, issues = validate_contract_items(
            items,
            path_prefix=(
                SKILL_EXPECTED_INPUTS_VALUE if kind == "inputs" else SKILL_EXPECTED_OUTPUTS_VALUE
            ),
            default_required=kind == "outputs",
            db=db,
        )
        if issues:
            raise contract_items_error(
                entity_type="skill", entity_id=skill_id, action=f"save_{kind}", issues=issues
            )
        getattr(db, f"set_skill_{kind}")(skill_id, items)
        self._reload_registry()
        db.materialize_agents_for_skill(skill_id)
        return getattr(db, f"get_skill_{kind}")(skill_id)


__all__ = [
    "SKILL_EXPECTED_INPUTS_VALUE",
    "SKILL_EXPECTED_OUTPUTS_VALUE",
    "SkillCommandService",
    "SkillIOKind",
    "project_skill_contract_fields",
]
