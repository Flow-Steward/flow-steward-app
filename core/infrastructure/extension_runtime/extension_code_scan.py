"""A static look at an extension's own Python code before it is installed.

This is not the security boundary. Python can reach anything indirectly, and
the libraries an extension depends on legitimately use the network, files and
processes. The scan only catches what an extension's own code has no business
doing in plain sight, and points the administrator at what deserves a look:

- errors refuse the bundle: dynamic code (``exec``, ``eval``, ``compile``,
  ``__import__`` or ``importlib.import_module`` with a computed name), native
  code (``ctypes``, ``cffi``), shells and process replacement (``os.system``,
  ``os.popen``, ``os.exec*``, ``os.spawn*``, ``os.fork``, ``pty.spawn``,
  ``subprocess`` with ``shell=True``, ``asyncio.create_subprocess_shell``),
  and loading pickled or marshalled data;
- warnings are listed for review: raw sockets, processes without a shell,
  opening an absolute path, and changing ownership, permissions or identity.

Tests, caches and virtual environments inside the bundle are not scanned.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

MAX_SCANNED_FILES = 2_000
MAX_SCANNED_BYTES = 5 * 1024 * 1024

_SKIPPED_DIRS = frozenset(
    {
        "tests",
        "test",
        "__pycache__",
        ".venv",
        "venv",
        "site-packages",
        "node_modules",
        "dev-wheels",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
_RUNS_A_SHELL = "runs a shell command"
_FORBIDDEN_BUILTINS = {
    "exec": "runs code built from a string",
    "eval": "runs code built from a string",
    "compile": "builds code from a string",
}
_FORBIDDEN_MODULES = {
    "ctypes": "loads native code",
    "cffi": "loads native code",
}
_FORBIDDEN_CALLS = {
    ("os", "system"): _RUNS_A_SHELL,
    ("os", "popen"): _RUNS_A_SHELL,
    ("os", "fork"): "forks the process",
    ("os", "forkpty"): "forks the process",
    ("pty", "spawn"): "starts a process in a terminal",
    ("asyncio", "create_subprocess_shell"): _RUNS_A_SHELL,
    ("pickle", "loads"): "loads pickled data, which can run code",
    ("pickle", "load"): "loads pickled data, which can run code",
    ("marshal", "loads"): "loads marshalled code",
    ("marshal", "load"): "loads marshalled code",
}
_FORBIDDEN_CALL_PREFIXES = {
    ("os", "exec"): "replaces the process",
    ("os", "spawn"): "starts a process",
}
_REVIEW_MODULES = {
    "socket": "opens raw network connections",
}
_REVIEW_CALLS = {
    ("os", "setuid"): "changes the process identity",
    ("os", "setgid"): "changes the process identity",
    ("os", "chown"): "changes file ownership",
    ("os", "chmod"): "changes file permissions",
    ("shutil", "chown"): "changes file ownership",
}
_SUBPROCESS_FUNCTIONS = frozenset({"run", "call", "check_call", "check_output", "Popen"})


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        # Local name -> the module it stands for (``import os as o``, ``from os import system``).
        self.aliases: dict[str, str] = {}
        self.findings: list[tuple[str, int, str, str]] = []

    def _add(self, severity: str, node: ast.AST, what: str, why: str) -> None:
        self.findings.append((severity, getattr(node, "lineno", 0), what, why))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.aliases[alias.asname or root] = alias.name if alias.asname else root
            self._check_module(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self._check_module(node, module)
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    def _check_module(self, node: ast.AST, module: str) -> None:
        root = module.split(".")[0]
        if root in _FORBIDDEN_MODULES:
            self._add("error", node, f"import {module}", _FORBIDDEN_MODULES[root])
        elif root in _REVIEW_MODULES:
            self._add("warning", node, f"import {module}", _REVIEW_MODULES[root])

    def _qualified(self, func: ast.AST) -> tuple[str, str]:
        """``(module, attribute)`` for a call, resolved through import aliases."""
        dotted = _dotted(func)
        if not dotted:
            return "", ""
        head, _, rest = dotted.partition(".")
        resolved = self.aliases.get(head, head)
        full = f"{resolved}.{rest}" if rest else resolved
        module, _, attribute = full.rpartition(".")
        return module, attribute

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # A name imported under the same spelling (``from re import compile``) is not the builtin.
        if (
            isinstance(func, ast.Name)
            and func.id in _FORBIDDEN_BUILTINS
            and func.id not in self.aliases
        ):
            self._add("error", node, f"{func.id}()", _FORBIDDEN_BUILTINS[func.id])
        module, attribute = self._qualified(func)
        name = f"{module}.{attribute}" if module else attribute
        computed_name = not node.args or not isinstance(node.args[0], ast.Constant)
        if name in {"__import__", "builtins.__import__"}:
            self._add("error", node, f"{name}()", "imports a module by a runtime name")
        elif name == "importlib.import_module" and computed_name:
            self._add("error", node, f"{name}()", "imports a module whose name is computed")
        key = (module, attribute)
        if key in _FORBIDDEN_CALLS:
            self._add("error", node, f"{name}()", _FORBIDDEN_CALLS[key])
        for (prefix_module, prefix), why in _FORBIDDEN_CALL_PREFIXES.items():
            if module == prefix_module and attribute.startswith(prefix):
                self._add("error", node, f"{name}()", why)
        if key in _REVIEW_CALLS:
            self._add("warning", node, f"{name}()", _REVIEW_CALLS[key])
        if module == "subprocess" and attribute in _SUBPROCESS_FUNCTIONS:
            shell = next((kw.value for kw in node.keywords if kw.arg == "shell"), None)
            if (isinstance(shell, ast.Constant) and shell.value is False) or shell is None:
                self._add("warning", node, f"{name}()", "starts a process")
            else:
                self._add("error", node, f"{name}(shell=...)", _RUNS_A_SHELL)
        if name in {"open", "io.open", "builtins.open"} and node.args:
            path = node.args[0]
            if isinstance(path, ast.Constant) and str(path.value).startswith("/"):
                self._add(
                    "warning", node, f"open({path.value!r})", "reads or writes outside the bundle"
                )
        self.generic_visit(node)


def _is_skipped_file(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _python_files(bundle_root: Path) -> tuple[list[Path], bool]:
    """The bundle's own Python files, and whether there were more than can be reviewed.

    Skipped directories are pruned during the walk, and the walk stops once the
    file limit is passed, so a huge tree is never listed in full.
    """
    files: list[Path] = []
    for directory, subdirectories, names in os.walk(bundle_root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in _SKIPPED_DIRS and not name.startswith(".")
        )
        for name in sorted(names):
            if name.endswith(".py") and not _is_skipped_file(name):
                files.append(Path(directory) / name)
                if len(files) > MAX_SCANNED_FILES:
                    return files[:MAX_SCANNED_FILES], True
    return files, False


def _is_python(executable: str) -> bool:
    name = Path(str(executable)).name
    return name == "python" or name.startswith("python3")


def _declared_code(
    bundle_root: Path, commands: list[Any]
) -> tuple[list[tuple[str, str]], list[Path]]:
    """Inline ``python -c`` sources and the ``.py`` files the manifest's commands run.

    A declared file is scanned even where the walk would skip it (``tests/``,
    a ``test_`` name): it is what actually runs.
    """
    inline: list[tuple[str, str]] = []
    declared: list[Path] = []
    for label, command in commands:
        if not isinstance(command, list) or not command:
            continue
        parts = [str(part) for part in command]
        if _is_python(parts[0]) and "-c" in parts[1:]:
            index = parts.index("-c", 1)
            if index + 1 < len(parts):
                inline.append((f"<{label} -c>", parts[index + 1]))
        for part in parts[1:]:
            if part.endswith(".py"):
                candidate = (bundle_root / part).resolve()
                if candidate.is_file() and candidate.is_relative_to(bundle_root.resolve()):
                    declared.append(candidate)
    return inline, declared


def _issue(code: str, message: str, file_path: str, line: int | None, severity: str) -> dict:
    return {
        "code": code,
        "message": message,
        "file_path": file_path,
        "field_path": f"line {line}" if line else "",
        "severity": severity,
    }


def _scan_source(
    source: str | bytes,
    label: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    try:
        tree = ast.parse(source, filename=label)
    except (SyntaxError, ValueError) as exc:
        errors.append(
            _issue(
                "extension_code_unparseable",
                f"{label} is not valid Python: {exc}",
                label,
                None,
                "error",
            )
        )
        return
    scanner = _Scanner()
    scanner.visit(tree)
    for severity, line, what, why in scanner.findings:
        code = "extension_code_forbidden" if severity == "error" else "extension_code_review"
        issue = _issue(code, f"{label}:{line}: {what} {why}", label, line, severity)
        (errors if severity == "error" else warnings).append(issue)


def _too_large(label: str) -> dict[str, Any]:
    return _issue(
        "extension_code_too_large",
        f"The extension has more Python code than can be reviewed "
        f"({MAX_SCANNED_FILES} files, {MAX_SCANNED_BYTES} bytes)",
        label,
        None,
        "error",
    )


def scan_extension_code(
    bundle_root: str | Path,
    commands: list[tuple[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(errors, warnings)`` for the extension's own Python code.

    ``commands`` are the manifest's ``(label, command)`` pairs (entrypoint,
    health): inline ``python -c`` sources are scanned by the same rules as
    files, and the files they name are always scanned.
    """
    root = Path(bundle_root)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not root.is_dir():
        return errors, warnings
    budget = MAX_SCANNED_BYTES
    inline, declared = _declared_code(root, list(commands or []))
    for label, source in inline:
        size = len(source.encode("utf-8"))
        if size > budget:
            errors.append(_too_large(label))
            return errors, warnings
        budget -= size
        _scan_source(source, label, errors, warnings)
    files, too_many = _python_files(root)
    if too_many:
        errors.append(_too_large(""))
        return errors, warnings
    seen: set[Path] = set()
    for path in [*declared, *files]:
        resolved = path.resolve()
        # A link leading out of the bundle is the bundle boundary's concern.
        if resolved in seen or not resolved.is_relative_to(root.resolve()):
            continue
        seen.add(resolved)
        relative = resolved.relative_to(root.resolve()).as_posix()
        # Read at most what is left of the budget, plus one byte to notice more.
        with path.open("rb") as handle:
            source = handle.read(budget + 1)
        if len(source) > budget:
            errors.append(_too_large(relative))
            return errors, warnings
        budget -= len(source)
        _scan_source(source, relative, errors, warnings)
    return errors, warnings


__all__ = ["scan_extension_code"]
