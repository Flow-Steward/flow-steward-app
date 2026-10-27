from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.application.installer import (
    InstallationKind,
    InstallConfig,
    InstallRunResult,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.errors import LifecycleError
from core.application.release.activation import ReleaseHandoffJournal
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import SourceInstallationOrigin
from core.application.release.service import ReleaseTransitionBinding
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor
from core.infrastructure.installer.release_handoff import InstallerReleaseHandoff
from core.infrastructure.installer.state_store import InstallerStateStore


def _release(version: str = "1.0.10", marker: str = "b") -> ReleaseIdentity:
    digest = f"sha256:{marker * 64}"
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=marker * 40,
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
                revision=marker * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )


def _transition_binding(
    target: ReleaseIdentity, *, config: InstallConfig | None = None
) -> ReleaseTransitionBinding:
    configuration_digest = "c" * 64
    if config is not None:
        serialized = json.dumps(
            config.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        configuration_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    origin = SourceInstallationOrigin(
        commit_sha="a" * 40,
        deployment_profile="scalable",
        platform="linux/amd64",
        installation_id="flow-steward-scalable",
        configuration_sha256=configuration_digest,
        release_repository="Flow-Steward/flow-steward-app",
    )
    return ReleaseTransitionBinding(
        installed_origin=origin,
        configuration_sha256=origin.configuration_sha256,
        release_repository=origin.release_repository,
        deployment_profile="scalable",
        platform="linux/amd64",
        target_release=target,
    )


@dataclass
class FakeGit:
    target: ReleaseIdentity
    dirty: bool = False
    untracked: str = ""
    head: str = "a" * 40
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, command: Sequence[str]) -> ProcessOutcome:
        command = tuple(command)
        self.commands.append(command)
        args = command[3:]
        if args[:2] == ("diff", "--quiet") or args[:3] == (
            "diff",
            "--cached",
            "--quiet",
        ):
            return self._outcome(command, returncode=1 if self.dirty else 0)
        if args[:2] == ("rev-parse", "--verify"):
            return self._outcome(command, stdout=self.target.commit_sha + "\n")
        if args[:2] == ("ls-files", "--others"):
            return self._outcome(command, stdout=self.untracked)
        if args[:2] == ("ls-tree", "-r"):
            return self._outcome(command, stdout="tracked.py\0conflict.txt\0")
        if args[:2] == ("checkout", "--detach"):
            self.head = self.target.commit_sha
            return self._outcome(command)
        if args == ("rev-parse", "HEAD^{commit}"):
            return self._outcome(command, stdout=self.head + "\n")
        return self._outcome(command)

    @staticmethod
    def _outcome(
        command: Sequence[str], *, returncode: int = 0, stdout: str = ""
    ) -> ProcessOutcome:
        return ProcessOutcome(tuple(command), returncode, stdout=stdout)


def _handoff(
    tmp_path: Path,
    *,
    git: FakeGit,
    installer,
    source_repository: str = "Flow-Steward/flow-steward-app",
) -> tuple[InstallerReleaseHandoff, InstallerStateStore]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = InstallerStateStore(tmp_path / "state")
    return (
        InstallerReleaseHandoff(
            repo_root=repo,
            state=state,
            source_repository=source_repository,
            git_runner=git,
            installer_runner=installer,
        ),
        state,
    )


def _successful_child(command: Sequence[str]) -> ProcessOutcome:
    result = InstallRunResult(
        classification=InstallationKind.INSTALLED,
        status="installed",
        operation_id="installer-op-1",
        log_path=".flow-steward/logs/install-installer-op-1.log",
    )
    return ProcessOutcome(
        tuple(command),
        0,
        stdout=json.dumps({"result": result.model_dump(mode="json")}),
    )


def test_dirty_tracked_checkout_refuses_before_any_checkout_or_pending_state(
    tmp_path: Path,
) -> None:
    target = _release()
    git = FakeGit(target, dirty=True)
    installer_calls: list[object] = []
    handoff, state = _handoff(
        tmp_path,
        git=git,
        installer=lambda *args: installer_calls.append(args),
    )

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60)

    assert raised.value.code == "release_checkout_dirty"
    assert not any("checkout" in command for command in git.commands)
    assert state.load_pending_release() is None
    assert installer_calls == []


