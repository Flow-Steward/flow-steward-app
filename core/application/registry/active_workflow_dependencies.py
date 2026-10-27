"""Resolve enabled workflows that depend on registry execution capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID

_TOKEN_CONNECTOR = "connector."


def lock_workflow_dependency_inventory(cur: Any) -> None:
    """Serialize lifecycle impact reads against workflow/agent dependency writers."""

    cur.execute(
        "LOCK TABLE registry_tools, registry_workflows, registry_workflow_groups, registry_workflow_for_each, "
        "registry_workflow_steps, registry_agent_tools IN SHARE MODE"
    )


def _participant_bindings(workflow: dict[str, Any]) -> dict[str, str]:
    participants = workflow.get("participants")
    if not isinstance(participants, list):
        settings = workflow.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        participants = settings.get("participants")
    if not isinstance(participants, list):
        return {}
    return {
        str(row.get("participant_key") or "").strip(): str(
            row.get("agent_id") or row.get("agent") or ""
        ).strip()
        for row in participants
        if isinstance(row, dict)
        and str(row.get("participant_key") or "").strip()
        and str(row.get("agent_id") or row.get("agent") or "").strip()
    }


def _nested_steps(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _nested_steps(item)
        return
    if not isinstance(value, dict):
        return
    if any(key in value for key in ("step_kind", "agent_id", "participant_key", "connector_id")):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _nested_steps(child)


def _workflow_steps(workflow: dict[str, Any]) -> Iterator[dict[str, Any]]:
    # Every step of a workflow, grouped or root, is listed in workflow["steps"].
    steps = workflow.get("steps")
    yield from _nested_steps(steps if isinstance(steps, list) else [])


def _connector_uses_tool(connector_id: str, tool_id: str) -> bool:
    normalized = str(connector_id or "").strip().removeprefix(_TOKEN_CONNECTOR)
    expected = str(tool_id or "").strip().removeprefix(_TOKEN_CONNECTOR)
    return bool(expected) and (normalized == expected or normalized.startswith(f"{expected}::"))


def workflow_registry_tool_issues(
    db: Any,
    workflow: dict[str, Any],
    *,
    account_id: str = "",
) -> list[dict[str, str]]:
    """Resolve missing, disabled, or unavailable direct/agent tool dependencies."""

    get_tool = getattr(db, "get_tool", None)
    if not callable(get_tool):
        return []
    get_agent = getattr(db, "get_agent", None)
    expected_account = str(account_id or workflow.get("account_id") or "").strip()
    required: dict[str, set[str]] = {}
    bindings = _participant_bindings(workflow)
    for step in _workflow_steps(workflow):
        connector_id = str(step.get("connector_id") or "").strip()
        # Only the explicit connector.<registry_tool_id> form denotes a registry
        # tool dependency. Extension runtime connectors use their own projected
        # IDs and are validated by the extension contract/runtime path.
        if connector_id.startswith(_TOKEN_CONNECTOR):
            tool_id = connector_id.removeprefix(_TOKEN_CONNECTOR).split("::", 1)[0].strip()
            if tool_id:
                required.setdefault(tool_id, set()).add("direct")
        explicit_agent = str(step.get("agent_id") or step.get("agent") or "").strip()
        participant_key = str(step.get("participant_key") or "").strip()
        agent_id = str(bindings.get(participant_key) or explicit_agent).strip()
        agent = get_agent(agent_id) if agent_id and callable(get_agent) else None
        if not isinstance(agent, dict):
            continue
        raw_tools = agent.get("tools") or agent.get("_tools") or []
        for value in raw_tools if isinstance(raw_tools, list) else []:
            tool_id = str(value.get("tool_id") if isinstance(value, dict) else value).strip()
            # Extension-owned agent capabilities resolve through the extension
            # runtime rather than registry_tools.
            if tool_id and not tool_id.startswith("extension."):
                required.setdefault(tool_id, set()).add("agent")

    issues: list[dict[str, str]] = []
    for tool_id, usages in sorted(required.items()):
        tool = get_tool(tool_id)
        reason = ""
        if not isinstance(tool, dict) or (
            expected_account
            and str(tool.get("account_id") or expected_account).strip() != expected_account
        ):
            reason = "missing"
        elif not bool(tool.get("enabled", True)):
            reason = "disabled"
        elif str(tool.get("availability_status") or "available").strip().lower() != "available":
            reason = "unavailable"
        if reason:
            for usage in sorted(usages):
                issues.append({"tool_id": tool_id, "usage": usage, "reason": reason})
    return issues


def enabled_workflow_dependencies(
    db: Any,
    *,
    agent_ids: Iterable[str] = (),
    tool_id: str = "",
) -> list[str]:
    """Return enabled workflows using agents or a direct connector tool.

    Skills deliberately have no direct workflow edge. Callers resolve the
    agents using a skill and pass those agent ids here. Tools may use the same
    transitive path and additionally match connector steps directly.
    """

    target_agents = {str(agent_id or "").strip() for agent_id in agent_ids}
    target_agents.discard("")
    dependencies: set[str] = set()
    workflows = db.list_workflows(enabled_only=True, include_definitions=True)
    for workflow in workflows or []:
        if not isinstance(workflow, dict):
            continue
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        bindings = _participant_bindings(workflow)
        for step in _workflow_steps(workflow):
            explicit_agent = str(step.get("agent_id") or step.get("agent") or "").strip()
            participant_key = str(step.get("participant_key") or "").strip()
            resolved_agent = str(bindings.get(participant_key) or explicit_agent).strip()
            if resolved_agent and resolved_agent in target_agents:
                dependencies.add(workflow_id)
                break
            if tool_id and _connector_uses_tool(str(step.get("connector_id") or ""), tool_id):
                dependencies.add(workflow_id)
                break
    return sorted(dependencies)


def workflow_dependency_impacts(
    db: Any,
    *,
    account_id: str,
    connector_id: str = "",
    tool_agent_ids: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return account-scoped workflow impact for connector-owned tools.

    Unlike the mutation guards above, delete preview must include disabled and
    otherwise inactive workflows because their definitions will still fail if
    started after the connector is removed.
    """

    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    normalized_connector_id = str(connector_id or "").strip()
    agents_by_tool = {
        str(tool_id or "").strip(): {
            str(agent_id or "").strip() for agent_id in agent_ids if str(agent_id or "").strip()
        }
        for tool_id, agent_ids in (tool_agent_ids or {}).items()
        if str(tool_id or "").strip()
    }
    tool_ids = sorted(agents_by_tool)
    impacts: list[dict[str, Any]] = []
    for workflow in db.list_workflows(enabled_only=False, include_definitions=True) or []:
        if not isinstance(workflow, dict):
            continue
        owner_account_id = (
            str(workflow.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        )
        if owner_account_id != normalized_account_id:
            continue
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        matched_tool_ids: set[str] = set()
        matched_connector = False
        bindings = _participant_bindings(workflow)
        for step in _workflow_steps(workflow):
            explicit_agent = str(step.get("agent_id") or step.get("agent") or "").strip()
            participant_key = str(step.get("participant_key") or "").strip()
            resolved_agent = str(bindings.get(participant_key) or explicit_agent).strip()
            if resolved_agent:
                matched_tool_ids.update(
                    tool_id
                    for tool_id, agent_ids in agents_by_tool.items()
                    if resolved_agent in agent_ids
                )

            step_connector_id = str(step.get("connector_id") or "").strip()
            explicit_tool_id = str(step.get("tool_id") or "").strip()
            if normalized_connector_id and step_connector_id == normalized_connector_id:
                matched_connector = True
                matched_tool_ids.update(tool_ids)
            for tool_id in tool_ids:
                if explicit_tool_id == tool_id or _connector_uses_tool(step_connector_id, tool_id):
                    matched_tool_ids.add(tool_id)

        if not matched_tool_ids and not matched_connector:
            continue
        enabled = bool(workflow.get("enabled", True))
        impacts.append(
            {
                "workflow_id": workflow_id,
                "display_name": str(
                    workflow.get("display_name") or workflow.get("name") or workflow_id
                ).strip()
                or workflow_id,
                "status": str(
                    workflow.get("status") or ("enabled" if enabled else "disabled")
                ).strip(),
                "enabled": enabled,
                "tool_ids": sorted(matched_tool_ids),
                "via_connector": matched_connector,
            }
        )
    return sorted(impacts, key=lambda row: str(row.get("workflow_id") or ""))
