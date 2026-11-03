"""Which stored and inline secrets one run may read, from trusted host records only.

A worker never takes this from the delivered message, from job metadata a caller
could supply, or from run state a step could author. It is derived per job from:

- the definition the job pinned: the workflow-local secret defaults it declares;
- the catalog secrets bound to that workflow's inputs for the job's own project;
- inline secrets present in the job's own dispatch record, the envelope the host
  encrypted in the transaction that created the job;
- for an Invoke child, what its parent job may read, limited to what the parent
  passed in, and only when the parent recorded this child in its own metadata.

A stored key that merely appears in launch inputs is never authority of its own:
a key is derivable from a workflow and field name, so accepting one would let any
launcher name another resource's credential.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: How many Invoke levels a delegated secret may travel.
MAX_DELEGATION_DEPTH = 5


@dataclass(frozen=True)
class RunJob:
    job_id: str
    account_id: str
    project_id: str
    workspace_id: str
    task_source: str
    meta: dict[str, Any]
    workflow_id: str
    definition: dict[str, Any] | None


@dataclass(frozen=True)
class RunSecretAuthority:
    stored_ref_keys: frozenset[str]
    inline_fingerprints: frozenset[str]

    def fingerprints(self) -> list[str]:
        """The run-state capability list the HTTP and connector boundaries check."""
        from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint

        return sorted(
            {secret_ref_fingerprint(key) for key in self.stored_ref_keys}
            | set(self.inline_fingerprints)
        )


EMPTY_AUTHORITY = RunSecretAuthority(frozenset(), frozenset())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def load_run_job(cur: Any, job_id: str) -> RunJob | None:
    """The job with the definition it pinned, read from the host database."""
    cur.execute(
        """
        SELECT j.id::text, j.account_id::text, j.project_id::text,
               p.workspace_id::text, j.task_source, j.meta,
               s.workflow_id, s.definition_jsonb
          FROM jobs AS j
          LEFT JOIN projects AS p ON p.id = j.project_id
          LEFT JOIN workflow_definition_snapshots AS s
            ON s.id = j.workflow_definition_snapshot_id AND s.account_id = j.account_id
         WHERE j.id::text = %s
        """,
        (_text(job_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    values = list(row.values()) if isinstance(row, Mapping) else list(row)
    definition = _mapping(values[7]) if values[7] is not None else None
    return RunJob(
        job_id=_text(values[0]),
        account_id=_text(values[1]),
        project_id=_text(values[2]),
        workspace_id=_text(values[3]),
        task_source=_text(values[4]).lower(),
        meta=_mapping(values[5]),
        workflow_id=_text(values[6]),
        definition=definition,
    )


def dispatch_extra_state(conn: Any, job_id: str) -> dict[str, Any]:
    """The extra state of the job's own encrypted dispatch record; empty when unreadable."""
    from core.application.auth.secrets import decrypt_secret
    from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository

    row = DispatchOutboxRepository(conn).get(job_id=_text(job_id))
    ciphertext = _text((row or {}).get("encrypted_envelope")) if isinstance(row, dict) else ""
    if not ciphertext:
        return {}
    try:
        decoded = json.loads(decrypt_secret(ciphertext))
    except Exception:
        return {}
    extra = decoded.get("extra_state") if isinstance(decoded, dict) else None
    return dict(extra) if isinstance(extra, dict) else {}


