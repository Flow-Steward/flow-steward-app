from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.jobs import task_enqueue_runtime
from core.application.jobs.submission_admission import (
    PublicSubmissionPausedError,
    public_submission_admission,
)
from core.infrastructure.db import scheduled_job_lock_store
from core.infrastructure.db.scheduled_job_lock_store import (
    ScheduledJobExecutionLockSchemaUnavailableError,
)
from core.infrastructure.security import secret_codec


@pytest.fixture(autouse=True)
def _application_outbox_test_doubles(monkeypatch):
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "enqueue-test-key"})

    class _OutboxRepositoryDouble:
        def __init__(self, bl):
            self.bl = bl

        def get(self, *, job_id):
            return self.bl.get_job_dispatch_outbox(job_id)

        def mark(self, *, job_id, status, last_error=None):
            return self.bl.mark_job_dispatch_outbox(job_id, status=status, last_error=last_error)

        def settle_after_execution_advanced(self, *, job_id):
            return self.bl.settle_job_dispatch_outbox_after_execution_advanced(job_id)

        def replace_envelope(self, *, job_id, encrypted_envelope):
            return self.bl.update_job_dispatch_outbox_envelope(
                job_id, encrypted_envelope=encrypted_envelope
            )

        def renew_preparation_lease(self, *, job_id, owner_token, lease_seconds):
            return self.bl.renew_job_dispatch_preparation_lease(
                job_id, owner_token=owner_token, lease_seconds=lease_seconds
            )

        def finalize_prepared_payload(self, *, job_id, **kwargs):
            return self.bl.finalize_job_dispatch_payload(job_id, **kwargs)

        def verify_preparation_owner(self, *, job_id, owner_token):
            return self.bl.verify_job_dispatch_preparation_owner(job_id, owner_token=owner_token)

        def settle_preparation_failure(self, *, job_id, **kwargs):
            return self.bl.settle_job_dispatch_preparation_failure(job_id, **kwargs)

        def sweep_advanced_pending(self, *, limit):
            return self.bl.sweep_advanced_job_dispatch_outbox(limit=limit)

        def claim_pending(self, *, limit, lease_seconds):
            return self.bl.claim_pending_job_dispatch_outbox(
                limit=limit, lease_seconds=lease_seconds
            )

    monkeypatch.setattr(
        task_enqueue_runtime,
        "_dispatch_outbox_repository",
        lambda bl: _OutboxRepositoryDouble(bl),
    )
    monkeypatch.setattr(
        "core.application.jobs.task_dispatch_common._dispatch_outbox_repository",
        lambda bl: _OutboxRepositoryDouble(bl),
    )
    monkeypatch.setattr(
        "core.application.jobs.task_dispatch_recovery._dispatch_outbox_repository",
        lambda bl: _OutboxRepositoryDouble(bl),
    )

    class _SubmissionRepositoryDouble:
        def __init__(self, bl, _transaction_conn):
            self.bl = bl

        def create(self, *, encrypted_envelope, outbox_status, **job_fields):
            owner_token = job_fields.pop("preparation_owner_token", None)
            lease_seconds = job_fields.pop("preparation_lease_seconds", 300)
            job_id = self.bl.create_job(**job_fields)
            self.bl.create_job_dispatch_outbox(
                str(job_id),
                encrypted_envelope=encrypted_envelope,
                status=outbox_status,
                preparation_owner_token=owner_token,
                preparation_lease_seconds=lease_seconds,
            )
            return job_id

    monkeypatch.setattr(
        "core.application.jobs.job_submission_repository.JobSubmissionRepository",
        _SubmissionRepositoryDouble,
    )


def test_enqueue_task_raises_when_dsn_is_missing() -> None:
    _raises_input_105_1 = MagicMock()
    _raises_input_105_2 = MagicMock()
    _raises_input_105_3 = MagicMock()
    _raises_input_105_4 = MagicMock()
    _raises_input_105_5 = MagicMock()
    with pytest.raises(RuntimeError, match="FS_OBSERVABILITY_DSN/FS_DB_DSN not configured"):
        task_enqueue_runtime.enqueue_task(
            config={},
            create_logger_fn=_raises_input_105_1,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id=1,
            project_name="demo",
            project_config={},
            test_mode=None,
            extra_state=None,
            meta=None,
            enqueue_task_fn=_raises_input_105_2,
            build_project_config_snapshot=_raises_input_105_3,
            project_account_id_fn=_raises_input_105_4,
            logger=_raises_input_105_5,
            log_prefix="test",
        )


def test_enqueue_task_uses_observability_dsn_and_forwards_notification_context() -> None:
    create_logger = MagicMock(return_value=MagicMock())
    project_account_id = MagicMock(return_value="acc-1")

    with patch(
        "core.application.jobs.task_enqueue_runtime.enqueue_job", return_value=77
    ) as enqueue_job:
        result = task_enqueue_runtime.enqueue_task(
            config={"fs_observability_dsn": "postgres://obs-dsn"},
            create_logger_fn=create_logger,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id=1,
            project_name="demo",
            project_config={},
            test_mode=None,
            extra_state=None,
            meta=None,
            enqueue_task_fn=MagicMock(),
            build_project_config_snapshot=MagicMock(),
            project_account_id_fn=project_account_id,
            logger=MagicMock(),
            log_prefix="test",
        )

    assert result == 77
    create_logger.assert_called_once_with("postgres://obs-dsn")
    project_account_id.assert_called_once()
    assert enqueue_job.call_args.kwargs["notification_account_id"] == "acc-1"


def test_queue_transport_unavailable_matches_chained_rabbitmq_messages() -> None:
    publish_error = RuntimeError("publish not confirmed")
    wrapped = RuntimeError("publish wrapper")
    wrapped.__cause__ = publish_error

    assert task_enqueue_runtime._is_queue_transport_unavailable(wrapped) is True


def test_server_policy_snapshots_override_submission_metadata_and_state() -> None:
    meta, extra_state, _test_mode = task_enqueue_runtime.prepare_enqueue_inputs(
        project_config={
            "project_name": "server-demo",
            "project_policy": {"retry_max_attempts": 3},
        },
        test_mode=None,
        extra_state={
            "project_policy": {"retry_max_attempts": 0},
        },
        meta={
            "project_policy_snapshot": {"retry_max_attempts": 0},
            "project_config_snapshot": {"project_name": "caller-demo"},
        },
        build_project_config_snapshot=lambda config: {"source": "server", **config},
    )

    assert extra_state["project_policy"] == {"retry_max_attempts": 3}
    assert meta["project_policy_snapshot"] == {"retry_max_attempts": 3}
    assert meta["project_config_snapshot"]["source"] == "server"
    assert meta["project_config_snapshot"]["project_name"] == "server-demo"


