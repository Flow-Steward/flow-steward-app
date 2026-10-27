"""A gate with no interrupt still needs an identity, or it cannot be decided.

Remediation epic 2026-07-29, WS-04 — `AUDIT-038` symptom (b), remaining half.

A workflow Test Run parks at a phase completion gate by **state**:
`run_workflow_test_run_ephemeral` stops streaming the graph as soon as the state
reports `awaiting_approval`, so the graph never reaches a LangGraph
`interrupt()` and the checkpoint carries no interrupt.

Everything in the approval lifecycle is keyed on an approval id — the run payload
advertises it, `POST /api/approvals/{thread_id}` requires it, and
`validate_current_gate` compares the submitted id against the current gate. With
no interrupt there was no id, so the observed behaviour on the running stack was:

* the run payload carried `approval_undecidable_reason` instead of an id;
* `GET /api/jobs/{id}/approval` answered `approval_id: null, allowed_actions: []`
  with *"Approval details are loading. If this persists, refresh the page."*;
* `POST /api/approvals/{thread_id}` rejected every decision as
  `stale_approval_gate`, because `current_approval_id({})` is `""`.

`graph_resume_runtime` already supports resuming these runs from state — it has
an explicit `thread_id.startswith("test-run-")` branch — so only the durable
identity was missing. It is minted when the run parks and stored on the anchor
job's meta.

The security property that must survive: the identity is a **server-side fact
read from the job row**, never something the caller can assert, and a gate that
is not a state-based test-run gate stays stale exactly as before.
"""

from __future__ import annotations

import json

import pytest

from core.application.management.approval_decision_service import ApprovalDecisionService
from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.errors import ServiceConflictError

STATE_ID = "state-approval-4f1c9ad2"


def _row(**overrides):
    row = {
        "id": "job-1",
        "status": "awaiting_approval",
        "thread_id": "test-run-abc",
        "meta": {"workflow_test_run": True, "state_approval_id": STATE_ID},
    }
    row.update(overrides)
    return row


def _validate(row, payload, approval_id):
    return ApprovalDecisionService(
        lock_job_row=lambda *_a, **_k: row,
        current_interrupt_payload=lambda *_a, **_k: payload,
        persist_decision=lambda **_k: None,
    ).validate_current_gate(
        row=row,
        payload=payload,
        decision=ApprovalDecisionInput(approved=True, approval_id=approval_id, comment="ok"),
        token_principal=False,
    )


class TestReadingTheStateBasedIdentity:
    def test_it_comes_from_the_job_meta(self):
        assert ApprovalDecisionService.state_based_approval_id(_row()) == STATE_ID

    def test_json_encoded_meta_is_decoded(self):
        row = _row(meta=json.dumps({"workflow_test_run": True, "state_approval_id": STATE_ID}))

        assert ApprovalDecisionService.state_based_approval_id(row) == STATE_ID

    @pytest.mark.parametrize(
        "meta",
        [
            {"state_approval_id": STATE_ID},
            {"workflow_test_run": False, "state_approval_id": STATE_ID},
            {"workflow_test_run": True},
            {},
            None,
            "not json at all",
        ],
        ids=["not-flagged", "flag-false", "no-id", "empty", "none", "garbage"],
    )
    def test_anything_else_yields_nothing(self, meta):
        assert ApprovalDecisionService.state_based_approval_id(_row(meta=meta)) == ""

    def test_a_missing_row_yields_nothing(self):
        assert ApprovalDecisionService.state_based_approval_id(None) == ""


