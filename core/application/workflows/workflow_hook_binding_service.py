"""Save a project's hook binding as one checked transaction (spec §9.2, §12).

A save names the hook, the child workflow (or none, to clear it), the
mappings ``child input ← hook input`` and ``child output → hook target``, and
the binding revision it edits. Everything is checked before anything is
written: the child is an available callable workflow of the same account, the
mappings fit both contracts, and the chain the child starts neither recurses,
nor exceeds the invocation depth, nor waits where the hook runs in a parallel
lane. A configured target that fails a check is an error, never an empty hook.

A hook is a place on the canvas for steps. Its steps are stored in a per-project
workflow the builder draws inside the hook frame and never names: the first
step added to an empty hook creates it empty (only its hidden end,
:data:`HOOK_END_STEP_ID`) and binds it; repeating the same request reuses that
workflow instead of creating a second one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.registry.workflow_lookup import get_workflow_row
from core.application.workflows.workflow_definition_validation import (
    ChildDefinition,
    scan_child_chain,
    validate_workflow_definition,
)
from core.application.workflows.workflow_hook_binding_repository import (
    WorkflowHookBindingRepository,
)
from core.application.workflows.workflow_hook_contract import (
    HOOK_CONFIG_KEY,
    HOOK_END_STEP_ID,
    HOOK_STORAGE_SETTING,
    HookContractError,
    HookDeclaration,
    hook_semantic_digest,
    hook_storage_marker,
    parse_hook_declaration,
    target_issue,
)
from core.application.workflows.workflow_invocation_target import (
    BindingSource,
    HookBindingRevision,
    HookOwner,
    hook_owner_for,
)

MAX_INVOKE_DEPTH = 5


class HookBindingError(ValueError):
    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__("; ".join(issue["message"] for issue in issues))
        self.issues = issues


def _issue(code: str, message: str, path: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class HookBindingRequest:
    owner_workflow_id: str
    project_id: str
    hook_id: str
    expected_revision: int
    target_workflow_id: str | None
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    #: The hook contract the client reviewed; a changed contract is refused.
    hook_contract_version: int | None = None
    hook_semantic_digest: str | None = None
    #: Prepare the binding for this extension release instead of now: it is
    #: checked against the release's declaration and activates with it.
    release_id: str | None = None


class _Overlay:
    """The current bindings with the one being saved in place, for the chain check."""

    def __init__(
        self, base: BindingSource, owner: HookOwner, hook_id: str, candidate: HookBindingRevision
    ) -> None:
        self._base, self._key, self._candidate = base, (owner, hook_id), candidate

    def binding(self, owner: HookOwner, hook_id: str) -> HookBindingRevision | None:
        if (owner, hook_id) == self._key:
            return self._candidate
        return self._base.binding(owner, hook_id)


def _definition(workflow: Mapping[str, Any]) -> dict[str, Any]:
    from core.application.workflows.workflow_definition_snapshot import frozen_definition

    return frozen_definition(workflow)


def find_hook(workflow: Mapping[str, Any], hook_id: str) -> tuple[str, HookDeclaration]:
    """The step id and declaration of *hook_id* in an author workflow."""
    for step in workflow.get("steps") or []:
        override = step.get("override") if isinstance(step, Mapping) else None
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        raw = config.get(HOOK_CONFIG_KEY) if isinstance(config, Mapping) else None
        if isinstance(raw, Mapping) and raw.get("hook_id") == hook_id:
            try:
                return _text(step.get("step_id")), parse_hook_declaration(raw)
            except HookContractError as exc:
                raise HookBindingError([exc.issue()]) from exc
    raise HookBindingError(
        [_issue("workflow.hook.unknown", f"The workflow declares no hook '{hook_id}'", "hook_id")]
    )


def _child_contract(child: Mapping[str, Any]) -> tuple[set[str] | None, set[str], set[str]]:
    """(input names or None for passthrough, required inputs, output names) of a child."""
    from core.application.registry.workflow_contract import WorkflowInterface

    interface = WorkflowInterface.from_settings(child.get("settings"))
    outputs = {row.name for row in interface.expected_outputs}
    if interface.input_mode == "passthrough":
        return None, set(), outputs
    if interface.input_mode == "json_schema":
        schema = interface.input_schema if isinstance(interface.input_schema, Mapping) else {}
        properties = schema.get("properties")
        names = set(properties) if isinstance(properties, Mapping) else set()
        return names, {str(item) for item in schema.get("required") or []}, outputs
    names = {row.name for row in interface.expected_inputs}
    required = {row.name for row in interface.expected_inputs if row.required}
    return names, required, outputs


def mapping_compatibility(
    input_mapping: Mapping[str, Any], output_mapping: Mapping[str, Any], child_settings: Any
) -> list[str]:
    """Why mappings ``child input ← …`` / ``child output → …`` no longer fit a child.

    The same rule for a hook binding and a direct Invoke step: every mapped
    child input still exists, every required child input is mapped, every
    mapped child output is still declared.
    """
    inputs, required, outputs = _child_contract({"settings": child_settings})
    mapped_inputs = {str(name) for name in input_mapping}
    issues: list[str] = []
    if inputs is not None:
        issues.extend(
            f"the child no longer declares input '{name}'"
            for name in sorted(mapped_inputs - inputs)
        )
    issues.extend(
        f"the child requires input '{name}', which is not mapped"
        for name in sorted(required - mapped_inputs)
    )
    issues.extend(
        f"the child no longer declares output '{name}'"
        for name in sorted({str(name) for name in output_mapping} - outputs)
    )
    return issues


def _target_issues(
    child: Mapping[str, Any] | None, *, account_id: str, target: str, draft: bool = False
) -> list:
    from core.application.registry.workflow_contract import WorkflowInterface

    if child is None or child.get("deleted_at"):
        return [_issue("workflow.hook.target_missing", f"Workflow '{target}' does not exist")]
    if _text(child.get("account_id")) and _text(child.get("account_id")) != account_id:
        return [_issue("workflow.hook.target_forbidden", f"Workflow '{target}' is not available")]
    if not child.get("enabled") and not draft:
        return [_issue("workflow.hook.target_disabled", f"Workflow '{target}' is disabled")]
    interface = WorkflowInterface.from_settings(child.get("settings"))
    if not interface.enabled or (
        interface.trigger_channels and "parent_workflow" not in interface.trigger_channels
    ):
        return [
            _issue(
                "workflow.hook.target_not_callable",
                f"Workflow '{target}' cannot be called by another workflow",
            )
        ]
    return []


#: The JSON types a value of a declared Interface field type can have.
_FIELD_JSON_TYPES: dict[str, frozenset[str]] = {
    "string": frozenset({"string"}),
    "paragraph": frozenset({"string"}),
    "select": frozenset({"string"}),
    "number": frozenset({"number", "integer"}),
    "boolean": frozenset({"boolean"}),
    "object": frozenset({"object"}),
    "array": frozenset({"array"}),
}


def _schema_types(schema: Any) -> frozenset[str] | None:
    """The JSON types a schema allows besides null, or None when it names none."""
    kind = schema.get("type") if isinstance(schema, Mapping) else None
    if isinstance(kind, str):
        kinds = [kind]
    elif isinstance(kind, list):
        kinds = kind
    else:
        kinds = []
    allowed = {str(item) for item in kinds} - {"null"}
    if "number" in allowed:
        allowed.add("integer")
    return frozenset(allowed) or None


def _child_field_types(
    child: Mapping[str, Any],
) -> tuple[dict[str, frozenset[str] | None], dict[str, frozenset[str] | None]]:
    """The JSON types of each declared child input and output (None: not restricted)."""
    from core.application.registry.workflow_contract import WorkflowInterface

    interface = WorkflowInterface.from_settings(child.get("settings"))
    outputs = {row.name: _FIELD_JSON_TYPES.get(row.type) for row in interface.expected_outputs}
    if interface.input_mode == "json_schema":
        schema = interface.input_schema if isinstance(interface.input_schema, Mapping) else {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        return {str(name): _schema_types(spec) for name, spec in properties.items()}, outputs
    inputs = {row.name: _FIELD_JSON_TYPES.get(row.type) for row in interface.expected_inputs}
    return inputs, outputs


def _type_issues(
    hook: HookDeclaration, child: Mapping[str, Any], request: HookBindingRequest
) -> list[dict[str, str]]:
    """Mapped values whose declared types can never fit, refused before any run."""
    input_types, output_types = _child_field_types(child)
    hook_inputs = hook.input_schema.get("properties") or {}
    hook_fields = hook.output_schema.get("properties") or {}
    issues: list[dict[str, str]] = []
    for child_input, hook_input in request.input_mapping.items():
        source = _schema_types(hook_inputs.get(hook_input))
        target = input_types.get(child_input)
        if source and target and not source & target:
            issues.append(
                _issue(
                    "workflow.hook.binding_input_type_mismatch",
                    f"Hook input '{hook_input}' is {'/'.join(sorted(source))}, but child "
                    f"input '{child_input}' takes {'/'.join(sorted(target))}",
                    f"input_mapping.{child_input}",
                )
            )
    for child_output, hook_target in request.output_mapping.items():
        source = output_types.get(child_output)
        target = _schema_types(hook_fields.get(hook_target))
        if source and target and not source & target:
            issues.append(
                _issue(
                    "workflow.hook.binding_output_type_mismatch",
                    f"Child output '{child_output}' is {'/'.join(sorted(source))}, but hook "
                    f"field '{hook_target}' takes {'/'.join(sorted(target))}",
                    f"output_mapping.{child_output}",
                )
            )
    return issues


def _mapping_issues(
    hook: HookDeclaration, child: Mapping[str, Any], request: HookBindingRequest
) -> list[dict[str, str]]:
    inputs, required, outputs = _child_contract(child)
    issues: list[dict[str, str]] = []
    for child_input, hook_input in request.input_mapping.items():
        if inputs is not None and child_input not in inputs:
            issues.append(
                _issue(
                    "workflow.hook.binding_input_unknown",
                    f"The child declares no input '{child_input}'",
                    f"input_mapping.{child_input}",
                )
            )
        if hook_input not in hook.input_names:
            issues.append(
                _issue(
                    "workflow.hook.binding_input_source_unknown",
                    f"'{hook_input}' is not a published input of hook '{hook.hook_id}'",
                    f"input_mapping.{child_input}",
                )
            )
    for name in sorted(required - set(request.input_mapping)):
        issues.append(
            _issue(
                "workflow.hook.binding_input_required",
                f"The child requires input '{name}'; map a hook input to it",
                f"input_mapping.{name}",
            )
        )
    for child_output, hook_target in request.output_mapping.items():
        if child_output not in outputs:
            issues.append(
                _issue(
                    "workflow.hook.binding_output_unknown",
                    f"The child declares no output '{child_output}'",
                    f"output_mapping.{child_output}",
                )
            )
        problem = target_issue(hook, hook_target)
        if problem is not None:
            issues.append(problem.issue())
    return [*issues, *_type_issues(hook, child, request)]


class WorkflowHookBindingService:
    def __init__(self, db: Any, *, account_id: str, actor: str = "") -> None:
        self.db = db
        self.account_id = _text(account_id)
        self.actor = actor

    def _owner(self, workflow: Mapping[str, Any], project_id: str) -> HookOwner:
        return hook_owner_for(workflow, account_id=self.account_id, project_id=project_id)

    def _workflow(self, workflow_id: str) -> Mapping[str, Any] | None:
        """The workflow as the account sees it, an installed extension's included.

        An installed extension workflow's platform row is only a projection
        anchor without steps; its definition is the projected row, the same view
        every API read path uses (``get_workflow_row``).
        """
        return get_workflow_row(self.db, workflow_id, account_id=self.account_id)

    def _author(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        if not isinstance(workflow, Mapping) or (
            _text(workflow.get("account_id"))
            and _text(workflow.get("account_id")) != self.account_id
        ):
            raise HookBindingError(
                [_issue("workflow.not_found", f"Workflow '{workflow_id}' not found", "workflow_id")]
            )
        return dict(workflow)

    def _load_child(self, owner_project: str):
        def load(child_id: str) -> ChildDefinition | None:
            child = self._workflow(child_id)
            if not isinstance(child, Mapping):
                return None
            return ChildDefinition(
                child_id,
                int(child.get("revision") or 0),
                _definition(child),
                owner=hook_owner_for(child, account_id=self.account_id, project_id=owner_project),
            )

        return load

    def _chain_issues(
        self,
        author: Mapping[str, Any],
        owner: HookOwner,
        hook_id: str,
        candidate: HookBindingRevision,
        repository: WorkflowHookBindingRepository,
    ) -> list[dict[str, str]]:
        bindings = _Overlay(repository, owner, hook_id, candidate)
        load_child = self._load_child(owner.project_id)
        target = candidate.target_workflow_id or ""
        issues = list(
            scan_child_chain(
                target,
                load_child,
                max_depth=MAX_INVOKE_DEPTH,
                ancestry=[_text(author.get("workflow_id"))],
                bindings=bindings,
            ).issues
        )
        # A hook in a parallel lane: the child chain may not wait anywhere.
        issues.extend(
            issue
            for issue in validate_workflow_definition(
                _definition(author),
                load_child=load_child,
                max_invoke_depth=MAX_INVOKE_DEPTH,
                owner=owner,
                bindings=bindings,
            )
            if issue["code"] == "workflow.placement.lane_child_waits"
        )
        return issues

    def _check(
        self,
        request: HookBindingRequest,
        repository: WorkflowHookBindingRepository,
        release_workflow: Mapping[str, Any] | None,
        draft_targets: Collection[str] = (),
    ) -> tuple[HookOwner, HookDeclaration, str, str | None, list[dict[str, str]]]:
        """``(owner, hook, digest, target, issues)``; refusals that need no save raise."""
        if request.release_id and not isinstance(release_workflow, Mapping):
            raise HookBindingError(
                [
                    _issue(
                        "workflow.hook.release_unknown",
                        f"Release '{request.release_id}' declares no such workflow",
                        "release_id",
                    )
                ]
            )
        if not _text(request.project_id):
            raise HookBindingError(
                [_issue("workflow.hook.project_required", "A binding belongs to one project")]
            )
        author = self._author(request.owner_workflow_id)
        owner = self._owner(author, request.project_id)
        declared = dict(release_workflow) if request.release_id and release_workflow else author
        _step_id, hook = find_hook(declared, request.hook_id)
        digest = hook_semantic_digest(hook)
        if (
            request.hook_contract_version is not None
            and request.hook_contract_version != hook.contract_version
        ) or (request.hook_semantic_digest is not None and request.hook_semantic_digest != digest):
            raise HookBindingError(
                [
                    _issue(
                        "workflow.hook.contract_changed",
                        f"Hook '{hook.hook_id}' changed since it was reviewed; "
                        "review the new contract",
                    )
                ]
            )
        target = _text(request.target_workflow_id) or None
        issues: list[dict[str, str]] = []
        if target is not None:
            child = self._workflow(target)
            issues = _target_issues(
                child, account_id=self.account_id, target=target, draft=target in draft_targets
            )
            if not issues and child is not None:
                issues = _mapping_issues(hook, child, request)
            if not issues:
                candidate = HookBindingRevision(
                    id="candidate",
                    binding_id="candidate",
                    revision=request.expected_revision + 1,
                    account_id=owner.account_id,
                    project_id=owner.project_id,
                    target_workflow_id=target,
                    input_mapping=dict(request.input_mapping),
                    output_mapping=dict(request.output_mapping),
                    hook_contract_version=hook.contract_version,
                    hook_semantic_digest=digest,
                )
                issues = self._chain_issues(
                    {**author, "steps": declared.get("steps") or []},
                    owner,
                    hook.hook_id,
                    candidate,
                    repository,
                )
        return owner, hook, digest, target, issues

    def preview(
        self,
        request: HookBindingRequest,
        *,
        release_workflow: Mapping[str, Any] | None = None,
        sample: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Every check of :meth:`save`, without writing: the issues or "valid".

        With a *sample* (hook inputs and a child result), a binding that passes
        also returns ``sample_output``: what a run would publish for them.
        """
        try:
            with self.db._transaction() as cur:
                repository = WorkflowHookBindingRepository(cur.connection)
                owner, hook, digest, target, issues = self._check(
                    request, repository, release_workflow
                )
                current = repository.binding_revision(owner, hook.hook_id)
        except HookBindingError as exc:
            return {"valid": False, "issues": exc.issues}
        if current != request.expected_revision:
            issues = [
                *issues,
                _issue(
                    "workflow.hook.binding_revision_changed",
                    f"The binding is at revision {current}, not {request.expected_revision}",
                    "expected_revision",
                ),
            ]
        output: dict[str, Any] | None = None
        if sample is not None and not issues:
            try:
                output = sample_hook_output(hook, request.output_mapping, target, sample)
            except HookContractError as exc:
                issues = [exc.issue()]
        return {
            "valid": not issues,
            "issues": issues,
            **({"sample_output": output} if output is not None else {}),
            "hook": hook.dump(),
            "hook_semantic_digest": digest,
            "binding_revision": current,
            "target_workflow_id": target,
        }

    def save(
        self,
        request: HookBindingRequest,
        *,
        release_workflow: Mapping[str, Any] | None = None,
        draft_targets: Collection[str] = (),
    ) -> HookBindingRevision:
        """Check the whole binding, then write its next revision, or nothing.

        A staged save (``request.release_id``) reads the hook from
        *release_workflow*, the release's own workflow artifact.

        *draft_targets* are workflows the caller's own transaction has just
        created as disabled drafts (a workflow import): one of them may be the
        target while still disabled. Every other check runs, and a run refuses
        the disabled child until someone enables it.
        """
        with self.db._transaction() as cur:
            repository = WorkflowHookBindingRepository(cur.connection)
            owner, hook, digest, target, issues = self._check(
                request, repository, release_workflow, draft_targets
            )
            if issues:
                raise HookBindingError(issues)
            return repository.save(
                owner,
                hook.hook_id,
                expected_revision=request.expected_revision,
                target_workflow_id=target,
                input_mapping=request.input_mapping,
                output_mapping=request.output_mapping,
                hook_contract_version=hook.contract_version,
                hook_semantic_digest=digest,
                created_by=self.actor,
                staged_release_id=request.release_id or None,
            )

    def describe(self, owner_workflow_id: str, project_id: str) -> list[dict[str, Any]]:
        """The hooks a workflow declares, each with its binding in *project_id*."""
        author = self._author(owner_workflow_id)
        owner = self._owner(author, project_id)
        hooks: list[tuple[str, HookDeclaration]] = []
        for step in author.get("steps") or []:
            override = step.get("override") if isinstance(step, Mapping) else None
            config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
            raw = config.get(HOOK_CONFIG_KEY) if isinstance(config, Mapping) else None
            if isinstance(raw, Mapping):
                try:
                    hooks.append((_text(step.get("step_id")), parse_hook_declaration(raw)))
                except HookContractError:
                    continue
        with self.db._transaction() as cur:
            repository = WorkflowHookBindingRepository(cur.connection)
            active = repository.active(owner, [hook.hook_id for _, hook in hooks])
            revisions = {
                hook.hook_id: repository.binding_revision(owner, hook.hook_id) for _, hook in hooks
            }
        described: list[dict[str, Any]] = []
        for step_id, hook in hooks:
            binding = active.get(hook.hook_id)
            described.append(
                {
                    "hook_id": hook.hook_id,
                    "step_id": step_id,
                    "display_name": hook.display_name,
                    "description": hook.description,
                    "contract_version": hook.contract_version,
                    "hook_semantic_digest": hook_semantic_digest(hook),
                    "contract": hook.dump(),
                    "binding_revision": revisions.get(hook.hook_id, 0),
                    "binding": (
                        {
                            "revision_id": binding.id,
                            "target_workflow_id": binding.target_workflow_id,
                            "input_mapping": dict(binding.input_mapping),
                            "output_mapping": dict(binding.output_mapping),
                            "hook_contract_version": binding.hook_contract_version,
                            "hook_semantic_digest": binding.hook_semantic_digest,
                        }
                        if binding is not None and binding.target_workflow_id
                        else None
                    ),
                }
            )
        return described

    def create_child_and_bind(
        self,
        *,
        owner_workflow_id: str,
        project_id: str,
        hook_id: str,
        expected_revision: int,
        child_workflow_id: str,
        display_name: str = "",
    ) -> HookBindingRevision:
        """The first step added to an empty hook: its empty storage workflow, then bound.

        The child, its project link and its binding are written in one
        transaction, so a child id the binding already names was created by an earlier identical
        request: that revision is returned and no second child is created.
        An existing workflow the binding does not name is never taken over.
        """
        from core.application.workflows.workflow_definition_service import (
            create_workflow_with_definition,
        )

        with self.db._transaction() as cur:
            author = self._author(owner_workflow_id)
            owner = self._owner(author, project_id)
            _step_id, hook = find_hook(author, hook_id)
            existing = self.db.get_workflow(child_workflow_id)
            if isinstance(existing, Mapping):
                current = WorkflowHookBindingRepository(cur.connection).binding(owner, hook_id)
                if current is not None and current.target_workflow_id == child_workflow_id:
                    return current
                raise HookBindingError(
                    [
                        _issue(
                            "workflow.hook.child_id_taken",
                            f"Workflow '{child_workflow_id}' already exists and was not "
                            "created for this hook; choose it as an existing workflow",
                            "child_workflow_id",
                        )
                    ]
                )
            child = _storage_child(
                child_workflow_id,
                hook,
                display_name,
                self.account_id,
                author_workflow_id=owner_workflow_id,
                project_id=project_id,
            )
            create_workflow_with_definition(self.db, child, author=self.actor or "hook")
            # The binding is the project's own, so its child belongs to that project.
            self.db.add_project_workflow_binding(project_id, child_workflow_id)
            return self.save(
                HookBindingRequest(
                    owner_workflow_id=owner_workflow_id,
                    project_id=project_id,
                    hook_id=hook_id,
                    expected_revision=expected_revision,
                    target_workflow_id=child_workflow_id,
                    input_mapping={name: name for name in sorted(hook.input_names)},
                    # Every result is read by its own name from the hook's state at
                    # its end; one no step set falls back to the hook input.
                    output_mapping=same_name_output_mapping(child, hook),
                    hook_contract_version=hook.contract_version,
                    hook_semantic_digest=hook_semantic_digest(hook),
                )
            )


