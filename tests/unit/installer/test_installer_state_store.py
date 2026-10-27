from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.application.installer import (
    InstallConfig,
    InstallJournal,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.release.activation import ReleaseHandoffJournal
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import SourceInstallationOrigin
from core.application.release.service import ReleaseTransitionBinding
from core.infrastructure.installer import state_store as state_store_module
from core.infrastructure.installer.state_store import InstallerStateStore


def _journal() -> InstallJournal:
    return InstallJournal(
        operation_id="install-1",
        project_identity="flow-steward-test",
        config=InstallConfig(),
        completed_steps=("preflight",),
    )


def _release(version: str, marker: str) -> ReleaseIdentity:
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


def test_journal_and_log_are_owner_only_and_secret_free(tmp_path: Path) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")

    store.save_journal(_journal())
    log = store.open_log("install-1")
    log.write("Authorization: Bearer super-secret")
    log.write('{"FS_AUTH_BOOTSTRAP_TOKEN": "json-secret"}')
    log.write("POSTGRES_PASSWORD='shell-secret'")
    log.close()

    assert oct(store.root.stat().st_mode & 0o777) == "0o700"
    assert oct(store.journal_path.stat().st_mode & 0o777) == "0o600"
    assert oct(log.path.stat().st_mode & 0o777) == "0o600"
    payload = json.loads(store.journal_path.read_text())
    assert "secret" not in store.journal_path.read_text().lower()
    assert payload["operation_id"] == "install-1"
    assert "super-secret" not in log.path.read_text()
    assert "json-secret" not in log.path.read_text()
    assert "shell-secret" not in log.path.read_text()
    assert "[REDACTED]" in log.path.read_text()


def test_save_journal_replaces_atomically_without_temp_file(tmp_path: Path) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    first = _journal()
    store.save_journal(first)

    store.save_journal(first.model_copy(update={"completed_steps": ("preflight", "secrets")}))

    assert store.load_journal().completed_steps == ("preflight", "secrets")
    assert list(store.root.glob("*.tmp")) == []


def test_pending_release_is_owner_only_and_does_not_replace_installed_release(
    tmp_path: Path,
) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    installed = _release("1.2.3", "a")
    pending = _release("1.3.0", "b")

    store.stage_pending_release(installed)
    store.publish_installed_release(installed)
    store.stage_pending_release(pending)

    assert store.load_installed_release() == installed
    assert store.load_pending_release() == pending
    assert oct(store.installed_release_path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.pending_release_path.stat().st_mode & 0o777) == "0o600"


def _transition_binding(target: ReleaseIdentity) -> ReleaseTransitionBinding:
    origin = SourceInstallationOrigin(
        commit_sha="a" * 40,
        deployment_profile="scalable",
        platform="linux/amd64",
        installation_id="flow-steward-scalable",
        configuration_sha256="c" * 64,
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


def test_release_handoff_retains_the_published_v1_wire_contract(tmp_path: Path) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    journal = ReleaseHandoffJournal(
        target=_release("1.3.0", "b"),
        phase="checkout_applied",
    )

    store.save_release_handoff(journal)

    payload = json.loads(store.release_handoff_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "target",
        "phase",
        "install_config",
        "explicit_fields",
        "release_repository",
        "release_registries",
    }


def _extract_v030_reader_fixture(source_repo: Path, target_checkout: Path) -> None:
    fixture_dir = source_repo / "tests" / "fixtures" / "release-handoff"
    manifest_path = fixture_dir / "v0.3.0-reader.json"
    manifest_bytes = manifest_path.read_bytes()
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == "4fd564cbcaea64b0ec6a027357db68ea6fb63718a834fd32cec5f04077782045"  # pragma: allowlist secret - pinned fixture checksum
    )
    manifest = json.loads(manifest_bytes)
    assert (
        manifest["source_commit"]
        == "a921a3da0485e43045e0c50d7b907744b2918618"  # pragma: allowlist secret - immutable fixture commit
    )
    archive_bytes = (fixture_dir / manifest["archive_path"]).read_bytes()
    assert hashlib.sha256(archive_bytes).hexdigest() == manifest["archive_sha256"]
    assert len(archive_bytes) == manifest["archive_bytes"]

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as bundle:
        members = bundle.getmembers()
        assert all(member.isfile() for member in members)
        assert [member.name for member in members] == [
            record["path"] for record in manifest["files"]
        ]
        for member in members:
            member_path = Path(member.name)
            assert not member_path.is_absolute()
            assert ".." not in member_path.parts
        bundle.extractall(target_checkout, filter="data")

    for record in manifest["files"]:
        extracted = target_checkout / record["path"]
        assert extracted.is_file() and not extracted.is_symlink()
        payload = extracted.read_bytes()
        assert len(payload) == record["bytes"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        blob_header = f"blob {len(payload)}\0".encode("ascii")
        blob_sha1 = hashlib.sha1(blob_header + payload, usedforsecurity=False).hexdigest()
        assert blob_sha1 == record["git_blob_sha1"]


def test_published_v030_target_reads_the_current_handoff_wire(
    tmp_path: Path,
) -> None:
    source_repo = Path(os.environ["PYTEST_SOURCE_REPO_ROOT"])
    target_checkout = tmp_path / "v0.3.0"
    target_checkout.mkdir()
    _extract_v030_reader_fixture(source_repo, target_checkout)
    state_root = target_checkout / ".flow-steward"
    store = InstallerStateStore(state_root)
    target = _release("1.3.0", "b")
    store.stage_pending_release(target)
    store.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))

    outcome = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from core.infrastructure.installer.state_store import InstallerStateStore; "
                "state=InstallerStateStore(Path('.flow-steward')); "
                "journal=state.load_release_handoff(); "
                "pending=state.load_pending_release(); "
                "assert journal is not None and journal.phase == 'checkout_applied'; "
                "assert pending == journal.target"
            ),
        ),
        cwd=target_checkout,
        env={**os.environ, "PYTHONPATH": str(target_checkout)},
        capture_output=True,
        text=True,
    )

    assert outcome.returncode == 0, outcome.stderr


