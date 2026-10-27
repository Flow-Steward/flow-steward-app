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


class TextualAvailability(StrEnum):
    """Whether a full-screen form is available for this invocation."""

    NOT_REQUESTED = "not_requested"
    AVAILABLE = "available"
    RESIZE_OR_PLAIN = "resize_or_plain"
    REFUSED = "refused"


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
    textual_availability: TextualAvailability
    minimum_size_message: str | None = None
    fallback_message: str | None = None


_TEXTUAL_MIN_COLUMNS = 80
_TEXTUAL_MIN_ROWS = 24
_PLAIN_FALLBACK_MIN_COLUMNS = 60
_PLAIN_FALLBACK_MIN_ROWS = 20
_TEXTUAL_MINIMUM_MESSAGE = "Textual requires a terminal of at least 60x20; use --plain."


def select_mode(
    *,
    terminal: TerminalFacts,
    json_requested: bool = False,
    plain_requested: bool = False,
    tui_requested: bool = False,
    non_interactive: bool = False,
    no_color_requested: bool = False,
    textual_eligible: bool = False,
    ci: bool = False,
) -> ModeDecision:
    """Return the fixed CLI mode precedence without inspecting the real terminal.

    ``ci`` is deliberately accepted but has no effect: actual terminal facts and
    explicit flags are the public contract.
    """
    del ci
    color_enabled = (
        terminal.stdout_isatty
        and tui_requested
        and not (
            plain_requested
            or no_color_requested
            or terminal.no_color_env
            or terminal.term.lower() == "dumb"
        )
    )
    if json_requested:
        return ModeDecision(
            output_mode=OutputMode.JSON,
            can_prompt=False,
            color_enabled=False,
            animation_enabled=False,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if non_interactive:
        return ModeDecision(
            output_mode=OutputMode.NON_INTERACTIVE,
            can_prompt=False,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if plain_requested:
        return ModeDecision(
            output_mode=OutputMode.PLAIN,
            can_prompt=terminal.stdin_isatty,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if not (terminal.stdin_isatty and terminal.stdout_isatty):
        return ModeDecision(
            output_mode=OutputMode.NON_INTERACTIVE,
            can_prompt=False,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if not tui_requested:
        return ModeDecision(
            output_mode=OutputMode.PLAIN,
            can_prompt=True,
            color_enabled=False,
            animation_enabled=False,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if not textual_eligible:
        return ModeDecision(
            output_mode=OutputMode.RICH,
            can_prompt=False,
            color_enabled=color_enabled,
            animation_enabled=color_enabled,
            textual_availability=TextualAvailability.NOT_REQUESTED,
        )
    if terminal.columns < _PLAIN_FALLBACK_MIN_COLUMNS or terminal.rows < _PLAIN_FALLBACK_MIN_ROWS:
        availability = TextualAvailability.REFUSED
        message = _TEXTUAL_MINIMUM_MESSAGE
    elif terminal.columns < _TEXTUAL_MIN_COLUMNS or terminal.rows < _TEXTUAL_MIN_ROWS:
        availability = TextualAvailability.RESIZE_OR_PLAIN
        # A usable terminal gets the line-oriented wizard automatically. This
        # is a fallback mode, not an error requiring another invocation.
        message = None
    else:
        availability = TextualAvailability.AVAILABLE
        message = None
    return ModeDecision(
        output_mode=(
            OutputMode.PLAIN
            if availability is TextualAvailability.RESIZE_OR_PLAIN
            else OutputMode.RICH
        ),
        can_prompt=availability
        in {
            TextualAvailability.AVAILABLE,
            TextualAvailability.RESIZE_OR_PLAIN,
        },
        color_enabled=color_enabled,
        animation_enabled=color_enabled,
        textual_availability=availability,
        minimum_size_message=message,
        fallback_message=(
            "Terminal is smaller than 80x24; using the plain interactive wizard."
            if availability is TextualAvailability.RESIZE_OR_PLAIN
            else None
        ),
    )
