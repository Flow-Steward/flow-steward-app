"""Item 5, step 3: the trees a Scalable installation created get the shared model.

On a switch to Compact they stay in Scalable's host directories (step 2 mounts them),
and Compact's roles reach them through fixed groups; so they first get that group and
the model's modes -- nothing copied, moved or re-owned. The auditor's conditions: a link
out of the root is not followed; a run killed half-way is finished by the next; private
entries and owners stay; a host group already on a model gid is refused by name before
anything changes; and the result says what changed and what was left alone.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact import shared_trees
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout

_LAYOUT = CompactVolumeLayout.for_current_user()  # the test cannot give away its files


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _scalable_tree(root: Path) -> dict[str, Path]:
    """What a Scalable installation's umask 022 leaves, plus what a writer made private."""
    paths = {
        "root": root,
        "dir": root / "run-1",
        "file": root / "run-1" / "report.json",
        "exec": root / "run-1" / "tool.sh",
        "private_file": root / "run-1" / "token.json",
        "private_dir": root / "cache",
        "inside_private": root / "cache" / "entry.bin",
    }
    paths["dir"].mkdir(parents=True)
    paths["private_dir"].mkdir()
    for key in ("file", "exec", "private_file", "inside_private"):
        paths[key].write_text("x", encoding="utf-8")
    for key, mode in (
        ("root", 0o755),
        ("dir", 0o755),
        ("file", 0o644),
        ("exec", 0o755),
        ("private_file", 0o600),
        ("private_dir", 0o700),
        ("inside_private", 0o644),
    ):
        paths[key].chmod(mode)
    return paths


def _model(tree: str = "artifacts") -> shared_trees.TreeModel:
    return shared_trees.tree_models(_LAYOUT)[tree]


def test_a_scalable_tree_is_brought_to_the_model(tmp_path: Path) -> None:
    paths = _scalable_tree(tmp_path / "artifacts")
    owners = {key: path.lstat().st_uid for key, path in paths.items()}

    report = shared_trees.normalize_tree(paths["root"], "artifacts", _model())

    assert _mode(paths["root"]) == 0o2770
    assert _mode(paths["dir"]) == 0o2770
    assert _mode(paths["file"]) == 0o660
    assert _mode(paths["exec"]) == 0o770
    # private stays private, and so does what is under a private directory
    assert _mode(paths["private_file"]) == 0o600
    assert _mode(paths["private_dir"]) == 0o700
    assert _mode(paths["inside_private"]) == 0o644
    assert {key: path.lstat().st_uid for key, path in paths.items()} == owners
    assert (report.directories, report.files, report.private) == (1, 2, 2)
    assert report.summary().startswith("Normalized 1 directories and 2 files under ")
    assert "left alone: 2 private entries" in report.summary()


def test_the_extension_tree_gets_the_extension_model(tmp_path: Path) -> None:
    paths = _scalable_tree(tmp_path / "extensions")

    shared_trees.normalize_tree(paths["root"], "extensions", _model("extensions"))

    assert (_mode(paths["dir"]), _mode(paths["file"]), _mode(paths["exec"])) == (
        0o2775,
        0o664,
        0o775,
    )


def test_a_link_out_of_the_root_is_not_followed(tmp_path: Path) -> None:
    paths = _scalable_tree(tmp_path / "artifacts")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x", encoding="utf-8")
    (outside / "secret").chmod(0o644)
    outside.chmod(0o755)
    (paths["dir"] / "to-file").symlink_to(outside / "secret")
    (paths["dir"] / "to-dir").symlink_to(outside, target_is_directory=True)

    report = shared_trees.normalize_tree(paths["root"], "artifacts", _model())

    assert _mode(outside / "secret") == 0o644
    assert _mode(outside) == 0o755
    assert report.links == 2


def test_a_root_that_is_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "artifacts").symlink_to(real, target_is_directory=True)

    model = _model()
    with pytest.raises(ValueError, match="not a real directory"):
        shared_trees.normalize_tree(tmp_path / "artifacts", "artifacts", model)


def test_a_run_killed_half_way_is_finished_by_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _scalable_tree(tmp_path / "artifacts")
    real_chmod = os.chmod
    calls = {"n": 0}

    def dies_on_the_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # stands for kill -9
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(shared_trees.os, "chmod", dies_on_the_second)
    model = _model()
    with pytest.raises(KeyboardInterrupt):
        shared_trees.normalize_tree(paths["root"], "artifacts", model)
    monkeypatch.setattr(shared_trees.os, "chmod", real_chmod)
    assert _mode(paths["root"]) == 0o755  # the record is written last

    shared_trees.normalize_tree(paths["root"], "artifacts", _model())

    assert _mode(paths["root"]) == 0o2770
    assert (_mode(paths["file"]), _mode(paths["exec"])) == (0o660, 0o770)


def test_a_normalized_tree_is_not_walked_again(tmp_path: Path) -> None:
    paths = _scalable_tree(tmp_path / "artifacts")
    shared_trees.normalize_tree(paths["root"], "artifacts", _model())

    again = shared_trees.normalize_tree(paths["root"], "artifacts", _model())

    assert again.already is True
    assert "already in the artifacts model" in again.summary()


def test_a_host_group_on_a_model_gid_is_refused_before_anything_changes(tmp_path: Path) -> None:
    """With the real gids: the host's `wheel` on 10100 would read every artifact."""
    paths = _scalable_tree(tmp_path / "artifacts")

    with pytest.raises(
        shared_trees.ForeignGroupError, match="host group wheel already uses gid 10100"
    ):
        shared_trees.normalize_trees(
            {"artifacts": paths["root"]},
            group_file_text="root:x:0:\nwheel:x:10100:alice\n",
        )

    assert _mode(paths["root"]) == 0o755
    assert _mode(paths["file"]) == 0o644


def test_our_own_host_group_on_the_gid_is_accepted() -> None:
    """The green direction: the runbook's optional group for host access."""
    shared_trees.refuse_foreign_groups(
        "root:x:0:\nflow-steward-artifacts:x:10100:alice\nstaff:x:20:\n",
        ("artifacts",),
        shared_trees.tree_models(),
    )


def test_the_command_reports_each_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    paths = _scalable_tree(tmp_path / "artifacts")
    groups = tmp_path / "group"
    groups.write_text("root:x:0:\n", encoding="utf-8")
    monkeypatch.setattr(shared_trees, "CompactVolumeLayout", lambda: _LAYOUT)

    code = shared_trees.main(["--host-group-file", str(groups), f"artifacts={paths['root']}"])

    assert code == 0
    (tree,) = json.loads(capsys.readouterr().out)["trees"]
    assert tree["tree"] == "artifacts"
    assert tree["files"] == 2
    assert tree["summary"].startswith("Normalized ")