def test_untracked_target_conflict_refuses_without_deleting_anything(tmp_path: Path) -> None:
    target = _release()
    git = FakeGit(target, untracked="conflict.txt\0unrelated.txt\0")
    handoff, state = _handoff(tmp_path, git=git, installer=lambda *_args: None)

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60)

    assert raised.value.code == "release_checkout_untracked_conflict"
    assert "conflict.txt" in raised.value.cause
    assert state.load_pending_release() is None


def test_missing_tag_untracked_conflict_does_not_mutate_the_checkout_repository(
    tmp_path: Path,
) -> None:
    target = _release()
    repo = tmp_path / "repo"
    local_tag_available = False
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> ProcessOutcome:
        nonlocal local_tag_available
        command = tuple(command)
        commands.append(command)
        command_repo = Path(command[2])
        args = command[3:]
        is_checkout_repo = command_repo == repo
        if args[:2] in {("diff", "--quiet"), ("diff", "--cached")}:
            return ProcessOutcome(command, 0)
        if args[:2] == ("rev-parse", "--verify"):
            if is_checkout_repo and not local_tag_available:
                return ProcessOutcome(command, 1)
            return ProcessOutcome(command, 0, stdout=target.commit_sha + "\n")
        if args[:2] == ("ls-files", "--others"):
            return ProcessOutcome(command, 0, stdout="conflict.txt\0")
        if args[:2] == ("ls-tree", "-r"):
            return ProcessOutcome(command, 0, stdout="tracked.py\0conflict.txt\0")
        if args[:2] == ("fetch", "--no-tags"):
            if is_checkout_repo:
                local_tag_available = True
            return ProcessOutcome(command, 0)
        return ProcessOutcome(command, 0)

    state = InstallerStateStore(tmp_path / "state")
    handoff = InstallerReleaseHandoff(
        repo_root=repo,
        state=state,
        git_runner=runner,
        installer_runner=lambda *_args: pytest.fail("installer must not run"),
    )

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60)

    assert raised.value.code == "release_checkout_untracked_conflict"
    assert not any(
        Path(command[2]) == repo and command[3:5] == ("fetch", "--no-tags") for command in commands
    )
    assert state.load_pending_release() is None
    assert state.load_release_handoff() is None


def test_missing_tag_is_fetched_from_the_configured_https_repository(tmp_path: Path) -> None:
    target = _release()
    git = FakeGit(target)
    tag_available = False

    def runner(command: Sequence[str]) -> ProcessOutcome:
        nonlocal tag_available
        command = tuple(command)
        if command[3:5] == ("rev-parse", "--verify") and not tag_available:
            git.commands.append(command)
            return ProcessOutcome(command, 1)
        if command[3:5] == ("fetch", "--no-tags"):
            git.commands.append(command)
            tag_available = True
            return ProcessOutcome(command, 0)
        return git(command)

    handoff, _state = _handoff(tmp_path, git=git, installer=lambda *_args: None)
    handoff.git_runner = runner
    handoff.source_repository = "example/releases"

    handoff._ensure_tag(target)

    fetch = next(command for command in git.commands if command[3] == "fetch")
    assert fetch[5] == "https://github.com/example/releases.git"