class TestDecidingAStateBasedGate:
    def test_the_matching_id_is_accepted(self):
        """The decision that used to be rejected as a stale gate."""
        assert _validate(_row(), {}, STATE_ID) == STATE_ID

    def test_a_wrong_id_is_still_stale(self):
        _raises_input_102_1 = _row()
        with pytest.raises(ServiceConflictError) as excinfo:
            _validate(_raises_input_102_1, {}, "state-approval-someone-elses")

        assert excinfo.value.payload["code"] == "stale_approval_gate"

    def test_a_gate_without_a_stored_identity_is_still_stale(self):
        """The guard must not become "accept whatever the caller sent"."""
        _raises_input_109_1 = _row(meta={"workflow_test_run": True})
        with pytest.raises(ServiceConflictError) as excinfo:
            _validate(_raises_input_109_1, {}, "anything")

        assert excinfo.value.payload["code"] == "stale_approval_gate"

    def test_a_non_test_run_gate_without_an_interrupt_is_still_stale(self):
        _raises_input_115_1 = _row(meta={"state_approval_id": STATE_ID})
        with pytest.raises(ServiceConflictError) as excinfo:
            _validate(_raises_input_115_1, {}, STATE_ID)

        assert excinfo.value.payload["code"] == "stale_approval_gate"

    def test_a_resolved_job_is_still_rejected(self):
        """Status is checked before identity, so a decided run cannot be re-decided."""
        _raises_input_122_1 = _row(status="completed")
        with pytest.raises(ServiceConflictError) as excinfo:
            _validate(_raises_input_122_1, {}, STATE_ID)

        assert excinfo.value.payload["code"] == "approval_already_resolved"


class TestALiveInterruptStillWins:
    def test_the_interrupt_id_takes_precedence(self):
        """The stored id is a fallback, not an override."""
        assert _validate(_row(), {"interrupt_id": "int-live-7"}, "int-live-7") == "int-live-7"

    def test_the_stored_id_cannot_be_used_when_an_interrupt_exists(self):
        _raises_input_134_1 = _row()
        with pytest.raises(ServiceConflictError) as excinfo:
            _validate(_raises_input_134_1, {"interrupt_id": "int-live-7"}, STATE_ID)

        assert excinfo.value.payload["code"] == "stale_approval_gate"


