"""Sensitivity follows the value, not the name it currently has.

Mapping `state.inputs.wp_password` into an agent input called `login` laundered a
credential into an innocuous-looking key: every guard that inspected the target
key alone let the plaintext through to the model prompt.
"""

from __future__ import annotations

import pytest

from core.application.orchestration.agents.prompt_builder import PromptBuilder
from core.application.orchestration.graph.nodes_contracts import _mapping_source_is_sensitive
from core.infrastructure.security.prompt_safety import (
    REDACTED_CREDENTIAL_MARKER,
    is_sensitive_name,
    redact_sensitive_structure,
)

_PLAINTEXT = "ALPHA-111"


@pytest.mark.parametrize(
    "selector",
    [
        "state.inputs.wp_password",
        "state.connection.api_key",
        "state.rows[0].token",
        "state.vault.client_secret",
    ],
)
def test_a_mapping_from_a_secret_source_is_recognised(selector: str) -> None:
    assert _mapping_source_is_sensitive(selector) is True


@pytest.mark.parametrize(
    "selector",
    ["state.facts.status", "state.inputs.topic", 'literal:"plain"', "state.order_total"],
)
def test_an_ordinary_mapping_is_not_treated_as_secret(selector: str) -> None:
    assert _mapping_source_is_sensitive(selector) is False


def test_a_secret_aliased_to_an_innocuous_key_is_still_redacted_in_the_prompt() -> None:
    """The exact shape the audit reproduced: wp_password mapped to `login`."""
    state = {
        "materialized_inputs": {"login": _PLAINTEXT, "topic": "scheduled publishing"},
        "materialized_input_sensitive_keys": ["login"],
    }

    block = PromptBuilder._build_materialized_inputs_context(state)

    assert _PLAINTEXT not in block
    assert REDACTED_CREDENTIAL_MARKER in block
    assert "scheduled publishing" in block, "an ordinary neighbour still reaches the model"


def test_without_provenance_an_ordinary_key_is_untouched() -> None:
    state = {"materialized_inputs": {"login": "operator-name"}}

    block = PromptBuilder._build_materialized_inputs_context(state)

    assert "operator-name" in block


def test_the_prompt_policy_is_the_canonical_one() -> None:
    """A second key-name list is how a name one list knows and the other does not leaks."""
    assert PromptBuilder._is_sensitive_task_envelope_key("wp_password") is is_sensitive_name(
        "wp_password"
    )
    assert PromptBuilder._is_sensitive_task_envelope_key("login") is is_sensitive_name("login")


def test_recursive_redaction_reaches_nested_and_listed_values() -> None:
    payload = {
        "connection": {"host": "mail.test", "password": _PLAINTEXT},
        "rows": [{"api_key": _PLAINTEXT, "sku": "SKU-1"}],
        "login": _PLAINTEXT,
    }

    redacted = redact_sensitive_structure(payload, sensitive_keys=frozenset({"login"}))

    assert _PLAINTEXT not in str(redacted)
    assert redacted["connection"]["host"] == "mail.test"
    assert redacted["rows"][0]["sku"] == "SKU-1"
