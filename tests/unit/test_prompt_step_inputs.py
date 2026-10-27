"""What a step mapped into an agent must reach the model — WF-002.

A live run materialised both mapped values and the prompt contained the unique
marker zero times: the values were reachable only through `{placeholder}`
substitution in the skill's own text, so an agent whose skill did not spell one
out reached the model with no trace of what the workflow had just fetched, and
answered from the run's metadata instead.
"""

from __future__ import annotations

from core.application.orchestration.agents.prompt_builder import (
    REDACTED_CREDENTIAL_MARKER,
    PromptBuilder,
)

_MARKER = "MARKER-9911"


def test_mapped_step_inputs_reach_the_prompt() -> None:
    block = PromptBuilder._build_materialized_inputs_context(
        {"materialized_inputs": {"order_status": "on-hold", "probe": _MARKER}}
    )

    assert _MARKER in block
    assert "on-hold" in block
    assert "order_status" in block


def test_each_mapped_value_appears_exactly_once() -> None:
    """A capture is checked for the marker; twice is as wrong as never."""
    block = PromptBuilder._build_materialized_inputs_context(
        {"materialized_inputs": {"probe": _MARKER}}
    )

    assert block.count(_MARKER) == 1


def test_a_step_with_no_mapped_inputs_adds_no_section() -> None:
    assert PromptBuilder._build_materialized_inputs_context({}) == ""
    assert PromptBuilder._build_materialized_inputs_context({"materialized_inputs": {}}) == ""


def test_a_mapped_credential_is_marked_not_printed() -> None:
    block = PromptBuilder._build_materialized_inputs_context(
        {"materialized_inputs": {"wp_user": "bot", "wp_password": "ALPHA-111"}}
    )

    assert "ALPHA-111" not in block
    assert REDACTED_CREDENTIAL_MARKER in block
    assert "bot" in block
