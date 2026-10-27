"""Real checkpointed approval flows shared by runtime and PostgreSQL regressions."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from core.application.jobs.workflow_run_projection import approval_notification_payload_from_state
from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.graph.builder_approval_gate import _registry_approval_gate_node
from core.application.orchestration.graph_runtime import graph_run_config
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import prime_run_definition
from core.application.orchestration.state import OrchestratorState
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.orchestration.validation_node import orchestrator_validation_node
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.infrastructure.runtime.graph_resume_runtime import run_graph_resume
from tests._workflow_definitions import edge, node


def approval_definition(*, loop: bool) -> dict[str, Any]:
    approval = {"step_id": "a1", "step_kind": "approval", "config": {}}
    steps = [
        approval,
        {"step_id": "done", "step_kind": "stop_success"},
        {"step_id": "rejected", "step_kind": "stop_fail"},
    ]
    if loop:
        approval["for_each_id"] = "items"
        steps[-1]["for_each_id"] = "items"
        edges = [
            edge(
                "a1", {"kind": "for_each_exit", "for_each_id": "items", "lane_id": None}, "approved"
            ),
            edge("a1", node("rejected"), "rejected"),
            edge("items", node("done"), source_kind="for_each"),
        ]
        containers = [
            {
                "for_each_id": "items",
                "over": "state.workflow_data.items",
                "item_name": "item",
                "index_name": "item_index",
                "body": {"kind": "sequence", "entry": node("a1")},
            }
        ]
    else:
        steps.append({"step_id": "a2", "step_kind": "approval"})
        edges = [
            edge("a1", node("a2"), "approved"),
            edge("a1", node("rejected"), "rejected"),
            edge("a2", node("done"), "approved"),
            edge("a2", node("rejected"), "rejected"),
        ]
        containers = []
    return {
        "steps": steps,
        "groups": [],
        "for_each": containers,
        "settings": {},
        "execution_graph": {
            "schema_version": 1,
            "entry": node("items", "for_each") if loop else node("a1"),
            "edges": edges,
        },
    }


class ApprovalGraphRun:
    """Use production hub, gate, validation and Stop nodes with production wiring."""

    def __init__(
        self, *, loop: bool, source: str = "workflow_test_run", thread_id: str | None = None
    ) -> None:
        self.store = None
        self.job_thread_store = None
        self.thread_id = thread_id or f"approval-regression-{uuid.uuid4()}"
        self.config = graph_run_config(self.thread_id)
        self.checkpointer = MemorySaver()
        builder = StateGraph(OrchestratorState)
        builder.add_node("orchestrator", orchestrator_hub_node)
        builder.add_node("human_approval_gate", _registry_approval_gate_node)
        builder.add_node("orchestrator_validation", orchestrator_validation_node)
        builder.add_node("control_flow_step", control_flow_step_node)
        builder.add_edge("control_flow_step", "orchestrator")
        builder.add_edge("orchestrator_validation", END)
        builder.set_entry_point("orchestrator")
        self.graph = builder.compile(checkpointer=self.checkpointer)
        definition = approval_definition(loop=loop)
        self.definition = definition
        ref = {
            "snapshot_id": str(uuid.uuid4()),
            "revision": 1,
            "digest": definition_digest(definition),
        }
        prime_run_definition(ref, definition)
        self.initial = {
            "workflow_id": "approval_regression",
            "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
            "workflow_definition_ref": ref,
            "task_source": source,
            "task_id": "approval-regression",
            "task_summary": "Review synthetic items",
            "agent_messages": [],
            "status": "in_progress",
            "workflow_data": {"items": ["first", "second"]},
        }

    def start(self) -> None:
        list(self.graph.stream(self.initial, self.config, stream_mode="values"))

    def snapshot(self) -> Any:
        return self.graph.get_state(self.config)

    def approval(self) -> dict[str, Any]:
        return approval_notification_payload_from_state(self.snapshot())

    def runner(self, **kwargs: Any) -> Any:
        return run_graph_resume(
            **kwargs,
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: self.checkpointer,
            build_graph=lambda **_kwargs: self.graph,
            get_logger_only=lambda: self.store,
            job_thread_store=self.job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=None,
            build_approval_entry=None,
            logger=logging.getLogger(__name__),
        )

    def resume(self, approval_id: str, *, approved: bool = True) -> Any:
        return self.runner(
            thread_id=self.thread_id,
            resume_value={"approved": approved},
            approved=approved,
            action="approve" if approved else "reject",
            comment="reviewed",
            interrupt_payload={},
            approval_id=approval_id,
            defer_terminal_persistence=True,
        )
