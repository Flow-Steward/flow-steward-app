"""Public copies of canonical messages retain every key except host output authority."""

from __future__ import annotations

from typing import Any


def public_agent_message(message: Any) -> Any:
    """Omit the internal receipt without modifying canonical history."""
    if not isinstance(message, dict):
        return message
    return {key: value for key, value in message.items() if key != "output_contract"}


def public_replay_outcome(outcome: Any) -> Any:
    """Project the message nested in a replay outcome, preserving all other data."""
    if not isinstance(outcome, dict) or "replay_message" not in outcome:
        return outcome
    return {**outcome, "replay_message": public_agent_message(outcome["replay_message"])}
