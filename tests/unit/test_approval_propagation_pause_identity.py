"""Forwarded approvals follow the actual durable pause, including recovery races."""

from types import SimpleNamespace

import pytest

from core.application.jobs.workflow_run_projection import approval_notification_payload_from_state
from core.application.workflows.approval_waiting_propagation import (
    pending_child_approval_event,
    propagate_approval_waiting,
    propagated_payload,
    trusted_forwarded_approval_job,
)
from core.infrastructure.runtime.worker.worker_execution_adapters import (
    _public_approval_gate_from_state,
)
from tests.unit.test_workflow_reaction_events import _chain_store


def _paused_store(
    *, gate_kind="recovery", approval_id="current-interrupt", request_id="request-new"
):
    store = _chain_store()
    child = store.jobs["child"]
    child["meta"].pop("workflow_wait")
    payload = {
        "gate_kind": gate_kind,
        "request_id": request_id,
        "step_id": "work",
        "step_execution_id": "child-exec",
        "allowed_actions": ["approve", "abort"],
    }
    state = SimpleNamespace(
        values={},
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(id=approval_id, value=payload)])],
    )
    gate = _public_approval_gate_from_state(job=child, state=state)
    store.get_public_progress_snapshot = lambda job_id: SimpleNamespace(
        snapshot=SimpleNamespace(
            job_id=job_id,
            project_id="p-1",
            workflow_id="wf-child",
            status="awaiting_approval",
            approval=gate,
        )
    )
    event = {
        "event_type": "job.approval_waiting",
        "event_id": "original-event",
        "job_id": "child",
        "workflow_id": "wf-child",
        "project_id": "p-1",
        "payload": approval_notification_payload_from_state(state),
    }
    return store, event


@pytest.mark.parametrize("wait_id", ["recovery:request-old", "approval:old-interrupt"])
def test_late_forwarded_recovery_cannot_target_a_new_pending_pause(wait_id):
    store, event = _paused_store()
    event["payload"].update(wait_id=wait_id, approval_id="old-interrupt")
    forwarded = {
        "job_id": "parent",
        "workflow_id": "wf-parent",
        "project_id": "p-1",
        "payload": propagated_payload(
            event,
            child_job_id="child",
            ancestor_wait=store.jobs["parent"]["meta"]["workflow_wait"],
        ),
    }
    assert trusted_forwarded_approval_job(store, forwarded) is None


def test_old_recovery_origin_does_not_propagate_to_a_new_pause():
    store, event = _paused_store()
    event["payload"].update(wait_id="recovery:request-old", approval_id="old-interrupt")
    emitted = []
    propagate_approval_waiting(store, event, evaluate=lambda row: emitted.append(row) or [])
    assert emitted == []


@pytest.mark.parametrize("gate_kind", ["recovery", "autonomy_gate"])
@pytest.mark.parametrize("request_id", ["request-new", ""])
def test_parent_parking_after_child_recovery_catches_up_exact_pause(gate_kind, request_id):
    store, original = _paused_store(gate_kind=gate_kind, request_id=request_id)
    event = pending_child_approval_event(store, "root")
    assert event is not None
    for key in ("wait_id", "approval_id", "reason", "allowed_actions"):
        assert event["payload"][key] == original["payload"][key]
    emitted = []
    propagate_approval_waiting(store, event, evaluate=lambda row: emitted.append(row) or [])
    assert [row["job_id"] for row in emitted] == ["parent", "root"]
    assert all(trusted_forwarded_approval_job(store, row) == "child" for row in emitted)


def test_recovery_without_a_durable_approval_projection_fails_closed():
    store, event = _paused_store()
    store.get_public_progress_snapshot = lambda job_id: None
    emitted = []
    propagate_approval_waiting(store, event, evaluate=lambda row: emitted.append(row) or [])
    assert emitted == []
    assert pending_child_approval_event(store, "root") is None


def test_authored_forwarding_preserves_recorded_current_approval_identity():
    store = _chain_store()
    child = store.jobs["child"]
    recorded = child["meta"]["workflow_wait"]
    state = SimpleNamespace(
        values={"pending_wait": recorded},
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="actual-interrupt-occurrence",
                        value={
                            "gate_kind": "approval",
                            "approval_id": recorded["approval_id"],
                            "allowed_actions": ["approve", "reject"],
                        },
                    )
                ]
            )
        ],
    )
    gate = _public_approval_gate_from_state(job=child, state=state)
    store.get_public_progress_snapshot = lambda job_id: SimpleNamespace(
        snapshot=SimpleNamespace(
            job_id=job_id,
            project_id="p-1",
            workflow_id="wf-child",
            status="awaiting_approval",
            approval=gate,
        )
    )
    origin = {
        "job_id": "child",
        "workflow_id": "wf-child",
        "payload": approval_notification_payload_from_state(state),
    }
    forwarded = {
        "job_id": "parent",
        "workflow_id": "wf-parent",
        "project_id": "p-1",
        "payload": propagated_payload(
            origin,
            child_job_id="child",
            ancestor_wait=store.jobs["parent"]["meta"]["workflow_wait"],
        ),
    }
    assert trusted_forwarded_approval_job(store, forwarded) == "child"
    caught = pending_child_approval_event(store, "root")
    assert caught is not None
    assert caught["payload"]["approval_id"] == recorded["approval_id"] == "exec-approval"
    assert caught["payload"]["approval_id"] != "actual-interrupt-occurrence"
    forged = {
        **forwarded,
        "payload": {**forwarded["payload"], "approval_id": "actual-interrupt-occurrence"},
    }
    assert trusted_forwarded_approval_job(store, forged) is None
    assert forwarded["payload"]["approval_id"] == "exec-approval"
    assert caught["payload"]["wait_id"] == recorded["wait_id"]


def test_snapshot_reconciliation_preserves_authoritative_checkpoint_identity():
    from core.application.jobs.public_workflow_snapshot_query import PublicWorkflowSnapshotQuery

    state = SimpleNamespace(
        values={},
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="actual-occurrence",
                        value={
                            "approval_id": "old-payload-id",
                            "request_id": "request-1",
                            "gate_kind": "recovery",
                            "allowed_actions": ["approve", "abort"],
                        },
                    )
                ]
            )
        ],
    )
    query = PublicWorkflowSnapshotQuery(
        None, get_thread_state_snapshot=lambda _: state, to_json_obj=lambda value: value
    )
    projected = query._approval_gate(state)
    expected = approval_notification_payload_from_state(state)
    assert projected is not None
    for key in ("approval_id", "wait_id", "reason", "allowed_actions"):
        assert projected.get(key) == expected[key]
