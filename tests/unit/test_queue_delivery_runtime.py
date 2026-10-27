from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.jobs.queue_delivery_queries import (
    build_queue_delivery_summary,
    load_queue_delivery_attention_jobs,
    load_queue_delivery_readiness_counts,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.description = [SimpleNamespace(name=column) for column in rows[0]]
        self.executed_sql = ""
        self.executed_params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.executed_sql = sql.as_string() if hasattr(sql, "as_string") else sql
        self.executed_params = params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _Logger:
    def __init__(self, rows):
        self.cursor = _Cursor(rows)
        self.conn = _Conn(self.cursor)

    def _column_exists(self, table, column):
        return table == "jobs" and column in {"deleted_at", "acknowledged_at"}


@pytest.mark.parametrize(
    "available_columns",
    [
        frozenset(),
        frozenset({"deleted_at"}),
        frozenset({"acknowledged_at"}),
        frozenset({"deleted_at", "acknowledged_at"}),
    ],
)
def test_readiness_counts_use_only_schema_gated_sql_and_bound_statuses(
    available_columns: frozenset[str],
) -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [(2, 3), (4, 5)]
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _column_exists=lambda table, column: table == "jobs" and column in available_columns,
    )

    counts = load_queue_delivery_readiness_counts(bl)

    assert counts == {
        "queued_pending_jobs": 2,
        "retrying_delivery_jobs": 3,
        "dead_lettered_jobs": 4,
        "post_claim_execution_failed_jobs": 5,
    }
    assert cursor.execute.call_count == 2
    pending_args, attention_args = [call.args for call in cursor.execute.call_args_list]
    assert len(pending_args) == 1
    assert len(attention_args) == 2
    pending_sql = pending_args[0]
    attention_sql, attention_params = attention_args
    pending_sql = " ".join(pending_sql.split())
    attention_sql = " ".join(attention_sql.split())
    deleted_filter = " AND deleted_at IS NULL" if "deleted_at" in available_columns else ""
    acknowledged_filter = (
        " AND acknowledged_at IS NULL" if "acknowledged_at" in available_columns else ""
    )
    assert pending_sql.endswith("WHERE status = 'pending'" + deleted_filter)
    assert attention_sql.endswith("WHERE status = ANY(%s)" + deleted_filter + acknowledged_filter)
    assert len(attention_params) == 2
    assert set(attention_params[0]) == {"failed", "max_iterations", "retryable_interrupted_step"}
    assert set(attention_params[1]) == {
        "triage_needed",
        "failed",
        "max_iterations",
        "retryable_interrupted_step",
    }


def test_load_queue_delivery_attention_jobs_explains_dead_lettered_deleted_project():
    job_id = "35f20236-be77-4124-aad9-f828c383e70c"
    bl = _Logger(
        [
            {
                "job_id": job_id,
                "project_id": "9029561a-0cfc-4e51-9e41-418790786518",
                "project_name": "Nightfire Project Updated c144",
                "project_deleted_at": "2026-06-30T09:57:51+00:00",
                "task_id": "task-1",
                "task_summary": "Incoming webhook",
                "status": "triage_needed",
                "error": "Queue payload references a project that no longer exists.",
                "last_error": "Queue payload references a project that no longer exists.",
                "queue_name": "platform.project.9029561a-0cfc-4e51-9e41-418790786518",
                "selected_workflow_key": "sample_incoming_webhook_router",
                "selected_workflow": "sample_incoming_webhook_router",
                "queued_at": "2026-07-01T10:00:00+00:00",
                "started_at": "2026-07-01T10:01:00+00:00",
                "finished_at": "2026-07-01T10:02:00+00:00",
                "acknowledged_at": None,
                "queue_delivery": {
                    "state": "dead_lettered",
                    "attempt_count": 3,
                    "retry_count": 2,
                    "last_failure_class": "missing_reference",
                    "last_failure_code": "project_not_found",
                    "last_failure_message": "Queue payload references a project that no longer exists.",
                    "dead_lettered_at": "2026-07-01T10:02:00+00:00",
                    "policy": {"retry_max_attempts": 2},
                },
            }
        ]
    )

    rows = load_queue_delivery_attention_jobs(bl, limit=3)

    assert len(rows) == 1
    assert rows[0]["job_id"] == job_id
    assert rows[0]["detail_url"] == f"/jobs/{job_id}"
    assert rows[0]["attention_kind"] == "dead_lettered"
    assert rows[0]["project_state"] == "trashed"
    assert rows[0]["failure_code"] == "project_not_found"
    assert rows[0]["retry_count"] == 2
    assert rows[0]["retry_max_attempts"] == 2
    assert "Restore the project" in rows[0]["resolution_hint"]
    assert "j.acknowledged_at IS NULL" in bl.cursor.executed_sql
    assert bl.cursor.executed_params[-1] == 3


