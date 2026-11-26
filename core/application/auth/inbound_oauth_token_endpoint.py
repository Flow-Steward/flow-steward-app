"""Token and revocation endpoints of the inbound OAuth authorization server (ADR 0047).

The token half of `InboundOAuthService`, kept in its own module: exchange a code,
refresh, and revoke a token. The service provides `clock`, `readiness`,
`_transaction`, `_rate_limited`, `_audit` and `_platform_client_usable`. Raw codes,
tokens and the code verifier are never logged or stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.application.auth import inbound_oauth_platform_clients as platform_clients
from core.application.auth import inbound_oauth_repository as repo
from core.application.auth.inbound_oauth_policy import (
    ACCESS_TOKEN_LIFETIME,
    host_of,
    new_access_token,
    new_refresh_token,
    normalize_resource,
    pkce_verifier_matches,
    secret_digest,
)


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


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    payload: dict[str, Any] | None = None
    error: OAuthProtocolError | None = None
    audit: tuple[str, dict[str, Any]] | None = None


class InboundOAuthTokenEndpointMixin:
    """Issue, refresh and revoke tokens for `InboundOAuthService`."""

    clock: Any
    readiness: Any
    _transaction: Any
    _rate_limited: Any
    _audit: Any
    _platform_client_usable: Any

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
        # ADR 0063: before any row lock, so a revocation cannot interleave.
        platform_clients.lock_for_issuance(conn, client_id)
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
        if not self._platform_client_usable(conn, client_id, account_id):
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
        # ADR 0063: before the grant row lock, the order a revocation takes them in.
        platform_clients.lock_for_issuance(conn, client_id)
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
            or not self._platform_client_usable(conn, client_id, account_id)
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


__all__ = ["InboundOAuthTokenEndpointMixin", "OAuthProtocolError"]
