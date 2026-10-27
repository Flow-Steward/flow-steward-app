from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.application.jobs import queue_delivery_queries as queue_delivery_runtime_queries
from core.infrastructure.runtime import readiness_runtime

_DEFAULT_POSTURE_SUCCESS = object()


def _build_bl(
    *,
    pending_count: int = 0,
    stale_leases: int = 0,
    worker_ids=None,
    posture_last_success=_DEFAULT_POSTURE_SUCCESS,
    workflow_last_success=_DEFAULT_POSTURE_SUCCESS,
):
    bl = MagicMock()
    if posture_last_success is _DEFAULT_POSTURE_SUCCESS:
        posture_last_success = datetime.now(UTC) - timedelta(seconds=30)
    posture_timestamp = (
        posture_last_success.isoformat() if isinstance(posture_last_success, datetime) else ""
    )
    if workflow_last_success is _DEFAULT_POSTURE_SUCCESS:
        workflow_last_success = posture_last_success
    workflow_timestamp = (
        workflow_last_success.isoformat() if isinstance(workflow_last_success, datetime) else ""
    )
    bl.get_security_posture_loop_status.return_value = {
        "last_attempt_at": posture_timestamp,
        "last_success_at": posture_timestamp,
        "last_failure_reason": "",
        "consecutive_failures": 0,
    }
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": workflow_timestamp,
        "last_success_at": workflow_timestamp,
        "last_failure_reason": "",
        "consecutive_failures": 0,
    }
    worker_id_values = ["worker-1"] if worker_ids is None else list(worker_ids)
    bl.get_active_jobs.return_value = [{"worker_id": worker_id} for worker_id in worker_id_values]
    bl._table_exists.return_value = True
    cur = MagicMock()
    # WARNING for the next person: entries below are consumed BY CALL ORDER, so
    # adding a readiness check that queries the database silently steals one and
    # shifts every check after it - which reads as several unrelated failures.
    # Dispatch on the statement instead, as the machine_tokens and
    # security_posture_snapshots branches in _fetchone do.
    positional = [
        (1,),
        (pending_count,),
        (0, 0),
        (0, 0),
        (stale_leases,),
        (0,),
        (1,),
    ]
    state = {"sql": "", "index": 0}

    def _execute(sql, *args, **kwargs):
        state["sql"] = str(sql)

    def _fetchone():
        # The worker identity check reads a machine_tokens row with its own
        # shape, so dispatch on the statement instead of on call order.
        if "machine_tokens" in state["sql"]:
            return ("installer-managed-worker", False, ["projects.read"], 0, "now", True, True)
        if "security_posture_snapshots" in state["sql"]:
            # A healthy platform has completed a posture iteration recently.
            # Dispatch on the statement rather than call order: consuming a
            # positional entry here would silently shift every later check.
            return (posture_last_success,)
        index = state["index"]
        state["index"] += 1
        return positional[index] if index < len(positional) else (0,)

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = _fetchone
    bl.conn.cursor.return_value.__enter__.return_value = cur
    return bl


def _queue_delivery_summary(bl):
    return queue_delivery_runtime_queries.build_queue_delivery_summary(
        queue_delivery_runtime_queries.load_queue_delivery_readiness_counts(bl)
    )


def test_workflow_knowledge_sync_readiness_fails_after_loop_failure() -> None:
    bl = MagicMock()
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": datetime.now(UTC).isoformat(),
        "last_success_at": "",
        "last_failure_reason": "graph with oid 21229 does not exist",
        "consecutive_failures": 1,
    }

    result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is False
    assert result["consecutive_failures"] == 1
    assert "graph with oid" in result["last_failure_reason"]


def test_workflow_knowledge_sync_readiness_preserves_latest_failure_detail() -> None:
    bl = MagicMock()
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": datetime.now(UTC).isoformat(),
        "last_success_at": "",
        "last_failure_reason": "graph with oid 21229 does not exist",
        "consecutive_failures": 1,
    }

    result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is False
    assert result["consecutive_failures"] == 1
    assert "graph with oid" in result["last_failure_reason"]


