"""Use cases of the inbound OAuth authorization server (ADR 0047).

Start an authorization, show and decide consent, exchange a code, refresh, revoke a
token, list and revoke grants, toggle the account switch, and resolve an access token
to a principal. HTTP adapters parse and serialize; this module owns every rule and
every transaction. Raw codes, tokens, the browser-binding cookie, the code verifier
and ``state`` are never logged or stored.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.auth import inbound_oauth_platform_clients as platform_clients
from core.application.auth import inbound_oauth_repository as repo
from core.application.auth.agent_access_presets import (
    ADVANCED_OPTIONS_ADMIN_ONLY,
    ADVANCED_OPTIONS_ADMIN_ONLY_MESSAGE,
    EXCEEDS_AUTHORITY_REASON,
    ROLE_PRESET_EXPIRY_DAYS,
    ROLE_PRESET_ID,
    RoleGrant,
    agent_access_preset,
    granted_preset_scopes,
    may_choose_agent_access,
    offered_presets,
    preset_offers,
    role_preset_grant,
    role_preset_scopes,
)
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.effective_permissions import person_role_permissions
from core.application.auth.inbound_oauth_clients import (
    ALLOWED_CLIENT_ID_HOSTS,
    ClientMetadataUnavailableError,
    ClientNotAllowedError,
    FetchedDocument,
    InvalidClientMetadataError,
    MetadataFetcher,
    cache_lifetime,
    fetch_client_metadata,
    validate_client_id,
    validate_client_metadata,
)
from core.application.auth.inbound_oauth_policy import (
    BLOCKER_DISABLED_BY_ADMIN,
    CODE_LIFETIME,
    DEFAULT_GRANT_EXPIRY_DAYS,
    DEFAULT_REQUESTED_SCOPES,
    GRANT_EXPIRY_DAY_CHOICES,
    MAX_STATE_LENGTH,
    PENDING_AUTHORIZATION_LIFETIME,
    SUPPORTED_SCOPES,
    InvalidScopeError,
    OAuthEnvironment,
    OAuthReadiness,
    current_oauth_environment,
    host_of,
    inbound_oauth_readiness,
    is_loopback_redirect_uri,
    is_oauth_access_token,
    new_authorization_code,
    new_browser_binding,
    normalize_resource,
    parse_explicit_scopes,
    redirect_uri_matches,
    redirect_with_params,
    secret_digest,
    valid_code_challenge,
)
from core.application.auth.inbound_oauth_token_endpoint import (
    InboundOAuthTokenEndpointMixin,
    OAuthProtocolError,
)
from core.application.auth.permissions import PERMISSIONS, VIEWER_ROLE, highest_system_role
from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.infrastructure.db.db_pool import transaction_connection

logger = logging.getLogger(__name__)

CONSENT_PATH = "/oauth/consent"
_AUTHORIZE_RATE = (300, 30)  # window seconds, attempts per key
_TOKEN_RATE = (300, 60)
_INTERACTIVE_SOURCES = frozenset({"local", "external"})


class ConsentError(Exception):
    """A consent-page failure: a stable code and the HTTP status the API answers."""

    def __init__(self, code: str, status: int, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message or code


def _client_kind(client_id: str) -> str:
    """``platform`` for an admin-registered client (ADR 0063), ``cimd`` otherwise."""
    return "platform" if platform_clients.is_platform_client_id(client_id) else "cimd"


@dataclass(frozen=True, slots=True)
class AuthorizeOutcome:
    """Where ``/oauth/authorize`` sends the browser, and the cookie it sets, if any."""

    location: str
    browser_binding: str | None = None


def _requested_scopes(row: Mapping[str, Any]) -> tuple[list[str], bool]:
    """What the client asked for, and whether it named anything.

    A request stored before clients could leave the scope open, or one that named
    nothing, is read as the operational preset's scopes for the preset offers.
    """
    explicit = repo.json_list(row.get("requested_scopes_jsonb"))
    return (explicit or list(DEFAULT_REQUESTED_SCOPES)), bool(explicit)


@dataclass
class InboundOAuthService(InboundOAuthTokenEndpointMixin):
    """Inbound OAuth use cases over the identity store ``bl``."""

    bl: Any
    environment: OAuthEnvironment = field(default_factory=current_oauth_environment)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    metadata_fetcher: MetadataFetcher = field(default=fetch_client_metadata)
    allowed_client_hosts: frozenset[str] = ALLOWED_CLIENT_ID_HOSTS
    rate_limits_enabled: bool = True

    # ------------------------------------------------------------------
    # Readiness and metadata
    # ------------------------------------------------------------------
    def _transaction(self):
        return transaction_connection(self.bl.conn)

    def readiness(self, account_id: str | None = None) -> OAuthReadiness:
        """Readiness for one account, or for the installation when ``account_id`` is None.

        Before sign-in the account is unknown: the installation is enabled when any
        account turned the switch on. Consent and every token use check the user's
        own account.
        """
        try:
            with self._transaction() as conn:
                storage = repo.storage_available(conn)
                if not storage:
                    enabled = False
                elif account_id is None:
                    enabled = repo.any_account_enabled(conn)
                else:
                    enabled = repo.oauth_enabled_for_account(conn, account_id)
        except Exception:
            logger.warning("Inbound OAuth readiness could not read its storage")
            storage, enabled = False, False
        return inbound_oauth_readiness(self.environment, storage_available=storage, enabled=enabled)

    def authorization_server_metadata(self) -> dict[str, Any] | None:
        """RFC 8414 document (decision 3), or ``None`` when not ready."""
        readiness = self.readiness()
        if not readiness.ready:
            return None
        issuer = readiness.issuer
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": list(SUPPORTED_SCOPES),
        }

    # ------------------------------------------------------------------
    # Rate limiting on the shared store (decision 21)
    # ------------------------------------------------------------------
    def _rate_limited(self, action: str, client_ip: str, client_id: str) -> bool:
        if not self.rate_limits_enabled:
            return False
        window, attempts = _AUTHORIZE_RATE if action == "authorize" else _TOKEN_RATE
        ip = str(client_ip or "unknown").strip() or "unknown"
        keys = [f"inbound-oauth:{action}:ip:{ip}"]
        if client_id:
            keys.append(f"inbound-oauth:{action}:client:{secret_digest(client_id)}:ip:{ip}")
        try:
            with self._transaction() as conn:
                retry_after = repo.consume_rate_limit(
                    conn,
                    keys,
                    now_epoch=self.clock().timestamp(),
                    window_seconds=window,
                    max_attempts=attempts,
                )
        except Exception:
            # Fail closed: an unavailable limiter must not open unlimited attempts.
            logger.warning("Inbound OAuth rate limiter unavailable")
            return True
        return retry_after > 0

    # ------------------------------------------------------------------
    # Authorization request (decision 6)
    # ------------------------------------------------------------------
    def _error_page(self, code: str) -> AuthorizeOutcome:
        return AuthorizeOutcome(location=redirect_with_params(CONSENT_PATH, {"error": code}))

    def _client_redirect_uris(self, client_id: str) -> list[str]:
        """Registered redirect URIs from the cached or freshly fetched CIMD document."""
        now = self.clock()
        with self._transaction() as conn:
            cached = repo.get_client(conn, client_id)
        if cached and cached["cache_expires_at"] > now:
            return repo.json_list(cached.get("redirect_uris_jsonb"))
        document: FetchedDocument = self.metadata_fetcher(client_id)
        metadata = validate_client_metadata(document.body, client_id=client_id)
        with self._transaction() as conn:
            repo.upsert_client(
                conn,
                client_id=client_id,
                client_name=metadata.client_name,
                redirect_uris=metadata.redirect_uris,
                metadata_sha256=metadata.metadata_sha256,
                fetched_at=now,
                cache_expires_at=now + cache_lifetime(document.cache_control),
            )
        return list(metadata.redirect_uris)

    def _platform_redirect_uris(self, client_id: str, repeated: bool) -> list[str] | None:
        """Redirect URIs of an active platform client, or None when it may not sign in."""
        if repeated:
            return None
        with self._transaction() as conn:
            registration = platform_clients.active_registration(conn, client_id)
            if registration is None:
                return None
            return platform_clients.ensure_client_row(conn, registration, self.clock())

    @staticmethod
    def _platform_client_usable(conn: Any, client_id: str, account_id: str) -> bool:
        """A platform client signs in only while registered, and only for its own account."""
        if not platform_clients.is_platform_client_id(client_id):
            return True
        registration = platform_clients.active_registration(conn, client_id)
        return registration is not None and registration.get("account_id") == account_id

    def _audit(self, action: str, *, username: str = "", status: str = "ok", **detail: Any) -> None:
        log_auth_audit_event(
            self.bl,
            action=action,
            username=username,
            actor=username or "oauth",
            status=status,
            detail=detail,
        )

    def start_authorization(
        self, params: Iterable[tuple[str, str]], *, client_ip: str = ""
    ) -> AuthorizeOutcome:
        """Validate ``/oauth/authorize`` and store a pending authorization.

        Errors before the redirect URI is trusted go to the React error page; every
        later error goes to the redirect URI with ``error``, ``state`` and ``iss``.
        """
        pairs = list(params)
        values: dict[str, str] = {}
        duplicated: set[str] = set()
        for key, value in pairs:
            if key in values:
                duplicated.add(key)
            values.setdefault(key, value)
        client_id = values.get("client_id", "")
        if self._rate_limited("authorize", client_ip, client_id):
            return self._error_page("rate_limited")
        readiness = self.readiness()
        if not readiness.ready:
            return self._error_page(readiness.blockers[0])
        platform = platform_clients.is_platform_client_id(client_id)
        if platform:
            # ADR 0063 decision 2: an admin-registered client, redirect URIs from its preset.
            redirect_uris = self._platform_redirect_uris(client_id, "client_id" in duplicated)
            if redirect_uris is None:
                self._audit("oauth_client_rejected", status="denied", client_kind="platform")
                return self._error_page(ClientNotAllowedError.code)
        else:
            try:
                validate_client_id(client_id, allowed_hosts=self.allowed_client_hosts)
                if "client_id" in duplicated:
                    raise ClientNotAllowedError("client_id is repeated")
            except ClientNotAllowedError:
                self._audit(
                    "oauth_client_rejected", status="denied", client_host=host_of(client_id)
                )
                return self._error_page(ClientNotAllowedError.code)
            try:
                redirect_uris = self._client_redirect_uris(client_id)
            except ClientMetadataUnavailableError:
                return self._error_page(ClientMetadataUnavailableError.code)
            except InvalidClientMetadataError:
                self._audit(
                    "oauth_client_rejected", status="denied", client_host=host_of(client_id)
                )
                return self._error_page(InvalidClientMetadataError.code)
        redirect_uri = values.get("redirect_uri", "")
        if "redirect_uri" in duplicated or not redirect_uri_matches(redirect_uri, redirect_uris):
            return self._error_page("invalid_redirect_uri")

        state = values.get("state")

        def redirect_error(error: str) -> AuthorizeOutcome:
            echoed = state if state is not None and len(state) <= MAX_STATE_LENGTH else None
            return AuthorizeOutcome(
                location=redirect_with_params(
                    redirect_uri, {"error": error, "state": echoed, "iss": readiness.issuer}
                )
            )

        if duplicated:
            return redirect_error("invalid_request")
        if values.get("response_type") != "code":
            return redirect_error("unsupported_response_type")
        if values.get("code_challenge_method") != "S256" or not valid_code_challenge(
            values.get("code_challenge")
        ):
            return redirect_error("invalid_request")
        if state is not None and len(state) > MAX_STATE_LENGTH:
            return redirect_error("invalid_request")
        raw_resource = values.get("resource", "")
        if not raw_resource and platform:
            # ADR 0063 decision 3: a platform client that sends no resource is bound
            # to this installation's MCP resource; a resource it does send must match.
            raw_resource = readiness.resource
        if not raw_resource:
            return redirect_error("invalid_request")
        resource = normalize_resource(raw_resource)
        if not resource or resource != normalize_resource(readiness.resource):
            return redirect_error("invalid_target")
        try:
            # Stored as named: an empty list is "nothing in particular" (ADR 0076 §5).
            requested_scopes = parse_explicit_scopes(values.get("scope"))
        except InvalidScopeError:
            return redirect_error("invalid_scope")

        binding = new_browser_binding()
        now = self.clock()
        with self._transaction() as conn:
            authorization_id = repo.insert_authorization(
                conn,
                client_id=client_id,
                redirect_uri=redirect_uri,
                resource=readiness.resource,
                code_challenge=str(values.get("code_challenge")),
                state=state,
                requested_scopes=requested_scopes,
                browser_binding_hash=secret_digest(binding),
                created_at=now,
                expires_at=now + PENDING_AUTHORIZATION_LIFETIME,
            )
        return AuthorizeOutcome(
            location=redirect_with_params(CONSENT_PATH, {"request": authorization_id}),
            browser_binding=binding,
        )

    # ------------------------------------------------------------------
    # Consent (decisions 6, 15)
    # ------------------------------------------------------------------
    @staticmethod
    def _require_interactive(user: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if not isinstance(user, Mapping):
            raise ConsentError("authentication_required", 401)
        if str(user.get("auth_source") or "").strip().lower() not in _INTERACTIVE_SOURCES:
            raise ConsentError("interactive_session_required", 403)
        return user

    def _pending(
        self, conn: Any, request_id: str, browser_binding: str | None, *, for_update: bool
    ) -> dict[str, Any]:
        if not repo.is_uuid(request_id):
            raise ConsentError("authorization_not_found", 404)
        row = repo.get_authorization(conn, request_id, for_update=for_update)
        if row is None:
            raise ConsentError("authorization_not_found", 404)
        expected = str(row.get("browser_binding_hash") or "")
        if not browser_binding or secret_digest(browser_binding) != expected:
            raise ConsentError("browser_mismatch", 403)
        if row.get("decided_at") is not None:
            raise ConsentError("authorization_already_decided", 409)
        if row["expires_at"] <= self.clock():
            raise ConsentError("authorization_expired", 410)
        return row

    def _account_ready(self, account_id: str) -> str:
        """Raise the first blocker for the caller's account; return the issuer."""
        readiness = self.readiness(account_id)
        if not readiness.ready:
            status = 403 if readiness.blockers[0] == BLOCKER_DISABLED_BY_ADMIN else 503
            raise ConsentError(readiness.blockers[0], status)
        return readiness.issuer

    @staticmethod
    def _preset_offers(user: Mapping[str, Any], requested: list[str]) -> list[dict[str, Any]]:
        return preset_offers(user, requested)

    def _role_grant(
        self, conn: Any, caller: Mapping[str, Any], requested: list[str], *, explicit: bool
    ) -> RoleGrant:
        """The "as my role" access for this request: the role ∩ what the client asked for.

        A client that named no scope asked for nothing in particular: the person's
        role is the whole answer, never "narrower" than it.
        """
        permissions = person_role_permissions(caller, conn)
        if not explicit:
            return role_preset_grant(permissions, role_preset_scopes(permissions))
        return role_preset_grant(permissions, requested)

    @staticmethod
    def _role_grant_available(caller: Mapping[str, Any], grant: RoleGrant) -> bool:
        if not grant.scopes:
            return False
        try:
            enforce_token_scope_within_caller_authority(
                dict(caller),
                requested_scopes=list(grant.scopes),
                requested_project_ids=[],
                requested_workflow_ids=[],
            )
        except TokenAuthorityError:
            return False
        return True

    def _role_access_view(self, caller: Mapping[str, Any], grant: RoleGrant) -> dict[str, Any]:
        """What the consent screen says the app gets "as my role", in catalogue words."""
        roles = caller.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        return {
            "preset": ROLE_PRESET_ID,
            "role": highest_system_role(roles) or VIEWER_ROLE,
            "scopes": list(grant.scopes),
            "permissions": [
                {"key": key, "description": PERMISSIONS[key].description}
                for key in grant.permissions
            ],
            "narrower": grant.narrower,
            "available": self._role_grant_available(caller, grant),
            "expiry_days": ROLE_PRESET_EXPIRY_DAYS,
            "expires_at": (self.clock() + timedelta(days=ROLE_PRESET_EXPIRY_DAYS)).isoformat(),
        }

    def consent_view(
        self,
        request_id: str,
        *,
        user: Mapping[str, Any] | None,
        browser_binding: str | None,
    ) -> dict[str, Any]:
        caller = self._require_interactive(user)
        with self._transaction() as conn:
            row = self._pending(conn, request_id, browser_binding, for_update=False)
            if not self._platform_client_usable(
                conn, str(row["client_id"]), str(caller.get("account_id") or "")
            ):
                raise ConsentError(ClientNotAllowedError.code, 403)
            requested, explicit = _requested_scopes(row)
            grant = self._role_grant(conn, caller, requested, explicit=explicit)
        self._account_ready(str(caller.get("account_id") or ""))
        role_access = self._role_access_view(caller, grant)
        advanced = may_choose_agent_access(caller)
        # ADR 0076 §5: an operator or a viewer is offered their role's access only; an
        # admin also keeps the presets, the expiry and the projects, the role preselected.
        role_offer = {
            "id": ROLE_PRESET_ID,
            "scopes": role_access["scopes"],
            "available": role_access["available"],
            "reason": "" if role_access["available"] else EXCEEDS_AUTHORITY_REASON,
            "subset_of_preset": role_access["narrower"],
        }
        offers = [role_offer, *self._preset_offers(caller, requested)] if advanced else []
        default = ROLE_PRESET_ID if role_access["available"] else None
        if advanced and default is None:
            default = next((offer["id"] for offer in offers if offer["available"]), None)
        client_id = str(row["client_id"])
        redirect_uri = str(row["redirect_uri"])
        return {
            "request_id": str(row["id"]),
            "expires_at": row["expires_at"].isoformat(),
            "client": {
                "client_id": client_id,
                "client_name": str(row.get("client_name") or "") or host_of(client_id),
                "client_host": host_of(client_id),
                # A platform client was registered by an administrator of this
                # installation; it publishes no identity document of its own.
                "client_kind": _client_kind(client_id),
                "redirect_uri": redirect_uri,
                "redirect_uri_host": host_of(redirect_uri),
                "redirect_uri_loopback": is_loopback_redirect_uri(redirect_uri),
            },
            "user": {
                "username": str(caller.get("username") or ""),
                "display_name": str(caller.get("display_name") or ""),
                "role": role_access["role"],
            },
            "requested_scopes": requested,
            "role_access": role_access,
            "advanced_options": advanced,
            "presets": offers,
            "default_preset": default,
            "expiry_days_options": list(GRANT_EXPIRY_DAY_CHOICES) if advanced else [],
            "default_expiry_days": DEFAULT_GRANT_EXPIRY_DAYS,
        }

    def decide(
        self,
        request_id: str,
        *,
        user: Mapping[str, Any] | None,
        browser_binding: str | None,
        decision: str,
        preset_id: str | None = None,
        expiry_days: int | None = None,
        project_ids: list[str] | None = None,
    ) -> str:
        """Record approve or deny and return the client redirect URL."""
        caller = self._require_interactive(user)
        user_id = str(caller.get("id") or "").strip()
        account_id = str(caller.get("account_id") or "").strip()
        username = str(caller.get("username") or "").strip()
        if not user_id or not account_id:
            raise ConsentError("authentication_required", 401)
        if decision not in {"approve", "deny"}:
            raise ConsentError("invalid_decision", 400)
        issuer = self._account_ready(account_id)
        now = self.clock()
        with self._transaction() as conn:
            row = self._pending(conn, request_id, browser_binding, for_update=True)
            if not self._platform_client_usable(conn, str(row["client_id"]), account_id):
                raise ConsentError(ClientNotAllowedError.code, 403)
            base = {"state": row.get("state"), "iss": issuer}
            if decision == "deny":
                recorded = repo.record_decision(
                    conn,
                    authorization_id=str(row["id"]),
                    decision="denied",
                    decided_at=now,
                    user_id=user_id,
                    account_id=account_id,
                )
                redirect_to = redirect_with_params(
                    str(row["redirect_uri"]), {"error": "access_denied", **base}
                )
                audit = ("oauth_consent_deny", {"authorization_id": str(row["id"])})
            else:
                scopes, projects, preset, days = self._approved_access(
                    conn, caller, row, preset_id, expiry_days, project_ids
                )
                code = new_authorization_code()
                recorded = repo.record_decision(
                    conn,
                    authorization_id=str(row["id"]),
                    decision="approved",
                    decided_at=now,
                    user_id=user_id,
                    account_id=account_id,
                    preset=preset,
                    scopes=scopes,
                    allowed_project_ids=projects,
                    grant_expires_at=now + timedelta(days=days),
                    code_hash=secret_digest(code),
                    code_expires_at=now + CODE_LIFETIME,
                )
                redirect_to = redirect_with_params(str(row["redirect_uri"]), {"code": code, **base})
                audit = (
                    "oauth_consent_approve",
                    {
                        "authorization_id": str(row["id"]),
                        "preset": preset,
                        "scopes": scopes,
                        "project_count": len(projects),
                        "expiry_days": days,
                    },
                )
            if not recorded:
                raise ConsentError("authorization_already_decided", 409)
        self._audit(
            audit[0], username=username, client_host=host_of(str(row["client_id"])), **audit[1]
        )
        return redirect_to

    def _approved_access(
        self,
        conn: Any,
        caller: Mapping[str, Any],
        row: Mapping[str, Any],
        preset_id: str | None,
        expiry_days: int | None,
        project_ids: list[str] | None,
    ) -> tuple[list[str], list[str], str, int]:
        requested, explicit = _requested_scopes(row)
        chosen = str(preset_id or "").strip().lower()
        projects = sorted({str(item).strip() for item in project_ids or [] if str(item).strip()})
        if not may_choose_agent_access(caller) and (
            chosen not in {"", ROLE_PRESET_ID} or expiry_days is not None or projects
        ):
            # ADR 0076 §5: an operator or a viewer gets their role's access for the
            # server's expiry; only an admin chooses a preset, an expiry or projects.
            raise ConsentError(
                ADVANCED_OPTIONS_ADMIN_ONLY, 403, ADVANCED_OPTIONS_ADMIN_ONLY_MESSAGE
            )
        if chosen in {"", ROLE_PRESET_ID}:
            scopes = list(self._role_grant(conn, caller, requested, explicit=explicit).scopes)
            if not scopes:
                raise ConsentError(
                    "exceeds_authority",
                    403,
                    "The access this app asks for is more than your role allows.",
                )
            chosen = ROLE_PRESET_ID
            if expiry_days is None:
                expiry_days = ROLE_PRESET_EXPIRY_DAYS
        else:
            preset = agent_access_preset(chosen)
            if preset is None or preset.id not in {item.id for item in offered_presets(requested)}:
                raise ConsentError("invalid_preset", 400)
            scopes = granted_preset_scopes(preset, requested)
            chosen = preset.id
        if expiry_days not in GRANT_EXPIRY_DAY_CHOICES:
            raise ConsentError("invalid_expiry", 400)
        account_id = str(caller.get("account_id") or "")
        if projects and repo.accessible_project_ids(conn, account_id, projects) != set(projects):
            raise ConsentError("project_not_accessible", 403)
        try:
            enforce_token_scope_within_caller_authority(
                dict(caller),
                requested_scopes=scopes,
                requested_project_ids=projects,
                requested_workflow_ids=[],
            )
        except TokenAuthorityError as exc:
            raise ConsentError("exceeds_authority", exc.status_code, str(exc)) from exc
        return scopes, projects, chosen, int(expiry_days)

    # ------------------------------------------------------------------
    # Settings: grants and the account switch (decision 16)
    # ------------------------------------------------------------------
    def _grant_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        now = self.clock()
        if row.get("revoked_at") is not None:
            status = "revoked"
        elif row["expires_at"] <= now:
            status = "expired"
        else:
            status = "active"

        def iso(value: Any) -> str | None:
            return value.isoformat() if isinstance(value, datetime) else None

        client_id = str(row["client_id"])
        return {
            "id": str(row["id"]),
            "client_id": client_id,
            "client_name": str(row.get("client_name_snapshot") or "") or host_of(client_id),
            "client_host": host_of(client_id),
            "client_kind": _client_kind(client_id),
            "preset": str(row.get("preset") or ""),
            "scopes": repo.json_list(row.get("scopes_jsonb")),
            "allowed_project_ids": repo.json_list(row.get("allowed_project_ids_jsonb")),
            "created_at": iso(row.get("created_at")),
            "expires_at": iso(row.get("expires_at")),
            "last_used_at": iso(row.get("last_used_at")),
            "revoked_at": iso(row.get("revoked_at")),
            "revoked_reason": row.get("revoked_reason"),
            "status": status,
        }

    def list_grants(self, user: Mapping[str, Any] | None) -> dict[str, Any]:
        caller = self._require_interactive(user)
        readiness = self.readiness(str(caller.get("account_id") or ""))
        grants: list[dict[str, Any]] = []
        if "storage_missing" not in readiness.blockers:
            with self._transaction() as conn:
                rows = repo.list_user_grants(conn, str(caller.get("id") or ""))
            grants = [self._grant_view(row) for row in rows]
        return {"grants": grants, "readiness": readiness.to_payload()}

    def revoke_user_grant(self, user: Mapping[str, Any] | None, grant_id: str) -> dict[str, Any]:
        caller = self._require_interactive(user)
        if not repo.is_uuid(grant_id):
            raise ConsentError("grant_not_found", 404)
        now = self.clock()
        with self._transaction() as conn:
            row = repo.get_user_grant(conn, grant_id, str(caller.get("id") or ""))
            if row is None:
                raise ConsentError("grant_not_found", 404)
            changed = repo.revoke_grant(conn, grant_id, reason="user_revoked", now=now)
            row = repo.get_user_grant(conn, grant_id, str(caller.get("id") or "")) or row
        if changed:
            self._audit(
                "oauth_grant_revoke",
                username=str(caller.get("username") or ""),
                grant_id=grant_id,
                reason="user_revoked",
                client_host=host_of(str(row["client_id"])),
            )
        return self._grant_view(row)

    def account_setting(self, account_id: str) -> dict[str, Any]:
        """The stored switch (works without the OAuth tables) and the readiness."""
        try:
            with self._transaction() as conn:
                enabled = repo.oauth_enabled_for_account(conn, account_id)
        except Exception:
            logger.warning("Inbound OAuth account switch could not be read")
            enabled = False
        return {"enabled": enabled, "readiness": self.readiness(account_id).to_payload()}

    def set_account_setting(self, account_id: str, *, enabled: bool, actor: str) -> dict[str, Any]:
        with self._transaction() as conn:
            repo.set_oauth_enabled_for_account(
                conn, account_id, enabled=bool(enabled), updated_by=actor or "admin"
            )
        self._audit(
            "oauth_setting_update", username=actor, account_id=account_id, enabled=bool(enabled)
        )
        return self.account_setting(account_id)

    # ------------------------------------------------------------------
    # Access-token resolution (decision 12)
    # ------------------------------------------------------------------
    def resolve_access_principal(self, token: str) -> dict[str, Any] | None:
        """Principal dict of a live OAuth access token, or ``None``.

        The environment part of readiness is checked here; storage, the account
        switch, revocation, expiry, user and membership state are checked by the
        single resolve query.
        """
        if not is_oauth_access_token(token):
            return None
        environment_ready = inbound_oauth_readiness(
            self.environment, storage_available=True, enabled=True
        )
        if not environment_ready.ready:
            return None
        try:
            with self._transaction() as conn:
                row = repo.resolve_access_token(conn, secret_digest(token), self.clock())
        except Exception:
            logger.warning("Inbound OAuth access token could not be resolved")
            return None
        if row is None:
            return None
        if normalize_resource(str(row.get("resource") or "")) != normalize_resource(
            environment_ready.resource
        ):
            # Bound to a resource this installation no longer is (its public origin moved).
            return None
        return {
            "principal_type": "user",
            "id": str(row["user_id"]),
            "username": str(row.get("username") or "").strip(),
            "display_name": str(row.get("display_name") or "").strip(),
            "account_id": str(row["account_id"]),
            "roles": [str(role) for role in row.get("roles") or []],
            "scopes": repo.json_list(row.get("token_scopes_jsonb")),
            "allowed_project_ids": repo.json_list(row.get("allowed_project_ids_jsonb")),
            "allowed_workflow_ids": repo.json_list(row.get("allowed_workflow_ids_jsonb")),
            "is_active": True,
            "expires_at": row.get("token_expires_at"),
            "auth_source": "oauth",
            "oauth_grant_id": str(row["grant_id"]),
            "oauth_client_id": str(row["client_id"]),
            "oauth_resource": str(row["resource"]),
        }


def resolve_oauth_access_principal(bl: Any, token: str) -> dict[str, Any] | None:
    """Entry point for ``local_auth_middleware``: the only resolver of ``fs_oat_`` bearers."""
    if not bl or not is_oauth_access_token(token):
        return None
    return InboundOAuthService(bl).resolve_access_principal(token)


__all__ = [
    "AuthorizeOutcome",
    "ConsentError",
    "InboundOAuthService",
    "OAuthProtocolError",
    "resolve_oauth_access_principal",
]
