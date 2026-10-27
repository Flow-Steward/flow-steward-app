"""Pure workflow step/container shape, configuration and outcome rules.

The whole-definition validator composes these rules with graph reachability and
child-invocation checks. These rules never load or mutate a definition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.application.workflows.execution_graph import (
    NODE_KIND_FOR_EACH,
    NODE_KIND_STEP,
    OUTCOME_APPROVED,
    OUTCOME_CASE,
    OUTCOME_DEFAULT,
    OUTCOME_FALSE,
    OUTCOME_REJECTED,
    OUTCOME_SUCCESS,
    OUTCOME_TRUE,
    ROOT_SCOPE,
    NodeRef,
    Outcome,
    Scope,
    for_each_scope,
    group_scope,
)
from core.application.workflows.workflow_step_config_contracts import STEP_CONFIG_ALLOWED_KEYS
from core.application.workflows.workflow_step_kinds import (
    FOR_EACH_TERMINAL_STEP_KINDS,
    STEP_KIND_AGENT,
    STEP_KIND_APPROVAL,
    STEP_KIND_BATCH,
    STEP_KIND_CONNECTOR,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_FILE_FROM_BASE64,
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
    STEP_KIND_WAIT,
    SUPPORTED_STEP_KINDS,
)

if TYPE_CHECKING:
    from core.application.workflows.workflow_for_each_rules import _ForEach

BODY_KIND_SEQUENCE = "sequence"
BODY_KIND_LANES = "lanes"
BODY_KINDS = frozenset({BODY_KIND_SEQUENCE, BODY_KIND_LANES})

DEFINITION_STEP_KINDS = frozenset(SUPPORTED_STEP_KINDS | {STEP_KIND_APPROVAL, STEP_KIND_WAIT})
STOP_STEP_KINDS = frozenset({STEP_KIND_STOP_SUCCESS, STEP_KIND_STOP_FAIL})
#: Steps without any outgoing edge: Stop ends the workflow, Break/Continue the
#: current For each item or the whole For each.
TERMINAL_STEP_KINDS = STOP_STEP_KINDS | FOR_EACH_TERMINAL_STEP_KINDS
WAITING_STEP_KINDS = frozenset({STEP_KIND_APPROVAL, STEP_KIND_WAIT})
#: Kinds a parallel lane runs through the existing executors. Branching, Stop,
#: Approval and Wait are not lane work; a lane ends at its group_exit.
LANE_STEP_KINDS = frozenset(
    {
        STEP_KIND_AGENT,
        STEP_KIND_CONNECTOR,
        STEP_KIND_INVOKE_WORKFLOW,
        STEP_KIND_HTTP_REQUEST,
        STEP_KIND_DATASET_QUERY,
        STEP_KIND_TRANSFORM,
        STEP_KIND_TABULAR_FEED,
        STEP_KIND_FILE_FROM_BASE64,
        STEP_KIND_TEMPLATE,
        STEP_KIND_SET_VARIABLE,
        STEP_KIND_BATCH,
    }
)

ERROR_POLICY_FAIL = "fail"
ERROR_POLICY_CONTINUE = "continue"
ERROR_POLICY_RETRY = "retry"
ERROR_POLICY_ROUTE_TO_STEP = "route_to_step"
ERROR_POLICY_AWAIT_APPROVAL = "await_approval"
ERROR_POLICY_TOKENS = frozenset(
    {
        ERROR_POLICY_FAIL,
        ERROR_POLICY_CONTINUE,
        ERROR_POLICY_RETRY,
        ERROR_POLICY_ROUTE_TO_STEP,
        ERROR_POLICY_AWAIT_APPROVAL,
    }
)
_ERROR_POLICY_KEYS = frozenset({"on_error", "max_retries", "backoff_seconds", "retry_on"})

#: Kinds whose config shape this validator checks beyond its allowed keys.
GRAPH_OWNED_CONFIG_KINDS = frozenset(
    {STEP_KIND_IF, STEP_KIND_SWITCH, STEP_KIND_APPROVAL, STEP_KIND_WAIT}
)
_SWITCH_CASE_KEYS = frozenset({"case_id", "when", "label"})

WAIT_DURATION_MS_MAX = 31_536_000_000

_BOUNDARY_KEYS = frozenset({"entry", "exit", "exit_on"})
_GROUP_BOUNDARY_KEYS = frozenset({"entry", "exit"})

#: Step fields of the phase-era format. A definition carrying any of them is
#: refused rather than read, so nothing old is silently dropped or reinterpreted.
_LEGACY_STEP_KEYS = (
    "phase_id",
    "parallel_group",
    "batch_policy",
    "batch_policy_jsonb",
    "execution_policy",
    "execution_policy_jsonb",
)
_LEGACY_GROUP_KEYS = (
    "phase_id",
    "phase_kind",
    "gate",
    "transitions",
    "transitions_jsonb",
    "on_failure",
    "retry_policy",
    "retry_policy_jsonb",
    "timeout_policy",
    "timeout_policy_jsonb",
    "sla_policy",
    "sla_policy_jsonb",
    "form_config",
    "form_config_jsonb",
    "expected_inputs",
    "required_outputs",
    "steps",
)
#: A Group runs its body once; iteration is a For each container.
_REMOVED_GROUP_KEYS = ("kind", "loop")


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _item_path(collection: str, index: int, raw: Any, id_key: str) -> str:
    """``steps.<id>`` once an item has an id, so paths survive reordering.

    Comparing issues before and after an edit must not see a moved step's
    unchanged issue as a new one.
    """
    item_id = _text(raw.get(id_key)) if isinstance(raw, Mapping) else None
    return f"{collection}.{item_id}" if item_id else f"{collection}[{index}]"


def _is_present(value: Any) -> bool:
    return value not in (None, "", {}, [])


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class _Group:
    group_id: str
    body_kind: str
    sequence_entry: NodeRef | None
    lanes: dict[str, str]
    error_policy: str | None
    path: str

    @property
    def scope(self) -> Scope:
        return ROOT_SCOPE

    def body_scope(self, lane_id: str | None) -> Scope:
        return group_scope(self.group_id, lane_id)


@dataclass(frozen=True)
class _Step:
    step_id: str
    kind: str
    group_id: str | None
    for_each_id: str | None
    lane_id: str | None
    config: Mapping[str, Any]
    error_policy: str | None
    boundary: Mapping[str, Any] | None
    child_workflow_id: str | None
    path: str

    @property
    def scope(self) -> Scope:
        if self.for_each_id is not None:
            return for_each_scope(self.for_each_id, self.lane_id)
        if self.group_id is not None:
            return group_scope(self.group_id, self.lane_id)
        return ROOT_SCOPE


@dataclass(frozen=True)
class ChildDefinition:
    """A child workflow definition at the revision a run would launch.

    ``owner`` is its hook binding scope (a ``HookOwner``) in the project the
    chain is walked for; without it the child's own hooks are not followed.
    """

    workflow_id: str
    revision: int
    definition: Mapping[str, Any]
    owner: Any = None


ChildLoader = Callable[[str], ChildDefinition | None]


@dataclass
class ChildChainScan:
    """What one walk of an invocation chain found.

    ``revisions`` records the revision of every child that was checked, so the
    launch of a child can compare the revision it pins with the one judged here.
    """

    waits: list[dict[str, Any]] = field(default_factory=list)
    revisions: dict[str, int] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)


def _error_policy_token(raw: Any, path: str, issues: list[dict[str, str]]) -> str | None:
    if raw in (None, {}, ""):
        return None
    if not isinstance(raw, Mapping):
        issues.append(
            _issue("workflow.error_policy.invalid", "error_policy must be an object", path)
        )
        return None
    for key in sorted(set(raw) - _ERROR_POLICY_KEYS):
        issues.append(
            _issue(
                "workflow.error_policy.key_unknown",
                f"Unknown error_policy key '{key}'; a route target is the step's error edge",
                f"{path}.{key}",
            )
        )
    token = raw.get("on_error")
    if token not in ERROR_POLICY_TOKENS:
        issues.append(
            _issue(
                "workflow.error_policy.on_error_invalid",
                "error_policy.on_error must be one of " + ", ".join(sorted(ERROR_POLICY_TOKENS)),
                f"{path}.on_error",
            )
        )
        return None
    return str(token)


def _legacy_key_issues(
    raw: Mapping[str, Any], keys: Sequence[str], path: str, what: str
) -> list[dict[str, str]]:
    return [
        _issue(
            "workflow.definition.legacy_key",
            f"{what} field '{key}' belongs to the phase-based format and is not accepted",
            f"{path}.{key}",
        )
        for key in keys
        if _is_present(raw.get(key))
    ]


def _collect_groups(raw_groups: Any, issues: list[dict[str, str]]) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}
    if raw_groups is None:
        return groups
    if not isinstance(raw_groups, list):
        issues.append(_issue("workflow.groups.invalid", "groups must be a list", "groups"))
        return groups
    for index, raw in enumerate(raw_groups):
        path = _item_path("groups", index, raw, "group_id")
        if not isinstance(raw, Mapping):
            issues.append(_issue("workflow.group.invalid", "Group must be an object", path))
            continue
        issues.extend(_legacy_key_issues(raw, _LEGACY_GROUP_KEYS, path, "Group"))
        issues.extend(
            _issue(
                "workflow.group.loop_removed",
                f"Group field '{key}' is not accepted: a Group runs its body once, "
                "and iteration is a For each container",
                f"{path}.{key}",
            )
            for key in _REMOVED_GROUP_KEYS
            if key in raw
        )
        group_id = _text(raw.get("group_id"))
        if group_id is None:
            issues.append(
                _issue("workflow.group.id_missing", "Group needs a group_id", f"{path}.group_id")
            )
            continue
        if group_id in groups:
            issues.append(
                _issue(
                    "workflow.group.id_duplicate",
                    f"Group id '{group_id}' is used twice",
                    f"{path}.group_id",
                )
            )
            continue
        issues.extend(
            _boundary_shape_issues(
                raw.get("boundary_contract"), f"{path}.boundary_contract", group=True
            )
        )
        body_kind, sequence_entry, lanes = _parse_body(
            raw.get("body"), owner="group", path=f"{path}.body", issues=issues
        )
        groups[group_id] = _Group(
            group_id=group_id,
            body_kind=body_kind,
            sequence_entry=sequence_entry,
            lanes=lanes,
            error_policy=_error_policy_token(
                raw.get("error_policy"), f"{path}.error_policy", issues
            ),
            path=path,
        )
    return groups


def _step_ref_id(raw: Any) -> str | None:
    ref = _node_ref(raw, {NODE_KIND_STEP})
    return ref.id if ref else None


def _node_ref(raw: Any, kinds: set[str]) -> NodeRef | None:
    if isinstance(raw, Mapping) and raw.get("kind") in kinds and set(raw) <= {"kind", "id"}:
        node_id = _text(raw.get("id"))
        return NodeRef(str(raw["kind"]), node_id) if node_id else None
    return None


def _parse_body(
    raw: Any, *, owner: str, path: str, issues: list[dict[str, str]]
) -> tuple[str, NodeRef | None, dict[str, str]]:
    """Parse a sequence or lanes body of a Group (*owner* ``group``) or For each.

    Only a sequence Group body may be entered at a For each node.
    """
    if not isinstance(raw, Mapping):
        issues.append(_issue(f"workflow.{owner}.body_missing", "The container needs a body", path))
        return "", None, {}
    body_kind = raw.get("kind")
    if body_kind not in BODY_KINDS:
        issues.append(
            _issue(
                f"workflow.{owner}.body_kind_invalid",
                "A body must be sequence or lanes",
                f"{path}.kind",
            )
        )
        return "", None, {}
    if body_kind == BODY_KIND_SEQUENCE:
        for key in sorted(set(raw) - {"kind", "entry"}):
            issues.append(
                _issue(
                    f"workflow.{owner}.body_key_unknown",
                    f"Unknown body key '{key}'",
                    f"{path}.{key}",
                )
            )
        entry_kinds = {NODE_KIND_STEP, NODE_KIND_FOR_EACH} if owner == "group" else {NODE_KIND_STEP}
        entry = _node_ref(raw.get("entry"), entry_kinds)
        if entry is None:
            issues.append(
                _issue(
                    f"workflow.{owner}.body_entry_missing",
                    "A sequence body needs an entry node",
                    f"{path}.entry",
                )
            )
        return BODY_KIND_SEQUENCE, entry, {}
    for key in sorted(set(raw) - {"kind", "lanes", "concurrency_limit"}):
        issues.append(
            _issue(
                f"workflow.{owner}.body_key_unknown", f"Unknown body key '{key}'", f"{path}.{key}"
            )
        )
    limit = raw.get("concurrency_limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        issues.append(
            _issue(
                f"workflow.{owner}.concurrency_limit_invalid",
                "concurrency_limit must be a positive integer",
                f"{path}.concurrency_limit",
            )
        )
    lanes: dict[str, str] = {}
    raw_lanes = raw.get("lanes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        issues.append(
            _issue(
                f"workflow.{owner}.lanes_missing",
                "A lanes body needs at least one lane",
                f"{path}.lanes",
            )
        )
        return BODY_KIND_LANES, None, lanes
    for index, lane in enumerate(raw_lanes):
        lane_path = f"{path}.lanes[{index}]"
        lane_id = _text(lane.get("lane_id")) if isinstance(lane, Mapping) else None
        if lane_id is None:
            issues.append(
                _issue(f"workflow.{owner}.lane_id_missing", "Lane needs a lane_id", lane_path)
            )
            continue
        if lane_id in lanes:
            issues.append(
                _issue(
                    f"workflow.{owner}.lane_id_duplicate",
                    f"Lane id '{lane_id}' is used twice",
                    f"{lane_path}.lane_id",
                )
            )
            continue
        for key in sorted(set(lane) - {"lane_id", "entry"}):
            issues.append(
                _issue(
                    f"workflow.{owner}.lane_key_unknown",
                    f"Unknown lane key '{key}'",
                    f"{lane_path}.{key}",
                )
            )
        entry = _step_ref_id(lane.get("entry"))
        if entry is None:
            issues.append(
                _issue(
                    f"workflow.{owner}.lane_entry_missing",
                    "Lane needs an entry step",
                    f"{lane_path}.entry",
                )
            )
            continue
        lanes[lane_id] = entry
    return BODY_KIND_LANES, None, lanes


def _collect_steps(
    raw_steps: Any,
    groups: Mapping[str, _Group],
    containers: Mapping[str, _ForEach],
    issues: list[dict[str, str]],
) -> dict[str, _Step]:
    steps: dict[str, _Step] = {}
    if raw_steps is None:
        return steps
    if not isinstance(raw_steps, list):
        issues.append(_issue("workflow.steps.invalid", "steps must be a list", "steps"))
        return steps
    for index, raw in enumerate(raw_steps):
        path = _item_path("steps", index, raw, "step_id")
        if not isinstance(raw, Mapping):
            issues.append(_issue("workflow.step.invalid", "Step must be an object", path))
            continue
        issues.extend(_legacy_key_issues(raw, _LEGACY_STEP_KEYS, path, "Step"))
        step_id = _text(raw.get("step_id"))
        if step_id is None:
            issues.append(
                _issue("workflow.step.id_missing", "Step needs a step_id", f"{path}.step_id")
            )
            continue
        if step_id in steps:
            issues.append(
                _issue(
                    "workflow.step.id_duplicate",
                    f"Step id '{step_id}' is used twice",
                    f"{path}.step_id",
                )
            )
            continue
        # A step without a kind is an agent step, as the stored column defaults.
        kind = str(raw.get("step_kind") or STEP_KIND_AGENT).strip().lower()
        if kind not in DEFINITION_STEP_KINDS:
            issues.append(
                _issue(
                    "workflow.step.kind_unsupported",
                    f"Step kind '{kind}' is not supported",
                    f"{path}.step_kind",
                )
            )
            continue
        owners: dict[str, str | None] = {}
        owner_ok = True
        for key in ("group_id", "for_each_id"):
            value = raw.get(key)
            owners[key] = _text(value) if value is not None else None
            if value is not None and owners[key] is None:
                owner_ok = False
                issues.append(
                    _issue(
                        f"workflow.step.{key.removesuffix('_id')}_invalid",
                        f"{key} must be a non-empty string or null",
                        f"{path}.{key}",
                    )
                )
        if not owner_ok:
            continue
        group_id, for_each_id = owners["group_id"], owners["for_each_id"]
        lane_id = _text(raw.get("lane_id")) if raw.get("lane_id") is not None else None
        if group_id is not None and for_each_id is not None:
            issues.append(
                _issue(
                    "workflow.step.owner_conflict",
                    f"Step '{step_id}' names both a group and a For each; a step has one "
                    "immediate container",
                    path,
                )
            )
            continue
        owner: _Group | _ForEach | None = (
            containers.get(for_each_id) if for_each_id is not None else groups.get(group_id or "")
        )
        if not _membership_ok(
            step_id,
            for_each_id or group_id,
            "For each" if for_each_id is not None else "group",
            owner,
            lane_id,
            path,
            issues,
        ):
            continue
        override = _mapping(raw.get("override"))
        child = _text(raw.get("workflow_id")) or _text(
            _mapping(override.get(STEP_KIND_INVOKE_WORKFLOW)).get("workflow_id")
        )
        boundary = raw.get("boundary_contract")
        steps[step_id] = _Step(
            step_id=step_id,
            kind=kind,
            group_id=group_id,
            for_each_id=for_each_id,
            lane_id=lane_id,
            config=_mapping(override.get(kind)),
            error_policy=_error_policy_token(
                raw.get("error_policy"), f"{path}.error_policy", issues
            ),
            boundary=boundary if isinstance(boundary, Mapping) else None,
            child_workflow_id=child if kind == STEP_KIND_INVOKE_WORKFLOW else None,
            path=path,
        )
        issues.extend(_boundary_shape_issues(boundary, f"{path}.boundary_contract", group=False))
        issues.extend(_graph_owned_config_issues(kind, override, path))
        issues.extend(_disabled_issues(raw.get("disabled"), kind, override, path))
    return steps


#: Why a step without a single Next outcome cannot be disabled: a skipped step
#: continues on its Next edge, and a branching, waiting-for-decision or Stop
#: step has no single Next edge to continue on.
DISABLED_STEP_NEEDS_NEXT_CODE = "workflow.step.disabled_needs_next"
DISABLED_STEP_NEEDS_NEXT_MESSAGE = "Only a step with one Next outcome can be disabled."


def step_can_be_disabled(kind: str, config: Mapping[str, Any]) -> bool:
    """Whether a run can skip a step of *kind*: it has exactly one Next outcome."""
    return step_outcomes(kind, config) == [Outcome(OUTCOME_SUCCESS)]


def _disabled_issues(
    raw: Any, kind: str, override: Mapping[str, Any], path: str
) -> list[dict[str, str]]:
    """A ``disabled`` flag that is not a boolean, or set on a step a run cannot skip."""
    if raw is None or raw is False:
        return []
    if raw is not True:
        return [
            _issue(
                "workflow.step.disabled_invalid",
                "disabled must be true or false",
                f"{path}.disabled",
            )
        ]
    if step_can_be_disabled(kind, _mapping(override.get(kind))):
        return []
    return [
        _issue(DISABLED_STEP_NEEDS_NEXT_CODE, DISABLED_STEP_NEEDS_NEXT_MESSAGE, f"{path}.disabled")
    ]


def _membership_ok(
    step_id: str,
    owner_id: str | None,
    owner_what: str,
    owner: _Group | _ForEach | None,
    lane_id: str | None,
    path: str,
    issues: list[dict[str, str]],
) -> bool:
    if owner_id is None:
        if lane_id is not None:
            issues.append(
                _issue(
                    "workflow.step.lane_without_group",
                    f"Step '{step_id}' names a lane but no container",
                    f"{path}.lane_id",
                )
            )
            return False
        return True
    owner_key = "for_each_id" if owner_what == "For each" else "group_id"
    if owner is None:
        issues.append(
            _issue(
                "workflow.step.group_unknown",
                f"Step '{step_id}' belongs to unknown {owner_what} '{owner_id}'",
                f"{path}.{owner_key}",
            )
        )
        return False
    if owner.body_kind == BODY_KIND_LANES:
        if lane_id is None or lane_id not in owner.lanes:
            issues.append(
                _issue(
                    "workflow.step.lane_unknown",
                    f"Step '{step_id}' must belong to a lane declared by {owner_what} '{owner_id}'",
                    f"{path}.lane_id",
                )
            )
            return False
        return True
    if lane_id is not None:
        issues.append(
            _issue(
                "workflow.step.lane_unexpected",
                f"{owner_what} '{owner_id}' has a sequence body; its steps have no lane",
                f"{path}.lane_id",
            )
        )
        return False
    return True


def _boundary_shape_issues(raw: Any, path: str, *, group: bool) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [_issue("workflow.boundary.invalid", "boundary_contract must be an object", path)]
    allowed = _GROUP_BOUNDARY_KEYS if group else _BOUNDARY_KEYS
    issues = [
        _issue(
            "workflow.boundary.key_unknown",
            f"Unknown boundary_contract key '{key}'",
            f"{path}.{key}",
        )
        for key in sorted(set(raw) - allowed)
    ]
    entry = raw.get("entry")
    if entry is not None and (
        not isinstance(entry, list) or not all(isinstance(row, Mapping) for row in entry)
    ):
        issues.append(
            _issue(
                "workflow.boundary.entry_invalid",
                "boundary_contract.entry is a list of expected inputs",
                f"{path}.entry",
            )
        )
    exit_outputs = raw.get("exit")
    if exit_outputs is not None and (
        not isinstance(exit_outputs, list) or not all(_text(item) for item in exit_outputs)
    ):
        issues.append(
            _issue(
                "workflow.boundary.exit_invalid",
                "boundary_contract.exit is a list of required output keys",
                f"{path}.exit",
            )
        )
    if "exit_on" in raw and not group:
        if exit_outputs is None:
            issues.append(
                _issue(
                    "workflow.boundary.exit_on_without_exit",
                    "exit_on needs exit outputs to check",
                    f"{path}.exit_on",
                )
            )
        if not isinstance(raw.get("exit_on"), list) or not raw.get("exit_on"):
            issues.append(
                _issue(
                    "workflow.boundary.exit_on_invalid",
                    "exit_on is a non-empty list of outcomes",
                    f"{path}.exit_on",
                )
            )
    return issues


def _graph_owned_config_issues(
    kind: str, override: Mapping[str, Any], path: str
) -> list[dict[str, str]]:
    if kind not in GRAPH_OWNED_CONFIG_KINDS:
        return []
    allowed = STEP_CONFIG_ALLOWED_KEYS[kind]
    raw = override.get(kind)
    config_path = f"{path}.override.{kind}"
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return [
            _issue("workflow.step.config_invalid", f"{kind} config must be an object", config_path)
        ]
    issues = [
        _issue(
            "workflow.step_config_key_unknown",
            f"Unknown {kind} config key '{key}'",
            f"{config_path}.{key}",
        )
        for key in sorted(set(raw) - allowed)
    ]
    if kind == STEP_KIND_SWITCH:
        issues.extend(_switch_config_issues(raw, config_path))
    elif kind == STEP_KIND_APPROVAL:
        issues.extend(_approval_config_issues(raw, config_path))
    elif kind == STEP_KIND_WAIT:
        issues.extend(_wait_config_issues(raw, config_path))
    return issues


def switch_case_ids(config: Mapping[str, Any]) -> list[str]:
    cases = config.get("cases")
    if not isinstance(cases, list):
        return []
    return [
        case_id
        for case in cases
        if isinstance(case, Mapping) and (case_id := _text(case.get("case_id")))
    ]


def _switch_config_issues(config: Mapping[str, Any], path: str) -> list[dict[str, str]]:
    cases = config.get("cases")
    if not isinstance(cases, list) or not cases:
        return [
            _issue(
                "workflow.switch.cases_missing", "Switch needs at least one case", f"{path}.cases"
            )
        ]
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        case_path = f"{path}.cases[{index}]"
        if not isinstance(case, Mapping):
            issues.append(
                _issue("workflow.switch.case_invalid", "Switch case must be an object", case_path)
            )
            continue
        issues.extend(
            _issue(
                "workflow.switch.case_key_unknown",
                f"Unknown switch case key '{key}'; the case target is a graph edge",
                f"{case_path}.{key}",
            )
            for key in sorted(set(case) - _SWITCH_CASE_KEYS)
        )
        case_id = _text(case.get("case_id"))
        if case_id is None:
            issues.append(
                _issue(
                    "workflow.switch.case_id_missing",
                    "Switch case needs a stable case_id",
                    f"{case_path}.case_id",
                )
            )
        elif case_id in seen:
            issues.append(
                _issue(
                    "workflow.switch.case_id_duplicate",
                    f"Switch case id '{case_id}' is used twice",
                    f"{case_path}.case_id",
                )
            )
        else:
            seen.add(case_id)
        if "when" not in case:
            issues.append(
                _issue(
                    "workflow.switch.case_when_missing",
                    "Switch case needs a when value",
                    f"{case_path}.when",
                )
            )
    return issues


def _approval_config_issues(config: Mapping[str, Any], path: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    message = config.get("message")
    if message is not None and not isinstance(message, str):
        issues.append(
            _issue(
                "workflow.approval.message_invalid",
                "Approval message must be a string",
                f"{path}.message",
            )
        )
    show_outputs = config.get("show_outputs")
    if show_outputs is not None and (
        not isinstance(show_outputs, list) or not all(_text(item) for item in show_outputs)
    ):
        issues.append(
            _issue(
                "workflow.approval.show_outputs_invalid",
                "show_outputs is a list of output selectors",
                f"{path}.show_outputs",
            )
        )
    for key in ("output_mapping", "response_mapping"):
        if config.get(key) is not None and not isinstance(config.get(key), Mapping):
            issues.append(
                _issue(
                    "workflow.approval.mapping_invalid", f"{key} must be an object", f"{path}.{key}"
                )
            )
    return issues


def _wait_config_issues(config: Mapping[str, Any], path: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    has_literal = "duration_ms" in config
    mapping = config.get("input_mapping")
    has_mapped = False
    if mapping is not None:
        if not isinstance(mapping, Mapping):
            issues.append(
                _issue(
                    "workflow.wait.input_mapping_invalid",
                    "input_mapping must be an object",
                    f"{path}.input_mapping",
                )
            )
        else:
            issues.extend(
                _issue(
                    "workflow.wait.input_unknown",
                    f"Wait maps only duration_ms, not '{key}'",
                    f"{path}.input_mapping.{key}",
                )
                for key in sorted(set(mapping) - {"duration_ms"})
            )
            has_mapped = "duration_ms" in mapping
    if has_literal == has_mapped:
        issues.append(
            _issue(
                "workflow.wait.duration_source",
                "Wait needs exactly one duration source: duration_ms or input_mapping.duration_ms",
                path,
            )
        )
    if has_literal:
        issues.extend(wait_duration_issues(config.get("duration_ms"), f"{path}.duration_ms"))
    return issues


def wait_duration_issues(value: Any, path: str) -> list[dict[str, str]]:
    """Check a Wait duration, literal or mapped, before any timer exists."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= WAIT_DURATION_MS_MAX
    ):
        return [
            _issue(
                "workflow.wait.duration_invalid",
                f"Wait duration_ms must be an integer from 0 to {WAIT_DURATION_MS_MAX}",
                path,
            )
        ]
    return []


