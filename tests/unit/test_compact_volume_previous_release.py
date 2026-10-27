"""AC17-R163: a Compact volume an earlier release created starts on this release.

Found by the auditor updating a real Compact installation: item 5 step 1 moved the
extension tree to 02775, the volume check then refused the 0750 every existing volume
has ("Compact volume mode is unsafe: extensions"), and the runtime crash-looped. The
tests built fresh volumes only. These start from the previous release's own layout --
the fixture is its volume.py, verbatim -- and from the trees its writers left.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core.infrastructure.extension_runtime import tree_modes
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout, prepare_compact_volume

ROOT = Path(__file__).resolve().parents[2]
_PREVIOUS = json.loads(
    (
        ROOT / "tests/fixtures/lifecycle/previous-release-e1b916b89/compact-volume-layout.json"
    ).read_text(encoding="utf-8")
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _modes(section: str) -> dict[str, int]:
    return {key: int(value, 8) for key, value in _PREVIOUS["extension_tree"][section].items()}


def _previous_volume(root: Path) -> dict[str, Path]:
    """The directories the previous release created, and the trees its writers left."""
    root.chmod(0o755)
    for entry in _PREVIOUS["directories"]:
        path = root / entry["path"]
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(int(entry["mode"], 8))
    extensions = root / "extensions"
    bundled, installed, private = _modes("bundled"), _modes("installed_release"), _modes("private")
    paths = {
        "bundled_dir": extensions / "acme_parser",
        "bundled_file": extensions / "acme_parser" / "extension.yaml",
        "bundled_exec": extensions / "acme_parser" / "bin" / "run",
        "release_dir": extensions / ".releases" / "acme_cms" / "1.0.0",
        "release_file": extensions / ".releases" / "acme_cms" / "1.0.0" / "manifest.json",
        "record": extensions / ".cleanup" / "pending.json",
        "staging": extensions / ".staging-abc",
    }
    for key in ("bundled_exec", "release_file", "record"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    for key in ("bundled_file", "bundled_exec", "release_file", "record"):
        paths[key].write_text("x", encoding="utf-8")
    paths["staging"].mkdir()
    for key, mode in (
        ("bundled_dir", bundled["directory"]),
        (None, bundled["directory"]),
        ("bundled_file", bundled["file"]),
        ("bundled_exec", bundled["executable"]),
        ("release_dir", installed["directory"]),
        ("release_file", installed["file"]),
        ("record", private["record"]),
        ("staging", private["staging_directory"]),
    ):
        (paths[key] if key else paths["bundled_exec"].parent).chmod(mode)
    for directory in (extensions / ".releases", extensions / ".releases" / "acme_cms"):
        directory.chmod(installed["directory"])
    (extensions / ".cleanup").chmod(private["staging_directory"])
    return paths


def test_the_previous_releases_volume_starts_and_its_extension_tree_is_normalized(
    tmp_path: Path,
) -> None:
    paths = _previous_volume(tmp_path)
    assert _mode(tmp_path / "extensions") == 0o750  # non-vacuity: the refused mode

    prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())

    assert _mode(tmp_path / "extensions") == tree_modes.EXTENSION_DIRECTORY_MODE
    for key in ("bundled_dir", "release_dir"):
        assert _mode(paths[key]) == tree_modes.EXTENSION_DIRECTORY_MODE, key
    assert _mode(paths["bundled_file"]) == tree_modes.EXTENSION_FILE_MODE
    assert _mode(paths["release_file"]) == tree_modes.EXTENSION_FILE_MODE
    assert _mode(paths["bundled_exec"]) == tree_modes.EXTENSION_EXECUTABLE_MODE
    # what a writer made private on purpose stays private
    assert _mode(paths["record"]) == 0o600
    assert _mode(paths["staging"]) == 0o700
    # every other directory of the previous layout is accepted unchanged
    for entry in _PREVIOUS["directories"][1:]:
        assert _mode(tmp_path / entry["path"]) == int(entry["mode"], 8), entry["path"]


def test_a_symlink_in_the_tree_is_not_followed_out_of_it(tmp_path: Path) -> None:
    """The peer's condition: a link to a file outside the root leaves that file alone."""
    paths = _previous_volume(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o640)
    (outside / "tree").mkdir()
    (outside / "tree").chmod(0o750)
    (paths["bundled_dir"] / "link").symlink_to(target)
    (paths["bundled_dir"] / "dirlink").symlink_to(outside / "tree", target_is_directory=True)

    prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())

    assert _mode(target) == 0o640
    assert _mode(outside / "tree") == 0o750
    assert (paths["bundled_dir"] / "link").is_symlink()


def test_a_normalization_killed_half_way_is_finished_by_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _previous_volume(tmp_path)
    real_chmod = os.chmod
    calls = {"n": 0}

    def dies_on_the_third(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # stands for kill -9: nothing after it runs
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(tree_modes.os, "chmod", dies_on_the_third)
    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(KeyboardInterrupt):
        prepare_compact_volume(tmp_path, layout=layout)
    monkeypatch.setattr(tree_modes.os, "chmod", real_chmod)
    assert _mode(tmp_path / "extensions") == 0o750  # the record is written last

    prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())

    assert _mode(tmp_path / "extensions") == tree_modes.EXTENSION_DIRECTORY_MODE
    assert _mode(paths["bundled_file"]) == tree_modes.EXTENSION_FILE_MODE
    assert _mode(paths["release_file"]) == tree_modes.EXTENSION_FILE_MODE


