from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_resolve_provider_project_default_beats_registry():
    try:
        from core.application.orchestration.agents.common import _resolve_provider
    except ImportError:
        pytest.skip("core.application.orchestration.agents.common not importable")

    project_config = {
        "agents_config": {
            "default_provider": "openai",
            "default_model": "gpt-5.6-sol",
            "agents": {"config": {}},
        }
    }

    fake_manifest = MagicMock()
    fake_manifest.config = {"provider": "anthropic", "model": "claude-sonnet-5"}
    fake_registry = MagicMock()
    fake_registry.agents.get_agent.return_value = fake_manifest

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=fake_registry,
    ):
        provider = _resolve_provider(project_config, "proofreader")

    assert provider == "openai"


def test_resolve_provider_project_agent_override_beats_project_default():
    try:
        from core.application.orchestration.agents.common import _resolve_provider
    except ImportError:
        pytest.skip("core.application.orchestration.agents.common not importable")

    project_config = {
        "agents_config": {
            "default_provider": "openai",
            "default_model": "gpt-5.6-sol",
            "agents": {
                "config": {
                    "composer": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-5",
                    },
                }
            },
        }
    }

    assert _resolve_provider(project_config, "composer") == "anthropic"


def test_resolve_provider_does_not_inherit_another_agents_override():
    try:
        from core.application.orchestration.agents.common import _resolve_provider
    except ImportError:
        pytest.skip("core.application.orchestration.agents.common not importable")

    project_config = {
        "agents_config": {
            "default_provider": "openai",
            "default_model": "gpt-5.6-sol",
            "agents": {
                "config": {
                    "composer": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-5",
                    },
                }
            },
        }
    }

    # There are no agent-name aliases: only the named agent gets its override,
    # every other agent resolves to the project default.
    assert _resolve_provider(project_config, "composer") == "anthropic"
    assert _resolve_provider(project_config, "reviser") == "openai"
    assert _resolve_provider(project_config, "rewriter") == "openai"
