"""Reusable readiness checks for health and metrics endpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime_containers import (
    current_deployment_profile,
    operator_container,
)

_TOKEN_00_00 = "+00:00"


def _lifecycle_pause_inactive() -> bool:
    return False


def _postgres_check(bl) -> dict[str, Any]:
    ok = False
    error = ""
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        ok = True
    except Exception as exc:
        error = str(exc)
    return {"ok": ok, "error": error}


def _rabbitmq_check() -> dict[str, Any]:
    ok = False
    error = ""
    state = "broker_unavailable"
    status_label = "Broker unavailable"
    status_message = "RabbitMQ transport is unavailable."
    try:
        from core.infrastructure.runtime.worker.queue_broker import _connect as mq_connect

        conn = mq_connect(retries=1, delay=0.1)
        ok = True
        state = "broker_available"
        status_label = "Broker available"
        status_message = "RabbitMQ transport is available."
        with suppress(Exception):
            conn.close()
    except Exception as exc:
        error = str(exc)
    return {
        "ok": ok,
        "error": error,
        "state": state,
        "status_label": status_label,
        "status_message": status_message,
    }


def _queue_delivery_check(bl, *, queue_delivery_summary_fn) -> dict[str, Any]:
    try:
        payload = queue_delivery_summary_fn(bl)
    except Exception as exc:
        return {
            "ok": False,
            "id": "queue_delivery",
            "category": "core",
            "title": "Queue delivery",
            "status": "fail",
            "detail": "Queue delivery observability could not be loaded.",
            "remediation": "Restore database access and inspect queue delivery state.",
            "error": str(exc),
            "state": "unknown",
            "state_label": "Queue delivery unavailable",
            "state_message": "Queue delivery observability could not be loaded.",
            "requires_intervention": False,
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 0,
            "post_claim_execution_failed_jobs": 0,
        }
    # Historical delivery failures are operator attention, not evidence that the
    # platform cannot accept new work.  Transport/database probe failures still
    # fail closed above; keep the durable attention state visible without
    # creating a second, history-dependent definition of readiness.
    requires_intervention = bool(payload.get("requires_intervention"))
    detail = str(payload.get("state_message") or "").strip()
    remediation = str(payload.get("operator_action") or "").strip()
    if requires_intervention and not remediation:
        remediation = "Resolve or acknowledge every delivery that requires operator intervention."
    return {
        "ok": True,
        "id": "queue_delivery",
        "category": "core",
        "title": "Queue delivery",
        "status": "pass",
        "detail": detail,
        "remediation": remediation,
        "error": "",
        **payload,
    }


def _worker_container() -> str:
    return operator_container("fs-worker", deployment_profile=current_deployment_profile())


def _worker_manager_check(
    bl,
    *,
    env_flag_fn,
    worker_consumer_count_fn: Callable[[], int] | None = None,
    lifecycle_pause_active_fn: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    require_workers = env_flag_fn("FS_READY_REQUIRE_WORKERS", True)
    ok = True
    worker_count = 0
    error = ""
    lifecycle_paused = False
    try:
        if worker_consumer_count_fn is None:
            try:
                from core.infrastructure.runtime.worker.queue_broker import (
                    get_ready_queue_consumer_count,
                )

                worker_consumer_count_fn = get_ready_queue_consumer_count
            except ImportError:
                worker_consumer_count_fn = None
        if worker_consumer_count_fn is not None:
            worker_count = int(worker_consumer_count_fn())
            if worker_count < 0:
                raise RuntimeError("RabbitMQ consumer count is unavailable")
        else:
            active_jobs = bl.get_active_jobs() if hasattr(bl, "get_active_jobs") else []
            worker_ids = {
                str(row.get("worker_id") or "").strip()
                for row in active_jobs
                if isinstance(row, dict)
            }
            worker_count = len({worker_id for worker_id in worker_ids if worker_id})
        lifecycle_paused = bool((lifecycle_pause_active_fn or _lifecycle_pause_inactive)())
        if require_workers:
            ok = worker_count > 0 or lifecycle_paused
    except Exception as exc:
        error = str(exc)
        if require_workers:
            ok = False
    if lifecycle_paused and worker_count == 0:
        detail = "Worker consumption is intentionally lifecycle-paused."
    elif not error:
        detail = f"{worker_count} live ready-queue consumer(s) detected."
    else:
        detail = "Live ready-queue consumer count is unavailable."
    return {
        "ok": ok,
        "id": "worker_manager",
        "category": "core",
        "title": "Worker availability",
        "status": "pass" if ok else "fail",
        "detail": detail,
        "remediation": (
            # AC17-R106: this field is in every readiness payload, pass or fail, and
            # the Compact doctor reads exactly this endpoint -- so naming a container
            # that profile does not run put the wrong instruction in front of the
            # operator on the one screen they were told to look at.
            f"Start or repair {_worker_container()} and confirm one ready-queue consumer is live."
        ),
        "required": require_workers,
        "worker_count": worker_count,
        "state": "lifecycle_paused" if lifecycle_paused else "active",
        "error": error,
    }


_WORKER_GROUNDING_SCOPES = ("projects.read", "projects:*", "read:*", "admin:all")


def _worker_identity_result(*, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "id": "worker_identity",
        "category": "core",
        "title": "Worker identity",
        "status": "pass" if ok else "fail",
        "detail": detail,
        "remediation": "Run flow-steward install --repair to rotate and activate worker identity.",
    }
    payload.update(extra)
    payload.setdefault("error", "")
    return payload


def _worker_identity_check(bl) -> dict[str, Any]:
    """Report the credential the worker is ACTUALLY using, not merely that one exists.

    Asking "does a valid account-wide token exist?" answers a question about the
    database, not about the running worker. A worker configured with a different,
    under-scoped credential left this check green while the deployment could not
    ground at all; the only red was worker_manager, which is the consequence
    rather than the cause, and nothing but the worker's own log named the reason.

    machine_tokens.last_used_at is stamped by token verification after the
    revoked/expiry/active checks but before the caller enforces scopes, so a
    credential that authenticates and is then refused for scope still leaves a
    trace. That makes the used credential observable server-side: the worker
    neither has to report on itself nor can it misreport.

    Deliberate boundaries:
      * staleness alone is never a failure. last_used_at is the past, so a
        stopped or idle worker keeps a historical timestamp. Failing on age would
        duplicate worker_manager, and "the worker is down" and "the worker uses
        the wrong credential" are different diagnoses that must not be merged.
        The window only labels the observation current or historical.
      * a bad credential fails regardless of age — the last thing that
        authenticated being under-scoped is actionable whether it was a minute or
        a day ago.
      * with several workers on one service account the newest last_used_at
        attributes to whichever authenticated last. Acceptable for the current
        single-worker topology, recorded as a known limitation.
    """
    window_minutes = max(1, int(_runtime_env.get("FS_READY_WORKER_IDENTITY_WINDOW_MINUTES", "15")))
    try:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT mt.label,
                       mt.revoked,
                       mt.scopes_jsonb,
                       jsonb_array_length(mt.allowed_project_ids_jsonb) AS project_binding_count,
                       mt.last_used_at,
                       (mt.last_used_at >= NOW() - make_interval(mins => %s)) AS used_recently,
                       mt.scopes_jsonb ?| ARRAY['projects.read','projects:*','read:*','admin:all']
                           AS has_grounding_scope
                  FROM service_accounts sa
                  JOIN machine_tokens mt
                    ON mt.service_account_id = sa.id
                   AND mt.account_id = sa.account_id
                 WHERE sa.service_account_key = 'flow-steward-worker'  -- pragma: allowlist secret - account key name in a lookup
                   AND mt.last_used_at IS NOT NULL
                 ORDER BY mt.last_used_at DESC
                 LIMIT 1
                """,
                (window_minutes,),
            )
            row = cur.fetchone()
    except Exception as exc:
        return _worker_identity_result(
            ok=False,
            detail="Could not determine which worker credential is in use.",
            error=str(exc),
        )

    if not row:
        return _worker_identity_result(
            ok=False,
            detail=(
                "No flow-steward-worker credential has ever authenticated; the worker "
                "is not reaching the platform with a machine token."
            ),
            observed_token="",
            last_used_at="",
        )

    if isinstance(row, Mapping):
        label, revoked, scopes, binding_count, last_used_at, used_recently, has_scope = (
            row.get("label"),
            row.get("revoked"),
            row.get("scopes_jsonb"),
            row.get("project_binding_count"),
            row.get("last_used_at"),
            row.get("used_recently"),
            row.get("has_grounding_scope"),
        )
    else:
        label, revoked, scopes, binding_count, last_used_at, used_recently, has_scope = row[:7]

    observed = str(label or "(unlabelled)")
    scope_list = list(scopes or []) if isinstance(scopes, (list, tuple)) else scopes
    freshness = "used recently" if used_recently else f"last used over {window_minutes}m ago"
    common = {
        "observed_token": observed,
        "observed_scopes": scope_list,
        "last_used_at": str(last_used_at or ""),
        "used_recently": bool(used_recently),
        "window_minutes": window_minutes,
    }

    if bool(revoked):
        return _worker_identity_result(
            ok=False,
            detail=(
                f"The worker credential in use ('{observed}', {freshness}) is revoked. "
                "The worker cannot ground until it is re-provisioned."
            ),
            **common,
        )
    if not bool(has_scope):
        return _worker_identity_result(
            ok=False,
            detail=(
                f"The worker credential in use ('{observed}', {freshness}) lacks a project-read "
                f"scope. It carries {scope_list!r}; one of "
                f"{list(_WORKER_GROUNDING_SCOPES)!r} is required for MCP project-read readiness."
            ),
            **common,
        )
    if int(binding_count or 0) > 0:
        return _worker_identity_result(
            ok=False,
            detail=(
                f"The worker credential in use ('{observed}', {freshness}) is bound to "
                f"{int(binding_count or 0)} project(s); the worker needs an account-wide "
                "credential so later projects are not excluded."
            ),
            **common,
        )
    return _worker_identity_result(
        ok=True,
        detail=(
            f"The worker credential in use ('{observed}', {freshness}) is account-wide "
            "and carries the project-read scope used by the MCP readiness probe."
        ),
        **common,
    )


