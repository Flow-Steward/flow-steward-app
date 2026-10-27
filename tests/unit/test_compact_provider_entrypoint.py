"""Provider-linked connection values become canonical Compact runtime URLs."""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import import_module
from os import environ
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


def _prepare_provider_environment() -> Callable[[], None]:
    module = import_module("core.entrypoints.cli.compact_provider_runtime")
    return module.prepare_provider_environment


def _clear_connection_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FS_DB_DSN",
        "DATABASE_URL",
        "POSTGRES_DSN",
        "LANGGRAPH_POSTGRES_URI",
        "RABBITMQ_URL",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "RABBITMQ_HOST",
        "RABBITMQ_PORT",
        "RABBITMQ_USER",
        "RABBITMQ_PASS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_environment_builds_canonical_urls_from_linked_service_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing URL construction must break linked-service startup."""
    _clear_connection_environment(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "pg.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5544")
    monkeypatch.setenv("POSTGRES_DB", "flow/steward")
    monkeypatch.setenv("POSTGRES_USER", "flow+steward")
    monkeypatch.setenv("POSTGRES_PASSWORD", "a/b+c")
    monkeypatch.setenv("RABBITMQ_HOST", "rabbit.internal")
    monkeypatch.setenv("RABBITMQ_PORT", "5673")
    monkeypatch.setenv("RABBITMQ_USER", "flow+steward")
    monkeypatch.setenv("RABBITMQ_PASS", "x/y+z")

    _prepare_provider_environment()()

    postgres = urlsplit(environ["FS_DB_DSN"])
    rabbitmq = urlsplit(environ["RABBITMQ_URL"])
    assert postgres.hostname == "pg.internal"
    assert postgres.port == 5544
    assert postgres.username == "flow%2Bsteward"
    # pragma: allowlist nextline secret -- synthetic credential fixture
    assert postgres.password == "a%2Fb%2Bc"
    assert postgres.path == "/flow%2Fsteward"
    assert environ["LANGGRAPH_POSTGRES_URI"] == environ["FS_DB_DSN"]
    assert rabbitmq.hostname == "rabbit.internal"
    assert rabbitmq.port == 5673
    assert rabbitmq.username == "flow%2Bsteward"
    # pragma: allowlist nextline secret -- synthetic credential fixture
    assert rabbitmq.password == "x%2Fy%2Bz"


@pytest.mark.parametrize(
    ("component_values", "other_service_values", "missing_name"),
    [
        (
            {"POSTGRES_HOST": "pg.internal"},
            # pragma: allowlist nextline secret -- synthetic credential fixture
            {"RABBITMQ_URL": "amqp://flow_steward:secret@rabbit:5672/flow"},
            "POSTGRES_PASSWORD",
        ),
        (
            {"RABBITMQ_HOST": "rabbit.internal"},
            {
                # pragma: allowlist nextline secret -- synthetic credential fixture
                "FS_DB_DSN": "postgresql://flow_steward:secret@pg:5432/flow_steward",
                # pragma: allowlist nextline secret -- synthetic credential fixture
                "LANGGRAPH_POSTGRES_URI": ("postgresql://flow_steward:secret@pg:5432/flow_steward"),
            },
            "RABBITMQ_PASS",
        ),
    ],
)
def test_provider_environment_refuses_incomplete_linked_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
    component_values: dict[str, str],
    other_service_values: dict[str, str],
    missing_name: str,
) -> None:
    """Defaulting a missing linked-service password must fail this test."""
    _clear_connection_environment(monkeypatch)
    for name, value in {**component_values, **other_service_values}.items():
        monkeypatch.setenv(name, value)

    prepare_environment = _prepare_provider_environment()
    with pytest.raises(ValueError, match=missing_name):
        prepare_environment()


