"""Installed-launcher smoke policy and opt-in disposable execution contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from tests.support.cli_smoke import (
    SMOKE_POLICIES,
    binding_reached,
    run_smoke_registry,
    safe_smoke_arguments,
)
from tests.support.disposable_installer import (
    DisposableInstallerProject,
    _installed_command_id,
)

ROOT = Path(__file__).resolve().parents[2]


def test_every_public_command_has_a_smoke_policy() -> None:
    spec_ids = {spec.command_id for spec in PUBLIC_COMMAND_SPECS}
    assert set(SMOKE_POLICIES) == spec_ids, {
        "missing": sorted(spec_ids - set(SMOKE_POLICIES)),
        "stale": sorted(set(SMOKE_POLICIES) - spec_ids),
    }


def test_smoke_cases_have_executable_owned_evidence_contracts() -> None:
    for command_id, case in SMOKE_POLICIES.items():
        assert case.command_id == command_id
        assert case.arguments
        assert tuple(case.arguments[:2]) == next(
            spec.path for spec in PUBLIC_COMMAND_SPECS if spec.command_id == command_id
        )
        assert case.expected_exit_code == 0
        assert case.fixture_owner
        assert case.mutation_evidence
        assert case.cleanup_evidence
        assert case.requires_running_system is True


def test_extension_lifecycle_is_explicitly_post_start() -> None:
    ordered_lifecycle = (
        "extensions.create",
        "extensions.validate",
        "extensions.health",
        "extensions.test",
        "extensions.lock-deps",
        "extensions.bundle",
        "extensions.install",
    )
    assert all(
        SMOKE_POLICIES[command_id].fixture_owner == "post_start_extension"
        for command_id in ordered_lifecycle
    )
    assert all(
        SMOKE_POLICIES[command_id].requires_running_system for command_id in ordered_lifecycle
    )


def test_mutating_smoke_arguments_cannot_confirm_the_example_operation() -> None:
    case = SMOKE_POLICIES["maintenance.enable"]

    assert safe_smoke_arguments(case) == (
        "maintenance",
        "enable",
        "--non-interactive",
        "--json",
    )


def test_live_smoke_rejects_parser_failures_as_unreached_bindings() -> None:
    assert binding_reached(returncode=2, stdout="", stderr="Usage: flow-steward") is False
    assert (
        binding_reached(
            returncode=2,
            stdout="",
            stderr=(
                "Test commands require Development. Run flow-steward environment switch "
                "development, then retry."
            ),
        )
        is True
    )
    assert (
        binding_reached(
            returncode=1,
            stdout='{"schema":"flow-steward.cli.v1","error":{"code":"not_found"}}',
            stderr="",
        )
        is True
    )


def test_registry_runner_invokes_every_binding_through_the_installed_launcher() -> None:
    class _Result:
        returncode = 1
        stdout = '{"schema":"flow-steward.cli.v1","error":{"code":"expected_refusal"}}'
        stderr = ""

    class _InstalledProject:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def cli(self, *arguments: str, input_text: str | None = None) -> _Result:
            assert input_text == ""
            self.calls.append(arguments)
            return _Result()

    project = _InstalledProject()

    evidence = run_smoke_registry(project, environment="development")

    assert {row["command_id"] for row in evidence} == set(SMOKE_POLICIES)
    assert len(project.calls) == len(SMOKE_POLICIES)
    assert all("--confirm" not in call for call in project.calls)


def test_installed_cli_evidence_uses_registry_ids_for_one_and_two_part_commands() -> None:
    assert _installed_command_id(("status", "--non-interactive", "--json")) == "status"
    assert _installed_command_id(("doctor", "--non-interactive", "--json")) == "doctor"
    assert _installed_command_id(("stack", "status", "--json")) == "stack.status"
    assert (
        _installed_command_id(
            (
                "--non-interactive",
                "database",
                "bootstrap-seed",
                "--author",
                "release-acceptance",
                "--confirm",
                "--plain",
            )
        )
        == "database.bootstrap-seed"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_every_public_binding_runs_in_development_and_transitioned_production(
    tmp_path: Path,
) -> None:
    project = DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)
    try:
        installed = project.install(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
        )
        assert installed.returncode == 0, installed.combined_output

        development = run_smoke_registry(project, environment="development")
        assert {row["command_id"] for row in development} == set(SMOKE_POLICIES)

        switched = project.switch_environment("production")
        assert switched.returncode == 0, switched.combined_output
        production = run_smoke_registry(project, environment="production")
        assert {row["command_id"] for row in production} == set(SMOKE_POLICIES)
    finally:
        project.cleanup()

    assert all(not resources for resources in project.cleanup_proof().values())
