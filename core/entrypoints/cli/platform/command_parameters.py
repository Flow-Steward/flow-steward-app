"""Build ordered Click parameters from the public command schema."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from core.application.management.command_specs.contracts import (
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
)

_CONFIRM_REVIEW_PARAM_NAME = "_confirm_review"


def _global_options() -> list[click.Option]:
    return [
        click.Option(["--json"], is_flag=True, default=None, help="Emit JSON output."),
        click.Option(
            ["--plain"], is_flag=True, default=None, help="Use plain text output (default)."
        ),
        click.Option(
            ["--tui"],
            is_flag=True,
            default=None,
            help="Opt in to full-screen forms and rich output.",
        ),
        click.Option(["--no-color"], is_flag=True, default=None, help="Disable color output."),
        click.Option(["--verbose"], is_flag=True, default=None, help="Include technical detail."),
        click.Option(
            ["--non-interactive"],
            is_flag=True,
            default=None,
            help="Fail instead of prompting for missing input.",
        ),
    ]


def _confirmation_options(spec: CommandSpec) -> list[click.Option]:
    if spec.interactive_policy is not InteractivePolicy.ALWAYS_REVIEW:
        return []
    if spec.confirmation_policy_ref:
        return [
            click.Option(
                ["--confirm", _CONFIRM_REVIEW_PARAM_NAME],
                type=str,
                default="",
                metavar="CONFIRMATION_VALUE",
                help="Confirm by repeating the exact canonical value printed after review.",
            )
        ]
    return [
        click.Option(
            ["--confirm", _CONFIRM_REVIEW_PARAM_NAME],
            is_flag=True,
            default=False,
            help="Confirm execution without an interactive review prompt.",
        )
    ]


def _field_params(
    fields: tuple[FieldSpec, ...], *, accepts_input_json: bool
) -> list[click.Parameter]:
    params: list[click.Parameter] = []
    if accepts_input_json:
        params.append(
            click.Option(
                ["--input-json"],
                type=click.Path(dir_okay=False, path_type=Path),
                default=None,
                help=(
                    "Read non-secret input fields from a JSON object file. "
                    "Explicit command options override values from this file. "
                    "Secrets are not accepted; use field-specific secure sources."
                ),
            )
        )
    for field in fields:
        if field.secret:
            file_option, stdin_option = _secret_option_names(field)
            if file_option:
                params.append(
                    click.Option(
                        [file_option, f"_{field.key}_file"],
                        type=click.Path(path_type=Path),
                        default=None,
                    )
                )
            if stdin_option:
                params.append(
                    click.Option(
                        [stdin_option, f"_{field.key}_stdin"],
                        is_flag=True,
                        default=False,
                    )
                )
        elif field.positional:
            # A positional multi-select takes every remaining word: `config set A=1 B=2`.
            params.append(
                click.Argument(
                    [field.key],
                    required=False,
                    nargs=-1 if field.type is FieldType.MULTI_SELECT else 1,
                    type=_click_type(field),
                )
            )
        else:
            params.append(_option_for_field(field))
    return params


def _secret_option_names(field: FieldSpec) -> tuple[str | None, str | None]:
    option_base = f"--{field.key.replace('_', '-')}"
    return (
        f"{option_base}-file" if field.allow_secret_file else None,
        f"{option_base}-stdin" if field.allow_secret_stdin else None,
    )


def _option_for_field(field: FieldSpec) -> click.Option:
    option = field.option_name or f"--{field.key.replace('_', '-')}"
    kwargs: dict[str, Any] = {"default": None, "type": _click_type(field)}
    if field.type is FieldType.PATH:
        kwargs["type"] = click.Path(path_type=Path)
    if field.type is FieldType.BOOLEAN:
        off_option = f"--no-{option.removeprefix('--')}"
        return click.Option([f"{option}/{off_option}", field.key], default=None)
    if field.type is FieldType.MULTI_SELECT:
        kwargs["multiple"] = True
    return click.Option([option, field.key], **kwargs)


def _normalize_click_value(field: FieldSpec, value: object) -> object:
    if field.type is FieldType.MULTI_SELECT and isinstance(value, tuple):
        return list(value)
    if field.type is FieldType.PATH and isinstance(value, Path):
        return str(value)
    return value


def _normalize_values_for_fields(
    fields: tuple[FieldSpec, ...], values: dict[str, Any]
) -> dict[str, Any]:
    by_key = {field.key: field for field in fields}
    return {
        key: _normalize_click_value(field, value) if (field := by_key.get(key)) else value
        for key, value in dict(values).items()
    }


def _click_type(field: FieldSpec) -> click.ParamType:
    if field.choices:
        return click.Choice([item.value for item in field.choices], case_sensitive=True)
    if field.type is FieldType.INTEGER:
        return click.INT
    if field.type is FieldType.DECIMAL:
        return click.FLOAT
    return click.STRING
