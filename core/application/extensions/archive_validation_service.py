from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.management.command_models_extensions import (
    ExtensionDescriptorValidateInput,
)
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

from .binding_context import ExtensionLifecycleContext
from .binding_projection_service import ExtensionBindingProjectionService

_TOKEN_EXTENSION_YAML = "extension.yaml"


@dataclass(frozen=True, slots=True)
class ValidatedArchiveUpload:
    filename: str
    safe_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ValidatedExtensionBundle:
    bundle_dir: Path
    descriptor_path: Path | None
    compiled: dict[str, Any]
    extension_id: str
    version: str


class ExtensionArchiveValidationService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        projection: ExtensionBindingProjectionService,
    ) -> None:
        self._context = context
        self._projection = projection

    def validate_upload(
        self,
        *,
        filename: str,
        content: bytes,
        max_archive_bytes: int,
    ) -> ValidatedArchiveUpload:
        normalized_filename = str(filename or "").strip()
        if not normalized_filename.lower().endswith(".zip"):
            raise ServiceValidationError("Install extension from file accepts only .zip archives")
        safe_name = os.path.basename(normalized_filename.replace("\\", "/"))
        if not safe_name or safe_name in {".", ".."} or safe_name != normalized_filename:
            raise ServiceValidationError(
                "Extension archive filename must be a plain '.zip' name without path components"
            )
        if not content:
            raise ServiceValidationError("Uploaded extension archive is empty")
        if len(content) > max_archive_bytes:
            raise ServiceValidationError(
                f"Extension archive is too large — maximum {max_archive_bytes} bytes"
            )
        return ValidatedArchiveUpload(
            filename=normalized_filename,
            safe_name=safe_name,
            content=content,
        )

    def validate_extracted_bundle(
        self,
        *,
        extracted_root: Path,
        bl: Any,
        scope: ScopeContext | None,
    ) -> ValidatedExtensionBundle:
        self._validate_native_package(extracted_root)
        bundle_dir, descriptor_path = self._locate_bundle(extracted_root)
        compiled, extension_id, version = self._compile_bundle(bundle_dir)
        self._validate_icon(bundle_dir)
        if descriptor_path is not None:
            self._validate_descriptor_identity(
                descriptor_path=descriptor_path,
                extension_id=extension_id,
                version=version,
                bl=bl,
                scope=scope,
            )
        return ValidatedExtensionBundle(
            bundle_dir=bundle_dir,
            descriptor_path=descriptor_path,
            compiled=compiled,
            extension_id=extension_id,
            version=version,
        )

    def _validate_native_package(self, extracted_root: Path) -> None:
        from core.infrastructure.extension_runtime.native_package import (
            native_target_payload_paths,
            validate_native_target_package,
        )

        metadata_paths = list(extracted_root.rglob(".fs-package.yaml"))
        if len(metadata_paths) > 1:
            raise ServiceValidationError("Extension archive has conflicting package metadata")
        if native_target_payload_paths(extracted_root) and not metadata_paths:
            raise ServiceValidationError(
                "Extension archive with bin/<runtime-target> payload requires package metadata"
            )
        if not metadata_paths:
            return
        package_metadata = self._load_package_metadata(metadata_paths[0])
        package_target = self._validate_runtime_target(package_metadata)
        package_root = metadata_paths[0].parent
        if not (package_root / _TOKEN_EXTENSION_YAML).is_file():
            raise ServiceValidationError(
                "Extension package metadata must be co-located with extension.yaml"
            )
        try:
            validate_native_target_package(
                package_root,
                package_metadata=package_metadata,
                runtime_target=package_target,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

    @staticmethod
    def _load_package_metadata(metadata_path: Path) -> dict[str, Any]:
        import yaml

        package_metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
        if not isinstance(package_metadata, dict):
            raise ServiceValidationError("Extension package metadata must be an object")
        return package_metadata

    @staticmethod
    def _validate_runtime_target(package_metadata: dict[str, Any]) -> str:
        from core.infrastructure.extension_runtime.runtime_target import (
            extension_runtime_target,
            require_extension_runtime_target,
        )

        try:
            package_target = require_extension_runtime_target(
                str(package_metadata.get("runtime_target") or "")
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        server_target = extension_runtime_target()
        if package_target != server_target:
            raise ServiceValidationError(
                f"Extension package requires server target '{package_target}', "
                f"but this server target is '{server_target}'",
                payload={
                    "package_runtime_target": package_target,
                    "server_runtime_target": server_target,
                },
            )
        return package_target

    @staticmethod
    def _locate_bundle(extracted_root: Path) -> tuple[Path, Path | None]:
        candidate_dirs: list[Path] = []
        if (extracted_root / _TOKEN_EXTENSION_YAML).is_file():
            candidate_dirs.append(extracted_root)
        for child in sorted(extracted_root.iterdir()):
            if child.is_dir() and (child / _TOKEN_EXTENSION_YAML).is_file():
                candidate_dirs.append(child)
        if len(candidate_dirs) != 1:
            raise ServiceValidationError(
                "Extension archive must contain exactly one canonical extension.yaml bundle "
                "with manifest_version: 2"
            )
        bundle_dir = candidate_dirs[0]
        descriptor_candidates = sorted(
            path for path in extracted_root.rglob("extension-descriptor.yaml") if path.is_file()
        )
        co_located = [path for path in descriptor_candidates if path.parent == bundle_dir]
        if len(co_located) != len(descriptor_candidates) or len(co_located) > 1:
            raise ServiceValidationError(
                "A runtime bundle may contain at most one co-located extension descriptor"
            )
        return bundle_dir, co_located[0] if co_located else None

    @staticmethod
    def _validate_icon(bundle_dir: Path) -> None:
        from core.infrastructure.extension_runtime.bundle_icon import extension_icon_issues

        issues = extension_icon_issues(bundle_dir)
        if issues:
            raise ServiceValidationError(
                str(issues[0]["message"]),
                payload={"compile_errors": [], "validation_errors": issues},
            )

    def _compile_bundle(self, bundle_dir: Path) -> tuple[dict[str, Any], str, str]:
        from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

        compiled = compile_extension_bundle(bundle_dir)
        if str(compiled.get("status") or "").strip().lower() != "valid":
            raise ServiceValidationError(
                str(compiled.get("scan_error") or "").strip() or "Extension package is invalid",
                payload={
                    "compile_errors": list(compiled.get("compile_errors") or []),
                    "validation_errors": list(compiled.get("validation_errors") or []),
                },
            )
        extension_id = str(compiled.get("extension_key") or "").strip()
        if not extension_id:
            raise ServiceValidationError("Extension package is missing extension_id")
        version = self._projection.extension_version_from_payload(compiled)
        if not version:
            raise ServiceValidationError("Extension package is missing version")
        return compiled, extension_id, version

    def _validate_descriptor_identity(
        self,
        *,
        descriptor_path: Path,
        extension_id: str,
        version: str,
        bl: Any,
        scope: ScopeContext | None,
    ) -> None:
        # Compatibility is declared against the extension host contract version.
        from core.application.extensions.service_support import _runtime_platform_version

        validation = self._context.command_service(bl).validate_descriptor(
            ExtensionDescriptorValidateInput(
                source_type="file",
                descriptor_path=str(descriptor_path),
                platform_version=_runtime_platform_version(),
                allow_insecure_remote=False,
            ),
            scope=scope,
        )
        normalized = self._context.command_service(bl).descriptor_validation_or_error(validation)
        descriptor_extension_id = str(normalized.get("extension_id") or "").strip()
        descriptor_version = str(normalized.get("release_version") or "").strip()
        if descriptor_extension_id != extension_id:
            raise ServiceValidationError(
                "extension.yaml and extension-descriptor.yaml extension_id must match",
                payload={
                    "manifest_extension_id": extension_id,
                    "descriptor_extension_id": descriptor_extension_id,
                },
            )
        if descriptor_version != version:
            raise ServiceValidationError(
                "extension.yaml version and extension-descriptor.yaml release_version must match",
                payload={
                    "manifest_version": version,
                    "descriptor_release_version": descriptor_version,
                },
            )


__all__ = [
    "ExtensionArchiveValidationService",
    "ValidatedArchiveUpload",
    "ValidatedExtensionBundle",
]


def materialized_python_requirements(
    *,
    bundle_root: Path,
    compiled_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Install a bundle's declared wheels, reporting a bad bundle as a bad request.

    The wheel resolver raises a plain ``ValueError`` for everything it refuses
    about a bundle's own contents — a wheel missing, a wheel without a declared
    hash, no wheel built for this platform — and names the tags it needed and
    the tags it found. Nothing on the install path caught it, so the transport
    turned that message into `500 Internal server error` with a correlation id
    and left the reason in the container log. These refusals are about what the
    caller uploaded, so they are 4xx and the caller gets to read them.
    """

    from core.infrastructure.extension_runtime.python_dependencies import (
        materialize_python_requirements,
    )

    try:
        return materialize_python_requirements(
            bundle_root=bundle_root,
            compiled_manifest=compiled_manifest,
        )
    except ValueError as exc:
        raise ServiceValidationError(str(exc)) from exc