def test_publish_failure_keeps_the_unstarted_job_pending_for_delivery_recovery() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "meta": {
            "idempotency": {
                "scope_key": "request:v1:scope",
                "key_digest": "sha256:key",
                "request_fingerprint": "sha256:request",
            },
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "not_started",
                "queue_name": "platform.project.proj-1",
                "dead_letter_queue_name": "platform.project.proj-1.dead_letter",
                "policy": {"retry_max_attempts": 2},
            },
        }
    }

    disposition = task_enqueue_runtime._log_publish_failure(
        bl=bl,
        job_id="job-1",
        project_id="proj-1",
        queue_name="platform.project.proj-1",
        publish_err=RuntimeError("publish not confirmed"),
        logger=MagicMock(),
        log_prefix="test",
    )

    assert disposition == "recoverable"
    assert bl.update_queue_delivery_meta.call_args.args == ("job-1",)
    assert bl.update_queue_delivery_meta.call_args.kwargs["status"] == "pending"
    delivery = bl.update_queue_delivery_meta.call_args.kwargs["queue_delivery"]
    assert delivery["state"] == "retrying_delivery"
    assert delivery["retry_count"] == 1
    bl.update_job.assert_not_called()


def test_known_unsent_publish_failure_keeps_idempotent_job_recoverable() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "meta": {
            "idempotency": {
                "scope_key": "request:v1:scope",
                "key_digest": "sha256:key",
                "request_fingerprint": "sha256:request",
            },
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
                "dead_letter_queue_name": "platform.project.proj-1.dead_letter",
                "policy": {"retry_max_attempts": 2},
            },
        }
    }
    publish_error = RuntimeError("RabbitMQ task queue is unavailable before task publish")
    publish_error.queue_dispatch_outcome = "not_sent"  # type: ignore[attr-defined]

    disposition = task_enqueue_runtime._log_publish_failure(
        bl=bl,
        job_id="job-1",
        project_id="proj-1",
        queue_name="platform.project.proj-1",
        publish_err=publish_error,
        logger=MagicMock(),
        log_prefix="test",
    )

    assert disposition == "recoverable"
    assert bl.update_queue_delivery_meta.call_args.args == ("job-1",)
    assert bl.update_queue_delivery_meta.call_args.kwargs["status"] == "pending"
    assert bl.update_queue_delivery_meta.call_args.kwargs["expected_dispatch_states"] == {
        "attempted"
    }
    delivery = bl.update_queue_delivery_meta.call_args.kwargs["queue_delivery"]
    assert delivery["dispatch_state"] == "not_started"
    assert delivery["state"] == "retrying_delivery"
    assert delivery["retry_count"] == 1


def test_publish_failure_terminalizes_idempotent_job_after_delivery_retries_exhausted() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "meta": {
            "idempotency": {
                "scope_key": "request:v1:scope",
                "key_digest": "sha256:key",
                "request_fingerprint": "sha256:request",
            },
            "queue_delivery": {
                "state": "queued",
                "queue_name": "platform.project.proj-1",
                "dead_letter_queue_name": "platform.project.proj-1.dead_letter",
                "policy": {"retry_max_attempts": 0},
            },
        }
    }

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
    ) as complete_failure:
        disposition = task_enqueue_runtime._log_publish_failure(
            bl=bl,
            job_id="job-1",
            project_id="proj-1",
            queue_name="platform.project.proj-1",
            publish_err=RuntimeError("publish not confirmed"),
            logger=MagicMock(),
            log_prefix="test",
        )

    assert disposition == "terminal_failed"
    assert bl.update_queue_delivery_meta.call_args.args == ("job-1",)
    assert bl.update_queue_delivery_meta.call_args.kwargs["status"] is None
    assert (
        bl.update_queue_delivery_meta.call_args.kwargs["queue_delivery"]["state"] == "dead_lettered"
    )
    assert complete_failure.call_args.kwargs["status"] == "triage_needed"
    assert complete_failure.call_args.kwargs["require_pending_unowned"] is True


def test_publish_failure_outbox_is_superseded_when_worker_wins_terminal_fence() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.settle_job_dispatch_outbox_after_execution_advanced.return_value = True
    bl.get_job.return_value = {
        "meta": {
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
                "policy": {"retry_max_attempts": 0},
            }
        }
    }

    from core.application.jobs.terminal_persistence import TerminalPersistenceConflict

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        side_effect=TerminalPersistenceConflict("worker claim won"),
    ):
        disposition = task_enqueue_runtime._log_publish_failure(
            bl=bl,
            job_id="job-1",
            project_id="proj-1",
            queue_name="platform.project.proj-1",
            publish_err=RuntimeError("publish not confirmed"),
            logger=MagicMock(),
            log_prefix="test",
        )

    task_enqueue_runtime._apply_publish_failure_outbox_disposition(
        bl,
        "job-1",
        disposition=disposition,
        error="publish not confirmed",
    )

    assert disposition == "superseded"
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_called_once_with("job-1")
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_terminal_publish_failure_marks_outbox_failed_not_superseded() -> None:
    bl = MagicMock()

    disposition = task_enqueue_runtime._apply_publish_failure_outbox_disposition(
        bl,
        "job-1",
        disposition="terminal_failed",
        error="publish not confirmed",
    )

    assert disposition == "terminal_failed"
    bl.mark_job_dispatch_outbox.assert_called_once_with(
        "job-1", status="failed", last_error="publish not confirmed"
    )
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_not_called()


def test_recoverable_publish_failure_cas_loss_supersedes_outbox() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=False)
    bl.settle_job_dispatch_outbox_after_execution_advanced.return_value = True
    bl.get_job.return_value = {
        "meta": {
            "idempotency": {
                "scope_key": "request:v1:scope",
                "key_digest": "sha256:key",
                "request_fingerprint": "sha256:request",
            },
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "not_started",
                "queue_name": "platform.project.proj-1",
                "policy": {"retry_max_attempts": 2},
            },
        }
    }

    disposition = task_enqueue_runtime._log_publish_failure(
        bl=bl,
        job_id="job-1",
        project_id="proj-1",
        queue_name="platform.project.proj-1",
        publish_err=RuntimeError("publish not confirmed"),
        logger=MagicMock(),
        log_prefix="test",
    )
    task_enqueue_runtime._apply_publish_failure_outbox_disposition(
        bl,
        "job-1",
        disposition=disposition,
        error="publish not confirmed",
    )

    assert disposition == "superseded"
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_called_once_with("job-1")
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_publish_failure_persistence_error_keeps_outbox_recoverable() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(side_effect=RuntimeError("database offline"))
    bl.get_job.return_value = {
        "meta": {
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        }
    }

    disposition = task_enqueue_runtime._log_publish_failure(
        bl=bl,
        job_id="job-1",
        project_id="proj-1",
        queue_name="platform.project.proj-1",
        publish_err=RuntimeError("publish not confirmed"),
        logger=MagicMock(),
        log_prefix="test",
    )
    task_enqueue_runtime._apply_publish_failure_outbox_disposition(
        bl,
        "job-1",
        disposition=disposition,
        error="publish not confirmed",
    )

    assert disposition == "persistence_unknown"
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_enqueue_scheduled_job_returns_schema_error_when_lock_table_missing() -> None:
    conn_cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = conn_cursor
    bl = MagicMock()
    bl.conn = conn
    bl.create_job.return_value = "11111111-1111-1111-1111-111111111111"
    bl.acquire_scheduled_job_execution_lock.side_effect = (
        ScheduledJobExecutionLockSchemaUnavailableError("missing lock table")
    )

    result = task_enqueue_runtime.enqueue_scheduled_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="schedule",
        project_id=7,
        project_name="demo",
        project_config={"project_name": "demo"},
        test_mode=None,
        extra_state={"workflow_id": "wf-1"},
        meta={},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="scheduled",
        scheduled_job_id=5,
        account_id="acc-demo",
        workflow_id="wf-1",
        lock_key="scheduled_job:acc-demo:7:5",
        run_type="scheduled",
        scheduled_for=None,
        scheduler_instance_id="scheduler-1",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "scheduled_job_lock_schema_unavailable"


