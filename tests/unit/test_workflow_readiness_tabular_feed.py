"""Every step kind the builder offers must validate as itself.

Remediation epic 2026-07-29, WS-04 — `AUDIT-035` (P0).

Readiness validation dispatched on step kind for connector, invoke_workflow,
dataset_query, http_request, transform and the control-flow kinds, then fell
through to agent validation for anything else. `tabular_feed` is offered by the
Step kind selector and its editor has no Agent field at all, so saving a
correctly configured Tabular Feed step failed with:

    Phase 'start' step #2 is missing agent_id or participant_key

(Workflows have no phases any more; the same failure now reads
``Step '<step_id>' is missing agent_id or participant_key``.)

The config contract already existed in
`core/application/tabular_feed/validation.py`; readiness simply never reached it.

The audit's wider ask is asserted here too: *every* kind the selector exposes
must have a readiness branch, so the next kind added cannot silently inherit the
agent fallback.
"""

from __future__ import annotations

import pytest

from core.application.registry.contract_validation_workflow_readiness import (
    validate_workflow_readiness,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_TABULAR_FEED,
    SUPPORTED_STEP_KINDS,
)
from tests._workflow_definitions import linear_workflow

VALID_TABULAR_FEED = {
    "operation": "inspect",
    "input": {"artifact_handle": "state.feed_handle"},
    "result_target": "state.feed_summary",
}


def workflow_with_step(step: dict) -> dict:
    return linear_workflow(step, workflow_id="wf_test", display_name="WF")


class FakeDb:
    """Minimal store: readiness fetches the workflow by id."""

    def __init__(self, workflow: dict):
        self._workflow = workflow

    def get_workflow(self, _workflow_id):
        return self._workflow

    def get_agent(self, _agent_id):
        return None


def readiness_errors(step: dict) -> list[dict]:
    workflow = workflow_with_step(step)
    result = validate_workflow_readiness(FakeDb(workflow), workflow["workflow_id"])
    return list((result or {}).get("errors") or [])


def codes(errors) -> set[str]:
    return {str(e.get("code") or "") for e in errors}


class TestTabularFeedIsNotTreatedAsAnAgentStep:
    def test_a_valid_tabular_feed_step_is_not_rejected_for_a_missing_agent(self):
        """The exact AUDIT-035 failure."""
        errors = readiness_errors(
            {
                "step_id": "feed",
                "step_kind": STEP_KIND_TABULAR_FEED,
                "override": {STEP_KIND_TABULAR_FEED: VALID_TABULAR_FEED},
            }
        )

        assert "workflow.step_agent_missing" not in codes(errors), (
            "tabular_feed fell through to the agent fallback again"
        )

    def test_no_agent_error_mentions_the_step(self):
        errors = readiness_errors(
            {
                "step_id": "feed",
                "step_kind": STEP_KIND_TABULAR_FEED,
                "override": {STEP_KIND_TABULAR_FEED: VALID_TABULAR_FEED},
            }
        )
        messages = " ".join(str(e.get("message") or "") for e in errors)

        assert "agent_id" not in messages
        assert "participant_key" not in messages


class TestTabularFeedConfigIsActuallyValidated:
    """Not rejecting for the wrong reason must not mean accepting anything."""

    def test_a_missing_config_is_reported_against_the_tabular_feed_path(self):
        errors = readiness_errors({"step_id": "feed", "step_kind": STEP_KIND_TABULAR_FEED})

        assert errors, "an unconfigured tabular_feed step must not validate"
        assert any("tabular_feed" in str(e.get("code") or "") for e in errors)
        assert any("tabular_feed" in str(e.get("path") or "") for e in errors)

    def test_an_unknown_operation_is_reported(self):
        errors = readiness_errors(
            {
                "step_id": "feed",
                "step_kind": STEP_KIND_TABULAR_FEED,
                "override": {STEP_KIND_TABULAR_FEED: {"operation": "not_a_real_operation"}},
            }
        )

        assert any("operation" in str(e.get("path") or "") for e in errors)

    def test_an_unsupported_field_is_reported(self):
        errors = readiness_errors(
            {
                "step_id": "feed",
                "step_kind": STEP_KIND_TABULAR_FEED,
                "override": {STEP_KIND_TABULAR_FEED: {**VALID_TABULAR_FEED, "not_a_field": 1}},
            }
        )

        assert any("not_a_field" in str(e.get("path") or "") for e in errors)

    def test_errors_name_the_step_and_its_path(self):
        errors = readiness_errors({"step_id": "feed", "step_kind": STEP_KIND_TABULAR_FEED})
        tabular_errors = [e for e in errors if "tabular_feed" in str(e.get("code") or "")]

        assert tabular_errors
        for error in tabular_errors:
            assert "Step 'feed'" in str(error.get("message") or ""), (
                "an author needs to know which step"
            )
            assert str(error.get("path") or "").startswith("workflow.steps.feed.")


class TestEveryOfferedKindHasAReadinessBranch:
    """The audit's wider ask, so the next kind cannot inherit the agent fallback."""

    # Sorted: every pytest-xdist worker must collect the same ids in the same order,
    # and a set iterates in string-hash order, which differs per process.
    NON_AGENT_KINDS = tuple(sorted(k for k in SUPPORTED_STEP_KINDS if k != "agent"))

    @pytest.mark.parametrize("step_kind", NON_AGENT_KINDS)
    def test_a_non_agent_kind_is_never_rejected_for_a_missing_agent(self, step_kind):
        errors = readiness_errors({"step_id": "s", "step_kind": step_kind})

        assert "workflow.step_agent_missing" not in codes(errors), (
            f"{step_kind} falls through to agent validation — it has no readiness branch"
        )

    def test_an_agent_step_without_an_agent_is_still_rejected(self):
        """The fallback must keep working for the kind it is for."""
        errors = readiness_errors({"step_id": "s", "step_kind": "agent"})

        assert "workflow.step_agent_missing" in codes(errors)
