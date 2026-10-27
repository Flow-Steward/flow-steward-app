# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .prompt_agent_trace import *  # noqa: F403,F401  # NOSONAR


class PromptAgentToolMixin:
    """Tool configuration and policy helpers."""

    def _build_tool_config(
        self,
        state: dict,
        tool_ids: list[str] | None = None,
        skill: str | None = None,
    ) -> str:
        """Build tool configuration block from state for LLM context.

        Injects runtime values (project_name, task_id) that tools require as
        parameters so every agent receives them automatically.
        """
        _ = tool_ids, skill
        project_name = state.get("project_name", "")
        if not project_name:
            return ""

        lines = [TOOL_CONFIGURATION_HEADER]
        lines.append(f"- project_name: {project_name}")
        lines.append(f"- task_id: {state.get('task_id', '')}")
        return "\n".join(lines)

    @staticmethod
    def _strip_available_tools_section(system_prompt: str) -> str:
        """Remove registry manifest tool claims when runtime resolution found no tools."""
        lines = str(system_prompt or "").splitlines()
        if not any(line.strip() == "## Available Tools" for line in lines):
            return "\n".join(lines)

        kept: list[str] = []
        skipping_available_tools = False
        for line in lines:
            stripped = line.strip()
            if stripped == "## Available Tools":
                skipping_available_tools = True
                continue
            if skipping_available_tools and stripped.startswith("## "):
                skipping_available_tools = False
            if not skipping_available_tools:
                kept.append(line)
        return "\n".join(kept).strip()

    def _get_effective_tool_ids(
        self,
        skill: str = "",
        step: dict | None = None,
    ) -> list[str]:
        """Resolve effective tool ids from agent-attached tools plus step overrides.

        Tool ids are never inferred from the skill name.
        """
        _ = skill
        tool_ids = list(dict.fromkeys(self.manifest.tools))

        step_override = (
            step.get("override") if isinstance(step, dict) else getattr(step, "override", None)
        ) or {}
        for tool_id in step_override.get("add_tools") or []:
            if tool_id not in tool_ids:
                tool_ids.append(tool_id)
        for tool_id in step_override.get("remove_tools") or []:
            tool_ids = [existing for existing in tool_ids if existing != tool_id]
        normalized = [
            str(tool_id or "").strip() for tool_id in (tool_ids or []) if str(tool_id or "").strip()
        ]
        return list(dict.fromkeys(normalized))

    # ── Context Sections ────────────────────────────────────────────────────

    def _get_context_sections(self, skill: str, feedback: str | None) -> list[str]:
        """Determine which context sections to include based on agent manifest.

        Reads from manifest.prompts.context_sections (YAML-defined) with lookup order:
        1. skill-specific key (e.g. "review_summary")
        2. "default" key
        3. hardcoded fallback
        """
        _ = feedback
        cs = self.manifest.prompts.context_sections or {}

        # 1. Skill-specific override (a skill may declare its own sections)
        if skill in cs:
            return list(cs[skill])

        # 2. Default from manifest
        if "default" in cs:
            return list(cs["default"])

        # 3. Fallback if manifest has no context_sections at all
        return ["kb_context"]

    # ── Tool Resolution ─────────────────────────────────────────────────────

    @staticmethod
    def _tool_runtime_context_from_state(state: dict) -> dict:
        context = {"source": "prompt_agent.resolve_tools"}
        if not isinstance(state, dict):
            return context
        for key in (
            "account_id",
            "tenant_id",
            "project_id",
            "workspace_id",
            "job_id",
            "workflow_id",
        ):
            value = state.get(key)
            if value not in (None, ""):
                context[key] = value
        return context

    def _resolve_tools(
        self, state: dict, skill: str = "", tool_ids: list[str] | None = None
    ) -> list:
        """Convert tool_ids from manifest to LangChain tool objects.

        Uses the ToolResolver from RegistryManager for dynamic resolution.
        """
        tool_ids = list(tool_ids or self._get_effective_tool_ids(skill=skill))
        rm = None
        try:
            from core.application.registry.registry_manager import RegistryManager

            rm = RegistryManager.instance()
            tool_catalog = []
            fallback_resolve_ids = []
            registry_tools = getattr(rm, "tools", None)
            if registry_tools is not None and hasattr(registry_tools, "get_enabled_catalog_entry"):
                for tool_id in tool_ids:
                    entry = registry_tools.get_enabled_catalog_entry(tool_id)
                    if entry:
                        tool_catalog.append(entry)
                    else:
                        fallback_resolve_ids.append(tool_id)
            if tool_catalog and hasattr(rm.tool_resolver, "resolve_catalog"):
                runtime_context = self._tool_runtime_context_from_state(state)
                try:
                    tools = rm.tool_resolver.resolve_catalog(
                        tool_catalog,
                        runtime_context=runtime_context,
                    )
                except TypeError:
                    tools = rm.tool_resolver.resolve_catalog(tool_catalog)
                if fallback_resolve_ids:
                    tools.extend(rm.tool_resolver.resolve(fallback_resolve_ids))
            else:
                tools = rm.tool_resolver.resolve(tool_ids)
        except Exception as e:
            logger.warning(
                "PromptAgent[%s]: ToolResolver failed; continuing without registry-resolved tools: %s",
                self.manifest.agent_id,
                e,
            )
            tools = []

        tools = self._apply_skill_tool_policy(tools, skill, state)
        tools = self._dedupe_tools_by_name(tools)

        if tools:
            logger.info(
                "PromptAgent[%s]: resolved %d tools from %s",
                self.manifest.agent_id,
                len(tools),
                [getattr(t, "name", "?") for t in tools],
            )
            return tools

        return []

    @staticmethod
    def _dedupe_tools_by_name(tools: list) -> list:
        deduped: list = []
        seen: set[str] = set()
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            deduped.append(tool)
        return deduped

    @staticmethod
    def _tool_policy_mode(state: dict | None = None) -> str:
        project_policy = (state or {}).get("project_policy")
        if not isinstance(project_policy, dict):
            project_config = (state or {}).get("project_config") or {}
            if isinstance(project_config, dict):
                project_policy = project_config.get("project_policy")
        if not isinstance(project_policy, dict):
            return ""
        return str(project_policy.get("tool_policy_mode") or "").strip().lower()

    @staticmethod
    def _tool_metadata(tool: Any) -> dict[str, Any]:
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            return metadata
        return {}

    @classmethod
    def _restricted_tool_allowed(cls, tool: Any) -> bool:
        name = str(getattr(tool, "name", "") or "").strip()
        metadata = cls._tool_metadata(tool)
        category = str(metadata.get("category") or "").strip().lower()
        if category:
            return category in _RESTRICTED_ALLOWED_TOOL_CATEGORIES
        return name in _RESTRICTED_ALLOWED_TOOL_NAMES

    @classmethod
    def _build_execution_policy_state(
        cls,
        state: dict,
        *,
        tool_ids: list[str],
        resolved_tools: list,
    ) -> dict[str, Any]:
        mode = cls._tool_policy_mode(state)
        if not mode:
            return {}
        normalized_tool_ids = [
            str(tool_id or "").strip() for tool_id in (tool_ids or []) if str(tool_id or "").strip()
        ]
        resolved_tool_names = [
            str(getattr(tool, "name", "") or "").strip()
            for tool in (resolved_tools or [])
            if str(getattr(tool, "name", "") or "").strip()
        ]
        policy = {
            "tool_policy_mode": mode,
            "registry_tool_ids": normalized_tool_ids,
            "resolved_tool_names": list(dict.fromkeys(resolved_tool_names)),
        }
        if mode == "registry_only" and not resolved_tool_names:
            policy["tool_resolution_warning"] = (
                "## Tool Resolution Diagnostic\n"
                "- registry_only resolved no runtime tools. Do not claim or rely on "
                "Flow Steward tool calls unless they appear in resolved_tool_names."
            )
        return policy

    @staticmethod
    def _apply_skill_tool_policy(tools: list, skill: str, state: dict | None = None) -> list:
        """Enforce skill-level tool safety policy after resolver output."""
        tool_policy_mode = PromptAgent._tool_policy_mode(state)
        if tool_policy_mode in {"permissive", "allow_all", "", "registry_only"}:
            return tools
        if tool_policy_mode == "restricted":
            return [tool for tool in tools if PromptAgent._restricted_tool_allowed(tool)]
        return tools

    # ── Post-Execution Hooks ────────────────────────────────────────────────


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