def test_lifecycle_pause_blocks_spoofed_and_scheduled_new_jobs() -> None:
    bl = MagicMock()
    common = {
        "bl": bl,
        "task_id": "task-1",
        "task_summary": "summary",
        "task_source": "schedule",
        "project_id": 7,
        "project_name": "demo",
        "project_config": {"project_name": "demo"},
        "test_mode": None,
        "extra_state": {"workflow_id": "wf-1"},
        "meta": {"invocation": {"channel": "resume"}},
        "enqueue_task_fn": MagicMock(),
        "build_project_config_snapshot": lambda cfg: cfg,
        "logger": MagicMock(),
        "log_prefix": "scheduled",
    }
    with patch.object(public_submission_admission, "_lifecycle_pause_active", return_value=True):
        with pytest.raises(PublicSubmissionPausedError):
            task_enqueue_runtime.enqueue_job(**common)
        with pytest.raises(PublicSubmissionPausedError):
            task_enqueue_runtime.enqueue_scheduled_job(
                **common,
                scheduled_job_id=5,
                account_id="acc-demo",
                workflow_id="wf-1",
                lock_key="scheduled_job:acc-demo:7:5",
                run_type="scheduled",
                scheduled_for=None,
                scheduler_instance_id="scheduler-1",
            )

    bl.create_job.assert_not_called()


def test_enqueue_scheduled_job_logs_activation_failure_and_keeps_enqueued() -> None:
    conn_cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = conn_cursor
    bl = MagicMock()
    bl.conn = conn
    bl.create_job.return_value = "11111111-1111-1111-1111-111111111111"
    bl.acquire_scheduled_job_execution_lock.return_value = {"id": 77}
    bl.activate_scheduled_job_execution_lock.side_effect = RuntimeError("activate boom")

    logger = MagicMock()
    result = task_enqueue_runtime.enqueue_scheduled_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="schedule",
        project_id=7,
        project_name="demo",
        project_config={"project_name": "demo"},
        test_mode=None,
        extra_state={"workflow_id": "wf-1"},
        meta={},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=logger,
        log_prefix="scheduled",
        scheduled_job_id=5,
        account_id="acc-demo",
        workflow_id="wf-1",
        lock_key="scheduled_job:acc-demo:7:5",
        run_type="scheduled",
        scheduled_for=None,
        scheduler_instance_id="scheduler-1",
    )

    assert result["status"] == "enqueued"
    assert result["job_id"] == "11111111-1111-1111-1111-111111111111"
    logger.error.assert_called()


def test_enqueue_scheduled_job_captures_replay_snapshot_before_publish() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.jobs.job_invocation_artifacts import load_replay_snapshot
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection
    from core.infrastructure.runtime.test_mode import parse_test_mode

    events: list[str] = []
    bl = MagicMock()
    bl.create_job.return_value = "job-scheduled"
    bl.get_job.return_value = {
        "id": "job-scheduled",
        "meta": {"invocation": {"channel": "schedule"}},
    }
    bl.acquire_scheduled_job_execution_lock.return_value = {"id": 77}
    plane = ArtifactDatasetPlane()

    def publish(**_kwargs):
        events.append("publish")
        assert bl.finalize_job_dispatch_payload.called

    result = task_enqueue_runtime.enqueue_scheduled_job(
        bl=bl,
        task_id="task-scheduled",
        task_summary="Nightly",
        task_source="schedule",
        project_id="project-7",
        project_name="demo",
        project_config={"project_name": "demo"},
        test_mode=False,
        extra_state={
            "workflow_id": "wf-nightly",
            "_workflow_secret_ref_fingerprints": ["a" * 64],
        },
        meta={"invocation": {"channel": "schedule"}},
        replay_snapshot={
            "workflow_id": "wf-nightly",
            "inputs": {"region": "eu"},
            "files": [],
            "test_mode": False,
            "input_contract": "direct_inputs",
            "task_envelope": {"inputs": {"region": "eu"}, "files": []},
            "_workflow_secret_ref_fingerprints": [],
        },
        artifact_plane=plane,
        enqueue_task_fn=publish,
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="scheduled",
        scheduled_job_id="schedule-7",
        account_id="acc-demo",
        workflow_id="wf-nightly",
        lock_key="scheduled_job:acc-demo:project-7:schedule-7",
        run_type="scheduled",
        scheduled_for=None,
        scheduler_instance_id="scheduler-1",
    )

    assert result["status"] == "enqueued"
    pointer = bl.finalize_job_dispatch_payload.call_args.kwargs["meta_patch"]["replay_snapshot"]
    reservation_writes = [
        call.kwargs["meta"]["attachment_persistence_artifacts"]
        for call in bl.update_job.call_args_list
        if isinstance(call.kwargs.get("meta"), dict)
        and "attachment_persistence_artifacts" in call.kwargs["meta"]
    ]
    assert reservation_writes[-1] == [
        {
            "artifact_id": pointer["artifact_id"],
            "object_key": f"artifacts/{pointer['artifact_id']}/replay-snapshot.enc",
        }
    ]
    loaded = load_replay_snapshot(
        artifact_plane=plane,
        pointer=pointer,
        project_id="project-7",
        job_id="job-scheduled",
    )
    assert loaded["inputs"] == {"region": "eu"}
    assert loaded["_workflow_secret_ref_fingerprints"] == ["a" * 64]
    rerun_state, _ = build_rerun_workflow_selection(
        {"id": "job-scheduled", "meta": {}},
        replay_snapshot=loaded,
        to_json_obj=lambda value: value,
        parse_test_mode=parse_test_mode,
    )
    assert rerun_state["inputs"] == {"region": "eu"}
    assert rerun_state["_workflow_secret_ref_fingerprints"] == ["a" * 64]
    assert events == ["publish"]