def test_missing_private_tag_uses_owner_only_askpass_without_exposing_token(
    tmp_path: Path,
) -> None:
    target = _release()
    git = FakeGit(target)
    tag_available = False
    secret = "private-release-token"  # pragma: allowlist secret
    observed: dict[str, object] = {}
    authenticated_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> ProcessOutcome:
        nonlocal tag_available
        command = tuple(command)
        if command[3:5] == ("rev-parse", "--verify") and not tag_available:
            git.commands.append(command)
            return ProcessOutcome(command, 1)
        if command[3:5] == ("fetch", "--no-tags"):
            git.commands.append(command)
            tag_available = True
            return ProcessOutcome(command, 0)
        return git(command)

    def authenticated(command: Sequence[str], environment: Mapping[str, str]) -> ProcessOutcome:
        nonlocal tag_available
        command = tuple(command)
        authenticated_commands.append(command)
        if "fetch" in command:
            tag_available = True
        observed["environment"] = dict(environment)
        askpass = Path(environment["GIT_ASKPASS"])
        observed["askpass_mode"] = askpass.stat().st_mode & 0o777
        observed["askpass_text"] = askpass.read_text(encoding="utf-8")
        return ProcessOutcome(command, 0)

    handoff, state = _handoff(tmp_path, git=git, installer=lambda *_args: None)
    credential_root = state.root / "secret-input"
    credential_root.mkdir(mode=0o700, parents=True)
    credential = credential_root / "github-release-token"
    credential.write_text(secret + "\n", encoding="utf-8")
    credential.chmod(0o600)
    handoff.git_runner = runner
    handoff.authenticated_git_runner = authenticated
    handoff.source_repository = "example/private-releases"

    handoff._ensure_tag(target)

    environment = observed["environment"]
    assert isinstance(environment, dict)
    fetch = next(command for command in authenticated_commands if "fetch" in command)
    assert "https://x-access-token@github.com/example/private-releases.git" in fetch
    assert "credential.helper=" in fetch
    assert "credential.useHttpPath=true" in fetch
    assert "http.followRedirects=false" in fetch
    assert secret not in " ".join(part for command in authenticated_commands for part in command)
    assert secret not in " ".join(environment.values())
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert observed["askpass_mode"] == 0o700
    assert secret not in str(observed["askpass_text"])
    assert not Path(environment["GIT_ASKPASS"]).exists()


def test_fresh_handoff_persists_custom_release_sources_for_child_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _release()
    git = FakeGit(target)
    repo = tmp_path / "repo"
    repo.mkdir()
    state = InstallerStateStore(tmp_path / "state")
    handoff = InstallerReleaseHandoff(
        repo_root=repo,
        state=state,
        source_repository="example/releases",
        source_registries="registry.example/flow,mirror.example/flow",
        git_runner=git,
        installer_runner=lambda *_args: ProcessOutcome(("installer",), 1),
    )

    handoff.prepare_checkout(
        target,
        install_config=InstallConfig(environment="production", deployment_profile="scalable"),
    )
    monkeypatch.delenv("FS_RELEASE_REPOSITORY", raising=False)
    monkeypatch.delenv("FS_RELEASE_REGISTRIES", raising=False)

    resumed = InstallerReleaseHandoff(repo_root=repo, state=state, git_runner=git)
    environment = resumed._installer_environment(target)
    journal = state.load_release_handoff()

    assert journal.release_repository == "example/releases"
    assert journal.release_registries == "registry.example/flow,mirror.example/flow"
    assert environment["FS_RELEASE_REPOSITORY"] == "example/releases"
    assert environment["FS_RELEASE_REGISTRIES"] == "registry.example/flow,mirror.example/flow"


def test_verified_handoff_lends_locks_and_only_private_installer_publishes_runtime(
    tmp_path: Path,
) -> None:
    target = _release()
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    seen_environment: dict[str, str] = {}

    def installer(
        command: Sequence[str], environment: Mapping[str, str], timeout: int
    ) -> ProcessOutcome:
        del timeout
        state = state_holder["state"]
        contender = InstallerStateStore(state.root)
        with pytest.raises(InstallerFailure) as refused, contender.operation_lock():
            pass
        assert refused.value.code == "installer_operation_active"
        state.publish_installed_release(target)
        seen_environment.update(environment)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state

    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "upgraded"
    assert state.load_installed_release() == target
    assert state.load_pending_release() is None
    assert state.load_release_handoff().phase == "installer_completed"
    assert seen_environment["FS_APP_IMAGE"] == target.images[0].registry_ref
    assert "FS_RELEASE_HANDOFF_INHERITED_LOCKS" in seen_environment
    assert all("docker" not in command for command in git.commands)


@pytest.mark.parametrize("probe_mode", ("0", "1", None))
def test_handoff_preserves_explicit_probe_execution_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_mode: str | None
) -> None:
    if probe_mode is None:
        monkeypatch.delenv("FS_INSTALLER_USE_HOST_PYTHON", raising=False)
    else:
        monkeypatch.setenv("FS_INSTALLER_USE_HOST_PYTHON", probe_mode)
    target = _release()
    seen: dict[str, str] = {}
    state_holder: dict[str, InstallerStateStore] = {}

    def installer(command, environment, timeout):
        del timeout
        seen.update(environment)
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=FakeGit(target), installer=installer)
    state_holder["state"] = state

    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "upgraded"
    assert seen.get("FS_INSTALLER_USE_HOST_PYTHON") == probe_mode
    assert state.load_installed_release() == target


