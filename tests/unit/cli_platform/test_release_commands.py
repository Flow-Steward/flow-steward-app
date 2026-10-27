from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner

from core.application.installer.contracts import InstallConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.management.command_specs.contracts import (
    InteractivePolicy,
    OperationProgressEvent,
    OperationStepStatus,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.application.release.catalog import PublishedRelease, ReleaseVersion
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import OfficialInstallationOrigin
from core.application.release.service import (
    PreparedReleaseUpgrade,
    ReleaseCheckRequest,
    ReleaseInfoRequest,
    ReleaseResult,
    ReleaseService,
    ReleaseUpgradeRequest,
)
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_bindings.common import PreparedCommand
from core.entrypoints.cli.platform.service_registry import ServiceRegistry
from core.entrypoints.cli.runtime import CLIRuntime
from core.infrastructure.release.installed_origin import InstalledReleaseOriginReader


def _release_identity(version: str) -> ReleaseIdentity:
    digest = "sha256:" + "a" * 64
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha="a" * 40,
        registry="ghcr.io/flow-steward",
        profile="scalable",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="a" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )


@dataclass
class _ReleaseService:
    calls: list[tuple[str, object]] = field(default_factory=list)
    catalog_unconfigured: bool = False
    migration_required: bool = False

    def info(self, request: ReleaseInfoRequest) -> ReleaseResult:
        self.calls.append(("info", request))
        return ReleaseResult(
            status="installed",
            active_environment="development",
            current_version="1.0.0",
            current_release=_release_identity("1.0.0"),
        )

    def check(self, request: ReleaseCheckRequest) -> ReleaseResult:
        self.calls.append(("check", request))
        return ReleaseResult(
            status="upgrade_available",
            active_environment="development",
            current_version="1.0.0",
            target_version="2.0.0",
            target_release=_release_identity("2.0.0"),
        )

    def prepare_upgrade(self, request: ReleaseUpgradeRequest) -> PreparedReleaseUpgrade:
        self.calls.append(("plan", request))
        if self.catalog_unconfigured:
            raise LifecycleError.stable_failure(
                code="catalog_unconfigured",
                cause="Published GitHub Release discovery is unavailable.",
                remediation="Configure FS_RELEASE_REPOSITORY and retry.",
                step="resolve_target",
            )
        return PreparedReleaseUpgrade(
            installed_release=_release_identity("1.0.0"),
            target_release=_release_identity(request.target_version or "2.0.0"),
            release_source="https://example.invalid/release",
            deployment_profile="scalable",
            platform="linux/amd64",
            dry_run=request.dry_run,
            timeout_seconds=request.timeout_seconds,
        )

    def review_upgrade(self, prepared: PreparedReleaseUpgrade) -> dict[str, object]:
        return {
            "target_version": prepared.target_release.version,
            "active_environment": "development (unchanged)",
            "backup_gate": "verified before the first mutation",
            "database_recovery": "never automatic",
            "next_steps": ["flow-steward maintenance disable --confirm"],
        }

    def upgrade(
        self, request: ReleaseUpgradeRequest | PreparedReleaseUpgrade, **kwargs: object
    ) -> ReleaseResult:
        self.calls.append(("upgrade", request))
        target_version = (
            request.target_release.version
            if isinstance(request, PreparedReleaseUpgrade)
            else request.target_version or "2.0.0"
        )
        progress = kwargs.get("progress_sink")
        if callable(progress):
            progress(
                OperationProgressEvent(
                    step_id="verify_health",
                    status=OperationStepStatus.SUCCEEDED,
                    label="Verify release readiness",
                )
            )
        if self.migration_required:
            return ReleaseResult(
                status="migration_required",
                deployment_profile="scalable",
                platform="linux/amd64",
                release_source="https://example.invalid/release",
                remediation="Create a backup, run database upgrade, then resume.",
            )
        return ReleaseResult(
            status="planned" if request.dry_run else "upgraded",
            active_environment="development",
            current_version="1.0.0",
            target_version=target_version,
            target_release=_release_identity(target_version),
            dry_run=request.dry_run,
            operation_id=None if request.dry_run else "release-operation",
            log_path=None if request.dry_run else "/tmp/release-operation.log",
            next_steps=("flow-steward maintenance disable --confirm",),
        )


