"""Activate prepared platform modules from the shared runtime directory.

A platform module (ADR 0082) reaches a running deployment as a prepared tree on the
shared runtime directory instead of being baked into the image. The operator CLI
writes the tree and its list; every runtime role only reads them. This module
verifies the prepared trees against the list, appends their import roots to
``sys.path`` and, for background roles, calls the role registration hooks.

Layout under the root::

    modules.json
    <module path>/src/...    first-party import root of the module
    <module path>/site/...   vendored third-party dependencies of the module
    <module path>/db/...     module SQL migrations, read by the host migrator only

The list is a file, not an environment variable, because Compact child processes
receive an allowlisted environment and would never see an env-driven list.

Platform modules are trusted in-process code. Publisher signing is planned and not
yet verified by Core; this loader only compares each file with the digest in the
list. The checks here are defence in depth against packaging mistakes and against one
module shadowing another's import names; they are not a sandbox. Before any path is
appended, every top-level importable name of every module is accounted for: ``src/``
holds only the module's own import package, every name in ``site/`` belongs to a
``*.dist-info`` in that ``site/``, a module's own package is unique and not
importable from Core's paths, and a vendored distribution is shared with another
module or with the image only at exactly the same version (and, between modules,
the same file digests). After import, the module's own package must come from its
own ``src/``.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import json
import logging
import os
import platform
import re
import sys
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.package_identity import CANONICAL_EXTENSION_ID_RE

DEFAULT_PLATFORM_MODULES_ROOT = Path("/run/flow-steward-runtime/platform-modules")
PLATFORM_MODULES_ROOT_ENV = "FS_PLATFORM_MODULES_ROOT"
MODULE_LIST_FILENAME = "modules.json"
MODULE_LIST_SCHEMA_VERSION = 1

PLATFORM_MODULE_ROLES = frozenset({"app", "worker", "control"})
ROLE_ENTRYPOINTS: Mapping[str, str] = MappingProxyType(
    {"worker": "register_worker", "control": "register_control"}
)

_IMPORT_TREES = ("src", "site")
# Every tree whose files are pinned by ``files_sha256``. ``db/`` is verified but never
# importable: the host migrator executes its SQL.
_VERIFIED_TREES = (*_IMPORT_TREES, "db")
_REQUIRED_ENTRY_KEYS = frozenset(
    {"id", "version", "import", "path", "python_cache_tag", "machine", "files_sha256"}
)
_IMPORT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HASH_CHUNK_BYTES = 1024 * 1024

_logger = logging.getLogger(__name__)


class PlatformModuleError(RuntimeError):
    """A prepared platform module cannot be activated; the process must not start."""


class PlatformModuleListError(PlatformModuleError):
    """The module list is malformed."""


class PlatformModuleIntegrityError(PlatformModuleError):
    """A prepared tree does not match the digests in the module list."""


class PlatformModuleCompatibilityError(PlatformModuleError):
    """A prepared tree was built for another interpreter or machine."""


class PlatformModuleConflictError(PlatformModuleError):
    """A prepared tree would shadow or contradict code that is already importable."""


class PlatformModuleImportError(PlatformModuleError):
    """A verified platform module cannot be imported or registered."""


@dataclass(frozen=True, slots=True)
class PreparedPlatformModule:
    """One verified and activated platform module."""

    module_id: str
    version: str
    import_name: str
    path: Path

    def identity(self) -> Mapping[str, str]:
        """The read-only identity Core hands the module's entrypoints."""
        return MappingProxyType({"id": self.module_id, "version": self.version})


@dataclass(frozen=True, slots=True)
class _ModuleEntry:
    module_id: str
    version: str
    import_name: str
    relative_path: str
    python_cache_tag: str
    machine: str
    files_sha256: Mapping[str, str]


