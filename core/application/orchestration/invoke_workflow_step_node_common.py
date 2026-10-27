# mypy: ignore-errors
"""Shared helpers for invoke_workflow_step_node."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from core.application.orchestration.helpers import to_bool as _to_bool
from core.application.orchestration.helpers import to_dict as _to_dict
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

TERMINAL_CHILD_STATUSES = {"completed", "failed", "cancelled", "max_iterations"}
STATE_TARGET_PREFIX = "state."
INVOKE_WORKFLOW_ANCESTRY_KEY = "invoke_workflow_ancestry"
#: Launch-metadata mark of an invocation made from a parallel lane, here or by
#: an ancestor: such children and all their descendants stay on the finite path.
INVOKE_LANE_PLACEMENT_KEY = "invoke_lane_placement"
_CHILD_POLICY_CONTEXT_KEYS = ("project_policy",)
_SENSITIVE_RUNTIME_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "private_key",
    "dsn",
)


def _coerce_positive_int_env(name: str, default: int) -> int:
    raw = str(_runtime_env.get(name, str(default)) or "").strip()
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _resolve_invoke_child_timeout_seconds() -> int:
    requested = _coerce_positive_int_env("FS_INVOKE_CHILD_TIMEOUT_SECONDS", 5400)
    timeout_seconds = max(30, requested)
    timeout_cap = max(
        30,
        _coerce_positive_int_env("FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS", 7200),
    )
    return min(timeout_seconds, timeout_cap)


def _resolve_invoke_child_poll_seconds() -> int:
    return max(1, _coerce_positive_int_env("FS_INVOKE_CHILD_POLL_SECONDS", 2))


def _resolve_invoke_workflow_max_depth() -> int:
    """Maximum number of nested workflow invocations allowed in one chain."""
    return max(1, _coerce_positive_int_env("FS_INVOKE_WORKFLOW_MAX_DEPTH", 5))


def _invoke_workflow_depth_exceeded(ancestry: list[str] | None) -> bool:
    """Bound the invocation chain so an unresolved cycle cannot run unbounded.

    The recursion check only rejects an id that is already in the chain. A chain
    that keeps producing new ids still consumes a worker slot and a database
    connection per level, so the depth is bounded as well.
    """
    depth = len(ancestry or [])
    return depth > _resolve_invoke_workflow_max_depth()


def _resolve_child_workflow_row(
    *,
    workflow_registry: Any,
    db: Any,
    child_workflow_id: str,
) -> Any:
    """Resolve the child workflow from the registry projection or the platform table.

    Extension-owned children are namespaced by the projection, so the id recorded on
    the step already carries its owning install. Returning ``None`` here means the id
    resolves to nothing at all, which is a different failure from a workflow that
    exists but is not callable — the caller reports them separately.
    """
    normalized = str(child_workflow_id or "").strip()
    if not normalized:
        return None
    getter = getattr(workflow_registry, "get_workflow", None)
    projected = getter(normalized) if callable(getter) else None
    if projected is not None:
        return projected
    db_getter = getattr(db, "get_workflow", None)
    row = db_getter(normalized) if callable(db_getter) else None
    return row if isinstance(row, dict) else None


def _resolve_invoked_child_snapshot(bl: Any, child_job_id: str, child_workflow_id: str) -> dict:
    """Read the callable contract of the already-created child, never live settings."""
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.orchestration.run_definition import RunDefinitionIntegrityError
    from core.application.workflows.workflow_definition_snapshot import definition_digest

    row = WorkflowDefinitionSnapshotRepository(getattr(bl, "conn", None)).load_for_job(child_job_id)
    definition = row.get("definition_jsonb") if isinstance(row, dict) else None
    if (
        not isinstance(definition, dict)
        or str(row.get("workflow_id") or "") != child_workflow_id
        or definition_digest(definition) != row.get("digest")
    ):
        raise RunDefinitionIntegrityError(
            f"Pinned definition of invoked child '{child_job_id}' is unavailable or invalid"
        )
    return {
        "workflow_id": child_workflow_id,
        "revision": row["revision"],
        "settings": dict(definition.get("settings") or {}),
        # The files the child returns are its steps' settings, pinned with it.
        "steps": list(definition.get("steps") or []),
    }


def _cancel_child_job_on_timeout(
    bl,
    child_job_id: Any,
    *,
    timeout_seconds: int,
    child_workflow_id: str = "",
) -> bool:
    if not _to_bool(_runtime_env.get("FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT", "1"), True):
        return False
    if not hasattr(bl, "get_job"):
        return False

    with contextlib.suppress(Exception):
        latest = bl.get_job(str(child_job_id))
        if isinstance(latest, dict):
            latest_status = _parse_status(latest.get("status"))
            if latest_status in TERMINAL_CHILD_STATUSES:
                return False

    reason = f"invoke_workflow timeout after {int(timeout_seconds)}s" + (
        f" for child workflow '{child_workflow_id}'" if child_workflow_id else ""
    )
    try:
        from core.application.jobs.terminal_persistence import TerminalPersistenceService

        TerminalPersistenceService(bl).complete_failure(
            job_id=str(child_job_id),
            status="cancelled",
            error_message=reason[:2000],
            error_code="invoke_workflow_timeout",
            category="cancellation",
        )
        return True
    except Exception:
        return False


def _extract_invoke_spec(step: dict | None) -> dict | None:
    if not isinstance(step, dict):
        return None
    override = _to_dict(step.get("override"))
    spec = override.get("invoke_workflow")
    return dict(spec) if isinstance(spec, dict) else None


def _extract_callable_contract(settings: Any) -> tuple[bool, list[dict], list[dict]]:
    from core.application.registry.workflow_contract import WorkflowInterface

    interface = WorkflowInterface.from_settings(settings)
    return (
        bool(interface.enabled),
        [row.dump() for row in interface.expected_inputs],
        [row.dump() for row in interface.expected_outputs],
    )


def _resolve_invoke_input(state: OrchestratorState, selector: str, key: str) -> tuple[Any, str]:
    source = str(selector or "").strip()
    if source.startswith("literal:"):
        raw = source[len("literal:") :].strip()
        if not raw:
            return "", ""
        try:
            return json.loads(raw), ""
        except Exception:
            return raw, ""

    from core.application.orchestration.expected_input_sources import (
        resolve_expected_input_source as _resolve_expected_input_source,
    )

    value, source_error = _resolve_expected_input_source(
        dict(state),
        source,
        f"invoke.input_mapping.{key}",
    )
    if source_error:
        return None, source_error
    return value, ""


def _get_logger():
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_stores import PlatformJobStore, resolve_observability_dsn

    configured_dsn = CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn")
    dsn = resolve_observability_dsn(str(configured_dsn) if configured_dsn is not None else "")
    if not dsn:
        raise RuntimeError("invoke_workflow requires platform observability DSN")
    return PlatformJobStore(dsn)


def _parse_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _extract_envelope(row: dict) -> dict:
    envelope = row.get("envelope")
    if isinstance(envelope, dict):
        return envelope
    if isinstance(envelope, str):
        try:
            parsed = json.loads(envelope)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _coerce_positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _child_input_fingerprint(child_inputs: dict[str, Any]) -> str:
    try:
        payload = json.dumps(child_inputs or {}, sort_keys=True, default=str, ensure_ascii=True)
    except Exception:
        payload = repr(child_inputs or {})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _retry_feedback_fingerprint(feedback: str) -> str:
    normalized = str(feedback or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _invocation_key(parent_job_id: Any, step_execution_id: Any) -> str:
    """One child invocation per logical entry of the parent invoke step.

    Retries of the same entry keep the step execution id and so find the same
    child; a rework or any new entry of the step is a new invocation.
    """
    return f"{str(parent_job_id or '').strip()}:{str(step_execution_id or '').strip()}"


def _find_existing_child_invocation(
    parent_job: dict[str, Any] | None,
    *,
    invocation_key: str,
) -> str:
    """Return the child job already launched for this invocation, if any."""
    if not isinstance(parent_job, dict) or not str(invocation_key or "").strip():
        return ""
    meta = _json_object(parent_job.get("meta"))
    invoke_children = meta.get("invoke_children")
    if not isinstance(invoke_children, dict):
        return ""

    matches: list[tuple[str, str]] = []
    for raw_child_id, raw_payload in invoke_children.items():
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        if str(payload.get("invocation_key") or "").strip() != invocation_key:
            continue
        child_id = str(payload.get("child_job_id") or raw_child_id or "").strip()
        if child_id:
            matches.append((str(payload.get("created_at") or "").strip(), child_id))
    if not matches:
        return ""
    return max(matches, key=lambda item: (item[0], item[1]))[1]


def _append_workflow_ancestry_item(ancestry: list[str], value: Any) -> None:
    workflow_id = str(value or "").strip()
    if workflow_id and workflow_id not in ancestry:
        ancestry.append(workflow_id)


def _workflow_invocation_ancestry(state: OrchestratorState) -> list[str]:
    ancestry: list[str] = []
    raw_ancestry = state.get(INVOKE_WORKFLOW_ANCESTRY_KEY)
    if isinstance(raw_ancestry, (list, tuple)):
        for item in raw_ancestry:
            _append_workflow_ancestry_item(ancestry, item)
    elif isinstance(raw_ancestry, str):
        with contextlib.suppress(Exception):
            parsed = json.loads(raw_ancestry)
            if isinstance(parsed, list):
                for item in parsed:
                    _append_workflow_ancestry_item(ancestry, item)

    _append_workflow_ancestry_item(ancestry, state.get("invoke_parent_workflow_id"))
    _append_workflow_ancestry_item(ancestry, state.get("parent_workflow_id"))
    _append_workflow_ancestry_item(ancestry, state.get("workflow_id"))
    return ancestry


def _parent_meta(parent_job: Any) -> dict[str, Any]:
    """The committed metadata of the parent job row, decoded."""
    raw = parent_job.get("meta") if isinstance(parent_job, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _lane_child_wait_issue(db: Any, child_workflow_id: str, *, ancestry: list[str]) -> str:
    """Why a child may not run from a parallel lane, or ``""``.

    Placement rules forbid authored Wait/Approval anywhere in the invocation
    chain below a lane. Validation checked the chain when the parent was saved;
    this checks it again against the child definitions a run launches now, so
    a child changed since cannot slip a pause into a lane.
    """
    from core.application.workflows.workflow_definition_service import definition_from_workflow
    from core.application.workflows.workflow_definition_validation import (
        ChildDefinition,
        scan_child_chain,
    )

    def load(workflow_id: str) -> ChildDefinition | None:
        getter = getattr(db, "get_workflow", None)
        row = getter(workflow_id) if callable(getter) else None
        if not isinstance(row, dict):
            return None
        return ChildDefinition(
            workflow_id=workflow_id,
            revision=int(row.get("revision") or 0),
            definition=definition_from_workflow(row),
        )

    scan = scan_child_chain(
        child_workflow_id,
        load,
        max_depth=_resolve_invoke_workflow_max_depth(),
        ancestry=[item for item in ancestry if item != child_workflow_id],
    )
    if scan.waits:
        wait = scan.waits[0]
        return (
            f"Workflow '{wait.get('workflow_id')}' (revision {wait.get('revision')}) has a "
            f"{wait.get('step_kind')} step '{wait.get('step_id')}'; a child called from a "
            "parallel lane cannot pause"
        )
    if scan.issues:
        return str(scan.issues[0].get("message") or "")
    return ""
