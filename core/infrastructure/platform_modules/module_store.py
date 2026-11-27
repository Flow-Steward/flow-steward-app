"""Filesystem operations on the platform-module root (ADR 0082 §5).

The deployment manager places every prepared module under the platform-module root;
this store reads what is staged, reports the active list and switches which staged
modules the process roles load. Layout under the root::

    staged/<module id>/<version>/module.json   descriptor (see below)
    staged/<module id>/<version>/src/...        first-party import root
    staged/<module id>/<version>/site/...       vendored third-party dependencies
    staged/<module id>/<version>/db/...         module SQL migrations
    modules.json                                the active list the loader reads

``module.json`` is ``{"id", "version", "import", "python_cache_tag", "machine",
"files_sha256"}`` plus an optional ``"provides"`` list of declared capabilities
(``["entitlement_source"]``) and an optional ``"requires"`` list of module ids
(``["acme.base"]`` or ``["acme.base>=1.2"]``) that must load first, and an optional
``"settings"`` list of deployment-level settings the operator sets
(``[{"name": "FS_MODULE_...", "description": ..., "default"?, "allowed_values"?}]``,
see ``module_settings.py``) -- exactly the loader's list entry without ``path``. A
module's digest is ``module_descriptor_digest`` of that descriptor, so a declared
capability, dependency or setting is pinned with the module, and a descriptor
without them keeps its digest. Each ``modules.json`` entry is the descriptor plus
``"path": "staged/<id>/<version>"``. A set in which more than one module declares
``entitlement_source`` is refused: a deployment has one entitlement source. So is a
set in which a module requires a module that is not in it, is older than required,
or that forms a dependency cycle, and a set in which two modules declare the same
setting name. ``declared_settings`` reports the settings of the active set, which is
what ``flow-steward config set`` checks a module setting against.

Two callers share this one implementation:

* the operator CLI on Scalable, which mounts the runtime directory read-write
  (``core.infrastructure.release.platform_module_activator.FilesystemModuleActivator``);
* the Compact runtime image, where a disposable container of the installed image runs
  ``core.entrypoints.cli.ops.platform_modules`` against its own data volume, because
  the operator CLI cannot enter that volume's runtime directory.

This module imports nothing from ``core.application``; failures are
``ModuleStoreError`` with the stable lifecycle code the caller reports. Staged
artifacts are never deleted here; retention belongs to the deployment manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infrastructure.platform_modules.dependencies import (
    parse_module_requirements,
    resolve_module_dependencies,
)
from core.infrastructure.platform_modules.loader import (
    ENTITLEMENT_SOURCE_CAPABILITY,
    MODULE_LIST_FILENAME,
    MODULE_LIST_SCHEMA_VERSION,
    OPTIONAL_ENTRY_KEYS,
    PlatformModuleError,
    activate_prepared_platform_modules,
)
from core.infrastructure.platform_modules.module_settings import (
    ModuleSettingDeclaration,
    conflicting_module_settings,
    module_set_setting_conflicts,
    parse_module_settings,
)

#: Keys every staged module's ``module.json`` descriptor carries. The active module
#: list (``modules.json``) carries these keys, any of the optional ones, plus ``path``.
MODULE_DESCRIPTOR_KEYS = frozenset(
    {"id", "version", "import", "python_cache_tag", "machine", "files_sha256"}
)
#: Keys a descriptor may carry in addition: ``provides`` (declared capabilities),
#: ``requires`` (modules that must load first) and ``settings`` (operator settings).
MODULE_DESCRIPTOR_OPTIONAL_KEYS = OPTIONAL_ENTRY_KEYS


def _has_descriptor_keys(keys: Iterable[str], *, extra: frozenset[str] = frozenset()) -> bool:
    present = set(keys)
    required = MODULE_DESCRIPTOR_KEYS | extra
    return required <= present <= required | MODULE_DESCRIPTOR_OPTIONAL_KEYS


STAGED_DIRNAME = "staged"
MODULE_DESCRIPTOR_FILENAME = "module.json"
#: Mode of a new list when nothing says otherwise (Scalable's runtime directory).
DEFAULT_LIST_MODE = 0o644
_VERIFY_PREFIX = ".verify-"
_TREES = ("src", "site", "db")
#: The role whose loader rules verify a candidate list. Every role applies the same
#: checks; the role name only selects the caller.
_VERIFY_ROLE = "control"


def canonical_module_descriptor(descriptor: Mapping[str, object]) -> bytes:
    """Return the one byte form of a module descriptor that its digest covers.

    UTF-8 JSON with sorted keys and no insignificant whitespace, so the digest
    depends on the descriptor's content, not on how the file was formatted.
    """
    return json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def module_descriptor_digest(descriptor: Mapping[str, object]) -> str:
    """Return the ``sha256:<hex>`` digest a pinned module reference carries.

    The descriptor lists the sha256 of every file of the module, so this one
    digest pins the whole prepared tree.
    """
    return "sha256:" + hashlib.sha256(canonical_module_descriptor(descriptor)).hexdigest()


class ModuleStoreError(Exception):
    """A refusal with the stable lifecycle code the caller reports."""

    def __init__(self, code: str, cause: str) -> None:
        super().__init__(cause)
        self.code = code
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ModuleRef:
    """One pinned module: id, version and descriptor digest."""

    id: str
    version: str
    digest: str

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest}

    @classmethod
    def from_json(cls, raw: object) -> ModuleRef:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"id", "version", "digest"}
            or not all(isinstance(raw[key], str) and raw[key] for key in raw)
        ):
            raise ModuleStoreError(
                "release_module_set_invalid",
                "A pinned platform module must be an object with exactly the string "
                "keys id, version and digest.",
            )
        return cls(id=raw["id"], version=raw["version"], digest=raw["digest"])


@dataclass(frozen=True, slots=True)
class ListOwnership:
    """The owner, group and mode the roles of a profile expect for ``modules.json``."""

    uid: int
    gid: int
    mode: int


def parse_module_set(raw: object) -> tuple[ModuleRef, ...]:
    """Read a JSON list of pinned modules; one version per module, ordered by id."""
    if not isinstance(raw, list):
        raise ModuleStoreError(
            "release_module_set_invalid", "A platform module set must be a JSON list."
        )
    return _ordered(ModuleRef.from_json(item) for item in raw)


def _ordered(modules: Iterable[ModuleRef]) -> tuple[ModuleRef, ...]:
    ordered = tuple(sorted(modules, key=lambda module: module.id))
    ids = [module.id for module in ordered]
    if len(ids) != len(set(ids)):
        raise ModuleStoreError(
            "release_module_set_invalid",
            "A deployment runs exactly one version of each platform module.",
        )
    return ordered


def _safe_segment(value: str, *, what: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ModuleStoreError(
            "release_module_artifact_invalid",
            f"Platform module {what} '{value}' cannot name a staged directory.",
        )
    return value


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ModuleStoreError(
            "release_module_artifact_missing", f"{what} {path} is not a regular file."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModuleStoreError(
            "release_module_artifact_invalid", f"{what} {path} cannot be read as JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ModuleStoreError(
            "release_module_artifact_invalid", f"{what} {path} is not a JSON object."
        )
    return payload


def read_module_descriptor(module_dir: Path) -> dict[str, Any]:
    """Read and shape-check one staged ``module.json``."""
    descriptor = _read_json_object(
        module_dir / MODULE_DESCRIPTOR_FILENAME, what="Platform module descriptor"
    )
    if not _has_descriptor_keys(descriptor):
        raise ModuleStoreError(
            "release_module_artifact_invalid",
            f"Platform module descriptor in {module_dir} must have exactly the keys "
            f"{sorted(MODULE_DESCRIPTOR_KEYS)} and optionally "
            f"{sorted(MODULE_DESCRIPTOR_OPTIONAL_KEYS)}.",
        )
    return descriptor


def render_module_list(entries: list[dict[str, Any]]) -> bytes:
    payload = {"schema_version": MODULE_LIST_SCHEMA_VERSION, "modules": entries}
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlatformModuleStore:
    """Staged artifacts and the active list under one platform-module root.

    Before any write the candidate list is verified by the loader itself against a
    throwaway root: ``<root>/.verify-<random>/`` receives the candidate as its
    ``modules.json`` and hard links (copies where the filesystem refuses links) of
    the staged trees at the same relative paths. The loader requires its list to be
    ``<root>/modules.json`` with every module inside that root and follows no
    symlinks, so a separate root holding the same bytes is the only way to run it
    without touching the live list. Symlinks inside a staged tree are reproduced as
    symlinks so the loader still refuses them.

    ``image_checks`` is the loader's own switch: true only where this process runs
    in a role image (Compact's disposable container), false in the management image,
    whose installed packages differ from the roles'.

    ``ownership`` fixes the list's owner, group and mode (Compact: the runtime
    directory's operator and group, readable by the roles and writable by none of
    them). Without it a rewritten list keeps the previous list's mode and owner and a
    new one follows the root's owner with ``DEFAULT_LIST_MODE`` (Scalable).
    """

    def __init__(
        self,
        root: Path,
        *,
        cache_tag: str | None = None,
        machine: str | None = None,
        image_checks: bool = False,
        ownership: ListOwnership | None = None,
    ) -> None:
        self.root = Path(root)
        self.cache_tag = cache_tag
        self.machine = machine
        self.image_checks = image_checks
        self.ownership = ownership

    @property
    def list_path(self) -> Path:
        return self.root / MODULE_LIST_FILENAME

    def runtime_directory_writable(self) -> bool:
        """Whether this process may write the runtime directory holding the root.

        Total: a directory this process cannot even inspect answers ``False``.
        """
        runtime_dir = self.root.parent
        try:
            return runtime_dir.is_dir() and os.access(runtime_dir, os.W_OK | os.X_OK)
        except OSError:
            return False

    # -- staged artifacts ---------------------------------------------------

    def staged_versions(self) -> dict[str, tuple[str, ...]]:
        staged_root = self.root / STAGED_DIRNAME
        if not staged_root.is_dir():
            return {}
        return {
            module_dir.name: tuple(
                sorted(path.name for path in module_dir.iterdir() if path.is_dir())
            )
            for module_dir in sorted(path for path in staged_root.iterdir() if path.is_dir())
        }

    def staged_ref(self, module_id: str, version: str) -> ModuleRef:
        module_dir = (
            self.root
            / STAGED_DIRNAME
            / _safe_segment(module_id, what="id")
            / _safe_segment(version, what="version")
        )
        descriptor = read_module_descriptor(module_dir)
        if descriptor["id"] != module_id or descriptor["version"] != version:
            raise ModuleStoreError(
                "release_module_artifact_invalid",
                f"Staged platform module {module_dir} describes "
                f"{descriptor['id']}@{descriptor['version']}.",
            )
        return ModuleRef(id=module_id, version=version, digest=module_descriptor_digest(descriptor))

    def staged_refs(self) -> tuple[ModuleRef, ...]:
        """Every staged artifact, each version, with its descriptor digest."""
        return tuple(
            self.staged_ref(module_id, version)
            for module_id, versions in self.staged_versions().items()
            for version in versions
        )

    # -- the active list ----------------------------------------------------

    def _installed_entries(self) -> list[dict[str, Any]]:
        if not self.list_path.exists() and not self.list_path.is_symlink():
            return []
        payload = _read_json_object(self.list_path, what="Platform module list")
        raw_modules = payload.get("modules")
        if payload.get("schema_version") != MODULE_LIST_SCHEMA_VERSION or not isinstance(
            raw_modules, list
        ):
            raise ModuleStoreError(
                "release_module_list_invalid",
                f"Platform module list {self.list_path} has an unsupported shape.",
            )
        for raw in raw_modules:
            if not isinstance(raw, dict) or not _has_descriptor_keys(
                raw, extra=frozenset({"path"})
            ):
                raise ModuleStoreError(
                    "release_module_list_invalid",
                    f"Platform module list {self.list_path} has a malformed entry.",
                )
        return list(raw_modules)

    def installed_refs(self) -> tuple[ModuleRef, ...]:
        """The active set, each digest computed from the active entry without ``path``.

        An entry is written from its staged descriptor plus ``path``, so this is the
        staged descriptor's digest; computing it from the entry reports what the
        roles actually load even if the staged copy was replaced afterwards.
        """
        refs: list[ModuleRef] = []
        for raw in self._installed_entries():
            descriptor = {key: value for key, value in raw.items() if key != "path"}
            refs.append(
                ModuleRef(
                    id=raw["id"],
                    version=raw["version"],
                    digest=module_descriptor_digest(descriptor),
                )
            )
        return _ordered(refs)

    def declared_settings(self) -> tuple[ModuleSettingDeclaration, ...]:
        """The settings the active set declares, by name; none without a list.

        The roles publish exactly these (``module_settings.publish_module_settings``),
        so ``config set`` accepts a module setting only when this answer names it.
        """
        entries = self._installed_entries()
        declarations = _settings_of(entries, code="release_module_list_invalid")
        return tuple(sorted(declarations, key=lambda declaration: declaration.name))

    def adds_module_migrations(
        self, previous: Iterable[ModuleRef], target: Iterable[ModuleRef]
    ) -> bool:
        """Compare the staged ``db/migrations/*.sql`` of both sets by id, name and content.

        Any target migration that the previous set does not ship byte for byte counts.
        A staged artifact that cannot be read answers ``True``.
        """
        try:
            before = self._migration_files(previous)
            after = self._migration_files(target)
        except (ModuleStoreError, OSError):
            return True
        return not after <= before

    def entries_for(self, modules: Iterable[ModuleRef]) -> list[dict[str, Any]]:
        """The list entries for ``modules``; every artifact is checked against its pin.

        A set in which more than one module declares ``entitlement_source`` is
        refused before anything is written (ADR 0082: one entitlement source).
        """
        entries = [self._entry_for(module) for module in _ordered(modules)]
        sources = [
            str(entry["id"])
            for entry in entries
            if isinstance(entry.get("provides"), list)
            and ENTITLEMENT_SOURCE_CAPABILITY in entry["provides"]
        ]
        if len(sources) > 1:
            raise ModuleStoreError(
                "release_module_set_rejected",
                f"Platform modules {', '.join(sources)} all declare "
                f"'{ENTITLEMENT_SOURCE_CAPABILITY}'; a deployment has exactly one "
                "entitlement source, so activate only one of them.",
            )
        _check_dependencies(entries)
        _settings_of(entries, code="release_module_artifact_invalid")
        return entries

    def current_bytes(self) -> bytes | None:
        try:
            if self.list_path.is_symlink() or not self.list_path.is_file():
                return None
            return self.list_path.read_bytes()
        except OSError:
            return None

    def verify(self, modules: Iterable[ModuleRef]) -> None:
        """Check ``modules`` as the roles would, without touching the live list."""
        entries = self.entries_for(modules)
        if entries:
            self.verify_entries(render_module_list(entries), entries)

    def activate(self, modules: Iterable[ModuleRef]) -> bool:
        """Make ``modules`` the active list; return whether the list changed.

        Every check runs before the first write; an empty set removes the list.
        """
        entries = self.entries_for(modules)
        if not entries:
            return self.remove_list()
        rendered = render_module_list(entries)
        if self.current_bytes() == rendered:
            return False
        self.verify_entries(rendered, entries)
        self.write_list(rendered)
        return True

    def verify_entries(self, rendered: bytes, entries: list[dict[str, Any]]) -> None:
        verify_root = Path(tempfile.mkdtemp(prefix=_VERIFY_PREFIX, dir=self.root))
        try:
            for entry in entries:
                source = self.root / entry["path"]
                target = verify_root / entry["path"]
                target.mkdir(parents=True)
                for tree in _TREES:
                    _mirror(source / tree, target / tree)
            (verify_root / MODULE_LIST_FILENAME).write_bytes(rendered)
            try:
                activate_prepared_platform_modules(
                    _VERIFY_ROLE,
                    root=verify_root,
                    sys_path=list(sys.path),
                    cache_tag=self.cache_tag,
                    machine=self.machine,
                    image_checks=self.image_checks,
                )
            except PlatformModuleError as exc:
                raise ModuleStoreError(
                    "release_module_set_rejected",
                    f"The process roles would refuse this platform module set: {exc}",
                ) from exc
        finally:
            shutil.rmtree(verify_root, ignore_errors=True)

    def write_list(self, rendered: bytes) -> None:
        """Replace the list atomically with its final mode and owner already set."""
        mode, owner = self._list_mode_and_owner()
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{MODULE_LIST_FILENAME}.", dir=self.root)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, mode)
            if owner is not None:
                self._apply_owner(temp_path, owner)
            os.replace(temp_path, self.list_path)
            _fsync_directory(self.root)
        finally:
            temp_path.unlink(missing_ok=True)

    def remove_list(self) -> bool:
        if not self.list_path.exists() and not self.list_path.is_symlink():
            return False
        self.list_path.unlink()
        _fsync_directory(self.root)
        return True

    # -- helpers ------------------------------------------------------------

    def _list_mode_and_owner(self) -> tuple[int, tuple[int, int] | None]:
        if self.ownership is not None:
            return self.ownership.mode, (self.ownership.uid, self.ownership.gid)
        # Keep the existing list's mode and owner; a new list follows the root's owner.
        reference = self.list_path if self.list_path.is_file() else self.root
        info = reference.stat()
        mode = stat.S_IMODE(info.st_mode) if reference == self.list_path else DEFAULT_LIST_MODE
        owner = (info.st_uid, info.st_gid)
        if os.geteuid() != 0 or owner == (os.geteuid(), os.getegid()):
            return mode, None
        return mode, owner

    def _apply_owner(self, path: Path, owner: tuple[int, int]) -> None:
        if os.geteuid() == 0:
            os.chown(path, *owner)
            return
        info = path.stat()
        if (info.st_uid, info.st_gid) == owner:
            return
        if self.ownership is None:
            # Scalable without root: the file stays this process's, as before.
            return
        uid, gid = owner
        if info.st_uid != uid:
            raise ModuleStoreError(
                "release_module_list_ownership",
                f"The platform module list must be owned by uid {uid}; this process "
                f"(uid {os.geteuid()}) cannot give it away.",
            )
        try:
            os.chown(path, -1, gid)
        except OSError as exc:
            raise ModuleStoreError(
                "release_module_list_ownership",
                f"The platform module list must belong to group {gid}: {exc}",
            ) from exc

    def _module_dir(self, module: ModuleRef) -> Path:
        module_id = _safe_segment(module.id, what="id")
        version = _safe_segment(module.version, what="version")
        module_dir = self.root / STAGED_DIRNAME / module_id / version
        if not module_dir.is_dir() or module_dir.is_symlink():
            raise ModuleStoreError(
                "release_module_artifact_missing",
                f"Platform module {module_id}@{version} is not staged at {module_dir}.",
            )
        return module_dir

    def _migration_files(self, modules: Iterable[ModuleRef]) -> set[tuple[str, str, str]]:
        files: set[tuple[str, str, str]] = set()
        for module in modules:
            migrations = self._module_dir(module) / "db" / "migrations"
            if not migrations.is_dir():
                continue
            for path in migrations.glob("*.sql"):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                files.add((module.id, path.name, digest))
        return files

    def _entry_for(self, module: ModuleRef) -> dict[str, Any]:
        module_dir = self._module_dir(module)
        relative = f"{STAGED_DIRNAME}/{module.id}/{module.version}"
        descriptor = read_module_descriptor(module_dir)
        if descriptor["id"] != module.id or descriptor["version"] != module.version:
            raise ModuleStoreError(
                "release_module_artifact_invalid",
                f"Staged platform module {module_dir} describes "
                f"{descriptor['id']}@{descriptor['version']}, not {module.id}@{module.version}.",
            )
        digest = module_descriptor_digest(descriptor)
        if digest != module.digest:
            raise ModuleStoreError(
                "release_module_digest_mismatch",
                f"Staged platform module {module.id}@{module.version} has digest {digest}; "
                f"the upgrade pinned {module.digest}.",
            )
        return {**descriptor, "path": relative}


def _settings_of(entries: list[dict[str, Any]], *, code: str) -> list[ModuleSettingDeclaration]:
    """Every setting the entries declare; refuse a malformed one or a name two declare."""
    declarations: list[ModuleSettingDeclaration] = []
    for entry in entries:
        module_id = str(entry["id"])
        try:
            declarations.extend(
                parse_module_settings(
                    entry.get("settings", []),
                    module_id=module_id,
                    where=f"Platform module {module_id}@{entry['version']} settings",
                )
            )
        except ValueError as exc:
            raise ModuleStoreError(code, str(exc)) from exc
    conflicts = conflicting_module_settings(declarations)
    if conflicts:
        raise ModuleStoreError(
            "release_module_set_rejected",
            "Platform modules declare the same setting: "
            + "; ".join(f"{name} ({', '.join(ids)})" for name, ids in conflicts.items())
            + ". Activate only one of the modules that declare it.",
        )
    overlaps = module_set_setting_conflicts((str(entry["id"]) for entry in entries), declarations)
    if overlaps:
        raise ModuleStoreError(
            "release_module_set_rejected",
            "Platform module settings overlap: "
            + "; ".join(overlaps)
            + ". A module's settings must start with its own prefix and with no other "
            "module's.",
        )
    return declarations


def _check_dependencies(entries: list[dict[str, Any]]) -> None:
    """Refuse a set in which a module's ``requires`` cannot be met (ADR 0082)."""
    modules = {}
    for entry in entries:
        module_id = str(entry["id"])
        try:
            requires = parse_module_requirements(entry.get("requires", []), module_id=module_id)
        except ValueError as exc:
            raise ModuleStoreError(
                "release_module_artifact_invalid",
                f"Platform module {module_id}@{entry['version']} requires {exc}.",
            ) from exc
        modules[module_id] = (str(entry["version"]), requires)
    problems = resolve_module_dependencies(modules).problems
    if problems:
        reasons = "; ".join(
            f"{module_id}@{modules[module_id][0]} {reason}"
            for module_id, reason in problems.items()
        )
        raise ModuleStoreError(
            "release_module_set_rejected",
            f"This platform module set cannot load: {reasons}. Add the required "
            "modules to the set or leave out the modules that need them.",
        )


def _mirror(source: Path, target: Path) -> None:
    """Reproduce ``source`` at ``target`` with hard links, keeping symlinks as symlinks."""
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
        return
    if not source.exists():
        return
    if not source.is_dir():
        _link_file(source, target)
        return
    target.mkdir()
    for child in sorted(source.iterdir()):
        _mirror(child, target / child.name)


def _link_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise ModuleStoreError(
            "release_module_artifact_invalid",
            f"Staged platform module file {source} is not a regular file.",
        )
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


__all__ = [
    "DEFAULT_LIST_MODE",
    "MODULE_DESCRIPTOR_FILENAME",
    "MODULE_DESCRIPTOR_KEYS",
    "MODULE_DESCRIPTOR_OPTIONAL_KEYS",
    "STAGED_DIRNAME",
    "ListOwnership",
    "ModuleRef",
    "ModuleStoreError",
    "PlatformModuleStore",
    "canonical_module_descriptor",
    "module_descriptor_digest",
    "parse_module_set",
    "read_module_descriptor",
    "render_module_list",
]
