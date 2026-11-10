"""Runtime of a composite action: the action Invoke and the steps of its child (ADR 0065).

An action Invoke is an ordinary Invoke of the workflow that implements the
action. What is added here is the action context of the child job: which
install each extension is called through and which connection each slot holds.
The host writes it into the child's ``jobs.meta`` when it creates the child;
the child's connector steps read their install and connection from it. Author
input never carries it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.extensions.composite_action_resolution import (
    InstalledComposites,
    installed_composites_for_store,
)
from core.application.workflows.workflow_action_contract import (
    ACTION_CONFIG_KEY,
    ACTION_CONNECTIONS_KEY,
    ACTION_INSTALLS_KEY,
    COMPOSITE_ACTION_META_KEY,
    SLOT_BINDING_KEY,
    ActionContractError,
    action_connections,
    action_implementation_of,
    action_installs,
    composite_action_context,
    is_installed_action_implementation,
    parse_action_ref,
    slot_of_binding,
)

InstallResolver = Callable[[str], InstalledComposites | None]


class ActionRuntimeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActionPlan:
    """What one action Invoke calls and the context its child job runs with."""

    extension_id: str
    install_id: str
    action_id: str
    workflow_id: str
    installs: dict[str, str]
    connections: dict[str, str]

    def context(self) -> dict[str, Any]:
        """The ``jobs.meta`` action context of the child."""
        return {
            "extension_id": self.extension_id,
            "install_id": self.install_id,
            "action_id": self.action_id,
            "installs": dict(self.installs),
            "connections": dict(self.connections),
        }

    def direct_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """The Invoke config as a plain direct Invoke of the implementation."""
        direct = {
            key: value
            for key, value in dict(spec).items()
            if key not in {ACTION_CONFIG_KEY, ACTION_INSTALLS_KEY, ACTION_CONNECTIONS_KEY}
        }
        direct["workflow_id"] = self.workflow_id
        return direct


def _text(value: Any) -> str:
    return str(value or "").strip()


def _usable_install(
    resolve_install: InstallResolver, install_id: str, *, extension_id: str
) -> InstalledComposites:
    installed = resolve_install(install_id)
    if installed is None or installed.extension_id != extension_id:
        raise ActionRuntimeError(
            "action_install_unavailable",
            f"Extension '{extension_id}' is not available through install '{install_id}'",
        )
    if not installed.active:
        raise ActionRuntimeError(
            "action_install_inactive",
            f"The install of extension '{extension_id}' is not active",
        )
    return installed


def plan_action_invoke(
    spec: Mapping[str, Any],
    *,
    parent_context: Mapping[str, Any],
    resolve_install: InstallResolver,
) -> ActionPlan:
    """Resolve one action Invoke, or raise :class:`ActionRuntimeError`.

    *parent_context* is the action context of the job that runs the Invoke:
    empty for a person's workflow, the caller's installs and slots when the
    Invoke stands inside an implementation.
    """
    try:
        ref = parse_action_ref(dict(spec).get(ACTION_CONFIG_KEY))
    except ActionContractError as exc:
        raise ActionRuntimeError(exc.code, str(exc)) from exc
    inherited_installs = {
        _text(key): _text(value)
        for key, value in dict(parent_context.get("installs") or {}).items()
        if _text(key) and _text(value)
    }
    inherited_connections = {
        _text(key): _text(value)
        for key, value in dict(parent_context.get("connections") or {}).items()
        if _text(key) and _text(value)
    }
    install_id = ref.install_id or inherited_installs.get(ref.extension_id, "")
    if not install_id:
        raise ActionRuntimeError(
            "action_install_unresolved",
            f"No install of extension '{ref.extension_id}' was chosen for action '{ref.action_id}'",
        )
    installed = _usable_install(resolve_install, install_id, extension_id=ref.extension_id)
    action = installed.action(ref.action_id)
    if action is None:
        raise ActionRuntimeError(
            "action_not_composite",
            f"Extension '{ref.extension_id}' has no action '{ref.action_id}' implemented "
            "by a workflow",
        )
    workflow_id = action.workflow_id(install_id)
    declared = _text(dict(spec).get("workflow_id"))
    if declared and declared != workflow_id:
        raise ActionRuntimeError(
            "action_target_mismatch",
            f"Action '{ref.action_id}' is implemented by another workflow than the step names; "
            "choose the action again",
        )
    chosen = action_installs(spec)
    installs = {ref.extension_id: install_id}
    needed = set(installed.required_dependencies) | {
        slot.extension_id for slot in action.implementation.connections
    }
    for extension_id in sorted(needed - {ref.extension_id}):
        dependency_install = chosen.get(extension_id) or inherited_installs.get(extension_id, "")
        if not dependency_install:
            raise ActionRuntimeError(
                "action_install_unresolved",
                f"No install of extension '{extension_id}' was chosen for action '{ref.action_id}'",
            )
        _usable_install(resolve_install, dependency_install, extension_id=extension_id)
        installs[extension_id] = dependency_install
    bindings = action_connections(spec)
    connections: dict[str, str] = {}
    for slot in action.implementation.connections:
        binding = bindings.get(slot.slot) or {}
        connection_id = _text(binding.get("connection_id"))
        parent_slot = _text(binding.get(SLOT_BINDING_KEY))
        if not connection_id and parent_slot:
            connection_id = inherited_connections.get(parent_slot, "")
        if not connection_id:
            raise ActionRuntimeError(
                "action_connection_required",
                f"Choose a connection for '{slot.slot}' of action '{ref.action_id}'",
            )
        connections[slot.slot] = connection_id
    return ActionPlan(
        extension_id=ref.extension_id,
        install_id=install_id,
        action_id=ref.action_id,
        workflow_id=workflow_id,
        installs=installs,
        connections=connections,
    )


def child_origin_issue(child_workflow: Any, plan: ActionPlan | None) -> str:
    """Why this Invoke may not create a child of *child_workflow*, or ``""``.

    An installed implementation runs only under the action Invoke that resolves
    to it. The check uses the child row the runtime is about to pin, so a saved
    definition that slipped past validation still cannot reach it.
    """
    row = child_workflow if isinstance(child_workflow, Mapping) else {}
    if not is_installed_action_implementation(row):
        return (
            "The action of this step is not implemented by the workflow it names"
            if plan is not None
            else ""
        )
    if plan is None:
        return "This workflow implements an extension action and runs only as that action"
    try:
        implementation = action_implementation_of(row)
    except ActionContractError:
        implementation = None
    if (
        implementation is None
        or implementation.action_id != plan.action_id
        or _text(row.get("source_install_id")) != plan.install_id
    ):
        return "The action of this step is not implemented by the workflow it names"
    return ""


def _job_meta(job_id: str, get_job: Callable[[str], Any] | None) -> Any:
    if not job_id or get_job is None:
        return None
    try:
        job = get_job(job_id)
    except Exception:
        return None
    return job.get("meta") if isinstance(job, Mapping) else None


def action_invoke_for_state(
    state: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    step_execution_id: str,
    get_logger: Callable[[], Any],
) -> tuple[ActionPlan | None, dict[str, Any], ActionRuntimeError | None]:
    """The plan, the direct Invoke config and the refusal of one Invoke step.

    A plain Invoke of an ordinary workflow comes back untouched and costs no
    read. An Invoke that names an installed implementation without its action
    is refused here, at execution, whatever the saved definition says. Once the
    child of this step entry exists nothing is resolved again: the child keeps
    the definition and the context it was created with.
    """
    from core.application.orchestration.invoke_workflow_step_node_common import (
        _find_existing_child_invocation,
        _invocation_key,
        _parent_meta,
    )
    from core.application.orchestration.runtime_identity import runtime_identity_from_state
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.workflow_lookup import get_workflow_row

    source = dict(spec)
    target = _text(source.get("workflow_id"))
    has_action = ACTION_CONFIG_KEY in source
    if not has_action and not target.startswith("extension."):
        return None, source, None
    job_id = _text(state.get("job_id"))
    logger = get_logger() if job_id else None
    getter = getattr(logger, "get_job", None)
    parent_job = (getter(job_id) if callable(getter) else None) or {}
    existing = _find_existing_child_invocation(
        parent_job if isinstance(parent_job, dict) else None,
        invocation_key=_invocation_key(job_id, step_execution_id),
    )
    if existing and callable(getter):
        child_meta = _parent_meta(getter(existing) or {})
        direct = ActionPlan("", "", "", target, {}, {}).direct_spec(source)
        direct["workflow_id"] = target or _text(child_meta.get("invoke_child_workflow_id"))
        return None, direct, None
    account_id = _text(runtime_identity_from_state(state).as_context().get("account_id"))
    db = getattr(RegistryManager.instance(), "db_store", None)

    def child_row(workflow_id: str) -> Any:
        kwargs = {"account_id": account_id} if account_id else {}
        return get_workflow_row(db, workflow_id, **kwargs) if db is not None else None

    if not has_action:
        issue = child_origin_issue(child_row(target), None)
        error = ActionRuntimeError("action_origin_required", issue) if issue else None
        return None, source, error
    try:
        plan = plan_action_invoke(
            source,
            parent_context=composite_action_context(_parent_meta(parent_job)),
            resolve_install=lambda install_id: installed_composites_for_store(
                db, install_id=install_id, account_id=account_id
            ),
        )
    except ActionRuntimeError as exc:
        return None, source, exc
    issue = child_origin_issue(child_row(plan.workflow_id), plan)
    if issue:
        return None, source, ActionRuntimeError("action_origin_required", issue)
    return plan, plan.direct_spec(source), None


#: Marks the message of an action Invoke so the run view labels it as an extension action.
ACTION_INVOKE_ARTIFACT = "action_invoke"


def action_invoke_report(step: Mapping[str, Any], summary: str, artifacts: dict[str, Any]) -> str:
    """Word an action Invoke's message as its action and tag its artifacts.

    The Invoke runtime speaks of a child workflow by id. For an action step the
    id is replaced by the action, and the extension and action are recorded so
    the run view shows the step like any other extension action.
    """
    override = step.get("override") if isinstance(step, Mapping) else None
    config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
    try:
        ref = (
            parse_action_ref(config.get(ACTION_CONFIG_KEY)) if isinstance(config, Mapping) else None
        )
    except (ActionContractError, AttributeError):
        ref = None
    if ref is None or not isinstance(config, Mapping) or ACTION_CONFIG_KEY not in config:
        return summary
    artifacts.setdefault("extension_id", ref.extension_id)
    artifacts.setdefault("operation_id", ref.action_id)
    artifacts[ACTION_INVOKE_ARTIFACT] = True
    workflow_id = _text(config.get("workflow_id"))
    text = str(summary or "")
    for wording in (f"Child workflow '{workflow_id}'", f"workflow '{workflow_id}'"):
        if workflow_id and wording in text:
            text = text.replace(wording, f"Extension action '{ref.action_id}'")
    return text.replace(workflow_id, ref.action_id) if workflow_id else text


def write_action_context(child_meta: dict[str, Any], plan: ActionPlan | None) -> None:
    """Record the action context in the launch metadata of the child job."""
    if plan is not None:
        child_meta[COMPOSITE_ACTION_META_KEY] = plan.context()


@dataclass(frozen=True)
class ConnectorActionBinding:
    """A connector step's binding and install after the action context is applied."""

    connection_binding: dict[str, Any]
    connector_install_id: str
    error: dict[str, Any] | None = None


