"""Pilot tests for the schema-driven Textual command form."""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import BaseModel

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, Input  # noqa: I001

from core.application.installer.spec import INSTALLER_SPEC
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    ExactlyOneOfGroup,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.projects import PROJECT_COMMAND_SPECS
from core.application.management.command_specs.quality import QUALITY_COMMAND_SPECS
from core.application.management.command_specs.testing import TESTING_COMMAND_SPECS
from core.application.management.command_specs.users import (
    USERS_CREATE_SPEC,
    USERS_FORCE_RESET_SPEC,
    USERS_SET_ROLES_SPEC,
)
from core.entrypoints.cli.tui.form_app import CommandFormApp, FormOutcome


ChoiceLoader = Callable[[str], Awaitable[tuple[ChoiceItem, ...]]]

TUI_THEME_COLOR_HEXES = {
    "#0b1020",
    "#151b2e",
    "#8a94a7",
    "#8b7cff",
    "#f5f7fa",
    "#ff6b7a",
    "#ffc857",
    "#5ee29a",
}
SVG_TERMINAL_CHROME_COLOR_HEXES = {
    "#28c840",
    "#292929",
    "#c5c8c6",
    "#febc2e",
    "#ff5f57",
}
PROJECTS_SHOW_SPEC = next(
    spec for spec in PROJECT_COMMAND_SPECS if spec.command_id == "projects.show"
)
QUALITY_VERIFY_SPEC = next(
    spec for spec in QUALITY_COMMAND_SPECS if spec.command_id == "quality.verify"
)
TESTING_FUZZ_API_SPEC = next(
    spec for spec in TESTING_COMMAND_SPECS if spec.command_id == "testing.fuzz-api"
)


class _ConditionalFormInput(BaseModel):
    """Minimal typed contract for conditional-field TUI regressions."""


class _ConditionalFormResult(BaseModel):
    """Minimal result contract for conditional-field TUI regressions."""


def _conditional_form_spec(
    fields: tuple[FieldSpec, ...],
    *,
    groups: tuple[ExactlyOneOfGroup, ...] = (),
) -> CommandSpec:
    return CommandSpec(
        command_id="tests.conditional",
        path=("tests", "conditional"),
        summary="Exercise conditional fields.",
        description="Conditional-field regression test fixture.",
        examples=(),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=fields,
        input_model=_ConditionalFormInput,
        result_model=_ConditionalFormResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref="tests.conditional",
        result_renderer_ref="tests",
        exactly_one_of=groups,
    )


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


async def _settle(pilot: Any) -> None:
    for _ in range(5):
        await pilot.pause()


def _screen_text(app: CommandFormApp) -> str:
    return html.unescape(app.export_screenshot(simplify=True)).replace("\xa0", " ")


def _svg_hex_colors(svg: str) -> set[str]:
    return {match.group(0).lower() for match in re.finditer(r"#[0-9A-Fa-f]{6}", svg)}


def _is_grayscale(color: str) -> bool:
    red, green, blue = (color[index : index + 2] for index in (1, 3, 5))
    return red == green == blue


async def _activate_focused_action(app: CommandFormApp, pilot: Any, selector: str) -> None:
    app.set_focus(app.query_one(selector))
    await pilot.press("enter")


def _checkbox_with_label(app: CommandFormApp, text: str) -> Checkbox:
    return next(checkbox for checkbox in app.query(Checkbox) if text in str(checkbox.label))


async def _account_and_project_loader(provider: str) -> tuple[ChoiceItem, ...]:
    if provider == "accounts.available":
        return (
            ChoiceItem(value="acct_1", label="Account One", description="Production account"),
            ChoiceItem(value="acct_2", label="Account Two", description="Sandbox account"),
        )
    if provider == "projects.available":
        return (
            ChoiceItem(value="proj_1", label="Project Alpha", description="Main project"),
            ChoiceItem(value="proj_2", label="Project Beta", description="Secondary project"),
        )
    return ()