def test_existing_canonical_connection_urls_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overwriting explicit canonical URLs must fail this test."""
    _clear_connection_environment(monkeypatch)
    # pragma: allowlist nextline secret -- synthetic credential fixture
    database_url = "postgresql://flow_steward:secret@existing-db:5432/flow_steward"
    # pragma: allowlist nextline secret -- synthetic credential fixture
    langgraph_url = "postgresql://graph:secret@existing-graph:5432/langgraph"
    # pragma: allowlist nextline secret -- synthetic credential fixture
    rabbitmq_url = "amqp://flow_steward:secret@existing-rabbit:5672/flow"
    monkeypatch.setenv("FS_DB_DSN", database_url)
    monkeypatch.setenv("LANGGRAPH_POSTGRES_URI", langgraph_url)
    monkeypatch.setenv("RABBITMQ_URL", rabbitmq_url)
    monkeypatch.setenv("POSTGRES_HOST", "ignored-postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored-secret")
    monkeypatch.setenv("RABBITMQ_HOST", "ignored-rabbit")
    monkeypatch.setenv("RABBITMQ_PASS", "ignored-secret")

    _prepare_provider_environment()()

    assert environ["FS_DB_DSN"] == database_url
    assert environ["LANGGRAPH_POSTGRES_URI"] == langgraph_url
    assert environ["RABBITMQ_URL"] == rabbitmq_url


def test_missing_langgraph_url_uses_linked_postgres_without_replacing_database_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coupling LangGraph to an explicit libpq DSN must fail this test."""
    _clear_connection_environment(monkeypatch)
    database_dsn = "dbname=flow user=flow password=secret host=primary-db port=5432"
    monkeypatch.setenv("FS_DB_DSN", database_dsn)
    monkeypatch.setenv("POSTGRES_HOST", "graph-db.internal")
    monkeypatch.setenv("POSTGRES_PASSWORD", "graph/secret")
    monkeypatch.setenv(
        "RABBITMQ_URL",
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "amqp://flow_steward:secret@provider-rabbit:5672/flow",
    )

    _prepare_provider_environment()()

    assert environ["FS_DB_DSN"] == database_dsn
    assert environ["LANGGRAPH_POSTGRES_URI"] == (
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "postgresql://flow_steward:graph%2Fsecret@graph-db.internal:5432/flow_steward"
    )


def test_existing_database_url_populates_missing_canonical_postgres_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring a provider-supplied DATABASE_URL must fail this test."""
    _clear_connection_environment(monkeypatch)
    # pragma: allowlist nextline secret -- synthetic credential fixture
    database_url = "postgresql://flow_steward:secret@provider-db:5432/flow_steward"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "RABBITMQ_URL",
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "amqp://flow_steward:secret@provider-rabbit:5672/flow",
    )

    _prepare_provider_environment()()

    assert environ["FS_DB_DSN"] == database_url
    assert environ["LANGGRAPH_POSTGRES_URI"] == database_url


def test_provider_entrypoint_returns_configuration_exit_before_runtime_import(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Importing the heavy runtime before provider validation must fail this test."""
    _clear_connection_environment(monkeypatch)
    module = import_module("core.entrypoints.cli.compact_provider_runtime")

    assert module.main() == 78
    assert "POSTGRES_HOST" in capsys.readouterr().err


def test_provider_entrypoint_delegates_after_environment_is_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the canonical adapter before runtime delegation must fail this test."""
    _clear_connection_environment(monkeypatch)
    # pragma: allowlist nextline secret -- synthetic credential fixture
    database_url = "postgresql://flow_steward:secret@provider-db:5432/flow_steward"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv(
        "RABBITMQ_URL",
        # pragma: allowlist nextline secret -- synthetic credential fixture
        "amqp://flow_steward:secret@provider-rabbit:5672/flow",
    )
    monkeypatch.setitem(
        sys.modules,
        "core.entrypoints.cli.compact_runtime",
        SimpleNamespace(main=lambda: 23),
    )
    module = import_module("core.entrypoints.cli.compact_provider_runtime")

    assert module.main() == 23
    assert environ["FS_DB_DSN"] == database_url
