from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext

from .binding_constants import (
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
    archive_too_large_message,
)
from .binding_context import ExtensionLifecycleContext
from .managed_release_service import ManagedReleaseService

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message


class ExtensionBindingProjectionService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        managed_release: ManagedReleaseService,
    ) -> None:
        self._context = context
        self._managed_release = managed_release

    def runtime_extension_payload(self, extension_detail: Any) -> dict[str, Any]:
        return self._extension_runtime_extension_payload(extension_detail)

    def runtime_extension_detail(
        self,
        *,
        bl: Any,
        extension_id: str,
        scope: ScopeContext | None = None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> Any:
        return self._extension_runtime_extension_detail(
            bl=bl,
            extension_id=extension_id,
            scope=scope,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def extension_version_from_payload(self, payload: Mapping[str, Any] | None) -> str:
        return self._extension_version_from_payload(payload)

    def callback_ids_from_payload(self, payload: Mapping[str, Any] | None) -> set[str]:
        return self._callback_ids_from_payload(payload)

    def reconcile_callback_registrations(
        self,
        *,
        bl: Any,
        extension_id: str,
        previous_callback_ids: set[str],
        current_callback_ids: set[str],
    ) -> None:
        self._reconcile_callback_registrations(
            bl=bl,
            extension_id=extension_id,
            previous_callback_ids=previous_callback_ids,
            current_callback_ids=current_callback_ids,
        )

    @staticmethod
    def _extension_runtime_extension_payload(extension_detail: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = (
            dict(getattr(extension_detail, "metadata", {}) or {})
            if isinstance(getattr(extension_detail, "metadata", {}), dict)
            else {}
        )
        release: dict[str, Any] = (
            metadata.get("release") if isinstance(metadata.get("release"), dict) else {}
        )
        descriptor: dict[str, Any] = (
            release.get("descriptor") if isinstance(release.get("descriptor"), dict) else {}
        )
        vendor: dict[str, Any] = (
            descriptor.get("vendor") if isinstance(descriptor.get("vendor"), dict) else {}
        )
        provenance: dict[str, Any] = (
            release.get("provenance") if isinstance(release.get("provenance"), dict) else {}
        )
        raw_origin = (
            str(
                getattr(extension_detail, "descriptor_origin", "")
                or provenance.get("origin")
                or (
                    (descriptor.get("provenance") or {}).get("origin")
                    if isinstance(descriptor, dict)
                    else ""
                )
                or "imported"
            )
            .strip()
            .lower()
        )
        state = str(getattr(extension_detail, "state", "") or "active").strip().lower()
        extension_id = str(getattr(extension_detail, "extension_id", "") or "").strip()
        version = str(getattr(extension_detail, "version", "") or "").strip()
        release_id = str(getattr(extension_detail, "release_id", "") or "").strip()
        install_id = str(getattr(extension_detail, "install_id", "") or "").strip()
        vendor_name = str(vendor.get("vendor_name") or vendor.get("name") or "").strip()
        return {
            "extension_id": extension_id,
            "extension_key": extension_id,
            "display_name": str(
                getattr(extension_detail, "display_name", "") or extension_id
            ).strip(),
            "description": str(getattr(extension_detail, "description", "") or "").strip(),
            "version": version,
            "state": "active" if state == "active" else "disabled",
            "descriptor_origin": raw_origin,
            "author": vendor_name,
            "vendor_name": vendor_name,
            "support_url": str(vendor.get("support_url") or "").strip(),
            "website_url": str(vendor.get("website_url") or vendor.get("url") or "").strip(),
            "installed_via": "extension_descriptor",
            "release_id": release_id,
            "extension_install_id": install_id,
            "install": {
                "status": "installed" if state == "active" else "disabled",
                "extension_id": extension_id,
                "version": version,
                "account_id": "",
                "install_id": install_id,
                "release_id": release_id,
            },
        }

    def _extension_runtime_extension_detail(
        self,
        *,
        bl: Any,
        extension_id: str,
        scope: ScopeContext | None = None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> tuple[dict[str, Any] | None, Any | None]:
        try:
            extension_detail = self._context.query_service(
                bl,
                install_store=install_store,
                runtime_store=runtime_store,
            ).get_extension(extension_id, scope=scope)
        except ServiceNotFoundError:
            return None, None
        if not bool(getattr(extension_detail, "has_runtime_module", False)):
            return None, None
        return self._extension_runtime_extension_payload(extension_detail), extension_detail

    @staticmethod
    def _extension_version_from_payload(payload: Mapping[str, Any] | None) -> str:
        sources: list[dict[str, Any]] = []
        root = dict(payload or {})
        sources.append(root)
        for key in ("compiled_manifest", "manifest", "descriptor", "identity"):
            nested = root.get(key)
            if isinstance(nested, dict):
                sources.append(dict(nested))
        for source in sources:
            version = str(source.get("version") or source.get("release_version") or "").strip()
            if version:
                return version
        return ""

    @staticmethod
    def _callback_ids_from_payload(payload: Mapping[str, Any] | None) -> set[str]:
        sources: list[dict[str, Any]] = []
        root = dict(payload or {})
        sources.append(root)
        for key in ("compiled_manifest", "manifest", "descriptor"):
            nested = root.get(key)
            if isinstance(nested, dict):
                sources.append(dict(nested))
        callback_ids: set[str] = set()
        for source in sources:
            callbacks = source.get("callbacks")
            if not isinstance(callbacks, list):
                continue
            for item in callbacks:
                if not isinstance(item, dict):
                    continue
                callback_id = str(item.get("callback_id") or "").strip()
                if callback_id:
                    callback_ids.add(callback_id)
        return callback_ids

    def _reconcile_callback_registrations(
        self,
        *,
        bl: Any,
        extension_id: str,
        previous_callback_ids: set[str],
        current_callback_ids: set[str],
    ) -> None:
        if not hasattr(bl, "list_extension_callback_registrations"):
            return
        removed_callback_ids = previous_callback_ids - current_callback_ids
        if not removed_callback_ids:
            return
        if not hasattr(bl, "update_extension_callback_registration_state"):
            return
        for callback_id in removed_callback_ids:
            registrations = bl.list_extension_callback_registrations(
                extension_id=extension_id,
                callback_id=callback_id,
            )
            for registration in registrations or []:
                registration_id = str((registration or {}).get("id") or "").strip()
                if not registration_id:
                    continue
                bl.update_extension_callback_registration_state(
                    registration_id=registration_id,
                    enabled=False,
                    error="Callback removed by extension update",
                )


__all__ = ["ExtensionBindingProjectionService"]
