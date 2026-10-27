"""The local BuildKit cache keeps what its index references and loses the rest."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core.application.tooling.ops.build_cache_gc import main, prune_build_cache

NOW = 1_800_000_000.0
OLD = NOW - 7200
FRESH = NOW - 60


def _blob(cache: Path, content: bytes, *, mtime: float = OLD) -> str:
    digest = hashlib.sha256(content).hexdigest()
    path = cache / "blobs" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o444)  # BuildKit writes its blobs read-only
    os.utime(path, (mtime, mtime))
    return digest


def _cache(root: Path, name: str = "audit") -> tuple[Path, dict[str, str]]:
    """A cache whose index -> manifest -> cache config -> layer chain is all referenced."""
    cache = root / name
    layer = _blob(cache, b"layer bytes " * 1000)
    config = _blob(cache, json.dumps({"layers": [{"blob": f"sha256:{layer}"}]}).encode())
    manifest = _blob(
        cache,
        json.dumps(
            {"config": {"digest": f"sha256:{config}"}, "layers": [{"digest": f"sha256:{layer}"}]}
        ).encode(),
    )
    (cache / "index.json").write_text(
        json.dumps({"manifests": [{"digest": f"sha256:{manifest}"}]}), encoding="utf-8"
    )
    return cache, {"layer": layer, "config": config, "manifest": manifest}


def _present(cache: Path) -> set[str]:
    return {path.name for path in (cache / "blobs" / "sha256").iterdir()}


def test_layers_no_index_references_are_deleted_and_referenced_ones_kept(tmp_path: Path) -> None:
    cache, referenced = _cache(tmp_path)
    orphan = _blob(cache, b"a layer the previous export wrote" * 100)

    report = prune_build_cache(tmp_path, now=NOW)

    assert _present(cache) == set(referenced.values())
    assert orphan not in _present(cache)
    [result] = report.caches
    assert (result.removed, result.referenced) == (1, 3)
    assert report.removed_bytes == len(b"a layer the previous export wrote" * 100)


def test_a_fresh_unreferenced_layer_belongs_to_a_running_build_and_is_kept(tmp_path: Path) -> None:
    """A parallel build writes its blobs before it repoints index.json."""
    cache, _referenced = _cache(tmp_path)
    in_flight = _blob(cache, b"written a minute ago", mtime=FRESH)

    report = prune_build_cache(tmp_path, now=NOW)

    assert in_flight in _present(cache)
    assert report.caches[0].kept_recent == 1


def test_a_cache_whose_index_names_a_missing_blob_is_left_alone(tmp_path: Path) -> None:
    cache, _referenced = _cache(tmp_path)
    orphan = _blob(cache, b"orphan")
    (cache / "index.json").write_text(
        json.dumps({"manifests": [{"digest": "sha256:" + "0" * 64}]}), encoding="utf-8"
    )

    report = prune_build_cache(tmp_path, now=NOW)

    assert orphan in _present(cache)
    assert report.caches[0].skipped_reason


def test_a_directory_without_an_index_is_left_alone(tmp_path: Path) -> None:
    stray = tmp_path / "half-written" / "blobs" / "sha256"
    stray.mkdir(parents=True)
    (stray / ("1" * 64)).write_bytes(b"x")

    report = prune_build_cache(tmp_path, now=NOW)

    assert (stray / ("1" * 64)).exists()
    assert report.caches[0].skipped_reason == "no index.json"


def test_every_image_cache_under_the_root_is_pruned(tmp_path: Path) -> None:
    caches = [_cache(tmp_path, name)[0] for name in ("app", "audit", "worker")]
    for cache in caches:
        _blob(cache, f"orphan of {cache.name}".encode())

    report = prune_build_cache(tmp_path, now=NOW)

    assert [result.removed for result in report.caches] == [1, 1, 1]


def test_pruning_twice_removes_nothing_the_second_time(tmp_path: Path) -> None:
    cache, referenced = _cache(tmp_path)
    _blob(cache, b"orphan")
    prune_build_cache(tmp_path, now=NOW)

    report = prune_build_cache(tmp_path, now=NOW)

    assert report.removed_bytes == 0
    assert _present(cache) == set(referenced.values())


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert prune_build_cache(tmp_path / "absent", now=NOW).caches == []


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_reports_retained_cache_only_when_not_quiet(tmp_path: Path, capsys, quiet) -> None:
    cache, referenced = _cache(tmp_path)
    args = [str(tmp_path), *(["--quiet"] if quiet else [])]

    main(args)
    assert _present(cache) == set(referenced.values())
    assert capsys.readouterr().out == (
        ""
        if quiet
        else "[build-cache] audit: removed 0 unreferenced blobs (0 MiB), kept 3 referenced\n"
    )
