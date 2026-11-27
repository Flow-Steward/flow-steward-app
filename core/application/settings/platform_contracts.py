"""Internal platform contracts for optional module seam injection."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from core.application.management.errors import ServiceForbiddenError

_LOG = logging.getLogger(__name__)

#: The machine code an API refusal carries when the account lacks a feature.
FEATURE_NOT_ENTITLED_CODE = "feature_not_entitled"
#: The capability a platform module declares in its descriptor (``"provides"``) to be
#: the deployment's entitlement source (ADR 0082). The module loader carries the same
#: name as ``core.infrastructure.platform_modules.loader.ENTITLEMENT_SOURCE_CAPABILITY``.
ENTITLEMENT_SOURCE_CAPABILITY = "entitlement_source"
#: The page permission a registered page declares to be shown only to an
#: installation administrator (ADR 0082 §3b). The authorization service decides it
#: with its installation-administrator predicate.
INSTALLATION_ADMIN_PERMISSION = "installation_admin"


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_entitlement_name(value: Any) -> str:
    """The canonical spelling of a feature or limit name: trimmed and lowercase."""
    return _normalize_text(value).lower()


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
    #: Decides whether a signed-in user administers the whole installation rather
    #: than one account. ``None`` falls back to the admin role check; the app
    #: composition passes ``core.application.auth.installation_admin.is_installation_admin``.
    installation_admin_predicate: Callable[[Mapping[str, Any]], bool] | None = None

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

    def can_administer_installation(self, auth_user: Mapping[str, Any] | None = None) -> bool:
        """Whether the user administers the installation, not only their own account."""
        if self._anonymous_allowed(auth_user):
            return True
        if not isinstance(auth_user, Mapping):
            return False
        predicate = self.installation_admin_predicate
        if predicate is None:
            return self._check_admin_access(auth_user)
        try:
            return bool(predicate(auth_user))
        except Exception:
            return False

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
            INSTALLATION_ADMIN_PERMISSION: self.can_administer_installation,
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


class EntitlementSource(Protocol):
    """Supplies features and numeric limits; it never decides (ADR 0082, section 3)."""

    def features_for(self, account_id: str) -> Collection[str]: ...

    def limits_for(self, account_id: str) -> Mapping[str, int]: ...


class EntitlementService(Protocol):
    def is_entitled(self, account_id: str, feature: str) -> bool: ...

    def limit(self, account_id: str, name: str) -> int | None: ...

    def snapshot(self, account_id: str) -> Mapping[str, bool]: ...

    def register_entitlement_source(self, source: EntitlementSource) -> None: ...


class EntitlementSourceError(Exception):
    """A registered source raised or answered with something that is not a valid answer."""


class EntitlementSourceRefusedError(PermissionError):
    """Core refused to register an entitlement source (ADR 0082).

    Raised to the registering code: a platform module that did not declare the
    ``entitlement_source`` capability, a second source while one is registered, or
    any registration after module loading finished. A module whose hook raises it
    fails to load and is rolled back like any other failing module.
    """


class FeatureNotEntitledError(ServiceForbiddenError):
    """An API request needs a feature the caller's account is not entitled to."""

    def __init__(self, feature: str) -> None:
        self.feature = normalize_entitlement_name(feature)
        message = f"This account is not entitled to the feature '{self.feature}'"
        super().__init__(
            message,
            payload={
                "code": FEATURE_NOT_ENTITLED_CODE,
                "message": message,
                "details": {"feature": self.feature},
            },
        )


def _source_features(source: EntitlementSource, account_id: str) -> frozenset[str]:
    raw = source.features_for(account_id)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Collection):
        raise EntitlementSourceError("features_for must return a collection of names")
    names = set()
    for item in raw:
        if not isinstance(item, str):
            raise EntitlementSourceError("features_for returned a name that is not a string")
        name = normalize_entitlement_name(item)
        if name:
            names.add(name)
    return frozenset(names)


def _source_limits(source: EntitlementSource, account_id: str) -> dict[str, int]:
    raw = source.limits_for(account_id)
    if not isinstance(raw, Mapping):
        raise EntitlementSourceError("limits_for must return a mapping")
    limits: dict[str, int] = {}
    for key, value in raw.items():
        name = normalize_entitlement_name(key) if isinstance(key, str) else ""
        if not name or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EntitlementSourceError("limits_for must map names to non-negative integers")
        limits[name] = value
    return limits


