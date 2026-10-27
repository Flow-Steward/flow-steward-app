from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.infrastructure.clone import compact_volume_publication
from core.infrastructure.clone.compact_volume_publication import (
    _durable_matches as _compact_durable_matches,
)
from core.infrastructure.clone.compact_volume_publication import (
    _merge_durable as _compact_merge_durable,
)
from core.infrastructure.clone.compact_volume_publication import (
    _replace_tree as _compact_replace_tree,
)
from core.infrastructure.clone.filesystem_publication import (
    _replace_directory,
    _replace_durable_children,
)
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer
from core.infrastructure.clone.volume_metadata import (
    apply_volume_metadata,
    inventory_volume_metadata,
    volume_metadata_matches,
)


class _Runner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.commands.append(argv)
        if argv[:3] == ("docker", "volume", "ls"):
            stdout = "flow-steward_fs-rabbitmq\n"
        elif argv[:2] == ("docker", "create"):
            stdout = "transfer-container\n"
        elif argv[:5] == ("docker", "exec", "transfer-container", "stat", "-c"):
            stdout = f"{os.geteuid()}:{os.getegid()}\n"
        elif "--capture" in argv:
            stdout = json.dumps(
                [
                    CloneEntry(
                        data_class=CloneDataClass.RABBITMQ,
                        path="rabbitmq",
                        entry_type="directory",
                        size=0,
                        mode=0o750,
                        uid=12345,
                        gid=12346,
                    ).model_dump(mode="json")
                ]
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_volume_replace_clears_synchronously_before_archive_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rabbitmq"
    source.mkdir()
    (source / "state").write_text("durable", encoding="utf-8")
    runner = _Runner()
    transfer = DockerCloneVolumeTransfer(
        project_name="flow-steward",
        tool_image="ghcr.io/example/cli@sha256:" + "a" * 64,
        runner=runner,
    )

    owner = (os.geteuid(), os.getegid())
    entries = (
        CloneEntry(
            data_class=CloneDataClass.RABBITMQ,
            path="rabbitmq",
            entry_type="directory",
            size=0,
            mode=source.stat().st_mode & 0o777,
            uid=owner[0],
            gid=owner[1],
        ),
        CloneEntry(
            data_class=CloneDataClass.RABBITMQ,
            path="rabbitmq/state",
            entry_type="file",
            size=7,
            mode=(source / "state").stat().st_mode & 0o777,
            uid=owner[0],
            gid=owner[1],
            sha256="a" * 64,
        ),
    )

    transfer.replace("rabbitmq", source, entries)

    start = runner.commands.index(("docker", "start", "transfer-container"))
    clear = runner.commands.index(
        (
            "docker",
            "exec",
            "transfer-container",
            "find",
            "/target",
            "-mindepth",
            "1",
            "-delete",
        )
    )
    copy = runner.commands.index(
        (
            "docker",
            "cp",
            "--archive",
            f"{source}/.",
            "transfer-container:/target",
        )
    )
    assert start < clear < copy


def test_volume_capture_preserves_numeric_ownership_and_modes(tmp_path: Path) -> None:
    runner = _Runner()
    transfer = DockerCloneVolumeTransfer(
        project_name="flow-steward",
        tool_image="ghcr.io/example/cli@sha256:" + "a" * 64,
        runner=runner,
    )

    entries = transfer.capture("rabbitmq", tmp_path / "capture")

    assert entries[0].uid == 12345
    assert entries[0].gid == 12346
    assert any("--capture" in command for command in runner.commands)
    assert any(command[:2] == ("docker", "cp") for command in runner.commands)
    assert (tmp_path / "capture").is_dir()


def test_volume_metadata_helper_applies_authenticated_modes_and_target_roles(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    root.mkdir(mode=0o700)
    payload = root / "state"
    payload.write_text("durable", encoding="utf-8")
    owner = (os.geteuid(), os.getegid())
    entries = (
        CloneEntry(
            data_class=CloneDataClass.RABBITMQ,
            path="rabbitmq",
            entry_type="directory",
            size=0,
            mode=0o750,
            uid=owner[0],
            gid=owner[1],
        ),
        CloneEntry(
            data_class=CloneDataClass.RABBITMQ,
            path="rabbitmq/state",
            entry_type="file",
            size=7,
            mode=0o640,
            uid=owner[0],
            gid=owner[1],
            sha256="a" * 64,
        ),
    )

    apply_volume_metadata(root, entries)

    assert root.stat().st_mode & 0o777 == 0o750
    assert payload.stat().st_mode & 0o777 == 0o640
    assert payload.stat().st_uid == owner[0]
    assert payload.stat().st_gid == owner[1]
    assert volume_metadata_matches(root, entries) is True


@pytest.mark.parametrize("mode", (0o1777, 0o3770))
def test_broker_volume_capture_and_restore_preserve_sticky_directory(
    tmp_path: Path, mode: int
) -> None:
    source = tmp_path / "broker"
    source.mkdir()
    (source / "state").write_text("broker-state", encoding="utf-8")
    source.chmod(mode)
    entries = inventory_volume_metadata(
        source, archive_root="rabbitmq", data_class=CloneDataClass.RABBITMQ
    )
    assert entries[0].mode == mode

    target = tmp_path / "restored"
    target.mkdir(mode=0o700)
    (target / "state").write_text("broker-state", encoding="utf-8")
    apply_volume_metadata(target, entries)

    assert target.stat().st_mode & 0o7777 == mode
    assert volume_metadata_matches(target, entries) is True


def test_filesystem_replace_fails_if_previous_sensitive_tree_remains(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    (target / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        "core.infrastructure.clone.filesystem_publication.shutil.rmtree",
        lambda _path, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="previous"):
        _replace_directory(source, target, plan_digest="a" * 64)


@pytest.mark.parametrize(
    "interrupted_state",
    ("after_copy", "after_target_rename", "after_new_rename"),
)
def test_filesystem_replace_reconciles_plan_bound_interruption_residue(
    tmp_path: Path,
    interrupted_state: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    (target / "old.txt").write_text("old", encoding="utf-8")
    suffix = ("b" * 64)[:16]
    temporary = target.with_name(f".{target.name}.clone-new-{suffix}")
    previous = target.with_name(f".{target.name}.clone-previous-{suffix}")
    if interrupted_state == "after_copy":
        temporary.mkdir()
        (temporary / "new.txt").write_text("new", encoding="utf-8")
    elif interrupted_state == "after_target_rename":
        target.rename(previous)
        temporary.mkdir()
        (temporary / "new.txt").write_text("new", encoding="utf-8")
    else:
        target.rename(previous)
        temporary.mkdir()
        (temporary / "new.txt").write_text("new", encoding="utf-8")
        temporary.rename(target)

    _replace_directory(source, target, plan_digest="b" * 64)

    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert not temporary.exists()
    assert not previous.exists()


def test_filesystem_replace_rebuilds_partial_initial_copy_before_promoting(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "first.txt").write_text("first", encoding="utf-8")
    (source / "second.txt").write_text("second", encoding="utf-8")
    suffix = ("c" * 64)[:16]
    temporary = target.with_name(f".{target.name}.clone-new-{suffix}")
    temporary.mkdir()
    (temporary / "first.txt").write_text("first", encoding="utf-8")

    _replace_directory(source, target, plan_digest="c" * 64)

    assert (target / "first.txt").read_text(encoding="utf-8") == "first"
    assert (target / "second.txt").read_text(encoding="utf-8") == "second"
    assert not temporary.exists()


def test_filesystem_replace_maps_publication_to_target_runtime_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    directory = source / "nested"
    directory.mkdir(parents=True)
    payload = directory / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    payload.chmod(0o640)
    link = source / "payload-link"
    link.symlink_to("nested/payload.txt")
    owner = (os.geteuid(), os.getegid())
    _replace_directory(
        source,
        target,
        plan_digest="d" * 64,
        publication_modes={
            ".": 0o750,
            "nested": 0o750,
            "nested/payload.txt": 0o640,
            "payload-link": 0o777,
        },
        owner=owner,
    )

    assert (target / "nested/payload.txt").read_text(encoding="utf-8") == "payload"
    assert (target / "payload-link").is_symlink()
    assert (target / "nested/payload.txt").stat().st_mode & 0o777 == 0o640
    assert (target / "nested/payload.txt").stat().st_uid == owner[0]
    assert (target / "nested/payload.txt").stat().st_gid == owner[1]
    assert (target / "payload-link").lstat().st_uid == owner[0]
    assert (target / "payload-link").lstat().st_gid == owner[1]


def test_filesystem_replace_refuses_wrong_target_runtime_identity_before_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")
    owner = (os.geteuid() + 1, os.getegid())

    with pytest.raises(RuntimeError, match="runtime identity"):
        _replace_directory(
            source,
            target,
            plan_digest="e" * 64,
            owner=owner,
        )

    assert not target.exists()


def test_durable_replacement_retains_active_installer_lock_inode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    lock = target / "install.lock"
    lock.write_text("active\n", encoding="utf-8")
    descriptor = lock.open("rb")
    inode = lock.stat().st_ino
    try:
        _replace_durable_children(source, target)

        assert lock.stat().st_ino == inode
        assert descriptor.fileno() >= 0
    finally:
        descriptor.close()


def test_durable_replacement_never_imports_source_configuration_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    publication_names = ("stack.toml", "compose.env", "active-revision.json")
    for name in publication_names:
        (source / name).write_text(f"source:{name}\n", encoding="utf-8")
        (target / name).write_text(f"target:{name}\n", encoding="utf-8")
    (source / "source-owned.json").write_text("source\n", encoding="utf-8")

    _replace_durable_children(source, target)

    for name in publication_names:
        assert (target / name).read_text(encoding="utf-8") == f"target:{name}\n"
    assert (target / "source-owned.json").read_text(encoding="utf-8") == "source\n"


@pytest.mark.parametrize("publication", ("host", "compact"))
@pytest.mark.parametrize("nested_shell", (False, True))
def test_durable_publication_retains_target_local_credential_files(
    tmp_path: Path, publication: str, nested_shell: bool
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_shell = source / ".flow-steward" if nested_shell else source
    target_shell = target / ".flow-steward" if nested_shell else target
    source_shell.mkdir(parents=True)
    target_shell.mkdir(parents=True)
    (source_shell / "product.json").write_text("synthetic-new-product")
    original = {}
    for name in ("secret-input", "secret-output"):
        credential = target_shell / name / "synthetic-target-credential"
        credential.parent.mkdir(mode=0o700)
        credential.write_text(f"synthetic-target:{name}")
        credential.chmod(0o600)
        metadata = credential.stat()
        original[name] = (metadata.st_ino, metadata.st_mode, metadata.st_uid, metadata.st_gid)
    if publication == "host":
        _replace_durable_children(source, target)
    else:
        entries = _durable_entries(source)
        _compact_merge_durable(source, target, entries, root_path="state", plan_digest="a" * 64)
        assert _compact_durable_matches(source, target, entries, root_path="state")

    assert (target_shell / "product.json").read_text() == "synthetic-new-product"
    for name in ("secret-input", "secret-output"):
        credential = target_shell / name / "synthetic-target-credential"
        assert credential.read_text() == f"synthetic-target:{name}"
        metadata = credential.stat()
        observed = (metadata.st_ino, metadata.st_mode, metadata.st_uid, metadata.st_gid)
        assert observed == original[name]


def test_compact_durable_replacement_merges_nested_runtime_without_losing_shell(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-state"
    target = tmp_path / "target-state"
    source_shell = source / ".flow-steward"
    target_shell = target / ".flow-steward"
    source_shell.mkdir(parents=True)
    target_shell.mkdir(parents=True)
    publication_names = ("stack.toml", "compose.env", "active-revision.json")
    for name in publication_names:
        (source_shell / name).write_text(f"source:{name}\n", encoding="utf-8")
        (target_shell / name).write_text(f"target:{name}\n", encoding="utf-8")
    (source_shell / "source-owned.json").write_text("source\n", encoding="utf-8")
    (target_shell / "old-source-owned.json").write_text("old\n", encoding="utf-8")
    (source / "compact-bootstrap.json").write_text("source-bootstrap\n", encoding="utf-8")
    (target / "compact-bootstrap.json").write_text("target-bootstrap\n", encoding="utf-8")

    _replace_durable_children(source, target)

    for name in publication_names:
        assert (target_shell / name).read_text(encoding="utf-8") == f"target:{name}\n"
    assert (target_shell / "source-owned.json").read_text(encoding="utf-8") == "source\n"
    assert not (target_shell / "old-source-owned.json").exists()
    assert (target / "compact-bootstrap.json").read_text(encoding="utf-8") == ("source-bootstrap\n")


@pytest.mark.parametrize("invalid_side", ("source", "target"))
@pytest.mark.parametrize("invalid_type", ("symlink", "file"))
def test_durable_replacement_rejects_non_directory_nested_shell_before_mutation(
    tmp_path: Path,
    invalid_side: str,
    invalid_type: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / "retained.txt").write_text("retained", encoding="utf-8")
    selected = source if invalid_side == "source" else target
    shell = selected / ".flow-steward"
    if invalid_type == "symlink":
        shell.symlink_to("retained.txt")
    else:
        shell.write_text("invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="nested shell"):
        _replace_durable_children(source, target)

    assert (target / "retained.txt").read_text(encoding="utf-8") == "retained"


@pytest.mark.parametrize("invalid_side", ("source", "target"))
@pytest.mark.parametrize("invalid_type", ("symlink", "file"))
def test_compact_durable_replacement_rejects_non_directory_nested_shell_before_mutation(
    tmp_path: Path,
    invalid_side: str,
    invalid_type: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / "retained.txt").write_text("retained", encoding="utf-8")
    selected = source if invalid_side == "source" else target
    shell = selected / ".flow-steward"
    if invalid_type == "symlink":
        shell.symlink_to("retained.txt")
    else:
        shell.write_text("invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="nested shell"):
        _compact_merge_durable(
            source,
            target,
            (),
            root_path="state",
            plan_digest="a" * 64,
        )

    assert (target / "retained.txt").read_text(encoding="utf-8") == "retained"
    assert _compact_durable_matches(source, target, (), root_path="state") is False


@pytest.mark.parametrize("invalid_side", ("source", "target"))
@pytest.mark.parametrize("invalid_type", ("symlink", "file"))
def test_compact_publish_rejects_non_directory_state_root_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_side: str,
    invalid_type: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_state = source / "state"
    target_state = target / "state"
    source_state.mkdir()
    target_state.mkdir()
    artifacts = target / "artifacts"
    artifacts.mkdir()
    (artifacts / "retained.txt").write_text("retained", encoding="utf-8")
    selected = source_state if invalid_side == "source" else target_state
    selected.rmdir()
    if invalid_type == "symlink":
        selected.symlink_to("artifacts")
    else:
        selected.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(compact_volume_publication, "_TARGET", target)

    with pytest.raises(ValueError, match="state root"):
        compact_volume_publication.publish(source, (), plan_digest="a" * 64)

    assert (artifacts / "retained.txt").read_text(encoding="utf-8") == "retained"


def _compact_entries(source: Path) -> tuple[CloneEntry, ...]:
    owner = (os.geteuid(), os.getegid())
    payload = source / "payload.txt"
    return (
        CloneEntry(
            data_class=CloneDataClass.LOCAL_ARTIFACTS,
            path="artifacts",
            entry_type="directory",
            size=0,
            mode=source.stat().st_mode & 0o777,
            uid=owner[0],
            gid=owner[1],
        ),
        CloneEntry(
            data_class=CloneDataClass.LOCAL_ARTIFACTS,
            path="artifacts/payload.txt",
            entry_type="file",
            size=payload.stat().st_size,
            mode=payload.stat().st_mode & 0o777,
            uid=owner[0],
            gid=owner[1],
            sha256=__import__("hashlib").sha256(payload.read_bytes()).hexdigest(),
        ),
    )


def _durable_entries(source: Path) -> tuple[CloneEntry, ...]:
    return inventory_volume_metadata(
        source,
        archive_root="state",
        data_class=CloneDataClass.DURABLE_STATE,
    )


def _compact_publication_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, tuple[CloneEntry, ...]]:
    source = tmp_path / "source"
    target = tmp_path / "target"
    for root_name in ("artifacts", "extensions", "state"):
        (source / root_name).mkdir(parents=True)
        (target / root_name).mkdir(parents=True)
        (source / root_name / "payload.txt").write_text("new", encoding="utf-8")
    (target / "artifacts/payload.txt").write_text("old", encoding="utf-8")
    entries = tuple(
        entry
        for data_class, root_name in (
            (CloneDataClass.LOCAL_ARTIFACTS, "artifacts"),
            (CloneDataClass.EXTENSIONS, "extensions"),
            (CloneDataClass.DURABLE_STATE, "state"),
        )
        for entry in inventory_volume_metadata(
            source / root_name,
            archive_root=root_name,
            data_class=data_class,
        )
    )
    monkeypatch.setattr(compact_volume_publication, "_TARGET", target)
    return source, target, entries


def _tree_evidence(root: Path) -> tuple[bytes, int, int, int, int, int, int]:
    payload = root / "payload.txt"
    root_stat = root.stat()
    payload_stat = payload.stat()
    return (
        payload.read_bytes(),
        root_stat.st_uid,
        root_stat.st_gid,
        root_stat.st_mode & 0o7777,
        payload_stat.st_uid,
        payload_stat.st_gid,
        payload_stat.st_mode & 0o7777,
    )


def test_compact_publication_preflights_later_root_before_artifact_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, entries = _compact_publication_fixture(tmp_path, monkeypatch)
    evidence = _tree_evidence(target / "artifacts")
    (target / "extensions").rmdir()
    (target / "extensions").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(ValueError, match="real directories"):
        compact_volume_publication.publish(source, entries, plan_digest="a" * 64)

    assert _tree_evidence(target / "artifacts") == evidence


def test_compact_publication_preflights_state_residue_before_artifact_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, entries = _compact_publication_fixture(tmp_path, monkeypatch)
    evidence = _tree_evidence(target / "artifacts")
    residue = target / f"state/.payload.txt.clone-new-{'b' * 16}"
    residue.write_text("foreign", encoding="utf-8")

    with pytest.raises(RuntimeError, match="foreign plan residue"):
        compact_volume_publication.publish(source, entries, plan_digest="a" * 64)

    assert _tree_evidence(target / "artifacts") == evidence
    assert residue.read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("residue_for_source", (True, False))
def test_compact_durable_rejects_foreign_residue_before_mutation(
    tmp_path: Path,
    residue_for_source: bool,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    (target / "payload.txt").write_text("old", encoding="utf-8")
    (target / "unrelated.txt").write_text("retained", encoding="utf-8")
    residue_name = "payload.txt" if residue_for_source else "removed.txt"
    residue = target / f".{residue_name}.clone-new-{'b' * 16}"
    residue.write_text("foreign", encoding="utf-8")
    entries = _durable_entries(source)

    with pytest.raises(RuntimeError, match="foreign plan residue"):
        _compact_merge_durable(
            source,
            target,
            entries,
            root_path="state",
            plan_digest="a" * 64,
        )

    assert (target / "payload.txt").read_text(encoding="utf-8") == "old"
    assert (target / "unrelated.txt").read_text(encoding="utf-8") == "retained"
    assert residue.read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("temporary_payload", ("new", "partial"))
def test_compact_durable_reconciles_new_child_temporary(
    tmp_path: Path,
    temporary_payload: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    temporary = target / f".payload.txt.clone-new-{'c' * 16}"
    temporary.write_text(temporary_payload, encoding="utf-8")

    _compact_merge_durable(
        source,
        target,
        _durable_entries(source),
        root_path="state",
        plan_digest="c" * 64,
    )

    assert (target / "payload.txt").read_text(encoding="utf-8") == "new"
    assert not temporary.exists()


def test_compact_durable_reconciles_nested_shell_child_temporary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_shell = source / ".flow-steward"
    target_shell = target / ".flow-steward"
    source_shell.mkdir(parents=True)
    target_shell.mkdir(parents=True)
    (source_shell / "source-owned.json").write_text("new", encoding="utf-8")
    (target_shell / "stack.toml").write_text("retained", encoding="utf-8")
    temporary = target_shell / f".source-owned.json.clone-new-{'d' * 16}"
    temporary.write_text("new", encoding="utf-8")

    _compact_merge_durable(
        source,
        target,
        _durable_entries(source),
        root_path="state",
        plan_digest="d" * 64,
    )

    assert (target_shell / "source-owned.json").read_text(encoding="utf-8") == "new"
    assert (target_shell / "stack.toml").read_text(encoding="utf-8") == "retained"
    assert not temporary.exists()


def test_compact_replace_rejects_foreign_plan_residue(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "artifacts"
    source.mkdir()
    target.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    (target / "old.txt").write_text("old", encoding="utf-8")
    foreign = target.with_name(f".{target.name}.clone-new-{'b' * 16}")
    foreign.mkdir()
    entries = _compact_entries(source)

    with pytest.raises(RuntimeError, match="foreign plan residue"):
        _compact_replace_tree(source, target, entries, "a" * 64)

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    "interrupted_state",
    ("after_copy", "after_target_rename", "after_new_rename"),
)
def test_compact_replace_reconciles_current_plan_interruption_residue(
    tmp_path: Path,
    interrupted_state: str,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "artifacts"
    source.mkdir()
    target.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    (target / "old.txt").write_text("old", encoding="utf-8")
    suffix = "c" * 16
    temporary = target.with_name(f".{target.name}.clone-new-{suffix}")
    previous = target.with_name(f".{target.name}.clone-previous-{suffix}")
    if interrupted_state == "after_copy":
        temporary.mkdir()
        (temporary / "payload.txt").write_text("new", encoding="utf-8")
    elif interrupted_state == "after_target_rename":
        target.rename(previous)
        temporary.mkdir()
        (temporary / "payload.txt").write_text("new", encoding="utf-8")
    else:
        target.rename(previous)
        temporary.mkdir()
        (temporary / "payload.txt").write_text("new", encoding="utf-8")
        temporary.rename(target)

    _compact_replace_tree(source, target, _compact_entries(source), "c" * 64)

    assert (target / "payload.txt").read_text(encoding="utf-8") == "new"
    assert not temporary.exists()
    assert not previous.exists()


def test_compact_replace_promotes_complete_temporary_for_new_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "artifacts"
    source.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    temporary = target.with_name(f".{target.name}.clone-new-{'e' * 16}")
    temporary.mkdir()
    (temporary / "payload.txt").write_text("new", encoding="utf-8")

    _compact_replace_tree(source, target, _compact_entries(source), "e" * 64)

    assert (target / "payload.txt").read_text(encoding="utf-8") == "new"
    assert not temporary.exists()


def test_compact_replace_rebuilds_mismatched_temporary_for_new_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "artifacts"
    unrelated = tmp_path / "unrelated.txt"
    source.mkdir()
    (source / "payload.txt").write_text("new", encoding="utf-8")
    unrelated.write_text("retained", encoding="utf-8")
    temporary = target.with_name(f".{target.name}.clone-new-{'f' * 16}")
    temporary.mkdir()
    (temporary / "payload.txt").write_text("stale", encoding="utf-8")

    _compact_replace_tree(source, target, _compact_entries(source), "f" * 64)

    assert (target / "payload.txt").read_text(encoding="utf-8") == "new"
    assert unrelated.read_text(encoding="utf-8") == "retained"
    assert not temporary.exists()


def test_compact_publication_verification_rejects_replacement_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source = source_root / "artifacts"
    target_root = tmp_path / "target"
    target = target_root / "artifacts"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "payload.txt").write_text("new", encoding="utf-8")
    (target / "payload.txt").write_text("new", encoding="utf-8")
    residue = target.with_name(f".{target.name}.clone-previous-{'d' * 16}")
    residue.mkdir()
    monkeypatch.setattr(compact_volume_publication, "_TARGET", target_root)

    assert (
        compact_volume_publication.publication_matches(
            source_root,
            _compact_entries(source),
        )
        is False
    )