def _presented(extra_state: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(stored keys, inline fingerprints) of references in the launch inputs."""
    from core.infrastructure.security.secret_reference import (
        as_secret_reference,
        inline_secret_ref_fingerprint,
    )

    stored: set[str] = set()
    inline: set[str] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 32:
            return
        reference = as_secret_reference(value)
        if reference is not None:
            if reference.ref_key:
                stored.add(reference.ref_key)
            elif fingerprint := inline_secret_ref_fingerprint(reference):
                inline.add(fingerprint)
            return
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    walk(extra_state.get("inputs"))
    walk(_mapping(extra_state.get("task_envelope")).get("inputs"))
    return stored, inline


def definition_default_refs(definition: Any) -> set[str]:
    """Workflow-local secret defaults the pinned definition declares."""
    settings = _mapping(_mapping(definition).get("settings"))
    rows = _mapping(settings.get("callable")).get("expected_inputs")
    return {
        _text(row.get("default_secret_ref"))
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, Mapping)
        and _text(row.get("type")).lower() == "secret"
        and _text(row.get("default_secret_ref"))
    }


def bound_storage_keys(cur: Any, job: RunJob) -> set[str]:
    """Catalog storage keys bound to the job's workflow for the job's own project."""
    cur.execute(
        """
        SELECT s.storage_ref_key
          FROM catalog_secret_bindings AS b
          JOIN catalog_secrets AS s ON s.id = b.secret_id AND s.account_id = b.account_id
         WHERE b.consumer_kind = 'workflow_input' AND b.workflow_id = %s
           AND b.account_id::text = %s AND b.consumer_project_id::text = %s
        """,
        (job.workflow_id, job.account_id, job.project_id),
    )
    return {
        _text(row["storage_ref_key"] if isinstance(row, Mapping) else row[0])
        for row in cur.fetchall() or []
    }


def trusted_parent(cur: Any, job: RunJob) -> RunJob | None:
    """The Invoke parent of a child job, when both sides of the link agree."""
    if job.task_source != "invoke_workflow":
        return None
    parent_id = _text(job.meta.get("invoke_parent_job_id"))
    if not parent_id or parent_id == job.job_id:
        return None
    parent = load_run_job(cur, parent_id)
    if parent is None or parent.account_id != job.account_id:
        return None
    if parent.project_id != job.project_id:
        return None
    if _text(job.meta.get("invoke_parent_workflow_id")) != parent.workflow_id:
        return None
    children = _mapping(parent.meta.get("invoke_children"))
    return parent if job.job_id in children else None


def catalog_storage_keys(cur: Any, keys: set[str]) -> set[str]:
    """The subset of `keys` that belongs to catalog secrets."""
    if not keys:
        return set()
    cur.execute(
        "SELECT storage_ref_key FROM catalog_secrets WHERE storage_ref_key = ANY(%s)",
        (sorted(keys),),
    )
    return {
        _text(row["storage_ref_key"] if isinstance(row, Mapping) else row[0])
        for row in cur.fetchall() or []
    }


def pinned_definition_approved(cur: Any, job: RunJob) -> bool:
    """Whether a person approved exactly the definition this job pinned."""
    from core.application.secrets import workflow_gate

    projection = workflow_gate.current_projection(
        cur, account_id=job.account_id, workflow_id=job.workflow_id, definition=job.definition
    )
    approval = workflow_gate.workflow_approval(
        cur, account_id=job.account_id, workflow_id=job.workflow_id
    )
    if approval is None:
        return False
    return approval.get("fingerprint") == workflow_gate.workflow_fingerprint(projection)


def run_secret_authority(conn: Any, cur: Any, *, job_id: str, depth: int = 0) -> RunSecretAuthority:
    """Stored keys and inline fingerprints the job may read; empty when unknown.

    A catalog key survives a level of an Invoke chain only when that level's own
    pinned definition is approved, whether the level bound the key or received it:
    an approved child never revives the authority of an unapproved parent.
    """
    job = load_run_job(cur, job_id)
    if job is None or job.definition is None:
        return EMPTY_AUTHORITY
    presented_stored, presented_inline = _presented(dispatch_extra_state(conn, job.job_id))
    stored = definition_default_refs(job.definition) | bound_storage_keys(cur, job)
    inline: set[str] = set()
    parent = trusted_parent(cur, job)
    if parent is not None and depth < MAX_DELEGATION_DEPTH:
        delegated = run_secret_authority(conn, cur, job_id=parent.job_id, depth=depth + 1)
        stored |= set(delegated.stored_ref_keys) & presented_stored
        inline |= set(delegated.inline_fingerprints) & presented_inline
    elif job.task_source != "invoke_workflow":
        # A caller-supplied inline value is this run's own: it carries the value.
        inline |= presented_inline
    if depth > 0:
        # An ancestor passes catalog keys on only from an approved pin. The job's
        # own level is checked by the catalog gate, which names the failure.
        catalog = catalog_storage_keys(cur, stored)
        if catalog and not pinned_definition_approved(cur, job):
            stored -= catalog
    return RunSecretAuthority(frozenset(stored), frozenset(inline))


__all__ = [
    "EMPTY_AUTHORITY",
    "MAX_DELEGATION_DEPTH",
    "RunJob",
    "RunSecretAuthority",
    "bound_storage_keys",
    "definition_default_refs",
    "dispatch_extra_state",
    "load_run_job",
    "run_secret_authority",
    "trusted_parent",
]