class CoreEntitlementService:
    """The only decider of feature entitlements and numeric limits, per account.

    Exactly one :class:`EntitlementSource` supplies features and limits; the
    service asks it and decides. With no source registered nothing is entitled and
    every limit is ``None``, so callers keep their instance default. ``None``
    therefore means "no source speaks for this limit", never "a source failed".

    Rules:

    - One designated source (ADR 0082). While Core's module loader runs a platform
      module's hook it opens :meth:`platform_module_scope` with the capabilities the
      module declared in its descriptor; only a module that declares
      ``entitlement_source`` may register. A second source, from any module, is
      refused, so no module can grant itself a feature or raise a limit beside the
      designated source. After loading, the composition root calls :meth:`seal`
      and every later registration is refused. Outside any scope and before the
      seal (Core's own composition, tests) one source may be registered directly.
      Every refusal raises :class:`EntitlementSourceRefusedError`.
    - A feature is entitled for an account when the source lists it for that
      account; a limit is the value the source gives, ``None`` when it gives none.
    - The source can be registered but never replaced or removed. Registering the
      same object again is a no-op. The one exception is Core's module loader,
      which rolls back the source of a platform module whose loading failed
      (:meth:`restore_loading_checkpoint`) while the role starts.
    - A source that raises, or answers with an invalid shape, fails the whole call
      closed: ``is_entitled`` answers ``False``, ``limit`` answers ``0`` (nothing
      allowed) and ``snapshot`` answers an empty mapping. ``limit`` must not answer
      ``None`` here: callers read ``None`` as "use the instance default", which is
      wider than any limit a source narrowed below it. The failure is logged once
      per source.
    - An empty account id (an anonymous request) is never entitled.
    - When a platform module failed to load in this role, Core's loader marks the
      service degraded (:meth:`mark_loading_degraded`): the failed module may have
      been the source of a feature, so a "not entitled" answer is no longer
      definite. ``is_entitled`` still answers ``False`` (deny), but
      :meth:`entitlement_status` answers ``None`` (unknown) instead of ``False``, so
      a caller that would destroy data on ``False`` holds off.

    Feature and limit names are compared trimmed and lowercase.
    """

    __slots__ = (
        "_degraded_by",
        "_failed_sources",
        "_lock",
        "_scope",
        "_sealed",
        "_source_owner",
        "_sources",
    )

    def __init__(self) -> None:
        self._sources: tuple[EntitlementSource, ...] = ()
        self._source_owner: str | None = None
        self._failed_sources: set[int] = set()
        self._degraded_by: tuple[str, ...] = ()
        self._scope: tuple[str | None, frozenset[str]] | None = None
        self._sealed = False
        self._lock = threading.Lock()

    @contextmanager
    def platform_module_scope(
        self, module_id: str | None, provides: Collection[str] = ()
    ) -> Iterator[None]:
        """Attribute registrations to one platform module while its hook runs (ADR 0082).

        Only Core's module loader opens it. ``module_id`` is the identity Core loaded
        the module under (``None`` for a module listed by
        ``FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES``, which has none) and ``provides``
        the capabilities its descriptor declares.
        """
        scope = (
            _normalize_text(module_id) or None,
            frozenset(_normalize_text(item).lower() for item in provides),
        )
        with self._lock:
            previous = self._scope
            self._scope = scope
        try:
            yield
        finally:
            with self._lock:
                self._scope = previous

    def seal(self) -> None:
        """Refuse every registration from now on: module loading has finished."""
        with self._lock:
            self._sealed = True

    @property
    def source_owner(self) -> str | None:
        """The platform module that registered the source; ``None`` when none did."""
        return self._source_owner

    def mark_loading_degraded(self, reason: str) -> None:
        """Record that a platform module failed to load in this role (ADR 0082).

        Only Core's module loader calls it. It never clears: a role whose module set
        loaded only partly stays degraded until it restarts.
        """
        text = _normalize_text(reason) or "unknown platform module"
        with self._lock:
            if text not in self._degraded_by:
                self._degraded_by = (*self._degraded_by, text)

    @property
    def loading_degraded(self) -> tuple[str, ...]:
        """The modules that failed to load in this role; empty when none did."""
        return self._degraded_by

    def register_entitlement_source(self, source: EntitlementSource) -> None:
        if not callable(getattr(source, "features_for", None)) or not callable(
            getattr(source, "limits_for", None)
        ):
            raise TypeError("an entitlement source needs features_for and limits_for")
        with self._lock:
            if any(existing is source for existing in self._sources):
                return
            owner = self._registering_owner()
            self._sources = (*self._sources, source)
            self._source_owner = owner

    def _registering_owner(self) -> str | None:
        """The module allowed to register now, or raise; the caller holds the lock."""
        owner: str | None = None
        if self._scope is not None:
            owner, provides = self._scope
            if ENTITLEMENT_SOURCE_CAPABILITY not in provides:
                who = (
                    f"platform module '{owner}'"
                    if owner
                    else "a module without a platform module identity"
                )
                raise EntitlementSourceRefusedError(
                    f"{who} does not declare the '{ENTITLEMENT_SOURCE_CAPABILITY}' "
                    "capability in its descriptor; only the designated entitlement "
                    "source may register one"
                )
        elif self._sealed:
            raise EntitlementSourceRefusedError(
                "platform module loading has finished; no entitlement source can be "
                "registered any more"
            )
        if self._sources:
            holder = f" by '{self._source_owner}'" if self._source_owner else ""
            raise EntitlementSourceRefusedError(
                f"an entitlement source is already registered{holder}; Core accepts "
                "exactly one entitlement source"
            )
        return owner

    def loading_checkpoint(self) -> tuple[EntitlementSource, ...]:
        """The registered sources, for the module loader's rollback (ADR 0082)."""
        with self._lock:
            return self._sources

    def restore_loading_checkpoint(self, checkpoint: tuple[EntitlementSource, ...]) -> None:
        """Drop every source registered after ``checkpoint`` was taken.

        Only Core's module loader calls it, when a platform module fails to load, so
        an entitlement source that module registered never decides anything.
        """
        kept = tuple(checkpoint)
        with self._lock:
            self._sources = tuple(
                source for source in self._sources if any(source is old for old in kept)
            )
            if not self._sources:
                self._source_owner = None

    def _report_failure(self, source: EntitlementSource, exc: Exception) -> None:
        with self._lock:
            if id(source) in self._failed_sources:
                return
            self._failed_sources.add(id(source))
        _LOG.warning(
            "entitlement source %s failed (%s); answering not entitled",
            type(source).__name__,
            type(exc).__name__,
        )

    def _entitled_features(self, account_id: str) -> frozenset[str] | None:
        """The union of every source's features, or ``None`` when a source failed."""
        features: set[str] = set()
        for source in self._sources:
            try:
                features |= _source_features(source, account_id)
            except Exception as exc:
                self._report_failure(source, exc)
                return None
        return frozenset(features)

    def is_entitled(self, account_id: str, feature: str) -> bool:
        account = _normalize_text(account_id)
        name = normalize_entitlement_name(feature)
        if not account or not name:
            return False
        features = self._entitled_features(account)
        return bool(features is not None and name in features)

    def entitlement_status(self, account_id: str, feature: str) -> bool | None:
        """Like :meth:`is_entitled`, but ``None`` when the answer is not definite.

        ``True`` and ``False`` are definite answers (``False`` includes "no source
        is registered" when every platform module of the role loaded); ``None``
        means the answer could not be established: a source failed, or a platform
        module failed to load in this role (:meth:`mark_loading_degraded`) and no
        remaining source grants the feature. A caller that must not act on a
        fail-closed ``False``, such as a purge that would delete what an
        entitlement keeps, uses this instead of :meth:`is_entitled`.
        """
        account = _normalize_text(account_id)
        name = normalize_entitlement_name(feature)
        if not account or not name:
            return False
        features = self._entitled_features(account)
        if features is None:
            return None
        if name in features:
            return True
        return None if self._degraded_by else False

    def limit(self, account_id: str, name: str) -> int | None:
        """The account's limit; ``None`` when no source defines it, ``0`` on a source failure."""
        account = _normalize_text(account_id)
        limit_name = normalize_entitlement_name(name)
        if not account or not limit_name:
            return None
        values: list[int] = []
        for source in self._sources:
            try:
                limits = _source_limits(source, account)
            except Exception as exc:
                self._report_failure(source, exc)
                return 0
            if limit_name in limits:
                values.append(limits[limit_name])
        return max(values) if values else None

    def snapshot(self, account_id: str) -> Mapping[str, bool]:
        """The entitled features of an account for the browser; a missing key is ``False``."""
        account = _normalize_text(account_id)
        features = self._entitled_features(account) if account else frozenset()
        return MappingProxyType(dict.fromkeys(sorted(features or ()), True))


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
    installation_admin_predicate: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    authorization_service = CommunityAuthorizationService(
        admin_roles=_normalize_role_set(admin_roles, fallback=("admin",)),
        operator_roles=_normalize_role_set(operator_roles, fallback=("operator",)),
        viewer_roles=_normalize_role_set(viewer_roles, fallback=("viewer",)),
        installation_admin_predicate=installation_admin_predicate,
    )
    entitlement_service = CoreEntitlementService()
    return {
        "authorization_service": authorization_service,
        "entitlement_service": entitlement_service,
        "audit_sink": DomainEventAuditSink(),
        "secret_provider": InDatabaseSecretProvider(),
        "extension_governance_policy": CommunityExtensionGovernancePolicy(),
    }


__all__ = [
    "ENTITLEMENT_SOURCE_CAPABILITY",
    "FEATURE_NOT_ENTITLED_CODE",
    "INSTALLATION_ADMIN_PERMISSION",
    "AuditSink",
    "AuthorizationService",
    "CommunityAuthorizationService",
    "CommunityExtensionGovernancePolicy",
    "CoreEntitlementService",
    "DomainEventAuditSink",
    "EntitlementService",
    "EntitlementSource",
    "EntitlementSourceError",
    "EntitlementSourceRefusedError",
    "ExtensionGovernancePolicy",
    "FeatureNotEntitledError",
    "InDatabaseSecretProvider",
    "SecretProvider",
    "build_platform_contracts",
    "normalize_entitlement_name",
]