def test_build_queue_delivery_summary_includes_attention_jobs():
    attention_jobs = [{"job_id": "job-1", "failure_code": "project_not_found"}]

    payload = build_queue_delivery_summary(
        {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 1,
            "post_claim_execution_failed_jobs": 0,
        },
        attention_jobs=attention_jobs,
    )

    assert payload["state"] == "idle"
    assert payload["attention_state"] == "dead_lettered"
    assert payload["requires_intervention"] is True
    assert payload["attention_jobs"] == attention_jobs
    assert payload["attention_job_count"] == 1
    assert payload["attention_jobs_shown"] == 1
    assert "Open a listed job" in payload["operator_action"]


def test_post_claim_execution_failure_asks_for_an_operator_without_unreadying_the_platform():
    """RC7-005: this used to assert requires_intervention is True.

    The name says it: delivery succeeded and execution then failed. That is a
    job needing an operator, not a platform that cannot accept work - and
    treating it as unreadiness made a healthy stack answer 503 until somebody
    acknowledged an ordinary failure. The attention state and the count stay so
    the job is still visible everywhere it was before.
    """
    payload = build_queue_delivery_summary(
        {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 0,
            "post_claim_execution_failed_jobs": 1,
        }
    )

    assert payload["attention_state"] == "execution_failed_after_delivery"
    assert payload["attention_job_count"] == 1
    assert "Open a listed job" in payload["operator_action"]
    assert payload["requires_intervention"] is False


def test_an_unrecoverable_delivery_still_unreadies_the_platform():
    """The other half: a dead letter means work cannot be delivered at all."""
    payload = build_queue_delivery_summary(
        {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 1,
            "post_claim_execution_failed_jobs": 0,
        }
    )

    assert payload["requires_intervention"] is True


def test_a_delivery_failure_and_an_execution_failure_together_still_unready():
    """A healthy-execution carve-out must not mask a real delivery failure."""
    payload = build_queue_delivery_summary(
        {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 1,
            "post_claim_execution_failed_jobs": 3,
        }
    )

    assert payload["requires_intervention"] is True
    assert payload["attention_job_count"] == 4


def test_idle_delivery_copy_has_keys_without_changing_runtime_state():
    snapshot = {
        "queued_pending_jobs": 0,
        "retrying_delivery_jobs": 0,
        "dead_lettered_jobs": 0,
        "post_claim_execution_failed_jobs": 0,
    }
    payload = build_queue_delivery_summary(snapshot)
    assert payload["state"] == "idle"
    assert payload["state_label"] == "Idle"
    assert payload["state_label_key"] == "react.monitoring.state_idle"
    assert (
        payload["state_message"] == "No queued delivery work is waiting for this scope right now."
    )
    assert payload["state_message_key"] == "react.monitoring.delivery_idle_message"
    assert payload["requires_intervention"] is False
    assert all(payload[key] == count for key, count in snapshot.items())


@pytest.mark.parametrize(
    "attention_count", ["dead_lettered_jobs", "post_claim_execution_failed_jobs"]
)
def test_idle_delivery_key_cannot_hide_attention_message(attention_count):
    payload = build_queue_delivery_summary({attention_count: 1})
    assert payload["state"] == "idle"
    assert payload.get("state_message_key") != "react.monitoring.delivery_idle_message"
    assert "but one or more" in payload["state_message"]


@pytest.mark.parametrize("state", ["idle", "queued_pending", "retrying_delivery"])
@pytest.mark.parametrize("attention", ["idle", "dead_lettered", "execution_failed_after_delivery"])
def test_delivery_summary_keys_preserve_all_state_and_attention_combinations(state, attention):
    counts = {
        "queued_pending_jobs": int(state == "queued_pending"),
        "retrying_delivery_jobs": int(state == "retrying_delivery"),
        "dead_lettered_jobs": int(attention == "dead_lettered"),
        "post_claim_execution_failed_jobs": int(attention == "execution_failed_after_delivery"),
    }
    payload = build_queue_delivery_summary(
        counts, attention_jobs=[{"job_id": "Owner Ąžuolas", "failure_code": "state.source"}]
    )

    def label_key(code):
        return (
            "react.monitoring.state_idle"
            if code == "idle"
            else f"react.monitoring.delivery_{code}_label"
        )

    expected_message_key = f"react.monitoring.delivery_{state}_message"
    if state == "idle" and attention != "idle":
        expected_message_key = (
            "react.monitoring.delivery_idle_dead_lettered_message"
            if attention == "dead_lettered"
            else "react.monitoring.delivery_idle_execution_failed_message"
        )
    assert payload["state"] == state
    assert payload["attention_state"] == attention
    assert payload["state_label_key"] == label_key(state)
    assert payload["state_message_key"] == expected_message_key
    assert payload["attention_label_key"] == label_key(attention)
    assert payload["attention_message_key"] == f"react.monitoring.delivery_{attention}_message"
    assert payload["operator_action_key"] == "react.monitoring.delivery_operator_action"
    assert payload["requires_intervention"] is (attention == "dead_lettered")
    assert payload["attention_jobs"][0] == {
        "job_id": "Owner Ąžuolas",
        "failure_code": "state.source",
    }
    assert all(payload[key] == count for key, count in counts.items())