def test_workflow_knowledge_sync_readiness_fails_when_success_is_stale() -> None:
    bl = MagicMock()
    stale = datetime.now(UTC) - timedelta(hours=2)
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": stale.isoformat(),
        "last_success_at": stale.isoformat(),
        "last_failure_reason": "",
        "consecutive_failures": 0,
    }

    with patch.dict(
        readiness_runtime._runtime_env,
        {
            "FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED": "1",
            "FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS": "120",
            "FS_READY_WORKFLOW_KNOWLEDGE_MISSED_INTERVALS": "4",
        },
        clear=False,
    ):
        result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is False
    assert result["age_seconds"] > result["max_age_seconds"]


def test_workflow_knowledge_sync_accepts_active_in_progress_iteration() -> None:
    bl = MagicMock()
    last_attempt = datetime.now(UTC) - timedelta(seconds=30)
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": last_attempt.isoformat(),
        "last_success_at": "",
        "last_failure_reason": "",
        "consecutive_failures": 0,
        "active_outbox_id": "outbox-active",
        "active_claim_owner": "worker-1",
        "active_claim_version": 7,
    }
    bl.get_domain_event_outbox_claim_status.return_value = {
        "active": True,
        "claim_expires_at": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
    }

    result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is True
    assert result["in_progress"] is True
    assert result["detail"] == "workflow knowledge sync iteration is in progress"
    bl.get_domain_event_outbox_claim_status.assert_called_once_with(
        outbox_id="outbox-active",
        claim_owner="worker-1",
        claim_version=7,
    )


def test_workflow_knowledge_sync_rejects_an_interrupted_latest_attempt() -> None:
    bl = MagicMock()
    stale_success = datetime.now(UTC) - timedelta(hours=2)
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": datetime.now(UTC).isoformat(),
        "last_success_at": stale_success.isoformat(),
        "last_failure_reason": "",
        "consecutive_failures": 0,
        "active_outbox_id": "outbox-stale",
        "active_claim_owner": "worker-dead",
        "active_claim_version": 3,
    }
    bl.get_domain_event_outbox_claim_status.return_value = {
        "active": False,
        "claim_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }

    result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is False
    assert "latest workflow knowledge sync iteration did not complete" in result["detail"]
    assert result["in_progress"] is False


def test_workflow_knowledge_sync_rejects_an_unreadable_attempt_timestamp() -> None:
    bl = MagicMock()
    stale_success = datetime.now(UTC) - timedelta(hours=2)
    bl.get_workflow_knowledge_sync_loop_status.return_value = {
        "last_attempt_at": "not-a-timestamp",
        "last_success_at": stale_success.isoformat(),
        "last_failure_reason": "",
        "consecutive_failures": 0,
    }

    result = readiness_runtime._workflow_knowledge_sync_check(bl)

    assert result["ok"] is False
    assert result["detail"] == "workflow knowledge sync returned an unreadable attempt timestamp"


def test_compute_readiness_reports_ready_when_checks_pass() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_conn = MagicMock()
    fake_queue_module._connect = MagicMock(return_value=fake_conn)

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is True
    assert payload["checks"]["postgres"]["ok"] is True
    assert payload["checks"]["rabbitmq"]["ok"] is True
    assert payload["checks"]["rabbitmq"]["state"] == "broker_available"
    assert payload["checks"]["worker_manager"]["worker_count"] == 1
    assert payload["checks"]["queue_delivery"]["state"] == "idle"
    fake_conn.close.assert_called_once()


def test_compute_readiness_fails_closed_when_guardrail_ownership_migration_is_missing() -> None:
    bl = _build_bl()
    bl._column_exists.side_effect = lambda table, column: (
        not (table == "registry_guardrails" and column == "account_id")
    )
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is False
    assert payload["checks"]["guardrail_ownership_schema"] == {
        "ok": False,
        "missing": ["registry_guardrails.account_id"],
        "error": ("Guardrail ownership migration is required: registry_guardrails.account_id"),
    }


def test_compute_readiness_requires_workers_when_flag_enabled() -> None:
    bl = _build_bl(worker_ids=[])
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda name, default=False: (
                True if name == "FS_READY_REQUIRE_WORKERS" else default
            ),
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is False
    assert payload["checks"]["worker_manager"]["required"] is True
    assert payload["checks"]["worker_manager"]["ok"] is False


def test_compute_readiness_accepts_intentionally_lifecycle_paused_worker() -> None:
    bl = _build_bl(worker_ids=[])
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda name, default=False: (
                True if name == "FS_READY_REQUIRE_WORKERS" else default
            ),
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
            lifecycle_pause_active_fn=lambda: True,
        )

    assert payload["ready"] is True
    assert payload["checks"]["worker_manager"]["ok"] is True
    assert payload["checks"]["worker_manager"]["state"] == "lifecycle_paused"


