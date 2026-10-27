"""Generic Textual form for schema-driven public CLI commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from inspect import isawaitable
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, Field
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Input, Static

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    FieldIssue,
    FieldSpec,
    FieldType,
    OperationType,
)
from core.entrypoints.cli.platform.input_resolution import (
    field_is_visible,
    validate_command_input,
)

ChoiceLoader = Callable[[str], tuple[ChoiceItem, ...] | Awaitable[tuple[ChoiceItem, ...]]]

MINIMUM_SIZE_MESSAGE = "Textual requires a terminal of at least 60x20; use --plain."


class FormOutcome(BaseModel):
    """Return value from the Textual form."""

    status: Literal["confirmed", "cancelled", "plain_fallback"]
    values: dict[str, Any] = Field(default_factory=dict)


class CommandFormApp(App[FormOutcome]):
    """Render a small generic form from a public command spec."""

    CSS_PATH = "theme.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel_or_back", "Back / cancel"),
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        ("f1", "context_help", "Help"),
        ("?", "context_help", "Help"),
        ("down", "focus_choice(1)", "Next choice"),
        ("up", "focus_choice(-1)", "Previous choice"),
    ]

    def __init__(
        self,
        *,
        spec: CommandSpec,
        initial_values: dict[str, Any],
        choice_loader: ChoiceLoader,
        terminal_size: tuple[int, int],
        color_enabled: bool = True,
        animation_enabled: bool = True,
    ) -> None:
        css_path = "theme.tcss" if color_enabled else "theme_monochrome.tcss"
        super().__init__(css_path=css_path, ansi_color=color_enabled)
        if not animation_enabled:
            self.animation_level = "none"
        self.spec = spec
        self.initial_values = dict(initial_values)
        self.choice_loader = choice_loader
        self.terminal_size = terminal_size
        self.color_enabled = color_enabled
        self.animation_enabled = animation_enabled
        self._choices: dict[str, tuple[ChoiceItem, ...]] = {
            field.key: tuple(field.choices) for field in spec.fields
        }
        self._choice_errors: dict[str, str] = {}
        self._loaded_choice_fields: set[str] = {
            field.key for field in spec.fields if not field.choice_provider
        }
        self._screen = "form"
        self._field_errors: dict[str, str] = {}
        self._help_text = "Focus a field and press F1 for detailed guidance."
        self._review_values: dict[str, Any] = {}
        self._return_focus_id: str | None = None
        self._advanced_expanded = False
        self._choice_values_by_widget_id: dict[str, tuple[str, str]] = {}
        self._draft_values: dict[str, Any] = {
            field.key: self._initial_seed(field) for field in spec.fields
        }

    def compose(self) -> ComposeResult:
        width, height = self.terminal_size
        if width < 60 or height < 20:
            yield Static(MINIMUM_SIZE_MESSAGE, id="minimum-size", classes="panel error")
            return
        if width < 80 or height < 24:
            yield Static(
                "Plain fallback available for terminals from 60x20 to 79x23.",
                id="plain-fallback",
                classes="panel",
            )
            yield Button("Use --plain", id="action-plain", variant="primary")
            return
        if self._screen == "review":
            yield from self._compose_review()
            return
        yield from self._compose_form()

    async def on_mount(self) -> None:
        provider_results: dict[str, tuple[ChoiceItem, ...]] = {}
        failed_providers: set[str] = set()
        for field in self.spec.fields:
            provider = field.choice_provider
            if not provider:
                continue
            if provider in provider_results:
                self._choices[field.key] = provider_results[provider]
                self._loaded_choice_fields.add(field.key)
                continue
            if provider in failed_providers:
                self._choices[field.key] = ()
                self._loaded_choice_fields.add(field.key)
                continue
            try:
                loaded_choices = self.choice_loader(provider)
                choices = await loaded_choices if isawaitable(loaded_choices) else loaded_choices
                provider_results[provider] = choices
                self._choices[field.key] = choices
                self._choice_errors.pop(field.key, None)
            except Exception:
                failed_providers.add(provider)
                self._choices[field.key] = ()
                self._choice_errors[field.key] = _choice_unavailable_message(
                    provider, optional=not field.required
                )
            self._loaded_choice_fields.add(field.key)
        await self.recompose()
        self._focus_form_start()

    async def on_input_changed(self, event: Input.Changed) -> None:
        field = self._field_from_widget_id(event.input.id or "")
        if field is None:
            return
        previous = self._draft_values.get(field.key)
        self._draft_values[field.key] = event.value
        self._help_text = _field_help(field)
        self._field_errors = {
            key: value for key, value in self._field_errors.items() if key != field.key
        }
        issue = self._inline_issue(field, event.value)
        if issue:
            self._field_errors[field.key] = issue
        self._set_validation_errors_display()
        if self._controller_visibility_changed(field.key, previous):
            await self._refresh_form_fields(event.input.id)

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        widget_id = event.checkbox.id or ""
        if not widget_id.startswith("choice-"):
            field = self._field_from_widget_id(widget_id)
            if field is not None and widget_id.startswith("field-"):
                previous = self._draft_values.get(field.key)
                self._draft_values[field.key] = event.checkbox.value
                if self._controller_visibility_changed(field.key, previous):
                    await self._refresh_form_fields(widget_id)
            return
        parsed = self._choice_from_widget_id(widget_id)
        if parsed is None:
            return
        field_key, selected = parsed
        field = self._field_by_key(field_key)
        if field is None:
            return
        if field.type is not FieldType.SINGLE_SELECT:
            previous = self._draft_values.get(field.key)
            self._draft_values[field.key] = self._selected_choices(field)
            if self._controller_visibility_changed(field.key, previous):
                await self._refresh_form_fields(widget_id)
            return
        if not event.value:
            previous = self._draft_values.get(field.key)
            self._draft_values[field.key] = self._single_choice_value(field)
            if self._controller_visibility_changed(field.key, previous):
                await self._refresh_form_fields(widget_id)
            return
        previous = self._draft_values.get(field.key)
        self._draft_values[field.key] = selected
        for choice in self._choices.get(field.key, ()):
            if choice.value == selected:
                continue
            checkbox = self.query_one(f"#{self._choice_widget_id(field, choice)}", Checkbox)
            checkbox.value = False
        if self._controller_visibility_changed(field.key, previous):
            await self._refresh_form_fields(widget_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "action-review":
                self._request_review()
            case "action-confirm":
                self.exit(FormOutcome(status="confirmed", values=dict(self._review_values)))
            case "action-cancel":
                self.exit(FormOutcome(status="cancelled"))
            case "action-plain":
                self.exit(FormOutcome(status="plain_fallback"))
            case "action-toggle-advanced":
                self._toggle_advanced()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter" and isinstance(self.focused, Button):
            self.action_submit()
            event.prevent_default()
            event.stop()

    def action_submit(self) -> None:
        if self.terminal_size[0] < 80 or self.terminal_size[1] < 24:
            if self.terminal_size[0] >= 60 and self.terminal_size[1] >= 20:
                self.exit(FormOutcome(status="plain_fallback"))
            return
        focused_id = getattr(self.focused, "id", None)
        if focused_id == "action-confirm":
            self.exit(FormOutcome(status="confirmed", values=dict(self._review_values)))
        elif focused_id == "action-cancel":
            self.exit(FormOutcome(status="cancelled"))
        elif focused_id == "action-toggle-advanced":
            self._toggle_advanced()
        elif self._screen == "form":
            self._request_review()

    def action_cancel(self) -> None:
        self.exit(FormOutcome(status="cancelled"))

    def action_cancel_or_back(self) -> None:
        if self._screen == "review":
            self._screen = "form"
            self.refresh(recompose=True)
            self.call_after_refresh(self._restore_form_focus)
            return
        self.exit(FormOutcome(status="cancelled"))

    def action_context_help(self) -> None:
        field = self._field_from_widget_id(getattr(self.focused, "id", "") or "")
        if field is not None:
            self._help_text = f"Help: {_field_help(field)}"
        elif self._help_text and not self._help_text.startswith("Help: "):
            self._help_text = f"Help: {self._help_text}"
        try:
            self.query_one("#field-help", Static).update(self._help_text)
        except NoMatches:
            return

    def action_focus_choice(self, offset: int) -> None:
        focused_id = getattr(self.focused, "id", "") or ""
        parsed = self._choice_from_widget_id(focused_id)
        if parsed is None:
            return
        field_key, value = parsed
        field = self._field_by_key(field_key)
        if field is None:
            return
        choices = [choice.value for choice in self._choices.get(field.key, ())]
        if value not in choices:
            return
        next_index = (choices.index(value) + offset) % len(choices)
        next_choice = self._choices.get(field.key, ())[next_index]
        self.set_focus(self.query_one(f"#{self._choice_widget_id(field, next_choice)}", Checkbox))

    def _toggle_advanced(self) -> None:
        focus_id = getattr(self.focused, "id", None)
        self._capture_visible_values()
        self._advanced_expanded = not self._advanced_expanded
        self.refresh(recompose=True)
        self.call_after_refresh(self._restore_widget_or_form, focus_id)

    def _compose_form(self) -> ComposeResult:
        self._choice_values_by_widget_id = {}
        examples = f" Examples: {self.spec.examples[0]}" if self.spec.examples else ""
        yield Static(
            f"{self.spec.summary}{examples}",
            id="summary",
            classes="panel accent",
        )
        yield Static(self._help_text, id="field-help", classes="panel")
        yield Static(_format_errors(self._field_errors), id="validation-errors", classes="error")
        yield from self._compose_form_fields()

        yield Horizontal(
            Button("Review", id="action-review", variant="primary", action="submit", compact=True),
            Button("Cancel", id="action-cancel", action="cancel", compact=True),
            classes="panel",
        )

    def _compose_form_fields(self) -> ComposeResult:
        self._choice_values_by_widget_id = {}
        for widget in self._form_field_widgets():
            widget.add_class("form-field-region")
            yield widget

    def _form_field_widgets(self) -> ComposeResult:

        grouped_keys = _grouped_field_keys(self.spec)
        by_key = {field.key: field for field in self.spec.fields}
        for group in self.spec.exactly_one_of:
            group_fields = tuple(
                by_key[key]
                for key in group.field_keys
                if field_is_visible(by_key[key], self._draft_values)
            )
            if not group_fields:
                continue
            yield Static(
                f"{group.label} — required, choose exactly one",
                id=f"group-{group.key}",
                classes="required",
            )
            for field in group_fields:
                yield from self._compose_field(field, marker="[Choose exactly one]")

        yield Static("Required", id="required-section", classes="required")
        for field in self._visible_fields(required=True, excluded_keys=grouped_keys):
            yield from self._compose_field(field)

        yield Static("Optional", id="optional-section", classes="muted")
        for field in self._visible_fields(required=False, excluded_keys=grouped_keys):
            yield from self._compose_field(field)

        advanced_label = (
            "Advanced (expanded)" if self._advanced_expanded else "Advanced (collapsed)"
        )
        yield Horizontal(
            Static(advanced_label, id="advanced-section", classes="muted"),
            Button(
                "Hide advanced" if self._advanced_expanded else "Show advanced",
                id="action-toggle-advanced",
                compact=True,
            ),
            classes="panel",
        )
        if self._advanced_expanded:
            for field in self._visible_fields(advanced=True):
                yield from self._compose_field(field)
        else:
            for field in self.spec.fields:
                if not field.advanced or not field_is_visible(field, self._draft_values):
                    continue
                if field.key in self._choice_errors:
                    yield Static(
                        self._choice_errors[field.key],
                        id=f"choice-error-{field.key}",
                        classes="error",
                    )

    def _compose_field(self, field: FieldSpec, *, marker: str | None = None) -> ComposeResult:
        marker = marker or ("[Required]" if field.required else "(Optional)")
        label = f"{field.label} {marker}"
        if field.secret and self._initial_for(field):
            label = f"{label} - Configured"
        yield Static(label, id=f"label-{field.key}", classes="field", markup=False)
        if field.type is FieldType.BOOLEAN:
            yield Checkbox(
                field.label,
                value=bool(self._initial_for(field)),
                id=f"field-{field.key}",
                classes="field",
                compact=True,
            )
        elif field.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}:
            choices = self._choices.get(field.key, ())
            if (
                not choices
                and field.choice_provider
                and field.key not in self._loaded_choice_fields
            ):
                yield Static(f"Loading {field.label} choices...", id=f"loading-{field.key}")
            for index, choice in enumerate(choices):
                widget_id = _choice_id(field.key, index)
                self._choice_values_by_widget_id[widget_id] = (field.key, choice.value)
                yield Checkbox(
                    f"{choice.label} - {choice.description}".rstrip(" -"),
                    value=choice.value in _as_list(self._initial_for(field)),
                    id=widget_id,
                    classes="field",
                    compact=True,
                )
            if field.key in self._choice_errors:
                yield Static(
                    self._choice_errors[field.key], id=f"choice-error-{field.key}", classes="error"
                )
        else:
            initial_value = self._initial_for(field)
            input_value = "" if field.secret or initial_value is None else str(initial_value)
            yield Input(
                value=input_value,
                placeholder=field.placeholder,
                password=field.secret,
                id=f"field-{field.key}",
                classes="field",
                compact=True,
            )
        if field.key in self._field_errors:
            yield Static(self._field_errors[field.key], id=f"error-{field.key}", classes="error")

    def _compose_review(self) -> ComposeResult:
        values = self._review_values
        yield Static(f"Review: {self.spec.summary}", id="review-title", classes="panel accent")
        roles_field = self._field_by_key("roles")
        if roles_field is not None and "roles" in values:
            labels_by_value = {
                choice.value: choice.label for choice in self._choices.get(roles_field.key, ())
            }
            selected_roles = [
                labels_by_value.get(value, value) for value in _as_list(values["roles"])
            ]
            yield Static(
                f"Selected roles: {', '.join(selected_roles)}",
                id="review-selected-roles",
                classes="panel required",
            )
        yield Static("Changed values", id="review-changed")
        for line in self._review_lines(values, changed=True):
            yield Static(line)
        yield Static("Unchanged values", id="review-unchanged")
        for line in self._review_lines(values, changed=False):
            yield Static(line)
        yield Static("Default/inherited values", id="review-defaults")
        for field in self.spec.fields:
            if (
                field.key in values
                and field_is_visible(field, values)
                and field.key not in self.initial_values
                and field.default is not None
            ):
                yield Static(f"{field.label}: {_display_value(field, values[field.key])}")
        yield Static("Warnings", id="review-warnings", classes="required")
        yield Static("! Review carefully before applying this command.")
        yield Static("Side effects", id="review-side-effects", classes="required")
        yield Static(_side_effect_text(self.spec.operation_type))
        yield Horizontal(
            Button(
                "Confirm", id="action-confirm", variant="success", action="submit", compact=True
            ),
            Button("Cancel", id="action-cancel", action="cancel", compact=True),
            classes="panel",
        )

    def _review_lines(self, values: dict[str, Any], *, changed: bool) -> Iterable[str]:
        for field in self.spec.fields:
            if field.key not in values or not field_is_visible(field, values):
                continue
            previous = self.initial_values.get(field.key, field.default)
            is_changed = values[field.key] != previous
            if is_changed is changed:
                yield f"{field.label}: {_display_value(field, values[field.key])}"

    def _request_review(self) -> None:
        values = self._current_values()
        errors = self._validate_values(values)
        if errors:
            self._field_errors = errors
            self._set_validation_errors_display()
            self.call_after_refresh(self._focus_first_invalid_field)
            return
        self._field_errors = {}
        self._review_values = values
        self._return_focus_id = getattr(self.focused, "id", None)
        self._screen = "review"
        self.refresh(recompose=True)
        self.call_after_refresh(self._focus_review_confirm)

    def _focus_form_start(self) -> None:
        if self._screen != "form":
            return
        for field_key in self._form_field_order():
            if self._focus_field(field_key):
                return
        self._focus_widget("action-plain") or self._focus_widget("action-review")

    def _restore_form_focus(self) -> None:
        self._restore_widget_or_form(self._return_focus_id)

    def _restore_widget_or_form(self, widget_id: str | None) -> None:
        if widget_id and self._focus_widget(widget_id):
            return
        self._focus_form_start()

    def _focus_review_confirm(self) -> None:
        if self._screen == "review":
            self._focus_widget("action-confirm")

    def _focus_first_invalid_field(self) -> None:
        invalid_keys = set(self._field_errors)
        for group in self.spec.exactly_one_of:
            if group.key in invalid_keys:
                invalid_keys.update(group.field_keys)
        for field_key in self._form_field_order():
            if field_key in invalid_keys and self._focus_field(field_key):
                return

    def _form_field_order(self) -> tuple[str, ...]:
        grouped_keys = _grouped_field_keys(self.spec)
        grouped = tuple(
            field.key
            for group in self.spec.exactly_one_of
            for field in (self._field_by_key(field_key) for field_key in group.field_keys)
            if field is not None and field_is_visible(field, self._draft_values)
        )
        required = tuple(
            field.key
            for field in self._visible_fields(
                required=True,
                excluded_keys=grouped_keys,
            )
        )
        optional = tuple(
            field.key
            for field in self._visible_fields(
                required=False,
                excluded_keys=grouped_keys,
            )
        )
        advanced = (
            tuple(field.key for field in self._visible_fields(advanced=True))
            if self._advanced_expanded
            else ()
        )
        return grouped + required + optional + advanced

    def _focus_field(self, field_key: str) -> bool:
        field = self._field_by_key(field_key)
        if field is None:
            return False
        if field.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}:
            choices = self._choices.get(field.key, ())
            if not choices:
                return False
            return self._focus_widget(self._choice_widget_id(field, choices[0]))
        return self._focus_widget(f"field-{field.key}")

    def _focus_widget(self, widget_id: str) -> bool:
        try:
            widget = self.query_one(f"#{widget_id}")
        except NoMatches:
            return False
        self.set_focus(widget)
        return True

    def _has_no_alternative(self, field: FieldSpec) -> bool:
        """An advanced select whose loaded catalog leaves at most one answer.

        A single-account installation has exactly one account, so the form
        neither offers it as a choice nor asks for memberships in accounts that
        do not exist.
        """
        return (
            field.advanced
            and field.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}
            and field.choice_provider is not None
            and field.key in self._loaded_choice_fields
            and field.key not in self._choice_errors
            and len(self._choices.get(field.key, ())) <= 1
        )

    def _current_values(self) -> dict[str, Any]:
        self._capture_visible_values()
        values: dict[str, Any] = {}
        for field in self.spec.fields:
            if not field_is_visible(field, self._draft_values):
                continue
            if self._has_no_alternative(field):
                given = self._draft_values.get(field.key, self._initial_seed(field))
                choices = self._choices.get(field.key, ())
                if given not in (None, [], ""):
                    values[field.key] = given
                elif field.type is FieldType.SINGLE_SELECT and choices:
                    values[field.key] = choices[0].value
                continue
            if field.advanced and not self._advanced_expanded:
                values[field.key] = self._draft_values.get(field.key, self._initial_seed(field))
                continue
            values[field.key] = self._read_widget_value(field)
        self._draft_values.update(values)
        return {key: value for key, value in values.items() if value not in (None, [], "")}

    def _capture_visible_values(self) -> None:
        for field in self.spec.fields:
            try:
                self._draft_values[field.key] = self._read_widget_value(field)
            except NoMatches:
                continue

    def _read_widget_value(self, field: FieldSpec) -> Any:
        if field.type is FieldType.BOOLEAN:
            return self.query_one(f"#field-{field.key}", Checkbox).value
        if field.type is FieldType.SINGLE_SELECT:
            for choice in self._choices.get(field.key, ()):
                checkbox = self.query_one(f"#{self._choice_widget_id(field, choice)}", Checkbox)
                if checkbox.value:
                    return choice.value
            return ""
        if field.type is FieldType.MULTI_SELECT:
            return self._selected_choices(field)
        value = self.query_one(f"#field-{field.key}", Input).value
        if field.secret and value == "":
            return self._draft_values.get(field.key, "")
        if field.type is FieldType.INTEGER and value != "":
            try:
                return int(value)
            except ValueError:
                return value
        return value

    def _validate_values(self, values: dict[str, Any]) -> dict[str, str]:
        preliminary_issues: list[FieldIssue] = []
        for field in self.spec.fields:
            if not field_is_visible(field, values):
                continue
            if field.required and field.key not in values:
                preliminary_issues.append(
                    FieldIssue(
                        field=field.key,
                        message=f"{field.label} is required.",
                        code="missing",
                    )
                )
            if field.key in values:
                inline_issue = self._inline_issue(field, values[field.key])
                if inline_issue:
                    preliminary_issues.append(
                        FieldIssue(field=field.key, message=inline_issue, code="inline")
                    )
        validation = validate_command_input(
            input_model=self.spec.input_model,
            values=values,
            fields=self.spec.fields,
            exactly_one_of=self.spec.exactly_one_of,
            preliminary_issues=tuple(preliminary_issues),
        )
        errors: dict[str, str] = {}
        for validation_issue in validation.issues:
            errors.setdefault(validation_issue.field, validation_issue.message)
        return errors

    def _inline_issue(self, field: FieldSpec, value: object) -> str:
        if field.required and value in ("", [], None):
            return f"{field.label} is required."
        if field.type is FieldType.EMAIL and value and not _looks_like_email(str(value)):
            return "Enter a valid email address."
        return ""

    def _set_validation_errors_display(self) -> None:
        try:
            self.query_one("#validation-errors", Static).update(_format_errors(self._field_errors))
        except NoMatches:
            return

    def _initial_for(self, field: FieldSpec) -> Any:
        if field.key in self._draft_values:
            return self._draft_values[field.key]
        return self._initial_seed(field)

    def _initial_seed(self, field: FieldSpec) -> Any:
        if field.key in self.initial_values:
            return self.initial_values[field.key]
        if field.default is not None:
            return _coerce_default(field.default)
        if field.type is FieldType.BOOLEAN:
            return False
        if field.type is FieldType.MULTI_SELECT:
            return []
        return ""

    def _selected_choices(self, field: FieldSpec) -> list[str]:
        selected = []
        for choice in self._choices.get(field.key, ()):
            checkbox = self.query_one(f"#{self._choice_widget_id(field, choice)}", Checkbox)
            if checkbox.value:
                selected.append(choice.value)
        return selected

    def _single_choice_value(self, field: FieldSpec) -> str:
        for choice in self._choices.get(field.key, ()):
            checkbox = self.query_one(f"#{self._choice_widget_id(field, choice)}", Checkbox)
            if checkbox.value:
                return choice.value
        return ""

    def _field_controls_visibility(self, field_key: str) -> bool:
        return any(field_key in field.visible_if for field in self.spec.fields)

    def _controller_visibility_changed(self, field_key: str, previous: object) -> bool:
        if not self._field_controls_visibility(field_key):
            return False
        previous_values = dict(self._draft_values)
        previous_values[field_key] = previous
        return any(
            field_is_visible(field, previous_values) != field_is_visible(field, self._draft_values)
            for field in self.spec.fields
        )

    def _visible_fields(
        self,
        *,
        required: bool | None = None,
        excluded_keys: set[str] | None = None,
        advanced: bool = False,
    ) -> tuple[FieldSpec, ...]:
        excluded_keys = excluded_keys or set()
        fields = tuple(
            field
            for field in self.spec.fields
            if field_is_visible(field, self._draft_values)
            and field.advanced is advanced
            and field.key not in excluded_keys
            and not self._has_no_alternative(field)
        )
        if required is None:
            return fields
        return tuple(field for field in fields if field.required is required)

    async def _refresh_form_fields(self, focus_id: str | None) -> None:
        self._capture_visible_values()
        await self.screen.remove_children(self.query(".form-field-region"))
        await self.screen.mount_all(self._compose_form_fields(), after="#validation-errors")
        self._restore_widget_or_form(focus_id)

    def _field_by_key(self, key: str) -> FieldSpec | None:
        return next((field for field in self.spec.fields if field.key == key), None)

    def _field_from_widget_id(self, widget_id: str) -> FieldSpec | None:
        if widget_id.startswith("field-"):
            return self._field_by_key(widget_id.removeprefix("field-"))
        if widget_id.startswith("choice-"):
            parsed = self._choice_from_widget_id(widget_id)
            if parsed is not None:
                return self._field_by_key(parsed[0])
        return None

    def _choice_widget_id(self, field: FieldSpec, choice: ChoiceItem) -> str:
        choices = self._choices.get(field.key, ())
        return _choice_id(field.key, choices.index(choice))

    def _choice_from_widget_id(self, widget_id: str) -> tuple[str, str] | None:
        return self._choice_values_by_widget_id.get(widget_id)


def _grouped_field_keys(spec: CommandSpec) -> set[str]:
    return {key for group in spec.exactly_one_of for key in group.field_keys}


def _field_help(field: FieldSpec) -> str:
    parts = [field.label]
    if field.description:
        parts.append(field.description)
    if field.examples:
        parts.append("Examples: " + ", ".join(field.examples))
    return " - ".join(parts)


def _choice_id(field_key: str, index: int) -> str:
    return f"choice-{field_key}-{index}"


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return [str(value)]


def _coerce_default(value: object) -> Any:
    if isinstance(value, tuple):
        return [*value]
    return value


def _display_value(field: FieldSpec, value: object) -> str:
    if field.secret:
        return "Configured" if value else "Not configured"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _format_errors(errors: dict[str, str]) -> str:
    return "\n".join(f"! {message}" for message in errors.values())


def _choice_unavailable_message(provider: str, *, optional: bool) -> str:
    noun = str(provider).partition(".")[0].replace("_", " ").strip().title()
    if noun.endswith("s"):
        noun = noun[:-1]
    next_step = (
        "Continue without this optional selection or run ./flow-steward doctor."
        if optional
        else "Run ./flow-steward doctor and retry this command."
    )
    return f"{noun or 'Requested'} choices are temporarily unavailable. {next_step}"


def _looks_like_email(value: str) -> bool:
    local, separator, domain = value.partition("@")
    return bool(local and separator and "." in domain and not domain.startswith("."))


def _side_effect_text(operation_type: OperationType) -> str:
    match operation_type:
        case OperationType.READ:
            return "No write side effects are expected."
        case OperationType.CHANGE:
            return "This command may write application state after confirmation."
        case OperationType.DESTRUCTIVE:
            return "This command may remove or overwrite application state after confirmation."
    return cast(str, operation_type)
