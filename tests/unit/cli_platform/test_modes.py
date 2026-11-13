"""Contract tests for pure terminal mode resolution."""

from __future__ import annotations

import pytest

from core.entrypoints.cli.platform.modes import (
    OutputMode,
    TerminalFacts,
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
    )

    assert decision.output_mode is OutputMode.JSON
    assert not decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled


def test_non_interactive_selects_non_interactive_human_mode() -> None:
    decision = select_mode(terminal=_terminal(), non_interactive=True)

    assert decision.output_mode is OutputMode.NON_INTERACTIVE
    assert not decision.can_prompt


def test_plain_asks_line_by_line_without_colour() -> None:
    decision = select_mode(terminal=_terminal(), plain_requested=True)

    assert decision.output_mode is OutputMode.PLAIN
    assert decision.can_prompt
    assert not decision.color_enabled
    assert not decision.animation_enabled


@pytest.mark.parametrize(("columns", "rows"), [(120, 40), (40, 10)])
def test_default_interactive_mode_is_coloured_at_any_terminal_size(columns: int, rows: int) -> None:
    decision = select_mode(terminal=_terminal(columns=columns, rows=rows))

    assert decision.output_mode is OutputMode.RICH
    assert decision.can_prompt
    assert decision.color_enabled
    assert decision.animation_enabled


def test_redirected_default_output_has_no_colour() -> None:
    decision = select_mode(terminal=_terminal(stdout_isatty=False))

    assert not decision.color_enabled
    assert not decision.animation_enabled


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
    decision = select_mode(terminal=terminal, no_color_requested=no_color)

    assert decision.output_mode is OutputMode.RICH
    assert not decision.color_enabled
    assert not decision.animation_enabled


def test_ci_alone_does_not_change_terminal_decision() -> None:
    assert select_mode(terminal=_terminal(), ci=True) == select_mode(terminal=_terminal(), ci=False)
