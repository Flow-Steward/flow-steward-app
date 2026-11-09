"""Presentation adapters for the installer's ordinary and advanced forms."""

from __future__ import annotations

from typing import Any

import click

from core.application.installer.errors import InstallerFailure
from core.application.installer.spec import INSTALLER_SPEC
from core.application.installer.wizard import (
    InstallerWizardPolicy,
    ordered_fields,
    require_source_opt_in,
)
from core.application.management.command_specs.contracts import FieldType
from core.entrypoints.cli.platform.input_resolution import field_is_visible
from core.entrypoints.cli.platform.modes import TextualAvailability


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
        if field.type is FieldType.SINGLE_SELECT and field.choices:
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
    decision: Any,
    terminal: Any,
    advanced: bool = False,
    from_source: bool = False,
) -> tuple[dict[str, Any], bool]:
    # The full-screen form is opt-in (--tui on a large enough terminal); every other
    # terminal, coloured or not, gets the line-by-line questions.
    if decision.textual_availability is not TextualAvailability.AVAILABLE:
        return collect_plain(
            supplied, renderer=renderer, advanced=advanced, from_source=from_source
        ), False
    from core.entrypoints.cli.tui.form_app import CommandFormApp, FormOutcome

    policy = InstallerWizardPolicy(locked_keys=frozenset(supplied), advanced=advanced)
    spec = INSTALLER_SPEC.model_copy(update={"fields": ordered_fields()})
    outcome = CommandFormApp(
        spec=spec,
        initial_values=supplied,
        choice_loader=lambda _provider: (),
        terminal_size=(terminal.columns, terminal.rows),
        color_enabled=decision.color_enabled,
        animation_enabled=decision.animation_enabled,
        field_resolver=policy.resolve_field,
        values_normalizer=policy.normalize,
    ).run() or FormOutcome(status="cancelled")
    if outcome.status == "plain_fallback":
        return collect_plain(
            supplied, renderer=renderer, advanced=advanced, from_source=from_source
        ), False
    if outcome.status != "confirmed":
        raise InstallerFailure(
            code="cancelled",
            cause="Installation cancelled.",
            remediation="Rerun ./install.sh when ready.",
            exit_code=10,
            retryable=False,
            step="configuration",
        )
    return policy.normalize(outcome.values, frozenset(supplied) | frozenset(outcome.values)), True


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
