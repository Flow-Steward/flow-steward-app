#!/usr/bin/env python3
"""Authoring helpers for extension Python dependencies."""

from __future__ import annotations

import io
import os
import stat
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yaml

from core.application.extensions.identity import extension_folder_name, is_canonical_extension_id
from core.application.tooling.extensions.models import (
    ExtensionDependencyLockRequest,
    ExtensionDependencyLockResult,
)
from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root
from core.infrastructure.extension_runtime.python_dependencies import (
    lock_direct_requirements,
    resolve_wheel_platforms,
)
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
    normalize_python_requirements,
)


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("extension.yaml must be a YAML object")
    return payload


def _extension_bundle_root(*, extension_id: str, root: str) -> Path:
    bundle_name = (
        extension_folder_name(extension_id)
        if is_canonical_extension_id(extension_id)
        else extension_id
    )
    return Path(root or default_extensions_root()).resolve() / bundle_name


def _atomic_write_text(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, raw_temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp_path)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _lock(request: ExtensionDependencyLockRequest) -> int:
    extension_id = str(request.extension_id or "").strip()
    bundle_root = _extension_bundle_root(extension_id=extension_id, root=str(request.root or ""))
    manifest_path = bundle_root / "extension.yaml"
    if not manifest_path.is_file():
        print(f"[FAIL] extension manifest not found: {manifest_path}", flush=True)
        return 2
    input_path = Path(str(request.input or "").strip() or str(bundle_root / "requirements.in"))
    if not input_path.is_file():
        print(f"[FAIL] direct requirements file not found: {input_path}", flush=True)
        return 2
    platforms = resolve_wheel_platforms(str(request.platforms or ""))
    try:
        locked = lock_direct_requirements(input_path=input_path, platforms=platforms)
    except Exception as exc:
        print(f"[FAIL] dependency lock failed: {exc}", flush=True)
        return 2
    _normalized, issues = normalize_python_requirements(
        locked,
        manifest_path=str(manifest_path),
    )
    if issues:
        print(f"[FAIL] generated invalid lock: {issues[0].get('message')}", flush=True)
        return 2
    manifest = _load_manifest(manifest_path)
    manifest[PYTHON_REQUIREMENTS_FIELD] = locked
    _atomic_write_text(
        manifest_path,
        yaml.safe_dump(manifest, sort_keys=False),
    )
    print(
        f"[PASS] wrote {len(locked)} locked python requirement(s) to {manifest_path}",
        flush=True,
    )
    return 0


def lock_extension_dependencies(
    request: ExtensionDependencyLockRequest,
) -> ExtensionDependencyLockResult:
    """Resolve and write extension Python requirement locks without parsing CLI args."""

    command = ExtensionDependencyLockRequest.model_validate(request)
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = _lock(command)
    return ExtensionDependencyLockResult(
        status="locked" if exit_code == 0 else "failed",
        extension_id=command.extension_id,
        exit_code=exit_code,
        output=output.getvalue(),
    )