def _workflow_knowledge_sync_check(bl) -> dict[str, Any]:
    enabled = str(_runtime_env.get("FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return {"ok": True, "enabled": False, "detail": "workflow knowledge sync is disabled"}

    status_reader = getattr(bl, "get_workflow_knowledge_sync_loop_status", None)
    if not callable(status_reader):
        return {
            "ok": False,
            "enabled": True,
            "detail": "workflow knowledge sync loop status repository port is unavailable",
        }
    try:
        loop_status = status_reader() or {}
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "detail": "workflow knowledge sync loop status could not be read",
            "error": str(exc),
        }
    last_attempt_at = str(loop_status.get("last_attempt_at") or "")
    last_success_at = str(loop_status.get("last_success_at") or "")
    consecutive_failures = max(0, int(loop_status.get("consecutive_failures") or 0))
    fields: dict[str, str | int] = {
        "last_attempt_at": last_attempt_at,
        "last_success_at": last_success_at,
        "last_failure_reason": str(loop_status.get("last_failure_reason") or "")[:512],
        "consecutive_failures": consecutive_failures,
    }
    if not last_attempt_at:
        return {
            "ok": False,
            "enabled": True,
            "never_completed": True,
            "detail": "workflow knowledge sync has not attempted an iteration",
            **fields,
        }
    if consecutive_failures > 0:
        return {
            "ok": False,
            "enabled": True,
            "detail": "the latest workflow knowledge sync iteration failed",
            **fields,
        }
    try:
        last_attempt = datetime.fromisoformat(last_attempt_at.replace("Z", _TOKEN_00_00))
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "enabled": True,
            "detail": "workflow knowledge sync returned an unreadable attempt timestamp",
            **fields,
        }
    last_success: datetime | None = None
    if last_success_at:
        try:
            last_success = datetime.fromisoformat(last_success_at.replace("Z", _TOKEN_00_00))
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "enabled": True,
                "detail": "workflow knowledge sync returned an unreadable success timestamp",
                **fields,
            }
    if last_success is None or last_attempt > last_success:
        active_outbox_id = str(loop_status.get("active_outbox_id") or "").strip()
        active_claim_owner = str(loop_status.get("active_claim_owner") or "").strip()
        active_claim_version = max(0, int(loop_status.get("active_claim_version") or 0))
        claim_reader = getattr(bl, "get_domain_event_outbox_claim_status", None)
        claim_status: dict[str, Any] = {}
        if callable(claim_reader) and active_outbox_id and active_claim_owner:
            try:
                claim_status = (
                    claim_reader(
                        outbox_id=active_outbox_id,
                        claim_owner=active_claim_owner,
                        claim_version=active_claim_version,
                    )
                    or {}
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "enabled": True,
                    "in_progress": False,
                    "detail": "workflow knowledge sync claim status could not be read",
                    "error": str(exc),
                    **fields,
                }
        if bool(claim_status.get("active")):
            return {
                "ok": True,
                "enabled": True,
                "in_progress": True,
                "claim_expires_at": str(claim_status.get("claim_expires_at") or ""),
                "detail": "workflow knowledge sync iteration is in progress",
                **fields,
            }
        return {
            "ok": False,
            "enabled": True,
            "in_progress": False,
            "detail": "the latest workflow knowledge sync iteration did not complete",
            **fields,
        }
    assert last_success is not None
    # AC17-R116: the same number the control loop compiles in, and every compose file
    # sets. It was 120 here while the loop ran on 300, so on an installation with no
    # compose -- the platform shape this profile exists for -- readiness called the
    # sync stale after 480s for a loop legitimately allowed 300s between successes.
    interval_seconds = max(
        30,
        int(_runtime_env.get("FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS", "300")),
    )
    max_age_seconds = interval_seconds * max(
        2,
        int(_runtime_env.get("FS_READY_WORKFLOW_KNOWLEDGE_MISSED_INTERVALS", "4")),
    )
    age_seconds = max(0, int((datetime.now(UTC) - last_success).total_seconds()))
    ok = age_seconds <= max_age_seconds
    return {
        "ok": ok,
        "enabled": True,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "detail": (
            "workflow knowledge sync is current"
            if ok
            else f"workflow knowledge sync is stale by {age_seconds}s (limit {max_age_seconds}s)"
        ),
        **fields,
    }


