"""Validation of a whole workflow definition against the execution_graph contract.

One pure function judges the *resulting* definition — steps, Groups, For each
containers and the graph together (ADR 0038). The atomic authoring service, import and run preflight all
call it; nothing re-implements these rules in an entrypoint.

The definition shape it reads::

    {
      "steps": [{step_id, step_kind, group_id|None, for_each_id|None,
                 lane_id|None, override, error_policy, boundary_contract,
                 workflow_id, disabled}],
      "groups": [{group_id, body, error_policy, boundary_contract}],
      "for_each": [{for_each_id, group_id|None, over, item_name, index_name,
                    body, error_policy, boundary_contract}],
      "execution_graph": {schema_version: 1, entry, edges},
    }

Every issue is an error. A disabled workflow may keep structural diagnostics,
but unsupported hook placement is refused even for a draft. An enabled workflow,
and every run, needs no issues.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.workflows.execution_graph import (
    NODE_KIND_FOR_EACH,
    NODE_KIND_GROUP,
    NODE_KIND_STEP,
    OUTCOME_CASE,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    REWORK_SOURCE_OUTCOMES,
    ROOT_SCOPE,
    SCOPE_KIND_FOR_EACH,
    SCOPE_KIND_GROUP,
    Edge,
    ExecutionGraph,
    ForEachExitRef,
    GroupExitRef,
    NodeRef,
    Outcome,
    Scope,
    exit_scope,
    parse_execution_graph,
)
from core.application.workflows.workflow_definition_rules import (
    BODY_KIND_LANES as BODY_KIND_LANES,
)
from core.application.workflows.workflow_definition_rules import (
    BODY_KIND_SEQUENCE as BODY_KIND_SEQUENCE,
)
from core.application.workflows.workflow_definition_rules import (
    DEFINITION_STEP_KINDS as DEFINITION_STEP_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    DISABLED_STEP_NEEDS_NEXT_CODE as DISABLED_STEP_NEEDS_NEXT_CODE,
)
from core.application.workflows.workflow_definition_rules import (
    DISABLED_STEP_NEEDS_NEXT_MESSAGE as DISABLED_STEP_NEEDS_NEXT_MESSAGE,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_AWAIT_APPROVAL as ERROR_POLICY_AWAIT_APPROVAL,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_CONTINUE as ERROR_POLICY_CONTINUE,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_FAIL as ERROR_POLICY_FAIL,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_RETRY as ERROR_POLICY_RETRY,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_ROUTE_TO_STEP as ERROR_POLICY_ROUTE_TO_STEP,
)
from core.application.workflows.workflow_definition_rules import (
    ERROR_POLICY_TOKENS as ERROR_POLICY_TOKENS,
)
from core.application.workflows.workflow_definition_rules import (
    GRAPH_OWNED_CONFIG_KINDS as GRAPH_OWNED_CONFIG_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    LANE_STEP_KINDS as LANE_STEP_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    STOP_STEP_KINDS as STOP_STEP_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    TERMINAL_STEP_KINDS as TERMINAL_STEP_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    WAIT_DURATION_MS_MAX as WAIT_DURATION_MS_MAX,
)
from core.application.workflows.workflow_definition_rules import (
    WAITING_STEP_KINDS as WAITING_STEP_KINDS,
)
from core.application.workflows.workflow_definition_rules import (
    ChildChainScan as ChildChainScan,
)
from core.application.workflows.workflow_definition_rules import (
    ChildDefinition as ChildDefinition,
)
from core.application.workflows.workflow_definition_rules import (
    ChildLoader as ChildLoader,
)
from core.application.workflows.workflow_definition_rules import (
    _collect_groups,
    _collect_steps,
    _Group,
    _issue,
    _mapping,
    _Step,
    _text,
)
from core.application.workflows.workflow_definition_rules import (
    effective_error_policy_source as effective_error_policy_source,
)
from core.application.workflows.workflow_definition_rules import (
    step_can_be_disabled as step_can_be_disabled,
)
from core.application.workflows.workflow_definition_rules import (
    step_outcomes as step_outcomes,
)
from core.application.workflows.workflow_definition_rules import (
    switch_case_ids as switch_case_ids,
)
from core.application.workflows.workflow_definition_rules import (
    wait_duration_issues as wait_duration_issues,
)
from core.application.workflows.workflow_for_each_rules import _collect_for_each, _ForEach
from core.application.workflows.workflow_hook_placement import hook_placement_issues
from core.application.workflows.workflow_step_kinds import (
    FOR_EACH_ONLY_STEP_KINDS,
    STEP_KIND_INVOKE_WORKFLOW,
)


class _Validator:
    def __init__(self, definition: Mapping[str, Any]) -> None:
        self.issues: list[dict[str, str]] = hook_placement_issues(definition)
        self.groups = _collect_groups(definition.get("groups"), self.issues)
        self.for_each = _collect_for_each(definition.get("for_each"), self.groups, self.issues)
        self.steps = _collect_steps(
            definition.get("steps"), self.groups, self.for_each, self.issues
        )
        self.graph: ExecutionGraph | None = None
        graph, graph_issues = parse_execution_graph(definition.get("execution_graph"))
        self.issues.extend(graph_issues)
        self.graph = graph

    # --- references and scopes -------------------------------------------------

    def containers(self) -> list[_Group | _ForEach]:
        return [*self.groups.values(), *self.for_each.values()]

    def container(self, ref: NodeRef) -> _Group | _ForEach | None:
        if ref.kind == NODE_KIND_GROUP:
            return self.groups.get(ref.id)
        if ref.kind == NODE_KIND_FOR_EACH:
            return self.for_each.get(ref.id)
        return None

    def node_scope(self, ref: NodeRef) -> Scope | None:
        if ref.kind == NODE_KIND_STEP:
            step = self.steps.get(ref.id)
            return step.scope if step else None
        container = self.container(ref)
        return container.scope if container else None

    def scope_container(self, scope: Scope) -> _Group | _ForEach | None:
        if scope == ROOT_SCOPE:
            return None
        if scope[0] == SCOPE_KIND_GROUP:
            return self.groups.get(str(scope[1]))
        if scope[0] == SCOPE_KIND_FOR_EACH:
            return self.for_each.get(str(scope[1]))
        return None

    def check_unique_ids(self) -> None:
        """Business ids name one node of the workflow, whatever its kind."""
        seen: dict[str, str] = {}
        nodes = [
            *((step.step_id, step.path) for step in self.steps.values()),
            *((group.group_id, group.path) for group in self.groups.values()),
            *((item.for_each_id, item.path) for item in self.for_each.values()),
        ]
        for node_id, path in nodes:
            if node_id in seen:
                self.issues.append(
                    _issue(
                        "workflow.node.id_duplicate",
                        f"Id '{node_id}' already names another node of this workflow",
                        path,
                    )
                )
            seen.setdefault(node_id, path)

    def check_hooks(self) -> None:
        """An Invoke is direct or a hook; a hook id names one hook of the workflow."""
        from core.application.workflows.workflow_hook_contract import (
            HOOK_CONFIG_KEY,
            invoke_config_issues,
        )

        seen: set[str] = set()
        for step in self.steps.values():
            if step.kind != STEP_KIND_INVOKE_WORKFLOW or HOOK_CONFIG_KEY not in step.config:
                continue
            for issue in invoke_config_issues(step.config):
                self.issues.append(
                    _issue(issue["code"], issue["message"], f"{step.path}.{issue['path']}")
                )
            hook = step.config.get(HOOK_CONFIG_KEY)
            hook_id = hook.get("hook_id") if isinstance(hook, Mapping) else None
            if not isinstance(hook_id, str):
                continue
            if hook_id in seen:
                self.issues.append(
                    _issue(
                        "workflow.hook.id_duplicate",
                        f"Hook id '{hook_id}' already names another hook of this workflow",
                        f"{step.path}.override.invoke_workflow.hook.hook_id",
                    )
                )
            seen.add(hook_id)

    def check_container_bodies(self) -> None:
        for container in self.containers():
            entries: list[tuple[str | None, NodeRef, str]] = []
            if container.body_kind == BODY_KIND_SEQUENCE and container.sequence_entry:
                entries.append((None, container.sequence_entry, f"{container.path}.body.entry"))
            for index, (lane, lane_entry) in enumerate(container.lanes.items()):
                entries.append(
                    (
                        lane,
                        NodeRef(NODE_KIND_STEP, lane_entry),
                        f"{container.path}.body.lanes[{index}].entry",
                    )
                )
            for lane_id, entry, path in entries:
                if self.node_scope(entry) != container.body_scope(lane_id):
                    where = f"lane '{lane_id}'" if lane_id else "the body"
                    owner = _container_label(container)
                    self.issues.append(
                        _issue(
                            "workflow.group.entry_foreign",
                            f"Entry {entry.kind} '{entry.id}' of {owner} must belong to {where}",
                            path,
                        )
                    )

    def check_edges(self, graph: ExecutionGraph) -> None:
        seen: set[tuple[NodeRef, Outcome]] = set()
        for index, edge in enumerate(graph.edges):
            path = f"execution_graph.edges[{index}]"
            source_scope = self.node_scope(edge.source)
            if source_scope is None:
                self.issues.append(
                    _issue(
                        "workflow.graph.source_unknown",
                        f"Edge source {edge.source.kind} '{edge.source.id}' does not exist",
                        f"{path}.source",
                    )
                )
                continue
            key = (edge.source, edge.outcome)
            if key in seen:
                self.issues.append(
                    _issue(
                        "workflow.graph.outcome_duplicate",
                        "This outcome already has an edge; one outcome leads to one target",
                        f"{path}.outcome",
                    )
                )
            seen.add(key)
            self.check_edge_outcome(edge, path)
            self.check_edge_target(edge, source_scope, path)
            self.check_edge_rework(edge, source_scope, path)

    def check_edge_outcome(self, edge: Edge, path: str) -> None:
        container = self.container(edge.source)
        if container is not None:
            allowed = {Outcome(OUTCOME_SUCCESS)}
            if container.error_policy == ERROR_POLICY_ROUTE_TO_STEP:
                allowed.add(Outcome(OUTCOME_ERROR))
            if edge.outcome not in allowed:
                self.issues.append(
                    _issue(
                        "workflow.graph.outcome_unsupported",
                        f"{_container_label(container).capitalize()} routes only success, "
                        "and error under route_to_step",
                        f"{path}.outcome",
                    )
                )
            return
        step = self.steps[edge.source.id]
        allowed_outcomes = set(step_outcomes(step.kind, step.config))
        if step.kind not in TERMINAL_STEP_KINDS and step.error_policy == ERROR_POLICY_ROUTE_TO_STEP:
            allowed_outcomes.add(Outcome(OUTCOME_ERROR))
        if edge.outcome in allowed_outcomes:
            return
        if step.kind in STOP_STEP_KINDS:
            message = f"Stop step '{step.step_id}' ends the workflow and has no outgoing edges"
        elif step.kind in TERMINAL_STEP_KINDS:
            message = (
                f"Step '{step.step_id}' ({step.kind}) ends the current For each item or the "
                "For each and has no outgoing edges"
            )
        elif edge.outcome.name == OUTCOME_ERROR:
            message = (
                f"Step '{step.step_id}' has an error edge only with its own route_to_step policy"
            )
        else:
            message = f"Step '{step.step_id}' ({step.kind}) has no '{edge.outcome.name}' outcome"
            if edge.outcome.case_id:
                message += f" for case '{edge.outcome.case_id}'"
        self.issues.append(_issue("workflow.graph.outcome_unsupported", message, f"{path}.outcome"))

    def check_exit_target(
        self, target: GroupExitRef | ForEachExitRef, source_scope: Scope, path: str
    ) -> None:
        if isinstance(target, ForEachExitRef):
            owner, kind = self.for_each.get(target.for_each_id), "for_each_exit"
            owner_id = target.for_each_id
        else:
            owner, kind = self.groups.get(target.group_id), "group_exit"
            owner_id = target.group_id
        if owner is None:
            self.issues.append(
                _issue(
                    "workflow.graph.target_unknown",
                    f"{kind} names unknown container '{owner_id}'",
                    f"{path}.target",
                )
            )
            return
        if source_scope != exit_scope(target):
            self.issues.append(
                _issue(
                    f"workflow.graph.{kind}_foreign",
                    f"Only a step of that body or lane may end at its {kind}",
                    f"{path}.target",
                )
            )

    def check_edge_target(self, edge: Edge, source_scope: Scope, path: str) -> None:
        target = edge.target
        if isinstance(target, GroupExitRef | ForEachExitRef):
            self.check_exit_target(target, source_scope, path)
            return
        target_scope = self.node_scope(target)
        if target_scope is None:
            self.issues.append(
                _issue(
                    "workflow.graph.target_unknown",
                    f"Edge target {target.kind} '{target.id}' does not exist",
                    f"{path}.target",
                )
            )
            return
        if target.kind in {NODE_KIND_GROUP, NODE_KIND_FOR_EACH}:
            source_container = self.scope_container(source_scope)
            if isinstance(source_container, _ForEach) or (
                source_container is not None and source_container.body_kind == BODY_KIND_LANES
            ):
                self.issues.append(
                    _issue(
                        "workflow.for_each.nested_unsupported"
                        if isinstance(source_container, _ForEach)
                        else "workflow.group.nested_unsupported",
                        "A For each body or a parallel lane holds no Group or For each",
                        f"{path}.target",
                    )
                )
                return
            if target.kind == NODE_KIND_GROUP and source_scope != ROOT_SCOPE:
                self.issues.append(
                    _issue(
                        "workflow.group.nested_unsupported",
                        "A Group is entered from the root; only a For each may sit in a Group",
                        f"{path}.target",
                    )
                )
                return
        if target_scope != source_scope:
            self.issues.append(
                _issue(
                    "workflow.graph.cross_scope",
                    "An edge stays in its scope: enter a container through its node, leave it "
                    "through its exit",
                    f"{path}.target",
                )
            )

    def check_edge_rework(self, edge: Edge, source_scope: Scope, path: str) -> None:
        if not edge.rework:
            return
        if edge.outcome.name not in REWORK_SOURCE_OUTCOMES:
            self.issues.append(
                _issue(
                    "workflow.graph.rework_source_invalid",
                    "Rework leaves only from a branch, rejected or error outcome",
                    f"{path}.rework",
                )
            )
        if isinstance(edge.target, GroupExitRef | ForEachExitRef):
            self.issues.append(
                _issue(
                    "workflow.graph.rework_target_invalid",
                    "A rework edge returns to a node, not to an exit",
                    f"{path}.target",
                )
            )
        if self.is_lane_scope(source_scope):
            self.issues.append(
                _issue(
                    "workflow.graph.rework_in_lane",
                    "A parallel lane has no rework",
                    f"{path}.rework",
                )
            )

    def is_lane_scope(self, scope: Scope) -> bool:
        container = self.scope_container(scope)
        return bool(container and container.body_kind == BODY_KIND_LANES)

    # --- ports, entry, reachability, cycles -------------------------------------

    def check_required_ports(self, graph: ExecutionGraph) -> None:
        present = {(edge.source, edge.outcome) for edge in graph.edges}
        for step in self.steps.values():
            ref = NodeRef(NODE_KIND_STEP, step.step_id)
            required = list(step_outcomes(step.kind, step.config))
            if (
                step.kind not in TERMINAL_STEP_KINDS
                and step.error_policy == ERROR_POLICY_ROUTE_TO_STEP
            ):
                required.append(Outcome(OUTCOME_ERROR))
            for outcome in required:
                if (ref, outcome) not in present:
                    label = outcome.name + (f" '{outcome.case_id}'" if outcome.case_id else "")
                    issue = _issue(
                        "workflow.graph.outcome_missing",
                        f"Step '{step.step_id}' has no edge for its {label} outcome",
                        step.path,
                    )
                    if outcome.name == OUTCOME_CASE and outcome.case_id:
                        issue["outcome_name"] = outcome.name
                        issue["outcome_case_id"] = outcome.case_id
                    self.issues.append(issue)
        for container in self.containers():
            ref = _container_ref(container)
            required = [Outcome(OUTCOME_SUCCESS)]
            if container.error_policy == ERROR_POLICY_ROUTE_TO_STEP:
                required.append(Outcome(OUTCOME_ERROR))
            for outcome in required:
                if (ref, outcome) not in present:
                    self.issues.append(
                        _issue(
                            "workflow.graph.outcome_missing",
                            f"{_container_label(container).capitalize()} has no edge for its "
                            f"{outcome.name} outcome",
                            container.path,
                        )
                    )

    def check_entry(self, graph: ExecutionGraph) -> None:
        entry = graph.entry
        if not isinstance(entry, NodeRef) or self.node_scope(entry) != ROOT_SCOPE:
            self.issues.append(
                _issue(
                    "workflow.graph.entry_invalid",
                    "The workflow entry must be a root step, Group or For each",
                    "execution_graph.entry",
                )
            )

    def scope_entries(self, graph: ExecutionGraph) -> dict[Scope, NodeRef]:
        entries: dict[Scope, NodeRef] = {}
        if isinstance(graph.entry, NodeRef) and self.node_scope(graph.entry) == ROOT_SCOPE:
            entries[ROOT_SCOPE] = graph.entry
        for container in self.containers():
            if container.sequence_entry:
                entries[container.body_scope(None)] = container.sequence_entry
            for lane_id, lane_entry in container.lanes.items():
                entries[container.body_scope(lane_id)] = NodeRef(NODE_KIND_STEP, lane_entry)
        return entries

    def check_reachability(self, graph: ExecutionGraph) -> None:
        successors: dict[NodeRef, list[NodeRef]] = {}
        for edge in graph.edges:
            source_step = (
                self.steps.get(edge.source.id) if edge.source.kind == NODE_KIND_STEP else None
            )
            # Stop, Break and Continue terminate before routing any outcome. An
            # invalid authored edge must not make skipped work appear reachable.
            if source_step and source_step.kind in TERMINAL_STEP_KINDS:
                continue
            if isinstance(edge.target, NodeRef):
                successors.setdefault(edge.source, []).append(edge.target)
        reached: set[NodeRef] = set()
        for entry in self.scope_entries(graph).values():
            stack = [entry]
            while stack:
                node = stack.pop()
                if node in reached:
                    continue
                reached.add(node)
                stack.extend(successors.get(node, []))
        for step in self.steps.values():
            if NodeRef(NODE_KIND_STEP, step.step_id) not in reached:
                self.issues.append(
                    _issue(
                        "workflow.graph.unreachable",
                        f"Step '{step.step_id}' is not reachable from the entry of its scope",
                        step.path,
                    )
                )
        for container in self.containers():
            if _container_ref(container) not in reached:
                self.issues.append(
                    _issue(
                        "workflow.graph.unreachable",
                        f"{_container_label(container).capitalize()} is not reachable from the "
                        "entry of its scope",
                        container.path,
                    )
                )

    def check_acyclic(self, graph: ExecutionGraph) -> None:
        """Without rework edges every sequential area is a DAG."""
        successors: dict[NodeRef, list[NodeRef]] = {}
        for edge in graph.edges:
            if not edge.rework and isinstance(edge.target, NodeRef):
                successors.setdefault(edge.source, []).append(edge.target)
        state: dict[NodeRef, int] = {}
        reported: set[NodeRef] = set()
        for start in successors:
            if state.get(start):
                continue
            stack: list[tuple[NodeRef, int]] = [(start, 0)]
            state[start] = 1
            while stack:
                node, index = stack[-1]
                children = successors.get(node, [])
                if index >= len(children):
                    state[node] = 2
                    stack.pop()
                    continue
                stack[-1] = (node, index + 1)
                child = children[index]
                if state.get(child) == 1:
                    if child not in reported:
                        reported.add(child)
                        self.issues.append(
                            _issue(
                                "workflow.graph.cycle_without_rework",
                                f"A path returns to {child.kind} '{child.id}' without a rework edge",
                                "execution_graph.edges",
                            )
                        )
                elif not state.get(child):
                    state[child] = 1
                    stack.append((child, 0))

    # --- placement and per-step contracts -------------------------------------

    def check_placement(self) -> None:
        """One composition matrix for every surface (ADR 0038)."""
        for step in self.steps.values():
            if self.is_lane_scope(step.scope):
                if step.kind not in LANE_STEP_KINDS:
                    self.issues.append(
                        _issue(
                            "workflow.placement.lane_kind_unsupported",
                            f"A parallel lane cannot hold a {step.kind} step; a lane ends at its exit",
                            step.path,
                        )
                    )
                continue
            if step.kind in FOR_EACH_ONLY_STEP_KINDS and step.for_each_id is None:
                self.issues.append(
                    _issue(
                        "workflow.placement.for_each_only",
                        f"A {step.kind} step belongs directly to the sequence body of a For each",
                        step.path,
                    )
                )

    def step_containers(self, step: _Step) -> list[tuple[str, str, str | None]]:
        """``(kind, id, policy)`` of the step's containers, nearest first."""
        chain: list[tuple[str, str, str | None]] = []
        group_id = step.group_id
        if step.for_each_id is not None:
            item = self.for_each.get(step.for_each_id)
            if item is not None:
                chain.append((NODE_KIND_FOR_EACH, item.for_each_id, item.error_policy))
                group_id = item.group_id
        group = self.groups.get(group_id or "")
        if group is not None:
            chain.append((NODE_KIND_GROUP, group.group_id, group.error_policy))
        return chain

    def check_error_policies(self) -> None:
        for step in self.steps.values():
            policy, _source = effective_error_policy_source(
                step_policy=step.error_policy, containers=self.step_containers(step)
            )
            outcomes = step_outcomes(step.kind, step.config)
            if (
                policy in {ERROR_POLICY_CONTINUE, ERROR_POLICY_AWAIT_APPROVAL}
                and outcomes
                and Outcome(OUTCOME_SUCCESS) not in outcomes
            ):
                self.issues.append(
                    _issue(
                        "workflow.error_policy.success_outcome_required",
                        f"Step '{step.step_id}' has no success outcome; '{policy}' cannot choose "
                        "a branch. Use fail or an explicit error route.",
                        f"{step.path}.error_policy",
                    )
                )

    def check_boundaries(self) -> None:
        for step in self.steps.values():
            exit_on = (step.boundary or {}).get("exit_on")
            if not isinstance(exit_on, list):
                continue
            own = set(step_outcomes(step.kind, step.config))
            for index, raw in enumerate(exit_on):
                path = f"{step.path}.boundary_contract.exit_on[{index}]"
                outcome = _outcome_from(raw)
                if outcome is None or outcome not in own:
                    self.issues.append(
                        _issue(
                            "workflow.boundary.exit_on_unsupported",
                            f"exit_on names an outcome step '{step.step_id}' does not have, or an error outcome",
                            path,
                        )
                    )

    def check_lane_children(
        self,
        load_child: ChildLoader | None,
        max_depth: int,
        *,
        owner: Any = None,
        bindings: Any = None,
    ) -> None:
        for step in self.steps.values():
            if step.kind != STEP_KIND_INVOKE_WORKFLOW:
                continue
            if not self.is_lane_scope(step.scope) or load_child is None:
                continue
            children, resolve_issues = _definition_children(
                {
                    "steps": [
                        {
                            "step_kind": step.kind,
                            "override": {step.kind: dict(step.config)},
                            "workflow_id": step.child_workflow_id,
                        }
                    ]
                },
                owner=owner,
                bindings=bindings,
            )
            self.issues.extend({**issue, "path": step.path} for issue in resolve_issues)
            if not children:
                continue
            scan = scan_child_chain(children[0], load_child, max_depth=max_depth, bindings=bindings)
            self.issues.extend({**issue, "path": step.path} for issue in scan.issues)
            for wait in scan.waits:
                chain = " → ".join(wait["chain"])
                self.issues.append(
                    _issue(
                        "workflow.placement.lane_child_waits",
                        f"Step '{step.step_id}' runs in a parallel lane, but child chain {chain} has "
                        f"a {wait['step_kind']} step '{wait['step_id']}'",
                        step.path,
                    )
                )

    def run(
        self,
        *,
        load_child: ChildLoader | None,
        max_depth: int,
        owner: Any = None,
        bindings: Any = None,
    ) -> list[dict[str, str]]:
        self.check_unique_ids()
        self.check_hooks()
        self.check_container_bodies()
        self.check_placement()
        self.check_error_policies()
        self.check_boundaries()
        self.check_lane_children(load_child, max_depth, owner=owner, bindings=bindings)
        graph = self.graph
        if graph is not None:
            self.check_entry(graph)
            self.check_edges(graph)
            self.check_required_ports(graph)
            self.check_reachability(graph)
            self.check_acyclic(graph)
        return self.issues


