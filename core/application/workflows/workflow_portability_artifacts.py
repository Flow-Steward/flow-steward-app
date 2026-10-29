# mypy: ignore-errors
"""The agents, skills, data contracts and guardrails a workflow bundle carries.

`definitions` makes a bundle self-contained: an import elsewhere creates these
entities instead of asking for each one to be mapped by hand. Only what this
installation owns travels; an extension's projection and another account's row
stay references in `dependencies`. Every record is written from an allowlist:
ownership, timestamps, storage ids and the model route never leave.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.application.registry.projected_artifact_rows import _durable_projection_install_id
from core.application.workflows.workflow_portability_common import (
    DEFINITIONS_BUNDLE_KEY,
    WorkflowPortabilityError,
    _as_dict,
    _as_list,
    _call_db_get,
    _call_db_list,
    _normalize_participants,
    _text,
    _workflow_steps,
)

DEFINITIONS_KEY = DEFINITIONS_BUNDLE_KEY
DEFINITION_SECTIONS = ("agents", "skills", "data_contracts", "guardrails")
#: Section -> (dependency type, id field).
DEFINITION_KINDS = {
    "agents": ("agent", "agent_id"),
    "skills": ("skill", "skill_id"),
    "data_contracts": ("data_contract", "contract_id"),
    "guardrails": ("guardrail", "id"),
}
#: Config keys that point at one installation's model route. An agent without
#: them runs on the target project's configured model.
MODEL_ROUTE_CONFIG_KEYS = frozenset(
    {"provider", "model", "model_provider_id", "provider_id", "model_id", "model_route"}
)
GUARDRAIL_SCOPES = ("global", "project")

_IO_FIELDS = ("name", "type", "description", "required")
AGENT_FIELDS = (
    "agent_id",
    "version",
    "display_name",
    "description",
    "enabled",
    "category",
    "prompts",
    "config",
    "quality",
    "tags",
    "depends_on",
    "builder_metadata_jsonb",
    "inputs",
    "outputs",
    "structured_inputs",
    "structured_outputs",
    "skills",
    "tools",
)
SKILL_FIELDS = (
    "skill_id",
    "version",
    "display_name",
    "description",
    "enabled",
    "category",
    "skill_type",
    "instructions",
    "prompt_template",
    "examples",
    "tags",
    "builder_metadata_jsonb",
    "expected_inputs",
    "expected_outputs",
)
DATA_CONTRACT_FIELDS = (
    "contract_id",
    "version",
    "display_name",
    "description",
    "kind",
    "schema",
    "enabled",
    "tags",
)
GUARDRAIL_FIELDS = (
    "id",
    "display_name",
    "description",
    "enabled",
    "mode",
    "rule_type",
    "severity",
    "config",
    "prompt_text",
    "agent_prompt",
    "sort_order",
    "agents",
    "scope",
    "source_project_name",
)
SECTION_FIELDS = {
    "agents": AGENT_FIELDS,
    "skills": SKILL_FIELDS,
    "data_contracts": DATA_CONTRACT_FIELDS,
    "guardrails": GUARDRAIL_FIELDS,
}
_REQUIRED_FIELDS = {
    "agents": ("agent_id", "display_name"),
    "skills": ("skill_id", "display_name"),
    "data_contracts": ("contract_id", "schema"),
    "guardrails": ("id", "mode", "rule_type", "agents", "scope"),
}


def empty_definitions() -> dict[str, list[dict[str, Any]]]:
    return {section: [] for section in DEFINITION_SECTIONS}


def definitions_have_entries(definitions: dict[str, Any]) -> bool:
    return any(_as_list(definitions.get(section)) for section in DEFINITION_SECTIONS)


def _portable_io(rows: Any, *, contract_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Ordered I/O fields; `contract_id` only where it names a carried contract."""
    portable: list[dict[str, Any]] = []
    for row in _as_list(rows):
        if not isinstance(row, dict) or not _text(row.get("name")):
            continue
        item = {key: row.get(key) for key in _IO_FIELDS if key in row}
        item["name"] = _text(row.get("name"))
        contract_id = _text(row.get("contract_id"))
        if contract_ids is not None and contract_id and contract_id in contract_ids:
            item["contract_id"] = contract_id
        portable.append(item)
    return portable