def test_source_transition_is_revalidated_under_lock_and_persisted_before_checkout(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    observations: list[str] = []

    def revalidate(observed: ReleaseTransitionBinding) -> None:
        state = state_holder["state"]
        with (
            pytest.raises(InstallerFailure) as refused,
            InstallerStateStore(state.root).operation_lock(),
        ):
            pass
        assert refused.value.code == "installer_operation_active"
        assert state.load_pending_release() is None
        assert state.load_release_handoff() is None
        assert observed == binding
        observations.append("revalidated")

    def installer(command, _environment, _timeout):
        state = state_holder["state"]
        journal = state.load_release_handoff()
        assert journal is not None
        assert state.load_release_transition_binding() == binding
        assert state.load_pending_release() == target
        assert git.head == target.commit_sha
        state.publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    handoff.transition_revalidator = revalidate

    result = handoff.activate(
        target,
        timeout_seconds=60,
        transition_binding=binding,
    )

    assert result.status == "upgraded"
    assert observations == ["revalidated"]
    assert state.load_release_transition_binding() is None
    assert state.load_release_handoff().phase == "installer_completed"


def test_source_transition_refuses_repository_mismatch_before_pending_or_checkout(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    installer_calls: list[object] = []
    handoff, state = _handoff(
        tmp_path,
        git=git,
        installer=lambda *args: installer_calls.append(args),
        source_repository="example/other-releases",
    )

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert raised.value.code == "release_source_repository_mismatch"
    assert state.load_pending_release() is None
    assert state.load_release_handoff() is None
    assert not any("checkout" in command for command in git.commands)
    assert installer_calls == []


def test_source_transition_resume_uses_durable_binding_without_reclassification(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    attempts = 0
    revalidations = 0

    def revalidate(observed: ReleaseTransitionBinding) -> None:
        nonlocal revalidations
        assert observed == binding
        revalidations += 1

    def installer(command, _environment, _timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    handoff.transition_revalidator = revalidate

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(target, timeout_seconds=60, transition_binding=binding)
    result = handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert result.status == "upgraded"
    assert revalidations == 1
    assert attempts == 2
    assert len([command for command in git.commands if "checkout" in command]) == 1


def test_source_transition_resumes_after_binding_is_durable_before_journal(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    revalidations = 0
    crash = True

    def revalidate(observed: ReleaseTransitionBinding) -> None:
        nonlocal revalidations
        assert observed == binding
        revalidations += 1

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    handoff.transition_revalidator = revalidate
    original_save = state.save_release_handoff

    def fail_once(journal: ReleaseHandoffJournal) -> None:
        nonlocal crash
        if crash:
            crash = False
            raise KeyboardInterrupt
        original_save(journal)

    state.save_release_handoff = fail_once  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert state.load_release_transition_binding() == binding
    assert state.load_release_handoff() is None
    assert state.load_pending_release() is None
    assert not any("checkout" in command for command in git.commands)

    result = handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert result.status == "upgraded"
    assert revalidations == 1


def test_binding_only_resume_requires_the_exact_reviewed_binding(tmp_path: Path) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    handoff, state = _handoff(
        tmp_path,
        git=git,
        installer=lambda *_args: pytest.fail("installer must not run"),
    )
    state.save_release_transition_binding(binding)

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60)

    assert raised.value.code == "release_handoff_binding_conflict"
    assert state.load_release_transition_binding() == binding
    assert state.load_release_handoff() is None
    assert state.load_pending_release() is None
    assert not any("checkout" in command for command in git.commands)


def test_source_transition_resumes_after_journal_is_durable_before_pending(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    crash = True

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    handoff.transition_revalidator = lambda observed: None
    original_stage = state.stage_pending_release

    def fail_once(release: ReleaseIdentity) -> None:
        nonlocal crash
        if crash:
            crash = False
            raise KeyboardInterrupt
        original_stage(release)

    state.stage_pending_release = fail_once  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert state.load_release_transition_binding() == binding
    assert state.load_release_handoff() == ReleaseHandoffJournal(
        target=target,
        phase="prepared",
    )
    assert state.load_pending_release() is None

    result = handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert result.status == "upgraded"


def test_source_transition_resumes_after_pending_is_durable_before_checkout(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    crash = True

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    handoff.transition_revalidator = lambda observed: None
    original_write = state.write_executable_text

    def fail_once(path: Path, value: str) -> None:
        nonlocal crash
        if crash:
            crash = False
            raise KeyboardInterrupt
        original_write(path, value)

    state.write_executable_text = fail_once  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert state.load_release_transition_binding() == binding
    assert state.load_release_handoff() == ReleaseHandoffJournal(
        target=target,
        phase="prepared",
    )
    assert state.load_pending_release() == target
    assert not any("checkout" in command for command in git.commands)

    result = handoff.activate(target, timeout_seconds=60, transition_binding=binding)

    assert result.status == "upgraded"


def test_completed_source_transition_is_terminal_for_the_common_handoff(
    tmp_path: Path,
) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target, head=target.commit_sha)
    installer_calls: list[object] = []
    handoff, state = _handoff(
        tmp_path,
        git=git,
        installer=lambda *args: installer_calls.append(args),
    )
    state.root.mkdir(parents=True, exist_ok=True)
    (state.root / "release-transition-binding.json").write_text(
        binding.model_dump_json(),
        encoding="utf-8",
    )
    state.publish_installed_release(target)
    state.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))

    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "upgraded"
    assert result.current_release == target
    assert installer_calls == []
    assert state.load_release_handoff() == ReleaseHandoffJournal(
        target=target,
        phase="installer_completed",
    )
    assert not (state.root / "release-transition-binding.json").exists()
    assert not any("checkout" in command for command in git.commands)


def test_next_release_reconciles_stale_binding_after_source_transition_publish(
    tmp_path: Path,
) -> None:
    first = _release("1.0.10", "b")
    second = _release("1.0.11", "c")
    binding = _transition_binding(first)
    git = FakeGit(second, head=first.commit_sha)
    state_holder: dict[str, InstallerStateStore] = {}

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(second)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    state.publish_installed_release(first)
    state.save_release_transition_binding(binding)
    state.save_release_handoff(ReleaseHandoffJournal(target=first, phase="installer_completed"))

    result = handoff.activate(second, timeout_seconds=60)

    assert result.status == "upgraded"
    assert state.load_installed_release() == second
    assert state.load_release_transition_binding() is None
    assert state.load_release_handoff() == ReleaseHandoffJournal(
        target=second,
        phase="installer_completed",
    )


def test_source_transition_resume_rejects_conflicting_binding(tmp_path: Path) -> None:
    target = _release()
    binding = _transition_binding(target)
    git = FakeGit(target, head=target.commit_sha)
    handoff, state = _handoff(tmp_path, git=git, installer=lambda *_args: None)
    state.stage_pending_release(target)
    state.save_release_transition_binding(binding)
    state.save_release_handoff(
        ReleaseHandoffJournal(
            target=target,
            phase="checkout_applied",
        )
    )
    changed = binding.model_copy(
        update={
            "installed_origin": binding.installed_origin.model_copy(update={"commit_sha": "d" * 40})
        }
    )

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60, transition_binding=changed)

    assert raised.value.code == "release_handoff_binding_conflict"
    assert state.load_pending_release() == target


def test_release_handoff_reuses_already_held_lifecycle_lock(tmp_path: Path) -> None:
    import fcntl

    target = _release()
    git = FakeGit(target)
    lifecycle_root = tmp_path / "lifecycle"
    state_root = tmp_path / "state"
    lifecycle_lock = lifecycle_root / "operations" / "active.lock"
    lifecycle_lock.parent.mkdir(parents=True)
    lifecycle_lock.touch()
    state = InstallerStateStore(state_root, lifecycle_state_root=lifecycle_root)
    repo = tmp_path / "repo"
    repo.mkdir()
    seen_locks: dict[str, int] = {}

    def installer(command, environment, _timeout):
        seen_locks.update(
            {
                str(path): int(fd)
                for path, fd in json.loads(
                    environment["FS_RELEASE_HANDOFF_INHERITED_LOCKS"]
                ).items()
            }
        )
        state.publish_installed_release(target)
        return _successful_child(command)

    handoff = InstallerReleaseHandoff(
        repo_root=repo,
        state=state,
        git_runner=git,
        installer_runner=installer,
    )
    descriptor = os.open(lifecycle_lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = handoff.activate(
            target,
            timeout_seconds=60,
            held_locks={str(lifecycle_lock.resolve()): descriptor},
        )
    finally:
        os.close(descriptor)

    assert result.status == "upgraded"
    assert seen_locks[str(lifecycle_lock.resolve())] == descriptor
    assert set(seen_locks) == {
        str(state.lock_path.resolve()),
        str(state.lifecycle_lock_path.resolve()),
        str(lifecycle_lock.resolve()),
    }


def test_handoff_lock_descriptors_survive_a_real_subprocess_and_inode_alias(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    lifecycle_root = tmp_path / "lifecycle"
    state_operations = state_root / "operations"
    lifecycle_operations = lifecycle_root / "operations"
    state_operations.mkdir(parents=True)
    lifecycle_operations.mkdir(parents=True)
    state_lock = state_operations / "active.lock"
    state_lock.touch()
    os.link(state_lock, lifecycle_operations / "active.lock")
    state = InstallerStateStore(state_root, lifecycle_state_root=lifecycle_root)
    script = """
import fcntl, json, os
locks = json.loads(os.environ['FS_RELEASE_HANDOFF_LOCKS'])
for path, raw_fd in locks.items():
    fd = int(raw_fd)
    left = os.stat(path)
    right = os.fstat(fd)
    assert (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('inherited-locks-ok')
"""

    with state.operation_lock() as locks:
        environment = {
            "FS_RELEASE_HANDOFF_LOCKS": json.dumps(locks, sort_keys=True),
        }
        outcome = SubprocessExecutor().run(
            (sys.executable, "-c", script),
            environment=environment,
            inherited_fds=tuple(locks.values()),
            timeout_seconds=10,
        )

    assert outcome.returncode == 0
    assert outcome.stdout.strip() == "inherited-locks-ok"
    assert len(set(locks.values())) == 2  # install.lock plus one inode-aliased lifecycle lock


def test_fresh_install_runs_only_in_verified_checkout_and_returns_secret_out_of_band(
    tmp_path: Path,
) -> None:
    target = _release()
    git = FakeGit(target)
    state_holder: dict[str, InstallerStateStore] = {}
    seen_command: tuple[str, ...] = ()

    def installer(command, _environment, _timeout):
        nonlocal seen_command
        seen_command = tuple(command)
        state = state_holder["state"]
        journal = state.load_release_handoff()
        assert journal is not None
        assert journal.install_config == config
        assert journal.explicit_fields == ("public_port",)
        state.save_release_handoff_secret("https://localhost/setup#secret")
        state.publish_installed_release(target)
        child_result = InstallRunResult(
            classification=InstallationKind.FRESH,
            status="installed",
            product_url="https://localhost",
        )
        return ProcessOutcome(
            tuple(command),
            0,
            stdout=json.dumps({"result": child_result.model_dump(mode="json")}),
        )

    config = InstallConfig(
        environment="production",
        deployment_profile="scalable",
        public_port=9443,
    )
    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state

    result = handoff.install_exact(
        target,
        config=config,
        explicit_fields=frozenset({"public_port"}),
        timeout_seconds=60,
    )

    assert result.bootstrap_setup_url == "https://localhost/setup#secret"
    assert len([command for command in git.commands if "checkout" in command]) == 1
    assert seen_command[3] == target.commit_sha
    bootstrap = (state.root / "release-handoff-bootstrap.py").read_text(encoding="utf-8")
    assert bootstrap.index("rev-parse") < bootstrap.index("sys.path.insert")
    assert bootstrap.index("flock") < bootstrap.index("rev-parse")
    assert not state.release_handoff_secret_path.exists()


def test_installer_resumes_durable_source_transition_without_reselecting_binding(
    tmp_path: Path,
) -> None:
    target = _release()
    config = InstallConfig(environment="production", deployment_profile="scalable")
    binding = _transition_binding(target, config=config)
    git = FakeGit(target)
    holder: dict[str, InstallerStateStore] = {}

    def installer(command, _environment, _timeout):
        state = holder["state"]
        assert state.load_release_transition_binding() == binding
        assert state.load_pending_release() == target
        state.publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    holder["state"] = state
    state.save_release_transition_binding(binding)
    state.stage_pending_release(target)
    state.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))
    git.head = target.commit_sha

    result = handoff.install_exact(
        target,
        config=config,
        explicit_fields=frozenset(),
        timeout_seconds=60,
    )

    assert result.status == "installed"
    assert state.load_installed_release() == target
    assert state.load_pending_release() is None
    assert state.load_release_transition_binding() is None
    assert state.load_release_handoff().phase == "installer_completed"
    assert not any("checkout" in command for command in git.commands)


@pytest.mark.parametrize("conflict", ("repository", "missing_journal", "target", "configuration"))
def test_installer_transition_resume_refuses_unbound_or_conflicting_context(
    tmp_path: Path, conflict: str
) -> None:
    target = _release()
    config = InstallConfig(environment="production", deployment_profile="scalable")
    binding = _transition_binding(target, config=config)
    handoff, state = _handoff(
        tmp_path,
        git=FakeGit(target),
        installer=lambda *_args: pytest.fail("conflicting recovery must not start installer"),
        source_repository="other/releases"
        if conflict == "repository"
        else binding.release_repository,
    )
    state.save_release_transition_binding(binding)
    state.stage_pending_release(target)
    if conflict != "missing_journal":
        state.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))

    requested = _release("1.0.11", "c") if conflict == "target" else target
    requested_config = (
        config.model_copy(update={"public_port": 9443}) if conflict == "configuration" else config
    )
    explicit_fields: frozenset[str] = frozenset()
    with pytest.raises(LifecycleError) as refused:
        handoff.install_exact(
            requested,
            config=requested_config,
            explicit_fields=explicit_fields,
            timeout_seconds=60,
        )

    assert refused.value.code in {
        "release_handoff_binding_conflict",
        "release_source_repository_mismatch",
        "release_handoff_target_conflict",
    }
    assert state.load_release_transition_binding() == binding
    assert state.load_pending_release() == target


