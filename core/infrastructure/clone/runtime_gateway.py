"""Confined runtime effects for consistent Clone creation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
from typing import Protocol

from core.application.clone.contracts import (
    ARCHIVE_ROOT_BY_DATA_CLASS,
    CloneDataClass,
    CloneEntry,
    CloneManifest,
)
from core.application.clone.runtime_policy import uses_compact_durable_state_transfer
from core.application.clone.service import CloneCreatePlan, PassphraseSource
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.clone.archive_gateway import (
    CloneArchiveGateway,
    TrustedPublicationMetadata,
)
from core.infrastructure.clone.capture_policy import clone_ignored_names

_INCOMPLETE_ARCHIVE = "archive.incomplete"
_ASSEMBLED_DIGEST = "assembled.sha256"
_VERIFIED_DIGEST = "verified.sha256"
_PUBLISHED_DIGEST = "published.sha256"


class CloneBackupPort(Protocol):
    def clone_dump(self, operation_id: str) -> AbstractContextManager[Path]: ...


class CloneServiceControlPort(Protocol):
    def service_states(self, names: tuple[str, ...]) -> dict[str, bool]: ...

    def stop(self, names: tuple[str, ...]) -> None: ...

    def start(self, names: tuple[str, ...]) -> None: ...


class CloneVolumeCapturePort(Protocol):
    def capture(self, name: str, destination: Path) -> tuple[CloneEntry, ...]: ...

    def capture_compact_filesystems(
        self,
        destination: Path,
        items: tuple[tuple[CloneDataClass, str], ...],
        *,
        durable_state_only: bool = False,
    ) -> tuple[CloneEntry, ...]: ...

    def validate_compact_filesystems(
        self,
        items: tuple[tuple[CloneDataClass, str], ...],
        *,
        durable_state_only: bool = False,
    ) -> None: ...


class CloneRuntimeGateway:
    """Capture state, build/verify one archive, and publish it atomically."""

    def __init__(
        self,
        *,
        staging_root: Path,
        output_root: Path,
        backup: CloneBackupPort,
        services: CloneServiceControlPort,
        volumes: CloneVolumeCapturePort,
        source_roots: Mapping[CloneDataClass | str, Path],
        archive: CloneArchiveGateway,
        source_secrets: Mapping[str, str] | None = None,
        database_parity_evidence: Mapping[str, str | int | bool] | None = None,
        capture_database_parity: Callable[[Path], None] | None = None,
        capture_preservation: Callable[[CloneCreatePlan, Path], None] | None = None,
        external_effect_categories: tuple[str, ...] = (),
        compact_data_volume: bool = False,
    ) -> None:
        self._staging_root = Path(os.path.abspath(staging_root))
        self._output_root = Path(os.path.abspath(output_root))
        self._backup = backup
        self._services = services
        self._volumes = volumes
        self._source_roots = {_source_key(key): Path(path) for key, path in source_roots.items()}
        self._archive = archive
        self._source_secrets = {
            str(name): str(value) for name, value in (source_secrets or {}).items()
        }
        self._database_parity_evidence = dict(database_parity_evidence or {})
        self._capture_database_parity = capture_database_parity
        self._capture_preservation = capture_preservation
        self._external_effect_categories = tuple(sorted(set(external_effect_categories)))
        self._compact_data_volume = compact_data_volume
        self._captured_volume_entries: dict[str, tuple[CloneEntry, ...]] = {}

    def preflight(self, plan: CloneCreatePlan) -> None:
        output = Path(os.path.abspath(plan.output_path))
        if output.parent != self._output_root:
            raise LifecycleError.stable_failure(
                code="clone_output_outside_root",
                cause="Clone output must be a direct child of the configured output directory.",
                remediation="Select the configured Clone output directory and retry.",
                step="preflight",
            )
        if self._published_archive_matches(plan):
            return
        if output.exists() or output.is_symlink():
            raise LifecycleError.stable_failure(
                code="clone_output_conflict",
                cause="The generated Clone archive path already exists.",
                remediation="Move the existing artifact or retry with a new creation timestamp.",
                step="preflight",
            )
        compact_requests: list[tuple[CloneDataClass, str]] = []
        mixed_state = not self._compact_data_volume and uses_compact_durable_state_transfer(
            plan.config
        )
        for item in plan.source_plan.items:
            if item.capture_kind != "filesystem":
                continue
            if self._compact_data_volume or (
                mixed_state and item.data_class is CloneDataClass.DURABLE_STATE
            ):
                compact_requests.append((item.data_class, item.archive_root))
                continue
            source = self._source_roots.get(item.data_class)
            if source is None or source.is_symlink() or not source.is_dir():
                raise LifecycleError.stable_failure(
                    code="clone_source_unavailable",
                    cause=f"The allowlisted {item.data_class.value} source is unavailable.",
                    remediation="Repair the installed data root before retrying Clone creation.",
                    step="preflight",
                )
        if compact_requests:
            if mixed_state:
                self._volumes.validate_compact_filesystems(
                    tuple(compact_requests), durable_state_only=True
                )
            else:
                self._volumes.validate_compact_filesystems(tuple(compact_requests))

    def capture_source(self, plan: CloneCreatePlan) -> None:
        self._captured_volume_entries.clear()
        workspace = self._prepare_workspace(plan.operation_id)
        self._reconcile_prior_service_state(plan.operation_id)
        self._remove_capture_products(workspace)
        source_root = workspace / "source"
        source_root.mkdir(mode=0o700)
        capture_failure: Exception | None = None
        recovery_failure: Exception | None = None
        service_names = self._capture_service_names(plan)

        try:
            with self._backup.clone_dump(plan.operation_id) as dump:
                database_root = source_root / "postgresql"
                database_root.mkdir(mode=0o700)
                shutil.copyfile(dump, database_root / "database.dump")
                (database_root / "database.dump").chmod(0o600)

            prior_states = self._services.service_states(service_names)
            self._write_recovery_state(plan.operation_id, prior_states)
            running = tuple(name for name in service_names if prior_states.get(name, False))
            if running:
                self._services.stop(running)
            self._capture_stopped_state(plan, source_root)
            self._capture_filesystems(plan, source_root)
            self._capture_metadata(plan, source_root)
        except Exception as exc:
            capture_failure = exc
        finally:
            if self.recovery_state_path(plan.operation_id).exists():
                try:
                    self._restore_recorded_service_state(plan.operation_id)
                except Exception as exc:
                    recovery_failure = exc

        if recovery_failure is not None:
            shutil.rmtree(source_root, ignore_errors=True)
            details = {
                "capture_failure": (
                    type(capture_failure).__name__ if capture_failure is not None else "none"
                ),
                "recovery_failure": type(recovery_failure).__name__,
            }
            failure = LifecycleError.recovery_required(
                code="clone_source_service_recovery_required",
                cause="Clone capture failed to restore the source service state exactly.",
                remediation="Inspect the Clone journal and restore the recorded source services.",
                step="capture_source",
                recovery_command="flow-steward clone create",
                details=details,
            )
            raise failure from (capture_failure or recovery_failure)
        if capture_failure is not None:
            shutil.rmtree(source_root, ignore_errors=True)
            raise capture_failure

        manifest = self._build_manifest(plan, source_root)
        self._write_json_atomic(
            self._manifest_path(plan.operation_id), manifest.model_dump(mode="json")
        )

    def assemble_archive(
        self,
        plan: CloneCreatePlan,
        *,
        passphrase_source: PassphraseSource,
    ) -> None:
        workspace = self._workspace(plan.operation_id)
        manifest = self._load_manifest(plan.operation_id)
        source_root = workspace / "source"
        partial = workspace / "archive.partial"
        incomplete = workspace / _INCOMPLETE_ARCHIVE
        partial.unlink(missing_ok=True)
        incomplete.unlink(missing_ok=True)
        with partial.open("x+b") as destination:
            os.fchmod(destination.fileno(), 0o600)
            self._archive.write_archive(
                source_root,
                manifest,
                destination,
                passphrase_source=passphrase_source,
                trusted_publication_metadata=_trusted_publication_metadata(manifest),
            )
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(partial, incomplete)
        self._write_digest_marker(workspace / _ASSEMBLED_DIGEST, incomplete)

    def verify_archive(
        self,
        plan: CloneCreatePlan,
        *,
        passphrase_source: PassphraseSource,
    ) -> None:
        workspace = self._workspace(plan.operation_id)
        incomplete = workspace / _INCOMPLETE_ARCHIVE
        with incomplete.open("rb") as source:
            verified = self._archive.stage_verified(
                source,
                staging_parent=workspace / "verification",
                passphrase_source=passphrase_source,
            )
        try:
            if verified.manifest != self._load_manifest(plan.operation_id):
                raise RuntimeError("Verified Clone manifest changed during assembly")
        finally:
            verified.cleanup()
            (workspace / "verification").rmdir()
        self._write_digest_marker(workspace / _VERIFIED_DIGEST, incomplete)

    def publish_archive(self, plan: CloneCreatePlan) -> None:
        workspace = self._workspace(plan.operation_id)
        source = workspace / _INCOMPLETE_ARCHIVE
        if not self._digest_marker_matches(workspace / _VERIFIED_DIGEST, source):
            raise RuntimeError("Clone archive must be verified before publication")
        self._ensure_private_root(self._output_root)
        destination = Path(os.path.abspath(plan.output_path))
        if destination.parent != self._output_root:
            raise RuntimeError("Clone destination escaped the configured output root")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Clone destination already exists")
        publish_temp = destination.parent / f".{destination.name}.partial"
        publish_temp.unlink(missing_ok=True)
        self._write_digest_marker(workspace / _PUBLISHED_DIGEST, source)
        try:
            with source.open("rb") as payload, publish_temp.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(payload, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(publish_temp, destination)
            destination.chmod(0o600)
            self._fsync_directory(destination.parent)
        finally:
            publish_temp.unlink(missing_ok=True)

    def postcondition_holds(self, plan: CloneCreatePlan, step_id: str) -> bool:
        if step_id in {
            "capture_source",
            "assemble_archive",
            "verify_archive",
            "publish_archive",
        } and self._published_archive_matches(plan):
            return True
        workspace = self._workspace(plan.operation_id)
        if step_id == "capture_source":
            return (workspace / "source").is_dir() and self._manifest_path(
                plan.operation_id
            ).is_file()
        if step_id == "assemble_archive":
            return self._digest_marker_matches(
                workspace / _ASSEMBLED_DIGEST,
                workspace / _INCOMPLETE_ARCHIVE,
            )
        if step_id == "verify_archive":
            return self._digest_marker_matches(
                workspace / _VERIFIED_DIGEST,
                workspace / _INCOMPLETE_ARCHIVE,
            )
        if step_id == "publish_archive":
            destination = Path(plan.output_path)
            return destination.is_file() and not destination.is_symlink()
        return False

    def artifact_size(self, plan: CloneCreatePlan) -> int:
        destination = Path(plan.output_path)
        size = destination.stat().st_size
        if size <= 0:
            raise RuntimeError("Published Clone archive is empty")
        return size

    def archive_digest(self, plan: CloneCreatePlan) -> str:
        return _sha256_path(Path(plan.output_path))

    def cleanup(
        self,
        plan: CloneCreatePlan,
        *,
        preserve_recovery: bool = False,
        preserve_resume: bool = False,
    ) -> None:
        workspace = self._workspace(plan.operation_id)
        publish_temp = Path(plan.output_path).parent / f".{Path(plan.output_path).name}.partial"
        publish_temp.unlink(missing_ok=True)
        if not workspace.exists():
            return
        if preserve_recovery and self.recovery_state_path(plan.operation_id).exists():
            self._remove_capture_products(workspace)
            return
        if preserve_resume and self._published_archive_matches(plan):
            self._remove_capture_products(workspace, preserve_published=True)
            return
        shutil.rmtree(workspace)

    def recovery_state_path(self, operation_id: str) -> Path:
        return self._workspace(operation_id) / "source-services.json"

    def _prepare_workspace(self, operation_id: str) -> Path:
        self._ensure_private_root(self._staging_root)
        workspace = self._workspace(operation_id)
        if workspace.is_symlink():
            raise RuntimeError("Clone workspace must not be a symbolic link")
        workspace.mkdir(mode=0o700, exist_ok=True)
        workspace.chmod(0o700)
        return workspace

    def _workspace(self, operation_id: str) -> Path:
        if (
            not operation_id
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"  # pragma: allowlist secret - identifier alphabet
                for character in operation_id
            )
        ):
            raise ValueError("Clone operation id is invalid")
        workspace = self._staging_root / operation_id
        if not workspace.absolute().is_relative_to(self._staging_root.absolute()):
            raise ValueError("Clone workspace escaped its staging root")
        return workspace

    def _manifest_path(self, operation_id: str) -> Path:
        return self._workspace(operation_id) / "manifest.json"

    def _capture_service_names(self, plan: CloneCreatePlan) -> tuple[str, ...]:
        classes = {item.data_class for item in plan.source_plan.items}
        names = ["rabbitmq-fs"]
        if CloneDataClass.BUNDLED_STORAGE in classes:
            names.append("seaweedfs")
        return tuple(names)

    def _capture_stopped_state(self, plan: CloneCreatePlan, source_root: Path) -> None:
        classes = {item.data_class for item in plan.source_plan.items}
        self._captured_volume_entries["rabbitmq"] = self._volumes.capture(
            "rabbitmq", source_root / "rabbitmq"
        )
        if CloneDataClass.BUNDLED_STORAGE in classes:
            self._captured_volume_entries["bundled-storage"] = self._volumes.capture(
                "seaweedfs", source_root / "bundled-storage"
            )

    def _capture_filesystems(self, plan: CloneCreatePlan, source_root: Path) -> None:
        if self._compact_data_volume:
            self._capture_compact_filesystems(plan, source_root)
            return
        mixed_state = uses_compact_durable_state_transfer(plan.config)
        if mixed_state:
            self._capture_compact_filesystems(plan, source_root, durable_state_only=True)
        for item in plan.source_plan.items:
            if item.capture_kind != "filesystem":
                continue
            if mixed_state and item.data_class is CloneDataClass.DURABLE_STATE:
                continue
            source = self._source_roots[item.data_class]
            ignored = clone_ignored_names(item.data_class)
            shutil.copytree(
                source,
                source_root / item.archive_root,
                symlinks=True,
                ignore=partial(_ignored_names, ignored=frozenset(ignored)),
            )

    def _capture_compact_filesystems(
        self,
        plan: CloneCreatePlan,
        source_root: Path,
        *,
        durable_state_only: bool = False,
    ) -> None:
        requests = tuple(
            (item.data_class, item.archive_root)
            for item in plan.source_plan.items
            if item.capture_kind == "filesystem"
            and (not durable_state_only or item.data_class is CloneDataClass.DURABLE_STATE)
        )
        if durable_state_only:
            entries = self._volumes.capture_compact_filesystems(
                source_root, requests, durable_state_only=True
            )
        else:
            entries = self._volumes.capture_compact_filesystems(source_root, requests)
        for _data_class, archive_root in requests:
            prefix = f"{archive_root}/"
            self._captured_volume_entries[archive_root] = tuple(
                entry
                for entry in entries
                if entry.path == archive_root or entry.path.startswith(prefix)
            )

    def _capture_metadata(self, plan: CloneCreatePlan, source_root: Path) -> None:
        secrets = source_root / "secrets"
        evidence = source_root / "evidence"
        secrets.mkdir(mode=0o700)
        evidence.mkdir(mode=0o700)
        self._write_json_file(secrets / "source.json", self._source_secrets)
        self._write_json_file(
            evidence / "release.json",
            {
                "source_installation_id": plan.source_installation_id,
                "release": plan.release.model_dump(mode="json"),
            },
        )
        self._write_json_file(
            evidence / "configuration.json",
            plan.config.model_dump(mode="json"),
        )
        if self._capture_database_parity is not None:
            parity_path = evidence / "database-parity.json"
            self._capture_database_parity(parity_path)
            if parity_path.is_symlink() or not parity_path.is_file():
                raise RuntimeError("Clone database parity capture did not publish evidence")
            parity_path.chmod(0o600)
            self._database_parity_evidence["snapshot_sha256"] = _sha256_path(parity_path)
        if self._capture_preservation is not None:
            preservation_path = evidence / "preservation.json"
            self._capture_preservation(plan, preservation_path)
            if preservation_path.is_symlink() or not preservation_path.is_file():
                raise RuntimeError("Clone preservation capture did not publish evidence")
            preservation_path.chmod(0o600)

    def _build_manifest(self, plan: CloneCreatePlan, source_root: Path) -> CloneManifest:
        inventory = _inventory(source_root)
        volume_roots = tuple(self._captured_volume_entries)
        entries = tuple(
            sorted(
                (
                    entry
                    for entry in inventory
                    if not any(
                        entry.path == root or entry.path.startswith(f"{root}/")
                        for root in volume_roots
                    )
                ),
                key=lambda entry: entry.path,
            )
        ) + tuple(
            sorted(
                (
                    entry
                    for captured in self._captured_volume_entries.values()
                    for entry in captured
                ),
                key=lambda entry: entry.path,
            )
        )
        entries = tuple(sorted(entries, key=lambda entry: entry.path))
        secret_names = tuple(sorted(self._source_secrets))
        return CloneManifest(
            created_at=plan.created_at,
            release=plan.release,
            deployment_profile=plan.config.deployment_profile,
            data_placement=plan.config.data_placement,
            file_storage=plan.config.file_storage,
            source_installation_id=plan.source_installation_id,
            entries=entries,
            entry_count=len(entries),
            aggregate_size=sum(entry.size for entry in entries),
            imported_secret_names=secret_names,
            source_secret_fingerprints={
                name: hashlib.sha256(self._source_secrets[name].encode()).hexdigest()
                for name in secret_names
            },
            external_effect_categories=self._external_effect_categories,
            database_parity_evidence=self._database_parity_evidence,
            capture_evidence={"postgresql_format": "custom", "maintenance": "manual"},
        )

    def _load_manifest(self, operation_id: str) -> CloneManifest:
        return CloneManifest.model_validate_json(
            self._manifest_path(operation_id).read_text(encoding="utf-8")
        )

    def _write_recovery_state(self, operation_id: str, states: dict[str, bool]) -> None:
        self._write_json_atomic(self.recovery_state_path(operation_id), states)

    def _reconcile_prior_service_state(self, operation_id: str) -> None:
        if self.recovery_state_path(operation_id).exists():
            self._restore_recorded_service_state(operation_id)

    def _restore_recorded_service_state(self, operation_id: str) -> None:
        path = self.recovery_state_path(operation_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected = {str(name): bool(running) for name, running in dict(raw).items()}
        names = tuple(expected)
        current = self._services.service_states(names)
        to_start = tuple(name for name in names if expected[name] and not current.get(name, False))
        to_stop = tuple(name for name in names if not expected[name] and current.get(name, False))
        if to_start:
            self._services.start(to_start)
        if to_stop:
            self._services.stop(to_stop)
        verified = self._services.service_states(names)
        if any(verified.get(name, False) != expected[name] for name in names):
            raise RuntimeError("Source service state restoration did not converge")
        path.unlink()

    def _remove_capture_products(
        self,
        workspace: Path,
        *,
        preserve_published: bool = False,
    ) -> None:
        shutil.rmtree(workspace / "source", ignore_errors=True)
        shutil.rmtree(workspace / "verification", ignore_errors=True)
        products = [
            "manifest.json",
            "archive.partial",
            _INCOMPLETE_ARCHIVE,
            _ASSEMBLED_DIGEST,
            _VERIFIED_DIGEST,
        ]
        if not preserve_published:
            products.append(_PUBLISHED_DIGEST)
        for name in products:
            (workspace / name).unlink(missing_ok=True)

    def _published_archive_matches(self, plan: CloneCreatePlan) -> bool:
        return self._digest_marker_matches(
            self._workspace(plan.operation_id) / _PUBLISHED_DIGEST,
            Path(plan.output_path),
        )

    @staticmethod
    def _write_json_file(path: Path, value: object) -> None:
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o600)

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        temporary = path.with_name(f".{path.name}.partial")
        temporary.unlink(missing_ok=True)
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(value, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_digest_marker(path: Path, payload: Path) -> None:
        CloneRuntimeGateway._write_json_atomic(path, {"sha256": _sha256_path(payload)})

    @staticmethod
    def _digest_marker_matches(marker: Path, payload: Path) -> bool:
        if (
            marker.is_symlink()
            or payload.is_symlink()
            or not marker.is_file()
            or not payload.is_file()
        ):
            return False
        try:
            expected = str(json.loads(marker.read_text(encoding="utf-8"))["sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return expected == _sha256_path(payload)

    @staticmethod
    def _ensure_private_root(root: Path) -> None:
        if root.is_symlink():
            raise RuntimeError("Clone runtime root must not be a symbolic link")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _source_key(value: CloneDataClass | str) -> CloneDataClass:
    if isinstance(value, CloneDataClass):
        return value
    for data_class, archive_root in ARCHIVE_ROOT_BY_DATA_CLASS.items():
        if value in {data_class.value, archive_root}:
            return data_class
    raise ValueError(f"Unsupported Clone source root key: {value}")


def _trusted_publication_metadata(manifest: CloneManifest) -> TrustedPublicationMetadata:
    roots = {"rabbitmq"}
    if any(entry.data_class is CloneDataClass.BUNDLED_STORAGE for entry in manifest.entries):
        roots.add("bundled-storage")
    if manifest.data_placement == "compact_volume":
        roots.update({"artifacts", "extensions", "state"})
    elif manifest.deployment_profile == "compact" and manifest.data_placement == "host_directories":
        roots.add("state")
    return TrustedPublicationMetadata(
        entries=tuple(
            entry
            for entry in manifest.entries
            if any(entry.path == root or entry.path.startswith(f"{root}/") for root in roots)
        )
    )


def _ignored_names(
    _directory: str,
    names: list[str],
    *,
    ignored: frozenset[str],
) -> list[str]:
    return sorted(set(names) & ignored)


def _inventory(root: Path) -> list[CloneEntry]:
    entries: list[CloneEntry] = []
    root_names = {
        archive_root: data_class for data_class, archive_root in ARCHIVE_ROOT_BY_DATA_CLASS.items()
    }

    def visit(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        data_class = root_names[relative.split("/", 1)[0]]
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=relative,
                    entry_type="symlink",
                    size=0,
                    mode=mode,
                    uid=metadata.st_uid,
                    gid=metadata.st_gid,
                    link_target=os.readlink(path),
                )
            )
            return
        if stat.S_ISDIR(metadata.st_mode):
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=relative,
                    entry_type="directory",
                    size=0,
                    mode=mode,
                    uid=metadata.st_uid,
                    gid=metadata.st_gid,
                )
            )
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if stat.S_ISREG(metadata.st_mode):
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=relative,
                    entry_type="file",
                    size=metadata.st_size,
                    mode=mode,
                    uid=metadata.st_uid,
                    gid=metadata.st_gid,
                    sha256=_sha256_path(path),
                )
            )
            return
        raise ValueError(f"Clone source contains an unsupported entry type: {relative}")

    for child in sorted(root.iterdir(), key=lambda item: item.name):
        visit(child)
    return entries


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["CloneRuntimeGateway"]
