"""Atomic group extraction composed from the canonical definition writers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.contract_validation_workflow_readiness import (
    _child_definition_loader,
)
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.registry.io_resolver import IOResolver
from core.application.workflows.workflow_change_authorization import (
    ChangeReferenceAccess,
    authorize_change_operations,
)
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    _CandidateStore,
    apply_workflow_changes,
    create_workflow_with_definition,
    definition_from_workflow,
    locked_workflow_definition,
    readiness_from_validator,
)
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_group_extraction_bindings import analyze_group_bindings
from core.application.workflows.workflow_group_extraction_proof import validate_source_producers
from core.application.workflows.workflow_group_extraction_structure import extract_group_structure
from core.application.workflows.workflow_hook_authoring_policy import (
    hook_declaration_authoring_issues,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityService


class GroupExtractionError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400, issues: list | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.issues = list(issues or [])


@dataclass
class _Plan:
    response: dict
    child: dict
    operations: list
    projects: list[str]


class GroupExtractionService:
    def __init__(
        self,
        db: Any,
        *,
        account_id: str,
        reference_access: ChangeReferenceAccess,
        project_visible: Callable[[str], bool],
        reactions: Callable[[str, str], list[dict]],
        validate: Callable = validate_workflow_readiness,
    ):
        self.db = db
        self.account_id = str(account_id)
        self.reference_access = reference_access
        self.project_visible = project_visible
        self.reactions = reactions
        self.validate = validate

    def _source(self, workflow_id: str, expected_revision: int) -> dict:
        source = self.db.get_workflow(workflow_id)
        if not source or str(source.get("account_id") or "") != self.account_id:
            raise GroupExtractionError("Workflow not found.", status_code=404)
        if type(expected_revision) is not int or expected_revision < 1:
            raise GroupExtractionError("A positive expected_revision is required.", status_code=428)
        if int(source.get("revision") or 0) != expected_revision:
            raise WorkflowRevisionConflictError(workflow_id, expected_revision)
        WorkflowPortabilityService(self.db).assert_structure_change_allowed(
            workflow_id, action="extract_workflow_group"
        )
        return source

    def _connected_hooks(self, source: dict, members: list[dict]) -> list[tuple[str, str]]:
        """``(hook id, project)`` of every configured binding of a hook among *members*."""
        from core.application.workflows.workflow_invocation_target import hook_ids, hook_owner_for

        moved = hook_ids({"steps": members})
        if not moved or not hasattr(self.db, "_transaction"):
            return []
        owner = hook_owner_for(source, account_id=self.account_id, project_id="")
        with self.db._transaction() as cur:
            cur.execute(
                """SELECT b.hook_id, b.project_id::text
                     FROM registry_workflow_hook_bindings b
                     JOIN registry_workflow_hook_binding_revisions r
                       ON r.id = b.active_revision_id
                    WHERE b.account_id::text = %s AND b.source_install_id = %s
                      AND b.local_workflow_id = %s AND b.hook_id = ANY(%s)
                      AND r.target_workflow_id IS NOT NULL
                    ORDER BY b.hook_id, b.project_id""",
                (owner.account_id, owner.source_install_id, owner.local_workflow_id, moved),
            )
            rows = cur.fetchall() or []
        return [
            (
                str(row["hook_id"] if isinstance(row, dict) else row[0]),
                str(row["project_id"] if isinstance(row, dict) else row[1]),
            )
            for row in rows
        ]

    def _plan(
        self,
        *,
        workflow_id: str,
        group_id: str,
        child_workflow_id: str,
        display_name: str,
        expected_revision: int,
    ) -> _Plan:
        source = self._source(workflow_id, expected_revision)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", child_workflow_id):
            raise GroupExtractionError(
                "Use a child identifier starting with a lowercase letter, followed by lowercase letters, digits or underscores."
            )
        if not display_name.strip() or len(display_name) > 200:
            raise GroupExtractionError("A child name of 1-200 characters is required.")
        resolver = IOResolver(self.db)
        nested = {
            str(item.get("for_each_id"))
            for item in source.get("for_each") or []
            if item.get("group_id") == group_id
        }
        members = [
            step
            for step in source.get("steps") or []
            if step.get("group_id") == group_id or step.get("for_each_id") in nested
        ]
        bindings = analyze_group_bindings(
            source,
            group_id,
            output_rows={
                s["step_id"]: resolver.get_step_state_outputs(workflow_id, s["step_id"])
                for s in source.get("steps") or []
            },
            retained_output_rows={
                s["step_id"]: resolver.get_step_state_writes(s, workflow_account_id=self.account_id)
                for s in source.get("steps") or []
            },
            available_types={
                s["step_id"]: resolver.get_available_type_map_for_step(workflow_id, s["step_id"])
                for s in members
            },
        )
        validate_source_producers(source, group_id, resolver, bindings)
        if self.db.get_workflow(child_workflow_id, include_trashed=True):
            bindings.refuse(
                "child_id_unavailable",
                "child_workflow_id",
                "This child identifier is unavailable; choose another.",
            )
        for hook_id, project_id in self._connected_hooks(source, members):
            # The hook would move to the new child, whose binding scope differs;
            # the client's binding would not follow, and losing it must never
            # turn a configured hook into an empty one.
            bindings.refuse(
                "hook_binding_relocation",
                f"hooks.{hook_id}",
                f"Hook '{hook_id}' is connected in project {project_id}; disconnect it, "
                "or keep the hook in the parent, before converting.",
            )
        if self.reactions(workflow_id, self.account_id):
            bindings.refuse(
                "reaction_boundary",
                "reactions",
                "This workflow has reactions whose scope must be reviewed before extraction; no reactions are silently moved or removed.",
            )
        projects = sorted(
            {
                str(row["project_id"])
                for row in self.db.list_project_workflows(enabled_only=False)
                if row.get("workflow_id") == workflow_id
                and row.get("enabled")
                and not row.get("deleted_at")
            }
        )
        if any(not self.project_visible(project) for project in projects):
            bindings.refuse(
                "project_access",
                "projects",
                "The child must be accessible from every project using its parent; project access is incomplete.",
            )
        used = {step["step_id"] for step in source.get("steps") or []}
        invoke_id = f"invoke_{group_id}"
        suffix = 1
        while invoke_id in used:
            invoke_id = f"invoke_{group_id}_{suffix}"
            suffix += 1
        invocation = {
            "step_id": invoke_id,
            "step_kind": "invoke_workflow",
            "display_name": display_name.strip(),
            "override": {
                "invoke_workflow": {
                    "workflow_id": child_workflow_id,
                    "input_mapping": bindings.input_mapping,
                    "output_mapping": bindings.output_mapping,
                }
            },
        }
        child, operations = {}, []
        try:
            structure = extract_group_structure(bindings.workflow, group_id, invocation)
            child = {
                "workflow_id": child_workflow_id,
                "display_name": display_name.strip(),
                "description": f"Extracted from {workflow_id}, group {group_id}.",
                "account_id": self.account_id,
                "scope_kind": "account",
                "enabled": True,
                "settings": {
                    "callable": bindings.interface,
                    "execution_graph": structure.child_definition["execution_graph"],
                },
                "steps": structure.child_definition["steps"],
                "groups": structure.child_definition["groups"],
                "for_each": structure.child_definition["for_each"],
            }
            operations = structure.parent_operations
        except ValueError as exc:
            bindings.refuse("structure_boundary", f"groups.{group_id}", str(exc))
        if child:
            bindings.issues.extend(hook_declaration_authoring_issues(child))
        if not bindings.issues:
            try:
                authorize_change_operations(
                    [{"op": "create_step", "step": step} for step in child["steps"]],
                    self.reference_access,
                )
                structural = list(
                    validate_workflow_definition(
                        definition_from_workflow(child),
                        load_child=_child_definition_loader(self.db),
                    )
                )
                bindings.issues.extend(structural)
                # Readiness repeats each structural issue under a "workflow." path:
                # list it once, as the definition writer does.
                projected = [
                    {
                        **issue,
                        "path": f"workflow.{issue['path']}" if issue.get("path") else "workflow",
                    }
                    for issue in structural
                ]
                bindings.issues.extend(
                    issue
                    for issue in readiness_from_validator(self.db, self.validate)(child)
                    if issue not in projected
                )
                child_view = _CandidateStore(self.db, child)
                candidate = apply_workflow_changes(
                    self.db,
                    workflow_id,
                    expected_revision=expected_revision,
                    operations=operations,
                    author="preview",
                    dry_run=True,
                    load_child=_child_definition_loader(child_view),
                    readiness=readiness_from_validator(child_view, self.validate),
                )
                bindings.issues.extend(candidate.issues)
            except WorkflowDefinitionChangeError as exc:
                bindings.issues.extend(exc.issues)
        fingerprint = {
            "revision": expected_revision,
            "child": child,
            "operations": operations,
            "projects": projects,
            "issues": bindings.issues,
        }
        digest = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        response = {
            "can_convert": not bindings.issues,
            "issues": bindings.issues,
            "revision": expected_revision,
            "preview_digest": digest,
            "child_workflow_id": child_workflow_id,
            "display_name": display_name.strip(),
            "invocation_step_id": invoke_id,
            "interface": bindings.interface,
            "input_mapping": bindings.input_mapping,
            "output_mapping": bindings.output_mapping,
            "step_count": len(members),
        }
        return _Plan(response, child, operations, projects)

    def preview(self, **kwargs) -> dict:
        return self._plan(**kwargs).response

    def apply(self, *, preview_digest: str, author: str, **kwargs) -> dict:
        # The same transaction owns planning, child creation and parent CAS.
        self._source(kwargs["workflow_id"], kwargs["expected_revision"])
        with locked_workflow_definition(
            self.db, kwargs["workflow_id"], kwargs["expected_revision"]
        ):
            plan = self._plan(**kwargs)
            if not plan.response["can_convert"]:
                raise GroupExtractionError(
                    "The group cannot be converted; nothing was saved.",
                    issues=plan.response["issues"],
                )
            if preview_digest != plan.response["preview_digest"]:
                raise GroupExtractionError(
                    "The conversion preview is stale; preview it again before applying.",
                    status_code=409,
                )
            child = dict(plan.child)
            child["execution_graph"] = child["settings"]["execution_graph"]
            created = create_workflow_with_definition(
                self.db, child, author=author, load_child=_child_definition_loader(self.db)
            )
            parent = apply_workflow_changes(
                self.db,
                kwargs["workflow_id"],
                expected_revision=kwargs["expected_revision"],
                operations=plan.operations,
                author=author,
                load_child=_child_definition_loader(self.db),
                readiness=readiness_from_validator(self.db, self.validate),
            )
            for project in plan.projects:
                self.db.add_project_workflow_binding(project, child["workflow_id"])
        return {
            **plan.response,
            "revision": parent.revision,
            "child_revision": created.revision,
            "applied": True,
        }
