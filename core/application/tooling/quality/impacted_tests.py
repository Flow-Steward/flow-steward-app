"""Select the backend unit tests a change reaches, for Tier 1 focused runs.

Tier 1 must cover the changed module, its tests and its direct consumers
(docs/standards/testing-standard.md, Test Selection). Picking those by hand is
where Tier 3 surprises come from, so this derives them from the tree:

- unit test files that changed;
- unit tests importing a changed module or a module that imports one;
- unit tests whose source names a changed non-Python file (text-pin tests that
  read lint.sh, compose files, the check registry and the like);
- every test under a directory whose ``conftest.py`` changed.

The import graph is read with ``ast`` and never imports project code.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from collections.abc import Iterable
from pathlib import Path

UNIT_TESTS = Path("tests/unit")
SCANNED_ROOTS = (Path("core"), Path("tests"))
# Names too common to identify one file when a test mentions them.
GENERIC_NAMES = frozenset(
    {
        "__init__.py",
        "README.md",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "conftest.py",
        "index.ts",
        "index.tsx",
        ".env.example",
    }
)


def changed_files(root: Path, base: str) -> list[str]:
    """Files that differ from the merge base with ``base``, staged, unstaged or untracked."""

    def git(*args: str) -> list[str]:
        result = subprocess.run(
            ("git", *args), cwd=root, check=True, capture_output=True, text=True
        )
        return [line for line in result.stdout.splitlines() if line]

    merge_base = git("merge-base", "HEAD", base)[0]
    files = git("diff", "--name-only", merge_base)
    files += git("ls-files", "--others", "--exclude-standard")
    return sorted(set(files))


def module_name(path: Path) -> str | None:
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _imports(path: Path, module: str) -> set[str]:
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


class ImportGraph:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths: dict[str, Path] = {}
        for scanned in SCANNED_ROOTS:
            for path in sorted((root / scanned).rglob("*.py")):
                relative = path.relative_to(root)
                module = module_name(relative)
                if module:
                    self.paths[module] = relative
        self.imports: dict[str, set[str]] = {}
        self.importers: dict[str, set[str]] = {}
        for module, relative in self.paths.items():
            resolved = {
                target
                for name in _imports(root / relative, module)
                if (target := self._resolve(name)) and target != module
            }
            self.imports[module] = resolved
            for target in resolved:
                self.importers.setdefault(target, set()).add(module)

    def _resolve(self, name: str) -> str | None:
        """Longest known module prefix of ``name`` (``core.a.b.Thing`` -> ``core.a.b``)."""
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in self.paths:
                return candidate
            parts.pop()
        return None

    def direct_importers(self, modules: Iterable[str]) -> set[str]:
        found: set[str] = set()
        for module in modules:
            found.update(self.importers.get(module, ()))
        return found


def _is_unit_test(path: Path) -> bool:
    return (
        path.is_relative_to(UNIT_TESTS) and path.name.startswith("test_") and path.suffix == ".py"
    )


def select_unit_tests(root: Path, changed: Iterable[str]) -> list[str]:
    graph = ImportGraph(root)
    unit_tests = [path for path in graph.paths.values() if _is_unit_test(path)]
    selected: set[Path] = set()
    changed_modules: set[str] = set()
    named_files: list[str] = []

    for raw in changed:
        path = Path(raw)
        if not (root / path).exists():
            continue
        if path.name == "conftest.py" and path.is_relative_to(UNIT_TESTS):
            directory = path.parent
            selected.update(test for test in unit_tests if test.is_relative_to(directory))
            continue
        if _is_unit_test(path):
            selected.add(path)
        module = module_name(path)
        if module in graph.paths:
            changed_modules.add(module)
        if path.suffix != ".py":
            named_files.append(raw)

    reached = changed_modules | graph.direct_importers(changed_modules)
    selected.update(
        test
        for test in unit_tests
        if (module := module_name(test)) and graph.imports.get(module, set()) & reached
    )

    if named_files:
        needles = set(named_files)
        needles.update(
            Path(name).name for name in named_files if Path(name).name not in GENERIC_NAMES
        )
        for test in unit_tests:
            if test in selected:
                continue
            try:
                source = (root / test).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(needle in source for needle in needles):
                selected.add(test)

    return sorted(str(path) for path in selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", default="main", help="Ref whose merge base the change is measured from."
    )
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("files", nargs="*", help="Changed files; default: derived from git.")
    args = parser.parse_args(argv)
    root = Path(args.repository_root).resolve()
    files = args.files or changed_files(root, args.base)
    for test in select_unit_tests(root, files):
        print(test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
