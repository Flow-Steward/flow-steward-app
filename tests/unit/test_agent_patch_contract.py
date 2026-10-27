"""A PATCH must change what it was asked to change, or say it cannot — AGENT-001.

`tools` and `skills` are relationships with their own endpoints, because order
and per-binding settings cannot be expressed by replacing a list. The patch model
did not declare them, so pydantic dropped them and the endpoint answered
`200 {"status": "no_change"}` — the caller was told nothing had changed while
believing it had asked for a change, and the agent kept its old tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.entrypoints.admin.agents_models import AgentPatch


def test_a_patch_of_supported_fields_still_works() -> None:
    patch = AgentPatch(display_name="Support Agent", enabled=True)

    assert patch.model_dump(exclude_none=True) == {
        "display_name": "Support Agent",
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("field", "value", "route"),
    [
        ("tools", ["audit_browser_navigate"], "/api/agents/{agent_id}/tools"),
        ("skills", [{"skill_id": "write_reply", "sort_order": 0}], "/api/agents/{agent_id}/skills"),
    ],
)
def test_a_relationship_field_is_refused_and_points_at_its_endpoint(field, value, route) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AgentPatch(**{field: value})

    message = str(excinfo.value)
    assert field in message
    assert route in message


def test_both_relationship_fields_are_named_at_once() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AgentPatch(tools=["t"], skills=[{"skill_id": "s"}])

    message = str(excinfo.value)
    assert "skills" in message
    assert "tools" in message


def test_an_unknown_field_is_refused_rather_than_dropped() -> None:
    """Silently ignoring a field the caller sent is how AGENT-001 stayed hidden."""
    with pytest.raises(ValidationError) as excinfo:
        AgentPatch(display_name="Support", not_a_field=1)

    assert "not_a_field" in str(excinfo.value)


def _agent_config_wire_fixture() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "assets/frontend/src/test/fixtures/agent-config-patch.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_rendered_agent_config_payload_matches_real_patch_model() -> None:
    payload = _agent_config_wire_fixture()
    patch = AgentPatch.model_validate(payload)
    assert patch.model_dump(exclude_unset=True) == payload
    assert not {"enabled", "skills", "tools", "name"}.intersection(patch.model_fields_set)


def test_legacy_name_from_agent_config_is_still_refused() -> None:
    payload = _agent_config_wire_fixture()
    with pytest.raises(ValidationError) as excinfo:
        AgentPatch.model_validate({**payload, "name": payload["display_name"]})
    assert [(error["loc"], error["type"]) for error in excinfo.value.errors()] == [
        (("name",), "extra_forbidden")
    ]
