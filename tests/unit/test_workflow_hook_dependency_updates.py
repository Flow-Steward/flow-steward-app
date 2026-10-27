"""A shared child stays one function for every caller (spec §9.2, §12).

Where-used names direct Invokes and project bindings; a breaking child change
is refused with its consumers; an extension release never breaks a bound hook,
and bindings staged for a release activate with it.
"""

from __future__ import annotations

import pytest

from core.application.workflows.workflow_hook_contract import (
    hook_semantic_digest,
    parse_hook_declaration,
)
from core.application.workflows.workflow_hook_release import (
    installed_hook_digests,
    release_hook_blockers,
    release_hook_digests,
)
from tests.unit.test_workflow_hook_contract import declaration

DIGEST = hook_semantic_digest(parse_hook_declaration(declaration()))


def release(**changes) -> dict:
    return {
        "invoice_flow": {
            "steps": [
                {
                    "step_id": "before",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"hook": declaration(**changes)}},
                }
            ]
        }
    }


def binding(**changes) -> dict:
    row = {
        "id": "b-1",
        "project_id": "p-1",
        "local_workflow_id": "invoice_flow",
        "hook_id": "before_invoice",
        "staged_release_id": None,
        "active_target": "enrich_invoice",
        "active_version": 1,
        "active_digest": DIGEST,
        "staged_target": None,
        "staged_digest": None,
    }
    row.update(changes)
    return row


def codes(bindings, workflows) -> list[str]:
    return [
        issue["code"]
        for issue in release_hook_blockers(
            bindings, release_id="r-2", digests=release_hook_digests(workflows)
        )
    ]


def test_a_relabelled_hook_does_not_block_the_release() -> None:
    assert codes([binding()], release(display_name="Before the invoice", description="x")) == []


def installed_codes(bindings, workflows, install_id="i-1") -> list[str]:
    digests = installed_hook_digests(release_hook_digests(workflows), install_id=install_id)
    return [
        issue["code"]
        for issue in release_hook_blockers(bindings, release_id="r-2", digests=digests)
    ]


def test_a_binding_on_the_installed_workflow_id_survives_an_update() -> None:
    """Live bindings name the workflow extension.<install>.<id>; the demo's Showcase 1.2.1 update was refused."""
    installed = binding(local_workflow_id="extension.i-1.invoice_flow")

    assert installed_codes([installed], release(description="Add your own steps here.")) == []
    # A real removal or contract change still blocks under the installed id.
    assert installed_codes([installed], {"invoice_flow": {"steps": []}}) == [
        "workflow.hook.release_removes_hook"
    ]
    moved = release(stage_contract={"summary": "Later.", "effects_before": ["x"], "effects_after": []})
    assert installed_codes([installed], moved) == ["workflow.hook.release_contract_changed"]
    # Another install's workflow is not this release's.
    assert installed_codes([binding(local_workflow_id="extension.i-2.invoice_flow")], release()) == [
        "workflow.hook.release_removes_hook"
    ]


def test_a_removed_hook_blocks_the_release() -> None:
    assert codes([binding()], {"invoice_flow": {"steps": []}}) == [
        "workflow.hook.release_removes_hook"
    ]


def test_a_changed_contract_blocks_unless_a_matching_binding_is_staged() -> None:
    moved = release(
        stage_contract={
            "summary": "Runs after the invoice is created.",
            "effects_before": ["invoice.create"],
            "effects_after": [],
        }
    )
    assert codes([binding()], moved) == ["workflow.hook.release_contract_changed"]
    new_digest = release_hook_digests(moved)[("invoice_flow", "before_invoice")]
    staged = binding(
        staged_release_id="r-2", staged_target="enrich_invoice", staged_digest=new_digest
    )
    assert codes([staged], moved) == []
    # Staged for another release, or for the old contract: still a blocker.
    assert codes([binding(staged_release_id="r-3", staged_digest=new_digest)], moved)
    assert codes([binding(staged_release_id="r-2", staged_digest=DIGEST)], moved)


def test_an_empty_hook_never_blocks_a_release() -> None:
    assert codes([binding(active_target=None)], {"invoice_flow": {"steps": []}}) == []


# --- PostgreSQL: where-used, consumer guard, account isolation ------------------

from tests.unit.test_workflow_hook_binding_postgres import hooked as hooked  # noqa: E402
from tests.unit.test_workflow_hook_binding_postgres import request  # noqa: E402


def _consumers(ctx, workflow_id: str, account_id: str | None = None):
    from core.application.workflows.workflow_where_used import workflow_consumers

    with ctx["conn"].cursor() as cur:
        return workflow_consumers(
            cur, account_id=account_id or ctx["account_id"], workflow_id=workflow_id
        )


