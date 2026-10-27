"""Change operations of a workflow definition: one operation at a time.

``workflow_definition_service.apply_workflow_changes`` loads the stored
definition, applies a change set through ``_ChangeSet`` and judges the result
as a whole; nothing here writes. Steps, Groups, For each containers and the
graph are addressed by their business ids (ADR 0035, ADR 0038).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SCHEMA_VERSION,
    EXECUTION_GRAPH_SETTING,
    NODE_KIND_FOR_EACH,
    NODE_KIND_GROUP,
    NODE_KIND_STEP,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    TARGET_KIND_FOR_EACH_EXIT,
    TARGET_KIND_GROUP_EXIT,
    parse_execution_graph,
)
from core.application.workflows.workflow_definition_validation import (
    TERMINAL_STEP_KINDS,
    step_outcomes,
)
from core.application.workflows.workflow_step_config_contracts import validate_step_config_keys
from core.application.workflows.workflow_step_kinds import STEP_KIND_FOR_EACH

_STRUCTURE_IN_CONFIG_CODE = "workflow.change.structure_in_config"
_FIELD_UNKNOWN_CODE = "workflow.change.field_unknown"

#: Step fields that make up its structure. A config operation cannot change
#: them; structure changes go through create/move/delete so the graph is
#: rewritten in the same set.
_STEP_STRUCTURE_FIELDS = frozenset({"step_id", "step_kind", "group_id", "for_each_id", "lane_id"})
_GROUP_STRUCTURE_FIELDS = frozenset({"group_id"})
_FOR_EACH_STRUCTURE_FIELDS = frozenset({"for_each_id", "group_id"})

OPERATION_KINDS = frozenset(
    {
        "create_step",
        "create_group",
        "create_for_each",
        "update_step_config",
        "change_step_kind",
        "update_group_config",
        "update_for_each_config",
        "set_entry",
        "connect",
        "disconnect",
        "insert_step_between",
        "move_step",
        "move_for_each",
        "delete_step",
        "delete_group",
        "delete_for_each",
    }
)


class WorkflowDefinitionChangeError(ValueError):
    """A change set was refused; nothing was written.

    ``issues`` carries the exact-path issues of the operation or of the
    resulting definition, in the shape the save guards already render.
    """

    def __init__(self, message: str, issues: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _refuse(code: str, message: str, path: str) -> WorkflowDefinitionChangeError:
    return WorkflowDefinitionChangeError(message, [_issue(code, message, path)])


def _config_patch(operation: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    """The non-empty ``patch`` object of a config operation."""
    patch = operation.get("patch")
    if not isinstance(patch, Mapping) or not patch:
        raise _refuse(
            "workflow.change.patch_invalid", "patch must be a non-empty object", f"{path}.patch"
        )
    return patch


def _structure_in_config(
    structural: list[str], path: str, hint: str = ""
) -> WorkflowDefinitionChangeError:
    """A config patch that names structure fields, which only structure operations change."""
    return _refuse(
        _STRUCTURE_IN_CONFIG_CODE,
        "A config change cannot change " + ", ".join(structural) + hint,
        f"{path}.patch",
    )


@dataclass
class _Definition:
    steps: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    for_each: list[dict[str, Any]]
    graph: dict[str, Any]

    def step(self, step_id: str) -> dict[str, Any] | None:
        return next((step for step in self.steps if step.get("step_id") == step_id), None)

    def group(self, group_id: str) -> dict[str, Any] | None:
        return next((group for group in self.groups if group.get("group_id") == group_id), None)

    def for_each_item(self, for_each_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.for_each if item.get("for_each_id") == for_each_id), None
        )

    def edges(self) -> list[dict[str, Any]]:
        return self.graph.setdefault("edges", [])

    def payload(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "groups": self.groups,
            "for_each": self.for_each,
            "execution_graph": self.graph,
        }


def definition_from_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The validator's definition shape of a loaded workflow."""
    settings = workflow.get("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    return {
        "steps": [_definition_step(step) for step in workflow.get("steps") or []],
        "groups": [_definition_group(group) for group in workflow.get("groups") or []],
        "for_each": [_definition_for_each(item) for item in workflow.get("for_each") or []],
        "execution_graph": settings.get(EXECUTION_GRAPH_SETTING),
    }


