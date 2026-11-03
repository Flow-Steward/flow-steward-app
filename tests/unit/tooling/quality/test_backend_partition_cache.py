from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core.application.tooling.quality.backend_partition_cache import cached_partition


def _write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Quality",
            "-c",
            "user.email=quality@example.invalid",
            *args,
        ),
        text=True,
    ).strip()


def _repo(root: Path) -> Path:
    root.mkdir()
    _write(root, "tests/unit/test_value.py", "def test_value(): assert 1\n")
    _write(root, "core/helper.py", "VALUE = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Initial")
    return root


def _partition() -> dict:
    return {"independent": ["tests/unit/test_value.py"], "frontend_contract": [], "reasons": {}}


def test_reuses_across_real_candidate_commits_and_frontend_only_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    root = _repo(tmp_path / "repo")
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    assert cached_partition(root, compute) == _partition()
    original = _git(root, "rev-parse", "HEAD")
    _write(root, "assets/frontend/src/view.tsx", "export const value = 2\n")
    _git(root, "add", "assets")
    _git(root, "commit", "-qm", "Frontend change")
    assert _git(root, "rev-parse", "HEAD") != original
    assert cached_partition(root, compute) == _partition()
    assert len(calls) == 1


def test_shared_cache_has_no_checkout_path_or_candidate_sha_identity(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".cache/flow-steward-quality").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    _git(second, "commit", "--allow-empty", "-qm", "Another candidate")
    assert _git(first, "rev-parse", "HEAD") != _git(second, "rev-parse", "HEAD")
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    cached_partition(first, compute)
    assert cached_partition(second, compute) == _partition()
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["core/helper.py", "tests/conftest.py", "tests/unit/helper.py"])
def test_every_python_dependency_invalidates_without_a_commit(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    root = _repo(tmp_path / "repo")
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    cached_partition(root, compute)
    _write(root, name, "VALUE = 2\n")
    cached_partition(root, compute)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "damage", ["corrupt", "missing", "checksum", "overlap", "missing-module", "unknown-schema"]
)
def test_invalid_cache_never_skips_partition_computation(
    tmp_path: Path, monkeypatch, damage: str
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    root = _repo(tmp_path / "repo")
    cached_partition(root, _partition)
    entry = next((root / "logs/quality/backend-partitions").glob("*.json"))
    record = json.loads(entry.read_text())
    if damage == "missing":
        entry.unlink()
    elif damage == "corrupt":
        entry.write_text("{not-json")
    else:
        if damage == "checksum":
            record["checksum"] = "bad"
        elif damage == "unknown-schema":
            record["schema"] = 999
        elif damage == "overlap":
            record["partition"]["frontend_contract"] = ["tests/unit/test_value.py"]
        elif damage == "missing-module":
            record["partition"]["independent"] = []
        if damage in {"overlap", "missing-module"}:
            encoded = json.dumps(
                record["partition"], sort_keys=True, separators=(",", ":")
            ).encode()
            record["checksum"] = hashlib.sha256(encoded).hexdigest()
        entry.write_text(json.dumps(record))
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    assert cached_partition(root, compute) == _partition()
    assert len(calls) == 1


def test_non_git_sources_and_symlink_target_content_invalidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    root = tmp_path / "repo"
    _write(root, "tests/unit/test_value.py", "def test_value(): pass\n")
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n")
    (root / "helper.py").symlink_to(target)
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    cached_partition(root, compute)
    cached_partition(root, compute)
    target.write_text("VALUE = 2\n")
    cached_partition(root, compute)
    replacement = tmp_path / "replacement.py"
    replacement.write_text("VALUE = 2\n")
    (root / "helper.py").unlink()
    (root / "helper.py").symlink_to(replacement)
    cached_partition(root, compute)
    assert len(calls) == 3


def test_failed_atomic_write_returns_computed_result_and_does_not_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    from core.application.tooling.quality import backend_partition_cache

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    root = _repo(tmp_path / "repo")
    calls = []

    def compute() -> dict:
        calls.append(True)
        return _partition()

    def fail_replace(*_args) -> None:
        raise OSError("unwritable cache")

    monkeypatch.setattr(backend_partition_cache.os, "replace", fail_replace)
    assert cached_partition(root, compute) == _partition()
    assert cached_partition(root, compute) == _partition()
    assert len(calls) == 2
    assert not list((root / "logs/quality/backend-partitions").glob("*.json"))
