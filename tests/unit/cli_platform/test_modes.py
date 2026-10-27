"""Contract tests for pure terminal mode resolution."""

from __future__ import annotations

import pytest

from core.entrypoints.cli.platform.modes import (
    OutputMode,
    TerminalFacts,
    TextualAvailability,
    select_mode,
)


def _terminal(**overrides: object) -> TerminalFacts:
    values: dict[str, object] = {
        "stdin_isatty": True,
        "stdout_isatty": True,
        "columns": 120,
        "rows": 40,
        "term": "xterm-256color",
        "no_color_env": False,
    }
    values.update(overrides)
    return TerminalFacts(**values)


def test_json_has_the_highest_precedence_and_disables_interaction_and_color() -> None:
    decision = select_mode(
        terminal=_terminal(),
        json_requested=True,
        plain_requested=True,
        non_interactive=True,
        tui_requested=True,
    )

    assert decision.output_mode is OutputMode.JSON
    assert not decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled


def test_non_interactive_selects_non_interactive_human_mode() -> None:
    decision = select_mode(terminal=_terminal(), non_interactive=True, tui_requested=True)

    assert decision.output_mode is OutputMode.NON_INTERACTIVE
    assert not decision.can_prompt
    assert decision.textual_availability is TextualAvailability.NOT_REQUESTED


def test_plain_uses_line_wizard_only_with_interactive_stdin() -> None:
    decision = select_mode(terminal=_terminal(), plain_requested=True, tui_requested=True)

    assert decision.output_mode is OutputMode.PLAIN
    assert decision.can_prompt
    assert not decision.color_enabled
    assert decision.textual_availability is TextualAvailability.NOT_REQUESTED


@pytest.mark.parametrize(("columns", "rows"), [(120, 40), (40, 10)])
def test_default_interactive_mode_uses_plain_wizard_without_screen_controls(
    columns: int, rows: int
) -> None:
    decision = select_mode(terminal=_terminal(columns=columns, rows=rows), textual_eligible=True)

    assert decision.output_mode is OutputMode.PLAIN
    assert decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled
    assert decision.textual_availability is TextualAvailability.NOT_REQUESTED


def test_capable_interactive_terminal_uses_textual_for_eligible_command() -> None:
    decision = select_mode(terminal=_terminal(), textual_eligible=True, tui_requested=True)

    assert decision.output_mode is OutputMode.RICH
    assert decision.can_prompt
    assert decision.textual_availability is TextualAvailability.AVAILABLE


def test_non_tty_never_prompts_without_explicit_plain() -> None:
    decision = select_mode(terminal=_terminal(stdin_isatty=False, stdout_isatty=False))

    assert decision.output_mode is OutputMode.NON_INTERACTIVE
    assert not decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled


def test_plain_can_prompt_with_interactive_stdin_and_redirected_stdout() -> None:
    decision = select_mode(terminal=_terminal(stdout_isatty=False), plain_requested=True)

    assert decision.output_mode is OutputMode.PLAIN
    assert decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled


@pytest.mark.parametrize(
    ("terminal", "no_color"),
    [
        (_terminal(no_color_env=True), False),
        (_terminal(term="dumb"), False),
        (_terminal(), True),
    ],
)
def test_color_controls_disable_color_and_animation(
    terminal: TerminalFacts, no_color: bool
) -> None:
    decision = select_mode(terminal=terminal, no_color_requested=no_color, tui_requested=True)

    assert not decision.color_enabled
    assert not decision.animation_enabled


def test_ci_alone_does_not_change_terminal_decision() -> None:
    assert select_mode(terminal=_terminal(), ci=True) == select_mode(terminal=_terminal(), ci=False)


def test_exact_minimum_terminal_supports_textual() -> None:
    decision = select_mode(
        terminal=_terminal(columns=80, rows=24), textual_eligible=True, tui_requested=True
    )

    assert decision.textual_availability is TextualAvailability.AVAILABLE


@pytest.mark.parametrize(("columns", "rows"), [(60, 20), (79, 23)])
def test_small_but_usable_terminal_requests_resize_or_plain_fallback(
    columns: int, rows: int
) -> None:
    decision = select_mode(
        terminal=_terminal(columns=columns, rows=rows), textual_eligible=True, tui_requested=True
    )

    assert decision.textual_availability is TextualAvailability.RESIZE_OR_PLAIN
    assert decision.minimum_size_message is None


def test_terminal_below_plain_minimum_refuses_textual_with_exact_minimum() -> None:
    decision = select_mode(
        terminal=_terminal(columns=59, rows=19), textual_eligible=True, tui_requested=True
    )

    assert decision.textual_availability is TextualAvailability.REFUSED
    assert (
        decision.minimum_size_message
        == "Textual requires a terminal of at least 60x20; use --plain."
    )
