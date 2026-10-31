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

from core.application.auth import inbound_oauth_repository as repo
from core.application.auth.agent_access_presets import (
    agent_access_preset,
    granted_preset_scopes,
    offered_presets,
    preset_offers,
)
from core.application.auth.audit import log_auth_audit_event
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
    ACCESS_TOKEN_LIFETIME,
    BLOCKER_DISABLED_BY_ADMIN,
    CODE_LIFETIME,
    DEFAULT_GRANT_EXPIRY_DAYS,
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
    new_access_token,
    new_authorization_code,
    new_browser_binding,
    new_refresh_token,
    normalize_resource,
    parse_requested_scopes,
    pkce_verifier_matches,
    redirect_uri_matches,
    redirect_with_params,
    secret_digest,
    valid_code_challenge,
)
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


class OAuthProtocolError(Exception):
    """An RFC 6749 §5.2 / RFC 7009 error for the token and revocation endpoints."""

    def __init__(self, error: str, description: str = "", *, status: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.description = description
        self.status = status

    def to_payload(self) -> dict[str, str]:
        payload = {"error": self.error}
        if self.description:
            payload["error_description"] = self.description
        return payload


class ConsentError(Exception):
    """A consent-page failure: a stable code and the HTTP status the API answers."""

    def __init__(self, code: str, status: int, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message or code


@dataclass(frozen=True, slots=True)
class AuthorizeOutcome:
    """Where ``/oauth/authorize`` sends the browser, and the cookie it sets, if any."""

    location: str
    browser_binding: str | None = None


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    payload: dict[str, Any] | None = None
    error: OAuthProtocolError | None = None
    audit: tuple[str, dict[str, Any]] | None = None


@dataclass
class InboundOAuthService:
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
        try:
            validate_client_id(client_id, allowed_hosts=self.allowed_client_hosts)
            if "client_id" in duplicated:
                raise ClientNotAllowedError("client_id is repeated")
        except ClientNotAllowedError:
            self._audit("oauth_client_rejected", status="denied", client_host=host_of(client_id))
            return self._error_page(ClientNotAllowedError.code)
        try:
            redirect_uris = self._client_redirect_uris(client_id)
        except ClientMetadataUnavailableError:
            return self._error_page(ClientMetadataUnavailableError.code)
        except InvalidClientMetadataError:
            self._audit("oauth_client_rejected", status="denied", client_host=host_of(client_id))
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
        if not raw_resource:
            return redirect_error("invalid_request")
        resource = normalize_resource(raw_resource)
        if not resource or resource != normalize_resource(readiness.resource):
            return redirect_error("invalid_target")
        try:
            requested_scopes = parse_requested_scopes(values.get("scope"))
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
        self._account_ready(str(caller.get("account_id") or ""))
        requested = repo.json_list(row.get("requested_scopes_jsonb"))
        offers = self._preset_offers(caller, requested)
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
                "redirect_uri": redirect_uri,
                "redirect_uri_host": host_of(redirect_uri),
                "redirect_uri_loopback": is_loopback_redirect_uri(redirect_uri),
            },
            "requested_scopes": requested,
            "presets": offers,
            "default_preset": default,
            "expiry_days_options": list(GRANT_EXPIRY_DAY_CHOICES),
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
        requested = repo.json_list(row.get("requested_scopes_jsonb"))
        preset = agent_access_preset(str(preset_id or ""))
        if preset is None or preset.id not in {item.id for item in offered_presets(requested)}:
            raise ConsentError("invalid_preset", 400)
        scopes = granted_preset_scopes(preset, requested)
        if expiry_days not in GRANT_EXPIRY_DAY_CHOICES:
            raise ConsentError("invalid_expiry", 400)
        projects = sorted({str(item).strip() for item in project_ids or [] if str(item).strip()})
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
        return scopes, projects, preset.id, int(expiry_days)

    # ------------------------------------------------------------------
    # Token endpoint (decisions 7, 8)
    # ------------------------------------------------------------------
    def token(self, form: Mapping[str, str], *, client_ip: str = "") -> dict[str, Any]:
        client_id = str(form.get("client_id") or "")
        if not client_id:
            raise OAuthProtocolError("invalid_request", "client_id is required")
        if self._rate_limited("token", client_ip, client_id):
            raise OAuthProtocolError("slow_down", "too many requests", status=429)
        if not self.readiness().ready:
            raise OAuthProtocolError("temporarily_unavailable", status=503)
        grant_type = str(form.get("grant_type") or "")
        if grant_type == "authorization_code":
            return self._exchange_code(form, client_id)
        if grant_type == "refresh_token":
            return self._refresh(form, client_id)
        raise OAuthProtocolError("unsupported_grant_type")

    def _issue_tokens(
        self,
        conn: Any,
        *,
        grant_id: str,
        scopes: list[str],
        grant_expires_at: datetime,
        parent_refresh_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        access_expires_at = min(now + ACCESS_TOKEN_LIFETIME, grant_expires_at)
        access_token = new_access_token()
        refresh_token = new_refresh_token()
        repo.insert_access_token(
            conn,
            token_hash=secret_digest(access_token),
            grant_id=grant_id,
            scopes=scopes,
            created_at=now,
            expires_at=access_expires_at,
        )
        repo.insert_refresh_token(
            conn,
            token_hash=secret_digest(refresh_token),
            grant_id=grant_id,
            parent_id=parent_refresh_id,
            created_at=now,
            expires_at=grant_expires_at,
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": max(0, int((access_expires_at - now).total_seconds())),
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }

    def _user_still_allowed(self, conn: Any, user_id: str, account_id: str) -> bool:
        state = repo.user_account_state(conn, user_id, account_id)
        return bool(
            state and state["user_active"] and state["account_active"] and state["is_member"]
        )

    def _exchange_code(self, form: Mapping[str, str], client_id: str) -> dict[str, Any]:
        code = str(form.get("code") or "")
        if not code:
            raise OAuthProtocolError("invalid_request", "code is required")
        code_hash = secret_digest(code)
        now = self.clock()
        with self._transaction() as conn:
            result = self._exchange_in_transaction(conn, form, client_id, code_hash, now)
        if result.audit is not None:
            self._audit(result.audit[0], status="denied", **result.audit[1])
        if result.error is not None:
            raise result.error
        assert result.payload is not None
        return result.payload

    def _exchange_in_transaction(
        self,
        conn: Any,
        form: Mapping[str, str],
        client_id: str,
        code_hash: str,
        now: datetime,
    ) -> _ExchangeResult:
        consumed = repo.consume_code(conn, code_hash, now)
        if consumed is None:
            known = repo.find_code(conn, code_hash)
            if known is not None and known.get("code_consumed_at") is not None:
                grant_id = str(known.get("grant_id") or "")
                if grant_id:
                    repo.revoke_grant(conn, grant_id, reason="code_reuse", now=now)
                return _ExchangeResult(
                    error=OAuthProtocolError("invalid_grant"),
                    audit=(
                        "oauth_code_reuse",
                        {"authorization_id": str(known["id"]), "grant_id": grant_id},
                    ),
                )
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        # From here the code is consumed whatever follows (decision 7.2).
        if (
            consumed.get("code_expires_at") is None
            or consumed["code_expires_at"] <= now
            or str(consumed["client_id"]) != client_id
            or str(form.get("redirect_uri") or "") != str(consumed["redirect_uri"])
            or not pkce_verifier_matches(form.get("code_verifier"), str(consumed["code_challenge"]))
        ):
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        sent_resource = str(form.get("resource") or "")
        if sent_resource and normalize_resource(sent_resource) != normalize_resource(
            str(consumed["resource"])
        ):
            return _ExchangeResult(error=OAuthProtocolError("invalid_target"))
        user_id = str(consumed["user_id"])
        account_id = str(consumed["account_id"])
        if not self._user_still_allowed(conn, user_id, account_id):
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        if not repo.oauth_enabled_for_account(conn, account_id):
            return _ExchangeResult(error=OAuthProtocolError("temporarily_unavailable", status=503))
        client = repo.get_client(conn, client_id) or {}
        scopes = repo.json_list(consumed.get("scopes_jsonb"))
        grant_expires_at = consumed["grant_expires_at"]
        grant_id = repo.insert_grant(
            conn,
            user_id=user_id,
            account_id=account_id,
            client_id=client_id,
            client_name_snapshot=str(client.get("client_name") or "") or host_of(client_id),
            resource=str(consumed["resource"]),
            preset=str(consumed["preset"]),
            scopes=scopes,
            allowed_project_ids=repo.json_list(consumed.get("allowed_project_ids_jsonb")),
            created_at=now,
            expires_at=grant_expires_at,
        )
        repo.set_authorization_grant(conn, str(consumed["id"]), grant_id)
        repo.touch_client(conn, client_id, now)
        payload = self._issue_tokens(
            conn,
            grant_id=grant_id,
            scopes=scopes,
            grant_expires_at=grant_expires_at,
            parent_refresh_id=None,
            now=now,
        )
        return _ExchangeResult(payload=payload)

    def _refresh(self, form: Mapping[str, str], client_id: str) -> dict[str, Any]:
        refresh_token = str(form.get("refresh_token") or "")
        if not refresh_token:
            raise OAuthProtocolError("invalid_request", "refresh_token is required")
        token_hash = secret_digest(refresh_token)
        now = self.clock()
        with self._transaction() as conn:
            result = self._refresh_in_transaction(conn, form, client_id, token_hash, now)
        if result.audit is not None:
            self._audit(result.audit[0], status="denied", **result.audit[1])
        if result.error is not None:
            raise result.error
        assert result.payload is not None
        return result.payload

    def _refresh_in_transaction(
        self,
        conn: Any,
        form: Mapping[str, str],
        client_id: str,
        token_hash: str,
        now: datetime,
    ) -> _ExchangeResult:
        grant_id = repo.refresh_token_grant_id(conn, token_hash)
        if grant_id is None:
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        grant = repo.lock_grant(conn, grant_id)
        if grant is None:
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        consumed = repo.consume_refresh_token(conn, token_hash, now)
        if consumed is None:
            repo.revoke_grant(conn, grant_id, reason="refresh_reuse", now=now)
            return _ExchangeResult(
                error=OAuthProtocolError("invalid_grant"),
                audit=("oauth_refresh_reuse", {"grant_id": grant_id}),
            )
        account_id = str(grant["account_id"])
        if (
            grant.get("revoked_at") is not None
            or grant["expires_at"] <= now
            or str(grant["client_id"]) != client_id
            or not self._user_still_allowed(conn, str(grant["user_id"]), account_id)
        ):
            return _ExchangeResult(error=OAuthProtocolError("invalid_grant"))
        if not repo.oauth_enabled_for_account(conn, account_id):
            return _ExchangeResult(error=OAuthProtocolError("temporarily_unavailable", status=503))
        grant_scopes = repo.json_list(grant.get("scopes_jsonb"))
        scopes = grant_scopes
        if "scope" in form:
            wanted = {
                item
                for item in str(form.get("scope") or "").split(" ")
                if item and item != "offline_access"
            }
            if not wanted or not wanted <= set(grant_scopes):
                return _ExchangeResult(error=OAuthProtocolError("invalid_scope"))
            scopes = [item for item in grant_scopes if item in wanted]
        sent_resource = str(form.get("resource") or "")
        if sent_resource and normalize_resource(sent_resource) != normalize_resource(
            str(grant["resource"])
        ):
            return _ExchangeResult(error=OAuthProtocolError("invalid_target"))
        repo.touch_client(conn, client_id, now)
        payload = self._issue_tokens(
            conn,
            grant_id=grant_id,
            scopes=scopes,
            grant_expires_at=grant["expires_at"],
            parent_refresh_id=str(consumed["id"]),
            now=now,
        )
        return _ExchangeResult(payload=payload)

    # ------------------------------------------------------------------
    # Revocation (decision 9)
    # ------------------------------------------------------------------
    def revoke_token(self, form: Mapping[str, str], *, client_ip: str = "") -> None:
        """RFC 7009: revoke the whole grant of an access or refresh token of this client."""
        client_id = str(form.get("client_id") or "")
        token = str(form.get("token") or "")
        if not client_id or not token:
            raise OAuthProtocolError("invalid_request", "token and client_id are required")
        if self._rate_limited("token", client_ip, client_id):
            raise OAuthProtocolError("slow_down", "too many requests", status=429)
        if not self.readiness().ready:
            raise OAuthProtocolError("temporarily_unavailable", status=503)
        now = self.clock()
        revoked_grant = ""
        with self._transaction() as conn:
            match = repo.grant_for_token(conn, secret_digest(token))
            if match is not None:
                grant = repo.lock_grant(conn, str(match["grant_id"]))
                if (
                    grant is not None
                    and str(grant["client_id"]) == client_id
                    and repo.revoke_grant(conn, str(grant["id"]), reason="client_revoked", now=now)
                ):
                    revoked_grant = str(grant["id"])
        if revoked_grant:
            self._audit("oauth_grant_revoke", grant_id=revoked_grant, reason="client_revoked")

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
