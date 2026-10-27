"""Pins the definition a job runs to the shared, deduplicated snapshot archive.

The pin happens in the job lifecycle transaction, before the dispatch intent
is published, so no workflow job reaches a worker without the definition it
must execute. Runs of one revision share one archive row; tenants never share.
"""

from __future__ import annotations

import json
from typing import Any

from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
from core.application.registry.db_store_workflows_mixin import load_workflow_definition_rows
from core.application.registry.workflow_lookup import get_workflow_row, is_projection_anchor
from core.application.workflows.workflow_definition_snapshot import (
    DEFINITION_SNAPSHOT_SCHEMA_VERSION,
    canonical_json,
    definition_digest,
    definition_ref,
    frozen_definition,
)
from core.application.workflows.workflow_definition_validation import (
    ChildDefinition,
    scan_child_chain,
    validate_workflow_definition,
)
from core.application.workflows.workflow_hook_binding_repository import (
    WorkflowHookBindingRepository,
)
from core.application.workflows.workflow_invocation_target import (
    HOOK_BINDING_PINS_META_KEY,
    hook_ids,
    hook_owner_for,
)
from core.infrastructure.db import psycopg_runtime

#: A concurrent cleanup may remove an unreferenced row between the insert and
#: the lock; the pin then simply asks again.
_PIN_ATTEMPTS = 3


