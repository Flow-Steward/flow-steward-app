"""Conservative static partition of backend tests by frontend dependencies.

This is an over-approximation, not pytest collection: a mixed module belongs in
the frontend contract group in its entirety. Unresolved reads, imports, fixture
lookups and repository scans also go there. Non-frontend repository files still
belong in the independent group's quality fingerprint.
"""

from __future__ import annotations

import argparse
import ast
import json
from copy import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeGuard

from core.application.tooling.quality.backend_partition_cache import cached_partition

_BUILTIN_FIXTURES = frozenset(
    [
        "self",
        "cls",
        "request",
        "pytestconfig",
        "cache",
        "capsys",
        "capsysbinary",
        "capfd",
        "capfdbinary",
        "caplog",
        "doctest_namespace",
        "monkeypatch",
        "pytester",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "recwarn",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
    ]
)
_FILE_READS = frozenset({"read_text", "read_bytes", "open", "listdir", "scandir"})
_SCANS = frozenset({"glob", "rglob", "iterdir", "walk"})
_DYNAMIC_IMPORTS = frozenset({"import_module", "__import__", "importorskip"})
_PROCESS_CALLS = frozenset({"run", "Popen", "check_call", "check_output", "system", "popen"})
_BACKEND_ROOTS = frozenset(
    {"core", "tests", "db", "docs", "examples", "infra", "locales", "scripts", "extensions"}
)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_INDEPENDENT_SHARED_GUARD = ast.dump(
    ast.parse('os.environ.get("FS_QUALITY_BACKEND_PARTITION") != "independent"', mode="eval").body,
    include_attributes=False,
)


@dataclass(frozen=True)
class BackendTestPartition:
    """Disjoint, exhaustive module selections relative to the repository root."""

    independent: tuple[str, ...]
    frontend_contract: tuple[str, ...]
    reasons: dict[str, tuple[str, ...]]


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _strings(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _frontend(value: str) -> bool:
    value = value.lower().replace("\\", "/")
    return any(
        marker in value
        for marker in (
            "frontend/",
            "/frontend",
            "platform-kit",
            "platform-renderer",
            "platform-components",
            ".tsx",
            ".jsx",
            ".html",
            "/static",
            "/app/assets",
            "pytest_source_repo_root",
        )
    ) or "assets" in value.split("/")


def _fixture(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, _FUNCTIONS) and any(
        _name(decorator.func if isinstance(decorator, ast.Call) else decorator).endswith("fixture")
        for decorator in node.decorator_list
    )


def _fixture_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return node.name


def _autouse(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        keyword.arg == "autouse"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
    )


def _requested_fixtures(node: ast.AST) -> set[str]:
    requested: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, _FUNCTIONS) and (child.name.startswith("test_") or _fixture(child)):
            parameters: set[str] = set()
            for decorator in child.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and _name(decorator.func).endswith("parametrize")
                    and decorator.args
                ):
                    for value in _strings(decorator.args[0]):
                        parameters.update(part.strip() for part in value.split(","))
            requested.update(
                arg.arg
                for arg in (*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs)
                if arg.arg not in parameters
            )
        if isinstance(child, ast.Call) and _name(child.func).endswith("usefixtures"):
            requested.update(value for argument in child.args for value in _strings(argument))
    return requested - _BUILTIN_FIXTURES


