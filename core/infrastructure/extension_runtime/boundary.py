from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

_MESSAGE_EXTENSION_COMMAND_PATH_MUST_STAY_WITHIN_ITS_BUNDLE = (
    "Extension command path must stay within its bundle"
)
_MESSAGE_USE_A_BUNDLE_RELATIVE_PY_SCRIPT_PATH_WITHOUT_PARENT_TRAVERSAL = (
    "Use a bundle-relative .py script path without parent traversal."
)

GRANDFATHERED_EXTENSION_RUNTIME_BOUNDARY_VIOLATIONS: frozenset[tuple[str, str]] = frozenset()

_IGNORED_RUNTIME_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    "test",
    "scripts",
    "docs",
    "artifacts",
    "ui",
    "schemas",
    "contracts",
    "wheels",
    ".fs-python",
}

_PYTHON_LAUNCHER_NAME_RE = re.compile(
    r"^(?:python|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?$",
    re.IGNORECASE,
)


def _issue(
    *,
    severity: str,
    code: str,
    message: str,
    file_path: str,
    field_path: str = "runtime_import_boundary",
    hint: str = "",
) -> dict[str, str]:
    payload = {
        "severity": severity,
        "code": code,
        "message": message,
        "file_path": file_path,
        "field_path": field_path,
    }
    if hint:
        payload["hint"] = hint
    return payload


def _approved_python_launcher(value: str) -> bool:
    token = str(value or "").strip()
    if not token or not _PYTHON_LAUNCHER_NAME_RE.fullmatch(Path(token).name):
        return False
    launcher = Path(token)
    if launcher.is_absolute():
        try:
            return launcher.resolve(strict=False) == Path(sys.executable).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
    return launcher.name == token


def _command_python_path_argument(command: list[str]) -> Path | None:
    if len(command) != 2 or not _approved_python_launcher(command[0]):
        return None
    candidate = Path(command[1])
    return candidate if candidate.suffix.lower() == ".py" else None