def test_a_normalized_volume_is_not_walked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _previous_volume(tmp_path)
    prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())
    walked = []
    monkeypatch.setattr(
        "core.infrastructure.runtime.compact.volume.normalize_previous_tree",
        lambda fd: walked.append(fd) or 0,
    )

    prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())

    assert walked == []


def test_a_mode_wider_than_the_model_is_still_refused(tmp_path: Path) -> None:
    """The green direction of the check: only the previous release's mode is admitted."""
    tmp_path.chmod(0o755)
    extensions = tmp_path / "extensions"
    extensions.mkdir()
    extensions.chmod(0o777)

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="mode is unsafe: extensions"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert _mode(extensions) == 0o777


def test_an_orphan_release_an_earlier_release_left_is_adopted_by_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both profiles: an install the previous release interrupted after publishing is
    retried by this one, which stages the same archive with the new modes."""
    from unittest.mock import MagicMock

    from core.application.extensions.managed_release_service import ManagedReleaseService

    installed = _modes("installed_release")
    staged, published = tmp_path / "staged", tmp_path / "release"
    for root in (staged, published):
        (root / "bin").mkdir(parents=True)
        (root / "extension.yaml").write_text("id: x\n", encoding="utf-8")
        (root / "bin" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "bin" / "run").chmod(0o755)
    ManagedReleaseService._normalize_managed_release_tree_modes(staged)
    for path, mode in (
        (published, installed["directory"]),
        (published / "bin", installed["directory"]),
        (published / "extension.yaml", installed["file"]),
        (published / "bin" / "run", installed["executable"]),
    ):
        path.chmod(mode)
    service = ManagedReleaseService(context=MagicMock())
    monkeypatch.setattr(service, "_assert_managed_release_unreferenced", lambda **_: None)

    created = service._publish_or_adopt_managed_release(
        bl=MagicMock(),
        extension_id="x",
        staged_root=staged,
        release_root=published,
        staged_fingerprint=ManagedReleaseService._managed_release_tree_fingerprint(staged),
    )

    assert created is False  # adopted, not refused as "does not match"
    assert _mode(published) == tree_modes.EXTENSION_DIRECTORY_MODE


def _spec(tree: str, mode: int):
    from core.infrastructure.runtime.compact.volume import _DirectorySpec

    # Compact's own owner is someone else: the host user owns what a switch bound here
    return _DirectorySpec(tree, os.getuid() + 1, os.getgid(), mode)


@pytest.mark.parametrize(("tree", "mode"), [("artifacts", 0o2770), ("extensions", 0o2775)])
def test_a_host_directory_bound_after_a_switch_keeps_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: str, mode: int
) -> None:
    """Item 5, step 4: after Scalable to Compact these trees are Scalable's host
    directories, owned by the host user -- nothing is re-owned -- so under that
    placement the volume check compares their group and mode, not their owner."""
    from core.infrastructure.runtime.compact import volume

    (tmp_path / tree).mkdir()
    (tmp_path / tree).chmod(mode)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        spec = _spec(tree, mode)
        with pytest.raises(ValueError, match="ownership is unsafe"):
            volume._ensure_directory(root_fd, spec)

        monkeypatch.setattr(
            volume, "_runtime_env", {"FS_COMPACT_DATA_PLACEMENT": "host_directories"}
        )
        assert tree in volume._host_bound_trees()
        volume._ensure_directory(root_fd, _spec(tree, mode), host_bound=True)
    finally:
        os.close(root_fd)


def test_a_host_directory_the_switch_did_not_normalize_is_still_refused(tmp_path: Path) -> None:
    """The group and the mode are still the model's: an unnormalized 0755 tree is not."""
    from core.infrastructure.runtime.compact import volume

    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts").chmod(0o755)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        spec = _spec("artifacts", 0o2770)
        with pytest.raises(ValueError, match="mode is unsafe: artifacts"):
            volume._ensure_directory(root_fd, spec, host_bound=True)
    finally:
        os.close(root_fd)


def test_only_the_bound_trees_are_host_owned_and_only_under_that_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.runtime.compact import volume

    monkeypatch.setattr(volume, "_runtime_env", {})
    assert volume._host_bound_trees() == frozenset()
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_DATA_PLACEMENT": "host_directories"})
    assert volume._host_bound_trees() == {"artifacts", "extensions"}
    # AC17-R169: in object storage the artifacts are the volume's own, and checked as such
    for backend, bound in (("local_fs", {"artifacts", "extensions"}), ("s3", {"extensions"})):
        monkeypatch.setattr(
            volume,
            "_runtime_env",
            {"FS_COMPACT_DATA_PLACEMENT": "host_directories", "ARTIFACT_STORAGE_BACKEND": backend},
        )
        assert volume._host_bound_trees() == bound
