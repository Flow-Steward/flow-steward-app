"""A step's inputs and outputs stay readable, bounded and free of secrets."""

from __future__ import annotations

import json

from core.application.orchestration.step_io_preview import (
    MAX_PREVIEW_BYTES,
    referenced_state_values,
    step_io_payload,
    step_io_preview,
)


def test_small_values_are_kept_as_they_are() -> None:
    value = {"url": "https://api.example.com/weather", "query_params": {"city": "Vilnius"}}
    assert step_io_preview(value) == value


def test_long_strings_and_lists_are_cut_with_their_full_size() -> None:
    preview = step_io_preview({"body": "x" * 5_000, "items": list(range(100))})

    assert preview["body"].endswith("(5000 characters in total)")
    assert preview["items"][-1].endswith("(100 in total)")
    assert len(json.dumps(preview)) <= MAX_PREVIEW_BYTES


def test_a_value_too_big_for_any_shape_is_cut_with_its_size() -> None:
    preview = step_io_preview({f"key_{index}": "y" * 200 for index in range(40)})

    assert isinstance(preview, str)
    assert preview.endswith("bytes in total)")


def test_credentials_are_masked() -> None:
    preview = step_io_preview({"headers": {"Authorization": "Bearer abc123"}, "api_key": "k-123"})

    assert "abc123" not in str(preview)
    assert "k-123" not in str(preview)


def test_a_non_mapping_value_becomes_a_field() -> None:
    assert step_io_payload([1, 2]) == {"value": [1, 2]}
    assert step_io_payload({"a": 1}) == {"a": 1}
    assert step_io_payload(None) == {}


def test_a_condition_shows_the_state_values_it_read() -> None:
    state = {"outputs": {"weather": {"city": "Berlin"}}}
    condition = {"left": "state.outputs.weather.city", "op": "eq", "right": "Vilnius"}

    assert referenced_state_values(condition, state) == {"state.outputs.weather.city": "Berlin"}


def test_an_agent_step_shows_its_mapped_inputs_with_secrets_masked() -> None:
    from core.application.orchestration.graph.nodes_contracts import agent_step_input_preview

    state = {
        "materialized_inputs": {
            "topic": "Winter boots",
            "login": "admin-password-value",
            "unrelated": "not mapped",
        },
        "materialized_input_sensitive_keys": ["login"],
    }
    step = {
        "input_mapping_jsonb": {"topic": "state.inputs.topic", "login": "state.inputs.wp_password"}
    }

    preview = agent_step_input_preview(state, step)

    assert preview["topic"] == "Winter boots"
    assert "admin-password-value" not in str(preview)
    assert "unrelated" not in preview
    assert agent_step_input_preview(state, {}) == {}


def test_an_agent_step_shows_its_mapped_outputs_not_the_state_patch() -> None:
    """The panel showed "Outputs: outputs · workflow_data" instead of the brief."""
    from core.application.orchestration.graph.nodes_contracts import (
        _apply_agent_step_output_mapping,
    )

    artifacts = {"brief": "Vilnius is the capital of Lithuania."}
    state_updates: dict = {}
    _apply_agent_step_output_mapping(
        step={"output_mapping_jsonb": {"brief": "state.outputs.brief"}},
        artifacts=artifacts,
        state={},
        state_updates=state_updates,
    )

    assert artifacts["output_payload"] == {"brief": "Vilnius is the capital of Lithuania."}
    assert state_updates
