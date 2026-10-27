"""One effective-target resolver for direct and hook Invoke (spec §12)."""

from __future__ import annotations

import pytest

from core.application.workflows.workflow_definition_validation import (
    ChildDefinition,
    scan_child_chain,
)
from core.application.workflows.workflow_invocation_target import (
    HookBindingRevision,
    HookOwner,
    InvocationTargetError,
    MappingBindings,
    PinnedBindings,
    definition_invocation_targets,
    hook_owner_for,
    resolve_invocation_target,
)
from tests.unit.test_workflow_hook_contract import declaration

PROJECT_A = "11111111-1111-4111-8111-111111111111"
PROJECT_B = "22222222-2222-4222-8222-222222222222"
OWNER_A = HookOwner("acc", PROJECT_A, "install-1", "invoice_flow")
OWNER_B = HookOwner("acc", PROJECT_B, "install-1", "invoice_flow")


def hook_config(**changes) -> dict:
    return {"hook": declaration(**changes)}


def revision(target: str | None, *, project: str = PROJECT_A, rid: str = "rev-1"):
    return HookBindingRevision(
        id=rid,
        binding_id="b-1",
        revision=1,
        account_id="acc",
        project_id=project,
        target_workflow_id=target,
        input_mapping={"invoice_id": "invoice_id"} if target else {},
        output_mapping={},
        hook_contract_version=1,
        hook_semantic_digest="d",
    )


def test_a_direct_invoke_resolves_to_its_workflow() -> None:
    target = resolve_invocation_target(
        {"workflow_id": "child"}, owner=OWNER_A, bindings=MappingBindings({})
    )
    assert (target.kind, target.workflow_id, target.empty) == ("direct", "child", False)


def test_an_empty_binding_is_no_call_not_a_missing_target() -> None:
    target = resolve_invocation_target(hook_config(), owner=OWNER_A, bindings=MappingBindings({}))
    assert (target.kind, target.workflow_id, target.empty) == ("hook", None, True)
    cleared = MappingBindings({(OWNER_A, "before_invoice"): revision(None)})
    assert resolve_invocation_target(hook_config(), owner=OWNER_A, bindings=cleared).empty


def test_projects_a_and_b_are_isolated() -> None:
    bindings = MappingBindings({(OWNER_A, "before_invoice"): revision("child_a")})
    assert (
        resolve_invocation_target(hook_config(), owner=OWNER_A, bindings=bindings).workflow_id
        == "child_a"
    )
    # No account-wide fallback: project B has no binding, so its hook is empty.
    assert resolve_invocation_target(hook_config(), owner=OWNER_B, bindings=bindings).empty


def test_mixed_direct_workflow_and_hook_is_refused() -> None:
    config = {"workflow_id": "child", **hook_config()}
    bindings = MappingBindings({})
    with pytest.raises(InvocationTargetError) as raised:
        resolve_invocation_target(config, owner=OWNER_A, bindings=bindings)
    assert raised.value.code == "workflow.hook.direct_target_conflict"


def test_a_pinned_run_reads_its_pin_never_the_live_binding() -> None:
    loaded = {"rev-1": revision("child_old")}
    pinned = PinnedBindings({"before_invoice": "rev-1"}, load_revision=loaded.get)
    assert (
        resolve_invocation_target(hook_config(), owner=OWNER_A, bindings=pinned).workflow_id
        == "child_old"
    )
    empty = PinnedBindings({"before_invoice": None}, load_revision=loaded.get)
    assert resolve_invocation_target(hook_config(), owner=OWNER_A, bindings=empty).empty
    unpinned = PinnedBindings({}, load_revision=loaded.get)
    config = hook_config()
    with pytest.raises(InvocationTargetError) as raised:
        resolve_invocation_target(config, owner=OWNER_A, bindings=unpinned)
    assert raised.value.code == "workflow.hook.binding_not_pinned"


def test_a_pinned_revision_of_another_project_is_refused() -> None:
    loaded = {"rev-1": revision("child", project=PROJECT_B)}
    pinned = PinnedBindings({"before_invoice": "rev-1"}, load_revision=loaded.get)
    config = hook_config()
    with pytest.raises(InvocationTargetError) as raised:
        resolve_invocation_target(config, owner=OWNER_A, bindings=pinned)
    assert raised.value.code == "workflow.hook.binding_scope_mismatch"


def test_hook_owner_identity_of_a_workflow_row() -> None:
    extension_row = {
        "workflow_id": "ext__invoice",
        "source_install_id": "install-1",
        "local_workflow_id": "invoice_flow",
    }
    assert hook_owner_for(extension_row, account_id="acc", project_id=PROJECT_A) == OWNER_A
    own = hook_owner_for({"workflow_id": "mine"}, account_id="acc", project_id=PROJECT_A)
    assert own == HookOwner("acc", PROJECT_A, "", "mine")


def _invoke(step_id: str, config: dict) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": config},
    }


def test_definition_targets_list_direct_and_configured_hooks() -> None:
    definition = {
        "steps": [
            _invoke("direct", {"workflow_id": "child_direct"}),
            _invoke("hooked", hook_config()),
            _invoke("empty", hook_config(hook_id="after_invoice")),
        ]
    }
    bindings = MappingBindings({(OWNER_A, "before_invoice"): revision("child_hook")})
    targets = definition_invocation_targets(definition, owner=OWNER_A, bindings=bindings)
    assert [(t.step_id, t.workflow_id) for t in targets] == [
        ("direct", "child_direct"),
        ("hooked", "child_hook"),
        ("empty", None),
    ]


def test_a_hook_to_invoke_to_hook_cycle_is_found_in_the_real_project() -> None:
    owner_row = {"workflow_id": "invoice_flow"}
    middle_row = {"workflow_id": "middle"}
    definitions = {
        "middle": {"steps": [_invoke("back", hook_config(hook_id="in_middle"))]},
        "invoice_flow": {"steps": [_invoke("hooked", hook_config())]},
    }
    rows = {"middle": middle_row, "invoice_flow": owner_row}
    bindings = MappingBindings(
        {
            (HookOwner("acc", PROJECT_A, "", "invoice_flow"), "before_invoice"): revision("middle"),
            (HookOwner("acc", PROJECT_A, "", "middle"), "in_middle"): revision("invoice_flow"),
        }
    )

    def load_child(workflow_id: str) -> ChildDefinition | None:
        if workflow_id not in definitions:
            return None
        return ChildDefinition(
            workflow_id,
            1,
            definitions[workflow_id],
            owner=hook_owner_for(rows[workflow_id], account_id="acc", project_id=PROJECT_A),
        )

    scan = scan_child_chain(
        "middle", load_child, max_depth=5, ancestry=["invoice_flow"], bindings=bindings
    )
    assert [issue["code"] for issue in scan.issues] == ["workflow.child.recursion"]
    # In project B nothing is bound, so there is no cycle there.
    scan_b = scan_child_chain(
        "middle",
        lambda wid: ChildDefinition(
            wid,
            1,
            definitions[wid],
            owner=hook_owner_for(rows[wid], account_id="acc", project_id=PROJECT_B),
        ),
        max_depth=5,
        ancestry=["invoice_flow"],
        bindings=bindings,
    )
    assert scan_b.issues == []
