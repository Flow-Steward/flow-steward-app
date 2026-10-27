"""
SkillRegistry — manages skill manifests loaded from PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.application.management.errors import ServiceScopeNotFoundError
from core.application.registry.skill_manifest import SkillManifest

if TYPE_CHECKING:
    from core.application.registry.agent_manifest import AgentManifest
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self, db_store: RegistryDBStore):
        self._db_store = db_store
        self._skills: dict[str, SkillManifest] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        self._skills.clear()
        try:
            rows = self._db_store.list_skills()
            for row in rows:
                try:
                    manifest = SkillManifest.from_db_row(row)
                    self._skills[manifest.skill_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load skill from DB row %s: %s", row.get("skill_id"), e
                    )
            from core.application.registry.projected_artifact_rows import projected_artifact_rows

            for row in projected_artifact_rows(self._db_store, "skill", "skill_id"):
                try:
                    inputs = list(row.get("expected_inputs") or [])
                    outputs = list(row.get("expected_outputs") or [])
                    row["_inputs"] = inputs
                    row["_outputs"] = outputs
                    if not row.get("input_schema") or not row.get("output_schema"):
                        from core.application.registry.db_store import RegistryDBStore

                        if not row.get("input_schema"):
                            row["input_schema"] = RegistryDBStore._build_contract_schema(inputs)
                        if not row.get("output_schema"):
                            row["output_schema"] = RegistryDBStore._build_contract_schema(outputs)
                    manifest = SkillManifest.from_db_row(row)
                    self._skills[manifest.skill_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load projected skill %s: %s", row.get("skill_id"), e
                    )
            logger.debug("Loaded %d skills from DB", len(self._skills))
        except Exception as e:
            logger.exception("Failed to load skills from DB: %s", e)

    def get_skill(self, skill_id: str) -> SkillManifest | None:
        return self._skills.get(skill_id)

    def resolve_assigned_skill(
        self,
        agent: AgentManifest,
        skill_id: str,
        *,
        scope: Mapping[str, Any],
        project_id: str | None = None,
    ) -> SkillManifest:
        """Read only the selected assigned skill under the invocation's runtime scope."""
        from core.application.workflows.discovery.scoped_capability_catalog import (
            ScopedCapabilityCatalog,
        )

        message = "Selected skill is unavailable in the current scope."
        if not skill_id or skill_id not in agent.skills:
            raise ServiceScopeNotFoundError(message)
        try:
            row = ScopedCapabilityCatalog(self._db_store).assert_assigned_skill_available(
                scope=scope,
                project_id=project_id,
                skill_id=skill_id,
            )
        except ValueError as exc:
            raise ServiceScopeNotFoundError(message) from exc
        return SkillManifest.from_db_row(row)

    def list_all(self) -> list[SkillManifest]:
        return list(self._skills.values())

    def list_enabled(self) -> list[SkillManifest]:
        return [c for c in self._skills.values() if c.enabled]

    def list_by_category(self, category: str) -> list[SkillManifest]:
        return [c for c in self._skills.values() if c.category == category]

    def reload(self) -> None:
        logger.info("Reloading skill registry from DB")
        self._load_from_db()
        logger.info("Skill registry reloaded: %d skills", len(self._skills))
