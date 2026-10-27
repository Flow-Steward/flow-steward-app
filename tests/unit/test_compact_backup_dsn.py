"""P2: a Compact backup dropped every DSN parameter but the five it passes as arguments.

_native_backup_gateway took host, port, user, dbname and password from FS_DB_DSN and
discarded the rest. A database that requires verified TLS -- sslmode=verify-full with
a root certificate -- was then dumped with libpq's default and no certificate check,
and one that requires a client certificate could not be dumped at all.
"""

from __future__ import annotations

import pytest

_VERIFYING_DSN = (
    "host=db.example.test port=5432 dbname=flow_steward user=flow_steward "
    "password=s3cret sslmode=verify-full sslrootcert=/etc/ssl/db-ca.pem "
    "sslcert=/etc/ssl/client.pem sslkey=/etc/ssl/client.key"
)


def _gateway(monkeypatch: pytest.MonkeyPatch, dsn: str):
    from core.entrypoints.cli import compact_platform_runtime

    monkeypatch.setenv("FS_DB_DSN", dsn)
    return compact_platform_runtime._native_backup_gateway()


def test_tls_parameters_reach_the_backup_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _gateway(monkeypatch, _VERIFYING_DSN)

    environment = gateway._native_process_environment()["env"]

    assert environment["PGSSLMODE"] == "verify-full"
    assert environment["PGSSLROOTCERT"] == "/etc/ssl/db-ca.pem"
    assert environment["PGSSLCERT"] == "/etc/ssl/client.pem"
    assert environment["PGSSLKEY"] == "/etc/ssl/client.key"
    assert environment["PGPASSWORD"] == "s3cret"


def test_nothing_from_the_dsn_reaches_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason the DSN is split at all: argv is visible to every user on the host."""
    gateway = _gateway(monkeypatch, _VERIFYING_DSN)

    arguments = " ".join(gateway._database_connection_args())

    assert "s3cret" not in arguments
    assert "sslmode" not in arguments
    assert "verify-full" not in arguments


def test_a_parameter_libpq_cannot_take_from_the_environment_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping it silently is the defect; carrying it on argv would leak it."""
    with pytest.raises(ValueError, match="sslpassword"):
        _gateway(monkeypatch, _VERIFYING_DSN + " sslpassword=keyphrase")