def platform_modules_root() -> Path:
    """Return the configured root of prepared platform modules."""
    configured = str(_runtime_env.get(PLATFORM_MODULES_ROOT_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_PLATFORM_MODULES_ROOT


def activate_prepared_platform_modules(
    role: str,
    *,
    root: Path | None = None,
    sys_path: MutableSequence[str] | None = None,
    cache_tag: str | None = None,
    machine: str | None = None,
    installed_version: Callable[[str], str | None] | None = None,
    image_checks: bool = True,
) -> tuple[PreparedPlatformModule, ...]:
    """Verify the prepared platform modules and append their import roots.

    Returns an empty tuple when no module list exists (plain Core). Every other
    problem raises a ``PlatformModuleError`` before ``sys.path`` is changed, so
    a process never runs with a partially activated module set.

    ``image_checks=False`` keeps every check that depends only on the artifacts
    and the list (schema, ids, path containment, file digests, interpreter tag and
    machine against the values passed in, ``src/`` layout, ``site/`` accounting,
    cross-module names, standard library and built-in names) and skips the checks
    that depend on what this interpreter can import or has installed: names
    importable from ``sys_path`` and installed distribution versions. Activation
    in the management image uses it, because that image's packages differ from the
    role images; image conflicts are caught when the database step runs in the
    application image and when each role starts with the default.
    """
    if role not in PLATFORM_MODULE_ROLES:
        raise PlatformModuleError(
            f"unknown platform module role '{role}'; expected one of "
            f"{', '.join(sorted(PLATFORM_MODULE_ROLES))}"
        )
    modules_root = platform_modules_root() if root is None else Path(root)
    list_path = modules_root / MODULE_LIST_FILENAME
    if not list_path.exists() and not list_path.is_symlink():
        return ()

    target_path = sys.path if sys_path is None else sys_path
    expected_tag = sys.implementation.cache_tag if cache_tag is None else cache_tag
    expected_machine = platform.machine() if machine is None else machine
    lookup_version: Callable[[str], str | None] | None = None
    if image_checks:
        lookup_version = installed_version or _path_version_lookup(target_path)

    entries = _read_module_list(modules_root, list_path)
    resolved_root = modules_root.resolve(strict=True)
    staged_distributions: dict[str, tuple[str, str]] = {}
    verified: list[tuple[_ModuleEntry, Path]] = []
    for entry in entries:
        module_dir = _resolve_module_dir(resolved_root, entry)
        _check_compatibility(entry, expected_tag=expected_tag, expected_machine=expected_machine)
        _verify_tree(module_dir, entry)
        _check_distributions(
            module_dir,
            entry,
            staged=staged_distributions,
            installed_version=lookup_version,
        )
        verified.append((entry, module_dir))
    # Every module's names are checked against Core's paths and against each other
    # before the first module path is appended, so the result cannot depend on the
    # order of the list.
    _check_top_level_names(verified, target_path, lookup_version)
    prepared = [
        PreparedPlatformModule(
            module_id=entry.module_id,
            version=entry.version,
            import_name=entry.import_name,
            path=module_dir,
        )
        for entry, module_dir in verified
    ]

    for module in prepared:
        for tree in _IMPORT_TREES:
            entry_path = str(module.path / tree)
            if entry_path not in target_path:
                target_path.append(entry_path)
    if prepared:
        if target_path is sys.path:
            # A role must never write into a verified module tree: a ``__pycache__``
            # written by the first import would be an unlisted file and the next start
            # would refuse the module. Compact's data volume is writable by its roles.
            sys.dont_write_bytecode = True
        importlib.invalidate_caches()
    return tuple(prepared)


def register_platform_modules_for_role(
    role: str,
    *,
    config: Mapping[str, Any],
    root: Path | None = None,
    logger: logging.Logger | None = None,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> tuple[PreparedPlatformModule, ...]:
    """Activate prepared modules for a background role and call its hooks.

    A module without the role hook is valid and skipped. An import failure or a
    hook that is present but not callable stops the process. Each hook receives a
    read-only context with ``role``, ``config`` and ``platform_module``, the
    ``{"id", "version"}`` identity under which Core loaded that module.
    """
    entrypoint = ROLE_ENTRYPOINTS.get(role)
    if entrypoint is None:
        raise PlatformModuleError(f"role '{role}' has no platform module registration hook")
    log = logger or _logger
    modules = activate_prepared_platform_modules(role, root=root)
    for module in modules:
        imported = import_prepared_platform_module(module, import_module=import_module)
        hook = getattr(imported, entrypoint, None)
        if hook is None:
            log.debug(
                "Platform module '%s' has no '%s' entrypoint; skipped",
                module.module_id,
                entrypoint,
            )
            continue
        if not callable(hook):
            raise PlatformModuleImportError(
                f"platform module '{module.module_id}' entrypoint '{entrypoint}' is not callable"
            )
        context: Mapping[str, Any] = MappingProxyType(
            {"role": role, "config": config, "platform_module": module.identity()}
        )
        hook(context)
    if modules:
        log.info(
            "Platform modules active for role %s: %s",
            role,
            describe_platform_modules(modules),
        )
    return modules


def import_prepared_platform_module(
    module: PreparedPlatformModule,
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> Any:
    """Import a prepared module; any failure is fatal and names the module."""
    try:
        imported = import_module(module.import_name)
    except Exception as exc:
        raise PlatformModuleImportError(
            f"platform module '{module.module_id}' {module.version} failed to import "
            f"'{module.import_name}': {type(exc).__name__}: {exc}"
        ) from exc
    verify_imported_module_origin(module, imported)
    return imported


def verify_imported_module_origin(module: PreparedPlatformModule, imported: Any) -> None:
    """Refuse an imported object that does not come from the module's own ``src/``.

    The name checks at activation keep import names unique; this catches what they
    cannot see, such as a name already in ``sys.modules``. The import package must
    be a regular package or a single-file module: a namespace package has no
    ``__file__`` and is refused. For a dotted import name the top-level package is
    checked as well when it is in ``sys.modules``.
    """
    own_src = (module.path / "src").resolve()
    candidates = [(module.import_name, imported)]
    top_level = module.import_name.split(".", 1)[0]
    if top_level != module.import_name and top_level in sys.modules:
        candidates.append((top_level, sys.modules[top_level]))
    for name, candidate in candidates:
        origin = getattr(candidate, "__file__", None)
        if not isinstance(origin, str) or not origin:
            raise PlatformModuleImportError(
                f"platform module '{module.module_id}' import '{name}' has no __file__; "
                "a platform module import package must be a regular package or module"
            )
        if not Path(origin).resolve().is_relative_to(own_src):
            raise PlatformModuleImportError(
                f"platform module '{module.module_id}' import '{name}' was loaded from "
                f"{origin}, outside the module's own {own_src}"
            )


def describe_platform_modules(modules: tuple[PreparedPlatformModule, ...]) -> str:
    """Return a compact ``id@version`` list for log lines."""
    return ", ".join(f"{module.module_id}@{module.version}" for module in modules) or "none"


def _read_module_list(root: Path, list_path: Path) -> tuple[_ModuleEntry, ...]:
    if list_path.is_symlink() or not list_path.is_file():
        raise PlatformModuleListError(f"{list_path} must be a regular file")
    try:
        payload = json.loads(list_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformModuleListError(f"{list_path} cannot be read as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlatformModuleListError(f"{list_path} must contain a JSON object")
    if payload.get("schema_version") != MODULE_LIST_SCHEMA_VERSION:
        raise PlatformModuleListError(
            f"{list_path} has schema_version {payload.get('schema_version')!r}; "
            f"expected {MODULE_LIST_SCHEMA_VERSION}"
        )
    raw_modules = payload.get("modules")
    if not isinstance(raw_modules, list):
        raise PlatformModuleListError(f"{list_path} must contain a 'modules' list")

    entries: list[_ModuleEntry] = []
    seen_ids: set[str] = set()
    seen_imports: set[str] = set()
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_modules):
        entry = _parse_entry(raw, where=f"{list_path} modules[{index}]")
        top_level = entry.import_name.split(".", 1)[0]
        if entry.module_id in seen_ids:
            raise PlatformModuleListError(f"{list_path} lists module '{entry.module_id}' twice")
        if top_level in seen_imports:
            raise PlatformModuleListError(
                f"{list_path} lists import package '{top_level}' for more than one module"
            )
        if entry.relative_path in seen_paths:
            raise PlatformModuleListError(
                f"{list_path} uses path '{entry.relative_path}' for more than one module"
            )
        seen_ids.add(entry.module_id)
        seen_imports.add(top_level)
        seen_paths.add(entry.relative_path)
        entries.append(entry)
    return tuple(entries)


def _parse_entry(raw: object, *, where: str) -> _ModuleEntry:
    if not isinstance(raw, dict):
        raise PlatformModuleListError(f"{where} must be an object")
    missing = sorted(_REQUIRED_ENTRY_KEYS - set(raw))
    unknown = sorted(set(raw) - _REQUIRED_ENTRY_KEYS)
    if missing or unknown:
        raise PlatformModuleListError(
            f"{where} has missing keys {missing} and unknown keys {unknown}"
        )
    text_fields = ("id", "version", "import", "path", "python_cache_tag", "machine")
    for key in text_fields:
        value = raw[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise PlatformModuleListError(f"{where} field '{key}' must be a non-empty string")
    # Platform modules share the ADR 0013 package identity grammar with extensions.
    if not CANONICAL_EXTENSION_ID_RE.fullmatch(raw["id"]):
        raise PlatformModuleListError(
            f"{where} id '{raw['id']}' is not a canonical lowercase dot-separated "
            "package id (ADR 0013)"
        )
    if not _IMPORT_NAME_PATTERN.match(raw["import"]):
        raise PlatformModuleListError(
            f"{where} import '{raw['import']}' is not a dotted Python module name"
        )
    relative_path = _safe_relative_path(raw["path"], where=f"{where} path")
    files = raw["files_sha256"]
    if not isinstance(files, dict) or not files:
        raise PlatformModuleListError(f"{where} field 'files_sha256' must be a non-empty object")
    digests: dict[str, str] = {}
    for file_path, digest in files.items():
        normalized = _safe_relative_path(file_path, where=f"{where} file '{file_path}'")
        if PurePosixPath(normalized).parts[0] not in _VERIFIED_TREES:
            raise PlatformModuleListError(
                f"{where} file '{file_path}' must live under {', '.join(_VERIFIED_TREES)}/"
            )
        if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
            raise PlatformModuleListError(
                f"{where} file '{file_path}' must have a lowercase hex sha256 digest"
            )
        if normalized in digests:
            raise PlatformModuleListError(f"{where} lists file '{normalized}' twice")
        digests[normalized] = digest
    return _ModuleEntry(
        module_id=raw["id"],
        version=raw["version"],
        import_name=raw["import"],
        relative_path=relative_path,
        python_cache_tag=raw["python_cache_tag"],
        machine=raw["machine"],
        files_sha256=MappingProxyType(digests),
    )


def _safe_relative_path(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlatformModuleListError(f"{where} must be a non-empty relative path")
    if "\\" in value or "\x00" in value:
        raise PlatformModuleListError(f"{where} must use forward slashes only")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PlatformModuleListError(f"{where} must be a normalized relative path inside the root")
    return candidate.as_posix()


def _resolve_module_dir(resolved_root: Path, entry: _ModuleEntry) -> Path:
    candidate = resolved_root / entry.relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' does not exist"
        ) from exc
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' "
            f"escapes the platform module root {resolved_root}"
        )
    if not resolved.is_dir():
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' is not a directory"
        )
    return resolved


def _check_compatibility(entry: _ModuleEntry, *, expected_tag: str, expected_machine: str) -> None:
    if entry.python_cache_tag != expected_tag:
        raise PlatformModuleCompatibilityError(
            f"platform module '{entry.module_id}' {entry.version} was prepared for "
            f"{entry.python_cache_tag}; this interpreter is {expected_tag}"
        )
    if entry.machine != expected_machine:
        raise PlatformModuleCompatibilityError(
            f"platform module '{entry.module_id}' {entry.version} was prepared for machine "
            f"{entry.machine}; this machine is {expected_machine}"
        )


def _verify_tree(module_dir: Path, entry: _ModuleEntry) -> None:
    present: set[str] = set()
    for tree in _VERIFIED_TREES:
        tree_root = module_dir / tree
        if tree_root.is_symlink():
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' {tree}/ must not be a symlink"
            )
        if not tree_root.exists():
            continue
        if not tree_root.is_dir():
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' {tree}/ is not a directory"
            )
        present.update(_walk_regular_files(module_dir, tree_root, entry))

    listed = set(entry.files_sha256)
    unlisted = sorted(present - listed)
    if unlisted:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' contains files not in the module list: "
            f"{', '.join(unlisted[:5])}"
        )
    missing = sorted(listed - present)
    if missing:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' is missing listed files: {', '.join(missing[:5])}"
        )
    for relative, expected in sorted(entry.files_sha256.items()):
        actual = _sha256_file(module_dir / relative)
        if actual != expected:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' file '{relative}' has sha256 {actual}; "
                f"the module list expects {expected}"
            )