class TestTheRunPathMintsAndPersistsIt:
    @staticmethod
    def _anchor(payload, **kwargs):
        from unittest.mock import MagicMock

        from core.application.workflows.platform_test_run_runtime import (
            anchor_test_run_awaiting_approval,
        )

        bl = MagicMock()
        bl.create_job.return_value = "job-1"
        anchor_test_run_awaiting_approval(
            bl,
            payload,
            project_id="p",
            workflow_id="w",
            task_id="t",
            task_summary="s",
            **kwargs,
        )
        return bl

    def test_the_minted_id_is_stored_on_the_job(self):
        bl = self._anchor(
            {"needs_user_approval": True, "thread_id": "test-run-x"},
            state_approval_id=STATE_ID,
        )

        meta = bl.create_job.call_args.kwargs["meta"]
        assert meta["state_approval_id"] == STATE_ID
        assert meta["workflow_test_run"] is True, (
            "state_based_approval_id refuses to read an unflagged row"
        )

    def test_the_resolved_interrupt_id_is_stored_on_the_job(self):
        bl = self._anchor(
            {
                "needs_user_approval": True,
                "thread_id": "test-run-x",
                "approval_id": "interrupt-current-7",
            }
        )

        assert bl.create_job.call_args.kwargs["meta"]["state_approval_id"] == "interrupt-current-7"

    def test_the_runtime_mints_one_when_the_payload_has_no_approval_id(self):
        from pathlib import Path

        source = Path("core/application/workflows/platform_test_run_runtime.py").read_text(
            encoding="utf-8"
        )

        assert 'f"state-approval-{uuid.uuid4().hex}"' in source
        assert 'payload["approval_undecidable_reason"] = ""' in source, (
            "a decidable gate must stop reporting itself as undecidable"
        )

    def test_durable_run_rehydrates_and_accepts_the_worker_persisted_gate(self, monkeypatch):
        from contextlib import nullcontext
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        from core.application.jobs import workflow_pause_persistence
        from core.application.jobs.execution_contracts import (
            TaskExecutionRequest,
            TaskGraphOutcome,
        )
        from core.application.management.scope import ScopeContext
        from core.application.registry.workflow_contract import WorkflowInterface
        from core.application.registry.workflow_contract_service import (
            WorkflowInterfaceService,
        )
        from core.application.workflows.test_run_query_service import (
            WorkflowTestRunQueryService,
        )
        from core.application.workflows.test_run_submission_service import submit_test_run
        from core.infrastructure.runtime.worker.worker_execution_adapters import (
            WorkerTerminalEffectsAdapter,
        )

        class DurableStore:
            def __init__(self):
                self.row = None

            def enqueue(self, **kwargs):
                self.row = {
                    "id": "41",
                    "thread_id": "thread-41",
                    "project_id": kwargs["project_id"],
                    "selected_workflow": kwargs["meta"]["selected_workflow_id"],
                    "task_id": kwargs["task_id"],
                    "task_summary": kwargs["task_summary"],
                    "task_source": kwargs["task_source"],
                    "status": "pending",
                    "meta": dict(kwargs["meta"]),
                    "queued_at": datetime.now(UTC),
                }
                return 41

            def get_job(self, job_id):
                assert job_id == "41"
                return self.row

            def find_by_thread_id(self, thread_id):
                assert thread_id == "thread-41"
                return self.row

            def update_job(self, job_id, **patch):
                assert job_id == "41"
                self.row.update(patch)

            def merge_job_meta(self, job_id, patch):
                assert job_id == "41"
                self.row["meta"].update(patch)

            def record_public_progress_transition(self, **_kwargs):
                return None

        class InterfaceService:
            @staticmethod
            def interface_for_workflow_id(_workflow_id):
                return WorkflowInterface(enabled=False, output_mode="none")

            @staticmethod
            def validate_inputs(interface, inputs, *, channel):
                return WorkflowInterfaceService().validate_inputs(
                    interface, inputs, channel=channel
                )

        store = DurableStore()
        store.conn = MagicMock()
        cursor = store.conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = lambda: {
            "status": store.row["status"],
            "worker_id": store.row.get("worker_id"),
        }
        monkeypatch.setattr(
            workflow_pause_persistence, "transaction_connection", lambda conn: nullcontext(conn)
        )
        submitted = submit_test_run(
            enqueue=store.enqueue,
            job_store=store,
            kind="workflow",
            workflow_id="wf-demo",
            project_id="project-1",
            task_id="task-1",
            task_summary="Pause for review",
            task_envelope={"inputs": {"project_id": "project-1"}},
            project_name="Demo",
            project_config={"project_id": "project-1"},
            workflow_interface_service=InterfaceService(),
        )
        request = TaskExecutionRequest(
            message={},
            job_id=submitted["job_id"],
            task_id="task-1",
            thread_id=submitted["thread_id"],
            task_summary="Pause for review",
            task_source="workflow_test_run",
            project_name="Demo",
            project_config={"project_id": "project-1"},
            extra_state={},
            workflow_id="wf-demo",
            test_mode=True,
        )
        result = {
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "approval_context": "Approve the release candidate.",
        }

        # The worker owns the running job before projecting its durable pause.
        store.update_job(submitted["job_id"], status="in_progress", worker_id="worker-1")
        persisted_status = WorkerTerminalEffectsAdapter(
            bl=store,
            worker_id=lambda: "worker-1",
        ).persist(
            request,
            job_row=store.row,
            project_id="project-1",
            final_status="awaiting_approval",
            result=result,
            graph_outcome=TaskGraphOutcome.from_result(result),
        )

        assert persisted_status == "awaiting_approval"
        cursor.execute.assert_called_once_with(
            "SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE", ("41",)
        )
        hydrated = WorkflowTestRunQueryService(repository=store).get_public_state("thread-41")
        assert hydrated["approval_id"]
        assert hydrated["needs_user_approval"] is True
        persisted_decisions = []
        decision_service = ApprovalDecisionService(
            lock_job_row=lambda *_args: store.row,
            current_interrupt_payload=lambda *_args: {},
            persist_decision=lambda **kwargs: persisted_decisions.append(kwargs),
        )
        decision = ApprovalDecisionInput(
            approved=True,
            approval_id=hydrated["approval_id"],
            comment="Approved after reload",
        )

        claimed = decision_service.claim_and_record(
            bl=store,
            thread_id="thread-41",
            scope=ScopeContext(account_id="acc_default", actor_id="operator-1"),
            decision=decision,
            token_principal=False,
            idempotency_key="",
        )

        assert claimed[1] == hydrated["approval_id"]
        assert claimed[5] is True
        assert persisted_decisions[0]["approval_id"] == hydrated["approval_id"]
        assert persisted_decisions[0]["decision"] == decision