def test_enqueue_job_persists_requested_mode_on_job_row() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    logger = MagicMock()
    enqueue_task_fn = MagicMock()

    job_id = task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        project_id="proj-1",
        project_name="demo",
        project_config={"secret_token": "top-secret"},
        mode="greenfield",
        test_mode=True,
        extra_state={"workflow_id": "research_brief"},
        meta={},
        enqueue_task_fn=enqueue_task_fn,
        build_project_config_snapshot=lambda cfg: cfg,
        logger=logger,
        log_prefix="enqueue",
    )

    assert job_id == "job-123"
    assert bl.create_job.call_args.kwargs["mode"] == "greenfield"
    assert bl.create_job.call_args.kwargs["selected_workflow_key"] == "research_brief"
    bl.record_public_progress_transition.assert_called_once()
    assert bl.record_public_progress_transition.call_args.kwargs["workflow_id"] == (
        "research_brief"
    )


def test_enqueue_job_commits_job_outbox_and_initial_snapshot_before_publish() -> None:
    events: list[str] = []
    bl = MagicMock()
    bl.create_job.side_effect = lambda **_kwargs: events.append("job") or "job-123"
    bl.record_public_progress_transition.side_effect = lambda **_kwargs: events.append("snapshot")
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    @contextmanager
    def _transaction(_bl):
        events.append("begin")
        try:
            yield None
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    publish = MagicMock(side_effect=lambda **_kwargs: events.append("publish"))
    with patch.object(task_enqueue_runtime, "_job_lifecycle_transaction", _transaction):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="api",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-1"},
            meta={},
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=MagicMock(),
            log_prefix="test",
        )

    assert events[:4] == ["begin", "job", "snapshot", "commit"]
    assert events[4] == "publish"


def test_scheduled_lock_conflict_rolls_back_job_and_initial_snapshot() -> None:
    events: list[str] = []
    bl = MagicMock()
    bl.create_job.side_effect = lambda **_kwargs: events.append("job") or "job-123"
    bl.record_public_progress_transition.side_effect = lambda **_kwargs: events.append("snapshot")
    bl.acquire_scheduled_job_execution_lock.return_value = None
    bl.list_active_scheduled_job_execution_locks.return_value = [{"job_id": "job-existing"}]

    @contextmanager
    def _transaction(_bl):
        events.append("begin")
        try:
            yield None
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    with patch(
        "core.application.jobs.task_scheduling._job_lifecycle_transaction",
        _transaction,
    ):
        result = task_enqueue_runtime.enqueue_scheduled_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="schedule",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-1"},
            meta={},
            enqueue_task_fn=MagicMock(),
            build_project_config_snapshot=lambda cfg: cfg,
            logger=MagicMock(),
            log_prefix="test",
            scheduled_job_id="schedule-1",
            account_id="account-1",
            workflow_id="wf-1",
            lock_key="lock-1",
            run_type="scheduled",
            scheduled_for=None,
            scheduler_instance_id="scheduler-1",
        )

    assert result["status"] == "conflict"
    assert events == ["begin", "job", "snapshot", "rollback"]


def test_enqueue_job_keeps_outbox_recoverable_until_broker_confirmation() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        project_id="proj-1",
        project_name="demo",
        project_config={"secret_token": "top-secret"},
        test_mode=False,
        extra_state={},
        meta={},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="enqueue",
    )

    persisted_meta = bl.create_job.call_args.kwargs["meta"]
    assert "dispatch_envelope" not in persisted_meta
    ciphertext = bl.create_job_dispatch_outbox.call_args.kwargs["encrypted_envelope"]
    assert "top-secret" not in ciphertext
    assert len(bl.update_queue_delivery_meta.call_args_list) == 2
    assert (
        bl.update_queue_delivery_meta.call_args_list[0].kwargs["queue_delivery"]["dispatch_state"]
        == "attempted"
    )
    assert (
        bl.update_queue_delivery_meta.call_args_list[1].kwargs["queue_delivery"]["dispatch_state"]
        == "publisher_confirmed"
    )
    bl.mark_job_dispatch_outbox.assert_called_with("job-123", status="published", last_error=None)


def test_queue_delivery_transition_requires_atomic_gateway() -> None:
    bl = SimpleNamespace(update_job=MagicMock())

    with pytest.raises(RuntimeError, match="atomic queue-delivery gateway"):
        task_enqueue_runtime._persist_queue_delivery_transition(
            bl,
            "job-123",
            {"queue_delivery": {"dispatch_state": "publisher_confirmed"}},
            expected_dispatch_states={"attempted"},
        )

    bl.update_job.assert_not_called()


def test_enqueue_job_replaces_outbox_with_post_hook_attachment_envelope() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    publish = MagicMock()

    task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        project_id="proj-1",
        project_name="demo",
        project_config={"secret_token": "top-secret"},
        test_mode=False,
        extra_state={
            "task_envelope": {"payload_complete": False, "files": []},
        },
        meta={
            "idempotency": {"scope_key": "scope", "key_digest": "key", "request_fingerprint": "fp"}
        },
        enqueue_task_fn=publish,
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="enqueue",
        pre_publish_fn=lambda _job_id, _bl, _heartbeat: {
            "task_envelope": {
                "payload_complete": True,
                "files": [{"artifact_id": "artifact-1"}],
            },
            "files": [{"artifact_id": "artifact-1"}],
        },
        fail_on_pre_publish_error=True,
    )

    final_ciphertext = bl.finalize_job_dispatch_payload.call_args.kwargs["encrypted_envelope"]
    final_envelope = json.loads(task_enqueue_runtime.decrypt_secret(final_ciphertext))
    assert final_envelope["extra_state"]["task_envelope"] == {
        "payload_complete": True,
        "files": [{"artifact_id": "artifact-1"}],
    }
    assert publish.call_args.kwargs["extra_state"]["task_envelope"]["payload_complete"] is True
    assert publish.call_args.kwargs["extra_state"]["task_envelope"]["files"] == [
        {"artifact_id": "artifact-1"}
    ]


