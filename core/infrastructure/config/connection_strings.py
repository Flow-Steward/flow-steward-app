from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from core.infrastructure.config.rabbitmq_transport import rabbitmq_transport_mode
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

EnvMapping = Mapping[str, str]

DEFAULT_POSTGRES_DB = "flow_steward"
DEFAULT_POSTGRES_USER = "flow_steward"
DEFAULT_POSTGRES_PASSWORD = ""
DEFAULT_POSTGRES_HOST = "pg-fs"
DEFAULT_POSTGRES_PORT = "5432"
DEFAULT_RABBITMQ_USER = "flow_steward"
DEFAULT_RABBITMQ_PASS = ""
DEFAULT_RABBITMQ_HOST = "rabbitmq-fs"
DEFAULT_RABBITMQ_PORT = "5672"
POSTGRES_COMPONENT_KEYS = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
)
RABBITMQ_COMPONENT_KEYS = (
    "RABBITMQ_USER",
    "RABBITMQ_PASS",
    "RABBITMQ_HOST",
    "RABBITMQ_PORT",
)


def resolve_postgres_dsn(env: EnvMapping | None = None) -> str:
    source = _env(env)
    explicit = _first(source, "FS_DB_DSN", "DATABASE_URL", "POSTGRES_DSN")
    if explicit:
        return explicit
    if not _has_any(source, POSTGRES_COMPONENT_KEYS):
        return ""
    return build_postgres_dsn(source)


def resolve_langgraph_postgres_uri(env: EnvMapping | None = None) -> str:
    source = _env(env)
    explicit = _first(source, "LANGGRAPH_POSTGRES_URI")
    if explicit:
        return explicit
    if not _has_any(source, POSTGRES_COMPONENT_KEYS):
        return ""
    return build_postgres_uri(source)


def resolve_rabbitmq_url(env: EnvMapping | None = None) -> str:
    source = _env(env)
    explicit = _first(source, "RABBITMQ_URL")
    if explicit:
        return explicit
    if not _has_any(source, RABBITMQ_COMPONENT_KEYS):
        return ""
    return build_rabbitmq_url(source)


def build_postgres_dsn(env: EnvMapping | None = None) -> str:
    parts = postgres_parts(env)
    return (
        f"dbname={_libpq_value(parts['db'])} "
        f"user={_libpq_value(parts['user'])} "
        f"password={_libpq_value(parts['password'])} "
        f"host={_libpq_value(parts['host'])} "
        f"port={_libpq_value(parts['port'])}"
    )


def build_postgres_uri(env: EnvMapping | None = None) -> str:
    parts = postgres_parts(env)
    user = quote(parts["user"], safe="")
    password = quote(parts["password"], safe="")
    host = parts["host"]
    port = parts["port"]
    db = quote(parts["db"], safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def build_rabbitmq_url(env: EnvMapping | None = None) -> str:
    parts = rabbitmq_parts(env)
    scheme = "amqps" if rabbitmq_transport_mode(env) == "tls" else "amqp"
    user = quote(parts["user"], safe="")
    password = quote(parts["password"], safe="")
    host = parts["host"]
    port = parts["port"]
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{user}:{password}@{host}:{port}/"


def postgres_parts(env: EnvMapping | None = None) -> dict[str, str]:
    source = _env(env)
    return {
        "db": _get(source, "POSTGRES_DB", DEFAULT_POSTGRES_DB),
        "user": _get(source, "POSTGRES_USER", DEFAULT_POSTGRES_USER),
        "password": _get(source, "POSTGRES_PASSWORD", DEFAULT_POSTGRES_PASSWORD),
        "host": _get(source, "POSTGRES_HOST", DEFAULT_POSTGRES_HOST),
        "port": _get(source, "POSTGRES_PORT", DEFAULT_POSTGRES_PORT),
    }


def rabbitmq_parts(env: EnvMapping | None = None) -> dict[str, str]:
    source = _env(env)
    return {
        "user": _get(source, "RABBITMQ_USER", DEFAULT_RABBITMQ_USER),
        "password": _get(source, "RABBITMQ_PASS", DEFAULT_RABBITMQ_PASS),
        "host": _get(source, "RABBITMQ_HOST", DEFAULT_RABBITMQ_HOST),
        "port": _get(
            source,
            "RABBITMQ_PORT",
            "5671" if rabbitmq_transport_mode(source) == "tls" else DEFAULT_RABBITMQ_PORT,
        ),
    }


def _env(env: EnvMapping | None) -> EnvMapping:
    return env if env is not None else _runtime_env


def _first(env: EnvMapping, *names: str) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _get(env: EnvMapping, name: str, default: str) -> str:
    return str(env.get(name) or default).strip()


def _has_any(env: EnvMapping, names: tuple[str, ...]) -> bool:
    return any(str(env.get(name) or "").strip() for name in names)


def _libpq_value(value: str) -> str:
    text = str(value)
    if not text or any(char.isspace() or char in {"'", "\\"} for char in text):
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return text
