"""First-party JSON, plain, and Rich-compatible result renderers."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal, Protocol

import click

from core.application.management.command_specs.contracts import (
    CLIFailureEnvelope,
    CLISuccessEnvelope,
    CommandSpec,
    ExactlyOneOfGroup,
    ExecutionResult,
    FieldSpec,
    FieldType,
    OperationProgressEvent,
    OperationStepStatus,
)
from core.entrypoints.cli.platform.confirmation import confirmation_token
from core.entrypoints.cli.platform.input_resolution import field_is_visible
from core.entrypoints.cli.platform.result_presentation import (
    _data,
    _progress_line,
    _render_error_findings_plain,
    _render_error_findings_rich,
    _render_extensions_create_plain,
    _render_readiness_plain,
    _render_readiness_rich,
    _render_rich_payload,
    _render_status_plain,
    _render_status_rich,
    _render_users_list_plain,
    _render_users_list_rich,
)

_REPORTS_DIAGNOSTICS = "reports.diagnostics"

_TOKEN_USERS_LIST = "users.list"
_TOKEN_EXTENSIONS_CREATE = "extensions.create"
_TOKEN_MATCH = "match:"


class Renderer(Protocol):
    def render_result(self, spec: CommandSpec, result: ExecutionResult) -> None: ...

    def collect_missing(
        self,
        fields: tuple[FieldSpec, ...],
        values: dict[str, Any],
        *,
        exactly_one_of: tuple[ExactlyOneOfGroup, ...] = (),
        include_optional: bool = False,
    ) -> dict[str, Any]: ...

    def confirm_review(self, spec: CommandSpec, values: dict[str, Any]) -> bool: ...

    def render_plan(self, spec: CommandSpec, plan: dict[str, Any]) -> None: ...

    def operation_progress(
        self, *, technical_sink: Callable[[str], None] | None = None
    ) -> AbstractContextManager[Callable[[OperationProgressEvent], None]]: ...


class JSONRenderer:
    def render_plan(self, spec: CommandSpec, plan: dict[str, Any]) -> None:
        del spec, plan

    def render_result(self, spec: CommandSpec, result: ExecutionResult) -> None:
        if result.error is not None:
            payload = CLIFailureEnvelope(
                schema="flow-steward.cli.v1",
                command=spec.command_id,
                error=result.error,
                operation_id=result.operation_id,
                log_path=result.log_path,
            )
        else:
            payload = CLISuccessEnvelope(
                schema="flow-steward.cli.v1",
                command=spec.command_id,
                result=_data(result),
                warnings=list(result.warnings),
                operation_id=result.operation_id,
                log_path=result.log_path,
            )
        click.echo(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, default=str))

    def collect_missing(
        self,
        fields: tuple[FieldSpec, ...],
        values: dict[str, Any],
        *,
        exactly_one_of: tuple[ExactlyOneOfGroup, ...] = (),
        include_optional: bool = False,
    ) -> dict[str, Any]:
        del fields
        del exactly_one_of
        del include_optional
        return values

    def confirm_review(self, spec: CommandSpec, values: dict[str, Any]) -> bool:
        del spec, values
        return False

    @contextmanager
    def operation_progress(
        self, *, technical_sink: Callable[[str], None] | None = None
    ) -> Iterator[Callable[[OperationProgressEvent], None]]:
        del technical_sink
        yield lambda _event: None


def _render_numbered_choices(field: FieldSpec) -> None:
    click.echo(f"{field.label} choices:")
    for index, choice in enumerate(field.choices, start=1):
        label = str(choice.label or choice.value)
        suffix = f" ({choice.value})" if choice.value != label else ""
        click.echo(f"  {index}. {label}{suffix}")


def _advanced_choice_has_no_alternative(field: FieldSpec) -> bool:
    """An advanced select whose catalog leaves at most one possible answer."""
    return (
        field.advanced
        and field.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}
        and field.choice_provider is not None
        and len(field.choices) <= 1
    )


def _has_prompt_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, [], ())


def _render_stack_logs_plain(payload: dict[str, Any]) -> None:
    for line in payload.get("lines") or ():
        click.echo(str(line))
    summary = {key: value for key, value in payload.items() if key != "lines"}
    click.echo(json.dumps(summary, ensure_ascii=False, default=str, sort_keys=True))


class PlainRenderer:
    def render_plan(self, spec: CommandSpec, plan: dict[str, Any]) -> None:
        click.echo(f"Impact plan for {spec.command_id}:")
        click.echo(json.dumps(plan, ensure_ascii=False, default=str, indent=2, sort_keys=True))

    def render_result(self, spec: CommandSpec, result: ExecutionResult) -> None:
        if result.error is not None:
            click.echo(f"Error: {result.error.message}")
            click.echo(f"Code: {result.error.code}")
            _render_error_findings_plain(result.error.details)
            if result.error.hint:
                click.echo(result.error.hint)
        elif spec.command_id == "status":
            _render_status_plain(_data(result))
        elif spec.command_id == "doctor":
            _render_readiness_plain(_data(result))
        elif spec.command_id == _TOKEN_USERS_LIST:
            _render_users_list_plain(_data(result))
        elif spec.command_id == _TOKEN_EXTENSIONS_CREATE:
            _render_extensions_create_plain(_data(result))
        elif spec.command_id == "stack.logs":
            _render_stack_logs_plain(_data(result))
        elif spec.command_id in _CONFIG_TEXT_RENDERERS:
            for line in _CONFIG_TEXT_RENDERERS[spec.command_id](_data(result)):
                click.echo(line)
        elif spec.command_id == _REPORTS_DIAGNOSTICS:
            # The report is the deliverable: one JSON document an operator saves and sends.
            report = _data(result).get("report") or {}
            click.echo(json.dumps(report, ensure_ascii=False, default=str, indent=2))
            return
        else:
            click.echo(json.dumps(_data(result), ensure_ascii=False, default=str, sort_keys=True))
        if result.log_path:
            click.echo(f"Log: {result.log_path}")

    def collect_missing(
        self,
        fields: tuple[FieldSpec, ...],
        values: dict[str, Any],
        *,
        exactly_one_of: tuple[ExactlyOneOfGroup, ...] = (),
        include_optional: bool = False,
    ) -> dict[str, Any]:
        by_key = {field.key: field for field in fields}
        for group in exactly_one_of:
            populated = [key for key in group.field_keys if _has_prompt_value(values.get(key))]
            if len(populated) == 1:
                continue
            group_fields = tuple(by_key[key] for key in group.field_keys)
            click.echo(f"{group.label} — required, choose exactly one:")
            for index, field in enumerate(group_fields, start=1):
                click.echo(f"  {index}. {field.label}")
            selected = click.prompt(
                group.label,
                type=click.Choice(
                    [str(index) for index in range(1, len(group_fields) + 1)],
                    case_sensitive=True,
                ),
            )
            selected_field = group_fields[int(selected) - 1]
            for key in group.field_keys:
                values.pop(key, None)
            values[selected_field.key] = click.prompt(
                selected_field.label,
                hide_input=selected_field.secret,
            )
        for field in fields:
            if field.key in values:
                continue
            if field.deferred_until_plan:
                continue
            if not field_is_visible(field, values):
                continue
            if _advanced_choice_has_no_alternative(field):
                # Nothing to decide: a single-account installation has exactly one
                # account, so the wizard neither offers it as a choice nor asks for
                # memberships in other accounts that do not exist.
                if field.type is FieldType.SINGLE_SELECT and field.choices:
                    values[field.key] = field.choices[0].value
                continue
            if field.type is FieldType.BOOLEAN:
                values[field.key] = click.confirm(field.label, default=bool(field.default))
            elif field.type is FieldType.SINGLE_SELECT and field.choices:
                _render_numbered_choices(field)
                choices = tuple(field.choices)
                default_index = next(
                    (
                        index
                        for index, choice in enumerate(choices, start=1)
                        if choice.value == str(field.default or "")
                    ),
                    1,
                )
                selected = click.prompt(
                    field.label,
                    type=click.Choice(
                        [str(index) for index in range(1, len(choices) + 1)],
                        case_sensitive=True,
                    ),
                    default=str(default_index),
                )
                values[field.key] = choices[int(selected) - 1].value
            elif field.type is FieldType.MULTI_SELECT and field.choices:
                _render_numbered_choices(field)
                default_items = (
                    field.default
                    if isinstance(field.default, (tuple, list, set, frozenset))
                    else ()
                )
                default = ",".join(
                    str(index)
                    for index, choice in enumerate(field.choices, start=1)
                    if choice.value in {str(item) for item in default_items}
                )
                while True:
                    raw = click.prompt(field.label, default=default, show_default=bool(default))
                    indexes = [item.strip() for item in str(raw).split(",") if item.strip()]
                    if all(
                        item.isdigit() and 1 <= int(item) <= len(field.choices) for item in indexes
                    ):
                        values[field.key] = list(
                            dict.fromkeys(field.choices[int(item) - 1].value for item in indexes)
                        )
                        break
                    click.echo(
                        f"Invalid value: choose comma-separated numbers from 1 to {len(field.choices)}."
                    )
            elif field.required or include_optional:
                default = None
                if include_optional and not field.secret:
                    default = field.default
                    if default is None and not field.required:
                        default = ""
                values[field.key] = click.prompt(
                    field.label,
                    hide_input=field.secret,
                    default=default,
                    show_default=default not in (None, ""),
                    type=int if field.type is FieldType.INTEGER else str,
                )
        return values

    def confirm_review(self, spec: CommandSpec, values: dict[str, Any]) -> bool:
        click.echo("Review pending change:")
        for field in spec.fields:
            if field.key in values and field_is_visible(field, values):
                shown = "[REDACTED]" if field.secret else values[field.key]
                click.echo(f"{field.label}: {shown}")
        if spec.command_id == _TOKEN_EXTENSIONS_CREATE:
            extension_id = str(values.get("extension_id") or "")
            output_root = Path(str(values.get("output_root") or "extensions"))
            try:
                from core.application.extensions.identity import extension_folder_name

                target_dir = output_root / extension_folder_name(extension_id)
            except ValueError:
                target_dir = output_root
            click.echo(f"Canonical extension ID: {extension_id}")
            click.echo(f"Target directory: {target_dir}")
            click.echo("Side effect: creation of a new extension scaffold.")
            click.echo("Warning: existing targets are not overwritten.")
        if spec.command_id == "projects.dispatch-notification":
            click.echo("Side effect: project notification dispatch will run for matching routes.")
        if spec.command_id == "reports.generate":
            click.echo("Side effect: report generation will run and may persist a report row.")
        if spec.command_id in {"jobs.approve", "jobs.reject"}:
            click.echo("Side effect: the approval thread will resume with this decision.")
        if spec.command_id == "jobs.cancel":
            click.echo("Side effect: the selected job will be marked cancelled.")
        if spec.command_id == "jobs.restart":
            click.echo("Side effect: a replacement job will be enqueued from the original inputs.")
        if spec.command_id.startswith("database."):
            click.echo(
                "Side effect: database tooling may read or update runtime metadata/artifacts."
            )
            click.echo("Warning: use disposable DSNs or temporary paths for tests and drills.")
        if spec.command_id.startswith(("testing.", "quality.", "localization.")):
            click.echo(
                "Side effect: tooling may run processes and write reports or generated files."
            )
            click.echo(
                "Warning: use disposable targets and temporary paths for mutation-capable runs."
            )
        policy = str(spec.confirmation_policy_ref or "")
        if policy.startswith(_TOKEN_MATCH):
            expected = confirmation_token(policy, values)
            if not expected:
                return False
            entered = click.prompt(f"Type {expected!r} to confirm", default="", show_default=False)
            return entered == expected
        return click.confirm("Apply this change?", default=False)

    @contextmanager
    def operation_progress(
        self, *, technical_sink: Callable[[str], None] | None = None
    ) -> Iterator[Callable[[OperationProgressEvent], None]]:
        def sink(event: OperationProgressEvent) -> None:
            line = _progress_line(event)
            click.echo(line)
            if technical_sink is not None:
                technical_sink(line)

        yield sink


class RichRenderer(PlainRenderer):
    """Render live structured progress, with deterministic redirected output."""

    def __init__(self, *, color_enabled: bool = True, animation_enabled: bool = True) -> None:
        self.color_enabled = color_enabled
        self.animation_enabled = animation_enabled

    def _console(self) -> Any:
        from rich.console import Console

        return Console(
            color_system="standard" if self.color_enabled else None,
            force_terminal=bool(self.color_enabled),
        )

    def render_plan(self, spec: CommandSpec, plan: dict[str, Any]) -> None:
        from rich.markup import escape
        from rich.table import Table

        def format_value(value: Any) -> str:
            if isinstance(value, dict):
                return (
                    "; ".join(
                        f"{str(nested_key).replace('_', ' ').title()}: {format_value(nested_value)}"
                        for nested_key, nested_value in value.items()
                    )
                    or "none"
                )
            if isinstance(value, (list, tuple)):
                return ", ".join(format_value(item) for item in value) or "none"
            if isinstance(value, bool):
                return "yes" if value else "no"
            if value is None:
                return "none"
            return str(value)

        console = self._console()
        console.print(
            f"[bold red]Impact plan[/bold red] for [bold]{escape(spec.command_id)}[/bold]"
        )
        identity = Table(show_header=False, box=None, padding=(0, 1))
        for key in ("extension_id", "revision", "install_id", "release_id"):
            value = plan.get(key)
            if value not in (None, ""):
                identity.add_row(
                    f"[cyan]{escape(key.replace('_', ' ').title())}[/cyan]", escape(str(value))
                )
        if identity.row_count:
            console.print(identity)
        section_styles = {"remove": "red", "affected": "yellow", "retained": "green"}
        for key, style in section_styles.items():
            rows = plan.get(key)
            if not isinstance(rows, list):
                continue
            console.print(f"[bold {style}]{key.title()}[/bold {style}] ({len(rows)})")
            if not rows:
                console.print("  [dim]none[/dim]")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    console.print(
                        f"  [{style}]•[/{style}] {escape(format_value(row))}",
                        highlight=False,
                    )
                    continue
                details = Table.grid(padding=(0, 1))
                details.add_column(style=style, no_wrap=True)
                details.add_column()
                for index, (field, value) in enumerate(row.items()):
                    marker = "•" if index == 0 else " "
                    label = str(field).replace("_", " ").title()
                    details.add_row(
                        f"  {marker} {escape(label)}",
                        escape(format_value(value)),
                    )
                console.print(details)

        rendered_keys = {
            "extension_id",
            "revision",
            "install_id",
            "release_id",
            *section_styles,
        }
        remaining = [
            (key, value)
            for key, value in plan.items()
            if key not in rendered_keys and value not in (None, "")
        ]
        if remaining:
            console.print("[bold cyan]Details[/bold cyan]")
            details = Table.grid(padding=(0, 1))
            details.add_column(style="cyan", no_wrap=True)
            details.add_column()
            for key, value in remaining:
                details.add_row(
                    escape(str(key).replace("_", " ").title()),
                    escape(format_value(value)),
                )
            console.print(details)

    def confirm_review(self, spec: CommandSpec, values: dict[str, Any]) -> bool:
        policy = str(spec.confirmation_policy_ref or "")
        if not policy.startswith(_TOKEN_MATCH):
            return super().confirm_review(spec, values)
        expected = confirmation_token(policy, values)
        if not expected:
            return False
        self._console().print(
            f"[bold yellow]Destructive action:[/bold yellow] type [bold]{expected}[/bold] to confirm."
        )
        entered = click.prompt(
            click.style(
                f"Type {expected!r} to confirm", fg="yellow" if self.color_enabled else None
            ),
            default="",
            show_default=False,
        )
        return entered == expected

    def render_result(self, spec: CommandSpec, result: ExecutionResult) -> None:
        from rich.markup import escape

        console = self._console()
        if result.error is not None:
            console.print(f"[bold red]Error[/bold red]: {escape(result.error.message)}")
            console.print(f"[red]Code[/red]: {escape(result.error.code)}")
            _render_error_findings_rich(console, result.error.details)
            if result.error.field_issues:
                for issue in result.error.field_issues:
                    console.print(
                        f"[yellow]Field[/yellow] {escape(issue.field)}: {escape(issue.message)}"
                    )
            if result.error.hint:
                console.print(f"[cyan]Hint[/cyan]: {escape(result.error.hint)}")
            if result.error.retryable:
                console.print("[yellow]Retryable[/yellow]: yes")
        elif spec.command_id == _REPORTS_DIAGNOSTICS:
            # The report is the deliverable: plain JSON that redirects straight to a file.
            report = _data(result).get("report") or {}
            click.echo(json.dumps(report, ensure_ascii=False, default=str, indent=2))
        else:
            payload = _data(result)
            status = str(payload.get("status") or "").strip()
            operation = str(payload.get("operation") or spec.command_id).strip()
            if status:
                console.print(
                    f"[bold green]✓[/bold green] {escape(operation)} "
                    f"[green]{escape(status)}[/green]"
                )
            else:
                console.print(f"[bold green]✓[/bold green] {escape(spec.command_id)}")

            if spec.command_id == "status":
                _render_status_rich(console, payload)
            elif spec.command_id == "doctor":
                _render_readiness_rich(console, payload)
            elif spec.command_id == _TOKEN_USERS_LIST:
                _render_users_list_rich(console, payload)
            elif spec.command_id in _CONFIG_TEXT_RENDERERS:
                for line in _CONFIG_TEXT_RENDERERS[spec.command_id](payload):
                    console.print(escape(line), highlight=False, soft_wrap=True)
            else:
                _render_rich_payload(console, payload, escape=escape)
            for warning in result.warnings:
                console.print(f"[yellow]Warning[/yellow]: {escape(warning.message)}")
        if result.log_path:
            console.print(
                f"[dim]Log: {escape(result.log_path)}[/dim]",
                highlight=False,
                soft_wrap=True,
            )

    @contextmanager
    def operation_progress(
        self, *, technical_sink: Callable[[str], None] | None = None
    ) -> Iterator[Callable[[OperationProgressEvent], None]]:
        from rich.console import Console

        color_system: Literal["auto", "standard", "256", "truecolor", "windows"] | None = "auto"
        if not self.color_enabled:
            color_system = None
        force_interactive = None if self.animation_enabled else False

        console = Console(color_system=color_system, force_interactive=force_interactive)
        started = time.monotonic()
        lock = threading.Lock()
        latest = OperationProgressEvent(
            step_id="operation",
            status=OperationStepStatus.RUNNING,
            label="Working",
        )
        stop_heartbeat = threading.Event()

        def record(event: OperationProgressEvent) -> None:
            line = _progress_line(event)
            if technical_sink is not None:
                technical_sink(line)

        if not console.is_terminal or not self.animation_enabled:

            def redirected_sink(event: OperationProgressEvent) -> None:
                nonlocal latest
                latest = event
                line = _progress_line(event)
                click.echo(line)
                if technical_sink is not None:
                    technical_sink(line)

            def heartbeat() -> None:
                while not stop_heartbeat.wait(5):
                    elapsed = time.monotonic() - started
                    redirected_sink(
                        latest.model_copy(
                            update={
                                "detail": f"waiting; {elapsed:.0f}s elapsed",
                                "elapsed_seconds": elapsed,
                            }
                        )
                    )

            if not console.is_terminal:
                thread = threading.Thread(target=heartbeat, daemon=True)
                thread.start()
                try:
                    yield redirected_sink
                finally:
                    stop_heartbeat.set()
                    thread.join(timeout=1)
            else:
                yield redirected_sink
            return

        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskID,
            TextColumn,
            TimeElapsedColumn,
        )

        tasks: dict[str, TaskID] = {}
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        progress.start()
        heartbeat_task: TaskID | None = None

        def heartbeat() -> None:
            nonlocal heartbeat_task
            while not stop_heartbeat.wait(5):
                elapsed = time.monotonic() - started
                with lock:
                    description = f"waiting; {elapsed:.0f}s elapsed"
                    if heartbeat_task is None:
                        heartbeat_task = progress.add_task(description, total=None)
                    else:
                        progress.update(heartbeat_task, description=description)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:

            def rich_sink(event: OperationProgressEvent) -> None:
                with lock:
                    task_id = tasks.get(event.step_id)
                    description = _progress_line(event)
                    if task_id is None:
                        task_id = progress.add_task(description, total=event.total)
                        tasks[event.step_id] = task_id
                    if event.total is not None and event.completed is not None:
                        progress.update(
                            task_id,
                            description=description,
                            total=event.total,
                            completed=event.completed,
                        )
                    elif event.total is not None:
                        progress.update(task_id, description=description, total=event.total)
                    elif event.completed is not None:
                        progress.update(task_id, description=description, completed=event.completed)
                    else:
                        progress.update(task_id, description=description)
                    if event.status in {
                        OperationStepStatus.SUCCEEDED,
                        OperationStepStatus.FAILED,
                        OperationStepStatus.CANCELLED,
                        OperationStepStatus.SKIPPED,
                    }:
                        progress.stop_task(task_id)
                record(event)

            yield rich_sink
        finally:
            stop_heartbeat.set()
            thread.join(timeout=1)
            progress.stop()


def _config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{name}={item}" for name, item in sorted(value.items())) or "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "none"
    if value is None or value == "":
        return "none"
    return str(value)


def _aligned(rows: list[tuple[str, str]], indent: str = "  ") -> list[str]:
    width = max((len(name) for name, _ in rows), default=0)
    return [f"{indent}{name.ljust(width)}  {value}" for name, value in rows]


def _config_show_lines(payload: dict[str, Any]) -> list[str]:
    """`config show` as sections; every changeable name is the one `config set` takes.

    --json keeps the machine shape.
    """
    from core.application.lifecycle.operator_settings import HOST_SETTINGS_DOCS_URL
    from core.application.lifecycle.setting_names import typed_setting_fields

    active = dict(payload.get("active") or {})
    host_settings = dict(active.pop("operator_settings", None) or {})
    typed = typed_setting_fields()
    changeable = [(name, _config_value(active[name])) for name in typed if name in active]
    fixed = [(name, _config_value(value)) for name, value in active.items() if name not in typed]
    lines = ["Settings"]
    lines.extend(_aligned(changeable))
    lines.extend(("", "Host settings set by the operator"))
    lines.extend(_aligned(sorted(host_settings.items())) if host_settings else ["  none"])
    lines.extend(("", "Pending changes"))
    diff = dict(payload.get("pending_diff") or {})
    pending_host = dict((diff.pop("operator_settings", None) or {}).get("pending") or {})
    rows = [
        (
            name,
            f"{_config_value(change.get('active'))} -> {_config_value(change.get('pending'))}",
        )
        for name, change in sorted(diff.items())
    ]
    rows.extend((name, value or "reset to default") for name, value in sorted(pending_host.items()))
    if rows:
        lines.extend(_aligned(rows))
        lines.append("  Apply them with: flow-steward config apply")
    else:
        lines.append("  none")
    lines.extend(("", "Fixed at installation"))
    lines.extend(_aligned(fixed) if fixed else ["  none"])
    lines.extend(("", "Protected values"))
    states = [
        (str(item.get("name") or ""), str(item.get("state") or ""))
        for item in payload.get("protected_setting_states") or ()
        if isinstance(item, dict)
    ]
    lines.extend(_aligned(states) if states else ["  none"])
    lines.extend(
        (
            "",
            "Change a setting: flow-steward config set NAME=VALUE, then flow-steward config apply",
            f"All settings: {HOST_SETTINGS_DOCS_URL}",
        )
    )
    return lines


_CONFIG_TEXT_RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "config.show": _config_show_lines,
}


RENDERERS: dict[str, Renderer] = {
    "status": RichRenderer(),
    "doctor": RichRenderer(),
    "stack.status": RichRenderer(),
    "stack.start": RichRenderer(),
    "stack.stop": RichRenderer(),
    "stack.restart": RichRenderer(),
    "stack.rebuild": RichRenderer(),
    "stack.logs": RichRenderer(),
    "environment.show": RichRenderer(),
    "environment.switch": RichRenderer(),
    "release.info": RichRenderer(),
    "release.check": RichRenderer(),
    "release.upgrade": RichRenderer(),
    "maintenance.status": RichRenderer(),
    "maintenance.enable": RichRenderer(),
    "maintenance.disable": RichRenderer(),
    "backup.create": RichRenderer(),
    "backup.list": RichRenderer(),
    "backup.verify": RichRenderer(),
    "clone.create": RichRenderer(),
    "clone.restore": RichRenderer(),
    "config.show": RichRenderer(),
    "proxy-fragment": RichRenderer(),
    "config.validate": RichRenderer(),
    "config.set": RichRenderer(),
    "config.apply": RichRenderer(),
    "users.issue-bootstrap-token": RichRenderer(),
    _TOKEN_USERS_LIST: RichRenderer(),
    "users.create": RichRenderer(),
    "users.set-password": RichRenderer(),
    "users.force-reset": RichRenderer(),
    "users.unlock": RichRenderer(),
    "users.set-active": RichRenderer(),
    "users.set-roles": RichRenderer(),
    "extensions.list": RichRenderer(),
    "extensions.show": RichRenderer(),
    "extensions.health": RichRenderer(),
    "extensions.install": RichRenderer(),
    "extensions.uninstall": RichRenderer(),
    "extensions.enable": RichRenderer(),
    "extensions.disable": RichRenderer(),
    _TOKEN_EXTENSIONS_CREATE: RichRenderer(),
    "extensions.validate": RichRenderer(),
    "extensions.test": RichRenderer(),
    "extensions.bundle": RichRenderer(),
    "extensions.lock-deps": RichRenderer(),
    "projects.list": RichRenderer(),
    "projects.show": RichRenderer(),
    "projects.notification-routes": RichRenderer(),
    "projects.notification-deliveries": RichRenderer(),
    "projects.dispatch-notification": RichRenderer(),
    "workflows.list": RichRenderer(),
    "workflows.entrypoints": RichRenderer(),
    "workflows.validate": RichRenderer(),
    "workflows.contract": RichRenderer(),
    "workflows.convert-group": RichRenderer(),
    "workflows.suggestions": RichRenderer(),
    "workflows.hooks-list": RichRenderer(),
    "workflows.hook-get": RichRenderer(),
    "workflows.hook-preview": RichRenderer(),
    "workflows.hook-bind": RichRenderer(),
    "workflows.hook-unbind": RichRenderer(),
    "workflows.hook-create-and-bind": RichRenderer(),
    "workflows.where-used": RichRenderer(),
    "workflows.authoring-schema": RichRenderer(),
    "workflows.draft-validate": RichRenderer(),
    "workflows.draft-save": RichRenderer(),
    "workflows.definition": RichRenderer(),
    "workflows.apply-changes": RichRenderer(),
    "workflows.export": RichRenderer(),
    "workflows.import": RichRenderer(),
    "workflows.run": RichRenderer(),
    "workflows.stats": RichRenderer(),
    "jobs.list": RichRenderer(),
    "jobs.show": RichRenderer(),
    "jobs.status": RichRenderer(),
    "jobs.logs": RichRenderer(),
    "jobs.messages": RichRenderer(),
    "jobs.errors": RichRenderer(),
    "jobs.approve": RichRenderer(),
    "jobs.reject": RichRenderer(),
    "jobs.ack": RichRenderer(),
    "jobs.unack": RichRenderer(),
    "jobs.cancel": RichRenderer(),
    "jobs.restart": RichRenderer(),
    "jobs.health-snapshot": RichRenderer(),
    "reports.generate": RichRenderer(),
    "reports.get": RichRenderer(),
    _REPORTS_DIAGNOSTICS: RichRenderer(),
    "database.status": RichRenderer(),
    "database.upgrade": RichRenderer(),
    "database.bootstrap-seed": RichRenderer(),
    "database.generate-migration": RichRenderer(),
    "database.query-diagnostics": RichRenderer(),
    "database.tune-env": RichRenderer(),
    "database.restore-parity-capture": RichRenderer(),
    "database.restore-parity-verify": RichRenderer(),
    "testing.smoke-test": RichRenderer(),
    "testing.nightfire-regression": RichRenderer(),
    "testing.fuzz-api": RichRenderer(),
    "testing.fuzz-mcp": RichRenderer(),
    "testing.fuzz-ui": RichRenderer(),
    "testing.cutover-preflight": RichRenderer(),
    "testing.cutover-smoke": RichRenderer(),
    "quality.lint": RichRenderer(),
    "quality.check-ci-excludes": RichRenderer(),
    "quality.check-naming": RichRenderer(),
    "quality.release-gate": RichRenderer(),
    "quality.verify": RichRenderer(),
    "quality.sonar-export-issues": RichRenderer(),
    "quality.sonar-ensure-rules": RichRenderer(),
    "quality.sonar-generate-backlog": RichRenderer(),
    "localization.bootstrap-catalog": RichRenderer(),
    "localization.rebuild-pot": RichRenderer(),
    "localization.init-locale": RichRenderer(),
    "localization.update": RichRenderer(),
    "localization.compile": RichRenderer(),
    "localization.validate-keys": RichRenderer(),
}


def renderer_for(
    spec: CommandSpec,
    *,
    json_requested: bool,
    plain_requested: bool,
    color_enabled: bool = True,
    animation_enabled: bool = True,
) -> Renderer:
    if json_requested:
        return JSONRenderer()
    if plain_requested:
        return PlainRenderer()
    renderer = RENDERERS[spec.result_renderer_ref]
    if isinstance(renderer, RichRenderer):
        if find_spec("rich") is None:
            return PlainRenderer()
        return RichRenderer(color_enabled=color_enabled, animation_enabled=animation_enabled)
    return renderer