async def _uuid_project_loader(provider: str) -> tuple[ChoiceItem, ...]:
    if provider == "projects.available":
        return (
            ChoiceItem(
                value="00000000-0000-0000-0000-000000000099",
                label="Project UUID",
                description="Real project identifier",
            ),
        )
    return await _account_and_project_loader(provider)


def test_form_renders_schema_sections_controls_and_hides_secrets() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"password": "swordfish", "roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            svg = _screen_text(app)

        assert "Required" in svg
        assert "Optional" in svg
        assert "Advanced" in svg
        assert "[Required]" in svg
        assert "(Optional)" in svg
        assert "Email" in svg
        assert "Password" in svg
        assert "Roles" in svg
        assert "Admin" in svg
        assert "Operator" in svg
        assert "Viewer" in svg
        assert "flow-steward users create" in svg
        assert "Primary account" not in svg
        assert "swordfish" not in svg

    _run(scenario())


def _choice_index(value: str) -> int:
    """Position of a file_storage choice in the installer spec, by value."""
    field = next(item for item in INSTALLER_SPEC.fields if item.key == "file_storage")
    return next(index for index, choice in enumerate(field.choices) if choice.value == value)


def test_installer_storage_switch_reacts_and_excludes_hidden_s3_values() -> None:
    """Catch a TUI that renders, validates, or submits inactive S3 fields."""

    async def scenario() -> None:
        app = CommandFormApp(
            spec=INSTALLER_SPEC,
            initial_values={
                "file_storage": "bundled",
                "s3_endpoint": "https://objects.example.test",
                "s3_credentials_path": "/run/secrets/s3.json",
            },
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert len(app.query("#field-s3_endpoint")) == 0
            assert len(app.query("#field-s3_credentials_path")) == 0

            # Select by value, not by position: the choice list gained local_fs after this
            # test was written, which silently shifted every index and made it assert
            # against the wrong backend.
            app.set_focus(
                app.query_one(f"#choice-file_storage-{_choice_index('external_s3')}", Checkbox)
            )
            await pilot.press("space")
            await _settle(pilot)
            assert app.query_one("#field-s3_endpoint", Input)
            assert app.query_one("#field-s3_credentials_path", Input)

            app.set_focus(
                app.query_one(f"#choice-file_storage-{_choice_index('bundled')}", Checkbox)
            )
            await pilot.press("space")
            await _settle(pilot)
            assert app._draft_values["file_storage"] == "bundled"
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)

            assert app._field_errors == {}
            assert "s3_endpoint" not in app._review_values
            assert "s3_credentials_path" not in app._review_values
            assert "/run/secrets/s3.json" not in _screen_text(app)

            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert "s3_endpoint" not in app.return_value.values
        assert "s3_credentials_path" not in app.return_value.values

    _run(scenario())