def test_source_transition_binding_is_a_separate_owner_only_operation_artifact(
    tmp_path: Path,
) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    target = _release("1.3.0", "b")
    binding = _transition_binding(target)

    store.save_release_transition_binding(binding)
    store.save_release_handoff(ReleaseHandoffJournal(target=target, phase="checkout_applied"))

    assert store.load_release_transition_binding() == binding
    assert oct(store.release_transition_binding_path.stat().st_mode & 0o777) == "0o600"
    assert "transition_binding" not in json.loads(
        store.release_handoff_path.read_text(encoding="utf-8")
    )
    assert store.clear_release_transition_binding(binding) is True
    assert store.load_release_transition_binding() is None


def test_release_handoff_secret_is_owner_only_and_consumed_once(tmp_path: Path) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")

    store.save_release_handoff_secret("https://localhost/bootstrap#one-time-token")

    assert oct(store.release_handoff_secret_path.stat().st_mode & 0o777) == "0o600"
    assert store.take_release_handoff_secret() == ("https://localhost/bootstrap#one-time-token")
    assert store.take_release_handoff_secret() == ""


def test_publish_installed_release_atomically_advances_the_complete_identity(
    tmp_path: Path,
) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    target = _release("2.0.0", "c")

    store.stage_pending_release(target)
    store.publish_installed_release(target)

    assert store.load_installed_release() == target
    assert store.load_pending_release() is None
    payload = json.loads(store.installed_release_path.read_text(encoding="utf-8"))
    assert payload["version"] == "2.0.0"
    assert payload["commit_sha"] == "c" * 40
    assert payload["images"][0]["manifest_digest"] == f"sha256:{'c' * 64}"
    assert list(store.root.glob("*.tmp")) == []


