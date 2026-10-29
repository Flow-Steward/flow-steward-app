# mypy: ignore-errors
"""Workflow portability service mixin."""

from __future__ import annotations

from typing import Any

import yaml

from core.application.orchestration.workflow_state_selector_contract import (
    validate_workflow_state_selectors,
)
from core.application.registry.boundary_contracts import validate_workflow_boundary_contracts
from core.application.registry.db_store_workflows_mixin import WorkflowCreateConflictError
from core.application.workflows.workflow_canvas_annotation_contract import (
    ANNOTATION_IMPORT_ERROR,
    ANNOTATIONS_SETTING,
    normalize_portable_annotations,
)
from core.application.workflows.workflow_definition_service import (
    create_workflow_with_definition,
)
from core.application.workflows.workflow_definition_validation import (
    validate_workflow_definition,
)
from core.application.workflows.workflow_hook_authoring_policy import (
    hook_declaration_authoring_issues,
)
from core.application.workflows.workflow_portability_artifacts import parse_definitions
from core.application.workflows.workflow_portability_bundle_children import (
    bundled_child_ids,
    bundled_workflow_ids,
    creation_order,
    root_workflow_id,
)
from core.application.workflows.workflow_portability_common import (
    BUNDLE_KIND,
    BUNDLE_WORKFLOWS_VALUE,
    DEFINITIONS_BUNDLE_KEY,
    HOOK_BINDINGS_KEY,
    OPTIONAL_TOP_LEVEL_KEYS,
    RESOLUTION_ACTIONS,
    SCHEMA_VERSION,
    TOP_LEVEL_KEYS,
    WorkflowPortabilityError,
    _as_list,
    _redact_sensitive,
    _text,
)
from core.application.workflows.workflow_portability_hook_bindings import bound_child_ids
from core.application.workflows.workflow_step_config_contracts import (
    validate_workflow_step_config_keys,
)


def _workflow_definition_issues(
    workflow: dict[str, Any],
    *,
    workflow_path: str,
) -> list[dict[str, Any]]:
    """Run the checks a save runs, over the definition an import would write.

    Preflight only ever checked step config keys, so a bundle whose control-flow
    step config the save path rejects imported cleanly and then could not be
    saved. This reuses the same contract validator rather than restating its rules.
    """
    from core.application.registry.contract_validation_workflow import (
        _validate_control_flow_step_config,
    )

    issues = list(validate_workflow_step_config_keys(workflow, workflow_path=workflow_path))
    issues.extend(validate_workflow_boundary_contracts(workflow, workflow_path=workflow_path))
    issues.extend(validate_workflow_state_selectors(workflow, workflow_path=workflow_path))
    settings = workflow.get("settings") or {}
    if isinstance(settings, dict) and ANNOTATIONS_SETTING in settings:
        try:
            normalize_portable_annotations(settings[ANNOTATIONS_SETTING])
        except ValueError:
            issues.append(
                {
                    "code": "workflow.canvas_annotations.invalid",
                    "message": ANNOTATION_IMPORT_ERROR,
                    "path": f"{workflow_path}.settings.canvas_annotations",
                    "severity": "error",
                }
            )
    for step in _as_list(workflow.get("steps")):
        if not isinstance(step, dict):
            continue
        step_kind = str(step.get("step_kind") or step.get("kind") or "").strip()
        if not step_kind:
            continue
        issues.extend(
            _validate_control_flow_step_config(
                step_kind=step_kind,
                step=step,
                step_path=f"{workflow_path}.steps.{_text(step.get('step_id'))}",
            )
        )
    return issues


