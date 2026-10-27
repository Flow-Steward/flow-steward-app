"""Prove a typed data boundary before relocating a group.

This planner is deliberately fail-closed. Runtime state/history is not a child
input contract, and a mutable accumulator is not a frozen input value.
The registry supplies canonical I/O rows; this module owns no type inference.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.step_templating import _TEMPLATE_RE, _split_default_filter
from core.application.orchestration.workflow_state_selector_contract import (
    StateSelectorReference,
    _boundary_entry_selectors,
    discover_for_each_state_selectors,
    discover_group_state_selectors,
    discover_step_state_selectors,
)
from core.application.workflows.workflow_group_extraction_flow import definite_state_before
from core.application.workflows.workflow_group_extraction_proof import is_runtime_context_root
from core.application.workflows.workflow_group_extraction_retained_outputs import (
    retained_parent_outputs,
)

_STATE_PREFIX = "state."

_TYPES = {"text": "string", "paragraph": "string", "json": "object"}
_JSON_TYPES = frozenset({"string", "number", "boolean", "object", "array"})
_PROVEN_KINDS = frozenset(
    {
        "http_request",
        "template",
        "transform",
        "set_variable",
        "if",
        "switch",
        "connector",
        "dataset_query",
        # The control steps of a moved For each: they steer only their own For
        # each, and Collect's target is judged by the Collect boundary rule.
        "break",
        "continue",
        "collect",
    }
)


@dataclass
class GroupBindings:
    workflow: dict[str, Any]
    interface: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)

    def refuse(self, code: str, path: str, message: str, subject: str = "") -> None:
        issue = {"code": code, "path": path, "message": message}
        if subject:
            # The state value the refusal is about, so a client can name it in plain words.
            issue["subject"] = subject
        if issue not in self.issues:
            self.issues.append(issue)


def _json_type(value: Any) -> str:
    name = str(value or "").strip()
    return _TYPES.get(name, name) if _TYPES.get(name, name) in _JSON_TYPES else ""


def _leaves(value: Any, path: str):
    """Use the selector discoverer's paths, including literal dotted mapping keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if isinstance(item, str):
                yield value, key, child_path
            else:
                yield from _leaves(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]"
            if isinstance(item, str):
                yield value, index, child_path
            else:
                yield from _leaves(item, child_path)


def _replace_read(
    owner: dict,
    path: str,
    reference: StateSelectorReference,
    source: str,
    target: str,
    plan: GroupBindings,
) -> None:
    locations = [
        (obj, key) for obj, key, leaf_path in _leaves(owner, path) if leaf_path == reference.path
    ]
    if len(locations) != 1:
        plan.refuse(
            "ambiguous_selector_path",
            reference.path,
            "This selector's configuration path is ambiguous; make the mapping explicit before converting.",
        )
        return
    obj, key = locations[0]
    raw = obj[key]

    def replace_expression(expression):
        selector, _default, has_default = _split_default_filter(expression)
        canonical = selector if selector.startswith(_STATE_PREFIX) else f"state.{selector}"
        if canonical != reference.selector:
            return expression
        replacement = target + reference.selector[len(source) :]
        if has_default:
            before, pipe, after = expression.partition("|")
            return before.replace(selector, replacement, 1) + pipe + after
        return expression.replace(selector, replacement, 1)

    if _TEMPLATE_RE.search(raw):

        def replace_token(match):
            expression = match.group(1)
            return match.group(0).replace(expression, replace_expression(expression), 1)

        updated = _TEMPLATE_RE.sub(replace_token, raw)
    elif "==" in raw or "!=" in raw:
        operator = "==" if "==" in raw else "!="
        left, right = raw.split(operator, 1)
        updated = replace_expression(left) + operator + replace_expression(right)
    elif raw.startswith("!state."):
        updated = "!" + replace_expression(raw[1:])
    else:
        updated = replace_expression(raw)
    if updated == raw and target not in raw:
        plan.refuse(
            "unsupported_selector_form",
            reference.path,
            "This authored selector cannot be rewritten without changing literal text.",
        )
    else:
        obj[key] = updated


def _is_message_read(value: str, path: str) -> bool:
    return value.strip().startswith("message.") and (
        path.endswith(".source")
        or ".input_mapping." in path
        or ".input_mapping_jsonb." in path
        or ".boundary_contract.exit[" in path
    )