class _Analyzer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.trees: dict[Path, ast.Module | None] = {}
        self.analyses: dict[Path, tuple[set[Path], set[str]]] = {}
        self.resolved: dict[str, set[Path]] = {}
        self.safe_closures: set[Path] = set()
        self.unsafe_closures: dict[Path, str] = {}
        self.startups: dict[tuple[Path, ...], set[str]] = {}
        self.startup_analyses: dict[Path, tuple[set[Path], set[str]]] = {}

    def tree(self, path: Path) -> ast.Module | None:
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

                class RuntimeImports(ast.NodeTransformer):
                    def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
                        if _name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                            return node.orelse
                        return self.generic_visit(node)

                parsed_tree = self.trees[path]
                assert parsed_tree is not None
                runtime_tree = RuntimeImports().visit(parsed_tree)
                assert isinstance(runtime_tree, ast.Module)
                self.trees[path] = runtime_tree
            except (OSError, UnicodeError, SyntaxError):
                self.trees[path] = None
        return self.trees[path]

    def resolve(self, module: str) -> set[Path]:
        if module in self.resolved:
            return self.resolved[module]
        parts = module.split(".")
        result: set[Path] = set()
        leaf = self.root.joinpath(*parts)
        if not leaf.with_suffix(".py").is_file() and not leaf.is_dir():
            self.resolved[module] = result
            return result
        for index in range(1, len(parts) + 1):
            package = self.root.joinpath(*parts[:index], "__init__.py")
            if package.is_file():
                result.add(package)
        module_path = self.root.joinpath(*parts).with_suffix(".py")
        if module_path.is_file():
            result.add(module_path)
        self.resolved[module] = result
        return result

    def imports(self, tree: ast.Module, path: Path) -> tuple[set[Path], set[str]]:
        dependencies: set[Path] = set()
        reasons: set[str] = set()
        package = list(path.relative_to(self.root).with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = package[: len(package) - node.level + 1] if node.level else []
                base = ".".join((*prefix, *(node.module or "").split("."))).strip(".")
                modules.append(base)
                modules.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Call):
                call = _name(node.func).split(".")[-1]
                if call in _DYNAMIC_IMPORTS:
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        modules.append(node.args[0].value)
                    else:
                        reasons.add("dynamic import")
                elif call in {"spec_from_file_location", "exec_module", "eval", "exec"}:
                    reasons.add("dynamic execution")
                elif (
                    call in {"patch", "setattr"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    # Patching can import a target without an import statement.
                    target = node.args[0].value
                    if target.startswith(("core.", "tests.", "serve.")):
                        modules.extend(
                            target.rsplit(".", index)[0]
                            for index in range(1, target.count(".") + 1)
                        )
            for module in modules:
                dependencies.update(self.resolve(module))
                if (
                    module.startswith(("core.", "tests.", "extensions."))
                    and not self.resolve(module)
                    and module == modules[0]
                ):
                    # A from-import may name an attribute, not a module. Only an
                    # unavailable top-level imported module is unresolved.
                    reasons.add("unresolved local import")
        dependencies.discard(path)
        return dependencies, reasons

    def analyze(self, path: Path, tree: ast.Module | None = None) -> tuple[set[Path], set[str]]:
        if tree is None and path in self.analyses:
            return self.analyses[path]
        supplied_tree = tree is not None
        tree = tree if supplied_tree else self.tree(path)
        if tree is None:
            return set(), {"unreadable Python source"}
        dependencies, reasons = self.imports(tree, path)
        if any(_frontend(value) for value in _strings(tree)) or _frontend(path.as_posix()):
            reasons.add("frontend or source-checkout reference")
        bindings: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        if target.id in bindings:
                            bindings[target.id] = ast.Name(id="__ambiguous_path__")
                        else:
                            bindings[target.id] = node.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            leaf = name.split(".")[-1]
            if leaf == "getfixturevalue":
                reasons.add("dynamic fixture lookup")
            if leaf in _PROCESS_CALLS and (
                name.startswith(("subprocess.", "os."))
                or leaf in {"Popen", "check_output", "check_call"}
            ):
                reasons.add("subprocess or shell execution")
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module in {"subprocess", "os"}
            and any(alias.name in _PROCESS_CALLS for alias in node.names)
            for node in ast.walk(tree)
        ):
            reasons.add("imported subprocess or shell function")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            leaf = name.split(".")[-1]
            if leaf in _FILE_READS | _SCANS:
                receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
                if (
                    leaf in {"open", "listdir", "scandir", "walk"}
                    and node.args
                    and (receiver is None or _name(receiver) in {"os", "io", "builtins"})
                ):
                    receiver = node.args[0]
                kind = _path_kind(receiver, bindings, set()) if receiver is not None else "unknown"
                if kind not in {"temporary", "backend"}:
                    reasons.add("unbounded filesystem input")
        result = dependencies, reasons
        if not supplied_tree:
            self.analyses[path] = result
        return result

    def closure_reasons(self, roots: set[Path]) -> set[str]:
        for root in sorted(roots):
            pending = [root]
            seen: set[Path] = set()
            while pending:
                path = pending.pop()
                if path in seen or path in self.safe_closures:
                    continue
                seen.add(path)
                if path in self.unsafe_closures:
                    return {self.unsafe_closures[path]}
                dependencies, direct = self.analyze(path)
                if direct:
                    reason = f"{path.relative_to(self.root).as_posix()}: {sorted(direct)[0]}"
                    self.unsafe_closures[root] = reason
                    return {reason}
                pending.extend(sorted(dependencies - seen, reverse=True))
            self.safe_closures.update(seen)
        return set()

    def startup_reasons(self, roots: set[Path]) -> set[str]:
        """Inspect shared imports as imports, without executing dormant APIs.

        Independent execution additionally removes assets from its workspace;
        this is the runtime backstop for startup metaprogramming. Requested
        fixtures and test imports use the stricter full-function closure.
        """
        identity = tuple(sorted(roots))
        if identity not in self.startups:
            self.startups[identity] = self._startup_reasons(identity)
        return self.startups[identity]

    def called_imports(self, imports: ast.Module, active: ast.Module, path: Path) -> set[Path]:
        called = {
            _name(node.func).split(".")[0]
            for node in ast.walk(active)
            if isinstance(node, ast.Call)
        }
        selected: list[ast.stmt] = []
        for node in ast.walk(imports):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [
                    alias
                    for alias in node.names
                    if (alias.asname or alias.name.split(".")[0]) in called
                ]
                if names:
                    selected.append(
                        ast.Import(names=names)
                        if isinstance(node, ast.Import)
                        else ast.ImportFrom(module=node.module, names=names, level=node.level)
                    )
        return self.imports(ast.Module(body=selected, type_ignores=[]), path)[0]

    def _startup_reasons(self, roots: tuple[Path, ...]) -> set[str]:
        pending = list(roots)
        seen: set[Path] = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            tree = self.tree(path)
            if tree is None:
                return {f"{path.relative_to(self.root)}: unreadable startup dependency"}
            if path not in self.startup_analyses:
                eager = _startup_tree(tree)
                dependencies, reasons = self.analyze(path, eager)
                # Path constants alone do not read frontend files on import.
                reasons.discard("frontend or source-checkout reference")
                if any("PYTEST_SOURCE_REPO_ROOT" in value for value in _strings(tree)):
                    reasons.add("source-checkout access")
                reasons.update(self.closure_reasons(self.called_imports(eager, eager, path)))
                self.startup_analyses[path] = dependencies, reasons
            dependencies, reasons = self.startup_analyses[path]
            if reasons:
                return {f"{path.relative_to(self.root)}: startup {sorted(reasons)[0]}"}
            pending.extend(sorted(dependencies - seen))
        return set()


def _prune_startup(node: ast.AST, selected: set[str]) -> ast.AST:
    if isinstance(node, (*_FUNCTIONS, ast.ClassDef)) and node.name in selected:
        return node
    if isinstance(node, _FUNCTIONS):
        function = copy(node)
        function.body = [ast.Pass()]
        return function
    result = copy(node)
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            setattr(result, field, _prune_startup(value, selected))
        elif isinstance(value, list):
            setattr(
                result,
                field,
                [
                    _prune_startup(child, selected) if isinstance(child, ast.AST) else child
                    for child in value
                ],
            )
    return result


def _startup_tree(tree: ast.Module) -> ast.Module:
    definitions = {
        node.name: node for node in tree.body if isinstance(node, (*_FUNCTIONS, ast.ClassDef))
    }
    selected: set[str] = set()

    # Shallow functional copies skip dormant bodies and preserve the original
    # tree for the full-function analysis of tests and requested fixtures.
    while True:
        eager = _prune_startup(tree, selected)
        assert isinstance(eager, ast.Module)
        calls = {_name(node.func) for node in ast.walk(eager) if isinstance(node, ast.Call)}
        additional = (calls & definitions.keys()) - selected
        if not additional:
            return eager
        selected.update(additional)


def _independent_shared_tree(tree: ast.Module) -> ast.Module:
    """Apply only the runtime guard owned by the independent test runner."""
    body: list[ast.stmt] = []
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and ast.dump(node.test, include_attributes=False) == _INDEPENDENT_SHARED_GUARD
        ):
            body.extend(node.orelse)
        else:
            body.append(node)
    return ast.Module(body=body, type_ignores=tree.type_ignores)