#: The skill is not among them: it is always derived from the step's agent, so a
#: definition never carries a stored one that could outlive an agent change.
_STEP_FIELDS = (
    "step_id",
    "step_kind",
    "group_id",
    "for_each_id",
    "lane_id",
    "block_key",
    "display_name",
    "participant_key",
    "agent_id",
    "connector_id",
    "connector_install_id",
    "workflow_id",
    "input_mapping_jsonb",
    "output_mapping_jsonb",
    "connection_binding_jsonb",
    "error_policy",
    "boundary_contract",
    "optional",
    "disabled",
    "context_inject",
    "override",
)
#: What a kind change keeps; everything else is the old kind's configuration.
_STEP_KIND_INDEPENDENT_FIELDS = (
    "step_id",
    "group_id",
    "for_each_id",
    "lane_id",
    "display_name",
    "error_policy",
    "boundary_contract",
    "optional",
    "disabled",
)
_GROUP_FIELDS = (
    "group_id",
    "display_name",
    "description",
    "body",
    "error_policy",
    "boundary_contract",
)
#: Fields of the pre-release loop group. They are carried to the validator,
#: which refuses them, instead of being dropped silently (ADR 0038).
_REMOVED_GROUP_FIELDS = ("kind", "loop")
_FOR_EACH_FIELDS = (
    "for_each_id",
    "group_id",
    "display_name",
    "description",
    "over",
    "item_name",
    "index_name",
    "body",
    "error_policy",
    "boundary_contract",
)


def _definition_step(step: Any) -> dict[str, Any]:
    raw = step if isinstance(step, Mapping) else {}
    payload = {key: copy.deepcopy(raw.get(key)) for key in _STEP_FIELDS if key in raw}
    payload["group_id"] = _text(raw.get("group_id")) or None
    payload["for_each_id"] = _text(raw.get("for_each_id")) or None
    payload["lane_id"] = _text(raw.get("lane_id")) or None
    return payload


def _definition_group(group: Any) -> dict[str, Any]:
    raw = group if isinstance(group, Mapping) else {}
    return {
        key: copy.deepcopy(raw.get(key))
        for key in (*_GROUP_FIELDS, *_REMOVED_GROUP_FIELDS)
        if key in raw
    }


def _definition_for_each(item: Any) -> dict[str, Any]:
    raw = item if isinstance(item, Mapping) else {}
    payload = {key: copy.deepcopy(raw.get(key)) for key in _FOR_EACH_FIELDS if key in raw}
    payload["group_id"] = _text(raw.get("group_id")) or None
    return payload


def removed_format_issues(definition: Mapping[str, Any]) -> list[dict[str, str]]:
    """Pre-release loop groups and inline For each steps, refused before any write.

    These shapes are not a draft the author can finish later: there is no
    converter, so even a disabled workflow is never stored with them.
    """
    issues: list[dict[str, str]] = []
    for index, group in enumerate(definition.get("groups") or []):
        if not isinstance(group, Mapping):
            continue
        for key in _REMOVED_GROUP_FIELDS:
            if key in group:
                group_id = _text(group.get("group_id")) or f"[{index}]"
                issues.append(
                    _issue(
                        "workflow.group.loop_removed",
                        f"Group field '{key}' is not accepted: a Group runs its body once, "
                        "and iteration is a For each container",
                        f"groups.{group_id}.{key}",
                    )
                )
    for index, step in enumerate(definition.get("steps") or []):
        if isinstance(step, Mapping) and _text(step.get("step_kind")) == STEP_KIND_FOR_EACH:
            step_id = _text(step.get("step_id")) or f"[{index}]"
            issues.append(
                _issue(
                    "workflow.step.for_each_inline_removed",
                    "The inline for_each step is not accepted; use a For each container",
                    f"steps.{step_id}.step_kind",
                )
            )
    return issues