def _security_posture_check(bl) -> dict[str, Any]:
    """Name a security capability that is dead instead of letting it be silent.

    RC7-001 / RA-001: the posture loop runs in control-plane, and
    _run_generic_loop catches every exception and keeps going. When the loop
    could not even import its own module - a production salt missing from that
    service - it failed on every iteration for the life of the process while
    /health/ready stayed green. Nothing recorded that the capability had never
    worked.

    The durable evidence already exists: a successful iteration writes a row to
    security_posture_snapshots. So "has this capability ever completed, and
    recently" is answerable from the same database readiness already reads, with
    no new schema and no cross-process channel to keep alive.

    Only enforced while the capability is enabled. A deployment that has turned
    posture scanning off is not broken, and must not be reported as such.
    """
    # The same names ControlConfig reads. Consulting a parallel spelling would
    # mean a deployment that turned posture scanning off still failed readiness.
    enabled = str(_runtime_env.get("FS_SECURITY_POSTURE_ENABLED", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return {"ok": True, "enabled": False, "detail": "security posture scanning is disabled"}

    status_reader = getattr(bl, "get_security_posture_loop_status", None)
    if not callable(status_reader):
        return {
            "ok": False,
            "enabled": True,
            "detail": "security posture loop status repository port is unavailable",
        }
    try:
        loop_status = status_reader() or {}
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "detail": "security posture loop status could not be read",
            "error": str(exc),
        }
    status_fields = {
        "last_attempt_at": str(loop_status.get("last_attempt_at") or ""),
        "last_success_at": str(loop_status.get("last_success_at") or ""),
        "last_failure_reason": str(loop_status.get("last_failure_reason") or "")[:512],
        "consecutive_failures": max(0, int(loop_status.get("consecutive_failures") or 0)),
    }
    if not status_fields["last_attempt_at"]:
        return {
            "ok": False,
            "enabled": True,
            "never_completed": True,
            "detail": (
                "security posture scanning has not attempted an iteration; check the "
                "control-plane error log and that SECURITY_PII_SALT is configured"
            ),
            **status_fields,
        }
    if status_fields["consecutive_failures"] > 0:
        return {
            "ok": False,
            "enabled": True,
            "detail": "the latest security posture iteration failed",
            **status_fields,
        }

    # AC17-R116: the loop compiles 300 and every compose file sets 300. Compiled at
    # 600 here, readiness allowed twice the staleness it should have -- an outage of
    # this loop stayed invisible for 40 minutes instead of 20 on an installation
    # with no compose. Found by the guard that requires one default per variable.
    interval_seconds = max(60, int(_runtime_env.get("FS_SECURITY_POSTURE_INTERVAL_SECONDS", "300")))
    # Several intervals of slack: one slow or skipped pass is not an outage.
    max_age_seconds = interval_seconds * max(
        2, int(_runtime_env.get("FS_READY_SECURITY_POSTURE_MISSED_INTERVALS", "4"))
    )

    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT MAX(created_at) FROM security_posture_snapshots")
            row = cur.fetchone()
            newest = row[0] if row else None
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "detail": "security posture history could not be read",
            "error": str(exc),
            **status_fields,
        }

    if newest is not None and not isinstance(newest, datetime):
        # Never raise out of a readiness check: an exception here would fail the
        # whole readiness response, which is a worse outage than the capability
        # this check reports on.
        return {
            "ok": False,
            "enabled": True,
            "detail": "security posture history returned an unreadable timestamp",
            "error": f"unexpected type {type(newest).__name__}",
            **status_fields,
        }

    if newest is None:
        last_success_text = str(status_fields["last_success_at"] or "")
        if last_success_text:
            try:
                last_success = datetime.fromisoformat(last_success_text.replace("Z", _TOKEN_00_00))
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=UTC)
                age_seconds = max(0, int((datetime.now(UTC) - last_success).total_seconds()))
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "enabled": True,
                    "detail": "security posture loop returned an unreadable success timestamp",
                    **status_fields,
                }
            ok = age_seconds <= max_age_seconds
            return {
                "ok": ok,
                "enabled": True,
                "never_completed": False,
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
                "detail": (
                    "security posture iteration completed successfully; no project snapshots "
                    "were produced"
                    if ok
                    else (
                        "security posture scanning has not completed for "
                        f"{age_seconds}s (limit {max_age_seconds}s); the control-plane loop "
                        "is failing"
                    )
                ),
                **status_fields,
            }
        return {
            "ok": False,
            "enabled": True,
            "never_completed": True,
            "detail": (
                "security posture scanning has never completed a successful iteration; "
                "check that SECURITY_PII_SALT reaches the control-plane service"
            ),
            **status_fields,
        }

    age_seconds = max(0, int((datetime.now(UTC) - newest).total_seconds()))
    ok = age_seconds <= max_age_seconds
    return {
        "ok": ok,
        "enabled": True,
        "never_completed": False,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "detail": (
            ""
            if ok
            else (
                "security posture scanning has not completed for "
                f"{age_seconds}s (limit {max_age_seconds}s); the control-plane loop is failing"
            )
        ),
        **status_fields,
    }


