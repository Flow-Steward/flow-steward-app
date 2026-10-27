#!/usr/bin/env python3
"""Materialize deterministic role source slices for app-family Docker targets."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import stat
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ALLOWED_SOURCE_ROOTS = ("core/", "db/", "locales/")
ALLOWED_SOURCE_FILES = {"VERSION"}
FORBIDDEN_SOURCE_ROOTS = ("backups/", "docs/", "extensions/", "logs/", "tests/")
FORBIDDEN_BINARY_SUFFIXES = {
    ".a",
    ".dylib",
    ".dll",
    ".exe",
    ".gz",
    ".so",
    ".tar",
    ".whl",
    ".zip",
}


@dataclass(frozen=True)
class DocumentedPath:
    path: str
    consumer: str


@dataclass(frozen=True)
class SourceSliceManifest:
    role: str
    entrypoints: tuple[str, ...]
    static_paths: tuple[str, ...]
    dynamic_paths: tuple[DocumentedPath, ...]
    package_data_paths: tuple[DocumentedPath, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return (
            self.static_paths
            + tuple(item.path for item in self.dynamic_paths)
            + tuple(item.path for item in self.package_data_paths)
        )


def _canonical_relative_path(raw_path: object) -> str:
    value = str(raw_path or "")
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or value != value.strip("/")
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise ValueError(f"source-slice path must be a relative canonical path: {value!r}")
    if value.startswith(FORBIDDEN_SOURCE_ROOTS):
        raise ValueError(f"source-slice path uses a forbidden root: {value}")
    if value not in ALLOWED_SOURCE_FILES and not value.startswith(ALLOWED_SOURCE_ROOTS):
        raise ValueError(f"source-slice path is outside canonical runtime roots: {value}")
    if path.suffix.lower() in FORBIDDEN_BINARY_SUFFIXES:
        raise ValueError(f"source-slice path is a forbidden binary artifact: {value}")
    return value


def _documented_paths(payload: object, *, section: str) -> tuple[DocumentedPath, ...]:
    if not isinstance(payload, list):
        raise ValueError(f"manifest {section} must be a list")
    entries: list[DocumentedPath] = []
    for raw_item in payload:
        if not isinstance(raw_item, dict) or set(raw_item) != {"path", "consumer"}:
            raise ValueError(f"manifest {section} entries require path and consumer")
        consumer = str(raw_item.get("consumer") or "").strip()
        if not consumer:
            raise ValueError(f"manifest {section} consumer must be named")
        entries.append(
            DocumentedPath(
                path=_canonical_relative_path(raw_item.get("path")),
                consumer=consumer,
            )
        )
    return tuple(entries)


def load_manifest(manifest_path: Path) -> SourceSliceManifest:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("source-slice manifest requires schema_version 1")
    role = str(payload.get("role") or "").strip()
    entrypoints = tuple(str(item).strip() for item in payload.get("entrypoints") or ())
    if not role or not entrypoints or any(not item for item in entrypoints):
        raise ValueError("source-slice manifest requires a role and entrypoints")
    raw_static_paths = payload.get("static_paths")
    if not isinstance(raw_static_paths, list):
        raise ValueError("manifest static_paths must be a list")
    manifest = SourceSliceManifest(
        role=role,
        entrypoints=entrypoints,
        static_paths=tuple(_canonical_relative_path(item) for item in raw_static_paths),
        dynamic_paths=_documented_paths(payload.get("dynamic_paths"), section="dynamic_paths"),
        package_data_paths=_documented_paths(
            payload.get("package_data_paths"), section="package_data_paths"
        ),
    )
    if len(set(manifest.paths)) != len(manifest.paths):
        raise ValueError("source-slice manifest paths must be unique across all sections")
    return manifest


def _module_index(source_root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted((source_root / "core").rglob("*.py")):
        relative = path.relative_to(source_root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _resolve_import_from(
    *, current: str, node: ast.ImportFrom, modules: dict[str, Path]
) -> set[str]:
    if node.level == 0:
        parts: list[str] = []
    else:
        current_path = modules[current]
        current_parts = current.split(".")
        package_parts = current_parts if current_path.name == "__init__.py" else current_parts[:-1]
        keep = len(package_parts) - node.level + 1
        parts = package_parts[: max(keep, 0)]
    if node.module:
        parts.extend(node.module.split("."))
    base = ".".join(parts)
    resolved = {base} if base in modules else set()
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        if candidate in modules:
            resolved.add(candidate)
    return resolved


def _direct_local_imports(
    module: str, modules: dict[str, Path], *, module_scope_only: bool = False
) -> set[str]:
    tree = ast.parse(modules[module].read_text(encoding="utf-8"))
    imports: set[str] = set()
    nodes = tree.body if module_scope_only else ast.walk(tree)
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                for end in range(len(parts), 0, -1):
                    candidate = ".".join(parts[:end])
                    if candidate in modules:
                        imports.add(candidate)
                        break
        elif isinstance(node, ast.ImportFrom):
            imports.update(_resolve_import_from(current=module, node=node, modules=modules))
    return imports


def _package_ancestors(source_root: Path, paths: Iterable[str]) -> set[str]:
    ancestors: set[str] = set()
    for relative in paths:
        path = Path(relative)
        if path.suffix != ".py":
            continue
        parent = path.parent
        while parent != Path("."):
            init_path = parent / "__init__.py"
            if (source_root / init_path).is_file():
                ancestors.add(init_path.as_posix())
            parent = parent.parent
    return ancestors


def static_python_paths(*, source_root: Path, entrypoints: Sequence[str]) -> set[str]:
    modules = _module_index(source_root)
    modules_by_path = {
        path.relative_to(source_root).as_posix(): module for module, path in modules.items()
    }
    missing = sorted(set(entrypoints) - set(modules))
    if missing:
        raise ValueError(f"unknown source-slice entrypoints: {missing}")
    visited: set[str] = set()
    pending = list(entrypoints)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        pending.extend(_direct_local_imports(module, modules) - visited)
        module_path = modules[module].relative_to(source_root).as_posix()
        for path in _package_ancestors(source_root, (module_path,)):
            initializer = modules_by_path[path]
            if initializer in visited:
                continue
            visited.add(initializer)
            pending.extend(
                _direct_local_imports(
                    initializer,
                    modules,
                    module_scope_only=True,
                )
                - visited
            )
    paths = {modules[module].relative_to(source_root).as_posix() for module in visited}
    return paths | _package_ancestors(source_root, paths)


def shortest_local_import_paths(
    *, source_root: Path, entrypoint: str, target_modules: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    modules = _module_index(source_root)
    if entrypoint not in modules:
        raise ValueError(f"unknown source-slice entrypoint: {entrypoint}")
    targets = set(target_modules)
    predecessors: dict[str, str | None] = {entrypoint: None}
    pending: deque[str] = deque((entrypoint,))
    while pending and not targets <= set(predecessors):
        current = pending.popleft()
        for dependency in sorted(_direct_local_imports(current, modules)):
            if dependency in predecessors:
                continue
            predecessors[dependency] = current
            pending.append(dependency)
    paths: dict[str, tuple[str, ...]] = {}
    for target in sorted(targets & set(predecessors)):
        path: list[str] = []
        current: str | None = target
        while current is not None:
            path.append(current)
            current = predecessors[current]
        paths[target] = tuple(reversed(path))
    return paths


def _literal_dynamic_module(node: ast.Call) -> str | None:
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    module_name = node.args[0].value
    if not isinstance(module_name, str) or not module_name.startswith("core."):
        return None
    function = node.func
    if isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}:
        return module_name
    if (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    ):
        return module_name
    return None


def literal_dynamic_python_paths(
    *, source_root: Path, python_paths: Sequence[str]
) -> dict[str, str]:
    modules = _module_index(source_root)
    documented: dict[str, str] = {}
    for relative in sorted(python_paths):
        source = source_root / relative
        if source.suffix != ".py" or not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            module_name = _literal_dynamic_module(node)
            if module_name not in modules:
                continue
            module_path = modules[module_name].relative_to(source_root).as_posix()
            documented.setdefault(
                module_path,
                f"{relative}:{node.lineno} literal dynamic import of {module_name}",
            )
    return documented


def _validated_source_file(source_root: Path, relative: str) -> Path:
    source = source_root / _canonical_relative_path(relative)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"source-slice path must resolve to a regular source file: {relative}")
    if not source.resolve().is_relative_to(source_root.resolve()):
        raise ValueError(f"source-slice path escapes the source root: {relative}")
    return source


def materialize_source_slice(*, source_root: Path, manifest_path: Path, output_root: Path) -> None:
    manifest = load_manifest(manifest_path)
    expected = static_python_paths(source_root=source_root, entrypoints=manifest.entrypoints)
    declared = set(manifest.static_paths)
    if declared != expected:
        raise ValueError(
            f"source-slice static closure mismatch for {manifest.role}: "
            f"missing={sorted(expected - declared)}; extra={sorted(declared - expected)}"
        )
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"source-slice output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(manifest.paths):
        source = _validated_source_file(source_root, relative)
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        source_mode = stat.S_IMODE(source.stat().st_mode)
        destination.chmod(0o755 if source_mode & 0o111 else 0o644)
        os.utime(destination, (0, 0))
    directories = sorted(
        (path for path in output_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        directory.chmod(0o755)
        os.utime(directory, (0, 0))
    output_root.chmod(0o755)
    os.utime(output_root, (0, 0))


def refresh_manifest_static_paths(*, source_root: Path, manifest_path: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entrypoints = tuple(str(item) for item in payload.get("entrypoints") or ())
    static_paths = static_python_paths(source_root=source_root, entrypoints=entrypoints)
    payload["static_paths"] = sorted(static_paths)
    for section in ("dynamic_paths", "package_data_paths"):
        payload[section] = [
            item
            for item in payload.get(section) or ()
            if not isinstance(item, dict) or str(item.get("path") or "") not in static_paths
        ]
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("--source-root", type=Path, required=True)
    materialize.add_argument("--manifest", type=Path, required=True)
    materialize.add_argument("--output-root", type=Path, required=True)
    refresh = subparsers.add_parser("refresh-static")
    refresh.add_argument("--source-root", type=Path, required=True)
    refresh.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "materialize":
        materialize_source_slice(
            source_root=args.source_root,
            manifest_path=args.manifest,
            output_root=args.output_root,
        )
    else:
        refresh_manifest_static_paths(
            source_root=args.source_root,
            manifest_path=args.manifest,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
