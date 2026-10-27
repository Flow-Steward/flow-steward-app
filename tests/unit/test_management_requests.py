from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.application.management.command_models import ProjectPolicyUpdateInput
from core.application.management.requests import (
    ProjectConnectionOAuthStartRequest,
    ProjectPolicyRequest,
)
from core.application.workflows.serve_workflow_helpers import build_project_policy_from_form


def test_project_policy_request_accepts_get_readback_payload() -> None:
    payload = ProjectPolicyRequest.model_validate(
        {
            "project_id": "project-1",
            "policy": {
                "autonomy_mode": "auto",
                "autonomy_confidence_threshold": 0.9,
                "retry_strategy": "flat",
                "retry_max_attempts": 4,
                "retry_backoff_initial_seconds": 3,
                "retry_backoff_max_seconds": 30,
                "concurrency_cap": 2,
                "tool_policy_mode": "restricted",
            },
        }
    )

    assert payload.autonomy_mode == "auto"
    assert payload.autonomy_confidence_threshold == 0.9
    assert payload.retry_strategy == "flat"
    assert payload.retry_max_attempts == 4
    assert payload.retry_backoff_initial_seconds == 3
    assert payload.retry_backoff_max_seconds == 30
    assert payload.concurrency_cap == 2
    assert payload.tool_policy_mode == "restricted"


def test_project_policy_request_accepts_every_autonomy_mode_exposed_by_the_ui() -> None:
    for autonomy_mode in ("manual", "supervised", "auto"):
        assert (
            ProjectPolicyRequest.model_validate({"autonomy_mode": autonomy_mode}).autonomy_mode
            == autonomy_mode
        )


def test_project_connection_oauth_intent_is_provider_neutral() -> None:
    request = ProjectConnectionOAuthStartRequest.model_validate({"intent": "connect_primary"})

    assert request.intent == "connect_primary"


@pytest.mark.parametrize("model", [ProjectPolicyRequest, ProjectPolicyUpdateInput])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency_cap", 0),
        ("concurrency_cap", -1),
        ("concurrency_cap", 1.5),
        ("autonomy_confidence_threshold", 2),
        ("autonomy_confidence_threshold", -0.1),
        ("autonomy_confidence_threshold", float("nan")),
        ("autonomy_confidence_threshold", float("inf")),
    ],
)
def test_project_policy_write_rejects_invalid_numbers_at_the_field(model, field, value) -> None:
    with pytest.raises(ValidationError) as rejected:
        model.model_validate({field: value})
    assert rejected.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("model", [ProjectPolicyRequest, ProjectPolicyUpdateInput])
@pytest.mark.parametrize("confidence", [0, 0.55, 1])
def test_project_policy_numeric_bounds_preserve_partial_and_null_updates(model, confidence) -> None:
    assert model.model_validate({}).model_dump(exclude_none=True) == {}
    assert (
        model.model_validate(
            {"concurrency_cap": None, "autonomy_confidence_threshold": None}
        ).model_dump(exclude_none=True)
        == {}
    )
    assert model.model_validate({"concurrency_cap": 2}).model_dump(exclude_none=True) == {
        "concurrency_cap": 2
    }
    assert model.model_validate({"autonomy_confidence_threshold": confidence}).model_dump(
        exclude_none=True
    ) == {"autonomy_confidence_threshold": confidence}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_concurrency_cap", "0"),
        ("policy_concurrency_cap", "1.5"),
        ("policy_concurrency_cap", "invalid-number"),
        ("policy_autonomy_confidence_threshold", "2"),
        ("policy_autonomy_confidence_threshold", "-0.1"),
        ("policy_autonomy_confidence_threshold", "nan"),
        ("policy_autonomy_confidence_threshold", "invalid-number"),
    ],
)
def test_project_policy_form_rejects_invalid_numbers_instead_of_clamping(field, value) -> None:
    with pytest.raises(ValueError, match=field):
        build_project_policy_from_form({field: value})


@pytest.mark.parametrize("blank", [None, "", " "])
def test_project_policy_form_keeps_blank_defaults_and_valid_boundaries(blank) -> None:
    defaults = build_project_policy_from_form(
        {"policy_concurrency_cap": blank, "policy_autonomy_confidence_threshold": blank}
    )
    assert defaults["concurrency_cap"] == 1
    assert defaults["autonomy_confidence_threshold"] == 0.8
    for confidence in ("0", "1"):
        policy = build_project_policy_from_form(
            {"policy_concurrency_cap": "2", "policy_autonomy_confidence_threshold": confidence}
        )
        assert policy["concurrency_cap"] == 2
        assert policy["autonomy_confidence_threshold"] == float(confidence)
