"""Thin PID-1 entrypoint for the fixed Compact production runtime."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, cast

from core.application.auth.bootstrap_self_mint import self_mint_bootstrap_link
from core.application.auth.worker_identity_bootstrap import (
    WorkerIdentityBootstrapService,
    WorkerIdentityStore,
)
from core.application.tooling.db import init_db, upgrade_db
from core.infrastructure.config.compact_runtime import CompactPortMap, CompactRuntimeSettings
from core.infrastructure.config.connection_strings import resolve_rabbitmq_url
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.rabbitmq_transport import build_rabbitmq_parameters
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.worker_identity_secret_store import (
    FileWorkerIdentitySecretStore,
)
from core.infrastructure.runtime.artifact_storage_factory import validate_artifact_storage_backend
from core.infrastructure.runtime.compact.bootstrap import CompactBootstrap
from core.infrastructure.runtime.compact.extension_dependencies import (
    materialize_compact_extension_dependencies,
)
from core.infrastructure.runtime.compact.health import (
    CompactHealthServer,
    RabbitMQProbeTarget,
    _postgres_ready,
    _rabbitmq_ready,
    consume_observations,
    monitor_dependencies,
    monitor_roles,
)
from core.infrastructure.runtime.compact.process_spec import build_process_specs
from core.infrastructure.runtime.compact.provenance import verify_compact_environment_identity
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.compact.router import CompactRouterConfig, render_router_config
from core.infrastructure.runtime.compact.runtime_secrets import establish_runtime_secrets
from core.infrastructure.runtime.compact.supervisor import CompactSupervisor
from core.infrastructure.runtime.compact.volume import (
    CompactVolumeInUse,
    CompactVolumeLayout,
    CompactVolumeLock,
    acquire_compact_volume_lock,
)
from core.infrastructure.runtime.entrypoint_db_adapters import (
    create_identity_store,
    create_user_counter,
)
from core.infrastructure.runtime.worker.worker_schema_compat import _run_schema_compatibility_check


class CompactBootstrapResult(NamedTuple):
    """What PID 1 learned while bringing the installation up."""

    worker_token: str
    bootstrap_state_file: str


def _bootstrap(data_root: Path) -> CompactBootstrapResult:
    dsn = str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("Compact bootstrap requires FS_DB_DSN")
    secret_store = FileWorkerIdentitySecretStore(data_root / "state" / "worker-mcp-token")

    def establish_baseline() -> None:
        init_db.bootstrap_canonical_schema_if_empty(dsn=dsn)
        upgrade_db.baseline_migration_history(
            dsn=dsn,
            author="compact-bootstrap",
        )

    def seed_database() -> None:
        init_db.initialize_database()

    def ensure_identity() -> None:
        identity_store = create_identity_store(dsn)
        WorkerIdentityBootstrapService(
            store=cast(WorkerIdentityStore, identity_store),
            secret_store=secret_store,
        ).ensure(account_id=str(_runtime_env.get("FS_DEFAULT_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID))

    CompactBootstrap(
        data_root=data_root,
        schema=establish_baseline,
        seed=seed_database,
        identity=ensure_identity,
        verify_schema=_run_schema_compatibility_check,
        extension_dependencies=materialize_compact_extension_dependencies,
        runtime_secrets=_establish_runtime_secrets,
    ).run()
    state_file = _sync_bootstrap_token_state(data_root, user_count=create_user_counter(dsn))
    token = secret_store.read()
    if not token:
        raise RuntimeError("Compact worker identity bootstrap returned no credential")
    return CompactBootstrapResult(worker_token=token, bootstrap_state_file=state_file)


def _establish_runtime_secrets(data_root: Path, layout: CompactVolumeLayout) -> None:
    """Fill in the long-lived secrets nobody handed this installation, once."""
    del layout
    outcome = establish_runtime_secrets(
        state_path=data_root / "state" / "runtime-secrets.json",
        environment=os.environ,
    )
    # Names only, never values: unlike the setup link these are not meant to be read
    # out of a log, and the line exists so an operator can tell "generated for you"
    # from "read back from the volume" without guessing.
    print(
        "Runtime secrets: "
        f"generated={', '.join(outcome.minted) or 'none'}; "
        f"restored={', '.join(outcome.reused) or 'none'}; "
        f"supplied={', '.join(outcome.operator_supplied) or 'none'}.",
        file=sys.stderr,
    )
    for name in outcome.rotated:
        print(
            f"{name} differs from the value this installation last ran with. Anything "
            "written under the previous one cannot be read with this one.",
            file=sys.stderr,
        )


def _sync_bootstrap_token_state(data_root: Path, *, user_count: Callable[[], int]) -> str:
    """Return the state file the app should honour, minting a link if nobody else did."""
    runtime_root = data_root / "state" / ".flow-steward" / "runtime"
    state_path = runtime_root / "bootstrap-token-state.json"
    token = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN") or "").strip()
    expires_at = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT") or "").strip()
    if token and expires_at:
        _write_bootstrap_token_state(state_path, token=token, expires_at=expires_at)
        return str(state_path)

    outcome = self_mint_bootstrap_link(state_path=state_path, user_count=user_count)
    if outcome.minted and outcome.expires_at is not None:
        _write_bootstrap_token_state(
            state_path,
            token=outcome.token,
            expires_at=outcome.expires_at.isoformat(),
        )
        # The one line carrying the secret, on the diagnostic stream rather than the
        # one a machine reads, which is the rule the installer's own entrypoint keeps.
        print(
            "Flow Steward has no administrator yet. Open this one-time setup link "
            f"before {outcome.expires_at.isoformat()} to create the first one, and "
            f"treat it as a secret: {outcome.link}",
            file=sys.stderr,
        )
        return str(state_path)

    # Steady state on a healthy installation, so it is worded as a statement rather
    # than a failure -- but it is stated, because a decision that leaves no trace is
    # the one that costs a round to explain.
    print(
        f"No first-administrator setup link was issued: {outcome.declined_because}.",
        file=sys.stderr,
    )
    # An expired file still answers more honestly than falling back to the localhost
    # rule: the operator is told the link has expired instead of being told nothing.
    return str(state_path) if state_path.exists() else ""


def _write_bootstrap_token_state(state_path: Path, *, token: str, expires_at: str) -> None:
    payload = {
        "expires_at": expires_at,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    temporary = state_path.parent / ".bootstrap-token-state.json.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, 10102)
        os.chmod(temporary, 0o640)
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_bootstrap_state_file(environment: dict[str, str], state_file: str) -> None:
    """Name the setup-token state file for the app, unless something already did.

    The app only consults a state file when FS_AUTH_BOOTSTRAP_STATE_FILE names one,
    and on an installation nobody installed nothing ever does -- which is why a
    self-minted link needs this line to have any effect at all. An installer-managed
    installation already names the same file through the /run symlink, and two
    spellings of one path is a difference an operator would have to chase down only
    to find it is none.
    """
    if state_file and not str(environment.get("FS_AUTH_BOOTSTRAP_STATE_FILE") or "").strip():
        environment["FS_AUTH_BOOTSTRAP_STATE_FILE"] = state_file


def _dsn_user(dsn: str) -> str:
    """The role named in a DSN, for the dependency probe; empty when it names none.

    Parsed by libpq's own rules: a keyword/value DSN has no URL shape, and a URL
    parser reads its host as None (the remediation ledger's DB-skip lesson).
    """
    try:
        from psycopg.conninfo import conninfo_to_dict

        return str(conninfo_to_dict(dsn).get("user") or "")
    except Exception:
        return ""


# The volume lock's descriptor is held here for the life of the process.
_HELD_VOLUME_LOCKS: list[CompactVolumeLock] = []


def _postgres_probe_target(dsn: str) -> tuple[tuple[str, int], str]:
    """Return the network address and role from the canonical PostgreSQL DSN."""
    try:
        from psycopg import ProgrammingError
        from psycopg.conninfo import conninfo_to_dict

        connection = conninfo_to_dict(dsn)
        host = str(connection.get("host") or "").strip()
        port = int(connection.get("port") or 5432)
    except (ProgrammingError, TypeError, ValueError) as exc:
        raise ValueError("FS_DB_DSN must name a valid PostgreSQL network endpoint") from exc
    if not host or not 1 <= port <= 65_535:
        raise ValueError("FS_DB_DSN must name a valid PostgreSQL network endpoint")
    return (host, port), str(connection.get("user") or "")


def _rabbitmq_probe_target(url: str) -> RabbitMQProbeTarget:
    """Retain the canonical client's verified transport for dependency probes."""
    from pika import URLParameters

    parameters = build_rabbitmq_parameters(url, parameter_factory=URLParameters)
    options = parameters.ssl_options
    return RabbitMQProbeTarget(
        address=(parameters.host, parameters.port),
        ssl_context=options.context if options is not None else None,
        server_hostname=options.server_hostname if options is not None else None,
    )


def _wait_for_bootstrap_dependencies(
    *,
    postgres: tuple[str, int],
    rabbitmq: RabbitMQProbeTarget,
    postgres_user: str,
    timeout_seconds: int,
    postgres_ready: Callable[..., bool] = _postgres_ready,
    rabbitmq_ready: Callable[..., bool] = _rabbitmq_ready,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for both protocols within the existing Compact startup budget."""
    deadline = clock() + timeout_seconds
    while True:
        remaining = max(0.0, deadline - clock())
        postgres_up = postgres_ready(postgres, postgres_user, min(remaining, 2.0))
        remaining = max(0.0, deadline - clock())
        rabbitmq_up = rabbitmq_ready(rabbitmq, min(remaining, 2.0))
        if postgres_up and rabbitmq_up:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            unavailable = [
                name
                for name, ready in (("PostgreSQL", postgres_up), ("RabbitMQ", rabbitmq_up))
                if not ready
            ]
            raise RuntimeError(
                f"{' and '.join(unavailable)} did not become ready before Compact bootstrap"
            )
        sleep(min(2.0, remaining))


def main() -> int:
    try:
        validate_artifact_storage_backend(_runtime_env.get("ARTIFACT_STORAGE_BACKEND"))
        settings = CompactRuntimeSettings.from_environment(os.environ)
        verify_compact_environment_identity(os.environ)
        public_port = int(
            _runtime_env.get("PORT", _runtime_env.get("FS_RUNTIME_HTTP_PORT", "8080"))
        )
        postgres_port = int(_runtime_env.get("POSTGRES_PORT", "5432"))
        if not 1 <= postgres_port <= 65535:
            raise ValueError("POSTGRES_PORT must be between 1 and 65535")
        postgres_target = (str(_runtime_env.get("POSTGRES_HOST") or "pg-fs"), postgres_port)
        rabbitmq_target = _rabbitmq_probe_target(resolve_rabbitmq_url())
        postgres_user = _dsn_user(
            str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "")
        )
        ports = CompactPortMap.build(
            public_port=public_port,
            worker_processes=settings.worker_processes,
        )
        if str(_runtime_env.get("FS_COMPACT_WAIT_FOR_DEPENDENCIES") or "") == "1":
            postgres_target, postgres_user = _postgres_probe_target(
                str(_runtime_env.get("FS_DB_DSN") or "")
            )
            _wait_for_bootstrap_dependencies(
                postgres=postgres_target,
                rabbitmq=rabbitmq_target,
                postgres_user=postgres_user,
                timeout_seconds=settings.startup_timeout_seconds,
            )
        data_root = Path(_runtime_env.get("FS_COMPACT_DATA_ROOT", "/data"))
        # P1-2: one runtime per volume, taken before bootstrap touches anything and held
        # until this process exits -- the kernel releases it however that happens.
        _HELD_VOLUME_LOCKS.append(acquire_compact_volume_lock(data_root))
        bootstrapped = _bootstrap(data_root)
        process_environment = dict(os.environ)
        process_environment["FS_WORKER_MCP_BEARER_TOKEN"] = bootstrapped.worker_token
        _publish_bootstrap_state_file(process_environment, bootstrapped.bootstrap_state_file)
        router_path = Path("/run/flow-steward-compact/router.yaml")
        router_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        router_path.write_text(
            render_router_config(CompactRouterConfig(public_port=public_port)),
            encoding="utf-8",
        )
        router_path.chmod(0o644)
        observation_read, observation_write = os.pipe()
        registry = ReadinessRegistry(
            worker_processes=settings.worker_processes,
            stale_seconds=settings.health_stale_seconds,
        )
        registry.set_bootstrap_ready(True)
        registry.set_schema_ready(True)
        specs = build_process_specs(
            settings,
            parent_environment=process_environment,
            public_port=public_port,
            observation_fd=observation_write,
        )
    except CompactVolumeInUse as exc:
        # Not a configuration error, and not this instance's to fix: the volume is in
        # use. EX_TEMPFAIL, so a restart policy retries once the other one has gone.
        print(str(exc), file=sys.stderr)
        return 75
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"Compact configuration error: {exc}", file=sys.stderr)
        return 78

    reader = threading.Thread(
        target=consume_observations,
        args=(observation_read, registry),
        daemon=True,
    )
    reader.start()
    dependency_stop = threading.Event()
    dependency_monitor = threading.Thread(
        target=monitor_dependencies,
        kwargs={
            "registry": registry,
            "postgres": postgres_target,
            "rabbitmq": rabbitmq_target,
            "stop": dependency_stop,
            "interval_seconds": settings.health_interval_seconds,
            "postgres_user": postgres_user,
        },
        daemon=True,
    )
    dependency_monitor.start()
    role_monitor = threading.Thread(
        target=monitor_roles,
        kwargs={
            "registry": registry,
            "specs": specs,
            "stop": dependency_stop,
            "interval_seconds": settings.health_interval_seconds,
        },
        daemon=True,
    )
    role_monitor.start()
    health = CompactHealthServer(registry, port=ports.health_port)
    health.start()
    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        role_state=registry.set_role_ready,
        readiness=lambda: registry.snapshot().ready,
        runtime_reset=registry.reset_runtime,
        role_reset=registry.reset_role,
    )
    signal.signal(signal.SIGTERM, supervisor.request_shutdown)
    signal.signal(signal.SIGINT, supervisor.request_shutdown)
    from core.application.monitoring.runtime_composition import start_optional_compact_reporting

    status_publisher = start_optional_compact_reporting(
        runtime_values=dict(_runtime_env), registry=registry
    )
    try:
        return supervisor.run()
    finally:
        dependency_stop.set()
        if status_publisher is not None:
            status_publisher.close()
        os.close(observation_write)
        health.close()
        reader.join(timeout=5)
        dependency_monitor.join(timeout=5)
        role_monitor.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
