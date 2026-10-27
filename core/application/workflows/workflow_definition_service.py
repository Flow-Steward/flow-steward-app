"""The one writer of a workflow definition: steps, containers and the graph.

Builder, API, MCP and import all change a definition through
``apply_workflow_changes``: a list of operations applied to the stored
definition, judged as a whole, and written as one transaction and one new
revision — or refused entirely. Restoring a history entry
(``restore_workflow_snapshot``) and saving an edit session's own copy
(``replace_workflow_content``) replace the whole definition and are judged and
written by the same path, so Apply, Restore and a kept copy share one blocking
policy.
Nothing else writes ``execution_graph``.

Operations name nodes by their stable business ids, which the caller assigns
before the write, so a later operation in the same set can refer to a node an
earlier one created.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SCHEMA_VERSION,
    EXECUTION_GRAPH_SETTING,
)
from core.application.workflows.workflow_canvas_annotation_contract import (
    ANNOTATION_IMPORT_ERROR,
    ANNOTATIONS_SETTING,
    normalize_portable_annotations,
)
from core.application.workflows.workflow_definition_changes import (
    OPERATION_KINDS,
    WorkflowDefinitionChangeError,
    _ChangeSet,
    _Definition,
    _definition_for_each,
    _definition_group,
    _definition_step,
    _load_definition,
    _refuse,
    _refuse_removed_format,
    _text,
)
from core.application.workflows.workflow_definition_changes import (
    definition_from_workflow as definition_from_workflow,
)
from core.application.workflows.workflow_definition_changes import (
    removed_format_issues as removed_format_issues,
)
from core.application.workflows.workflow_definition_validation import (
    ChildLoader,
    validate_workflow_definition,
)
from core.application.workflows.workflow_history import (
    LEGACY_DEFINITION_KEYS,
    SNAPSHOT_KIND_PROJECTION,
    holds_definition,
    is_projection_workflow,
    restore_candidate,
    snapshot_graph_valid,
    snapshot_kind,
)
from core.application.workflows.workflow_hook_authoring_policy import (
    hook_declaration_authoring_issues,
)
from core.application.workflows.workflow_hook_placement import HOOK_ROOT_ONLY_CODE
from core.application.workflows.workflow_reaction_scope_repository import (
    retained_reaction_scope_issues,
)

#: Settings keys owned by dedicated services. A metadata save restores the stored
#: value instead of taking the caller's.
SERVICE_OWNED_SETTINGS_KEYS = (
    EXECUTION_GRAPH_SETTING,
    ANNOTATIONS_SETTING,
    "canvas_annotations_revision",
    # A hook's storage marker is written once, by create-and-bind (R2-11).
    "hook_storage",
)

#: Workflow columns a definition write carries over unchanged.
_WORKFLOW_METADATA_KEYS = (
    "version",
    "display_name",
    "description",
    "enabled",
    "trigger",
    "tags",
    "builder_metadata_jsonb",
    "account_id",
    "scope_kind",
    "source_install_id",
    "source_release_id",
    "source_projection_id",
    "local_workflow_id",
    "projection_mode",
    "derived_from_kind",
    "derived_from_ref",
    "sync_status",
    "availability_status",
    "availability_reason_code",
    "availability_reason_text",
    "new_runs_blocked",
    "visible_in_catalog",
)


@dataclass
class WorkflowChangeResult:
    revision: int | None
    issues: list[dict[str, str]] = field(default_factory=list)
    workflow: dict[str, Any] = field(default_factory=dict)
    #: False for a dry run: nothing was written and the revision is unchanged.
    applied: bool = True
    #: The issues that would refuse the set, including hard draft contracts, dry run only.
    blocking_issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues


def preserve_service_owned_settings(payload: Mapping[str, Any], existing: Any = None) -> dict:
    """Keep caller-supplied settings out of the keys this service owns.

    A metadata write carries whatever the caller sent, so the stored value is
    restored instead — or the key is dropped when nothing is stored.
    """

    data = dict(payload or {})
    settings = data.get("settings")
    if not isinstance(settings, dict):
        return data
    settings = dict(settings)
    stored = existing.get("settings") if isinstance(existing, Mapping) else None
    stored = stored if isinstance(stored, Mapping) else {}
    for key in SERVICE_OWNED_SETTINGS_KEYS:
        if key in stored:
            settings[key] = stored[key]
        else:
            settings.pop(key, None)
    data["settings"] = settings
    return data


ReadinessCheck = Callable[[dict[str, Any]], list[dict[str, Any]]]


def _distinct_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The issues in order, each (code, path, message) once."""
    seen: set[tuple[str, str, str]] = set()
    distinct: list[dict[str, Any]] = []
    for issue in issues:
        key = (
            _text(issue.get("code")) if isinstance(issue, Mapping) else "",
            _text(issue.get("path")) if isinstance(issue, Mapping) else "",
            _text(issue.get("message")) if isinstance(issue, Mapping) else str(issue),
        )
        if key not in seen:
            seen.add(key)
            distinct.append(issue)
    return distinct


