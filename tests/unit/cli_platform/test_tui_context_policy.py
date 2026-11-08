"""Context policy regressions for the generic Textual command adapter."""

from __future__ import annotations

import asyncio
import html
from typing import Any

import pytest
from pydantic import BaseModel

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, Input

from core.application.installer.spec import INSTALLER_SPEC
from core.application.installer.wizard import InstallerWizardPolicy
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.entrypoints.cli.tui.form_app import CommandFormApp


class _ContextInput(BaseModel):
    route: str
    certificate: str
    port: int


def _spec() -> CommandSpec:
    return CommandSpec(
        command_id="tests.context",
        path=("tests", "context"),
        summary="Configure website access",
        description="Contextual form fixture",
        examples=(),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="route",
                label="Website access",
                type=FieldType.SINGLE_SELECT,
                default="bundled",
                choices=(
                    ChoiceItem(value="bundled", label="Included gateway"),
                    ChoiceItem(value="external", label="Existing web server"),
                ),
            ),
            FieldSpec(
                key="certificate",
                label="Certificate",
                type=FieldType.SINGLE_SELECT,
                default="automatic",
                choices=(
                    ChoiceItem(value="automatic", label="Automatic certificate"),
                    ChoiceItem(value="trial", label="Local trial"),
                ),
            ),
            FieldSpec(
                key="port", label="Website port", type=FieldType.INTEGER, default=80, advanced=True
            ),
        ),
        input_model=_ContextInput,
        result_model=BaseModel,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref="tests.context",
        result_renderer_ref="tests",
    )


def _resolve(field: FieldSpec, values: dict[str, Any]) -> FieldSpec:
    if field.key == "certificate" and values.get("route") == "external":
        return field.model_copy(
            update={
                "choices": (ChoiceItem(value="existing", label="Web server certificate"),),
                "default": "existing",
            }
        )
    if field.key == "port":
        return field.model_copy(update={"visible_if": {"route": "manual"}})
    return field


def _normalize(values: dict[str, Any], explicit_keys: frozenset[str]) -> dict[str, Any]:
    result = dict(values)
    external = result.get("route") == "external"
    if "certificate" not in explicit_keys:
        result["certificate"] = "existing" if external else "automatic"
    if "port" not in explicit_keys:
        result["port"] = 443 if external else 80
    return result


def _app(initial_values: dict[str, Any] | None = None) -> CommandFormApp:
    return CommandFormApp(
        spec=_spec(),
        initial_values=initial_values or {},
        choice_loader=lambda _: (),
        terminal_size=(100, 40),
        field_resolver=_resolve,
        values_normalizer=_normalize,
    )


async def _settle(pilot: Any) -> None:
    for _ in range(5):
        await pilot.pause()


async def _activate(app: CommandFormApp, pilot: Any, selector: str) -> None:
    app.set_focus(app.query_one(selector, Button))
    await pilot.press("enter")
    await _settle(pilot)


def test_context_choices_refresh_without_a_visibility_change() -> None:
    async def scenario() -> None:
        app = _app()
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#choice-route-1", Checkbox).value = True
            await _settle(pilot)
            choices = [str(widget.label) for widget in app.query(Checkbox)]
            assert "Web server certificate" in choices
            assert "Automatic certificate" not in choices
            assert app.query_one("#choice-certificate-0", Checkbox).value is True
            await _activate(app, pilot, "#action-review")
            await _activate(app, pilot, "#action-confirm")
        assert app.return_value is not None
        assert app.return_value.values == {
            "route": "external",
            "certificate": "existing",
            "port": 443,
        }

    asyncio.run(scenario())


def test_context_normalizes_initial_values_before_render_and_review() -> None:
    async def scenario() -> None:
        app = _app({"route": "external"})
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            assert "Web server certificate" in str(
                app.query_one("#choice-certificate-0", Checkbox).label
            )
            assert not app.query("#field-port")
            await _activate(app, pilot, "#action-review")
            assert app.query("#action-confirm")
            await _activate(app, pilot, "#action-confirm")
        assert app.return_value is not None
        assert app.return_value.values["port"] == 443

    asyncio.run(scenario())


def test_context_preserves_deliberate_answers_during_unrelated_changes() -> None:
    async def scenario() -> None:
        spec = _spec().model_copy(
            update={
                "fields": (
                    *_spec().fields,
                    FieldSpec(key="note", label="Note", type=FieldType.TEXT),
                )
            }
        )
        app = CommandFormApp(
            spec=spec,
            initial_values={},
            choice_loader=lambda _: (),
            terminal_size=(100, 40),
            field_resolver=_resolve,
            values_normalizer=_normalize,
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#choice-certificate-1", Checkbox).value = True
            await _settle(pilot)
            app.query_one("#field-note", Input).value = "Keep the trial certificate"
            await _settle(pilot)
            await _activate(app, pilot, "#action-review")
            await _activate(app, pilot, "#action-confirm")
        assert app.return_value is not None
        assert app.return_value.values["certificate"] == "trial"

    asyncio.run(scenario())


def test_review_uses_human_readable_choice_labels() -> None:
    async def scenario() -> None:
        app = _app({"route": "external", "port": 8443})
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _activate(app, pilot, "#action-review")
            text = html.unescape(app.export_screenshot(simplify=True)).replace("\xa0", " ")
            assert "Website access: Existing web server" in text
            assert "Certificate: Web server certificate" in text
            assert "Website access: external" not in text
            await _activate(app, pilot, "#action-confirm")
        assert app.return_value is not None
        assert app.return_value.values["port"] == 8443

    asyncio.run(scenario())


def test_installer_hostname_edit_preserves_input_cursor_and_selection() -> None:
    async def scenario() -> None:
        policy = InstallerWizardPolicy()
        spec = INSTALLER_SPEC.model_copy(
            update={
                "fields": tuple(
                    field for field in INSTALLER_SPEC.fields if field.key == "public_host"
                )
            }
        )
        app = CommandFormApp(
            spec=spec,
            initial_values={"public_host": "example.com"},
            choice_loader=lambda _: (),
            terminal_size=(100, 40),
            field_resolver=policy.resolve_field,
            values_normalizer=policy.normalize,
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            hostname = app.query_one("#field-public_host", Input)
            app.set_focus(hostname)
            hostname.cursor_position = 3
            await pilot.press("shift+right", "shift+right")
            await _settle(pilot)
            assert hostname.selected_text == "mp"
            assert hostname.cursor_position == 5

            await pilot.press("s")
            await _settle(pilot)
            assert app.query_one("#field-public_host", Input) is hostname
            assert app.focused is hostname
            assert hostname.value == "exasle.com"
            assert hostname.cursor_position == 4
            assert hostname.selected_text == ""

            await pilot.press("t")
            await _settle(pilot)
            assert app.query_one("#field-public_host", Input) is hostname
            assert hostname.value == "exastle.com"
            assert hostname.cursor_position == 5
            assert hostname.selected_text == ""

    asyncio.run(scenario())
