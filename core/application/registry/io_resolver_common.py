# ruff: noqa: F401
"""
IOResolver — computes effective I/O, template variables, canonical response
contract previews, and step-level I/O validation for registry entities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_BATCH,
    STEP_KIND_CONNECTOR,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_FOR_EACH,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_IF,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_SWITCH,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    normalize_step_kind,
)
from core.infrastructure.security.prompt_safety import blocked_reference_examples

CONNECTOR_PREFIX = "connector."
INPUTS_PREFIX = "inputs."
STATE_INPUTS_PREFIX = "state.inputs."
STATE_PREFIX = "state."


if TYPE_CHECKING:
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)

# Orchestrator state keys that are always available as template variables
GLOBAL_STATE_VARS: list[dict] = [
    {
        "name": "task_id",
        "type": "text",
        "description": "Unique task identifier",
        "description_key": "react.workflows.variable_description.task_id",
    },
    {
        "name": "task_summary",
        "type": "text",
        "description": "Task title / description",
        "description_key": "react.workflows.variable_description.task_summary",
    },
    {
        "name": "task_source",
        "type": "text",
        "description": "Generic origin marker (for example manual, provider, web, schedule)",
        "description_key": "react.workflows.variable_description.task_source",
    },
    {
        "name": "project_name",
        "type": "text",
        "description": "Project name",
        "description_key": "react.workflows.variable_description.project_name",
    },
    {
        "name": "effort_hint",
        "type": "text",
        "description": "Effort hint: micro|small|standard|major",
        "description_key": "react.workflows.variable_description.effort_hint",
    },
    {
        "name": "task_type",
        "type": "text",
        "description": "Optional external task label (transport/projection only)",
        "description_key": "react.workflows.variable_description.task_type",
    },
    {
        "name": "risk_level",
        "type": "text",
        "description": "Risk level: low|medium|high",
        "description_key": "react.workflows.variable_description.risk_level",
    },
    {
        "name": "routing_reason",
        "type": "text",
        "description": "Deterministic routing reason",
        "description_key": "react.workflows.variable_description.routing_reason",
    },
    {
        "name": "routing_signals",
        "type": "object",
        "description": "Structured routing signals",
        "description_key": "react.workflows.variable_description.routing_signals",
    },
    {
        "name": "reentry_context",
        "type": "object",
        "description": "Parent/re-entry lineage metadata",
        "description_key": "react.workflows.variable_description.reentry_context",
    },
    {
        "name": "resume_parent",
        "type": "text",
        "description": "Whether this run resumes from a parent lineage",
        "description_key": "react.workflows.variable_description.resume_parent",
    },
    {
        "name": "feedback_type",
        "type": "text",
        "description": "Re-entry feedback subtype",
        "description_key": "react.workflows.variable_description.feedback_type",
    },
    {
        "name": "parent_task_id",
        "type": "text",
        "description": "Parent task identifier for lineage-aware workflows",
        "description_key": "react.workflows.variable_description.parent_task_id",
    },
    {
        "name": "parent_job_id",
        "type": "text",
        "description": "Parent job identifier for lineage-aware workflows",
        "description_key": "react.workflows.variable_description.parent_job_id",
    },
    {
        "name": "parent_workflow_id",
        "type": "text",
        "description": "Parent workflow identifier",
        "description_key": "react.workflows.variable_description.parent_workflow_id",
    },
    {
        "name": "kb_context",
        "type": "text",
        "description": "Internal project context",
        "description_key": "react.workflows.variable_description.kb_context",
    },
    {
        "name": "status",
        "type": "text",
        "description": "Job status",
        "description_key": "react.workflows.variable_description.status",
    },
    {
        "name": "agent_messages",
        "type": "array",
        "description": "All AgentMessageV1 envelopes in runtime order",
        "description_key": "react.workflows.variable_description.agent_messages",
    },
    {
        "name": "feedback",
        "type": "text",
        "description": "Current retry feedback",
        "description_key": "react.workflows.variable_description.feedback",
    },
    {
        "name": "current_step_id",
        "type": "text",
        "description": "Workflow step the run is at",
        "description_key": "react.workflows.variable_description.current_step_id",
    },
    {
        "name": "current_group_id",
        "type": "text",
        "description": "Real group of the current step, when it is inside one",
        "description_key": "react.workflows.variable_description.current_group_id",
    },
    {
        "name": "rework_instructions",
        "type": "text",
        "description": "Rework instructions produced by an authored step or gate",
        "description_key": "react.workflows.variable_description.rework_instructions",
    },
]

# Well-known dotted paths
DOTTED_PATH_VARS: list[dict] = [
    {
        "name": "project_config.conventions",
        "type": "text",
        "description": "Project coding conventions",
        "description_key": "react.workflows.variable_description.project_conventions",
    },
    {
        "name": "project_config.project_rules",
        "type": "text",
        "description": "Project-specific rules",
        "description_key": "react.workflows.variable_description.project_rules",
    },
]

BLOCKED_PATH_VARS: list[dict] = [
    {**item, "type": "blocked"} for item in blocked_reference_examples()
]

# State keys always available for validation (superset of template vars)
ALWAYS_AVAILABLE_KEYS = {v["name"] for v in GLOBAL_STATE_VARS}


class IOResolverCommonMixin:
    """Common registry cache helpers and effective I/O computation."""

    def __init__(self, db: RegistryDBStore):
        self.db = db
        self._workflow_cache: dict[str, dict | None] = {}
        self._workflow_list_cache: list[dict] | None = None
        self._tool_cache: dict[str, dict | None] = {}
        self._agent_cache: dict[str, dict | None] = {}
        self._skill_cache: dict[str, dict | None] = {}
        self._agent_skills_cache: dict[str, list[dict]] = {}
        self._agent_tools_cache: dict[str, list[dict]] = {}
        self._agent_inputs_cache: dict[str, list[dict]] = {}
        self._agent_outputs_cache: dict[str, list[dict]] = {}
        self._skill_inputs_cache: dict[str, list[dict]] = {}
        self._skill_outputs_cache: dict[str, list[dict]] = {}
        self._extension_install_contract_cache: dict[tuple[str, str], dict | None] = {}

    def _get_workflow(self, workflow_id: str) -> dict | None:
        key = str(workflow_id or "").strip()
        if not key:
            return None
        if key not in self._workflow_cache:
            from core.application.registry.workflow_lookup import get_workflow_row

            self._workflow_cache[key] = get_workflow_row(self.db, key)
        return self._workflow_cache[key]

    def _list_workflows(self) -> list[dict]:
        if self._workflow_list_cache is None:
            self._workflow_list_cache = list(self.db.list_workflows() or [])
        return self._workflow_list_cache

    def _get_tool(self, tool_id: str) -> dict | None:
        key = str(tool_id or "").strip()
        if not key:
            return None
        if key not in self._tool_cache:
            from core.application.registry.workflow_lookup import get_registry_artifact_row

            self._tool_cache[key] = get_registry_artifact_row(
                self.db,
                artifact_kind="tool",
                id_key="tool_id",
                artifact_id=key,
                platform_getter="get_tool",
            )
        return self._tool_cache[key]

    def _get_extension_install_contract(
        self,
        *,
        install_id: str,
        account_id: str,
    ) -> dict | None:
        normalized_install_id = str(install_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        if not normalized_install_id:
            return None
        key = (normalized_install_id, normalized_account_id)
        if key in self._extension_install_contract_cache:
            return self._extension_install_contract_cache[key]

        contract: dict | None = None
        try:
            connection = getattr(self.db, "conn", None)
            if connection is not None:
                from core.application.auth.config import DEFAULT_ACCOUNT_ID
                from core.application.extensions.workflow_install_contract import (
                    resolve_workflow_extension_install_contract,
                )

                contract = resolve_workflow_extension_install_contract(
                    connection,
                    install_id=normalized_install_id,
                    account_id=normalized_account_id,
                    default_account_id=DEFAULT_ACCOUNT_ID,
                )
        except Exception as exc:
            logger.warning(
                "extension output contract lookup failed for install_id=%s: %s",
                normalized_install_id,
                exc,
            )
        self._extension_install_contract_cache[key] = contract
        return contract

    def _get_agent(self, agent_id: str) -> dict | None:
        key = str(agent_id or "").strip()
        if not key:
            return None
        if key not in self._agent_cache:
            from core.application.registry.workflow_lookup import get_registry_artifact_row

            self._agent_cache[key] = get_registry_artifact_row(
                self.db,
                artifact_kind="agent",
                id_key="agent_id",
                artifact_id=key,
                platform_getter="get_agent",
            )
        return self._agent_cache[key]

    def _get_skill(self, skill_id: str) -> dict | None:
        key = str(skill_id or "").strip()
        if not key:
            return None
        if key not in self._skill_cache:
            from core.application.registry.workflow_lookup import get_registry_artifact_row

            self._skill_cache[key] = get_registry_artifact_row(
                self.db,
                artifact_kind="skill",
                id_key="skill_id",
                artifact_id=key,
                platform_getter="get_skill",
            )
        return self._skill_cache[key]

    def _get_agent_skills(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        if key not in self._agent_skills_cache:
            self._agent_skills_cache[key] = list(self.db.get_agent_skills(key) or [])
        return self._agent_skills_cache[key]

    def _get_agent_tools(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        if key not in self._agent_tools_cache:
            self._agent_tools_cache[key] = list(self.db.get_agent_tools(key) or [])
        return self._agent_tools_cache[key]

    def _get_agent_inputs(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        if key not in self._agent_inputs_cache:
            self._agent_inputs_cache[key] = list(self.db.get_agent_inputs(key) or [])
        return self._agent_inputs_cache[key]

    def _get_agent_outputs(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        if key not in self._agent_outputs_cache:
            self._agent_outputs_cache[key] = list(self.db.get_agent_outputs(key) or [])
        return self._agent_outputs_cache[key]

    # Own-only agent reads. Used when computing the contract for *materialization*
    # (own_only=True): reading the full table there would resurrect a just-removed
    # skill/tool field as 'own', because compute runs before the stale rows are
    # deleted. Falls back to the full accessor for db doubles without the split.
    def _get_agent_own_inputs(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        getter = getattr(self.db, "get_agent_own_inputs", None) or self.db.get_agent_inputs
        return list(getter(key) or [])

    def _get_agent_own_outputs(self, agent_id: str) -> list[dict]:
        key = str(agent_id or "").strip()
        if not key:
            return []
        getter = getattr(self.db, "get_agent_own_outputs", None) or self.db.get_agent_outputs
        return list(getter(key) or [])

    def _get_skill_inputs(self, skill_id: str) -> list[dict]:
        key = str(skill_id or "").strip()
        if not key:
            return []
        if key not in self._skill_inputs_cache:
            rows = list(self.db.get_skill_inputs(key) or [])
            if not rows:
                skill = self._get_skill(key) or {}
                rows = list(skill.get("_inputs") or skill.get("expected_inputs") or [])
            self._skill_inputs_cache[key] = rows
        return self._skill_inputs_cache[key]

    def _get_skill_outputs(self, skill_id: str) -> list[dict]:
        key = str(skill_id or "").strip()
        if not key:
            return []
        if key not in self._skill_outputs_cache:
            rows = list(self.db.get_skill_outputs(key) or [])
            if not rows:
                skill = self._get_skill(key) or {}
                rows = list(skill.get("_outputs") or skill.get("expected_outputs") or [])
            self._skill_outputs_cache[key] = rows
        return self._skill_outputs_cache[key]

    @staticmethod
    def _connector_operation_selector(step: dict[str, Any]) -> str:
        if not isinstance(step, dict):
            return ""
        override = step.get("override")
        override = override if isinstance(override, dict) else {}
        connector_override = override.get(STEP_KIND_CONNECTOR)
        connector_override = connector_override if isinstance(connector_override, dict) else {}
        return str(
            connector_override.get("operation_id")
            or connector_override.get("function_name")
            or connector_override.get("action_id")
            or step.get("operation_id")
            or step.get("function_name")
            or ""
        ).strip()

    def compute_effective_io(self, agent_id: str, *, own_only: bool = False) -> dict:
        """Merge agent's own I/O + skill I/O + tool parameters.

        Returns {inputs: [...], outputs: [...]} where each item has
        {name, type, description, required, source, source_id}.

        own_only restricts the agent layer (step 3) to agent-authored fields.
        Used by contract materialization, where reading the full (already
        materialized) table would re-surface stale inherited rows as 'agent'.
        """
        agent_inputs = (
            self._get_agent_own_inputs(agent_id) if own_only else self._get_agent_inputs(agent_id)
        )
        agent_outputs = (
            self._get_agent_own_outputs(agent_id) if own_only else self._get_agent_outputs(agent_id)
        )
        seen_inputs: dict[str, dict] = {}
        seen_outputs: dict[str, dict] = {}

        # 1. Skill I/O (takes precedence — added first)
        caps = self._get_agent_skills(agent_id)
        for cap in caps:
            cap_id = cap["skill_id"]
            for inp in self._get_skill_inputs(cap_id):
                key = inp["name"]
                if key not in seen_inputs:
                    seen_inputs[key] = {
                        "name": key,
                        "type": inp.get("type", "text"),
                        "description": inp.get("description", ""),
                        "required": inp.get("required", False),
                        "source": "skill",
                        "source_id": cap_id,
                    }
            for out in self._get_skill_outputs(cap_id):
                key = out["name"]
                if key not in seen_outputs:
                    seen_outputs[key] = {
                        "name": key,
                        "type": out.get("type", "text"),
                        "description": out.get("description", ""),
                        "required": out.get("required", True),
                        "source": "skill",
                        "source_id": cap_id,
                    }

        # 2. Tool parameters (from interface JSONB)
        tools = self._get_agent_tools(agent_id)
        for tool_row in tools:
            tool = self._get_tool(tool_row["tool_id"])
            if not tool:
                continue
            interface = tool.get("interface") or {}
            # Single-function tools
            for param in interface.get("parameters", []):
                key = param.get("name", "")
                if key and key not in seen_inputs:
                    seen_inputs[key] = {
                        "name": key,
                        "type": param.get("type", "text"),
                        "description": param.get("description", ""),
                        "required": param.get("required", False),
                        "source": "tool",
                        "source_id": tool_row["tool_id"],
                    }
            # Multi-function tools
            for func in interface.get("functions", []):
                for param in func.get("parameters", []):
                    key = param.get("name", "")
                    if key and key not in seen_inputs:
                        seen_inputs[key] = {
                            "name": key,
                            "type": param.get("type", "text"),
                            "description": param.get("description", ""),
                            "required": param.get("required", False),
                            "source": "tool",
                            "source_id": tool_row["tool_id"],
                        }
            # Tool returns as outputs
            returns = interface.get("returns") or {}
            if returns.get("name"):
                key = returns["name"]
                if key not in seen_outputs:
                    seen_outputs[key] = {
                        "name": key,
                        "type": returns.get("type", "text"),
                        "description": returns.get("description", ""),
                        "source": "tool",
                        "source_id": tool_row["tool_id"],
                    }

        # 3. Agent's own I/O (added last, doesn't override)
        for inp in agent_inputs:
            key = inp["name"]
            if key not in seen_inputs:
                seen_inputs[key] = {
                    "name": key,
                    "type": inp.get("type", "text"),
                    "description": inp.get("description", ""),
                    "required": inp.get("required", False),
                    "source": "agent",
                    "source_id": agent_id,
                }

        for out in agent_outputs:
            key = out["name"]
            if key not in seen_outputs:
                seen_outputs[key] = {
                    "name": key,
                    "type": out.get("type", "text"),
                    "description": out.get("description", ""),
                    "required": out.get("required", True),
                    "source": "agent",
                    "source_id": agent_id,
                }

        return {
            "inputs": list(seen_inputs.values()),
            "outputs": list(seen_outputs.values()),
        }


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
