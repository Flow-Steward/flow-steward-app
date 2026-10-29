"""The Template step: assemble one text from declared inputs with standard Jinja.

Config (``override.template``):

- ``text`` (required, may be empty): the Jinja source.
- ``format``: ``text`` (default), ``markdown`` or ``html``. HTML escapes every
  inserted value unless the template marks it ``safe``; the other formats insert
  values as they are. The format never converts the result.
- ``inputs``: the values the template reads, each
  ``{"name", "type", "required", "source"}``. ``name`` is the local name used in
  the template (``{{ header }}``); ``source`` is the workflow state selector it
  is read from (``state.outputs.header``). ``type`` is one of ``INPUT_TYPES``.
- ``result_target``: the state path that receives the rendered text.

The template sees only its declared inputs, typed as they are in state. A
required input that is missing fails the step before rendering; a missing
optional one stays undefined, so ``default(...)`` and ``is defined`` work, and
any other use of it fails the step (``StrictUndefined``). Rendering runs in the
bounded child process of ``jinja_sandbox``; a failed render writes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.state_paths import resolve_state_path_with_workflow_data
from core.infrastructure.security.jinja_sandbox import (
    MAX_TEMPLATE_CHARS,
    TEMPLATE_FORMATS,
    TemplateRenderError,
    check_template_syntax,
    render_template,
)
from core.infrastructure.security.prompt_safety import (
    is_forbidden_reference_path,
    is_sensitive_name,
    normalize_reference_path,
)
from core.infrastructure.security.secret_reference import is_secret_reference

TEMPLATE_CONFIG_KEY = "template"
DEFAULT_FORMAT = "text"
#: Contract types a template input may declare.
INPUT_TYPES = ("text", "number", "boolean", "object", "array", "json")
#: A rendered text up to this size is kept whole in the step's message, so Job
#: Details can show it in full; a longer one keeps only the bounded preview.
MESSAGE_TEXT_MAX_CHARS = 64_000

_STATE_PREFIX = "state."
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
#: Names Jinja reads as literals, operators or loop helpers, never as an input.
_RESERVED_NAMES = frozenset(
    {
        "and",
        "caller",
        "else",
        "false",
        "if",
        "in",
        "is",
        "loop",
        "none",
        "not",
        "or",
        "self",
        "true",
        "varargs",
        "kwargs",
        "False",
        "None",
        "True",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def template_format(config: Mapping[str, Any]) -> str:
    return _text(config.get("format")) or DEFAULT_FORMAT


def _normalize_input_type(value: Any) -> str:
    """Keep the legacy list spelling aligned with Transform's array type."""
    input_type = _text(value) or "text"
    return "array" if input_type == "list" else input_type


def template_inputs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The declared inputs, normalized; malformed rows are reported by validation."""
    rows = config.get("inputs")
    declared: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        declared.append(
            {
                "name": _text(row.get("name")),
                "type": _normalize_input_type(row.get("type")),
                "required": row.get("required") is not False,
                "source": _text(row.get("source")),
            }
        )
    return declared


def _source_problem(source: str) -> str:
    """Why a selector may not feed a template, or an empty string."""
    if not source.startswith(_STATE_PREFIX) or source == _STATE_PREFIX:
        return "must be a workflow variable such as state.outputs.text"
    path = normalize_reference_path(source[len(_STATE_PREFIX) :])
    forbidden, reason = is_forbidden_reference_path(path)
    if forbidden:
        return reason
    if any(is_sensitive_name(part) for part in path.split(".") if part):
        return "reads a credential field, which a template may not use"
    return ""


def validate_template_config(config: Mapping[str, Any], *, path_prefix: str) -> list[dict]:
    """Authoring issues for one ``template`` configuration."""
    issues: list[dict] = []

    def issue(field: str, code: str, message: str) -> None:
        issues.append(
            {
                "code": f"workflow.step_template_{code}",
                "message": message,
                "path": f"{path_prefix}.{field}",
                "severity": "error",
            }
        )

    source_text = config.get("text")
    output_format = template_format(config)
    if output_format not in TEMPLATE_FORMATS:
        issue("format", "format_invalid", "format must be text, markdown or html")
    if not isinstance(source_text, str):
        issue("text", "text_missing", "The Template step needs its template text")
    elif len(source_text) > MAX_TEMPLATE_CHARS:
        issue(
            "text", "text_too_large", f"The template is longer than {MAX_TEMPLATE_CHARS} characters"
        )
    else:
        problem = check_template_syntax(source_text, output_format)
        if problem is not None:
            where = f" (line {problem.line})" if problem.line else ""
            issue("text", "syntax", f"Template error{where}: {problem.message}")
    if not _text(config.get("result_target")):
        issue(
            "result_target", "result_target_missing", "Choose the variable that receives the text"
        )

    raw_inputs = config.get("inputs")
    if raw_inputs is not None and not isinstance(raw_inputs, list):
        issue("inputs", "inputs_invalid", "inputs must be a list")
    _validate_template_inputs(raw_inputs if isinstance(raw_inputs, list) else [], issue)
    return issues


def _validate_template_inputs(raw_inputs: list[Any], issue: Any) -> None:
    """Report each declared input whose name, type or source cannot be used."""
    seen: set[str] = set()
    for index, row in enumerate(raw_inputs):
        field = f"inputs[{index}]"
        if not isinstance(row, Mapping):
            issue(field, "input_invalid", "Each input needs a name, a type and a source")
            continue
        name = _text(row.get("name"))
        if not _IDENTIFIER.match(name) or name in _RESERVED_NAMES:
            issue(
                f"{field}.name",
                "input_name_invalid",
                f"'{name}' is not a usable name: use letters, digits and _, starting with a letter",
            )
        elif name in seen:
            issue(f"{field}.name", "input_name_duplicate", f"The input name '{name}' is used twice")
        seen.add(name)
        declared_type = _text(row.get("type")) or "text"
        input_type = _normalize_input_type(declared_type)
        if input_type not in INPUT_TYPES:
            issue(
                f"{field}.type",
                "input_type_invalid",
                f"Input '{name}' has type '{declared_type}'; use one of {', '.join(INPUT_TYPES)}",
            )
        source = _text(row.get("source"))
        problem = _source_problem(source) if source else "is missing"
        if problem:
            issue(f"{field}.source", "input_source_invalid", f"The source of '{name}' {problem}")


def _type_problem(value: Any, input_type: str) -> str:
    if input_type == "json":
        return ""
    expected = {
        "text": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }.get(input_type, False)
    if expected:
        return ""
    return f"expected {input_type}, got {_value_type_name(value)}"


def _value_type_name(value: Any) -> str:
    """Name a value in the vocabulary of the declared input types."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _holds_secret_reference(value: Any) -> bool:
    if is_secret_reference(value):
        return True
    if isinstance(value, Mapping):
        return any(_holds_secret_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_secret_reference(item) for item in value)
    return False


