"""Internal platform contracts for optional module seam injection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

_COMMUNITY_ENTITLEMENTS: dict[str, bool] = {
    "external_sign_in": False,
    "identity_access": False,
    "audit_export": False,
    "extension_governance": False,
    "private_extension_catalog": False,
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_role_set(
    values: Iterable[str] | None, *, fallback: tuple[str, ...]
) -> frozenset[str]:
    items = list(values or fallback)
    normalized = {_normalize_text(item).lower() for item in items if _normalize_text(item)}
    return frozenset(normalized or {item.lower() for item in fallback})


def _user_roles(auth_user: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(auth_user, Mapping):
        return set()
    roles = auth_user.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return {_normalize_text(role).lower() for role in roles if _normalize_text(role)}


class AuthorizationService(Protocol):
    def is_allowed(
        self, permission: str, *, auth_user: Mapping[str, Any] | None = None
    ) -> bool: ...

    def can_view_settings(self, auth_user: Mapping[str, Any] | None = None) -> bool: ...

    def can_manage_users(self, auth_user: Mapping[str, Any] | None = None) -> bool: ...

    def can_manage_service_accounts(self, auth_user: Mapping[str, Any] | None = None) -> bool: ...

    def can_manage_identity_integrations(
        self, auth_user: Mapping[str, Any] | None = None
    ) -> bool: ...

    def can_view_private_settings_page(
        self, auth_user: Mapping[str, Any] | None = None
    ) -> bool: ...


@dataclass(slots=True)
class CommunityAuthorizationService:
    """Community-default authorization wrapper over existing role behavior."""

    admin_roles: frozenset[str] = field(default_factory=lambda: frozenset({"admin"}))
    operator_roles: frozenset[str] = field(default_factory=lambda: frozenset({"operator"}))
    viewer_roles: frozenset[str] = field(default_factory=lambda: frozenset({"viewer"}))
    allow_anonymous_admin_surfaces: bool = True

    def _anonymous_allowed(self, auth_user: Mapping[str, Any] | None) -> bool:
        return auth_user is None and self.allow_anonymous_admin_surfaces

    def _check_admin_access(self, auth_user: Mapping[str, Any] | None) -> bool:
        if self._anonymous_allowed(auth_user):
            return True
        return bool(_user_roles(auth_user) & self.admin_roles)

    def can_view_settings(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        return self._check_admin_access(auth_user)

    def can_manage_users(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        return self._check_admin_access(auth_user)

    def can_manage_service_accounts(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        return self._check_admin_access(auth_user)

    def can_manage_identity_integrations(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        return self._check_admin_access(auth_user)

    def can_view_private_settings_page(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        return self._check_admin_access(auth_user)

    def is_allowed(self, permission: str, *, auth_user: Mapping[str, Any] | None = None) -> bool:
        normalized = _normalize_text(permission).lower()
        if not normalized or normalized in {"always", "public"}:
            return True
        if normalized == "authenticated":
            return isinstance(auth_user, Mapping)

        decision_map = {
            "view_settings": self.can_view_settings,
            "manage_users": self.can_manage_users,
            "can_manage_users": self.can_manage_users,
            "manage_service_accounts": self.can_manage_service_accounts,
            "can_manage_service_accounts": self.can_manage_service_accounts,
            "manage_identity_integrations": self.can_manage_identity_integrations,
            "can_manage_identity_integrations": self.can_manage_identity_integrations,
            "can_view_private_settings_page": self.can_view_private_settings_page,
            "view_private_settings_page": self.can_view_private_settings_page,
        }
        decision = decision_map.get(normalized)
        if decision is not None:
            return bool(decision(auth_user))

        roles = _user_roles(auth_user)
        if normalized == "operator":
            if self._anonymous_allowed(auth_user):
                return True
            return bool(roles & (self.admin_roles | self.operator_roles))
        if normalized == "viewer":
            if self._anonymous_allowed(auth_user):
                return True
            return bool(roles & (self.admin_roles | self.operator_roles | self.viewer_roles))
        return False


class EntitlementService(Protocol):
    def is_enabled(self, feature: str) -> bool: ...

    def snapshot(self) -> dict[str, bool]: ...


@dataclass(slots=True)
class CommunityEntitlementService:
    """Community entitlements are explicit and deterministic."""

    feature_flags: dict[str, bool] = field(default_factory=lambda: dict(_COMMUNITY_ENTITLEMENTS))

    def is_enabled(self, feature: str) -> bool:
        return bool(self.feature_flags.get(_normalize_text(feature).lower(), False))

    def snapshot(self) -> dict[str, bool]:
        return dict(sorted(self.feature_flags.items()))


class AuditSink(Protocol):
    def emit(
        self, *, event_type: str, payload: Mapping[str, Any] | None = None, logger: Any = None
    ) -> None: ...


@dataclass(slots=True)
class DomainEventAuditSink:
    """Default sink keeps Community behavior on local runtime logging."""

    source: str = "platform"

    def emit(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        logger: Any = None,
    ) -> None:
        if logger is None:
            return
        event_name = _normalize_text(event_type) or "audit.event"
        event_payload = dict(payload or {})
        if hasattr(logger, "log_domain_event"):
            logger.log_domain_event(event_name, payload=event_payload, source=self.source)
            return
        if hasattr(logger, "info"):
            logger.info("audit_event type=%s payload=%s", event_name, event_payload)


class SecretProvider(Protocol):
    def save_provider_secret(
        self,
        logger: Any,
        *,
        provider_key: str,
        secret_key: str,
        secret_value: str,
        project_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool: ...

    def get_provider_secret(
        self,
        logger: Any,
        *,
        provider_key: str,
        secret_key: str,
        project_id: str | None = None,
    ) -> str: ...


@dataclass(slots=True)
class InDatabaseSecretProvider:
    """Default secret provider delegates to Flow Steward logger storage methods."""

    def save_provider_secret(
        self,
        logger: Any,
        *,
        provider_key: str,
        secret_key: str,
        secret_value: str,
        project_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        save_secret = getattr(logger, "save_provider_secret", None)
        if not callable(save_secret):
            return False
        return bool(
            save_secret(
                provider_key=provider_key,
                secret_key=secret_key,
                secret_value=secret_value,
                project_id=project_id,
                metadata=dict(metadata or {}),
            )
        )

    def get_provider_secret(
        self,
        logger: Any,
        *,
        provider_key: str,
        secret_key: str,
        project_id: str | None = None,
    ) -> str:
        get_secret = getattr(logger, "get_provider_secret", None)
        if not callable(get_secret):
            return ""
        value = get_secret(
            provider_key=provider_key,
            secret_key=secret_key,
            project_id=project_id,
        )
        return str(value or "")


class ExtensionGovernancePolicy(Protocol):
    def allow_extension_install(
        self, *, extension_id: str, actor: Mapping[str, Any] | None = None
    ) -> tuple[bool, str]: ...

    def allow_remote_base_url(
        self,
        *,
        remote_base_url: str,
        extension_id: str = "",
        actor: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]: ...

    def allow_domain(
        self,
        *,
        domain: str,
        purpose: str = "",
        actor: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]: ...


@dataclass(slots=True)
class CommunityExtensionGovernancePolicy:
    """Community default is permissive by design."""

    def allow_extension_install(
        self, *, extension_id: str, actor: Mapping[str, Any] | None = None
    ) -> tuple[bool, str]:
        _ = (extension_id, actor)
        return True, ""

    def allow_remote_base_url(
        self,
        *,
        remote_base_url: str,
        extension_id: str = "",
        actor: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        _ = (remote_base_url, extension_id, actor)
        return True, ""

    def allow_domain(
        self,
        *,
        domain: str,
        purpose: str = "",
        actor: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        _ = (domain, purpose, actor)
        return True, ""


def build_platform_contracts(
    *,
    admin_roles: Iterable[str] | None = None,
    operator_roles: Iterable[str] | None = None,
    viewer_roles: Iterable[str] | None = None,
    entitlement_overrides: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    entitlements = dict(_COMMUNITY_ENTITLEMENTS)
    for key, enabled in dict(entitlement_overrides or {}).items():
        normalized = _normalize_text(key).lower()
        if not normalized:
            continue
        entitlements[normalized] = bool(enabled)

    authorization_service = CommunityAuthorizationService(
        admin_roles=_normalize_role_set(admin_roles, fallback=("admin",)),
        operator_roles=_normalize_role_set(operator_roles, fallback=("operator",)),
        viewer_roles=_normalize_role_set(viewer_roles, fallback=("viewer",)),
    )
    entitlement_service = CommunityEntitlementService(feature_flags=entitlements)
    return {
        "authorization_service": authorization_service,
        "entitlement_service": entitlement_service,
        "audit_sink": DomainEventAuditSink(),
        "secret_provider": InDatabaseSecretProvider(),
        "extension_governance_policy": CommunityExtensionGovernancePolicy(),
    }


__all__ = [
    "AuditSink",
    "AuthorizationService",
    "CommunityAuthorizationService",
    "CommunityEntitlementService",
    "CommunityExtensionGovernancePolicy",
    "DomainEventAuditSink",
    "EntitlementService",
    "ExtensionGovernancePolicy",
    "InDatabaseSecretProvider",
    "SecretProvider",
    "build_platform_contracts",
]
