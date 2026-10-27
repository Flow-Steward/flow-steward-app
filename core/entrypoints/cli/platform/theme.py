"""Semantic CLI presentation styles.

The palette is intentionally centralized so Click help, Rich output, and
Textual themes can converge on the same meaning without scattering color
literals through adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import click


@dataclass(frozen=True)
class CLIStyle:
    """A Click-compatible style declaration for one semantic role."""

    fg: str | int | tuple[int, int, int] | None = None
    bold: bool | None = None
    dim: bool | None = None
    underline: bool | None = None

    def kwargs(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "fg": self.fg,
                "bold": self.bold,
                "dim": self.dim,
                "underline": self.underline,
            }.items()
            if value is not None
        }


HELP_STYLES: dict[str, CLIStyle] = {
    "help.usage": CLIStyle(fg="bright_cyan", bold=True),
    "help.heading": CLIStyle(fg="bright_cyan", bold=True),
    "help.command": CLIStyle(fg="green", bold=True),
    "help.option": CLIStyle(fg="bright_blue"),
    "help.argument": CLIStyle(fg="cyan"),
    "help.required": CLIStyle(fg="bright_red", bold=True),
    "help.optional": CLIStyle(fg="bright_black"),
    "help.description": CLIStyle(),
    "help.example": CLIStyle(fg="green"),
    "help.warning": CLIStyle(fg="yellow", bold=True),
    "help.error": CLIStyle(fg="bright_red", bold=True),
}


def style_text(text: object, semantic: str, *, color_enabled: bool) -> str:
    """Return ``text`` styled for a semantic role when color is enabled."""

    rendered = str(text)
    if not color_enabled:
        return rendered
    return click.style(rendered, **HELP_STYLES.get(semantic, CLIStyle()).kwargs())