@dataclass
class _Runtime:
    release_service: _ReleaseService = field(default_factory=_ReleaseService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(cast(Any, runtime)), SimpleNamespace()), runtime


def _real_service_app(*, outcome: str) -> Any:
    installed = _release_identity("1.0.0")
    target = _release_identity("2.0.0")
    published = PublishedRelease(
        version=ReleaseVersion.parse(target.version),
        tag=target.tag,
        commit_sha=target.commit_sha,
        published_at="2026-09-25T10:00:00Z",
        source="https://example.invalid/releases/v2.0.0",
        repository="example/flow-steward",
    )

    class Discovery:
        def latest(self):
            return published

        def for_version(self, _version):
            return published

    class State:
        def load_installed_release(self):
            return installed

        def load_pending_release(self):
            return None

    class Resolver:
        def probe(self, _release, *, profile, platform):
            assert (profile, platform) == ("scalable", "linux/amd64")
            return target

        resolve = probe

    class Maintenance:
        def status(self):
            return MaintenanceStatusResult(status="enabled")

    class Activator:
        def activate(self, release, *, timeout_seconds, **_kwargs):
            assert release == target
            assert timeout_seconds == 60
            if outcome == "migration_required":
                return ReleaseResult(
                    status="migration_required",
                    remediation="Create a backup, run database upgrade, then resume.",
                )
            return ReleaseResult(
                status="upgraded",
                current_release=target,
                current_version=target.version,
            )

    class Origin:
        def load(self):
            return OfficialInstallationOrigin(release=installed)

    service = ReleaseService(
        discovery=Discovery(),
        state=State(),
        resolver=Resolver(),
        maintenance=Maintenance(),
        activator=Activator(),
        profile="scalable",
        platform="linux/amd64",
        origin=Origin(),
    )
    runtime = SimpleNamespace(release_service=service)
    return build_click_app(ServiceRegistry(cast(Any, runtime)), SimpleNamespace())


def test_release_specs_are_exact_and_closed() -> None:
    specs = {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "release"}

    assert tuple(specs) == ("release.info", "release.check", "release.upgrade")
    assert specs["release.info"].operation_type is OperationType.READ
    assert specs["release.check"].operation_type is OperationType.READ
    assert specs["release.info"].compact_support.value == "platform"
    assert specs["release.check"].compact_support.value == "platform"
    assert "source" in specs["release.info"].description.lower()
    assert "source" in specs["release.check"].description.lower()
    upgrade = specs["release.upgrade"]
    assert upgrade.compact_support.value == "platform"
    assert upgrade.operation_type is OperationType.CHANGE
    assert upgrade.interactive_policy is InteractivePolicy.ALWAYS_REVIEW
    assert upgrade.lifecycle_interrupts is True
    assert upgrade.plan_builder_ref == "release.upgrade.preview"
    assert tuple(field.key for field in upgrade.fields) == (
        "target_version",
        "dry_run",
        "timeout_seconds",
    )
    assert {field.key: field.option_name for field in upgrade.fields} == {
        "target_version": "--target-version",
        "dry_run": "--dry-run",
        "timeout_seconds": "--timeout-seconds",
    }
    assert upgrade.fields[2].default == 60
    assert upgrade.fields[2].advanced is True


def test_release_help_has_only_the_closed_public_inputs() -> None:
    app, _runtime = _app()
    runner = CliRunner()

    group = runner.invoke(app, ["release", "--help"])
    upgrade = runner.invoke(app, ["release", "upgrade", "--help"])

    assert group.exit_code == 0, group.output
    assert all(leaf in group.output for leaf in ("info", "check", "upgrade"))
    assert upgrade.exit_code == 0, upgrade.output
    assert "--target-version" in upgrade.output
    assert "--dry-run" in upgrade.output
    assert "--timeout-seconds" in upgrade.output
    assert "--base-url" not in upgrade.output
    assert "--backup-output" not in upgrade.output
    assert "--author" not in upgrade.output
    assert "--operation-id" not in upgrade.output