def _walk_regular_files(module_dir: Path, tree_root: Path, entry: _ModuleEntry) -> set[str]:
    files: set[str] = set()
    for current, dirnames, filenames in os.walk(tree_root, followlinks=False):
        current_path = Path(current)
        for name in (*dirnames, *filenames):
            path = current_path / name
            relative = path.relative_to(module_dir).as_posix()
            if path.is_symlink():
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a symlink: {relative}"
                )
            if name.endswith(".pth"):
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a .pth file: {relative}"
                )
        for name in filenames:
            path = current_path / name
            if not path.is_file():
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a non-regular file: "
                    f"{path.relative_to(module_dir).as_posix()}"
                )
            files.add(path.relative_to(module_dir).as_posix())
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _path_version_lookup(search_path: MutableSequence[str]) -> Callable[[str], str | None]:
    def lookup(name: str) -> str | None:
        for distribution in importlib.metadata.distributions(name=name, path=list(search_path)):
            return distribution.version
        return None

    return lookup


def _check_distributions(
    module_dir: Path,
    entry: _ModuleEntry,
    *,
    staged: dict[str, tuple[str, str]],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    """Check vendored metadata; ``installed_version=None`` skips the image comparison."""
    site_root = module_dir / "site"
    if not site_root.is_dir():
        return
    for dist_info in sorted(site_root.glob("*.dist-info")):
        metadata = importlib.metadata.PathDistribution(dist_info).metadata
        name = metadata.get("Name") if metadata else None
        version = metadata.get("Version") if metadata else None
        if not name or not version:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' has unreadable metadata in "
                f"site/{dist_info.name}"
            )
        normalized = _normalize_distribution_name(name)
        staged_version = staged.get(normalized)
        if staged_version is not None and staged_version[0] != version:
            raise PlatformModuleConflictError(
                f"platform module '{entry.module_id}' ships {name} {version}, but platform "
                f"module '{staged_version[1]}' ships {name} {staged_version[0]}"
            )
        present = installed_version(name) if installed_version is not None else None
        if present is not None and present != version:
            raise PlatformModuleConflictError(
                f"platform module '{entry.module_id}' ships {name} {version}, but {name} "
                f"{present} is already importable"
            )
        staged.setdefault(normalized, (version, entry.module_id))