@pytest.mark.parametrize("with_replay", [False, True])
def test_prepared_upload_keeps_pinned_job_metadata_out_of_execution(with_replay) -> None:
    descriptor = {
        "artifact_id": "art-upload",
        "filename": "REPORT.CSV",
        "size_bytes": 29,
        "sha256": "a" * 64,
    }
    prepared_inputs = {"source_file": descriptor, "request": "Read the uploaded CSV"}
    task_envelope = {"payload_complete": True, "inputs": prepared_inputs, "files": [descriptor]}
    durable_meta = {
        "workflow_definition_ref": {"revision": 4, "digest": "b" * 64},
        "invocation": {"files": [{"artifact_id": "art-upload", "filename": "REPORT.CSV"}]},
        "attachment_persistence_artifacts": [
            {"artifact_id": "art-upload", "object_key": "artifacts/art-upload/blob"}
        ],
        "idempotency": {"scope_key": "scope", "key_digest": "key", "request_fingerprint": "fp"},
    }
    prepared_patch = {
        **durable_meta,
        "files": [descriptor],
        "inputs": prepared_inputs,
        "task_envelope": task_envelope,
    }
    bl = MagicMock()
    bl.create_job.return_value = "job-upload"
    bl.get_job.return_value = {"id": "job-upload", "meta": copy.deepcopy(durable_meta)}
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    publish = MagicMock()
    plane = MagicMock()
    plane.get_artifact.return_value = {
        **descriptor,
        "owner_scope": {"project_id": "proj-1", "job_id": "job-upload"},
    }
    snapshots = []

    def capture_snapshot(**kwargs):
        snapshots.append(kwargs["snapshot"])
        return {"kind": "job_replay_snapshot", "version": 1, "status": "available"}

    with patch.object(task_enqueue_runtime, "capture_replay_snapshot", capture_snapshot):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-upload",
            task_summary="Read the uploaded CSV",
            task_source="mcp_workflow",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-upload", "files": [], "inputs": {}},
            meta={},
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=MagicMock(),
            log_prefix="enqueue",
            pre_publish_fn=lambda _job_id, _bl, _heartbeat: prepared_patch,
            fail_on_pre_publish_error=True,
            replay_snapshot={"workflow_id": "wf-upload", "inputs": {}, "files": []}
            if with_replay
            else None,
            artifact_plane=plane if with_replay else None,
        )

    finalized = bl.finalize_job_dispatch_payload.call_args.kwargs
    envelope = json.loads(task_enqueue_runtime.decrypt_secret(finalized["encrypted_envelope"]))
    expected_state = {
        "workflow_id": "wf-upload",
        "files": [descriptor],
        "inputs": prepared_inputs,
        "task_envelope": task_envelope,
    }
    assert envelope["extra_state"] == expected_state
    assert publish.call_args.kwargs["extra_state"] == expected_state
    assert "workflow_definition_ref" not in envelope["extra_state"]
    assert "runtime_schema_version" not in envelope["extra_state"]
    for key, value in durable_meta.items():
        assert finalized["meta_patch"][key] == value
    if with_replay:
        assert len(snapshots) == 1
        assert snapshots[0]["inputs"]["source_file"] == descriptor
        assert snapshots[0]["task_envelope"]["inputs"]["source_file"] == descriptor
        assert snapshots[0]["files"] == [
            {**descriptor, "owner_project_id": "proj-1", "owner_job_id": "job-upload"}
        ]
        assert finalized["meta_patch"]["replay_snapshot"]["status"] == "available"
        assert not {"files", "inputs", "task_envelope"} & finalized["meta_patch"].keys()
    else:
        assert finalized["meta_patch"] == prepared_patch


def test_enqueue_captures_replay_snapshot_after_attachments_and_before_publish() -> None:
    events: list[str] = []
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.get_job.return_value = {
        "id": "job-123",
        "meta": {"invocation": {"mapped_inputs": {"invoice_id": "INV-1"}}},
    }
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    publish = MagicMock(side_effect=lambda **_kwargs: events.append("publish"))
    artifact_plane = MagicMock()
    artifact_plane.get_artifact.return_value = {
        "artifact_id": "art-final",
        "sha256": "a" * 64,
        "size_bytes": 17,
        "owner_scope": {"project_id": "proj-1", "job_id": "job-123"},
    }

    def persist_attachments(_job_id, _bl, _heartbeat):
        events.append("attachments")
        return {
            "task_envelope": {
                "inputs": {"invoice_id": "INV-1"},
                "files": [{"artifact_id": "art-final", "sha256": "a" * 64, "size_bytes": 17}],
            },
            "files": [{"artifact_id": "art-final", "sha256": "a" * 64, "size_bytes": 17}],
        }

    def capture_snapshot(**kwargs):
        events.append("snapshot")
        assert kwargs["job_id"] == "job-123"
        assert kwargs["snapshot"]["files"] == [
            {
                "artifact_id": "art-final",
                "sha256": "a" * 64,
                "size_bytes": 17,
                "owner_project_id": "proj-1",
                "owner_job_id": "job-123",
            }
        ]
        return {"kind": "job_replay_snapshot", "version": 1, "status": "available"}

    with patch.object(
        task_enqueue_runtime,
        "capture_replay_snapshot",
        side_effect=capture_snapshot,
    ):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-1", "files": []},
            meta={"invocation": {"mapped_inputs": {"invoice_id": "INV-1"}}},
            replay_snapshot={
                "workflow_id": "wf-1",
                "inputs": {"invoice_id": "INV-1"},
                "files": [],
                "test_mode": False,
            },
            artifact_plane=artifact_plane,
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=MagicMock(),
            log_prefix="enqueue",
            pre_publish_fn=persist_attachments,
            fail_on_pre_publish_error=True,
        )

    assert events == ["attachments", "snapshot", "publish"]
    assert bl.create_job_dispatch_outbox.call_args.kwargs["status"] == "preparing"
    finalized = bl.finalize_job_dispatch_payload.call_args.kwargs
    assert finalized["meta_patch"]["replay_snapshot"]["status"] == "available"
    assert "task_envelope" not in finalized["meta_patch"]
    assert "files" not in finalized["meta_patch"]


def test_replay_snapshot_capture_failure_prevents_publish_and_terminalizes() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.get_job.return_value = {"id": "job-123", "meta": {}}
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    publish = MagicMock()

    _raises_input_982_1 = MagicMock()
    _raises_input_982_2 = MagicMock()
    with (
        patch.object(
            task_enqueue_runtime,
            "capture_replay_snapshot",
            side_effect=RuntimeError("storage unavailable"),
        ),
        patch.object(task_enqueue_runtime, "_terminalize_pre_publish_failure") as terminalize,
        pytest.raises(RuntimeError, match="storage unavailable"),
    ):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-1"},
            meta={},
            replay_snapshot={"workflow_id": "wf-1", "inputs": {}, "files": [], "test_mode": False},
            artifact_plane=_raises_input_982_1,
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=_raises_input_982_2,
            log_prefix="enqueue",
        )

    publish.assert_not_called()
    terminalize.assert_called_once()