class TestTheGateSurfaceExposesIt:
    def test_the_query_service_fills_in_the_identity_and_actions(self):
        from pathlib import Path

        source = Path("core/application/management/query_services_approval.py").read_text(
            encoding="utf-8"
        )

        assert "state_based_approval_id(job)" in source
        assert "ApprovalDecisionService.normalized_allowed_actions(payload)" in source, (
            "an approval with no allowed actions renders no buttons"
        )


class TestTheDecisionDoesNotFailOnAMissingProgressStream:
    """The next failure after the identity fix, found by driving a real Reject.

    A Test Run is anchored as a job so its approval can be seen and decided, but
    it never publishes a public progress stream — the ephemeral runner has no
    step recorder. `_record_in_progress_marker` called `current_snapshot`, which
    raises `LookupError`, so the reviewer's Reject returned **500**:

        LookupError: No public progress snapshot for job 73d734b4-…

    A phase-exit approval is represented by the durable checkpoint and by the
    snapshot's dedicated approval field.  It is not an ``active_steps`` item.
    The public projection therefore must not become a second authority that can
    reject an otherwise valid durable decision.
    """

    @staticmethod
    def _mark(meta):
        from unittest.mock import MagicMock, patch

        from core.application.management.approval_repository import ApprovalRepository

        row = {"id": "job-1", "project_id": "proj-1", "meta": meta}
        service = MagicMock()
        service.current_snapshot.side_effect = LookupError("No public progress snapshot")
        with (
            patch(
                "core.application.jobs.public_progress_service.PublicProgressService",
                return_value=service,
            ),
            patch("core.application.jobs.public_progress_repository.PublicProgressRepository"),
        ):
            ApprovalRepository._record_in_progress_marker(
                transaction_conn=MagicMock(), row=row, payload={}
            )
        return service

    @pytest.mark.parametrize(
        "meta",
        [{"workflow_test_run": True, "state_approval_id": STATE_ID}, {}],
        ids=["test-run", "submitted-job"],
    )
    def test_a_missing_projection_is_recreated_without_blocking_the_decision(self, meta):
        service = self._mark(meta)

        service.record_transition.assert_called_once()
        assert service.record_transition.call_args.kwargs["job_status"] == "in_progress"
        assert "step" not in service.record_transition.call_args.kwargs

    @staticmethod
    def _mark_with_snapshot(meta):
        """After a resume the snapshot exists but models no awaiting step."""
        from unittest.mock import MagicMock, patch

        from core.application.management.approval_repository import ApprovalRepository

        snapshot = MagicMock()
        snapshot.active_steps = []
        service = MagicMock()
        service.current_snapshot.return_value = MagicMock(snapshot=snapshot)
        with (
            patch(
                "core.application.jobs.public_progress_service.PublicProgressService",
                return_value=service,
            ),
            patch("core.application.jobs.public_progress_repository.PublicProgressRepository"),
        ):
            ApprovalRepository._record_in_progress_marker(
                transaction_conn=MagicMock(),
                row={"id": "job-1", "project_id": "proj-1", "meta": meta},
                payload={},
            )
        return service

    @pytest.mark.parametrize(
        "meta",
        [{"workflow_test_run": True, "state_approval_id": STATE_ID}, {}],
        ids=["test-run", "submitted-job"],
    )
    def test_a_phase_exit_gate_needs_no_awaiting_step_to_clear_approval(self, meta):
        service = self._mark_with_snapshot(meta)

        service.record_transition.assert_called_once()
        assert service.record_transition.call_args.kwargs["job_status"] == "in_progress"
        assert "step" not in service.record_transition.call_args.kwargs


