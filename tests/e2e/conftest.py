from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
import requests

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import (
    apply_test_lane_defaults,
    resolve_base_url,
    resolve_test_env,
)
from tests.e2e.neutral_registry import ensure_neutral_registry, ensure_project_links

apply_test_lane_defaults(os.environ, lane="backend", include_dotenv=True)

for _lane in ("backend", "ui"):
    _resolved_env = resolve_test_env(os.environ, lane=_lane, include_dotenv=True)
    _id_var = f"TEST_PROJECT_{_lane.upper()}_ID"
    _name_var = f"TEST_PROJECT_{_lane.upper()}_NAME"
    if str(_resolved_env.project_id or "").strip():
        os.environ.setdefault(_id_var, str(_resolved_env.project_id))
    if str(_resolved_env.project_name or "").strip():
        os.environ.setdefault(_name_var, str(_resolved_env.project_name))


def _connect_e2e_db():
    dsn = _resolve_e2e_dsn()
    if not dsn:
        return None
    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        try:
            from core.infrastructure.db import psycopg_runtime as psycopg_runtime  # type: ignore

            conn = psycopg_runtime.connect(dsn)
            conn.autocommit = True
            return conn
        except Exception:
            return None


def _resolve_e2e_dsn() -> str:
    dsn = str(os.getenv("FS_DB_DSN") or os.getenv("E2E_AUTH_DSN") or "").strip()
    if not dsn:
        try:
            from core.infrastructure.config.runtime_config import CONFIG

            dsn = str(CONFIG.get("fs_db_dsn") or CONFIG.get("langgraph_postgres_uri") or "").strip()
        except Exception:
            dsn = ""
    if not dsn:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("FS_DB_DSN="):
                    continue
                dsn = line.split("=", 1)[1].strip()
                break
    if not Path("/.dockerenv").exists():
        if "host=pg-fs" in dsn:
            dsn = dsn.replace("host=pg-fs", "host=127.0.0.1")
        if "@pg-fs:" in dsn:
            dsn = dsn.replace("@pg-fs:", "@127.0.0.1:")
    return dsn


def _project_exists(conn, project_id: str) -> bool:
    normalized_id = str(project_id or "").strip()
    if not normalized_id:
        return False
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL LIMIT 1",
                    (normalized_id,),
                )
            except Exception:
                cur.execute("SELECT 1 FROM projects WHERE id = %s LIMIT 1", (normalized_id,))
            return cur.fetchone() is not None
    except Exception:
        return False


