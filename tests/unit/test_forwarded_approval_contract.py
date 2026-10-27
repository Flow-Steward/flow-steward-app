"""Parent reactions must address the child's actual decision, with its exact actions."""

import copy

import pytest

from core.application.workflows.approval_waiting_propagation import pending_child_approval_event
from tests.unit.test_workflow_reaction_events import (
    _CHILD_APPROVAL,
    _chain_store,
    _rule,
    _run,
)
from tests.unit.test_workflow_reaction_events import dispatched as dispatched


@pytest.mark.parametrize("actions", [["approve", "reject"], ["approve", "abort"]])
def test_forwarded_notification_keeps_exact_approval_actions(dispatched, actions):
    store = _chain_store()
    store.approvals["child"]["allowed_actions"] = actions
    payload = {**_CHILD_APPROVAL, "allowed_actions": actions}
    _run(store, "job.awaiting_approval", payload, job_id="child", wf="wf-child")
    assert len(dispatched) == 3
    assert all(row["payload"].get("allowed_actions") == actions for row in dispatched)


@pytest.mark.parametrize("capability", ["notify", "human_decision"])
def test_human_decision_parent_reactions_address_child_job_and_keep_parent_rule_identity(
    dispatched,
    capability,
):
    rules = [_rule("wf-parent", "job.approval_waiting"), _rule("wf-root", "job.approval_waiting")]
    for rule in rules:
        rule["action"]["channel_capability"] = capability
    store = _chain_store(rules=rules)
    payload = {
        **_CHILD_APPROVAL,
        "approval_id": "exec-approval",
        "allowed_actions": ["approve", "reject"],
    }
    _run(store, "job.awaiting_approval", payload, job_id="child", wf="wf-child")
    assert len(dispatched) == 2
    assert [row["job_id"] for row in dispatched] == ["child", "child"]
    assert [row["workflow_id"] for row in dispatched] == ["wf-parent", "wf-root"]
    assert dispatched[0]["event_id"].startswith("wf-event:parent:")
    assert dispatched[1]["event_id"].startswith("wf-event:root:")
    assert all(row["payload"]["approval_id"] == "exec-approval" for row in dispatched)


def test_late_parent_catchup_supplies_authored_approval_actions():
    event = pending_child_approval_event(_chain_store(), "root")
    assert event is not None
    assert event["payload"].get("allowed_actions") == ["approve", "reject"]


def test_forged_forwarded_action_cannot_address_an_unlinked_job(dispatched):
    rule = _rule("wf-parent", "job.approval_waiting")
    rule["action"]["channel_capability"] = "human_decision"
    store = _chain_store(rules=[rule])
    store.jobs["other"] = copy.deepcopy(store.jobs["child"])
    store.jobs["other"]["id"] = "other"
    store.jobs["other"]["meta"].pop("invoke_parent_job_id")
    _run(
        store,
        "job.approval_waiting",
        {
            **_CHILD_APPROVAL,
            "propagated": True,
            "approval_job_id": "other",
            "allowed_actions": ["approve"],
        },
        job_id="parent",
        wf="wf-parent",
    )
    assert dispatched == []