_EXTENSION_MODULE_SUFFIXES = (".so", ".pyd")
_SOURCE_MODULE_SUFFIXES = (".py", ".pyc", ".pyw")
_METADATA_DIR_SUFFIXES = (".dist-info", ".egg-info", ".data")
_CACHE_DIR = "__pycache__"


def _importable_file_name(filename: str) -> str | None:
    """The top-level import name a file at the root of an import tree provides."""
    if filename.endswith(_EXTENSION_MODULE_SUFFIXES):
        # ``name.cpython-312-x86_64-linux-gnu.so``, ``name.abi3.so``, ``name.so``.
        stem = filename.split(".", 1)[0]
    else:
        suffix = next((s for s in _SOURCE_MODULE_SUFFIXES if filename.endswith(s)), None)
        if suffix is None:
            return None
        stem = filename[: -len(suffix)]
    return stem if stem.isidentifier() else None


def _importable_dir_name(dirname: str) -> str | None:
    if dirname == _CACHE_DIR or dirname.endswith(_METADATA_DIR_SUFFIXES):
        return None
    return dirname if dirname.isidentifier() else None


def _tree_entries(tree_root: Path) -> list[tuple[str, str | None]]:
    """``(entry name, top-level import name or None)`` for the root of one import tree."""
    if not tree_root.is_dir():
        return []
    entries: list[tuple[str, str | None]] = []
    for child in sorted(tree_root.iterdir()):
        if child.is_dir():
            entries.append((child.name, _importable_dir_name(child.name)))
        else:
            entries.append((child.name, _importable_file_name(child.name)))
    return entries


