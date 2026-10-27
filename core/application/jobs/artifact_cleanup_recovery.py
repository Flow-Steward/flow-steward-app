"""Bounded reconciliation and recovery of artifact cleanup and preparing submissions."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from core.application.extensions.artifact_plane_values import (
    REPLAY_CLEANUP_COMPLETION_PREFIX,
    replay_cleanup_completion_key,
)
from core.application.jobs.artifact_cleanup_runtime import (
    _MESSAGE_INVALID_CLEANUP_JOURNAL,
    JOB_DELETED_TRANSITION,
    PREPARING_TERMINALIZED_TRANSITION,
    _cleanup_plan_has_residuals,
    _parse_cleanup_journal,
    _purge_cleanup_plan,
    finalize_job_artifact_purge,
    purge_job_artifacts,
    purge_storage_keys,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.object_storage import (
    STORAGE_CLEANUP_PAGE_SIZE,
    read_storage_key_page,
    read_storage_key_page_after,
)

ARTIFACT_CLEANUP_RECONCILIATION_DEFAULT_BUDGET = 16


ARTIFACT_CLEANUP_RECONCILIATION_MAX_BUDGET = 128


_ARTIFACT_CLEANUP_RECONCILIATION_SHARDS = 256


_ARTIFACT_CLEANUP_RECONCILIATION_SHARDS_PER_RUN = 16


_ARTIFACT_CLEANUP_RECONCILIATION_CHECKPOINT_KEY = "artifact_cleanup_reconciliation_cursor"


class _DatabaseReconciliationCheckpointStore:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def load(self) -> dict[str, Any]:
        from core.infrastructure.db.system_settings_store import get_system_setting

        row = get_system_setting(
            conn=self._conn,
            setting_key=_ARTIFACT_CLEANUP_RECONCILIATION_CHECKPOINT_KEY,
        )
        value = row.get("setting_value_jsonb") if isinstance(row, dict) else None
        return dict(value) if isinstance(value, dict) else {}

    def save(self, checkpoint: dict[str, Any]) -> None:
        from core.infrastructure.db.system_settings_store import save_system_setting

        save_system_setting(
            conn=self._conn,
            setting_key=_ARTIFACT_CLEANUP_RECONCILIATION_CHECKPOINT_KEY,
            value=checkpoint,
            updated_by="artifact_cleanup_reconciler",
        )


def _reconciliation_budget(value: int | None) -> int:
    raw = value if value is not None else ARTIFACT_CLEANUP_RECONCILIATION_DEFAULT_BUDGET
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = ARTIFACT_CLEANUP_RECONCILIATION_DEFAULT_BUDGET
    return max(1, min(ARTIFACT_CLEANUP_RECONCILIATION_MAX_BUDGET, parsed))


def _normalized_reconciliation_checkpoint(raw: Any) -> dict[str, Any]:
    checkpoint = raw if isinstance(raw, Mapping) else {}
    try:
        version = int(checkpoint.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    raw_next_shard = checkpoint.get("next_shard") if version >= 2 else checkpoint.get("shard")
    try:
        next_shard = int(str(raw_next_shard or 0))
    except (TypeError, ValueError):
        next_shard = 0
    if not 0 <= next_shard < _ARTIFACT_CLEANUP_RECONCILIATION_SHARDS:
        next_shard = 0

    raw_after_keys: Mapping[str, Any]
    if version >= 2 and isinstance(checkpoint.get("after_keys"), Mapping):
        raw_after_keys = checkpoint["after_keys"]
    else:
        legacy_after_key = str(checkpoint.get("after_key") or "").strip()
        raw_after_keys = {f"{next_shard:02x}": legacy_after_key} if legacy_after_key else {}
    after_keys: dict[str, str] = {}
    for raw_shard, raw_after_key in raw_after_keys.items():
        shard_token = str(raw_shard or "").strip().lower()
        if len(shard_token) != 2:
            continue
        try:
            shard = int(shard_token, 16)
        except ValueError:
            continue
        after_key = str(raw_after_key or "").strip()
        shard_prefix = f"{REPLAY_CLEANUP_COMPLETION_PREFIX}{shard:02x}/"
        if after_key.startswith(shard_prefix):
            after_keys[f"{shard:02x}"] = after_key
    return {"version": 2, "next_shard": next_shard, "after_keys": after_keys}


def _select_reconciliation_marker_keys(
    storage: Any,
    *,
    checkpoint: Mapping[str, Any],
    budget: int,
) -> tuple[list[str], dict[str, Any], int]:
    shard = int(checkpoint["next_shard"])
    after_keys = dict(checkpoint.get("after_keys") or {})
    selected: list[str] = []
    scan_failures = 0
    visited_shards = 0
    while (
        len(selected) < budget and visited_shards < _ARTIFACT_CLEANUP_RECONCILIATION_SHARDS_PER_RUN
    ):
        shard_prefix = f"{REPLAY_CLEANUP_COMPLETION_PREFIX}{shard:02x}/"
        shard_token = f"{shard:02x}"
        after_key = str(after_keys.get(shard_token) or "")
        remaining = budget - len(selected)
        try:
            candidates, next_key = read_storage_key_page_after(
                storage,
                shard_prefix,
                limit=remaining,
                start_after=after_key or None,
            )
        except Exception:
            candidates = []
            next_key = None
            scan_failures += 1
        else:
            if next_key is None:
                after_keys.pop(shard_token, None)
            else:
                after_keys[shard_token] = next_key
        selected.extend(candidates)
        visited_shards += 1
        shard = (shard + 1) % _ARTIFACT_CLEANUP_RECONCILIATION_SHARDS
    return (
        selected,
        {"version": 2, "next_shard": shard, "after_keys": after_keys},
        scan_failures,
    )


def reconcile_job_artifact_purge_markers(
    bl: Any,
    *,
    storage: Any,
    budget: int | None = None,
    checkpoint_store: Any | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Reconcile a bounded journal shard after canonical DB transition proof."""
    result = {"scanned": 0, "reconciled": 0, "preserved": 0, "failed": 0}
    get_fn = getattr(storage, "get", None)
    get_job = getattr(bl, "get_job", None)
    if not callable(get_fn) or not callable(get_job):
        result["failed"] += 1
        return result
    store = checkpoint_store or _DatabaseReconciliationCheckpointStore(getattr(bl, "conn", None))
    try:
        checkpoint = _normalized_reconciliation_checkpoint(store.load())
    except Exception:
        result["failed"] += 1
        return result
    marker_keys, next_checkpoint, scan_failures = _select_reconciliation_marker_keys(
        storage,
        checkpoint=checkpoint,
        budget=_reconciliation_budget(budget),
    )
    result["failed"] += scan_failures
    for marker_key in marker_keys:
        result["scanned"] += 1
        try:
            raw = get_fn(marker_key)
            marker = json.loads(raw) if raw else None
            if not isinstance(marker, dict):
                raise ValueError(_MESSAGE_INVALID_CLEANUP_JOURNAL)
            project_id = str(marker.get("project_id") or "").strip()
            job_id = str(marker.get("job_id") or "").strip()
            fingerprint = str(marker.get("authority_fingerprint") or "").strip()
            transition = str(marker.get("db_transition") or JOB_DELETED_TRANSITION).strip()
            parsed = _parse_cleanup_journal(
                raw,
                project_id=project_id,
                job_id=job_id,
                fingerprint=fingerprint,
                db_transition=transition,
            )
            if parsed is None or marker_key != replay_cleanup_completion_key(project_id, job_id):
                raise ValueError(_MESSAGE_INVALID_CLEANUP_JOURNAL)
            if parsed.get("state") == "committed":
                current_time = float(time.time() if now is None else now)
                if current_time < float(parsed["remove_after"]):
                    result["preserved"] += 1
                    continue
                purge_result = _purge_cleanup_plan(storage, parsed["objects"])
                if purge_result["failed"] or _cleanup_plan_has_residuals(
                    storage, parsed["objects"]
                ):
                    result["failed"] += 1
                    continue
                if purge_storage_keys(storage, [marker_key])["failed"] == 0:
                    result["reconciled"] += 1
                else:
                    result["failed"] += 1
                continue
            if parsed.get("state") != "complete" or int(parsed.get("version") or 0) == 1:
                raise ValueError(_MESSAGE_INVALID_CLEANUP_JOURNAL)
            job = get_job(job_id)
            transition_committed = job is None
            if job is not None:
                meta = job.get("meta") if isinstance(job, Mapping) else None
                if isinstance(meta, str):
                    meta = json.loads(meta)
                transition_committed = bool(
                    transition == PREPARING_TERMINALIZED_TRANSITION
                    and isinstance(job, Mapping)
                    and str(job.get("project_id") or "").strip() == project_id
                    and str(job.get("status") or "").strip() == "failed"
                    and not (
                        isinstance(meta, dict) and meta.get("attachment_persistence_artifacts")
                    )
                )
            if not transition_committed:
                result["preserved"] += 1
                continue
            if finalize_job_artifact_purge(
                {"id": job_id, "project_id": project_id},
                storage=storage,
                now=now,
            ):
                result["reconciled"] += 1
            else:
                result["failed"] += 1
        except Exception:
            result["failed"] += 1
    try:
        store.save(next_checkpoint)
    except Exception:
        result["failed"] += 1
    return result