def _container_ref(container: _Group | _ForEach) -> NodeRef:
    if isinstance(container, _ForEach):
        return NodeRef(NODE_KIND_FOR_EACH, container.for_each_id)
    return NodeRef(NODE_KIND_GROUP, container.group_id)


def _container_label(container: _Group | _ForEach) -> str:
    if isinstance(container, _ForEach):
        return f"For each '{container.for_each_id}'"
    return f"group '{container.group_id}'"


def _outcome_from(raw: Any) -> Outcome | None:
    if not isinstance(raw, Mapping) or set(raw) - {"name", "case_id"}:
        return None
    name = raw.get("name")
    case_id = _text(raw.get("case_id"))
    if name == OUTCOME_ERROR or not isinstance(name, str):
        return None
    if (name == OUTCOME_CASE) != (case_id is not None):
        return None
    return Outcome(name, case_id)


def definition_authored_waits(definition: Mapping[str, Any]) -> list[dict[str, str]]:
    """Authored Approval/Wait steps of one definition, wherever they are placed."""
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        {"step_id": str(step.get("step_id") or ""), "step_kind": str(step.get("step_kind") or "")}
        for step in steps
        if isinstance(step, Mapping)
        and str(step.get("step_kind") or "").strip().lower() in WAITING_STEP_KINDS
    ]


