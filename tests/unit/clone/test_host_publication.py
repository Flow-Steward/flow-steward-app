from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.clone.contracts import CloneDataClass
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.clone.installation_restore_effects import InstalledCloneRestoreEffects
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata


def _publication():
    from core.infrastructure.clone import host_directory_publication

    return host_directory_publication


def _fixture(tmp_path):
    source = tmp_path / "stage/extensions"
    source.mkdir(parents=True)
    (source / "new.txt").write_text("source marker")
    (source / "new.txt").chmod(0o640)
    (source / "nested").mkdir(mode=0o750)
    (source / "nested/payload").write_text("nested source")
    (source / "link").symlink_to("nested/payload")
    target = tmp_path / "external/extensions"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("target marker")
    entries = inventory_volume_metadata(
        source, archive_root="extensions", data_class=CloneDataClass.EXTENSIONS
    )
    return source, target, entries


class _HostTransfer:
    def publish_host_filesystem(
        self,
        source,
        target,
        entries,
        *,
        plan_digest,
        owner,
        operation_id=None,
        verify=False,
        validate_only=False,
    ):
        module = _publication()
        if verify:
            return module.publication_matches(source, target, entries, owner=owner)
        if validate_only:
            module.validate_publication(
                source, target, entries, plan_digest=plan_digest, owner=owner
            )
        else:
            module.publish(source, target, entries, plan_digest=plan_digest, owner=owner)
        return True

    def publish_compact_data(self, *_args, **kwargs):
        assert kwargs["durable_state_only"] is True
        return True


@pytest.mark.parametrize("profile", ["scalable", "compact"])
def test_host_restore_preserves_exact_bind_root_without_parent_write_or_root_rename(
    tmp_path, monkeypatch, profile
):
    source, target, entries = _fixture(tmp_path)
    inode = target.stat().st_ino
    rename = Path.rename

    def bind_aware_rename(path, destination):
        if path == target:
            raise OSError(errno.EBUSY, "bind mount root cannot be renamed")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", bind_aware_rename)
    prepared = SimpleNamespace(
        operation_id="clone-owned-operation",
        staged_root=str(source.parent),
        manifest=SimpleNamespace(entries=entries),
        plan=SimpleNamespace(
            plan_digest="a" * 64,
            retained_shell=SimpleNamespace(
                configuration=DeploymentConfig(
                    environment="production",
                    deployment_profile=profile,
                    data_placement="host_directories",
                ),
                host_uid=os.geteuid(),
                host_gid=os.getegid(),
                filesystem_roots={CloneDataClass.EXTENSIONS.value: str(target)},
            ),
        ),
    )
    effects = InstalledCloneRestoreEffects(
        operation_root=tmp_path / "operations",
        services=None,
        service_names=(),
        volumes=_HostTransfer(),
        target_roots={CloneDataClass.EXTENSIONS: target},
        backup=None,
        import_secrets=lambda _value: None,
        regenerate_configuration=lambda _value: None,
        activate_release=lambda _value: None,
        verify_database=lambda _value: True,
        secrets_match=lambda _value: True,
        configuration_matches=lambda _value: True,
        release_activated=lambda _value: True,
        verify_target=lambda _value: None,
    )
    effects.replace_filesystems(prepared)

    assert target.stat().st_ino == inode
    assert (target / "new.txt").read_text() == "source marker"
    assert not (target / "old.txt").exists()
    assert effects.filesystems_replaced(prepared)
    assert not tuple(target.parent.glob(".extensions.clone-*"))


def test_host_publication_keeps_mount_root_inode_and_normalizes_authenticated_ownership(tmp_path):
    source, target, entries = _fixture(tmp_path)
    inode = target.stat().st_ino
    # Source identities are evidence, not the host target's runtime identity.
    entries = tuple(entry.model_copy(update={"uid": 29001, "gid": 29002}) for entry in entries)
    owner = (os.geteuid(), os.getegid())
    module = _publication()
    module.publish(source, target, entries, plan_digest="b" * 64, owner=owner)
    module.publish(source, target, entries, plan_digest="b" * 64, owner=owner)

    assert target.stat().st_ino == inode
    assert module.publication_matches(source, target, entries, owner=owner)
    assert (target / "new.txt").stat().st_mode & 0o777 == 0o640
    link_stat = (target / "link").lstat()
    link_owner = (link_stat.st_uid, link_stat.st_gid)
    assert link_owner == owner
    assert (target / "link").readlink() == Path("nested/payload")
    assert not (target / "old.txt").exists()


@pytest.mark.parametrize(
    "prefix", ["partial", "prepared", "old-renamed", "new-renamed", "old-only", "new-only"]
)
def test_host_publication_resumes_each_child_crash_prefix(tmp_path, prefix):
    source, target, entries = _fixture(tmp_path)
    digest = "c" * 64
    temporary = target / (".new.txt.clone-new-" + "c" * 16)
    previous = target / (".new.txt.clone-previous-" + "c" * 16)
    current = target / "new.txt"
    current.write_text("old source-name payload")
    if prefix in {"partial", "prepared", "old-renamed", "new-only"}:
        temporary.write_text("partial" if prefix == "partial" else "source marker")
        temporary.chmod(0o640)
    if prefix in {"old-renamed", "new-renamed", "old-only"}:
        current.rename(previous)
    if prefix == "new-renamed":
        current.write_text("source marker")
        current.chmod(0o640)
    if prefix == "new-only":
        current.unlink()

    module = _publication()
    owner = (os.geteuid(), os.getegid())
    module.publish(source, target, entries, plan_digest=digest, owner=owner)
    assert module.publication_matches(source, target, entries, owner=owner)
    assert not temporary.exists()
    assert not previous.exists()