def _path_kind(node: ast.AST, bindings: dict[str, ast.AST], seen: set[str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _frontend(node.value):
            return "frontend"
        value = node.value.replace("\\", "/").split("/")[0]
        if (
            value in _BACKEND_ROOTS
            or value in {"VERSION", "pyproject.toml", "pytest.ini"}
            or value.startswith(("requirements", ".env.example"))
        ):
            return "backend"
        return "unknown"
    if isinstance(node, ast.Name):
        if node.id in {"tmp_path", "tmpdir", "tmp_path_factory", "tmpdir_factory"}:
            return "temporary"
        if node.id == "__file__":
            return "backend"
        if node.id in bindings and node.id not in seen:
            return _path_kind(bindings[node.id], bindings, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        left = _path_kind(node.left, bindings, seen)
        right = _path_kind(node.right, bindings, seen)
        if "frontend" in {left, right}:
            return "frontend"
        return left if left in {"backend", "temporary"} else right
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute) and node.attr in {"parent", "parents"}:
            return "root"
        return _path_kind(node.value, bindings, seen)
    if isinstance(node, ast.Call):
        name = _name(node.func)
        if name.startswith("tempfile."):
            return "temporary"
        if name in {"Path", "pathlib.Path", "str"} and node.args:
            return _path_kind(node.args[0], bindings, seen)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in {"joinpath", "join"}:
                kinds = {_path_kind(arg, bindings, seen) for arg in node.args}
                if "frontend" in kinds:
                    return "frontend"
                if "backend" in kinds:
                    return "backend"
            return _path_kind(node.func.value, bindings, seen)
    return "unknown"


def _shared_context(
    analyzer: _Analyzer, test: Path, tree: ast.Module
) -> tuple[set[Path], set[str]]:
    shared = [directory / "conftest.py" for directory in reversed(test.parent.parents)]
    shared.append(test.parent / "conftest.py")
    shared = [path for path in shared if path.is_relative_to(analyzer.root) and path.is_file()]
    modules = {
        path: _independent_shared_tree(module)
        if (module := analyzer.tree(path)) is not None
        else None
        for path in shared
    }
    fixtures: dict[str, tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    selected: dict[Path, set[str]] = {path: set() for path in shared}
    reasons: set[str] = set()
    dependencies: set[Path] = set()
    for path, module in modules.items():
        if module is None:
            reasons.add(f"{path.relative_to(analyzer.root)}: unreadable conftest")
            continue
        for node in module.body:
            if _fixture(node):
                fixtures[_fixture_name(node)] = path, node
                if _autouse(node):
                    selected[path].add(node.name)
    local_fixtures = {_fixture_name(node) for node in tree.body if _fixture(node)}
    requested = _requested_fixtures(tree) - local_fixtures
    requested.update(name for name, (path, node) in fixtures.items() if node.name in selected[path])
    seen: set[str] = set()
    while requested - seen:
        name = sorted(requested - seen)[0]
        seen.add(name)
        if name in _BUILTIN_FIXTURES or name in local_fixtures:
            continue
        if name not in fixtures:
            reasons.add(f"unknown fixture: {name}")
            continue
        path, fixture = fixtures[name]
        selected[path].add(fixture.name)
        requested.update(arg.arg for arg in fixture.args.args)
    for path, module in modules.items():
        if module is None:
            continue
        functions = {node.name: node for node in module.body if isinstance(node, _FUNCTIONS)}
        body = [node for node in module.body if not isinstance(node, _FUNCTIONS)]
        selected[path].update(name for name in functions if name.startswith("pytest_"))
        references = {
            node.id
            for node in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(node, ast.Name)
        }
        selected[path].update(references & functions.keys())
        expanded: set[str] = set()
        while selected[path] - expanded:
            name = sorted(selected[path] - expanded)[0]
            expanded.add(name)
            function = functions[name]
            body.append(function)
            selected[path].update(
                node.id
                for node in ast.walk(function)
                if isinstance(node, ast.Name) and node.id in functions
            )
        imported, direct = analyzer.analyze(path, ast.Module(body=body, type_ignores=[]))
        # Imports made by selected fixtures can run the imported API. Imports
        # used only during conftest initialization are inspected as startup.
        active_tree = ast.Module(body=body, type_ignores=[])
        fixture_imports = analyzer.called_imports(module, active_tree, path)
        dependencies.update(fixture_imports)
        direct.update(analyzer.startup_reasons(imported - fixture_imports))
        reasons.update(f"{path.relative_to(analyzer.root)}: {reason}" for reason in direct)
    return dependencies, reasons


def partition_backend_tests(root: Path) -> BackendTestPartition:
    """Assign every non-stress unit test module once, without importing tests."""
    root = root.resolve()
    analyzer = _Analyzer(root)
    independent: list[str] = []
    contracts: list[str] = []
    explanations: dict[str, tuple[str, ...]] = {}
    modules = set((root / "tests/unit").rglob("test_*.py"))
    modules.update((root / "tests/unit").rglob("*_test.py"))
    for test in sorted(modules):
        if "stress" in test.relative_to(root / "tests/unit").parts:
            continue
        relative = test.relative_to(root).as_posix()
        tree = analyzer.tree(test)
        dependencies, reasons = analyzer.analyze(test)
        dependencies = set(dependencies)
        dependencies.update(
            directory / "__init__.py"
            for directory in test.parent.parents
            if directory.is_relative_to(root) and (directory / "__init__.py").is_file()
        )
        if (test.parent / "__init__.py").is_file():
            dependencies.add(test.parent / "__init__.py")
        if tree is not None:
            shared_dependencies, shared_reasons = _shared_context(analyzer, test, tree)
            dependencies.update(shared_dependencies)
            reasons = reasons | shared_reasons
        if not reasons:
            reasons = analyzer.closure_reasons(dependencies)
        if reasons:
            contracts.append(relative)
            explanations[relative] = tuple(sorted(reasons))
        else:
            independent.append(relative)
    return BackendTestPartition(tuple(independent), tuple(contracts), explanations)


def main() -> int:
    """Print explicit pytest module arguments, refusing an empty selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--group", choices=("independent", "frontend-contract"))
    args = parser.parse_args()
    cached = cached_partition(
        args.repository_root, lambda: asdict(partition_backend_tests(args.repository_root))
    )
    result = BackendTestPartition(
        tuple(cached["independent"]), tuple(cached["frontend_contract"]), cached["reasons"]
    )
    if args.group is None:
        print(
            json.dumps(
                {
                    "independent": result.independent,
                    "frontend_contract": result.frontend_contract,
                    "reasons": result.reasons,
                },
                sort_keys=True,
            )
        )
        return 0
    selection = result.independent if args.group == "independent" else result.frontend_contract
    if not selection:
        parser.error(f"backend partition {args.group!r} contains no test modules")
    print("\n".join(selection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
