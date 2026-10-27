"""SEC-015/016: Security policy management and observability API.

Endpoints:
    GET  /api/security/policy                    — global policy
    PUT  /api/security/policy                    — update global policy (admin)
    GET  /api/security/projects/{id}/policy      — project policy (merged)
    PUT  /api/security/projects/{id}/policy      — project override (operator+)
    POST /api/security/projects/{id}/rules       — add custom rule
    GET  /api/security/posture                   — posture score
    GET  /api/security/events                    — event list (filtered, paginated)
    GET  /api/security/findings                  — findings list (filtered, paginated)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access
from core.application.management.scope import scope_from_principal
from core.application.security import policy_service
from core.application.security.observability_service import (
    empty_posture_payload,
    get_posture_payload,
)
from core.entrypoints.admin import deps
from core.entrypoints.api.error_safety import safe_error_response

ACCESS_DENIED_VALUE = "Access denied"
AUTHENTICATION_REQUIRED_MESSAGE = "Authentication required"
POLICY_STORAGE_UNAVAILABLE_MESSAGE = "Policy storage unavailable"
PROJECT_NOT_FOUND_MESSAGE = "Project not found"


logger = logging.getLogger(__name__)

router = APIRouter(tags=["security"])


def _request_principal(request: Request) -> dict | None:
    """Extract principal payload from request state (new auth context first)."""
    principal = getattr(request.state, "auth_principal", None)
    if isinstance(principal, Mapping):
        return dict(principal)
    if principal is not None and hasattr(principal, "model_dump"):
        try:
            dumped = principal.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass

    auth_user = getattr(request.state, "auth_user", None)
    if isinstance(auth_user, Mapping):
        return dict(auth_user)

    return None


def _require_auth_principal(request: Request) -> dict:
    """Raise 401 if unauthenticated."""
    principal = _request_principal(request)
    if not isinstance(principal, dict):
        raise _AuthError(AUTHENTICATION_REQUIRED_MESSAGE)
    return principal


class _AuthError(Exception):
    pass


def _request_account_id(request: Request) -> str | None:
    """Extract account_id from the resolved auth principal (if present)."""
    principal = _request_principal(request) or {}
    if isinstance(principal, dict):
        return str(principal.get("account_id") or "").strip() or None
    return None


def _scope_for_request(request: Request, *, project_id: str | None = None):
    """Build a ScopeContext from the current request principal."""
    principal = _require_auth_principal(request)
    return scope_from_principal(principal, project_id=project_id)


def _load_project_stub(project_id: str) -> dict | None:
    """Best-effort load of minimal project record for scope checks."""
    try:
        bl = deps.get_logger_only()
        if bl and hasattr(bl, "get_project"):
            return bl.get_project(project_id)
    except Exception:
        pass
    return None


def _project_account_id(project: Mapping[str, object] | None) -> str | None:
    if not isinstance(project, Mapping):
        return None
    return str(project.get("account_id") or "").strip() or None


def _is_admin(request: Request) -> bool:
    """Check if the current user has admin role."""
    principal = _request_principal(request) or {}
    roles = principal.get("roles") or []
    return "admin" in roles


def _has_operator_role(request: Request) -> bool:
    principal = _request_principal(request) or {}
    roles = {str(role or "").strip().lower() for role in (principal.get("roles") or [])}
    return bool(roles.intersection({"admin", "operator"}))


def _require_security_telemetry_access(request: Request, project_id: str | None) -> None:
    _require_auth_principal(request)
    if not _has_operator_role(request):
        raise PermissionError(ACCESS_DENIED_VALUE)
    if not project_id:
        if not _is_admin(request):
            raise PermissionError(ACCESS_DENIED_VALUE)
        return
    project = _load_project_stub(project_id)
    if not project:
        raise KeyError(PROJECT_NOT_FOUND_MESSAGE)
    require_project_access(_scope_for_request(request, project_id=project_id), project)


def _error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _canonicalize_submitted_shape(
    submitted: dict[str, object], canonical: dict[str, object]
) -> dict[str, object]:
    """Project overrides stay partial while values come from the validated DTO."""
    normalized: dict[str, object] = {}
    for key, submitted_value in submitted.items():
        canonical_value = canonical[key]
        if isinstance(submitted_value, dict) and isinstance(canonical_value, dict):
            normalized[key] = _canonicalize_submitted_shape(submitted_value, canonical_value)
        else:
            normalized[key] = canonical_value
    return normalized


# ---------------------------------------------------------------------------
# SEC-015: Policy management
# ---------------------------------------------------------------------------
@router.get("/api/security/policy")
async def get_global_policy(request: Request):
    """Return the global security policy."""
    if not _request_principal(request):
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    if not _is_admin(request):
        return _error("Admin role required", 403)
    account_id = _request_account_id(request)
    if not account_id:
        return _error("Account scope required", 403)

    from core.infrastructure.security.policy_loader import load_global_policy

    policy = load_global_policy(account_id)
    return policy.model_dump(mode="json")


@router.put("/api/security/policy")
async def update_global_policy(request: Request):
    """Update the global security policy (admin only)."""
    if not _is_admin(request):
        return _error("Admin role required", 403)
    account_id = _request_account_id(request)
    if not account_id:
        return _error("Account scope required", 403)

    body = await request.json()
    from core.infrastructure.security.policy import SecurityPolicy
    from core.infrastructure.security.policy_loader import validate_policy

    try:
        policy = SecurityPolicy(**body)
        errors = validate_policy(policy)
        if errors:
            return _error(f"Validation errors: {', '.join(errors)}")
    except Exception as e:
        return safe_error_response(logger, e, message="Invalid security policy", status_code=400)

    # Store in DB settings or YAML override
    try:
        bl = deps.get_logger_only()
        actor = str((_request_principal(request) or {}).get("username") or "system").strip()
        canonical_payload = policy.model_dump(mode="json")
        if not policy_service.persist_global_policy(
            bl,
            canonical_payload,
            actor=actor or "system",
            account_id=account_id,
        ):
            return _error(POLICY_STORAGE_UNAVAILABLE_MESSAGE, 503)
    except Exception as e:
        return safe_error_response(
            logger,
            e,
            message="Failed to persist global policy",
        )

    return {"status": "updated", "policy": policy.model_dump(mode="json")}


@router.get("/api/security/projects/{project_id}/policy")
async def get_project_policy(
    request: Request,
    project_id: str,
    include_sources: Annotated[bool, Query()] = False,
):
    """Return merged policy for a project (requires project access)."""
    try:
        scope = _scope_for_request(request, project_id=project_id)
        project = _load_project_stub(project_id)
        if not project:
            return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
        require_project_access(scope, project)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    from core.infrastructure.security.policy_loader import load_global_policy, merge_policies

    project_override = _get_project_security_override(project_id)
    global_policy = load_global_policy(_project_account_id(project))
    try:
        merged = merge_policies(global_policy, project_override)
    except Exception:
        return _error("Invalid project security policy configuration", 500)
    merged_payload = merged.model_dump(mode="json")
    if not include_sources:
        return merged_payload

    global_payload = global_policy.model_dump(mode="json")
    override_payload = project_override or {}
    source_map = _build_policy_source_map(global_payload, override_payload)
    return {
        "effective_policy": merged_payload,
        "global_policy": global_payload,
        "project_override": override_payload,
        "source_map": source_map,
    }


@router.put("/api/security/projects/{project_id}/policy")
async def update_project_policy(request: Request, project_id: str):
    """Update project-level security policy override (operator+ required)."""
    try:
        scope = _scope_for_request(request, project_id=project_id)
        project = _load_project_stub(project_id)
        if not project:
            return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
        require_project_access(scope, project)
        # Require operator or admin role for policy writes
        principal = _require_auth_principal(request)
        roles = set(principal.get("roles") or [])
        if not roles.intersection({"admin", "operator"}):
            return _error("Operator or admin role required", 403)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    body = await request.json()
    if not isinstance(body, dict):
        return _error("Project policy override must be a JSON object", 400)

    # Validate project override by merging onto effective global policy.
    try:
        from core.infrastructure.security.policy_loader import (
            load_global_policy,
            merge_policies,
            validate_policy,
        )

        merged = merge_policies(load_global_policy(_project_account_id(project)), body)
        errors = validate_policy(merged)
        if errors:
            return _error(f"Validation errors: {', '.join(errors)}", 400)
        canonical_override = _canonicalize_submitted_shape(body, merged.model_dump(mode="json"))
    except Exception as e:
        return safe_error_response(
            logger,
            e,
            message="Invalid project policy override",
            status_code=400,
        )

    old = _get_project_security_override(project_id) or {}

    try:
        save_result = _save_project_security_override(project_id, canonical_override)
        if save_result == "storage_unavailable":
            return _error(POLICY_STORAGE_UNAVAILABLE_MESSAGE, 503)
        if save_result == "not_found":
            return _error(PROJECT_NOT_FOUND_MESSAGE, 404)

        # Log policy change
        try:
            from core.infrastructure.security.event_logger import get_security_logger

            sec_logger = get_security_logger()
            if sec_logger:
                actor = (_request_principal(request) or {}).get("username", "unknown")
                sec_logger.log_policy_change(
                    project_id=project_id,
                    actor_id=actor,
                    before=old,
                    after=canonical_override,
                )
        except Exception:
            pass

    except Exception as e:
        return safe_error_response(logger, e, message="Failed to update project policy")

    return {"status": "updated", "project_id": project_id}


@router.post("/api/security/projects/{project_id}/rules")
async def add_custom_rule(request: Request, project_id: str):
    """Add a custom scanning rule for a project (operator+ required)."""
    try:
        scope = _scope_for_request(request, project_id=project_id)
        project = _load_project_stub(project_id)
        if not project:
            return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
        require_project_access(scope, project)
        principal = _require_auth_principal(request)
        roles = set(principal.get("roles") or [])
        if not roles.intersection({"admin", "operator"}):
            return _error("Operator or admin role required", 403)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    body = await request.json()
    from core.infrastructure.security.policy import CustomRule

    try:
        rule = CustomRule(**body)
    except Exception as e:
        return safe_error_response(logger, e, message="Invalid custom rule", status_code=400)

    # Append to project's custom rules
    override = _get_project_security_override(project_id) or {}
    rules = override.get("custom_rules", [])
    rules.append(rule.model_dump(mode="json"))
    override["custom_rules"] = rules

    try:
        save_result = _save_project_security_override(project_id, override)
        if save_result == "storage_unavailable":
            return _error(POLICY_STORAGE_UNAVAILABLE_MESSAGE, 503)
        if save_result == "not_found":
            return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
    except Exception as e:
        return safe_error_response(logger, e, message="Failed to save custom rule")

    return {"status": "added", "rule": rule.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# SEC-016: Security observability
# ---------------------------------------------------------------------------
@router.get("/api/security/posture")
async def get_posture(
    request: Request,
    project_id: Annotated[str | None, Query()] = None,
):
    """Get latest posture snapshot."""
    try:
        _require_security_telemetry_access(request, project_id)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except KeyError:
        return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    try:
        principal = _request_principal(request) or {}
        # Always scope global posture to the caller's account. Fall back to the
        # default account rather than passing None, which would otherwise let the
        # query return the newest snapshot across all accounts (FS-AUDIT-007).
        account_id = scope_from_principal(principal).account_id
        payload = get_posture_payload(project_id=project_id, account_id=account_id)
        if payload:
            return payload
        return empty_posture_payload(status="no_data")
    except Exception:
        return empty_posture_payload(status="error")


@router.get("/api/security/events")
async def list_events(
    request: Request,
    project_id: Annotated[str | None, Query()] = None,
    job_id: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    surface: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
):
    """List security events with filtering and pagination (account-scoped)."""
    try:
        _require_security_telemetry_access(request, project_id)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except KeyError:
        return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    try:
        from core.infrastructure.security.event_logger import get_security_logger

        sec_logger = get_security_logger()
        if not sec_logger:
            return {"events": [], "page": page, "per_page": per_page, "total": 0}

        account_id = _request_account_id(request)
        offset = (page - 1) * per_page
        events = sec_logger.list_events(
            account_id=account_id,
            project_id=project_id,
            job_id=job_id,
            severity=severity,
            surface=surface,
            limit=per_page,
            offset=offset,
        )
        # Serialize datetimes
        for e in events:
            if "created_at" in e and hasattr(e["created_at"], "isoformat"):
                e["created_at"] = e["created_at"].isoformat()
        return {"events": events, "page": page, "per_page": per_page}
    except Exception:
        return {"events": [], "page": page, "per_page": per_page, "error": "unavailable"}


@router.get("/api/security/findings")
async def list_findings(
    request: Request,
    project_id: Annotated[str | None, Query()] = None,
    event_id: Annotated[str | None, Query()] = None,
    scanner: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
):
    """List security findings with filtering and pagination (account-scoped)."""
    try:
        _require_security_telemetry_access(request, project_id)
    except _AuthError:
        return _error(AUTHENTICATION_REQUIRED_MESSAGE, 401)
    except KeyError:
        return _error(PROJECT_NOT_FOUND_MESSAGE, 404)
    except (PermissionError, ServiceForbiddenError):
        return _error(ACCESS_DENIED_VALUE, 403)

    try:
        from core.infrastructure.security.event_logger import get_security_logger

        sec_logger = get_security_logger()
        if not sec_logger:
            return {"findings": [], "page": page, "per_page": per_page}

        account_id = _request_account_id(request)
        offset = (page - 1) * per_page
        findings = sec_logger.list_findings(
            account_id=account_id,
            project_id=project_id,
            event_id=event_id,
            scanner=scanner,
            limit=per_page,
            offset=offset,
        )
        for f in findings:
            if "created_at" in f and hasattr(f["created_at"], "isoformat"):
                f["created_at"] = f["created_at"].isoformat()
        return {"findings": findings, "page": page, "per_page": per_page}
    except Exception:
        return {"findings": [], "page": page, "per_page": per_page, "error": "unavailable"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_project_security_override(project_id: str) -> dict | None:
    """Fetch security policy from project config with metadata fallback."""
    return policy_service.get_project_security_override(deps.get_logger_only(), project_id)


def _save_project_security_override(
    project_id: str, payload: dict
) -> policy_service.ProjectSecurityOverrideWriteResult:
    """Persist project security override under projects.config.security_policy."""
    return policy_service.update_project_security_override(
        deps.get_logger_only(),
        project_id,
        payload,
    )


def _build_policy_source_map(global_policy: dict, project_override: dict) -> dict[str, str]:
    """Return dotted-path -> source marker (`global` or `project`)."""
    source_map: dict[str, str] = {}

    def _walk(global_value, override_value, prefix: str) -> None:
        if isinstance(global_value, dict) or isinstance(override_value, dict):
            global_dict = global_value if isinstance(global_value, dict) else {}
            override_dict = override_value if isinstance(override_value, dict) else {}
            for key in sorted(set(global_dict.keys()) | set(override_dict.keys())):
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                _walk(global_dict.get(key), override_dict.get(key), child_prefix)
            return
        if isinstance(global_value, list) or isinstance(override_value, list):
            source_map[prefix] = "project" if override_value is not None else "global"
            return
        source_map[prefix] = "project" if override_value is not None else "global"

    _walk(global_policy, project_override, "")
    return source_map
