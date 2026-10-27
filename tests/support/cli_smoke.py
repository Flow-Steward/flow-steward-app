"""Registry-owned policies for installed CLI release smoke coverage."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

SmokePolicy = Literal[
    "read_live",
    "disposable_change",
    "disposable_destructive",
    "protocol_fake",
]


class InstalledCliProject(Protocol):
    def cli(self, *arguments: str, input_text: str | None = None) -> Any: ...


@dataclass(frozen=True)
class SmokeCase:
    """One installed-launcher smoke contract."""

    command_id: str
    arguments: tuple[str, ...]
    policy: SmokePolicy
    expected_exit_code: int
    fixture_owner: str
    mutation_evidence: str
    cleanup_evidence: str
    requires_running_system: bool = True


_READ_LIVE_IDS = frozenset(
    {
        "status",
        "doctor",
        "stack.status",
        "stack.logs",
        "environment.show",
        "maintenance.status",
        "release.info",
        "release.check",
        "backup.list",
        "backup.verify",
        "config.show",
        "config.validate",
        "users.list",
        "extensions.list",
        "extensions.show",
        "extensions.health",
        "extensions.validate",
        "extensions.test",
        "projects.list",
        "projects.show",
        "projects.notification-routes",
        "projects.notification-deliveries",
        "workflows.list",
        "workflows.entrypoints",
        "workflows.validate",
        "workflows.contract",
        "workflows.suggestions",
        "workflows.hooks-list",
        "workflows.hook-get",
        "workflows.hook-preview",
        "workflows.where-used",
        "workflows.authoring-schema",
        "workflows.draft-validate",
        "workflows.definition",
        "workflows.export",
        "workflows.stats",
        "jobs.list",
        "jobs.show",
        "jobs.status",
        "jobs.logs",
        "jobs.messages",
        "jobs.errors",
        "reports.get",
        "database.status",
        "database.query-diagnostics",
        "quality.check-ci-excludes",
        "quality.check-naming",
        "localization.validate-keys",
    }
)

_DISPOSABLE_CHANGE_IDS = frozenset(
    {
        "stack.start",
        "stack.stop",
        "stack.restart",
        "stack.rebuild",
        "environment.switch",
        "maintenance.enable",
        "maintenance.disable",
        "release.upgrade",
        "backup.create",
        "config.set",
        "config.apply",
        "users.create",
        "users.set-password",
        "users.force-reset",
        "users.unlock",
        "users.set-active",
        "users.set-roles",
        "users.issue-bootstrap-token",
        "extensions.install",
        "extensions.enable",
        "extensions.disable",
        "extensions.create",
        "extensions.bundle",
        "extensions.lock-deps",
        "jobs.approve",
        "jobs.reject",
        "jobs.ack",
        "jobs.unack",
        "jobs.cancel",
        "jobs.restart",
        "reports.generate",
        "database.upgrade",
        "database.bootstrap-seed",
        "database.generate-migration",
        "database.tune-env",
        "database.restore-parity-capture",
        "database.restore-parity-verify",
        "testing.smoke-test",
        "testing.nightfire-regression",
        "quality.verify",
        "quality.lint",
        "quality.release-gate",
        "localization.bootstrap-catalog",
        "localization.rebuild-pot",
        "localization.init-locale",
        "localization.update",
        "localization.compile",
    }
)

_DISPOSABLE_DESTRUCTIVE_IDS = frozenset(
    {
        "extensions.uninstall",
    }
)

_PROTOCOL_FAKE_IDS = frozenset(
    {
        "projects.dispatch-notification",
        "workflows.convert-group",
        "workflows.hook-bind",
        "workflows.hook-unbind",
        "workflows.hook-create-and-bind",
        "workflows.draft-save",
        "workflows.apply-changes",
        "workflows.import",
        "workflows.run",
        "jobs.health-snapshot",
        "testing.fuzz-api",
        "testing.fuzz-mcp",
        "testing.fuzz-ui",
        "testing.cutover-preflight",
        "testing.cutover-smoke",
        "quality.sonar-export-issues",
        "quality.sonar-ensure-rules",
        "quality.sonar-generate-backlog",
    }
)

_POLICY_IDS: tuple[tuple[SmokePolicy, frozenset[str]], ...] = (
    ("read_live", _READ_LIVE_IDS),
    ("disposable_change", _DISPOSABLE_CHANGE_IDS),
    ("disposable_destructive", _DISPOSABLE_DESTRUCTIVE_IDS),
    ("protocol_fake", _PROTOCOL_FAKE_IDS),
)
_POLICY_BY_COMMAND = {
    command_id: policy for policy, command_ids in _POLICY_IDS for command_id in command_ids
}
_EXTENSION_LIFECYCLE_IDS = frozenset(
    {
        "extensions.create",
        "extensions.validate",
        "extensions.health",
        "extensions.test",
        "extensions.lock-deps",
        "extensions.bundle",
        "extensions.install",
        "extensions.show",
        "extensions.enable",
        "extensions.disable",
        "extensions.uninstall",
    }
)


def _example_arguments(command_id: str, example: str) -> tuple[str, ...]:
    arguments = tuple(shlex.split(example))
    if not arguments or arguments[0] != "flow-steward":
        raise AssertionError(f"{command_id} has no installed-launcher example")
    return arguments[1:]


def _build_smoke_policies() -> Mapping[str, SmokeCase]:
    cases: dict[str, SmokeCase] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        policy = _POLICY_BY_COMMAND.get(spec.command_id)
        if policy is None:
            continue
        post_start_extension = spec.command_id in _EXTENSION_LIFECYCLE_IDS
        cases[spec.command_id] = SmokeCase(
            command_id=spec.command_id,
            arguments=_example_arguments(spec.command_id, spec.examples[0]),
            policy=policy,
            expected_exit_code=0,
            fixture_owner=(
                "post_start_extension"
                if post_start_extension
                else "local_protocol_fake"
                if policy == "protocol_fake"
                else "disposable_installation"
            ),
            mutation_evidence=(
                "before_after_owned_resource_fingerprint"
                if policy != "read_live"
                else "read_result_and_unchanged_fingerprint"
            ),
            cleanup_evidence=(
                "extension_archive_and_directory_absent"
                if post_start_extension
                else "protocol_fake_stopped_and_paths_absent"
                if policy == "protocol_fake"
                else "task_manifest_resources_absent"
            ),
        )
    return MappingProxyType(cases)


def safe_smoke_arguments(case: SmokeCase) -> tuple[str, ...]:
    """Reach a live binding without confirming the registry example's mutation."""
    arguments = tuple(argument for argument in case.arguments if argument != "--confirm")
    return (*arguments, "--non-interactive", "--json")