def test_replay_snapshot_reservation_is_journaled_before_capture() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.get_job.return_value = {"id": "job-123", "meta": {}}
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    publish = MagicMock()

    def fail_capture(**kwargs):
        artifact_id = kwargs.get("artifact_id")
        assert isinstance(artifact_id, str)
        assert artifact_id.startswith("art_")
        journaled = bl.update_job.call_args.kwargs["meta"]["attachment_persistence_artifacts"]
        assert journaled == [
            {
                "artifact_id": artifact_id,
                "object_key": f"artifacts/{artifact_id}/replay-snapshot.enc",
            }
        ]
        raise RuntimeError("storage unavailable")

    _raises_input_1040_1 = MagicMock()
    _raises_input_1040_2 = MagicMock()
    with (
        patch.object(task_enqueue_runtime, "capture_replay_snapshot", side_effect=fail_capture),
        patch.object(task_enqueue_runtime, "_terminalize_pre_publish_failure"),
        pytest.raises(RuntimeError, match="storage unavailable"),
    ):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"workflow_id": "wf-1"},
            meta={},
            replay_snapshot={"workflow_id": "wf-1", "inputs": {}, "files": [], "test_mode": False},
            artifact_plane=_raises_input_1040_1,
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=_raises_input_1040_2,
            log_prefix="enqueue",
        )

    publish.assert_not_called()


def test_enqueue_job_commits_job_before_attachment_side_effects() -> None:
    transaction_active = False

    @contextmanager
    def transaction():
        nonlocal transaction_active
        transaction_active = True
        try:
            yield None
        finally:
            transaction_active = False

    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(transaction=transaction)
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    def persist_attachments(_job_id, _bl, preparation_heartbeat):
        assert transaction_active is False
        preparation_heartbeat()
        return {"task_envelope": {"payload_complete": True, "files": []}}

    task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        project_id="proj-1",
        project_name="demo",
        project_config={},
        test_mode=False,
        extra_state={"task_envelope": {"payload_complete": False, "files": []}},
        meta={"task_envelope": {"payload_complete": False, "files": []}},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="enqueue",
        pre_publish_fn=persist_attachments,
        fail_on_pre_publish_error=True,
    )


def test_enqueue_job_finalizes_prepared_payload_and_outbox_in_one_transaction() -> None:
    transaction_active = False

    @contextmanager
    def transaction():
        nonlocal transaction_active
        transaction_active = True
        try:
            yield None
        finally:
            transaction_active = False

    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(transaction=transaction)
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    def finalize_payload(*_args, **_kwargs):
        assert transaction_active is True
        return True

    bl.finalize_job_dispatch_payload.side_effect = finalize_payload

    task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        project_id="proj-1",
        project_name="demo",
        project_config={},
        test_mode=False,
        extra_state={"task_envelope": {"payload_complete": False, "files": []}},
        meta={"task_envelope": {"payload_complete": False, "files": []}},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="enqueue",
        pre_publish_fn=lambda _job_id, _bl, _heartbeat: {
            "task_envelope": {"payload_complete": True, "files": []}
        },
        fail_on_pre_publish_error=True,
    )

    assert "dispatch_outbox_status" not in bl.create_job.call_args.kwargs
    bl.finalize_job_dispatch_payload.assert_called_once()
    assert bl.finalize_job_dispatch_payload.call_args.kwargs["preparation_owner_token"]
    bl.update_job_dispatch_outbox_envelope.assert_not_called()


def test_enqueue_job_terminalizes_when_prepared_payload_finalization_fails() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.update_job_dispatch_outbox_envelope.return_value = False
    bl.finalize_job_dispatch_payload.return_value = False
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    _raises_input_1174_1 = MagicMock()
    _raises_input_1174_2 = MagicMock()
    with (
        patch(
            "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
        ) as complete_failure,
        pytest.raises(RuntimeError, match="could not be finalized"),
    ):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"task_envelope": {"payload_complete": False, "files": []}},
            meta={"task_envelope": {"payload_complete": False, "files": []}},
            enqueue_task_fn=_raises_input_1174_1,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=_raises_input_1174_2,
            log_prefix="enqueue",
            pre_publish_fn=lambda _job_id, _bl, _heartbeat: {
                "task_envelope": {"payload_complete": True, "files": []}
            },
            fail_on_pre_publish_error=True,
        )

    complete_failure.assert_called_once()
    assert complete_failure.call_args.kwargs["require_pending_unowned"] is True
    bl.settle_job_dispatch_preparation_failure.assert_called_with(
        "job-123",
        owner_token=bl.create_job_dispatch_outbox.call_args.kwargs["preparation_owner_token"],
        last_error="pre_publish_setup_failed",
    )


def test_lost_preparation_lease_never_terminalizes_or_marks_transferred_outbox() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-lease-lost"
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)

    _raises_input_1216_1 = MagicMock()
    _raises_input_1216_2 = MagicMock()
    with (
        patch.object(
            task_enqueue_runtime,
            "_run_pre_publish_hook",
            side_effect=task_enqueue_runtime.PreparationLeaseLostError("lease transferred"),
        ),
        patch.object(task_enqueue_runtime, "_terminalize_pre_publish_failure") as terminalize,
        pytest.raises(task_enqueue_runtime.PreparationLeaseLostError),
    ):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="manual",
            project_id="project-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={"task_envelope": {"payload_complete": False}},
            meta={
                "attachment_persistence_artifacts": [
                    {"artifact_id": "artifact-1", "object_key": "artifacts/artifact-1/blob"}
                ]
            },
            enqueue_task_fn=_raises_input_1216_1,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=_raises_input_1216_2,
            log_prefix="enqueue",
            pre_publish_fn=lambda _job_id, _bl, _heartbeat: {},
            fail_on_pre_publish_error=True,
        )

    terminalize.assert_not_called()
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_redeliver_pre_execution_job_republishes_only_the_existing_job() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "task_id": "task-1",
        "thread_id": "thread-1",
        "task_summary": "summary",
        "task_source": "webhook",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "selected_workflow_id": "wf-1",
            "entry_path": "webhook",
            "source_system": "webhook",
            "source_event_id": "request-1",
            "project_config_snapshot": {"project_name": "demo"},
            "files": [{"artifact_id": "artifact-1"}],
            "execution_envelope": {
                "inputs": {"invoice_id": "INV-1", "token": "top-secret"},
                "files": [{"artifact_id": "artifact-1"}],
            },
            "invocation": {"mapped_inputs": {"invoice_id": "INV-1"}},
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "not_started",
                "queue_name": "platform.project.proj-1",
            },
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "encrypted_envelope": task_enqueue_runtime._encrypt_dispatch_envelope(
            {
                "project_name": "demo",
                "project_config": {
                    "project_name": "demo",
                    "secret_token": "top-secret",
                },
                "extra_state": {
                    "workflow_id": "wf-1",
                    "task_envelope": {
                        "inputs": {"invoice_id": "INV-1", "token": "top-secret"},
                        "files": [{"artifact_id": "artifact-1"}],
                    },
                    "inputs": {"invoice_id": "INV-1", "token": "top-secret"},
                },
            }
        ),
    }
    publish = MagicMock()

    assert task_enqueue_runtime.redeliver_pre_execution_job(
        bl=bl,
        job_id="job-123",
        enqueue_task_fn=publish,
        logger=MagicMock(),
    )
    assert publish.call_args.kwargs["job_id"] == "job-123"
    assert publish.call_args.kwargs["extra_state"]["task_envelope"]["files"] == [
        {"artifact_id": "artifact-1"}
    ]
    assert publish.call_args.kwargs["extra_state"]["inputs"]["token"] == "top-secret"
    assert publish.call_args.kwargs["project_config"]["secret_token"] == "top-secret"
    assert (
        bl.update_queue_delivery_meta.call_args_list[0].kwargs["queue_delivery"]["dispatch_state"]
        == "attempted"
    )
    assert (
        bl.update_queue_delivery_meta.call_args_list[1].kwargs["queue_delivery"]["dispatch_state"]
        == "publisher_confirmed"
    )


