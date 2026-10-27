#!/usr/bin/env python3
# mypy: ignore-errors
"""Create a distributable archive for one extension bundle."""

from __future__ import annotations

import hashlib
import io
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yaml

from core.application.extensions.identity import extension_folder_name, is_canonical_extension_id
from core.application.extensions.semver import normalize_semver
from core.application.tooling.extensions.models import ExtensionBundleRequest, ExtensionBundleResult
from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root
from core.infrastructure.extension_runtime.python_dependencies import (
    download_wheels_for_requirements,
    resolve_wheel_platforms,
)
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
    normalize_python_requirements,
)

_TOKEN_EXTENSION_YAML = "extension.yaml"

_MAX_EXTENSION_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
_EXCLUDED_AUTHORING_DIRECTORIES = frozenset(
    {
        ".fs-python",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dev-wheels",
    }
)


def _load_manifest(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / _TOKEN_EXTENSION_YAML
    if not manifest_path.exists():
        return {}
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _manifest_version(bundle_dir: Path) -> str:
    manifest_path = bundle_dir / _TOKEN_EXTENSION_YAML
    if not manifest_path.exists():
        return "0.0.0"
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw.startswith("version:"):
            value = raw.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    return "0.0.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 64)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_installer_archive_limits(archive_path: Path) -> None:
    compressed_bytes = archive_path.stat().st_size
    if compressed_bytes > _MAX_EXTENSION_ARCHIVE_BYTES:
        raise ValueError(
            "Extension archive exceeds the installer compressed-size limit "
            f"({_MAX_EXTENSION_ARCHIVE_BYTES} bytes)"
        )
    with zipfile.ZipFile(archive_path) as archive:
        uncompressed_bytes = sum(info.file_size for info in archive.infolist())
    if uncompressed_bytes > _MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError(
            "Extension archive exceeds the installer uncompressed-size limit "
            f"({_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES} bytes)"
        )


def _bundle(request: ExtensionBundleRequest) -> int:
    extension_id = str(request.extension_id or "").strip()
    root = Path(str(request.root or "").strip() or default_extensions_root()).resolve()
    bundle_name = (
        extension_folder_name(extension_id)
        if is_canonical_extension_id(extension_id)
        else extension_id
    )
    bundle_dir = root / bundle_name
    if not bundle_dir.exists():
        print(f"[FAIL] extension folder not found: {bundle_dir}", flush=True)
        return 2

    from core.infrastructure.extension_runtime.bundle_icon import extension_icon_issues

    # A package is what a catalog publishes and an installer accepts; neither
    # takes a bundle without its logo, so neither should the packager.
    for issue in extension_icon_issues(bundle_dir):
        print(f"[FAIL] {issue['field_path']}: {issue['message']}. {issue['hint']}", flush=True)
        return 2

    runtime_target = ""
    if str(request.runtime_target or "").strip():
        from core.infrastructure.extension_runtime.runtime_target import (
            require_extension_runtime_target,
        )

        try:
            runtime_target = require_extension_runtime_target(request.runtime_target)
        except ValueError as exc:
            print(f"[FAIL] {exc}", flush=True)
            return 2

    from core.infrastructure.extension_runtime.native_package import native_target_payload_paths

    if native_target_payload_paths(bundle_dir) and not runtime_target:
        print(
            "[FAIL] Native extension bundles require --runtime-target so the package "
            "contains exactly one installer-compatible architecture",
            flush=True,
        )
        return 2

    if bool(request.with_wheels):
        manifest = _load_manifest(bundle_dir)
        requirements, issues = normalize_python_requirements(
            manifest.get(PYTHON_REQUIREMENTS_FIELD),
            manifest_path=str(bundle_dir / _TOKEN_EXTENSION_YAML),
        )
        if issues:
            print(
                f"[FAIL] invalid {PYTHON_REQUIREMENTS_FIELD}: {issues[0].get('message')}",
                flush=True,
            )
            return 2
        if requirements:
            try:
                for platform in resolve_wheel_platforms(str(request.platforms or "")):
                    download_wheels_for_requirements(
                        requirements=requirements,
                        destination=bundle_dir / "wheels",
                        platform=platform,
                    )
            except Exception as exc:
                print(f"[FAIL] wheel download failed: {exc}", flush=True)
                return 2
            print(f"[PASS] wheels ready: {bundle_dir / 'wheels'}", flush=True)

    requested_version = str(request.version or "").strip()
    manifest_override: bytes | None = None
    descriptor_override: bytes | None = None
    if requested_version:
        try:
            version = normalize_semver(requested_version, "extension version")
        except ValueError as exc:
            print(f"[FAIL] {exc}", flush=True)
            return 2
        manifest = _load_manifest(bundle_dir)
        if str(manifest.get("extension_id") or "").strip() != extension_id:
            print(
                "[FAIL] extension.yaml extension_id does not match the requested bundle", flush=True
            )
            return 2
        manifest["version"] = version
        manifest_override = yaml.safe_dump(
            manifest,
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        descriptor_path = bundle_dir / "extension-descriptor.yaml"
        if descriptor_path.exists():
            descriptor = yaml.safe_load(descriptor_path.read_text(encoding="utf-8")) or {}
            if not isinstance(descriptor, dict):
                print("[FAIL] extension-descriptor.yaml must contain an object", flush=True)
                return 2
            if str(descriptor.get("extension_id") or "").strip() != extension_id:
                print(
                    "[FAIL] extension-descriptor.yaml extension_id does not match the requested bundle",
                    flush=True,
                )
                return 2
            descriptor["release_version"] = version
            descriptor_override = yaml.safe_dump(
                descriptor,
                sort_keys=False,
                allow_unicode=True,
            ).encode("utf-8")
    else:
        version = _manifest_version(bundle_dir)
    out_dir = Path(str(request.out_dir or "dist/extensions")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target_suffix = f"-{runtime_target}" if runtime_target else ""
    archive_name = f"{extension_id}-{version}{target_suffix}.zip"
    archive_path = out_dir / archive_name
    top_folder = f"{bundle_name}"
    native_executable: dict[str, str] | None = None
    if runtime_target and native_target_payload_paths(bundle_dir):
        from core.infrastructure.extension_runtime.native_package import (
            build_native_package_metadata,
        )

        try:
            native_executable = build_native_package_metadata(
                bundle_dir, runtime_target=runtime_target
            )
        except ValueError as exc:
            print(f"[FAIL] {exc}", flush=True)
            return 2
    # A target package without a native executable is one built per architecture
    # because its Python wheels are: it carries only the wheels this target runs.
    from core.infrastructure.extension_runtime.native_package import (
        wheel_supports_runtime_target,
    )

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle_dir)
            if any(part in _EXCLUDED_AUTHORING_DIRECTORIES for part in relative.parts):
                continue
            if path.name in {".fs-extension-install.yaml", ".fs-package.yaml"}:
                continue
            if (
                runtime_target
                and relative.parts[0] == "wheels"
                and not wheel_supports_runtime_target(relative.name, runtime_target)
            ):
                continue
            if runtime_target and "bin" in relative.parts:
                bin_index = relative.parts.index("bin")
                if (
                    len(relative.parts) <= bin_index + 1
                    or relative.parts[bin_index + 1] != runtime_target
                ):
                    continue
            if relative == Path(_TOKEN_EXTENSION_YAML) and manifest_override is not None:
                archive.writestr(str(Path(top_folder) / relative), manifest_override)
                continue
            if relative == Path("extension-descriptor.yaml") and descriptor_override is not None:
                archive.writestr(str(Path(top_folder) / relative), descriptor_override)
                continue
            archive.write(path, arcname=str(Path(top_folder) / relative))
        if runtime_target:
            package_metadata: dict[str, Any] = {"runtime_target": runtime_target}
            if native_executable is not None:
                package_metadata["native_executable"] = native_executable
            metadata = yaml.safe_dump(package_metadata, sort_keys=True).encode("utf-8")
            archive.writestr(str(Path(top_folder) / ".fs-package.yaml"), metadata)

    try:
        _validate_installer_archive_limits(archive_path)
    except ValueError as exc:
        archive_path.unlink(missing_ok=True)
        print(f"[FAIL] {exc}", flush=True)
        return 2

    digest = _sha256(archive_path)
    print(f"[PASS] packed: {archive_path}", flush=True)
    print(f"[PASS] sha256: {digest}", flush=True)
    return 0


def bundle_extension(request: ExtensionBundleRequest) -> ExtensionBundleResult:
    """Build an extension archive without invoking a CLI parser."""

    command = ExtensionBundleRequest.model_validate(request)
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = _bundle(command)
    return ExtensionBundleResult(
        status="bundled" if exit_code == 0 else "failed",
        extension_id=command.extension_id,
        exit_code=exit_code,
        output=output.getvalue(),
    )