@pytest.mark.parametrize("residue", ["foreign", "unknown-child", "ambiguous", "symlink"])
def test_host_publication_refuses_untrusted_residue_before_target_only_deletion(tmp_path, residue):
    source, target, entries = _fixture(tmp_path)
    temporary = target / (".new.txt.clone-new-" + "d" * 16)
    if residue == "foreign":
        (target / (".new.txt.clone-new-" + "e" * 16)).write_text("foreign")
    elif residue == "unknown-child":
        (target / (".unknown.clone-new-" + "d" * 16)).write_text("unknown")
    elif residue == "symlink":
        temporary.symlink_to(tmp_path / "outside")
    else:
        (target / "new.txt").write_text("old")
        temporary.write_text("new")
        (target / (".new.txt.clone-previous-" + "d" * 16)).write_text("old")

    module = _publication()
    owner = (os.geteuid(), os.getegid())
    with pytest.raises((ValueError, RuntimeError)):
        module.publish(source, target, entries, plan_digest="d" * 64, owner=owner)
    assert (target / "old.txt").read_text() == "target marker"


def test_host_publication_does_not_follow_target_symlink_or_source_link(tmp_path):
    source, target, entries = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    (target / "nested").symlink_to(outside, target_is_directory=True)
    module = _publication()
    owner = (os.geteuid(), os.getegid())
    module.publish(source, target, entries, plan_digest="f" * 64, owner=owner)
    assert (outside / "keep").read_text() == "untouched"
    assert not (outside / "payload").exists()
    assert not (target / "nested").is_symlink()


def test_host_publication_rejects_invalid_later_child_before_any_mutation(tmp_path):
    source, target, entries = _fixture(tmp_path)
    (source / "nested/payload").write_text("changed after authentication")
    module = _publication()
    owner = (os.geteuid(), os.getegid())
    with pytest.raises(ValueError):
        module.publish(source, target, entries, plan_digest="a" * 64, owner=owner)
    assert (target / "old.txt").read_text() == "target marker"


@pytest.mark.parametrize("root", ["source", "target"])
def test_host_publication_rejects_symlink_roots_before_mutation(tmp_path, root):
    source, target, entries = _fixture(tmp_path)
    alias = tmp_path / "root-alias"
    alias.symlink_to(source if root == "source" else target, target_is_directory=True)
    module = _publication()
    owner = (os.geteuid(), os.getegid())
    with pytest.raises(ValueError):
        module.publish(
            alias if root == "source" else source,
            alias if root == "target" else target,
            entries,
            plan_digest="a" * 64,
            owner=owner,
        )
    assert (target / "old.txt").read_text() == "target marker"


def test_host_verification_detects_metadata_or_unmatched_child_residue(tmp_path):
    source, target, entries = _fixture(tmp_path)
    owner = (os.geteuid(), os.getegid())
    module = _publication()
    module.publish(source, target, entries, plan_digest="a" * 64, owner=owner)
    (target / "new.txt").chmod(0o600)
    assert not module.publication_matches(source, target, entries, owner=owner)
    (target / "new.txt").chmod(0o640)
    (target / ".foreign.clone-new-1111111111111111").write_text("foreign")
    assert not module.publication_matches(source, target, entries, owner=owner)


def test_host_publication_resumes_after_authenticated_symlink_creation(tmp_path, monkeypatch):
    source, target, entries = _fixture(tmp_path)
    module = _publication()
    owner = (os.geteuid(), os.getegid())
    symlink_to = Path.symlink_to

    def interrupted_symlink(path, destination, target_is_directory=False):
        symlink_to(path, destination, target_is_directory=target_is_directory)
        if path.parent == target:
            raise RuntimeError("interrupted after symlink creation")

    with monkeypatch.context() as interruption:
        interruption.setattr(Path, "symlink_to", interrupted_symlink)
        with pytest.raises(RuntimeError, match="interrupted after symlink creation"):
            module.publish(source, target, entries, plan_digest="a" * 64, owner=owner)

    module.publish(source, target, entries, plan_digest="a" * 64, owner=owner)
    assert module.publication_matches(source, target, entries, owner=owner)
    assert (target / "link").readlink() == Path("nested/payload")


def test_host_publication_resumes_after_removing_existing_target_symlink(tmp_path, monkeypatch):
    source, target, entries = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("unrelated bytes")
    current = target / "new.txt"
    current.symlink_to(outside)
    module = _publication()
    owner = (os.geteuid(), os.getegid())
    rename, unlink = Path.rename, Path.unlink

    def interrupted_rename(path, destination):
        old_link = path == current and path.is_symlink()
        result = rename(path, destination)
        if old_link:
            raise RuntimeError("interrupted after target symlink removal")
        return result

    def interrupted_unlink(path, missing_ok=False):
        old_link = path == current and path.is_symlink()
        unlink(path, missing_ok=missing_ok)
        if old_link:
            raise RuntimeError("interrupted after target symlink removal")

    with monkeypatch.context() as interruption:
        interruption.setattr(Path, "rename", interrupted_rename)
        interruption.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(RuntimeError, match="interrupted after target symlink removal"):
            module.publish(source, target, entries, plan_digest="b" * 64, owner=owner)

    module.publish(source, target, entries, plan_digest="b" * 64, owner=owner)
    assert module.publication_matches(source, target, entries, owner=owner)
    assert outside.read_text() == "unrelated bytes"
