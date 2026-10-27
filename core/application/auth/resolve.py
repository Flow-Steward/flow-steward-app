"""Principal resolution from various authentication sources."""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from core.application.auth.account_memberships import enrich_user_payload_with_account_memberships
from core.application.auth.config import DEFAULT_ACCOUNT_ID, env_flag
from core.application.auth.password import session_token_hash
from core.application.auth.principal import ResolvedPrincipal
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql

AND_U_DELETED_AT_IS_NULL_VALUE = "AND u.deleted_at IS NULL"


logger = logging.getLogger(__name__)


def _execute_with_prepare_retry(cur: Any, query: Any, params: tuple[Any, ...]) -> None:
    """Retry once without server-side prepare when psycopg statement names collide."""
    try:
        cur.execute(query, params)
    except psycopg.errors.DuplicatePreparedStatement:
        cur.execute(query, params, prepare=False)


def _qualified_column(alias: str, column_name: str) -> Any:
    return psycopg_sql.SQL("{alias}.{column}").format(
        alias=psycopg_sql.Identifier(alias),
        column=psycopg_sql.Identifier(column_name),
    )


def interactive_principal_type(raw: dict | None) -> str:
    if not isinstance(raw, dict):
        return "user"

    roles = {
        str(role or "").strip().lower()
        for role in (raw.get("roles") or [])
        if str(role or "").strip()
    }
    for role_name in ("admin", "operator", "viewer"):
        if role_name in roles:
            return role_name

    fallback = str(raw.get("principal_type") or "").strip().lower()
    if fallback and fallback != "service_account":
        return fallback
    return "user"


# ---------------------------------------------------------------------------
# Table availability checks
# ---------------------------------------------------------------------------