def _candidate_workflow(workflow: Mapping[str, Any], definition: _Definition) -> dict[str, Any]:
    settings = dict(workflow.get("settings") or {})
    settings[EXECUTION_GRAPH_SETTING] = definition.graph
    return {
        **dict(workflow),
        "settings": settings,
        "steps": definition.steps,
        "groups": definition.groups,
        "for_each": definition.for_each,
    }


def _readiness_issues(
    workflow: Mapping[str, Any], definition: _Definition, readiness: ReadinessCheck | None
) -> list[dict[str, Any]]:
    if readiness is None:
        return []
    return list(readiness(_candidate_workflow(workflow, definition)))


class _CandidateStore:
    """The registry store with one workflow replaced by its candidate definition.

    Read-only: readiness checks the candidate exactly as it would check the
    stored row, and nothing can be written through it.
    """

    def __init__(self, db: Any, candidate: Mapping[str, Any]) -> None:
        self._db = db
        self._candidate = candidate

    def get_workflow(
        self, workflow_id: str, include_trashed: bool = False
    ) -> dict[str, Any] | None:
        if _text(workflow_id) == _text(self._candidate.get("workflow_id")):
            return copy.deepcopy(dict(self._candidate))
        return self._db.get_workflow(workflow_id, include_trashed=include_trashed)

    def __getattr__(self, name: str) -> Any:
        if name.startswith(
            ("save_", "add_", "delete_", "upsert_", "create_", "toggle_", "update_")
        ):
            raise AttributeError(name)
        return getattr(self._db, name)


def readiness_from_validator(
    db: Any, validate: Callable[[Any, str], Mapping[str, Any]]
) -> ReadinessCheck:
    """A readiness check that runs *validate* (store, workflow_id) on the candidate."""

    def check(candidate: dict[str, Any]) -> list[dict[str, Any]]:
        result = validate(_CandidateStore(db, candidate), _text(candidate.get("workflow_id")))
        errors = result.get("errors") if isinstance(result, Mapping) else None
        return [dict(item) for item in errors or [] if isinstance(item, Mapping)]

    return check


def create_workflow_with_definition(
    db: Any,
    payload: Mapping[str, Any],
    *,
    author: str,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
) -> WorkflowChangeResult:
    """Create a workflow together with its definition, as one write.

    Import uses this instead of a raw save so the graph still has a single
    writer. ``payload`` carries the workflow row fields plus ``steps``,
    ``groups``, ``for_each`` and ``execution_graph``. A disabled workflow is stored with its
    issues; an enabled one is refused unless its definition is structurally
    valid. The caller owns the surrounding transaction.
    """
    data = dict(payload)
    raw_graph = data.pop(EXECUTION_GRAPH_SETTING, None)
    graph = (
        copy.deepcopy(dict(raw_graph))
        if isinstance(raw_graph, Mapping)
        else {"schema_version": EXECUTION_GRAPH_SCHEMA_VERSION, "entry": None, "edges": []}
    )
    graph.setdefault("edges", [])
    definition = _Definition(
        steps=[_definition_step(step) for step in data.get("steps") or []],
        groups=[_definition_group(group) for group in data.get("groups") or []],
        for_each=[_definition_for_each(item) for item in data.get("for_each") or []],
        graph=graph,
    )
    _refuse_removed_format(definition.payload())
    authoring_issues = hook_declaration_authoring_issues({**data, "steps": definition.steps})
    issues = authoring_issues + list(
        validate_workflow_definition(
            definition.payload(), load_child=load_child, max_invoke_depth=max_invoke_depth
        )
    )
    blocking = (
        issues
        if bool(data.get("enabled"))
        else authoring_issues + [issue for issue in issues if issue["code"] == HOOK_ROOT_ONLY_CODE]
    )
    if blocking:
        raise WorkflowDefinitionChangeError("The workflow definition has blocking issues", blocking)
    settings = dict(data.get("settings") or {})
    if ANNOTATIONS_SETTING in settings:
        try:
            settings[ANNOTATIONS_SETTING] = normalize_portable_annotations(
                settings[ANNOTATIONS_SETTING]
            )
        except ValueError as exc:
            raise _refuse(
                "workflow.canvas_annotations.invalid",
                ANNOTATION_IMPORT_ERROR,
                "settings.canvas_annotations",
            ) from exc
    settings.pop("canvas_annotations_revision", None)
    if ANNOTATIONS_SETTING in settings:
        settings["canvas_annotations_revision"] = 0
    settings[EXECUTION_GRAPH_SETTING] = definition.graph
    data.update(
        {
            "settings": settings,
            "steps": definition.steps,
            "groups": definition.groups,
            "for_each": definition.for_each,
        }
    )
    revision = db.save_workflow(data, author=author, create_only=True)
    return WorkflowChangeResult(revision=revision, issues=issues)


