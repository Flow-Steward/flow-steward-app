from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.application.tooling.quality.file_scan_cache import FileScanCache

ROOT = Path(__file__).resolve().parents[4]
METADATA = {
    "version": "1.5.0",
    "plugins_used": [{"name": "KeywordDetector"}],
    "filters_used": [],
    "results": {},
    "generated_at": "2026-11-03T00:00:00Z",
}


def _module():
    assert (ROOT / "core/application/tooling/quality/secret_scan.py").is_file(), (
        "Secret scanner adapter is missing"
    )
    return importlib.import_module("core.application.tooling.quality.secret_scan")


@pytest.fixture
def scan(tmp_path: Path):
    baseline = tmp_path / "baseline"
    baseline.write_text(json.dumps(METADATA))
    clean, flagged = tmp_path / "clean.py", tmp_path / "flagged.py"
    clean.write_text("VALUE = 1\n")
    flagged.write_text("VALUE = 2\n")
    cache = FileScanCache(tmp_path / "cache", tool="detect-secrets", key_files=[baseline])
    report = {
        **METADATA,
        "results": {
            str(flagged): [
                {
                    "type": "Secret Keyword",
                    "filename": str(flagged),
                    "hashed_secret": "a" * 40,
                    "is_verified": False,
                    "line_number": 1,
                }
            ]
        },
    }
    return cache, [clean, flagged], baseline, report


def test_failed_batch_retains_clean_file_and_never_unverified_finding(scan) -> None:
    cache, paths, baseline, report = scan
    _module().cache_scan_result(
        cache, cache.snapshot(paths), paths, 1, json.dumps(report), baseline
    )
    assert cache.pending(paths) == [paths[1]]


@pytest.mark.parametrize("output", ["", "{}", "warning: unreadable\n{}", '{"results":'])
def test_incomplete_or_diagnostic_output_cannot_create_partial_passes(scan, output: str) -> None:
    cache, paths, baseline, _ = scan
    with pytest.raises(ValueError):
        _module().cache_scan_result(cache, cache.snapshot(paths), paths, 1, output, baseline)
    assert cache.pending(paths) == paths


def test_wrong_configuration_or_unknown_finding_path_cannot_create_passes(scan) -> None:
    cache, paths, baseline, report = scan
    snapshot = cache.snapshot(paths)
    report["version"] = "0.0.0"
    with pytest.raises(ValueError):
        _module().cache_scan_result(cache, snapshot, paths, 1, json.dumps(report), baseline)
    report["version"] = "1.5.0"
    report["results"]["not-scanned.py"] = report["results"].pop(str(paths[1]))
    with pytest.raises(ValueError):
        _module().cache_scan_result(cache, snapshot, paths, 1, json.dumps(report), baseline)
    assert cache.pending(paths) == paths


def test_clean_file_changed_after_scan_remains_pending(scan) -> None:
    cache, paths, baseline, report = scan
    snapshot = cache.snapshot(paths)
    paths[0].write_text("VALUE = 3\n")
    _module().cache_scan_result(cache, snapshot, paths, 1, json.dumps(report), baseline)
    assert cache.pending(paths) == paths


def test_warning_even_with_zero_exit_cannot_create_passes(scan) -> None:
    cache, paths, baseline, _ = scan
    with pytest.raises(ValueError):
        _module().cache_scan_result(
            cache, cache.snapshot(paths), paths, 0, "WARNING unreadable", baseline
        )
    assert cache.pending(paths) == paths


def test_configuration_changed_during_success_cannot_create_passes(scan) -> None:
    cache, paths, baseline, _ = scan
    snapshot = cache.snapshot(paths)
    baseline.write_text(json.dumps({**METADATA, "version": "1.6.0"}))
    with pytest.raises(ValueError, match="configuration changed"):
        _module().cache_scan_result(cache, snapshot, paths, 0, "", baseline)
    assert cache.pending(paths) == paths


def test_real_hook_exposes_unreadable_file_and_never_caches_it(scan) -> None:
    cache, paths, baseline, _ = scan
    # The real pinned scanner must expose its read warning to the validator.
    metadata = json.loads((ROOT / ".secrets.baseline").read_text())
    metadata["results"] = {}
    baseline.write_text(json.dumps(metadata))
    cache = FileScanCache(
        cache.directory.parent.parent, tool="detect-secrets", key_files=[baseline]
    )
    snapshot = cache.snapshot(paths)
    paths[0].chmod(0)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "core.application.tooling.quality.secret_scan",
                "hook",
                "--json",
                "--baseline",
                str(baseline),
                *map(str, paths),
            ],
            capture_output=True,
            text=True,
        )
        assert "WARNING" in result.stderr, result.stdout + result.stderr
        with pytest.raises(ValueError):
            _module().cache_scan_result(
                cache, snapshot, paths, result.returncode, result.stdout + result.stderr, baseline
            )
    finally:
        paths[0].chmod(0o600)
    assert cache.pending(paths) == paths


def test_real_scanner_retry_receives_only_the_changed_failed_file(
    tmp_path: Path, monkeypatch
) -> None:
    _module()
    monkeypatch.chdir(tmp_path)
    baseline = Path("baseline.json")
    metadata = json.loads((ROOT / ".secrets.baseline").read_text())
    metadata["results"] = {}
    baseline.write_text(json.dumps(metadata))
    clean, flagged = Path("clean.py"), Path("flagged.py")
    clean.write_text("VALUE = 1\n")
    flagged.write_text("PASS" + "WORD = 'unit-only-placeholder-value'\n")
    cache = FileScanCache(tmp_path / "cache", tool="detect-secrets", key_files=[baseline])

    def run(paths):
        started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "core.application.tooling.quality.secret_scan",
                "hook",
                "--json",
                "--baseline",
                str(baseline),
                *map(str, paths),
            ],
            capture_output=True,
            text=True,
        )
        return result, time.monotonic() - started

    paths = [clean, flagged]
    snapshot = cache.snapshot(paths)
    first, first_seconds = run(paths)
    assert first.returncode == 1, first.stdout + first.stderr
    _module().cache_scan_result(cache, snapshot, paths, 1, first.stdout + first.stderr, baseline)
    flagged.write_text("VALUE = 2\n")
    pending = cache.pending(paths)
    assert pending == [flagged]
    snapshot = cache.snapshot(pending)
    retry, retry_seconds = run(pending)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    _module().cache_scan_result(cache, snapshot, pending, 0, retry.stdout + retry.stderr, baseline)
    assert cache.pending(paths) == []
    print(
        json.dumps(
            {
                "scope": "real scanner two-file regression",
                "cold_files": 2,
                "retry_files": 1,
                "cold_seconds": first_seconds,
                "retry_seconds": retry_seconds,
            }
        )
    )