def test_conditional_boolean_text_and_multi_select_controllers_refresh_fields() -> None:
    """Catch controller handlers that update drafts without rebuilding dependent fields."""

    async def scenario() -> None:
        boolean = CommandFormApp(
            spec=_conditional_form_spec(
                (
                    FieldSpec(key="enabled", label="Enabled", type=FieldType.BOOLEAN),
                    FieldSpec(
                        key="boolean_target",
                        label="Boolean target",
                        type=FieldType.TEXT,
                        visible_if={"enabled": True},
                    ),
                )
            ),
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with boolean.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert len(boolean.query("#field-boolean_target")) == 0
            boolean.set_focus(boolean.query_one("#field-enabled", Checkbox))
            await pilot.press("space")
            await _settle(pilot)
            assert boolean.query_one("#field-boolean_target", Input)

        text = CommandFormApp(
            spec=_conditional_form_spec(
                (
                    FieldSpec(key="mode", label="Mode", type=FieldType.TEXT, default="hidden"),
                    FieldSpec(
                        key="text_target",
                        label="Text target",
                        type=FieldType.TEXT,
                        visible_if={"mode": "show"},
                    ),
                )
            ),
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with text.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert len(text.query("#field-text_target")) == 0
            mode_input = text.query_one("#field-mode", Input)
            mode_input.value = ""
            await _settle(pilot)
            mode_input.value = "show"
            await _settle(pilot)
            assert text.query_one("#field-text_target", Input)

        multi_select = CommandFormApp(
            spec=_conditional_form_spec(
                (
                    FieldSpec(
                        key="modes",
                        label="Modes",
                        type=FieldType.MULTI_SELECT,
                        choices=(ChoiceItem(value="enabled", label="Enabled"),),
                    ),
                    FieldSpec(
                        key="multi_target",
                        label="Multi target",
                        type=FieldType.TEXT,
                        visible_if={"modes": ["enabled"]},
                    ),
                )
            ),
            initial_values={"modes": ("enabled",)},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with multi_select.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert multi_select.query_one("#field-multi_target", Input)
            multi_select.set_focus(multi_select.query_one("#choice-modes-0", Checkbox))
            await pilot.press("space")
            await _settle(pilot)
            assert len(multi_select.query("#field-multi_target")) == 0

    _run(scenario())


def test_conditional_grouped_fields_follow_visibility_in_rendering_and_focus_order() -> None:
    """Catch grouped fields that bypass the shared conditional visibility predicate."""

    async def scenario() -> None:
        app = CommandFormApp(
            spec=_conditional_form_spec(
                (
                    FieldSpec(key="show_group", label="Show group", type=FieldType.BOOLEAN),
                    FieldSpec(
                        key="first_choice",
                        label="First choice",
                        type=FieldType.TEXT,
                        visible_if={"show_group": True},
                    ),
                    FieldSpec(
                        key="second_choice",
                        label="Second choice",
                        type=FieldType.TEXT,
                        visible_if={"show_group": True},
                    ),
                ),
                groups=(
                    ExactlyOneOfGroup(
                        key="target", label="Target", field_keys=("first_choice", "second_choice")
                    ),
                ),
            ),
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert len(app.query("#group-target")) == 0
            assert len(app.query("#field-first_choice")) == 0
            assert {"first_choice", "second_choice"}.isdisjoint(app._form_field_order())

            app.set_focus(app.query_one("#field-show_group", Checkbox))
            await pilot.press("space")
            await _settle(pilot)
            assert app.query_one("#group-target")
            assert app.query_one("#field-first_choice", Input)
            assert {"first_choice", "second_choice"} <= set(app._form_field_order())

            await pilot.press("space")
            await _settle(pilot)
            assert len(app.query("#group-target")) == 0
            assert {"first_choice", "second_choice"}.isdisjoint(app._form_field_order())

    _run(scenario())


def test_existing_user_form_declares_and_validates_required_target_group() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_FORCE_RESET_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            form = _screen_text(app)
            assert "Target — required, choose exactly one" in form
            assert "User ID [Choose exactly one]" in form
            assert "Email [Choose exactly one]" in form

            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            assert "Target is required; choose exactly one of User ID or Email." in _screen_text(
                app
            )

    _run(scenario())


def test_set_roles_form_aggregates_required_roles_and_target_errors() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_SET_ROLES_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            screen = _screen_text(app)

            assert "Roles is required." in screen
            assert "Target is required; choose exactly one of User ID or Email." in screen

    _run(scenario())


def test_integer_literal_field_preserves_typed_value_through_review() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=QUALITY_VERIFY_SPEC,
            initial_values={"tier": 2, "evidence_mode": "report"},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)

            assert app._field_errors == {}
            assert app._review_values["tier"] == 2
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["tier"] == 2

    _run(scenario())


def test_integer_zero_preserves_supplied_value_through_review() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=TESTING_FUZZ_API_SPEC,
            initial_values={"seed": 0},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert app.query_one("#field-seed", Input).value == "0"
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)

            assert app._field_errors == {}
            assert app._review_values["seed"] == 0
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["seed"] == 0

    _run(scenario())


def test_no_color_mode_uses_monochrome_rendering_and_disables_animation() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
            color_enabled=False,
            animation_enabled=False,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            colors = _svg_hex_colors(app.export_screenshot(simplify=True))

        assert colors.isdisjoint(TUI_THEME_COLOR_HEXES)
        assert all(_is_grayscale(color) for color in colors - SVG_TERMINAL_CHROME_COLOR_HEXES)
        assert app.animation_level == "none"

    _run(scenario())


def test_color_mode_uses_semantic_theme_and_keeps_animation_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")

    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
            color_enabled=True,
            animation_enabled=True,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            summary = app.query_one("#summary")
            required = app.query_one("#required-section")
            assert app.screen.styles.background.hex == "#0B1020"
            assert app.screen.styles.color.hex == "#F5F7FA"
            assert summary.styles.background.hex == "#151B2E"
            assert summary.styles.color.hex == "#8B7CFF"
            assert required.styles.color.hex == "#FFC857"
            assert {
                "#0b1020",
                "#151b2e",
                "#8a94a7",
                "#8b7cff",
                "#f5f7fa",
                "#ffc857",
            } <= _svg_hex_colors(app.export_screenshot(simplify=True))
        assert app.animation_level == "full"

    _run(scenario())


@pytest.mark.parametrize("color_enabled", [True, False])
def test_typed_input_remains_visible_while_focused(color_enabled: bool) -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
            color_enabled=color_enabled,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#field-email")
            await pilot.press("o", "g", "a")
            await _settle(pilot)
            input_widget = app.query_one("#field-email", Input)
            rendered = _screen_text(app)

            assert input_widget.value == "oga"
            assert "oga" in rendered
            assert input_widget.styles.color != input_widget.styles.background
            cursor = input_widget.get_component_rich_style("input--cursor")
            selection = input_widget.get_component_rich_style("input--selection")
            assert cursor.color != cursor.bgcolor
            assert selection.color != selection.bgcolor

    _run(scenario())


@pytest.mark.parametrize("color_enabled", [True, False])
def test_role_markers_distinguish_selected_from_unselected(color_enabled: bool) -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
            color_enabled=color_enabled,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            admin = _checkbox_with_label(app, "Admin")
            viewer = _checkbox_with_label(app, "Viewer")
            admin_marker = admin.get_component_rich_style("toggle--button")
            viewer_marker = viewer.get_component_rich_style("toggle--button")

            assert admin.value is False
            assert viewer.value is True
            assert admin_marker.color == admin_marker.bgcolor
            assert viewer_marker.color != viewer_marker.bgcolor

    _run(scenario())


def test_enter_toggles_focused_role_without_opening_review() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            admin = _checkbox_with_label(app, "Admin")
            app.set_focus(admin)
            await pilot.press("enter")
            await _settle(pilot)

            assert admin.value is True
            assert app._screen == "form"
            assert app.query_one("#action-review", Button)

    _run(scenario())


def test_review_prominently_shows_selected_roles() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={
                "email": "operator@example.com",
                "password": "swordfish",
                "roles": ["viewer"],
            },
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            await pilot.click("#field-password")
            await pilot.press(*"swordfish123!")
            admin = _checkbox_with_label(app, "Admin")
            app.set_focus(admin)
            await pilot.press("space")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)

            assert "Selected roles: Admin, Viewer" in _screen_text(app)

    _run(scenario())


def test_project_choices_load_successfully_and_query_failures_are_visible() -> None:
    async def failing_loader(provider: str) -> tuple[ChoiceItem, ...]:
        raise RuntimeError(f"{provider} query unavailable")

    async def scenario() -> None:
        app = CommandFormApp(
            spec=PROJECTS_SHOW_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert "Project Alpha" in _screen_text(app)

        failed = CommandFormApp(
            spec=PROJECTS_SHOW_SPEC,
            initial_values={},
            choice_loader=failing_loader,
            terminal_size=(80, 24),
        )
        async with failed.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            svg = _screen_text(failed)

        assert "Project choices are temporarily unavailable" in svg
        assert "./flow-steward doctor" in svg
        assert "Run flow-steward" not in svg
        assert "query unavailable" not in svg

    _run(scenario())


def test_shared_account_choice_provider_loads_once_and_failure_is_non_blocking() -> None:
    calls: list[str] = []

    async def failing_loader(provider: str) -> tuple[ChoiceItem, ...]:
        calls.append(provider)
        raise RuntimeError("database password must never be shown")

    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=failing_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            screen = _screen_text(app)
            assert screen.count("Account choices are temporarily unavailable") == 1
            assert "database password" not in screen

            await pilot.click("#field-email")
            await pilot.press(*"operator@example.com")
            await pilot.click("#field-password")
            await pilot.press(*"swordfish")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.status == "confirmed"
        assert "account_id" not in app.return_value.values
        assert "account_ids" not in app.return_value.values

    _run(scenario())
    assert calls == ["accounts.available"]


def test_project_uuid_choice_round_trips_without_widget_id_mangling() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=PROJECTS_SHOW_SPEC,
            initial_values={},
            choice_loader=_uuid_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            project_checkbox = next(
                checkbox
                for checkbox in app.query(Checkbox)
                if "Project UUID" in str(checkbox.label)
            )
            await pilot.click(f"#{project_checkbox.id}")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["project_id"] == "00000000-0000-0000-0000-000000000099"

    _run(scenario())


def test_navigation_contextual_help_inline_validation_and_cancel_keys() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#field-email")
            await pilot.press("b", "a", "d")
            await pilot.press("tab")
            await _settle(pilot)
            assert "Enter a valid email address." in _screen_text(app)

            await pilot.press("shift+tab")
            await pilot.press("f1")
            await _settle(pilot)
            help_text = _screen_text(app)
            email_field = next(field for field in USERS_CREATE_SPEC.fields if field.key == "email")
            assert email_field.description
            assert email_field.examples
            assert "Help: Email" in help_text
            assert email_field.description in help_text
            assert email_field.examples[0] in help_text

            await pilot.press("ctrl+c")

        assert app.return_value == FormOutcome(status="cancelled")

    _run(scenario())


def test_form_validation_preserves_inline_email_message_over_pydantic_issue() -> None:
    app = CommandFormApp(
        spec=USERS_CREATE_SPEC,
        initial_values={},
        choice_loader=_account_and_project_loader,
        terminal_size=(80, 24),
    )

    errors = app._validate_values({"email": "bad", "password": "swordfish", "roles": ["viewer"]})

    assert errors["email"] == "Enter a valid email address."


def test_keyboard_only_flow_reviews_returns_restores_focus_and_confirms() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={
                "email": "operator@example.com",
                "password": "swordfish",
                "roles": ["viewer"],
            },
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert getattr(app.focused, "id", None) == "field-email"

            await pilot.press("f1")
            await _settle(pilot)
            assert getattr(app.focused, "id", None) == "field-email"
            await pilot.press("tab")
            assert getattr(app.focused, "id", None) == "field-password"
            await pilot.press("shift+tab")
            assert getattr(app.focused, "id", None) == "field-email"

            for expected_focus in (
                "field-password",
                "field-display_name",
                "choice-roles-0",
            ):
                await pilot.press("tab")
                assert getattr(app.focused, "id", None) == expected_focus

            await pilot.press("down")
            assert getattr(app.focused, "id", None) == "choice-roles-1"
            await pilot.press("space")

            for expected_focus in (
                "choice-roles-2",
                "field-is_active",
                "field-must_change_password",
                "action-toggle-advanced",
                "action-review",
                "action-cancel",
            ):
                await pilot.press("tab")
                assert getattr(app.focused, "id", None) == expected_focus
            await pilot.press("shift+tab")
            assert getattr(app.focused, "id", None) == "action-review"

            await pilot.press("enter")
            await _settle(pilot)
            assert app._screen == "review"
            assert getattr(app.focused, "id", None) == "action-confirm"

            await pilot.press("escape")
            await _settle(pilot)
            assert app._screen == "form"
            assert getattr(app.focused, "id", None) == "action-review"

            await pilot.press("enter")
            await _settle(pilot)
            assert app._screen == "review"
            assert getattr(app.focused, "id", None) == "action-confirm"
            await pilot.press("enter")

        assert app.return_value is not None
        assert app.return_value.status == "confirmed"
        assert app.return_value.values["roles"] == ["operator", "viewer"]

    _run(scenario())


def test_keyboard_only_advanced_toggle_keeps_navigation_in_mounted_controls() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={
                "email": "operator@example.com",
                "password": "swordfish",
                "roles": ["viewer"],
            },
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            for _ in range(8):
                await pilot.press("tab")
            assert getattr(app.focused, "id", None) == "action-toggle-advanced"

            await pilot.press("enter")
            await _settle(pilot)
            assert app._advanced_expanded is True
            assert getattr(app.focused, "id", None) == "action-toggle-advanced"

            await pilot.press("tab")
            assert getattr(app.focused, "id", None) == "choice-account_id-0"
            await pilot.press("down")
            assert getattr(app.focused, "id", None) == "choice-account_id-1"
            await pilot.press("space")

        assert app._draft_values["account_id"] == "acct_2"

    _run(scenario())


def test_keyboard_only_validation_focuses_first_invalid_field() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            for expected_focus in (
                "field-password",
                "field-display_name",
                "choice-roles-0",
                "choice-roles-1",
                "choice-roles-2",
                "field-is_active",
                "field-must_change_password",
                "action-toggle-advanced",
                "action-review",
            ):
                await pilot.press("tab")
                assert getattr(app.focused, "id", None) == expected_focus

            await pilot.press("enter")
            await _settle(pilot)

            assert app._screen == "form"
            assert getattr(app.focused, "id", None) == "field-email"
            assert "Email is required." in _screen_text(app)
            assert "Password is required." in _screen_text(app)

    _run(scenario())


def test_review_validation_keeps_input_and_confirmation_returns_values_without_service_call() -> (
    None
):
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#field-email")
            await pilot.press("b", "a", "d")
            await pilot.click("#field-password")
            await pilot.press("s", "w", "o", "r", "d", "f", "i", "s", "h")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)

            assert app.return_value is None
            assert app.query_one("#field-email", Input).value == "bad"
            assert "Enter a valid email address." in _screen_text(app)

            app.query_one("#field-email", Input).value = "operator@example.com"
            await pilot.click(f"#{_checkbox_with_label(app, 'Operator').id}")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            review_svg = _screen_text(app)

            assert "Changed values" in review_svg
            assert "Unchanged values" in review_svg
            assert "Default/inherited values" in review_svg
            assert "Warnings" in review_svg
            assert "Side effects" in review_svg
            assert "Password: Configured" in review_svg
            assert "swordfish" not in review_svg

            await pilot.press("escape")
            await _settle(pilot)
            assert "Create a user." in _screen_text(app)

            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value == FormOutcome(
            status="confirmed",
            values={
                "email": "operator@example.com",
                "password": "swordfish",
                "roles": ["operator", "viewer"],
                "is_active": True,
                "must_change_password": False,
                "actor": "cli",
            },
        )

    _run(scenario())


def test_arrow_and_space_toggle_multi_select_without_typing_role_keys() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click(f"#{_checkbox_with_label(app, 'Admin').id}")
            await pilot.press("down")
            await pilot.press("space")
            await _settle(pilot)
            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["roles"] == ["admin", "operator", "viewer"]

    _run(scenario())


def test_collapsed_advanced_initial_values_are_preserved_in_confirmed_payload() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"], "account_id": "acct_1", "actor": "automation"},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["account_id"] == "acct_1"
        assert app.return_value.values["actor"] == "automation"
        assert "account_ids" not in app.return_value.values

    _run(scenario())


def test_advanced_section_can_expand_and_edit_account_scope_and_actor() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert "Primary account" not in _screen_text(app)

            toggle = app.query_one("#action-toggle-advanced", Button)
            app.set_focus(toggle)
            await pilot.press("enter")
            await _settle(pilot)
            expanded = _screen_text(app)
            assert "Primary account" in expanded
            actor = app.query_one("#field-actor", Input)

            await pilot.click(f"#{_checkbox_with_label(app, 'Account Two').id}")
            actor.value = "reviewer"
            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["account_id"] == "acct_2"
        assert app.return_value.values["actor"] == "reviewer"

    _run(scenario())


def test_advanced_edits_survive_collapse_before_review() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            toggle = app.query_one("#action-toggle-advanced", Button)
            app.set_focus(toggle)
            await pilot.press("enter")
            await _settle(pilot)

            await pilot.click(f"#{_checkbox_with_label(app, 'Account Two').id}")
            app.query_one("#field-actor", Input).value = "reviewer"
            toggle = app.query_one("#action-toggle-advanced", Button)
            app.set_focus(toggle)
            await pilot.press("enter")
            await _settle(pilot)
            assert "Primary account" not in _screen_text(app)

            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["account_id"] == "acct_2"
        assert app.return_value.values["actor"] == "reviewer"

    _run(scenario())


def test_terminal_size_boundaries_use_plain_fallback_and_minimum_message() -> None:
    async def scenario() -> None:
        full = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with full.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            assert "Create a user." in _screen_text(full)

        fallback = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(60, 20),
        )
        async with fallback.run_test(size=(60, 20)) as pilot:
            await _settle(pilot)
            assert "Plain fallback available" in _screen_text(fallback)
            await pilot.press("enter")
        assert fallback.return_value == FormOutcome(status="plain_fallback")

        refused = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={},
            choice_loader=_account_and_project_loader,
            terminal_size=(59, 19),
        )
        async with refused.run_test(size=(59, 19)) as pilot:
            await _settle(pilot)
            svg = _screen_text(refused)

        assert "Textual requires a terminal of at least 60x20; use --plain." in svg

    _run(scenario())


