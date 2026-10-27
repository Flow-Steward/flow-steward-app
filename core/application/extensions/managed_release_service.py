from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceUnavailableError,
    ServiceValidationError,
)

from .binding_constants import (
    _MANAGED_EXTENSION_STAGE_NAME_RE,
    _MANAGED_RELEASE_INSTALL_METADATA_FILENAME,
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
    archive_too_large_message,
)
from .binding_context import ExtensionLifecycleContext

_MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID = (
    "Installed extension active release path is invalid"
)
_MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED = (
    "Extension release references could not be verified"
)

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message


class ManagedReleaseService:
    def __init__(self, *, context: ExtensionLifecycleContext) -> None:
        self._context = context

    def remove_managed_extension_staging_path(self, path: Path) -> None:
        self._remove_managed_extension_staging_path(path)

    def reconcile_managed_extension_staging(self, *, extensions_root: Path) -> int:
        return self._reconcile_managed_extension_staging(extensions_root=extensions_root)

    def record_install_id_in_bundle_metadata(
        self,
        *,
        bundle_root: Path,
        metadata_filename: str,
        install_id: str,
    ) -> None:
        self._record_install_id_in_bundle_metadata(
            bundle_root=bundle_root,
            metadata_filename=metadata_filename,
            install_id=install_id,
        )

    def python_dependency_install_metadata(
        self,
        python_deps: dict[str, Any],
        *,
        bundle_root: Path,
        compiled: dict[str, Any],
    ) -> dict[str, Any]:
        return self._python_dependency_install_metadata(
            python_deps,
            bundle_root=bundle_root,
            compiled=compiled,
        )

    def managed_simple_extension_root(self, extension_id: str) -> Path:
        return self._managed_simple_extension_root(extension_id)

    def managed_simple_extension_active_release_root(self, extension_id: str) -> Path | None:
        return self._managed_simple_extension_active_release_root(extension_id)

    def validated_managed_simple_extension_release_directory(
        self,
        *,
        extension_id: str,
        release_root: Path,
    ) -> Path:
        return self._validated_managed_simple_extension_release_directory(
            extension_id=extension_id,
            release_root=release_root,
        )

    def validated_managed_simple_extension_cleanup_directory(
        self,
        *,
        extension_id: str,
        bundle_root: Path,
    ) -> Path:
        return self._validated_managed_simple_extension_cleanup_directory(
            extension_id=extension_id,
            bundle_root=bundle_root,
        )

    def managed_simple_extension_release_root(
        self,
        *,
        extension_id: str,
        version: str,
        checksum: str = "",
    ) -> Path:
        return self._managed_simple_extension_release_root(
            extension_id=extension_id,
            version=version,
            checksum=checksum,
        )

    def managed_release_tree_fingerprint(
        self,
        bundle_root: Path,
        *,
        source_identity: bool = False,
        install_metadata_content_override: bytes | None = None,
    ) -> str:
        return self._managed_release_tree_fingerprint(
            bundle_root,
            source_identity=source_identity,
            install_metadata_content_override=install_metadata_content_override,
        )

    def normalize_managed_release_tree_modes(self, bundle_root: Path) -> None:
        self._normalize_managed_release_tree_modes(bundle_root)

    def assert_managed_release_unreferenced(
        self,
        *,
        bl: Any,
        runtime_store: Any | None,
        extension_id: str,
        release_root: Path,
    ) -> None:
        self._assert_managed_release_unreferenced(
            bl=bl,
            runtime_store=runtime_store,
            extension_id=extension_id,
            release_root=release_root,
        )

    def publish_or_adopt_managed_release(
        self,
        *,
        bl: Any,
        extension_id: str,
        staged_root: Path,
        release_root: Path,
        staged_fingerprint: str,
        runtime_store: Any | None = None,
    ) -> bool:
        return self._publish_or_adopt_managed_release(
            bl=bl,
            runtime_store=runtime_store,
            extension_id=extension_id,
            staged_root=staged_root,
            release_root=release_root,
            staged_fingerprint=staged_fingerprint,
        )

    def activate_managed_simple_extension_release(
        self,
        *,
        active_root: Path,
        release_root: Path,
    ) -> None:
        self._activate_managed_simple_extension_release(
            active_root=active_root,
            release_root=release_root,
        )

    def remove_managed_simple_extension_active_root(self, active_root: Path) -> None:
        self._remove_managed_simple_extension_active_root(active_root)

    @staticmethod
    def _remove_managed_extension_staging_path(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
            return
        if not path.exists():
            return
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(path)
            return
        path.unlink()

    @classmethod
    def _reconcile_managed_extension_staging(cls, *, extensions_root: Path) -> int:
        """Remove interrupted pre-publication trees while holding the lifecycle lock."""
        root = Path(extensions_root)
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            raise ServiceValidationError("Managed extensions root is invalid")
        removed = 0
        for candidate in root.iterdir():
            if not _MANAGED_EXTENSION_STAGE_NAME_RE.fullmatch(candidate.name):
                continue
            cls._remove_managed_extension_staging_path(candidate)
            removed += 1
        return removed

    @staticmethod
    def _bundle_metadata_with_install_id(*, metadata_text: str, install_id: str) -> str:
        import yaml

        payload = yaml.safe_load(metadata_text) or {}
        if not isinstance(payload, dict):
            payload = {}
        payload["install_id"] = install_id
        return yaml.safe_dump(payload, sort_keys=True)

    @classmethod
    def _record_install_id_in_bundle_metadata(
        cls, *, bundle_root: Path, metadata_filename: str, install_id: str
    ) -> None:
        normalized_install_id = str(install_id or "").strip()
        if not normalized_install_id:
            return

        metadata_path = bundle_root / metadata_filename
        metadata_stat = metadata_path.lstat()
        if not stat.S_ISREG(metadata_stat.st_mode):
            raise ServiceValidationError("Extension install metadata must be a regular file")
        rendered = cls._bundle_metadata_with_install_id(
            metadata_text=metadata_path.read_text(encoding="utf-8"),
            install_id=normalized_install_id,
        ).encode("utf-8")
        temp_path = bundle_root.parent / (
            f".{bundle_root.name}.{metadata_path.name}.tmp-{uuid.uuid4().hex}"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IMODE(metadata_stat.st_mode),
            )
            # The temporary sibling may inherit a different group than the
            # release; preserve its writer group before the atomic replacement.
            os.fchown(descriptor, -1, metadata_stat.st_gid)
            os.fchmod(descriptor, stat.S_IMODE(metadata_stat.st_mode))
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                target.write(rendered)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, metadata_path)
            cls._fsync_directory(metadata_path.parent)
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _python_dependency_install_metadata(
        python_deps: dict[str, Any],
        *,
        bundle_root: Path,
        compiled: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(python_deps or {})
        if str(metadata.get("mode") or "").strip() != "bundled_wheels":
            return metadata
        from core.infrastructure.extension_runtime.python_dependencies import (
            extension_python_site_dir_from_parts,
        )

        version = str(compiled.get("version") or "").strip() or "0.0.0"
        metadata["site_dir"] = str(
            extension_python_site_dir_from_parts(bundle_root, version=version)
        )
        return metadata

    @staticmethod
    def _managed_simple_extension_root(extension_id: str) -> Path:
        from core.application.extensions.identity import extension_folder_name
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root

        extensions_root = Path(default_extensions_root()).resolve()
        bundle_root = extensions_root / extension_folder_name(extension_id)
        if extensions_root != bundle_root.parent:
            raise ServiceValidationError("extension_id must identify a managed extension directory")
        return bundle_root

    @classmethod
    def _managed_simple_extension_active_release_root(
        cls,
        extension_id: str,
    ) -> Path | None:
        """Return the active release only when its link stays in the managed tree."""
        active_root = cls._managed_simple_extension_root(extension_id)
        if not active_root.is_symlink():
            return None

        try:
            link_target = Path(os.readlink(active_root))
            lexical_target = link_target
            if not lexical_target.is_absolute():
                lexical_target = active_root.parent / lexical_target
            lexical_target = Path(os.path.abspath(lexical_target))
            resolved_target = active_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ServiceValidationError(
                _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
            ) from exc

        if resolved_target != lexical_target:
            raise ServiceValidationError(
                _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
            )
        return cls._validated_managed_simple_extension_release_directory(
            extension_id=extension_id,
            release_root=resolved_target,
        )

    @classmethod
    def _validated_managed_simple_extension_release_directory(
        cls,
        *,
        extension_id: str,
        release_root: Path,
    ) -> Path:
        """Validate a physical release immediately before destructive cleanup."""
        active_root = cls._managed_simple_extension_root(extension_id)
        releases_root = active_root.parent / ".releases"
        expected_parent = releases_root / active_root.name
        candidate = Path(os.path.abspath(release_root))
        for ancestor in (releases_root, expected_parent):
            if ancestor.is_symlink() or not ancestor.is_dir():
                raise ServiceValidationError(
                    _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
                )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ServiceValidationError(
                _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
            ) from exc
        if (
            candidate.parent != expected_parent
            or candidate.is_symlink()
            or not candidate.is_dir()
            or resolved != candidate
        ):
            raise ServiceValidationError(
                _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
            )
        return resolved

    def _validated_managed_simple_extension_cleanup_directory(
        self,
        *,
        extension_id: str,
        bundle_root: Path,
    ) -> Path:
        """Validate an exact direct bundle or an immutable managed release."""
        active_root = self._managed_simple_extension_root(extension_id)
        candidate = Path(os.path.abspath(bundle_root))
        if candidate == active_root:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ServiceValidationError(
                    _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
                ) from exc
            if candidate.is_symlink() or not candidate.is_dir() or resolved != candidate:
                raise ServiceValidationError(
                    _MESSAGE_INSTALLED_EXTENSION_ACTIVE_RELEASE_PATH_IS_INVALID
                )
            return resolved
        return self._validated_managed_simple_extension_release_directory(
            extension_id=extension_id,
            release_root=candidate,
        )

    @classmethod
    def _managed_simple_extension_release_root(
        cls,
        *,
        extension_id: str,
        version: str,
        checksum: str = "",
    ) -> Path:
        active_root = cls._managed_simple_extension_root(extension_id)
        normalized_version = re.sub(r"[^A-Za-z0-9._-]+", "_", str(version or "").strip())
        if not normalized_version:
            raise ServiceValidationError("Extension release version is required")
        fingerprint = re.sub(r"[^a-fA-F0-9]+", "", str(checksum or ""))[:16]
        release_name = f"{normalized_version}-{fingerprint}" if fingerprint else normalized_version
        releases_root = active_root.parent / ".releases"
        expected_parent = releases_root / active_root.name
        for ancestor in (releases_root, expected_parent):
            if ancestor.is_symlink():
                raise ServiceValidationError(
                    "Extension release path may not contain symlinked ancestors"
                )
            if ancestor.exists() and not ancestor.is_dir():
                raise ServiceValidationError("Extension release path is invalid")
        release_root = expected_parent / release_name
        if release_root.parent != expected_parent:
            raise ServiceValidationError("Extension release path is invalid")
        return release_root

    @staticmethod
    def _managed_release_tree_fingerprint(
        bundle_root: Path,
        *,
        source_identity: bool = False,
        install_metadata_content_override: bytes | None = None,
    ) -> str:
        """Return a path-independent content fingerprint for an extension tree."""
        root = Path(bundle_root)
        if root.is_symlink() or not root.is_dir():
            raise ServiceValidationError("Extension release artifact must be a real directory")

        digest = hashlib.sha256()
        try:
            root_mode = stat.S_IMODE(root.lstat().st_mode)
            root_identity = "" if source_identity else f"{root_mode:o}"
            digest.update(f"root\0{root_identity}\0".encode())
            for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
                relative = path.relative_to(root).as_posix()
                metadata = path.lstat()
                mode = stat.S_IMODE(metadata.st_mode)
                encoded_path = relative.encode("utf-8", errors="surrogateescape")
                digest.update(len(encoded_path).to_bytes(8, "big"))
                digest.update(encoded_path)
                if stat.S_ISDIR(metadata.st_mode):
                    directory_identity = "" if source_identity else f"{mode:o}"
                    digest.update(f"\0directory\0{directory_identity}\0".encode())
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ServiceValidationError(
                        "Extension release artifacts may contain only regular files and directories"
                    )
                file_mode = mode & 0o111 if source_identity else mode
                normalized_content = (
                    install_metadata_content_override
                    if relative == _MANAGED_RELEASE_INSTALL_METADATA_FILENAME
                    else None
                )
                content_size = (
                    len(normalized_content) if normalized_content is not None else metadata.st_size
                )
                digest.update(f"\0file\0{file_mode:o}\0{content_size}\0".encode())
                if normalized_content is not None:
                    digest.update(normalized_content)
                    continue
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
        except ServiceError:
            raise
        except OSError as exc:
            raise ServiceValidationError(
                "Extension release artifact could not be verified"
            ) from exc
        return digest.hexdigest()

    @classmethod
    def _normalize_managed_release_tree_modes(cls, bundle_root: Path) -> None:
        """Canonicalize modes so exact retry identity is independent of host umask."""
        from core.infrastructure.extension_runtime.tree_modes import (
            apply_extension_directory_mode,
            extension_file_mode,
        )

        root = Path(bundle_root)
        cls._managed_release_tree_fingerprint(root, source_identity=True)
        apply_extension_directory_mode(root.chmod)
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                apply_extension_directory_mode(path.chmod)
            elif stat.S_ISREG(metadata.st_mode):
                path.chmod(extension_file_mode(bool(stat.S_IMODE(metadata.st_mode) & 0o111)))

    @classmethod
    def _managed_release_matches_install_id_rewrite(
        cls,
        *,
        staged_root: Path,
        release_root: Path,
    ) -> bool:
        """Accept only the exact metadata bytes emitted by our install-id recorder."""
        import yaml

        staged_metadata = staged_root / _MANAGED_RELEASE_INSTALL_METADATA_FILENAME
        release_metadata = release_root / _MANAGED_RELEASE_INSTALL_METADATA_FILENAME
        try:
            existing_bytes = release_metadata.read_bytes()
            existing_text = existing_bytes.decode("utf-8")
            existing_payload = yaml.safe_load(existing_text) or {}
            if not isinstance(existing_payload, dict):
                return False
            install_id = str(existing_payload.get("install_id") or "").strip()
            if not install_id:
                return False
            expected_bytes = cls._bundle_metadata_with_install_id(
                metadata_text=staged_metadata.read_bytes().decode("utf-8"),
                install_id=install_id,
            ).encode("utf-8")
        except Exception:
            return False
        return existing_bytes == expected_bytes

    @staticmethod
    def _managed_release_reference_matches(value: Any, release_root: Path) -> bool:
        normalized = str(value or "").strip()
        if not normalized:
            return False
        try:
            return Path(normalized).resolve(strict=False) == release_root.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
            ) from exc

    def _assert_managed_release_unreferenced(
        self,
        *,
        bl: Any,
        runtime_store: Any | None,
        extension_id: str,
        release_root: Path,
    ) -> None:
        active_root = self._managed_simple_extension_root(extension_id)
        try:
            active_links = [
                path
                for path in active_root.parent.iterdir()
                if not path.name.startswith(".") and path.is_symlink()
            ]
        except OSError as exc:
            raise ServiceUnavailableError(
                "Extension active release references could not be verified"
            ) from exc
        if any(
            self._managed_release_reference_matches(path, release_root) for path in active_links
        ):
            raise ServiceConflictError("Extension release artifact is already active")

        list_extensions = getattr(bl, "list_extensions", None)
        if not callable(list_extensions):
            raise ServiceUnavailableError("Extension release references cannot be verified")
        try:
            extension_inventory = list_extensions(enabled_only=False)
            if extension_inventory is None:
                raise ServiceUnavailableError(
                    _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
                )
            extension_rows = list(extension_inventory)
        except Exception as exc:
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
            ) from exc
        for row in extension_rows:
            if not isinstance(row, Mapping):
                raise ServiceUnavailableError(
                    _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
                )
            payload = dict(row)
            compiled_manifest = payload.get("compiled_manifest")
            nested = dict(compiled_manifest) if isinstance(compiled_manifest, Mapping) else {}
            if any(
                self._managed_release_reference_matches(value, release_root)
                for value in (payload.get("bundle_root"), nested.get("bundle_root"))
            ):
                raise ServiceConflictError(
                    "Extension release artifact is already referenced by lifecycle storage"
                )

        try:
            resolved_runtime_store = (
                runtime_store if runtime_store is not None else self._context.runtime_store(bl)
            )
            self._context.assert_store_authority(
                bl=bl,
                store=resolved_runtime_store,
                store_name="runtime store",
            )
            list_releases = getattr(resolved_runtime_store, "list_releases", None)
            if not callable(list_releases):
                raise ServiceUnavailableError("Extension release references cannot be verified")
            release_inventory = list_releases()
            if release_inventory is None:
                raise ServiceUnavailableError(
                    _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
                )
            releases = list(release_inventory)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
            ) from exc
        for release in releases:
            descriptor = getattr(release, "descriptor", None)
            if not isinstance(descriptor, Mapping):
                raise ServiceUnavailableError(
                    _MESSAGE_EXTENSION_RELEASE_REFERENCES_COULD_NOT_BE_VERIFIED
                )
            if self._managed_release_reference_matches(descriptor.get("bundle_root"), release_root):
                raise ServiceConflictError(
                    "Extension release artifact is already referenced by lifecycle storage"
                )

    def _publish_or_adopt_managed_release(
        self,
        *,
        bl: Any,
        runtime_store: Any | None = None,
        extension_id: str,
        staged_root: Path,
        release_root: Path,
        staged_fingerprint: str,
    ) -> bool:
        """Publish a new candidate or adopt an exact, proven-unreferenced orphan.

        The caller holds the global extension lifecycle lock. ``True`` means this
        attempt created the candidate and may remove it during ordinary rollback;
        ``False`` means an existing orphan was adopted and must be preserved.
        """
        if not (release_root.exists() or release_root.is_symlink()):
            self._assert_managed_release_unreferenced(
                bl=bl,
                runtime_store=runtime_store,
                extension_id=extension_id,
                release_root=release_root,
            )
            from core.infrastructure.extension_runtime.atomic_publish import rename_no_replace

            try:
                rename_no_replace(staged_root, release_root)
            except OSError:
                if not (release_root.exists() or release_root.is_symlink()):
                    raise
            else:
                return True
        if release_root.is_symlink() or not release_root.is_dir():
            raise ServiceConflictError("Extension release artifact exists with an unsupported type")

        self._assert_managed_release_unreferenced(
            bl=bl,
            runtime_store=runtime_store,
            extension_id=extension_id,
            release_root=release_root,
        )
        # AC17-R163: an orphan an earlier release left has that release's modes, which a
        # retry stages differently; the content still has to match exactly.
        from core.infrastructure.extension_runtime.tree_modes import normalize_previous_release

        release_fd = os.open(release_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            normalize_previous_release(release_fd)
        finally:
            os.close(release_fd)
        existing_fingerprint = self._managed_release_tree_fingerprint(release_root)
        if existing_fingerprint != staged_fingerprint:
            metadata_marker = b"flow-steward-install-id-rewrite"
            if not self._managed_release_matches_install_id_rewrite(
                staged_root=staged_root,
                release_root=release_root,
            ) or self._managed_release_tree_fingerprint(
                release_root,
                install_metadata_content_override=metadata_marker,
            ) != self._managed_release_tree_fingerprint(
                staged_root,
                install_metadata_content_override=metadata_marker,
            ):
                raise ServiceConflictError(
                    "Existing extension release artifact does not match the requested archive"
                )
            staged_metadata = staged_root / _MANAGED_RELEASE_INSTALL_METADATA_FILENAME
            release_metadata = release_root / _MANAGED_RELEASE_INSTALL_METADATA_FILENAME
            os.replace(staged_metadata, release_metadata)
            if self._managed_release_tree_fingerprint(release_root) != staged_fingerprint:
                raise ServiceConflictError(
                    "Existing extension release artifact could not be restored for retry"
                )
        if staged_root.exists():
            shutil.rmtree(staged_root)
        return False

    @staticmethod
    def _activate_managed_simple_extension_release(
        *, active_root: Path, release_root: Path
    ) -> None:
        staged_link = active_root.parent / f".{active_root.name}.link-{uuid.uuid4().hex[:8]}"
        try:
            staged_link.symlink_to(
                os.path.relpath(release_root, active_root.parent), target_is_directory=True
            )
            os.replace(staged_link, active_root)
        finally:
            with contextlib.suppress(FileNotFoundError):
                staged_link.unlink()

    @staticmethod
    def _remove_managed_simple_extension_active_root(active_root: Path) -> None:
        if active_root.is_symlink():
            active_root.unlink()
        elif active_root.exists():
            shutil.rmtree(active_root, ignore_errors=True)


__all__ = ["ManagedReleaseService"]
