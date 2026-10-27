"""Installed-stack Clone adapters composed from retained lifecycle owners."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.application.lifecycle.contracts import (
    ComposeTarget,
    LifecycleOperationKind,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.release.contracts import ReleaseIdentity
from core.infrastructure.clone.filesystem_publication import (
    _copy_file_for_target,
    _tree_content_snapshot,
)
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_READONLY_SOURCE_MOUNT = "/source:ro"
_TARGET_MOUNT = "/target"
# Staging directory inside the transfer container for published Clone sources.
_CAPTURE_SOURCE_DIR = "/capture/source"
# Parent of the private capture scratch directory. It is deliberately the
# Linux filesystem /tmp rather than TMPDIR (which can be host-backed and reject
# the source modes Docker replays). Safe on a shared directory: it is only ever
# passed as ``dir=`` to tempfile.TemporaryDirectory, whose mkdtemp creates an
# unpredictable owner-only (0700) child; nothing uses a fixed name beneath it.
_LINUX_CAPTURE_SCRATCH_ROOT = "/tmp"  # nosec B108  # NOSONAR
_VOLUME_METADATA_MODULE = "core.infrastructure.clone.volume_metadata"


class CloneInstallationStateGuard:
    """Read-only admission guard backed by the canonical installer state owner."""

    def __init__(
        self,
        state: InstallerStateStore,
        *,
        lifecycle_state: LifecycleStateStore | None = None,
    ) -> None:
        self._state = state
        self._lifecycle_state = lifecycle_state

    def require_ready(self, operation_id: str | None = None) -> None:
        journal = self._state.load_journal()
        handoff = self._state.load_release_handoff()
        unfinished = (
            self._state.has_active_operation()
            or (journal is not None and journal.status != "completed")
            or self._state.load_pending_release() is not None
            or (handoff is not None and handoff.phase != "installer_completed")
        )
        if unfinished and not self._matching_clone_resume(operation_id):
            raise LifecycleError.recovery_required(
                code="clone_installer_operation_unfinished",
                cause="An installer or release handoff operation is unfinished.",
                remediation="Resume or recover the installer operation before using Clone.",
                step="preflight",
                recovery_command="./install.sh",
            )
        if self._state.load_installed_release() is None:
            raise LifecycleError.stable_failure(
                code="clone_installed_release_required",
                cause="Clone requires a completely installed target release.",
                remediation="Complete or repair the installation before using Clone.",
                step="preflight",
            )

    def _matching_clone_resume(self, operation_id: str | None) -> bool:
        if not operation_id or self._lifecycle_state is None or self._state.has_active_operation():
            return False
        journals = self._lifecycle_state.unfinished_journals()
        if len(journals) != 1:
            return False
        journal = journals[0]
        if (
            journal.operation_id != operation_id
            or journal.kind is not LifecycleOperationKind.CLONE_RESTORE
        ):
            return False
        try:
            expected = ReleaseIdentity.model_validate(journal.parameters["release"])
        except (KeyError, TypeError, ValueError):
            return False
        pending = self._state.load_pending_release()
        handoff = self._state.load_release_handoff()
        return pending in (None, expected) and (handoff is None or handoff.target == expected)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class ComposeCloneServiceControl:
    """Observe and change only named services from one derived Compose target."""

    def __init__(self, *, docker: DockerLifecycleGateway, target: ComposeTarget) -> None:
        self._docker = docker
        self._target = target

    def service_states(self, names: tuple[str, ...]) -> dict[str, bool]:
        observed = self._docker.status(self._target).services
        return {
            name: any(item.service == name and item.state.lower() == "running" for item in observed)
            for name in names
        }

    def stop(self, names: tuple[str, ...]) -> None:
        if names:
            self._docker.stop(
                self._target,
                names,
                timeout_seconds=120,
                output_sink=lambda _line: None,
                heartbeat=lambda _line: None,
            )

    def start(self, names: tuple[str, ...]) -> None:
        if names:
            self._docker.start(
                self._target,
                names,
                timeout_seconds=120,
                output_sink=lambda _line: None,
                heartbeat=lambda _line: None,
            )


class DockerCloneVolumeTransfer:
    """Copy only allowlisted Compose volumes through an existing digest-pinned image."""

    _VOLUME_KEYS: ClassVar[dict[str, str]] = {
        "compact-data": "fs-compact-data",
        "rabbitmq": "fs-rabbitmq",
        "seaweedfs": "seaweedfs-data",
    }

    def __init__(
        self,
        *,
        project_name: str,
        tool_image: str,
        runner: ProcessRunner | None = None,
    ) -> None:
        self._project_name = project_name
        self._tool_image = tool_image
        self._runner = runner or subprocess.run

    def capture(self, name: str, destination: Path) -> tuple[CloneEntry, ...]:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Clone volume capture destination already exists")
        metadata = self._capture_metadata(name, archive_root=destination.name)
        self._capture_content(name, destination)
        return metadata

    def capture_compact_filesystems(
        self,
        destination: Path,
        items: tuple[tuple[CloneDataClass, str], ...],
        *,
        durable_state_only: bool = False,
    ) -> tuple[CloneEntry, ...]:
        if durable_state_only:
            self.validate_compact_filesystems(items, durable_state_only=True)
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise FileExistsError("Compact Clone capture destination is invalid")
        created_destination = not destination.exists()
        destination.mkdir(mode=0o700, exist_ok=True)
        roots = tuple(archive_root for _data_class, archive_root in items)
        if any(
            (destination / archive_root).exists() or (destination / archive_root).is_symlink()
            for archive_root in roots
        ):
            raise FileExistsError("Compact Clone capture root already exists")
        container = self._create_transfer(
            "compact-data",
            mount=_READONLY_SOURCE_MOUNT,
            capture_scratch=True,
            durable_state_only=durable_state_only,
        )
        try:
            self._run(("docker", "start", container))
            outcome = self._run(
                (
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "python",
                    "-m",
                    "core.infrastructure.clone.compact_volume_capture",
                    *(("--durable-state-only",) if durable_state_only else ()),
                ),
                input_text=json.dumps(
                    [
                        {"data_class": data_class.value, "archive_root": archive_root}
                        for data_class, archive_root in items
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            raw = json.loads(outcome.stdout)
            if not isinstance(raw, list):
                raise ValueError("Compact Clone capture metadata is invalid")
            entries = tuple(CloneEntry.model_validate(item) for item in raw)
            for archive_root in roots:
                # Docker replays source modes, which host-backed staging may
                # reject. Keep those modes on Linux scratch only; authoritative
                # source metadata remains in entries, not in transport files.
                # mkdtemp provides unpredictable names and owner-only mode;
                # keep this secure temporary transport on the Linux filesystem.
                with tempfile.TemporaryDirectory(
                    prefix="fs-clone-capture-",
                    dir=_LINUX_CAPTURE_SCRATCH_ROOT,
                ) as scratch:
                    captured = Path(scratch) / archive_root
                    self._run(
                        (
                            "docker",
                            "cp",
                            f"{container}:/capture/{archive_root}",
                            str(captured),
                        )
                    )
                    _copy_private_capture_entry(captured, destination / archive_root)
            return entries
        except Exception:
            for archive_root in roots:
                shutil.rmtree(destination / archive_root, ignore_errors=True)
            if created_destination:
                with suppress(OSError):
                    destination.rmdir()
            raise
        finally:
            self._remove(container)

    def validate_compact_filesystems(
        self,
        items: tuple[tuple[CloneDataClass, str], ...],
        *,
        durable_state_only: bool = False,
    ) -> None:
        if durable_state_only and items != ((CloneDataClass.DURABLE_STATE, "state"),):
            raise ValueError("Compact mixed-placement capture requires state-only input")
        # Inspect original components before subpath resolution: Docker can resolve
        # an internal-volume symlink and hide it behind the eventual mount root.
        # This helper only validates the fixed subtree; it cannot write the volume.
        container = self._create_transfer("compact-data", mount=_READONLY_SOURCE_MOUNT)
        try:
            self._run(("docker", "start", container))
            self._run(
                (
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "python",
                    "-m",
                    "core.infrastructure.clone.compact_volume_capture",
                    "--validate-only",
                    *(("--durable-state-only",) if durable_state_only else ()),
                ),
                input_text=json.dumps(
                    [
                        {"data_class": data_class.value, "archive_root": archive_root}
                        for data_class, archive_root in items
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        finally:
            self._remove(container)

    def replace(self, name: str, source: Path, entries: tuple[CloneEntry, ...]) -> None:
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Clone volume source must be a real directory")
        container = self._create_transfer(name, mount=_TARGET_MOUNT)
        try:
            self._run(("docker", "start", container))
            root_owner = self._run(
                ("docker", "exec", container, "stat", "-c", "%u:%g", _TARGET_MOUNT)
            ).stdout.strip()
            roots = tuple(entry for entry in entries if "/" not in entry.path)
            expected_root = roots[0] if len(roots) == 1 else None
            if expected_root is None or root_owner != f"{expected_root.uid}:{expected_root.gid}":
                raise RuntimeError("Clone volume role ownership does not match the target")
            self._run(
                (
                    "docker",
                    "exec",
                    container,
                    "find",
                    _TARGET_MOUNT,
                    "-mindepth",
                    "1",
                    "-delete",
                )
            )
            self._run(("docker", "cp", "--archive", f"{source}/.", f"{container}:/target"))
            self._run(
                (
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "python",
                    "-m",
                    _VOLUME_METADATA_MODULE,
                ),
                input_text=json.dumps(
                    [entry.model_dump(mode="json") for entry in entries],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        finally:
            self._remove(container)

    def matches(self, name: str, source: Path, entries: tuple[CloneEntry, ...]) -> bool:
        if source.is_symlink() or not source.is_dir():
            return False
        with tempfile.TemporaryDirectory(prefix="clone-volume-verify-") as temporary:
            captured = Path(temporary) / "captured"
            self._capture_content(name, captured)
            return _tree_content_snapshot(source) == _tree_content_snapshot(
                captured
            ) and self._metadata_matches(name, entries)

    def _capture_content(self, name: str, destination: Path) -> None:
        destination.mkdir(mode=0o700)
        container = self._create_transfer(name, mount=_READONLY_SOURCE_MOUNT)
        try:
            self._run(("docker", "start", container))
            self._run(("docker", "cp", f"{container}:/source/.", str(destination)))
        finally:
            self._remove(container)

    def _capture_metadata(
        self,
        name: str,
        *,
        archive_root: str,
    ) -> tuple[CloneEntry, ...]:
        data_class = {
            "rabbitmq": CloneDataClass.RABBITMQ,
            "seaweedfs": CloneDataClass.BUNDLED_STORAGE,
        }.get(name)
        if data_class is None:
            raise ValueError("Unsupported Clone metadata volume")
        container = self._create_transfer(name, mount=_READONLY_SOURCE_MOUNT)
        try:
            self._run(("docker", "start", container))
            outcome = self._run(
                (
                    "docker",
                    "exec",
                    container,
                    "python",
                    "-m",
                    _VOLUME_METADATA_MODULE,
                    "--capture",
                    "/source",
                    archive_root,
                    data_class.value,
                )
            )
            raw = json.loads(outcome.stdout)
            if not isinstance(raw, list):
                raise ValueError("Clone volume metadata output is invalid")
            return tuple(CloneEntry.model_validate(item) for item in raw)
        finally:
            self._remove(container)

    def publish_compact_data(
        self,
        staged_root: Path,
        entries: tuple[CloneEntry, ...],
        *,
        plan_digest: str,
        verify: bool = False,
        durable_state_only: bool = False,
        validate_only: bool = False,
    ) -> bool:
        if verify and validate_only:
            raise ValueError("Compact publication validation and verification are separate modes")
        staged = Path(staged_root)
        if staged.is_symlink() or not staged.is_dir():
            raise ValueError("Compact Clone staging must be a real directory")
        authenticated_roots = {entry.path for entry in entries if "/" not in entry.path}
        required_roots = {"state"} if durable_state_only else {"extensions", "state"}
        if not required_roots.issubset(authenticated_roots):
            raise ValueError("Compact Clone staged filesystem metadata is incomplete")
        if durable_state_only:
            if any(
                entry.data_class is not CloneDataClass.DURABLE_STATE
                or not (entry.path == "state" or entry.path.startswith("state/"))
                for entry in entries
            ):
                raise ValueError("Compact mixed-placement publication requires state-only metadata")
            self.validate_compact_filesystems(
                ((CloneDataClass.DURABLE_STATE, "state"),), durable_state_only=True
            )
        compact_roots = tuple(
            root_name
            for root_name in ("artifacts", "extensions", "state")
            if root_name in authenticated_roots
        )
        container = self._create_transfer(
            "compact-data",
            mount=f"{_TARGET_MOUNT}:ro" if validate_only else _TARGET_MOUNT,
            capture_scratch=True,
            durable_state_only=durable_state_only,
        )
        try:
            self._run(("docker", "start", container))
            self._run(("docker", "exec", container, "mkdir", "-m", "0700", _CAPTURE_SOURCE_DIR))
            for root_name in compact_roots:
                source = staged / root_name
                if source.is_symlink() or not source.is_dir():
                    raise ValueError("Compact Clone staged filesystem root is invalid")
                self._run(
                    (
                        "docker",
                        "cp",
                        str(source),
                        f"{container}:{_CAPTURE_SOURCE_DIR}/{root_name}",
                    )
                )
            command = [
                "docker",
                "exec",
                "-i",
                container,
                "python",
                "-m",
                "core.infrastructure.clone.compact_volume_publication",
                "--source",
                _CAPTURE_SOURCE_DIR,
                "--plan-digest",
                plan_digest,
            ]
            if verify:
                command.append("--verify")
            if validate_only:
                command.append("--validate-only")
            if durable_state_only:
                command.append("--durable-state-only")
            self._run(
                tuple(command),
                input_text=json.dumps(
                    [entry.model_dump(mode="json") for entry in entries],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return True
        except RuntimeError:
            if verify:
                return False
            raise
        finally:
            self._remove(container)

    def _metadata_matches(self, name: str, entries: tuple[CloneEntry, ...]) -> bool:
        container = self._create_transfer(name, mount=_READONLY_SOURCE_MOUNT)
        try:
            self._run(("docker", "start", container))
            self._run(
                (
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "python",
                    "-m",
                    _VOLUME_METADATA_MODULE,
                    "--verify",
                    "/source",
                ),
                input_text=json.dumps(
                    [entry.model_dump(mode="json") for entry in entries],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return True
        except RuntimeError:
            return False
        finally:
            self._remove(container)

    def publish_host_filesystem(
        self,
        source: Path,
        target: Path,
        entries: tuple[CloneEntry, ...],
        *,
        plan_digest: str,
        owner: tuple[int, int],
        operation_id: str | None = None,
        verify: bool = False,
        validate_only: bool = False,
    ) -> bool:
        """Publish through an exact reviewed host-root bind, never a parent mount."""
        if verify and validate_only:
            raise ValueError("Host publication validation and verification are separate modes")
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Host Clone staging must be a real directory")
        # Host roots may be external to the project and invisible in this CLI.
        # --mount refuses absent host paths rather than creating them as -v does.
        if (
            not target.is_absolute()
            or target == Path(target.anchor)
            or ".." in target.parts
            or any(character in str(target) for character in (",", "\n", "\r", "\x00"))
        ):
            raise ValueError("Host Clone target root is invalid")
        outcome = self._run(
            (
                "docker",
                "create",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--label",
                f"com.flow-steward.clone.project={self._project_name}",
                "--label",
                f"com.flow-steward.clone.plan={plan_digest}",
                "--label",
                f"com.flow-steward.clone.operation={operation_id or plan_digest}",
                "--mount",
                "type=volume,destination=/capture,volume-nocopy",
                "--mount",
                f"type=bind,source={target},destination=/target{',readonly' if verify or validate_only else ''}",
                "--entrypoint",
                "sh",
                self._tool_image,
                "-c",
                "exec sleep 600",
            )
        )
        container = outcome.stdout.strip()
        if not container:
            raise RuntimeError("Docker did not return a host Clone transfer container id")
        try:
            self._run(("docker", "start", container))
            self._run(("docker", "exec", container, "mkdir", "-m", "0700", _CAPTURE_SOURCE_DIR))
            self._run(("docker", "cp", f"{source}/.", f"{container}:{_CAPTURE_SOURCE_DIR}"))
            command = [
                "docker",
                "exec",
                "-i",
                container,
                "python",
                "-m",
                "core.infrastructure.clone.host_directory_publication",
                "--plan-digest",
                plan_digest,
                "--uid",
                str(owner[0]),
                "--gid",
                str(owner[1]),
            ]
            if verify:
                command.append("--verify")
            if validate_only:
                command.append("--validate-only")
            self._run(
                command,
                input_text=json.dumps([entry.model_dump(mode="json") for entry in entries]),
            )
            return True
        except RuntimeError:
            if verify:
                return False
            raise
        finally:
            # Cleanup failure must invalidate verification, not leave sensitive
            # anonymous scratch behind while reporting a green postcondition.
            self._run(("docker", "rm", "--force", "--volumes", container))

    def _create_transfer(
        self,
        name: str,
        *,
        mount: str,
        capture_scratch: bool = False,
        durable_state_only: bool = False,
    ) -> str:
        if durable_state_only and name != "compact-data":
            raise ValueError("Compact durable subpath requires the Compact data volume")
        volume = self._resolve_volume(name)
        volume_mount = ("-v", f"{volume}:{mount}")
        if durable_state_only:
            destination = mount.split(":", 1)[0]
            read_only = ",readonly" if mount.endswith(":ro") else ""
            volume_mount = (
                "--mount",
                f"type=volume,source={volume},destination={destination}/state/.flow-steward,"
                f"volume-subpath=state/.flow-steward,volume-nocopy{read_only}",
            )
        scratch = (
            ("--mount", "type=volume,destination=/capture,volume-nocopy") if capture_scratch else ()
        )
        outcome = self._run(
            (
                "docker",
                "create",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                *scratch,
                *volume_mount,
                "--entrypoint",
                "sh",
                self._tool_image,
                "-c",
                "exec sleep 600",
            )
        )
        container = outcome.stdout.strip()
        if not container:
            raise RuntimeError("Docker did not return a Clone transfer container id")
        return container

    def _resolve_volume(self, name: str) -> str:
        try:
            volume_key = self._VOLUME_KEYS[name]
        except KeyError as exc:
            raise ValueError("Unsupported Clone volume") from exc
        outcome = self._run(
            (
                "docker",
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={self._project_name}",
                "--filter",
                f"label=com.docker.compose.volume={volume_key}",
                "--format",
                "{{.Name}}",
            )
        )
        names = tuple(line.strip() for line in outcome.stdout.splitlines() if line.strip())
        if len(names) != 1:
            raise RuntimeError("Clone requires exactly one matching project-owned volume")
        return names[0]

    def _remove(self, container: str) -> None:
        self._runner(
            ["docker", "rm", "--force", "--volumes", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            outcome = self._runner(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                timeout=600,
                input=input_text,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Clone Docker operation could not be started") from exc
        if outcome.returncode != 0:
            raise RuntimeError("Clone Docker operation failed")
        return outcome


def _copy_private_capture_entry(source: Path, destination: Path) -> None:
    """Copy transport content without applying source ownership or modes."""
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        os.symlink(os.readlink(source), destination)
    elif stat.S_ISDIR(metadata.st_mode):
        destination.mkdir(mode=0o700)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_private_capture_entry(child, destination / child.name)
    elif stat.S_ISREG(metadata.st_mode):
        _copy_file_for_target(source, destination, mode=0o600)
    else:
        raise ValueError("Compact Clone capture contains an unsupported entry")


__all__ = [
    "CloneInstallationStateGuard",
    "ComposeCloneServiceControl",
    "DockerCloneVolumeTransfer",
]
