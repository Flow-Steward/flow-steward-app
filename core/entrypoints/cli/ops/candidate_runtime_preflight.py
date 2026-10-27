"""Read-only database and worker-identity preflight for release candidates."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import cast

from core.application.auth.worker_identity_bootstrap import (
    WorkerIdentityStore,
    is_valid_worker_identity,
)
from core.application.tooling.db.migration_runner import inspect_migrations
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.entrypoint_db_adapters import (
    connect_database,
    create_identity_store,
)

_COMPACT_WORKER_TOKEN_PATH = Path("/data/state/worker-mcp-token")
# AC17-R142 follow-up. On Compact the runtime issues this credential during its own
# bootstrap, and the installer's health step only checks that the container is running:
# measured on a fresh install, the preflight ran 0.2 s after the runtime started, before
# bootstrap had written the file. The check therefore waits, bounded, for the credential
# the workers will actually run with to become valid.
_IDENTITY_WAIT_SECONDS = 180.0
_IDENTITY_POLL_SECONDS = 2.0


def _worker_token() -> str:
    """The credential the workers actually run with.

    AC17-R142: on Compact that is the runtime's own file -- the runtime issues it at
    bootstrap and hands it to its worker roles, whatever the environment says. This
    used to prefer FS_WORKER_MCP_BEARER_TOKEN, which on Compact is a copy the installer
    kept in .env.prod and the runtime revokes when it issues its own; the check passed
    only while the installer's copy was the newest, some fifteen seconds per run. A
    profile without that file (Scalable) still reads the environment.
    """
    try:
        runtime_owned = _COMPACT_WORKER_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        runtime_owned = ""
    if runtime_owned:
        return runtime_owned
    return str(_runtime_env.get("FS_WORKER_MCP_BEARER_TOKEN") or "").strip()


def main() -> int:
    dsn = str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "").strip()
    account_id = str(_runtime_env.get("FS_DEFAULT_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID).strip()
    if not dsn:
        print("candidate database DSN is missing", file=sys.stderr)
        return 2

    try:
        connection = connect_database(dsn)
        try:
            migration_state = inspect_migrations(connection, root=Path("/app"))
        finally:
            connection.close()
    except Exception as exc:
        print(
            f"candidate database migration preflight failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    if migration_state["pending"] or migration_state["drifted"]:
        print(
            "candidate database migrations are not current: "
            f"pending={','.join(migration_state['pending']) or '-'} "
            f"drifted={','.join(migration_state['drifted']) or '-'}",
            file=sys.stderr,
        )
        return 2

    deadline = time.monotonic() + _IDENTITY_WAIT_SECONDS
    while True:
        token = _worker_token()
        if token:
            try:
                identity_store = cast(WorkerIdentityStore, create_identity_store(dsn))
                try:
                    principal = identity_store.resolve_machine_token(token)
                finally:
                    store_connection = getattr(identity_store, "conn", None)
                    if store_connection is not None:
                        store_connection.close()
            except Exception as exc:
                print(
                    f"candidate worker identity preflight failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 2
            if is_valid_worker_identity(principal, account_id=account_id):
                print("candidate database and worker identity preflight passed")
                return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(_IDENTITY_POLL_SECONDS)
    if not token:
        print("candidate worker credential is missing", file=sys.stderr)
    else:
        print(
            "candidate worker credential is invalid, revoked, under-scoped, "
            "project-restricted, or belongs to another account",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
