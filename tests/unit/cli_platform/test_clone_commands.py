import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner

from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import ExitCode
from core.application.management.command_specs.registry import get_command_spec
from core.entrypoints.cli.clone_composition import build_installed_clone_service
from core.entrypoints.cli.lifecycle_click_app import (
    LIFECYCLE_COMMAND_DOMAINS,
    click_app,
)
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_bindings.common import PreparedCommand
from core.entrypoints.cli.platform.service_registry import (
    PLAN_BUILDERS,
    SERVICE_FACTORIES,
    ServiceRegistry,
)
from core.entrypoints.cli.platform_runtime import _canonical_argv, _parser

_ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_source_installation_clone_composition_returns_typed_prerequisite(
    profile: str,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class ReleaseService:
        def require_official_installation(self):
            calls.append(profile)
            raise LifecycleError.stable_failure(
                code="clone_official_release_required",
                cause="Portable Clone requires an official release installation.",
                remediation=(
                    "Run release check, enable maintenance, and complete release upgrade."
                ),
                step="clone_prerequisite",
            )

    runtime = SimpleNamespace(release_service=ReleaseService())

    with pytest.raises(LifecycleError) as raised:
        build_installed_clone_service(
            runtime,
            repo_root=tmp_path,
            environment_loader=dict,
            platform_machine=lambda: "x86_64",
        )

    assert raised.value.code == "clone_official_release_required"
    assert "release check" in raised.value.remediation
    assert "release upgrade" in raised.value.remediation
    assert calls == [profile]


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize(
    "command",
    (
        ("clone", "create", "--passphrase-stdin", "--confirm", "--json"),
        ("clone", "restore", "/private/source.fsclone", "--passphrase-stdin", "--json"),
    ),
)
def test_source_installation_clone_cli_returns_typed_prerequisite(
    profile: str,
    command: tuple[str, ...],
) -> None:
    calls: list[str] = []

    class Runtime:
        @property
        def clone_service(self):
            calls.append(profile)
            raise LifecycleError.stable_failure(
                code="clone_official_release_required",
                cause="Portable Clone requires an official release installation.",
                remediation=(
                    "Run release check, enable maintenance, and complete release upgrade."
                ),
                step="clone_prerequisite",
            )

    runtime = Runtime()
    app = build_click_app(ServiceRegistry(cast(Any, runtime)), SimpleNamespace())

    result = CliRunner().invoke(app, command)

    assert result.exit_code == ExitCode.STABLE_FAILURE
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "clone_official_release_required"
    assert payload["error"]["code"] != "internal_error"
    assert "release check" in payload["error"]["hint"]
    assert "release upgrade" in payload["error"]["hint"]
    assert calls == [profile]


def test_clone_help_uses_executable_confirmation_examples_and_value_name() -> None:
    runner = CliRunner()

    create = runner.invoke(click_app, ["clone", "create", "--help"])
    restore = runner.invoke(click_app, ["clone", "restore", "--help"])

    assert create.exit_code == 0
    assert "--passphrase-stdin --confirm" in create.output
    assert restore.exit_code == 0
    assert "--confirm CONFIRMATION_VALUE" in restore.output
    assert "--confirm TARGET" not in restore.output
    assert "flow-steward clone restore x.fsclone --passphrase-stdin" in restore.output


def test_clone_unattended_docs_accept_only_review_exit_ten() -> None:
    for relative in (
        "docs/operations/checkout-reference.md",
        "docs/GUIDE.md",
        "docs/operations/installation-clone.md",
    ):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert "--passphrase-stdin --non-interactive || review_status=$?" in text
        assert 'if [ "$review_status" -eq 0 ]; then' in text
        assert "exit 70" in text
        assert 'if [ "$review_status" -ne 10 ]; then exit "$review_status"; fi' in text
        assert "--non-interactive --confirm '<printed-target-or-plan-value>'" in text


def test_clone_unattended_review_shell_guard_executes_only_after_exit_ten(
    tmp_path: Path,
) -> None:
    script = """
review_status=0
first_pass || review_status=$?
if [ "$review_status" -eq 0 ]; then exit 70; fi
if [ "$review_status" -ne 10 ]; then exit "$review_status"; fi
second_pass
"""
    for first_status, expected_status, expected_second_pass in (
        (10, 0, True),
        (7, 7, False),
        (0, 70, False),
    ):
        marker = tmp_path / f"clone-second-pass-{first_status}"
        marker.unlink(missing_ok=True)
        command = (
            f"first_pass() {{ return {first_status}; }}; "
            f"second_pass() {{ : > {marker}; }}; "
            f"{script}"
        )
        outcome = subprocess.run(("sh", "-c", command), check=False)

        assert outcome.returncode == expected_status
        assert marker.exists() is expected_second_pass
        marker.unlink(missing_ok=True)


def test_clone_public_surface_contains_exactly_create_and_restore() -> None:
    create = get_command_spec(("clone", "create"))
    restore = get_command_spec(("clone", "restore"))

    assert create.command_id == "clone.create"
    assert restore.command_id == "clone.restore"
    assert restore.plan_builder_ref == "clone.restore.preview"
    assert restore.confirmation_policy_ref == (
        "match:target_installation_id,target_profile,public_host,plan_digest"
    )
    assert tuple(field.key for field in restore.fields) == (
        "archive_path",
        "passphrase_stdin",
        "operation_id",
    )
    assert restore.fields[0].positional is True


def test_compact_platform_dispatches_clone_create_and_restore() -> None:
    create = _parser().parse_args(
        ["clone-create", "--output", "/safe/backups", "--passphrase-stdin"]
    )
    restore = _parser().parse_args(
        ["clone-restore", "--archive", "/safe/source.fsclone", "--passphrase-stdin"]
    )

    assert _canonical_argv(create) == [
        "clone",
        "create",
        "--output-directory",
        "/safe/backups",
        "--passphrase-stdin",
    ]
    assert _canonical_argv(restore) == [
        "clone",
        "restore",
        "/safe/source.fsclone",
        "--passphrase-stdin",
    ]


def test_shipped_lifecycle_entrypoint_exposes_clone_commands() -> None:
    result = CliRunner().invoke(click_app, ["clone", "--help"])

    assert "clone" in LIFECYCLE_COMMAND_DOMAINS
    assert result.exit_code == 0, result.output
    assert "create" in result.output
    assert "restore" in result.output


def test_compact_clone_create_forwards_required_review_confirmation() -> None:
    create = _parser().parse_args(
        ["clone-create", "--output", "/safe/backups", "--passphrase-stdin", "--confirm"]
    )

    assert _canonical_argv(create)[-1] == "--confirm"


def test_clone_restore_confirmation_binds_full_review_tuple_and_cleans_declined_staging() -> None:
    cleaned: list[str] = []

    async def prepare(_runtime: object, _value: object) -> PreparedCommand:
        from core.application.management.command_specs.clone import CloneRestoreExecutionInput

        execution = CloneRestoreExecutionInput.model_construct(
            prepared=object(),
            target_installation_id="target",
            target_profile="compact",
            public_host="clone.example.test",
            plan_digest="a" * 64,
        )
        return PreparedCommand(
            execution_input=execution,
            review=execution.model_dump(exclude={"prepared"}),
            cleanup=lambda: cleaned.append("cleaned"),
        )

    original = PLAN_BUILDERS["clone.restore.preview"]
    original_service = SERVICE_FACTORIES["clone.restore"]
    PLAN_BUILDERS["clone.restore.preview"] = prepare
    SERVICE_FACTORIES["clone.restore"] = lambda *_args, **_kwargs: None
    try:
        app = build_click_app(ServiceRegistry(cast(Any, SimpleNamespace())), SimpleNamespace())
        result = CliRunner().invoke(
            app,
            [
                "clone",
                "restore",
                "/private/source.fsclone",
                "--passphrase-stdin",
                "--confirm",
                "target",
                "--non-interactive",
            ],
            input="secret\n",
        )
    finally:
        PLAN_BUILDERS["clone.restore.preview"] = original
        SERVICE_FACTORIES["clone.restore"] = original_service

    assert result.exit_code != 0
    assert cleaned == ["cleaned"], (result.output, result.exception)


def test_clone_restore_noninteractive_preview_prints_complete_review_and_confirmation() -> None:
    cleaned: list[str] = []
    restored: list[str] = []

    async def prepare(_runtime: object, _value: object) -> PreparedCommand:
        from core.application.management.command_specs.clone import CloneRestoreExecutionInput

        execution = CloneRestoreExecutionInput.model_construct(
            prepared=object(),
            target_installation_id="target-installation",
            target_profile="compact",
            public_host="clone.example.test",
            plan_digest="b" * 64,
        )
        return PreparedCommand(
            execution_input=execution,
            review=execution.model_dump(exclude={"prepared"}),
            cleanup=lambda: cleaned.append("cleaned"),
        )

    original = PLAN_BUILDERS["clone.restore.preview"]
    original_service = SERVICE_FACTORIES["clone.restore"]
    PLAN_BUILDERS["clone.restore.preview"] = prepare

    def restore(_runtime: object, _value: object, **_kwargs: object):
        from core.application.clone.operation import CloneRestoreResult

        restored.append("restored")
        return CloneRestoreResult(
            operation_id="clone-restore-test",
            target_installation_id="target-installation",
            release_version="1.2.3",
            plan_digest="b" * 64,
            log_path="/tmp/clone-restore-test.log",
        )

    SERVICE_FACTORIES["clone.restore"] = restore
    try:
        app = build_click_app(ServiceRegistry(cast(Any, SimpleNamespace())), SimpleNamespace())
        first = CliRunner().invoke(
            app,
            [
                "clone",
                "restore",
                "/private/source.fsclone",
                "--passphrase-stdin",
                "--non-interactive",
            ],
            input="secret\n",
        )
        expected = "target-installation|compact|clone.example.test|" + "b" * 64
        json_preview = CliRunner().invoke(
            app,
            [
                "clone",
                "restore",
                "/private/source.fsclone",
                "--passphrase-stdin",
                "--json",
                "--non-interactive",
            ],
            input="secret\n",
        )
        second = CliRunner().invoke(
            app,
            [
                "clone",
                "restore",
                "/private/source.fsclone",
                "--passphrase-stdin",
                "--non-interactive",
                "--confirm",
                expected,
            ],
            input="secret\n",
        )
    finally:
        PLAN_BUILDERS["clone.restore.preview"] = original
        SERVICE_FACTORIES["clone.restore"] = original_service

    assert first.exit_code == 10
    assert "target_installation_id" in first.output
    assert "target-installation" in first.output
    assert "target_profile" in first.output
    assert "compact" in first.output
    assert "public_host" in first.output
    assert "clone.example.test" in first.output
    assert "plan_digest" in first.output
    assert "b" * 64 in first.output
    assert f"Confirmation value: {expected}" in first.output
    assert json_preview.exit_code == 10
    json_payload = json.loads(json_preview.output)
    assert json_payload["error"]["details"] == {
        "review": {
            "target_installation_id": "target-installation",
            "target_profile": "compact",
            "public_host": "clone.example.test",
            "plan_digest": "b" * 64,
        },
        "confirmation_value": expected,
    }
    assert second.exit_code == 0, second.output
    assert restored == ["restored"]
    assert cleaned == ["cleaned", "cleaned"]