def recover_expired_preparing_submissions(
    bl: Any,
    *,
    storage: Any,
    limit: int = 100,
    outbox_repository: Any | None = None,
) -> dict[str, int]:
    """Clean and terminalize uploads that never reached atomic finalization."""
    from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository

    repository = outbox_repository or DispatchOutboxRepository(bl.conn)
    rows = repository.claim_expired_preparing(limit=limit, lease_seconds=60)
    result = {"claimed": len(rows or []), "recovered": 0, "cleanup_failed": 0, "superseded": 0}
    for claimed in rows or []:
        job_id = str((claimed or {}).get("job_id") or "").strip()
        owner_token = str((claimed or {}).get("owner_token") or "").strip()
        if not owner_token or not repository.verify_cleanup_owner(
            job_id=job_id, owner_token=owner_token
        ):
            result["superseded"] += 1
            continue
        job = bl.get_job(job_id) if job_id and hasattr(bl, "get_job") else None
        if not isinstance(job, dict):
            job = {
                "id": job_id,
                "status": "pending",
                "worker_id": None,
                "meta": (claimed or {}).get("meta") or {},
            }
        if str(job.get("status") or "") != "pending" or str(job.get("worker_id") or "").strip():
            repository.settle_cleanup(
                job_id=job_id,
                owner_token=owner_token,
                status="superseded",
                last_error="execution_advanced",
            )
            result["superseded"] += 1
            continue
        cleanup_stats = purge_job_artifacts(
            job,
            storage=storage,
            db_transition=PREPARING_TERMINALIZED_TRANSITION,
        )
        if cleanup_stats["failed"]:
            result["cleanup_failed"] += 1
            continue

        latest_meta = job.get("meta") or {}
        if isinstance(latest_meta, str):
            try:
                latest_meta = json.loads(latest_meta)
            except Exception:
                latest_meta = {}
        latest_meta = dict(latest_meta) if isinstance(latest_meta, dict) else {}
        latest_meta.pop("attachment_persistence_artifacts", None)
        identity = latest_meta.pop("idempotency", None)
        if isinstance(identity, dict):
            latest_meta["abandoned_idempotency"] = {
                **identity,
                "reason": "submission_preparation_timeout",
            }

        from core.application.jobs.job_lifecycle_uow import JobLifecycleUnitOfWork
        from core.infrastructure.db.db_pool import transaction_connection

        unit_of_work_factory = getattr(bl, "job_lifecycle_unit_of_work", None)
        if callable(unit_of_work_factory):
            transaction = unit_of_work_factory().transaction()
        elif getattr(bl, "conn", None) is not None:
            transaction = JobLifecycleUnitOfWork(
                lambda connection=bl.conn, transaction_factory=transaction_connection: (
                    transaction_factory(connection)
                )
            ).transaction()
        else:
            transaction = nullcontext()
        from core.application.jobs.terminal_persistence import (
            TerminalPersistenceConflict,
            TerminalPersistenceService,
        )

        try:
            with transaction as transaction_conn:
                transaction_repository = (
                    repository
                    if outbox_repository is not None
                    else DispatchOutboxRepository(transaction_conn or bl.conn)
                )
                if not transaction_repository.verify_cleanup_owner(
                    job_id=job_id, owner_token=owner_token
                ):
                    raise TerminalPersistenceConflict("cleanup lease lost")
                # Fail the canonical claim when the stored identity is complete.
                if isinstance(identity, dict) and all(
                    str(identity.get(key) or "").strip()
                    for key in ("scope_key", "key_digest", "request_id", "request_fingerprint")
                ):
                    from core.application.idempotency.repository import (
                        RequestIdempotencyRepository,
                    )

                    RequestIdempotencyRepository(transaction_conn or bl.conn).fail(
                        scope_key=str(identity["scope_key"]),
                        key_digest=str(identity["key_digest"]),
                        request_id=str(identity["request_id"]),
                        request_fingerprint=str(identity["request_fingerprint"]),
                        error_code="submission_preparation_timeout",
                    )
                bl.update_job(job_id, meta=latest_meta)
                TerminalPersistenceService(bl).complete_failure(
                    job_id=job_id,
                    status="failed",
                    error_message="Submission payload preparation timed out",
                    error_code="submission_preparation_timeout",
                    category="submission",
                    require_pending_unowned=True,
                )
                if not transaction_repository.settle_cleanup(
                    job_id=job_id,
                    owner_token=owner_token,
                    status="failed",
                    last_error="submission_preparation_timeout",
                ):
                    raise TerminalPersistenceConflict("cleanup lease lost")
        except TerminalPersistenceConflict:
            try:
                repository.settle_cleanup(
                    job_id=job_id,
                    owner_token=owner_token,
                    status="superseded",
                    last_error="terminal_state_advanced",
                )
            except Exception:
                result["cleanup_failed"] += 1
                continue
            result["superseded"] += 1
            continue
        except Exception:
            result["cleanup_failed"] += 1
            continue
        finalize_job_artifact_purge(job, storage=storage)
        result["recovered"] += 1
    return result