def auth_tables_available(bl) -> bool:
    required = {"users", "roles", "user_roles", "auth_sessions"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def machine_auth_tables_available(bl) -> bool:
    required = {"service_accounts", "machine_tokens"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def pat_tables_available(bl) -> bool:
    required = {"personal_access_tokens", "users"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def auth_identity_tables_available(bl) -> bool:
    required = {"users", "roles", "user_roles"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def auth_password_reset_tables_available(bl) -> bool:
    required = {"users", "auth_password_resets"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def auth_user_column_exists(bl, column_name: str) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._column_exists("users", str(column_name or "").strip()))
    except Exception:
        return False


def auth_user_must_change_password_select(bl, alias: str = "u") -> Any:
    if auth_user_column_exists(bl, "must_change_password"):
        return _qualified_column(alias, "must_change_password") + psycopg_sql.SQL(
            " AS must_change_password"
        )
    return psycopg_sql.SQL("false AS must_change_password")


def auth_user_timezone_select(bl, alias: str = "u") -> Any:
    if auth_user_column_exists(bl, "timezone"):
        return _qualified_column(alias, "timezone") + psycopg_sql.SQL(" AS timezone")
    return psycopg_sql.SQL("NULL AS timezone")


def auth_user_locale_select(bl, alias: str = "u") -> Any:
    if auth_user_column_exists(bl, "locale"):
        return _qualified_column(alias, "locale") + psycopg_sql.SQL(" AS locale")
    return psycopg_sql.SQL("NULL AS locale")


def auth_user_account_id_select(bl, alias: str = "u") -> Any:
    """SELECT fragment for account_id — NULL when column absent (fail-closed, no default fallback)."""
    if auth_user_column_exists(bl, "account_id"):
        return _qualified_column(alias, "account_id") + psycopg_sql.SQL(" AS account_id")
    return psycopg_sql.SQL("NULL AS account_id")


def auth_session_column_exists(bl, column_name: str) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._column_exists("auth_sessions", str(column_name or "").strip()))
    except Exception:
        return False


def auth_session_account_id_select(bl, session_alias: str = "s", user_alias: str = "u") -> Any:
    if auth_session_column_exists(bl, "account_id"):
        if auth_user_column_exists(bl, "account_id"):
            return psycopg_sql.SQL(
                "COALESCE(NULLIF({session_alias}.{column}::text, ''), "
                "{user_alias}.{column}::text) AS account_id"
            ).format(
                session_alias=psycopg_sql.Identifier(session_alias),
                user_alias=psycopg_sql.Identifier(user_alias),
                column=psycopg_sql.Identifier("account_id"),
            )
        return _qualified_column(session_alias, "account_id") + psycopg_sql.SQL(" AS account_id")
    return auth_user_account_id_select(bl, alias=user_alias)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def seed_default_roles(bl) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roles (key, label)
            VALUES
                ('admin', 'Admin'),
                ('operator', 'Operator'),
                ('viewer', 'Viewer')
            ON CONFLICT (key) DO UPDATE
            SET label = EXCLUDED.label
            """
        )


def local_auth_bootstrap_required(bl=None) -> bool:
    from core.application.auth.enforce import local_auth_enabled

    if not local_auth_enabled():
        return False
    if not env_flag("FS_AUTH_BOOTSTRAP_ENABLED", True):
        return False
    if not bl or not auth_tables_available(bl):
        return False
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            row = cur.fetchone()
            return int((row[0] if row else 0) or 0) == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# User lookups
# ---------------------------------------------------------------------------


def _dict_cursor(bl):
    """Return a RealDictCursor when psycopg_runtime extras is importable.

    Some tests and local tooling run without psycopg_runtime installed, and importing
    the top-level package alone does not guarantee an ``extras`` attribute.
    Fall back to the connection's default cursor in those cases.
    """
    try:
        _pge2 = __import__("psycopg_runtime.extras", fromlist=["RealDictCursor"])
        return bl.conn.cursor(cursor_factory=_pge2.RealDictCursor)
    except ModuleNotFoundError:
        try:
            from core.infrastructure.db.psycopg_runtime import extras as _pge2

            return bl.conn.cursor(cursor_factory=_pge2.RealDictCursor)
        except Exception:
            return bl.conn.cursor()
    except ImportError:
        return bl.conn.cursor()
    except Exception:
        return bl.conn.cursor()


def get_user_roles(bl, user_id: Any) -> list[str]:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return []
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.key
            FROM user_roles ur
            JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s
            ORDER BY r.key
            """,
            (normalized_user_id,),
        )
        rows = cur.fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def get_user_by_username(bl, username: str) -> dict | None:
    must_change_select = auth_user_must_change_password_select(bl, alias="u")
    tz_select = auth_user_timezone_select(bl, alias="u")
    locale_select = auth_user_locale_select(bl, alias="u")
    acct_select = auth_user_account_id_select(bl, alias="u")
    active_clause = (
        psycopg_sql.SQL(AND_U_DELETED_AT_IS_NULL_VALUE)
        if auth_user_column_exists(bl, "deleted_at")
        else psycopg_sql.SQL("")
    )
    with _dict_cursor(bl) as cur:
        cur.execute(
            psycopg_sql.SQL(
                """
            SELECT u.id, u.username, u.display_name, u.password_hash, u.is_active, u.created_at, u.updated_at
            , {must_change_select}
            , {tz_select}
            , {locale_select}
            , {acct_select}
            FROM users u
            WHERE lower(u.username) = lower(%s)
              {active_clause}
            LIMIT 1
            """
            ).format(
                must_change_select=must_change_select,
                tz_select=tz_select,
                locale_select=locale_select,
                acct_select=acct_select,
                active_clause=active_clause,
            ),
            (str(username or "").strip(),),
        )
        row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["roles"] = get_user_roles(bl, str(payload["id"]))
    return enrich_user_payload_with_account_memberships(bl, payload)


def get_user_by_id(bl, user_id: Any) -> dict | None:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return None
    must_change_select = auth_user_must_change_password_select(bl, alias="u")
    tz_select = auth_user_timezone_select(bl, alias="u")
    locale_select = auth_user_locale_select(bl, alias="u")
    acct_select = auth_user_account_id_select(bl, alias="u")
    active_clause = (
        psycopg_sql.SQL(AND_U_DELETED_AT_IS_NULL_VALUE)
        if auth_user_column_exists(bl, "deleted_at")
        else psycopg_sql.SQL("")
    )
    with _dict_cursor(bl) as cur:
        cur.execute(
            psycopg_sql.SQL(
                """
            SELECT u.id, u.username, u.display_name, u.password_hash, u.is_active, u.created_at, u.updated_at
            , {must_change_select}
            , {tz_select}
            , {locale_select}
            , {acct_select}
            FROM users u
            WHERE u.id = %s
              {active_clause}
            LIMIT 1
            """
            ).format(
                must_change_select=must_change_select,
                tz_select=tz_select,
                locale_select=locale_select,
                acct_select=acct_select,
                active_clause=active_clause,
            ),
            (normalized_user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["roles"] = get_user_roles(bl, str(payload["id"]))
    return enrich_user_payload_with_account_memberships(bl, payload)


def get_user_by_session_token(bl, token: str) -> dict | None:
    token_hash = session_token_hash(token)
    must_change_select = auth_user_must_change_password_select(bl, alias="u")
    tz_select = auth_user_timezone_select(bl, alias="u")
    locale_select = auth_user_locale_select(bl, alias="u")
    acct_select = auth_session_account_id_select(bl, session_alias="s", user_alias="u")
    active_clause = (
        psycopg_sql.SQL(AND_U_DELETED_AT_IS_NULL_VALUE)
        if auth_user_column_exists(bl, "deleted_at")
        else psycopg_sql.SQL("")
    )
    with _dict_cursor(bl) as cur:
        _execute_with_prepare_retry(
            cur,
            psycopg_sql.SQL(
                """
            SELECT
                u.id,
                u.username,
                u.display_name,
                u.is_active,
                {must_change_select},
                {tz_select},
                {locale_select},
                {acct_select},
                s.id AS session_id,
                s.expires_at
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token_hash = %s
              AND s.revoked = false
              AND s.expires_at > now()
              {active_clause}
            LIMIT 1
            """
            ).format(
                must_change_select=must_change_select,
                tz_select=tz_select,
                locale_select=locale_select,
                acct_select=acct_select,
                active_clause=active_clause,
            ),
            (token_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        _execute_with_prepare_retry(
            cur,
            "UPDATE auth_sessions SET last_seen_at = now() WHERE session_token_hash = %s",
            (token_hash,),
        )
    payload = dict(row)
    # Fail-closed: a session without an account_id is invalid — do not assign a default.
    # Migration 089 adds account_id to users; if it's absent the session must be rejected.
    if not str(payload.get("account_id") or "").strip():
        return None
    payload["roles"] = get_user_roles(bl, str(payload["id"]))
    return enrich_user_payload_with_account_memberships(bl, payload)


# ---------------------------------------------------------------------------
# Bearer token resolution: session → PAT → machine
# ---------------------------------------------------------------------------


def resolve_user_from_session(bl, token: str) -> dict | None:
    """Resolve a bearer token as a session token."""
    if not token or not bl or not auth_tables_available(bl):
        return None
    user = get_user_by_session_token(bl, token)
    if user:
        user.setdefault("auth_source", "local")
        user["principal_type"] = interactive_principal_type(user)
    return user


def resolve_machine_principal(bl, token: str) -> dict | None:
    """Resolve a bearer token as a machine (service account) token."""
    normalized_token = str(token or "").strip()
    if not normalized_token or not bl or not machine_auth_tables_available(bl):
        return None
    if hasattr(bl, "resolve_machine_token"):
        try:
            principal = bl.resolve_machine_token(normalized_token)
            return dict(principal) if isinstance(principal, dict) else None
        except Exception:
            return None
    return None


def resolve_pat_principal(bl, token: str) -> dict | None:
    """Resolve a bearer token as a personal access token."""
    normalized_token = str(token or "").strip()
    if not normalized_token or not bl or not pat_tables_available(bl):
        return None
    if hasattr(bl, "resolve_pat"):
        try:
            principal = bl.resolve_pat(normalized_token)
            return dict(principal) if isinstance(principal, dict) else None
        except Exception:
            return None
    return None


def resolve_bearer_principal(bl, token: str) -> dict | None:
    """Unified bearer token resolution: session → PAT → machine token.

    This is the single function that all entry points (serve.py, MCP) should use
    to resolve a Bearer token to a principal dict.
    Note: A2A deferred — not implemented.
    """
    normalized_token = str(token or "").strip()
    if not normalized_token or not bl:
        return None

    # 1. Try session token
    principal = resolve_user_from_session(bl, normalized_token)
    if principal:
        return principal

    # 2. Try personal access token
    principal = resolve_pat_principal(bl, normalized_token)
    if principal:
        return principal

    # 3. Try machine (service account) token
    principal = resolve_machine_principal(bl, normalized_token)
    if principal:
        return principal

    return None


def _normalize_principal_dict(raw: dict) -> ResolvedPrincipal:
    """Convert a raw principal dict (any auth source) into a ResolvedPrincipal."""
    from core.application.auth.principal import (
        coerce_optional_project_id,
        coerce_optional_workflow_id,
        coerce_optional_workspace_id,
    )

    allowed_pids: list[str] = []
    for item in raw.get("allowed_project_ids") or []:
        pid = coerce_optional_project_id(item)
        if pid is not None:
            allowed_pids.append(pid)

    allowed_wids: list[str] = []
    for item in raw.get("allowed_workflow_ids") or []:
        wid = coerce_optional_workflow_id(item)
        if wid:
            allowed_wids.append(wid)

    allowed_workspace_ids = [
        workspace_id
        for item in raw.get("allowed_workspace_ids") or []
        if (workspace_id := coerce_optional_workspace_id(item)) is not None
    ]

    principal_type = str(raw.get("principal_type") or "").strip().lower()
    auth_source = str(raw.get("auth_source") or "").strip().lower()
    if principal_type != "service_account" and auth_source in {"", "local", "external"}:
        principal_type = interactive_principal_type(raw)

    return ResolvedPrincipal(
        id=str(raw.get("id") or "").strip(),
        username=str(raw.get("username") or raw.get("service_account_key") or "").strip(),
        email=str(raw.get("email") or raw.get("username") or "").strip(),
        display_name=str(raw.get("display_name") or "").strip(),
        is_active=bool(raw.get("is_active", True)),
        roles=[str(r) for r in (raw.get("roles") or [])],
        scopes=[str(s) for s in (raw.get("scopes") or [])],
        auth_source=str(raw.get("auth_source") or "").strip(),
        principal_type=principal_type or "user",
        account_id=str(raw.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
        account_ids=[
            str(account_id).strip()
            for account_id in (raw.get("account_ids") or [])
            if str(account_id).strip()
        ],
        account_memberships=[
            dict(item) for item in (raw.get("account_memberships") or []) if isinstance(item, dict)
        ],
        workspace_id=coerce_optional_workspace_id(raw.get("workspace_id")),
        allowed_workspace_ids=allowed_workspace_ids,
        allowed_project_ids=allowed_pids,
        allowed_workflow_ids=allowed_wids,
        service_account_id=str(raw.get("service_account_id") or "").strip(),
        service_account_key=str(raw.get("service_account_key") or "").strip(),
        expires_at=raw.get("expires_at"),
        must_change_password=bool(raw.get("must_change_password", False)),
        timezone=str(raw.get("timezone") or "").strip(),
        locale=str(raw.get("locale") or "").strip(),
    )


def _principal_requires_account_id(raw: dict) -> bool:
    source = str(raw.get("auth_source") or "").strip().lower()
    principal_type = str(raw.get("principal_type") or "").strip().lower()
    return source in {"pat", "machine"} or principal_type == "service_account"


def normalize_principal(raw: dict | ResolvedPrincipal | None) -> ResolvedPrincipal | None:
    """Normalize any raw principal-like payload into ``ResolvedPrincipal``."""
    if raw is None:
        return None
    if isinstance(raw, ResolvedPrincipal):
        return raw
    if not isinstance(raw, dict):
        return None
    if _principal_requires_account_id(raw) and not str(raw.get("account_id") or "").strip():
        return None
    return _normalize_principal_dict(raw)


def resolve_to_principal(bl, token: str) -> ResolvedPrincipal | None:
    """Resolve a bearer token to a ``ResolvedPrincipal``.

    Wraps ``resolve_bearer_principal`` and normalizes the raw dict into the
    unified model so every downstream consumer sees a consistent shape.
    """
    return normalize_principal(resolve_bearer_principal(bl, token))
