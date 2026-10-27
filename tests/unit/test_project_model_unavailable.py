"""A project whose default model belongs to a removed provider says so plainly.

The project's default route wins over the agent's own model. When it names an
LLM provider source that was removed or reinstalled (a new install id means a
new provider key), the run used to fail with "extension provider ... is not
available for account", which names neither the project nor what to do.
"""

from __future__ import annotations

import pytest

from core.application.orchestration import model_provider

_GONE = "llmext_acme_owner_pi_gone_0123456789ab"


def _unavailable(**_kwargs):
    raise model_provider.ModelProviderResolutionError(
        f"extension provider '{_GONE}' is not available for account"
    )


@pytest.fixture(autouse=True)
def _gone_provider(monkeypatch):
    monkeypatch.setattr(model_provider, "_build_extension_chat_model", _unavailable)
    monkeypatch.setattr(model_provider, "_get_registry_agent_config", lambda _name: {})


def test_a_project_default_on_a_removed_provider_names_the_project_and_the_fix() -> None:
    config = {
        "project_name": "Acme Project",
        "account_id": "acct_x",
        "agents_config": {"default_provider": _GONE, "default_model": "acme/small"},
    }

    with pytest.raises(model_provider.ModelProviderResolutionError) as caught:
        model_provider.get_chat_model(config, agent_name="acme_answerer")

    message = str(caught.value)
    assert message.startswith("Project 'Acme Project' has the default model acme/small")
    assert "no longer available" in message
    assert "Choose another model in the project's settings" in message
    assert "not available for account" not in message


def test_a_project_override_for_one_agent_names_that_agent() -> None:
    config = {
        "project_name": "Acme Project",
        "account_id": "acct_x",
        "agents_config": {
            "agents": {"config": {"acme_answerer": {"provider": _GONE, "model": "acme/small"}}}
        },
    }

    with pytest.raises(model_provider.ModelProviderResolutionError) as caught:
        model_provider.get_chat_model(config, agent_name="acme_answerer")

    assert str(caught.value).startswith(
        "Project 'Acme Project' sets the model acme/small for the agent 'acme_answerer'"
    )


def test_the_agents_own_model_keeps_the_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        model_provider,
        "_get_registry_agent_config",
        lambda _name: {"provider": _GONE, "model": "acme/small"},
    )
    config = {"project_name": "Acme Project", "account_id": "acct_x", "agents_config": {}}

    with pytest.raises(model_provider.ModelProviderResolutionError) as caught:
        model_provider.get_chat_model(config, agent_name="acme_answerer")

    assert "not available for account" in str(caught.value)
