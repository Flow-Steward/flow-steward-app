"""The effective target of an Invoke step: a direct workflow or a hook binding.

One resolver for every consumer — runtime, the child-chain scan (recursion,
depth, lane waits), compatibility checks and where-used — so a hook is never
walked by a second, hook-only scanner (spec §12).

* A direct Invoke targets its ``workflow_id``.
* A hook targets what the binding of *its* project names: the current binding
  for authoring and preflight, the job's pinned binding at run time. No
  binding, or a cleared one, is an empty hook — no call at all, never a
  "missing target" error. A configured target that is missing, disabled or
  incompatible is an ordinary error of whoever uses it.
* Bindings are scoped to ``(account, project, source install, local workflow,
  hook id)``; there is no account-wide fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.application.workflows.workflow_hook_contract import (
    DIRECT_TARGET_KEY,
    HOOK_CONFIG_KEY,
    HookContractError,
    HookDeclaration,
    invoke_config_issues,
    parse_hook_declaration,
)

#: ``jobs.meta`` key of the binding revision (or explicit ``null``) a job runs
#: each hook with, fixed in the transaction that pins its definition.
HOOK_BINDING_PINS_META_KEY = "hook_binding_pins"


class InvocationTargetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HookOwner:
    """Where a hook's bindings live: one project of one account, one author workflow."""

    account_id: str
    project_id: str
    source_install_id: str
    local_workflow_id: str


@dataclass(frozen=True)
class HookBindingRevision:
    """One immutable revision of a project's binding; ``target`` None = cleared."""

    id: str
    binding_id: str
    revision: int
    account_id: str
    project_id: str
    target_workflow_id: str | None
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    hook_contract_version: int = 1
    hook_semantic_digest: str = ""


class BindingSource(Protocol):
    def binding(self, owner: HookOwner, hook_id: str) -> HookBindingRevision | None: ...


@dataclass(frozen=True)
class InvocationTarget:
    kind: str  # "direct" or "hook"
    step_id: str = ""
    workflow_id: str | None = None
    hook: HookDeclaration | None = None
    binding: HookBindingRevision | None = None

    @property
    def empty(self) -> bool:
        """A hook without a configured target: the step calls nothing."""
        return self.kind == "hook" and self.workflow_id is None


class MappingBindings:
    """Bindings held in memory, keyed by ``(owner, hook_id)`` (current authoring view)."""

    def __init__(self, bindings: Mapping[tuple[HookOwner, str], HookBindingRevision]) -> None:
        self._bindings = dict(bindings)

    def binding(self, owner: HookOwner, hook_id: str) -> HookBindingRevision | None:
        return self._bindings.get((owner, hook_id))


class PinnedBindings:
    """The binding revisions a job pinned at submission; the registry is never read.

    A hook missing from the pin map is an integrity error: every hook of the
    pinned definition has an entry, ``None`` meaning "no binding when the job
    was submitted".
    """

    def __init__(
        self,
        pins: Mapping[str, str | None],
        *,
        load_revision: Callable[[str], HookBindingRevision | None],
    ) -> None:
        self._pins = dict(pins)
        self._load = load_revision

    def binding(self, owner: HookOwner, hook_id: str) -> HookBindingRevision | None:
        if hook_id not in self._pins:
            raise InvocationTargetError(
                "workflow.hook.binding_not_pinned",
                f"The run has no pinned binding for hook '{hook_id}'; start a new run",
            )
        revision_id = self._pins[hook_id]
        if revision_id is None:
            return None
        revision = self._load(str(revision_id))
        if revision is None:
            raise InvocationTargetError(
                "workflow.hook.binding_revision_missing",
                f"The pinned binding revision of hook '{hook_id}' is not available",
            )
        if (revision.account_id, revision.project_id) != (owner.account_id, owner.project_id):
            raise InvocationTargetError(
                "workflow.hook.binding_scope_mismatch",
                f"The pinned binding of hook '{hook_id}' belongs to another project",
            )
        return revision


def _text(value: Any) -> str:
    return str(value or "").strip()


def hook_owner_for(workflow: Mapping[str, Any], *, account_id: Any, project_id: Any) -> HookOwner:
    """The binding scope of an author workflow row in one project.

    An extension workflow is identified by its install and local id, never by
    its projected id or a release UUID; an account's own workflow by its id.
    """
    source_install_id = _text(workflow.get("source_install_id"))
    local_workflow_id = (
        _text(workflow.get("local_workflow_id")) if source_install_id else ""
    ) or _text(workflow.get("workflow_id"))
    return HookOwner(_text(account_id), _text(project_id), source_install_id, local_workflow_id)


def resolve_invocation_target(
    config: Mapping[str, Any],
    *,
    owner: HookOwner | None,
    bindings: BindingSource | None,
    step_id: str = "",
) -> InvocationTarget:
    """The target one Invoke config calls in *owner*'s project."""
    source = dict(config or {})
    issues = invoke_config_issues(source)
    if issues:
        raise InvocationTargetError(issues[0]["code"], issues[0]["message"])
    if HOOK_CONFIG_KEY not in source:
        return InvocationTarget("direct", step_id, _text(source.get(DIRECT_TARGET_KEY)))
    try:
        hook = parse_hook_declaration(source[HOOK_CONFIG_KEY])
    except HookContractError as exc:  # pragma: no cover - invoke_config_issues checked it
        raise InvocationTargetError(exc.code, str(exc)) from exc
    if owner is None or bindings is None:
        raise InvocationTargetError(
            "workflow.hook.project_required",
            f"Hook '{hook.hook_id}' is bound per project; its target needs the project",
        )
    binding = bindings.binding(owner, hook.hook_id)
    target = binding.target_workflow_id if binding is not None else None
    return InvocationTarget("hook", step_id, target or None, hook, binding)


def definition_invocation_targets(
    definition: Mapping[str, Any],
    *,
    owner: HookOwner | None,
    bindings: BindingSource | None,
) -> list[InvocationTarget]:
    """The target of every Invoke step of *definition*, empty hooks included."""
    targets: list[InvocationTarget] = []
    for step in definition.get("steps") or []:
        if not isinstance(step, Mapping) or _text(step.get("step_kind")) != "invoke_workflow":
            continue
        override = step.get("override")
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        if not isinstance(config, Mapping):
            config = {DIRECT_TARGET_KEY: step.get("workflow_id")}
        targets.append(
            resolve_invocation_target(
                config, owner=owner, bindings=bindings, step_id=_text(step.get("step_id"))
            )
        )
    return targets


def hook_ids(definition: Mapping[str, Any]) -> list[str]:
    """The hook ids a definition declares, in step order."""
    found: list[str] = []
    for step in definition.get("steps") or []:
        if not isinstance(step, Mapping) or _text(step.get("step_kind")) != "invoke_workflow":
            continue
        override = step.get("override")
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        hook = config.get(HOOK_CONFIG_KEY) if isinstance(config, Mapping) else None
        hook_id = hook.get("hook_id") if isinstance(hook, Mapping) else None
        if isinstance(hook_id, str) and hook_id not in found:
            found.append(hook_id)
    return found


__all__ = [
    "HOOK_BINDING_PINS_META_KEY",
    "BindingSource",
    "HookBindingRevision",
    "HookOwner",
    "InvocationTarget",
    "InvocationTargetError",
    "MappingBindings",
    "PinnedBindings",
    "definition_invocation_targets",
    "hook_ids",
    "hook_owner_for",
    "resolve_invocation_target",
]
