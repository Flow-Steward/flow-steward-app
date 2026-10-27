from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.registry.builder_metadata import builder_metadata_summary
from core.application.workflows.knowledge_projection_common import (
    _GRAPH_EDGE_KIND_MAP,
    _as_list,
    _mapping,
    _ProjectionAccumulator,
    _summary_rows,
    _text,
    _visible_catalog_row,
    agent_node_id,
    agent_type_node_id,
    group_node_id,
    io_field_node_id,
    reaction_node_id,
    skill_category_node_id,
    skill_node_id,
    step_kind_node_id,
    step_node_id,
    tool_action_node_id,
    tool_category_node_id,
    tool_node_id,
    workflow_node_id,
)
from core.application.workflows.knowledge_projection_contracts import (
    _child_workflow_id,
    _connector_id,
    _connector_operation_id,
    _field_names,
    _find_summary_row,
    _normalize_contract_rows,
    _raw_groups_and_steps,
    _schema_contract_rows,
    _tool_interface_operations,
)
from core.application.workflows.knowledge_projection_models import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)
from core.application.workflows.workflow_step_kinds import normalize_step_kind


class _WorkflowKnowledgeProjectionBuilder:
    def __init__(
        self,
        db: Any,
        *,
        account_id: str = "",
        tenant_id: str = "",
        project_id: str = "",
    ) -> None:
        self._db = db
        self._account_id = _text(account_id)
        self._tenant_id = _text(tenant_id)
        self._project_id = _text(project_id)
        self._projection = _ProjectionAccumulator()
        self._step_kind_profiles: dict[str, dict[str, Any]] = {}
        rows = _as_list(
            getattr(self._db, "list_step_kind_profiles", lambda **_: [])(enabled_only=False) or []
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            step_kind_key = _text(row.get("step_kind_key"))
            if step_kind_key:
                self._step_kind_profiles[step_kind_key] = dict(row)

    def build(self) -> WorkflowKnowledgeProjection:
        return self._projection.build()

    def _summary_rows(self, method_name: str, *, enabled_only: bool = False) -> list[Any]:
        return _summary_rows(
            self._db,
            method_name,
            enabled_only=enabled_only,
            account_id=self._account_id,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
        )

    def _link_contract_fields(
        self,
        *,
        source_node: WorkflowKnowledgeNode,
        fields: Sequence[Mapping[str, Any]],
        direction: str,
    ) -> None:
        edge_kind = "consumes_input" if direction == "input" else "produces_output"
        for row in fields:
            field_name = _text(row.get("name"))
            if not field_name:
                continue
            field_type = _text(row.get("type") or "text") or "text"
            description = _text(row.get("description"))
            field_node = WorkflowKnowledgeNode(
                id=io_field_node_id(field_name),
                kind="io_field",
                label=field_name,
                summary=description,
                text="\n".join(
                    part
                    for part in [
                        f"Field: {field_name}",
                        f"Type: {field_type}" if field_type else "",
                        f"Description: {description}" if description else "",
                    ]
                    if part
                ),
                metadata={
                    "field_name": field_name,
                    "field_type": field_type,
                },
            )
            self._add_node_with_document(field_node)
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"{edge_kind}:{source_node.id}:{field_node.id}",
                    kind=edge_kind,
                    source_id=source_node.id,
                    target_id=field_node.id,
                    metadata={
                        "required": bool(row.get("required", False)),
                        "type": field_type,
                        "description": description,
                        "contract_id": _text(row.get("contract_id")),
                        "contract_kind": _text(row.get("contract_kind")),
                        "contract_display_name": _text(row.get("contract_display_name")),
                    },
                )
            )

    def _add_tool_action(
        self,
        *,
        tool_node: WorkflowKnowledgeNode,
        operation: Mapping[str, Any],
    ) -> WorkflowKnowledgeNode:
        tool_id = _text(tool_node.metadata.get("tool_id"))
        operation_id = _text(operation.get("name") or operation.get("operation_id"))
        if not tool_id or not operation_id:
            return WorkflowKnowledgeNode(id="", kind="tool_action", label="")
        display_name = (
            _text(operation.get("display_name") or operation.get("label") or operation_id)
            or operation_id
        )
        description = _text(operation.get("description"))
        parameter_rows = _normalize_contract_rows(operation.get("parameters"))
        output_rows = _schema_contract_rows(
            operation.get("returns") or operation.get("output_schema")
        )
        action_node = WorkflowKnowledgeNode(
            id=tool_action_node_id(tool_id, operation_id),
            kind="tool_action",
            label=display_name,
            summary=description,
            text="\n".join(
                part
                for part in [
                    f"Tool action: {display_name}",
                    f"Tool ID: {tool_id}",
                    f"Operation ID: {operation_id}",
                    f"Description: {description}" if description else "",
                    f"Consumes: {', '.join(_field_names(parameter_rows))}"
                    if parameter_rows
                    else "",
                    f"Produces: {', '.join(_field_names(output_rows))}" if output_rows else "",
                ]
                if part
            ),
            metadata={
                "tool_id": tool_id,
                "operation_id": operation_id,
                "parameters": [dict(row) for row in parameter_rows],
                "returns": [dict(row) for row in output_rows],
                "input_schema": _mapping(operation.get("input_schema")),
                "output_schema": _mapping(operation.get("output_schema")),
            },
        )
        self._add_node_with_document(action_node)
        self._projection.add_edge(
            WorkflowKnowledgeEdge(
                id=f"tool_has_action:{tool_id}:{operation_id}",
                kind="tool_has_action",
                source_id=tool_node.id,
                target_id=action_node.id,
            )
        )
        self._link_contract_fields(
            source_node=action_node, fields=parameter_rows, direction="input"
        )
        self._link_contract_fields(source_node=action_node, fields=output_rows, direction="output")
        return action_node

    def add_workflow_projection(
        self,
        *,
        workflow: dict[str, Any],
        canvas_graph: dict[str, Any],
        include_related_capabilities: bool,
    ) -> None:
        workflow_id = _text(
            workflow.get("workflow_id") or canvas_graph.get("workflow", {}).get("id")
        )
        if not workflow_id:
            return
        raw_groups, raw_steps = _raw_groups_and_steps(workflow)
        canvas_step_rows = {
            _text(row.get("id")): row
            for row in _as_list(canvas_graph.get("steps"))
            if isinstance(row, Mapping) and _text(row.get("id"))
        }
        step_kinds = {
            _step_graph_id(raw_step): _text(
                canvas_step_rows.get(_step_graph_id(raw_step), {}).get("kind")
                or normalize_step_kind(raw_step.get("step_kind"), preserve_unknown=True)
            )
            or "unknown"
            for raw_step in raw_steps
        }
        workflow_payload = _mapping(canvas_graph.get("workflow"))
        interface_contract = _mapping(canvas_graph.get("interface_contract"))
        output_contract = _mapping(canvas_graph.get("output_contract"))
        workflow_display_name = (
            _text(
                workflow_payload.get("display_name") or workflow.get("display_name") or workflow_id
            )
            or workflow_id
        )
        workflow_summary = _text(workflow.get("description"))
        workflow_builder_summary = builder_metadata_summary(workflow.get("builder_metadata_jsonb"))
        workflow_inputs = _normalize_contract_rows(interface_contract.get("expected_inputs"))
        workflow_outputs = _normalize_contract_rows(output_contract.get("expected_outputs"))
        tags = [item for item in _as_list(workflow.get("tags")) if _text(item)]
        workflow_text_parts = [
            f"Workflow: {workflow_display_name}",
            f"Workflow ID: {workflow_id}",
        ]
        if workflow_summary:
            workflow_text_parts.append(f"Description: {workflow_summary}")
        if workflow_builder_summary:
            workflow_text_parts.append(f"Builder guidance: {workflow_builder_summary}")
        if tags:
            workflow_text_parts.append(f"Tags: {', '.join(_text(item) for item in tags)}")
        trigger_channels = [
            item for item in _as_list(interface_contract.get("trigger_channels")) if _text(item)
        ]
        if trigger_channels:
            workflow_text_parts.append(
                f"Trigger channels: {', '.join(_text(item) for item in trigger_channels)}"
            )
        expected_inputs = _field_names(workflow_inputs)
        if expected_inputs:
            workflow_text_parts.append(f"Expected inputs: {', '.join(expected_inputs)}")
        expected_outputs = _field_names(workflow_outputs)
        if expected_outputs:
            workflow_text_parts.append(f"Expected outputs: {', '.join(expected_outputs)}")
        group_names = [_text(row.get("display_name") or row.get("group_id")) for row in raw_groups]
        if group_names:
            workflow_text_parts.append(f"Groups: {', '.join(group_names)}")
        step_kind_names = sorted(set(step_kinds.values()))
        if step_kind_names:
            workflow_text_parts.append(f"Step kinds: {', '.join(step_kind_names)}")
        workflow_node = WorkflowKnowledgeNode(
            id=workflow_node_id(workflow_id),
            kind="workflow",
            label=workflow_display_name,
            summary=workflow_summary,
            text="\n".join(workflow_text_parts),
            metadata={
                "workflow_id": workflow_id,
                "enabled": bool(workflow.get("enabled", workflow_payload.get("enabled", False))),
                "visible_in_catalog": workflow.get("visible_in_catalog", True),
                "callable_by_parent": bool(workflow_payload.get("callable_by_parent", False)),
                "builder_summary": workflow_builder_summary,
                "builder_metadata_jsonb": dict(workflow.get("builder_metadata_jsonb") or {}),
                "tags": tags,
                "trigger_channels": trigger_channels,
                "expected_inputs": expected_inputs,
                "expected_outputs": expected_outputs,
            },
        )
        self._add_node_with_document(workflow_node)
        self._link_contract_fields(
            source_node=workflow_node, fields=workflow_inputs, direction="input"
        )
        self._link_contract_fields(
            source_node=workflow_node, fields=workflow_outputs, direction="output"
        )

        # Only Groups become nodes; root steps hang off the workflow.
        for raw_group in raw_groups:
            group_id = _text(raw_group.get("group_id"))
            group_display_name = _text(raw_group.get("display_name") or group_id) or group_id
            group_text_parts = [
                f"Group: {group_display_name}",
                f"Group ID: {group_id}",
                f"Workflow: {workflow_display_name}",
            ]
            body = _mapping(raw_group.get("body"))
            if body:
                group_text_parts.append(f"Body: {body}")
            error_policy = _mapping(raw_group.get("error_policy"))
            if error_policy:
                group_text_parts.append(f"Error policy: {error_policy}")
            group_node = WorkflowKnowledgeNode(
                id=group_node_id(workflow_id, group_id),
                kind="group",
                label=group_display_name,
                summary=_text(raw_group.get("description")),
                text="\n".join(group_text_parts),
                metadata={
                    "workflow_id": workflow_id,
                    "group_id": group_id,
                    "body_kind": _text(body.get("kind")),
                    "sort_order": raw_group.get("sort_order"),
                },
            )
            self._add_node_with_document(group_node)
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"has_group:{workflow_id}:{group_id}",
                    kind="has_group",
                    source_id=workflow_node.id,
                    target_id=group_node.id,
                    metadata={"sort_order": raw_group.get("sort_order")},
                )
            )

        for raw_step in raw_steps:
            step_graph_id = _step_graph_id(raw_step)
            step_row = _mapping(canvas_step_rows.get(step_graph_id))
            step_kind = step_kinds[step_graph_id]
            group_id = _text(raw_step.get("group_id")) or None
            lane_id = _text(raw_step.get("lane_id")) or None
            step_display_name = _text(
                step_row.get("display_name") or raw_step.get("display_name") or step_graph_id
            )
            config_summary = _text(step_row.get("config_summary"))
            agent_id = _text(raw_step.get("agent_id"))
            skill_id = _text(raw_step.get("skill_id"))
            connector_id = _connector_id(raw_step)
            connector_operation_id = _connector_operation_id(raw_step)
            child_workflow_id = _child_workflow_id(raw_step)
            step_text_parts = [
                f"Step: {step_display_name}",
                f"Step kind: {step_kind}",
                f"Workflow: {workflow_display_name}",
            ]
            if group_id:
                step_text_parts.append(f"Group ID: {group_id}")
            if lane_id:
                step_text_parts.append(f"Lane ID: {lane_id}")
            if config_summary:
                step_text_parts.append(f"Summary: {config_summary}")
            if agent_id:
                step_text_parts.append(f"Agent: {agent_id}")
            if skill_id:
                step_text_parts.append(f"Skill: {skill_id}")
            if connector_id:
                step_text_parts.append(f"Tool: {connector_id}")
            if connector_operation_id:
                step_text_parts.append(f"Tool action: {connector_operation_id}")
            if child_workflow_id:
                step_text_parts.append(f"Invokes workflow: {child_workflow_id}")
            step_node = WorkflowKnowledgeNode(
                id=step_node_id(workflow_id, step_graph_id),
                kind="step",
                label=step_display_name,
                summary=config_summary,
                text="\n".join(step_text_parts),
                metadata={
                    "workflow_id": workflow_id,
                    "group_id": group_id,
                    "lane_id": lane_id,
                    "step_graph_id": step_graph_id,
                    "step_id": _text(step_row.get("step_id") or raw_step.get("step_id")),
                    "step_kind": step_kind,
                    "agent_id": agent_id,
                    "skill_id": skill_id,
                    "tool_id": connector_id,
                    "tool_action_id": connector_operation_id,
                    "child_workflow_id": child_workflow_id,
                    "optional": bool(raw_step.get("optional", False)),
                    "disabled": bool(raw_step.get("disabled", False)),
                    "config_summary": config_summary,
                },
            )
            self._add_node_with_document(step_node)
            # Every step belongs to its workflow directly; a group step also hangs off its group.
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"has_step:{workflow_id}:{step_graph_id}",
                    kind="has_step",
                    source_id=workflow_node.id,
                    target_id=step_node.id,
                    metadata={
                        "sort_order": raw_step.get("sort_order"),
                        "optional": bool(raw_step.get("optional", False)),
                    },
                )
            )
            if group_id:
                self._projection.add_edge(
                    WorkflowKnowledgeEdge(
                        id=f"has_step:{group_node_id(workflow_id, group_id)}:{step_graph_id}",
                        kind="has_step",
                        source_id=group_node_id(workflow_id, group_id),
                        target_id=step_node.id,
                        metadata={
                            "lane_id": lane_id,
                            "sort_order": raw_step.get("sort_order"),
                            "optional": bool(raw_step.get("optional", False)),
                        },
                    )
                )
            step_kind_profile = self._step_kind_profiles.get(step_kind, {})
            step_kind_description = _text(step_kind_profile.get("description"))
            step_kind_builder_summary = builder_metadata_summary(
                step_kind_profile.get("builder_metadata_jsonb")
            )
            step_kind_node = WorkflowKnowledgeNode(
                id=step_kind_node_id(step_kind),
                kind="step_kind",
                label=step_kind,
                summary=step_kind_description,
                text="\n".join(
                    part
                    for part in [
                        f"Step kind: {step_kind}",
                        f"Description: {step_kind_description}" if step_kind_description else "",
                        f"Builder guidance: {step_kind_builder_summary}"
                        if step_kind_builder_summary
                        else "",
                    ]
                    if part
                ),
                metadata={
                    "step_kind": step_kind,
                    "builder_summary": step_kind_builder_summary,
                    "builder_metadata_jsonb": dict(
                        step_kind_profile.get("builder_metadata_jsonb") or {}
                    ),
                },
            )
            self._add_node_with_document(step_kind_node)
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"step_kind:{step_node.id}:{step_kind}",
                    kind="classifies_step_kind",
                    source_id=step_node.id,
                    target_id=step_kind_node.id,
                )
            )
            if not include_related_capabilities:
                continue
            if agent_id:
                agent_node = self._add_agent(agent_id)
                self._projection.add_edge(
                    WorkflowKnowledgeEdge(
                        id=f"uses_agent:{step_node.id}:{agent_id}",
                        kind="uses_agent",
                        source_id=step_node.id,
                        target_id=agent_node.id,
                    )
                )
            if skill_id:
                skill_node = self._add_skill(skill_id)
                self._projection.add_edge(
                    WorkflowKnowledgeEdge(
                        id=f"uses_skill:{step_node.id}:{skill_id}",
                        kind="uses_skill",
                        source_id=step_node.id,
                        target_id=skill_node.id,
                    )
                )
            if connector_id:
                tool_node = self._add_tool(connector_id)
                self._projection.add_edge(
                    WorkflowKnowledgeEdge(
                        id=f"uses_tool:{step_node.id}:{connector_id}",
                        kind="uses_tool",
                        source_id=step_node.id,
                        target_id=tool_node.id,
                    )
                )
                if connector_operation_id:
                    self._projection.add_edge(
                        WorkflowKnowledgeEdge(
                            id=f"uses_tool_action:{step_node.id}:{connector_id}:{connector_operation_id}",
                            kind="uses_tool_action",
                            source_id=step_node.id,
                            target_id=tool_action_node_id(connector_id, connector_operation_id),
                        )
                    )
            if child_workflow_id:
                child_node = self._add_workflow_reference(child_workflow_id)
                self._projection.add_edge(
                    WorkflowKnowledgeEdge(
                        id=f"invokes_workflow:{step_node.id}:{child_workflow_id}",
                        kind="invokes_workflow",
                        source_id=step_node.id,
                        target_id=child_node.id,
                    )
                )

        for edge_row in _as_list(canvas_graph.get("edges")):
            if not isinstance(edge_row, Mapping):
                continue
            raw_kind = _text(edge_row.get("kind"))
            source_step = _text(edge_row.get("source_step_id"))
            target_step = _text(edge_row.get("target_step_id"))
            target_group = _text(edge_row.get("target_group_id"))
            normalized_kind = _GRAPH_EDGE_KIND_MAP.get(raw_kind, raw_kind or "related_to")
            if not source_step or not (target_step or target_group):
                continue
            target_id = (
                step_node_id(workflow_id, target_step)
                if target_step
                else group_node_id(workflow_id, target_group)
            )
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"workflow_edge:{workflow_id}:{_text(edge_row.get('id') or normalized_kind)}",
                    kind=normalized_kind,
                    source_id=step_node_id(workflow_id, source_step),
                    target_id=target_id,
                    metadata=dict(edge_row.get("metadata") or {}),
                )
            )

        for reaction_row in _as_list(canvas_graph.get("automation_rules")):
            if not isinstance(reaction_row, Mapping):
                continue
            reaction_key = _text(reaction_row.get("reaction_key"))
            if not reaction_key:
                continue
            reaction_display_name = (
                _text(reaction_row.get("display_name") or reaction_key) or reaction_key
            )
            reaction_node = WorkflowKnowledgeNode(
                id=reaction_node_id(workflow_id, reaction_key),
                kind="reaction",
                label=reaction_display_name,
                summary=_text(reaction_row.get("event_type")),
                text="\n".join(
                    [
                        f"Reaction: {reaction_display_name}",
                        f"Workflow: {workflow_display_name}",
                        f"Event type: {_text(reaction_row.get('event_type'))}",
                        f"Action type: {_text(reaction_row.get('action_type'))}",
                    ]
                ),
                metadata={
                    "workflow_id": workflow_id,
                    "reaction_key": reaction_key,
                    "scope": _text(reaction_row.get("scope") or "workflow") or "workflow",
                    "scope_id": _text(reaction_row.get("scope_id") or "workflow") or "workflow",
                    "event_type": _text(reaction_row.get("event_type")),
                    "action_type": _text(reaction_row.get("action_type")),
                    "enabled": bool(reaction_row.get("enabled", True)),
                },
            )
            self._add_node_with_document(reaction_node)
            reaction_scope = _text(reaction_row.get("scope") or "workflow") or "workflow"
            reaction_scope_id = _text(reaction_row.get("scope_id") or "workflow") or "workflow"
            if reaction_scope == "group":
                source_id = group_node_id(workflow_id, reaction_scope_id)
            elif reaction_scope == "step":
                source_id = step_node_id(workflow_id, reaction_scope_id)
            else:
                source_id = workflow_node.id
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"has_reaction:{source_id}:{reaction_node.id}",
                    kind="has_reaction",
                    source_id=source_id,
                    target_id=reaction_node.id,
                )
            )

    def add_capability_catalog(self) -> None:
        for row in self._summary_rows("list_agents_summary", enabled_only=False):
            if isinstance(row, Mapping) and _visible_catalog_row(row):
                self._add_agent(_text(row.get("agent_id")))
        for row in self._summary_rows("list_skills_summary", enabled_only=False):
            if isinstance(row, Mapping) and _visible_catalog_row(row):
                self._add_skill(_text(row.get("skill_id")))
        for row in self._summary_rows("list_tools_summary", enabled_only=False):
            if isinstance(row, Mapping) and _visible_catalog_row(row):
                self._add_tool(_text(row.get("tool_id")))

    def _add_workflow_reference(self, workflow_id: str) -> WorkflowKnowledgeNode:
        workflow = getattr(self._db, "get_workflow", lambda _wf: None)(workflow_id)
        if isinstance(workflow, dict):
            display_name = _text(workflow.get("display_name") or workflow_id) or workflow_id
            builder_summary = builder_metadata_summary(workflow.get("builder_metadata_jsonb"))
            node = WorkflowKnowledgeNode(
                id=workflow_node_id(workflow_id),
                kind="workflow",
                label=display_name,
                summary=_text(workflow.get("description")),
                text="\n".join(
                    part
                    for part in [
                        f"Workflow: {display_name}",
                        f"Workflow ID: {workflow_id}",
                        f"Description: {_text(workflow.get('description'))}",
                        f"Builder guidance: {builder_summary}" if builder_summary else "",
                    ]
                    if part
                ),
                metadata={
                    "workflow_id": workflow_id,
                    "builder_summary": builder_summary,
                    "builder_metadata_jsonb": dict(workflow.get("builder_metadata_jsonb") or {}),
                },
            )
        else:
            node = WorkflowKnowledgeNode(
                id=workflow_node_id(workflow_id),
                kind="workflow",
                label=workflow_id,
                text=f"Workflow ID: {workflow_id}",
                metadata={"workflow_id": workflow_id},
            )
        self._add_node_with_document(node)
        return node

    def _add_agent(self, agent_id: str) -> WorkflowKnowledgeNode:
        agent_id = _text(agent_id)
        if not agent_id:
            return WorkflowKnowledgeNode(id="", kind="agent", label="")
        row = getattr(self._db, "get_agent", lambda _agent_id: None)(agent_id)
        summary_row = (
            row
            if isinstance(row, Mapping)
            else _find_summary_row(
                self._summary_rows("list_agents_summary", enabled_only=False),
                "agent_id",
                agent_id,
            )
        )
        display_name = _text((summary_row or {}).get("display_name") or agent_id) or agent_id
        description = _text(
            (summary_row or {}).get("description") or (summary_row or {}).get("description_preview")
        )
        builder_summary = builder_metadata_summary(
            (summary_row or {}).get("builder_metadata_jsonb")
        )
        agent_type = (
            _text((summary_row or {}).get("agent_type_key") or "unspecified") or "unspecified"
        )
        model = _text((summary_row or {}).get("model"))
        input_rows = _normalize_contract_rows(
            (row or {}).get("_inputs")
            or getattr(self._db, "get_agent_inputs", lambda *_: [])(agent_id)
            or []
        )
        output_rows = _normalize_contract_rows(
            (row or {}).get("_outputs")
            or getattr(self._db, "get_agent_outputs", lambda *_: [])(agent_id)
            or []
        )
        node = WorkflowKnowledgeNode(
            id=agent_node_id(agent_id),
            kind="agent",
            label=display_name,
            summary=description,
            text="\n".join(
                part
                for part in [
                    f"Agent: {display_name}",
                    f"Agent ID: {agent_id}",
                    f"Description: {description}" if description else "",
                    f"Builder guidance: {builder_summary}" if builder_summary else "",
                    f"Agent type: {agent_type}",
                    f"Model: {model}" if model else "",
                    f"Expected inputs: {', '.join(_field_names(input_rows))}" if input_rows else "",
                    f"Expected outputs: {', '.join(_field_names(output_rows))}"
                    if output_rows
                    else "",
                ]
                if part
            ),
            metadata={
                "agent_id": agent_id,
                "agent_type": agent_type,
                "model": model,
                "enabled": bool((summary_row or {}).get("enabled", True)),
                "visible_in_catalog": (summary_row or {}).get("visible_in_catalog", True)
                is not False,
                "builder_summary": builder_summary,
                "builder_metadata_jsonb": dict(
                    (summary_row or {}).get("builder_metadata_jsonb") or {}
                ),
                "expected_inputs": [dict(row) for row in input_rows],
                "expected_outputs": [dict(row) for row in output_rows],
            },
        )
        self._add_node_with_document(node)
        self._link_contract_fields(source_node=node, fields=input_rows, direction="input")
        self._link_contract_fields(source_node=node, fields=output_rows, direction="output")
        type_node = WorkflowKnowledgeNode(
            id=agent_type_node_id(agent_type),
            kind="agent_type",
            label=agent_type,
            metadata={"agent_type": agent_type},
        )
        self._projection.add_node(type_node)
        self._projection.add_edge(
            WorkflowKnowledgeEdge(
                id=f"agent_type:{agent_id}:{agent_type}",
                kind="classifies_agent_type",
                source_id=node.id,
                target_id=type_node.id,
            )
        )
        for skill_row in _as_list(
            getattr(self._db, "get_agent_skills", lambda *_: [])(agent_id) or []
        ):
            if not isinstance(skill_row, Mapping):
                continue
            skill_id = _text(skill_row.get("skill_id"))
            if not skill_id:
                continue
            skill_node = self._add_skill(skill_id)
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"agent_has_skill:{agent_id}:{skill_id}",
                    kind="agent_has_skill",
                    source_id=node.id,
                    target_id=skill_node.id,
                    metadata={"sort_order": skill_row.get("sort_order")},
                )
            )
        for tool_row in _as_list(
            getattr(self._db, "get_agent_tools", lambda *_: [])(agent_id) or []
        ):
            if not isinstance(tool_row, Mapping):
                continue
            tool_id = _text(tool_row.get("tool_id"))
            if not tool_id:
                continue
            tool_node = self._add_tool(tool_id)
            self._projection.add_edge(
                WorkflowKnowledgeEdge(
                    id=f"agent_has_tool:{agent_id}:{tool_id}",
                    kind="agent_has_tool",
                    source_id=node.id,
                    target_id=tool_node.id,
                    metadata={"sort_order": tool_row.get("sort_order")},
                )
            )
        return node

    def _add_skill(self, skill_id: str) -> WorkflowKnowledgeNode:
        skill_id = _text(skill_id)
        if not skill_id:
            return WorkflowKnowledgeNode(id="", kind="skill", label="")
        row = getattr(self._db, "get_skill", lambda _skill_id: None)(skill_id)
        summary_row = (
            row
            if isinstance(row, Mapping)
            else _find_summary_row(
                self._summary_rows("list_skills_summary", enabled_only=False),
                "skill_id",
                skill_id,
            )
        )
        display_name = _text((summary_row or {}).get("display_name") or skill_id) or skill_id
        description = _text(
            (summary_row or {}).get("description") or (summary_row or {}).get("description_preview")
        )
        builder_summary = builder_metadata_summary(
            (summary_row or {}).get("builder_metadata_jsonb")
        )
        category = _text((summary_row or {}).get("category") or "unspecified") or "unspecified"
        input_rows = _normalize_contract_rows(
            (row or {}).get("_inputs")
            or getattr(self._db, "get_skill_inputs", lambda *_: [])(skill_id)
            or []
        )
        output_rows = _normalize_contract_rows(
            (row or {}).get("_outputs")
            or getattr(self._db, "get_skill_outputs", lambda *_: [])(skill_id)
            or []
        )
        node = WorkflowKnowledgeNode(
            id=skill_node_id(skill_id),
            kind="skill",
            label=display_name,
            summary=description,
            text="\n".join(
                part
                for part in [
                    f"Skill: {display_name}",
                    f"Skill ID: {skill_id}",
                    f"Description: {description}" if description else "",
                    f"Builder guidance: {builder_summary}" if builder_summary else "",
                    f"Category: {category}",
                    f"Expected inputs: {', '.join(_field_names(input_rows))}" if input_rows else "",
                    f"Expected outputs: {', '.join(_field_names(output_rows))}"
                    if output_rows
                    else "",
                ]
                if part
            ),
            metadata={
                "skill_id": skill_id,
                "category": category,
                "enabled": bool((summary_row or {}).get("enabled", True)),
                "visible_in_catalog": (summary_row or {}).get("visible_in_catalog", True)
                is not False,
                "builder_summary": builder_summary,
                "builder_metadata_jsonb": dict(
                    (summary_row or {}).get("builder_metadata_jsonb") or {}
                ),
                "expected_inputs": [dict(row) for row in input_rows],
                "expected_outputs": [dict(row) for row in output_rows],
            },
        )
        self._add_node_with_document(node)
        self._link_contract_fields(source_node=node, fields=input_rows, direction="input")
        self._link_contract_fields(source_node=node, fields=output_rows, direction="output")
        category_node = WorkflowKnowledgeNode(
            id=skill_category_node_id(category),
            kind="skill_category",
            label=category,
            metadata={"category": category},
        )
        self._projection.add_node(category_node)
        self._projection.add_edge(
            WorkflowKnowledgeEdge(
                id=f"skill_category:{skill_id}:{category}",
                kind="classifies_skill_category",
                source_id=node.id,
                target_id=category_node.id,
            )
        )
        return node

    def _add_tool(self, tool_id: str) -> WorkflowKnowledgeNode:
        tool_id = _text(tool_id)
        if not tool_id:
            return WorkflowKnowledgeNode(id="", kind="tool", label="")
        row = getattr(self._db, "get_tool", lambda _tool_id: None)(tool_id)
        summary_row = (
            row
            if isinstance(row, Mapping)
            else _find_summary_row(
                self._summary_rows("list_tools_summary", enabled_only=False),
                "tool_id",
                tool_id,
            )
        )
        display_name = _text((summary_row or {}).get("display_name") or tool_id) or tool_id
        description = _text(
            (summary_row or {}).get("description") or (summary_row or {}).get("description_preview")
        )
        builder_summary = builder_metadata_summary(
            (summary_row or {}).get("builder_metadata_jsonb")
        )
        category = _text((summary_row or {}).get("category") or "unspecified") or "unspecified"
        impl_type = _text((summary_row or {}).get("impl_type"))
        operations = _tool_interface_operations(row or summary_row or {})
        node = WorkflowKnowledgeNode(
            id=tool_node_id(tool_id),
            kind="tool",
            label=display_name,
            summary=description,
            text="\n".join(
                part
                for part in [
                    f"Tool: {display_name}",
                    f"Tool ID: {tool_id}",
                    f"Description: {description}" if description else "",
                    f"Builder guidance: {builder_summary}" if builder_summary else "",
                    f"Category: {category}",
                    f"Implementation: {impl_type}" if impl_type else "",
                    f"Actions: {', '.join(_text(item.get('name') or item.get('operation_id')) for item in operations if _text(item.get('name') or item.get('operation_id')))}"
                    if operations
                    else "",
                ]
                if part
            ),
            metadata={
                "tool_id": tool_id,
                "category": category,
                "impl_type": impl_type,
                "enabled": bool((summary_row or {}).get("enabled", True)),
                "visible_in_catalog": (summary_row or {}).get("visible_in_catalog", True)
                is not False,
                "builder_summary": builder_summary,
                "builder_metadata_jsonb": dict(
                    (summary_row or {}).get("builder_metadata_jsonb") or {}
                ),
                "actions": [
                    _text(item.get("name") or item.get("operation_id"))
                    for item in operations
                    if _text(item.get("name") or item.get("operation_id"))
                ],
            },
        )
        self._add_node_with_document(node)
        category_node = WorkflowKnowledgeNode(
            id=tool_category_node_id(category),
            kind="tool_category",
            label=category,
            metadata={"category": category},
        )
        self._projection.add_node(category_node)
        self._projection.add_edge(
            WorkflowKnowledgeEdge(
                id=f"tool_category:{tool_id}:{category}",
                kind="classifies_tool_category",
                source_id=node.id,
                target_id=category_node.id,
            )
        )
        for operation in operations:
            self._add_tool_action(tool_node=node, operation=operation)
        return node

    def _add_node_with_document(self, node: WorkflowKnowledgeNode) -> None:
        self._projection.add_node(node)
        if node.text:
            self._projection.add_document(
                WorkflowKnowledgeDocument(
                    id=f"doc:{node.id}",
                    entity_id=node.id,
                    entity_kind=node.kind,
                    title=node.label,
                    text=node.text,
                    metadata=dict(node.metadata),
                )
            )


def _step_graph_id(raw_step: Mapping[str, Any]) -> str:
    return _text(raw_step.get("id") or raw_step.get("step_id"))
