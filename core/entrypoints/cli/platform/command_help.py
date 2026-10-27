"""Schema-derived CLI help grouping, field descriptions and semantic styling."""

from __future__ import annotations

from collections.abc import Iterable

import click

from core.application.management.command_specs.contracts import (
    ExactlyOneOfGroup,
    FieldSpec,
    FieldType,
)
from core.entrypoints.cli.platform.command_parameters import _secret_option_names
from core.entrypoints.cli.platform.help_formatter import SemanticHelpFormatter
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_HELP_COLOR_OFF_FLAGS = ("--json", "--plain", "--no-color")


def _secret_input_policy(fields: Iterable[FieldSpec]) -> str:
    file_options: list[str] = []
    stdin_options: list[str] = []
    for field in fields:
        if not field.secret:
            continue
        file_option, stdin_option = _secret_option_names(field)
        if file_option:
            file_options.append(file_option)
        if stdin_option:
            stdin_options.append(stdin_option)
    file_policy = ""
    if file_options:
        verb = "is" if len(file_options) == 1 else "are"
        file_policy = f"{', '.join(file_options)} {verb} limited to .flow-steward/secret-input/"
    if stdin_options:
        stdin_policy = f"use {', '.join(stdin_options)} for external secret managers."
        return f"{file_policy}; {stdin_policy}" if file_policy else stdin_policy
    return f"{file_policy}." if file_policy else ""


def _field_groups(
    fields: Iterable[FieldSpec], groups: tuple[ExactlyOneOfGroup, ...]
) -> tuple[tuple[str, tuple[FieldSpec, ...], str | None], ...]:
    all_fields = tuple(fields)
    by_key = {field.key: field for field in all_fields}
    grouped_keys = {key for group in groups for key in group.field_keys}
    return (
        *(
            (
                f"{group.label} — required, choose exactly one",
                tuple(by_key[key] for key in group.field_keys),
                "Choose exactly one",
            )
            for group in groups
        ),
        (
            "Required inputs",
            tuple(
                field
                for field in all_fields
                if field.required and not field.advanced and field.key not in grouped_keys
            ),
            None,
        ),
        (
            "Optional inputs",
            tuple(
                field
                for field in all_fields
                if not field.required and not field.advanced and field.key not in grouped_keys
            ),
            None,
        ),
        ("Advanced inputs", tuple(field for field in all_fields if field.advanced), None),
    )


def _params_for_field(
    params: dict[str, click.Parameter], field: FieldSpec
) -> tuple[click.Parameter, ...]:
    if field.secret:
        return tuple(
            params[name] for name in (f"_{field.key}_file", f"_{field.key}_stdin") if name in params
        )
    return (params[field.key],) if field.key in params else ()


def _help_rows(
    ctx: click.Context, params: dict[str, click.Parameter], names: Iterable[str]
) -> list[tuple[str, str]]:
    rows = []
    for name in names:
        param = params.get(name)
        if param is None:
            continue
        record = param.get_help_record(ctx)
        if record is not None:
            rows.append(record)
    return rows


def _field_help(
    field: FieldSpec,
    inherited: str,
    *,
    formatter: click.HelpFormatter | None = None,
    group_requirement: str | None = None,
) -> str:
    required_marker = group_requirement or ("Required" if field.required else "Optional")
    bits = [
        field.description or inherited,
        _style_help(
            formatter,
            required_marker,
            "help.required" if field.required or group_requirement else "help.optional",
        ),
    ]
    if (
        field.type is FieldType.EXTENSION_ID
        and "Canonical lowercase dot-separated package ID" not in field.description
    ):
        bits.append("Canonical lowercase dot-separated package ID (for example, acme.example).")
    if field.secret:
        bits.append(
            "Only files under .flow-steward/secret-input/ are accepted; use stdin for external secret managers."
        )
    if field.default is not None:
        bits.append(f"Default: {_format_public_default(field.default)}.")
    if field.choices:
        bits.append("Choices: " + ", ".join(choice.value for choice in field.choices) + ".")
    if field.examples:
        bits.append("Examples: " + ", ".join(field.examples) + ".")
    return " ".join(part for part in bits if part)


def _format_public_default(value: object) -> str:
    if isinstance(value, (tuple, list, set, frozenset)):
        return ",".join(str(item) for item in value)
    return str(value)


def _make_help_formatter(ctx: click.Context) -> SemanticHelpFormatter:
    return SemanticHelpFormatter(
        width=ctx.terminal_width,
        max_width=ctx.max_content_width,
        color_enabled=_help_color_enabled(ctx),
    )


def _record_help_color_flags(ctx: click.Context, args: list[str]) -> None:
    if "--tui" in args:
        ctx.meta["help_tui_requested"] = True
    if any(flag in args for flag in _HELP_COLOR_OFF_FLAGS):
        ctx.meta["help_color_off"] = True
    if "--no-color" in args:
        ctx.color = False


def _help_color_enabled(ctx: click.Context) -> bool:
    if ctx.meta.get("help_color_off"):
        return False
    tui_requested = bool(ctx.meta.get("help_tui_requested"))
    current: click.Context | None = ctx
    while current is not None:
        tui_requested = tui_requested or bool(current.params.get("tui"))
        if (
            current.params.get("no_color")
            or current.params.get("plain")
            or current.params.get("json")
        ):
            return False
        current = current.parent
    if _runtime_env.get("NO_COLOR") or _runtime_env.get("TERM", "").lower() == "dumb":
        return False
    if ctx.color is False:
        return False
    return tui_requested


def _style_help(
    formatter: click.HelpFormatter | None,
    text: object,
    semantic: str,
) -> str:
    if isinstance(formatter, SemanticHelpFormatter):
        return formatter.style(text, semantic)
    return str(text)
