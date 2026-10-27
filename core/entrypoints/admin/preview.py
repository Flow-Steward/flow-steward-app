"""
Admin API routes — Prompt preview for agents and workflows.

Shows the fully assembled prompt that an agent would receive for a given
skill, including tool prompts, context templates, output format, etc.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.entrypoints.admin._registry_scope import assert_row_in_account_scope, request_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preview", tags=["preview"])


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _assert_agent_in_scope(registry, agent_id: str, request: Request) -> None:
    """Deny cross-account prompt preview using the account_id-bearing DB row.

    The in-memory ``AgentManifest`` does not carry account scope, so the owning
    account is read from the registry DB store. Global ('system') agents stay
    previewable; account-owned agents are only previewable in-account.
    """
    db = getattr(registry, "db_store", None)
    if db is None:
        return
    row = db.get_agent(agent_id)
    if row is not None:
        assert_row_in_account_scope(
            row, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )


def _assert_workflow_in_scope(registry, workflow_id: str, request: Request) -> None:
    """Deny cross-account workflow prompt preview using the DB workflow row."""
    db = getattr(registry, "db_store", None)
    if db is None:
        return
    row = db.get_workflow(workflow_id)
    if row is not None:
        assert_row_in_account_scope(
            row, request_scope(request), detail=f"Workflow '{workflow_id}' not found"
        )


def _primary_skill_for_agent(agent: object) -> str:
    skills = getattr(agent, "skills", None)
    if not isinstance(skills, list):
        return ""
    for item in skills:
        skill_id = str(item or "").strip()
        if skill_id:
            return skill_id
    return ""


# --------------------------------------------------------------------------- #
# Pydantic models                                                             #
# --------------------------------------------------------------------------- #


class AgentPreviewRequest(BaseModel):
    """Optional overrides for the preview."""

    project_name: str = "preview-project"
    task_summary: str = "Example task for prompt preview"
    task_id: str = "preview-001"
    # Extra state keys to inject
    extra_state: dict = {}


# --------------------------------------------------------------------------- #
# Agent prompt preview                                                         #
# --------------------------------------------------------------------------- #


@router.get(
    "/agent/{agent_id}",
    responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}},
)
def preview_agent_prompt(
    agent_id: str,
    request: Request,
    skill: Annotated[str, Query(description="Skill ID to preview")],
):
    """Preview the assembled prompt for an agent+skill combination.

    Returns the system prompt, user prompt, tool config block, and metadata
    about what components contributed to each section.
    """
    registry = _get_registry()
    _assert_agent_in_scope(registry, agent_id, request)
    agent = registry.agents.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    if skill not in agent.skills:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_id}' does not have skill '{skill}'. Available: {agent.skills}",
        )

    return _build_agent_preview(agent, skill, registry)


@router.post(
    "/agent/{agent_id}",
    responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}},
)
def preview_agent_prompt_with_state(
    agent_id: str,
    request: Request,
    skill: Annotated[str, Query(description="Skill ID to preview")],
    body: AgentPreviewRequest | None = None,
):
    """Preview with custom state values (for testing context template resolution)."""
    body = body or AgentPreviewRequest()
    registry = _get_registry()
    _assert_agent_in_scope(registry, agent_id, request)
    agent = registry.agents.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    if skill not in agent.skills:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_id}' does not have skill '{skill}'. Available: {agent.skills}",
        )

    return _build_agent_preview(
        agent,
        skill,
        registry,
        project_name=body.project_name,
        task_summary=body.task_summary,
        task_id=body.task_id,
        extra_state=body.extra_state,
    )


def _build_agent_preview(
    agent,
    skill: str,
    registry,
    project_name: str = "preview-project",
    task_summary: str = "Example task for prompt preview",
    task_id: str = "preview-001",
    extra_state: dict | None = None,
) -> dict:
    """Build the full prompt preview for an agent+skill."""
    from core.application.orchestration.agents.prompt_builder import PromptBuilder

    # Build a mock state with minimal required fields
    mock_state = {
        "task_id": task_id,
        "task_summary": task_summary,
        "project_name": project_name,
        "project_config": {
            "conventions": {},
        },
        "status": "in_progress",
        "messages": [],
        "agent_messages": [],
        "current_step": 0,
        "workflow_path": [],
        "needs_user_approval": False,
    }
    if extra_state:
        mock_state.update(extra_state)

    builder = PromptBuilder()
    system_prompt, user_prompt, version_hash = builder.build(
        agent=agent,
        skill=skill,
        step=None,
        tool_registry=registry.tools,
        state=mock_state,
        feedback=None,
    )

    # Build tool config block (same as PromptAgent._build_tool_config)
    tool_config = _build_tool_config_preview(mock_state, agent)

    # Collect metadata about what contributed to the prompt
    components = _extract_components(agent, skill, registry)

    return {
        "agent_id": agent.agent_id,
        "skill": skill,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "tool_config": tool_config,
        "full_user_prompt": (user_prompt + "\n\n" + tool_config) if tool_config else user_prompt,
        "version_hash": version_hash,
        "components": components,
    }


def _build_tool_config_preview(state: dict, agent) -> str:
    """Simplified version of PromptAgent._build_tool_config for preview."""
    _ = agent
    lines = ["## Tool Configuration"]
    project_name = state.get("project_name", "")

    if project_name:
        lines.append(f"- project_name: {project_name}")

    task_id = state.get("task_id", "")
    if task_id:
        lines.append(f"- task_id: {task_id}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def _extract_components(agent, skill: str, registry) -> list[dict]:
    """Extract metadata about each component that contributes to the prompt."""
    components = []

    # System prompt
    if agent.prompts.system:
        components.append(
            {
                "section": "system_prompt",
                "source": "agent",
                "label": f"Agent system prompt ({agent.display_name})",
                "length": len(agent.prompts.system),
            }
        )

    # Tool prompts
    for tool_id in agent.tools:
        tool = registry.tools.get_enabled_tool(tool_id)
        if tool and tool.prompt:
            components.append(
                {
                    "section": "system_prompt",
                    "source": "tool",
                    "label": f"Tool: {tool.display_name}",
                    "length": len(tool.prompt),
                }
            )

    # Skill prompt
    cap_prompt = agent.prompts.skill_prompts.get(skill, "")
    if cap_prompt:
        components.append(
            {
                "section": "user_prompt",
                "source": "skill",
                "label": f"Skill instructions ({skill})",
                "length": len(cap_prompt),
            }
        )

    # Examples
    examples = agent.prompts.examples.get(skill, [])
    if examples:
        components.append(
            {
                "section": "user_prompt",
                "source": "agent",
                "label": f"Examples ({len(examples)} examples)",
                "length": sum(len(str(e)) for e in examples),
            }
        )

    # Context template
    if agent.prompts.context_template:
        components.append(
            {
                "section": "user_prompt",
                "source": "agent",
                "label": "Context template",
                "length": len(agent.prompts.context_template),
            }
        )

    return components


# --------------------------------------------------------------------------- #
# Workflow preview                                                             #
# --------------------------------------------------------------------------- #


@router.get(
    "/workflow/{workflow_id}",
    responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}},
)
def preview_workflow(workflow_id: str, request: Request):
    """Preview the full workflow: its steps, groups and the prompts agents receive.

    For each step, shows what prompt the agent would receive for that skill.
    """
    registry = _get_registry()
    _assert_workflow_in_scope(registry, workflow_id, request)
    wf = registry.workflows.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")

    steps_preview = []
    for step in wf.steps:
        base = {
            "step_id": step.step_id,
            "step_kind": step.step_kind,
            "group_id": step.group_id,
            "lane_id": step.lane_id,
            "agent_id": step.agent,
            "optional": step.optional,
            "disabled": step.disabled,
        }
        if not step.is_agent_step:
            steps_preview.append(base)
            continue
        agent = registry.agents.get_agent(step.agent)
        if not agent:
            steps_preview.append({**base, "skill": "", "error": f"Agent '{step.agent}' not found"})
            continue
        resolved_skill_id = _primary_skill_for_agent(agent)
        try:
            preview = _build_agent_preview(agent, resolved_skill_id, registry)
            steps_preview.append(
                {
                    **base,
                    "agent_display_name": agent.display_name,
                    "skill": resolved_skill_id,
                    "has_override": step.override is not None,
                    "has_context_inject": step.context_inject is not None,
                    "system_prompt_length": len(preview["system_prompt"]),
                    "user_prompt_length": len(preview["full_user_prompt"]),
                    "components": preview["components"],
                    "version_hash": preview["version_hash"],
                    # Full prompts available on demand per step
                    "system_prompt": preview["system_prompt"],
                    "user_prompt": preview["full_user_prompt"],
                }
            )
        except Exception as e:
            steps_preview.append({**base, "skill": resolved_skill_id, "error": str(e)})

    return {
        "workflow_id": wf.workflow_id,
        "display_name": wf.display_name,
        "description": wf.description,
        "trigger": wf.trigger,
        "settings": wf.settings,
        "step_count": len(wf.steps),
        "steps": steps_preview,
        "groups": [group.dump() for group in wf.groups],
        "for_each": [item.dump() for item in wf.for_each],
    }
