"""Human output: no command answers in JSON unless --json asked for it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.application.management.command_specs.contracts import (
    CLIWarning,
    CommandSpec,
    ExecutionResult,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.application.tooling.contracts import ToolRunResult
from core.entrypoints.cli.platform import command_names
from core.entrypoints.cli.platform.renderers import (
    _CONFIG_TEXT_RENDERERS,
    _OWN_LAYOUT_COMMANDS,
    _REPORTS_DIAGNOSTICS,
    PlainRenderer,
    RichRenderer,
)
from core.entrypoints.cli.platform.result_view import (
    emit_plain,
    format_scalar,
    humanize_key,
    plan_blocks,
    result_blocks,
)


@pytest.fixture(autouse=True)
def _canonical_prefix() -> Iterator[None]:
    command_names.set_invocation_prefix(None)
    yield
    command_names.set_invocation_prefix(None)


def _spec(command_id: str) -> CommandSpec:
    return next(spec for spec in PUBLIC_COMMAND_SPECS if spec.command_id == command_id)


def _plain(spec: CommandSpec, payload: dict, capsys: pytest.CaptureFixture[str]) -> str:
    emit_plain(result_blocks(spec, payload))
    return capsys.readouterr().out


def _result(payload: dict) -> ExecutionResult:
    # Any typed model satisfies the envelope; the renderer reads public_result.
    return ExecutionResult(
        result=ToolRunResult(operation="x", status="completed", exit_code=0),
        public_result=payload,
    )


def test_a_change_prints_a_result_line_and_the_facts_not_the_object(capsys) -> None:
    output = _plain(
        _spec("users.create"),
        {
            "status": "created",
            "user": {
                "id": "usr_42",
                "email": "anna@acme.io",
                "roles": ["admin"],
                "is_active": True,
            },
            "revoked_sessions": 0,
            "warnings": [],
        },
        capsys,
    )

    assert output.splitlines()[0] == "✓ Users: created"
    assert "anna@acme.io" in output
    assert "Active:" in output and "yes" in output
    assert "Roles:" in output and "admin" in output
    assert "{" not in output and '"' not in output


def test_next_steps_are_printed_with_the_name_the_operator_runs(capsys) -> None:
    command_names.set_invocation_prefix("./flow-steward")

    output = _plain(
        _spec("maintenance.enable"),
        {
            "status": "enabled",
            "changed": True,
            "next_steps": ["flow-steward maintenance disable --confirm"],
        },
        capsys,
    )

    assert "✓ Maintenance: enabled" in output
    assert "Next: ./flow-steward maintenance disable --confirm" in output
    assert "Next steps" not in output


def test_a_failed_status_is_marked_as_a_failure(capsys) -> None:
    output = _plain(_spec("maintenance.enable"), {"status": "blocked"}, capsys)

    assert output.splitlines()[0] == "✗ Maintenance: blocked"


def test_a_list_of_records_is_a_table_with_chosen_columns(capsys) -> None:
    output = _plain(
        _spec("users.list"),
        {
            "users": [
                {
                    "id": "usr_1",
                    "email": "a@acme.io",
                    "display_name": "Anna",
                    "roles": ["admin", "operator"],
                    "is_active": True,
                    "must_change_password": False,
                    "account_id": "acc_1",
                    "password_hash_version": 3,
                },
                {
                    "id": "usr_2",
                    "email": "bob@acme.io",
                    "display_name": "",
                    "roles": [],
                    "is_active": False,
                    "must_change_password": True,
                    "account_id": "acc_1",
                },
            ],
            "total": 2,
            "has_more": False,
        },
        capsys,
    )
    lines = output.splitlines()
    header = next(line for line in lines if "Email" in line)

    assert header.split() == [
        "ID",
        "Email",
        "Name",
        "Roles",
        "Active",
        "Password",
        "reset",
        "Account",
    ]
    assert "Password hash version" not in output
    assert any("usr_2" in line and "bob@acme.io" in line and "no" in line for line in lines)
    assert "admin, operator" in output
    # Columns line up: every row starts its second cell where the header does.
    email_column = header.index("Email")
    assert all(line[email_column:].startswith(("a@", "bob@")) for line in lines if "usr_" in line)


def test_nested_sections_and_dates_read_as_text(capsys) -> None:
    output = _plain(
        _spec("release.info"),
        {
            "installed": {"version": "0.9.13", "installed_at": "2026-11-09T10:22:33.123456Z"},
            "base_url": "https://fs.example.com",
            "checks": [{"name": "images", "ok": True, "detail": {"pulled": 7}}],
        },
        capsys,
    )

    assert "Installed" in output
    assert "2026-11-09 10:22:33 UTC" in output
    assert "Base URL:" in output
    assert "{" not in output and "}" not in output


@pytest.mark.parametrize(
    ("key", "label"),
    [("files_created", "Files created"), ("base_url", "Base URL"), ("api_key_id", "API key ID")],
)
def test_field_names_become_labels(key: str, label: str) -> None:
    assert humanize_key(key) == label


def test_scalars_read_as_words() -> None:
    assert format_scalar(True) == "yes"
    assert format_scalar(None) == "none"
    assert format_scalar("2026-01-02T03:04:05+00:00") == "2026-01-02 03:04:05 UTC"


def test_warnings_from_the_result_and_the_executor_are_both_shown(capsys) -> None:
    emit_plain(
        result_blocks(
            _spec("maintenance.enable"),
            {"status": "enabled", "warnings": ["Backups are older than 7 days."]},
            (CLIWarning(code="w", message="Run flow-steward backup create first."),),
        )
    )
    output = capsys.readouterr().out

    assert "! Backups are older than 7 days." in output
    assert "! Run flow-steward backup create first." in output


def test_impact_plan_is_readable_sections(capsys) -> None:
    emit_plain(
        plan_blocks(
            _spec("maintenance.enable"),
            {
                "extension_id": "acme.crm",
                "remove": [{"kind": "bundle", "id": "b1"}],
                "retained": [],
            },
        )
    )
    output = capsys.readouterr().out

    assert "Impact plan: maintenance enable" in output
    assert "Remove (1)" in output
    assert "• Kind: bundle" in output
    assert "{" not in output


_SHARED_LAYOUT_SPECS = [
    spec
    for spec in PUBLIC_COMMAND_SPECS
    if spec.command_id not in _OWN_LAYOUT_COMMANDS
    and spec.command_id not in _CONFIG_TEXT_RENDERERS
    and spec.command_id != _REPORTS_DIAGNOSTICS
]
_AWKWARD_PAYLOAD = {
    "status": "completed",
    "items": [{"id": 1, "name": "a", "meta": {"x": 1}, "tags": ["p", "q"]}],
    "details": {"a": {"b": [1, 2], "c": None}},
    "mixed": [1, {"k": "v"}],
    "text": "line one\nline two",
    "next_steps": ["flow-steward status"],
}


@pytest.mark.parametrize("spec", _SHARED_LAYOUT_SPECS, ids=lambda spec: spec.command_id)
@pytest.mark.parametrize("renderer_class", [PlainRenderer, RichRenderer])
def test_no_command_prints_json_without_the_json_flag(
    spec: CommandSpec, renderer_class: type[PlainRenderer], capsys
) -> None:
    renderer = (
        renderer_class(color_enabled=False, animation_enabled=False)
        if renderer_class is RichRenderer
        else renderer_class()
    )

    renderer.render_result(spec, _result(dict(_AWKWARD_PAYLOAD)))
    output = capsys.readouterr().out

    assert "{" not in output and "}" not in output
    assert '"' not in output
    assert "line two" in output


def test_an_empty_listing_says_so_instead_of_paging_fields(capsys) -> None:
    output = _plain(_spec("jobs.list"), {"jobs": [], "total": 0, "page": 1, "per_page": 25}, capsys)

    assert "No jobs found." in output
    assert "Per page" not in output and "Total" not in output


def test_page_based_results_offer_the_next_page_through_the_commands_own_options(
    capsys,
) -> None:
    command_names.set_invocation_prefix("./flow-steward")

    output = _plain(
        _spec("jobs.list"),
        {
            "jobs": [{"id": "j1", "status": "done"}, {"id": "j2", "status": "failed"}],
            "total": 5,
            "page": 1,
            "per_page": 2,
            "offset": 0,
            "total_pages": 3,
        },
        capsys,
    )

    assert "Showing 2 of 5 (page 1 of 3)." in output
    assert "Next page: ./flow-steward jobs list --offset 2 --limit 2" in output
    assert "Total pages" not in output
