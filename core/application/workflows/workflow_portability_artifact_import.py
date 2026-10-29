# mypy: ignore-errors
"""Preflight and creation of the definitions a workflow bundle carries.

A definition whose id is free here is created from the file. A taken id always
needs a decision: reuse the record that is already here (unchanged), or create
the definition under a new id. Nothing existing is ever overwritten: creation is
create-only inside the import's own transaction, so a late failure or a
concurrent save leaves no partial import behind.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from core.application.registry.catalog_categories import known_catalog_category
from core.application.registry.db_store_common import RegistryCreateConflictError
from core.application.workflows.workflow_portability_artifacts import (
    DEFINITION_KINDS,
    DEFINITIONS_KEY,
    empty_definitions,
)
from core.application.workflows.workflow_portability_common import (
    BLOCKING_STATUSES,
    WorkflowPortabilityError,
    _as_dict,
    _as_list,
    _call_db_get,
    _dependency_key,
    _resolution_target_ref,
    _text,
)

logger = logging.getLogger(__name__)

#: The dependency type of the one row that asks for the guardrails' project.
GUARDRAIL_PROJECT_DEPENDENCY = "guardrail_project"
GUARDRAIL_PROJECT_MESSAGE = (
    "This file has guardrails for a project. Choose the project to attach them to."
)
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
#: Ids the platform derives itself: contracts generated from step I/O and records an
#: extension projects. A file must not create them as manual records.
_RESERVED_ID_PREFIXES = {
    "agent": ("extension.",),
    "skill": ("extension.",),
    "data_contract": ("auto.", "extension."),
}
_TABLES = {
    "agent": ("registry_agents", "agent_id"),
    "skill": ("registry_skills", "skill_id"),
    "data_contract": ("registry_data_contracts", "contract_id"),
    "guardrail": ("registry_guardrails", "id"),
}
_LABELS = {
    "agent": "Agent",
    "skill": "Skill",
    "data_contract": "Data contract",
    "guardrail": "Guardrail",
}


def _rewrite_contract_refs(value: Any, targets: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                targets.get(item, item)
                if key == "contract_id" and isinstance(item, str)
                else _rewrite_contract_refs(item, targets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_contract_refs(item, targets) for item in value]
    return value


_DEPENDS_ON_KINDS = ("soft", "hard")


def _depends_on_payload(value: Any, target: Any) -> dict[str, Any]:
    """An agent's `depends_on` with each agent id pointed where the import put it."""
    depends_on = dict(_as_dict(value))
    for strength in _DEPENDS_ON_KINDS:
        depends_on[strength] = [
            target("agent", _text(agent))
            for agent in _as_list(depends_on.get(strength))
            if _text(agent)
        ]
    return depends_on


def _definition_id_problem(kind: str, ref: str) -> str:
    if not _ID_PATTERN.match(ref):
        return f"'{ref}' is not a valid id."
    if ref.startswith(_RESERVED_ID_PREFIXES.get(kind, ())):
        return f"'{ref}' is reserved for ids the platform creates; use another id."
    return ""


def _contract_content_problem(item: dict[str, Any]) -> str:
    """The checks a manual contract save applies, so a file cannot skip them."""
    if not _text(item.get("display_name")):
        return "A data contract needs a display_name."
    kind = (_text(item.get("kind")) or "json").lower()
    if kind not in {"json", "list"}:
        return "A data contract kind must be json or list."
    schema = item.get("schema") or {}
    if not isinstance(schema, dict):
        return "A data contract schema must be an object."
    schema_type = _text(schema.get("type")).lower()
    if schema_type and schema_type != {"json": "object", "list": "array"}[kind]:
        return f"A {kind} data contract must use schema.type={'object' if kind == 'json' else 'array'}."
    return ""


