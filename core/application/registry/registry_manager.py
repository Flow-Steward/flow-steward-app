"""
RegistryManager — unified singleton access to all 4 registries.
Backend: PostgreSQL via RegistryDBStore.
Reloaded on SIGHUP or after admin operations.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager, suppress

from core.application.registry.agent_registry import AgentRegistry
from core.application.registry.builtin_mcp_tools import sync_builtin_mcp_tools
from core.application.registry.builtin_standard_tools import sync_builtin_standard_tools
from core.application.registry.builtin_step_kind_profiles import sync_builtin_step_kind_profiles
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.prompt_contracts import validate_skill_prompt
from core.application.registry.seed_builder_metadata_sync import (
    sync_registry_seed_builder_metadata,
)
from core.application.registry.skill_registry import SkillRegistry
from core.application.registry.tool_registry import ToolRegistry
from core.application.registry.workflow_registry import WorkflowRegistry
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_CONNECTOR,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_INVOKE_WORKFLOW,
    is_control_flow_step_kind,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

_instance: RegistryManager | None = None
_lock = threading.Lock()
_thread_local = threading.local()
_runtime_override_lock = threading.RLock()
_runtime_override_registry: RegistryManager | None = None


class RegistryManager:
    """Unified access to all 4 registries. Thread-safe singleton."""

    def __init__(self, db_dsn: str, *, sync_builtins: bool = True):
        self.db_dsn = db_dsn

        self._db_store = RegistryDBStore(self.db_dsn)
        self._db_store.ensure_builder_registry_schema()
        if sync_builtins:
            with self._db_store.suppress_projection_sync():
                changed_step_kinds = sync_builtin_step_kind_profiles(self._db_store)
                changed_tools = sync_builtin_mcp_tools(self._db_store)
                changed_tools.extend(sync_builtin_standard_tools(self._db_store))
                changed_seed_metadata = sync_registry_seed_builder_metadata(self._db_store)
            if changed_step_kinds:
                logger.info("Synced builtin step kind profiles: %s", changed_step_kinds)
            if changed_tools:
                logger.info("Synced builtin tool manifests: %s", changed_tools)
            if changed_seed_metadata:
                logger.info("Backfilled seed builder metadata: %s", changed_seed_metadata)
        self.agents = AgentRegistry(self._db_store)
        self.tools = ToolRegistry(self._db_store)
        self.skills = SkillRegistry(self._db_store)
        self.workflows = WorkflowRegistry(self._db_store)

        # Self-heal the materialized agent contracts once on a fresh database so
        # capability discovery sees agent outputs immediately. Skipped for
        # read-only snapshots (sync_builtins=False) which must not write.
        if sync_builtins:
            self._db_store.ensure_agent_contracts_materialized()

        # Build the dynamic tool resolver (tool_id → LangChain callables)
        self.tool_resolver = self.tools.build_resolver()

        logger.info(
            "RegistryManager initialized — agents=%d tools=%d skills=%d workflows=%d",
            len(self.agents.list_all()),
            len(self.tools.list_all()),
            len(self.skills.list_all()),
            len(self.workflows.list_all()),
        )
        resolver_info = self.tool_resolver.list_registered()
        logger.info(
            "ToolResolver ready: %d tool_ids resolved → %s",
            len(resolver_info),
            {tid: len(fns) for tid, fns in resolver_info.items()},
        )
        for err in self.tool_resolver.get_errors():
            logger.warning("ToolResolver: %s", err)

    @property
    def db_store(self):
        """Access the DB store."""
        return self._db_store

    @classmethod
    def _resolve_db_dsn(cls) -> str:
        db_dsn = _runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL")
        if not db_dsn:
            raise RuntimeError("No database DSN configured. Set FS_DB_DSN or DATABASE_URL.")
        return db_dsn

    @classmethod
    def instance(cls) -> RegistryManager:
        """Return the singleton instance. Creates it on first call."""
        global _instance
        global _runtime_override_registry
        override = getattr(_thread_local, "registry_override", None)
        if override is not None:
            return override
        if _runtime_override_registry is not None:
            return _runtime_override_registry
        if _instance is None:
            with _lock:
                if _instance is None:
                    _instance = cls(cls._resolve_db_dsn())
        return _instance

    @classmethod
    def read_only(cls) -> RegistryManager:
        """Return a fresh registry snapshot without builtin sync side effects."""
        return cls(cls._resolve_db_dsn(), sync_builtins=False)

    @classmethod
    @contextmanager
    def thread_local_instance(cls, registry: RegistryManager):
        previous = getattr(_thread_local, "registry_override", None)
        _thread_local.registry_override = registry
        try:
            yield registry
        finally:
            if previous is None:
                with suppress(AttributeError):
                    delattr(_thread_local, "registry_override")
            else:
                _thread_local.registry_override = previous

    @classmethod
    @contextmanager
    def process_instance_override(cls, registry: RegistryManager):
        global _runtime_override_registry
        with _runtime_override_lock:
            previous = _runtime_override_registry
            _runtime_override_registry = registry
            try:
                yield registry
            finally:
                _runtime_override_registry = previous

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful for tests)."""
        global _instance
        with _lock:
            _instance = None

    def reload_all(self) -> None:
        """Reload all registries. Safe to call while the app is running."""
        with _lock:
            logger.info("Reloading all registries")
            try:
                self._db_store.ensure_builder_registry_schema()
                with self._db_store.suppress_projection_sync():
                    changed_step_kinds = sync_builtin_step_kind_profiles(self._db_store)
                    changed_tools = sync_builtin_mcp_tools(self._db_store)
                    changed_tools.extend(sync_builtin_standard_tools(self._db_store))
                    changed_seed_metadata = sync_registry_seed_builder_metadata(self._db_store)
                if changed_step_kinds:
                    logger.info("Synced builtin step kind profiles: %s", changed_step_kinds)
                if changed_tools:
                    logger.info("Synced builtin tool manifests: %s", changed_tools)
                if changed_seed_metadata:
                    logger.info("Backfilled seed builder metadata: %s", changed_seed_metadata)
            except Exception as e:
                logger.warning("Builtin registry sync failed during reload: %s", e)
            self.agents.reload()
            self.tools.reload()
            self.skills.reload()
            self.workflows.reload()
            self.tool_resolver = self.tools.build_resolver()
            logger.info(
                "Registry reload complete — agents=%d tools=%d skills=%d workflows=%d",
                len(self.agents.list_all()),
                len(self.tools.list_all()),
                len(self.skills.list_all()),
                len(self.workflows.list_all()),
            )

    def refresh_workflow_cache(self) -> None:
        """Refresh only the read-only workflow snapshot after a durable mutation."""
        with _lock:
            self.workflows.reload()

    def validate(self) -> tuple[list[str], list[str]]:
        """Cross-registry integrity check. Returns (errors, warnings)."""
        errors: list[str] = []
        warnings: list[str] = []
        from core.application.registry.db_store import RegistryDBStore

        all_tools = list(self.tools.list_all() or [])
        all_skills = list(self.skills.list_all() or [])
        all_agents = list(self.agents.list_all() or [])
        all_workflows = list(self.workflows.list_all() or [])

        all_tool_ids = {t.tool_id for t in all_tools}
        all_skill_ids = {c.skill_id for c in all_skills}
        all_agent_ids = {a.agent_id for a in all_agents}
        enabled_agent_ids = {a.agent_id for a in all_agents if a.enabled}

        enabled_non_alias_workflows = [wf for wf in all_workflows if wf.enabled]

        def _participant_bindings(workflow) -> dict[str, str]:
            settings = getattr(workflow, "settings", {}) if workflow is not None else {}
            if not isinstance(settings, dict):
                settings = {}
            raw = settings.get("participants")
            if not isinstance(raw, list):
                return {}
            bindings: dict[str, str] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                participant_key = str(item.get("participant_key") or "").strip()
                agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
                if participant_key and agent_id:
                    bindings[participant_key] = agent_id
            return bindings

        used_agents_by_enabled_workflows: set[str] = set()
        for workflow in enabled_non_alias_workflows:
            bindings = _participant_bindings(workflow)
            for step in workflow.steps:
                step_agent = str(step.agent or "").strip()
                participant_key = str(getattr(step, "participant_key", "") or "").strip()
                if participant_key and participant_key in bindings:
                    step_agent = str(bindings.get(participant_key) or "").strip()
                if step_agent:
                    used_agents_by_enabled_workflows.add(step_agent)

        for agent in all_agents:
            if not agent.enabled:
                continue
            if (
                enabled_non_alias_workflows
                and not str(agent.agent_id or "").startswith("extension.")
                and agent.agent_id not in used_agents_by_enabled_workflows
            ):
                warnings.append(f"Enabled agent '{agent.agent_id}' is unused by enabled workflows")
            if len(agent.skills) == 1 and (agent.structured_inputs or agent.structured_outputs):
                warnings.append(
                    f"Agent '{agent.agent_id}' duplicates structured I/O at the agent level while "
                    f"single skill '{agent.skills[0]}' already defines the canonical contract"
                )
            if str(agent.prompts.output_format or "").strip():
                errors.append(
                    f"Agent '{agent.agent_id}' still defines deprecated prompts.output_format; "
                    "canonical phase_result_v2 is the only allowed outer response contract"
                )

        for agent in all_agents:
            for cap_id in agent.skills:
                if cap_id not in agent.prompts.skill_prompts:
                    errors.append(
                        f"Agent '{agent.agent_id}': skill '{cap_id}' listed but "
                        f"no matching key in prompts.skill_prompts"
                    )
                if cap_id not in all_skill_ids:
                    errors.append(
                        f"Agent '{agent.agent_id}': skill '{cap_id}' not found in registry"
                    )
                    continue
                prompt_errors = validate_skill_prompt(agent.prompts.skill_prompts.get(cap_id, ""))
                for prompt_error in prompt_errors:
                    errors.append(f"Agent '{agent.agent_id}' skill '{cap_id}': {prompt_error}")
            for tool_id in agent.tools:
                if tool_id not in all_tool_ids:
                    errors.append(
                        f"Agent '{agent.agent_id}': tool '{tool_id}' not found in registry"
                    )

        for skill in all_skills:
            if not skill.enabled:
                continue
            if not skill.input_schema:
                errors.append(f"Enabled skill '{skill.skill_id}' is missing input_schema")
            if not skill.output_schema:
                errors.append(f"Enabled skill '{skill.skill_id}' is missing output_schema")
            expected_input_schema = RegistryDBStore._build_contract_schema(skill.expected_inputs)
            expected_output_schema = RegistryDBStore._build_contract_schema(skill.expected_outputs)
            if skill.input_schema != expected_input_schema:
                errors.append(
                    f"Skill '{skill.skill_id}' input_schema drifts from structured inputs"
                )
            if skill.output_schema != expected_output_schema:
                errors.append(
                    f"Skill '{skill.skill_id}' output_schema drifts from structured outputs"
                )

        for workflow in all_workflows:
            workflow_enabled = bool(workflow.enabled)
            bindings = _participant_bindings(workflow)
            for step in workflow.steps:
                step_kind = str(getattr(step, "step_kind", "agent") or "agent").strip().lower()
                participant_key = str(getattr(step, "participant_key", "") or "").strip()
                resolved_agent_id = str(step.agent or "").strip()
                if participant_key:
                    if participant_key not in bindings and not step.optional:
                        errors.append(
                            f"Workflow '{workflow.workflow_id}' step '{step.step_id}': "
                            f"participant_key '{participant_key}' is not declared in workflow participants"
                        )
                    bound_agent_id = str(bindings.get(participant_key) or "").strip()
                    if bound_agent_id:
                        if (
                            resolved_agent_id
                            and resolved_agent_id != bound_agent_id
                            and not step.optional
                        ):
                            errors.append(
                                f"Workflow '{workflow.workflow_id}' step '{step.step_id}': "
                                f"agent '{resolved_agent_id}' conflicts with participant_key "
                                f"'{participant_key}' binding '{bound_agent_id}'"
                            )
                        resolved_agent_id = bound_agent_id

                if (
                    step_kind
                    in {
                        STEP_KIND_CONNECTOR,
                        STEP_KIND_DATASET_QUERY,
                        STEP_KIND_INVOKE_WORKFLOW,
                    }
                    or step_kind == STEP_KIND_HTTP_REQUEST
                    or is_control_flow_step_kind(step_kind)
                ):
                    if workflow_enabled and step.override:
                        for tool_id in step.override.get("add_tools", []):
                            if tool_id not in all_tool_ids:
                                errors.append(
                                    f"Workflow '{workflow.workflow_id}' step '{step.step_id}' "
                                    f"override: tool '{tool_id}' not found in registry"
                                )
                    continue

                if workflow_enabled and not resolved_agent_id and not step.optional:
                    errors.append(
                        f"Workflow '{workflow.workflow_id}' step '{step.step_id}': "
                        "agent step requires agent_id or participant_key"
                    )
                    continue

                if (
                    workflow_enabled
                    and resolved_agent_id not in all_agent_ids
                    and not step.optional
                ):
                    errors.append(
                        f"Workflow '{workflow.workflow_id}' step '{step.step_id}': "
                        f"agent '{resolved_agent_id}' not found in registry"
                    )
                if (
                    workflow_enabled
                    and resolved_agent_id in all_agent_ids
                    and resolved_agent_id not in enabled_agent_ids
                    and not step.optional
                ):
                    errors.append(
                        f"Workflow '{workflow.workflow_id}' step '{step.step_id}': "
                        f"agent '{resolved_agent_id}' is disabled"
                    )
                if workflow_enabled and step.override:
                    for tool_id in step.override.get("add_tools", []):
                        if tool_id not in all_tool_ids:
                            errors.append(
                                f"Workflow '{workflow.workflow_id}' step '{step.step_id}' "
                                f"override: tool '{tool_id}' not found in registry"
                            )

        provided_caps = set()
        for agent in all_agents:
            if agent.enabled:
                provided_caps.update(agent.skills)
        for cap in self.skills.list_enabled():
            if cap.skill_id not in provided_caps:
                warnings.append(
                    f"Skill '{cap.skill_id}' is enabled but no enabled agent provides it"
                )

        used_tools: set[str] = set()
        for agent in all_agents:
            used_tools.update(agent.tools)
        for workflow in enabled_non_alias_workflows:
            for step in workflow.steps:
                connector_id = str(getattr(step, "connector_id", "") or "").strip()
                if connector_id:
                    used_tools.add(connector_id)
                override = getattr(step, "override", None)
                if isinstance(override, dict):
                    used_tools.update(
                        str(tool_id or "").strip()
                        for tool_id in override.get("add_tools", [])
                        if str(tool_id or "").strip()
                    )
        for tool in self.tools.list_enabled():
            config = getattr(tool, "config", {})
            if isinstance(config, dict) and config.get("agent_exposure") == "connector_only":
                continue
            if tool.tool_id not in used_tools:
                warnings.append(f"Tool '{tool.tool_id}' is enabled but used by no agent")

        return errors, warnings

    def get_stats(self) -> dict:
        if self._db_store:
            return self._db_store.get_stats()
        return {
            "agents": {
                "total": len(self.agents.list_all()),
                "enabled": sum(1 for a in self.agents.list_all() if a.enabled),
            },
            "tools": {
                "total": len(self.tools.list_all()),
                "enabled": sum(1 for t in self.tools.list_all() if t.enabled),
            },
            "skills": {
                "total": len(self.skills.list_all()),
                "enabled": len(self.skills.list_enabled()),
            },
            "workflows": {
                "total": len(self.workflows.list_all()),
                "enabled": len(self.workflows.list_enabled()),
            },
        }