def _refuse_removed_format(definition: Mapping[str, Any]) -> None:
    issues = removed_format_issues(definition)
    if issues:
        raise WorkflowDefinitionChangeError(
            "The definition uses a removed pre-release format; nothing was saved", issues
        )


def _load_definition(workflow: Mapping[str, Any]) -> _Definition:
    definition = definition_from_workflow(workflow)
    graph = definition["execution_graph"]
    graph = (
        copy.deepcopy(dict(graph))
        if isinstance(graph, Mapping)
        else {"schema_version": EXECUTION_GRAPH_SCHEMA_VERSION, "entry": None, "edges": []}
    )
    graph.setdefault("edges", [])
    return _Definition(
        steps=definition["steps"],
        groups=definition["groups"],
        for_each=definition["for_each"],
        graph=graph,
    )


def _node_ref(kind: str, node_id: str) -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def _edge_touches(edge: Mapping[str, Any], kind: str, node_id: str) -> bool:
    for end in ("source", "target"):
        ref = edge.get(end)
        if isinstance(ref, Mapping) and ref.get("kind") == kind and ref.get("id") == node_id:
            return True
    return False


def _edge_exits_group(edge: Mapping[str, Any], group_id: str) -> bool:
    target = edge.get("target")
    return (
        isinstance(target, Mapping)
        and target.get("kind") == TARGET_KIND_GROUP_EXIT
        and target.get("group_id") == group_id
    )


