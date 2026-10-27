"""A later real gate must survive the state-resume cursor normalization."""

from __future__ import annotations

import pytest

from core.application.orchestration.run_definition import clear_run_definition_cache
from tests._successive_approval_graph import ApprovalGraphRun


@pytest.fixture(autouse=True)
def isolate_event_sinks(monkeypatch):
    from core.application.orchestration.agents import common

    clear_run_definition_cache()
    monkeypatch.setattr(common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "publish_agent_message", lambda *args, **kwargs: True)
    yield
    clear_run_definition_cache()


def assert_second_gate(run: ApprovalGraphRun, *, loop: bool) -> None:
    run.start()
    first = run.approval()
    first_values = run.snapshot().values
    assert first["step_id"] == "a1"
    assert not run.snapshot().tasks
    outcome = run.resume(first["approval_id"])
    snapshot = run.snapshot()
    second = run.approval()
    assert snapshot.values["status"] == "awaiting_approval"
    assert snapshot.values["needs_user_approval"] is True
    assert not snapshot.tasks
    assert second["step_id"] == ("a1" if loop else "a2")
    assert second["step_execution_id"] != first["step_execution_id"]
    assert second["approval_id"] != first["approval_id"]
    assert second["allowed_actions"] == ["approve", "reject"]
    assert snapshot.values["pending_wait"]["step_execution_id"] == second["step_execution_id"]
    assert snapshot.values["pending_wait"]["approval_id"] == second["approval_id"]
    decisions = [
        m for m in snapshot.values["agent_messages"] if m["from_agent"] == "human_approval_gate"
    ]
    assert len(decisions) == 1
    assert decisions[0]["step_execution_id"] == first["step_execution_id"]
    if loop:
        assert (
            first_values["run_cursor"]["frames"][-1]["item_index"],
            snapshot.values["run_cursor"]["frames"][-1]["item_index"],
        ) == (0, 1)
        assert (
            first_values["run_cursor"]["frames"][-1]["for_each_execution_id"]
            == snapshot.values["run_cursor"]["frames"][-1]["for_each_execution_id"]
        )
    assert run.approval() == second
    # Both real checkpoints above are correct on the broken implementation.
    assert outcome.status == "awaiting_approval", outcome
    assert not outcome.terminal and not outcome.retryable
    assert outcome.result["approval"] == second
    assert outcome.result["workflow_wait"]["approval_id"] == second["approval_id"]
    recovered = run.resume(first["approval_id"])
    assert recovered.status == "awaiting_approval"
    assert recovered.result["approval"] == second
    assert run.snapshot().values["agent_messages"] == snapshot.values["agent_messages"]


def test_root_second_state_gate_survives_resume() -> None:
    assert_second_gate(ApprovalGraphRun(loop=False), loop=False)


def test_same_step_second_loop_gate_survives_resume() -> None:
    assert_second_gate(ApprovalGraphRun(loop=True), loop=True)


@pytest.mark.parametrize("loop", [False, True], ids=["root", "loop"])
def test_ordinary_interrupt_second_gate_keeps_execution_identity(loop: bool) -> None:
    run = ApprovalGraphRun(loop=loop, source="webhook")
    run.start()
    first = run.approval()
    assert any(task.interrupts for task in run.snapshot().tasks)
    outcome = run.resume(first["approval_id"])
    second = run.approval()
    assert any(task.interrupts for task in run.snapshot().tasks)
    assert second["step_id"] == ("a1" if loop else "a2")
    assert second["approval_id"] != first["approval_id"]
    assert second["step_execution_id"] != first["step_execution_id"]
    assert outcome.status == "awaiting_approval" and not outcome.retryable
    assert outcome.result["approval"] == second
    assert run.approval() == second
    terminal = run.resume(second["approval_id"], approved=False)
    assert terminal.status == "failed" and terminal.terminal
    assert run.snapshot().values["workflow_terminal"]["step_id"] == "rejected"