def _owner_guards(owner: dict, path: str, plan: GroupBindings) -> None:
    kind = owner.get("step_kind")
    if kind and kind not in _PROVEN_KINDS:
        code = "implicit_agent_context" if kind == "agent" else "unsupported_step_boundary"
        message = "This step has runtime context not represented by explicit transferable inputs."
        if kind == "agent":
            message = "Agent prompts also receive task/history context; extract an explicit callable agent workflow before moving this step."
        if kind == "file_from_base64":
            code, message = (
                "artifact_boundary",
                "File ownership requires an explicit child artifact contract.",
            )
        plan.refuse(code, path, message)
    http = (owner.get("override") or {}).get("http_request") or {}
    if (
        kind == "http_request"
        and str(http.get("response_mode") or "").strip().lower() == "artifact"
    ):
        plan.refuse(
            "artifact_boundary",
            f"{path}.override.http_request.response_mode",
            "An HTTP artifact response requires an explicit child artifact ownership contract.",
        )
    if (owner.get("boundary_contract") or {}).get("exit"):
        plan.refuse(
            "exit_contract_boundary",
            f"{path}.boundary_contract.exit",
            "An exit contract can depend on parent state or message history; expose an explicit callable boundary before conversion.",
        )
    if owner.get("context_inject"):
        plan.refuse(
            "implicit_message_history",
            f"{path}.context_inject",
            "Context injection can read parent message history; declare an explicit child input instead.",
        )
    for _obj, _key, leaf_path in _leaves(owner, path):
        if _is_message_read(_obj[_key], leaf_path):
            plan.refuse(
                "message_history_boundary",
                leaf_path,
                "Message selectors require a producer-specific child output/input mapping; state bindings can be converted automatically.",
            )
    for row in (owner.get("boundary_contract") or {}).get("entry") or []:
        source = str(row.get("source") or "").strip()
        if not source.startswith((_STATE_PREFIX, "message.")):
            plan.refuse(
                "implicit_boundary_input",
                f"{path}.boundary_contract.entry",
                "A boundary input without an explicit state path can read runtime context; use a typed state.* source before conversion.",
            )


def _state_outputs(
    members: list[dict], rows: Mapping[str, list[dict]], plan: GroupBindings
) -> dict[str, str]:
    result: dict[str, str] = {}
    for member in members:
        step_id = member["step_id"]
        for row in rows.get(step_id, []):
            name = str(row.get("name") or "").removeprefix(_STATE_PREFIX)
            path = f"steps.{step_id}.outputs.{name}"
            if not re.fullmatch(r"[A-Za-z_]\w*", name, flags=re.ASCII):
                plan.refuse(
                    "partial_state_write",
                    path,
                    "A nested write must preserve the parent's other object fields; expose a whole typed result before converting.",
                )
                continue
            value_type = _json_type(row.get("type"))
            if not value_type or is_reserved_state_target(name):
                plan.refuse(
                    "unsupported_output_type",
                    path,
                    "This output is not an ordinary transferable typed state value.",
                )
                continue
            if name in result and result[name] != value_type:
                plan.refuse(
                    "ambiguous_output_type",
                    path,
                    "Multiple steps write this output with different types.",
                )
            result[name] = value_type
            plan.output_mapping[name] = f"state.{name}"
    return result


def _external_read(
    reference: StateSelectorReference,
    owner: dict,
    path: str,
    local_names: set[str],
    outputs: dict,
    types: dict,
    plan: GroupBindings,
    definite: set[str],
) -> None:
    try:
        tokens = parse_state_path(reference.selector)
    except StatePathError:
        tokens = []
    if not tokens or tokens[0].kind != "key":
        plan.refuse(
            "whole_state_input",
            reference.path,
            "The whole runtime state cannot become a typed child input.",
            reference.selector or "state",
        )
        return
    root = str(tokens[0].value)
    if root in local_names and path.startswith("steps."):
        return
    if is_runtime_context_root(root):
        plan.refuse(
            "runtime_context_boundary",
            reference.path,
            "This runtime-owned context changes during execution; it cannot become a frozen child input.",
        )
        return
    if root in outputs:
        if root in definite:
            return
        plan.refuse(
            "mutable_external_state",
            reference.path,
            "This value is read and written inside the group; a frozen child input would change iteration or branch behavior.",
            f"state.{root}",
        )
        return
    source, alias = f"state.{root}", f"source_{root}"
    if root == "inputs":
        if len(tokens) < 2 or tokens[1].kind != "key":
            plan.refuse(
                "whole_state_input",
                reference.path,
                "Select an individual workflow input instead of the entire input object.",
                reference.selector or "state.inputs",
            )
            return
        name = str(tokens[1].value)
        source, alias = f"state.inputs.{name}", f"input_{name}"
    value_type = _json_type(types.get(source))
    if not value_type:
        plan.refuse(
            "unknown_input_type",
            reference.path,
            f"No unambiguous transferable type is declared for {source}.",
            source,
        )
        return
    if (
        reference.path.startswith("for_each.")
        and reference.path.endswith(".over")
        and (value_type != "array")
    ):
        plan.refuse(
            "for_each_source_boundary",
            reference.path,
            "Automatic conversion moves a For each over a typed array; declare the source as an array first.",
            source,
        )
        return
    properties = plan.interface["input_schema"]["properties"]
    schema = {"type": [value_type, "null"]}
    if alias in properties and properties[alias] != schema:
        plan.refuse(
            "ambiguous_input_type",
            reference.path,
            f"{source} has different types at different reads.",
            source,
        )
        return
    properties[alias] = schema
    plan.input_mapping[alias] = source
    _replace_read(owner, path, reference, source, f"state.inputs.{alias}", plan)


