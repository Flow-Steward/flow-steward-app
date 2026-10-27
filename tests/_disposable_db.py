"""Disposable canonical PostgreSQL for integration tests.

Remediation epic 2026-07-29, WS-01. Backend integration evidence may not be
produced against the live dev database — several suites write to it — and it may
not be skipped forever either. This module starts a throwaway PostgreSQL
container from the same image the stack ships, applies the canonical schema and
every SQL migration, hands out its DSN, and removes it afterwards.

Inside lint.sh, the session fixture reuses the gate-owned database only after
verifying its explicit isolation marker and exact audit connection target. The
gate owns its initialization and teardown. Outside that environment, the
container gets a random name and an ephemeral host port and is removed on
teardown even when a test fails. Neither path uses the running product database.

Opt out with ``FS_TEST_DISPOSABLE_DB=0`` (tests then skip rather than fail).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_IMAGE = "fs-postgres-extended:0.1.0-pgvector-0.8.5"
DB_NAME = "flow_steward"
DB_USER = "flow_steward"
DB_PASSWORD = "disposable-test-password"  # pragma: allowlist secret
READY_TIMEOUT_SECONDS = 90


class DisposableDatabaseUnavailable(RuntimeError):
    """The disposable database cannot be provided in this environment."""


@dataclass(frozen=True)
class DisposableDatabase:
    container: str
    host: str
    port: int
    db_name: str = DB_NAME
    db_user: str = DB_USER
    db_password: str = DB_PASSWORD

    @property
    def dsn(self) -> str:
        """libpq keyword/value DSN — the spelling the platform's own `.env` uses."""
        from psycopg.conninfo import make_conninfo

        return make_conninfo(
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_password,
            host=self.host,
            port=self.port,
        )

    @property
    def uri(self) -> str:
        """URI DSN — LangGraph's PostgresSaver only accepts this spelling."""
        return (
            f"postgresql://{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
            f"@{self.host}:{self.port}/{quote(self.db_name, safe='')}"
            "?sslmode=disable"
        )


_AUDIT_TARGET = ("pg-audit", "5432", "audit")
_AUDIT_DSN_KEYS = frozenset({"host", "port", "dbname", "user", "password", "sslmode"})
_AUDIT_ENV_OVERRIDES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")


def _audit_connection_info(dsn: str) -> dict[str, str]:
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    try:
        info = conninfo_to_dict(dsn)
    except (ProgrammingError, ValueError):
        raise ValueError("Invalid isolated audit database connection") from None
    from tests._xdist_isolation import AUDIT_DATABASE, audit_database_name

    target = (info.get("host"), info.get("port", "5432"), info.get("user"))
    # A parallel gate gives each xdist worker its own copy of the audit
    # database (tests/_xdist_isolation.py); no other database name is accepted.
    allowed_databases = {AUDIT_DATABASE, audit_database_name()}
    if (
        target != _AUDIT_TARGET
        or info.get("dbname") not in allowed_databases
        or set(info) - _AUDIT_DSN_KEYS
    ):
        raise ValueError("Unsafe isolated audit database target or connection override")
    return info


def isolated_audit_database() -> DisposableDatabase | None:
    """Reuse only lint.sh's explicitly isolated canonical database; never a live DSN.

    The gate owns initialization and teardown. Reject ambiguous libpq routes and
    conflicting alternate DSNs instead of silently skipping the integration tests.
    """
    if os.environ.get("FS_AUDIT_ISOLATED") != "1":
        return None
    if any(os.environ.get(key, "").strip() for key in _AUDIT_ENV_OVERRIDES):
        raise ValueError("Unsafe isolated audit database environment override")
    info = _audit_connection_info(os.environ.get("FS_DB_DSN", ""))
    for key in ("DATABASE_URL", "LANGGRAPH_POSTGRES_URI"):
        if os.environ.get(key):
            _audit_connection_info(os.environ[key])
    return DisposableDatabase(
        container="",
        host="pg-audit",
        port=5432,
        db_name=info["dbname"],
        db_user="audit",
        db_password=info.get("password", ""),
    )


def disposable_db_image() -> str:
    return os.environ.get("FS_TEST_POSTGRES_IMAGE", DEFAULT_IMAGE).strip() or DEFAULT_IMAGE


def published_host() -> str:
    """Return the host that reaches Docker-published ports from this test process."""
    return os.environ.get("FS_TEST_DOCKER_PUBLISHED_HOST", "").strip() or "127.0.0.1"


def _schema_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "db" / "schema")


def _run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def unavailable_reason() -> str | None:
    """Return why a disposable database cannot be started, or ``None``."""
    if os.environ.get("FS_TEST_DISPOSABLE_DB", "").strip() in {"0", "false", "no"}:
        return "disabled by FS_TEST_DISPOSABLE_DB"
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    probe = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    if probe.returncode != 0:
        return "docker daemon is not reachable"
    image = disposable_db_image()
    inspected = _run(["docker", "image", "inspect", image], timeout=30)
    if inspected.returncode != 0:
        return f"canonical PostgreSQL image {image!r} is not available locally"
    return None