def test_worker_readiness_uses_live_queue_consumer_not_assigned_job_history() -> None:
    bl = _build_bl(worker_ids=["stale-worker-from-job"])

    payload = readiness_runtime._worker_manager_check(
        bl,
        env_flag_fn=lambda _name, default=False: default,
        worker_consumer_count_fn=lambda: 0,
    )

    assert payload["required"] is True
    assert payload["worker_count"] == 0
    assert payload["ok"] is False
    assert payload["id"] == "worker_manager"
    assert payload["status"] == "fail"
    assert payload["remediation"]


def test_worker_readiness_accepts_zero_consumers_during_lifecycle_owned_pause() -> None:
    payload = readiness_runtime._worker_manager_check(
        _build_bl(worker_ids=[]),
        env_flag_fn=lambda _name, default=False: default,
        worker_consumer_count_fn=lambda: 0,
        lifecycle_pause_active_fn=lambda: True,
    )

    assert payload["required"] is True
    assert payload["worker_count"] == 0
    assert payload["ok"] is True
    assert payload["state"] == "lifecycle_paused"
    assert payload["detail"] == "Worker consumption is intentionally lifecycle-paused."
    assert payload["status"] == "pass"


def test_worker_readiness_accepts_injected_public_maintenance_pause() -> None:
    payload = readiness_runtime._worker_manager_check(
        _build_bl(worker_ids=[]),
        env_flag_fn=lambda _name, default=False: default,
        worker_consumer_count_fn=lambda: 0,
        lifecycle_pause_active_fn=lambda: True,
    )

    assert payload["required"] is True
    assert payload["worker_count"] == 0
    assert payload["ok"] is True
    assert payload["state"] == "lifecycle_paused"
    assert payload["status"] == "pass"


def _identity_bl(row):
    """Cursor stub returning one machine_tokens row, as the check's query does."""
    bl = MagicMock()
    cursor = bl.conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    return bl


def test_worker_identity_fails_when_no_credential_has_ever_authenticated() -> None:
    """Outcome 3: nothing has ever authenticated, so the worker never reaches us."""
    payload = readiness_runtime._worker_identity_check(_identity_bl(None))

    assert payload["ok"] is False
    assert payload["id"] == "worker_identity"
    assert payload["status"] == "fail"
    assert "has ever authenticated" in payload["detail"]
    assert "install --repair" in payload["remediation"]


def test_worker_identity_passes_for_the_account_wide_credential_in_use() -> None:
    """Outcome 1: the credential actually being used is valid and account-wide."""
    payload = readiness_runtime._worker_identity_check(
        _identity_bl(("installer-managed-worker", False, ["projects.read"], 0, "now", True, True))
    )

    assert payload["ok"] is True
    assert payload["status"] == "pass"
    assert "installer-managed-worker" in payload["detail"]
    assert payload["observed_token"] == "installer-managed-worker"


def test_worker_identity_fails_and_names_the_cause_for_an_under_scoped_credential() -> None:
    """Outcome 2: this is the case that used to read green.

    A worker configured with a credential lacking projects.read cannot ground.
    The old check asked only whether SOME valid token existed, answered yes, and
    left worker_manager to fail with the consequence instead of the cause.
    """
    payload = readiness_runtime._worker_identity_check(
        _identity_bl(("under-scoped-probe", False, ["jobs.read"], 0, "now", True, False))
    )

    assert payload["ok"] is False
    assert payload["status"] == "fail"
    assert "under-scoped-probe" in payload["detail"], "the offending token is not named"
    assert "jobs.read" in payload["detail"], "the observed scopes are not reported"
    assert "projects.read" in payload["detail"], "the required scope is not reported"