def _distribution_top_level_names(dist_info: Path, entry: _ModuleEntry) -> set[str]:
    """The top-level names a vendored distribution declares.

    ``top_level.txt`` when present, else the first path component of every
    ``RECORD`` row. A distribution with neither cannot be accounted for.
    """
    top_level = dist_info / "top_level.txt"
    if top_level.is_file():
        names: set[str] = set()
        for line in top_level.read_text(encoding="utf-8").splitlines():
            raw = line.strip().replace("\\", "/")
            if not raw:
                continue
            name = raw.split("/", 1)[0].split(".", 1)[0]
            if name.isidentifier():
                names.add(name)
        return names
    record = dist_info / "RECORD"
    if record.is_file():
        names = set()
        rows = csv.reader(record.read_text(encoding="utf-8").splitlines())
        for row in rows:
            if not row or not row[0]:
                continue
            parts = PurePosixPath(row[0].replace("\\", "/")).parts
            if not parts or parts[0] in {".", ".."} or parts[0].endswith(_METADATA_DIR_SUFFIXES):
                continue
            name = (
                _importable_dir_name(parts[0])
                if len(parts) > 1
                else _importable_file_name(parts[0])
            )
            if name:
                names.add(name)
        return names
    raise PlatformModuleIntegrityError(
        f"platform module '{entry.module_id}' vendors site/{dist_info.name} without "
        "top_level.txt or RECORD, so its import names cannot be accounted for"
    )


