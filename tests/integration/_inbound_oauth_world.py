"""Shared PostgreSQL fixture data for the inbound OAuth suites (ADR 0047).

Three users of the default account (admin, operator, viewer), two projects, a browser
session per user and the account switch on. Everything created here is removed by
:meth:`OAuthWorld.cleanup`.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.inbound_oauth_clients import FetchedDocument
from core.application.auth.inbound_oauth_policy import (
    INBOUND_OAUTH_SETTING_KEY,
    current_oauth_environment,
    pkce_s256_challenge,
)
from core.application.auth.inbound_oauth_service import InboundOAuthService
from core.application.auth.password import session_token_hash

ORIGIN = "https://fs.example.test"
RESOURCE = f"{ORIGIN}/mcp"
FORWARD_SECRET = "integration-forward-secret-" + "x" * 16  # pragma: allowlist secret
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT = "http://127.0.0.1:53111/callback"
CSRF = "csrf-integration-value"


def client_document(client_id: str = CLIENT_ID) -> bytes:
    """A loopback desktop-agent CIMD document: public client, loopback redirect."""
    return json.dumps(
        {
            "client_id": client_id,
            "client_name": "Desktop agent (test)",
            "redirect_uris": ["http://127.0.0.1/callback", "http://localhost/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()


def oauth_environment(monkeypatch) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", ORIGIN)
    monkeypatch.setenv("FS_MCP_FORWARD_SECRET", FORWARD_SECRET)
    monkeypatch.delenv("FS_MCP_ISSUER_URL", raising=False)
    monkeypatch.delenv("FS_MCP_RESOURCE_URL", raising=False)


@dataclass
class OAuthWorld:
    conn: Any
    dsn: str
    store: Any
    users: dict[str, dict[str, str]] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)

    def service(self, *, clock: Callable[[], datetime] | None = None) -> InboundOAuthService:
        def _fetch(client_id: str) -> FetchedDocument:
            self.fetches.append(client_id)
            return FetchedDocument(body=client_document(client_id), cache_control="max-age=600")

        return InboundOAuthService(
            self.store,
            environment=current_oauth_environment(),
            clock=clock or (lambda: datetime.now(UTC)),
            metadata_fetcher=_fetch,
            rate_limits_enabled=False,
        )

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(statement, params)
            rows = cur.fetchall() if cur.description else []
        self.conn.commit()
        return rows

    def set_enabled(self, enabled: bool) -> None:
        self.sql(
            "INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb) "
            "VALUES (%s, %s, %s::jsonb) ON CONFLICT (account_id, setting_key) "
            "DO UPDATE SET setting_value_jsonb = EXCLUDED.setting_value_jsonb",
            (DEFAULT_ACCOUNT_ID, INBOUND_OAUTH_SETTING_KEY, json.dumps({"enabled": enabled})),
        )

    def principal(self, who: str) -> dict[str, Any]:
        user = self.users[who]
        return {
            "id": user["id"],
            "username": user["username"],
            "roles": [who],
            "principal_type": who,
            "auth_source": "local",
            "account_id": DEFAULT_ACCOUNT_ID,
            "scopes": [],
        }

    def grant_rows(self) -> list[tuple]:
        return self.sql(
            "SELECT id, revoked_at, revoked_reason, expires_at, scopes_jsonb FROM oauth_grants "
            "WHERE user_id = ANY(%s::uuid[]) ORDER BY created_at",
            ([user["id"] for user in self.users.values()],),
        )

    def cleanup(self) -> None:
        self.conn.rollback()
        user_ids = [user["id"] for user in self.users.values()]
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM oauth_authorizations WHERE client_id LIKE 'https://claude.ai/%%'"
            )
            cur.execute("DELETE FROM oauth_grants WHERE user_id = ANY(%s::uuid[])", (user_ids,))
            cur.execute("DELETE FROM oauth_clients WHERE client_id LIKE 'https://claude.ai/%%'")
            cur.execute(
                "DELETE FROM auth_rate_limits WHERE key LIKE 'inbound-oauth:%%'",
            )
            cur.execute("DELETE FROM auth_sessions WHERE user_id = ANY(%s::uuid[])", (user_ids,))
            cur.execute(
                "DELETE FROM personal_access_tokens WHERE user_id = ANY(%s::uuid[])", (user_ids,)
            )
            cur.execute(
                "DELETE FROM user_account_memberships WHERE user_id = ANY(%s::uuid[])", (user_ids,)
            )
            cur.execute("DELETE FROM user_roles WHERE user_id = ANY(%s::uuid[])", (user_ids,))
            cur.execute("DELETE FROM users WHERE id = ANY(%s::uuid[])", (user_ids,))
            cur.execute("DELETE FROM projects WHERE id = ANY(%s::uuid[])", (self.projects,))
            cur.execute(
                "DELETE FROM system_settings WHERE account_id = %s AND setting_key = %s",
                (DEFAULT_ACCOUNT_ID, INBOUND_OAUTH_SETTING_KEY),
            )
        self.conn.commit()
        for connection in (getattr(self.store, "_direct_conn", None),):
            if connection is not None:
                connection.close()


def build_world(pg_conn, dsn: str) -> OAuthWorld:
    from core.infrastructure.db.platform_stores import PlatformIdentityStore

    world = OAuthWorld(conn=pg_conn, dsn=dsn, store=PlatformIdentityStore(dsn))
    suffix = uuid.uuid4().hex[:8]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO roles (key, label) VALUES ('admin', 'Admin'), ('operator', 'Operator'), "
            "('viewer', 'Viewer') ON CONFLICT (key) DO NOTHING"
        )
        for role in ("admin", "operator", "viewer"):
            user_id = str(uuid.uuid4())
            username = f"oauth-{role}-{suffix}@example.test"
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name, account_id) "
                "VALUES (%s, %s, 'not-a-real-hash', %s, %s)",
                (user_id, username, f"OAuth {role}", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                "INSERT INTO user_roles (user_id, role_id) SELECT %s, id FROM roles WHERE key = %s",
                (user_id, role),
            )
            cur.execute(
                "INSERT INTO user_account_memberships (user_id, account_id, is_default) "
                "VALUES (%s, %s, true)",
                (user_id, DEFAULT_ACCOUNT_ID),
            )
            session = secrets.token_urlsafe(24)
            cur.execute(
                "INSERT INTO auth_sessions (user_id, session_token_hash, expires_at, account_id) "
                "VALUES (%s, %s, now() + interval '1 hour', %s)",
                (user_id, session_token_hash(session), DEFAULT_ACCOUNT_ID),
            )
            world.users[role] = {"id": user_id, "username": username, "session": session}
        for index in range(2):
            project_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, f"OAuth project {index} {suffix}", DEFAULT_ACCOUNT_ID),
            )
            world.projects.append(project_id)
    pg_conn.commit()
    world.set_enabled(True)
    return world


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, pkce_s256_challenge(verifier)


def authorize_params(challenge: str, **overrides: str | None) -> list[tuple[str, str]]:
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "scope": " ".join(
            (
                "projects.read",
                "jobs.read",
                "jobs.write",
                "reports.read",
                "reports.write",
                "workflows.read",
                "workflows.write",
            )
        ),
        "state": "integration-state",
    }
    params.update(overrides)
    return [(key, value) for key, value in params.items() if value is not None]


def query_of(location: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def code_from(world: OAuthWorld, service: InboundOAuthService, who: str = "operator", **decision):
    """authorize -> approve through the service; returns (code, verifier, request_id)."""
    verifier, challenge = pkce_pair()
    outcome = service.start_authorization(authorize_params(challenge, **decision.pop("params", {})))
    request_id = query_of(outcome.location)["request"]
    redirect = service.decide(
        request_id,
        user=world.principal(who),
        browser_binding=outcome.browser_binding,
        decision="approve",
        preset_id=decision.pop("preset", "operational"),
        expiry_days=decision.pop("expiry_days", 30),
        project_ids=decision.pop("project_ids", []),
    )
    return query_of(redirect)["code"], verifier, request_id


def exchange(service: InboundOAuthService, code: str, verifier: str, **overrides: str) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }
    form.update(overrides)
    return service.token(form)


def shifted(base: datetime, **delta: float) -> Callable[[], datetime]:
    return lambda: base + timedelta(**delta)
