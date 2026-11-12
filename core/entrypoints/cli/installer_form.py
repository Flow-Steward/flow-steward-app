"""Presentation adapters for the installer's ordinary and advanced forms."""

from __future__ import annotations

from typing import Any

import click

from core.application.installer.spec import INSTALLER_SPEC
from core.application.installer.wizard import (
    InstallerWizardPolicy,
    ordered_fields,
    require_source_opt_in,
)
from core.application.management.command_specs.contracts import FieldType
from core.entrypoints.cli.platform.input_resolution import field_is_visible
from core.entrypoints.cli.platform.prompts import arrow_keys_available, ask_choice


def collect_plain(
    supplied: dict[str, Any], *, renderer: Any, advanced: bool = False, from_source: bool = False
) -> dict[str, Any]:
    policy = InstallerWizardPolicy(locked_keys=frozenset(supplied), advanced=advanced)
    explicit = set(supplied)
    values = policy.normalize(supplied, frozenset(explicit))
    require_source_opt_in(values, from_source=from_source)
    for original in ordered_fields():
        field = policy.resolve_field(original, values)
        if field.key in supplied or field.deferred_until_plan:
            continue
        if not field_is_visible(field, values) or (field.advanced and not advanced):
            continue
        if field.type is FieldType.SINGLE_SELECT and len(field.choices) == 1:
            values[field.key] = field.choices[0].value
            continue
        if field.description:
            click.echo(field.description)
        if field.type is FieldType.SINGLE_SELECT and field.choices and arrow_keys_available():
            current = values.get(field.key)
            values[field.key] = ask_choice(
                field.label, tuple(field.choices), str(current) if current else None
            )
        elif field.type is FieldType.SINGLE_SELECT and field.choices:
            for index, choice in enumerate(field.choices, 1):
                click.echo(f"  {index}. {choice.label}")
            default = next(
                (
                    str(index)
                    for index, choice in enumerate(field.choices, 1)
                    if choice.value == values.get(field.key)
                ),
                "1",
            )
            selected = click.prompt(
                field.label,
                type=click.Choice([str(index) for index in range(1, len(field.choices) + 1)]),
                default=default,
            )
            values[field.key] = field.choices[int(selected) - 1].value
        else:
            prompt_field = field.model_copy(
                update={"visible_if": {}, "default": values.get(field.key, field.default)}
            )
            answer = renderer.collect_missing((prompt_field,), {}, include_optional=True)
            values.update(answer)
        explicit.add(field.key)
        require_source_opt_in(values, from_source=from_source)
        values = policy.normalize(values, frozenset(explicit))
    return values


def collect_interactive(
    supplied: dict[str, Any],
    *,
    renderer: Any,
    advanced: bool = False,
    from_source: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Ask the installation questions one at a time; the review comes afterwards."""
    return collect_plain(
        supplied, renderer=renderer, advanced=advanced, from_source=from_source
    ), False


def confirm_installation(renderer: Any, values: dict[str, Any], *, advanced: bool) -> bool:
    policy = InstallerWizardPolicy(advanced=advanced)
    fields = tuple(policy.resolve_field(field, values) for field in ordered_fields())
    visible = tuple(
        field
        for field in fields
        if field_is_visible(field, values) and (advanced or not field.advanced)
    )
    displayed = dict(values)
    for field in visible:
        for choice in field.choices:
            if choice.value == displayed.get(field.key):
                displayed[field.key] = choice.label
                break
    # Visibility has already been evaluated against canonical values.
    spec = INSTALLER_SPEC.model_copy(
        update={"fields": tuple(field.model_copy(update={"visible_if": {}}) for field in visible)}
    )
    return bool(renderer.confirm_review(spec, displayed))