def binding_reached(*, returncode: int, stdout: str, stderr: str) -> bool:
    """Distinguish a bound command result from Click parser/dispatch failures."""
    del returncode
    combined = f"{stdout}\n{stderr}"
    return "No such command" not in combined and "Usage: flow-steward" not in combined


def run_smoke_registry(
    project: InstalledCliProject,
    *,
    environment: str,
) -> list[dict[str, object]]:
    """Exercise every public command binding with mutation-safe arguments."""
    evidence: list[dict[str, object]] = []
    for command_id, case in SMOKE_POLICIES.items():
        arguments = safe_smoke_arguments(case)
        result = project.cli(*arguments, input_text="")
        if not binding_reached(
            returncode=int(result.returncode),
            stdout=str(result.stdout),
            stderr=str(result.stderr),
        ):
            raise AssertionError(
                f"{environment} command did not reach its binding: {command_id}: "
                f"exit={result.returncode}: {result.stderr}"
            )
        evidence.append(
            {
                "environment": environment,
                "command_id": command_id,
                "policy": case.policy,
                "exit_code": int(result.returncode),
            }
        )
    return evidence


SMOKE_POLICIES: Mapping[str, SmokeCase] = _build_smoke_policies()


__all__ = [
    "SMOKE_POLICIES",
    "SmokeCase",
    "SmokePolicy",
    "binding_reached",
    "run_smoke_registry",
    "safe_smoke_arguments",
]
