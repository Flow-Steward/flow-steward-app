"""Agent skill/tool assignments and structured I/O commands.

Split out of ``command_services_agent`` to keep each module under the size
limit. The mixin relies on ``AgentCommandService`` for the database handle,
registry reload, account-scoped agent lookup and readiness callbacks.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from core.application.management.command_models import AgentMutationResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
)
from core.application.management.scope import ScopeContext
from core.application.registry.agent_own_io import save_agent_own_io
from core.application.registry.contract_item_validation import validate_contract_items
from core.application.registry.db_store_agents_mixin import (
    AgentRelationshipConflictError,
    AgentRelationshipReadinessError,
    AgentRelationshipValidationError,
)

from .command_services_agent_rules import (
    contract_items_error,
    contract_validation_error,
    primary_skill_id,
    reject_standard_tool_actions_for_agent,
)

logger = logging.getLogger(__name__)

_VIOLATES_FOREIGN_KEY = "violates foreign key"

AgentIOKind = Literal["inputs", "outputs"]


class AgentRelationshipCommandsMixin:
    """Skill and tool assignments of one agent, plus its structured I/O."""

    # Provided by AgentCommandService.
    _db: Any
    _reload_registry: Any
    _require_agent: Any
    _relationship_validator: Any
    _skill_compatibility_validator: Any

    # ------------------------------------------------------------------ #
    # Skill assignments                                                   #
    # ------------------------------------------------------------------ #

    def add_agent_skill(
        self,
        agent_id: str,
        skill_id: str,
        skill_prompt: str | None,
        *,
        scope: ScopeContext,
    ) -> list[dict[str, Any]]:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        try:
            db.add_agent_skill(
                agent_id,
                skill_id,
                skill_prompt,
                validate_skill_compatibility=self._skill_compatibility_validator,
                validate_agent=self._relationship_validator,
            )
        except AgentRelationshipReadinessError as exc:
            raise contract_validation_error(
                entity_type="agent",
                entity_id=agent_id,
                action="save_skill_mapping",
                validation=exc.validation,
            ) from exc
        except AgentRelationshipValidationError as exc:
            raise ServiceNotFoundError("Skill not found") from exc
        except AgentRelationshipConflictError as exc:
            raise ServiceConflictError(str(exc)) from exc
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                raise ServiceConflictError("Skill already assigned") from exc
            if _VIOLATES_FOREIGN_KEY in str(exc).lower():
                raise ServiceNotFoundError(f"Skill '{skill_id}' not found") from exc
            raise ServiceError(str(exc), status_code=500) from exc
        self._reload_registry()
        return db.get_agent_skills(agent_id)

    def remove_agent_skill(
        self, agent_id: str, skill_id: str, *, scope: ScopeContext
    ) -> list[dict[str, Any]]:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        db.remove_agent_skill(agent_id, skill_id)
        self._reload_registry()
        db.materialize_agent_contract(agent_id)
        return db.get_agent_skills(agent_id)

    def reorder_agent_skills(
        self, agent_id: str, ordered_ids: list[str], *, scope: ScopeContext
    ) -> list[dict[str, Any]]:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        db.reorder_agent_skills(agent_id, ordered_ids)
        self._reload_registry()
        db.materialize_agent_contract(agent_id)
        return db.get_agent_skills(agent_id)

    def update_agent_skill_prompt(
        self, agent_id: str, skill_id: str, skill_prompt: str, *, scope: ScopeContext
    ) -> AgentMutationResultView:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        db.update_skill_prompt(agent_id, skill_id, skill_prompt)
        self._reload_registry()
        return AgentMutationResultView(status="updated", agent_id=agent_id)

    # ------------------------------------------------------------------ #
    # Tool assignments                                                    #
    # ------------------------------------------------------------------ #

    def replace_agent_relationships(
        self,
        agent_id: str,
        *,
        expected_skill_ids: list[str],
        expected_tool_ids: list[str],
        skill_ids: list[str],
        tool_ids: list[str],
        scope: ScopeContext,
    ) -> Any:
        """Atomically replace the exact skill/tool snapshot with CAS protection."""
        db = self._db()
        self._require_agent(db, agent_id, scope)
        reject_standard_tool_actions_for_agent(tool_ids)
        try:
            result = db.replace_agent_relationships(
                agent_id,
                expected_skill_ids=expected_skill_ids,
                expected_tool_ids=expected_tool_ids,
                skill_ids=skill_ids,
                tool_ids=tool_ids,
                validate_skill_compatibility=self._skill_compatibility_validator,
                validate_agent=self._relationship_validator,
            )
        except AgentRelationshipConflictError as exc:
            raise ServiceConflictError(str(exc)) from exc
        except AgentRelationshipValidationError as exc:
            raise contract_validation_error(
                entity_type="agent",
                entity_id=agent_id,
                action="replace_relationships",
                validation=exc.validation,
            ) from exc
        except KeyError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        except Exception as exc:
            if _VIOLATES_FOREIGN_KEY in str(exc).lower():
                raise ServiceNotFoundError("Skill or tool not found") from exc
            raise
        self._reload_registry()
        return result

    def add_agent_tool(
        self, agent_id: str, tool_id: str | None, *, scope: ScopeContext
    ) -> list[dict[str, Any]]:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        requested_tool_id = tool_id
        tool_id = str(tool_id or "").strip()
        reject_standard_tool_actions_for_agent([tool_id])
        added = False
        try:
            db.add_agent_tool(agent_id, tool_id)
            added = True
        except AgentRelationshipValidationError as exc:
            raise ServiceNotFoundError("Tool not found") from exc
        except AgentRelationshipConflictError as exc:
            raise ServiceConflictError(str(exc)) from exc
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                raise ServiceConflictError("Tool already assigned") from exc
            if _VIOLATES_FOREIGN_KEY in str(exc).lower():
                raise ServiceNotFoundError(f"Tool '{requested_tool_id}' not found") from exc
            raise ServiceError(str(exc), status_code=500) from exc
        validation = self._relationship_validator(db, agent_id)
        if not validation.get("valid", False):
            if added:
                try:
                    db.remove_agent_tool(agent_id, tool_id)
                except Exception:
                    logger.warning(
                        "Failed to rollback invalid agent tool mapping agent_id=%s tool_id=%s",
                        agent_id,
                        tool_id,
                    )
            raise contract_validation_error(
                entity_type="agent",
                entity_id=agent_id,
                action="save_tool_mapping",
                validation=validation,
            )
        self._reload_registry()
        db.materialize_agent_contract(agent_id)
        return db.get_agent_tools(agent_id)

    def remove_agent_tool(
        self, agent_id: str, tool_id: str, *, scope: ScopeContext
    ) -> list[dict[str, Any]]:
        db = self._db()
        self._require_agent(db, agent_id, scope)
        db.remove_agent_tool(agent_id, tool_id)
        self._reload_registry()
        db.materialize_agent_contract(agent_id)
        return db.get_agent_tools(agent_id)

    # ------------------------------------------------------------------ #
    # Structured I/O                                                      #
    # ------------------------------------------------------------------ #

    def set_agent_contract_items(
        self,
        agent_id: str,
        kind: AgentIOKind,
        items: list[dict[str, Any]],
        *,
        scope: ScopeContext,
    ) -> list[dict[str, Any]]:
        """Replace inputs or outputs; with one primary skill they belong to that skill."""
        db = self._db()
        agent = self._require_agent(db, agent_id, scope)
        skill_id = primary_skill_id(agent)
        _, issues = validate_contract_items(
            items,
            path_prefix=f"agent.{kind}",
            default_required=kind == "outputs",
        )
        if issues:
            raise contract_items_error(
                entity_type="agent", entity_id=agent_id, action=f"save_{kind}", issues=issues
            )
        if skill_id:
            getattr(db, f"set_skill_{kind}")(skill_id, items)
            self._reload_registry()
            db.materialize_agents_for_skill(skill_id)
            return getattr(db, f"get_skill_{kind}")(skill_id)
        getattr(db, f"set_agent_{kind}")(agent_id, items)
        db.materialize_agent_contract(agent_id)
        return getattr(db, f"get_agent_own_{kind}")(agent_id)

    def set_agent_own_io(
        self,
        agent_id: str,
        kind: AgentIOKind,
        items: list[dict[str, Any]],
        *,
        scope: ScopeContext,
    ) -> list[dict[str, Any]]:
        """Replace the agent's own inputs or outputs; skills and tools are not touched."""
        db = self._db()
        self._require_agent(db, agent_id, scope)
        saved, issues = save_agent_own_io(db, agent_id, kind, items)
        if issues:
            raise contract_items_error(
                entity_type="agent", entity_id=agent_id, action=f"save_own_{kind}", issues=issues
            )
        return saved


__all__ = ["AgentIOKind", "AgentRelationshipCommandsMixin"]
