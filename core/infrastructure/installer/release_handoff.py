"""Resumable Git-to-installer handoff for an already resolved official release."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from core.application.installer.contracts import InstallConfig, InstallRunResult
from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import ExitCode
from core.application.release.activation import ReleaseHandoffJournal
from core.application.release.catalog import (
    DEFAULT_RELEASE_REPOSITORY,
    validated_release_repository,
)
from core.application.release.contracts import ReleaseIdentity
from core.application.release.images import all_release_roles
from core.application.release.service import ReleaseResult, ReleaseTransitionBinding
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.release.github_credentials import GitHubReleaseCredentialLoader
from core.infrastructure.release.installed_origin import _configuration_sha256

GitRunner = Callable[[Sequence[str]], ProcessOutcome]
AuthenticatedGitRunner = Callable[[Sequence[str], Mapping[str, str]], ProcessOutcome]
InstallerRunner = Callable[[Sequence[str], Mapping[str, str], int], ProcessOutcome]
TransitionRevalidator = Callable[[ReleaseTransitionBinding], None]

_BOOTSTRAP_NAME = "release-handoff-bootstrap.py"
_BOOTSTRAP = """\
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
expected = sys.argv[2]
state_root = Path(sys.argv[3]).resolve()
lifecycle_root = Path(sys.argv[4]).resolve()
expected_paths = {
    os.path.realpath(lock_path)
    for lock_path in (
    state_root / "install.lock",
    state_root / "operations" / "active.lock",
    lifecycle_root / "operations" / "active.lock",
    )
}
try:
    inherited = json.loads(os.environ["FS_RELEASE_HANDOFF_INHERITED_LOCKS"])
except (KeyError, TypeError, ValueError):
    raise SystemExit("release_handoff_lock_unavailable")
if not isinstance(inherited, dict) or set(inherited) != expected_paths:
    raise SystemExit("release_handoff_lock_unavailable")
for lock_path, raw_fd in inherited.items():
    try:
        fd = int(raw_fd)
        file_stat = os.stat(lock_path)
        descriptor_stat = os.fstat(fd)
        if (file_stat.st_dev, file_stat.st_ino) != (descriptor_stat.st_dev, descriptor_stat.st_ino):
            raise OSError("lock descriptor mismatch")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, TypeError, ValueError):
        raise SystemExit("release_handoff_lock_unavailable")
observed = subprocess.run(
    ("git", "-C", str(repo), "rev-parse", "HEAD^{commit}"),
    check=True,
    capture_output=True,
    text=True,
    timeout=30,
).stdout.strip().lower()
if observed != expected:
    raise SystemExit("release_handoff_head_mismatch")
os.environ["FS_RELEASE_HANDOFF_LOCK_PID"] = str(os.getpid())
os.environ["FS_RELEASE_HANDOFF_LOCKS"] = json.dumps(inherited, sort_keys=True)
sys.path.insert(0, str(repo))
from core.entrypoints.cli.installer import run_pending_release_handoff

raise SystemExit(run_pending_release_handoff())
"""
_MIGRATION_CODES = ("release_migration_required",)
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *Password*https://x-access-token@github.com/*)
    IFS= read -r token < "$FS_GITHUB_RELEASE_TOKEN_FILE"
    printf '%s\\n' "$token"
    ;;
  *) exit 1 ;;
esac
"""