def _definition_children(
    definition: Mapping[str, Any], *, owner: Any = None, bindings: Any = None
) -> tuple[list[str], list[dict[str, str]]]:
    """The workflows *definition* calls: direct Invokes and configured hooks.

    Hooks resolve through the shared target resolver in *owner*'s project; an
    empty hook calls nothing. Without bindings only direct Invokes are known.
    """
    from core.application.workflows.workflow_hook_contract import HOOK_CONFIG_KEY
    from core.application.workflows.workflow_invocation_target import (
        InvocationTargetError,
        resolve_invocation_target,
    )

    children: list[str] = []
    issues: list[dict[str, str]] = []
    for step in definition.get("steps") or []:
        if (
            not isinstance(step, Mapping)
            or str(step.get("step_kind") or "").strip().lower() != STEP_KIND_INVOKE_WORKFLOW
        ):
            continue
        config = _mapping(_mapping(step.get("override")).get(STEP_KIND_INVOKE_WORKFLOW))
        if HOOK_CONFIG_KEY in config:
            if bindings is None or owner is None:
                continue
            try:
                child = resolve_invocation_target(
                    config, owner=owner, bindings=bindings
                ).workflow_id
            except InvocationTargetError as exc:
                issues.append(_issue(exc.code, str(exc), ""))
                continue
        else:
            child = _text(step.get("workflow_id")) or _text(config.get("workflow_id"))
        if child and child not in children:
            children.append(child)
    return children, issues


