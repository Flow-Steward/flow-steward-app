from __future__ import annotations

from pathlib import Path
from typing import Any

from core.application.management.errors import ServiceValidationError

from .extension_descriptor import extract_llm_provider_contract
from .service_support import _normalize_text


def validate_managed_release_artifact(
    extension_id: str,
    descriptor: dict[str, Any],
    *,
    active_root: Path | None = None,
) -> None:
    descriptor_root = _normalize_text(descriptor.get("bundle_root"))
    if not descriptor_root:
        return
    from core.application.extensions.identity import extension_folder_name
    from core.infrastructure.extension_runtime.extension_runtime import (
        compile_extension_bundle,
        default_extensions_root,
    )

    resolved_active_root = active_root or (
        Path(default_extensions_root()) / extension_folder_name(extension_id)
    )
    managed_releases_root = resolved_active_root.parent / ".releases" / resolved_active_root.name
    try:
        is_managed_release = (
            Path(descriptor_root)
            .resolve(strict=False)
            .is_relative_to(managed_releases_root.resolve(strict=False))
        )
    except (OSError, RuntimeError, ValueError):
        is_managed_release = False
    if not is_managed_release:
        return
    try:
        release_root = resolved_active_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ServiceValidationError(
            "Managed extension release artifact is not available; "
            "reinstall or repair the extension before enabling."
        ) from exc
    if not resolved_active_root.is_symlink() or not release_root.is_dir():
        raise ServiceValidationError(
            "Managed extension release artifact is not available; "
            "reinstall or repair the extension before enabling."
        )
    compiled = compile_extension_bundle(
        resolved_active_root,
        logical_bundle_dir_name=extension_folder_name(extension_id),
    )
    if (
        _normalize_text(compiled.get("status")).lower() != "valid"
        or _normalize_text(compiled.get("extension_key")) != extension_id
    ):
        raise ServiceValidationError(
            "Managed extension release failed validation; "
            "reinstall or repair the extension before enabling."
        )


def validate_extension_activation(record: Any, release: Any | None) -> None:
    runtime_mode = _normalize_text(getattr(record, "runtime_mode", "")).lower()
    if runtime_mode == "remote_https":
        remote_base_url = _normalize_text(getattr(record, "remote_base_url", ""))
        if not remote_base_url:
            raise ServiceValidationError(
                "remote_base_url is required before activating a remote_https extension"
            )
        try:
            from .url_safety import assert_safe_remote_https_url

            assert_safe_remote_https_url(remote_base_url, purpose="remote_base_url")
        except Exception as exc:
            raise ServiceValidationError(f"remote_base_url is not allowed: {exc}") from exc

    if release is None:
        return
    descriptor = dict(getattr(release, "descriptor", {}) or {})
    validate_managed_release_artifact(
        _normalize_text(getattr(record, "extension_id", "")),
        descriptor,
    )
    contract = extract_llm_provider_contract(descriptor)
    if contract is None:
        return
    from core.application.settings.llm_provider_extension_service import (
        require_llm_provider_connection_config,
    )

    install_config = dict(getattr(record, "config", {}) or {})
    effective_config = install_config.get("llm_provider_connection_config")
    try:
        require_llm_provider_connection_config(
            contract,
            effective_config if isinstance(effective_config, dict) else {},
        )
    except ValueError as exc:
        raise ServiceValidationError(str(exc)) from exc


__all__ = ["validate_extension_activation", "validate_managed_release_artifact"]