def _with_carried_children(
    resolution_index: dict[tuple[str, str], dict[str, Any]],
    placed_ids: dict[str, str],
    carried_ids: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """The resolutions plus where each already placed carried child now lives.

    A carried child is created (or reused) before its callers, possibly under a new
    id; the caller's invoke steps are rewritten to that id like any mapped child.
    """
    merged = dict(resolution_index)
    for source_id, target_id in placed_ids.items():
        if source_id in carried_ids:
            merged[("child_workflow", source_id)] = {
                "action": "map_existing",
                "target_ref": target_id,
            }
    return merged


def _bundle_role(workflow_id: str, root_id: str, child_ids: set[str]) -> str:
    """How a bundled workflow takes part: the template itself, or a child it calls."""
    if workflow_id and workflow_id == root_id:
        return "root"
    return "child" if workflow_id in child_ids else "workflow"


class WorkflowPortabilityImportMixin:
    def parse_bundle(self, bundle_yaml: str) -> dict[str, Any]:
        raw = _text(bundle_yaml)
        if not raw:
            raise WorkflowPortabilityError("YAML bundle is required.", code="bundle_yaml_required")
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise WorkflowPortabilityError(
                "YAML bundle could not be parsed.",
                code="bundle_yaml_invalid",
                errors=[{"message": str(exc)}],
            ) from exc
        if not isinstance(parsed, dict):
            raise WorkflowPortabilityError(
                "YAML bundle must be a mapping.", code="bundle_yaml_invalid"
            )

        keys = list(parsed.keys())
        expected = list(TOP_LEVEL_KEYS)
        if set(keys) - set(OPTIONAL_TOP_LEVEL_KEYS) != set(expected):
            missing = [key for key in expected if key not in parsed]
            extra = [
                key for key in keys if key not in expected and key not in OPTIONAL_TOP_LEVEL_KEYS
            ]
            raise WorkflowPortabilityError(
                "YAML bundle must contain exactly the workflow bundle top-level sections.",
                code="bundle_schema_invalid",
                errors=[{"missing": missing, "extra": extra}],
            )
        if parsed.get("schema_version") != SCHEMA_VERSION or parsed.get("kind") != BUNDLE_KIND:
            # A phase-based v1 bundle is refused, never converted: its routing
            # cannot be read as an execution graph without guessing.
            raise WorkflowPortabilityError(
                f"Unsupported workflow bundle schema; this platform imports {SCHEMA_VERSION} "
                "bundles. Recreate a phase-based v1 workflow in the builder.",
                code="bundle_schema_unsupported",
                errors=[
                    {
                        "schema_version": parsed.get("schema_version"),
                        "kind": parsed.get("kind"),
                        "required_schema_version": SCHEMA_VERSION,
                    }
                ],
            )
        if not isinstance(parsed.get("metadata"), dict):
            raise WorkflowPortabilityError(
                "Bundle metadata must be an object.", code="bundle_schema_invalid"
            )
        if not isinstance(parsed.get("workflows"), list):
            raise WorkflowPortabilityError(
                "Bundle workflows must be a list.", code="bundle_schema_invalid"
            )
        if not isinstance(parsed.get("dependencies"), (dict, list)):
            raise WorkflowPortabilityError(
                "Bundle dependencies must be an object or list.",
                code="bundle_schema_invalid",
            )
        if not isinstance(parsed.get("import_hints"), dict):
            raise WorkflowPortabilityError(
                "Bundle import_hints must be an object.",
                code="bundle_schema_invalid",
            )
        if HOOK_BINDINGS_KEY in parsed and not isinstance(parsed[HOOK_BINDINGS_KEY], list):
            raise WorkflowPortabilityError(
                "Bundle hook_bindings must be a list.",
                code="bundle_schema_invalid",
            )
        if DEFINITIONS_BUNDLE_KEY in parsed:
            parsed[DEFINITIONS_BUNDLE_KEY] = parse_definitions(parsed[DEFINITIONS_BUNDLE_KEY])
        return parsed

    #: Definition issues an incomplete draft may still have. Everything else the
    #: definition validator reports is structural — a target that does not exist,
    #: an edge across scopes, a cycle without rework, a phase-era field — and no
    #: valid workflow has it, so importing it as a draft only defers the failure.
    DRAFT_DEFINITION_CODES = frozenset(
        {
            "workflow.graph.missing",
            "workflow.graph.entry_missing",
            "workflow.graph.outcome_missing",
            "workflow.graph.unreachable",
        }
    )

    @classmethod
    def _topology_issues(cls, workflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run the definition validator over each workflow the bundle carries.

        Every issue is reported, so preflight is never silent; `blocking` marks
        the ones an import cannot honour. A draft that is not connected yet is a
        normal thing to import and finish later — readiness enforces the rest
        before the workflow is enabled or run.
        """
        issues: list[dict[str, Any]] = []
        for workflow in workflows:
            workflow_id = _text(workflow.get("workflow_id"))
            definition = {
                "steps": _as_list(workflow.get("steps")),
                "groups": _as_list(workflow.get("groups")),
                "for_each": _as_list(workflow.get("for_each")),
                "execution_graph": workflow.get("execution_graph"),
            }
            for issue in validate_workflow_definition(definition):
                code = str(issue.get("code") or "").strip()
                issues.append(
                    {
                        **issue,
                        "workflow_id": workflow_id,
                        "blocking": code not in cls.DRAFT_DEFINITION_CODES,
                    }
                )
        return issues

    def _import_hook_authoring_issues(
        self,
        workflows: list[dict[str, Any]],
        *,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        child_ids: set[str],
        root_id: str,
        explicit_target: str,
    ) -> list[dict[str, Any]]:
        """Judge only definitions this import actually creates, before side effects."""
        issues = []
        for workflow in workflows:
            source_id = _text(workflow.get("workflow_id"))
            resolution = resolution_index.get(("workflow", source_id)) or {}
            # This is the same reuse branch as apply_import: no new owner exists.
            if source_id in child_ids and _text(resolution.get("action")) == "use_existing":
                continue
            target_id = self._target_workflow_id(
                source_id,
                resolution_index=resolution_index,
                explicit_target=explicit_target if source_id == root_id else "",
            )
            issues.extend(
                {**issue, "workflow_id": target_id, "blocking": True}
                for issue in hook_declaration_authoring_issues(
                    {**workflow, "workflow_id": target_id}
                )
            )
        return issues

    def preflight_import(
        self,
        bundle_yaml: str,
        *,
        target_workflow_id: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        target_project_id: str = "",
    ) -> dict[str, Any]:
        bundle = self.parse_bundle(bundle_yaml)
        resolution_index = self._resolution_index(resolutions or [])
        definition_plan = self._definition_plan(
            bundle, resolution_index, target_project_id=target_project_id
        )
        # A reference the file also defines is answered by its definition row.
        dependencies = [
            row
            for row in self._bundle_dependencies(bundle)
            if (_text(row.get("dependency_type")), _text(row.get("ref")))
            not in definition_plan["keys"]
        ]
        dependencies.extend(
            row
            for row in definition_plan["external_rows"]
            if _text(row.get("dependency_type")) != "data_contract"
        )
        workflow_entries = [
            item for item in _as_list(bundle.get("workflows")) if isinstance(item, dict)
        ]

        full_workflows = [
            item
            for item in workflow_entries
            if _text(item.get("portability_kind")) != "extension_workflow_ref"
        ]
        if not workflow_entries:
            dependencies.append(
                {
                    "dependency_type": "bundle",
                    "ref": "workflows",
                    "label": "workflows",
                    "sources": [BUNDLE_WORKFLOWS_VALUE],
                    "required": True,
                    "metadata": {},
                }
            )
        for ref_workflow in workflow_entries:
            if _text(ref_workflow.get("portability_kind")) == "extension_workflow_ref":
                ref = _text(
                    ref_workflow.get("source_install_id") or ref_workflow.get("workflow_id")
                )
                dependencies.append(
                    {
                        "dependency_type": "extension_workflow_ref",
                        "ref": ref,
                        "label": _text(ref_workflow.get("workflow_id") or ref),
                        "sources": [BUNDLE_WORKFLOWS_VALUE],
                        "required": True,
                        "metadata": _redact_sensitive(ref_workflow),
                    }
                )

        # A child workflow the bundle carries is part of this import, not a
        # dependency the target must already have.
        carried_ids = bundled_workflow_ids(full_workflows)
        # A carried child a hook binding targets is a child of this import too.
        child_ids = bundled_child_ids(full_workflows) | bound_child_ids(bundle, carried_ids)
        root_id = root_workflow_id(bundle, full_workflows)
        for index, workflow in enumerate(full_workflows):
            for issue in _workflow_definition_issues(
                workflow,
                workflow_path=f"workflows[{index}]",
            ):
                dependencies.append(
                    {
                        "dependency_type": "workflow",
                        "ref": str(issue.get("path") or f"workflows[{index}]").strip(),
                        "label": str(issue.get("message") or "Invalid workflow override"),
                        "sources": [str(issue.get("path") or f"workflows[{index}]").strip()],
                        "required": True,
                        "metadata": dict(issue),
                    }
                )
            workflow_id = _text(workflow.get("workflow_id"))
            if not workflow_id:
                dependencies.append(
                    {
                        "dependency_type": "workflow",
                        "ref": f"workflows[{index}]",
                        "label": f"Workflow {index + 1}",
                        "sources": [BUNDLE_WORKFLOWS_VALUE],
                        "required": True,
                        "metadata": {"code": "workflow_id_missing"},
                    }
                )
                continue
            target_id = self._target_workflow_id(
                workflow_id,
                resolution_index=resolution_index,
                explicit_target=target_workflow_id if workflow_id == root_id else "",
            )
            # A carried child whose own id is taken keeps its row once a rename
            # resolves it: the row then reports the accepted new id as resolved.
            # Dropping it lost the rename from every later check and summary.
            renamed_child_of_taken_id = (
                workflow_id != root_id
                and target_id != workflow_id
                and self._workflow_identity_reserved(workflow_id)
            )
            if renamed_child_of_taken_id or self._workflow_identity_reserved(target_id):
                dependencies.append(
                    {
                        "dependency_type": "workflow",
                        "ref": workflow_id,
                        "label": workflow_id,
                        "sources": [BUNDLE_WORKFLOWS_VALUE],
                        "required": True,
                        "metadata": {
                            "code": "workflow_id_conflict",
                            "target_workflow_id": target_id,
                            # A carried child may be reused instead of copied.
                            "bundled_child": workflow_id in child_ids,
                        },
                    }
                )

        preflight_dependencies = [
            self._bundled_child_dependency(item, resolution_index)
            if _text(item.get("dependency_type")) == "child_workflow"
            and _text(item.get("ref")) in carried_ids
            else self._classify_dependency(item, resolution_index)
            for item in self._dedupe_dependency_rows(dependencies)
        ]
        preflight_dependencies.extend(definition_plan["rows"])
        preflight_dependencies.extend(
            self._classify_external_contract(row)
            for row in self._dedupe_dependency_rows(
                [
                    row
                    for row in definition_plan["external_rows"]
                    if _text(row.get("dependency_type")) == "data_contract"
                ]
            )
        )
        hook_bindings = self._hook_binding_plan(
            bundle,
            workflow_entries=workflow_entries,
            resolution_index=resolution_index,
            child_ids=child_ids,
            root_id=root_id,
            explicit_target=target_workflow_id,
        )
        preflight_dependencies.extend(self._hook_binding_dependency_rows(hook_bindings))
        blocking = [item for item in preflight_dependencies if item["blocking"]]
        topology_issues = self._topology_issues(full_workflows)
        topology_issues.extend(
            self._import_hook_authoring_issues(
                full_workflows,
                resolution_index=resolution_index,
                child_ids=child_ids,
                root_id=root_id,
                explicit_target=target_workflow_id,
            )
        )
        blocking_topology = [item for item in topology_issues if item["blocking"]]
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "workflow_import_preflight",
            "ok": not blocking and not blocking_topology,
            "can_apply": not blocking and not blocking_topology,
            "topology_issues": topology_issues,
            "summary": {
                "workflow_count": len(full_workflows),
                "reference_count": len(workflow_entries) - len(full_workflows),
                "dependency_count": len(preflight_dependencies),
                "blocking_count": len(blocking),
            },
            "root_workflow_id": root_id,
            "workflows": [
                {
                    "workflow_id": _text(item.get("workflow_id")),
                    "display_name": _text(item.get("display_name") or item.get("workflow_id")),
                    "portability_kind": _text(
                        item.get("portability_kind") or "workflow_definition"
                    ),
                    "role": _bundle_role(_text(item.get("workflow_id")), root_id, child_ids),
                }
                for item in workflow_entries
            ],
            "dependencies": preflight_dependencies,
            "hook_bindings": hook_bindings,
            "required_resolution_actions": sorted(RESOLUTION_ACTIONS),
        }

    def apply_import(
        self,
        bundle_yaml: str,
        *,
        target_workflow_id: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        author: str = "admin",
        owner_account_id: str = "",
        owner_project_id: str = "",
    ) -> dict[str, Any]:
        preflight = self.preflight_import(
            bundle_yaml,
            target_workflow_id=target_workflow_id,
            resolutions=resolutions or [],
            target_project_id=owner_project_id,
        )
        topology_issues = [
            item
            for item in _as_list(preflight.get("topology_issues"))
            if isinstance(item, dict) and item.get("blocking")
        ]
        if topology_issues:
            # Refuse before anything is written. The same typed codes the contract
            # validator returns are what the caller gets here, so an import that a
            # later validation would reject never becomes a stored draft.
            raise WorkflowPortabilityError(
                "Workflow import defines an invalid topology.",
                code="workflow_import_topology_invalid",
                errors=topology_issues,
            )
        if not preflight.get("can_apply"):
            raise WorkflowPortabilityError(
                "Workflow import has unresolved blocking dependencies.",
                code="workflow_import_preflight_blocked",
                errors=[
                    item
                    for item in _as_list(preflight.get("dependencies"))
                    if isinstance(item, dict) and item.get("blocking")
                ],
            )

        hook_bindings = [
            item for item in _as_list(preflight.get("hook_bindings")) if isinstance(item, dict)
        ]
        if hook_bindings and not _text(owner_project_id):
            # A binding belongs to one project; importing without one would leave
            # every configured hook of the file empty while reporting success.
            raise WorkflowPortabilityError(
                "This file connects hooks in a project; choose the project to import into.",
                code="workflow_import_hook_binding_project_required",
                status_code=422,
                errors=[
                    {"hook_id": item["hook_id"], "owner_workflow_id": item["owner_workflow_id"]}
                    for item in hook_bindings
                ],
            )

        bundle = self.parse_bundle(bundle_yaml)
        resolution_index = self._resolution_index(resolutions or [])
        definition_plan = self._definition_plan(
            bundle, resolution_index, target_project_id=owner_project_id
        )
        workflows = [
            item
            for item in _as_list(bundle.get("workflows"))
            if isinstance(item, dict)
            and _text(item.get("portability_kind")) != "extension_workflow_ref"
        ]
        if not workflows:
            raise WorkflowPortabilityError(
                "Bundle does not contain an importable workflow definition.",
                code="workflow_import_no_definition",
            )
        add_project_binding = None
        if owner_project_id:
            add_project_binding = getattr(self.db, "add_project_workflow_binding", None)
            if not callable(add_project_binding):
                raise WorkflowPortabilityError(
                    "Workflow project binding is unavailable.",
                    code="workflow_import_project_binding_unavailable",
                    status_code=503,
                )

        carried_ids = bundled_workflow_ids(workflows)
        child_ids = bundled_child_ids(workflows) | bound_child_ids(bundle, carried_ids)
        root_id = root_workflow_id(bundle, workflows)
        imported: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        # Where each carried workflow ends up here; a caller's invoke steps follow it.
        placed_ids: dict[str, str] = {}
        with self._import_transaction():
            authoring_issues = self._import_hook_authoring_issues(
                workflows,
                resolution_index=resolution_index,
                child_ids=child_ids,
                root_id=root_id,
                explicit_target=target_workflow_id,
            )
            if authoring_issues:
                raise WorkflowPortabilityError(
                    "Workflow import defines an invalid topology.",
                    code="workflow_import_topology_invalid",
                    errors=authoring_issues,
                )
            resolution_index = self._ensure_resolution_side_effects(resolution_index)
            # The agents and skills the workflows call exist before the workflows.
            definitions = self._apply_definitions(
                definition_plan,
                resolution_index,
                owner_account_id=owner_account_id,
                owner_project_id=owner_project_id,
                author=author,
            )
            # Children first, so a workflow is saved after the ones it calls.
            for workflow in creation_order(workflows):
                source_workflow_id = _text(workflow.get("workflow_id"))
                resolution = resolution_index.get(("workflow", source_workflow_id)) or {}
                if (
                    source_workflow_id in child_ids
                    and _text(resolution.get("action")) == "use_existing"
                ):
                    existing_id = (
                        _text(
                            resolution.get("target_workflow_id")
                            or resolution.get("target_ref")
                            or resolution.get("target_id")
                        )
                        or source_workflow_id
                    )
                    if not self._workflow_exists(existing_id):
                        raise WorkflowPortabilityError(
                            f"Workflow '{existing_id}' does not exist to reuse.",
                            code="workflow_import_reuse_missing",
                            status_code=409,
                        )
                    placed_ids[source_workflow_id] = existing_id
                    reused.append(
                        {"workflow_id": existing_id, "source_workflow_id": source_workflow_id}
                    )
                    continue
                target_id = self._target_workflow_id(
                    source_workflow_id,
                    resolution_index=resolution_index,
                    explicit_target=target_workflow_id if source_workflow_id == root_id else "",
                )
                conflict_error = WorkflowPortabilityError(
                    f"Workflow '{target_id}' already exists.",
                    code="workflow_id_conflict",
                    status_code=409,
                    errors=[
                        {
                            "dependency_type": "workflow",
                            "ref": source_workflow_id,
                            "status": "conflict",
                            "code": "workflow_id_conflict",
                            "target_workflow_id": target_id,
                        }
                    ],
                )
                if self._workflow_identity_reserved(target_id):
                    raise conflict_error
                payload = self._import_payload(
                    workflow,
                    target_id,
                    _with_carried_children(resolution_index, placed_ids, carried_ids),
                    owner_account_id=owner_account_id,
                    owner_project_id=owner_project_id,
                )
                try:
                    create_workflow_with_definition(self.db, payload, author=author)
                except WorkflowCreateConflictError as exc:
                    raise conflict_error from exc
                if add_project_binding is not None:
                    add_project_binding(owner_project_id, target_id)
                placed_ids[source_workflow_id] = target_id
                imported.append(
                    {
                        "workflow_id": target_id,
                        "source_workflow_id": source_workflow_id,
                        "enabled": False,
                        "import_state": "draft",
                        "role": _bundle_role(source_workflow_id, root_id, child_ids),
                    }
                )
            # After every workflow exists, in the same transaction: a binding the
            # save refuses undoes the workflows above as well.
            restored_bindings = self._apply_hook_bindings(
                hook_bindings,
                placed_ids=placed_ids,
                created_ids={item["workflow_id"] for item in imported},
                account_id=owner_account_id,
                project_id=owner_project_id,
                author=author,
            )

        root_target = placed_ids.get(root_id) or (imported[0]["workflow_id"] if imported else "")
        return {
            "status": "imported",
            "workflow_id": root_target,
            "imported_workflows": imported,
            "reused_workflows": reused,
            "hook_bindings": restored_bindings,
            "project_id": owner_project_id,
            "definitions": definitions,
            "preflight": preflight,
        }