def scan_child_chain(
    workflow_id: str,
    load_child: ChildLoader,
    *,
    max_depth: int,
    ancestry: Sequence[str] = (),
    bindings: Any = None,
) -> ChildChainScan:
    """Find authored waits anywhere in the invocation chain below a lane step.

    *load_child* returns the child definition at the revision a run would
    launch (the pinned one at child launch). The walk keeps the invocation
    guards: an id already in the chain is recursion, and a chain deeper than
    *max_depth* is refused, so a hidden wait can never slip past unchecked.
    """
    scan = ChildChainScan()
    _scan_child(
        workflow_id,
        load_child,
        max_depth=max_depth,
        chain=list(ancestry),
        scan=scan,
        bindings=bindings,
    )
    return scan


def _scan_child(
    workflow_id: str,
    load_child: ChildLoader,
    *,
    max_depth: int,
    chain: list[str],
    scan: ChildChainScan,
    bindings: Any = None,
) -> None:
    if workflow_id in chain:
        scan.issues.append(
            _issue(
                "workflow.child.recursion",
                "Workflow invocation chain repeats " + " → ".join([*chain, workflow_id]),
                "",
            )
        )
        return
    chain = [*chain, workflow_id]
    if len(chain) > max_depth:
        scan.issues.append(
            _issue(
                "workflow.child.depth_exceeded",
                f"Workflow invocation chain is deeper than {max_depth}: " + " → ".join(chain),
                "",
            )
        )
        return
    child = load_child(workflow_id)
    if child is None:
        scan.issues.append(
            _issue(
                "workflow.child.unavailable",
                f"Child workflow '{workflow_id}' cannot be loaded to check it for waits",
                "",
            )
        )
        return
    scan.revisions.setdefault(workflow_id, child.revision)
    for wait in definition_authored_waits(child.definition):
        scan.waits.append(
            {**wait, "workflow_id": workflow_id, "revision": child.revision, "chain": chain}
        )
    grandchildren, issues = _definition_children(
        child.definition, owner=child.owner, bindings=bindings
    )
    scan.issues.extend(issues)
    for grandchild in grandchildren:
        _scan_child(
            grandchild, load_child, max_depth=max_depth, chain=chain, scan=scan, bindings=bindings
        )


