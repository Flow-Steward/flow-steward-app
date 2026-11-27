"""Import-name and vendored-distribution accounting for platform modules (ADR 0082).

Before any module path is appended to ``sys.path``, every top-level importable name
of every module is accounted for: ``src/`` holds only the module's own import
package, every name in ``site/`` belongs to a ``*.dist-info`` in that ``site/``, a
module's own package is unique and not importable from Core's paths, and a
vendored distribution is shared with another module or with the image only at
exactly the same version (and, between modules, the same file digests).
:mod:`core.infrastructure.platform_modules.loader` runs these checks.
"""

from __future__ import annotations

import csv
import importlib.machinery
import importlib.metadata
import re
import sys
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from core.infrastructure.platform_modules.errors import (
    PlatformModuleConflictError,
    PlatformModuleIntegrityError,
)

if TYPE_CHECKING:
    from core.infrastructure.platform_modules.loader import _ModuleEntry


def _metadata_value(metadata: importlib.metadata.PackageMetadata | None, key: str) -> str | None:
    """One header of a distribution's metadata, or ``None`` when it is missing."""
    if not metadata or key not in metadata:
        return None
    return metadata[key]


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
        name = _metadata_value(metadata, "Name")
        version = _metadata_value(metadata, "Version")
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
                name=str(_metadata_value(metadata, "Name") or ""),
                version=str(_metadata_value(metadata, "Version") or ""),
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
