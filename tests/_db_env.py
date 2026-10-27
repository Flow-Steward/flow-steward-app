from __future__ import annotations

import os
import socket
from urllib.parse import unquote, urlparse


def dsn_host_port(dsn: str) -> tuple[str, int] | None:
    """Return ``(host, port)`` for a DSN, or ``None`` when there is no TCP host.

    libpq accepts two DSN spellings and the test harness sees both: the URI form
    (``postgresql://user@host:5432/db``) and the keyword/value form
    (``dbname=flow_steward host=pg-fs port=5432``). ``urlparse`` only understands
    the first — on a keyword/value DSN it returns ``hostname=None``, which is why
    reachability guards written against it silently never fire.

    Returns ``None`` for local socket connections and for DSNs with no host at
    all; callers should treat that as "nothing to probe".
    """
    normalized = str(dsn or "").strip()
    if not normalized:
        return None

    host = ""
    port_raw = ""
    if "://" in normalized:
        parsed = urlparse(normalized)
        host = parsed.hostname or ""
        port_raw = str(parsed.port or "")
    else:
        for token in normalized.split():
            key, sep, value = token.partition("=")
            if not sep:
                continue
            key = key.strip().lower()
            value = unquote(value.strip().strip("'\""))
            if key == "host":
                host = value
            elif key == "port":
                port_raw = value

    # libpq treats a leading "/" or "@" as a Unix-domain socket directory.
    if not host or host.startswith("/") or host.startswith("@"):
        return None
    try:
        port = int(port_raw) if port_raw else 5432
    except ValueError:
        port = 5432
    return host, port


def dsn_unreachable_reason(dsn: str) -> str | None:
    """Return why ``dsn``'s host cannot be resolved, or ``None`` when it can.

    Resolution failure is reported as a reason rather than raised so that
    harnesses can turn an unreachable database into an honest skip instead of a
    misleading red result.
    """
    target = dsn_host_port(dsn)
    if target is None:
        return None
    host, port = target
    try:
        socket.getaddrinfo(host, port)
    except OSError as exc:
        return f"{host}:{port} is not reachable from this test environment: {exc}"
    return None


def resolve_test_db_dsn() -> str:
    """Return the most stable DB DSN available for test harnesses."""
    for candidate in (
        os.environ.get("FS_DB_DSN"),
        os.environ.get("DATABASE_URL"),
        os.environ.get("LANGGRAPH_POSTGRES_URI"),
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized

    try:
        from tests.unit import conftest as unit_conftest
    except Exception:
        return ""

    preset_env = getattr(unit_conftest, "_PRESET_DB_ENV", {})
    for key in ("FS_DB_DSN", "DATABASE_URL"):
        normalized = str(preset_env.get(key, "") or "").strip()
        if normalized:
            return normalized
    return ""
