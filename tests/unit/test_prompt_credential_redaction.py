"""A withheld credential must be visible as withheld — AGENT-002.

Keeping secrets out of a model prompt is mandatory. Deleting the field without a
trace is not: an agent handed a username and nothing for the password cannot tell
a withheld value from one that was never supplied, and neither can the operator
reading the job. One audit run ended with the agent reporting that it had entered
"username audit02_bot and password audit02_bot" into a login form.
"""

from __future__ import annotations

import pytest

from core.application.orchestration.agents.prompt_builder import (
    REDACTED_CREDENTIAL_MARKER,
    PromptBuilder,
)

_WITHHELD_VALUE = "ALPHA-111"


@pytest.mark.parametrize(
    "field",
    ["wp_password", "api_key", "access_token", "client_secret", "db_credential", "private_key"],
)
def test_a_credential_field_is_marked_rather_than_deleted(field: str) -> None:
    bounded = PromptBuilder._bounded_prompt_value({field: _WITHHELD_VALUE, "user": "bot"}, limit=4000)

    assert _WITHHELD_VALUE not in str(bounded), "the value must never reach the prompt"
    assert bounded[field] == REDACTED_CREDENTIAL_MARKER
    assert bounded["user"] == "bot", "a neighbour that is not a credential is untouched"


def test_the_marker_says_where_the_value_can_be_used() -> None:
    """An agent that knows a secret exists can ask a tool to use it."""
    assert "tool boundary" in REDACTED_CREDENTIAL_MARKER
    assert _WITHHELD_VALUE not in REDACTED_CREDENTIAL_MARKER


def test_nested_credentials_are_marked_too() -> None:
    bounded = PromptBuilder._bounded_prompt_value(
        {"connection": {"host": "mail.test", "password": _WITHHELD_VALUE}}, limit=4000
    )

    assert _WITHHELD_VALUE not in str(bounded)
    assert bounded["connection"]["password"] == REDACTED_CREDENTIAL_MARKER
    assert bounded["connection"]["host"] == "mail.test"


def test_a_field_that_merely_sounds_technical_is_not_redacted() -> None:
    bounded = PromptBuilder._bounded_prompt_value(
        {"order_status": "on-hold", "order_total": "25.48"}, limit=4000
    )

    assert bounded == {"order_status": "on-hold", "order_total": "25.48"}
