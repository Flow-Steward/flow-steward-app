from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.tooling.quality import file_scan_cache
from core.application.tooling.quality.file_scan_cache import FileScanCache


def _cache(root: Path | None, config: Path, *, version: str = "1.5.0") -> FileScanCache:
    return FileScanCache(root, tool="detect-secrets", keys=[version], key_files=[config])


def test_only_content_without_a_recorded_pass_is_pending(tmp_path: Path) -> None:
    config = tmp_path / "baseline"
    config.write_text("{}")
    clean, changed = tmp_path / "clean.py", tmp_path / "changed.py"
    clean.write_text("A = 1\n")
    changed.write_text("B = 1\n")
    _cache(tmp_path / "cache", config).record([clean, changed])

    changed.write_text("B = 2\n")

    assert _cache(tmp_path / "cache", config).pending([clean, changed]) == [changed]


def test_a_new_tool_version_or_configuration_rescans_everything(tmp_path: Path) -> None:
    config = tmp_path / "baseline"
    config.write_text("{}")
    source = tmp_path / "source.py"
    source.write_text("A = 1\n")
    _cache(tmp_path / "cache", config).record([source])

    assert _cache(tmp_path / "cache", config, version="1.6.0").pending([source]) == [source]
    config.write_text('{"results": {}}')
    assert _cache(tmp_path / "cache", config).pending([source]) == [source]


def test_without_the_cache_volume_every_file_is_pending(tmp_path: Path) -> None:
    config = tmp_path / "baseline"
    config.write_text("{}")
    source = tmp_path / "source.py"
    source.write_text("A = 1\n")
    _cache(None, config).record([source])

    assert _cache(None, config).pending([source]) == [source]


def test_cli_lists_pending_files_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(file_scan_cache, "default_cache_root", lambda: tmp_path / "cache")
    Path("baseline").write_text("{}")
    Path("a.py").write_text("A = 1\n")
    Path("b.py").write_text("B = 1\n")
    Path("files").write_text("a.py\nb.py\n")
    Path("passed").write_text("a.py\n")
    args = ["--tool", "detect-secrets", "--key-file", "baseline"]

    assert file_scan_cache.main(["record", *args, "--files-from", "passed"]) == 0
    assert file_scan_cache.main(["pending", *args, "--files-from", "files"]) == 0

    assert capsys.readouterr().out == "b.py\n"


def test_scan_snapshot_never_marks_later_changed_bytes_as_passed(tmp_path: Path) -> None:
    config = tmp_path / "baseline"
    config.write_text("{}")
    clean, changed = tmp_path / "clean.py", tmp_path / "changed.py"
    clean.write_text("A = 1\n")
    changed.write_text("B = 1\n")
    cache = _cache(tmp_path / "cache", config)
    snapshot = cache.snapshot([clean, changed])
    changed.write_text("B = 2\n")

    cache.record_snapshot(snapshot)

    assert cache.pending([clean, changed]) == [changed]


def test_directory_or_symlink_cannot_substitute_for_a_pass_marker(tmp_path: Path) -> None:
    config = tmp_path / "baseline"
    config.write_text("{}")
    source = tmp_path / "source.py"
    source.write_text("A = 1\n")
    cache = _cache(tmp_path / "cache", config)
    cache.record([source])
    marker = next(path for path in cache.directory.rglob("*") if path.is_file())
    marker.unlink()
    marker.mkdir()
    assert cache.pending([source]) == [source]
    marker.rmdir()
    marker.symlink_to(source)
    assert cache.pending([source]) == [source]


def test_exported_current_passes_reuse_across_checkouts_and_leave_changes_pending(
    tmp_path: Path, monkeypatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        (root / "baseline").write_text("{}")
        (root / "clean.py").write_text("A = 1\n")
        (root / "changed.py").write_text("B = 1\n")
    monkeypatch.chdir(first)
    cache = _cache(tmp_path / "cache-one", Path("baseline"))
    cache.record([Path("clean.py"), Path("changed.py")])
    Path("changed.py").write_text("B = 2\n")
    manifest = tmp_path / "passes.json"
    cache.export_manifest([Path("clean.py"), Path("changed.py")], manifest)

    monkeypatch.chdir(second)
    Path("changed.py").write_text("B = 2\n")
    Path("new.py").write_text("C = 1\n")
    file_scan_cache.import_manifest(manifest, tmp_path / "cache-two")
    restored = _cache(tmp_path / "cache-two", Path("baseline"))

    assert restored.pending([Path("clean.py"), Path("changed.py"), Path("new.py")]) == [
        Path("changed.py"),
        Path("new.py"),
    ]
    assert len(json.loads(manifest.read_text())["files"]) == 1
    assert not (tmp_path / "cache-two" / "evidence").exists()


@pytest.mark.parametrize("bad_path", ["../outside.py", "/outside.py", "dir/../source.py"])
def test_malformed_import_creates_no_passes(tmp_path: Path, monkeypatch, bad_path: str) -> None:
    monkeypatch.chdir(tmp_path)
    Path("baseline").write_text("{}")
    Path("source.py").write_text("A = 1\n")
    producer = _cache(tmp_path / "producer", Path("baseline"))
    producer.record([Path("source.py")])
    manifest = tmp_path / "passes.json"
    producer.export_manifest([Path("source.py")], manifest)
    payload = json.loads(manifest.read_text())
    payload["files"].append({"path": bad_path, "sha256": "a" * 64})
    manifest.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        file_scan_cache.import_manifest(manifest, tmp_path / "consumer")

    assert _cache(tmp_path / "consumer", Path("baseline")).pending([Path("source.py")]) == [
        Path("source.py")
    ]


def test_corrupt_manifest_cannot_retarget_a_pass_to_an_unscanned_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("baseline").write_text("{}")
    Path("scanned.py").write_text("VALUE = 1\n")
    Path("unscanned.py").write_text("VALUE = 1\n")
    producer = _cache(tmp_path / "producer", Path("baseline"))
    producer.record([Path("scanned.py")])
    manifest = tmp_path / "passes.json"
    producer.export_manifest([Path("scanned.py")], manifest)
    payload = json.loads(manifest.read_text())
    payload["files"][0]["path"] = "unscanned.py"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        file_scan_cache.import_manifest(manifest, tmp_path / "consumer")
    assert _cache(tmp_path / "consumer", Path("baseline")).pending([Path("unscanned.py")]) == [
        Path("unscanned.py")
    ]