# AC17-R164: after a restore the graph catalogue can carry graph ids that no longer name
# the graph's namespace -- a dump does not keep them, and db_restore.sh repairs them as
# its last step. A restore that died before that step left graph search broken while
# the installer and health reported ready.
_STALE_GRAPHS_SQL = (
    "SELECT name FROM ag_catalog.ag_graph "
    "WHERE graphid IS DISTINCT FROM namespace::oid ORDER BY name"
)


_GRAPH_CATALOG_TITLE = "Graph catalogue identity"


def _graph_catalog_check(bl) -> dict[str, Any]:
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ag_catalog.ag_graph') IS NOT NULL")
            row = cur.fetchone()
            if not (row[0] if row else False):
                return {"ok": True, "detail": "The graph extension is not installed."}
            cur.execute(_STALE_GRAPHS_SQL)
            stale = [str(item[0]) for item in cur.fetchall() or ()]
    except Exception as exc:
        return {"ok": False, "title": _GRAPH_CATALOG_TITLE, "error": str(exc)}
    if not stale:
        return {"ok": True, "title": _GRAPH_CATALOG_TITLE, "detail": ""}
    return {
        "ok": False,
        "title": _GRAPH_CATALOG_TITLE,
        "stale_graphs": stale,
        "detail": (
            "Graph ids no longer match their namespaces, so graph search is broken: "
            + ", ".join(stale)
            + ". A database restore did not finish its graph catalogue repair."
        ),
        "remediation": (
            "Restore the backup again with core/entrypoints/cli/scripts/db_restore.sh until "
            'it prints "[restore] done" (docs/operations/compact-scalable-deployment.md).'
        ),
    }