def test_one_child_of_a_hook_and_a_direct_invoke_is_seen_by_every_caller(hooked) -> None:
    ctx = hooked
    ctx["service"].save(request(ctx))
    caller = f"{ctx['owner_id']}_direct"
    ctx["save"](
        caller,
        steps=[
            {
                "step_id": "call",
                "step_kind": "invoke_workflow",
                "override": {
                    "invoke_workflow": {
                        "workflow_id": ctx["child_id"],
                        "input_mapping": {"invoice_id": "state.invoice_id"},
                    }
                },
            }
        ],
        enabled=False,
    )
    found = {(c.kind, c.caller_workflow_id, c.hook_id) for c in _consumers(ctx, ctx["child_id"])}
    assert ("hook", ctx["owner_id"], "before_invoice") in found
    assert ("invoke", caller, None) in found
    # Another account never sees these consumers.
    assert _consumers(ctx, ctx["child_id"], account_id="00000000-0000-0000-0000-0000000000a7") == []


def test_a_breaking_child_interface_save_is_refused_with_its_consumers(hooked) -> None:
    from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError

    ctx = hooked
    ctx["service"].save(request(ctx))
    # Only an enabled caller blocks a save; a disabled one gets diagnostics.
    ctx["conn"].execute(
        "UPDATE registry_workflows SET enabled = true WHERE workflow_id = %s", (ctx["owner_id"],)
    )
    child = ctx["store"].get_workflow(ctx["child_id"])
    settings = dict(child["settings"])
    callable_settings = dict(settings["callable"])
    callable_settings["expected_outputs"] = []  # drops company_name, which the hook maps
    settings["callable"] = callable_settings
    with pytest.raises(WorkflowConsumerConflictError) as raised:
        ctx["store"].save_workflow(
            {**child, "settings": settings}, expected_revision=int(child["revision"])
        )
    consumer = raised.value.issues[0]["consumer"]
    assert (consumer["kind"], consumer["hook_id"]) == ("hook", "before_invoice")
    assert "company_name" in raised.value.issues[0]["message"]
    # Nothing was saved.
    assert ctx["store"].get_workflow(ctx["child_id"])["revision"] == child["revision"]


def test_an_additive_child_change_is_saved(hooked) -> None:
    ctx = hooked
    ctx["service"].save(request(ctx))
    child = ctx["store"].get_workflow(ctx["child_id"])
    settings = dict(child["settings"])
    callable_settings = dict(settings["callable"])
    callable_settings["expected_outputs"] = [
        *callable_settings["expected_outputs"],
        {"name": "crm_id", "type": "string"},
    ]
    settings["callable"] = callable_settings
    ctx["store"].save_workflow(
        {**child, "settings": settings}, expected_revision=int(child["revision"])
    )
    assert ctx["store"].get_workflow(ctx["child_id"])["revision"] > child["revision"]


def test_uninstalling_or_unbinding_never_deletes_the_customer_child(hooked) -> None:
    ctx = hooked
    first = ctx["service"].save(request(ctx))
    ctx["service"].save(request(ctx, expected_revision=first.revision, target_workflow_id=None))
    assert ctx["store"].get_workflow(ctx["child_id"]) is not None
    assert _consumers(ctx, ctx["child_id"]) == []


def test_a_wait_added_under_a_lane_caller_is_refused_and_serial_callers_allow_it(hooked) -> None:
    from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError

    ctx = hooked
    caller = f"{ctx['owner_id']}_lanes"
    child_id = ctx["child_id"]

    def invoke(step_id: str, lane: str) -> dict:
        return {
            "step_id": step_id,
            "step_kind": "invoke_workflow",
            "group_id": "lanes",
            "lane_id": lane,
            "override": {
                "invoke_workflow": {
                    "workflow_id": child_id,
                    "input_mapping": {"invoice_id": "state.invoice_id"},
                }
            },
        }

    ctx["created"].append(caller)
    ctx["store"].save_workflow(
        {
            "workflow_id": caller,
            "display_name": caller,
            "enabled": True,
            "account_id": ctx["account_id"],
            "settings": {
                "execution_graph": {
                    "schema_version": 1,
                    "entry": {"kind": "group", "id": "lanes"},
                    "edges": [],
                }
            },
            "steps": [invoke("left", "a"), invoke("right", "b")],
            "groups": [
                {
                    "group_id": "lanes",
                    "body": {
                        "kind": "lanes",
                        "lanes": [
                            {"lane_id": "a", "entry": {"kind": "step", "id": "left"}},
                            {"lane_id": "b", "entry": {"kind": "step", "id": "right"}},
                        ],
                    },
                }
            ],
            "for_each": [],
        }
    )
    child = ctx["store"].get_workflow(child_id)
    waiting = {
        **child,
        "steps": [
            {"step_id": "ask", "step_kind": "approval"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "for_each": [],
    }
    with pytest.raises(WorkflowConsumerConflictError) as raised:
        ctx["store"].save_workflow(waiting, expected_revision=int(child["revision"]))
    assert "parallel lane" in raised.value.issues[0]["message"]
    # With only the serial hook caller, the same wait is an ordinary allowed pause.
    ctx["conn"].execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (caller,))
    ctx["service"].save(request(ctx))
    ctx["store"].save_workflow(waiting, expected_revision=int(child["revision"]))
