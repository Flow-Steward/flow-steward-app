"""Process-local CSP authority for installed extension browser contracts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

_CSP_DIRECTIVES = (
    "script-src",
    "connect-src",
    "frame-src",
)
_PICKER_PERMISSION_KEYS = {
    "script-src": "allowed_script_origins",
    "connect-src": "allowed_connect_origins",
    "frame-src": "allowed_frame_origins",
}
_EMPTY_PERMISSIONS = dict.fromkeys(_CSP_DIRECTIVES, ())


def _picker_declarations(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        declaration = value.get("external_resource_picker")
        if isinstance(declaration, Mapping):
            yield declaration
        if str(value.get("type") or "").strip() == "external_resource_picker":
            data = value.get("data")
            if isinstance(data, Mapping):
                yield data
        for nested in value.values():
            yield from _picker_declarations(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _picker_declarations(nested)


def _closed_https_origin(value: Any) -> str | None:
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        _ = parsed.port
    except ValueError:
        return None
    return value


def browser_permissions(manifests: Iterator[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Return closed HTTPS origins; malformed picker declarations contribute nothing."""
    origins = {directive: set() for directive in _CSP_DIRECTIVES}
    for manifest in manifests:
        for declaration in _picker_declarations(manifest):
            declaration_origins: dict[str, list[str]] = {}
            valid = True
            for directive, key in _PICKER_PERMISSION_KEYS.items():
                if key not in declaration:
                    continue
                raw_origins = declaration.get(key)
                if not isinstance(raw_origins, list):
                    valid = False
                    break
                parsed_origins = [_closed_https_origin(origin) for origin in raw_origins]
                if any(origin is None for origin in parsed_origins):
                    valid = False
                    break
                declaration_origins[directive] = [
                    origin for origin in parsed_origins if origin is not None
                ]
            if valid:
                for directive, values in declaration_origins.items():
                    origins[directive].update(values)
    return {directive: tuple(sorted(origins[directive])) for directive in _CSP_DIRECTIVES}


def _active_installed_manifests(*, installs: Any, runtime: Any) -> Iterator[Mapping[str, Any]]:
    list_active = getattr(installs, "list_active_installs", None)
    get_release = getattr(runtime, "get_release", None)
    if not callable(list_active) or not callable(get_release):
        return
    try:
        active_installs = list_active() or []
    except Exception:
        return
    for install in active_installs:
        extension_id = str(getattr(install, "extension_id", "") or "").strip()
        install_id = str(getattr(install, "install_id", "") or "").strip()
        release_id = str(getattr(install, "release_id", "") or "").strip()
        state = str(getattr(install, "state", "") or "").strip().lower()
        if not extension_id or not install_id or not release_id or state != "active":
            continue
        try:
            release = get_release(release_id)
        except Exception:
            continue
        if str(getattr(release, "extension_id", "") or "").strip() != extension_id:
            continue
        descriptor = getattr(release, "descriptor", None)
        if not isinstance(descriptor, Mapping):
            continue
        manifest = descriptor.get("compiled_manifest")
        if not isinstance(manifest, Mapping):
            manifest = descriptor
        if str(manifest.get("extension_id") or "").strip() != extension_id:
            continue
        yield manifest


class ExtensionBrowserPermissionProjection:
    """Cache CSP origins derived exclusively from active installed releases."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._permissions = dict(_EMPTY_PERMISSIONS)

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._permissions)

    def refresh(self, *, installs: Any, runtime: Any) -> dict[str, tuple[str, ...]]:
        permissions = browser_permissions(
            _active_installed_manifests(installs=installs, runtime=runtime)
        )
        with self._lock:
            self._permissions = permissions
            return dict(self._permissions)

    def invalidate(self) -> None:
        with self._lock:
            self._permissions = dict(_EMPTY_PERMISSIONS)


_projection = ExtensionBrowserPermissionProjection()


def extension_browser_permission_snapshot() -> dict[str, tuple[str, ...]]:
    """Read the cached CSP authority without filesystem or database access."""
    return _projection.snapshot()


def refresh_extension_browser_permissions(
    *, installs: Any, runtime: Any
) -> dict[str, tuple[str, ...]]:
    """Replace cached CSP authority after an install lifecycle transition."""
    return _projection.refresh(installs=installs, runtime=runtime)


def refresh_browser_permissions_for_lifecycle(*, bl: Any, lifecycle_context: Any) -> None:
    """Refresh after a committed lifecycle change, clearing authority on any failure."""
    try:
        if bl is None:
            raise RuntimeError("Extension lifecycle storage is unavailable")
        refresh_extension_browser_permissions(
            installs=lifecycle_context.install_store(bl),
            runtime=lifecycle_context.runtime_store(bl),
        )
    except Exception:
        invalidate_extension_browser_permissions()


def refresh_browser_permissions_from_stores(
    *, get_install_store: Any, get_runtime_store: Any
) -> None:
    """Refresh after a transport-owned lifecycle command, failing closed on errors."""
    try:
        refresh_extension_browser_permissions(
            installs=get_install_store(),
            runtime=get_runtime_store(),
        )
    except Exception:
        invalidate_extension_browser_permissions()


def invalidate_extension_browser_permissions() -> None:
    """Fail closed if an authoritative lifecycle refresh cannot be completed."""
    _projection.invalidate()


__all__ = [
    "ExtensionBrowserPermissionProjection",
    "browser_permissions",
    "extension_browser_permission_snapshot",
    "invalidate_extension_browser_permissions",
    "refresh_browser_permissions_for_lifecycle",
    "refresh_browser_permissions_from_stores",
    "refresh_extension_browser_permissions",
]
