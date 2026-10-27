from __future__ import annotations

from core.infrastructure.config.connection_strings import (
    build_postgres_dsn,
    resolve_langgraph_postgres_uri,
    resolve_postgres_dsn,
    resolve_rabbitmq_url,
)


def test_postgres_dsn_is_derived_from_component_variables() -> None:
    env = {
        "POSTGRES_DB": "flow",
        "POSTGRES_USER": "fs_user",
        "POSTGRES_PASSWORD": "secret value",
        "POSTGRES_HOST": "pg",
        "POSTGRES_PORT": "15432",
    }

    assert build_postgres_dsn(env) == (
        "dbname=flow user=fs_user password='secret value' host=pg port=15432"
    )


def test_explicit_postgres_dsn_remains_advanced_override() -> None:
    env = {
        "FS_DB_DSN": "postgresql://override",
        "POSTGRES_DB": "flow",
        "POSTGRES_USER": "fs_user",
        "POSTGRES_PASSWORD": "secret",
    }

    assert resolve_postgres_dsn(env) == "postgresql://override"


def test_postgres_dsn_is_empty_without_components_or_override() -> None:
    assert resolve_postgres_dsn({}) == ""


def test_postgres_builders_do_not_invent_default_passwords() -> None:
    env = {
        "POSTGRES_DB": "flow",
        "POSTGRES_USER": "fs_user",
        "POSTGRES_HOST": "pg",
        "POSTGRES_PORT": "15432",
    }

    assert build_postgres_dsn(env) == "dbname=flow user=fs_user password='' host=pg port=15432"
    assert resolve_langgraph_postgres_uri(env) == "postgresql://fs_user:@pg:15432/flow"


def test_langgraph_uri_is_derived_and_url_encoded() -> None:
    env = {
        "POSTGRES_DB": "flow steward",
        "POSTGRES_USER": "fs:user",
        "POSTGRES_PASSWORD": "secret/@value",
        "POSTGRES_HOST": "pg",
        "POSTGRES_PORT": "5432",
    }

    assert resolve_langgraph_postgres_uri(env) == (
        "postgresql://fs%3Auser:secret%2F%40value@pg:5432/flow%20steward"
    )


def test_rabbitmq_url_uses_canonical_pass_component() -> None:
    env = {
        "RABBITMQ_USER": "fs:user",
        "RABBITMQ_PASS": "rabbit/@value",
        "RABBITMQ_HOST": "rabbitmq",
        "RABBITMQ_PORT": "5679",
    }

    assert resolve_rabbitmq_url(env) == "amqps://fs%3Auser:rabbit%2F%40value@rabbitmq:5679/"


def test_rabbitmq_url_is_empty_without_components_or_override() -> None:
    assert resolve_rabbitmq_url({}) == ""


def test_rabbitmq_builder_does_not_invent_default_password() -> None:
    env = {
        "RABBITMQ_USER": "fs_user",
        "RABBITMQ_HOST": "rabbitmq",
        "RABBITMQ_PORT": "5679",
    }

    assert resolve_rabbitmq_url(env) == "amqps://fs_user:@rabbitmq:5679/"
