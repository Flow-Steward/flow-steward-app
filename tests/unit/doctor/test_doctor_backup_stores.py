"""Doctor freshness uses real host backup metadata, independently of Compact volume reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.doctor.contracts import CheckBlocked
from core.application.doctor.hints import hint_checks
from core.application.doctor.service import DoctorService
from core.application.lifecycle.backup_service import BackupCreateRequest, BackupService
from core.infrastructure.installer.installation_doctor import InstallationDoctorProbe
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway


def _verified_backup(root: Path) -> Path:
    def pg_process(command, **kwargs):
        if "pg_dump" in command:
            kwargs["stdout"].write(b"PGDMP doctor-fixture")
        elif "pg_restore" in command and "-l" in command:
            kwargs["stdin"].read()
        else:
            raise AssertionError(f"unexpected database process: {command}")
        return SimpleNamespace(returncode=0, stderr=b"")

    gateway = FilesystemBackupGateway(
        project_root=root,
        key_source=lambda: "doctor-fixture-key",
        runner=pg_process,
        now=lambda: datetime(2026, 10, 7, 12, tzinfo=UTC),
    )
    return root / BackupService(gateway=gateway).create(BackupCreateRequest()).path


def _probe(root: Path, *, rows=(), stopped: bool = False, error: bool = False):
    class Runtime:
        def stopped_services(self, _config, *, log):
            return ["compact-runtime"] if stopped else []

        def runtime_python(self, _config, _script):
            if stopped:
                raise AssertionError("the stopped runtime must not be queried")
            if error:
                raise OSError("native backup probe failed")
            return json.dumps(rows)

    return InstallationDoctorProbe(
        root=root,
        gateway=Runtime(),
        state=InstallerStateStore(root / ".flow-steward"),
        config=SimpleNamespace(deployment_profile="compact"),
        log=SimpleNamespace(write=lambda _message: None),
    )


def test_compact_doctor_observes_verified_host_backup_while_runtime_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _verified_backup(tmp_path)
    directory = tmp_path / "backups"
    directory.chmod(0o750)
    paths = (backup, backup.with_name(backup.name + ".metadata.json"))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    # Synthetic key matching _verified_backup, never a deployed credential.
    (tmp_path / ".env.prod").write_text(
        "FS_BACKUP_ENCRYPTION_KEY=doctor-fixture-key\n"  # pragma: allowlist secret
    )
    probe = _probe(tmp_path, stopped=True)
    monkeypatch.setattr(probe, "now", lambda: datetime(2026, 10, 8, 12, tzinfo=UTC))
    facts = probe.backups()
    checks = [check for check in hint_checks() if check.check_id == "backup_freshness"]
    finding = DoctorService(checks, profile="compact").diagnose(probe).findings[0]

    assert facts.newest_verified == f"host:backups/{backup.name}"
    assert facts.newest_verified_at == datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert facts.unverified == 0
    assert finding.status == "ok"
    assert f"host:backups/{backup.name}" in finding.cause
    assert directory.stat().st_mode & 0o777 == 0o750
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


@pytest.mark.parametrize("tampered", (False, True))
def test_compact_doctor_keeps_same_basename_stores_distinct(tmp_path: Path, tampered: bool):
    backup = _verified_backup(tmp_path)
    if tampered:
        backup.write_bytes(b"changed-after-verification")
    native = {
        "name": backup.name,
        "status": "verified",
        "verified_at": "2026-10-07T11:00:00+00:00",
    }

    probe = _probe(tmp_path, rows=[native])
    facts = probe.backups()

    assert {row["name"] for row in probe._backup_rows()} == {
        f"host:backups/{backup.name}",
        f"compact:/data/backups/{backup.name}",
    }
    prefix = "compact:/data/backups" if tampered else "host:backups"
    assert facts.newest_verified == f"{prefix}/{backup.name}"
    assert facts.unverified == int(tampered)


def test_compact_doctor_preserves_native_freshness_without_host_store(tmp_path: Path):
    facts = _probe(
        tmp_path,
        rows=[
            {
                "name": "native.dump.enc",
                "status": "verified",
                "verified_at": "2026-10-07T13:00:00+00:00",
            }
        ],
    ).backups()

    assert facts.newest_verified == "compact:/data/backups/native.dump.enc"
    assert facts.newest_verified_at == datetime(2026, 10, 7, 13, tzinfo=UTC)
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("unverified", (False, True))
def test_compact_doctor_does_not_invent_empty_native_store_when_stopped(
    tmp_path: Path, unverified: bool
):
    if unverified:
        backup = _verified_backup(tmp_path)
        backup.with_name(backup.name + ".metadata.json").unlink()

    probe = _probe(tmp_path, stopped=True)
    with pytest.raises(CheckBlocked):
        probe.backups()
    assert (tmp_path / "backups").exists() is unverified


def test_compact_doctor_does_not_hide_running_native_probe_errors(tmp_path: Path):
    _verified_backup(tmp_path)

    probe = _probe(tmp_path, error=True)
    with pytest.raises(OSError, match="native backup probe failed"):
        probe.backups()


@pytest.mark.parametrize("kind", ("missing", "directory", "dangling_link", "outside_link", "file"))
def test_read_only_backup_listing_does_not_create_or_chmod(tmp_path: Path, kind: str):
    backups = tmp_path / "backups"
    if kind == "directory":
        backups.mkdir(mode=0o750)
    elif kind == "dangling_link":
        backups.symlink_to(tmp_path / "absent")
    elif kind == "outside_link":
        outside = tmp_path / "outside"
        outside.mkdir()
        backups.symlink_to(outside)
    elif kind == "file":
        backups.write_bytes(b"not-a-directory")
    gateway = FilesystemBackupGateway(project_root=tmp_path)

    if kind in {"dangling_link", "outside_link", "file"}:
        with pytest.raises(OSError):
            gateway.list_backups(read_only=True)
    else:
        assert gateway.list_backups(read_only=True) == ()
        if kind == "directory":
            assert backups.stat().st_mode & 0o777 == 0o750
        else:
            assert not backups.exists()


def test_read_only_backup_listing_propagates_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o750)
    original_iterdir = Path.iterdir

    def denied_iterdir(path: Path):
        if path == backups:
            raise PermissionError("fixture backup directory is unreadable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)

    gateway = FilesystemBackupGateway(project_root=tmp_path)
    with pytest.raises(PermissionError, match="fixture backup directory is unreadable"):
        gateway.list_backups(read_only=True)