def _materialize_configured_project_id(conn, project_id: str, project_name: str) -> bool:
    normalized_id = str(project_id or "").strip()
    normalized_name = str(project_name or "").strip()
    if not normalized_id or not normalized_name:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (
                    id,
                    tenant_id,
                    workspace_id,
                    account_id,
                    name,
                    description,
                    agents_config,
                    project_rules,
                    deleted_at,
                    deleted_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, NULL, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    workspace_id = EXCLUDED.workspace_id,
                    account_id = EXCLUDED.account_id,
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    agents_config = EXCLUDED.agents_config,
                    project_rules = EXCLUDED.project_rules,
                    deleted_at = NULL,
                    deleted_by = NULL
                """,
                (
                    normalized_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_WORKSPACE_ID,
                    DEFAULT_ACCOUNT_ID,
                    normalized_name,
                    "",
                    "{}",
                    None,
                ),
            )
        return True
    except Exception:
        return False


def _project_id_by_name(
    conn, project_name: str, workspace_id: str | None = DEFAULT_WORKSPACE_ID
) -> str:
    normalized_name = str(project_name or "").strip()
    if not normalized_name:
        return ""
    normalized_workspace_id = str(workspace_id or "").strip()
    try:
        with conn.cursor() as cur:
            if normalized_workspace_id:
                cur.execute(
                    """
                    SELECT id
                    FROM projects
                    WHERE name = %s AND workspace_id = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (normalized_name, normalized_workspace_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id
                    FROM projects
                    WHERE name = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (normalized_name,),
                )
            row = cur.fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    if isinstance(row, dict):
        return str(row.get("id") or "").strip()
    if isinstance(row, (tuple, list)):
        return str((row[0] if row else "") or "").strip()
    return str(row or "").strip()


def _ensure_project_id_by_name(project_name: str) -> str:
    normalized_name = str(project_name or "").strip()
    if not normalized_name:
        return ""

    dsn = _resolve_e2e_dsn()
    if not dsn:
        return ""

    try:
        from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

        logger = PlatformLogger(dsn)

        existing_id = ""
        conn = _connect_e2e_db()
        if conn is not None:
            try:
                existing_id = _project_id_by_name(conn, normalized_name)
            finally:
                close = getattr(conn, "close", None)
                if callable(close):
                    close()

        if existing_id:
            project_id = existing_id
        else:
            project_id = str(
                logger.add_project(normalized_name, workspace_id=DEFAULT_WORKSPACE_ID) or ""
            ).strip()
    except Exception:
        return ""
    return str(project_id or "").strip()


def _sync_test_project_ids(env: dict[str, str]) -> None:
    conn = _connect_e2e_db()
    if conn is None:
        return
    try:
        for lane in ("backend", "ui"):
            name_var = f"TEST_PROJECT_{lane.upper()}_NAME"
            id_var = f"TEST_PROJECT_{lane.upper()}_ID"
            current_id = str(env.get(id_var) or "").strip()
            project_name = str(env.get(name_var) or "").strip()
            if (
                current_id
                and project_name
                and not _project_exists(conn, current_id)
                and _materialize_configured_project_id(conn, current_id, project_name)
            ):
                env[id_var] = current_id
                continue
            if current_id:
                env[id_var] = current_id
                continue

            resolved_id = _project_id_by_name(conn, project_name) if project_name else ""
            if resolved_id:
                env[id_var] = resolved_id
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


_sync_test_project_ids(os.environ)


_AUTH_SESSION: requests.Session | None = None
_REGISTRY_BASELINE_READY = False
_REGISTRY_BASELINE_LOCK = threading.Lock()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _bootstrap_headers() -> dict[str, str]:
    token = str(os.getenv("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if not token:
        return {}
    return {"x-platform-setup-token": token}


def _build_seed_credentials() -> dict[str, str]:
    resolved = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
    return {
        "username": resolved.username,
        "password": resolved.password,
        "display_name": resolved.display_name,
    }


def _resolve_seed_role_key(creds: dict[str, str]) -> str:
    role = str(os.getenv("E2E_BACKEND_ROLE") or "").strip().lower()
    if role in {"admin", "operator", "viewer"}:
        return role

    username = str(creds.get("username") or "").strip()
    role_usernames = {
        "admin": str(os.getenv("E2E_ADMIN_USERNAME", "e2e_admin@example.com") or "").strip(),
        "operator": str(
            os.getenv("E2E_OPERATOR_USERNAME", "e2e_operator@example.com") or ""
        ).strip(),
        "viewer": str(os.getenv("E2E_VIEWER_USERNAME", "e2e_viewer@example.com") or "").strip(),
    }
    for role_key, role_username in role_usernames.items():
        if role_username and username == role_username:
            return role_key
    return "admin"


def _password_hash(password: str) -> str:
    raw = str(password or "")
    if not raw:
        raise ValueError("password is required")
    iterations = max(100_000, int(os.getenv("FS_AUTH_PASSWORD_ITERATIONS", "260000")))
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _seed_user_via_db(creds: dict[str, str]) -> bool:
    """Upsert deterministic E2E auth user directly in DB when API bootstrap is unavailable."""
    dsn = _resolve_e2e_dsn()
    if not dsn:
        return False

    user_id = None
    password_hash = _password_hash(creds["password"])
    username = str(creds["username"] or "").strip()
    display_name = str(creds["display_name"] or "").strip() or username
    role_key = _resolve_seed_role_key(creds)
    role_label = role_key.capitalize()
    if not username:
        return False

    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(dsn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE username = %s
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (username,),
                )
                row = cur.fetchone()
                user_id = str((row[0] if row else "") or "").strip()
                if user_id:
                    cur.execute(
                        """
                        UPDATE users
                        SET
                            password_hash = %s,
                            display_name = %s,
                            is_active = true,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (password_hash, display_name, user_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO users (username, password_hash, display_name, is_active)
                        VALUES (%s, %s, %s, true)
                        RETURNING id
                        """,
                        (username, password_hash, display_name),
                    )
                    row = cur.fetchone()
                    user_id = str((row[0] if row else "") or "").strip()
                cur.execute(
                    "INSERT INTO roles (key, label) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (role_key, role_label),
                )
                cur.execute("SELECT id FROM roles WHERE key = %s LIMIT 1", (role_key,))
                role_row = cur.fetchone()
                role_id = str((role_row[0] if role_row else "") or "").strip()
                if user_id and role_id:
                    # Keep deterministic role mapping for the shared E2E users.
                    cur.execute(
                        """
                        DELETE FROM user_roles
                        WHERE user_id = %s
                          AND role_id IN (
                            SELECT id FROM roles WHERE key IN ('admin', 'operator', 'viewer')
                          )
                        """,
                        (user_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO user_roles (user_id, role_id)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id, role_id) DO NOTHING
                        """,
                        (user_id, role_id),
                    )
            conn.commit()
            try:
                from core.application.auth import auth_login_clear_user_failures

                auth_login_clear_user_failures(username)
            except Exception:
                pass
            return bool(user_id)
        finally:
            conn.close()
    except Exception:
        return False


def _seed_user_via_admin_api(
    *,
    base_url: str,
    verify_ssl: bool,
    creds: dict[str, str],
) -> bool:
    """Create/update a deterministic E2E role user through the admin user API."""
    role_key = _resolve_seed_role_key(creds)
    if role_key == "admin":
        return False

    admin_env = resolve_test_env(
        os.environ,
        lane="backend",
        role="admin",
        include_dotenv=True,
    )
    admin_session = requests.Session()
    login_resp = admin_session.post(
        f"{base_url}/api/auth/login",
        json={"email": admin_env.username, "password": admin_env.password},
        verify=verify_ssl,
        timeout=20,
    )
    if login_resp.status_code != 200:
        return False
    try:
        token = str((login_resp.json() or {}).get("csrf_token") or "").strip()
    except Exception:
        token = ""
    if token:
        _set_session_csrf_header(admin_session, token)

    csrf_headers = _merge_csrf_header({}, admin_session)
    email = str(creds["username"] or "").strip()
    payload = {
        "email": email,
        "password": creds["password"],
        "display_name": creds["display_name"] or email,
        "roles": [role_key],
        "is_active": True,
        "must_change_password": False,
    }
    create_resp = admin_session.post(
        f"{base_url}/api/auth/users",
        json=payload,
        headers=csrf_headers,
        verify=verify_ssl,
        timeout=20,
    )
    if create_resp.status_code in (200, 201):
        return True
    if create_resp.status_code != 409:
        return False

    list_resp = admin_session.get(
        f"{base_url}/api/auth/users",
        params={"search": email, "per_page": 50},
        headers=csrf_headers,
        verify=verify_ssl,
        timeout=20,
    )
    if list_resp.status_code != 200:
        return False
    try:
        users = (list_resp.json() or {}).get("users") or []
    except Exception:
        return False
    user_id = ""
    for row in users:
        if not isinstance(row, dict):
            continue
        if str(row.get("email") or row.get("username") or "").strip().lower() == email.lower():
            user_id = str(row.get("id") or "").strip()
            break
    if not user_id:
        return False

    update_resp = admin_session.put(
        f"{base_url}/api/auth/users/{user_id}",
        json={
            "password": creds["password"],
            "display_name": creds["display_name"] or email,
            "roles": [role_key],
            "is_active": True,
            "must_change_password": False,
        },
        headers=csrf_headers,
        verify=verify_ssl,
        timeout=20,
    )
    return update_resp.status_code == 200


def _ensure_local_auth_session(base_url: str, verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    creds = _build_seed_credentials()
    retry_budget = max(0, _e2e_http_retry_count())

    def _post_with_retry(
        path: str,
        *,
        json_payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        attempts = retry_budget + 1
        for attempt in range(attempts):
            try:
                resp = session.post(
                    f"{base_url}{path}",
                    json=json_payload,
                    headers=headers,
                    verify=verify_ssl,
                    timeout=20,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= attempts - 1 or not _is_transient_transport_error(exc):
                    raise
                time.sleep(0.25 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < attempts - 1:
                time.sleep(0.25 * (attempt + 1))
                continue
            return resp
        raise RuntimeError("unreachable: auth post retry loop exhausted")

    def _post_auth(
        path: str,
        *,
        json_payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        """Post to canonical JSON auth endpoints only."""
        return _post_with_retry(path, json_payload=json_payload, headers=headers)

    def _post_login() -> requests.Response:
        login_payload = {"email": creds["username"], "password": creds["password"]}
        response = _post_auth("/api/auth/login", json_payload=login_payload)
        if response.status_code != 409:
            return response
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return response
        options = payload.get("account_options") if isinstance(payload, dict) else None
        active = [
            row
            for row in options or []
            if isinstance(row, dict)
            and bool(row.get("is_active", True))
            and str(row.get("account_id") or "").strip()
        ]
        defaults = [row for row in active if bool(row.get("is_default"))]
        selected = defaults[0] if len(defaults) == 1 else active[0] if len(active) == 1 else None
        if not payload.get("requires_account_selection") or selected is None:
            return response
        return _post_auth(
            "/api/auth/login",
            json_payload={
                **login_payload,
                "account_id": str(selected["account_id"]).strip(),
            },
        )

    # Fast path: test user already exists.
    login_resp = _post_login()
    if login_resp.status_code == 200:
        try:
            token = str((login_resp.json() or {}).get("csrf_token") or "").strip()
        except Exception:
            token = ""
        if token:
            _set_session_csrf_header(session, token)
        return session

    # Local auth may be disabled in specific environments (skip E2E auth forcing there).
    if login_resp.status_code == 400 and "local auth is disabled" in login_resp.text.lower():
        pytest.skip("Local auth disabled in target environment; skipping auth-required E2E run")

    # If first-run bootstrap is needed, seed the deterministic role account.
    bootstrap_resp = _post_auth(
        "/api/auth/bootstrap",
        headers=_bootstrap_headers(),
        json_payload={
            "email": creds["username"],
            "password": creds["password"],
            "display_name": creds["display_name"],
        },
    )
    if bootstrap_resp.status_code not in (200, 409):
        seeded_via_db = False
        if _env_flag("E2E_DB_SEED_FALLBACK", True):
            seeded_via_db = _seed_user_via_db(creds)
        seeded_via_admin_api = False
        if not seeded_via_db and _env_flag("E2E_ADMIN_API_SEED_FALLBACK", True):
            seeded_via_admin_api = _seed_user_via_admin_api(
                base_url=base_url,
                verify_ssl=verify_ssl,
                creds=creds,
            )
        if not seeded_via_db and not seeded_via_admin_api:
            raise RuntimeError(
                "Could not seed E2E auth user via bootstrap: "
                f"{bootstrap_resp.status_code} {bootstrap_resp.text[:300]}"
            )

    # Login must succeed after bootstrap/seed step.
    login_resp = _post_login()
    if login_resp.status_code != 200:
        if _env_flag("E2E_DB_SEED_FALLBACK", True) and _seed_user_via_db(creds):
            login_resp = _post_login()
        if login_resp.status_code != 200:
            if login_resp.status_code == 429:
                pytest.skip(
                    "Login throttled (429) — rate-limit test cross-contamination. "
                    "Re-run after the lockout window expires."
                )
            raise RuntimeError(
                f"Could not authenticate seeded E2E user: {login_resp.status_code} {login_resp.text[:300]}"
            )
    try:
        token = str((login_resp.json() or {}).get("csrf_token") or "").strip()
    except Exception:
        token = ""
    if token:
        _set_session_csrf_header(session, token)
    return session


def _set_session_csrf_header(session: requests.Session, token: str) -> None:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return
    headers = getattr(session, "headers", None)
    if not isinstance(headers, MutableMapping):
        try:
            headers = dict(headers or {})
        except Exception:
            headers = {}
        session.headers = headers
    headers["x-csrf-token"] = normalized_token


def _authed_session() -> tuple[requests.Session, bool]:
    global _AUTH_SESSION
    if _AUTH_SESSION is not None:
        verify_ssl = _env_flag("E2E_VERIFY_SSL", False)
        return _AUTH_SESSION, verify_ssl
    base_url = resolve_base_url(os.environ, include_dotenv=True)
    verify_ssl = _env_flag("E2E_VERIFY_SSL", False)
    try:
        _AUTH_SESSION = _ensure_local_auth_session(base_url=base_url, verify_ssl=verify_ssl)
    except (requests.ConnectionError, requests.Timeout) as exc:
        if _env_flag("E2E_REQUIRE_SERVER", False):
            raise
        pytest.skip(f"Flow Steward server not available at {base_url}: {exc}")
    _ensure_registry_baseline(base_url, _AUTH_SESSION, verify_ssl)
    return _AUTH_SESSION, verify_ssl


def _e2e_http_retry_count() -> int:
    raw = str(os.getenv("E2E_HTTP_RETRY_COUNT", "2") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(0, min(value, 10))


def _is_transient_transport_error(exc: BaseException) -> bool:
    # Keep retries narrow so we only mask short-lived transport flakiness.
    text = str(exc or "").lower()
    transient_markers = (
        "connection reset",
        "connection aborted",
        "remote disconnected",
        "read timed out",
        "timed out",
        "eof occurred",
        "badstatusline",
        "broken pipe",
    )
    return any(marker in text for marker in transient_markers)


def e2e_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    method_upper = str(method or "GET").strip().upper() or "GET"
    session, verify_ssl = _authed_session()
    transient_http_statuses = {429, 502, 503, 504}

    def _send(active_session: requests.Session, active_verify_ssl: bool) -> requests.Response:
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault("verify", active_verify_ssl)
        if method_upper in {"POST", "PUT", "PATCH", "DELETE"}:
            request_kwargs["headers"] = _merge_csrf_header(
                request_kwargs.get("headers"), active_session
            )
        return active_session.request(method_upper, url, **request_kwargs)

    def _send_with_retry(
        active_session: requests.Session, active_verify_ssl: bool
    ) -> requests.Response:
        safe_retry_methods = {"GET", "HEAD", "OPTIONS"}
        max_retries = _e2e_http_retry_count()
        attempts = 0
        while True:
            try:
                response = _send(active_session, active_verify_ssl)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                should_retry = (
                    method_upper in safe_retry_methods
                    and attempts < max_retries
                    and _is_transient_transport_error(exc)
                )
                if not should_retry:
                    raise
                attempts += 1
                time.sleep(min(0.25 * attempts, 1.0))
                continue
            should_retry_http = (
                method_upper in safe_retry_methods
                and attempts < max_retries
                and response.status_code in transient_http_statuses
            )
            if should_retry_http:
                attempts += 1
                time.sleep(min(0.25 * attempts, 1.0))
                continue
            return response

    response = _send_with_retry(session, verify_ssl)
    if response.status_code != 401 or not _env_flag("E2E_ENFORCE_AUTH", True):
        return response

    # Session cookies can expire during long-running matrix tests.
    # Rebuild auth session once and retry transparently.
    global _AUTH_SESSION
    _AUTH_SESSION = None
    retry_session, retry_verify_ssl = _authed_session()
    return _send_with_retry(retry_session, retry_verify_ssl)


def _merge_csrf_header(headers: Any, session: requests.Session) -> Any:
    csrf_token = str(session.cookies.get("fs_csrf") or "").strip()
    if not csrf_token:
        csrf_token = str(session.headers.get("x-csrf-token") or "").strip()
    if not csrf_token:
        return headers
    if headers is None:
        return {"x-csrf-token": csrf_token}
    if isinstance(headers, dict):
        for key in headers:
            if str(key or "").strip().lower() == "x-csrf-token":
                return headers
        merged = dict(headers)
        merged["x-csrf-token"] = csrf_token
        return merged
    return headers


def _session_request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    verify_ssl: bool,
    attempts: int = 3,
    **kwargs: Any,
) -> requests.Response:
    last: requests.Response | None = None
    for attempt in range(1, attempts + 1):
        response = session.request(method, url, verify=verify_ssl, timeout=20, **kwargs)
        last = response
        if response.status_code >= 500 and attempt < attempts:
            time.sleep(0.2 * attempt)
            continue
        return response
    assert last is not None
    return last


def _ensure_registry_baseline(base_url: str, session: requests.Session, verify_ssl: bool) -> None:
    """Provision the neutral agent, skill and workflows the live suites run against.

    The built-in registry catalog is empty in 1.0, so the suites create their own
    neutral fixture entities through the public registry API once per session.
    """
    global _REGISTRY_BASELINE_READY
    if _REGISTRY_BASELINE_READY:
        return
    with _REGISTRY_BASELINE_LOCK:
        if _REGISTRY_BASELINE_READY:
            return

        def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
            if str(method).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                kwargs["headers"] = _merge_csrf_header(kwargs.get("headers") or {}, session)
            return _session_request_with_retry(
                session, method, url, verify_ssl=verify_ssl, **kwargs
            )

        ensure_neutral_registry(_request, base_url)
        for lane in ("backend", "ui"):
            project_id = str(os.environ.get(f"TEST_PROJECT_{lane.upper()}_ID") or "").strip()
            ensure_project_links(_request, base_url, project_id)
        _REGISTRY_BASELINE_READY = True


@pytest.fixture(autouse=True)
def e2e_authenticated_requests(request):
    """Patch requests.* for E2E tests so protected endpoints run authenticated by default."""
    if not request.node.get_closest_marker("e2e"):
        yield
        return
    if request.node.get_closest_marker("e2e_auth_public"):
        yield
        return
    if not _env_flag("E2E_ENFORCE_AUTH", True):
        yield
        return

    session, verify_ssl = _authed_session()

    def _wrap(method_name: str):
        method = getattr(session, method_name)
        method_upper = method_name.strip().upper()

        def _call(url: str, *args: Any, **kwargs: Any):
            kwargs.setdefault("verify", verify_ssl)
            if method_upper in {"POST", "PUT", "PATCH", "DELETE"}:
                kwargs["headers"] = _merge_csrf_header(kwargs.get("headers"), session)
            return method(url, *args, **kwargs)

        return _call

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(requests, "request", _wrap("request"))
    monkeypatch.setattr(requests, "get", _wrap("get"))
    monkeypatch.setattr(requests, "post", _wrap("post"))
    monkeypatch.setattr(requests, "put", _wrap("put"))
    monkeypatch.setattr(requests, "patch", _wrap("patch"))
    monkeypatch.setattr(requests, "delete", _wrap("delete"))
    try:
        yield
    finally:
        monkeypatch.undo()