@dataclass(frozen=True, slots=True)
class _BundledDistribution:
    """A third-party distribution a module vendors in its ``site/``."""

    name: str
    version: str
    #: ``site/``-relative path -> listed sha256 of every file the distribution
    #: owns: its ``*.dist-info`` and the top-level names it declares.
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _ModuleNames:
    entry: _ModuleEntry
    module_dir: Path
    own_name: str
    #: Top-level name in ``site/`` -> the vendored distributions declaring it.
    site: Mapping[str, tuple[_BundledDistribution, ...]]


def _distribution_files(entry: _ModuleEntry, dist_info: Path, names: set[str]) -> Mapping[str, str]:
    files: dict[str, str] = {}
    for relative, digest in entry.files_sha256.items():
        parts = PurePosixPath(relative).parts
        if len(parts) < 2 or parts[0] != "site":
            continue
        top = parts[1]
        provided = _importable_dir_name(top) if len(parts) > 2 else _importable_file_name(top)
        if top == dist_info.name or (provided is not None and provided in names):
            files["/".join(parts[1:])] = digest
    return MappingProxyType(files)


def _module_top_level_names(module_dir: Path, entry: _ModuleEntry) -> _ModuleNames:
    """Every top-level name the module's ``src/`` and ``site/`` make importable.

    Refuses anything in ``src/`` other than the declared import package and any
    name in ``site/`` that no ``*.dist-info`` in that ``site/`` declares.
    """
    own_name = entry.import_name.split(".", 1)[0]
    for entry_name, import_name in _tree_entries(module_dir / "src"):
        if import_name != own_name:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' src/ may contain only its import "
                f"package '{own_name}', but contains '{entry_name}'"
            )

    site_root = module_dir / "site"
    site_names = {name for _entry, name in _tree_entries(site_root) if name}
    declared: dict[str, list[_BundledDistribution]] = {}
    if site_root.is_dir():
        for dist_info in sorted(site_root.glob("*.dist-info")):
            names = _distribution_top_level_names(dist_info, entry)
            metadata = importlib.metadata.PathDistribution(dist_info).metadata
            distribution = _BundledDistribution(
                name=str(metadata.get("Name") or ""),
                version=str(metadata.get("Version") or ""),
                files=_distribution_files(entry, dist_info, names),
            )
            for name in names:
                declared.setdefault(name, []).append(distribution)
    unaccounted = sorted(site_names - set(declared))
    if unaccounted:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' site/ contains {', '.join(unaccounted)} "
            "that no vendored *.dist-info in that site/ declares"
        )
    if own_name in site_names:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' site/ ships its own import package '{own_name}'"
        )
    return _ModuleNames(
        entry=entry,
        module_dir=module_dir,
        own_name=own_name,
        site=MappingProxyType({name: tuple(declared[name]) for name in sorted(site_names)}),
    )


