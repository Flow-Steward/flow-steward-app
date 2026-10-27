from __future__ import annotations

import ast
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from core.infrastructure.config.env_registry import env_spec_map

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_FILES = (
    ".env.example",
    ".env.example.prod",
    ".env.example.ops",
    ".env.example.test",
    ".env.example.memory",
    ".env.example.quality",
)
PYTHON_ENV_READ_HELPERS = frozenset(
    {
        "_env_int",
        "_env_float",
        "_env_str",
        "_env_csv_set",
        "_env_flag",
        "_env_bool",
        "_read_int_env",
        "_read_float_env",
        "_bounded_int",
        "getenv",
    }
)
# Immutable release provenance contains historical interpolation examples, not reads.
NON_EXECUTABLE_SCAN_PATHS = frozenset({"infra/tooling/release/ledger.yaml"})
AUDIT_SCAN_GLOBS = (
    "core/**/*.py",
    "examples/**/*.py",
    "tests/**/*.py",
)
SHELL_SCAN_GLOBS = (
    "core/**/*.sh",
    "infra/**/*.sh",
)
YAML_SCAN_GLOBS = (
    "infra/**/*.yaml",
    "infra/**/*.yml",
)

GENERATED_SCAN_PARTS = frozenset(
    {".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist", "node_modules"}
)
SHELL_ENV_READ_PATTERNS = (
    re.compile(r"\$\{([A-Z][A-Z0-9_]+)(?::[-+?][^}]*)?\}"),
    re.compile(r"\$([A-Z][A-Z0-9_]+)\b"),
)
YAML_ENV_READ_PATTERNS = (re.compile(r"\$\{([A-Z][A-Z0-9_]+)(?::[-+?][^}]*)?\}"),)
# The one module whose literal storage-probe scripts are scanned as Python source.
EMITTED_PYTHON_SOURCE_PATH = "core/infrastructure/lifecycle/docker_runtime_support.py"
# Every reader _scan_python_env_calls recognises names one of these identifiers:
# a helper, os.getenv, os.environ / `from os import environ`, or the runtime_env
# adapter. Source that contains none of them cannot produce a read.
PYTHON_ENV_CALL_TOKENS = frozenset({"environ", "runtime_env", *PYTHON_ENV_READ_HELPERS})
# _scan_python_env_name_maps_in_tree only reads dicts assigned to a name containing "ENV".
PYTHON_ENV_NAME_MAP_TOKEN = "ENV"
# Results per (relative path, content digest); keyed by content, so never stale.
PYTHON_SCAN_CACHE_LIMIT = 16384
_PYTHON_SCAN_CACHE: dict[tuple[str, bytes], frozenset[str]] = {}


def audit_artifact_path(*, repo_root: Path | None = None) -> Path:
    root = (repo_root or ROOT).resolve()
    return root / "docs/configuration/flow-steward-env-audit.json"


def build_env_audit(*, repo_root: Path | None = None) -> list[dict[str, object]]:
    root = (repo_root or ROOT).resolve()
    manifest_specs = env_spec_map()
    documented = _documented_env_values(root)
    reads = _scan_env_reads(root)
    names = sorted(set(documented) | set(reads) | set(manifest_specs))
    rows: list[dict[str, object]] = []
    for name in names:
        spec = manifest_specs.get(name)
        doc_sources = documented.get(name, [])
        read_locations = reads.get(name, [])
        rows.append(
            {
                "variable_name": name,
                "canonical_name": name,
                "retired_alias_names": [],
                "documented_sources": [row["source"] for row in doc_sources],
                "code_read_locations": [row["location"] for row in read_locations],
                "owning_subsystem": spec.owner if spec else _owner_from_name(name),
                "required": spec.doctor_required if spec else False,
                "secret": spec.secret if spec else _looks_secret(name),
                "environments": list(spec.environments)
                if spec
                else _environments_from_docs(doc_sources),
                "conditional_groups": list(spec.groups) if spec else [],
                "default_example_value": doc_sources[0]["value"] if doc_sources else "",
                "notes": _audit_notes(
                    name=name,
                    spec_notes=str(spec.notes) if spec else "",
                    documented=doc_sources,
                    reads=read_locations,
                ),
            }
        )
    return rows


