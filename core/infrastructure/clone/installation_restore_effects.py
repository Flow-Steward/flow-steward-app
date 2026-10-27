"""Installed Clone restore effects, postconditions, and target readiness."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.application.clone.planning import PreparedCloneRestore
from core.application.clone.runtime_policy import uses_compact_durable_state_transfer
from core.application.lifecycle.backup_service import BackupService
from core.application.lifecycle.preservation_service import (
    PreservationManifest,
    PreservationService,
)
from core.infrastructure.clone.filesystem_publication import (
    _durable_children_match,
    _replace_durable_children,
    _require_real_nested_shell,
)

if TYPE_CHECKING:
    from core.infrastructure.clone.installation_gateway import (
        ComposeCloneServiceControl,
        DockerCloneVolumeTransfer,
    )

_TARGET_BEFORE_PRESERVATION_FILE = "target-before-preservation.json"
_TARGET_READINESS_TIMEOUT_SECONDS = 120.0
_TARGET_READINESS_POLL_SECONDS = 0.5
_LOGGER = logging.getLogger("core.infrastructure.clone.installation_gateway")

RestoreEffect = Callable[[PreparedCloneRestore], None]
RestorePostcondition = Callable[[PreparedCloneRestore], bool]


class InstalledCloneRestoreEffects:
    """Production effects for every declared destructive restore phase."""

    def __init__(
        self,
        *,
        operation_root: Path,
        services: ComposeCloneServiceControl,
        service_names: tuple[str, ...],
        volumes: DockerCloneVolumeTransfer,
        target_roots: Mapping[CloneDataClass, Path],
        backup: BackupService,
        import_secrets: Callable[[Mapping[str, str]], None],
        regenerate_configuration: Callable[[PreparedCloneRestore], None],
        activate_release: Callable[[PreparedCloneRestore], None],
        verify_database: Callable[[PreparedCloneRestore], bool],
        secrets_match: Callable[[PreparedCloneRestore], bool],
        configuration_matches: Callable[[PreparedCloneRestore], bool],
        release_activated: Callable[[PreparedCloneRestore], bool],
        verify_target: Callable[[PreparedCloneRestore], None],
        readiness_probe: Callable[[PreparedCloneRestore, float], None] | None = None,
        compact_data_volume: bool = False,
        preservation: PreservationService | None = None,
        capture_preservation: Callable[[PreparedCloneRestore, str], PreservationManifest]
        | None = None,
    ) -> None:
        self._operation_root = Path(operation_root)
        self._services = services
        self._service_names = service_names
        self._volumes = volumes
        self._target_roots = {key: Path(value) for key, value in target_roots.items()}
        self._backup = backup
        self._import_secrets = import_secrets
        self._regenerate_configuration = regenerate_configuration
        self._activate_release = activate_release
        self._verify_database = verify_database
        self._secrets_match = secrets_match
        self._configuration_matches = configuration_matches
        self._release_activated = release_activated
        self._verify_target = verify_target
        self._readiness_probe = readiness_probe
        self._compact_data_volume = compact_data_volume
        self._preservation = preservation
        self._capture_preservation = capture_preservation

    def handlers(self) -> dict[str, RestoreEffect]:
        return {
            "persist_plan": self.persist_plan,
            "stop_target": self.stop_target,
            "replace_filesystems": self.replace_filesystems,
            "restore_database": self.restore_database,
            "restore_service_state": self.restore_service_state,
            "restore_identity": self.restore_identity,
            "regenerate_configuration": self._regenerate_configuration,
            "activate_release": self._activate_release,
            "start_maintenance": self.start_maintenance,
            "verify_restored_target": self._verify_restored_target,
        }

    def postconditions(self) -> dict[str, RestorePostcondition]:
        return {
            "persist_plan": self.plan_persisted,
            "stop_target": self.target_stopped,
            "replace_filesystems": self.filesystems_replaced,
            "restore_database": self._verify_database,
            "restore_service_state": self.service_state_restored,
            "restore_identity": self._secrets_match,
            "regenerate_configuration": self._configuration_matches,
            "activate_release": self._release_activated,
            "start_maintenance": self.target_started,
            "verify_restored_target": self.target_verified,
        }

    def persist_plan(self, prepared: PreparedCloneRestore) -> None:
        root = self._operation_root / prepared.operation_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / "prepared.json"
        temporary = root / ".prepared.partial"
        payload = prepared.model_dump_json(exclude={"staged_root"}).encode()
        with temporary.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if self._capture_preservation is not None:
            before = self._capture_preservation(prepared, "before")
            self._write_owner_only_json(root / _TARGET_BEFORE_PRESERVATION_FILE, before.to_dict())

    def plan_persisted(self, prepared: PreparedCloneRestore) -> bool:
        path = self._operation_root / prepared.operation_id / "prepared.json"
        if path.is_symlink() or not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if payload.get("plan", {}).get("plan_digest") != prepared.plan.plan_digest:
            return False
        if self._capture_preservation is not None:
            preservation_path = path.parent / _TARGET_BEFORE_PRESERVATION_FILE
            if preservation_path.is_symlink() or not preservation_path.is_file():
                return False
        return True

    def stop_target(self, _prepared: PreparedCloneRestore) -> None:
        running = self._services.service_states(self._service_names)
        self._services.stop(tuple(name for name, value in running.items() if value))

    def target_stopped(self, _prepared: PreparedCloneRestore) -> bool:
        return not any(self._services.service_states(self._service_names).values())

    def replace_filesystems(self, prepared: PreparedCloneRestore) -> None:
        staged = Path(prepared.staged_root)
        if self._compact_data_volume:
            self._volumes.publish_compact_data(
                staged,
                prepared.manifest.entries,
                plan_digest=prepared.plan.plan_digest,
            )
            return
        mixed_state = uses_compact_durable_state_transfer(
            prepared.plan.retained_shell.configuration
        )
        if mixed_state:
            self._volumes.publish_compact_data(
                staged,
                _volume_entries(prepared, "state"),
                plan_digest=prepared.plan.plan_digest,
                durable_state_only=True,
                validate_only=True,
            )
        durable = None if mixed_state else self._target_roots.get(CloneDataClass.DURABLE_STATE)
        if durable is not None:
            _require_real_nested_shell(staged / "state", durable)
        owner = (
            prepared.plan.retained_shell.host_uid,
            prepared.plan.retained_shell.host_gid,
        )
        host_trees: list[tuple[Path, Path, tuple[CloneEntry, ...]]] = []
        for data_class, archive_root in (
            (CloneDataClass.LOCAL_ARTIFACTS, "artifacts"),
            (CloneDataClass.EXTENSIONS, "extensions"),
        ):
            host_root = prepared.plan.retained_shell.filesystem_roots.get(data_class.value)
            source = staged / archive_root
            if source.is_dir():
                if not host_root:
                    raise ValueError("Host Clone target filesystem identity is missing")
                host_trees.append(
                    (source, Path(host_root), _volume_entries(prepared, archive_root))
                )
        for source, target, entries in host_trees:
            self._volumes.publish_host_filesystem(
                source,
                target,
                entries,
                plan_digest=prepared.plan.plan_digest,
                owner=owner,
                operation_id=prepared.operation_id,
                validate_only=True,
            )
        for source, target, entries in host_trees:
            self._volumes.publish_host_filesystem(
                source,
                target,
                entries,
                plan_digest=prepared.plan.plan_digest,
                owner=owner,
                operation_id=prepared.operation_id,
            )
        if durable is not None:
            _replace_durable_children(
                staged / "state",
                durable,
                publication_modes=_publication_modes(prepared, "state"),
                owner=owner,
            )
        if mixed_state:
            self._volumes.publish_compact_data(
                staged,
                _volume_entries(prepared, "state"),
                plan_digest=prepared.plan.plan_digest,
                durable_state_only=True,
            )

    def filesystems_replaced(self, prepared: PreparedCloneRestore) -> bool:
        staged = Path(prepared.staged_root)
        if self._compact_data_volume:
            return self._volumes.publish_compact_data(
                staged,
                prepared.manifest.entries,
                plan_digest=prepared.plan.plan_digest,
                verify=True,
            )
        owner = (
            prepared.plan.retained_shell.host_uid,
            prepared.plan.retained_shell.host_gid,
        )
        for data_class, archive_root in (
            (CloneDataClass.LOCAL_ARTIFACTS, "artifacts"),
            (CloneDataClass.EXTENSIONS, "extensions"),
        ):
            host_root = prepared.plan.retained_shell.filesystem_roots.get(data_class.value)
            source = staged / archive_root
            if source.is_dir() and (
                not host_root
                or not self._volumes.publish_host_filesystem(
                    source,
                    Path(host_root),
                    _volume_entries(prepared, archive_root),
                    plan_digest=prepared.plan.plan_digest,
                    owner=owner,
                    operation_id=prepared.operation_id,
                    verify=True,
                )
            ):
                return False
        durable = self._target_roots.get(CloneDataClass.DURABLE_STATE)
        if uses_compact_durable_state_transfer(prepared.plan.retained_shell.configuration):
            return self._volumes.publish_compact_data(
                staged,
                _volume_entries(prepared, "state"),
                plan_digest=prepared.plan.plan_digest,
                verify=True,
                durable_state_only=True,
            )
        source_durable = staged / "state"
        return durable is None or _durable_children_match(
            source_durable,
            durable,
            publication_modes=_publication_modes(prepared, "state"),
            owner=owner,
        )

    def restore_database(self, prepared: PreparedCloneRestore) -> None:
        self._backup.restore_clone_dump(
            Path(prepared.staged_root) / "postgresql/database.dump",
            recovery_target=prepared.plan.target_installation_id,
        )

    def restore_service_state(self, prepared: PreparedCloneRestore) -> None:
        staged = Path(prepared.staged_root)
        self._volumes.replace(
            "rabbitmq",
            staged / "rabbitmq",
            _volume_entries(prepared, "rabbitmq"),
        )
        bundled = staged / "bundled-storage"
        if bundled.is_dir():
            self._volumes.replace(
                "seaweedfs",
                bundled,
                _volume_entries(prepared, "bundled-storage"),
            )

    def service_state_restored(self, prepared: PreparedCloneRestore) -> bool:
        staged = Path(prepared.staged_root)
        if not self._volumes.matches(
            "rabbitmq",
            staged / "rabbitmq",
            _volume_entries(prepared, "rabbitmq"),
        ):
            return False
        bundled = staged / "bundled-storage"
        return not bundled.is_dir() or self._volumes.matches(
            "seaweedfs",
            bundled,
            _volume_entries(prepared, "bundled-storage"),
        )

    def restore_identity(self, prepared: PreparedCloneRestore) -> None:
        path = Path(prepared.staged_root) / "secrets/source.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Clone source secrets payload must be an object")
        values = {str(name): str(value) for name, value in raw.items()}
        if tuple(sorted(values)) != tuple(sorted(prepared.manifest.imported_secret_names)):
            raise ValueError("Clone source secret inventory does not match the manifest")
        for name, value in values.items():
            expected = prepared.manifest.source_secret_fingerprints.get(name)
            if expected != hashlib.sha256(value.encode()).hexdigest():
                raise ValueError("Clone source secret fingerprint does not match the manifest")
        self._import_secrets(values)

    def start_maintenance(self, _prepared: PreparedCloneRestore) -> None:
        self._services.start(self._service_names)

    def target_started(self, _prepared: PreparedCloneRestore) -> bool:
        states = self._services.service_states(self._service_names)
        return all(states.get(name, False) for name in self._service_names)

    def target_verified(self, prepared: PreparedCloneRestore) -> bool:
        self._verify_restored_target(prepared)
        if self._preservation is not None and self._capture_preservation is not None:
            source = PreservationManifest(
                **json.loads(
                    (Path(prepared.staged_root) / "evidence/preservation.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            before = PreservationManifest(
                **json.loads(
                    (
                        self._operation_root
                        / prepared.operation_id
                        / _TARGET_BEFORE_PRESERVATION_FILE
                    ).read_text(encoding="utf-8")
                )
            )
            after = self._capture_preservation(prepared, "after")
            comparison = self._preservation.compare_clone_restore(
                source=source,
                target_before=before,
                after=after,
            )
            if not comparison.success:
                failed = ", ".join(failure.field for failure in comparison.failures)
                raise RuntimeError(f"Clone preservation verification failed: {failed}")
        return True

    def _verify_restored_target(self, prepared: PreparedCloneRestore) -> None:
        self._wait_for_target_readiness(prepared)
        # Maintenance, full readiness reconciliation, parity and release checks are
        # not startup transport probes. Their failures must never be replayed.
        self._verify_target(prepared)

    def _wait_for_target_readiness(self, prepared: PreparedCloneRestore) -> None:
        # Compose --wait only observes running state for services without a healthcheck.
        # A restarted app may not have opened its HTTP listener yet. Retry that transport
        # boundary only. The callback receives its remaining socket timeout budget;
        # it is not forcibly cancellable, so reject any result arriving too late.
        if self._readiness_probe is None:
            return
        deadline = monotonic() + _TARGET_READINESS_TIMEOUT_SECONDS
        last_error: OSError | None = None
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                if last_error is not None:
                    _LOGGER.warning(
                        "clone_target_listener_deadline_exceeded",
                        extra={"error_type": type(last_error).__name__},
                    )
                    raise last_error
                raise TimeoutError("Clone target readiness deadline exceeded")
            try:
                self._readiness_probe(prepared, remaining)
            except (ConnectionRefusedError, ConnectionResetError, TimeoutError) as exc:
                last_error = exc
            else:
                if monotonic() >= deadline:
                    raise TimeoutError("Clone target readiness deadline exceeded")
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                _LOGGER.warning(
                    "clone_target_listener_deadline_exceeded",
                    extra={"error_type": type(last_error).__name__},
                )
                raise last_error
            _LOGGER.debug(
                "clone_target_listener_pending",
                extra={"error_type": type(last_error).__name__, "remaining_seconds": remaining},
            )
            sleep(min(_TARGET_READINESS_POLL_SECONDS, remaining))

    @staticmethod
    def _write_owner_only_json(path: Path, payload: object) -> None:
        temporary = path.with_name(f".{path.name}.partial")
        temporary.unlink(missing_ok=True)
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(payload, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _publication_modes(prepared: PreparedCloneRestore, root: str) -> dict[str, int]:
    prefix = f"{root}/"
    selected: dict[str, int] = {}
    for entry in prepared.manifest.entries:
        if entry.path == root:
            selected["."] = entry.mode
        elif entry.path.startswith(prefix):
            selected[entry.path.removeprefix(prefix)] = entry.mode
    return selected


def _volume_entries(
    prepared: PreparedCloneRestore,
    root: str,
) -> tuple[CloneEntry, ...]:
    prefix = f"{root}/"
    selected: list[CloneEntry] = []
    for entry in prepared.manifest.entries:
        if entry.path == root or entry.path.startswith(prefix):
            selected.append(entry)
    return tuple(selected)
