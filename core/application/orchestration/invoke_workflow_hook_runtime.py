"""A hook runs as an ordinary Invoke (spec §8-11).

The hook's target is the binding revision the job pinned at submission — the
registry binding is never read at run time. Its published inputs come from
the author state through the declaration's ``input_mapping``. Then:

* an empty hook calls nothing: its output is the inputs plus declared
  defaults, and the step succeeds;
* a configured hook becomes a direct Invoke of the bound child with the hook
  inputs as literal child inputs; the existing child lifecycle (submission,
  durable wait, resume adoption, cancellation) is unchanged. The full child
  result is validated by the child's own Interface first, then the binding's
  output mapping selects the hook fields and the whole hook output is
  validated before it is published.

The output is ``state.hooks.<hook_id>`` = ``{"output": …, "step_execution_id": …}``
in the declared ``workflow_data`` channel: the original author variables are
never changed, and the producing execution is recorded with the value.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from core.application.workflows.workflow_hook_contract import (
    HOOK_CONFIG_KEY,
    HOOK_END_STEP_ID,
    HookContractError,
    HookDeclaration,
    is_hook_storage,
    normalize_hook_output,
    parse_hook_declaration,
    select_hook_targets,
)
from core.application.workflows.workflow_invocation_target import (
    HOOK_BINDING_PINS_META_KEY,
    HookBindingRevision,
    InvocationTargetError,
    PinnedBindings,
    hook_owner_for,
)

HOOKS_STATE_ROOT = "hooks"


def holds_no_steps(workflow: Mapping[str, Any] | None) -> bool:
    """Whether a hook's storage workflow is still empty: only its hidden end (R2-11).

    Such a workflow has nothing to run, so the hook passes its inputs through
    exactly like a hook with no binding, and the workflow itself never runs.
    """
    if workflow is None or not is_hook_storage(workflow):
        return False
    steps = [step for step in workflow.get("steps") or [] if isinstance(step, Mapping)]
    return (
        len(steps) == 1
        and steps[0].get("step_id") == HOOK_END_STEP_ID
        and steps[0].get("step_kind") == "stop_success"
        and not workflow.get("groups")
        and not workflow.get("for_each")
    )


@dataclass(frozen=True)
class HookPlan:
    hook: HookDeclaration
    inputs: dict[str, Any]
    binding: HookBindingRevision | None
    step_execution_id: str
    #: The bound storage workflow holds no steps yet: nothing runs.
    holds_no_steps: bool = False

    @property
    def empty(self) -> bool:
        return (
            self.binding is None or self.binding.target_workflow_id is None or self.holds_no_steps
        )

    def direct_spec(self) -> dict[str, Any]:
        """The ordinary Invoke config of the bound child: hook inputs as literals."""
        assert self.binding is not None and self.binding.target_workflow_id is not None
        return {
            "workflow_id": self.binding.target_workflow_id,
            "input_mapping": {
                child_input: "literal:" + json.dumps(self.inputs[hook_input])
                for child_input, hook_input in self.binding.input_mapping.items()
                if hook_input in self.inputs
            },
            "output_mapping": {},
            "artifact_mapping": {},
        }

    def output_updates(
        self, state: Mapping[str, Any], child_outputs: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(state updates, hook output)`` for the whole result, or HookContractError."""
        targets = (
            None
            if self.empty or self.binding is None
            else select_hook_targets(self.binding.output_mapping, dict(child_outputs or {}))
        )
        output = normalize_hook_output(self.hook, self.inputs, targets)
        return publish_hook_output(state, self.hook.hook_id, output, self.step_execution_id), output

    def summary(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook.hook_id,
            "contract_version": self.hook.contract_version,
            "binding_revision_id": self.binding.id if self.binding is not None else None,
            "target_workflow_id": self.binding.target_workflow_id if self.binding else None,
        }


def publish_hook_output(
    state: Mapping[str, Any], hook_id: str, output: Mapping[str, Any], step_execution_id: str
) -> dict[str, Any]:
    """One ``workflow_data`` update that replaces ``hooks.<hook_id>`` as a whole."""
    base = state.get("workflow_data")
    workflow_data = copy.deepcopy(dict(base)) if isinstance(base, Mapping) else {}
    hooks = workflow_data.get(HOOKS_STATE_ROOT)
    hooks = dict(hooks) if isinstance(hooks, Mapping) else {}
    hooks[hook_id] = {"output": copy.deepcopy(dict(output)), "step_execution_id": step_execution_id}
    workflow_data[HOOKS_STATE_ROOT] = hooks
    return {"workflow_data": workflow_data}


def clear_hook_outputs(state: Mapping[str, Any], hook_ids: set[str]) -> dict[str, Any] | None:
    """Drop the outputs of *hook_ids* so a new pass never reads the previous one."""
    base = state.get("workflow_data")
    hooks = base.get(HOOKS_STATE_ROOT) if isinstance(base, Mapping) else None
    if not isinstance(base, Mapping) or not isinstance(hooks, Mapping):
        return None
    if not set(hooks) & hook_ids:
        return None
    workflow_data = copy.deepcopy(dict(base))
    workflow_data[HOOKS_STATE_ROOT] = {
        key: value for key, value in hooks.items() if key not in hook_ids
    }
    return {"workflow_data": workflow_data}


