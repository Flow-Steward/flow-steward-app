"""Installed Clone composition must keep its private namespace owner-only."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.contracts import DeploymentConfig
from core.entrypoints.cli.clone_composition import build_installed_clone_service
from core.infrastructure.installer.doctor_probes import wide_entries
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


def _compose(root: Path, profile: str):
    state = root / ".flow-steward"
    workspace = state / ("clone/workspace" if profile == "compact" else "clone")
    config = DeploymentConfig(deployment_profile=profile, environment="production")
    target = SimpleNamespace(project_name="namespace-fixture", runtime_services=())
    runtime = SimpleNamespace(
        release_service=SimpleNamespace(
            require_official_installation=lambda: SimpleNamespace(
                images=(SimpleNamespace(role="fs-cli", registry_ref="fixture@sha256:" + "a" * 64),)
            ),
            state=object(),
            selector=object(),
        ),
        configuration_service=SimpleNamespace(
            read_active=lambda: config, derive_target=lambda _config: target
        ),
        docker_lifecycle_gateway=object(),
        backup_service=object(),
        maintenance_service=object(),
        preservation_service=object(),
        _clone_data_roots=lambda *_args: {},
        _clone_output_root=lambda *_args: state / "clone/outbox",
        _clone_input_root=lambda *_args: state / "clone/inbox",
        _clone_workspace_root=lambda: workspace,
        _clone_state_root=lambda: state,
        _lifecycle_state=lambda: LifecycleStateStore(root),
    )
    return build_installed_clone_service(
        runtime, repo_root=root, environment_loader=dict, platform_machine=lambda: "arm64"
    )


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("prepare_create", (False, True))
def test_installed_composition_keeps_clone_prefixes_private(
    tmp_path, profile, existing, prepare_create
):
    state = tmp_path / ".flow-steward"
    state.mkdir(mode=0o750)
    sentinel = state / "unrelated"
    sentinel.mkdir(mode=0o755)
    (sentinel / "marker").write_bytes(b"unchanged")
    prefixes = [state / "clone"]
    if profile == "compact":
        prefixes.append(prefixes[0] / "workspace")
    if existing:
        for prefix in prefixes:
            prefix.mkdir(mode=0o775)
            prefix.chmod(0o775)
    private_roots = [prefixes[-1] / name for name in ("prepared", "restore-evidence")]
    if existing:
        for private_root in private_roots:
            private_root.mkdir(mode=0o775)
            private_root.chmod(0o775)
    before = (state.stat().st_mode, sentinel.stat().st_mode, sentinel.stat().st_ino)
    previous_umask = os.umask(0o002)
    try:
        service = _compose(tmp_path, profile)
        # Exercise the real create staging owner after the actual composition.
        if prepare_create:
            service._runtime._prepare_workspace("owned-operation")
    finally:
        os.umask(previous_umask)

    assert [stat.S_IMODE(path.stat().st_mode) for path in prefixes] == [0o700] * len(prefixes)
    assert [stat.S_IMODE(path.stat().st_mode) for path in private_roots] == [0o700] * len(
        private_roots
    )
    assert wide_entries(prefixes[0]) == []
    after = (state.stat().st_mode, sentinel.stat().st_mode, sentinel.stat().st_ino)
    assert after == before
    assert (sentinel / "marker").read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "profile,prefix", (("scalable", "clone"), ("compact", "clone"), ("compact", "workspace"))
)
def test_installed_composition_rejects_linked_clone_prefix_without_touching_target(
    tmp_path, profile, prefix
):
    state = tmp_path / ".flow-steward"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o775)
    (outside / "marker").write_bytes(b"untouched")
    link = state / "clone"
    if prefix == "workspace":
        link.mkdir()
        link.chmod(0o775)
        link = link / "workspace"
    link.symlink_to(outside, target_is_directory=True)
    before = (outside.stat().st_mode, outside.stat().st_ino)

    with pytest.raises(RuntimeError, match="real directories"):
        _compose(tmp_path, profile)

    assert link.is_symlink()
    after = (outside.stat().st_mode, outside.stat().st_ino)
    assert after == before
    assert (outside / "marker").read_bytes() == b"untouched"
    if prefix == "workspace":
        assert stat.S_IMODE(link.parent.stat().st_mode) == 0o775


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_installed_composition_refuses_foreign_clone_prefix_without_chmod(
    tmp_path, profile, monkeypatch
):
    state = tmp_path / ".flow-steward"
    state.mkdir()
    clone = state / "clone"
    clone.mkdir()
    clone.chmod(0o775)
    (clone / "marker").write_bytes(b"retained")
    before = clone.stat()
    # Change only the clone descriptor's ownership; the real state anchor stays owned.
    real_fstat = os.fstat

    def foreign_clone_metadata(descriptor):
        metadata = real_fstat(descriptor)
        if metadata.st_ino == before.st_ino:
            fields = list(metadata)
            fields[4] += 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(
        "core.infrastructure.clone.private_namespace.os.fstat", foreign_clone_metadata
    )

    with pytest.raises(RuntimeError, match="owned by the current user"):
        _compose(tmp_path, profile)

    assert clone.stat().st_mode == before.st_mode
    assert clone.stat().st_ino == before.st_ino
    assert (clone / "marker").read_bytes() == b"retained"
    assert not (clone / "workspace").exists()


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("existing", (False, True))
def test_installed_composition_refuses_foreign_state_anchor_before_namespace_mutation(
    tmp_path, profile, existing, monkeypatch
):
    state = tmp_path / ".flow-steward"
    state.mkdir(mode=0o775)
    state.chmod(0o775)
    (state / "marker").write_bytes(b"retained")
    clone = state / "clone"
    if existing:
        clone.mkdir(mode=0o775)
        clone.chmod(0o775)
    before = state.stat()
    real_fstat = os.fstat

    def foreign_state_metadata(descriptor):
        metadata = real_fstat(descriptor)
        if metadata.st_ino == before.st_ino:
            fields = list(metadata)
            fields[4] += 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(
        "core.infrastructure.clone.private_namespace.os.fstat", foreign_state_metadata
    )

    with pytest.raises(RuntimeError, match="owned by the current user"):
        _compose(tmp_path, profile)

    assert state.stat().st_mode == before.st_mode
    assert state.stat().st_ino == before.st_ino
    assert (state / "marker").read_bytes() == b"retained"
    assert clone.exists() is existing
    if existing:
        assert stat.S_IMODE(clone.stat().st_mode) == 0o775
    assert not (clone / "workspace").exists()


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("name", ("prepared", "restore-evidence"))
def test_installed_composition_rejects_linked_restore_root_before_repair(tmp_path, profile, name):
    state = tmp_path / ".flow-steward"
    state.mkdir(mode=0o750)
    clone = state / "clone"
    clone.mkdir(mode=0o775)
    clone.chmod(0o775)
    workspace = clone
    if profile == "compact":
        workspace = clone / "workspace"
        workspace.mkdir(mode=0o775)
        workspace.chmod(0o775)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o775)
    outside.chmod(0o775)
    (outside / "marker").write_bytes(b"untouched")
    (workspace / name).symlink_to(outside, target_is_directory=True)
    before = (state.stat().st_mode, clone.stat().st_mode, outside.stat().st_mode)

    with pytest.raises(RuntimeError, match="real directories"):
        _compose(tmp_path, profile)

    after = (state.stat().st_mode, clone.stat().st_mode, outside.stat().st_mode)
    assert after == before
    assert (outside / "marker").read_bytes() == b"untouched"
