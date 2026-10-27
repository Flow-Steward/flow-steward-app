"""
Workflow manifest — workflow-owned steps, real loop/parallel groups and the
execution graph that alone decides which node runs next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.application.registry.builder_metadata import normalize_builder_metadata
from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SETTING,
    ExecutionGraph,
    parse_execution_graph,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_INVOKE_WORKFLOW,
    is_control_flow_step_kind,
    normalize_step_kind,
)


@dataclass
class StepConfig:
    """A single atomic step owned by a workflow, at the root or in a real group.

    Canonical step kinds:
    - ``agent``: execute a bound agent (default)
    - ``connector``: execute a connector-backed step
    - ``invoke_workflow``: call a child workflow
    """

    step_id: str = ""
    display_name: str = ""
    step_kind: str = STEP_KIND_AGENT
    participant_key: str = ""
    agent: str = ""  # concrete agent_id (for step_kind=agent)
    skill_id: str = ""  # derived/runtime helper only; workflow steps do not own skill binding
    connector_id: str = ""  # for step_kind=connector
    connector_install_id: str = ""  # tracks extension install dependency for connector steps
    workflow_id: str = ""  # for step_kind=invoke_workflow
    input_mapping_jsonb: dict = field(default_factory=dict)
    output_mapping_jsonb: dict = field(default_factory=dict)
    connection_binding_jsonb: dict = field(default_factory=dict)
    group_id: str | None = None  # business id of the immediate Group, None otherwise
    for_each_id: str | None = None  # business id of the immediate For each, None otherwise
    lane_id: str | None = None  # lane of a lanes body, None at the root and in a sequence
    optional: bool = False
    disabled: bool = False  # the run skips the step and continues on its Next edge
    override: dict | None = None  # add_tools, remove_tools, config, extra_prompt
    context_inject: dict | None = None
    config_override: dict | None = None
    autonomy: dict | str | None = None
    error_policy: dict | None = None
    boundary_contract: dict | None = None

    @property
    def is_agent_step(self) -> bool:
        return str(self.step_kind or STEP_KIND_AGENT).strip().lower() == STEP_KIND_AGENT

    @property
    def is_connector_step(self) -> bool:
        return str(self.step_kind or "").strip().lower() == STEP_KIND_CONNECTOR

    @property
    def is_invoke_workflow_step(self) -> bool:
        return str(self.step_kind or "").strip().lower() == STEP_KIND_INVOKE_WORKFLOW

    @property
    def is_control_flow_step(self) -> bool:
        return is_control_flow_step_kind(self.step_kind)

    @property
    def skill(self) -> str:
        """Derived skill accessor for step projections and runtime consumers."""
        return str(self.skill_id or "").strip()

    @classmethod
    def from_dict(cls, d: dict) -> StepConfig:
        override = d.get("override")
        raw_step_kind = d.get("step_kind") or d.get("executor_kind") or d.get("kind")
        step_kind = (
            normalize_step_kind(
                raw_step_kind,
                default="",
                preserve_unknown=True,
            )
            or STEP_KIND_AGENT
        )
        workflow_id = str(d.get("workflow_id") or d.get("child_workflow_id") or "").strip()
        if (
            step_kind == STEP_KIND_INVOKE_WORKFLOW
            and not workflow_id
            and isinstance(override, dict)
        ):
            invoke = override.get("invoke_workflow")
            if isinstance(invoke, dict):
                workflow_id = str(invoke.get("workflow_id") or "").strip()

        return cls(
            step_id=str(d.get("step_id") or d.get("id") or "").strip(),
            display_name=str(d.get("display_name") or "").strip(),
            step_kind=step_kind,
            participant_key=str(d.get("participant_key") or "").strip(),
            agent=d.get("agent", d.get("agent_id", "")),
            skill_id="",
            connector_id=str(d.get("connector_id") or "").strip(),
            connector_install_id=str(d.get("connector_install_id") or "").strip(),
            workflow_id=workflow_id,
            input_mapping_jsonb=(
                dict(d.get("input_mapping_jsonb") or d.get("input_mapping") or {})
                if isinstance(d.get("input_mapping_jsonb") or d.get("input_mapping"), dict)
                else {}
            ),
            output_mapping_jsonb=(
                dict(d.get("output_mapping_jsonb") or d.get("output_mapping") or {})
                if isinstance(d.get("output_mapping_jsonb") or d.get("output_mapping"), dict)
                else {}
            ),
            connection_binding_jsonb=(
                dict(d.get("connection_binding_jsonb") or d.get("connection_binding") or {})
                if isinstance(
                    d.get("connection_binding_jsonb") or d.get("connection_binding"), dict
                )
                else {}
            ),
            group_id=str(d.get("group_id") or "").strip() or None,
            for_each_id=str(d.get("for_each_id") or "").strip() or None,
            lane_id=str(d.get("lane_id") or "").strip() or None,
            optional=d.get("optional", False),
            disabled=d.get("disabled") is True,
            override=override,
            context_inject=d.get("context_inject"),
            config_override=d.get("config_override"),
            autonomy=d.get("autonomy"),
            error_policy=d.get("error_policy"),
            boundary_contract=d.get("boundary_contract"),
        )


@dataclass
class WorkflowParticipant:
    """Workflow-level participant binding key -> concrete agent_id."""

    participant_key: str
    agent_id: str

    @classmethod
    def from_dict(cls, d: dict) -> WorkflowParticipant:
        return cls(
            participant_key=str(d.get("participant_key") or "").strip(),
            agent_id=str(d.get("agent_id") or d.get("agent") or "").strip(),
        )


def _json_field_reader(d: dict):
    import json as _json

    def read(*keys: str):
        for key in keys:
            value = d.get(key)
            if isinstance(value, str):
                value = _json.loads(value) if value.strip() else None
            if value is not None:
                return value
        return None

    return read


def _lane_ids(body: dict) -> list[str]:
    lanes = body.get("lanes") if isinstance(body, dict) else None
    if not isinstance(lanes, list):
        return []
    return [
        str(lane.get("lane_id") or "").strip()
        for lane in lanes
        if isinstance(lane, dict) and str(lane.get("lane_id") or "").strip()
    ]


@dataclass
class GroupConfig:
    """A Group runs its sequence or lanes body once (ADR 0038).

    Step membership comes from each step's ``group_id``/``lane_id``; a sequence
    Group may also hold For each containers.
    """

    group_id: str
    display_name: str = ""
    description: str = ""
    body: dict = field(default_factory=dict)
    error_policy: dict | None = None
    boundary_contract: dict | None = None

    @property
    def lane_ids(self) -> list[str]:
        return _lane_ids(self.body)

    @classmethod
    def from_dict(cls, d: dict) -> GroupConfig:
        read = _json_field_reader(d)
        body = read("body", "body_jsonb")
        return cls(
            group_id=str(d.get("group_id") or "").strip(),
            display_name=str(d.get("display_name") or d.get("group_id") or "").strip(),
            description=str(d.get("description") or ""),
            body=body if isinstance(body, dict) else {},
            error_policy=read("error_policy", "error_policy_jsonb") or None,
            boundary_contract=read("boundary_contract", "boundary_contract_jsonb"),
        )

    def dump(self) -> dict:
        return {
            "group_id": self.group_id,
            "display_name": self.display_name,
            "description": self.description,
            "body": dict(self.body),
            "error_policy": self.error_policy,
            "boundary_contract": self.boundary_contract,
        }


@dataclass
class ForEachConfig:
    """Walk a ready collection one item at a time through a sequence or lanes body."""

    for_each_id: str
    over: str
    item_name: str
    index_name: str | None = None
    group_id: str | None = None
    display_name: str = ""
    description: str = ""
    body: dict = field(default_factory=dict)
    error_policy: dict | None = None
    boundary_contract: dict | None = None

    @property
    def lane_ids(self) -> list[str]:
        return _lane_ids(self.body)

    @classmethod
    def from_dict(cls, d: dict) -> ForEachConfig:
        read = _json_field_reader(d)
        body = read("body", "body_jsonb")
        return cls(
            for_each_id=str(d.get("for_each_id") or "").strip(),
            over=str(d.get("over") or d.get("over_selector") or "").strip(),
            item_name=str(d.get("item_name") or "").strip(),
            index_name=str(d.get("index_name") or "").strip() or None,
            group_id=str(d.get("group_id") or "").strip() or None,
            display_name=str(d.get("display_name") or d.get("for_each_id") or "").strip(),
            description=str(d.get("description") or ""),
            body=body if isinstance(body, dict) else {},
            error_policy=read("error_policy", "error_policy_jsonb") or None,
            boundary_contract=read("boundary_contract", "boundary_contract_jsonb"),
        )

    def dump(self) -> dict:
        return {
            "for_each_id": self.for_each_id,
            "group_id": self.group_id,
            "display_name": self.display_name,
            "description": self.description,
            "over": self.over,
            "item_name": self.item_name,
            "index_name": self.index_name,
            "body": dict(self.body),
            "error_policy": self.error_policy,
            "boundary_contract": self.boundary_contract,
        }


@dataclass
class WorkflowPublicContract:
    """Declares what a workflow accepts and produces as a black-box callable unit.

    When ``callable`` is True, the workflow can be invoked as a child workflow
    or through an external channel using only this contract — internal step
    structure stays private.
    """

    callable: bool = False
    expected_inputs: list[dict] = field(
        default_factory=list
    )  # [{name, type, description, required}]
    expected_outputs: list[dict] = field(default_factory=list)  # [{name, type, description}]
    output_mode: str = "none"
    #: File outputs derived from the steps' "Return from workflow" settings.
    returned_files: list[dict] = field(default_factory=list)
    trigger_channels: list[str] = field(default_factory=list)
    input_mode: str = "fields"
    strict_inputs: bool = True

    @classmethod
    def from_settings(
        cls, settings: dict | None, *, steps: list[dict] | None = None
    ) -> WorkflowPublicContract:
        """Extract typed settings.callable plus the files the *steps* return."""
        from core.application.registry.workflow_contract import WorkflowInterface

        interface = WorkflowInterface.from_settings(settings or {}, steps=steps or [])
        return cls(
            callable=bool(interface.enabled),
            expected_inputs=[row.dump() for row in interface.expected_inputs],
            expected_outputs=[row.dump() for row in interface.expected_outputs],
            output_mode=interface.output_mode,
            returned_files=[row.dump() for row in interface.returned_files],
            trigger_channels=list(interface.trigger_channels),
            input_mode=interface.input_mode,
            strict_inputs=bool(interface.strict_inputs),
        )


@dataclass
class WorkflowManifest:
    workflow_id: str
    version: str
    display_name: str
    description: str
    enabled: bool
    trigger: dict  # generic authored trigger metadata
    steps: list[StepConfig]
    groups: list[GroupConfig] = field(default_factory=list)
    for_each: list[ForEachConfig] = field(default_factory=list)
    participants: list[WorkflowParticipant] = field(default_factory=list)
    settings: dict = field(default_factory=dict)  # max_total_cost, notifications, etc.
    tags: list[str] = field(default_factory=list)
    public_contract: WorkflowPublicContract = field(default_factory=WorkflowPublicContract)
    builder_metadata: dict = field(default_factory=dict)

    @property
    def variant_of_workflow_id(self) -> str:
        return str((self.settings or {}).get("variant_of_workflow_id") or "").strip()

    @property
    def traits(self) -> dict:
        traits = (self.settings or {}).get("traits")
        return traits if isinstance(traits, dict) else {}

    @property
    def policies(self) -> dict:
        policies = (self.settings or {}).get("policies")
        return policies if isinstance(policies, dict) else {}

    @classmethod
    def from_db_row(cls, row: dict) -> WorkflowManifest:
        """Construct from a RegistryDBStore.get_workflow() result."""
        import json as _json

        trigger = row.get("trigger") or {}
        if isinstance(trigger, str):
            trigger = _json.loads(trigger)
        settings = row.get("settings") or {}
        if isinstance(settings, str):
            settings = _json.loads(settings)

        participants_raw = row.get("participants")
        if isinstance(participants_raw, str):
            participants_raw = _json.loads(participants_raw)
        if not isinstance(participants_raw, list):
            participants_raw = settings.get("participants", [])
        if not isinstance(participants_raw, list):
            participants_raw = []
        participants = [
            WorkflowParticipant.from_dict(item)
            for item in participants_raw
            if isinstance(item, dict)
            and str(item.get("participant_key") or "").strip()
            and str(item.get("agent_id") or item.get("agent") or "").strip()
        ]

        steps = [_step_config_from_row(row_step) for row_step in row.get("steps") or []]
        groups = [
            GroupConfig.from_dict(group)
            for group in row.get("groups") or []
            if isinstance(group, dict)
        ]
        for_each = [
            ForEachConfig.from_dict(item)
            for item in row.get("for_each") or []
            if isinstance(item, dict)
        ]

        return cls(
            workflow_id=row["workflow_id"],
            version=row.get("version", "1.0.0"),
            display_name=row.get("display_name", row["workflow_id"]),
            description=row.get("description", ""),
            enabled=row.get("enabled", True),
            trigger=trigger,
            steps=steps,
            groups=groups,
            for_each=for_each,
            participants=participants,
            settings=settings,
            tags=list(row.get("tags") or []),
            public_contract=WorkflowPublicContract.from_settings(
                settings, steps=[step for step in row.get("steps") or [] if isinstance(step, dict)]
            ),
            builder_metadata=normalize_builder_metadata(row.get("builder_metadata_jsonb")),
        )

    def to_dict(self) -> dict:
        participants = [
            {"participant_key": p.participant_key, "agent_id": p.agent_id}
            for p in self.participants
            if str(p.participant_key or "").strip() and str(p.agent_id or "").strip()
        ]
        settings = dict(self.settings or {})
        if participants:
            settings["participants"] = participants
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "trigger": self.trigger,
            "tags": self.tags,
            "settings": settings,
            "participants": participants,
            "step_count": len(self.steps),
            "group_count": len(self.groups),
            "for_each_count": len(self.for_each),
            "builder_metadata_jsonb": normalize_builder_metadata(self.builder_metadata),
        }

    @property
    def execution_graph(self) -> ExecutionGraph | None:
        """The parsed routing graph, or ``None`` while the definition has none."""
        graph, _issues = parse_execution_graph((self.settings or {}).get(EXECUTION_GRAPH_SETTING))
        return graph

    def get_step(self, step_id: str) -> StepConfig | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def get_group(self, group_id: str) -> GroupConfig | None:
        for group in self.groups:
            if group.group_id == group_id:
                return group
        return None

    def get_for_each(self, for_each_id: str) -> ForEachConfig | None:
        for item in self.for_each:
            if item.for_each_id == for_each_id:
                return item
        return None

    def steps_in_scope(
        self,
        group_id: str | None,
        lane_id: str | None = None,
        *,
        for_each_id: str | None = None,
    ) -> list[StepConfig]:
        return [
            step
            for step in self.steps
            if step.group_id == group_id
            and step.for_each_id == for_each_id
            and step.lane_id == lane_id
        ]


def _step_config_from_row(row_step: dict) -> StepConfig:
    import json as _json

    step_payload = dict(row_step)
    for key in ("override", "context_inject", "boundary_contract"):
        value = step_payload.get(key)
        if isinstance(value, str):
            step_payload[key] = _json.loads(value) if value.strip() else None
    autonomy = step_payload.get("autonomy")
    if isinstance(autonomy, str):
        stripped = autonomy.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                step_payload["autonomy"] = _json.loads(stripped)
            except Exception:
                step_payload["autonomy"] = stripped
    step_payload["skill_id"] = ""
    if "error_policy" not in step_payload and "error_policy_jsonb" in step_payload:
        step_payload["error_policy"] = step_payload.get("error_policy_jsonb")
    if "boundary_contract" not in step_payload and "boundary_contract_jsonb" in step_payload:
        step_payload["boundary_contract"] = step_payload.get("boundary_contract_jsonb")
    return StepConfig.from_dict(step_payload)
