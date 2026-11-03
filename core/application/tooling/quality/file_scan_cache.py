"""Remember which file contents a per-file scanner already passed.

Some gate tools judge every file on its own: detect-secrets compares one file
with that file's baseline entries and nothing else. For those, "this exact
content passed under this exact tool and configuration" stays true, so a scan
only needs the files whose content it has not passed yet.

An entry is keyed by the tool, a configuration key (tool version and the
contents of every configuration file the caller names) and the file's path and
content hash. Only readable, unchanged scanned content earns a pass. Bounded
manifests carry those passes between authenticated hosted runs; they never
create whole-check or release evidence. Missing proof always costs a rescan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILES = 20_000
_MAX_BYTES = 8 * 1024 * 1024


def _content_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _regular_empty_marker(path: Path) -> bool:
    try:
        return (
            not any(parent.is_symlink() for parent in (path, *path.parents))
            and path.is_file()
            and path.stat().st_size == 0
        )
    except OSError:
        return False


def _write_marker(path: Path) -> bool:
    try:
        if any(parent.is_symlink() for parent in path.parents):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        return True
    except FileExistsError:
        return _regular_empty_marker(path)
    except OSError:
        return False


def _marker_name(path: str, content: str) -> str:
    return hashlib.sha256(f"{path}\0{content}".encode()).hexdigest()


def default_cache_root() -> Path | None:
    # Hosted manifest import is stdlib-only and supplies its cache root directly.
    from core.application.tooling.quality.evidence import shared_evidence_root

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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", tool):
            raise ValueError("invalid file scanner name")
        self.tool = tool
        self.keys = tuple(keys)
        self.key_files = tuple(key_files)
        self.key = self._configuration_key()
        self.directory = cache_root / tool / self.key if cache_root is not None else None

    def _configuration_key(self) -> str:
        digest = hashlib.sha256()
        for label, value in (
            ("schema", b"2"),
            ("tool", self.tool.encode()),
            *((f"key:{key}", key.encode()) for key in self.keys),
            *((f"file:{path}", path.read_bytes()) for path in self.key_files),
        ):
            encoded = label.encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            digest.update(len(value).to_bytes(8, "big") + value)
        return digest.hexdigest()

    def _entry(self, path: Path) -> Path | None:
        if self.directory is None:
            return None
        content = _content_hash(path)
        if content is None:
            return None
        return self._entry_for_content(path, content)

    def _entry_for_content(self, path: Path, content: str) -> Path | None:
        if self.directory is None:
            return None
        name = _marker_name(path.as_posix(), content)
        return self.directory / name[:2] / name

    def pending(self, paths: Iterable[Path]) -> list[Path]:
        """Paths whose current content has no recorded pass."""
        if self._configuration_key() != self.key:
            return list(paths)
        return [
            path
            for path in paths
            if (entry := self._entry(path)) is None or not _regular_empty_marker(entry)
        ]

    def record(self, paths: Iterable[Path]) -> None:
        """Mark the current content of ``paths`` as passed."""
        self.record_snapshot(self.snapshot(paths))

    def snapshot(self, paths: Iterable[Path]) -> dict:
        return {
            "configuration": self.key,
            "files": {
                path.as_posix(): content
                for path in paths
                if (content := _content_hash(path)) is not None
            },
        }

    def record_snapshot(self, snapshot: dict, eligible: Iterable[str] | None = None) -> int:
        if snapshot.get("configuration") != self.key or self._configuration_key() != self.key:
            raise ValueError("scanner configuration changed during the scan")
        files = snapshot.get("files")
        if not isinstance(files, dict):
            raise ValueError("invalid scanner snapshot")
        allowed = set(files if eligible is None else eligible)
        recorded = 0
        for raw, content in files.items():
            if raw not in allowed or not isinstance(content, str) or not _HEX.fullmatch(content):
                continue
            path = Path(raw)
            if (
                _content_hash(path) != content
                or (entry := self._entry_for_content(path, content)) is None
            ):
                continue
            recorded += _write_marker(entry)
        return recorded

    def export_manifest(self, paths: Iterable[Path], destination: Path) -> int:
        if self._configuration_key() != self.key:
            raise ValueError("scanner configuration changed before export")
        files = []
        for path in paths:
            raw = _canonical_path(path.as_posix())
            content = _content_hash(path)
            if (
                content is not None
                and (entry := self._entry_for_content(path, content)) is not None
                and _regular_empty_marker(entry)
            ):
                files.append({"path": raw, "sha256": content})
        payload = {
            "schema_version": 1,
            "tool": self.tool,
            "configuration": self.key,
            "files": files,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["checksum"] = hashlib.sha256(encoded.encode()).hexdigest()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(files) > _MAX_FILES or len(encoded.encode()) > _MAX_BYTES:
            raise ValueError("scanner cache manifest exceeds its bounded size")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded + "\n")
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return len(files)


def _canonical_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or any(char in raw for char in "\\\x00\r\n"):
        raise ValueError("invalid cached source path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw or raw == ".":
        raise ValueError("cached source path must be relative and canonical")
    return raw


def import_manifest(source: Path, cache_root: Path) -> int:
    if source.is_symlink() or not source.is_file() or source.stat().st_size > _MAX_BYTES:
        raise ValueError("invalid scanner cache manifest file")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "tool",
        "configuration",
        "files",
        "checksum",
    }:
        raise ValueError("invalid scanner cache manifest schema")
    checksum = payload.pop("checksum")
    if (
        checksum
        != hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise ValueError("scanner cache manifest checksum mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["tool"] != "detect-secrets"
    ):
        raise ValueError("unsupported scanner cache manifest")
    key = payload["configuration"]
    files = payload["files"]
    if (
        not isinstance(key, str)
        or not _HEX.fullmatch(key)
        or not isinstance(files, list)
        or len(files) > _MAX_FILES
    ):
        raise ValueError("invalid scanner cache manifest entries")
    markers = []
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("invalid cached file record")
        raw = _canonical_path(item["path"])
        content = item["sha256"]
        if raw in seen or not isinstance(content, str) or not _HEX.fullmatch(content):
            raise ValueError("invalid or duplicate cached file identity")
        seen.add(raw)
        name = _marker_name(raw, content)
        markers.append(cache_root / "detect-secrets" / key / name[:2] / name)
    # Validate the whole manifest before creating any marker.
    return sum(_write_marker(marker) for marker in markers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-file pass cache for gate scanners.")
    parser.add_argument(
        "action", choices=("pending", "record", "snapshot", "record-scan", "export", "import")
    )
    parser.add_argument("--tool", default="detect-secrets")
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--key-file", action="append", default=[], type=Path)
    parser.add_argument("--files-from", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "import":
        if args.source is None or args.cache_root is None:
            parser.error("import requires --source and --cache-root")
        print(f"[file-cache] imported {import_manifest(args.source, args.cache_root)} file passes")
        return 0
    if args.files_from is None:
        parser.error("--files-from is required")
    cache = FileScanCache(
        None if args.no_cache else args.cache_root or default_cache_root(),
        tool=args.tool,
        keys=args.key,
        key_files=args.key_file,
    )
    paths = [
        Path(line) for line in args.files_from.read_text(encoding="utf-8").splitlines() if line
    ]
    if args.action == "pending":
        for path in cache.pending(paths):
            print(path.as_posix())
    elif args.action == "record":
        cache.record(paths)
    elif args.action == "snapshot":
        if args.output is None:
            parser.error("snapshot requires --output")
        args.output.write_text(json.dumps(cache.snapshot(paths)), encoding="utf-8")
    elif args.action == "export":
        if args.output is None:
            parser.error("export requires --output")
        cache.export_manifest(paths, args.output)
    else:
        if (
            args.snapshot is None
            or args.report is None
            or args.baseline is None
            or args.exit_code is None
        ):
            parser.error("record-scan requires snapshot, report, baseline and exit-code")
        from core.application.tooling.quality.secret_scan import cache_scan_result

        cache_scan_result(
            cache,
            json.loads(args.snapshot.read_text()),
            paths,
            args.exit_code,
            args.report.read_text(),
            args.baseline,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