class TestTheRejectionActuallyResumes:
    """`AUDIT-038`: "return into retry/rework after a rejection".

    The removed in-process resume path only claimed its resume intent when a
    live interrupt payload existed. A state-based gate has an empty payload and
    a real approval id, so the Reject was accepted (202), nothing resumed, and
    the job ended up `stuck` instead of returning into rework.

    The durable path keys the resume on the recorded decision, not on the
    payload: a state-based gate whose decision recorded a workflow resume must
    publish it.
    """

    def test_a_state_based_rejection_publishes_its_workflow_resume(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow
        from core.application.management.command_services_approval import (
            ApprovalCommandService,
        )
        from core.application.management.scope import ScopeContext

        cursor = MagicMock()
        cursor.fetchone.return_value = ("project-1", "account-1", "workspace-1", "wf", "job-1")
        store = SimpleNamespace(conn=MagicMock())
        store.conn.cursor.return_value.__enter__.return_value = cursor
        resume_row = WorkflowResumeQueueRow(
            job_id="job-1",
            project_id="project-1",
            task_id="TASK-1",
            resume_revision=1,
            publish_token="publish-1",
        )
        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: store,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: MagicMock(),
        )
        service._claim_and_record_decision = MagicMock(
            return_value=({}, STATE_ID, None, None, True, True, resume_row)
        )

        accepted = service.resume_task(
            thread_id="test-run-abc",
            scope=ScopeContext(account_id="account-1", actor_id="operator"),
            decision=ApprovalDecisionInput(
                approved=False, action="reject", comment="rework it", approval_id=STATE_ID
            ),
        )

        assert accepted.approval_id == STATE_ID
        assert accepted.status == "resume_pending"
        enqueue_resume.assert_called_once_with(
            project_id="project-1",
            job_id="job-1",
            task_id="TASK-1",
            thread_id="test-run-abc",
            resume_revision=1,
        )


class TestEachParkIsItsOwnGate:
    """`AUDIT-038`: approve/reject/override end to end.

    A rejection correctly returns the run into rework, the phase re-runs, and the
    run parks at the phase completion gate again. But the gate identity lives on
    the job row for a state-based gate, and the re-park kept the identity of the
    gate that had already been decided.

    First-decision-wins then read the follow-up Approve as a replay of the
    Reject. Observed live: Reject 202 -> rework -> re-park -> Approve
    `409 approval_already_resolved`, leaving the run permanently undecidable.
    """

    @staticmethod
    def _rotate(meta):
        from unittest.mock import MagicMock

        from core.infrastructure.runtime.graph_resume_runtime import _rotate_state_approval_id

        store = MagicMock()
        store.get_job_meta_by_thread_id.return_value = {"meta": meta}
        _rotate_state_approval_id(
            bl=MagicMock(),
            thread_id="test-run-abc",
            job_thread_store=store,
            logger=MagicMock(),
        )
        return store

    def test_a_re_park_gets_a_fresh_identity(self):
        store = self._rotate({"workflow_test_run": True, "state_approval_id": STATE_ID})

        written = store.update_job_meta_by_thread_id.call_args.kwargs["meta"]
        assert written["state_approval_id"] != STATE_ID
        assert written["state_approval_id"].startswith("state-approval-")
        assert written["workflow_test_run"] is True, "the flag must survive the rotation"

    def test_json_encoded_meta_is_handled(self):
        store = self._rotate(json.dumps({"workflow_test_run": True, "state_approval_id": STATE_ID}))

        assert store.update_job_meta_by_thread_id.called

    @pytest.mark.parametrize(
        "meta",
        [
            {"workflow_test_run": True},
            {"state_approval_id": STATE_ID},
            {},
            None,
        ],
        ids=["no-id", "not-a-test-run", "empty", "none"],
    )
    def test_a_gate_backed_by_a_real_interrupt_is_never_touched(self, meta):
        store = self._rotate(meta)

        store.update_job_meta_by_thread_id.assert_not_called()

    def test_a_rotation_failure_does_not_break_the_resume(self):
        from unittest.mock import MagicMock

        from core.infrastructure.runtime.graph_resume_runtime import _rotate_state_approval_id

        store = MagicMock()
        store.get_job_meta_by_thread_id.side_effect = RuntimeError("connection reset")

        _rotate_state_approval_id(
            bl=MagicMock(),
            thread_id="test-run-abc",
            job_thread_store=store,
            logger=MagicMock(),
        )

    def test_the_resume_path_rotates_on_a_re_park(self):
        from pathlib import Path

        source = Path("core/infrastructure/runtime/graph_resume_runtime.py").read_text(
            encoding="utf-8"
        )

        assert 'if graph_status == "awaiting_approval":' in source
        assert "_rotate_state_approval_id(" in source