def write_env_audit_artifact(*, repo_root: Path | None = None) -> Path:
    root = (repo_root or ROOT).resolve()
    target = audit_artifact_path(repo_root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_env_audit(repo_root=root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _documented_env_values(root: Path) -> dict[str, list[dict[str, str]]]:
    values: dict[str, list[dict[str, str]]] = {}
    for relative in EXAMPLE_FILES:
        path = root / relative
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not key:
                continue
            values.setdefault(key, []).append(
                {
                    "source": f"{relative}:{line_no}",
                    "value": value.strip().strip('"').strip("'"),
                }
            )
    return values


def _scan_env_reads(root: Path) -> dict[str, list[dict[str, str]]]:
    matches: dict[str, list[dict[str, str]]] = {}
    python_paths = sorted(
        {
            path
            for pattern in AUDIT_SCAN_GLOBS
            for path in root.glob(pattern)
            if not GENERATED_SCAN_PARTS.intersection(path.relative_to(root).parts)
        },
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in python_paths:
        rel = path.relative_to(root).as_posix()
        for name in sorted(_python_file_env_names(rel, path.read_text(encoding="utf-8"))):
            matches.setdefault(name, []).append({"location": rel})
    shell_paths = sorted(
        {
            path
            for pattern in SHELL_SCAN_GLOBS
            for path in root.glob(pattern)
            if not GENERATED_SCAN_PARTS.intersection(path.relative_to(root).parts)
        },
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in shell_paths:
        rel = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            for pattern in SHELL_ENV_READ_PATTERNS:
                for match in pattern.finditer(line):
                    name = str(match.group(1))
                    matches.setdefault(name, []).append({"location": rel})
    yaml_paths = sorted(
        {
            path
            for pattern in YAML_SCAN_GLOBS
            for path in root.glob(pattern)
            if not GENERATED_SCAN_PARTS.intersection(path.relative_to(root).parts)
        },
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in yaml_paths:
        rel = path.relative_to(root).as_posix()
        if rel in NON_EXECUTABLE_SCAN_PATHS:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            for pattern in YAML_ENV_READ_PATTERNS:
                for match in pattern.finditer(line):
                    name = str(match.group(1))
                    matches.setdefault(name, []).append({"location": rel})
    # A single read can match more than one regex pattern (an os-qualified literal
    # read also matches the generic getenv pattern), which would record the same
    # location twice. Collapse duplicate locations per variable while preserving
    # first-seen order.
    deduped: dict[str, list[dict[str, str]]] = {}
    for name, locations in matches.items():
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for entry in locations:
            location = str(entry.get("location") or "")
            if location in seen:
                continue
            seen.add(location)
            unique.append(entry)
        deduped[name] = unique
    return deduped


def _python_file_env_names(relative: str, text: str) -> frozenset[str]:
    """Every name one Python file reads; every location in a file is the file itself."""
    key = (relative, hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest())
    cached = _PYTHON_SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    haystack = _identifier_haystack(text)
    may_read = any(token in haystack for token in PYTHON_ENV_CALL_TOKENS)
    may_map = PYTHON_ENV_NAME_MAP_TOKEN in haystack
    names: set[str] = set()
    tree = None
    if may_read or may_map or relative == EMITTED_PYTHON_SOURCE_PATH:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
    if tree is not None:
        if may_read:
            names.update(_scan_python_env_calls_in_tree(tree))
        for source in _known_emitted_python_sources_in_tree(relative, tree):
            names.update(_scan_python_env_calls(source))
        if may_map:
            names.update(name for name, _line_no in _scan_python_env_name_maps_in_tree(tree))
    result = frozenset(names)
    if len(_PYTHON_SCAN_CACHE) >= PYTHON_SCAN_CACHE_LIMIT:
        _PYTHON_SCAN_CACHE.clear()
    _PYTHON_SCAN_CACHE[key] = result
    return result


def _identifier_haystack(text: str) -> str:
    """Text in which every identifier the parser sees appears verbatim.

    The parser NFKC-normalises non-ASCII identifiers, so a token prefilter has to
    look at the normalised text; ASCII text is already its own normal form.
    """
    return text if text.isascii() else unicodedata.normalize("NFKC", text)


def _known_emitted_python_sources_in_tree(relative: str, tree: ast.Module) -> tuple[str, ...]:
    """Extract only the two verified literal storage-probe factories; never execute them."""
    if relative != EMITTED_PYTHON_SOURCE_PATH:
        return ()
    return tuple(
        node.body[0].value.value
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_bundled_client_prelude", "local_storage_write_probe_script"}
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    )


def _scope_node_lists(
    tree: ast.Module,
) -> list[tuple[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, list[ast.AST]]]:
    """Inspect each lexical scope without borrowing a nested function's aliases.

    Returns the module first, then every function ``ast.walk`` would find, each
    with the nodes of its own body (popped depth-first, skipping nested
    functions, classes and lambdas). One pass: a nested function met while
    collecting a scope is queued as a scope instead of found by another walk.
    """
    scopes: list[tuple[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, list[ast.AST]]] = []
    pending_scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [tree]
    while pending_scopes:
        scope = pending_scopes.pop()
        nodes: list[ast.AST] = []
        pending: list[ast.AST] = list(scope.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pending_scopes.append(node)
                continue
            if isinstance(node, (ast.ClassDef, ast.Lambda)):
                pending_scopes.extend(_nested_function_scopes(node))
                continue
            nodes.append(node)
            pending.extend(ast.iter_child_nodes(node))
        scopes.append((scope, nodes))
    return scopes


def _nested_function_scopes(
    node: ast.ClassDef | ast.Lambda,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Functions inside a class or lambda; their own nested functions come later."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    pending: list[ast.AST] = list(ast.iter_child_nodes(node))
    while pending:
        child = pending.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(child)
            continue
        pending.extend(ast.iter_child_nodes(child))
    return found


def _environment_copy(node: ast.AST, environment_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in environment_names
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "dict" and len(node.args) == 1:
            return isinstance(node.args[0], ast.Name) and node.args[0].id in environment_names
        if isinstance(node.func, ast.Attribute) and node.func.attr == "copy" and not node.args:
            return isinstance(node.func.value, ast.Name) and node.func.value.id in environment_names
    # The artifact adapter accepts an explicit environment or a runtime-env copy.
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
        override, fallback = node.values
        return (
            isinstance(override, ast.Name)
            and override.id == "environ"
            and _environment_copy(fallback, environment_names)
        )
    return False


def _environment_import_names(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module in {
            "core.infrastructure.runtime_env",
            "core.infrastructure.config.runtime_env",
        }:
            imported_name = "runtime_env"
        elif node.module == "os":
            imported_name = "environ"
        else:
            continue
        names.update(
            alias.asname or alias.name for alias in node.names if alias.name == imported_name
        )
    return names


def _os_reader_imports(nodes: list[ast.AST]) -> set[str]:
    readers: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import) and any(
            alias.name == "os" and alias.asname in {None, "os"} for alias in node.names
        ):
            readers.add("os")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "os"
            and any(
                alias.name == "getenv" and alias.asname in {None, "getenv"} for alias in node.names
            )
        ):
            readers.add("getenv")
    return readers


def _scope_binding_counts(nodes: list[ast.AST]) -> dict[str, int]:
    """Reject ambiguous bindings rather than infer control flow or runtime values."""
    counts: dict[str, int] = {}
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names = [node.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        else:
            continue
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _scan_python_env_calls(text: str) -> set[str]:
    """Discover explicit current readers; resolve only unambiguous module strings."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    if not any(token in _identifier_haystack(text) for token in PYTHON_ENV_CALL_TOKENS):
        return set()
    return _scan_python_env_calls_in_tree(tree)


def _scan_python_env_calls_in_tree(tree: ast.Module) -> set[str]:
    scope_nodes = _scope_node_lists(tree)
    module_nodes = scope_nodes[0][1]
    module_bindings = _scope_binding_counts(module_nodes)
    environment_names = {
        name for name in _environment_import_names(module_nodes) if module_bindings[name] == 1
    }
    module_os_readers = {
        name
        for name in {"os", "getenv"}
        if name not in module_bindings
        or (name in _os_reader_imports(module_nodes) and module_bindings[name] == 1)
    }
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants.update(
                {
                    target.id: node.value.value
                    for target in targets
                    if isinstance(target, ast.Name) and module_bindings[target.id] == 1
                }
            )
    found: set[str] = set()
    for scope, nodes in scope_nodes:
        bindings = _scope_binding_counts(nodes)
        parameters = (
            set()
            if isinstance(scope, ast.Module)
            else {node.arg for node in ast.walk(scope.args) if isinstance(node, ast.arg)}
        )
        shadowed = set(bindings) | parameters if scope is not tree else set()
        os_readers = module_os_readers - shadowed
        os_readers.update(
            name
            for name in _os_reader_imports(nodes)
            if bindings[name] == 1 and name not in parameters
        )
        helpers = (PYTHON_ENV_READ_HELPERS - {"getenv"} - shadowed) | (os_readers & {"getenv"})
        readers = environment_names - shadowed
        readers.update(
            name
            for name in _environment_import_names(nodes)
            if bindings[name] == 1 and name not in parameters
        )
        for node in nodes:
            if isinstance(node, ast.Assign) and _environment_copy(node.value, readers):
                readers.update(
                    target.id
                    for target in node.targets
                    if isinstance(target, ast.Name)
                    and bindings[target.id] == 1
                    and target.id not in parameters
                )
        for node in nodes:
            argument: ast.AST | None = None
            if isinstance(node, ast.Call) and node.args:
                function = node.func
                if isinstance(function, ast.Name) and function.id in helpers:
                    argument = node.args[0]
                elif isinstance(function, ast.Attribute):
                    owner = ast.unparse(function.value)
                    if (owner == "os" and "os" in os_readers and function.attr == "getenv") or (
                        ((owner == "os.environ" and "os" in os_readers) or owner in readers)
                        and function.attr in {"get", "getenv"}
                    ):
                        argument = node.args[0]
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                owner = ast.unparse(node.value)
                if owner in readers or (owner == "os.environ" and "os" in os_readers):
                    argument = node.slice
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                name = argument.value
            elif isinstance(argument, ast.Name) and argument.id not in shadowed:
                name = constants.get(argument.id, "")
            else:
                continue
            if re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
                found.add(name)
    return found


def _scan_python_env_name_maps_in_tree(tree: ast.Module) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if not any("ENV" in target for target in targets):
            continue
        if node.value is not None:
            found.extend(_env_keys_from_mapping(node.value, line_no=getattr(node, "lineno", 1)))
    return found


def _env_keys_from_mapping(node: ast.AST, *, line_no: int) -> list[tuple[str, int]]:
    if not isinstance(node, ast.Dict):
        return []
    found: list[tuple[str, int]] = []
    for key, value in zip(node.keys, node.values, strict=False):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            candidate = str(key.value)
            if re.fullmatch(r"[A-Z][A-Z0-9_]+", candidate):
                found.append((candidate, getattr(key, "lineno", line_no)))
        found.extend(_env_keys_from_mapping(value, line_no=getattr(value, "lineno", line_no)))
    return found


def _owner_from_name(name: str) -> str:
    if name.startswith("SONAR_"):
        return "quality"
    if name.startswith(("E2E_", "TEST_")):
        return "testing"
    if name.startswith("ARTIFACT_"):
        return "artifacts"
    if "AUTH" in name or name.endswith("_SECRET_KEY"):
        return "auth"
    if "REDACT" in name or "ATTACHMENT" in name:
        return "security"
    if name.endswith("_API_KEY") or "PROVIDER" in name or "MODEL" in name:
        return "providers"
    return "runtime"


def _looks_secret(name: str) -> bool:
    return any(token in name for token in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "DSN", "AUTH"))


def _environments_from_docs(documented: list[dict[str, str]]) -> list[str]:
    if not documented:
        return ["production", "local_development"]
    sources = {row["source"].split(":", 1)[0] for row in documented}
    environments: list[str] = []
    if ".env.example.prod" in sources:
        environments.append("production")
    if any(
        source in sources
        for source in {".env.example", ".env.example.test", ".env.example.quality"}
    ):
        environments.append("local_development")
    if not environments:
        environments = ["production", "local_development"]
    return environments


def _audit_notes(
    *,
    name: str,
    spec_notes: str,
    documented: list[dict[str, str]],
    reads: list[dict[str, str]],
) -> str:
    del name
    notes: list[str] = []
    if spec_notes:
        notes.append(spec_notes)
    if documented and not reads:
        notes.append("documented but no direct literal env read was found in the audited scan")
    if reads and not documented:
        notes.append("used but undocumented in .env.example surfaces")
    if not documented and not reads and spec_notes:
        notes.append("registry-managed metadata only")
    return "; ".join(dict.fromkeys(notes))