def validate_workflow_definition(
    definition: Mapping[str, Any],
    *,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
    owner: Any = None,
    bindings: Any = None,
) -> list[dict[str, str]]:
    """All issues of the resulting definition; an empty list means runnable.

    *load_child* lets the validator walk invocation chains of lane steps.
    Without it the chain check is skipped and the caller owns it.
    """
    if not isinstance(definition, Mapping):
        return [_issue("workflow.definition.invalid", "Workflow definition must be an object", "")]
    return _Validator(definition).run(
        load_child=load_child, max_depth=max_invoke_depth, owner=owner, bindings=bindings
    )


def definition_validation_result(issues: list[dict[str, str]]) -> dict[str, Any]:
    """The ``{valid, errors, warnings}`` shape the save guards already read."""
    return {"valid": not issues, "errors": list(issues), "warnings": []}


__all__ = [
    "BODY_KIND_LANES",
    "BODY_KIND_SEQUENCE",
    "DEFINITION_STEP_KINDS",
    "DISABLED_STEP_NEEDS_NEXT_CODE",
    "DISABLED_STEP_NEEDS_NEXT_MESSAGE",
    "ERROR_POLICY_ROUTE_TO_STEP",
    "ERROR_POLICY_TOKENS",
    "GRAPH_OWNED_CONFIG_KINDS",
    "LANE_STEP_KINDS",
    "TERMINAL_STEP_KINDS",
    "WAIT_DURATION_MS_MAX",
    "ChildChainScan",
    "ChildDefinition",
    "ChildLoader",
    "definition_authored_waits",
    "definition_validation_result",
    "effective_error_policy_source",
    "scan_child_chain",
    "step_can_be_disabled",
    "step_outcomes",
    "switch_case_ids",
    "validate_workflow_definition",
    "wait_duration_issues",
]
