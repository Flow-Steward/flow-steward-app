"""The definition a run executes: the pinned archive row, never the live registry.

A checkpoint carries only ``workflow_definition_ref`` ({snapshot_id, revision,
digest}); the definition itself is read from ``workflow_definition_snapshots``
through the job and kept in a small per-process cache keyed by snapshot id.
Every load re-checks the archived revision and digest against the ref, and the
digest against the archived content: a mismatch is an integrity error and the
run stops, there is no fallback to the current workflow.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, FrozenDefinition
from core.application.workflows.workflow_definition_snapshot import definition_digest

WORKFLOW_DEFINITION_REF_KEY = "workflow_definition_ref"
RUNTIME_SCHEMA_VERSION_KEY = "runtime_schema_version"

_CACHE_SIZE = 64

SnapshotLoader = Callable[[str], Mapping[str, Any] | None]


class RunDefinitionIntegrityError(RuntimeError):
    """The run's pinned definition is missing or does not match its ref."""


class IncompatibleRuntimeStateError(RuntimeError):
    """The checkpoint was written by a runtime this version cannot continue."""


_lock = threading.Lock()
#: snapshot id -> (revision, digest, definition)
_cache: OrderedDict[str, tuple[int, str, FrozenDefinition]] = OrderedDict()


def definition_ref_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """The checkpoint's ref, after refusing a state this runtime cannot continue."""
    version = state.get(RUNTIME_SCHEMA_VERSION_KEY)
    if version != RUNTIME_SCHEMA_VERSION:
        raise IncompatibleRuntimeStateError(
            f"Run state version {version!r} cannot be continued by runtime version "
            f"{RUNTIME_SCHEMA_VERSION}; start a new run."
        )
    ref = state.get(WORKFLOW_DEFINITION_REF_KEY)
    if not isinstance(ref, Mapping) or not all(
        ref.get(key) not in (None, "") for key in ("snapshot_id", "revision", "digest")
    ):
        raise RunDefinitionIntegrityError("The run state has no pinned workflow definition")
    return {
        "snapshot_id": str(ref["snapshot_id"]),
        "revision": int(ref["revision"]),
        "digest": str(ref["digest"]),
    }


def _verified(ref: Mapping[str, Any], row: Mapping[str, Any] | None) -> FrozenDefinition:
    if row is None:
        raise RunDefinitionIntegrityError(
            f"Pinned workflow definition {ref['snapshot_id']} is not available to this run"
        )
    definition = row.get("definition_jsonb")
    if not isinstance(definition, Mapping):
        raise RunDefinitionIntegrityError(
            f"Pinned workflow definition {ref['snapshot_id']} has no content"
        )
    if (
        str(row.get("id") or ref["snapshot_id"]) != ref["snapshot_id"]
        or int(row.get("revision") or 0) != ref["revision"]
        or str(row.get("digest") or "") != ref["digest"]
        or definition_digest(dict(definition)) != ref["digest"]
    ):
        raise RunDefinitionIntegrityError(
            f"Pinned workflow definition {ref['snapshot_id']} does not match the run's ref"
        )
    return FrozenDefinition(definition)


def _remember(ref: Mapping[str, Any], definition: FrozenDefinition) -> None:
    snapshot_id = str(ref["snapshot_id"])
    with _lock:
        _cache[snapshot_id] = (int(ref["revision"]), str(ref["digest"]), definition)
        _cache.move_to_end(snapshot_id)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def prime_run_definition(ref: Mapping[str, Any], definition: Mapping[str, Any]) -> None:
    """Make a definition available to an in-process run that has no job row.

    The definition is checked against the ref exactly as an archive row is.
    """
    checked = {
        "snapshot_id": str(ref.get("snapshot_id") or ""),
        "revision": int(ref.get("revision") or 0),
        "digest": str(ref.get("digest") or ""),
    }
    frozen = _verified(
        checked,
        {
            "id": checked["snapshot_id"],
            "revision": checked["revision"],
            "digest": checked["digest"],
            "definition_jsonb": definition,
        },
    )
    _remember(checked, frozen)


