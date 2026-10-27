from __future__ import annotations

from core.application.extensions.entitlement_lifecycle_v2 import (
    begin_drain_window,
    build_uninstall_drain_plan,
    can_start_new_invocation,
    normalize_entitlement_state,
    record_usage_event_idempotent,
)


def test_entitlement_state_alignment_and_invocation_gating() -> None:
    assert normalize_entitlement_state("trial_active") == "active"
    assert normalize_entitlement_state("grace_period") == "grace"
    assert normalize_entitlement_state("suspended_nonpayment") == "suspended"
    assert normalize_entitlement_state("disabled") == "terminated"

    allowed, reason = can_start_new_invocation(entitlement_state="active")
    assert allowed is True
    assert reason == ""

    allowed, reason = can_start_new_invocation(entitlement_state="suspended", inflight_count=2)
    assert allowed is False
    assert "in-flight" in reason

    allowed, reason = can_start_new_invocation(entitlement_state="terminated")
    assert allowed is False
    assert "terminated" in reason


def test_uninstall_dependency_checks_and_drain_plan() -> None:
    blocked = build_uninstall_drain_plan(
        install_id="pi_1",
        dependent_workflow_ids=["wf_1"],
        inflight_invocations=1,
        force=False,
    )
    assert blocked["allowed"] is False
    assert blocked["needs_drain"] is True

    forced = build_uninstall_drain_plan(
        install_id="pi_1",
        dependent_workflow_ids=["wf_1"],
        inflight_invocations=1,
        force=True,
    )
    assert forced["allowed"] is True
    assert forced["mode"] == "forced"

    drain = begin_drain_window(install_id="pi_1", ttl_seconds=120, reason="dependency cleanup")
    assert drain["drain_until"] > drain["drain_started_at"]
    assert drain["reason"] == "dependency cleanup"


def test_usage_event_recording_is_idempotent() -> None:
    store: dict[str, dict] = {}
    first = record_usage_event_idempotent(
        usage_store=store,
        event_id="evt_1",
        payload={"install_id": "pi_1", "event_kind": "action_invoked", "quantity": 1},
    )
    second = record_usage_event_idempotent(
        usage_store=store,
        event_id="evt_1",
        payload={"install_id": "pi_1", "event_kind": "action_invoked", "quantity": 2},
    )
    assert first == second
    assert len(store) == 1