def test_worker_identity_fails_for_a_project_bound_credential() -> None:
    payload = readiness_runtime._worker_identity_check(
        _identity_bl(("bound-worker", False, ["projects.read"], 2, "now", True, True))
    )

    assert payload["ok"] is False
    assert "bound to 2 project(s)" in payload["detail"]


def test_worker_identity_fails_for_a_revoked_credential() -> None:
    payload = readiness_runtime._worker_identity_check(
        _identity_bl(("revoked-worker", True, ["projects.read"], 0, "then", False, True))
    )

    assert payload["ok"] is False
    assert "revoked" in payload["detail"]


def test_worker_identity_does_not_fail_merely_because_the_observation_is_old() -> None:
    """Staleness must not duplicate worker_manager.

    last_used_at is the past, so a stopped or idle worker keeps a historical
    timestamp. "The worker is down" is worker_manager's diagnosis; failing here
    on age alone would merge two different causes into one red.
    """
    payload = readiness_runtime._worker_identity_check(
        _identity_bl(("installer-managed-worker", False, ["projects.read"], 0, "then", False, True))
    )

    assert payload["ok"] is True
    assert payload["used_recently"] is False
    assert "last used over" in payload["detail"]


def test_compute_readiness_fails_when_stuck_jobs_exceed_limit() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 2},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is False
    assert payload["checks"]["stuck_jobs"]["ok"] is False
    assert payload["checks"]["stuck_jobs"]["detection_count"] == 2


def test_compute_readiness_uses_lightweight_queue_delivery_counts() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
        patch(
            "core.application.jobs.queue_delivery_queries.load_queue_delivery_readiness_counts",
            return_value={
                "queued_pending_jobs": 0,
                "retrying_delivery_jobs": 1,
                "dead_lettered_jobs": 0,
                "post_claim_execution_failed_jobs": 0,
            },
        ) as readiness_counts,
        patch(
            "core.application.jobs.queue_delivery_queries.load_queue_delivery_counts",
            side_effect=AssertionError("historical queue counts should not be used for readiness"),
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["checks"]["queue_delivery"]["state"] == "retrying_delivery"
    readiness_counts.assert_called_once_with(bl)


def test_queue_delivery_intervention_is_attention_not_platform_unreadiness() -> None:
    payload = readiness_runtime._queue_delivery_check(
        MagicMock(),
        queue_delivery_summary_fn=lambda _bl: {
            "state": "intervention_required",
            "state_label": "Needs operator action",
            "state_message": "A dead-lettered job requires operator intervention.",
            "requires_intervention": True,
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 1,
            "post_claim_execution_failed_jobs": 0,
            "operator_action": "Acknowledge or restart the dead-lettered job.",
        },
    )

    assert payload["ok"] is True
    assert payload["id"] == "queue_delivery"
    assert payload["status"] == "pass"
    assert payload["detail"] == "A dead-lettered job requires operator intervention."
    assert payload["remediation"] == "Acknowledge or restart the dead-lettered job."


def test_persisted_stuck_job_blocks_readiness_without_new_watchdog_detection() -> None:
    bl = MagicMock()
    cursor = bl.conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)

    payload = readiness_runtime._stuck_jobs_check(
        bl,
        watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
    )

    assert payload["ok"] is False
    assert payload["persisted_count"] == 1
    assert payload["detection_count"] == 0
    assert payload["id"] == "stuck_jobs"
    assert payload["status"] == "fail"
    assert "1 persisted stuck job" in payload["detail"]
    assert payload["remediation"]