class InstallerReleaseHandoff:
    """Change the checkout once, then let only InstallerService change the runtime."""

    def __init__(
        self,
        *,
        repo_root: Path,
        state: InstallerStateStore,
        source_repository: str = DEFAULT_RELEASE_REPOSITORY,
        source_registries: str = "",
        git_runner: GitRunner | None = None,
        authenticated_git_runner: AuthenticatedGitRunner | None = None,
        installer_runner: InstallerRunner | None = None,
        transition_revalidator: TransitionRevalidator | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.state = state
        self.source_repository = validated_release_repository(source_repository)
        self.source_registries = str(source_registries or "").strip()
        self.transition_revalidator = transition_revalidator
        executor = SubprocessExecutor()
        self.github_credential = GitHubReleaseCredentialLoader(state.root)
        self.git_runner = git_runner or (
            lambda command: executor.run(command, cwd=self.repo_root, timeout_seconds=60)
        )
        self.authenticated_git_runner = authenticated_git_runner or (
            lambda command, environment: executor.run(
                command,
                cwd=self.repo_root,
                timeout_seconds=60,
                environment=environment,
            )
        )
        self.installer_runner = (
            (
                lambda command, environment, timeout, _inherited_fds: installer_runner(
                    command, environment, timeout
                )
            )
            if installer_runner is not None
            else lambda command, environment, timeout, inherited_fds: executor.run(
                command,
                cwd=self.repo_root,
                timeout_seconds=timeout,
                environment=environment,
                separate_stderr=True,
                inherited_fds=inherited_fds,
            )
        )

    def activate(
        self,
        release: ReleaseIdentity,
        *,
        timeout_seconds: int,
        held_locks: Mapping[str, int] | None = None,
        transition_binding: ReleaseTransitionBinding | None = None,
    ) -> ReleaseResult:
        with self.state.operation_lock(held_locks=held_locks) as inherited_locks:
            if (
                transition_binding is not None
                and transition_binding.release_repository != self.source_repository
            ):
                raise self._repository_mismatch()
            durable = self.state.load_release_handoff()
            durable_binding = self.state.load_release_transition_binding()
            durable, durable_binding = self._reconcile_terminal_handoff_locked(
                durable,
                durable_binding,
            )
            if (
                durable_binding is not None
                and durable_binding.release_repository != self.source_repository
            ):
                raise self._repository_mismatch()
            if durable_binding is not None:
                if durable_binding.target_release != release or (
                    transition_binding is not None and durable_binding != transition_binding
                ):
                    raise self._binding_conflict()
                if transition_binding is None:
                    installed = self.state.load_installed_release()
                    pending = self.state.load_pending_release()
                    terminal_reconciliation = (
                        durable is not None
                        and durable.target == release
                        and installed == release
                        and pending in (None, release)
                    )
                    if not terminal_reconciliation:
                        raise self._binding_conflict()
            elif transition_binding is not None:
                if self.transition_revalidator is None:
                    raise LifecycleError.stable_failure(
                        code="release_transition_revalidator_unavailable",
                        cause="Source transition origin cannot be revalidated.",
                        remediation="Repair the release runtime composition and retry.",
                        step="handoff",
                    )
                self.transition_revalidator(transition_binding)
            journal = self._prepare_checkout_locked(
                release,
                transition_binding=transition_binding,
            )
            if journal.phase == "installer_completed":
                return self._completed_without_child(release)
            outcome = self._run_installer(
                release,
                timeout_seconds=timeout_seconds,
                inherited_locks=inherited_locks,
            )
            if outcome.returncode != 0:
                error, log_path = self._child_error(outcome)
                if error["code"] in _MIGRATION_CODES:
                    self.state.save_release_handoff(
                        journal.model_copy(update={"phase": "migration_required"})
                    )
                    return ReleaseResult(
                        status="migration_required",
                        current_release=self.state.load_installed_release(),
                        current_version=(self.state.load_installed_release() or release).version,
                        target_release=release,
                        target_version=release.version,
                        detail=error["message"],
                        remediation=error["hint"]
                        or (
                            "Create a backup with `flow-steward backup create`, run "
                            "`flow-steward database upgrade --maintenance-confirmed --confirm`, "
                            "then repeat this release upgrade. Maintenance remains enabled."
                        ),
                        log_path=log_path,
                    )
                raise self._failed_child(
                    error=error,
                    log_path=log_path,
                )
            child_result = self._child_result(outcome)
            self._complete_install_locked(release)
        return self._completed(release, child_result)

    def install_exact(
        self,
        release: ReleaseIdentity,
        *,
        config: InstallConfig,
        explicit_fields: frozenset[str],
        timeout_seconds: int,
    ) -> InstallRunResult:
        """Install an exact official release or resume its durably reviewed transition."""
        with self.state.operation_lock() as inherited_locks:
            transition_binding = self._installer_transition_binding(release, config=config)
            self._prepare_checkout_locked(
                release,
                install_config=config,
                explicit_fields=explicit_fields,
                transition_binding=transition_binding,
            )
            self.state.clear_release_handoff_secret()
            outcome = self._run_installer(
                release,
                timeout_seconds=timeout_seconds,
                inherited_locks=inherited_locks,
            )
            if outcome.returncode != 0:
                raise self._failed(
                    "The private fresh-install handoff failed; durable pending state was preserved."
                )
            try:
                payload = json.loads(outcome.stdout)
                result = InstallRunResult.model_validate(payload["result"])
            except (KeyError, TypeError, ValueError) as exc:
                raise self._failed("The private installer returned an invalid result.") from exc
            secret_url = self.state.take_release_handoff_secret()
            if secret_url:
                result = result.model_copy(update={"bootstrap_setup_url": secret_url})
            self._complete_install_locked(release)
        return result

    def _installer_transition_binding(
        self, release: ReleaseIdentity, *, config: InstallConfig
    ) -> ReleaseTransitionBinding | None:
        """Reuse a reviewed transition only when every durable resume input agrees."""
        binding = self.state.load_release_transition_binding()
        if binding is None:
            return None
        if binding.release_repository != self.source_repository:
            raise self._repository_mismatch()
        journal = self.state.load_release_handoff()
        if (
            journal is None
            or journal.target != release
            or self.state.load_pending_release() != release
            or binding.target_release != release
            or binding.configuration_sha256 != _configuration_sha256(config)
        ):
            raise self._binding_conflict()
        return binding

    def prepare_checkout(
        self,
        release: ReleaseIdentity,
        *,
        install_config: InstallConfig | None = None,
        explicit_fields: frozenset[str] = frozenset(),
    ) -> ReleaseHandoffJournal:
        """Atomically bind pending state to the exact release checkout."""
        with self.state.operation_lock():
            return self._prepare_checkout_locked(
                release,
                install_config=install_config,
                explicit_fields=explicit_fields,
            )

    def _prepare_checkout_locked(
        self,
        release: ReleaseIdentity,
        *,
        install_config: InstallConfig | None = None,
        explicit_fields: frozenset[str] = frozenset(),
        transition_binding: ReleaseTransitionBinding | None = None,
    ) -> ReleaseHandoffJournal:
        journal = self.state.load_release_handoff()
        durable_binding = self.state.load_release_transition_binding()
        journal, durable_binding = self._reconcile_terminal_handoff_locked(
            journal,
            durable_binding,
        )
        if journal is not None and journal.phase == "installer_completed":
            installed = self.state.load_installed_release()
            pending = self.state.load_pending_release()
            if installed != journal.target or pending is not None:
                raise self._failed(
                    "Completed handoff does not match terminal durable release state."
                )
            if journal.target == release:
                self._verify_head(release)
                if durable_binding is not None:
                    self.state.clear_release_transition_binding(durable_binding)
                return journal
            self.state.clear_release_handoff()
            journal = None
        if journal is not None and journal.target != release:
            raise LifecycleError.stable_failure(
                code="release_handoff_target_conflict",
                cause=f"Release {journal.target.version} already has an unfinished handoff.",
                remediation=f"Resume release {journal.target.version} before selecting another.",
                step="handoff",
            )
        if durable_binding is not None and durable_binding.target_release != release:
            raise self._binding_conflict()
        if (
            durable_binding is not None
            and transition_binding is not None
            and durable_binding != transition_binding
        ):
            raise self._binding_conflict()
        if journal is not None and durable_binding is not None and transition_binding is None:
            raise self._binding_conflict()
        if (
            journal is not None
            and install_config is not None
            and journal.install_config not in (None, install_config)
        ):
            raise self._failed("Fresh-install handoff configuration changed while pending.")

        pending = self.state.load_pending_release()
        if journal is None:
            with self._verified_tag_source(release) as tag_source:
                self._preflight_checkout(release, tag_source=tag_source)
                if transition_binding is not None and durable_binding is None:
                    self.state.save_release_transition_binding(transition_binding)
                journal = ReleaseHandoffJournal(
                    target=release,
                    phase="prepared",
                    install_config=install_config,
                    explicit_fields=tuple(sorted(explicit_fields)),
                    release_repository=self.source_repository,
                    release_registries=self.source_registries,
                )
                self.state.save_release_handoff(journal)
                self.state.stage_pending_release(release)
                self.state.write_executable_text(self.state.root / _BOOTSTRAP_NAME, _BOOTSTRAP)
                self._import_verified_tag(release, tag_source=tag_source)
                self._checkout(release)
                journal = journal.model_copy(update={"phase": "checkout_applied"})
                self.state.save_release_handoff(journal)
                return journal
        else:
            if pending is None and journal.phase == "prepared":
                self.state.stage_pending_release(release)
                pending = release
            if pending != release:
                raise self._failed("Pending release does not match the durable handoff.")
            # A crash may occur after any journal phase is durable but before an
            # earlier bootstrap write reached disk. Re-materializing this private,
            # constant script is idempotent and keeps every nonterminal resume usable.
            self.state.write_executable_text(self.state.root / _BOOTSTRAP_NAME, _BOOTSTRAP)

        if journal.phase == "prepared":
            with self._verified_tag_source(release) as tag_source:
                self._preflight_checkout(release, tag_source=tag_source)
                self._import_verified_tag(release, tag_source=tag_source)
                self._checkout(release)
            journal = journal.model_copy(update={"phase": "checkout_applied"})
            self.state.save_release_handoff(journal)
        else:
            self._verify_head(release)
            if journal.phase == "migration_required":
                journal = journal.model_copy(update={"phase": "checkout_applied"})
                self.state.save_release_handoff(journal)
        return journal

    def _reconcile_terminal_handoff_locked(
        self,
        journal: ReleaseHandoffJournal | None,
        binding: ReleaseTransitionBinding | None,
    ) -> tuple[ReleaseHandoffJournal | None, ReleaseTransitionBinding | None]:
        if journal is None:
            return journal, binding
        installed = self.state.load_installed_release()
        pending = self.state.load_pending_release()
        if installed != journal.target or pending not in (None, installed):
            return journal, binding
        if pending == installed:
            self.state.clear_pending_release(installed)
        if journal.phase != "installer_completed":
            journal = journal.model_copy(update={"phase": "installer_completed"})
            self.state.save_release_handoff(journal)
        if binding is not None:
            self.state.clear_release_transition_binding(binding)
            binding = None
        return journal, binding

    @staticmethod
    def _binding_conflict() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_handoff_binding_conflict",
            cause="The source transition binding does not match the durable handoff.",
            remediation="Resume the exact reviewed transition before selecting another.",
            step="handoff",
        )

    @staticmethod
    def _repository_mismatch() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_source_repository_mismatch",
            cause=(
                "The source transition repository differs from the installer handoff repository."
            ),
            remediation="Run release check again and review a target from one repository.",
            step="handoff",
        )

    def _run_installer(
        self,
        release: ReleaseIdentity,
        *,
        timeout_seconds: int,
        inherited_locks: Mapping[str, int],
    ) -> ProcessOutcome:
        environment = self._installer_environment(release)
        environment["FS_RELEASE_HANDOFF_INHERITED_LOCKS"] = json.dumps(
            inherited_locks, sort_keys=True
        )
        return self.installer_runner(
            (
                sys.executable,
                str(self.state.root / _BOOTSTRAP_NAME),
                str(self.repo_root),
                release.commit_sha,
                str(self.state.root),
                str(self.repo_root / ".flow-steward"),
            ),
            environment,
            timeout_seconds,
            tuple(inherited_locks.values()),
        )

    def complete_install(self, release: ReleaseIdentity) -> None:
        """Publish the terminal handoff marker after InstallerService commits identity."""
        with self.state.operation_lock():
            self._complete_install_locked(release)

    def _complete_install_locked(self, release: ReleaseIdentity) -> None:
        journal = self.state.load_release_handoff()
        if journal is None or journal.target != release:
            raise self._failed("Release handoff journal disappeared before completion.")
        if self.state.load_installed_release() != release:
            raise self._failed(
                "Installer exited successfully without publishing the target release."
            )
        self.state.save_release_handoff(journal.model_copy(update={"phase": "installer_completed"}))
        binding = self.state.load_release_transition_binding()
        if binding is not None:
            self.state.clear_release_transition_binding(binding)

    def _preflight_checkout(
        self,
        release: ReleaseIdentity,
        *,
        tag_source: Path | None = None,
    ) -> None:
        for args in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
            outcome = self._git(*args)
            if outcome.returncode == 1:
                raise LifecycleError.stable_failure(
                    code="release_checkout_dirty",
                    cause="Tracked release files contain local modifications.",
                    remediation="Commit, stash, or revert tracked changes before upgrading.",
                    step="checkout_preflight",
                )
            if outcome.returncode != 0:
                raise self._failed("Git could not inspect tracked checkout changes.")
        untracked = self._nul_paths(
            self._git_checked("ls-files", "--others", "--exclude-standard", "-z")
        )
        target_paths = self._nul_paths(
            self._git_checked_at(
                tag_source or self.repo_root,
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                release.commit_sha,
            )
        )
        conflicts = sorted(untracked.intersection(target_paths))
        if conflicts:
            raise LifecycleError.stable_failure(
                code="release_checkout_untracked_conflict",
                cause="Untracked paths would be replaced by the target release: "
                + ", ".join(conflicts[:10]),
                remediation="Move those untracked files aside and repeat the upgrade.",
                step="checkout_preflight",
            )

    def _ensure_tag(self, release: ReleaseIdentity) -> None:
        with self._verified_tag_source(release) as tag_source:
            self._import_verified_tag(release, tag_source=tag_source)

    @contextmanager
    def _verified_tag_source(self, release: ReleaseIdentity) -> Iterator[Path | None]:
        ref = f"refs/tags/{release.tag}^{{commit}}"
        observed = self._git("rev-parse", "--verify", ref)
        if observed.returncode == 0:
            if observed.stdout.strip().lower() != release.commit_sha:
                raise self._tag_mismatch(release)
            yield None
            return
        with tempfile.TemporaryDirectory(prefix="flow-steward-release-tag-") as raw_directory:
            isolated_repository = Path(raw_directory) / "repository.git"
            isolated_repository.mkdir(mode=0o700)
            init_command = (
                "git",
                "-C",
                str(isolated_repository),
                "init",
                "--bare",
                "--template=",
            )
            init = self.git_runner(init_command)
            if init.returncode != 0:
                raise self._failed("Git preflight command failed: init.")
            self._fetch_exact_tag(release.tag, repository=isolated_repository)
            isolated_ref = self._git_at(
                isolated_repository,
                "rev-parse",
                "--verify",
                ref,
            )
            if (
                isolated_ref.returncode != 0
                or isolated_ref.stdout.strip().lower() != release.commit_sha
            ):
                raise self._tag_mismatch(release)
            yield isolated_repository

    @staticmethod
    def _tag_mismatch(release: ReleaseIdentity) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_tag_mismatch",
            cause=f"Local tag {release.tag} does not resolve to the published commit.",
            remediation="Repair the origin/tag mapping and repeat release preflight.",
            step="checkout_preflight",
        )

    def _import_verified_tag(
        self,
        release: ReleaseIdentity,
        *,
        tag_source: Path | None,
    ) -> None:
        if tag_source is None:
            return
        refspec = f"refs/tags/{release.tag}:refs/tags/{release.tag}"
        outcome = self._git("fetch", "--no-tags", str(tag_source), refspec)
        if outcome.returncode != 0:
            raise self._failed("Git preflight command failed: import verified tag.")
        observed = self._git("rev-parse", "--verify", f"refs/tags/{release.tag}^{{commit}}")
        if observed.returncode != 0 or observed.stdout.strip().lower() != release.commit_sha:
            raise self._tag_mismatch(release)

    def _fetch_exact_tag(self, tag: str, *, repository: Path) -> None:
        refspec = f"refs/tags/{tag}:refs/tags/{tag}"
        token = self.github_credential.load()
        if token is None:
            outcome = self.git_runner(
                (
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "--no-tags",
                    f"https://github.com/{self.source_repository}.git",
                    refspec,
                )
            )
        else:
            del token
            with self._github_askpass_environment() as environment:
                outcome = self._fetch_authenticated_tag(
                    refspec=refspec,
                    repository=repository,
                    environment=environment,
                )
        if outcome.returncode != 0:
            raise self._failed("Git preflight command failed: fetch.")

    def _fetch_authenticated_tag(
        self,
        *,
        refspec: str,
        repository: Path,
        environment: Mapping[str, str],
    ) -> ProcessOutcome:
        return self.authenticated_git_runner(
            (
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "http.followRedirects=false",
                "-C",
                str(repository),
                "fetch",
                "--no-tags",
                f"https://x-access-token@github.com/{self.source_repository}.git",
                refspec,
            ),
            environment,
        )

    @contextmanager
    def _github_askpass_environment(self) -> Iterator[dict[str, str]]:
        self.state.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".github-release-askpass-",
            dir=self.state.root,
            text=True,
        )
        path = Path(raw_path)
        try:
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the askpass helper contains a credential path and must remain owner-only.
            os.fchmod(descriptor, 0o700)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(_ASKPASS_SCRIPT)
                handle.flush()
                os.fsync(handle.fileno())
            yield {
                "GIT_ASKPASS": str(path),
                "GIT_ASKPASS_REQUIRE": "force",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "FS_GITHUB_RELEASE_TOKEN_FILE": str(self.github_credential.path),
            }
        finally:
            path.unlink(missing_ok=True)

    def _checkout(self, release: ReleaseIdentity) -> None:
        self._git_checked(
            "checkout",
            "--detach",
            "--no-recurse-submodules",
            release.commit_sha,
        )
        self._verify_head(release)

    def _verify_head(self, release: ReleaseIdentity) -> None:
        observed = self._git_checked("rev-parse", "HEAD^{commit}").strip().lower()
        if observed != release.commit_sha:
            raise self._failed("Checked-out commit does not match the pending release.")

    def _installer_environment(self, release: ReleaseIdentity) -> dict[str, str]:
        role_variables = {role.role: role.environment_variable for role in all_release_roles()}
        environment = dict(os.environ)
        environment["FS_RELEASE_HANDOFF_STATE"] = str(self.state.release_handoff_path)
        journal = self.state.load_release_handoff()
        if journal is not None:
            if journal.release_repository:
                environment["FS_RELEASE_REPOSITORY"] = journal.release_repository
            if journal.release_registries:
                environment["FS_RELEASE_REGISTRIES"] = journal.release_registries
        for image in release.images:
            try:
                variable = role_variables[image.role]
            except KeyError as exc:
                raise self._failed(f"Release contains unknown product role {image.role}.") from exc
            environment[variable] = image.registry_ref
        return environment

    def _child_result(self, outcome: ProcessOutcome) -> InstallRunResult:
        try:
            payload = json.loads(outcome.stdout)
            if not isinstance(payload, dict) or set(payload) != {"result"}:
                raise ValueError("unexpected private installer success envelope")
            return InstallRunResult.model_validate(payload["result"])
        except (TypeError, ValueError) as exc:
            raise self._failed("The private installer returned an invalid success result.") from exc

    def _child_error(self, outcome: ProcessOutcome) -> tuple[dict[str, str], str | None]:
        try:
            payload = json.loads(outcome.stdout)
            if not isinstance(payload, dict) or not set(payload).issubset({"error", "log_path"}):
                raise ValueError("unexpected private installer failure envelope")
            error = payload.get("error")
            if not isinstance(error, dict) or not set(error).issubset(
                {"code", "message", "hint", "retryable"}
            ):
                raise ValueError("unexpected private installer error")
            code = str(error.get("code") or "")
            if not code:
                raise ValueError("private installer error has no code")
            parsed = {
                "code": code,
                "message": str(error.get("message") or "Private installer failed."),
                "hint": str(error.get("hint") or ""),
            }
            raw_log_path = payload.get("log_path")
            log_path = str(raw_log_path) if isinstance(raw_log_path, str) and raw_log_path else None
            return parsed, log_path
        except (TypeError, ValueError) as exc:
            raise self._failed("The private installer returned an invalid failure result.") from exc

    @staticmethod
    def _failed_child(*, error: Mapping[str, str], log_path: str | None) -> LifecycleError:
        return LifecycleError(
            code="release_handoff_failed",
            summary="The private installer handoff failed.",
            cause=error["message"],
            remediation=error["hint"] or "Correct the reported cause and repeat the upgrade.",
            retryable=True,
            step="handoff",
            exit_code=ExitCode.STABLE_FAILURE,
            log_path=log_path,
            details={"installer_error_code": error["code"]},
        )

    def _git(self, *args: str) -> ProcessOutcome:
        return self.git_runner(("git", "-C", str(self.repo_root), *args))

    def _git_at(self, repository: Path, *args: str) -> ProcessOutcome:
        return self.git_runner(("git", "-C", str(repository), *args))

    def _git_checked(self, *args: str) -> str:
        outcome = self._git(*args)
        if outcome.returncode != 0:
            raise self._failed(f"Git preflight command failed: {args[0]}.")
        return outcome.stdout

    def _git_checked_at(self, repository: Path, *args: str) -> str:
        outcome = self._git_at(repository, *args)
        if outcome.returncode != 0:
            raise self._failed(f"Git preflight command failed: {args[0]}.")
        return outcome.stdout

    @staticmethod
    def _nul_paths(value: str) -> set[str]:
        return {item for item in value.split("\0") if item}

    @staticmethod
    def _completed(release: ReleaseIdentity, child: InstallRunResult) -> ReleaseResult:
        return ReleaseResult(
            status="upgraded",
            current_version=release.version,
            current_release=release,
            target_version=release.version,
            target_release=release,
            remediation="Verify the release, then disable maintenance explicitly.",
            detail="; ".join(child.warnings),
            operation_id=child.operation_id,
            log_path=child.log_path,
            next_steps=("flow-steward maintenance disable --confirm",),
        )

    @staticmethod
    def _completed_without_child(release: ReleaseIdentity) -> ReleaseResult:
        return ReleaseResult(
            status="upgraded",
            current_version=release.version,
            current_release=release,
            target_version=release.version,
            target_release=release,
            remediation="Verify the release, then disable maintenance explicitly.",
            next_steps=("flow-steward maintenance disable --confirm",),
        )

    @staticmethod
    def _failed(cause: str) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_handoff_failed",
            cause=cause,
            remediation="Correct the reported cause and repeat the same release upgrade.",
            step="handoff",
        )


__all__ = ["InstallerReleaseHandoff"]