def test_redeliver_attempted_job_publishes_inside_final_transaction_fence() -> None:
    transaction_active = False

    @contextmanager
    def transaction():
        nonlocal transaction_active
        transaction_active = True
        try:
            yield None
        finally:
            transaction_active = False

    bl = MagicMock()
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(transaction=transaction)
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "encrypted_envelope": task_enqueue_runtime._encrypt_dispatch_envelope(
            {
                "project_name": "demo",
                "project_config": {},
                "extra_state": {},
            }
        ),
    }

    def publish(**_kwargs):
        assert transaction_active is True

    assert task_enqueue_runtime.redeliver_pre_execution_job(
        bl=bl,
        job_id="job-123",
        enqueue_task_fn=publish,
        logger=MagicMock(),
        outbox_claimed=True,
    )
    bl.mark_job_dispatch_outbox.assert_called_with("job-123", status="published", last_error=None)


def test_redeliver_pre_execution_job_refuses_terminal_or_claimed_job() -> None:
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job-123",
        "status": "cancelled",
        "worker_id": None,
        "meta": {"queue_delivery": {"state": "retrying_delivery", "dispatch_state": "not_started"}},
    }
    publish = MagicMock()

    assert not task_enqueue_runtime.redeliver_pre_execution_job(
        bl=bl,
        job_id="job-123",
        enqueue_task_fn=publish,
        logger=MagicMock(),
        outbox_claimed=True,
    )
    publish.assert_not_called()
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_called_once_with("job-123")


def test_redeliver_settles_outbox_when_attempt_fence_loses_to_worker() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=False)
    bl.settle_job_dispatch_outbox_after_execution_advanced.return_value = True
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "not_started",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "encrypted_envelope": task_enqueue_runtime._encrypt_dispatch_envelope(
            {
                "project_name": "demo",
                "project_config": {},
                "extra_state": {},
            }
        ),
    }
    publish = MagicMock()

    assert not task_enqueue_runtime.redeliver_pre_execution_job(
        bl=bl,
        job_id="job-123",
        enqueue_task_fn=publish,
        logger=MagicMock(),
        outbox_claimed=True,
    )

    publish.assert_not_called()
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_called_once_with("job-123")


def test_dispatch_outbox_relay_recovers_attempted_job_after_publisher_crash() -> None:
    bl = MagicMock()
    bl.claim_pending_job_dispatch_outbox.return_value = ["job-123"]
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "task_id": "task-1",
        "thread_id": "thread-1",
        "task_summary": "summary",
        "task_source": "api",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "queued",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "encrypted_envelope": task_enqueue_runtime._encrypt_dispatch_envelope(
            {
                "project_name": "demo",
                "project_config": {},
                "extra_state": {"workflow_id": "wf-1"},
            }
        ),
    }
    publish = MagicMock()

    result = task_enqueue_runtime.relay_pending_dispatch_outbox(
        bl=bl,
        enqueue_task_fn=publish,
        logger=MagicMock(),
    )

    assert result == {"claimed": 1, "published": 1, "superseded": 0, "failed": 0}
    publish.assert_called_once()
    assert (
        bl.update_queue_delivery_meta.call_args.kwargs["queue_delivery"]["dispatch_state"]
        == "publisher_confirmed"
    )


def test_dispatch_outbox_relay_sweeps_advanced_pending_rows_before_claim() -> None:
    bl = MagicMock()
    bl.sweep_advanced_job_dispatch_outbox.return_value = 2
    bl.claim_pending_job_dispatch_outbox.return_value = []

    result = task_enqueue_runtime.relay_pending_dispatch_outbox(
        bl=bl,
        enqueue_task_fn=MagicMock(),
        logger=MagicMock(),
        limit=25,
    )

    assert result == {"claimed": 0, "published": 0, "superseded": 2, "failed": 0}
    bl.sweep_advanced_job_dispatch_outbox.assert_called_once_with(limit=25)


def test_dispatch_outbox_relay_terminalizes_unreadable_envelope_after_limit() -> None:
    bl = MagicMock()
    bl.claim_pending_job_dispatch_outbox.return_value = ["job-123"]
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "attempt_count": 3,
        "encrypted_envelope": "not-valid-ciphertext",
    }

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
    ) as complete_failure:
        result = task_enqueue_runtime.relay_pending_dispatch_outbox(
            bl=bl,
            enqueue_task_fn=MagicMock(),
            logger=MagicMock(),
        )

    assert result == {"claimed": 1, "published": 0, "superseded": 0, "failed": 1}
    bl.mark_job_dispatch_outbox.assert_called_once_with(
        "job-123",
        status="failed",
        last_error="dispatch_envelope_unreadable",
    )
    complete_failure.assert_called_once_with(
        job_id="job-123",
        status="failed",
        error_message="Durable queue dispatch data could not be read.",
        error_code="dispatch_envelope_unreadable",
        category="queue",
        retryable=False,
        require_pending_unowned=True,
    )


def test_dispatch_outbox_relay_settles_outbox_when_worker_claim_wins() -> None:
    bl = MagicMock()
    bl.claim_pending_job_dispatch_outbox.return_value = ["job-123"]
    outbox = {
        "status": "pending",
        "attempt_count": 3,
        "encrypted_envelope": "not-valid-ciphertext",
    }
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = outbox

    def settle(_job_id: str) -> bool:
        outbox["status"] = "superseded"
        return True

    bl.settle_job_dispatch_outbox_after_execution_advanced.side_effect = settle

    from core.application.jobs.terminal_persistence import TerminalPersistenceConflict

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        side_effect=TerminalPersistenceConflict("worker claim won"),
    ):
        logger = MagicMock()
        result = task_enqueue_runtime.relay_pending_dispatch_outbox(
            bl=bl,
            enqueue_task_fn=MagicMock(),
            logger=logger,
        )

    assert result == {"claimed": 1, "published": 0, "superseded": 1, "failed": 0}
    bl.mark_job_dispatch_outbox.assert_not_called()
    bl.settle_job_dispatch_outbox_after_execution_advanced.assert_called_once_with("job-123")
    logger.error.assert_not_called()
    logger.info.assert_called()


