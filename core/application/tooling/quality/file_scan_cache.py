"""Remember which file contents a per-file scanner already passed.

Some gate tools judge every file on its own: detect-secrets compares one file
with that file's baseline entries and nothing else. For those, "this exact
content passed under this exact tool and configuration" stays true, so a scan
only needs the files whose content it has not passed yet.

An entry is keyed by the tool, a configuration key (tool version and the
contents of every configuration file the caller names) and the file's path and
content hash. Entries are recorded only after a whole scan passed, and live in
the cache volume the fs-audit service shares between checkouts. Without that
volume every file is pending, which is the uncached behaviour.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Iterable
from pathlib import Path

from core.application.tooling.quality.evidence import shared_evidence_root


def default_cache_root() -> Path | None:
    evidence_root = shared_evidence_root()
    return evidence_root.parent / "file-results" if evidence_root is not None else None


class FileScanCache:
    def __init__(
        self,
        cache_root: Path | None,
        *,
        tool: str,
        keys: Iterable[str] = (),
        key_files: Iterable[Path] = (),
    ) -> None:
        digest = hashlib.sha256()
        for label, value in (
            ("tool", tool.encode()),
            *((f"key:{key}", key.encode()) for key in keys),
            *((f"file:{path}", path.read_bytes()) for path in key_files),
        ):
            encoded = label.encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            digest.update(len(value).to_bytes(8, "big") + value)
        self.directory = cache_root / tool / digest.hexdigest() if cache_root is not None else None

    def _entry(self, path: Path) -> Path | None:
        if self.directory is None:
            return None
        try:
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        name = hashlib.sha256(f"{path.as_posix()}\0{content}".encode()).hexdigest()
        return self.directory / name[:2] / name

    def pending(self, paths: Iterable[Path]) -> list[Path]:
        """Paths whose current content has no recorded pass."""
        return [
            path for path in paths if (entry := self._entry(path)) is None or not entry.exists()
        ]

    def record(self, paths: Iterable[Path]) -> None:
        """Mark the current content of ``paths`` as passed."""
        for path in paths:
            entry = self._entry(path)
            if entry is None:
                continue
            try:
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.touch()
            except OSError:
                return  # Only a cache: a read-only or full volume costs a rescan.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-file pass cache for gate scanners.")
    parser.add_argument("action", choices=("pending", "record"))
    parser.add_argument("--tool", required=True)
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--key-file", action="append", default=[], type=Path)
    parser.add_argument("--files-from", required=True, type=Path)
    args = parser.parse_args(argv)
    cache = FileScanCache(
        default_cache_root(), tool=args.tool, keys=args.key, key_files=args.key_file
    )
    paths = [
        Path(line) for line in args.files_from.read_text(encoding="utf-8").splitlines() if line
    ]
    if args.action == "pending":
        for path in cache.pending(paths):
            print(path.as_posix())
    else:
        cache.record(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
