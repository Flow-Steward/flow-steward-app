"""A Test Run that awaits approval must be decidable.

Remediation epic 2026-07-29, WS-04 — part of `AUDIT-038` (P0).

The reviewer put a `human_approval` gate on a phase, ran the workflow to
`awaiting_approval`, entered a comment and clicked *Reject and continue*. The UI
answered:

    Workflow test run approval ID is unavailable. Refresh before deciding.

and left the run awaiting approval. Refreshing did not help — it removed the
decision controls entirely.

The frontend reads `approval_id` from the Test Run payload
(`detail-test-run-controller.ts:97`). The payload resolved it **only** from the
checkpoint snapshot, which is written independently of the state the graph
returns. A run could therefore report `awaiting_approval` with `approval_id:
null`, leaving no way to decide it.

Two changes are covered here:

- the id is now also recovered from the run state when the snapshot has none;
- when neither source has one, the payload says so in
  `approval_undecidable_reason` instead of presenting a decidable-looking
  approval with a null id.

This is the "cannot be decided" half of AUDIT-038. The gate-ordering half — the
side effect running before the approval is requested — is a separate change and
is not covered by this file.
"""

from __future__ import annotations

import pytest

from core.application.workflows import platform_test_run_runtime as rt


class FakeInterrupt:
    def __init__(self, interrupt_id: str):
        self.id = interrupt_id


class FakeTask:
    def __init__(self, interrupts):
        self.interrupts = interrupts


class FakeSnapshot:
    def __init__(self, tasks):
        self.tasks = tasks


class TestApprovalIdFromTheCheckpoint:
    def test_an_interrupt_in_the_snapshot_supplies_the_id(self, monkeypatch):
        monkeypatch.setattr(
            rt,
            "_get_thread_state_snapshot",
            lambda _t: FakeSnapshot([FakeTask([FakeInterrupt("int-abc")])]),
        )

        assert rt._current_approval_id("thread-1") == "int-abc"

    @pytest.mark.parametrize(
        "snapshot",
        [None, FakeSnapshot([]), FakeSnapshot([FakeTask([])])],
    )
    def test_a_snapshot_without_an_interrupt_yields_nothing(self, monkeypatch, snapshot):
        """The condition that produced the dead end."""
        monkeypatch.setattr(rt, "_get_thread_state_snapshot", lambda _t: snapshot)

        assert rt._current_approval_id("thread-1") == ""

    def test_an_empty_thread_id_yields_nothing(self):
        assert rt._current_approval_id("") == ""


class TestApprovalIdRecoveredFromRunState:
    """The fallback that keeps the run decidable."""

    @pytest.mark.parametrize("key", ["approval_id", "interrupt_id"])
    def test_a_top_level_id_is_recovered(self, key):
        assert rt._approval_id_from_state({key: "int-from-state"}) == "int-from-state"

    @pytest.mark.parametrize("container", ["pending_interrupt", "approval_interrupt"])
    @pytest.mark.parametrize("key", ["approval_id", "id", "interrupt_id"])
    def test_a_nested_id_is_recovered(self, container, key):
        assert rt._approval_id_from_state({container: {key: "int-nested"}}) == "int-nested"

    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"approval_id": ""},
            {"approval_id": "   "},
            {"pending_interrupt": None},
            {"pending_interrupt": "not-a-dict"},
            # The removed legacy intent list is no longer an identity source.
            {"approval_resume_intents": [{"approval_id": "int-intent"}]},
        ],
    )
    def test_nothing_usable_yields_nothing(self, state):
        assert rt._approval_id_from_state(state) == ""


class TestTheContractHolds:
    """`awaiting_approval` must never be presented as decidable when it is not."""

    def test_the_payload_carries_an_undecidable_reason_field(self):
        from pathlib import Path

        source = Path("core/application/workflows/platform_test_run_runtime.py").read_text(
            encoding="utf-8"
        )

        assert '"approval_undecidable_reason"' in source, (
            "a null approval_id with no explanation is what the UI could only "
            "report as 'unavailable, refresh before deciding'"
        )

    def test_the_state_fallback_is_consulted_before_giving_up(self):
        from pathlib import Path

        source = Path("core/application/workflows/platform_test_run_runtime.py").read_text(
            encoding="utf-8"
        )
        start = source.index('approval_undecidable_reason = ""')
        block = source[start : start + 900]

        assert "_current_approval_id(" in block
        assert "_approval_id_from_state(" in block
        assert block.index("_current_approval_id(") < block.index("_approval_id_from_state("), (
            "the checkpoint remains the primary source; the state is the fallback"
        )
