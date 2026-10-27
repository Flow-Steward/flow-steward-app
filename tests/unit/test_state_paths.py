"""One state-path language across step kinds — STATE-001.

A `set_variable` step stored `[{"stock": 7}]`, the next step read
`seed_rows[0].stock`, and the job finished green having resolved `null`: the
connector, control-flow and agent resolvers walked mappings only, while the
transform step understood the very same path. These tests pin the one language.
"""

from __future__ import annotations

import pytest

from core.application.orchestration import (
    connector_mapping,
    control_flow_step_helpers,
    expected_input_sources,
)
from core.application.orchestration.state_paths import (
    StatePathError,
    resolve_state_path,
    strip_state_prefix,
)

_STATE = {
    "seed_rows": [{"stock": 7}, {"stock": 9}],
    "empty": [],
    "digits": {"0": "a mapping key, not an index"},
    "explicit_none": None,
    "workflow_data": {"hit": [{"id": 27, "sku": "SKU-1"}]},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("seed_rows[0].stock", 7),
        ("seed_rows[1].stock", 9),
        ("state.seed_rows[0].stock", 7),
        # `rows.0.stock` is how an author writes it without the bracket syntax.
        ("seed_rows.0.stock", 7),
        ("digits.0", "a mapping key, not an index"),
    ],
)
def test_indexed_paths_resolve(path: str, expected: object) -> None:
    assert resolve_state_path(_STATE, path) == (expected, True)


@pytest.mark.parametrize(
    "path",
    ["seed_rows[2].stock", "empty[0]", "seed_rows[0].missing", "absent.key"],
)
def test_paths_that_run_off_the_end_report_missing(path: str) -> None:
    value, found = resolve_state_path(_STATE, path)
    assert (value, found) == (None, False)


def test_a_resolved_none_is_not_the_same_as_missing() -> None:
    """The distinction a caller needs to stop turning a typo into a green run."""
    assert resolve_state_path(_STATE, "explicit_none") == (None, True)
    assert resolve_state_path(_STATE, "no_such_key") == (None, False)


def test_malformed_paths_are_rejected_rather_than_silently_empty() -> None:
    with pytest.raises(StatePathError):
        resolve_state_path(_STATE, "seed_rows[-1].stock")
    with pytest.raises(StatePathError):
        resolve_state_path(_STATE, "seed_rows[bad].stock")


def test_strip_state_prefix_is_optional_everywhere() -> None:
    assert strip_state_prefix("state.rows[0]") == "rows[0]"
    assert strip_state_prefix("rows[0]") == "rows[0]"
    assert strip_state_prefix("state") == ""


@pytest.mark.parametrize(
    "resolver",
    [
        control_flow_step_helpers._resolve_state_path,
        connector_mapping._resolve_state_path,
        expected_input_sources.resolve_state_path,
    ],
    ids=["control-flow", "connector", "agent-input"],
)
def test_every_step_family_reads_the_same_indexed_path(resolver) -> None:
    assert resolver(_STATE, "seed_rows[0].stock") == 7
    assert resolver(_STATE, "seed_rows.0.stock") == 7


@pytest.mark.parametrize(
    "resolver",
    [
        control_flow_step_helpers._resolve_state_path,
        connector_mapping._resolve_state_path,
        expected_input_sources.resolve_state_path,
    ],
    ids=["control-flow", "connector", "agent-input"],
)
def test_every_step_family_still_falls_back_to_workflow_data(resolver) -> None:
    assert resolver(_STATE, "hit[0].id") == 27


def test_control_flow_prefers_a_present_null_over_a_stale_mirror() -> None:
    state = {"approved": None, "workflow_data": {"approved": True}}

    assert control_flow_step_helpers._evaluate_condition("state.approved", state) is False


def test_control_flow_present_null_blocks_stale_public_containers() -> None:
    state = {
        "approved": None,
        "outputs": {"approved": True},
        "artifacts": {"approved": True},
    }

    assert control_flow_step_helpers._evaluate_condition("state.approved", state) is False


