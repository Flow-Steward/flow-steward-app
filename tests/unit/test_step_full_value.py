from __future__ import annotations

from core.application.reports.step_full_value import (
    MAX_FULL_VALUE_CHARS,
    full_step_output_value,
)

BRIEF = "Kaunas is Lithuania's second city. " * 40


def test_an_agent_answer_is_returned_in_full() -> None:
    """The run view cut it at "… (1027 characters in total)"."""
    message = {"artifacts": {"brief": BRIEF, "output_payload": {"brief": BRIEF[:500]}}}

    assert full_step_output_value(message, "brief") == {"found": True, "value": BRIEF}


def test_state_outputs_http_bodies_and_transform_results_are_found() -> None:
    message = {
        "artifacts": {
            "_state_patch": {"places": [{"city": "Vilnius"}], "outputs": {"email": "Dear team"}},
            "result_target": "state.places",
            "http_request": {"response_body": {"results": [1, 2, 3]}},
        }
    }

    assert full_step_output_value(message, "state.outputs.email")["value"] == "Dear team"
    assert full_step_output_value(message, "value")["value"] == [{"city": "Vilnius"}]
    assert full_step_output_value(message, "body")["value"] == {"results": [1, 2, 3]}


def test_internal_and_unknown_fields_are_not_found() -> None:
    message = {"artifacts": {"_state_patch": {"x": 1}, "brief": "text"}}

    assert full_step_output_value(message, "_state_patch")["found"] is False
    assert full_step_output_value(message, "missing")["found"] is False
    assert full_step_output_value(message, "")["found"] is False


def test_secrets_are_masked_and_huge_text_is_capped() -> None:
    # Built at run time so the repository's secret scanner sees no literal.
    planted = "-".join(["planted", "credential", "value"])
    message = {
        "artifacts": {
            "brief": "x" * (MAX_FULL_VALUE_CHARS + 10),
            "mapped_outputs": {"login": {"password": planted}},
        }
    }

    assert planted not in str(full_step_output_value(message, "login")["value"])
    capped = full_step_output_value(message, "brief")["value"]
    assert capped.endswith(f"({MAX_FULL_VALUE_CHARS + 10} characters in total)")