class WorkflowPortabilityArtifactImportMixin:
    # ------------------------------------------------------------------ preflight

    def _definition_plan(
        self,
        bundle: dict[str, Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        *,
        target_project_id: str = "",
    ) -> dict[str, Any]:
        """How each carried definition lands here, and what it still needs."""
        definitions = _as_dict(bundle.get(DEFINITIONS_KEY)) or empty_definitions()
        rows: list[dict[str, Any]] = []
        targets: dict[tuple[str, str], str] = {}
        actions: dict[tuple[str, str], str] = {}
        claimed: dict[str, set[str]] = {kind: set() for kind, _ in DEFINITION_KINDS.values()}
        for section, (kind, id_field) in DEFINITION_KINDS.items():
            for item in _as_list(definitions.get(section)):
                ref = _text(item.get(id_field))
                row = self._classify_definition(
                    kind,
                    ref,
                    item,
                    resolution_index.get((kind, ref)),
                    claimed=claimed[kind],
                    target_project_id=target_project_id,
                    resolution_index=resolution_index,
                )
                rows.append(row)
                targets[(kind, ref)] = row["metadata"]["target_ref"]
                actions[(kind, ref)] = row["metadata"]["action"]
        external = self._definition_external_rows(definitions)
        project_rules = [
            item
            for item in _as_list(definitions.get("guardrails"))
            if _text(item.get("scope")) == "project"
            and actions.get(("guardrail", _text(item.get("id")))) == "create"
        ]
        if project_rules:
            rows.append(
                self._guardrail_project_row(project_rules, target_project_id=target_project_id)
            )
        return {
            "definitions": definitions,
            "rows": rows,
            "external_rows": external,
            "targets": targets,
            "actions": actions,
            "keys": set(targets),
        }

    def _classify_definition(
        self,
        kind: str,
        ref: str,
        item: dict[str, Any],
        resolution: dict[str, Any] | None,
        *,
        claimed: set[str],
        target_project_id: str,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        label = _text(item.get("display_name")) or ref
        reserved = self._definition_reserved(kind, ref)
        existing = self._accessible_definition(kind, ref) if reserved else None
        action = _text(resolution.get("action")) if resolution else ""
        target = ref
        message = ""
        if not reserved:
            status, actions_offered, plan_action = (
                "create_from_bundle",
                ["rename", "abort"],
                "create",
            )
            message = f"{_LABELS[kind]} is created from this file."
        else:
            status = "conflict"
            plan_action = "conflict"
            actions_offered = (
                ["use_existing", "rename", "abort"] if existing else ["rename", "abort"]
            )
            message = (
                f"{_LABELS[kind]} id '{ref}' is already used here. Reuse that record or "
                "create this one under a new id."
                if existing
                else f"{_LABELS[kind]} id '{ref}' is taken by a record you cannot reuse "
                "(another account's, or in Trash). Create this one under a new id."
            )
        if action and action not in actions_offered:
            status, plan_action = "conflict", "conflict"
            message = f"That choice is not available for this {_LABELS[kind].lower()}."
        elif action == "rename":
            target = _resolution_target_ref(resolution)
            problem = self._rename_problem(kind, target, claimed)
            if problem:
                status, plan_action, message = "conflict", "conflict", problem
            else:
                status, plan_action, message = "resolved", "create", ""
                claimed.add(target)
        elif action == "use_existing":
            # Reuse keeps the id; the rewrite would otherwise point the workflow at an
            # unchecked target. Another id is a rename.
            problem = "" if existing else f"{_LABELS[kind]} '{ref}' cannot be reused from here."
            if not problem and _resolution_target_ref(resolution) not in ("", ref):
                problem = (
                    f"Reusing keeps the id '{ref}'. To use another id, create it under a new id."
                )
            if not problem and kind == "guardrail":
                problem = self._guardrail_reuse_problem(
                    existing, item, target_project_id, resolution_index
                )
            if problem:
                status, plan_action, message = "conflict", "conflict", problem
            else:
                status, plan_action, message = "resolved", "use_existing", ""
        elif plan_action == "create":
            claimed.add(ref)
        issue = self._definition_content_problem(kind, item)
        if not issue and kind == "guardrail":
            issue = self._guardrail_agents_problem(item, resolution_index)
        if not issue and kind != "guardrail" and action != "rename":
            # Created under its own id: the id a rename picks is checked by _rename_problem.
            issue = _definition_id_problem(kind, ref)
        if issue and plan_action == "create":
            status, message = "blocked", issue
            actions_offered = ["abort"]
        return {
            "dependency_key": _dependency_key(kind, ref),
            "dependency_type": kind,
            "ref": ref,
            "label": label,
            "status": status,
            "blocking": status in BLOCKING_STATUSES,
            "required": True,
            "sources": [f"{DEFINITIONS_KEY}.{kind}"],
            "resolution_actions": actions_offered,
            "resolved_by": dict(resolution) if resolution and status == "resolved" else None,
            "message": message,
            "metadata": {
                "definition": True,
                "action": plan_action,
                "target_ref": target if plan_action != "use_existing" else ref,
                **({"scope": _text(item.get("scope"))} if kind == "guardrail" else {}),
                **(
                    {"source_project_name": _text(item.get("source_project_name"))}
                    if _text(item.get("source_project_name"))
                    else {}
                ),
            },
        }

    def _rename_problem(self, kind: str, target: str, claimed: set[str]) -> str:
        if not target:
            return f"Enter the new {_LABELS[kind].lower()} id."
        if kind == "guardrail":
            try:
                uuid.UUID(target)
            except ValueError:
                return "A guardrail id must be a UUID."
        else:
            problem = _definition_id_problem(kind, target)
            if problem:
                return problem
        if target in claimed or self._definition_reserved(kind, target):
            return f"{_LABELS[kind]} id '{target}' is taken too; choose another."
        return ""

    def _definition_content_problem(self, kind: str, item: dict[str, Any]) -> str:
        """What a save would refuse, said before anything is written."""
        if kind == "data_contract":
            return _contract_content_problem(item)
        if kind in {"agent", "skill"}:
            category = _text(item.get("category"))
            if category and not known_catalog_category(category):
                return f"Category '{category}' is not one of this installation's categories."
        if kind == "skill" and _text(item.get("skill_type")) not in {"", "base", "secondary"}:
            return "skill_type must be base or secondary."
        return ""

    def _guardrail_reuse_problem(
        self,
        existing: dict[str, Any] | None,
        item: dict[str, Any],
        target_project_id: str,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> str:
        """A reused rule must already cover the same scope and the same final agents."""
        existing = existing or {}
        project_id = _text(existing.get("project_id"))
        if _text(item.get("scope")) == "global" and project_id:
            return "The existing guardrail applies to one project; this one is global."
        if _text(item.get("scope")) == "project" and project_id != _text(target_project_id):
            return "The existing guardrail belongs to another project. Create a copy instead."
        final_agents = {
            self._definition_target_from_index("agent", agent, resolution_index)
            for agent in _as_list(item.get("agents"))
        }
        if not final_agents.issubset({_text(agent) for agent in _as_list(existing.get("_agents"))}):
            return (
                "The existing guardrail is not attached to the same agents. Create a copy instead."
            )
        return ""

    def _guardrail_agents_problem(
        self, item: dict[str, Any], resolution_index: dict[tuple[str, str], dict[str, Any]]
    ) -> str:
        """A guardrail can only target this account's agents; the save refuses the rest."""
        account_id = _text(getattr(self, "account_id", ""))
        if not account_id:
            return ""
        for agent in _as_list(item.get("agents")):
            agent_id = self._definition_target_from_index("agent", _text(agent), resolution_index)
            row = _call_db_get(self.db, "get_agent", agent_id)
            if row and _text(row.get("account_id")) != account_id:
                return (
                    f"This guardrail targets agent '{agent_id}', which is not one of this "
                    "account's agents. Create the agent under a new id instead."
                )
        return ""

    def _definition_target_from_index(
        self, kind: str, ref: str, resolution_index: dict[tuple[str, str], dict[str, Any]]
    ) -> str:
        resolution = resolution_index.get((kind, ref))
        if resolution and _text(resolution.get("action")) == "rename":
            return _resolution_target_ref(resolution) or ref
        return ref

    def _definition_external_rows(self, definitions: dict[str, Any]) -> list[dict[str, Any]]:
        """References from the definitions to records the file does not carry."""
        carried = {
            kind: {_text(item.get(id_field)) for item in _as_list(definitions.get(section))}
            for section, (kind, id_field) in DEFINITION_KINDS.items()
        }
        rows: list[dict[str, Any]] = []

        def add(kind: str, ref: str, source: str) -> None:
            if ref:
                rows.append(
                    {
                        "dependency_type": kind,
                        "ref": ref,
                        "label": ref,
                        "sources": [source],
                        "required": True,
                        "metadata": {},
                    }
                )

        for agent in _as_list(definitions.get("agents")):
            agent_id = _text(agent.get("agent_id"))
            for skill in _as_list(agent.get("skills")):
                skill_id = _text(skill.get("skill_id"))
                if skill_id not in carried["skill"]:
                    add("skill", skill_id, f"{DEFINITIONS_KEY}.agents.{agent_id}.skills")
            for tool_id in _as_list(agent.get("tools")):
                add("tool", _text(tool_id), f"{DEFINITIONS_KEY}.agents.{agent_id}.tools")
            for strength in _DEPENDS_ON_KINDS:
                for other in _as_list(_as_dict(agent.get("depends_on")).get(strength)):
                    if _text(other) not in carried["agent"]:
                        add(
                            "agent",
                            _text(other),
                            f"{DEFINITIONS_KEY}.agents.{agent_id}.depends_on.{strength}",
                        )
        for skill in _as_list(definitions.get("skills")):
            skill_id = _text(skill.get("skill_id"))
            for key in ("expected_inputs", "expected_outputs"):
                for field in _as_list(skill.get(key)):
                    contract_id = _text(field.get("contract_id"))
                    if contract_id and contract_id not in carried["data_contract"]:
                        add(
                            "data_contract",
                            contract_id,
                            f"{DEFINITIONS_KEY}.skills.{skill_id}.{key}.{_text(field.get('name'))}",
                        )
        for rule in _as_list(definitions.get("guardrails")):
            for agent_id in _as_list(rule.get("agents")):
                if _text(agent_id) not in carried["agent"]:
                    add("agent", _text(agent_id), f"{DEFINITIONS_KEY}.guardrails.{rule.get('id')}")
        return rows

    def _classify_external_contract(self, row: dict[str, Any]) -> dict[str, Any]:
        ref = _text(row.get("ref"))
        exists = _call_db_get(self.db, "get_data_contract", ref) is not None
        return {
            "dependency_key": _dependency_key("data_contract", ref),
            "dependency_type": "data_contract",
            "ref": ref,
            "label": ref,
            "status": "resolved" if exists else "blocked",
            "blocking": not exists,
            "required": True,
            "sources": _as_list(row.get("sources")),
            "resolution_actions": ["use_existing", "abort"] if exists else ["abort"],
            "resolved_by": None,
            "message": "" if exists else f"Data contract '{ref}' is neither in the file nor here.",
            "metadata": {},
        }

    def _guardrail_project_row(
        self, rules: list[dict[str, Any]], *, target_project_id: str
    ) -> dict[str, Any]:
        chosen = bool(_text(target_project_id))
        return {
            "dependency_key": _dependency_key(GUARDRAIL_PROJECT_DEPENDENCY, "project"),
            "dependency_type": GUARDRAIL_PROJECT_DEPENDENCY,
            "ref": "project",
            "label": "Project for guardrails",
            "status": "resolved" if chosen else "blocked",
            "blocking": not chosen,
            "required": True,
            "sources": [f"{DEFINITIONS_KEY}.guardrails"],
            "resolution_actions": [] if chosen else ["abort"],
            "resolved_by": {"project_id": _text(target_project_id)} if chosen else None,
            "message": "" if chosen else GUARDRAIL_PROJECT_MESSAGE,
            "metadata": {
                "rules": [
                    {
                        "id": _text(rule.get("id")),
                        "display_name": _text(rule.get("display_name")),
                        "source_project_name": _text(rule.get("source_project_name")),
                    }
                    for rule in rules
                ]
            },
        }

    def _definition_reserved(self, kind: str, ref: str) -> bool:
        if not ref:
            return False
        reserved = getattr(self.db, "registry_entity_reserved", None)
        if callable(reserved):
            return bool(reserved(kind, ref))
        return self._accessible_definition(kind, ref) is not None

    def _accessible_definition(self, kind: str, ref: str) -> dict[str, Any] | None:
        """An active record this import may reuse: this account's, or the platform's."""
        account_id = _text(getattr(self, "account_id", ""))
        if kind == "data_contract":
            return _call_db_get(self.db, "get_data_contract", ref)
        if kind == "guardrail":
            if not account_id:
                return None
            return _call_db_get(self.db, "get_guardrail", ref, account_id=account_id)
        row = self._existing_row_for_dependency(kind, ref)
        if not row:
            return None
        row_account = _text(row.get("account_id"))
        foreign = account_id and row_account and row_account != account_id
        if foreign and _text(row.get("scope_kind")) != "system":
            return None
        return row

    # ------------------------------------------------------------------ apply

    def _apply_definitions(
        self,
        plan: dict[str, Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        *,
        owner_account_id: str,
        owner_project_id: str,
        author: str,
    ) -> dict[str, Any]:
        """Create the planned definitions in dependency order, inside the import transaction."""
        definitions = plan["definitions"]
        targets: dict[tuple[str, str], str] = plan["targets"]
        actions: dict[tuple[str, str], str] = plan["actions"]
        created: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        disabled: list[dict[str, Any]] = []

        def target(kind: str, ref: str) -> str:
            if (kind, ref) in targets:
                return targets[(kind, ref)]
            return self._mapped_ref(kind, ref, resolution_index) or ref

        def record(kind: str, ref: str) -> bool:
            key = (kind, ref)
            outcome = {"kind": kind, "source_id": ref, "id": targets.get(key, ref)}
            if actions.get(key) == "use_existing":
                reused.append(outcome)
                return False
            if actions.get(key) != "create":
                raise WorkflowPortabilityError(
                    f"{_LABELS[kind]} '{ref}' has no import decision.",
                    code="workflow_import_preflight_blocked",
                )
            created.append(outcome)
            return True

        contract_targets = {
            ref: target_id for (kind, ref), target_id in targets.items() if kind == "data_contract"
        }
        try:
            for item in _as_list(definitions.get("data_contracts")):
                ref = _text(item.get("contract_id"))
                if not record("data_contract", ref):
                    continue
                self.db.save_data_contract(
                    {
                        "contract_id": contract_targets[ref],
                        "version": _text(item.get("version")) or "1.0.0",
                        "display_name": _text(item.get("display_name")) or contract_targets[ref],
                        "description": _text(item.get("description")),
                        "kind": _text(item.get("kind")) or "json",
                        "schema": _rewrite_contract_refs(
                            _as_dict(item.get("schema")), contract_targets
                        ),
                        "enabled": bool(item.get("enabled", True)),
                        "tags": list(item.get("tags") or []),
                        "projection_mode": "manual",
                    },
                    author=author,
                    create_only=True,
                )
            for item in _as_list(definitions.get("skills")):
                ref = _text(item.get("skill_id"))
                if not record("skill", ref):
                    continue
                self.db.save_skill(
                    self._skill_payload(
                        item, target("skill", ref), contract_targets, owner_account_id
                    ),
                    author=author,
                    create_only=True,
                )
            model_problem = self._project_model_problem(owner_project_id)
            for item in _as_list(definitions.get("agents")):
                ref = _text(item.get("agent_id"))
                if not record("agent", ref):
                    continue
                agent_id = target("agent", ref)
                enabled = bool(item.get("enabled", False)) and not model_problem
                if bool(item.get("enabled", False)) and model_problem:
                    disabled.append({"agent_id": agent_id, "reason": model_problem})
                self.db.save_agent(
                    self._agent_payload(item, agent_id, target, owner_account_id, enabled),
                    author=author,
                    create_only=True,
                )
            for item in _as_list(definitions.get("guardrails")):
                ref = _text(item.get("id"))
                if not record("guardrail", ref):
                    continue
                self.db.save_guardrail(
                    {
                        "id": target("guardrail", ref),
                        "display_name": _text(item.get("display_name")),
                        "description": _text(item.get("description")),
                        "enabled": bool(item.get("enabled", True)),
                        "mode": _text(item.get("mode")),
                        "rule_type": _text(item.get("rule_type")),
                        "severity": _text(item.get("severity")) or "error",
                        "config": _as_dict(item.get("config")),
                        "prompt_text": _text(item.get("prompt_text")),
                        "agent_prompt": _text(item.get("agent_prompt")),
                        "sort_order": int(item.get("sort_order") or 0),
                        "project_id": (
                            _text(owner_project_id) or None
                            if _text(item.get("scope")) == "project"
                            else None
                        ),
                        "agents": [target("agent", _text(a)) for a in _as_list(item.get("agents"))],
                    },
                    author=author,
                    account_id=owner_account_id,
                    actor_id=author,
                    actor_type="user",
                    create_only=True,
                )
        except RegistryCreateConflictError as exc:
            raise WorkflowPortabilityError(
                f"{_LABELS.get(exc.kind, exc.kind)} '{exc.entity_id}' was created meanwhile; "
                "run the import again to choose what to do.",
                code="workflow_import_definition_conflict",
                status_code=409,
                errors=[{"dependency_type": exc.kind, "ref": exc.entity_id, "status": "conflict"}],
            ) from exc
        except ValueError as exc:
            # A record the preflight accepted changed before the import was applied (a
            # reused skill moved to Trash, say); the transaction rolls everything back.
            raise WorkflowPortabilityError(
                f"The file's records could not be saved: {exc}. Nothing was imported; "
                "run the import again.",
                code="workflow_import_definition_invalid",
                status_code=409,
            ) from exc
        return {"created": created, "reused": reused, "agents_left_disabled": disabled}

    def _skill_payload(
        self,
        item: dict[str, Any],
        skill_id: str,
        contract_targets: dict[str, str],
        owner_account_id: str,
    ) -> dict[str, Any]:
        def io(rows: Any) -> list[dict[str, Any]]:
            fields = []
            for row in _as_list(rows):
                field = {
                    "name": _text(row.get("name")),
                    "type": _text(row.get("type")) or "text",
                    "description": _text(row.get("description")),
                    "required": bool(row.get("required", False)),
                }
                contract_id = _text(row.get("contract_id"))
                if contract_id:
                    field["contract_id"] = contract_targets.get(contract_id, contract_id)
                fields.append(field)
            return fields

        return {
            "skill_id": skill_id,
            "version": _text(item.get("version")) or "1.0.0",
            "display_name": _text(item.get("display_name")) or skill_id,
            "description": _text(item.get("description")),
            "enabled": bool(item.get("enabled", False)),
            "category": _text(item.get("category")),
            "skill_type": _text(item.get("skill_type")) or "secondary",
            "instructions": [_text(line) for line in _as_list(item.get("instructions"))],
            "prompt_template": _text(item.get("prompt_template")),
            "examples": [row for row in _as_list(item.get("examples")) if isinstance(row, dict)],
            "tags": [_text(tag) for tag in _as_list(item.get("tags")) if _text(tag)],
            "builder_metadata_jsonb": _as_dict(item.get("builder_metadata_jsonb")),
            "expected_inputs": io(item.get("expected_inputs")),
            "expected_outputs": io(item.get("expected_outputs")),
            "account_id": owner_account_id or None,
            "scope_kind": "account" if owner_account_id else "system",
        }

    def _agent_payload(
        self,
        item: dict[str, Any],
        agent_id: str,
        target,
        owner_account_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        skills = [
            (target("skill", _text(row.get("skill_id"))), _text(row.get("skill_prompt")))
            for row in _as_list(item.get("skills"))
        ]
        prompts = dict(_as_dict(item.get("prompts")))
        prompts["skill_prompts"] = dict(skills)

        def io(rows: Any) -> list[dict[str, Any]]:
            return [
                {
                    "name": _text(row.get("name")),
                    "type": _text(row.get("type")) or "text",
                    "description": _text(row.get("description")),
                    "required": bool(row.get("required", False)),
                }
                for row in _as_list(rows)
            ]

        return {
            "agent_id": agent_id,
            "version": _text(item.get("version")) or "1.0.0",
            "display_name": _text(item.get("display_name")) or agent_id,
            "description": _text(item.get("description")),
            "enabled": enabled,
            "category": _text(item.get("category")),
            "prompts": prompts,
            "config": _as_dict(item.get("config")),
            "quality": _as_dict(item.get("quality")),
            "tags": [_text(tag) for tag in _as_list(item.get("tags")) if _text(tag)],
            "depends_on": _depends_on_payload(item.get("depends_on"), target),
            "builder_metadata_jsonb": _as_dict(item.get("builder_metadata_jsonb")),
            "inputs": _as_dict(item.get("inputs")) or {"required": [], "optional": []},
            "outputs": [_text(name) for name in _as_list(item.get("outputs")) if _text(name)],
            "structured_inputs": io(item.get("structured_inputs")),
            "structured_outputs": io(item.get("structured_outputs")),
            "skills": [skill_id for skill_id, _ in skills],
            "tools": [target("tool", _text(tool)) for tool in _as_list(item.get("tools"))],
            "account_id": owner_account_id or None,
            "scope_kind": "account" if owner_account_id else "system",
        }

    def _project_model_problem(self, project_id: str) -> str:
        """Why an agent without its own model cannot run in the project; empty when it can.

        Resolves the model route without calling the model.
        """
        if not _text(project_id):
            return (
                "No project was chosen for the import, so no model is set for this agent; "
                "link the workflow to a project with a model, then enable the agent."
            )
        unchecked = (
            "The project's model could not be checked; check the project's settings, "
            "then enable the agent."
        )
        get_store = getattr(self, "get_project_store", None)
        try:
            loader = getattr(
                get_store() if callable(get_store) else None, "get_project_run_config", None
            )
            if not callable(loader):
                return unchecked
            config = loader(project_id) or {}
        except Exception:
            logger.warning(
                "Import could not read the model of project %s", project_id, exc_info=True
            )
            return unchecked
        from core.application.orchestration.model_provider import (
            ModelUnavailableError,
            _resolve_model_route,
        )

        try:
            _resolve_model_route(dict(config), None, registry_cfg={})
        except ModelUnavailableError:
            return (
                "The project has no configured model for this agent; "
                "choose one in the project's settings, then enable it."
            )
        return ""


__all__ = [
    "GUARDRAIL_PROJECT_DEPENDENCY",
    "GUARDRAIL_PROJECT_MESSAGE",
    "WorkflowPortabilityArtifactImportMixin",
]
