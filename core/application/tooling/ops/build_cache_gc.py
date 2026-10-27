#!/usr/bin/env python3
"""Delete the image-layer blobs a local BuildKit cache no longer references.

``infra/docker/docker-compose.yaml`` exports each image's build cache with
``cache_to: type=local,dest=<state>/build-cache/<image>,mode=min``. BuildKit's
local exporter writes the new layers and repoints ``index.json`` at them, but
never deletes the previous ones, so every rebuild leaves its old layers behind.
By 2026-10-01 one developer machine held 74 GB of unreferenced blobs, 57 GB of
them in ``build-cache/audit`` alone (the quality-gate image).

This keeps every blob reachable from a cache's ``index.json`` and deletes the
rest. A blob younger than ``min_age_seconds`` is kept even when unreferenced: a
build running in parallel writes its blobs before it repoints the index, and
deleting them in between would corrupt that build's export.

Standard library only: ``core/entrypoints/cli/scripts/docker_compose_env.sh``
runs it on the host, where the product's dependencies are not installed.

    python3 -m core.application.tooling.ops.build_cache_gc <state>/build-cache
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MIN_AGE_SECONDS = 3600
# Manifests and cache configs are small JSON documents; layers are large and are
# never parsed for references.
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(rb"sha256:([0-9a-f]{64})")


@dataclass
class CachePruneResult:
    cache: str
    referenced: int = 0
    removed: int = 0
    removed_bytes: int = 0
    kept_recent: int = 0
    skipped_reason: str = ""


@dataclass
class PruneReport:
    caches: list[CachePruneResult] = field(default_factory=list)

    @property
    def removed_bytes(self) -> int:
        return sum(cache.removed_bytes for cache in self.caches)


def _references(path: Path) -> list[str]:
    return [match.decode() for match in _DIGEST.findall(path.read_bytes())]


def prune_cache(cache_dir: Path, *, min_age_seconds: int, now: float) -> CachePruneResult:
    """Prune one ``<image>`` cache directory (``index.json`` + ``blobs/sha256``)."""
    result = CachePruneResult(cache=cache_dir.name)
    blobs_dir = cache_dir / "blobs" / "sha256"
    index = cache_dir / "index.json"
    if not blobs_dir.is_dir() or not index.is_file():
        result.skipped_reason = "no index.json"
        return result
    try:
        sizes = {blob.name: blob.stat().st_size for blob in blobs_dir.iterdir() if blob.is_file()}
        roots = _references(index)
    except OSError as exc:
        result.skipped_reason = f"unreadable ({exc})"
        return result
    if not roots or any(root not in sizes for root in roots):
        # An index that names nothing, or a blob that is gone, is not a cache we
        # understand; deleting from it could only make it worse.
        result.skipped_reason = "index.json does not name a present blob"
        return result

    reachable: set[str] = set()
    pending = list(roots)
    while pending:
        digest = pending.pop()
        if digest in reachable or digest not in sizes:
            continue
        reachable.add(digest)
        if sizes[digest] <= _MAX_DOCUMENT_BYTES:
            pending.extend(_references(blobs_dir / digest))
    result.referenced = len(reachable)

    for digest, size in sizes.items():
        if digest in reachable:
            continue
        blob = blobs_dir / digest
        try:
            if now - blob.stat().st_mtime < min_age_seconds:
                result.kept_recent += 1
                continue
            # BuildKit writes blobs read-only.
            blob.chmod(0o644)
            blob.unlink()
        except FileNotFoundError:
            continue
        result.removed += 1
        result.removed_bytes += size
    return result


def prune_build_cache(
    root: Path, *, min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS, now: float | None = None
) -> PruneReport:
    """Prune every image cache under ``<state>/build-cache``."""
    report = PruneReport()
    if not root.is_dir():
        return report
    moment = time.time() if now is None else now
    for cache_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        report.caches.append(prune_cache(cache_dir, min_age_seconds=min_age_seconds, now=moment))
    return report


def main(argv: list[str] | None = None) -> None:
    """Prune and report. Best effort: a usage error exits through argparse,
    anything else is a successful run, so there is no status to return."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="the build-cache directory")
    parser.add_argument("--min-age-seconds", type=int, default=DEFAULT_MIN_AGE_SECONDS)
    parser.add_argument(
        "--quiet", action="store_true", help="print only when something was removed"
    )
    args = parser.parse_args(argv)
    report = prune_build_cache(args.root, min_age_seconds=args.min_age_seconds)
    if not args.quiet or report.removed_bytes:
        for cache in report.caches:
            if cache.skipped_reason:
                print(f"[build-cache] {cache.cache}: left alone, {cache.skipped_reason}")
            elif cache.removed or not args.quiet:
                print(
                    f"[build-cache] {cache.cache}: removed {cache.removed} unreferenced blobs "
                    f"({cache.removed_bytes / 2**20:.0f} MiB), kept {cache.referenced} referenced"
                )


if __name__ == "__main__":
    main()
