"""PostgreSQL backup process operations and libpq transport policy."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from core.application.lifecycle.backup_service import BackupGatewayError, BackupLog


class BackupPostgresContext(Protocol):
    """Borrow the configured transport and confinement policy without copying state."""

    @property
    def _project_root(self) -> Path: ...

    @property
    def _runner(self) -> Callable[..., subprocess.CompletedProcess[bytes]]: ...

    @property
    def _timeout_seconds(self) -> float: ...

    @property
    def _native_database(self) -> bool: ...

    @property
    def _database_name(self) -> str: ...

    @property
    def _database_user(self) -> str: ...

    def _require_confined_temp(self, path: Path) -> None: ...

    def _compose_prefix(self) -> tuple[str, ...]: ...

    def _database_connection_args(self) -> tuple[str, ...]: ...

    def _database_server_args(self) -> tuple[str, ...]: ...

    def _native_process_environment(self) -> dict[str, object]: ...


_REMEDIATION_RETAINED_RESTORE_LOG = "Keep maintenance enabled and inspect the retained restore log."


_AGE_RESTORE_CATALOG_REPAIR_SQL = r"""
/* FLOW_STEWARD_AGE_RESTORE_REPAIR */
DO $flow_steward$
DECLARE
    stale_graphs bigint;
BEGIN
    IF to_regclass('ag_catalog.ag_graph') IS NULL
       OR to_regclass('ag_catalog.ag_label') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'LOCK TABLE ag_catalog.ag_graph IN ACCESS EXCLUSIVE MODE';
    EXECUTE 'LOCK TABLE ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE';
    EXECUTE 'SELECT count(*) FROM ag_catalog.ag_graph '
            'WHERE graphid IS DISTINCT FROM namespace::oid'
       INTO stale_graphs;
    IF stale_graphs = 0 THEN
        RETURN;
    END IF;
    EXECUTE 'ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT fk_graph_oid';
    EXECUTE 'UPDATE ag_catalog.ag_label AS label SET graph = graph.namespace::oid '
            'FROM ag_catalog.ag_graph AS graph '
            'WHERE label.graph = graph.graphid '
            'AND graph.graphid IS DISTINCT FROM graph.namespace::oid';
    EXECUTE 'UPDATE ag_catalog.ag_graph SET graphid = namespace::oid '
            'WHERE graphid IS DISTINCT FROM namespace::oid';
    EXECUTE 'ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT fk_graph_oid '
            'FOREIGN KEY (graph) REFERENCES ag_catalog.ag_graph (graphid)';
    EXECUTE 'SELECT count(*) FROM ag_catalog.ag_graph '
            'WHERE graphid IS DISTINCT FROM namespace::oid'
       INTO stale_graphs;
    IF stale_graphs <> 0 THEN
        RAISE EXCEPTION 'AGE graph catalog identity repair did not converge';
    END IF;
