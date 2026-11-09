"""One question at a time, in the normal terminal flow (prompt-toolkit in a terminal).

In a real terminal a choice is picked with the arrow keys and Enter (no mouse, no
full-screen form); anywhere else (a pipe, a provider console without a TTY, tests)
the same question is a numbered list answered by typing the number.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib.util import find_spec

import click

from core.application.management.command_specs.contracts import ChoiceItem, FieldSpec


def arrow_keys_available() -> bool:
    """Arrow-key selection needs a terminal on both ends and prompt_toolkit."""
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
    return interactive and find_spec("prompt_toolkit") is not None


def show_field_help(field: FieldSpec) -> None:
    """The field's description, dimmed, before its question."""
    text = field.description.strip()
    if text:
        click.echo(click.style(text, dim=True))


def _choice_label(choice: ChoiceItem) -> str:
    label = str(choice.label or choice.value)
    if choice.description:
        label = f"{label} — {choice.description}"
    return label


def _numbered_choice(label: str, choices: Sequence[ChoiceItem], default: str | None) -> str:
    click.echo(f"{label} choices:")
    for index, choice in enumerate(choices, start=1):
        shown = str(choice.label or choice.value)
        suffix = f" ({choice.value})" if choice.value != shown else ""
        click.echo(f"  {index}. {shown}{suffix}")
    default_index = next(
        (index for index, choice in enumerate(choices, start=1) if choice.value == default),
        1,
    )
    selected = click.prompt(
        label,
        type=click.Choice(
            [str(index) for index in range(1, len(choices) + 1)], case_sensitive=True
        ),
        default=str(default_index),
    )
    return choices[int(selected) - 1].value


def ask_choice(label: str, choices: Sequence[ChoiceItem], default: str | None = None) -> str:
    """Ask for one of ``choices`` and return its value."""
    if not choices:
        raise ValueError(f"{label} has no choices")
    if not arrow_keys_available():
        return _numbered_choice(label, choices, default)
    from prompt_toolkit.shortcuts import choice as choose

    values = {choice.value for choice in choices}
    return choose(
        message=f"{label}:",
        options=[(choice.value, _choice_label(choice)) for choice in choices],
        default=default if default in values else choices[0].value,
        mouse_support=False,
    )
