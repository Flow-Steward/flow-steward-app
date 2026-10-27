"""Public workflow reaction events: catalog, identity, scopes, waits and propagation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.application.extensions.event_email import render_event_email
from core.application.notifications import dispatch as notification_dispatch
from core.application.workflows import workflow_reaction_sources
from core.application.workflows.approval_waiting_propagation import (
    pending_child_approval_event,
    propagate_approval_waiting,
    propagated_payload,
)
from core.application.workflows.job_lifecycle_reactions import run_job_lifecycle_reactions
from core.application.workflows.reaction_scopes import (
    compose_scoped_reaction_key,
    merge_scoped_reaction_rules,
    normalize_reaction_scope,
)
from core.application.workflows.workflow_lifecycle_event_sink import (
    bind_workflow_event_sink,
    publish_group_event,
    publish_step_event,
)
from core.application.workflows.workflow_reaction_events import (
    builtin_reaction_events,
    public_reaction_event_type,
    reaction_event_id,
    recovery_wait_id,
)

# --- catalog and scopes -------------------------------------------------------


def _events(scope: str) -> set[str]:
    return {row["event_type"] for row in builtin_reaction_events(scope)}


def test_catalog_offers_each_scope_its_events() -> None:
    steps = {"job.step_started", "job.step_completed", "job.step_failed", "job.step_blocked"}
    groups = {"job.group_started", "job.group_completed", "job.group_failed"}
    waits = {"job.approval_waiting", "job.timer_waiting"}

    assert _events("workflow") == {"job.completed", "job.failed", *steps, *groups, *waits}
    assert _events("group") == {*steps, *groups, *waits}
    assert _events("step") == {*steps, *waits}


@pytest.mark.parametrize("scope", ["phase", "lane", "project"])
def test_unknown_and_retired_scopes_are_refused(scope: str) -> None:
    with pytest.raises(ValueError):
        normalize_reaction_scope(scope, "x")
    with pytest.raises(ValueError):
        workflow_reaction_sources.builtin_workflow_reaction_event_options(scope)


def test_a_reaction_key_cannot_smuggle_a_scope_prefix() -> None:
    with pytest.raises(ValueError, match="must not start with"):
        compose_scoped_reaction_key("workflow", "", "scope::phase::x::rule")


def test_merge_drops_rules_of_the_retired_phase_scope() -> None:
    _, _, merged = merge_scoped_reaction_rules(
        [
            {"reaction_key": "scope::phase::build::legacy"},
            {"reaction_key": "scope::step::review::keep"},
        ],
        [{"reaction_key": "new"}],
        scope_kind="workflow",
        scope_id="",
    )

    assert [row["reaction_key"] for row in merged] == ["scope::step::review::keep", "new"]


@pytest.mark.parametrize(
    ("retired", "replacement"),
    [
        ("job.phase_started", "job.group_started"),
        ("job.phase_completed", "job.group_completed"),
        ("job.phase_failed", "job.group_failed"),
        ("job.gate_waiting", "job.approval_waiting"),
    ],
)
def test_validator_refuses_retired_events_naming_the_replacement(
    retired: str, replacement: str
) -> None:
    sources = workflow_reaction_sources.build_workflow_reaction_trigger_sources(
        object(), scope_kind="workflow"
    )
    with pytest.raises(ValueError, match=replacement):
        workflow_reaction_sources.validate_workflow_reaction_rules(
            [{"event_type": retired}], available_sources=sources, scope_kind="workflow"
        )


def test_producer_names_map_to_public_events() -> None:
    assert public_reaction_event_type("job.awaiting_approval") == "job.approval_waiting"
    assert public_reaction_event_type("job.awaiting_timer") == "job.timer_waiting"
    assert public_reaction_event_type("job.completed") == "job.completed"


def test_wait_event_identity_is_the_wait_id_not_the_producer_occurrence() -> None:
    first = reaction_event_id(
        job_id="job-1",
        event_type="job.awaiting_approval",
        payload={"wait_id": "w-1"},
        fallback_event_id="first-run-hmac",
    )
    again = reaction_event_id(
        job_id="job-1",
        event_type="job.awaiting_approval",
        payload={"wait_id": "w-1"},
        fallback_event_id="approval-resume:job-1:3:job.awaiting_approval:a-1",
    )

    assert first == again == "wf-event:job-1:job.approval_waiting:w-1"
    assert recovery_wait_id(request_id="req-1", approval_id="i-1") == "recovery:req-1"
    assert recovery_wait_id(approval_id="i-1") == "approval:i-1"


# --- lifecycle evaluation ---------------------------------------------------------


class _Store:
    """Rules per workflow and job rows: the lifecycle reaction store."""

    def __init__(self, rules: list[dict[str, Any]], jobs: dict[str, dict[str, Any]] | None = None):
        self.rules = rules
        self.jobs = jobs or {}
        self.approvals: dict[str, dict[str, Any]] = {}

    def list_workflow_reactions(self, workflow_id, enabled_only=True):
        return [rule for rule in self.rules if rule["workflow_id"] == workflow_id]

    def get_job(self, job_id):
        return self.jobs.get(str(job_id))

    def get_public_progress_snapshot(self, job_id):
        job = self.jobs.get(job_id)
        approval = self.approvals.get(job_id)
        if not job or not approval:
            raise LookupError(job_id)
        return SimpleNamespace(
            snapshot=SimpleNamespace(
                job_id=job_id,
                project_id=job["project_id"],
                workflow_id=job["meta"]["selected_workflow_id"],
                status=job["status"],
                approval=SimpleNamespace(model_dump=lambda **_: dict(approval)),
            )
        )


def _rule(workflow_id: str, event_type: str, key: str = "notify", **extra: Any) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "reaction_key": key,
        "event_type": event_type,
        "enabled": True,
        "conditions": {},
        "action": {
            "type": "notify",
            "target": "extension_channel",
            "provider_key": "acme.chat",
            "channel_id": "ops",
        },
        **extra,
    }


@pytest.fixture
def dispatched(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def dispatch(_bl, **kwargs):
        calls.append(kwargs)
        return [{"status": "delivered"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", dispatch)
    return calls


def _run(store: _Store, event_type: str, payload: dict[str, Any], *, job_id="job-1", wf="wf"):
    return run_job_lifecycle_reactions(
        store,
        event_type=event_type,
        project_id="p-1",
        workflow_id=wf,
        job_id=job_id,
        event_id="producer-id",
        payload=payload,
        to_json_obj=lambda value: value,
    )


def test_one_notification_identity_per_wait_across_producers(dispatched) -> None:
    store = _Store([_rule("wf", "job.approval_waiting")])
    payload = {"wait_id": "w-1", "approval_id": "a-1", "reason": "authored"}

    _run(store, "job.awaiting_approval", payload)
    _run(store, "job.awaiting_approval", payload)

    assert [call["event_id"] for call in dispatched] == [
        "wf-event:job-1:job.approval_waiting:w-1:rule:notify",
        "wf-event:job-1:job.approval_waiting:w-1:rule:notify",
    ]
    assert dispatched[0]["payload"]["wait_id"] == "w-1"
    assert dispatched[0]["payload"]["reason"] == "authored"


def test_timer_waits_reach_timer_reactions(dispatched) -> None:
    store = _Store([_rule("wf", "job.timer_waiting")])

    _run(store, "job.awaiting_timer", {"wait_id": "t-1", "due_at": "2026-10-13T00:00:00+00:00"})

    assert [call["event_type"] for call in dispatched] == ["job.timer_waiting"]
    assert dispatched[0]["payload"]["due_at"] == "2026-10-13T00:00:00+00:00"


def test_two_rules_on_one_route_are_two_explicit_subscriptions(dispatched) -> None:
    store = _Store(
        [_rule("wf", "job.step_failed", key="first"), _rule("wf", "job.step_failed", key="second")]
    )

    _run(store, "job.step_failed", {"step_id": "review", "step_execution_id": "x-1"})

    assert [call["event_id"] for call in dispatched] == [
        "producer-id:job.step_failed:rule:first",
        "producer-id:job.step_failed:rule:second",
    ]


def test_scoped_rules_observe_their_group_and_step(dispatched) -> None:
    store = _Store(
        [
            _rule("wf", "job.step_failed", key="scope::group::batch::g"),
            _rule("wf", "job.step_failed", key="scope::step::review::s"),
            _rule("wf", "job.step_failed", key="scope::step::other::never"),
            _rule("wf", "job.step_failed", key="scope::phase::batch::retired"),
        ]
    )

    results = _run(
        store,
        "job.step_failed",
        {"step_id": "review", "step_execution_id": "x-1", "group_id": "batch"},
    )

    assert [row["reaction_key"] for row in results] == [
        "scope::group::batch::g",
        "scope::step::review::s",
    ]


# --- approval propagation -----------------------------------------------------------


def _parent(
    job_id: str,
    *,
    child_id: str,
    workflow: str,
    parent_id: str | None = None,
    status: str = "awaiting_child",
    step_execution_id: str | None = None,
    link: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution = step_execution_id or f"{job_id}-invoke-exec"
    meta: dict[str, Any] = {
        "selected_workflow_id": workflow,
        "workflow_wait": {
            "kind": "child",
            "wait_id": f"{job_id}-child-wait",
            "child_job_id": child_id,
            "step_id": "call_child",
            "step_execution_id": execution,
            "attempt": 1,
            "group_id": "fanout" if job_id == "root" else None,
        },
        "invoke_children": {
            child_id: {
                "invocation_key": f"{job_id}:{execution}",
                "step_execution_id": execution,
                **(link or {}),
            }
        },
    }
    if parent_id:
        meta["invoke_parent_job_id"] = parent_id
    return {"id": job_id, "project_id": "p-1", "status": status, "meta": meta}


def _approval_child(parent_id: str = "parent") -> dict[str, Any]:
    return {
        "id": "child",
        "project_id": "p-1",
        "status": "awaiting_approval",
        "meta": {
            "selected_workflow_id": "wf-child",
            "invoke_parent_job_id": parent_id,
            "workflow_wait": {
                "kind": "approval",
                "wait_id": "w-1",
                "approval_id": "exec-approval",
                "step_id": "approve",
                "step_execution_id": "child-exec",
            },
        },
    }


def _chain_store(*, root_status: str = "awaiting_child", rules: list | None = None) -> _Store:
    jobs = {
        "child": _approval_child(),
        "parent": _parent("parent", child_id="child", workflow="wf-parent", parent_id="root"),
        "root": _parent("root", child_id="parent", workflow="wf-root", status=root_status),
    }
    store = _Store(
        rules
        if rules is not None
        else [
            _rule("wf-child", "job.approval_waiting"),
            _rule("wf-parent", "job.approval_waiting"),
            _rule("wf-root", "job.approval_waiting"),
        ],
        jobs,
    )

    store.approvals["child"] = {
        "job_id": "child",
        "approval_id": "exec-approval",
        "wait_id": "w-1",
        "reason": "authored",
        "allowed_actions": ["approve", "reject"],
        "step_id": "approve",
        "step_execution_id": "child-exec",
        "group_id": None,
    }
    return store


_CHILD_APPROVAL = {
    "wait_id": "w-1",
    "approval_id": "exec-approval",
    "reason": "authored",
    "allowed_actions": ["approve", "reject"],
    "step_id": "approve",
    "step_execution_id": "child-exec",
    "content": {"summary": "review output must not travel"},
}


def test_child_approval_notifies_every_waiting_ancestor(dispatched) -> None:
    store = _chain_store()

    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")

    assert [(call["workflow_id"], call["job_id"]) for call in dispatched] == [
        ("wf-child", "child"),
        ("wf-parent", "child"),
        ("wf-root", "child"),
    ]
    parent_payload = dispatched[1]["payload"]
    root_payload = dispatched[2]["payload"]
    for payload in (parent_payload, root_payload):
        assert payload["propagated"] is True
        assert payload["origin_job_id"] == "child"
        assert payload["origin_workflow_id"] == "wf-child"
        assert payload["origin_step_execution_id"] == "child-exec"
        assert payload["approval_job_id"] == "child"
        assert payload["approval_id"] == "exec-approval"
        assert payload["wait_id"] == "w-1"
        assert payload["source_event_type"] == "job.approval_waiting"
        assert "content" not in payload
        assert "review output" not in str(payload)
    # The immediate parent's child is the approval job; the root's child is the parent.
    assert parent_payload["child_job_id"] == "child"
    assert root_payload["child_job_id"] == "parent"
    # Scope is the ancestor's own invoke execution, never the inner child step.
    assert parent_payload["step_id"] == "call_child"
    assert parent_payload["step_execution_id"] == "parent-invoke-exec"
    assert root_payload["group_id"] == "fanout"
    # Identity: origin wait, ancestor job, rule (route identity is added by the claim).
    assert dispatched[1]["event_id"] == (
        "wf-event:parent:job.approval_waiting:w-1:propagated:rule:notify"
    )
    assert dispatched[2]["event_id"] == (
        "wf-event:root:job.approval_waiting:w-1:propagated:rule:notify"
    )


def test_ancestor_group_and_step_rules_match_the_invoke_execution(dispatched) -> None:
    store = _chain_store(
        rules=[
            _rule("wf-root", "job.approval_waiting", key="scope::group::fanout::g"),
            _rule("wf-parent", "job.approval_waiting", key="scope::step::call_child::s"),
            _rule("wf-parent", "job.approval_waiting", key="scope::step::approve::inner"),
        ]
    )

    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")

    assert [(call["job_id"], call["event_id"].rsplit(":rule:", 1)[1]) for call in dispatched] == [
        ("child", "scope::step::call_child::s"),
        ("child", "scope::group::fanout::g"),
    ]


def test_propagation_stops_at_a_terminal_ancestor(dispatched) -> None:
    store = _chain_store(root_status="cancelled")

    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")

    assert [call["job_id"] for call in dispatched] == ["child", "child"]


def test_propagation_needs_the_trusted_link_and_a_pending_approval(dispatched) -> None:
    store = _chain_store()
    store.jobs["parent"]["meta"]["invoke_children"] = {}
    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")
    assert [call["job_id"] for call in dispatched] == ["child"]

    dispatched.clear()
    store = _chain_store()
    store.jobs["parent"]["meta"]["invoke_children"]["child"]["cancel_requested_at"] = "now"
    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")
    assert [call["job_id"] for call in dispatched] == ["child"]

    dispatched.clear()
    store = _chain_store()
    store.jobs["child"]["status"] = "in_progress"  # already decided
    _run(store, "job.awaiting_approval", _CHILD_APPROVAL, job_id="child", wf="wf-child")
    assert [call["job_id"] for call in dispatched] == ["child"]


def test_extension_payload_lineage_is_never_trusted(dispatched) -> None:
    store = _chain_store()
    store.jobs["child"]["meta"].pop("invoke_parent_job_id")

    _run(
        store,
        "job.awaiting_approval",
        {**_CHILD_APPROVAL, "invoke_parent_job_id": "parent", "origin_job_id": "root"},
        job_id="child",
        wf="wf-child",
    )

    assert [call["job_id"] for call in dispatched] == ["child"]


def test_a_propagated_event_never_bubbles_again(dispatched) -> None:
    store = _chain_store()

    _run(
        store,
        "job.approval_waiting",
        propagated_payload(
            {"job_id": "child", "workflow_id": "wf-child", "payload": _CHILD_APPROVAL},
            child_job_id="child",
            ancestor_wait=store.jobs["parent"]["meta"]["workflow_wait"],
        ),
        job_id="parent",
        wf="wf-parent",
    )

    assert [call["job_id"] for call in dispatched] == ["child"]


def test_lineage_cycles_and_depth_are_bounded() -> None:
    store = _chain_store()
    # root claims to be a child of parent: a cycle.
    store.jobs["root"]["meta"]["invoke_parent_job_id"] = "parent"
    store.jobs["parent"]["meta"]["invoke_children"]["root"] = {"step_execution_id": "x"}
    evaluated: list[str] = []
    origin = {
        "job_id": "child",
        "workflow_id": "wf-child",
        "event_id": "e",
        "payload": dict(_CHILD_APPROVAL),
    }

    propagate_approval_waiting(
        store, origin, evaluate=lambda event: evaluated.append(event["job_id"]) or []
    )
    assert evaluated == ["parent", "root"]

    evaluated.clear()
    propagate_approval_waiting(
        store, origin, evaluate=lambda event: evaluated.append(event["job_id"]) or [], max_depth=1
    )
    assert evaluated == ["parent"]


def test_a_parent_that_parks_late_catches_up_the_pending_child_approval() -> None:
    store = _chain_store()

    origin = pending_child_approval_event(store, "root")

    assert origin is not None
    assert origin["job_id"] == "child"
    assert origin["payload"]["wait_id"] == "w-1"
    assert origin["payload"]["reason"] == "authored"


# --- step and group events from the hub -----------------------------------------------


def test_hub_events_are_published_only_through_a_bound_sink() -> None:
    state = {"job_id": "job-1", "workflow_id": "wf"}
    received: list[dict[str, Any]] = []

    assert (
        publish_step_event(
            state, "job.step_started", step_id="s", step_execution_id="x-1", attempt=1
        )
        is False
    )
    with bind_workflow_event_sink(lambda **event: received.append(event)):
        publish_step_event(
            state, "job.step_started", step_id="s", step_execution_id="x-1", attempt=2
        )
    publish_step_event(state, "job.step_started", step_id="s", step_execution_id="x-1", attempt=3)

    assert len(received) == 1
    assert received[0]["event_id"] == "wf-event:job-1:job.step_started:x-1:2"
    assert received[0]["payload"]["attempt"] == 2
    assert received[0]["payload"]["group_id"] is None


def test_group_events_need_a_real_group_execution() -> None:
    received: list[dict[str, Any]] = []
    state = {
        "job_id": "job-1",
        "workflow_id": "wf",
        "run_cursor": {
            "frames": [{"frame": "group", "group_id": "batch", "group_execution_id": "ge-1"}]
        },
    }
    with bind_workflow_event_sink(lambda **event: received.append(event)):
        assert publish_group_event(state, "job.group_started", group_id="batch") is True
        assert publish_group_event(state, "job.group_failed", group_id="unknown") is False
        publish_step_event(
            state,
            "job.step_completed",
            step_id="item",
            step_execution_id="x-9",
            attempt=1,
            group_id="batch",
        )

    assert received[0]["event_id"] == "wf-event:job-1:job.group_started:ge-1"
    assert received[0]["payload"]["group_execution_id"] == "ge-1"
    assert received[1]["payload"]["group_execution_id"] == "ge-1"
    with pytest.raises(ValueError):
        publish_group_event(state, "job.phase_started", group_id="batch")


def test_a_failing_sink_never_reaches_the_hub() -> None:
    def broken(**_event):
        raise RuntimeError("store down")

    with bind_workflow_event_sink(broken):
        assert (
            publish_step_event(
                {"job_id": "j", "workflow_id": "wf"},
                "job.step_failed",
                step_id="s",
                step_execution_id="x",
                attempt=1,
            )
            is False
        )


def test_worker_sink_skips_workflows_without_a_matching_rule(dispatched) -> None:
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerLifecycleEventSink,
    )

    store = _Store(
        [_rule("wf", "job.step_failed")],
        {"job-1": {"id": "job-1", "project_id": "p-1", "meta": {}}},
    )
    sink = WorkerLifecycleEventSink(store, lambda: store)

    for event_type in ("job.step_started", "job.step_failed"):
        sink(
            event_type=event_type,
            job_id="job-1",
            workflow_id="wf",
            event_id=f"wf-event:job-1:{event_type}:x-1:1",
            payload={"step_id": "s", "step_execution_id": "x-1", "attempt": 1},
        )

    assert [call["event_type"] for call in dispatched] == ["job.step_failed"]


# --- wait payloads and the standard email ----------------------------------------------


def test_timer_wait_payload_from_checkpoint_state() -> None:
    from core.application.jobs.workflow_wait_events import wait_notification_payload_from_state

    wait = {
        "wait_id": "t-1",
        "kind": "timer",
        "step_id": "pause",
        "step_execution_id": "x-1",
        "attempt": 1,
        "group_id": None,
        "duration_ms": 60000,
        "due_at": "2026-10-13T00:00:00+00:00",
        "due_epoch_ms": 1,
    }
    state = SimpleNamespace(
        values={"pending_wait": wait},
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={**wait, "gate_kind": "wait"})])],
    )

    payload = wait_notification_payload_from_state("awaiting_timer", state)

    assert payload == {
        "step_id": "pause",
        "step_execution_id": "x-1",
        "group_id": None,
        "attempt": 1,
        "wait_id": "t-1",
        "due_at": "2026-10-13T00:00:00+00:00",
        "duration_ms": 60000,
    }


def test_event_email_names_a_group_only_for_a_real_group() -> None:
    _, root_body = render_event_email(
        {
            "event_type": "job.approval_waiting",
            "job_id": "job-1",
            "payload": {"step_id": "approve", "group_id": None},
        },
        base_url="https://fs.example",
    )
    _, group_body = render_event_email(
        {
            "event_type": "job.group_failed",
            "job_id": "job-1",
            "payload": {"group_id": "batch", "group_label": "Batch review"},
        }
    )

    assert "Group:" not in root_body
    assert "Phase" not in root_body
    assert "Step: approve" in root_body
    assert "Group: Batch review" in group_body
    assert "Step:" not in group_body


def test_event_email_links_a_propagated_approval_to_the_approval_job() -> None:
    _, body = render_event_email(
        {
            "event_type": "job.approval_waiting",
            "job_id": "parent",
            "payload": {"propagated": True, "approval_job_id": "child", "approval_id": "a-1"},
        },
        base_url="https://fs.example",
    )

    assert "https://fs.example/jobs/child" in body
    assert "/jobs/parent" not in body


@pytest.mark.parametrize("scope", ["step", "group"])
@pytest.mark.parametrize(
    ("diagnostic", "secret"),
    [
        ("HTTP failed: Authorization: Bearer test-secret-value", "test-secret-value"),
        ("Cannot reach https://user:private-pass@example.test/orders", "private-pass"),
        ('Remote error {"password": "test-private-value"}', "test-private-value"),
        ("x" * 480 + " sk-" + "a" * 60, "sk-" + "a" * 12),
    ],
)
def test_step_and_group_errors_are_redacted_before_the_notification_sink(scope, diagnostic, secret):
    received = []
    state = {"job_id": "j", "workflow_id": "wf"}
    with bind_workflow_event_sink(lambda **event: received.append(event)):
        if scope == "step":
            publish_step_event(
                state,
                "job.step_failed",
                step_id="s",
                step_execution_id="x",
                attempt=2,
                error=diagnostic,
            )
        else:
            publish_group_event(
                state,
                "job.group_failed",
                group_id="g",
                group_execution_id="gx",
                error=diagnostic,
            )
    assert len(received) == 1
    assert secret not in received[0]["payload"]["error"]
    assert len(received[0]["payload"]["error"]) <= 500
    assert received[0]["job_id"] == "j"
    assert received[0]["workflow_id"] == "wf"


def test_a_nonsecret_step_error_remains_actionable_in_notifications():
    received = []
    with bind_workflow_event_sink(lambda **event: received.append(event)):
        publish_step_event(
            {"job_id": "j", "workflow_id": "wf"},
            "job.step_failed",
            step_id="http",
            step_execution_id="x",
            attempt=1,
            error="HTTP 504: upstream timed out",
        )
    assert received[0]["payload"]["error"] == "HTTP 504: upstream timed out"