def test_templates_prefer_a_present_null_and_then_apply_their_default_filter() -> None:
    from core.application.orchestration.step_templating import _render_template_string

    state = {"approved": None, "workflow_data": {"approved": True}}

    assert _render_template_string("{{ state.approved }}", state) == ""
    assert _render_template_string("{{ state.approved | default:false }}", state) == "False"


def test_connector_mapping_prefers_a_present_null_before_applying_defaults() -> None:
    state = {"approved": None, "workflow_data": {"approved": True}}

    payload, unresolved = connector_mapping._render_input_payload(
        {
            "plain": "state.approved",
            "with_default": "state.approved | default(false)",
        },
        state,
    )

    assert unresolved == {}
    assert payload == {"plain": None, "with_default": False}


def test_agent_input_prefers_a_present_null_over_a_stale_mirror() -> None:
    state = {"approved": None, "workflow_data": {"approved": True}}

    assert expected_input_sources.resolve_state_path(state, "approved") is None


def test_agent_input_present_null_blocks_stale_public_containers() -> None:
    state = {
        "approved": None,
        "materialized_inputs": {"approved": True},
        "inputs": {"approved": True},
        "outputs": {"approved": True},
        "artifacts": {"approved": True},
    }

    assert expected_input_sources.resolve_runtime_public_value(state, "approved") is None


# ---------------------------------------------------------------------------
# One selector language, everywhere
#
# The connector step learned indexes; transform kept its own dict-only walker,
# so `state.rows[0].sku` resolved in one step kind and returned nothing in the
# next. An author cannot be expected to know which step they are standing in.
# ---------------------------------------------------------------------------


def test_transform_resolves_an_indexed_selector() -> None:
    from core.application.orchestration.step_templating import _resolve_path

    state = {"rows": [{"sku": "A1"}, {"sku": "B2"}]}

    assert _resolve_path(state, "rows[0].sku") == "A1"
    assert _resolve_path(state, "rows.1.sku") == "B2"


def test_transform_still_falls_back_to_workflow_data() -> None:
    from core.application.orchestration.step_templating import _resolve_path

    state = {"workflow_data": {"rows": [{"sku": "C3"}]}}

    assert _resolve_path(state, "rows[0].sku") == "C3"


def test_transform_reports_an_out_of_range_index_as_missing() -> None:
    from core.application.orchestration.step_templating import _resolve_path

    assert _resolve_path({"rows": [{"sku": "A1"}]}, "rows[7].sku") is None


def test_a_malformed_selector_is_a_typed_step_failure_not_a_crash() -> None:
    """`state.rows[abc].sku` threw StatePathError out of the connector step, so
    an authoring mistake surfaced as an unhandled exception rather than as a
    step that says which selector is wrong."""
    from core.application.orchestration import connector_step_runtime as runtime

    result = runtime.execute_connector_step(
        step={
            "step_id": "call_api",
            "connector_id": "demo.connector",
            "override": {
                "connector": {
                    "function_name": "demo_call",
                    "input_mapping": {"sku": "state.rows[abc].sku"},
                }
            },
        },
        state={"rows": [{"sku": "A1"}]},
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input_selector"
    assert "rows[abc].sku" in result["detail"]
    assert "call_api" in result["detail"]


def test_shared_state_writer_drops_host_owned_test_mode_target() -> None:
    from core.application.orchestration.step_templating import set_state_path

    update: dict = {}
    set_state_path(update, {"test_mode": False}, "state.test_mode", True)

    assert update == {}


@pytest.mark.parametrize(
    "target",
    [
        "state..trusted_runtime_context.test_mode",
        ".trusted_runtime_context.test_mode",
        "state...test_mode",
    ],
)
def test_shared_state_writer_fails_closed_on_malformed_empty_segments(target: str) -> None:
    from core.application.orchestration.step_templating import set_state_path

    update: dict = {}
    set_state_path(update, {"trusted_runtime_context": {"test_mode": False}}, target, True)

    assert update == {}
