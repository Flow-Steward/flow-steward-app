"""Turn a platform module source tree into a staged artifact (ADR 0082 prototype).

Standard library only, so it runs on any host Python without Core's dependencies::

    python3 -m tests.support.platform_module_builder SOURCE_DIR MODULES_ROOT \\
        --python-cache-tag cpython-312 --machine x86_64 [--replace]

A source tree holds ``module-source.json`` (``{"id", "version", "import"}`` and an
optional ``"base"``) and any of ``src/``, ``site/`` and ``db/``. With ``base`` the
tree is an overlay: the base tree's files are taken first and the overlay's files
replace or add to them, so a new version ships as a delta of the previous one.

The artifact lands at ``<MODULES_ROOT>/staged/<id>/<version>/`` with ``module.json``
in exactly the shape ``core.infrastructure.release.platform_module_activator``
reads: ``id``, ``version``, ``import``, ``python_cache_tag``, ``machine`` and the
sha256 of every file under the three trees. Identical input gives byte-identical
output: sorted keys, fixed file modes, no timestamps, no ``__pycache__``.

It lives with the test support code because it is a prototype tool: the real
deployment manager that prepares modules is outside this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SOURCE_DESCRIPTOR = "module-source.json"
MODULE_DESCRIPTOR = "module.json"
STAGED_DIRNAME = "staged"
TREES = ("src", "site", "db")
#: Never copied: bytecode caches and desktop clutter. The loader refuses unlisted
#: files, so a cache written into a source tree must not reach the artifact.
_SKIPPED_DIRS = frozenset({"__pycache__"})
_SKIPPED_FILES = frozenset({".DS_Store"})
_SKIPPED_SUFFIXES = (".pyc", ".pyo")
_FILE_MODE = 0o644
_DIR_MODE = 0o755
_SOURCE_KEYS = frozenset({"id", "version", "import"})
_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_IMPORT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")


class ModuleBuildError(RuntimeError):
    """The source tree cannot become a staged artifact."""


@dataclass(frozen=True, slots=True)
class BuiltModule:
    """What a ``PreparedModuleRef`` pins, plus where the artifact was written."""

    id: str
    version: str
    digest: str
    path: Path


@dataclass(frozen=True, slots=True)
class _Source:
    module_id: str
    version: str
    import_name: str
    files: Mapping[str, Path]


def descriptor_digest(descriptor: Mapping[str, object]) -> str:
    """Same bytes and digest as ``core.application.release.platform_modules``."""
    canonical = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_source_descriptor(source_dir: Path) -> dict[str, object]:
    path = source_dir / SOURCE_DESCRIPTOR
    if path.is_symlink() or not path.is_file():
        raise ModuleBuildError(f"{path} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModuleBuildError(f"{path} cannot be read as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModuleBuildError(f"{path} must contain a JSON object")
    unknown = set(payload) - _SOURCE_KEYS - {"base"}
    if unknown:
        raise ModuleBuildError(f"{path} has unknown keys {sorted(unknown)}")
    return payload


def _tree_files(source_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for tree in TREES:
        tree_root = source_dir / tree
        if tree_root.is_symlink():
            raise ModuleBuildError(f"{tree_root} must not be a symlink")
        if not tree_root.exists():
            continue
        if not tree_root.is_dir():
            raise ModuleBuildError(f"{tree_root} is not a directory")
        for current, dirnames, filenames in os.walk(tree_root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in _SKIPPED_DIRS)
            current_path = Path(current)
            for name in (*dirnames, *filenames):
                if (current_path / name).is_symlink():
                    raise ModuleBuildError(f"{current_path / name} is a symlink")
            for name in sorted(filenames):
                if name in _SKIPPED_FILES or name.endswith(_SKIPPED_SUFFIXES):
                    continue
                if name.endswith(".pth"):
                    raise ModuleBuildError(f"{current_path / name}: .pth files are refused")
                path = current_path / name
                if not path.is_file():
                    raise ModuleBuildError(f"{path} is not a regular file")
                files[path.relative_to(source_dir).as_posix()] = path
    return files


def _resolve_source(source_dir: Path, *, seen: tuple[Path, ...] = ()) -> _Source:
    source_dir = source_dir.resolve()
    if source_dir in seen:
        raise ModuleBuildError(f"{source_dir} is its own base")
    payload = _read_source_descriptor(source_dir)
    files: dict[str, Path] = {}
    base_value = payload.get("base")
    base: _Source | None = None
    if base_value is not None:
        if not isinstance(base_value, str) or not base_value.strip():
            raise ModuleBuildError(f"{source_dir / SOURCE_DESCRIPTOR} 'base' must be a path")
        base = _resolve_source(source_dir / base_value, seen=(*seen, source_dir))
        files.update(base.files)
    files.update(_tree_files(source_dir))

    def field(key: str) -> str:
        value = payload.get(key)
        if value is None and base is not None and key != "version":
            value = {"id": base.module_id, "import": base.import_name}[key]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ModuleBuildError(f"{source_dir / SOURCE_DESCRIPTOR} needs a '{key}' string")
        return value

    module_id, version, import_name = field("id"), field("version"), field("import")
    if base is not None and (module_id, import_name) != (base.module_id, base.import_name):
        raise ModuleBuildError(f"{source_dir} must keep the id and import name of its base")
    if not _ID_PATTERN.fullmatch(module_id):
        raise ModuleBuildError(f"module id {module_id!r} is not a lowercase package id")
    if not _IMPORT_PATTERN.fullmatch(import_name):
        raise ModuleBuildError(f"import name {import_name!r} is not a dotted module name")
    if not _VERSION_PATTERN.fullmatch(version) or version in {".", ".."}:
        raise ModuleBuildError(f"version {version!r} cannot name a staged directory")
    return _Source(module_id, version, import_name, files)


def _render_descriptor(descriptor: Mapping[str, object]) -> bytes:
    return (json.dumps(descriptor, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _write_tree(target: Path, files: Mapping[str, Path]) -> None:
    for relative in sorted(files):
        destination = target / PurePosixPath(relative)
        for parent in reversed(destination.relative_to(target).parents):
            directory = target / parent
            if not directory.is_dir():
                directory.mkdir()
                os.chmod(directory, _DIR_MODE)
        shutil.copyfile(files[relative], destination)
        os.chmod(destination, _FILE_MODE)


def build_staged_artifact(
    source_dir: Path | str,
    staged_root: Path | str,
    *,
    python_cache_tag: str,
    machine: str,
    replace: bool = False,
) -> BuiltModule:
    """Build ``source_dir`` into ``<staged_root>/staged/<id>/<version>/``.

    ``staged_root`` is the platform-module root (the directory that holds
    ``modules.json`` and ``staged/``). An existing staged version is refused
    unless ``replace`` is set. The artifact is assembled beside the root and
    moved into place in one rename, so a reader never sees half of it.
    """
    if not python_cache_tag or not machine:
        raise ModuleBuildError("python_cache_tag and machine are required")
    source = _resolve_source(Path(source_dir))
    if not source.files:
        raise ModuleBuildError(f"{source_dir} has no files under {', '.join(TREES)}/")
    descriptor = {
        "id": source.module_id,
        "version": source.version,
        "import": source.import_name,
        "python_cache_tag": python_cache_tag,
        "machine": machine,
        "files_sha256": {
            relative: _sha256(path) for relative, path in sorted(source.files.items())
        },
    }
    root = Path(staged_root)
    root.mkdir(parents=True, exist_ok=True)
    module_parent = root / STAGED_DIRNAME / source.module_id
    final = module_parent / source.version
    if (final.exists() or final.is_symlink()) and not replace:
        raise FileExistsError(f"{final} is already staged; pass replace=True to rebuild it")

    # A hidden sibling at the root: neither the loader nor staged_module_refs reads it.
    building = Path(tempfile.mkdtemp(prefix=".build-", dir=root))
    try:
        os.chmod(building, _DIR_MODE)
        _write_tree(building, source.files)
        descriptor_path = building / MODULE_DESCRIPTOR
        descriptor_path.write_bytes(_render_descriptor(descriptor))
        os.chmod(descriptor_path, _FILE_MODE)
        module_parent.mkdir(parents=True, exist_ok=True)
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                final.unlink()
            else:
                shutil.rmtree(final)
        os.replace(building, final)
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return BuiltModule(
        id=source.module_id,
        version=source.version,
        digest=descriptor_digest(descriptor),
        path=final,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("modules_root", type=Path, help="the platform-module root")
    parser.add_argument("--python-cache-tag", default=sys.implementation.cache_tag)
    parser.add_argument("--machine", default=platform.machine())
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        built = build_staged_artifact(
            args.source_dir,
            args.modules_root,
            python_cache_tag=args.python_cache_tag,
            machine=args.machine,
            replace=args.replace,
        )
    except (ModuleBuildError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "id": built.id,
                "version": built.version,
                "digest": built.digest,
                "path": str(built.path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