def _queue_lag_check(bl) -> dict[str, Any]:
    max_pending = max(0, int(_runtime_env.get("FS_READY_MAX_PENDING_JOBS", "1000")))
    ok = True
    pending_count = 0
    error = ""
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
            row = cur.fetchone()
            pending_count = int((row[0] if row else 0) or 0)
            ok = pending_count <= max_pending
    except Exception as exc:
        error = str(exc)
        ok = False
    return {
        "ok": ok,
        "pending_count": pending_count,
        "max_pending": max_pending,
        "error": error,
    }


def _list_active_project_leases(bl) -> list[dict[str, Any]]:
    lease_lister = getattr(bl, "list_project_execution_leases", None)
    if callable(lease_lister):
        try:
            rows = lease_lister(active_only=True)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            return []
    return []


def _lease_health_check(bl) -> dict[str, Any]:
    max_stale_leases = max(0, int(_runtime_env.get("FS_READY_MAX_STALE_LEASES", "0")))
    stale_threshold = max(5, int(_runtime_env.get("FS_READY_LEASE_STALE_SECONDS", "120")))
    ok = True
    stale_leases = 0
    error = ""
    try:
        active_leases = _list_active_project_leases(bl)
        if active_leases:
            now = datetime.now(UTC)
            stale_leases = sum(
                1
                for row in active_leases
                if (
                    isinstance(row.get("heartbeat_at"), datetime)
                    and int((now - row["heartbeat_at"]).total_seconds()) >= stale_threshold
                )
            )
            ok = stale_leases <= max_stale_leases
        elif bl._table_exists("project_execution_slots"):
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM project_execution_slots
                    WHERE expires_at > now()
                      AND heartbeat_at < now() - make_interval(secs => %s)
                    """,
                    (stale_threshold,),
                )
                row = cur.fetchone()
                stale_leases = int((row[0] if row else 0) or 0)
                ok = stale_leases <= max_stale_leases
    except Exception as exc:
        error = str(exc)
        ok = False
    return {
        "ok": ok,
        "stale_leases": stale_leases,
        "max_stale_leases": max_stale_leases,
        "stale_threshold_seconds": stale_threshold,
        "error": error,
    }


def _stuck_jobs_check(bl, *, watchdog_scan_fn) -> dict[str, Any]:
    max_stuck = max(0, int(_runtime_env.get("FS_READY_MAX_STUCK_JOBS", "0")))
    ok = True
    detection_count = 0
    persisted_count = 0
    error = ""
    try:
        scan = watchdog_scan_fn(bl, apply=False)
        detection_count = int(scan.get("detection_count") or 0)
        with bl.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'stuck' AND deleted_at IS NULL")
            row = cur.fetchone()
            persisted_count = int((row[0] if row else 0) or 0)
        ok = detection_count <= max_stuck and persisted_count <= max_stuck
    except Exception as exc:
        error = str(exc)
        ok = False
    detail = (
        f"{persisted_count} persisted stuck job(s); "
        f"{detection_count} additional watchdog detection(s); maximum allowed {max_stuck}."
    )
    return {
        "ok": ok,
        "id": "stuck_jobs",
        "category": "core",
        "title": "Stuck jobs",
        "status": "pass" if ok else "fail",
        "detail": detail,
        "remediation": (
            "Open each stuck job, resolve its recorded cause, then recover, cancel, or acknowledge it."
        ),
        "persisted_count": persisted_count,
        "detection_count": detection_count,
        "max_allowed": max_stuck,
        "error": error,
    }


def load_lifecycle_journals() -> Sequence[object]:
    """Load durable lifecycle mutations from the packaged runtime root."""
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    return LifecycleStateStore(Path(__file__).resolve().parents[3]).unfinished_journals()


def _lifecycle_recovery_check(
    lifecycle_journals_fn: Callable[[], Sequence[object]] | None,
) -> dict[str, Any]:
    if lifecycle_journals_fn is None:
        return {
            "ok": True,
            "unfinished_count": 0,
            "recovery_required_count": 0,
            "state": "not_configured",
            "error": "",
        }
    try:
        journals = tuple(lifecycle_journals_fn())
        statuses = [
            str(getattr(getattr(journal, "status", ""), "value", getattr(journal, "status", "")))
            for journal in journals
        ]
        recovery_required_count = statuses.count("recovery_required")
        state = "clear"
        if journals:
            state = "recovery_required" if recovery_required_count else "mutation_in_progress"
        return {
            "ok": not journals,
            "unfinished_count": len(journals),
            "recovery_required_count": recovery_required_count,
            "state": state,
            "error": "",
        }
    except Exception:
        return {
            "ok": False,
            "unfinished_count": 0,
            "recovery_required_count": 0,
            "state": "unknown",
            "error": "Lifecycle recovery state is unreadable.",
        }


def compute_readiness(
    bl,
    *,
    config: dict[str, Any],
    env_flag_fn,
    watchdog_scan_fn,
    queue_delivery_summary_fn,
    lifecycle_journals_fn: Callable[[], Sequence[object]] | None = None,
    lifecycle_pause_active_fn: Callable[[], bool] | None = None,
    workflow_knowledge_account_id: str = "",
    workflow_knowledge_tenant_id: str = "",
    workflow_knowledge_project_id: str | None = None,
    workflow_knowledge_catalog_digest: str = "",
) -> dict[str, Any]:
    _ = config
    checks: dict[str, dict[str, Any]] = {}
    ready = True

    checks["postgres"] = _postgres_check(bl)
    ready = ready and bool(checks["postgres"]["ok"])

    ownership_columns = {
        "registry_guardrails": ("account_id", "created_by", "updated_by", "deleted_by"),
        "registry_changelog": ("account_id", "actor_id", "actor_type"),
    }
    column_exists = getattr(bl, "_column_exists", None)
    missing_ownership_columns: list[str] = []
    if not callable(column_exists):
        missing_ownership_columns.append("schema_column_probe")
    else:
        try:
            for table_name, columns in ownership_columns.items():
                missing_ownership_columns.extend(
                    f"{table_name}.{column_name}"
                    for column_name in columns
                    if not bool(column_exists(table_name, column_name))
                )
        except Exception as exc:
            missing_ownership_columns = [f"schema_probe_failed:{exc}"]
    checks["guardrail_ownership_schema"] = {
        "ok": not missing_ownership_columns,
        "missing": missing_ownership_columns,
        "error": (
            "Guardrail ownership migration is required: " + ", ".join(missing_ownership_columns)
            if missing_ownership_columns
            else ""
        ),
    }
    ready = ready and bool(checks["guardrail_ownership_schema"]["ok"])

    checks["rabbitmq"] = _rabbitmq_check()
    ready = ready and bool(checks["rabbitmq"]["ok"])

    try:
        from core.infrastructure.runtime.memory_preflight import validate_memory_preflight

        memory_result = validate_memory_preflight(
            bl=bl,
            check_connectivity=False,
            check_workflow_knowledge=True,
            workflow_knowledge_account_id=workflow_knowledge_account_id,
            workflow_knowledge_tenant_id=workflow_knowledge_tenant_id,
            workflow_knowledge_project_id=workflow_knowledge_project_id,
            workflow_knowledge_catalog_digest=workflow_knowledge_catalog_digest,
        )
        checks["memory_backends"] = {
            "ok": bool(memory_result.get("ok")),
            "matrix": memory_result.get("matrix") or {},
            "error": "; ".join(memory_result.get("errors") or []),
            "workflow_knowledge": memory_result.get("workflow_knowledge") or {},
        }
    except Exception as exc:
        checks["memory_backends"] = {"ok": False, "matrix": {}, "error": str(exc)}
    ready = ready and bool(checks["memory_backends"]["ok"])

    checks["worker_manager"] = _worker_manager_check(
        bl,
        env_flag_fn=env_flag_fn,
        lifecycle_pause_active_fn=lifecycle_pause_active_fn,
    )
    ready = ready and bool(checks["worker_manager"]["ok"])

    # A required security capability that has never run is a readiness failure,
    # named rather than swallowed.
    checks["security_posture"] = _security_posture_check(bl)
    ready = ready and bool(checks["security_posture"]["ok"])

    checks["workflow_knowledge_sync"] = _workflow_knowledge_sync_check(bl)
    ready = ready and bool(checks["workflow_knowledge_sync"]["ok"])

    checks["queue_lag"] = _queue_lag_check(bl)
    ready = ready and bool(checks["queue_lag"]["ok"])

    checks["queue_delivery"] = _queue_delivery_check(
        bl, queue_delivery_summary_fn=queue_delivery_summary_fn
    )
    ready = ready and bool(checks["queue_delivery"]["ok"])

    checks["lease_health"] = _lease_health_check(bl)
    ready = ready and bool(checks["lease_health"]["ok"])

    checks["stuck_jobs"] = _stuck_jobs_check(bl, watchdog_scan_fn=watchdog_scan_fn)
    ready = ready and bool(checks["stuck_jobs"]["ok"])

    checks["worker_identity"] = _worker_identity_check(bl)
    ready = ready and bool(checks["worker_identity"]["ok"])

    checks["lifecycle_recovery"] = _lifecycle_recovery_check(lifecycle_journals_fn)
    ready = ready and bool(checks["lifecycle_recovery"]["ok"])

    checks["graph_catalog"] = _graph_catalog_check(bl)
    ready = ready and bool(checks["graph_catalog"]["ok"])

    return {"ready": ready, "checks": checks}
