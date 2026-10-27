"""Internal installer entrypoint for worker identity provisioning."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from core.application.auth.worker_identity_bootstrap import (
    WorkerIdentityBootstrapService,
    WorkerIdentityStore,
)
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.installer.worker_identity_secret_store import (
    EnvWorkerIdentitySecretStore,
)
from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    args = parser.parse_args()
    env_path = Path(args.env_file).resolve()
    # Read through the canonical runtime environment rather than os.environ, so
    # the variable stays on the audited, registered surface.
    dsn = str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("worker identity bootstrap requires FS_DB_DSN")
    # Entrypoints go through the adapter rather than importing a concrete store:
    # the dependency budget keeps database infrastructure out of this layer.
    identity_store = create_identity_store(dsn)
    secret_store = EnvWorkerIdentitySecretStore(
        env_path=env_path,
        state_store=InstallerStateStore(env_path.parent / ".flow-steward"),
    )
    WorkerIdentityBootstrapService(
        store=cast(WorkerIdentityStore, identity_store),
        secret_store=secret_store,
    ).ensure(account_id=str(args.account_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