def test_ensure_env_secrets_preserves_existing_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# operator comment\nCUSTOM_EXPANSION=${KEEP_ME:-default}\nFS_SECRET_KEY=existing\n",
        encoding="utf-8",
    )
    generated: list[str] = []

    def factory() -> str:
        value = f"generated-{len(generated)}"
        generated.append(value)
        return value

    store = InstallerStateStore(tmp_path / ".flow-steward")
    first = store.ensure_env_secrets(
        env_path,
        ("FS_SECRET_KEY", "ENCRYPTION_KEY", "FS_BACKUP_ENCRYPTION_KEY"),
        secret_factory=factory,
    )
    second = store.ensure_env_secrets(
        env_path,
        ("FS_SECRET_KEY", "ENCRYPTION_KEY", "FS_BACKUP_ENCRYPTION_KEY"),
        secret_factory=factory,
    )

    assert first == second
    assert first["FS_SECRET_KEY"] == "existing"
    assert len(generated) == 2
    text = env_path.read_text(encoding="utf-8")
    assert "# operator comment" in text
    assert "CUSTOM_EXPANSION=${KEEP_ME:-default}" in text
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("rename_errno", [errno.EBUSY, errno.EPERM])
def test_update_env_values_supports_bind_mounted_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rename_errno: int
) -> None:
    """Docker engines reject atomic rename over file binds with EBUSY or EPERM."""
    env_path = tmp_path / ".env.prod"
    env_path.write_text("FS_WORKER_MCP_BEARER_TOKEN=old\n", encoding="utf-8")
    real_replace = state_store_module.os.replace

    def _replace(source: Path, target: Path) -> None:
        if Path(target) == env_path:
            raise OSError(rename_errno, "file-level bind mount cannot be replaced")
        real_replace(source, target)

    monkeypatch.setattr(state_store_module.os, "replace", _replace)
    store = InstallerStateStore(tmp_path / ".flow-steward")

    store.update_env_values(env_path, {"FS_WORKER_MCP_BEARER_TOKEN": "rotated-token"})

    assert store.read_env_values(env_path)["FS_WORKER_MCP_BEARER_TOKEN"] == "rotated-token"
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_constructing_state_store_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / ".flow-steward"

    InstallerStateStore(root)

    assert not root.exists()


@pytest.mark.parametrize(
    "placeholder",
    (
        "CHANGE_ME_STRONG_PASSWORD",
        "change-me",
        "REPLACE_WITH_SECURE_TOKEN",
        "local-dev-secret-not-for-production",
        "default-pii-salt-change-me",
    ),
)
def test_ensure_env_secrets_replaces_example_placeholders(tmp_path: Path, placeholder: str) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"FS_SECRET_KEY={placeholder}\n", encoding="utf-8")
    store = InstallerStateStore(tmp_path / ".flow-steward")

    values = store.ensure_env_secrets(
        env_path,
        ("FS_SECRET_KEY",),
        secret_factory=lambda: "secure-generated-value",
    )

    assert values["FS_SECRET_KEY"] == "secure-generated-value"
    assert placeholder not in env_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "saved_secret",
    (
        "change-meeting-prod-secret",
        "replace-withdrawal-prod-secret",
        "your-passwordless-prod-secret",
    ),
)
def test_ensure_env_secrets_preserves_real_values_with_placeholder_like_prefixes(
    tmp_path: Path, saved_secret: str
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"FS_SECRET_KEY={saved_secret}\n", encoding="utf-8")
    store = InstallerStateStore(tmp_path / ".flow-steward")

    values = store.ensure_env_secrets(
        env_path,
        ("FS_SECRET_KEY",),
        secret_factory=lambda: "unexpected-rotation",
    )

    assert values["FS_SECRET_KEY"] == saved_secret
    assert f"FS_SECRET_KEY={saved_secret}" in env_path.read_text(encoding="utf-8")


def test_journal_rejects_secret_shaped_fields(tmp_path: Path) -> None:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    unsafe = _journal().model_dump(mode="json")
    unsafe["bootstrap_token"] = "do-not-write"

    try:
        store._write_json(store.journal_path, unsafe)
    except ValueError as exc:
        assert "secret" in str(exc).lower()
    else:
        raise AssertionError("secret-shaped journal data must be rejected")


def test_state_store_does_not_change_process_umask(tmp_path: Path) -> None:
    previous = os.umask(0o027)
    os.umask(previous)
    store = InstallerStateStore(tmp_path / ".flow-steward")

    store.save_journal(_journal())

    observed = os.umask(previous)
    os.umask(observed)
    assert observed == previous


def test_operation_lock_fails_closed_for_concurrent_installer(tmp_path: Path) -> None:
    first = InstallerStateStore(tmp_path / ".flow-steward")
    second = InstallerStateStore(tmp_path / ".flow-steward")

    with (
        first.operation_lock(),
        pytest.raises(InstallerFailure) as raised,
        second.operation_lock(),
    ):
        raise AssertionError("concurrent installer must never enter the lock")

    assert raised.value.code == "installer_operation_active"
    assert raised.value.exit_code == 6