def orphan_scan(
    storage: Any,
    *,
    known_keys: set[str] | None = None,
    prefix: str = "",
    limit: int = STORAGE_CLEANUP_PAGE_SIZE,
    continuation_token: str | None = None,
) -> dict[str, Any]:
    """Best-effort bounded page scan for keys outside the known-keys set.

    Returns a dict with at least ``orphan_keys`` (list) and ``orphan_count``
    (int). Continuation follows the adapter contract; LocalFS tokens are
    process-local and must not be persisted across adapter restarts. Never
    raises — returns a partial result on storage errors.
    """
    known = known_keys or set()
    try:
        keys, next_token = read_storage_key_page(
            storage,
            prefix,
            limit=limit,
            continuation_token=continuation_token,
        )
    except Exception as exc:
        return {
            "orphan_keys": [],
            "orphan_count": 0,
            "continuation_token": continuation_token,
            "error": str(exc),
        }
    orphans = [key for key in keys if key not in known]
    return {
        "orphan_keys": orphans,
        "orphan_count": len(orphans),
        "continuation_token": next_token,
    }


def resolve_artifact_storage_adapter_from_env(
    *, environ: Mapping[str, str] | None = None
) -> Any | None:
    """Build object storage adapter from ARTIFACT_* env vars."""
    env = dict(environ or _runtime_env)
    # AC17-R128: the same backend the storage factory resolves; cleanup that
    # assumes a different one sweeps somewhere nothing was written.
    backend = str(env.get("ARTIFACT_STORAGE_BACKEND") or "local_fs").strip().lower()
    key_prefix = str(
        env.get("ARTIFACT_KEY_PREFIX") or env.get("ARTIFACT_S3_KEY_PREFIX") or ""
    ).strip()
    config: dict[str, Any] = {"backend": backend, "key_prefix": key_prefix}
    if backend == "s3":
        config.update(
            {
                "endpoint_url": env.get("ARTIFACT_S3_ENDPOINT_URL", ""),
                # AC17-R129: the same bucket the storage factory creates, which a
                # live install confirms is the one objects are written to. Sweeping a
                # different name than the runtime fills is work against nothing.
                "bucket": env.get("ARTIFACT_S3_BUCKET", "flow-steward-artifacts"),
                "access_key": env.get("ARTIFACT_S3_ACCESS_KEY", ""),
                "secret_key": env.get("ARTIFACT_S3_SECRET_KEY", ""),
                "region": env.get("ARTIFACT_S3_REGION", "us-east-1"),
                "use_ssl": env.get("ARTIFACT_S3_USE_SSL", "true").lower()
                not in ("0", "false", "no"),
                "presign_ttl_seconds": int(env.get("ARTIFACT_S3_PRESIGN_TTL_SECONDS") or 600),
            }
        )
    elif backend == "local_fs":
        config["root_dir"] = env.get("ARTIFACT_LOCAL_ROOT_DIR", "/data/artifacts")

    from core.application.extensions.artifact_storage import create_storage_adapter

    return create_storage_adapter(config)