def test_confirmed_form_values_validate_through_the_command_input_model() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_account_and_project_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        validated = USERS_CREATE_SPEC.input_model.model_validate(app.return_value.values)
        assert validated.email == "o@e.x"
        assert validated.roles == ["viewer"]

    _run(scenario())


async def _single_account_loader(provider: str) -> tuple[ChoiceItem, ...]:
    if provider == "accounts.available":
        return (ChoiceItem(value="acct_main", label="Main account", description="Default"),)
    return await _account_and_project_loader(provider)


def test_a_single_account_installation_is_not_asked_for_an_account() -> None:
    async def scenario() -> None:
        app = CommandFormApp(
            spec=USERS_CREATE_SPEC,
            initial_values={"roles": ["viewer"]},
            choice_loader=_single_account_loader,
            terminal_size=(80, 24),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            toggle = app.query_one("#action-toggle-advanced", Button)
            app.set_focus(toggle)
            await pilot.press("enter")
            await _settle(pilot)
            expanded = _screen_text(app)
            assert "Primary account" not in expanded
            assert "Account memberships" not in expanded
            assert "Main account" not in expanded

            await pilot.click("#field-email")
            await pilot.press("o", "@", "e", ".", "x")
            await pilot.click("#field-password")
            await pilot.press("s", "e", "c", "r", "e", "t")
            await _activate_focused_action(app, pilot, "#action-review")
            await _settle(pilot)
            await _activate_focused_action(app, pilot, "#action-confirm")

        assert app.return_value is not None
        assert app.return_value.values["account_id"] == "acct_main"
        assert "account_ids" not in app.return_value.values

    _run(scenario())