def test_compute_readiness_passes_scoped_workflow_knowledge_preflight() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={
                "ok": False,
                "matrix": {},
                "errors": ["Workflow knowledge index is stale."],
                "workflow_knowledge": {"scope": {"freshness": "stale"}},
            },
        ) as preflight,
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            workflow_knowledge_account_id="acct_test",
            workflow_knowledge_tenant_id="tenant_test",
            workflow_knowledge_project_id="proj_test",
            workflow_knowledge_catalog_digest="digest-now",
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is False
    assert payload["checks"]["memory_backends"]["workflow_knowledge"]["scope"] == {
        "freshness": "stale"
    }
    preflight.assert_called_once_with(
        bl=bl,
        check_connectivity=False,
        check_workflow_knowledge=True,
        workflow_knowledge_account_id="acct_test",
        workflow_knowledge_tenant_id="tenant_test",
        workflow_knowledge_project_id="proj_test",
        workflow_knowledge_catalog_digest="digest-now",
    )


def test_compute_readiness_fails_closed_on_stale_lifecycle_recovery() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
            lifecycle_journals_fn=lambda: (
                SimpleNamespace(kind="stack_rebuild", status="recovery_required"),
            ),
        )

    assert payload["ready"] is False
    assert payload["checks"]["lifecycle_recovery"] == {
        "ok": False,
        "unfinished_count": 1,
        "recovery_required_count": 1,
        "state": "recovery_required",
        "error": "",
    }


def test_a_dead_security_capability_makes_the_platform_not_ready() -> None:
    """RC7-001: the posture loop can fail forever while readiness stays green.

    Readiness must name it. This is the parity half - the check itself is
    covered in test_readiness_security_posture.py; here it must actually reach
    the aggregate verdict.
    """
    bl = _build_bl(posture_last_success=None)
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_queue_delivery_summary,
        )

    assert payload["ready"] is False
    posture = payload["checks"]["security_posture"]
    assert posture["ok"] is False
    assert posture["never_completed"] is True
    assert "SECURITY_PII_SALT" in posture["detail"]


def test_an_ordinary_failed_job_does_not_unready_a_healthy_platform() -> None:
    """RC7-005 / RA-008 at the aggregate level.

    A delivered workflow that failed for ordinary reasons must leave
    /health/ready at 200 so new work can still execute. The job stays visible -
    the queue delivery check still reports it - it simply is not a reason the
    platform cannot accept work.
    """
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    def _summary_with_failed_job(_bl):
        return queue_delivery_runtime_queries.build_queue_delivery_summary(
            {
                "queued_pending_jobs": 0,
                "retrying_delivery_jobs": 0,
                "dead_lettered_jobs": 0,
                "post_claim_execution_failed_jobs": 2,
            }
        )

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_summary_with_failed_job,
        )

    assert payload["ready"] is True
    delivery = payload["checks"]["queue_delivery"]
    assert delivery["ok"] is True
    assert delivery["post_claim_execution_failed_jobs"] == 2, "the job must stay visible"


def test_an_undeliverable_historical_job_remains_attention_without_unreadiness() -> None:
    bl = _build_bl()
    fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_broker")
    fake_queue_module._connect = MagicMock(return_value=MagicMock())

    def _summary_with_dead_letter(_bl):
        return queue_delivery_runtime_queries.build_queue_delivery_summary(
            {
                "queued_pending_jobs": 0,
                "retrying_delivery_jobs": 0,
                "dead_lettered_jobs": 1,
                "post_claim_execution_failed_jobs": 0,
            }
        )

    with (
        patch.dict(
            sys.modules, {"core.infrastructure.runtime.worker.queue_broker": fake_queue_module}
        ),
        patch(
            "core.infrastructure.runtime.memory_preflight.validate_memory_preflight",
            return_value={"ok": True, "matrix": {}, "errors": []},
        ),
    ):
        payload = readiness_runtime.compute_readiness(
            bl,
            config={},
            env_flag_fn=lambda _name, default=False: default,
            watchdog_scan_fn=lambda _bl, apply=False: {"detection_count": 0},
            queue_delivery_summary_fn=_summary_with_dead_letter,
        )

    assert payload["ready"] is True
    assert payload["checks"]["queue_delivery"]["ok"] is True
    assert payload["checks"]["queue_delivery"]["requires_intervention"] is True


