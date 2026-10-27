"""Contract tests for safe CLI command input resolution."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from core.application.management.command_specs.contracts import FieldIssue, FieldSpec, FieldType
from core.application.management.command_specs.users import USERS_SET_ROLES_SPEC
from core.entrypoints.cli.platform.input_resolution import (
    SECRET_FILE_REMEDIATION,
    load_input_json,
    resolve_non_secret_values,
    resolve_secret_values,
    validate_command_input,
)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool
    attempts: int
    roles: list[str]
    conditional: str | None = None
    password: str = Field(repr=False)


def _fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(key="name", label="Name", type=FieldType.TEXT, required=True),
        FieldSpec(key="enabled", label="Enabled", type=FieldType.BOOLEAN, required=True),
        FieldSpec(key="attempts", label="Attempts", type=FieldType.INTEGER, required=True),
        FieldSpec(key="roles", label="Roles", type=FieldType.MULTI_SELECT, required=True),
        FieldSpec(
            key="conditional",
            label="Conditional",
            type=FieldType.TEXT,
            visible_if={"enabled": True},
        ),
        FieldSpec(
            key="password",
            label="Password",
            type=FieldType.SECRET,
            required=True,
            secret=True,
            allow_secret_file=True,
            allow_secret_stdin=True,
        ),
    )


def test_non_secret_resolution_uses_documented_precedence_and_preserves_falsey_values() -> None:
    result = resolve_non_secret_values(
        fields=_fields(),
        explicit_values={"name": "cli", "enabled": False, "attempts": 0, "roles": []},
        input_json={"name": "json", "enabled": True, "attempts": 9, "roles": ["viewer"]},
        persisted_config={"name": "config"},
        interactive=False,
    )

    assert result.values == {"name": "cli", "enabled": False, "attempts": 0, "roles": []}
    assert not result.issues


def test_json_is_next_after_explicit_and_persisted_config_requires_opt_in() -> None:
    fields = (
        FieldSpec(key="name", label="Name", type=FieldType.TEXT, default="default"),
        FieldSpec(
            key="region",
            label="Region",
            type=FieldType.TEXT,
            allow_persisted_config=True,
            default="us",
        ),
    )
    result = resolve_non_secret_values(
        fields=fields,
        explicit_values={"name": "cli"},
        input_json={"name": "json"},
        persisted_config={"name": "config", "region": "eu"},
        interactive=False,
    )

    assert result.values == {"name": "cli", "region": "eu"}


def test_json_rejects_unknown_and_secret_fields_together(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    secret = "raw-secret-must-not-propagate"
    source.write_text(json.dumps({"name": "Ada", "unknown": "value", "password": secret}))

    loaded = load_input_json(source, io.StringIO(""), fields=_fields())

    assert {(issue.field, issue.code) for issue in loaded.issues} == {
        ("unknown", "unknown_field"),
        ("password", "secret_forbidden"),
    }
    assert loaded.values == {"name": "Ada"}
    assert secret not in repr(loaded)


def test_input_json_rejects_non_utf8_bytes_as_invalid_json(tmp_path: Path) -> None:
    source = tmp_path / "non-utf8.json"
    source.write_bytes(b'{"name":"Ada"}\xff')

    loaded = load_input_json(source, io.StringIO(""), fields=_fields())

    assert loaded.values == {}
    assert [(issue.field, issue.code) for issue in loaded.issues] == [
        ("input_json", "invalid_json")
    ]


def test_input_json_stdin_conflicts_with_secret_stdin() -> None:
    result = resolve_secret_values(
        fields=_fields(),
        secret_stdin_fields={"password"},
        input_json_path="-",
        stdin=io.StringIO("secret"),
    )

    assert [(issue.field, issue.code) for issue in result.issues] == [
        ("password", "stdin_conflict")
    ]


def test_missing_non_interactive_required_fields_are_aggregated() -> None:
    result = resolve_non_secret_values(
        fields=_fields(), explicit_values={}, input_json={}, persisted_config={}, interactive=False
    )

    assert {(issue.field, issue.code) for issue in result.issues} == {
        ("name", "missing"),
        ("enabled", "missing"),
        ("attempts", "missing"),
        ("roles", "missing"),
    }


def test_missing_required_secret_is_reported_without_reading_or_echoing_stdin() -> None:
    result = resolve_secret_values(fields=_fields(), stdin=io.StringIO(""))

    assert [(issue.field, issue.code) for issue in result.issues] == [("password", "missing")]


def test_hidden_conditional_values_are_removed() -> None:
    result = resolve_non_secret_values(
        fields=_fields(),
        explicit_values={
            "name": "Ada",
            "enabled": False,
            "attempts": 1,
            "roles": [],
            "conditional": "stale",
        },
        input_json={},
        persisted_config={},
        interactive=False,
    )

    assert "conditional" not in result.values


def test_secret_file_strips_one_trailing_line_ending_and_preserves_other_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    source = root / "password"
    source.write_bytes(b" value\nsecond\r\n")
    source.chmod(0o600)

    result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": source},
        stdin=io.StringIO(""),
        secret_root=root,
    )

    assert result.values == {"password": b" value\nsecond"}


@pytest.mark.parametrize("canonical_path", [False, True])
def test_secret_file_under_trusted_compact_state_alias_retains_strict_child_policy(
    tmp_path: Path, canonical_path: bool
) -> None:
    state = tmp_path / "data" / "state"
    root = state / "secret-input"
    root.mkdir(parents=True, mode=0o700)
    source = root / "password"
    source.write_bytes(b"fixture-secret\n")
    source.chmod(0o600)
    alias = tmp_path / "app-state"
    alias.symlink_to(state, target_is_directory=True)
    approved_root = alias / "secret-input"
    requested_root = root if canonical_path else approved_root
    result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": requested_root / "password"},
        stdin=io.StringIO(""),
        secret_root=approved_root,
    )
    assert not result.issues
    assert result.values == {"password": b"fixture-secret"}
    (root / "link").symlink_to(source)
    rejected = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": requested_root / "link"},
        stdin=io.StringIO(""),
        secret_root=approved_root,
    )
    assert rejected.issues[0].code == "unsafe_secret_file"


@pytest.mark.parametrize("unsafe", ["permissions", "owner", "directory-link", "foreign-alias"])
def test_trusted_secret_root_alias_does_not_relax_file_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    root = tmp_path / "state" / "secret-input"
    root.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "app-state"
    alias.symlink_to(root.parent, target_is_directory=True)
    approved_root = alias / "secret-input"
    source = root / "password"
    source.write_bytes(b"fixture-secret\n")
    source.chmod(0o600)
    requested = approved_root / source.name
    if unsafe == "permissions":
        source.chmod(0o644)
    elif unsafe == "owner":
        monkeypatch.setattr(os, "getuid", lambda: source.stat().st_uid + 1)
    elif unsafe == "directory-link":
        child_alias = root / "child-alias"
        child_alias.symlink_to(root, target_is_directory=True)
        requested = approved_root / child_alias.name / source.name
    else:
        foreign_alias = tmp_path / "unapproved-alias"
        foreign_alias.symlink_to(root, target_is_directory=True)
        requested = foreign_alias / source.name

    result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": requested},
        stdin=io.StringIO(""),
        secret_root=approved_root,
    )
    assert result.values == {}
    assert result.issues[0].code == "unsafe_secret_file"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX ownership and permissions are a container contract"
)
def test_secret_file_rejects_traversal_symlink_outside_root_and_unsafe_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    outside.chmod(0o600)
    link = root / "link"
    link.symlink_to(outside)
    unsafe = root / "unsafe"
    unsafe.write_bytes(b"secret")
    unsafe.chmod(0o644)

    link_result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": link},
        stdin=io.StringIO(""),
        secret_root=root,
    )
    unsafe_result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": unsafe},
        stdin=io.StringIO(""),
        secret_root=root,
    )

    assert link_result.issues[0].message == SECRET_FILE_REMEDIATION
    assert unsafe_result.issues[0].code == "unsafe_secret_file"
    assert SECRET_FILE_REMEDIATION in unsafe_result.issues[0].message


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX ownership and permissions are a container contract"
)
def test_secret_file_rejects_in_root_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    target_directory = root / "actual"
    target_directory.mkdir(mode=0o700)
    source = target_directory / "password"
    source.write_bytes(b"secret")
    source.chmod(0o600)
    linked_directory = root / "alias"
    linked_directory.symlink_to(target_directory, target_is_directory=True)

    result = resolve_secret_values(
        fields=_fields(),
        secret_file_paths={"password": linked_directory / "password"},
        stdin=io.StringIO(""),
        secret_root=root,
    )

    assert result.issues[0].code == "unsafe_secret_file"
    assert result.issues[0].message == SECRET_FILE_REMEDIATION


def test_exactly_one_secret_source_is_accepted_and_stdin_is_read_once() -> None:
    stream = io.StringIO("secret\n")
    conflict = resolve_secret_values(
        fields=_fields(),
        secret_values={"password": "forbidden"},
        secret_stdin_fields={"password"},
        stdin=stream,
    )
    resolved = resolve_secret_values(
        fields=_fields(), secret_stdin_fields={"password"}, stdin=io.StringIO("secret\n")
    )

    assert conflict.issues[0].code == "multiple_secret_sources"
    assert resolved.values == {"password": "secret"}


def test_validation_aggregates_pydantic_errors_without_retaining_secret_and_keeps_values() -> None:
    result = validate_command_input(
        input_model=_Input,
        values={
            "name": "Ada",
            "enabled": "not-bool",
            "attempts": "bad",
            "roles": [],
            "password": "never-show",
        },
    )

    assert result.value is None
    assert {issue.field for issue in result.issues} == {"enabled", "attempts"}
    assert all("never-show" not in issue.message for issue in result.issues)
    assert result.values["password"] == "never-show"


def test_validation_aggregates_required_group_and_model_issues() -> None:
    result = validate_command_input(
        input_model=USERS_SET_ROLES_SPEC.input_model,
        values={},
        fields=USERS_SET_ROLES_SPEC.fields,
        exactly_one_of=USERS_SET_ROLES_SPEC.exactly_one_of,
        preliminary_issues=(
            FieldIssue(field="roles", message="This field is required.", code="missing"),
        ),
    )

    assert [(issue.field, issue.code) for issue in result.issues] == [
        ("roles", "missing"),
        ("target", "exactly_one_of"),
    ]