@contextmanager
def locked_workflow_definition(
    db: Any, workflow_id: str, expected_revision: int
) -> Iterator[tuple[dict[str, Any], Any]]:
    """Yield the locked definition and its cursor for one atomic operation."""
    from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError

    with db._transaction() as cur:
        cur.execute(
            "SELECT revision FROM registry_workflows"
            " WHERE workflow_id = %s AND deleted_at IS NULL FOR UPDATE",
            (workflow_id,),
        )
        locked = cur.fetchone()
        if locked is None:
            raise LookupError(f"Workflow '{workflow_id}' not found")
        if int(locked["revision"]) != int(expected_revision):
            raise WorkflowRevisionConflictError(workflow_id, int(expected_revision))
        workflow = db.get_workflow(workflow_id)
        if not isinstance(workflow, dict):
            raise LookupError(f"Workflow '{workflow_id}' not found")
        yield workflow, cur


def apply_workflow_changes(
    db: Any,
    workflow_id: str,
    *,
    expected_revision: int,
    operations: list[Any],
    author: str,
    readiness: ReadinessCheck | None = None,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
    dry_run: bool = False,
) -> WorkflowChangeResult:
    """Apply a change set atomically and return the new revision.

    ``dry_run`` judges the same set the same way and writes nothing: it returns
    the resulting definition's issues, those that would refuse it, and the
    unchanged revision, so the builder shows exactly what Apply would decide.

    The resulting definition is validated as a whole, by the definition
    validator and by *readiness* (the caller's check of steps against the
    environment: agents, connections, extensions, secrets). An enabled
    workflow refuses every blocking issue of the result, including readiness
    issues caused by dependencies changing outside the definition. A repair
    must resolve all blocking issues atomically; incomplete edits require the
    workflow to be disabled first. A disabled workflow stores an incomplete
    definition
    together with its issues. Either way the set is one write and
    one revision, or no write at all. Authorization and scope are the caller's
    checks, made before this call.
    """
    normalized_id = _text(workflow_id)
    if not isinstance(operations, list) or not operations:
        raise _refuse(
            "workflow.change.empty", "A change set needs at least one operation", "operations"
        )
    with locked_workflow_definition(db, normalized_id, expected_revision) as (workflow, cur):
        definition = _load_definition(workflow)
        change_set = _ChangeSet(definition)
        for index, operation in enumerate(operations):
            change_set.apply(index, operation)
        result = _judge_and_write(
            db,
            cur,
            normalized_id,
            workflow,
            definition,
            expected_revision=expected_revision,
            author=author,
            readiness=readiness,
            load_child=load_child,
            max_invoke_depth=max_invoke_depth,
            dry_run=dry_run,
        )
    if not result.applied:
        return result
    result.workflow = db.get_workflow(normalized_id) or {}
    return result


