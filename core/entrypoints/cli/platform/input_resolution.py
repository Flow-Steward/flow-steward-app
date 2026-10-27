"""Safe, side-effect-free command input loading, precedence, and validation."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from pydantic import BaseModel, ValidationError

from core.application.management.command_specs.contracts import (
    ExactlyOneOfGroup,
    FieldIssue,
    FieldSpec,
)

DEFAULT_SECRET_FILE_ROOT = Path("/app/.flow-steward/secret-input")
SECRET_FILE_REMEDIATION = (  # pragma: allowlist secret
    "Use stdin for secrets outside .flow-steward/secret-input/."
)


@dataclass(frozen=True)
class ResolutionResult:
    """Resolved inputs plus safe aggregate issues, without presentation behavior."""

    values: dict[str, Any]
    issues: tuple[FieldIssue, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    """Pydantic validation output that retains values for interactive correction."""

    value: BaseModel | None
    values: dict[str, Any]
    issues: tuple[FieldIssue, ...] = ()


def load_input_json(
    path: str | Path,
    stdin: TextIO,
    *,
    fields: tuple[FieldSpec, ...] = (),
) -> ResolutionResult:
    """Load a JSON object and reject unknown or secret fields together."""
    try:
        raw = stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ResolutionResult(
            {},
            (
                FieldIssue(
                    field="input_json",
                    message="Input JSON must be a valid object.",
                    code="invalid_json",
                ),
            ),
        )
    if not isinstance(parsed, dict):
        return ResolutionResult(
            {},
            (
                FieldIssue(
                    field="input_json", message="Input JSON must be an object.", code="invalid_json"
                ),
            ),
        )
    by_key = {field.key: field for field in fields}
    issues: list[FieldIssue] = []
    for key in parsed:
        field = by_key.get(key)
        if field is None:
            issues.append(
                FieldIssue(field=key, message="Unknown input field.", code="unknown_field")
            )
        elif field.secret:
            issues.append(
                FieldIssue(
                    field=key,
                    message="Secrets cannot be supplied in input JSON.",
                    code="secret_forbidden",
                )
            )
    values = {
        key: value
        for key, value in parsed.items()
        if (field := by_key.get(key)) is not None and not field.secret
    }
    return ResolutionResult(values, tuple(issues))


def resolve_non_secret_values(
    *,
    fields: tuple[FieldSpec, ...],
    explicit_values: Mapping[str, Any],
    input_json: Mapping[str, Any],
    persisted_config: Mapping[str, Any],
    interactive: bool,
) -> ResolutionResult:
    """Resolve non-secret values in CLI, JSON, config, default, prompt order."""
    values: dict[str, Any] = {}
    for field in fields:
        if field.secret:
            continue
        if field.key in explicit_values:
            values[field.key] = explicit_values[field.key]
        elif field.key in input_json:
            values[field.key] = input_json[field.key]
        elif field.allow_persisted_config and field.key in persisted_config:
            values[field.key] = persisted_config[field.key]
        elif field.default is not None:
            values[field.key] = field.default
    _remove_hidden_values(fields, values)
    if interactive:
        return ResolutionResult(values)
    issues = tuple(
        FieldIssue(field=field.key, message="This field is required.", code="missing")
        for field in fields
        if not field.secret
        and field.required
        and field_is_visible(field, values)
        and field.key not in values
    )
    return ResolutionResult(values, issues)


def resolve_secret_values(
    *,
    fields: tuple[FieldSpec, ...],
    secret_values: Mapping[str, Any] | None = None,
    secret_file_paths: Mapping[str, str | Path] | None = None,
    secret_stdin_fields: Set[str] | None = None,
    input_json_path: str | Path | None = None,
    stdin: TextIO,
    secret_root: Path = DEFAULT_SECRET_FILE_ROOT,
) -> ResolutionResult:
    """Resolve secret sources without printing, logging, or duplicating stdin reads."""
    secret_values = secret_values or {}
    secret_file_paths = secret_file_paths or {}
    secret_stdin_fields = secret_stdin_fields or set()
    fields_by_key = {field.key: field for field in fields if field.secret}
    issues: list[FieldIssue] = []
    values: dict[str, Any] = {}
    if str(input_json_path) == "-" and secret_stdin_fields:
        return ResolutionResult(
            {},
            tuple(
                FieldIssue(
                    field=key,
                    message="Input JSON and secret input cannot both use stdin.",
                    code="stdin_conflict",
                )
                for key in sorted(secret_stdin_fields)
            ),
        )
    stdin_value: str | None = None
    for key, field in fields_by_key.items():
        source_count = (
            int(key in secret_values)
            + int(key in secret_file_paths)
            + int(key in secret_stdin_fields)
        )
        if source_count > 1:
            issues.append(
                FieldIssue(
                    field=key,
                    message="Specify exactly one secret source.",
                    code="multiple_secret_sources",
                )
            )
            continue
        if key in secret_values:
            values[key] = secret_values[key]
        elif key in secret_file_paths:
            if not field.allow_secret_file:
                issues.append(
                    FieldIssue(
                        field=key,
                        message="Secret files are not allowed for this field.",
                        code="secret_file_forbidden",
                    )
                )
                continue
            secret, issue = _read_secret_file(Path(secret_file_paths[key]), secret_root)
            if issue is not None:
                issues.append(FieldIssue(field=key, message=issue[0], code=issue[1]))
            else:
                values[key] = secret
        elif key in secret_stdin_fields:
            if not field.allow_secret_stdin:
                issues.append(
                    FieldIssue(
                        field=key,
                        message="Secret stdin is not allowed for this field.",
                        code="secret_stdin_forbidden",
                    )
                )
                continue
            if stdin_value is None:
                stdin_value = _strip_one_line_ending(stdin.read())
            values[key] = stdin_value
        elif field.required:
            issues.append(FieldIssue(field=key, message="This field is required.", code="missing"))
    return ResolutionResult(values, tuple(issues))


def validate_command_input(
    *,
    input_model: type[BaseModel],
    values: Mapping[str, Any],
    fields: tuple[FieldSpec, ...] = (),
    exactly_one_of: tuple[ExactlyOneOfGroup, ...] = (),
    preliminary_issues: tuple[FieldIssue, ...] = (),
) -> ValidationResult:
    """Validate all public input concerns through one stable issue pipeline."""
    retained = dict(values)
    issues = [*preliminary_issues]
    issues.extend(_exactly_one_issues(fields=fields, groups=exactly_one_of, values=retained))
    value: BaseModel | None = None
    try:
        value = input_model.model_validate(retained)
    except ValidationError as error:
        issues.extend(_pydantic_issues(error))
    deduplicated = _deduplicate_issues(tuple(issues), exactly_one_of=exactly_one_of)
    return ValidationResult(
        value=value if not deduplicated else None,
        values=retained,
        issues=deduplicated,
    )


def _pydantic_issues(error: ValidationError) -> tuple[FieldIssue, ...]:
    return tuple(
        FieldIssue(
            field=_error_field(item.get("loc", ())),
            message=str(item["msg"]),
            code=str(item["type"]),
        )
        for item in error.errors(include_input=False)
    )


def _deduplicate_issues(
    issues: tuple[FieldIssue, ...],
    *,
    exactly_one_of: tuple[ExactlyOneOfGroup, ...] = (),
) -> tuple[FieldIssue, ...]:
    """Keep issue order while avoiding duplicate public missing diagnostics."""
    deduplicated: list[FieldIssue] = []
    seen: set[tuple[str, str, str]] = set()
    missing_fields: set[str] = set()
    for issue in issues:
        identity = (issue.field, issue.message, issue.code)
        if (
            identity in seen
            or (issue.code == "missing" and issue.field in missing_fields)
            or _duplicates_exactly_one_group(issue, exactly_one_of)
        ):
            continue
        deduplicated.append(issue)
        seen.add(identity)
        if issue.code == "missing":
            missing_fields.add(issue.field)
    return tuple(deduplicated)


def _duplicates_exactly_one_group(issue: FieldIssue, groups: tuple[ExactlyOneOfGroup, ...]) -> bool:
    if issue.field != "input" or issue.code != "value_error":
        return False
    message = issue.message.lower()
    return "exactly one" in message and any(
        all(field_key in message for field_key in group.field_keys) for group in groups
    )


def _exactly_one_issues(
    *,
    fields: tuple[FieldSpec, ...],
    groups: tuple[ExactlyOneOfGroup, ...],
    values: Mapping[str, Any],
) -> tuple[FieldIssue, ...]:
    labels = {field.key: field.label for field in fields}
    issues: list[FieldIssue] = []
    for group in groups:
        populated = [key for key in group.field_keys if _has_public_value(values.get(key))]
        if len(populated) == 1:
            continue
        choices = " or ".join(
            labels.get(key, key.replace("_", " ").title()) for key in group.field_keys
        )
        issues.append(
            FieldIssue(
                field=group.key,
                message=f"{group.label} is required; choose exactly one of {choices}.",
                code="exactly_one_of",
            )
        )
    return tuple(issues)


def _has_public_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, [], ())


def _remove_hidden_values(fields: tuple[FieldSpec, ...], values: dict[str, Any]) -> None:
    for field in fields:
        if field.key in values and not field_is_visible(field, values):
            values.pop(field.key)


def field_is_visible(field: FieldSpec, values: Mapping[str, Any]) -> bool:
    """Evaluate renderer-neutral conditional field visibility."""
    for key, expected in field.visible_if.items():
        actual = values.get(key)
        if isinstance(expected, Mapping) and "values" in expected:
            if actual not in expected["values"]:
                return False
        elif actual != expected:
            return False
    return True


def _read_secret_file(path: Path, root: Path) -> tuple[bytes | None, tuple[str, str] | None]:
    try:
        root_real = root.resolve(strict=True)
        path_absolute = path.absolute()
        try:
            relative_path = path_absolute.relative_to(root_real)
        except ValueError:
            # Compact's image-owned state alias is part of the approved root,
            # not a user-supplied child link. Keep checking every child below
            # its resolved target and reject unrelated aliases as before.
            relative_path = path_absolute.relative_to(root.absolute())
        current = root_real
        for component in relative_path.parts:
            current /= component
            if current.is_symlink():
                return None, (SECRET_FILE_REMEDIATION, "unsafe_secret_file")
        if path.is_symlink():
            return None, (SECRET_FILE_REMEDIATION, "unsafe_secret_file")
        path_real = path.resolve(strict=True)
        path_real.relative_to(root_real)
        file_stat = path_real.stat()
    except (OSError, ValueError):
        return None, (SECRET_FILE_REMEDIATION, "unsafe_secret_file")
    if not stat.S_ISREG(file_stat.st_mode):
        return None, (SECRET_FILE_REMEDIATION, "unsafe_secret_file")
    if os.name != "nt" and (file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077):
        return None, (f"Unsafe secret file. {SECRET_FILE_REMEDIATION}", "unsafe_secret_file")
    try:
        return cast(bytes, _strip_one_line_ending(path_real.read_bytes())), None
    except OSError:
        return None, (SECRET_FILE_REMEDIATION, "unsafe_secret_file")


def _strip_one_line_ending(value: str | bytes) -> str | bytes:
    if isinstance(value, bytes):
        if value.endswith(b"\r\n"):
            return value[:-2]
        if value.endswith((b"\n", b"\r")):
            return value[:-1]
        return value
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith(("\n", "\r")):
        return value[:-1]
    return value


def _error_field(location: object) -> str:
    if not isinstance(location, tuple) or not location:
        return "input"
    return ".".join(str(part) for part in location)