def _failure(reason: str, message: str, *, line: int | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": reason, "message": message}
    if line:
        error["line"] = line
    return {"ok": False, "failure_reason": reason, "errors": [error]}


def template_context(
    config: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The local values the template sees, or a failure naming the bad input."""
    context: dict[str, Any] = {}
    for row in template_inputs(config):
        name, source = row["name"], row["source"]
        value, found = resolve_state_path_with_workflow_data(
            dict(state), source.removeprefix(_STATE_PREFIX)
        )
        if not found:
            if row["required"]:
                return context, _failure(
                    "input_missing", f"Required input '{name}' has no value at {source}"
                )
            continue
        problem = _type_problem(value, row["type"])
        if problem:
            return context, _failure("input_type", f"Input '{name}': {problem}")
        if _holds_secret_reference(value):
            return context, _failure(
                "input_secret", f"Input '{name}' holds a credential, which a template may not use"
            )
        context[name] = value
    return context, None


def execute_template_step(config: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Render the configured template; ``{"ok": True, "text": ...}`` or a failure."""
    issues = validate_template_config(config, path_prefix="template")
    if issues:
        first = issues[0]
        return _failure("config_invalid", str(first.get("message") or "Invalid template step"))
    output_format = template_format(config)
    context, failure = template_context(config, state)
    if failure is not None:
        return {**failure, "inputs": context, "format": output_format}
    try:
        text = render_template(str(config.get("text")), context, output_format=output_format)
    except TemplateRenderError as exc:
        return {
            **_failure(exc.reason, exc.message, line=exc.line),
            "inputs": context,
            "format": output_format,
        }
    return {
        "ok": True,
        "text": text,
        "format": output_format,
        "length": len(text),
        "inputs": context,
    }


def template_step_artifacts(
    config: Mapping[str, Any],
    result: Mapping[str, Any],
    state_updates: dict[str, Any],
) -> dict[str, Any]:
    """What the step's message records: inputs, the produced text, and its format."""
    from core.application.orchestration.step_io_preview import step_io_payload, step_io_preview

    target = _text(config.get("result_target"))
    ok = bool(result.get("ok"))
    summary: dict[str, Any] = {
        "ok": ok,
        "format": result.get("format") or template_format(config),
        "result_target": target,
    }
    if ok:
        summary["length"] = result.get("length")
    else:
        summary["failure_reason"] = result.get("failure_reason")
        summary["errors"] = result.get("errors")
    artifacts: dict[str, Any] = {
        TEMPLATE_CONFIG_KEY: summary,
        "result_target": target,
        "input_payload": step_io_payload(dict(result.get("inputs") or {})),
    }
    if ok:
        artifacts["output_payload"] = {
            "result_target": target,
            "format": summary["format"],
            "value": step_io_preview(result.get("text")),
        }
        if len(str(result.get("text") or "")) <= MESSAGE_TEXT_MAX_CHARS:
            artifacts["_state_patch"] = state_updates
    return artifacts


def template_step_summary(result: Mapping[str, Any]) -> str:
    if result.get("ok"):
        return f"Rendered {result.get('length')} characters of {result.get('format')}"
    errors = result.get("errors") or [{}]
    first = errors[0] if isinstance(errors, list) and errors else {}
    message = _text(first.get("message")) if isinstance(first, Mapping) else ""
    return f"Template failed: {message or result.get('failure_reason')}"


__all__ = [
    "INPUT_TYPES",
    "MESSAGE_TEXT_MAX_CHARS",
    "TEMPLATE_CONFIG_KEY",
    "execute_template_step",
    "template_context",
    "template_format",
    "template_inputs",
    "template_step_artifacts",
    "template_step_summary",
    "validate_template_config",
]