class WorkflowDefinitionPinError(RuntimeError):
    """The workflow cannot be run: it is missing or its definition is not runnable."""

    def __init__(self, message: str, issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


class WorkflowRevisionChangedError(WorkflowDefinitionPinError):
    """The workflow changed after the caller loaded the revision it asked to run."""

    def __init__(self, workflow_id: str, expected_revision: int, revision: int) -> None:
        super().__init__(
            f"Workflow '{workflow_id}' is at revision {revision}, not {expected_revision}"
        )
        self.workflow_id = workflow_id
        self.expected_revision = expected_revision
        self.revision = revision


#: ``jobs.meta`` key of the revision a test run was started for.
EXPECTED_REVISION_META_KEY = "expected_workflow_revision"


class WorkflowDefinitionSnapshotRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _cursor(self):
        return self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor)

    def _projected_definition(
        self, workflow: dict[str, Any], *, account_id: str
    ) -> dict[str, Any] | None:
        """An installed extension workflow's definition, from its active projection.

        The platform row of an extension workflow is only a projection anchor:
        its steps, groups and graph live in the install's projection, which
        every read path resolves with ``get_workflow_row``. The pin reads the
        same view, on this transaction's connection.
        """
        anchor = dict(workflow)

        class _PinStore:
            extension_runtime_store = DBExtensionRuntimeStore(self.conn)

            @staticmethod
            def get_workflow(_workflow_id: str) -> dict[str, Any]:
                return dict(anchor)

        projected = get_workflow_row(
            _PinStore(), str(workflow.get("workflow_id") or ""), account_id=account_id
        )
        return dict(projected) if isinstance(projected, dict) else None

    def _workflow_row(
        self, cur, workflow_id: str, *, account_id: str = ""
    ) -> dict[str, Any] | None:
        cur.execute(
            """SELECT id, workflow_id, revision, settings, source_install_id, projection_mode,
                      local_workflow_id
                 FROM registry_workflows
                WHERE workflow_id = %s AND deleted_at IS NULL
                FOR SHARE""",
            (workflow_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        workflow = dict(row)
        rows = load_workflow_definition_rows(cur, [workflow_id]).get(workflow_id)
        workflow["steps"] = rows.steps if rows else []
        workflow["groups"] = rows.groups if rows else []
        workflow["for_each"] = rows.for_each if rows else []
        # An anchor that carries steps of its own (materialized) keeps them.
        if is_projection_anchor(workflow) and not workflow["steps"]:
            projected = self._projected_definition(workflow, account_id=account_id)
            if projected is None:
                raise WorkflowDefinitionPinError(
                    f"Workflow '{workflow_id}' belongs to an extension install that is not "
                    "active, so it cannot run",
                    [
                        {
                            "code": "workflow.extension_unavailable",
                            "message": "The extension that ships this workflow is not active",
                            "path": "workflow_id",
                        }
                    ],
                )
            if projected.get("new_runs_blocked"):
                # Listed and readable, so the user sees why; never run.
                reason = str(projected.get("availability_reason_text") or "").strip() or (
                    "A required extension is missing"
                )
                raise WorkflowDefinitionPinError(
                    f"Workflow '{workflow_id}' cannot run: {reason}",
                    [
                        {
                            "code": "workflow.extension_dependency_missing",
                            "message": reason,
                            "path": "dependencies",
                        }
                    ],
                )
            for key in ("steps", "groups", "for_each", "settings"):
                workflow[key] = projected.get(key) or ([] if key != "settings" else {})
        return workflow

    def pin_for_job(
        self, *, job_id: Any, workflow_id: str, validate: bool = True
    ) -> dict[str, Any]:
        """Attach the current revision's snapshot to the job and return its ref.

        The revision is re-validated here, at the moment it is pinned: a
        definition that is not structurally runnable never reaches dispatch.
        A step test run (``validate=False``) executes one step of a possibly
        incomplete draft and never routes; its route validated that step.
        A job that names the revision it was started for (a builder test run)
        is refused when the workflow has moved on, in the transaction that
        creates it, so no job of another revision ever exists.
        """
        normalized_id = str(workflow_id or "").strip()
        with self._cursor() as cur:
            cur.execute(
                "SELECT account_id, project_id, meta ->> %s AS expected_revision, "
                "meta -> 'invoke_lane_placement' AS invoke_lane_placement FROM jobs WHERE id = %s",
                (EXPECTED_REVISION_META_KEY, job_id),
            )
            job = cur.fetchone()
            if job is None:
                raise WorkflowDefinitionPinError(f"Job '{job_id}' not found")
            workflow = self._workflow_row(cur, normalized_id, account_id=job["account_id"])
            if workflow is None:
                raise WorkflowDefinitionPinError(f"Workflow '{normalized_id}' not found")
            expected = str(job.get("expected_revision") or "").strip()
            if expected and int(expected) != int(workflow["revision"]):
                raise WorkflowRevisionChangedError(
                    normalized_id, int(expected), int(workflow["revision"])
                )
            definition = frozen_definition(workflow)
            if job.get("invoke_lane_placement") is True and any(
                step.get("step_kind") in {"approval", "wait"}
                for step in definition.get("steps") or []
            ):
                # Descendants inherit this host-owned placement marker and
                # pass through the same pin. Validate the revision being
                # dispatched, not the one a previous dependency scan saw.
                raise WorkflowDefinitionPinError(
                    f"Workflow '{normalized_id}' cannot pause when invoked from a lane"
                )

            # The binding of every hook, or its explicit absence, is fixed in
            # this transaction: the run never reads a live binding later.
            owner = hook_owner_for(
                workflow, account_id=job["account_id"], project_id=job.get("project_id")
            )
            bindings = WorkflowHookBindingRepository(self.conn)
            hook_pins = {
                hook_id: (revision.id if revision is not None else None)
                for hook_id, revision in bindings.active(owner, hook_ids(definition)).items()
            }

            def load_child(child_id: str) -> ChildDefinition | None:
                child = self._workflow_row(cur, child_id, account_id=job["account_id"])
                if child is None:
                    return None
                return ChildDefinition(
                    child_id,
                    int(child["revision"]),
                    frozen_definition(child),
                    owner=hook_owner_for(
                        child, account_id=job["account_id"], project_id=job.get("project_id")
                    ),
                )

            issues: list[dict[str, str]] = []
            if validate:
                child_loader = None if job.get("invoke_lane_placement") is True else load_child
                issues = validate_workflow_definition(
                    definition, load_child=child_loader, owner=owner, bindings=bindings
                )
            if job.get("invoke_lane_placement") is True:
                # A serial child can inherit a parallel placement from its
                # caller. Check its complete chain before any child effects.
                chain = scan_child_chain(normalized_id, load_child, max_depth=5, bindings=bindings)
                issues.extend(chain.issues)
                for wait in chain.waits:
                    issue = {
                        "code": "workflow.placement.lane_child_waits",
                        "message": (
                            f"Workflow '{normalized_id}' runs in a lane, but child chain "
                            f"{' → '.join(wait['chain'])} has a {wait['step_kind']} "
                            f"step '{wait['step_id']}'"
                        ),
                        "path": "steps",
                    }
                    issues.append(issue)
            if issues:
                raise WorkflowDefinitionPinError(
                    f"Workflow '{normalized_id}' revision {workflow['revision']} is not runnable",
                    issues,
                )
            digest = definition_digest(definition)
            # The archive row belongs to the job's account: a shared system
            # workflow run by two tenants yields two rows, never one.
            snapshot_id = self._get_or_create(
                cur,
                account_id=job["account_id"],
                workflow_registry_id=workflow["id"],
                workflow_id=normalized_id,
                revision=int(workflow["revision"]),
                digest=digest,
                definition=definition,
            )
            ref = definition_ref(snapshot_id, int(workflow["revision"]), digest)
            cur.execute(
                """UPDATE jobs
                      SET workflow_definition_snapshot_id = %s,
                          meta = COALESCE(meta, '{}'::jsonb)
                                 || jsonb_build_object(
                                        'workflow_definition_ref',
                                        jsonb_build_object('revision', %s::int, 'digest', %s::text),
                                        %s::text, %s::jsonb
                                    )
                    WHERE id = %s""",
                (
                    snapshot_id,
                    ref["revision"],
                    ref["digest"],
                    HOOK_BINDING_PINS_META_KEY,
                    json.dumps(hook_pins),
                    job_id,
                ),
            )
            return ref

    def ensure_pinned(self, *, job_id: Any, workflow_id: str) -> dict[str, Any]:
        """Read the job's submission pin; never create one after dispatch.

        A job pinned at submission keeps its snapshot: redelivery and resume
        never re-read the registry. A pin for another workflow than the one now
        requested is an integrity error, not a silent switch.
        """
        normalized_id = str(workflow_id or "").strip()
        with self._cursor() as cur:
            cur.execute(
                """SELECT s.id, s.workflow_id, s.revision, s.digest
                     FROM jobs j
                     LEFT JOIN workflow_definition_snapshots s
                       ON s.id = j.workflow_definition_snapshot_id
                      AND s.account_id = j.account_id
                    WHERE j.id = %s
                    FOR UPDATE OF j""",
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise WorkflowDefinitionPinError(f"Job '{job_id}' not found")
        if row.get("id") is not None:
            if normalized_id and str(row.get("workflow_id") or "") != normalized_id:
                raise WorkflowDefinitionPinError(
                    f"Job '{job_id}' is pinned to workflow '{row.get('workflow_id')}', "
                    f"not '{normalized_id}'"
                )
            return definition_ref(str(row["id"]), int(row["revision"]), str(row["digest"]))
        raise WorkflowDefinitionPinError(
            f"Job '{job_id}' has no pinned workflow definition; start a new run"
        )

    def _get_or_create(
        self,
        cur,
        *,
        account_id: Any,
        workflow_registry_id: Any,
        workflow_id: str,
        revision: int,
        digest: str,
        definition: dict[str, Any],
    ) -> str:
        identity = (account_id, workflow_registry_id, revision, digest)
        for _attempt in range(_PIN_ATTEMPTS):
            cur.execute(
                """INSERT INTO workflow_definition_snapshots
                       (account_id, workflow_registry_id, workflow_id, revision, digest,
                        schema_version, definition_jsonb)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (account_id, workflow_registry_id, revision, digest) DO NOTHING""",
                (
                    account_id,
                    workflow_registry_id,
                    workflow_id,
                    revision,
                    digest,
                    DEFINITION_SNAPSHOT_SCHEMA_VERSION,
                    canonical_json(definition),
                ),
            )
            # Lock the row against the retention cleanup until the job's
            # foreign key holds it.
            cur.execute(
                """SELECT id FROM workflow_definition_snapshots
                    WHERE account_id = %s AND workflow_registry_id = %s
                      AND revision = %s AND digest = %s
                    FOR KEY SHARE""",
                identity,
            )
            row = cur.fetchone()
            if row is not None:
                return str(row["id"])
        raise WorkflowDefinitionPinError(
            f"Workflow '{workflow_id}' definition snapshot could not be pinned"
        )

    def load(self, snapshot_id: Any, *, account_id: Any) -> dict[str, Any] | None:
        """The archived definition, readable only inside its own account."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT id, workflow_id, revision, digest, schema_version, definition_jsonb
                     FROM workflow_definition_snapshots
                    WHERE id = %s AND account_id = %s""",
                (snapshot_id, account_id),
            )
            row = cur.fetchone()
        return dict(row) if row is not None else None

    def load_for_job(self, job_id: Any) -> dict[str, Any] | None:
        """The snapshot a job pinned, through the job's own account."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT s.id, s.workflow_id, s.revision, s.digest, s.schema_version,
                          s.definition_jsonb
                     FROM jobs j
                     JOIN workflow_definition_snapshots s
                       ON s.id = j.workflow_definition_snapshot_id
                      AND s.account_id = j.account_id
                    WHERE j.id = %s""",
                (job_id,),
            )
            row = cur.fetchone()
        return dict(row) if row is not None else None


__all__ = [
    "EXPECTED_REVISION_META_KEY",
    "WorkflowDefinitionPinError",
    "WorkflowDefinitionSnapshotRepository",
    "WorkflowRevisionChangedError",
]
