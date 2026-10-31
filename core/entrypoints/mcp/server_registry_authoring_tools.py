"""API-backed agent, skill, enablement and model-option MCP tools.

Each tool forwards the caller's bearer to one admin HTTP route, whose
application service (``AgentCommandService``, ``SkillCommandService``, the
workflow metadata save, the model catalog) validates, authorizes and saves.
Nothing here decides anything; the docstrings are the MCP contract.

Common contract for the agent and skill tools:

- Agents and skills are account registry objects: every call needs an admin
  user, and a token needs ``admin:all`` (reads included), as on the HTTP routes.
- Disabled agents and skills are listed, read, changed and validated here like
  enabled ones; ``capability_search``/``capability_describe`` keep hiding them.
- A refused write is a tool error whose ``error.details`` is the route's error
  payload unchanged: ``registry_id_conflict`` / ``registry_name_conflict`` (409,
  with the visible ``matches`` and a ``suggested_id``/``suggested_display_name``
  that nothing reserves), ``Contract validation failed`` (400, with ``issues``)
  or a dependency conflict (409). Nothing is renamed or retried automatically.
- Fields have no version check: the last write wins. Read the object right
  before changing it; after an uncertain result, read it again before retrying.
"""

from __future__ import annotations

from typing import Any

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp

# --- schemas ---------------------------------------------------------------


@mcp.tool()
def agent_authoring_schema() -> str:
    """Return the JSON Schemas of the agent create and patch bodies and their rules.

    ``create.json_schema`` is the body of agent_create, ``patch.json_schema`` the
    ``changes`` of agent_update; ``patch.semantics`` lists how a patch merges
    (null never clears, config/prompts merge shallowly, an empty provider/model
    inherits the project route) and ``related`` names the readiness, enable and
    skill assignment operations.
    """
    return api_tool_response("agent_authoring_schema", locals())


@mcp.tool()
def skill_authoring_schema() -> str:
    """Return the JSON Schemas of the skill create and patch bodies and their rules.

    ``create.json_schema`` is the body of skill_create, ``patch.json_schema`` the
    ``changes`` of skill_update (every field sent replaces the stored value);
    ``related.inputs``/``related.outputs`` give the item schema of
    skill_inputs_set/skill_outputs_set.
    """
    return api_tool_response("skill_authoring_schema", locals())


# --- agents ----------------------------------------------------------------


@mcp.tool()
def agent_list(search: str | None = None, enabled_only: bool = False) -> str:
    """List the agents visible to the caller, disabled ones included by default.

    ``search`` matches id, name, description and category. Each row carries
    ``agent_id``, ``display_name`` and ``enabled``; read one with agent_get.
    """
    return api_tool_response("agent_list", locals())


@mcp.tool()
def agent_get(agent_id: str) -> str:
    """Read one agent's current editable settings, even when it is disabled.

    Returns the stored fields (display_name, description, category, prompts,
    config with provider/model, inputs, outputs, enabled, tags) with its
    assigned skills (skill_id, skill_prompt, sort_order) and tools. Read it right
    before agent_update: the next write replaces what it names.
    """
    return api_tool_response("agent_get", locals())


@mcp.tool()
def agent_create(agent: dict[str, Any]) -> str:
    """Create one agent under exactly ``agent['agent_id']``; it is saved disabled by default.

    ``agent`` is the create body of agent_authoring_schema; only the fields sent
    are set and every omitted one takes its schema default. ``skills`` may list
    ``{skill_id, skill_prompt}`` assignments. A taken id or name is a 409
    conflict that changes nothing; keep the same id after an uncertain result
    and read it with agent_get. Returns ``status``, ``agent_id`` and the
    readiness ``validation``. Enable it afterwards with agent_set_enabled.
    """
    return api_tool_response("agent_create", locals())


@mcp.tool()
def agent_update(agent_id: str, changes: dict[str, Any]) -> str:
    """Change only the fields present in ``changes`` (the agent patch body).

    null never clears a field; config and prompts merge shallowly; an empty
    config.provider/config.model makes the agent inherit the project route.
    skills and tools are refused: use agent_skill_assign/agent_skill_unassign.
    An empty ``changes`` answers ``no_change``. Returns ``status`` and the
    readiness ``validation``; a renamed display_name may be a 409 name conflict.
    """
    return api_tool_response("agent_update", locals())


@mcp.tool()
def agent_validate(agent_id: str) -> str:
    """Check one agent's readiness (enable, test, invoke) without changing it.

    Returns ``validation`` with ``valid``, ``errors`` and ``warnings``. A valid
    result is a static check only: it does not prove that a run succeeds.
    """
    return api_tool_response("agent_validate", locals())


@mcp.tool()
def agent_set_enabled(agent_id: str, enabled: bool) -> str:
    """Set the agent's enabled state to exactly ``enabled`` (never a toggle).

    Enabling requires a valid readiness result, otherwise 400 'Contract
    validation failed' with the issues; disabling is refused with 409 while
    enabled workflows run the agent.
    """
    return api_tool_response("agent_set_enabled", locals())


