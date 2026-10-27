"""Semantic Click help formatter for the public CLI."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import click

from core.entrypoints.cli.platform.theme import style_text


class SemanticHelpFormatter(click.HelpFormatter):
    """Help formatter that styles Click-generated structure by semantic role."""

    def __init__(
        self,
        *,
        color_enabled: bool,
        width: int | None = None,
        max_width: int | None = None,
    ) -> None:
        super().__init__(width=width, max_width=max_width)
        self.color_enabled = color_enabled
        self._section_stack: list[str] = []

    def style(self, text: object, semantic: str) -> str:
        """Style one semantic token if this help render permits color."""

        return style_text(text, semantic, color_enabled=self.color_enabled)

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Write and track a styled section heading."""

        self.write_paragraph()
        self.write_heading(name)
        self._section_stack.append(name)
        self.indent()
        try:
            yield
        finally:
            self.dedent()
            self._section_stack.pop()

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        if prefix is None:
            prefix = self.style("Usage:", "help.usage") + " "
        super().write_usage(prog, args, prefix=prefix)

    def write_heading(self, heading: str) -> None:
        super().write_heading(self.style(heading, "help.heading"))

    def write_dl(
        self,
        rows: Iterable[tuple[str, str]],
        col_max: int = 30,
        col_spacing: int = 2,
    ) -> None:
        section = self._section_stack[-1] if self._section_stack else ""
        term_semantic = _term_semantic_for_section(section)
        super().write_dl(
            [
                (
                    self.style(first, term_semantic),
                    self.style(second, "help.description"),
                )
                for first, second in rows
            ],
            col_max=col_max,
            col_spacing=col_spacing,
        )


def _term_semantic_for_section(section: str) -> str:
    if section == "Commands":
        return "help.command"
    if section == "Arguments":
        return "help.argument"
    return "help.option"