def _judge_and_write(
    db: Any,
    cur: Any,
    workflow_id: str,
    base: Mapping[str, Any],
    definition: _Definition,
    *,
    expected_revision: int,
    author: str,
    readiness: ReadinessCheck | None,
    load_child: ChildLoader | None,
    max_invoke_depth: int,
    dry_run: bool = False,
) -> WorkflowChangeResult:
    """Judge a candidate definition and write it with *base*'s metadata and settings.

    *base* is the locked workflow, or for a restore the locked workflow with
    the restored author fields; its ``enabled`` decides what blocks.
    """
    enabled = bool(base.get("enabled"))
    _refuse_removed_format(definition.payload())
    structural = list(
        validate_workflow_definition(
            definition.payload(), load_child=load_child, max_invoke_depth=max_invoke_depth
        )
    )
    authoring_issues = hook_declaration_authoring_issues(
        _candidate_workflow(base, definition), existing=base
    )
    readiness_after = _readiness_issues(base, definition, readiness)
    # The readiness adapter prefixes structural paths with "workflow.".
    # Remove only exact copies of the same structural record; distinct
    # owners, messages, locale provenance and readiness issues remain.
    projected_structural = [
        {
            **issue,
            "path": f"workflow.{_text(issue.get('path'))}"
            if _text(issue.get("path"))
            else "workflow",
        }
        for issue in structural
    ]
    readiness_after = [issue for issue in readiness_after if issue not in projected_structural]
    reaction_issues = retained_reaction_scope_issues(
        cur, workflow_id, steps=definition.steps, groups=definition.groups
    )
    issues = _distinct_issues(structural + authoring_issues + readiness_after + reaction_issues)
    blocking = (
        issues
        if enabled
        else _distinct_issues(
            reaction_issues
            + authoring_issues
            + [issue for issue in structural if issue["code"] == HOOK_ROOT_ONLY_CODE]
        )
    )
    if dry_run:
        return WorkflowChangeResult(
            revision=int(base["revision"]),
            issues=issues,
            workflow=_candidate_workflow(base, definition),
            applied=False,
            blocking_issues=blocking,
        )
    if blocking:
        raise WorkflowDefinitionChangeError(
            "The change would leave the workflow with blocking graph or reaction scope issues; "
            "nothing was saved and the current revision keeps running",
            blocking,
        )
    settings = dict(base.get("settings") or {})
    settings[EXECUTION_GRAPH_SETTING] = definition.graph
    payload = {key: base[key] for key in _WORKFLOW_METADATA_KEYS if key in base}
    payload.update(
        {
            "workflow_id": workflow_id,
            "settings": settings,
            "steps": definition.steps,
            "groups": definition.groups,
            "for_each": definition.for_each,
        }
    )
    revision = db.save_workflow(payload, author=author, expected_revision=int(expected_revision))
    return WorkflowChangeResult(revision=revision, issues=issues)


def _restorable_definition(snapshot: Mapping[str, Any]) -> None:
    """Refuse an entry that is not a definition the current format accepts.

    Removed formats are refused for a disabled workflow too: there is no
    converter, so they are never stored as a draft.
    """
    for key in LEGACY_DEFINITION_KEYS:
        if key in snapshot:
            raise _refuse(
                "workflow.history.phases_removed",
                "This revision uses the removed phase-based format and cannot be restored",
                key,
            )
    if snapshot_kind(snapshot) == SNAPSHOT_KIND_PROJECTION:
        raise _refuse(
            "workflow.history.projection_reference",
            "This revision references an installed extension's definition and cannot be restored",
            "snapshot_kind",
        )
    if not holds_definition(snapshot):
        raise _refuse(
            "workflow.history.snapshot_incomplete",
            "This entry does not record the workflow definition and cannot be restored",
            "steps",
        )
    _refuse_removed_format(snapshot)
    if not snapshot_graph_valid(snapshot):
        raise _refuse(
            "workflow.history.graph_invalid",
            "This revision's routing graph is not in a supported format",
            f"settings.{EXECUTION_GRAPH_SETTING}",
        )
    for collection, id_key in (
        ("steps", "step_id"),
        ("groups", "group_id"),
        ("for_each", "for_each_id"),
    ):
        ids = [
            _text(row.get(id_key)) if isinstance(row, Mapping) else ""
            for row in snapshot[collection]
        ]
        if not all(ids) or len(set(ids)) != len(ids):
            raise _refuse(
                "workflow.history.node_ids_invalid",
                f"Every entry of '{collection}' in this revision needs its own {id_key}",
                collection,
            )