def start_disposable_db() -> DisposableDatabase:
    """Start a throwaway PostgreSQL container and wait until it accepts queries."""
    reason = unavailable_reason()
    if reason:
        raise DisposableDatabaseUnavailable(reason)

    container = f"fs-disposable-test-db-{uuid.uuid4().hex[:12]}"
    created = _run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            # Ephemeral host port: never collides with the running stack.
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_DB={DB_NAME}",
            "--env",
            f"POSTGRES_USER={DB_USER}",
            "--env",
            f"POSTGRES_PASSWORD={DB_PASSWORD}",
            # Same path the stack uses (infra/docker/docker-compose.yaml): the
            # postgres entrypoint applies db/schema/*.sql on first boot, so the
            # disposable database is built the canonical way rather than by a
            # test-only reimplementation that could drift from it.
            "--volume",
            f"{_schema_dir()}:/docker-entrypoint-initdb.d:ro",
            # Throwaway data: durability costs wall-clock and buys nothing here.
            "--tmpfs",
            "/var/lib/postgresql/data:rw",
            disposable_db_image(),
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
            "-c",
            "synchronous_commit=off",
            # Match the stack's ceiling so a leaked transaction cannot wedge a
            # test run the way it wedged the platform (RD-001 / RD-002).
            "-c",
            "idle_in_transaction_session_timeout=60s",
        ],
        timeout=180,
    )
    if created.returncode != 0:
        raise DisposableDatabaseUnavailable(
            f"could not start disposable PostgreSQL: {created.stderr.strip()[:400]}"
        )

    try:
        published = _run(["docker", "port", container, "5432/tcp"], timeout=30)
        if published.returncode != 0 or not published.stdout.strip():
            raise DisposableDatabaseUnavailable("disposable PostgreSQL published no host port")
        # "127.0.0.1:49173" (possibly several lines; take the first IPv4 mapping)
        mapping = next(
            line for line in published.stdout.splitlines() if line.strip().startswith("127.0.0.1:")
        )
        _host, _, port_text = mapping.strip().rpartition(":")
        database = DisposableDatabase(
            container=container,
            host=published_host(),
            port=int(port_text),
        )
        _wait_until_ready(database)
        return database
    except Exception:
        stop_disposable_db(container)
        raise


def _wait_until_ready(database: DisposableDatabase) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        ready = _run(
            ["docker", "exec", database.container, "pg_isready", "-U", DB_USER, "-d", DB_NAME],
            timeout=20,
        )
        if ready.returncode == 0:
            # pg_isready goes green slightly before the server accepts real
            # connections during first-boot initdb; confirm with a real query.
            try:
                import psycopg

                with (
                    psycopg.connect(database.dsn, connect_timeout=5) as conn,
                    conn.cursor() as cur,
                ):
                    cur.execute("SELECT 1")
                    cur.fetchone()
                return
            except Exception as exc:  # retried until the deadline
                last_error = str(exc)
        else:
            last_error = ready.stderr.strip() or ready.stdout.strip()
        time.sleep(0.5)
    raise DisposableDatabaseUnavailable(
        f"disposable PostgreSQL did not become ready in {READY_TIMEOUT_SECONDS}s: {last_error[:300]}"
    )


def stop_disposable_db(container: str) -> None:
    """Remove the container. Safe to call more than once."""
    _run(["docker", "rm", "--force", "--volumes", container], timeout=60)


@contextlib.contextmanager
def redirected_to(database: DisposableDatabase):
    """Point every DSN lookup at the disposable database for the duration.

    Both the environment and the already-resolved ``CONFIG`` mapping have to be
    redirected: `CONFIG` is populated at import time, so parts of the upgrade
    path (`init_platform_observability_schema`) would otherwise still resolve
    the compose-internal `pg-fs` host.
    """
    from core.infrastructure.config.runtime_config import CONFIG

    env_values = {
        "FS_DB_DSN": database.dsn,
        "DATABASE_URL": database.uri,
        "LANGGRAPH_POSTGRES_URI": database.uri,
    }
    config_values = {
        "fs_db_dsn": database.dsn,
        "langgraph_postgres_uri": database.uri,
    }
    previous_env = {key: os.environ.get(key) for key in env_values}
    previous_config = {key: CONFIG.get(key) for key in config_values}
    os.environ.update(env_values)
    CONFIG.update(config_values)
    try:
        yield database
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in previous_config.items():
            if value is None:
                CONFIG.pop(key, None)
            else:
                CONFIG[key] = value


def apply_canonical_state(database: DisposableDatabase) -> None:
    """Apply the canonical seeds and every pending SQL migration.

    The canonical schema itself is applied by the PostgreSQL entrypoint from the
    mounted ``db/schema`` directory before this runs.
    """
    from core.application.tooling.db import init_db, upgrade_db

    with redirected_to(database):
        upgrade_db.upgrade_database(dsn=database.dsn, author="disposable-test-db")
        init_db.apply_clean_bootstrap(dsn=database.dsn, author="disposable-test-db")


__all__ = [
    "DB_NAME",
    "DB_PASSWORD",
    "DB_USER",
    "DisposableDatabase",
    "DisposableDatabaseUnavailable",
    "apply_canonical_state",
    "disposable_db_image",
    "isolated_audit_database",
    "published_host",
    "redirected_to",
    "start_disposable_db",
    "stop_disposable_db",
    "unavailable_reason",
]