def test_release_info_and_check_support_plain_and_json() -> None:
    app, runtime = _app()
    runner = CliRunner()

    plain = runner.invoke(app, ["release", "info", "--plain"])
    machine = runner.invoke(app, ["release", "check", "--json"])

    assert plain.exit_code == 0, plain.output
    assert "development" in plain.output
    payload = json.loads(machine.output)
    assert payload["command"] == "release.check"
    assert payload["result"]["status"] == "upgrade_available"
    assert [call[0] for call in runtime.release_service.calls] == ["info", "check"]


def test_source_release_check_json_keeps_source_and_official_target_separate() -> None:
    app, runtime = _app()
    runtime.release_service.check = lambda request: ReleaseResult(
        status="transition_available",
        installed_origin="source",
        source_commit_sha="c" * 40,
        deployment_profile="scalable",
        platform="linux/amd64",
        target_version="2.0.0",
        target_release=_release_identity("2.0.0"),
        detail="Source and official release semantic ordering is unknown.",
        remediation="Run release upgrade with explicit confirmation.",
    )

    result = CliRunner().invoke(app, ["release", "check", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["status"] == "transition_available"
    assert payload["installed_origin"] == "source"
    assert payload["source_commit_sha"] == "c" * 40
    assert payload["current_version"] == ""
    assert payload["current_release"] is None
    assert payload["target_version"] == "2.0.0"


def test_release_json_reports_unsupported_host_architecture_actionably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli import lifecycle_runtime

    monkeypatch.setattr(lifecycle_runtime.host_platform, "machine", lambda: "riscv64")
    runtime = lifecycle_runtime.LifecycleCLIRuntime()
    app = build_click_app(ServiceRegistry(runtime, allowed_domains=frozenset({"release"})), runtime)

    result = CliRunner().invoke(app, ["release", "info", "--json"])

    assert result.exit_code == 8, result.output
    error = json.loads(result.output)["error"]
    assert error["code"] == "unsupported_release_architecture"
    assert "Linux amd64 or arm64" in error["hint"]


def test_upgrade_requires_review_and_forwards_exact_typed_request() -> None:
    app, runtime = _app()
    runner = CliRunner()

    refused = runner.invoke(
        app,
        ["release", "upgrade", "--target-version", "2.0.0", "--non-interactive", "--plain"],
    )
    result = runner.invoke(
        app,
        [
            "release",
            "upgrade",
            "--target-version",
            "2.0.0",
            "--timeout-seconds",
            "90",
            "--non-interactive",
            "--confirm",
            "--json",
        ],
    )

    assert refused.exit_code == 10
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["operation_id"] == "release-operation"
    assert payload["log_path"] == "/tmp/release-operation.log"
    assert payload["result"]["next_steps"] == ["flow-steward maintenance disable --confirm"]
    request = runtime.release_service.calls[-1][1]
    assert isinstance(request, PreparedReleaseUpgrade)
    assert request.target_release.version == "2.0.0"
    assert request.timeout_seconds == 90
    assert request.dry_run is False


def test_refused_release_review_never_reaches_upgrade_execution() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["release", "upgrade", "--target-version", "2.0.0", "--non-interactive", "--plain"],
    )

    assert result.exit_code == 10
    assert [call[0] for call in runtime.release_service.calls] == ["plan"]


def test_dry_run_is_reviewed_but_returns_no_operation_or_backup() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        [
            "release",
            "upgrade",
            "--dry-run",
            "--non-interactive",
            "--confirm",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["status"] == "planned"
    assert payload["result"]["dry_run"] is True
    assert "backup_path" not in payload["result"]
    assert payload["operation_id"] is None
    assert [call[0] for call in runtime.release_service.calls] == ["plan", "upgrade"]


def test_dry_run_preserves_catalog_precondition_from_review_plan() -> None:
    app, runtime = _app()
    runtime.release_service.catalog_unconfigured = True
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            app,
            ["release", "upgrade", "--dry-run", "--non-interactive", "--confirm", "--json"],
        )
        payload = json.loads(result.output)
        log_text = Path(payload["log_path"]).read_text(encoding="utf-8")

    assert result.exit_code == 8, result.output
    assert payload["error"]["code"] == "catalog_unconfigured"
    assert payload["error"]["message"] == "Published GitHub Release discovery is unavailable."
    assert payload["error"]["hint"] == "Configure FS_RELEASE_REPOSITORY and retry."
    assert payload["operation_id"]
    assert "PLAN_FAILED catalog_unconfigured" in log_text
    assert [call[0] for call in runtime.release_service.calls] == ["plan"]


def test_migration_required_is_a_non_success_public_outcome() -> None:
    app, runtime = _app()
    runtime.release_service.migration_required = True

    result = CliRunner().invoke(
        app,
        ["release", "upgrade", "--non-interactive", "--confirm", "--json"],
    )

    assert result.exit_code == 8, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "release_migration_required"
    assert "database migration" in payload["error"]["message"]
    assert "database upgrade" in payload["error"]["hint"]
    assert payload["error"]["details"]["deployment_profile"] == "scalable"
    assert payload["error"]["details"]["platform"] == "linux/amd64"


def test_real_release_service_migration_json_keeps_maintenance_enabled() -> None:
    result = CliRunner().invoke(
        _real_service_app(outcome="migration_required"),
        ["release", "upgrade", "--non-interactive", "--confirm", "--json"],
    )

    assert result.exit_code == 8, result.output
    details = json.loads(result.output)["error"]["details"]
    assert details["next_steps"] == [
        "flow-steward backup create",
        "flow-steward database upgrade --maintenance-confirmed --confirm",
        "flow-steward release upgrade --target-version 2.0.0 --confirm",
    ]
    assert "maintenance disable" not in json.dumps(details)


def test_real_release_service_success_json_reports_target_as_current() -> None:
    result = CliRunner().invoke(
        _real_service_app(outcome="upgraded"),
        ["release", "upgrade", "--non-interactive", "--confirm", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["current_version"] == "2.0.0"
    assert payload["current_release"]["version"] == "2.0.0"


def test_release_preview_preserves_the_exact_typed_execution_input() -> None:
    import asyncio

    runtime = _Runtime()
    registry = ServiceRegistry(cast(Any, runtime))
    request = ReleaseUpgradeRequest(target_version="2.0.0", dry_run=True)

    prepared = asyncio.run(registry.build_plan("release.upgrade.preview", request))

    assert isinstance(prepared, PreparedCommand)
    assert isinstance(prepared.execution_input, PreparedReleaseUpgrade)
    assert prepared.execution_input.target_release.version == "2.0.0"
    assert prepared.execution_input.dry_run is True
    assert prepared.review["active_environment"] == "development (unchanged)"
    assert prepared.review["backup_gate"] == "verified before the first mutation"
    assert prepared.review["next_steps"] == ["flow-steward maintenance disable --confirm"]


def test_cli_runtime_composes_release_service_from_lifecycle_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.entrypoints.cli.lifecycle_runtime._installed_runtime_environment",
        lambda: {"FS_OBSERVABILITY_DSN": "postgresql://lifecycle-test"},
    )
    service = CLIRuntime().release_service

    assert isinstance(service, ReleaseService)
    assert isinstance(service.origin, InstalledReleaseOriginReader)


def test_cli_runtime_uses_durable_release_repository_for_discovery_and_handoff() -> None:
    active = InstallConfig(environment="production", deployment_profile="scalable")

    class Store:
        def read_durable_environment(self, config):
            assert config == active
            return {
                "FS_RELEASE_REPOSITORY": "example/durable-releases",
                "FS_COMPOSE_PROJECT_NAME": "flow-steward-scalable",
            }

    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda required=False: active,
        store=Store(),
    )
    runtime.__dict__["maintenance_service"] = SimpleNamespace()

    service = runtime.release_service

    assert service.selector.release_repository == "example/durable-releases"
    assert service.activator.handoff.source_repository == "example/durable-releases"


def test_release_progress_uses_the_common_lifecycle_channel() -> None:
    app, _runtime = _app()

    result = CliRunner().invoke(
        app,
        ["release", "upgrade", "--non-interactive", "--confirm", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "Verify release readiness" in result.output
    assert "flow-steward maintenance disable --confirm" in result.output


def test_legacy_release_argparse_module_is_deleted() -> None:
    assert importlib.util.find_spec("core.entrypoints.cli.release") is None