def _replace_definition(
    db: Any,
    workflow_id: str,
    *,
    snapshot: Mapping[str, Any],
    expected_revision: int,
    author: str,
    reference_access: Any,
    authorize: Callable[[dict[str, Any]], None] | None,
    readiness: ReadinessCheck | None,
    load_child: ChildLoader | None,
    max_invoke_depth: int,
    dry_run: bool,
) -> WorkflowChangeResult:
    """Replace the author's content of the locked workflow with *snapshot*, or judge it.

    One path for a history restore and for an edit session's own copy: the
    candidate is the current row with the snapshot's author fields, settings
    and definition (``restore_candidate``), judged and written by
    ``_judge_and_write`` like an Apply. The answer is read back inside the same
    transaction, so it is the revision this call wrote, never a later one.
    """
    normalized_id = _text(workflow_id)
    raw = copy.deepcopy(dict(snapshot))
    _restorable_definition(raw)
    with locked_workflow_definition(db, normalized_id, expected_revision) as (workflow, cur):
        if is_projection_workflow(workflow):
            raise _refuse(
                "workflow.history.projection_reference",
                "An installed extension owns this workflow's definition; it cannot be restored",
                "workflow_id",
            )
        if authorize is not None:
            authorize(workflow)
        if reference_access is not None:
            from core.application.workflows.workflow_change_authorization import (
                authorize_definition_references,
            )

            authorize_definition_references(
                raw["steps"], reference_access, current_steps=workflow.get("steps") or []
            )
        base, restored = restore_candidate(raw, workflow)
        graph = restored["execution_graph"]
        graph = (
            copy.deepcopy(dict(graph))
            if isinstance(graph, Mapping)
            else {"schema_version": EXECUTION_GRAPH_SCHEMA_VERSION, "entry": None, "edges": []}
        )
        graph.setdefault("edges", [])
        definition = _Definition(
            steps=list(restored["steps"]),
            groups=list(restored["groups"]),
            for_each=list(restored["for_each"]),
            graph=graph,
        )
        result = _judge_and_write(
            db,
            cur,
            normalized_id,
            base,
            definition,
            expected_revision=expected_revision,
            author=author,
            readiness=readiness,
            load_child=load_child,
            max_invoke_depth=max_invoke_depth,
            dry_run=dry_run,
        )
        if result.applied:
            result.workflow = db.get_workflow(normalized_id) or {}
    return result


def restore_workflow_snapshot(
    db: Any,
    workflow_id: str,
    *,
    snapshot: Mapping[str, Any],
    expected_revision: int,
    author: str,
    reference_access: Any = None,
    authorize: Callable[[dict[str, Any]], None] | None = None,
    readiness: ReadinessCheck | None = None,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
) -> WorkflowChangeResult:
    """Write a history entry's definition back as a new revision.

    The author's content comes from *snapshot*; identity, ``enabled``,
    provenance, availability, canvas notes and input secrets and sample files
    stay as they are now. The candidate is judged like an Apply of the current
    workflow: an enabled workflow takes only a ready definition, a disabled one
    stores an incomplete definition with its issues. *reference_access* checks
    every resource the restored steps name, as Apply checks its operations;
    *authorize* re-applies the caller's ownership policy to the locked row.
    """
    return _replace_definition(
        db,
        workflow_id,
        snapshot=snapshot,
        expected_revision=expected_revision,
        author=author,
        reference_access=reference_access,
        authorize=authorize,
        readiness=readiness,
        load_child=load_child,
        max_invoke_depth=max_invoke_depth,
        dry_run=False,
    )


def replace_workflow_content(
    db: Any,
    workflow_id: str,
    *,
    content: Mapping[str, Any],
    expected_revision: int,
    author: str,
    reference_access: Any = None,
    authorize: Callable[[dict[str, Any]], None] | None = None,
    readiness: ReadinessCheck | None = None,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
    dry_run: bool = False,
) -> WorkflowChangeResult:
    """Write an author's whole content (an edit session's copy) as a new revision.

    The same writer as a restore: current ``enabled``, provenance, notes and
    input authority stay; the candidate is judged like an Apply; ``dry_run``
    judges without writing. *content* is already parsed authoring content.
    """
    return _replace_definition(
        db,
        workflow_id,
        snapshot=content,
        expected_revision=expected_revision,
        author=author,
        reference_access=reference_access,
        authorize=authorize,
        readiness=readiness,
        load_child=load_child,
        max_invoke_depth=max_invoke_depth,
        dry_run=dry_run,
    )


__all__ = [
    "OPERATION_KINDS",
    "SERVICE_OWNED_SETTINGS_KEYS",
    "WorkflowChangeResult",
    "WorkflowDefinitionChangeError",
    "apply_workflow_changes",
    "create_workflow_with_definition",
    "definition_from_workflow",
    "preserve_service_owned_settings",
    "readiness_from_validator",
    "removed_format_issues",
    "replace_workflow_content",
    "restore_workflow_snapshot",
]