def analyze_group_bindings(
    workflow: Mapping[str, Any],
    group_id: str,
    *,
    output_rows: Mapping[str, list[dict]],
    available_types: Mapping[str, dict[str, str]],
    retained_output_rows: Mapping[str, list[dict]] | None = None,
) -> GroupBindings:
    plan = GroupBindings(workflow=copy.deepcopy(dict(workflow)))
    group = next((g for g in plan.workflow.get("groups", []) if g["group_id"] == group_id), None)
    if group is None:
        plan.refuse("group_not_found", "group_id", "The selected group no longer exists.")
        return plan
    nested = [
        item for item in plan.workflow.get("for_each", []) if item.get("group_id") == group_id
    ]
    nested_ids = {item["for_each_id"] for item in nested}
    members = [
        s
        for s in plan.workflow.get("steps", [])
        if s.get("group_id") == group_id or s.get("for_each_id") in nested_ids
    ]
    outputs = _state_outputs(members, output_rows, plan)
    parent_contract = (workflow.get("settings") or {}).get("callable") or {}
    retained = retained_parent_outputs(
        workflow,
        group_id,
        retained_output_rows or {},
        _json_type,
    )
    for row in parent_contract.get("expected_outputs") or []:
        name = str(row.get("name") or "")
        if name not in outputs and (
            not _json_type(row.get("type")) or retained.get(name) != _json_type(row.get("type"))
        ):
            plan.refuse(
                "parent_output_boundary",
                f"settings.callable.expected_outputs.{name}",
                "The parent's callable result may come from moved message history; expose a transferable state output before conversion.",
            )
    from core.application.registry.workflow_contract import interface_from_definition

    member_ids = {str(step.get("step_id") or "") for step in members}
    for returned_file in interface_from_definition(workflow).returned_files:
        if returned_file.source.step_id in member_ids:
            plan.refuse(
                "parent_output_boundary",
                f"steps.{returned_file.source.step_id}.override.returned_files",
                "A step in this group returns a file from the workflow; untick “Return from workflow” before conversion.",
            )
    plan.interface = {
        "enabled": True,
        "trigger_channels": ["parent_workflow"],
        "input_mode": "json_schema",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "expected_outputs": [
            {"name": name, "type": kind, "required": False}
            for name, kind in sorted(outputs.items())
        ],
        "output_mode": "variables" if outputs else "none",
    }
    # Item and index of a moved For each are published inside the child.
    local_names = {
        str(name)
        for item in nested
        for name in (item.get("item_name"), item.get("index_name"))
        if name
    }
    _collect_target_guards(members, outputs, plan)

    if outputs and (group.get("error_policy") or {}).get("on_error") == "route_to_step":
        plan.refuse(
            "failure_state_boundary",
            f"groups.{group_id}.error_policy",
            "A failed child does not publish partial state to the parent's error branch.",
        )
    # Group entry reads use the same canonical upstream type evidence as the
    # body entry. Conflicting maps are refused by individual member reads.
    group_types = dict(available_types.get(members[0]["step_id"], {})) if members else {}
    owners = [
        (
            group,
            f"groups.{group_id}",
            discover_group_state_selectors(group, group_path=f"groups.{group_id}"),
            group_types,
        )
    ]
    owners.extend(
        (
            item,
            f"for_each.{item['for_each_id']}",
            discover_for_each_state_selectors(
                item, for_each_path=f"for_each.{item['for_each_id']}"
            ),
            group_types,
        )
        for item in nested
    )
    for member in members:
        path = f"steps.{member['step_id']}"
        refs = discover_step_state_selectors(member, step_path=path) + _boundary_entry_selectors(
            member, path
        )
        owners.append((member, path, refs, dict(available_types.get(member["step_id"], {}))))
    definite = definite_state_before(plan.workflow, group, output_rows)
    for owner, path, references, types in owners:
        _owner_guards(owner, path, plan)
        for reference in dict.fromkeys(references):
            _external_read(
                reference,
                owner,
                path,
                local_names,
                outputs,
                types,
                plan,
                definite.get(str(owner.get("step_id") or ""), set()),
            )
    plan.interface["expected_inputs"] = [
        {"name": name, "type": schema["type"][0], "required": False}
        for name, schema in plan.interface["input_schema"]["properties"].items()
    ]
    member_ids = {step["step_id"] for step in members}
    parent_owners = (
        [
            (step, f"steps.{step['step_id']}", "step")
            for step in plan.workflow.get("steps", [])
            if step["step_id"] not in member_ids
        ]
        + [
            (other, f"groups.{other['group_id']}", "group")
            for other in plan.workflow.get("groups", [])
            if other.get("group_id") != group_id
        ]
        + [
            (other, f"for_each.{other['for_each_id']}", "for_each")
            for other in plan.workflow.get("for_each", [])
            if other.get("for_each_id") not in nested_ids
        ]
    )
    moved_messages = (
        "message.agent.",
        f"message.group.{group_id}.",
        *(f"message.step.{step['step_id']}." for step in members),
    )
    for owner, path, owner_kind in parent_owners:
        if owner.get("step_kind") == "agent":
            plan.refuse(
                "parent_agent_context",
                path,
                "A parent agent may consume the moved steps' message history; expose that dependency through explicit callable inputs and outputs first.",
            )
        authored = {
            key: owner[key]
            for key in (
                "override",
                "input_mapping_jsonb",
                "context_inject",
                "boundary_contract",
                "over",
            )
            if key in owner
        }
        for container, key, leaf_path in _leaves(authored, path):
            if _is_message_read(container[key], leaf_path) and any(
                container[key].strip().startswith(prefix) for prefix in moved_messages
            ):
                plan.refuse(
                    "moved_message_reference",
                    leaf_path,
                    "This reference names a message producer that would move to another job; declare a state output before converting.",
                )
        if owner_kind == "group":
            references = discover_group_state_selectors(owner, group_path=path)
        elif owner_kind == "for_each":
            references = discover_for_each_state_selectors(owner, for_each_path=path)
        else:
            references = discover_step_state_selectors(
                owner, step_path=path
            ) + _boundary_entry_selectors(owner, path)
        for ref in references:
            if any(
                ref.selector == f"state.{name}"
                or ref.selector.startswith((f"state.{name}.", f"state.{name}["))
                for name in local_names
            ):
                plan.refuse(
                    "for_each_local_escape",
                    ref.path,
                    "A step outside this group reads a For each item or index of it; expose an explicit output before converting.",
                )
    return plan