def alter_saved_second_gate(monkeypatch, mutate) -> None:
    """Inject a persisted corruption after real execution, before resume projection."""
    from core.application.orchestration.agents import common

    original = common.run_graph_with_cancel_check

    def run_then_alter(graph, graph_input, config, **kwargs):
        result = original(graph, graph_input, config, **kwargs)
        changes = mutate(graph.get_state(config).values)
        graph.update_state(config, changes, as_node="orchestrator_validation")
        return result

    monkeypatch.setattr(common, "run_graph_with_cancel_check", run_then_alter)


@pytest.mark.parametrize(
    "corruption",
    ["missing_step", "missing_ref", "missing_version", "bad_digest", "missing_recovery_identity"],
)
def test_advanced_gate_missing_authority_stays_retryable(monkeypatch, corruption: str) -> None:
    run = ApprovalGraphRun(loop=False)
    run.start()
    first = run.approval()

    def corrupt(values):
        if corruption == "missing_step":
            return {"run_cursor": {**values["run_cursor"], "step_id": None}}
        if corruption == "missing_ref":
            return {"workflow_definition_ref": None}
        if corruption == "missing_version":
            return {"runtime_schema_version": None}
        if corruption == "bad_digest":
            return {
                "workflow_definition_ref": {**values["workflow_definition_ref"], "digest": "0" * 64}
            }
        return {
            "recovery_pending": {"kind": "rework_budget", "step_id": "a2"},
            "pending_wait": None,
        }

    alter_saved_second_gate(monkeypatch, corrupt)
    outcome = run.resume(first["approval_id"])
    assert outcome.retryable, outcome
    assert outcome.reason == "approval_projection_unavailable"
    assert outcome.status == "awaiting_approval"
    assert not outcome.terminal
    assert "approval" not in outcome.result


def test_pinned_nonapproval_cursor_with_stale_pause_flags_normalizes(monkeypatch) -> None:
    run = ApprovalGraphRun(loop=False)
    run.start()
    first = run.approval()
    alter_saved_second_gate(
        monkeypatch,
        lambda values: {
            "run_cursor": {**values["run_cursor"], "step_id": "done"},
            "pending_wait": None,
        },
    )
    outcome = run.resume(first["approval_id"])
    assert run.snapshot().values["needs_user_approval"] is True
    assert run.snapshot().values["status"] == "awaiting_approval"
    assert outcome.status == "in_progress" and not outcome.retryable
    assert "approval" not in outcome.result and "workflow_wait" not in outcome.result


def test_advanced_recovery_gate_has_stable_identity_without_authored_wait(monkeypatch) -> None:
    run = ApprovalGraphRun(loop=False)
    run.start()
    first = run.approval()
    alter_saved_second_gate(
        monkeypatch,
        lambda values: {
            "recovery_pending": {
                "kind": "rework_budget",
                "request_id": "next-recovery",
                "step_id": "a2",
                "step_execution_id": values["run_cursor"]["step_execution_id"],
                "allowed_actions": ["approve", "reject"],
            },
            "pending_wait": None,
        },
    )
    outcome = run.resume(first["approval_id"])
    assert outcome.status == "awaiting_approval" and not outcome.retryable
    assert outcome.result["approval"]["approval_id"] == "recovery:next-recovery"
    assert "workflow_wait" not in outcome.result
    recovered = run.resume(first["approval_id"])
    assert recovered.result["approval"] == outcome.result["approval"]


@pytest.mark.parametrize("status", ["completed", "failed", "stuck", "cancelled", "max_iterations"])
def test_terminal_checkpoint_precedes_new_gate_classification(monkeypatch, status: str) -> None:
    run = ApprovalGraphRun(loop=False)
    run.start()
    first = run.approval()
    # The stream still yields awaiting, while the committed checkpoint is terminal.
    alter_saved_second_gate(monkeypatch, lambda _: {"status": status})
    outcome = run.resume(first["approval_id"])
    assert outcome.status == status and outcome.terminal
    assert not outcome.retryable
    assert "approval" not in outcome.result
