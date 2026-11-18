"""An agent without its own model says so before a run fails on model_unavailable.

Live in claude.ai (2026-11-17): an assistant created and enabled an agent with no
model, the workflow passed readiness, and the first run failed with
``model_unavailable: no configured provider/model route``.
"""

from __future__ import annotations

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.contract_validation_entities import validate_agent_readiness


class _AgentDB:
    def __init__(self, config):
        self.agent = {
            "agent_id": "commentator",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "config": config,
            "_skills": [],
        }

    def get_agent(self, agent_id):
        return dict(self.agent) if agent_id == "commentator" else None


def _codes(result, kind):
    return {issue["code"] for issue in result[kind]}


@pytest.mark.parametrize(
    "config",
    [{}, {"provider": "openai"}, {"model": "gpt-x"}, '{"max_iterations": 3}', None],
)
def test_agent_without_its_own_route_is_warned(config):
    result = validate_agent_readiness(_AgentDB(config), "commentator")
    assert "agent.model_unset" in _codes(result, "warnings")
    # A warning: projects may still provide the route, so enabling is not blocked by it.
    assert "agent.model_unset" not in _codes(result, "errors")


@pytest.mark.parametrize(
    "config",
    [{"provider": "openai", "model": "gpt-x"}, '{"provider": "openai", "model": "gpt-x"}'],
)
def test_agent_with_provider_and_model_is_not_warned(config):
    result = validate_agent_readiness(_AgentDB(config), "commentator")
    assert "agent.model_unset" not in _codes(result, "warnings")
