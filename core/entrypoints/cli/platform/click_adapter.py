"""Programmatic Click adapter for the closed public command registry."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import click

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

# Compose ``env_file`` may contain a stale NO_COLOR value. The launcher marker
# carries the host's actual intent; normalize it before Textual is imported.
if "FS_CLI_HOST_NO_COLOR" in _runtime_env:
    _host_no_color = _runtime_env.pop("FS_CLI_HOST_NO_COLOR")
    if _host_no_color:
        _runtime_env["NO_COLOR"] = _host_no_color
    else:
        _runtime_env.pop("NO_COLOR", None)

from core.application.management.command_specs.contracts import (
    CLIErrorDetail,
    CLIFailureEnvelope,
    CommandSpec,
    ExactlyOneOfGroup,
    ExecutionResult,
    ExitCode,
    FieldIssue,
    FieldSpec,
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.command_help import (
    _field_groups,
    _field_help,
    _help_rows,
    _make_help_formatter,
    _params_for_field,
    _record_help_color_flags,
    _secret_input_policy,
    _style_help,
)
from core.entrypoints.cli.platform.command_parameters import (
    _CONFIRM_REVIEW_PARAM_NAME,
    _confirmation_options,
    _field_params,
    _global_options,
    _normalize_click_value,
    _normalize_values_for_fields,
)
from core.entrypoints.cli.platform.confirmation import confirmation_token
from core.entrypoints.cli.platform.errors import map_exception
from core.entrypoints.cli.platform.execution import execute_command
from core.entrypoints.cli.platform.input_resolution import (
    DEFAULT_SECRET_FILE_ROOT,
    load_input_json,
    resolve_non_secret_values,
    resolve_secret_values,
    validate_command_input,
)
from core.entrypoints.cli.platform.modes import (
    OutputMode,
    TerminalFacts,
    TextualAvailability,
    select_mode,
)
from core.entrypoints.cli.platform.operation_log import (
    OperationLog,
    OperationLogUnavailable,
    operation_log_root,
)
from core.entrypoints.cli.platform.redaction import RedactionContext, redact_text
from core.entrypoints.cli.platform.renderers import renderer_for

_GLOBAL_PARAM_NAMES = ("json", "plain", "tui", "no_color", "verbose", "non_interactive")
_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _has_resolved_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, (), [])


class PublicCommand(click.Command):
    """Command with grouped, schema-derived help and JSON parser errors."""

    command_id: str = ""
    field_specs: tuple[FieldSpec, ...] = ()
    exactly_one_of: tuple[ExactlyOneOfGroup, ...] = ()
    examples: tuple[str, ...] = ()

    def get_help(self, ctx: click.Context) -> str:
        formatter = _make_help_formatter(ctx)
        self.format_help(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        _record_help_color_flags(ctx, args)
        if "--json" in args:
            ctx.meta["json_errors"] = True
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as error:
            if ctx.meta.get("json_errors"):
                click.echo(
                    _json_error(
                        self.command_id or ctx.info_name or "flow-steward",
                        "syntax_error",
                        str(error),
                    )
                )
                raise click.exceptions.Exit(2) from error
            raise

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        fields = getattr(self, "field_specs", ())
        if not fields:
            return super().format_options(ctx, formatter)
        formatter.write_paragraph()
        by_name = {param.name: param for param in self.get_params(ctx) if param.name is not None}
        global_rows = _help_rows(ctx, by_name, _GLOBAL_PARAM_NAMES)
        help_option = self.get_help_option(ctx)
        if help_option is not None:
            record = help_option.get_help_record(ctx)
            if record is not None:
                global_rows.append(record)
        if global_rows:
            with formatter.section("Global options"):
                formatter.write_dl(global_rows)
        input_rows = _help_rows(ctx, by_name, ("input_json",))
        if input_rows:
            with formatter.section("Input sources"):
                formatter.write_dl(input_rows)
        review_rows = _help_rows(ctx, by_name, (_CONFIRM_REVIEW_PARAM_NAME,))
        if review_rows:
            with formatter.section("Review"):
                formatter.write_dl(review_rows)
        for heading, selected, group_requirement in _field_groups(fields, self.exactly_one_of):
            rows = []
            for field in selected:
                params = _params_for_field(by_name, field)
                if not params:
                    continue
                records = [record for param in params if (record := param.get_help_record(ctx))]
                if records:
                    rows.append(
                        (
                            " / ".join(record[0] for record in records),
                            _field_help(
                                field,
                                " ".join(record[1] for record in records),
                                formatter=formatter,
                                group_requirement=group_requirement,
                            ),
                        )
                    )
                elif isinstance(params[0], click.Argument):
                    rows.append(
                        (
                            field.label,
                            _field_help(
                                field,
                                "",
                                formatter=formatter,
                                group_requirement=group_requirement,
                            ),
                        )
                    )
            if rows:
                with formatter.section(heading):
                    formatter.write_dl(rows)
        if secret_policy := _secret_input_policy(fields):
            formatter.write(
                _style_help(
                    formatter,
                    "Secret input policy:",
                    "help.warning",
                )
                + f" {secret_policy}\n"
            )
        return None

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_epilog(ctx, formatter)
        examples = getattr(self, "examples", ())
        if examples:
            with formatter.section("Examples"):
                for example in examples:
                    formatter.write_text(_style_help(formatter, example, "help.example"))


class PublicGroup(click.Group):
    """Group that accepts presentation flags before or after a segment."""

    def get_help(self, ctx: click.Context) -> str:
        formatter = _make_help_formatter(ctx)
        self.format_help(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        _record_help_color_flags(ctx, args)
        if "--json" in args:
            ctx.meta["json_errors"] = True
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as error:
            if ctx.meta.get("json_errors"):
                click.echo(_json_error(ctx.info_name or "flow-steward", "syntax_error", str(error)))
                raise click.exceptions.Exit(2) from error
            raise


def build_click_app(registry: Any, runtime: Any) -> click.Group:
    """Build the full public Click tree from ``PUBLIC_COMMAND_SPECS`` only."""
    root = PublicGroup(
        name="flow-steward",
        help="Flow Steward public management commands.",
        context_settings=_CONTEXT_SETTINGS,
        params=[_version_option(), *_global_options()],
    )
    groups: dict[str, PublicGroup] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 1:
            root.add_command(_build_leaf(spec, registry, runtime))
            continue
        domain = spec.path[0]
        group = groups.get(domain)
        if group is None:
            group = PublicGroup(
                name=domain,
                help=f"{domain.title()} commands.",
                params=_global_options(),
                context_settings=_CONTEXT_SETTINGS,
            )
            groups[domain] = group
            root.add_command(group)
        group.add_command(_build_leaf(spec, registry, runtime))
    return root


def _build_leaf(spec: CommandSpec, registry: Any, runtime: Any) -> PublicCommand:
    del runtime
    params = [
        *_global_options(),
        *_confirmation_options(spec),
        *_field_params(spec.fields, accepts_input_json=spec.accepts_input_json),
    ]

    def callback(**values: Any) -> None:
        ctx = click.get_current_context()
        globals_ = _global_values(ctx, values)
        terminal = _terminal_facts()
        decision = select_mode(
            terminal=terminal,
            json_requested=globals_["json"],
            plain_requested=globals_["plain"],
            tui_requested=globals_["tui"],
            no_color_requested=globals_["no_color"],
            non_interactive=globals_["non_interactive"],
            textual_eligible=(
                spec.interactive_policy is InteractivePolicy.ALWAYS_REVIEW
                and not globals_["json"]
                and not globals_["plain"]
                and not globals_["non_interactive"]
            ),
        )
        renderer = renderer_for(
            spec,
            json_requested=globals_["json"],
            plain_requested=(
                not globals_["tui"] or globals_["plain"] or decision.output_mode is OutputMode.PLAIN
            ),
            color_enabled=decision.color_enabled,
            animation_enabled=decision.animation_enabled,
        )
        if decision.textual_availability is TextualAvailability.REFUSED:
            _fail(
                spec,
                globals_["json"],
                "validation_failed",
                (),
                decision.minimum_size_message or "Terminal is too small; use --plain.",
                exit_code=ExitCode.VALIDATION,
            )
        explicit = {
            field.key: _normalize_click_value(field, values[field.key])
            for field in spec.fields
            if field.key in values and values[field.key] not in (None, ()) and not field.secret
        }
        input_json_values, input_json_issues = _input_json(values.get("input_json"), spec.fields)
        resolved = resolve_non_secret_values(
            fields=spec.fields,
            explicit_values=explicit,
            input_json=input_json_values,
            persisted_config={},
            interactive=decision.can_prompt,
        )
        secret_files = {
            field.key: values[f"_{field.key}_file"]
            for field in spec.fields
            if field.secret and values.get(f"_{field.key}_file")
        }
        secret_stdin = {
            field.key for field in spec.fields if field.secret and values.get(f"_{field.key}_stdin")
        }
        secrets = resolve_secret_values(
            fields=spec.fields,
            secret_file_paths=secret_files,
            secret_stdin_fields=secret_stdin,
            input_json_path=values.get("input_json"),
            stdin=sys.stdin,
            secret_root=DEFAULT_SECRET_FILE_ROOT,
        )
        preliminary_issues = (*input_json_issues, *resolved.issues, *secrets.issues)
        fatal_preliminary_issues = tuple(
            issue for issue in preliminary_issues if issue.code != "missing"
        )
        raw_confirmation = values.get(_CONFIRM_REVIEW_PARAM_NAME)
        review_confirmed = bool(raw_confirmation) and spec.confirmation_policy_ref is None
        review_required = True
        validation = validate_command_input(
            input_model=spec.input_model,
            values={**resolved.values, **secrets.values},
            fields=spec.fields,
            exactly_one_of=spec.exactly_one_of,
            preliminary_issues=preliminary_issues,
        )
        if fatal_preliminary_issues:
            _fail(spec, globals_["json"], "validation_failed", validation.issues)
        should_collect_missing = validation.value is None and (
            decision.can_prompt
            or decision.textual_availability
            in {TextualAvailability.REFUSED, TextualAvailability.RESIZE_OR_PLAIN}
        )
        should_collect_textual_review = (
            validation.value is not None
            and spec.interactive_policy is InteractivePolicy.ALWAYS_REVIEW
            and not review_confirmed
            and spec.confirmation_policy_ref is None
            and decision.textual_availability is TextualAvailability.AVAILABLE
        )
        if should_collect_missing or should_collect_textual_review:
            prompted, review_confirmed = _collect_interactive_values(
                spec=spec,
                registry=registry,
                renderer=renderer,
                decision=decision,
                initial_values=_normalize_values_for_fields(spec.fields, validation.values),
                terminal_size=(terminal.columns, terminal.rows),
                json_requested=globals_["json"],
            )
            if spec.confirmation_policy_ref:
                review_confirmed = False
            validation = validate_command_input(
                input_model=spec.input_model,
                values=prompted,
                fields=spec.fields,
                exactly_one_of=spec.exactly_one_of,
            )
        if validation.issues or validation.value is None:
            _fail(spec, globals_["json"], "validation_failed", validation.issues)
        command_input = validation.value
        assert command_input is not None
        prepared: Any = None
        if spec.plan_builder_ref:
            try:
                prepared = asyncio.run(registry.build_plan(spec.plan_builder_ref, command_input))
                command_input = prepared.execution_input
                review_required = prepared.review_required
                renderer.render_plan(spec, dict(prepared.review))
                review_confirmed = bool(raw_confirmation) and spec.confirmation_policy_ref is None
            except Exception as exc:
                if prepared is not None and prepared.cleanup is not None:
                    prepared.cleanup()
                plan_known_secrets = tuple(
                    str(command_input.model_dump()[field.key])
                    for field in spec.fields
                    if field.secret and command_input.model_dump().get(field.key) is not None
                )
                mapped = map_exception(
                    exc,
                    known_secrets=plan_known_secrets,
                    verbose=globals_["verbose"],
                    operation_type=spec.operation_type,
                )
                plan_log: OperationLog | None = None
                if spec.operation_type is not OperationType.READ:
                    with contextlib.suppress(Exception):
                        plan_log = OperationLog(spec.command_id)
                        plan_log.write(f"PLAN_FAILED {mapped.code}: {mapped.cause}")
                        plan_log.write(f"REMEDIATION: {mapped.remediation}")
                        plan_log.close()
                _fail(
                    spec,
                    globals_["json"],
                    mapped.code,
                    mapped.field_issues,
                    mapped.cause or mapped.summary,
                    exit_code=mapped.exit_code,
                    hint=mapped.remediation,
                    retryable=mapped.retryable,
                    operation_id=plan_log.operation_id if plan_log is not None else None,
                    log_path=str(plan_log.path) if plan_log is not None else None,
                )
            if prepared.deferred_fields:
                deferred = tuple(
                    field.model_copy(update={"deferred_until_plan": False, "required": True})
                    for field in spec.fields
                    if field.key in prepared.deferred_fields
                    and not _has_resolved_value(command_input.model_dump().get(field.key))
                )
                if deferred and decision.can_prompt:
                    existing_values = {
                        key: value
                        for key, value in command_input.model_dump(mode="python").items()
                        if _has_resolved_value(value)
                    }
                    prompted = renderer.collect_missing(
                        deferred,
                        existing_values,
                    )
                    validation = validate_command_input(
                        input_model=spec.input_model,
                        values=prompted,
                        fields=spec.fields,
                        exactly_one_of=spec.exactly_one_of,
                    )
                    if validation.issues or validation.value is None:
                        _fail(
                            spec,
                            globals_["json"],
                            "validation_failed",
                            validation.issues,
                        )
                    command_input = validation.value
                elif deferred:
                    _fail(
                        spec,
                        globals_["json"],
                        "validation_failed",
                        tuple(
                            FieldIssue(
                                field=field.key,
                                message="This field is required after reviewing source inventory.",
                                code="missing",
                            )
                            for field in deferred
                        ),
                        details={
                            "source_storage_inventory": prepared.review.get(
                                "source_storage_inventory"
                            )
                        },
                    )
            assert command_input is not None
        expected_confirmation = ""
        if spec.confirmation_policy_ref:
            expected_confirmation = confirmation_token(
                spec.confirmation_policy_ref,
                command_input.model_dump(),
            )
        if spec.confirmation_policy_ref and isinstance(raw_confirmation, str):
            review_confirmed = (
                bool(expected_confirmation) and raw_confirmation == expected_confirmation
            )
        if (
            spec.interactive_policy is InteractivePolicy.ALWAYS_REVIEW
            and review_required
            and not review_confirmed
        ):
            if decision.can_prompt and renderer.confirm_review(spec, command_input.model_dump()):
                review_confirmed = True
            else:
                if spec.plan_builder_ref and prepared.cleanup is not None:
                    with contextlib.suppress(Exception):
                        prepared.cleanup()
                if expected_confirmation and not globals_["json"]:
                    click.echo(f"Confirmation value: {expected_confirmation}")
                message = (
                    "Change cancelled."
                    if decision.can_prompt
                    else "Change requires confirmation; rerun interactively or pass --confirm."
                )
                _fail(
                    spec,
                    globals_["json"],
                    "cancelled",
                    (),
                    message,
                    exit_code=ExitCode.INTERRUPTED,
                    details=(
                        {
                            "review": dict(prepared.review),
                            "confirmation_value": expected_confirmation,
                        }
                        if globals_["json"] and prepared is not None
                        else None
                    ),
                )
        known_secrets = tuple(
            str(command_input.model_dump()[field.key])
            for field in spec.fields
            if field.secret and command_input.model_dump().get(field.key) is not None
        )
        operation_log: OperationLog | None = None
        invocation_started = False
        result: ExecutionResult | None = None
        try:
            if _should_create_operation_log(spec, command_input):
                # A mutating command records itself before it runs, so a log
                # that cannot be opened stops it. Name the reason: this is a
                # read-only or unwritable state directory, not a defect.
                try:
                    operation_log = OperationLog(spec.command_id)
                    operation_log.write(
                        f"command={spec.command_id} operation_id={operation_log.operation_id}"
                    )
                except OSError as exc:
                    raise OperationLogUnavailable(operation_log_root(), exc) from exc
            progress_technical_sink = operation_log.write if operation_log is not None else None
            service_technical_sink = _service_technical_sink(
                operation_log.write if operation_log is not None else None,
                verbose=globals_["verbose"],
                output_mode=decision.output_mode,
                known_secrets=known_secrets,
            )
            with renderer.operation_progress(
                technical_sink=progress_technical_sink
            ) as progress_sink:
                invocation_started = True
                result = execute_command(
                    spec=spec,
                    command_input=command_input,
                    services=registry,
                    progress_sink=progress_sink,
                    technical_sink=service_technical_sink,
                    known_secrets=known_secrets,
                    capture_stdout=decision.output_mode is OutputMode.JSON,
                )
            result = _with_operation_metadata(result, operation_log)
            if operation_log is not None:
                operation_log.write(f"exit_code={int(result.exit_code)}")
                operation_log.write(f"log_path={operation_log.path}")
        except Exception as exc:
            result = _mapped_execution_failure(
                exc,
                spec=spec,
                invocation_started=invocation_started,
                operation_log=operation_log,
                known_secrets=known_secrets,
                verbose=globals_["verbose"],
            )
        finally:
            if prepared is not None and prepared.cleanup is not None and not invocation_started:
                prepared.cleanup()
            if operation_log is not None:
                try:
                    operation_log.close()
                except Exception as exc:
                    result = _mapped_execution_failure(
                        exc,
                        spec=spec,
                        invocation_started=invocation_started,
                        operation_log=operation_log,
                        known_secrets=known_secrets,
                        verbose=globals_["verbose"],
                    )
        assert result is not None
        try:
            renderer.render_result(spec, result)
        except Exception as exc:
            result = _mapped_execution_failure(
                exc,
                spec=spec,
                invocation_started=invocation_started,
                operation_log=operation_log,
                known_secrets=known_secrets,
                verbose=globals_["verbose"],
            )
            _render_fallback_result(
                spec,
                result,
                output_mode=decision.output_mode,
            )
        if result.error is not None:
            raise click.exceptions.Exit(int(result.exit_code))
        process_exit_code = int(getattr(result.result, "process_exit_code", 0) or 0)
        if process_exit_code:
            raise click.exceptions.Exit(process_exit_code)

    command = PublicCommand(
        name=spec.path[-1],
        help=spec.summary,
        short_help=spec.summary,
        callback=callback,
        params=params,
        context_settings={
            **_CONTEXT_SETTINGS,
            "allow_extra_args": False,
            "ignore_unknown_options": False,
        },
    )
    command.command_id = spec.command_id
    command.field_specs = spec.fields
    command.exactly_one_of = spec.exactly_one_of
    command.examples = spec.examples
    return command


def _collect_interactive_values(
    *,
    spec: CommandSpec,
    registry: Any,
    renderer: Any,
    decision: Any,
    initial_values: dict[str, Any],
    terminal_size: tuple[int, int],
    json_requested: bool,
) -> tuple[dict[str, Any], bool]:
    if decision.minimum_size_message:
        _fail(
            spec,
            json_requested,
            "validation_failed",
            (),
            decision.minimum_size_message,
            exit_code=ExitCode.VALIDATION,
        )
    if decision.fallback_message:
        click.echo(decision.fallback_message)
    if decision.textual_availability is TextualAvailability.AVAILABLE:
        outcome = _run_textual_form(
            spec=spec,
            initial_values=initial_values,
            registry=registry,
            terminal_size=terminal_size,
            color_enabled=decision.color_enabled,
            animation_enabled=decision.animation_enabled,
        )
        status = str(getattr(outcome, "status", "cancelled"))
        if status == "cancelled":
            _fail(
                spec,
                json_requested,
                "cancelled",
                (),
                "Change cancelled.",
                exit_code=ExitCode.INTERRUPTED,
            )
        if status == "confirmed":
            return {**initial_values, **dict(getattr(outcome, "values", {}) or {})}, True
    try:
        plain_fields = _plain_fields_with_dynamic_choices(spec.fields, registry)
    except Exception as exc:
        mapped = map_exception(exc)
        _fail(
            spec,
            json_requested,
            mapped.code,
            mapped.field_issues,
            mapped.cause or mapped.summary,
            exit_code=mapped.exit_code,
        )
    if spec.exactly_one_of:
        return (
            renderer.collect_missing(
                plain_fields,
                dict(initial_values),
                exactly_one_of=spec.exactly_one_of,
            ),
            False,
        )
    return renderer.collect_missing(plain_fields, dict(initial_values)), False


def _plain_fields_with_dynamic_choices(
    fields: tuple[FieldSpec, ...], registry: Any
) -> tuple[FieldSpec, ...]:
    """Resolve each dynamic catalog once before the plain wizard prompts."""

    async def load() -> tuple[FieldSpec, ...]:
        catalogs: dict[str, tuple[Any, ...]] = {}
        resolved: list[FieldSpec] = []
        for field in fields:
            provider = str(field.choice_provider or "").strip()
            if not provider:
                resolved.append(field)
                continue
            if provider not in catalogs:
                catalogs[provider] = tuple(await registry.choices(provider))
            choices = catalogs[provider]
            if not choices and field.required:
                raise RuntimeError(f"No choices are available for {field.label}")
            resolved.append(field.model_copy(update={"choices": choices}))
        return tuple(resolved)

    return asyncio.run(load())


def _run_textual_form(
    *,
    spec: CommandSpec,
    initial_values: dict[str, Any],
    registry: Any,
    terminal_size: tuple[int, int],
    color_enabled: bool,
    animation_enabled: bool,
) -> Any:
    from core.entrypoints.cli.tui.form_app import CommandFormApp, FormOutcome

    async def choice_loader(provider: str) -> tuple[Any, ...]:
        return tuple(await registry.choices(provider))

    outcome = CommandFormApp(
        spec=spec,
        initial_values=initial_values,
        choice_loader=choice_loader,
        terminal_size=terminal_size,
        color_enabled=color_enabled,
        animation_enabled=animation_enabled,
    ).run()
    return outcome or FormOutcome(status="cancelled")


def _should_create_operation_log(spec: CommandSpec, command_input: Any | None = None) -> bool:
    del command_input
    if spec.lifecycle_interrupts:
        return False
    if spec.operation_type is not OperationType.READ:
        return True
    model_fields = getattr(spec.result_model, "model_fields", {})
    return "stdout_full" in model_fields or "stderr_full" in model_fields


def _service_technical_sink(
    log_sink: Any,
    *,
    verbose: bool,
    output_mode: OutputMode,
    known_secrets: tuple[str, ...] = (),
) -> Any:
    if log_sink is None and not verbose:
        return None
    context = RedactionContext(known_secrets=known_secrets)

    def sink(message: str) -> None:
        text = redact_text(str(message), context=context)
        if log_sink is not None:
            with contextlib.suppress(Exception):
                log_sink(text)
        if verbose:
            with contextlib.suppress(Exception):
                click.echo(text, err=output_mode is OutputMode.JSON)

    return sink


def _mapped_execution_failure(
    exc: BaseException,
    *,
    spec: CommandSpec,
    invocation_started: bool,
    operation_log: OperationLog | None,
    known_secrets: tuple[str, ...],
    verbose: bool,
) -> ExecutionResult:
    mapped = map_exception(
        exc,
        known_secrets=known_secrets,
        verbose=verbose,
        operation_type=spec.operation_type if invocation_started else None,
    )
    return _with_operation_metadata(
        ExecutionResult(
            error=CLIErrorDetail(
                code=mapped.code,
                message=mapped.cause or mapped.summary,
                field_issues=mapped.field_issues,
                hint=mapped.remediation,
                retryable=mapped.retryable,
            ),
            exit_code=mapped.exit_code,
        ),
        operation_log if invocation_started else None,
    )


def _with_operation_metadata(
    result: ExecutionResult,
    operation_log: OperationLog | None,
) -> ExecutionResult:
    if operation_log is None:
        return result
    return result.model_copy(
        update={
            "operation_id": operation_log.operation_id,
            "log_path": str(operation_log.path),
        }
    )


def _render_fallback_result(
    spec: CommandSpec,
    result: ExecutionResult,
    *,
    output_mode: OutputMode,
) -> None:
    """Render a failure without depending on the renderer that just failed."""
    assert result.error is not None
    if output_mode is OutputMode.JSON:
        payload = CLIFailureEnvelope(
            schema="flow-steward.cli.v1",
            command=spec.command_id,
            error=result.error,
            operation_id=result.operation_id,
            log_path=result.log_path,
        )
        click.echo(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, default=str))
        return
    click.echo(f"Error: {result.error.message}", err=True)
    click.echo(f"Code: {result.error.code}", err=True)
    if result.error.hint:
        click.echo(result.error.hint, err=True)


def _version_option() -> click.Option:
    return click.Option(
        ["--version"],
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_print_version,
        help="Show the Flow Steward CLI version and exit.",
    )


def _print_version(ctx: click.Context, _param: click.Parameter, value: object) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"flow-steward, version {_read_version()}")
    ctx.exit()


def _input_json(
    path: Path | None, fields: tuple[FieldSpec, ...]
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    if path is None:
        return {}, ()
    loaded = load_input_json(path, sys.stdin, fields=fields)
    return loaded.values, loaded.issues


def _global_values(ctx: click.Context, leaf_values: dict[str, Any]) -> dict[str, bool]:
    values = {name: bool(leaf_values.get(name)) for name in _GLOBAL_PARAM_NAMES}
    current: click.Context | None = ctx.parent
    while current is not None:
        for name in _GLOBAL_PARAM_NAMES:
            values[name] = values[name] or bool(current.params.get(name))
        current = current.parent
    return values


def _fail(
    spec: CommandSpec,
    json_requested: bool,
    code: str,
    issues: Iterable[Any],
    cause: str = "",
    *,
    exit_code: ExitCode = ExitCode.VALIDATION,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
    retryable: bool = False,
    operation_id: str | None = None,
    log_path: str | None = None,
) -> None:
    field_issues = tuple(issues)
    message = (
        cause
        or "; ".join(f"{issue.field}: {issue.message}" for issue in field_issues)
        or "Invalid input."
    )
    if json_requested:
        click.echo(
            _json_error(
                spec.command_id,
                code,
                message,
                field_issues=field_issues,
                details=details,
                hint=hint,
                retryable=retryable,
                operation_id=operation_id,
                log_path=log_path,
            )
        )
    else:
        click.echo(f"Error: {message}", err=True)
        if hint:
            click.echo(f"Hint: {hint}", err=True)
        if operation_id:
            click.echo(f"Operation ID: {operation_id}", err=True)
        if log_path:
            click.echo(f"Log: {log_path}", err=True)
    raise click.exceptions.Exit(int(exit_code))


def _json_error(
    command: str,
    code: str,
    message: str,
    *,
    field_issues: Iterable[Any] = (),
    hint: str | None = None,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    operation_id: str | None = None,
    log_path: str | None = None,
) -> str:
    return json.dumps(
        CLIFailureEnvelope(
            schema="flow-steward.cli.v1",
            command=command,
            error=CLIErrorDetail(
                code=code,
                message=message,
                field_issues=tuple(field_issues),
                hint=hint,
                retryable=retryable,
                details=details or {},
            ),
            operation_id=operation_id,
            log_path=log_path,
        ).model_dump(mode="json")
    )


def _result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return dict(result) if isinstance(result, dict) else {"value": str(result)}


def _terminal_facts() -> TerminalFacts:
    size = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((80, 24))
    return TerminalFacts(
        stdin_isatty=sys.stdin.isatty(),
        stdout_isatty=sys.stdout.isatty(),
        columns=size.columns,
        rows=size.lines,
        term=_runtime_env.get("TERM", ""),
        no_color_env=bool(_runtime_env.get("NO_COLOR")),
    )


def _read_version() -> str:
    version_file = Path(__file__).resolve().parents[4] / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"
