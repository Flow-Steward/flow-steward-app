"""Use cases of the "connect an AI agent" wizard (task 4, Settings → Account → AI agents).

The wizard is a view over records that already exist: personal access tokens for local
clients, inbound OAuth grants (ADR 0047) for cloud clients. It adds no connection
registry. This module owns every rule the wizard depends on:

* the MCP address a client is given, from the canonical installation configuration
  (``FS_PUBLIC_BASE_URL`` through :func:`mcp_auth_urls`), never a container address;
* the blockers a cloud client meets (the inbound OAuth readiness of the account);
* the access presets of :mod:`agent_access_presets`, each checked against the caller's
  authority with the check every token issuance uses;
* exactly-once token issuance per attempt through :class:`RequestIdempotencyService`.

Issuance claims the attempt, creates the token and records the replay answer in one
transaction, so an attempt either produced exactly one token or nothing. A repeat of
the same attempt (a double click, a retry after a lost response) gets the token's id
and current metadata with ``secret_already_shown``; the secret itself is never stored,
replayed, logged or put in a URL.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from core.application.auth import inbound_oauth_repository as oauth_repo
from core.application.auth.agent_access_presets import (
    AGENT_ACCESS_SCOPES,
    agent_access_preset,
    preset_offers,
)
from core.application.auth.agent_connection_catalog import (
    AUTH_PERSONAL_TOKEN,
    CLIENTS,
    CONNECTION_CLOUD,
    FIRST_REQUEST_STEP,
    MARKETPLACE_ARCHIVE,
    PACKAGES,
    REPOSITORY_URL,
    TOKEN_PLACEHOLDER,
    AgentClient,
    ClientPackageSetup,
    SetupStep,
    agent_client,
    agent_skill_package,
    client_package_setup,
)
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.inbound_oauth_policy import (
    DEFAULT_GRANT_EXPIRY_DAYS,
    GRANT_EXPIRY_DAY_CHOICES,
    OAuthEnvironment,
    OAuthReadiness,
    current_oauth_environment,
)
from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.application.canonical_json import canonical_json_hash
from core.application.idempotency import (
    IdempotencyScope,
    RequestIdempotencyRepository,
    RequestIdempotencyService,
)
from core.infrastructure.config.version import get_app_version
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.public_identity import mcp_auth_urls, public_origin

ISSUE_OPERATION = "auth.agent_access.issue_token"
#: How long a repeat of the same attempt still answers with the token it created.
REPLAY_WINDOW = timedelta(days=1)
TOKEN_LABEL_PREFIX = "AI agent"

BLOCKER_PUBLIC_ADDRESS_MISSING = "public_address_missing"
BLOCKER_INTERNAL_ADDRESS = "internal_address"


class AgentConnectionError(Exception):
    """A refusal with a stable code the HTTP adapter maps to its status."""

    def __init__(self, code: str, status: int, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code

    def to_payload(self) -> dict[str, str]:
        payload = {"error": self.code}
        if self.message != self.code:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """The address a client connects to, or why there is none to give."""

    url: str
    blocker: str = ""


def _is_container_host(hostname: str) -> bool:
    """A single-label name other than ``localhost``: a service on a container network."""
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost":
        return False
    if host.startswith("[") or ":" in host:
        return False
    return "." not in host and not host.replace(".", "").isdigit()


def agent_mcp_endpoint(environment: OAuthEnvironment) -> McpEndpoint:
    """``{FS_PUBLIC_BASE_URL origin}/mcp`` (or its explicit override), never a fallback.

    Without a public base URL :func:`mcp_auth_urls` falls back to development
    addresses (``http://localhost:8100``) that only make sense inside a developer's
    stack, so the wizard reports the missing setting instead of handing them out. A
    host that is a bare container service name (``fs-mcp``) is refused the same way.
    """
    identity = environment.as_identity_mapping()
    if not public_origin(identity):
        return McpEndpoint(url="", blocker=BLOCKER_PUBLIC_ADDRESS_MISSING)
    _issuer, resource = mcp_auth_urls(identity)
    parsed = urlsplit(resource)
    if parsed.scheme not in {"http", "https"} or _is_container_host(parsed.hostname or ""):
        return McpEndpoint(url="", blocker=BLOCKER_INTERNAL_ADDRESS)
    return McpEndpoint(url=resource.rstrip("/"))


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _token_view(token: Mapping[str, Any]) -> dict[str, Any]:
    """Token metadata the wizard shows; never ``raw_token``."""
    return {
        "id": str(token.get("id") or ""),
        "label": str(token.get("label") or ""),
        "scopes": list(token.get("scopes") or []),
        "allowed_project_ids": list(token.get("allowed_project_ids") or []),
        "expires_at": _iso(token.get("expires_at")),
        "created_at": _iso(token.get("created_at")),
        "last_used_at": _iso(token.get("last_used_at")),
        "revoked": bool(token.get("revoked", False)),
        "expired": bool(token.get("expired", False)),
    }


def _caller_ids(user: Mapping[str, Any] | None) -> tuple[str, str]:
    caller = dict(user or {})
    user_id = str(caller.get("id") or "").strip()
    account_id = str(caller.get("account_id") or "").strip()
    if not user_id or not account_id:
        raise AgentConnectionError("authentication_required", 401)
    return user_id, account_id


@dataclass
class AgentConnectionService:
    """Wizard use cases over the identity store ``bl``."""

    bl: Any
    environment: OAuthEnvironment = field(default_factory=current_oauth_environment)
    #: ``account_id -> readiness`` of inbound OAuth; the OAuth service in production.
    oauth_readiness: Callable[[str], OAuthReadiness] | None = None
    core_version: str = field(default_factory=get_app_version)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Setup view
    # ------------------------------------------------------------------
    def _cloud_readiness(self, account_id: str) -> dict[str, Any]:
        if self.oauth_readiness is None:
            from core.application.auth.inbound_oauth_service import InboundOAuthService

            readiness = InboundOAuthService(self.bl, environment=self.environment).readiness(
                account_id
            )
        else:
            readiness = self.oauth_readiness(account_id)
        return readiness.to_payload()

    def _values(self, package_id: str, version: str, client: AgentClient) -> dict[str, str]:
        release_url = f"{REPOSITORY_URL}/releases/tag/v{self.core_version}"
        return {
            "core_version": self.core_version,
            "client": client.label,
            "package": package_id,
            "release_url": release_url,
            "package_archive": f"{package_id}-{version}.zip",
            "package_tarball": f"{package_id}-{version}.tar.gz",
            "marketplace_archive": MARKETPLACE_ARCHIVE.format(core_version=self.core_version),
        }

    @staticmethod
    def _fill(template: str, values: Mapping[str, str]) -> str:
        """Replace the known ``{name}`` placeholders; ``{token}`` and JSON braces stay."""
        filled = template
        for name, value in values.items():
            filled = filled.replace("{" + name + "}", value)
        return filled

    def _step(self, step: SetupStep, values: Mapping[str, str]) -> dict[str, Any]:
        archive = values.get(step.archive, "") if step.archive else ""
        return {
            "id": step.id,
            "kind": step.kind,
            "title_key": step.title_key,
            "title": step.title,
            "description_key": step.description_key,
            "description": step.description,
            "hint_key": step.hint_key,
            "hint": step.hint,
            "values": {**values, "archive": archive},
            "content": self._fill(step.template, values),
            "uses_token": step.uses_token,
            "uses_mcp_url": "{mcp_url}" in step.template,
            "file_name": step.file_name,
            "archive_layout": step.archive_layout,
            "manifest_dir": step.manifest_dir,
            "platform_preset": step.platform_preset,
            "url": values.get(step.link, "") if step.link else "",
        }

    def _client_package(
        self, client: AgentClient, setup: ClientPackageSetup, mcp_url: str
    ) -> dict[str, Any]:
        package = agent_skill_package(setup.package_id)
        version = package.version if package else ""
        values = self._values(setup.package_id, version, client)
        values["mcp_url"] = mcp_url
        readme = f"{REPOSITORY_URL}/blob/v{self.core_version}/docs/agent-skills/{setup.package_id}"
        values["readme_url"] = f"{readme}/README.md" + (
            f"#{setup.readme_anchor}" if setup.readme_anchor else ""
        )
        return {
            "package_id": setup.package_id,
            "offered": setup.offered,
            "available": setup.offered and bool(setup.steps),
            "auth": setup.auth,
            "verified": setup.verified,
            "message_key": setup.message_key,
            "message": setup.message,
            "steps": [self._step(step, values) for step in setup.steps],
        }

    def setup_view(self, user: Mapping[str, Any] | None) -> dict[str, Any]:
        """Everything the wizard renders for this user, without any secret."""
        _user_id, account_id = _caller_ids(user)
        endpoint = agent_mcp_endpoint(self.environment)
        cloud = self._cloud_readiness(account_id)
        cloud_blockers = list(cloud.get("blockers") or [])
        if endpoint.blocker and endpoint.blocker not in cloud_blockers:
            cloud_blockers.insert(0, endpoint.blocker)
        clients = []
        for client in CLIENTS:
            is_cloud = client.connection == CONNECTION_CLOUD
            if is_cloud:
                blockers = cloud_blockers
            elif endpoint.blocker:
                blockers = [endpoint.blocker]
            else:
                blockers = []
            clients.append(
                {
                    "id": client.id,
                    "label": client.label,
                    "connection": client.connection,
                    "description_key": client.description_key,
                    "description": client.description,
                    "mcp_blockers": blockers,
                    "packages": [
                        self._client_package(client, setup, endpoint.url)
                        for setup in client.packages
                    ],
                }
            )
        return {
            "mcp_url": endpoint.url,
            "mcp_blocker": endpoint.blocker,
            "cloud_readiness": {"ready": not cloud_blockers, "blockers": cloud_blockers},
            "core_version": self.core_version,
            "packages": [
                {
                    "id": package.id,
                    "display_name": package.display_name,
                    "short_description": package.short_description,
                    "version": package.version,
                    "needs_mcp": package.needs_mcp,
                    "description_key": package.description_key,
                    "description": package.description,
                }
                for package in PACKAGES
            ],
            "presets": preset_offers(dict(user or {}), list(AGENT_ACCESS_SCOPES)),
            "expiry_days_options": list(GRANT_EXPIRY_DAY_CHOICES),
            "default_expiry_days": DEFAULT_GRANT_EXPIRY_DAYS,
            "first_request": self._step(FIRST_REQUEST_STEP, {}),
            "clients": clients,
        }

    # ------------------------------------------------------------------
    # Token issuance (exactly once per attempt)
    # ------------------------------------------------------------------
    def _issue_request(
        self,
        *,
        client_id: str,
        package_id: str,
        preset_id: str,
        project_ids: list[str] | None,
        expiry_days: int | None,
    ) -> tuple[AgentClient, list[str], list[str], int]:
        client = agent_client(client_id)
        if client is None:
            raise AgentConnectionError("unknown_client", 400)
        setup = client_package_setup(client, package_id)
        if setup is None or not setup.offered:
            raise AgentConnectionError("package_not_offered", 400)
        if setup.auth != AUTH_PERSONAL_TOKEN:
            # Cloud clients connect through OAuth consent; the Developer package and any
            # other package without MCP never needs a token.
            raise AgentConnectionError("token_not_used", 400)
        preset = agent_access_preset(preset_id)
        if preset is None:
            raise AgentConnectionError("invalid_preset", 400)
        if expiry_days not in GRANT_EXPIRY_DAY_CHOICES:
            raise AgentConnectionError("invalid_expiry", 400)
        projects = sorted({str(item).strip() for item in project_ids or [] if str(item).strip()})
        return client, list(preset.scopes), projects, int(expiry_days)

    def issue_token(
        self,
        user: Mapping[str, Any] | None,
        *,
        attempt_key: str,
        client_id: str,
        package_id: str,
        preset_id: str,
        project_ids: list[str] | None,
        expiry_days: int | None,
    ) -> dict[str, Any]:
        """Create at most one personal token for this attempt.

        Returns ``{"status": "created", "secret": ...}`` the first time and
        ``{"status": "replayed", "secret_already_shown": True}`` for every repeat of
        the same attempt, both with the token's current metadata.
        """
        user_id, account_id = _caller_ids(user)
        endpoint_blocker = agent_mcp_endpoint(self.environment).blocker
        if endpoint_blocker:
            # A token nobody can use is not issued: the client would have no address.
            raise AgentConnectionError(endpoint_blocker, 409)
        client, scopes, projects, days = self._issue_request(
            client_id=client_id,
            package_id=package_id,
            preset_id=preset_id,
            project_ids=project_ids,
            expiry_days=expiry_days,
        )
        fingerprint = canonical_json_hash(
            {
                "user_id": user_id,
                "client_id": client.id,
                "package_id": package_id,
                "preset_id": str(preset_id),
                "project_ids": projects,
                "expiry_days": days,
            }
        )
        scope = IdempotencyScope(
            operation=ISSUE_OPERATION,
            account_id=account_id,
            dimensions=(("user_id", user_id),),
        )
        with transaction_connection(self.bl.conn) as conn:
            idempotency = RequestIdempotencyService(
                RequestIdempotencyRepository(conn), now=self.clock
            )
            claim = idempotency.claim(
                scope=scope,
                opaque_key=attempt_key,
                request_fingerprint=fingerprint,
                request_id=uuid.uuid4().hex,
            )
            if claim.outcome == "replay":
                return self._replay(user_id, claim.response or {})
            if claim.outcome == "mismatch":
                raise AgentConnectionError(
                    "attempt_mismatch",
                    409,
                    "This attempt was already used with different settings.",
                )
            if claim.outcome == "expired":
                raise AgentConnectionError("attempt_expired", 409)
            if not claim.claimed:
                raise AgentConnectionError("attempt_in_progress", 409)
            self._check_authority(conn, user, account_id, scopes, projects)
            token = self.bl.create_pat(
                user_id=user_id,
                account_id=account_id,
                label=f"{TOKEN_LABEL_PREFIX} · {client.label}",
                scopes=scopes,
                allowed_project_ids=projects,
                allowed_workflow_ids=[],
                expires_in_days=days,
            )
            view = _token_view(token)
            idempotency.accept(
                claim=claim,
                job_id=None,
                response={"token_id": view["id"]},
                result_expires_at=self.clock() + REPLAY_WINDOW,
            )
        log_auth_audit_event(
            self.bl,
            action="token_create",
            username=str((user or {}).get("username") or ""),
            actor=str((user or {}).get("username") or "auth"),
            status="ok",
            detail={
                "token_id": view["id"],
                "label": view["label"],
                "expires_at": view["expires_at"],
                "source": "agent_access_wizard",
            },
        )
        return {
            "status": "created",
            "token": view,
            "secret": str(token.get("raw_token") or ""),
            "secret_already_shown": False,
        }

    def _check_authority(
        self,
        conn: Any,
        user: Mapping[str, Any] | None,
        account_id: str,
        scopes: list[str],
        projects: list[str],
    ) -> None:
        if projects and oauth_repo.accessible_project_ids(conn, account_id, projects) != set(
            projects
        ):
            raise AgentConnectionError("project_not_accessible", 403)
        try:
            enforce_token_scope_within_caller_authority(
                dict(user or {}),
                requested_scopes=scopes,
                requested_project_ids=projects,
                requested_workflow_ids=[],
            )
        except TokenAuthorityError as exc:
            raise AgentConnectionError("exceeds_authority", exc.status_code, str(exc)) from exc

    def _find_token(self, user_id: str, token_id: str) -> dict[str, Any] | None:
        tokens = self.bl.list_user_pats(user_id, include_revoked=True)
        return next((token for token in tokens if str(token.get("id")) == token_id), None)

    def _replay(self, user_id: str, response: Mapping[str, Any]) -> dict[str, Any]:
        token_id = str(response.get("token_id") or "")
        token = self._find_token(user_id, token_id) if token_id else None
        return {
            "status": "replayed",
            "token": _token_view(token) if token else {"id": token_id},
            "secret": "",
            "secret_already_shown": True,
        }

    # ------------------------------------------------------------------
    # Usage facts
    # ------------------------------------------------------------------
    def token_status(self, user: Mapping[str, Any] | None, token_id: str) -> dict[str, Any]:
        """The token's expiry, scopes, last use and state; nothing about which client."""
        user_id, _account_id = _caller_ids(user)
        token = self._find_token(user_id, str(token_id or "").strip())
        if token is None:
            raise AgentConnectionError("token_not_found", 404)
        return {"token": _token_view(token)}


__all__ = [
    "BLOCKER_INTERNAL_ADDRESS",
    "BLOCKER_PUBLIC_ADDRESS_MISSING",
    "ISSUE_OPERATION",
    "REPLAY_WINDOW",
    "TOKEN_PLACEHOLDER",
    "AgentConnectionError",
    "AgentConnectionService",
    "McpEndpoint",
    "agent_mcp_endpoint",
]