def step_outcomes(kind: str, config: Mapping[str, Any]) -> list[Outcome]:
    """The non-error outcomes a step of *kind* must route; terminal kinds route none."""
    if kind in TERMINAL_STEP_KINDS:
        return []
    if kind == STEP_KIND_IF:
        return [Outcome(OUTCOME_TRUE), Outcome(OUTCOME_FALSE)]
    if kind == STEP_KIND_SWITCH:
        return [Outcome(OUTCOME_CASE, case_id) for case_id in switch_case_ids(config)] + [
            Outcome(OUTCOME_DEFAULT)
        ]
    if kind == STEP_KIND_APPROVAL:
        return [Outcome(OUTCOME_APPROVED), Outcome(OUTCOME_REJECTED)]
    return [Outcome(OUTCOME_SUCCESS)]


PolicySource = tuple[str, str | None]


def effective_error_policy_source(
    *,
    step_policy: str | None,
    containers: Sequence[tuple[str, str, str | None]],
) -> tuple[str, PolicySource | None]:
    """The policy of a failed step and the node that declared it (ADR 0038).

    The first declared policy wins, taken whole, in the order step → immediate
    For each → enclosing Group. *containers* are ``(kind, id, policy)`` nearest
    first. ``None`` as source means nothing was declared and the default ``fail``
    applies, which differs from an explicitly declared ``fail``.
    """
    if step_policy:
        return step_policy, ("step", None)
    for kind, container_id, policy in containers:
        if policy:
            return policy, (kind, container_id)
    return ERROR_POLICY_FAIL, None