def test_interrupted_handoff_resumes_after_checkout_without_repeating_it(tmp_path: Path) -> None:
    target = _release()
    git = FakeGit(target)
    attempts = 0
    state_holder: dict[str, InstallerStateStore] = {}

    def installer(command, _environment, _timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(target, timeout_seconds=60)
    result = handoff.activate(target, timeout_seconds=60)

    checkout_commands = [command for command in git.commands if "checkout" in command]
    assert len(checkout_commands) == 1
    assert attempts == 2
    assert result.status == "upgraded"


@pytest.mark.parametrize(
    ("phase", "head", "checkout_count"),
    (("prepared", "a" * 40, 1), ("checkout_applied", "b" * 40, 0)),
)
def test_nonterminal_resume_recreates_missing_private_bootstrap(
    tmp_path: Path,
    phase: str,
    head: str,
    checkout_count: int,
) -> None:
    target = _release()
    git = FakeGit(target, head=head)
    handoff, state = _handoff(
        tmp_path,
        git=git,
        installer=lambda command, _environment, _timeout: ProcessOutcome(tuple(command), 0),
    )
    state.stage_pending_release(target)
    state.save_release_handoff(ReleaseHandoffJournal(target=target, phase=phase))

    handoff.prepare_checkout(target)

    bootstrap = state.root / "release-handoff-bootstrap.py"
    assert bootstrap.is_file()
    assert oct(bootstrap.stat().st_mode & 0o777) == "0o700"
    assert len([command for command in git.commands if "checkout" in command]) == checkout_count


def test_completed_handoff_rolls_over_for_the_next_release(tmp_path: Path) -> None:
    first = _release("1.0.10", "b")
    second = _release("1.0.11", "c")
    git = FakeGit(first)
    state_holder: dict[str, InstallerStateStore] = {}
    targets = iter((first, second))

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(next(targets))
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state

    assert handoff.activate(first, timeout_seconds=60).status == "upgraded"
    git.target = second
    assert handoff.activate(second, timeout_seconds=60).status == "upgraded"

    checkout_commands = [command for command in git.commands if "checkout" in command]
    assert len(checkout_commands) == 2
    assert state.load_installed_release() == second
    assert state.load_pending_release() is None
    journal = state.load_release_handoff()
    assert journal is not None
    assert journal.target == second
    assert journal.phase == "installer_completed"


def test_crash_after_installed_publication_reconciles_before_next_release(tmp_path: Path) -> None:
    first = _release("1.0.10", "b")
    second = _release("1.0.11", "c")
    git = FakeGit(first)
    state_holder: dict[str, InstallerStateStore] = {}
    crashed = False

    def installer(command, _environment, _timeout):
        nonlocal crashed
        state = state_holder["state"]
        if not crashed:
            crashed = True
            state.publish_installed_release(first)
            raise KeyboardInterrupt
        state.publish_installed_release(second)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state

    with pytest.raises(KeyboardInterrupt):
        handoff.activate(first, timeout_seconds=60)
    assert state.load_release_handoff().phase == "checkout_applied"
    assert state.load_installed_release() == first
    assert state.load_pending_release() is None

    git.target = second
    result = handoff.activate(second, timeout_seconds=60)

    assert result.status == "upgraded"
    assert state.load_installed_release() == second
    assert state.load_release_handoff().target == second


def test_migration_stop_keeps_old_installed_release_and_durable_pending_target(
    tmp_path: Path,
) -> None:
    previous = _release("1.0.9", "a")
    target = _release()
    git = FakeGit(target)
    attempts = 0
    state_holder: dict[str, InstallerStateStore] = {}

    def installer(command, _environment, _timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ProcessOutcome(
                tuple(command),
                8,
                stdout=json.dumps(
                    {
                        "error": {
                            "code": "release_migration_required",
                            "message": "Candidate requires a database migration.",
                            "hint": "Create a backup, run database upgrade, then resume.",
                            "retryable": True,
                        },
                        "log_path": ".flow-steward/logs/install-migration.log",
                    }
                ),
            )
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    state.stage_pending_release(previous)
    state.publish_installed_release(previous)

    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "migration_required"
    assert state.load_installed_release() == previous
    assert state.load_pending_release() == target
    assert state.load_release_handoff().phase == "migration_required"
    assert "database upgrade" in result.remediation
    assert result.log_path == ".flow-steward/logs/install-migration.log"

    resumed = handoff.activate(target, timeout_seconds=60)

    assert resumed.status == "upgraded"
    assert attempts == 2
    assert len([command for command in git.commands if "checkout" in command]) == 1
    assert state.load_installed_release() == target
    assert state.load_pending_release() is None


def test_next_upgrade_reconciles_crash_after_installed_identity_publish(
    tmp_path: Path,
) -> None:
    previous = _release("1.0.9", "a")
    target = _release("1.0.10", "b")
    git = FakeGit(target, head=previous.commit_sha)
    state_holder: dict[str, InstallerStateStore] = {}

    def installer(command, _environment, _timeout):
        state_holder["state"].publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=git, installer=installer)
    state_holder["state"] = state
    state.publish_installed_release(previous)
    state.stage_pending_release(previous)
    state.save_release_handoff(ReleaseHandoffJournal(target=previous, phase="checkout_applied"))

    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "upgraded"
    assert result.operation_id == "installer-op-1"
    assert result.log_path == ".flow-steward/logs/install-installer-op-1.log"
    assert state.load_installed_release() == target
    assert state.load_pending_release() is None
    assert state.load_release_handoff() == ReleaseHandoffJournal(
        target=target, phase="installer_completed"
    )
    assert len([command for command in git.commands if "checkout" in command]) == 1


def test_generic_schema_failure_is_not_reported_as_migration_required(tmp_path: Path) -> None:
    target = _release()
    git = FakeGit(target)

    def installer(command, _environment, _timeout):
        return ProcessOutcome(
            tuple(command),
            8,
            stdout='{"error":{"code":"schema_baseline_failed"}}',
        )

    handoff, state = _handoff(tmp_path, git=git, installer=installer)

    with pytest.raises(LifecycleError) as raised:
        handoff.activate(target, timeout_seconds=60)

    assert raised.value.code == "release_handoff_failed"
    assert state.load_release_handoff().phase == "checkout_applied"