def test_dispatch_outbox_relay_retries_unreadable_envelope_before_limit() -> None:
    bl = MagicMock()
    bl.claim_pending_job_dispatch_outbox.return_value = ["job-123"]
    bl.get_job.return_value = {
        "id": "job-123",
        "project_id": "proj-1",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "attempted",
                "queue_name": "platform.project.proj-1",
            }
        },
    }
    bl.get_job_dispatch_outbox.return_value = {
        "status": "pending",
        "attempt_count": 2,
        "encrypted_envelope": "not-valid-ciphertext",
    }

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
    ) as complete_failure:
        result = task_enqueue_runtime.relay_pending_dispatch_outbox(
            bl=bl,
            enqueue_task_fn=MagicMock(),
            logger=MagicMock(),
        )

    assert result == {"claimed": 1, "published": 0, "superseded": 0, "failed": 1}
    bl.mark_job_dispatch_outbox.assert_not_called()
    complete_failure.assert_not_called()


def test_redeliver_refuses_missing_encrypted_dispatch_outbox() -> None:
    bl = MagicMock()
    type(bl).update_queue_delivery_meta = MagicMock(return_value=True)
    bl.get_job.return_value = {
        "id": "job-123",
        "status": "pending",
        "worker_id": None,
        "meta": {
            "execution_envelope": {"inputs": {"token": "secret"}},
            "project_config_snapshot": {"token": "[REDACTED]"},
            "queue_delivery": {
                "state": "retrying_delivery",
                "dispatch_state": "not_started",
            },
        },
    }
    publish = MagicMock()

    assert not task_enqueue_runtime.redeliver_pre_execution_job(
        bl=bl,
        job_id="job-123",
        enqueue_task_fn=publish,
        logger=MagicMock(),
    )
    publish.assert_not_called()
    bl.update_queue_delivery_meta.assert_not_called()


def test_enqueue_job_does_not_publish_when_attempt_fence_is_lost() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-123"
    bl.get_job.return_value = {"meta": {}}
    type(bl).update_queue_delivery_meta = MagicMock(return_value=False)
    publish = MagicMock()

    _raises_input_1677_1 = MagicMock()
    with pytest.raises(RuntimeError, match="publish fence"):
        task_enqueue_runtime.enqueue_job(
            bl=bl,
            task_id="task-1",
            task_summary="summary",
            task_source="webhook",
            project_id="proj-1",
            project_name="demo",
            project_config={},
            test_mode=False,
            extra_state={},
            meta={},
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda cfg: cfg,
            logger=_raises_input_1677_1,
            log_prefix="test",
        )

    publish.assert_not_called()


def test_enqueue_job_initializes_queue_delivery_policy_subset() -> None:
    bl = MagicMock()
    bl.create_job.return_value = "job-456"

    task_enqueue_runtime.enqueue_job(
        bl=bl,
        task_id="task-2",
        task_summary="summary",
        task_source="manual",
        project_id="proj-2",
        project_name="demo",
        project_config={
            "project_policy": {
                "retry_strategy": "exponential",
                "retry_max_attempts": 4,
                "retry_backoff_initial_seconds": 15,
                "retry_backoff_max_seconds": 120,
            }
        },
        test_mode=False,
        extra_state={},
        meta={},
        enqueue_task_fn=MagicMock(),
        build_project_config_snapshot=lambda cfg: cfg,
        logger=MagicMock(),
        log_prefix="enqueue",
    )

    queue_delivery = bl.create_job.call_args.kwargs["meta"]["queue_delivery"]
    assert queue_delivery["queue_name"] == "platform.project.proj-2"
    assert queue_delivery["dead_letter_queue_name"] == "platform.project.proj-2.dead_letter"
    assert queue_delivery["policy"]["retry_max_attempts"] == 4
    assert queue_delivery["policy"]["supported_fields"] == [
        {
            "field": "retry_max_attempts",
            "scope": "delivery_retry",
            "reason": "Maximum RabbitMQ redelivery retries before the job is dead-lettered.",
        }
    ]
    ignored_fields = {
        row["field"]: row["reason"] for row in queue_delivery["policy"]["ignored_fields"]
    }
    assert "retry_strategy" in ignored_fields
    assert "retry_backoff_initial_seconds" in ignored_fields
    assert "retry_backoff_max_seconds" in ignored_fields


def test_reconcile_promotes_expired_pre_publish_lock_for_non_terminal_job() -> None:
    now = datetime(2026, 4, 6, 18, 0, tzinfo=UTC)
    row = {
        "id": 9,
        "status": "pre_publish",
        "expires_at": now - timedelta(seconds=60),
        "linked_job_exists": 44,
        "linked_job_status": "in_progress",
    }
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    with (
        patch.object(
            scheduled_job_lock_store,
            "activate_scheduled_job_execution_lock",
            return_value={"id": 9, "job_id": 44, "scheduled_job_id": 5},
        ) as activate_mock,
        patch.object(
            scheduled_job_lock_store,
            "release_scheduled_job_execution_lock_by_id",
            return_value=None,
        ) as release_mock,
    ):
        summary = scheduled_job_lock_store.reconcile_scheduled_job_execution_locks(
            conn=conn,
            table_exists=lambda _name: True,
            coerce_int=lambda value, default: int(value) if value is not None else int(default),
            now=now,
        )

    assert summary["checked"] == 1
    assert summary["activated"] == 1
    assert summary["released"] == 0
    assert summary["stale"] == 0
    assert summary["details"][0]["release_reason"] == "pre_publish_promoted_active"
    activate_mock.assert_called_once()
    release_mock.assert_not_called()
    _sql, params = cur.execute.call_args_list[0].args
    assert "retryable_interrupted_step" not in list(params[1] or [])


def test_replay_snapshot_keeps_prepared_named_file_binding():
    descriptor = {
        "artifact_id": "art-upload",
        "filename": "actual.csv",
        "size_bytes": 17,
        "sha256": "a" * 64,
    }
    plane = MagicMock()
    plane.get_artifact.return_value = {
        **descriptor,
        "owner_scope": {"project_id": "proj-1", "job_id": "job-1"},
    }
    snapshot = task_enqueue_runtime._final_replay_snapshot(
        {"inputs": {"source_file": {"filename": "actual.csv"}}, "files": []},
        artifact_plane=plane,
        project_id="proj-1",
        job_id="job-1",
        effective_extra_state={
            "inputs": {"source_file": descriptor},
            "files": [descriptor],
            "task_envelope": {"inputs": {"source_file": descriptor}},
        },
    )
    assert snapshot["inputs"]["source_file"]["artifact_id"] == "art-upload"
    assert snapshot["task_envelope"]["inputs"]["source_file"]["artifact_id"] == "art-upload"
    assert snapshot["files"][0]["artifact_id"] == "art-upload"