def _collect_target_guards(
    members: list[dict], outputs: Mapping[str, str], plan: GroupBindings
) -> None:
    """A moved Collect adds to a variable that must exist in the child too.

    The child starts with only its inputs, so a target the parent created
    before the group would be missing there. Conversion is refused until a
    moved step creates the target (ADR 0038: Collect never creates it).
    """
    created = {
        str(row_name)
        for member in members
        if member.get("step_kind") == "set_variable"
        for row_name in _set_variable_roots(member)
    }
    for member in members:
        if member.get("step_kind") != "collect":
            continue
        config = (member.get("override") or {}).get("collect") or {}
        target = str(config.get("target") or "").strip().removeprefix(_STATE_PREFIX)
        root = target.split(".", 1)[0].split("[", 1)[0]
        if root and root not in created:
            plan.refuse(
                "collect_target_boundary",
                f"steps.{member['step_id']}.override.collect.target",
                "This Collect adds to a variable created outside the group; create it inside the "
                "group (Set Variable) or keep the group in this workflow.",
            )
        elif root and root not in outputs:
            plan.refuse(
                "collect_target_boundary",
                f"steps.{member['step_id']}.override.collect.target",
                "The collected variable is not a typed output of the moved steps.",
            )


def _set_variable_roots(step: Mapping[str, Any]) -> set[str]:
    config = (step.get("override") or {}).get("set_variable") or {}
    targets = [config.get("target"), *dict(config.get("assignments") or {}).keys()]
    return {
        str(target).strip().removeprefix(_STATE_PREFIX).split(".", 1)[0].split("[", 1)[0]
        for target in targets
        if str(target or "").strip()
    }