def _command_python_file(bundle_root: Path, command: list[str]) -> Path | None:
    candidate = _command_python_path_argument(command)
    if candidate is None or candidate.suffix.lower() != ".py" or candidate.is_absolute():
        return None
    resolved = (bundle_root / candidate).resolve()
    try:
        resolved.relative_to(bundle_root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _command_python_inline_source(command: list[str]) -> str | None:
    if len(command) != 3 or not _approved_python_launcher(command[0]) or command[1] != "-c":
        return None
    source = str(command[2] or "").strip()
    return source or None


def _command_value(payload: dict[str, Any] | None) -> list[str]:
    value = (payload or {}).get("command")
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _manifest_command_python_files(
    bundle_root: Path,
    *,
    entrypoint: dict[str, Any] | None,
    healthcheck: dict[str, Any] | None,
) -> set[Path]:
    files: set[Path] = set()
    for payload in (entrypoint, healthcheck):
        path = _command_python_file(bundle_root, _command_value(payload))
        if path is not None:
            files.add(path)
    return files


def validate_extension_runtime_command(
    *,
    bundle_root: str | Path,
    command: list[str],
    field_path: str,
) -> list[dict[str, str]]:
    root_text = str(bundle_root or "").strip()
    root = Path(root_text).resolve() if root_text else None
    normalized = [str(item or "").strip() for item in command if str(item or "").strip()]
    command_file = f"{root / 'extension.yaml'}:{field_path}" if root is not None else field_path
    unsupported_message = (
        "Extension command must use an approved Python launcher with either "
        "one bundle-contained .py script or safe inline Python via -c"
    )
    if not normalized or not _approved_python_launcher(normalized[0]):
        return [
            _issue(
                severity="error",
                code="extension_command_unsupported",
                message=unsupported_message,
                file_path=normalized[0] if normalized else command_file,
                field_path=field_path,
            )
        ]

    script_path = _command_python_path_argument(normalized)
    if script_path is not None:
        if root is None or not root.is_dir():
            return [
                _issue(
                    severity="error",
                    code="extension_command_bundle_root_required",
                    message=("Extension file command requires an explicit existing bundle root"),
                    file_path=root_text or command_file,
                    field_path=field_path,
                )
            ]
        if script_path.is_absolute() or ".." in script_path.parts:
            return [
                _issue(
                    severity="error",
                    code="extension_command_path_escape",
                    message=_MESSAGE_EXTENSION_COMMAND_PATH_MUST_STAY_WITHIN_ITS_BUNDLE,
                    file_path=str(script_path),
                    field_path=field_path,
                    hint=_MESSAGE_USE_A_BUNDLE_RELATIVE_PY_SCRIPT_PATH_WITHOUT_PARENT_TRAVERSAL,
                )
            ]
        try:
            resolved_script = (root / script_path).resolve(strict=True)
            resolved_script.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return [
                _issue(
                    severity="error",
                    code="extension_command_path_escape",
                    message=_MESSAGE_EXTENSION_COMMAND_PATH_MUST_STAY_WITHIN_ITS_BUNDLE,
                    file_path=str(script_path),
                    field_path=field_path,
                    hint=_MESSAGE_USE_A_BUNDLE_RELATIVE_PY_SCRIPT_PATH_WITHOUT_PARENT_TRAVERSAL,
                )
            ]
        if not resolved_script.is_file():
            return [
                _issue(
                    severity="error",
                    code="extension_command_path_escape",
                    message=_MESSAGE_EXTENSION_COMMAND_PATH_MUST_STAY_WITHIN_ITS_BUNDLE,
                    file_path=str(script_path),
                    field_path=field_path,
                    hint=_MESSAGE_USE_A_BUNDLE_RELATIVE_PY_SCRIPT_PATH_WITHOUT_PARENT_TRAVERSAL,
                )
            ]
        return []

    inline_source = _command_python_inline_source(normalized)
    if inline_source is None:
        return [
            _issue(
                severity="error",
                code="extension_command_unsupported",
                message=unsupported_message,
                file_path=command_file,
                field_path=field_path,
            )
        ]
    try:
        tree = ast.parse(inline_source, filename=field_path)
    except SyntaxError:
        return [
            _issue(
                severity="error",
                code="extension_command_inline_syntax",
                message="Extension inline Python command must have valid syntax",
                file_path=command_file,
                field_path=field_path,
            )
        ]
    issues = _runtime_boundary_tree_issues(
        tree=tree,
        file_path=command_file,
        file_is_grandfathered=False,
    )
    for issue in issues:
        issue["field_path"] = field_path
    return issues


def _manifest_command_issues(
    bundle_root: Path,
    *,
    entrypoint: dict[str, Any] | None,
    healthcheck: dict[str, Any] | None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for label, payload in (("entrypoint.command", entrypoint), ("health.command", healthcheck)):
        command = _command_value(payload)
        if (
            label == "entrypoint.command"
            and payload
            and str(payload.get("mode") or "subprocess").strip().lower() != "remote_https"
            and not command
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="extension_command_required",
                    message="Local extension entrypoint command is required",
                    file_path=str(bundle_root / "extension.yaml"),
                    field_path=label,
                )
            )
        if command:
            issues.extend(
                validate_extension_runtime_command(
                    bundle_root=bundle_root,
                    command=command,
                    field_path=label,
                )
            )
    return issues


def _runtime_python_files(bundle_root: Path, *, always_include: set[Path]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(bundle_root.rglob("*.py")):
        resolved = path.resolve()
        relative_parts = path.relative_to(bundle_root).parts
        if resolved not in always_include and any(
            part in _IGNORED_RUNTIME_PARTS for part in relative_parts
        ):
            continue
        files.append(resolved)
    for path in sorted(always_include):
        if path not in files:
            files.append(path)
    return files


def _relative_posix(bundle_root: Path, path: Path) -> str:
    return path.relative_to(bundle_root).as_posix()


def _is_grandfathered(extension_id: str, relative_path: str) -> bool:
    return (extension_id, relative_path) in GRANDFATHERED_EXTENSION_RUNTIME_BOUNDARY_VIOLATIONS


def _module_is_private_core(module_name: str) -> bool:
    return module_name == "core" or module_name.startswith("core.")


def _literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):
        return str(node.s)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left and right:
            return f"{left}{right}"
    return ""


def _node_is_sys_path_reference(
    node: ast.AST,
    *,
    sys_names: set[str],
    sys_path_names: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in sys_path_names
    if isinstance(node, ast.Attribute) and node.attr == "path":
        owner = node.value
        return isinstance(owner, ast.Name) and owner.id in sys_names
    return False


def _assignment_target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in node.elts:
            names.update(_assignment_target_names(element))
        return names
    return set()


def _runtime_boundary_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    sys_names = {"sys"}
    sys_path_names: set[str] = set()
    importlib_names = {"importlib"}
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = str(alias.asname or alias.name.split(".", 1)[0] or "").strip()
                if alias.name == "sys" and local_name:
                    sys_names.add(local_name)
                elif alias.name == "importlib" and local_name:
                    importlib_names.add(local_name)
        elif isinstance(node, ast.ImportFrom):
            module_name = str(node.module or "").strip()
            for alias in node.names:
                local_name = str(alias.asname or alias.name or "").strip()
                if not local_name:
                    continue
                if module_name == "sys" and alias.name == "path":
                    sys_path_names.add(local_name)
                elif module_name == "importlib" and alias.name == "import_module":
                    import_module_names.add(local_name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                value = node.value
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                targets = [node.target]
            if value is None or not _node_is_sys_path_reference(
                value,
                sys_names=sys_names,
                sys_path_names=sys_path_names,
            ):
                continue
            for target in targets:
                for name in _assignment_target_names(target):
                    if name not in sys_path_names:
                        sys_path_names.add(name)
                        changed = True
    return sys_names, sys_path_names, importlib_names, import_module_names


def _target_assigns_sys_path_attribute(
    node: ast.AST,
    *,
    sys_names: set[str],
) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "path":
        owner = node.value
        return isinstance(owner, ast.Name) and owner.id in sys_names
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(
            _target_assigns_sys_path_attribute(element, sys_names=sys_names)
            for element in node.elts
        )
    return False


def _target_mutates_sys_path_reference(
    node: ast.AST,
    *,
    sys_names: set[str],
    sys_path_names: set[str],
) -> bool:
    if _node_is_sys_path_reference(
        node,
        sys_names=sys_names,
        sys_path_names=sys_path_names,
    ):
        return True
    if isinstance(node, ast.Subscript):
        return _node_is_sys_path_reference(
            node.value,
            sys_names=sys_names,
            sys_path_names=sys_path_names,
        )
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(
            _target_mutates_sys_path_reference(
                element,
                sys_names=sys_names,
                sys_path_names=sys_path_names,
            )
            for element in node.elts
        )
    return False


def _assignment_touches_sys_path(
    node: ast.AST,
    *,
    sys_names: set[str],
    sys_path_names: set[str],
) -> bool:
    if isinstance(node, ast.Assign):
        return any(
            _target_assigns_sys_path_attribute(target, sys_names=sys_names)
            or (
                isinstance(target, ast.Subscript)
                and _target_mutates_sys_path_reference(
                    target,
                    sys_names=sys_names,
                    sys_path_names=sys_path_names,
                )
            )
            for target in node.targets
        )
    if isinstance(node, ast.AnnAssign):
        return _target_assigns_sys_path_attribute(node.target, sys_names=sys_names) or (
            isinstance(node.target, ast.Subscript)
            and _target_mutates_sys_path_reference(
                node.target,
                sys_names=sys_names,
                sys_path_names=sys_path_names,
            )
        )
    if isinstance(node, ast.AugAssign):
        return _target_mutates_sys_path_reference(
            node.target,
            sys_names=sys_names,
            sys_path_names=sys_path_names,
        )
    return False


def _call_touches_sys_path(
    node: ast.Call,
    *,
    sys_names: set[str],
    sys_path_names: set[str],
) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {"append", "extend", "insert"}:
        return _node_is_sys_path_reference(
            func.value,
            sys_names=sys_names,
            sys_path_names=sys_path_names,
        )
    return False


def _dynamic_private_core_import(
    node: ast.Call,
    *,
    importlib_names: set[str],
    import_module_names: set[str],
) -> str:
    if not node.args:
        return ""
    func = node.func
    module_name = ""
    if isinstance(func, ast.Name) and (func.id == "__import__" or func.id in import_module_names):
        module_name = _literal_string(node.args[0])
    elif isinstance(func, ast.Attribute) and func.attr == "import_module":
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in importlib_names:
            module_name = _literal_string(node.args[0])
    if module_name and _module_is_private_core(module_name):
        return module_name
    return ""


def _runtime_boundary_tree_issues(
    *,
    tree: ast.AST,
    file_path: str,
    file_is_grandfathered: bool,
) -> list[dict[str, str]]:
    if file_is_grandfathered:
        return []
    issues: list[dict[str, str]] = []
    sys_names, sys_path_names, importlib_names, import_module_names = _runtime_boundary_aliases(
        tree
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = str(alias.name or "")
                if _module_is_private_core(module_name):
                    issues.append(
                        _issue(
                            severity="error",
                            code="extension_private_core_import",
                            message=(
                                "Extension runtime code must not import private core "
                                f"module '{module_name}'"
                            ),
                            file_path=file_path,
                            hint=(
                                "Use flowsteward_extension_sdk or stdin runtime payload "
                                "contracts instead of core.* imports."
                            ),
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module_name = str(node.module or "")
            if _module_is_private_core(module_name):
                issues.append(
                    _issue(
                        severity="error",
                        code="extension_private_core_import",
                        message=(
                            "Extension runtime code must not import private core "
                            f"module '{module_name}'"
                        ),
                        file_path=file_path,
                        hint=(
                            "Use flowsteward_extension_sdk or stdin runtime payload "
                            "contracts instead of core.* imports."
                        ),
                    )
                )
        elif (
            isinstance(node, ast.Call)
            and _call_touches_sys_path(
                node,
                sys_names=sys_names,
                sys_path_names=sys_path_names,
            )
        ) or _assignment_touches_sys_path(
            node,
            sys_names=sys_names,
            sys_path_names=sys_path_names,
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="extension_sys_path_manipulation",
                    message="Extension runtime code must not mutate sys.path",
                    file_path=file_path,
                    hint=(
                        "Third-party libs belong in python_requirements; "
                        "platform primitives belong in flowsteward_extension_sdk."
                    ),
                )
            )
        elif isinstance(node, ast.Call):
            module_name = _dynamic_private_core_import(
                node,
                importlib_names=importlib_names,
                import_module_names=import_module_names,
            )
            if module_name:
                issues.append(
                    _issue(
                        severity="error",
                        code="extension_private_core_import",
                        message=(
                            "Extension runtime code must not dynamically import private "
                            f"core module '{module_name}'"
                        ),
                        file_path=file_path,
                        hint=(
                            "Use flowsteward_extension_sdk or stdin runtime payload "
                            "contracts instead of dynamic core.* imports."
                        ),
                    )
                )
    return issues


def validate_extension_runtime_boundary(
    *,
    bundle_root: str | Path,
    extension_id: str,
    entrypoint: dict[str, Any] | None = None,
    healthcheck: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    root = Path(bundle_root).resolve()
    issues = _manifest_command_issues(
        root,
        entrypoint=entrypoint,
        healthcheck=healthcheck,
    )
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            issues.append(
                _issue(
                    severity="error",
                    code="extension_embedded_database",
                    message="Extension bundles must not contain embedded database files",
                    file_path=str(path),
                    field_path="extension_storage_boundary",
                    hint="Use project-scoped host connections and public SDK storage contracts.",
                )
            )
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [str(alias.name or "") for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported_modules = [str(node.module or "")]
            if any(
                module == "sqlite3" or module.startswith("sqlite3.") for module in imported_modules
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="extension_private_sqlite_persistence",
                        message="Extension bundles must not import sqlite3",
                        file_path=str(path),
                        field_path="extension_storage_boundary",
                        hint="Use project-scoped host connections and public SDK storage contracts.",
                    )
                )
                break
    # SQLite may be an external datasource exposed by an extension-owned connector
    # catalog. The storage boundary is enforced by the embedded-file and sqlite3
    # runtime-import checks above; scanning arbitrary manifest text would reject
    # legitimate customer-managed SQLite connections as well.
    manifest_python_files = _manifest_command_python_files(
        root,
        entrypoint=entrypoint,
        healthcheck=healthcheck,
    )
    for path in _runtime_python_files(root, always_include=manifest_python_files):
        relative_path = _relative_posix(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        except OSError:
            continue
        issues.extend(
            _runtime_boundary_tree_issues(
                tree=tree,
                file_path=str(path),
                file_is_grandfathered=_is_grandfathered(extension_id, relative_path),
            )
        )
    return issues


__all__ = [
    "GRANDFATHERED_EXTENSION_RUNTIME_BOUNDARY_VIOLATIONS",
    "validate_extension_runtime_boundary",
    "validate_extension_runtime_command",
]