def _edge_exits_for_each(edge: Mapping[str, Any], for_each_id: str) -> bool:
    target = edge.get("target")
    return (
        isinstance(target, Mapping)
        and target.get("kind") == TARGET_KIND_FOR_EACH_EXIT
        and target.get("for_each_id") == for_each_id
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _same_port(
    edge: Mapping[str, Any], source: Mapping[str, Any], outcome: Mapping[str, Any]
) -> bool:
    edge_outcome = _mapping(edge.get("outcome"))
    return (
        edge.get("source") == dict(source)
        and edge_outcome.get("name") == outcome.get("name")
        and edge_outcome.get("case_id") == outcome.get("case_id")
    )


def _checked_edge(raw: Any, path: str) -> dict[str, Any]:
    """One edge in its stored shape; shape errors refuse the operation."""
    probe = {
        "schema_version": EXECUTION_GRAPH_SCHEMA_VERSION,
        "entry": _node_ref(NODE_KIND_STEP, "_probe"),
        "edges": [raw],
    }
    graph, issues = parse_execution_graph(probe, path=path)
    if graph is None:
        raise WorkflowDefinitionChangeError("Edge is invalid", issues)
    return graph.edges[0].dump()


def _checked_port(raw: Mapping[str, Any], path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (source, outcome) pair an operation addresses."""
    edge = _checked_edge(
        {
            "source": raw.get("source"),
            "outcome": raw.get("outcome"),
            "target": _node_ref(NODE_KIND_STEP, "_probe"),
        },
        path,
    )
    return edge["source"], edge["outcome"]


class _ChangeSet:
    def __init__(self, definition: _Definition) -> None:
        self.definition = definition

    def apply(self, index: int, operation: Any) -> None:
        path = f"operations[{index}]"
        if not isinstance(operation, Mapping):
            raise _refuse("workflow.change.invalid", "Operation must be an object", path)
        kind = _text(operation.get("op"))
        if kind not in OPERATION_KINDS:
            raise _refuse(
                "workflow.change.op_unknown",
                f"Unknown operation '{kind}'; expected one of "
                + ", ".join(sorted(OPERATION_KINDS)),
                f"{path}.op",
            )
        getattr(self, f"_{kind}")(operation, path)

    # --- nodes ------------------------------------------------------------------

    def _create_step(self, operation: Mapping[str, Any], path: str) -> None:
        step = _definition_step(operation.get("step"))
        step_id = _text(step.get("step_id"))
        if not step_id:
            raise _refuse(
                "workflow.change.step_id_missing",
                "A new step needs its step_id",
                f"{path}.step.step_id",
            )
        if self.definition.step(step_id):
            raise _refuse(
                "workflow.change.step_exists",
                f"Step '{step_id}' already exists",
                f"{path}.step.step_id",
            )
        if not _text(step.get("step_kind")):
            raise _refuse(
                "workflow.change.step_kind_missing",
                "A new step needs its step_kind",
                f"{path}.step.step_kind",
            )
        self._check_config_keys(step, f"{path}.step")
        self.definition.steps.append(step)
        if step.get("group_id") is None and step.get("for_each_id") is None:
            self._default_entry(_node_ref(NODE_KIND_STEP, step_id))

    def _create_group(self, operation: Mapping[str, Any], path: str) -> None:
        group = _definition_group(operation.get("group"))
        group_id = _text(group.get("group_id"))
        if not group_id:
            raise _refuse(
                "workflow.change.group_id_missing",
                "A new group needs its group_id",
                f"{path}.group.group_id",
            )
        if self.definition.group(group_id):
            raise _refuse(
                "workflow.change.group_exists",
                f"Group '{group_id}' already exists",
                f"{path}.group.group_id",
            )
        removed = [key for key in _REMOVED_GROUP_FIELDS if key in group]
        if removed:
            raise _refuse(
                "workflow.group.loop_removed",
                "A Group runs its body once and has no "
                + ", ".join(removed)
                + "; create a For each to iterate",
                f"{path}.group",
            )
        self.definition.groups.append(group)
        self._default_entry(_node_ref(NODE_KIND_GROUP, group_id))

    def _create_for_each(self, operation: Mapping[str, Any], path: str) -> None:
        item = _definition_for_each(operation.get("for_each"))
        for_each_id = _text(item.get("for_each_id"))
        if not for_each_id:
            raise _refuse(
                "workflow.change.for_each_id_missing",
                "A new For each needs its for_each_id",
                f"{path}.for_each.for_each_id",
            )
        if self.definition.for_each_item(for_each_id):
            raise _refuse(
                "workflow.change.for_each_exists",
                f"For each '{for_each_id}' already exists",
                f"{path}.for_each.for_each_id",
            )
        if item.get("group_id") is not None:
            self._require_group(_text(item.get("group_id")), f"{path}.for_each.group_id")
        self.definition.for_each.append(item)
        if item.get("group_id") is None:
            self._default_entry(_node_ref(NODE_KIND_FOR_EACH, for_each_id))

    def _default_entry(self, ref: dict[str, str]) -> None:
        """The first root node becomes the entry; the author may change it."""
        if self.definition.graph.get("entry") is None:
            self.definition.graph["entry"] = ref

    def _update_step_config(self, operation: Mapping[str, Any], path: str) -> None:
        step_id = _text(operation.get("step_id"))
        step = self._require_step(step_id, f"{path}.step_id")
        patch = _config_patch(operation, path)
        structural = sorted(set(patch) & _STEP_STRUCTURE_FIELDS)
        if structural:
            raise _structure_in_config(structural, path, "; create, move or delete the step")
        unknown = sorted(set(patch) - set(_STEP_FIELDS))
        if unknown:
            raise _refuse(
                _FIELD_UNKNOWN_CODE,
                "Unknown step field " + ", ".join(unknown),
                f"{path}.patch",
            )
        step.update(copy.deepcopy(dict(patch)))
        self._check_config_keys(step, f"{path}.patch")

    def _change_step_kind(self, operation: Mapping[str, Any], path: str) -> None:
        """Give a step another kind: a structural change that rewrites its ports.

        The step keeps its id, placement, name, error policy, boundary contract
        and its optional and disabled flags; its kind-specific configuration is
        replaced by the payload. Edges on outcomes the new kind does not have are dropped (the
        same set connects the new ports); an error edge stays unless the step
        becomes a Stop.
        """
        step_id = _text(operation.get("step_id"))
        step = self._require_step(step_id, f"{path}.step_id")
        payload = operation.get("step")
        if not isinstance(payload, Mapping):
            raise _refuse("workflow.change.invalid", "step must be an object", f"{path}.step")
        kind = _text(payload.get("step_kind"))
        if not kind:
            raise _refuse(
                "workflow.change.step_kind_missing",
                "A kind change needs the new step_kind",
                f"{path}.step.step_kind",
            )
        moved = sorted(
            key
            for key in ("step_id", "group_id", "for_each_id", "lane_id")
            if key in payload and payload.get(key) != step.get(key)
        )
        if moved:
            raise _refuse(
                _STRUCTURE_IN_CONFIG_CODE,
                "A kind change cannot change " + ", ".join(moved) + "; move or recreate the step",
                f"{path}.step",
            )
        unknown = sorted(set(payload) - set(_STEP_FIELDS))
        if unknown:
            raise _refuse(
                _FIELD_UNKNOWN_CODE,
                "Unknown step field " + ", ".join(unknown),
                f"{path}.step",
            )
        kept = {key: step[key] for key in _STEP_KIND_INDEPENDENT_FIELDS if key in step}
        step.clear()
        step.update({**kept, **copy.deepcopy(dict(payload)), "step_id": step_id})
        self._check_config_keys(step, f"{path}.step")
        own = step_outcomes(kind, _mapping(_mapping(step.get("override")).get(kind)))
        ports = {(item.name, item.case_id) for item in own}
        if kind not in TERMINAL_STEP_KINDS:
            ports.add((OUTCOME_ERROR, None))
        source = _node_ref(NODE_KIND_STEP, step_id)

        def stale(edge: Mapping[str, Any]) -> bool:
            outcome = _mapping(edge.get("outcome"))
            return (
                edge.get("source") == source
                and (
                    outcome.get("name"),
                    outcome.get("case_id"),
                )
                not in ports
            )

        self._drop_edges(stale)

    def _update_group_config(self, operation: Mapping[str, Any], path: str) -> None:
        group_id = _text(operation.get("group_id"))
        group = self._require_group(group_id, f"{path}.group_id")
        patch = _config_patch(operation, path)
        structural = sorted(set(patch) & _GROUP_STRUCTURE_FIELDS)
        if structural:
            raise _structure_in_config(structural, path)
        unknown = sorted(set(patch) - set(_GROUP_FIELDS))
        if unknown:
            raise _refuse(
                _FIELD_UNKNOWN_CODE,
                "Unknown group field " + ", ".join(unknown),
                f"{path}.patch",
            )
        group.update(copy.deepcopy(dict(patch)))

    def _update_for_each_config(self, operation: Mapping[str, Any], path: str) -> None:
        """Change a For each's source, names, body, policy or contract.

        A body kind change (sequence ↔ lanes) is a config change of the same
        set: the author moves and reconnects the steps explicitly; nothing is
        guessed and the validator judges the result.
        """
        for_each_id = _text(operation.get("for_each_id"))
        item = self._require_for_each(for_each_id, f"{path}.for_each_id")
        patch = _config_patch(operation, path)
        structural = sorted(set(patch) & _FOR_EACH_STRUCTURE_FIELDS)
        if structural:
            raise _structure_in_config(structural, path, "; move or recreate the For each")
        unknown = sorted(set(patch) - set(_FOR_EACH_FIELDS))
        if unknown:
            raise _refuse(
                _FIELD_UNKNOWN_CODE,
                "Unknown For each field " + ", ".join(unknown),
                f"{path}.patch",
            )
        item.update(copy.deepcopy(dict(patch)))

    def _move_step(self, operation: Mapping[str, Any], path: str) -> None:
        """Change a step's membership; every edge it had is dropped.

        An edge cannot cross scopes, so the old edges cannot survive a move. The
        same change set connects the step again, explicitly.
        """
        step_id = _text(operation.get("step_id"))
        step = self._require_step(step_id, f"{path}.step_id")
        group_id = _text(operation.get("group_id")) or None
        for_each_id = _text(operation.get("for_each_id")) or None
        lane_id = _text(operation.get("lane_id")) or None
        if group_id is not None and for_each_id is not None:
            raise _refuse(
                "workflow.step.owner_conflict",
                "A step has one immediate container: name a group or a For each",
                path,
            )
        if group_id is not None:
            self._require_group(group_id, f"{path}.group_id")
        if for_each_id is not None:
            self._require_for_each(for_each_id, f"{path}.for_each_id")
        step["group_id"] = group_id
        step["for_each_id"] = for_each_id
        step["lane_id"] = lane_id
        self._drop_edges(lambda edge: _edge_touches(edge, NODE_KIND_STEP, step_id))
        self._drop_entry_refs(_node_ref(NODE_KIND_STEP, step_id))

    def _move_for_each(self, operation: Mapping[str, Any], path: str) -> None:
        """Move a For each between the root and a sequence Group, with its body.

        Like a step move, the edges that touched the For each node are dropped
        and the same set reconnects it.
        """
        for_each_id = _text(operation.get("for_each_id"))
        item = self._require_for_each(for_each_id, f"{path}.for_each_id")
        group_id = _text(operation.get("group_id")) or None
        if group_id is not None:
            self._require_group(group_id, f"{path}.group_id")
        item["group_id"] = group_id
        self._drop_edges(lambda edge: _edge_touches(edge, NODE_KIND_FOR_EACH, for_each_id))
        self._drop_entry_refs(_node_ref(NODE_KIND_FOR_EACH, for_each_id))

    def _delete_step(self, operation: Mapping[str, Any], path: str) -> None:
        """Remove a step and its edges. No bypass is built around it."""
        step_id = _text(operation.get("step_id"))
        self._require_step(step_id, f"{path}.step_id")
        self.definition.steps = [
            step for step in self.definition.steps if step.get("step_id") != step_id
        ]
        self._drop_edges(lambda edge: _edge_touches(edge, NODE_KIND_STEP, step_id))
        self._drop_entry_refs(_node_ref(NODE_KIND_STEP, step_id))

    def _delete_group(self, operation: Mapping[str, Any], path: str) -> None:
        """Remove a group with its contents. There is no automatic ungroup."""
        group_id = _text(operation.get("group_id"))
        self._require_group(group_id, f"{path}.group_id")
        if operation.get("with_contents") is not True:
            raise _refuse(
                "workflow.change.group_delete_contents",
                "Deleting a group removes its steps; pass with_contents=true, or move the steps out first",
                f"{path}.with_contents",
            )
        nested = [
            _text(item.get("for_each_id"))
            for item in self.definition.for_each
            if item.get("group_id") == group_id
        ]
        for for_each_id in nested:
            self._remove_for_each(for_each_id)
        removed = {
            _text(step.get("step_id"))
            for step in self.definition.steps
            if step.get("group_id") == group_id
        }
        self.definition.steps = [
            step for step in self.definition.steps if step.get("group_id") != group_id
        ]
        self.definition.groups = [
            group for group in self.definition.groups if group.get("group_id") != group_id
        ]
        self._drop_edges(
            lambda edge: (
                _edge_touches(edge, NODE_KIND_GROUP, group_id)
                or _edge_exits_group(edge, group_id)
                or any(_edge_touches(edge, NODE_KIND_STEP, step_id) for step_id in removed)
            )
        )
        entry = self.definition.graph.get("entry")
        if entry == _node_ref(NODE_KIND_GROUP, group_id):
            self.definition.graph["entry"] = None

    def _delete_for_each(self, operation: Mapping[str, Any], path: str) -> None:
        """Remove a For each with its body steps. There is no automatic unwrap."""
        for_each_id = _text(operation.get("for_each_id"))
        self._require_for_each(for_each_id, f"{path}.for_each_id")
        if operation.get("with_contents") is not True:
            raise _refuse(
                "workflow.change.for_each_delete_contents",
                "Deleting a For each removes its steps; pass with_contents=true, or move the "
                "steps out first",
                f"{path}.with_contents",
            )
        self._remove_for_each(for_each_id)

    def _remove_for_each(self, for_each_id: str) -> None:
        removed = {
            _text(step.get("step_id"))
            for step in self.definition.steps
            if step.get("for_each_id") == for_each_id
        }
        self.definition.steps = [
            step for step in self.definition.steps if step.get("for_each_id") != for_each_id
        ]
        self.definition.for_each = [
            item for item in self.definition.for_each if item.get("for_each_id") != for_each_id
        ]
        self._drop_edges(
            lambda edge: (
                _edge_touches(edge, NODE_KIND_FOR_EACH, for_each_id)
                or _edge_exits_for_each(edge, for_each_id)
                or any(_edge_touches(edge, NODE_KIND_STEP, step_id) for step_id in removed)
            )
        )
        self._drop_entry_refs(_node_ref(NODE_KIND_FOR_EACH, for_each_id))

    # --- graph --------------------------------------------------------------------

    def _set_entry(self, operation: Mapping[str, Any], path: str) -> None:
        """Set the root entry, or the entry of a container's sequence body or lane.

        A body or lane entry is a step; a sequence Group body may also start at
        one of its For each containers.
        """
        target = operation.get("target")
        group_id = _text(operation.get("group_id")) or None
        for_each_id = _text(operation.get("for_each_id")) or None
        if group_id is None and for_each_id is None:
            edge = _checked_edge(
                {
                    "source": _node_ref(NODE_KIND_STEP, "_probe"),
                    "outcome": {"name": OUTCOME_SUCCESS},
                    "target": target,
                },
                f"{path}.target",
            )
            self.definition.graph["entry"] = edge["target"]
            return
        if group_id is not None and for_each_id is not None:
            raise _refuse(
                "workflow.change.entry_invalid",
                "Name the group or the For each whose entry is set, not both",
                path,
            )
        owner = (
            self._require_group(group_id, f"{path}.group_id")
            if group_id is not None
            else self._require_for_each(_text(for_each_id), f"{path}.for_each_id")
        )
        lane_id = _text(operation.get("lane_id")) or None
        entry_kinds = (
            {NODE_KIND_STEP, NODE_KIND_FOR_EACH}
            if group_id is not None and lane_id is None
            else {NODE_KIND_STEP}
        )
        node_id = _text(target.get("id")) if isinstance(target, Mapping) else ""
        if (
            not isinstance(target, Mapping)
            or target.get("kind") not in entry_kinds
            or not node_id
            or set(target) - {"kind", "id"}
        ):
            raise _refuse(
                "workflow.change.entry_invalid",
                "A body or lane entry is a step; a sequence Group body may start at a For each",
                f"{path}.target",
            )
        entry = _node_ref(str(target["kind"]), node_id)
        body = _mapping(owner.get("body"))
        if lane_id is None:
            body["entry"] = entry
        else:
            raw_lanes = body.get("lanes")
            lanes = raw_lanes if isinstance(raw_lanes, list) else []
            lane = next(
                (
                    item
                    for item in lanes
                    if isinstance(item, dict) and item.get("lane_id") == lane_id
                ),
                None,
            )
            if lane is None:
                raise _refuse(
                    "workflow.change.lane_unknown",
                    f"'{group_id or for_each_id}' has no lane '{lane_id}'",
                    f"{path}.lane_id",
                )
            lane["entry"] = entry
        owner["body"] = body

    def _connect(self, operation: Mapping[str, Any], path: str) -> None:
        edge = _checked_edge(operation.get("edge"), f"{path}.edge")
        existing = self._port_edge(edge["source"], edge["outcome"])
        if existing is not None and operation.get("replace") is not True:
            raise _refuse(
                "workflow.change.outcome_taken",
                "This outcome already has an edge; replace it explicitly with replace=true",
                f"{path}.edge.outcome",
            )
        if existing is not None:
            self.definition.edges().remove(existing)
        self.definition.edges().append(edge)

    def _disconnect(self, operation: Mapping[str, Any], path: str) -> None:
        source, outcome = _checked_port(operation, path)
        existing = self._port_edge(source, outcome)
        if existing is None:
            raise _refuse(
                "workflow.change.edge_unknown",
                "There is no edge on this outcome",
                f"{path}.outcome",
            )
        self.definition.edges().remove(existing)

    def _insert_step_between(self, operation: Mapping[str, Any], path: str) -> None:
        """Replace A.outcome→B with A.outcome→N and N.success→B.

        Only an ordinary step fits this shortcut: a branching, waiting or Stop
        step has other outcomes the author must route explicitly.
        """
        source, outcome = _checked_port(operation, path)
        existing = self._port_edge(source, outcome)
        if existing is None:
            raise _refuse(
                "workflow.change.edge_unknown",
                "There is no edge on this outcome",
                f"{path}.outcome",
            )
        step = _definition_step(operation.get("step"))
        kind = _text(step.get("step_kind"))
        own = step_outcomes(kind, (step.get("override") or {}).get(kind) or {})
        if kind in TERMINAL_STEP_KINDS or [item.name for item in own] != [OUTCOME_SUCCESS]:
            raise _refuse(
                "workflow.change.insert_kind_unsupported",
                f"A {kind or 'step'} step has more than one outcome; create it and connect each outcome",
                f"{path}.step.step_kind",
            )
        # The new step joins the scope of the edge it splits: the source's own
        # scope, which for a For each node is the Group it sits in.
        source_id = _text(source.get("id"))
        source_step = (
            self.definition.step(source_id) if source.get("kind") == NODE_KIND_STEP else None
        )
        source_for_each = (
            self.definition.for_each_item(source_id)
            if source.get("kind") == NODE_KIND_FOR_EACH
            else None
        )
        if source_step:
            step["group_id"] = source_step.get("group_id")
            step["for_each_id"] = source_step.get("for_each_id")
            step["lane_id"] = source_step.get("lane_id")
        else:
            step["group_id"] = source_for_each.get("group_id") if source_for_each else None
            step["for_each_id"] = None
            step["lane_id"] = None
        self._create_step({"step": step}, path)
        step_id = _text(step.get("step_id"))
        index = self.definition.edges().index(existing)
        to_new = {**existing, "target": _node_ref(NODE_KIND_STEP, step_id)}
        to_old = {
            "source": _node_ref(NODE_KIND_STEP, step_id),
            "outcome": {"name": OUTCOME_SUCCESS},
            "target": existing["target"],
            "rework": False,
        }
        self.definition.edges()[index : index + 1] = [to_new, to_old]

    # --- helpers --------------------------------------------------------------------

    def _port_edge(
        self, source: Mapping[str, Any], outcome: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        return next(
            (edge for edge in self.definition.edges() if _same_port(edge, source, outcome)), None
        )

    def _drop_edges(self, predicate: Callable[[Mapping[str, Any]], bool]) -> None:
        self.definition.graph["edges"] = [
            edge for edge in self.definition.edges() if not predicate(edge)
        ]

    def _drop_entry_refs(self, ref: dict[str, str]) -> None:
        """Clear entries that name the node; a replacement is set explicitly."""
        if self.definition.graph.get("entry") == ref:
            self.definition.graph["entry"] = None
        for container in [*self.definition.groups, *self.definition.for_each]:
            body = _mapping(container.get("body"))
            if body.get("entry") == ref:
                body.pop("entry")
            for lane in body.get("lanes") or []:
                if isinstance(lane, dict) and lane.get("entry") == ref:
                    lane.pop("entry")

    def _require_step(self, step_id: str, path: str) -> dict[str, Any]:
        step = self.definition.step(step_id) if step_id else None
        if step is None:
            raise _refuse("workflow.change.step_unknown", f"Step '{step_id}' does not exist", path)
        return step

    def _require_group(self, group_id: str, path: str) -> dict[str, Any]:
        group = self.definition.group(group_id) if group_id else None
        if group is None:
            raise _refuse(
                "workflow.change.group_unknown", f"Group '{group_id}' does not exist", path
            )
        return group

    def _require_for_each(self, for_each_id: str, path: str) -> dict[str, Any]:
        item = self.definition.for_each_item(for_each_id) if for_each_id else None
        if item is None:
            raise _refuse(
                "workflow.change.for_each_unknown",
                f"For each '{for_each_id}' does not exist",
                path,
            )
        return item

    @staticmethod
    def _check_config_keys(step: Mapping[str, Any], path: str) -> None:
        issues = validate_step_config_keys(
            step_kind=_text(step.get("step_kind")),
            override=step.get("override"),
            step_path=path,
        )
        if issues:
            raise WorkflowDefinitionChangeError("Step config has unknown keys", issues)
