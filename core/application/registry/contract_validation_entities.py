# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

from core.application.auth.config import DEFAULT_ACCOUNT_ID

from .contract_validation_invoke import *  # noqa: F403,F401  # NOSONAR


from .contract_item_validation import validate_contract_items


def validate_skill_readiness(db, skill_id: str) -> dict:
    from .workflow_lookup import get_registry_artifact_row

    cap = get_registry_artifact_row(
        db,
        artifact_kind="skill",
        id_key="skill_id",
        artifact_id=skill_id,
        platform_getter="get_skill",
    )
    if not cap:
        return {
            "valid": False,
            "errors": [_issue(code="skill.not_found", message=f"Skill '{skill_id}' not found")],
            "warnings": [],
        }

    errors: list[dict] = []
    warnings: list[dict] = []
    inputs, input_issues = validate_contract_items(
        cap.get("_inputs") or cap.get("expected_inputs") or [],
        path_prefix="skill.expected_inputs",
        default_required=False,
    )
    outputs, output_issues = validate_contract_items(
        cap.get("_outputs") or cap.get("expected_outputs") or [],
        path_prefix="skill.expected_outputs",
        default_required=True,
        db=db,
    )
    errors.extend(input_issues)
    errors.extend(output_issues)

    if not outputs:
        errors.append(
            _issue(
                code="skill.outputs_missing",
                message="Skill must declare at least one output contract field",
                path="skill.expected_outputs",
            )
        )

    for idx, item in enumerate(inputs):
        if (
            str(item.get("type") or "") in {"json", "list", "object", "array"}
            and not str(item.get("contract_id") or "").strip()
        ):
            warnings.append(
                _issue(
                    code="skill.input_data_contract_missing",
                    message=(
                        f"Structured input '{item['name']}' has no data contract; "
                        "auto-wiring and schema-safe structured output will stay generic"
                    ),
                    path=f"skill.expected_inputs[{idx}].contract_id",
                    severity="warning",
                )
            )
    for idx, item in enumerate(outputs):
        if (
            str(item.get("type") or "") in {"json", "list", "object", "array"}
            and not str(item.get("contract_id") or "").strip()
        ):
            warnings.append(
                _issue(
                    code="skill.output_data_contract_missing",
                    message=(
                        f"Structured output '{item['name']}' has no data contract; "
                        "structured output schema will stay generic for this field"
                    ),
                    path=f"skill.expected_outputs[{idx}].contract_id",
                    severity="warning",
                )
            )

    if not inputs:
        warnings.append(
            _issue(
                code="skill.inputs_empty",
                message="Skill has no declared inputs",
                path="skill.expected_inputs",
                severity="warning",
            )
        )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_tool_readiness(db, tool_id: str) -> dict:
    from .workflow_lookup import get_registry_artifact_row

    tool = get_registry_artifact_row(
        db,
        artifact_kind="tool",
        id_key="tool_id",
        artifact_id=tool_id,
        platform_getter="get_tool",
    )
    if not isinstance(tool, dict):
        return {
            "valid": False,
            "errors": [_issue(code="tool.not_found", message=f"Tool '{tool_id}' not found")],
            "warnings": [],
        }

    errors: list[dict] = []
    warnings: list[dict] = []
    normalized_tool_id = str(tool.get("tool_id") or "").strip()
    display_name = str(tool.get("display_name") or "").strip()
    version = str(tool.get("version") or "").strip()
    interface_obj = _to_object(tool.get("interface"))
    implementation_obj = _to_object(tool.get("implementation"))
    config_obj = _to_object(tool.get("config"))
    tags = tool.get("tags")

    if normalized_tool_id != str(tool_id or "").strip():
        errors.append(
            _issue(
                code="tool.id_mismatch",
                message=f"Tool payload id '{normalized_tool_id}' does not match '{tool_id}'",
                path="tool.tool_id",
            )
        )
    if not normalized_tool_id:
        errors.append(
            _issue(
                code="tool.id_missing",
                message="Tool id is required",
                path="tool.tool_id",
            )
        )
    if not display_name:
        errors.append(
            _issue(
                code="tool.display_name_missing",
                message="Tool display_name is required",
                path="tool.display_name",
            )
        )
    if not version:
        warnings.append(
            _issue(
                code="tool.version_missing",
                message="Tool version is empty",
                path="tool.version",
                severity="warning",
            )
        )
    if not isinstance(tags, list):
        errors.append(
            _issue(
                code="tool.tags_invalid",
                message="Tool tags must be a list",
                path="tool.tags",
            )
        )
    if not isinstance(interface_obj, dict):
        errors.append(
            _issue(
                code="tool.interface_invalid",
                message="Tool interface must be an object",
                path="tool.interface",
            )
        )
    if not isinstance(implementation_obj, dict):
        errors.append(
            _issue(
                code="tool.implementation_invalid",
                message="Tool implementation must be an object",
                path=PATH_TOOL_IMPLEMENTATION,
            )
        )
    if not isinstance(config_obj, dict):
        errors.append(
            _issue(
                code="tool.config_invalid",
                message="Tool config must be an object",
                path="tool.config",
            )
        )
    enabled = bool(tool.get("enabled", True))
    implementation_kind = (
        str(implementation_obj.get("kind") or implementation_obj.get("type") or "").strip().lower()
        if implementation_obj
        else ""
    )
    if enabled and not implementation_obj:
        errors.append(
            _issue(
                code="tool.implementation_empty",
                message="Enabled tool has empty implementation payload",
                path=PATH_TOOL_IMPLEMENTATION,
            )
        )
    if enabled and implementation_obj and not implementation_kind:
        errors.append(
            _issue(
                code="tool.implementation_kind_missing",
                message="Enabled tool must set implementation.kind",
                path="tool.implementation.kind",
            )
        )
    if enabled and implementation_obj and implementation_kind in {"internal", "mcp", "extension"}:
        try:
            from core.application.orchestration.tools.tool_resolver import ToolResolver
            from core.application.registry.tool_manifest import ToolManifest

            manifest = ToolManifest.from_db_row(tool)
            resolver = ToolResolver()
            resolver.register_from_manifest(manifest)
            resolved = list(resolver.list_registered().get(normalized_tool_id) or [])
            resolve_errors = [
                str(item or "").strip()
                for item in (resolver.get_errors() or [])
                if normalized_tool_id in str(item or "")
            ]
            if resolve_errors:
                for idx, err in enumerate(resolve_errors):
                    errors.append(
                        _issue(
                            code="tool.runtime_unresolvable",
                            message=f"Enabled tool failed runtime resolver check: {err}",
                            path=f"tool.implementation[{idx}]",
                        )
                    )
            elif not resolved:
                errors.append(
                    _issue(
                        code="tool.runtime_unresolvable",
                        message="Enabled tool resolves to zero callable functions in ToolResolver",
                        path=PATH_TOOL_IMPLEMENTATION,
                    )
                )
        except Exception as exc:
            errors.append(
                _issue(
                    code="tool.runtime_validation_failed",
                    message=f"Enabled tool runtime validation failed: {exc}",
                    path=PATH_TOOL_IMPLEMENTATION,
                )
            )
    elif (
        enabled
        and implementation_obj
        and implementation_kind not in {"internal", "mcp", "native", "extension"}
    ):
        errors.append(
            _issue(
                code="tool.runtime_validation_unsupported",
                message=(
                    "Enabled tool uses unsupported implementation.kind "
                    f"'{implementation_kind}'. Supported kinds: internal, mcp, native, extension."
                ),
                path="tool.implementation.kind",
            )
        )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_guardrail_readiness(
    db,
    guardrail_id: str | int,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    normalized_id = str(guardrail_id or "").strip()
    if not normalized_id:
        return {
            "valid": False,
            "errors": [
                _issue(
                    code="guardrail.id_invalid", message=f"Invalid guardrail id '{guardrail_id}'"
                )
            ],
            "warnings": [],
        }
    guardrail = db.get_guardrail(normalized_id, account_id=account_id)
    if not isinstance(guardrail, dict):
        return {
            "valid": False,
            "errors": [
                _issue(code="guardrail.not_found", message=f"Guardrail '{normalized_id}' not found")
            ],
            "warnings": [],
        }

    errors: list[dict] = []
    warnings: list[dict] = []
    mode = str(guardrail.get("mode") or "").strip().lower()
    severity = str(guardrail.get("severity") or "").strip().lower()
    rule_type = str(guardrail.get("rule_type") or "").strip()
    display_name = str(guardrail.get("display_name") or "").strip()
    config_obj = _to_object(guardrail.get("config"))
    scoped_agents = list(guardrail.get("_agents") or [])

    if not scoped_agents:
        errors.append(
            _issue(
                code="guardrail.agent_scope_empty",
                message="Guardrail must be assigned to at least one agent",
                path="guardrail.agents",
            )
        )

    if not display_name:
        errors.append(
            _issue(
                code="guardrail.display_name_missing",
                message="Guardrail display_name is required",
                path="guardrail.display_name",
            )
        )
    if mode not in {"deterministic", "semantic"}:
        errors.append(
            _issue(
                code="guardrail.mode_invalid",
                message=f"Guardrail mode '{mode}' is invalid",
                path="guardrail.mode",
            )
        )
    if severity not in {"error", "warning"}:
        errors.append(
            _issue(
                code="guardrail.severity_invalid",
                message=f"Guardrail severity '{severity}' is invalid",
                path="guardrail.severity",
            )
        )
    if not rule_type:
        errors.append(
            _issue(
                code="guardrail.rule_type_missing",
                message="Guardrail rule_type is required",
                path="guardrail.rule_type",
            )
        )
    if not isinstance(config_obj, dict):
        errors.append(
            _issue(
                code="guardrail.config_invalid",
                message="Guardrail config must be an object",
                path="guardrail.config",
            )
        )
    if mode == "semantic" and not str(guardrail.get("prompt_text") or "").strip():
        warnings.append(
            _issue(
                code="guardrail.prompt_text_missing",
                message="Semantic guardrail has empty prompt_text",
                path="guardrail.prompt_text",
                severity="warning",
            )
        )

    for idx, agent_id in enumerate(scoped_agents):
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            errors.append(
                _issue(
                    code="guardrail.agent_id_missing",
                    message="Guardrail agent scope contains an empty agent_id",
                    path=f"guardrail.agents[{idx}]",
                )
            )
            continue
        if not isinstance(db.get_agent(normalized_agent_id), dict):
            errors.append(
                _issue(
                    code="guardrail.agent_missing",
                    message=f"Guardrail references missing agent '{normalized_agent_id}'",
                    path=f"guardrail.agents[{idx}]",
                )
            )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_agent_skill_compatibility(db, agent_id: str, skill_id: str) -> dict:
    """Validate the compatibility of one agent<->skill pair."""
    errors: list[dict] = []
    warnings: list[dict] = []

    from .workflow_lookup import get_registry_artifact_row

    agent = get_registry_artifact_row(
        db,
        artifact_kind="agent",
        id_key="agent_id",
        artifact_id=agent_id,
        platform_getter="get_agent",
    )
    if not isinstance(agent, dict):
        errors.append(
            _issue(
                code="agent.not_found",
                message=f"Agent '{agent_id}' not found",
                path="agent",
            )
        )
        return {"valid": False, "errors": errors, "warnings": warnings}
    # Disabled agents must remain editable: the supported lifecycle is to
    # configure their skill/tool contract first and enable them only after the
    # resulting readiness check passes. Runtime entrypoints enforce enabled
    # state separately when an agent is invoked.

    cap_row = get_registry_artifact_row(
        db,
        artifact_kind="skill",
        id_key="skill_id",
        artifact_id=skill_id,
        platform_getter="get_skill",
    )
    if not isinstance(cap_row, dict):
        errors.append(
            _issue(
                code="skill.not_found",
                message=f"Skill '{skill_id}' not found",
                path="skill",
            )
        )
        return {"valid": False, "errors": errors, "warnings": warnings}
    if not bool(cap_row.get("enabled", True)):
        errors.append(
            _issue(
                code="skill.disabled",
                message=f"Skill '{skill_id}' is disabled",
                path="skill.enabled",
            )
        )

    mapped_skills = list(agent.get("_skills") or [])
    get_agent_skills = getattr(db, "get_agent_skills", None)
    if not mapped_skills and callable(get_agent_skills):
        mapped_skills = list(get_agent_skills(agent_id) or [])
    mapped_cap_ids = {
        str(item.get("skill_id") or "").strip() for item in mapped_skills if isinstance(item, dict)
    }
    if skill_id not in mapped_cap_ids:
        errors.append(
            _issue(
                code="agent.skill_not_mapped",
                message=f"Agent '{agent_id}' is not mapped to skill '{skill_id}'",
                path=PATH_AGENT_SKILLS,
            )
        )

    cap_validation = validate_skill_readiness(db, skill_id)
    for issue in cap_validation.get("errors") or []:
        prefixed = dict(issue)
        prefixed["path"] = f"skill.{issue.get('path') or ''}".strip(".")
        errors.append(prefixed)
    for issue in cap_validation.get("warnings") or []:
        prefixed = dict(issue)
        prefixed["path"] = f"skill.{issue.get('path') or ''}".strip(".")
        warnings.append(prefixed)

    mapped_tools = list(agent.get("_tools") or [])
    get_agent_tools = getattr(db, "get_agent_tools", None)
    if not mapped_tools and callable(get_agent_tools):
        mapped_tools = list(get_agent_tools(agent_id) or [])
    direct_tools = {
        str(item.get("tool_id") or "").strip() for item in mapped_tools if isinstance(item, dict)
    }
    for tool_id in sorted(direct_tools):
        if not tool_id:
            continue
        tool = get_registry_artifact_row(
            db,
            artifact_kind="tool",
            id_key="tool_id",
            artifact_id=tool_id,
            platform_getter="get_tool",
        )
        if not isinstance(tool, dict):
            errors.append(
                _issue(
                    code="agent.tool_missing",
                    message=f"Agent '{agent_id}' maps missing tool '{tool_id}'",
                    path=PATH_AGENT_TOOLS,
                )
            )
            continue
        if not bool(tool.get("enabled", True)):
            errors.append(
                _issue(
                    code="agent.tool_disabled",
                    message=f"Agent '{agent_id}' maps disabled tool '{tool_id}'",
                    path=PATH_AGENT_TOOLS,
                )
            )
            continue
        tool_validation = validate_tool_readiness(db, tool_id)
        for issue in tool_validation.get("errors") or []:
            if not isinstance(issue, dict):
                continue
            issue_code = str(issue.get("code") or "error").strip()
            issue_message = str(issue.get("message") or "").strip()
            errors.append(
                _issue(
                    code=f"agent.tool_contract.{issue_code}",
                    message=f"Agent '{agent_id}' tool '{tool_id}' is not runtime-ready: {issue_message}",
                    path=PATH_AGENT_TOOLS,
                )
            )
        for issue in tool_validation.get("warnings") or []:
            if not isinstance(issue, dict):
                continue
            issue_code = str(issue.get("code") or "warning").strip()
            issue_message = str(issue.get("message") or "").strip()
            warnings.append(
                _issue(
                    code=f"agent.tool_contract.{issue_code}",
                    message=f"Agent '{agent_id}' tool '{tool_id}' warning: {issue_message}",
                    path=PATH_AGENT_TOOLS,
                    severity="warning",
                )
            )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_agent_readiness(db, agent_id: str) -> dict:
    from .workflow_lookup import get_registry_artifact_row

    agent = get_registry_artifact_row(
        db,
        artifact_kind="agent",
        id_key="agent_id",
        artifact_id=agent_id,
        platform_getter="get_agent",
    )
    if not agent:
        return {
            "valid": False,
            "errors": [_issue(code="agent.not_found", message=f"Agent '{agent_id}' not found")],
            "warnings": [],
        }

    errors: list[dict] = []
    warnings: list[dict] = []
    skills = list(agent.get("_skills") or [])
    if not skills:
        is_projected_agent = bool(
            str(agent.get("source_install_id") or "").strip()
            or str(agent.get("owner_install_id") or "").strip()
            or str(agent.get("projection_mode") or "").strip().lower() == "derived"
        )
        if is_projected_agent:
            warnings.append(
                _issue(
                    code="agent.skills_missing_projected",
                    message=(
                        "Extension-owned projected agent has no explicit skill mappings; "
                        "runtime will treat it as a self-contained prompt agent."
                    ),
                    path=PATH_AGENT_SKILLS,
                    severity="warning",
                )
            )
        else:
            errors.append(
                _issue(
                    code="agent.skills_missing",
                    message="Agent must have at least one skill mapping before enabling",
                    path=PATH_AGENT_SKILLS,
                )
            )

    for idx, cap in enumerate(skills):
        cap_id = str(cap.get("skill_id") or "").strip()
        if not cap_id:
            errors.append(
                _issue(
                    code="agent.skill_id_missing",
                    message="Skill mapping is missing skill_id",
                    path=_indexed_path(PATH_AGENT_SKILLS, idx),
                )
            )
            continue

        cap_row = get_registry_artifact_row(
            db,
            artifact_kind="skill",
            id_key="skill_id",
            artifact_id=cap_id,
            platform_getter="get_skill",
        )
        if not cap_row:
            errors.append(
                _issue(
                    code="agent.skill_missing",
                    message=f"Agent references unknown skill '{cap_id}'",
                    path=_indexed_path(PATH_AGENT_SKILLS, idx),
                )
            )
            continue

        if not bool(cap_row.get("enabled", True)):
            errors.append(
                _issue(
                    code="agent.skill_disabled",
                    message=f"Skill '{cap_id}' is disabled",
                    path=_indexed_path(PATH_AGENT_SKILLS, idx),
                )
            )

        pair_validation = validate_agent_skill_compatibility(db, agent_id, cap_id)
        for issue in pair_validation.get("errors") or []:
            prefixed = dict(issue)
            prefixed["path"] = _indexed_path(PATH_AGENT_SKILLS, idx, str(issue.get("path") or ""))
            errors.append(prefixed)
        for issue in pair_validation.get("warnings") or []:
            prefixed = dict(issue)
            prefixed["path"] = _indexed_path(PATH_AGENT_SKILLS, idx, str(issue.get("path") or ""))
            warnings.append(prefixed)

    if len(skills) > 1:
        warnings.append(
            _issue(
                code="agent.multiple_skills",
                message=(
                    "Agent has multiple skills; ensure all contracts are intentionally compatible "
                    "with this agent's runtime behavior"
                ),
                path=PATH_AGENT_SKILLS,
                severity="warning",
            )
        )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