def hook_inputs(state: Mapping[str, Any], hook: HookDeclaration) -> dict[str, Any]:
    """The published inputs, by presence: a source that does not exist stays absent."""
    from core.application.orchestration.state_paths import (
        StatePathError,
        resolve_state_path_with_workflow_data,
    )

    inputs: dict[str, Any] = {}
    for name, selector in hook.input_mapping.items():
        try:
            value, found = resolve_state_path_with_workflow_data(dict(state), selector)
        except StatePathError as exc:
            raise HookContractError(
                "workflow.hook.input_selector_invalid",
                f"The source of hook input '{name}' is not a state path: {exc}",
                f"hook.input_mapping.{name}",
            ) from exc
        if found:
            inputs[name] = copy.deepcopy(value)
    from core.application.workflows.workflow_hook_contract import _schema_errors

    problems = _schema_errors(hook.input_schema, inputs)
    if problems:
        raise HookContractError(
            "workflow.hook.input_invalid",
            f"The hook inputs do not match the hook contract: {problems[0]}",
            "hook.input_schema",
        )
    return inputs


def plan_hook(
    state: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    step_execution_id: str,
    parent_job: Mapping[str, Any],
    owner_workflow: Mapping[str, Any],
    load_revision: Callable[[str, str], HookBindingRevision | None],
) -> HookPlan:
    """The hook's pinned target and its inputs, or InvocationTarget/HookContract errors."""
    hook = parse_hook_declaration(spec.get(HOOK_CONFIG_KEY))
    meta = parent_job.get("meta")
    if isinstance(meta, str):
        meta = json.loads(meta or "{}")
    pins = dict(meta or {}).get(HOOK_BINDING_PINS_META_KEY)
    if not isinstance(pins, Mapping):
        raise InvocationTargetError(
            "workflow.hook.binding_not_pinned",
            "The run pinned no hook bindings; start a new run",
        )
    account_id = str(parent_job.get("account_id") or "")
    owner = hook_owner_for(
        owner_workflow, account_id=account_id, project_id=parent_job.get("project_id")
    )
    bindings = PinnedBindings(
        {str(key): value for key, value in pins.items()},
        load_revision=lambda revision_id: load_revision(revision_id, account_id),
    )
    binding = bindings.binding(owner, hook.hook_id)
    return HookPlan(hook, hook_inputs(state, hook), binding, step_execution_id)


def binding_compatibility_issues(plan: HookPlan, child_settings: Any) -> list[str]:
    """Whether the bound child, as it is now, still fits the pinned mappings.

    The child is chosen at its own submission; an edit after the binding was
    saved is caught here, before any child effect (spec §11).
    """
    from core.application.workflows.workflow_hook_binding_service import mapping_compatibility

    if plan.binding is None:
        return []
    return mapping_compatibility(
        plan.binding.input_mapping, plan.binding.output_mapping, child_settings
    )


def load_binding_revision(revision_id: str, account_id: str) -> HookBindingRevision | None:
    """Read one pinned revision through the registry store's own transaction."""
    from core.application.registry.registry_manager import RegistryManager
    from core.application.workflows.workflow_hook_binding_repository import (
        WorkflowHookBindingRepository,
    )

    db = getattr(RegistryManager.instance(), "db_store", None)
    if db is None or not hasattr(db, "_transaction"):
        return None
    with db._transaction() as cur:
        return WorkflowHookBindingRepository(cur.connection).revision(
            revision_id, account_id=account_id
        )


__all__ = [
    "HOOKS_STATE_ROOT",
    "HookPlan",
    "binding_compatibility_issues",
    "clear_hook_outputs",
    "holds_no_steps",
    "hook_inputs",
    "load_binding_revision",
    "plan_hook",
    "publish_hook_output",
]


def plan_hook_for_state(
    state: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    step_execution_id: str,
    get_logger: Callable[[], Any],
) -> HookPlan:
    """:func:`plan_hook` for the running job: its pins, its author workflow identity."""
    from core.application.registry.registry_manager import RegistryManager

    parent_job_id = str(state.get("job_id") or "").strip()
    parent_job = (get_logger().get_job(parent_job_id) if parent_job_id else None) or {}
    db = getattr(RegistryManager.instance(), "db_store", None)
    getter = getattr(db, "get_workflow", None)
    workflow_id = str(state.get("workflow_id") or "").strip()
    owner = getter(workflow_id) if callable(getter) else None
    plan = plan_hook(
        state,
        spec,
        step_execution_id=step_execution_id,
        parent_job=parent_job,
        owner_workflow=owner if isinstance(owner, Mapping) else {"workflow_id": workflow_id},
        load_revision=load_binding_revision,
    )
    target = plan.binding.target_workflow_id if plan.binding is not None else None
    if target and callable(getter) and holds_no_steps(getter(target)):
        empty_plan: HookPlan = replace(plan, holds_no_steps=True)
        return empty_plan
    return plan


def hook_ids_of_steps(steps: Mapping[str, Mapping[str, Any]], step_ids: set[str]) -> set[str]:
    """The hook ids declared by *step_ids* (the body of one For each pass)."""
    found: set[str] = set()
    for step_id in step_ids:
        step = steps.get(step_id) or {}
        override = step.get("override")
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        hook = config.get(HOOK_CONFIG_KEY) if isinstance(config, Mapping) else None
        if isinstance(hook, Mapping) and isinstance(hook.get("hook_id"), str):
            found.add(hook["hook_id"])
    return found
