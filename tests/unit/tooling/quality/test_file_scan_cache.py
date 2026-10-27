from __future__ import annotations

from pathlib import Path

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