def _platform_dsn() -> str:
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn

    configured_dsn = CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn")
    dsn = resolve_observability_dsn(str(configured_dsn) if configured_dsn is not None else "")
    if not dsn:
        raise RunDefinitionIntegrityError(
            "The pinned workflow definition requires the platform DSN"
        )
    return dsn


def _default_loader(job_id: str) -> Mapping[str, Any] | None:
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.infrastructure.db import psycopg_runtime

    with psycopg_runtime.connect(_platform_dsn()) as conn:
        return WorkflowDefinitionSnapshotRepository(conn).load_for_job(job_id)


PinFunction = Callable[[str, str], Mapping[str, Any]]


def _default_pin(job_id: str, workflow_id: str) -> Mapping[str, Any]:
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.infrastructure.db import psycopg_runtime

    # One transaction: the job row lock, the archive row and the job's
    # reference commit together or not at all.
    with psycopg_runtime.connect(_platform_dsn()) as conn:
        return WorkflowDefinitionSnapshotRepository(conn).ensure_pinned(
            job_id=job_id, workflow_id=workflow_id
        )


def ensure_run_definition(
    state: Mapping[str, Any],
    *,
    pin: PinFunction | None = None,
    loader: SnapshotLoader | None = None,
) -> tuple[FrozenDefinition, dict[str, Any]]:
    """The run's definition, pinning it first when the run has no ref yet.

    Returns the definition and the state update that records the ref. A state
    written by the phase runtime is refused instead of being re-planned.
    """
    if state.get(WORKFLOW_DEFINITION_REF_KEY) or state.get(RUNTIME_SCHEMA_VERSION_KEY) is not None:
        return load_run_definition(state, loader=loader), {}
    if state.get("phase_stack"):
        raise IncompatibleRuntimeStateError(
            "This run was started by the phase runtime and cannot be continued; start a new run."
        )
    job_id = str(state.get("job_id") or "").strip()
    if not job_id:
        raise RunDefinitionIntegrityError("A workflow run without a job has no pinned definition")
    ref = dict((pin or _default_pin)(job_id, str(state.get("workflow_id") or "").strip()))
    update = {RUNTIME_SCHEMA_VERSION_KEY: RUNTIME_SCHEMA_VERSION, WORKFLOW_DEFINITION_REF_KEY: ref}
    return load_run_definition({**state, **update}, loader=loader), update


def load_run_definition(
    state: Mapping[str, Any],
    *,
    loader: SnapshotLoader | None = None,
) -> FrozenDefinition:
    """The frozen definition this run executes, verified against its ref."""
    ref = definition_ref_from_state(state)
    with _lock:
        cached = _cache.get(ref["snapshot_id"])
    if cached is not None:
        revision, digest, frozen = cached
        if (revision, digest) != (ref["revision"], ref["digest"]):
            raise RunDefinitionIntegrityError(
                f"Pinned workflow definition {ref['snapshot_id']} does not match the run's ref"
            )
        return frozen
    job_id = str(state.get("job_id") or "").strip()
    if not job_id:
        raise RunDefinitionIntegrityError(
            f"Pinned workflow definition {ref['snapshot_id']} cannot be loaded without its job"
        )
    frozen = _verified(ref, (loader or _default_loader)(job_id))
    _remember(ref, frozen)
    return frozen


def clear_run_definition_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = [
    "RUNTIME_SCHEMA_VERSION_KEY",
    "WORKFLOW_DEFINITION_REF_KEY",
    "IncompatibleRuntimeStateError",
    "RunDefinitionIntegrityError",
    "clear_run_definition_cache",
    "definition_ref_from_state",
    "ensure_run_definition",
    "load_run_definition",
    "prime_run_definition",
]