@mcp.tool()
def agent_skill_assign(agent_id: str, skill_id: str, skill_prompt: str | None = None) -> str:
    """Assign a skill to an agent, with optional assignment instructions.

    ``skill_prompt`` is the agent-specific instruction for this skill; omitted,
    the assignment has none. A skill already assigned is a 409 conflict, an
    unknown skill a 404; a skill the agent's contract cannot use is refused with
    its readiness issues. Returns the agent's assignments in order.
    """
    return api_tool_response("agent_skill_assign", locals())


@mcp.tool()
def agent_skill_unassign(agent_id: str, skill_id: str) -> str:
    """Remove a skill from an agent; returns the remaining assignments in order."""
    return api_tool_response("agent_skill_unassign", locals())


@mcp.tool()
def agent_skill_set_instructions(agent_id: str, skill_id: str, skill_prompt: str) -> str:
    """Replace the assignment instructions (skill_prompt) of one assigned skill."""
    return api_tool_response("agent_skill_set_instructions", locals())


# --- skills ----------------------------------------------------------------


@mcp.tool()
def skill_list(search: str | None = None, enabled_only: bool = False) -> str:
    """List the skills visible to the caller, disabled ones included by default."""
    return api_tool_response("skill_list", locals())


@mcp.tool()
def skill_get(skill_id: str) -> str:
    """Read one skill's current editable settings, even when it is disabled.

    Returns the stored fields (display_name, description, category, skill_type,
    instructions, prompt_template, expected_inputs, expected_outputs, enabled,
    tags). Read it right before skill_update.
    """
    return api_tool_response("skill_get", locals())


@mcp.tool()
def skill_create(skill: dict[str, Any]) -> str:
    """Create one skill under exactly ``skill['skill_id']``; it is saved disabled by default.

    ``skill`` is the create body of skill_authoring_schema; omitted fields take
    their schema defaults. A taken id or name is a 409 conflict that changes
    nothing. Returns ``status``, ``skill_id`` and the readiness ``validation``.
    A skill is instructions for an agent: it does not create an integration.
    """
    return api_tool_response("skill_create", locals())


@mcp.tool()
def skill_update(skill_id: str, changes: dict[str, Any]) -> str:
    """Change only the fields present in ``changes`` (the skill patch body).

    null never clears a field and every field sent replaces the stored value.
    expected_inputs/expected_outputs change through skill_inputs_set and
    skill_outputs_set. An empty ``changes`` answers ``no_change``.
    """
    return api_tool_response("skill_update", locals())


@mcp.tool()
def skill_inputs_set(skill_id: str, inputs: list[dict[str, Any]]) -> str:
    """Replace the skill's whole expected input list (items: name, type, description, required).

    Invalid items are refused with their issues and nothing changes. Agents using
    the skill pick the new contract up. Returns the saved list.
    """
    return api_tool_response("skill_inputs_set", locals())


@mcp.tool()
def skill_outputs_set(skill_id: str, outputs: list[dict[str, Any]]) -> str:
    """Replace the skill's whole expected output list (items: name, type, description, required).

    Invalid items are refused with their issues and nothing changes. Agents using
    the skill pick the new contract up. Returns the saved list.
    """
    return api_tool_response("skill_outputs_set", locals())


@mcp.tool()
def skill_validate(skill_id: str) -> str:
    """Check one skill's readiness (enable, agent assignment) without changing it.

    A valid result is a static check only: it does not prove that a run succeeds.
    """
    return api_tool_response("skill_validate", locals())


@mcp.tool()
def skill_set_enabled(skill_id: str, enabled: bool) -> str:
    """Set the skill's enabled state to exactly ``enabled`` (never a toggle).

    Enabling requires a valid readiness result; disabling is refused with 409
    while enabled workflows depend on an agent using the skill.
    """
    return api_tool_response("skill_set_enabled", locals())


# --- workflows and models --------------------------------------------------


@mcp.tool()
def workflow_set_enabled(workflow_id: str, enabled: bool, expected_revision: int) -> str:
    """Set the workflow's enabled state to exactly ``enabled`` at ``expected_revision``.

    Read the revision with workflow_definition first. A stale revision is a 409
    conflict to reload, never an overwrite. Enabling requires a valid readiness
    result (400 with the issues otherwise) and compatible callers. Needs the
    authority of workflow_apply_changes (admin user, ``admin:all`` token).
    Enabling does not run the workflow.
    """
    return api_tool_response("workflow_set_enabled", locals())


@mcp.tool()
def model_options() -> str:
    """List the LLM providers and models the caller's account may select, without secrets.

    ``provider_id`` and ``model_id`` are the values of an agent's
    ``config.provider`` and ``config.model``; leave both empty to inherit the
    project route.
    """
    return api_tool_response("model_options", locals())


__all__ = [
    name
    for name in globals()
    if name.startswith(("agent_", "skill_", "workflow_set_", "model_options"))
]
