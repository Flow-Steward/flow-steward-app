"""Fail-closed runtime maintenance classification for HTTP ingress."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_RUNTIME_MAINTENANCE_MARKER = Path("/run/flow-steward-runtime/maintenance-state.json")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_SESSION_MUTATIONS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/switch-account",
    }
)
_SAFE_METHOD_SIDE_EFFECT_PATHS = frozenset(
    {
        "/api/integrations/oauth/callback",
        "/verify-email-change",
    }
)
_SAFE_METHOD_SIDE_EFFECT_PREFIXES = ("/webhook/extensions/",)


@dataclass(frozen=True)
class MaintenanceRequestDecision:
    allowed: bool
    code: str = ""


def runtime_maintenance_state(
    marker_path: Path = _RUNTIME_MAINTENANCE_MARKER,
) -> dict[str, object] | None:
    """Read the shared runtime marker without following symlinks."""
    marker = Path(marker_path)
    if marker.is_symlink():
        raise ValueError("runtime maintenance state must be a regular file")
    if not marker.exists():
        return None
    if not marker.is_file():
        raise ValueError("runtime maintenance state must be a regular file")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("runtime maintenance state must be an object")
        if payload.get("schema_version") != 1:
            raise ValueError("runtime maintenance state schema is unsupported")
        if payload.get("phase") not in {"draining", "enabled"}:
            raise ValueError("runtime maintenance phase is invalid")
        if payload.get("owner_kind") not in {"manual", "release"}:
            raise ValueError("runtime maintenance owner kind is invalid")
        owner_id = str(payload.get("owner_id") or "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", owner_id) is None:
            raise ValueError("runtime maintenance owner is invalid")
        if not str(payload.get("activated_at") or "").strip():
            raise ValueError("runtime maintenance activation time is invalid")
        return payload
    except (OSError, ValueError) as exc:
        raise ValueError("runtime maintenance state is invalid") from exc


def runtime_maintenance_active() -> bool:
    """Treat invalid state as active so mutation ingress fails closed."""
    try:
        return runtime_maintenance_state() is not None
    except ValueError:
        return True


def runtime_maintenance_status() -> dict[str, object]:
    """Return the public status while retaining fail-closed invalid-state behavior."""
    try:
        state = runtime_maintenance_state()
    except ValueError:
        return {"status": "enabled", "activated_at": None, "owner_kind": None}
    if state is None:
        return {"status": "disabled", "activated_at": None, "owner_kind": None}
    return {
        "status": state["phase"],
        "activated_at": state["activated_at"],
        "owner_kind": state["owner_kind"],
    }


def maintenance_request_decision(
    method: str,
    path: str,
    authenticated: bool,
) -> MaintenanceRequestDecision:
    """Classify one request while maintenance is draining or enabled."""
    del authenticated
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "").split("?", 1)[0]
    if normalized_method not in _SAFE_METHODS:
        if normalized_path in _ALLOWED_SESSION_MUTATIONS:
            return MaintenanceRequestDecision(allowed=True)
        return MaintenanceRequestDecision(
            allowed=False,
            code="maintenance_mode_enabled",
        )
    if normalized_method in {"GET", "HEAD"} and (
        normalized_path in _SAFE_METHOD_SIDE_EFFECT_PATHS
        or any(normalized_path.startswith(prefix) for prefix in _SAFE_METHOD_SIDE_EFFECT_PREFIXES)
    ):
        return MaintenanceRequestDecision(
            allowed=False,
            code="maintenance_mode_enabled",
        )
    return MaintenanceRequestDecision(allowed=True)


def build_maintenance_http_middleware(
    *,
    maintenance_active: Callable[[], bool] = runtime_maintenance_active,
) -> Callable[..., object]:
    """Build middleware that rejects before parsing or invoking a mutating route."""

    async def middleware(request: Request, call_next: Callable[..., object]) -> Response:
        if not maintenance_active():
            return await call_next(request)
        decision = maintenance_request_decision(
            request.method,
            request.url.path,
            authenticated=bool(getattr(request.state, "principal", None)),
        )
        if decision.allowed:
            return await call_next(request)
        return JSONResponse(
            {
                "error": {
                    "code": decision.code,
                    "message": "Maintenance Mode is enabled; retry later.",
                }
            },
            status_code=503,
            headers={"Retry-After": "60"},
        )

    return middleware


maintenance_http_middleware = build_maintenance_http_middleware()


__all__ = [
    "MaintenanceRequestDecision",
    "build_maintenance_http_middleware",
    "maintenance_http_middleware",
    "maintenance_request_decision",
    "runtime_maintenance_active",
    "runtime_maintenance_state",
    "runtime_maintenance_status",
]