def sample_hook_output(
    hook: HookDeclaration,
    output_mapping: Mapping[str, str],
    target_workflow_id: str | None,
    sample: Mapping[str, Any],
) -> dict[str, Any]:
    """The hook output a run would publish for sample inputs and a child result.

    The same rules as the Invoke runtime: inputs are checked against the hook
    contract, the mapped child outputs are selected, then the whole output is
    normalized and validated. No child runs here: the caller brings its result
    (for example from the child's Test Run).
    """
    from core.application.workflows.workflow_hook_contract import (
        _schema_errors,
        normalize_hook_output,
        select_hook_targets,
    )

    inputs = dict(sample.get("inputs") or {})
    problems = _schema_errors(hook.input_schema, inputs)
    if problems:
        raise HookContractError(
            "workflow.hook.input_invalid",
            f"The hook inputs do not match the hook contract: {problems[0]}",
            "sample.inputs",
        )
    targets = (
        select_hook_targets(dict(output_mapping), dict(sample.get("child_outputs") or {}))
        if target_workflow_id
        else None
    )
    return normalize_hook_output(hook, inputs, targets)


def same_name_output_mapping(child: Mapping[str, Any], hook: HookDeclaration) -> dict[str, str]:
    """Each child output written back to the hook field of exactly the same name.

    Only fields the hook lets its steps write -- its mutable fields and its
    output-only fields -- are mapped; any other output stays unmapped.
    """
    callable_settings = (child.get("settings") or {}).get("callable") or {}
    writable = {*hook.mutable_fields, *hook.additional_outputs}
    names = [_text(row.get("name")) for row in callable_settings.get("expected_outputs") or []]
    return {name: name for name in names if name and name in writable}