def _nested_contract_ids(value: Any) -> list[str]:
    """Contract ids a contract schema names, in document order."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "contract_id" and isinstance(item, str) and item.strip():
                found.append(item.strip())
            else:
                found.extend(_nested_contract_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_nested_contract_ids(item))
    return found


def _owned_manual_row(row: Any, ref: str) -> dict[str, Any] | None:
    """A row this installation authored: not an extension projection or bridge."""
    if not isinstance(row, dict) or not row:
        return None
    if ref.startswith("extension.") or _durable_projection_install_id(row):
        return None
    return row


class WorkflowPortabilityArtifactsMixin:
    def _export_definitions(
        self, workflows: list[dict[str, Any]], *, account_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Every owned agent, skill, manual data contract and guardrail the workflows use."""
        definitions = empty_definitions()
        agent_ids: list[str] = []
        step_skill_ids: list[str] = []
        for workflow in workflows:
            if _text(workflow.get("portability_kind")) == "extension_workflow_ref":
                continue
            participants = _normalize_participants(workflow)
            participant_agents = {
                item["participant_key"]: item["agent_id"]
                for item in participants
                if item.get("participant_key") and item.get("agent_id")
            }
            agent_ids.extend(item["agent_id"] for item in participants if item.get("agent_id"))
            for step in _workflow_steps(workflow):
                agent_id = _text(step.get("agent_id") or step.get("agent"))
                if not agent_id:
                    agent_id = participant_agents.get(_text(step.get("participant_key")), "")
                if agent_id:
                    agent_ids.append(agent_id)
                skill_id = _text(step.get("skill_id") or step.get("skill"))
                if skill_id:
                    step_skill_ids.append(skill_id)

        exported_agents: set[str] = set()
        skill_ids: list[str] = []
        for agent_id in dict.fromkeys(agent_ids):
            row = self._exportable_row("agent", agent_id, account_id)
            if row is None:
                continue
            portable = self._portable_agent(agent_id, row)
            definitions["agents"].append(portable)
            exported_agents.add(agent_id)
            skill_ids.extend(item["skill_id"] for item in portable["skills"])
        skill_ids.extend(step_skill_ids)

        skill_rows: list[dict[str, Any]] = []
        for skill_id in dict.fromkeys(skill_ids):
            row = self._exportable_row("skill", skill_id, account_id)
            if row is not None:
                skill_rows.append(row)
        contracts = self._export_contracts(skill_rows)
        contract_ids = {item["contract_id"] for item in contracts}
        definitions["data_contracts"] = contracts
        definitions["skills"] = [self._portable_skill(row, contract_ids) for row in skill_rows]
        definitions["guardrails"] = self._export_guardrails(exported_agents, account_id=account_id)
        return definitions

    def _exportable_row(self, kind: str, ref: str, account_id: str) -> dict[str, Any] | None:
        row = _owned_manual_row(_call_db_get(self.db, f"get_{kind}", ref), ref)
        if row is None:
            return None
        row_account = _text(row.get("account_id"))
        system_row = _text(row.get("scope_kind")) == "system"
        if account_id and row_account and row_account != account_id and not system_row:
            return None
        return row

    def _portable_agent(self, agent_id: str, row: dict[str, Any]) -> dict[str, Any]:
        prompts = dict(_as_dict(row.get("prompts")))
        skill_prompts = _as_dict(prompts.pop("skill_prompts", None))
        skills = []
        for item in _as_list(row.get("_skills") or row.get("skills")):
            skill_id = _text(item.get("skill_id") if isinstance(item, dict) else item)
            if not skill_id:
                continue
            prompt = item.get("skill_prompt") if isinstance(item, dict) else None
            skills.append(
                {"skill_id": skill_id, "skill_prompt": _text(prompt or skill_prompts.get(skill_id))}
            )
        tools = [
            _text(item.get("tool_id") if isinstance(item, dict) else item)
            for item in _as_list(row.get("_tools") or row.get("tools"))
        ]
        config = {
            key: value
            for key, value in _as_dict(row.get("config")).items()
            if key not in MODEL_ROUTE_CONFIG_KEYS
        }
        own_inputs = _call_db_list(self.db, "get_agent_own_inputs", agent_id)
        own_outputs = _call_db_list(self.db, "get_agent_own_outputs", agent_id)
        return {
            "agent_id": agent_id,
            "version": _text(row.get("version")) or "1.0.0",
            "display_name": _text(row.get("display_name")) or agent_id,
            "description": _text(row.get("description")),
            "enabled": bool(row.get("enabled", False)),
            "category": _text(row.get("category")),
            "prompts": prompts,
            "config": config,
            "quality": _as_dict(row.get("quality")),
            "tags": [_text(tag) for tag in _as_list(row.get("tags")) if _text(tag)],
            "depends_on": _as_dict(row.get("depends_on")),
            "builder_metadata_jsonb": _as_dict(row.get("builder_metadata_jsonb")),
            "inputs": _as_dict(row.get("inputs")),
            "outputs": [_text(item) for item in _as_list(row.get("outputs")) if _text(item)],
            "structured_inputs": _portable_io(own_inputs),
            "structured_outputs": _portable_io(own_outputs),
            "skills": skills,
            "tools": [tool for tool in tools if tool],
        }

    def _portable_skill(self, row: dict[str, Any], contract_ids: set[str]) -> dict[str, Any]:
        skill_id = _text(row.get("skill_id"))
        inputs = (
            row.get("_inputs") if row.get("_inputs") is not None else row.get("expected_inputs")
        )
        outputs = (
            row.get("_outputs") if row.get("_outputs") is not None else row.get("expected_outputs")
        )
        return {
            "skill_id": skill_id,
            "version": _text(row.get("version")) or "1.0.0",
            "display_name": _text(row.get("display_name")) or skill_id,
            "description": _text(row.get("description")),
            "enabled": bool(row.get("enabled", False)),
            "category": _text(row.get("category")),
            "skill_type": _text(row.get("skill_type")) or "secondary",
            "instructions": [_text(item) for item in _as_list(row.get("instructions"))],
            "prompt_template": _text(row.get("prompt_template")),
            "examples": [item for item in _as_list(row.get("examples")) if isinstance(item, dict)],
            "tags": [_text(tag) for tag in _as_list(row.get("tags")) if _text(tag)],
            "builder_metadata_jsonb": _as_dict(row.get("builder_metadata_jsonb")),
            "expected_inputs": _portable_io(inputs, contract_ids=contract_ids),
            "expected_outputs": _portable_io(outputs, contract_ids=contract_ids),
        }

    def _export_contracts(self, skill_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Manual contracts the skills' I/O names, each after the contracts it names.

        A derived contract (`auto.*` field shapes) is left out: the target's public
        contract sync creates it again from the skill's I/O.
        """
        pending: list[str] = []
        for row in skill_rows:
            for key in ("_inputs", "_outputs", "expected_inputs", "expected_outputs"):
                for item in _as_list(row.get(key)):
                    if isinstance(item, dict) and _text(item.get("contract_id")):
                        pending.append(_text(item.get("contract_id")))
        ordered: list[dict[str, Any]] = []
        placed: set[str] = set()
        visiting: set[str] = set()

        def place(contract_id: str) -> None:
            if contract_id in placed or contract_id in visiting:
                return
            row = _call_db_get(self.db, "get_data_contract", contract_id)
            if not isinstance(row, dict) or _text(row.get("projection_mode")) != "manual":
                return
            visiting.add(contract_id)
            schema = row.get("schema_json") if row.get("schema_json") is not None else {}
            for nested in _nested_contract_ids(schema):
                place(nested)
            visiting.discard(contract_id)
            placed.add(contract_id)
            ordered.append(
                {
                    "contract_id": contract_id,
                    "version": _text(row.get("version")) or "1.0.0",
                    "display_name": _text(row.get("display_name")) or contract_id,
                    "description": _text(row.get("description")),
                    "kind": _text(row.get("kind")) or "json",
                    "schema": schema,
                    "enabled": bool(row.get("enabled", True)),
                    "tags": [_text(tag) for tag in _as_list(row.get("tags")) if _text(tag)],
                }
            )

        for contract_id in dict.fromkeys(pending):
            place(contract_id)
        return ordered

    def _export_guardrails(self, agent_ids: set[str], *, account_id: str) -> list[dict[str, Any]]:
        """The account's rules for the exported agents, global and every project's."""
        if not agent_ids or not account_id:
            return []
        rows = _call_db_list(self.db, "list_guardrails", account_id=account_id)
        portable: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or _durable_projection_install_id(row):
                continue
            agents = [
                _text(agent) for agent in _as_list(row.get("_agents")) if _text(agent) in agent_ids
            ]
            if not agents:
                continue
            project_id = _text(row.get("project_id"))
            rule = {
                "id": _text(row.get("id")),
                "display_name": _text(row.get("display_name")),
                "description": _text(row.get("description")),
                "enabled": bool(row.get("enabled", True)),
                "mode": _text(row.get("mode")),
                "rule_type": _text(row.get("rule_type")),
                "severity": _text(row.get("severity")) or "error",
                "config": _as_dict(row.get("config")),
                "prompt_text": _text(row.get("prompt_text")),
                "agent_prompt": _text(row.get("agent_prompt")),
                "sort_order": int(row.get("sort_order") or 0),
                "agents": sorted(dict.fromkeys(agents)),
                "scope": "project" if project_id else "global",
            }
            if project_id:
                project_name = self._project_name(project_id)
                if project_name:
                    rule["source_project_name"] = project_name
            portable.append(rule)
        return portable

    def _project_name(self, project_id: str) -> str:
        for store in (getattr(self, "project_connection_store", None), self.db):
            project = _call_db_get(store, "get_project", project_id)
            if isinstance(project, dict) and _text(project.get("name")):
                return _text(project.get("name"))
        return ""


def _definition_error(message: str, path: str) -> WorkflowPortabilityError:
    return WorkflowPortabilityError(
        message, code="bundle_definitions_invalid", errors=[{"path": path, "message": message}]
    )


def parse_definitions(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Validate a bundle's `definitions` strictly; anything unexpected is refused with its path."""
    if raw is None:
        return empty_definitions()
    if not isinstance(raw, dict):
        raise _definition_error("Bundle definitions must be an object.", DEFINITIONS_KEY)
    unknown = sorted(str(key) for key in raw if key not in DEFINITION_SECTIONS)
    if unknown:
        raise _definition_error(
            f"Bundle definitions has unknown sections: {', '.join(unknown)}.",
            f"{DEFINITIONS_KEY}.{unknown[0]}",
        )
    parsed = empty_definitions()
    for section in DEFINITION_SECTIONS:
        items = raw.get(section)
        if items is None:
            continue
        section_path = f"{DEFINITIONS_KEY}.{section}"
        if not isinstance(items, list):
            raise _definition_error(f"{section} must be a list.", section_path)
        _, id_field = DEFINITION_KINDS[section]
        seen: set[str] = set()
        for index, item in enumerate(items):
            path = f"{section_path}[{index}]"
            if not isinstance(item, dict):
                raise _definition_error("Each definition must be an object.", path)
            extra = sorted(str(key) for key in item if key not in SECTION_FIELDS[section])
            if extra:
                raise _definition_error(
                    f"Unknown fields: {', '.join(extra)}.", f"{path}.{extra[0]}"
                )
            for field in _REQUIRED_FIELDS[section]:
                if item.get(field) in (None, "", []):
                    raise _definition_error(f"{field} is required.", f"{path}.{field}")
            ref = _text(item.get(id_field))
            if ref in seen:
                raise _definition_error(f"Duplicate {id_field} '{ref}'.", f"{path}.{id_field}")
            seen.add(ref)
            _validate_definition_shape(section, item, path)
            parsed[section].append(dict(item))
    return parsed


def _require_type(item: dict[str, Any], field: str, expected: type | tuple, path: str) -> None:
    if field in item and item[field] is not None and not isinstance(item[field], expected):
        raise _definition_error(f"{field} has the wrong type.", f"{path}.{field}")


def _validate_io(rows: Any, path: str) -> None:
    if rows is None:
        return
    if not isinstance(rows, list):
        raise _definition_error("I/O must be a list.", path)
    for index, row in enumerate(rows):
        row_path = f"{path}[{index}]"
        if not isinstance(row, dict) or not _text(row.get("name")):
            raise _definition_error("Each I/O field needs a name.", row_path)
        extra = sorted(str(key) for key in row if key not in (*_IO_FIELDS, "contract_id"))
        if extra:
            raise _definition_error(
                f"Unknown fields: {', '.join(extra)}.", f"{row_path}.{extra[0]}"
            )


def _validate_definition_shape(section: str, item: dict[str, Any], path: str) -> None:
    for field in ("enabled",):
        _require_type(item, field, bool, path)
    for field in ("prompts", "config", "quality", "depends_on", "builder_metadata_jsonb"):
        _require_type(item, field, dict, path)
    for field in ("tags", "instructions", "examples", "outputs"):
        _require_type(item, field, list, path)
    if section == "agents":
        _require_type(item, "inputs", dict, path)
        _validate_io(item.get("structured_inputs"), f"{path}.structured_inputs")
        _validate_io(item.get("structured_outputs"), f"{path}.structured_outputs")
        if _as_dict(item.get("prompts")).get("skill_prompts"):
            raise _definition_error(
                "Per-skill prompts belong in skills[].skill_prompt.",
                f"{path}.prompts.skill_prompts",
            )
        if MODEL_ROUTE_CONFIG_KEYS & set(_as_dict(item.get("config"))):
            raise _definition_error(
                "An agent definition cannot carry a model route.", f"{path}.config"
            )
        skills = item.get("skills") or []
        if not isinstance(skills, list):
            raise _definition_error("skills must be a list.", f"{path}.skills")
        seen_skills: set[str] = set()
        for index, skill in enumerate(skills):
            skill_path = f"{path}.skills[{index}]"
            if not isinstance(skill, dict) or not _text(skill.get("skill_id")):
                raise _definition_error("Each skill needs a skill_id.", skill_path)
            if set(skill) - {"skill_id", "skill_prompt"}:
                raise _definition_error("Unknown skill binding fields.", skill_path)
            if _text(skill.get("skill_id")) in seen_skills:
                raise _definition_error("Duplicate skill binding.", skill_path)
            seen_skills.add(_text(skill.get("skill_id")))
        tools = item.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise _definition_error("tools must be a list of tool ids.", f"{path}.tools")
    elif section == "skills":
        _validate_io(item.get("expected_inputs"), f"{path}.expected_inputs")
        _validate_io(item.get("expected_outputs"), f"{path}.expected_outputs")
    elif section == "data_contracts":
        _require_type(item, "schema", dict, path)
    elif section == "guardrails":
        try:
            uuid.UUID(_text(item.get("id")))
        except ValueError as exc:
            raise _definition_error("A guardrail id must be a UUID.", f"{path}.id") from exc
        if _text(item.get("scope")) not in GUARDRAIL_SCOPES:
            raise _definition_error("scope must be global or project.", f"{path}.scope")
        agents = item.get("agents")
        if not isinstance(agents, list) or not all(isinstance(a, str) and a for a in agents):
            raise _definition_error("agents must be a list of agent ids.", f"{path}.agents")
        _require_type(item, "sort_order", int, path)


__all__ = [
    "DEFINITIONS_KEY",
    "DEFINITION_KINDS",
    "DEFINITION_SECTIONS",
    "MODEL_ROUTE_CONFIG_KEYS",
    "WorkflowPortabilityArtifactsMixin",
    "definitions_have_entries",
    "empty_definitions",
    "parse_definitions",
]