def _bundle_signature(distributions: Sequence[_BundledDistribution]) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                _normalize_distribution_name(dist.name),
                dist.version,
                tuple(sorted(dist.files.items())),
            )
            for dist in distributions
        )
    )


def _check_top_level_names(
    verified: Sequence[tuple[_ModuleEntry, Path]],
    search_path: MutableSequence[str],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    """Refuse a module set whose import names could resolve to the wrong code.

    ``installed_version=None`` (``image_checks=False``) skips the comparison with
    what ``search_path`` makes importable; every other rule still applies.

    A module's own import package is unique across modules and never importable
    from Core's paths. A vendored distribution may appear in several modules only
    when every copy has the same version and the same listed file digests; the
    first module's copy then wins by ``sys.path`` order, which is harmless because
    the copies are identical. It may also already be importable from Core's paths
    when the image has that distribution at exactly the same version (Scalable roles
    run images with different locks); Core's copy then wins because module paths
    are appended after Core's.
    """
    modules = [_module_top_level_names(module_dir, entry) for entry, module_dir in verified]
    owners: dict[str, tuple[str, tuple[Any, ...] | None]] = {}
    providers: dict[str, list[_ModuleNames]] = {}
    for module in modules:
        claims: list[tuple[str, tuple[Any, ...] | None]] = [(module.own_name, None)]
        claims += [(name, _bundle_signature(dists)) for name, dists in module.site.items()]
        for name, signature in claims:
            providers.setdefault(name, []).append(module)
            previous = owners.setdefault(name, (module.entry.module_id, signature))
            if previous[0] == module.entry.module_id:
                continue
            if signature is None or previous[1] is None:
                raise PlatformModuleConflictError(
                    f"platform modules '{previous[0]}' and '{module.entry.module_id}' both "
                    f"provide the top-level import name '{name}'"
                )
            if signature != previous[1]:
                raise PlatformModuleConflictError(
                    f"platform modules '{previous[0]}' and '{module.entry.module_id}' both "
                    f"vendor '{name}' but with different distribution versions or contents"
                )
    for module in modules:
        _check_name_is_free(module, module.own_name, search_path, providers, installed_version)
        for name in module.site:
            _check_name_is_free(module, name, search_path, providers, installed_version)


def _installed_top_level_names(
    distribution: str,
    search_path: Sequence[str],
    *,
    packages_distributions: Callable[[], Mapping[str, Sequence[str]]] = (
        importlib.metadata.packages_distributions
    ),
) -> set[str]:
    """The top-level names an installed distribution provides.

    ``top_level.txt`` first, then the first path component of its ``RECORD``
    files. OS-packaged distributions often carry neither; then the interpreter's
    ``packages_distributions()`` mapping is asked. An empty set means the name
    cannot be attributed to the distribution.
    """
    for installed in importlib.metadata.distributions(name=distribution, path=list(search_path)):
        top_level = installed.read_text("top_level.txt")
        if top_level:
            return {
                line.strip().split("/", 1)[0].split(".", 1)[0]
                for line in top_level.splitlines()
                if line.strip()
            }
        names: set[str] = set()
        for file in installed.files or ():
            parts = PurePosixPath(str(file)).parts
            if not parts or parts[0] in {".", ".."}:
                continue
            name = (
                _importable_dir_name(parts[0])
                if len(parts) > 1
                else _importable_file_name(parts[0])
            )
            if name:
                names.add(name)
        if names:
            return names
        break
    wanted = _normalize_distribution_name(distribution)
    try:
        mapping = packages_distributions()
    except Exception:
        return set()
    return {
        name
        for name, owners in mapping.items()
        if any(_normalize_distribution_name(owner) == wanted for owner in owners)
    }


def _check_name_is_free(
    module: _ModuleNames,
    name: str,
    search_path: MutableSequence[str],
    providers: Mapping[str, Sequence[_ModuleNames]],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    module_id = module.entry.module_id
    if name in sys.builtin_module_names or name in sys.stdlib_module_names:
        raise PlatformModuleConflictError(
            f"platform module '{module_id}' import name '{name}' is a standard library or "
            "built-in module"
        )
    if installed_version is None:
        return
    spec = importlib.machinery.PathFinder.find_spec(name, list(search_path))
    if spec is None:
        return
    locations = [
        location
        for location in (*(spec.submodule_search_locations or ()), spec.origin)
        if isinstance(location, str) and location
    ]
    # A second activation in the same process finds the trees of the modules that
    # provide this name (its own, or identical copies of a shared distribution).
    own_roots = [
        provider.module_dir / tree
        for provider in providers.get(name, (module,))
        for tree in (("src",) if name == provider.own_name else ("site",))
    ]
    if locations and all(
        any(Path(location).is_relative_to(root) for root in own_roots) for location in locations
    ):
        return
    where = locations[0] if locations else spec.name
    distributions = module.site.get(name) if name != module.own_name else None
    if not distributions:
        raise PlatformModuleConflictError(
            f"platform module '{module_id}' import name '{name}' is already importable from {where}"
        )
    # A vendored name may already be importable only from the very same
    # distribution at the very same version; Core's copy then wins on sys.path.
    for dist in distributions:
        present = installed_version(dist.name)
        if present is not None and present != dist.version:
            raise PlatformModuleConflictError(
                f"platform module '{module_id}' ships {dist.name} {dist.version}, but "
                f"{dist.name} {present} is already importable"
            )
        if present is None or name not in _installed_top_level_names(dist.name, search_path):
            raise PlatformModuleConflictError(
                f"platform module '{module_id}' import name '{name}' is already importable "
                f"from {where}, which is not {dist.name} {dist.version} installed in this image"
            )