def _storage_child(
    workflow_id: str,
    hook: HookDeclaration,
    display_name: str,
    account_id: str,
    *,
    author_workflow_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    """The empty, enabled callable workflow that holds a hook's steps.

    It is marked ``settings.hook_storage`` (author workflow, hook, project) and
    kept out of the catalog: lists and pickers a person browses never show it.

    Its inputs are the hook inputs; its outputs are every field the hook lets
    its steps write, read by name from its state at its end (``variables``),
    all optional. Its graph is only the hidden end step: structurally valid,
    and while it holds nothing else the hook runs nothing and passes its inputs
    through (``holds_no_steps``). Once it has steps, a result no step sets falls back to
    the hook input of that name, or to the output-only field's default.
    """
    properties = dict(hook.input_schema.get("properties") or {})
    required = {str(name) for name in hook.input_schema.get("required") or []}
    inputs = [
        {"name": name, "type": _field_type(schema), "required": name in required}
        for name, schema in properties.items()
    ]
    outputs = [
        *(
            {"name": name, "type": _field_type(properties.get(name, {})), "required": False}
            for name in hook.mutable_fields
        ),
        *(
            {"name": name, "type": _field_type(spec["schema"]), "required": False}
            for name, spec in hook.additional_outputs.items()
        ),
    ]
    return {
        "workflow_id": workflow_id,
        "display_name": display_name or hook.display_name,
        "description": hook.description,
        "enabled": True,
        "account_id": account_id,
        "scope_kind": "account",
        # A place inside the author workflow, never a workflow a person browses.
        "visible_in_catalog": False,
        "settings": {
            HOOK_STORAGE_SETTING: hook_storage_marker(
                author_workflow_id=author_workflow_id, hook_id=hook.hook_id, project_id=project_id
            ),
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "input_mode": "fields",
                "expected_inputs": inputs,
                "output_mode": "variables" if outputs else "none",
                "expected_outputs": outputs,
            },
        },
        "steps": [{"step_id": HOOK_END_STEP_ID, "step_kind": "stop_success"}],
        "groups": [],
        "for_each": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": {"kind": "step", "id": HOOK_END_STEP_ID},
            "edges": [],
        },
    }


def _field_type(schema: Any) -> str:
    kind = schema.get("type") if isinstance(schema, Mapping) else None
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    return {"integer": "number"}.get(str(kind or "string"), str(kind or "string"))


__all__ = [
    "HookBindingError",
    "HookBindingRequest",
    "WorkflowHookBindingService",
    "find_hook",
    "mapping_compatibility",
    "same_name_output_mapping",
    "sample_hook_output",
]
