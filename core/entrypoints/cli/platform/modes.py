"""Deterministic terminal mode selection for the public CLI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OutputMode(StrEnum):
    """Renderer contract selected before command execution."""

    RICH = "rich"
    PLAIN = "plain"
    JSON = "json"
    NON_INTERACTIVE = "non_interactive"


@dataclass(frozen=True)
class TerminalFacts:
    """Injected terminal properties, keeping mode selection pure and testable."""

    stdin_isatty: bool
    stdout_isatty: bool
    columns: int
    rows: int
    term: str
    no_color_env: bool


@dataclass(frozen=True)
class ModeDecision:
    """The complete presentation decision consumed by later CLI adapters."""

    output_mode: OutputMode
    can_prompt: bool
    color_enabled: bool
    animation_enabled: bool


def select_mode(
    *,
    terminal: TerminalFacts,
    json_requested: bool = False,
    plain_requested: bool = False,
    non_interactive: bool = False,
    no_color_requested: bool = False,
    ci: bool = False,
) -> ModeDecision:
    """Return the fixed CLI mode precedence without inspecting the real terminal.

    ``ci`` is deliberately accepted but has no effect: actual terminal facts and
    explicit flags are the public contract.
    """
    del ci
    # Colour is the default for a terminal, as in every mainstream CLI; it carries meaning
    # (success, failure, warning) and the same words are printed without it.
    color_enabled = terminal.stdout_isatty and not (
        plain_requested
        or no_color_requested
        or terminal.no_color_env
        or terminal.term.lower() == "dumb"
    )
    if json_requested:
        return ModeDecision(
            output_mode=OutputMode.JSON,
            can_prompt=False,
            color_enabled=False,
            animation_enabled=False,
        )
    if non_interactive:
        return ModeDecision(
            output_mode=OutputMode.NON_INTERACTIVE,
            can_prompt=False,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
        )
    if plain_requested:
        return ModeDecision(
            output_mode=OutputMode.PLAIN,
            can_prompt=terminal.stdin_isatty,
            color_enabled=False,
            animation_enabled=False,
        )
    if not (terminal.stdin_isatty and terminal.stdout_isatty):
        return ModeDecision(
            output_mode=OutputMode.NON_INTERACTIVE,
            can_prompt=False,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
        )
    return ModeDecision(
        output_mode=OutputMode.RICH,
        can_prompt=True,
        color_enabled=color_enabled,
        animation_enabled=color_enabled,
    )