def _binding_error(connector_id: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "runtime": "unresolved",
        "connector_id": connector_id,
        "error": code,
        "detail": detail,
        # Resolution happens before the connector is called.
        "retryable": False,
        "definitely_no_external_effect": True,
    }


def resolve_connector_action_binding(
    *,
    step: Mapping[str, Any],
    connection_binding: Mapping[str, Any],
    connector_install_id: str,
    load_context: Callable[[], Mapping[str, Any]],
) -> ConnectorActionBinding:
    """Fill a connector step's slot and missing install from the job's action context.

    A step that names a fixed connection and an install is returned unchanged
    and the context is never loaded. A slot outside an action job is an error:
    there is nobody to supply it.
    """
    from core.application.workflows.extension_connection_validation import (
        named_extension_without_install,
    )

    binding = dict(connection_binding or {})
    install_id = _text(connector_install_id)
    connector_id = _text(step.get("connector_id"))
    slot = slot_of_binding(binding)
    named = None if install_id else named_extension_without_install(step)
    if not slot and named is None:
        return ConnectorActionBinding(binding, install_id)
    context = dict(load_context() or {})
    step_id = _text(step.get("step_id")) or "unnamed"
    if not context:
        if not slot:
            # The existing "choose the extension action again" refusal owns this case.
            return ConnectorActionBinding(binding, install_id)
        if _text(binding.get("connection_id")):
            # The author's own workflow in a Development environment: it names the
            # slot it will read once packaged and a connection to test with now. A
            # bundle never carries a connection id, so an installed
            # implementation cannot reach this branch.
            binding.pop(SLOT_BINDING_KEY, None)
            return ConnectorActionBinding(binding, install_id)
        return ConnectorActionBinding(
            binding,
            install_id,
            _binding_error(
                connector_id,
                "action_context_missing",
                f"Step '{step_id}' reads connection slot '{slot}', which exists only "
                "inside an extension action. The connector was not called.",
            ),
        )
    if named is not None:
        install_id = _text(dict(context.get("installs") or {}).get(named.extension_id))
        if not install_id:
            return ConnectorActionBinding(
                binding,
                install_id,
                _binding_error(
                    connector_id,
                    "action_install_unresolved",
                    f"Step '{step_id}' calls extension '{named.extension_id}', for which the "
                    "action was given no install. The connector was not called.",
                ),
            )
    if slot:
        connection_id = _text(dict(context.get("connections") or {}).get(slot))
        if not connection_id:
            return ConnectorActionBinding(
                binding,
                install_id,
                _binding_error(
                    connector_id,
                    "action_connection_required",
                    f"Step '{step_id}' reads connection slot '{slot}', which the action "
                    "was given no connection for. The connector was not called.",
                ),
            )
        binding = {key: value for key, value in binding.items() if key != SLOT_BINDING_KEY} | {
            "connection_id": connection_id
        }
    return ConnectorActionBinding(binding, install_id)


def job_action_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """The action context of the job that owns *state*, read from its durable row."""
    from core.application.orchestration.connector_events import _shared_job_store

    job_id = _text(state.get("job_id"))
    store = _shared_job_store() if job_id else None
    return composite_action_context(_job_meta(job_id, getattr(store, "get_job", None)))


__all__ = [
    "ACTION_INVOKE_ARTIFACT",
    "ActionPlan",
    "ActionRuntimeError",
    "ConnectorActionBinding",
    "action_invoke_for_state",
    "action_invoke_report",
    "child_origin_issue",
    "job_action_context",
    "plan_action_invoke",
    "resolve_connector_action_binding",
    "write_action_context",
]