END
$flow_steward$;
""".strip()


# P2: every libpq connection parameter a DSN may carry besides the five the gateway
# passes as arguments, and the environment variable libpq reads it from (PostgreSQL 18,
# 32.15 Environment Variables). Environment rather than argv, because the tools --
# pg_dump, pg_restore, psql, dropdb, createdb -- all honour it without changing their
# argument shapes, and it is not shown to other users by ps.
_LIBPQ_PARAMETER_ENVIRONMENT = {
    "hostaddr": "PGHOSTADDR",
    "passfile": "PGPASSFILE",
    "require_auth": "PGREQUIREAUTH",
    "channel_binding": "PGCHANNELBINDING",
    "service": "PGSERVICE",
    "options": "PGOPTIONS",
    "application_name": "PGAPPNAME",
    "sslmode": "PGSSLMODE",
    "requiressl": "PGREQUIRESSL",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcertmode": "PGSSLCERTMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslsni": "PGSSLSNI",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "requirepeer": "PGREQUIREPEER",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "gssencmode": "PGGSSENCMODE",
    "krbsrvname": "PGKRBSRVNAME",
    "gsslib": "PGGSSLIB",
    "gssdelegation": "PGGSSDELEGATION",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "client_encoding": "PGCLIENTENCODING",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "min_protocol_version": "PGMINPROTOCOLVERSION",
    "max_protocol_version": "PGMAXPROTOCOLVERSION",
}
# Passed as the gateway's own arguments already.
_LIBPQ_ARGUMENT_PARAMETERS = frozenset({"host", "port", "user", "dbname", "password"})


def _libpq_environment(parameters: Mapping[str, str]) -> dict[str, str]:
    """The environment that carries a DSN's remaining parameters to the libpq tools.

    A parameter libpq has no environment variable for -- sslpassword, keepalives and
    the like -- could reach the tools only on the command line, where the DSN must not
    go. It is refused by name, before any backup starts, rather than dropped.
    """
    environment: dict[str, str] = {}
    unsupported: list[str] = []
    for name, value in parameters.items():
        if name in _LIBPQ_ARGUMENT_PARAMETERS or value in (None, ""):
            continue
        variable = _LIBPQ_PARAMETER_ENVIRONMENT.get(name)
        if variable is None:
            unsupported.append(name)
            continue
        environment[variable] = str(value)
    if unsupported:
        raise ValueError(
            "FS_DB_DSN sets "
            + ", ".join(sorted(unsupported))
            + ", which the backup tools can receive only on the command line; remove it "
            "or supply it another way libpq reads, such as a service file"
        )
    return environment


def dump_database(
    self: BackupPostgresContext,
    destination: Path,
    *,
    log: BackupLog,
    _nonempty_regular_file: Callable[[Path], bool],
) -> None:
    self._require_confined_temp(destination)
    if self._native_database:
        command = ("pg_dump", *self._database_connection_args(), "-Fc")
    else:
        command = (
            *self._compose_prefix(),
            "exec",
            "-T",
            "pg-fs",
            "pg_dump",
            "-U",
            self._database_user,
            "-d",
            self._database_name,
            "-Fc",
        )
    log.write("Starting pg_dump custom-format export.")
    try:
        with destination.open("wb") as output:
            os.fchmod(output.fileno(), 0o600)
            outcome = self._runner(
                list(command),
                cwd=self._project_root,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
                **self._native_process_environment(),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.write("pg_dump could not be started.")
        raise BackupGatewayError(
            code="backup_pg_dump_failed",
            cause="PostgreSQL backup export could not be started.",
            remediation="Check Docker/PostgreSQL availability, inspect the retained log, and retry.",
            step="pg_dump",
        ) from exc
    if int(outcome.returncode) != 0:
        log.write(f"pg_dump failed with exit code {int(outcome.returncode)}.")
        raise BackupGatewayError(
            code="backup_pg_dump_failed",
            cause="PostgreSQL custom-format export failed.",
            remediation="Check Docker/PostgreSQL availability, inspect the retained log, and retry.",
            step="pg_dump",
        )
    if not _nonempty_regular_file(destination):
        log.write("pg_dump returned an empty export.")
        raise BackupGatewayError(
            code="backup_pg_dump_empty",
            cause="PostgreSQL produced an empty backup export.",
            remediation="Inspect PostgreSQL health and the retained backup log before retrying.",
            step="pg_dump",
            retryable=False,
        )
    log.write("pg_dump custom-format export completed.")


def inspect_dump(self: BackupPostgresContext, source: Path, *, log: BackupLog) -> None:
    self._require_confined_temp(source)
    command = (
        ("pg_restore", "-l")
        if self._native_database
        else (*self._compose_prefix(), "exec", "-T", "pg-fs", "pg_restore", "-l")
    )
    log.write("Starting non-mutating pg_restore archive listing.")
    try:
        with source.open("rb") as payload:
            outcome = self._runner(
                list(command),
                cwd=self._project_root,
                stdin=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
                **self._native_process_environment(),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.write("pg_restore archive listing could not be started.")
        raise BackupGatewayError(
            code="backup_pg_restore_list_failed",
            cause="The decrypted backup could not be inspected with pg_restore -l.",
            remediation="Check Docker/PostgreSQL tooling and inspect the retained log.",
            step="pg_restore_list",
        ) from exc
    if int(outcome.returncode) != 0:
        log.write(f"pg_restore -l failed with exit code {int(outcome.returncode)}.")
        raise BackupGatewayError(
            code="backup_pg_restore_list_failed",
            cause="The decrypted payload is not a readable PostgreSQL custom-format dump.",
            remediation="Create a new backup after checking PostgreSQL and available disk space.",
            step="pg_restore_list",
            retryable=False,
        )
    log.write("Non-mutating pg_restore archive listing completed.")


def restore_database(
    self: BackupPostgresContext,
    source: Path,
    *,
    log: BackupLog,
    maintenance_confirmed: bool,
    verified: bool,
    recovery_target: str,
    expected_recovery_target: str,
) -> None:
    """Restore one already decrypted and inspected dump through the configured transport."""
    self._require_confined_temp(source)
    if not maintenance_confirmed:
        raise ValueError("maintenance confirmation is required before restore")
    if not verified:
        raise ValueError("backup verification is required before restore")
    if not recovery_target or recovery_target != expected_recovery_target:
        raise ValueError("recovery target does not match this installation")
    if self._database_name in {"postgres", "template0", "template1"}:
        raise ValueError("PostgreSQL system databases cannot be restore targets")
    if self._native_database:
        server_args = self._database_server_args()
        reset_commands = (
            (
                "dropdb",
                "--if-exists",
                "--force",
                "--maintenance-db=postgres",
                *server_args,
                self._database_name,
            ),
            (
                "createdb",
                "--template=template0",
                f"--owner={self._database_user}",
                "--maintenance-db=postgres",
                *server_args,
                self._database_name,
            ),
        )
        command = (
            "pg_restore",
            "-v",
            "--no-owner",
            "--no-privileges",
            *self._database_connection_args(),
        )
        finalize_command = (
            "psql",
            *self._database_connection_args(),
            "-v",
            "ON_ERROR_STOP=1",
        )
    else:
        compose = (*self._compose_prefix(), "exec", "-T", "pg-fs")
        reset_commands = (
            (
                *compose,
                "dropdb",
                "-U",
                self._database_user,
                "--if-exists",
                "--force",
                self._database_name,
            ),
            (
                *compose,
                "createdb",
                "-U",
                self._database_user,
                "--template=template0",
                f"--owner={self._database_user}",
                self._database_name,
            ),
        )
        command = (
            *compose,
            "pg_restore",
            "-U",
            self._database_user,
            "-d",
            self._database_name,
            "-v",
            "--no-owner",
            "--no-privileges",
        )
        finalize_command = (
            *compose,
            "psql",
            "-U",
            self._database_user,
            "-d",
            self._database_name,
            "-v",
            "ON_ERROR_STOP=1",
        )
    log.write("Starting verified PostgreSQL restore in confirmed maintenance mode.")
    for reset_command in reset_commands:
        try:
            reset = self._runner(
                list(reset_command),
                cwd=self._project_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
                **self._native_process_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupGatewayError(
                code="backup_database_reset_failed",
                cause="The confirmed recovery database replacement could not be started.",
                remediation=_REMEDIATION_RETAINED_RESTORE_LOG,
                step="database_reset",
                retryable=False,
            ) from exc
        if int(reset.returncode) != 0:
            raise BackupGatewayError(
                code="backup_database_reset_failed",
                cause="The confirmed recovery database could not be replaced.",
                remediation=_REMEDIATION_RETAINED_RESTORE_LOG,
                step="database_reset",
                retryable=False,
            )
    try:
        with source.open("rb") as payload:
            outcome = self._runner(
                list(command),
                cwd=self._project_root,
                stdin=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
                **self._native_process_environment(),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupGatewayError(
            code="backup_pg_restore_failed",
            cause="PostgreSQL restore could not be started.",
            remediation="Check PostgreSQL availability and the retained restore log.",
            step="pg_restore",
        ) from exc
    if int(outcome.returncode) != 0:
        raise BackupGatewayError(
            code="backup_pg_restore_failed",
            cause="PostgreSQL restore failed.",
            remediation="Inspect the retained restore log before retrying recovery.",
            step="pg_restore",
            retryable=False,
        )
    try:
        finalized = self._runner(
            list(finalize_command),
            cwd=self._project_root,
            input=_AGE_RESTORE_CATALOG_REPAIR_SQL.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=self._timeout_seconds,
            **self._native_process_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupGatewayError(
            code="backup_database_finalize_failed",
            cause="PostgreSQL restore finalization could not be started.",
            remediation=_REMEDIATION_RETAINED_RESTORE_LOG,
            step="database_finalize",
            retryable=False,
        ) from exc
    if int(finalized.returncode) != 0:
        raise BackupGatewayError(
            code="backup_database_finalize_failed",
            cause="PostgreSQL restore finalization failed.",
            remediation=_REMEDIATION_RETAINED_RESTORE_LOG,
            step="database_finalize",
            retryable=False,
        )
    log.write("Verified PostgreSQL restore completed.")
