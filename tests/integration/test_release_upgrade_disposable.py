"""Disposable official release handoff and upgrade acceptance scenarios."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.application.installer import (
    BootstrapVerification,
    ImageCurrency,
    InstallationEvidence,
    InstallationKind,
    InstallConfig,
    InstallJournal,
    InstallRunResult,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.service import InstallerService
from core.application.readiness import CoreReadinessService
from core.application.release.images import required_product_roles
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.release_handoff import InstallerReleaseHandoff
from core.infrastructure.installer.state_store import InstallerStateStore
from tests.support.disposable_installer import (
    DisposableInstallerProject,
    PublicHTTPResult,
)

ROOT = Path(__file__).resolve().parents[2]


def _json_result(result) -> dict[str, object]:
    assert result.returncode == 0, result.combined_output
    payload = json.loads(result.stdout)
    assert payload["schema"] == "flow-steward.cli.v1"
    return dict(payload["result"])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _official_identity(
    *, profile: str, version: str, commit_sha: str, marker: str
) -> ReleaseIdentity:
    digest = "sha256:" + marker * 64
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=commit_sha,
        registry="registry.example/flow-steward",
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"registry.example/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=commit_sha,
                platform="linux/amd64",
            )
            for role in required_product_roles(profile)
        ),
    )


@pytest.mark.integration
@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("migration_required", (False, True))
def test_disposable_official_handoff_activates_exact_profile_or_stops_for_migration(
    tmp_path: Path,
    profile: str,
    migration_required: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    tracked = repo / "release.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-q", "-m", "baseline")
    baseline_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1.0.9")
    tracked.write_text("candidate\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1.0.10")
    _git(repo, "checkout", "-q", "--detach", baseline_sha)

    state = InstallerStateStore(tmp_path / "state")
    previous = _official_identity(
        profile=profile,
        version="1.0.9",
        commit_sha=baseline_sha,
        marker="a",
    )
    target = _official_identity(
        profile=profile,
        version="1.0.10",
        commit_sha=candidate_sha,
        marker="b",
    )
    state.stage_pending_release(previous)
    state.publish_installed_release(previous)
    config = InstallConfig(environment="production", deployment_profile=profile)
    state.save_journal(
        InstallJournal(
            operation_id="installed-baseline",
            status="completed",
            project_identity="disposable-release",
            config=config,
            completed_steps=("manifest",),
        )
    )

    class ActivationGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def inspect(self):
            self.calls.append("inspect")
            return InstallationEvidence(
                journal_exists=True,
                journal_completed=True,
                config_exists=True,
                env_exists=True,
                project_identity="disposable-release",
                observed_project_identity="disposable-release",
                durable_secrets_present=True,
            )

        def installed_config(self, recorded):
            return recorded

        def unfinished_profile_switch(self):
            return None

        def image_currency(self, _config, *, log):
            del log
            self.calls.append("image_currency")
            return ImageCurrency(
                source_revision=candidate_sha,
                running_revision=baseline_sha,
                update_required=True,
                reason="disposable exact release update",
            )

        def precheck_migrations(self, _config, *, log):
            del log
            self.calls.append("precheck_migrations")
            if migration_required:
                raise InstallerFailure(
                    code="release_migration_required",
                    cause="The disposable candidate requires a migration.",
                    remediation="Run the explicit database migration flow.",
                    exit_code=8,
                    step="migrations",
                )

        def verify_storage(self, _config, *, log, read_only=False):
            del log
            assert read_only is True
            self.calls.append("verify_storage")

        def verify_public_route(self, _config, *, log):
            del log
            self.calls.append("verify_public_route")

        def verify_bootstrap(self, _config, *, log, require_first_admin=False):
            del log
            assert require_first_admin is False
            self.calls.append("verify_bootstrap")
            return BootstrapVerification(bootstrap_required=False)

        def converge_compact_worker_processes(self, _config, *, log, changed=False):
            del log, changed
            self.calls.append("converge_compact_worker_processes")
            return ""

        def publish_release_environment(self, _config, release):
            assert release == target
            self.calls.append("publish_release_environment")

        def record_installed_releases(self, *, current, previous):
            assert (current, previous) == (candidate_sha, baseline_sha)
            self.calls.append("record_installed_releases")

        def prune_superseded_releases(self, _config, *, current, previous, log):
            del log
            assert (current, previous) == (candidate_sha, baseline_sha)
            self.calls.append("prune_superseded_releases")
            return ""

        def take_notices(self):
            return ()

        def product_url(self, _config):
            return "https://localhost:8099"

        def __getattr__(self, name):
            def record(_config, *, log, **_kwargs):
                del log
                self.calls.append(name)

            return record

    gateway = ActivationGateway()
    readiness = CoreReadinessService(
        probe=lambda: (
            gateway.calls.append("shared_readiness")
            or {"checks": [{"id": "runtime", "category": "core", "status": "pass"}]}
        )
    )

    def installer_runner(command, environment, _timeout):
        assert _git(repo, "rev-parse", "HEAD") == candidate_sha
        for image in target.images:
            assert image.registry_ref in environment.values()
        previous_pid = os.environ.get("FS_RELEASE_HANDOFF_LOCK_PID")
        previous_locks = os.environ.get("FS_RELEASE_HANDOFF_LOCKS")
        os.environ["FS_RELEASE_HANDOFF_LOCK_PID"] = str(os.getpid())
        os.environ["FS_RELEASE_HANDOFF_LOCKS"] = environment["FS_RELEASE_HANDOFF_INHERITED_LOCKS"]
        try:
            InstallerService(
                gateway=gateway,
                state=state,
                project_identity="disposable-release",
                readiness_service=readiness,
            ).install(config, target_release=target)
        except InstallerFailure as exc:
            return ProcessOutcome(
                tuple(command),
                exc.exit_code,
                stdout=json.dumps({"error": {"code": exc.code}}),
            )
        finally:
            if previous_pid is None:
                os.environ.pop("FS_RELEASE_HANDOFF_LOCK_PID", None)
            else:
                os.environ["FS_RELEASE_HANDOFF_LOCK_PID"] = previous_pid
            if previous_locks is None:
                os.environ.pop("FS_RELEASE_HANDOFF_LOCKS", None)
            else:
                os.environ["FS_RELEASE_HANDOFF_LOCKS"] = previous_locks
        child_result = InstallRunResult(
            classification=InstallationKind.INSTALLED,
            status="installed",
            operation_id="disposable-install-op",
            log_path=".flow-steward/logs/disposable-install.log",
        )
        return ProcessOutcome(
            tuple(command),
            0,
            stdout=json.dumps({"result": child_result.model_dump(mode="json")}),
        )

    result = InstallerReleaseHandoff(
        repo_root=repo,
        state=state,
        installer_runner=installer_runner,
    ).activate(target, timeout_seconds=60)

    assert _git(repo, "rev-parse", "HEAD") == candidate_sha
    if migration_required:
        assert result.status == "migration_required"
        assert state.load_installed_release() == previous
        assert state.load_pending_release() == target
        assert "precheck_migrations" in gateway.calls
        assert "start_runtime" not in gateway.calls
    else:
        assert result.status == "upgraded"
        assert state.load_installed_release() == target
        assert state.load_pending_release() is None
        for required_call in (
            "start_runtime",
            "verify_storage",
            "verify_public_route",
            "shared_readiness",
            "publish_release_environment",
        ):
            assert required_call in gateway.calls
        assert gateway.calls.index("start_runtime") < gateway.calls.index("verify_storage")
        assert gateway.calls.index("verify_storage") < gateway.calls.index("verify_public_route")
        assert gateway.calls.index("verify_public_route") < gateway.calls.index("shared_readiness")
        assert gateway.calls.index("shared_readiness") < gateway.calls.index(
            "publish_release_environment"
        )


def _cleanup_live_official_release_projects(
    projects: dict[str, DisposableInstallerProject],
) -> None:
    failures: list[Exception] = []
    for project in projects.values():
        try:
            project.cleanup()
        except Exception as exc:  # pragma: no cover - aggregated live cleanup evidence
            failures.append(exc)
    if failures:
        raise ExceptionGroup("official release acceptance cleanup failed", failures)


def _assert_live_release_readiness(profile: str, readiness: PublicHTTPResult) -> None:
    assert readiness.status == 200
    assert readiness.payload["ready"] is True
    if profile == "scalable":
        assert readiness.payload["status"] == "ready"


def test_live_release_readiness_matches_both_runtime_contracts() -> None:
    _assert_live_release_readiness(
        "compact", PublicHTTPResult(status=200, payload={"ready": True}, headers={})
    )
    _assert_live_release_readiness(
        "scalable",
        PublicHTTPResult(
            status=200,
            payload={"ready": True, "status": "ready"},
            headers={},
        ),
    )


def test_live_official_release_cleanup_attempts_every_baseline() -> None:
    compact = Mock(spec=DisposableInstallerProject)
    scalable = Mock(spec=DisposableInstallerProject)
    compact.cleanup.side_effect = RuntimeError("compact cleanup failed")

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        _cleanup_live_official_release_projects({"compact": compact, "scalable": scalable})

    compact.cleanup.assert_called_once_with()
    scalable.cleanup.assert_called_once_with()


@pytest.fixture(scope="module")
def live_official_release_projects() -> Iterator[dict[str, DisposableInstallerProject]]:
    projects: dict[str, DisposableInstallerProject] = {}
    for profile in ("compact", "scalable"):
        manifest = os.environ.get(
            f"FLOW_STEWARD_TEST_RELEASE_ACCEPTANCE_{profile.upper()}_MANIFEST", ""
        ).strip()
        if manifest:
            projects[profile] = DisposableInstallerProject(
                manifest_path=Path(manifest).resolve(),
                source_root=ROOT,
            )
    try:
        yield projects
    finally:
        _cleanup_live_official_release_projects(projects)


@pytest.mark.integration
@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_live_official_release_upgrade_uses_public_cli_and_real_disposable_stack(
    tmp_path: Path,
    profile: str,
    live_official_release_projects: dict[str, DisposableInstallerProject],
) -> None:
    """Opt-in live acceptance; the platform script supplies two disposable baselines."""
    target_version = os.environ.get(
        "FLOW_STEWARD_TEST_RELEASE_ACCEPTANCE_TARGET_VERSION", ""
    ).strip()
    if profile not in live_official_release_projects or not target_version:
        pytest.skip("official release acceptance inputs are not configured")
    project = live_official_release_projects[profile]
    manifest_path = project.manifest_path
    try:
        before = _json_result(project.cli("release", "info", "--json"))
        previous_release = before["current_release"]
        assert before["deployment_profile"] == profile
        assert before["current_version"] != target_version
        assert previous_release["version"] == before["current_version"]
        assert all("@sha256:" in image["registry_ref"] for image in previous_release["images"])

        maintenance = _json_result(
            project.cli(
                "maintenance",
                "enable",
                "--non-interactive",
                "--confirm",
                "--json",
            )
        )
        assert maintenance["status"] == "enabled"

        checked = _json_result(project.cli("release", "check", "--json"))
        assert checked["status"] == "upgrade_available"
        assert checked["deployment_profile"] == profile
        assert checked["current_version"] == before["current_version"]
        assert checked["current_version"] != target_version
        assert checked["target_version"] == target_version

        upgraded = _json_result(
            project.cli(
                "release",
                "upgrade",
                "--target-version",
                target_version,
                "--non-interactive",
                "--confirm",
                "--json",
            )
        )
        assert upgraded["status"] == "upgraded"
        assert upgraded["current_version"] == target_version
        assert upgraded["deployment_profile"] == profile
        target = upgraded["target_release"]
        assert target["version"] == target_version
        assert all("@sha256:" in image["registry_ref"] for image in target["images"])

        info = _json_result(project.cli("release", "info", "--json"))
        assert info["current_version"] == target_version
        assert info["deployment_profile"] == profile
        assert info["current_release"] != previous_release
        assert info["current_release"] == upgraded["current_release"]

        stack = tomllib.loads((project.state_directory / "stack.toml").read_text(encoding="utf-8"))
        tls_mode = str(stack.get("tls_mode") or "local")
        assert project.required_services_healthy()
        assert project.storage_round_trip() == b"installer-proof"
        assert project.public_route_healthy(tls_mode=tls_mode)
        readiness = project.public_json("GET", "/health/ready")
        _assert_live_release_readiness(profile, readiness)
    finally:
        project.cleanup()
        shutil.copy2(manifest_path, tmp_path / "disposable-installer-resources.json")