def test_doctor_reports_a_missing_installation_instead_of_an_internal_error() -> None:
    """OPS-002: `flow-steward doctor` exited 8 with a generic internal_error.

    The runtime raised while resolving a database connection, before Doctor could
    say anything, so the one command whose job is to name what is missing named
    nothing. On a host with no installer configuration the answer is a finding,
    not a crash.
    """
    from core.application.readiness.contracts import ReadinessPrerequisiteMissing
    from core.application.readiness.service import CoreReadinessService

    def probe():
        raise ReadinessPrerequisiteMissing(
            "No configured database connection is available on this host.",
            remediation="Run the installer on this host.",
        )

    report = CoreReadinessService(probe=probe).check()

    assert report.status == "failed"
    assert report.core_failed == 1
    assert report.process_exit_code == 4
    finding = report.findings[0]
    assert finding.check_id == "readiness_prerequisite"
    assert "database connection" in finding.detail
    assert "installer" in finding.remediation


def test_the_prerequisite_finding_reaches_status_as_a_blocker() -> None:
    from core.application.readiness.contracts import ReadinessPrerequisiteMissing
    from core.application.readiness.service import CoreReadinessService

    def probe():
        raise ReadinessPrerequisiteMissing("Nothing is installed here.")

    report = CoreReadinessService(probe=probe).status()

    assert report.findings, "status must carry the blocker, not an empty report"
    assert report.findings[0].check_id == "readiness_prerequisite"


def test_an_ordinary_probe_failure_is_still_an_error() -> None:
    """Only a missing prerequisite becomes a finding; a broken probe is a fault."""
    import pytest

    from core.application.readiness.service import CoreReadinessService

    def probe():
        raise RuntimeError("the probe itself is broken")

    _raises_callable_810_1 = CoreReadinessService(probe=probe).check
    with pytest.raises(RuntimeError):
        _raises_callable_810_1()


# ---------------------------------------------------------------------------
# The CLI boundary
#
# The service turns a missing prerequisite into a finding, but the exception is
# also raised while resolving the installed stack's DSN — before any probe runs
# — so on a host that has never been installed it escapes past the service and
# reaches the generic mapper. There it became `internal_error` with exit 8 or
# 70: "an unexpected internal error occurred", for the one situation the
# command exists to report. Nothing was unexpected about it.
# ---------------------------------------------------------------------------


def test_a_missing_prerequisite_is_a_precondition_not_an_internal_error() -> None:
    from core.application.management.command_specs.contracts import ExitCode
    from core.application.readiness.contracts import ReadinessPrerequisiteMissing
    from core.entrypoints.cli.platform.errors import map_exception

    mapped = map_exception(
        ReadinessPrerequisiteMissing(
            "No configured database connection is available on this host.",
            remediation="Run the installer on this host.",
        )
    )

    assert mapped.exit_code == ExitCode.PRECONDITION
    assert mapped.code == "precondition_failed"


def test_the_operator_is_told_what_is_missing_and_what_to_do() -> None:
    """`internal_error` replaces the cause with a placeholder, which is why this
    path had to stop being one: the detail is the entire value of the answer."""
    from core.application.readiness.contracts import ReadinessPrerequisiteMissing
    from core.entrypoints.cli.platform.errors import map_exception

    mapped = map_exception(
        ReadinessPrerequisiteMissing(
            "No configured database connection is available on this host.",
            remediation="Run the installer on this host, or set FS_OBSERVABILITY_DSN.",
        )
    )

    assert "database connection" in mapped.cause
    assert "installer" in mapped.remediation


def test_a_read_operation_does_not_downgrade_it_to_a_stable_failure() -> None:
    """The generic mapper picks exit 8 for reads and 70 otherwise; a precondition
    is a precondition whichever kind of command hit it."""
    from core.application.management.command_specs.contracts import ExitCode, OperationType
    from core.application.readiness.contracts import ReadinessPrerequisiteMissing
    from core.entrypoints.cli.platform.errors import map_exception

    for operation_type in (OperationType.READ, OperationType.CHANGE, None):
        mapped = map_exception(
            ReadinessPrerequisiteMissing("Nothing is installed here."),
            operation_type=operation_type,
        )
        assert mapped.exit_code == ExitCode.PRECONDITION, operation_type
