"""Prepare provider-linked services before importing the Compact runtime."""

from __future__ import annotations

import sys

from core.infrastructure.config.connection_strings import (
    build_postgres_uri,
    build_rabbitmq_url,
)
from core.infrastructure.runtime_env import runtime_env


def _value(name: str) -> str:
    return str(runtime_env.get(name) or "").strip()


def _require(*names: str) -> None:
    for name in names:
        if not _value(name):
            raise ValueError(f"Compact provider deployment requires {name}")


def prepare_provider_environment() -> None:
    """Publish canonical URLs from generic linked-service environment values."""
    if not _value("FS_DB_DSN"):
        database_url = _value("DATABASE_URL")
        if database_url:
            runtime_env["FS_DB_DSN"] = database_url
        else:
            _require("POSTGRES_HOST", "POSTGRES_PASSWORD")
            runtime_env["FS_DB_DSN"] = build_postgres_uri(runtime_env)

    if not _value("LANGGRAPH_POSTGRES_URI"):
        if _value("POSTGRES_HOST"):
            runtime_env["LANGGRAPH_POSTGRES_URI"] = build_postgres_uri(runtime_env)
        else:
            runtime_env["LANGGRAPH_POSTGRES_URI"] = runtime_env["FS_DB_DSN"]

    if not _value("RABBITMQ_URL"):
        _require("RABBITMQ_HOST", "RABBITMQ_PASS")
        runtime_env["RABBITMQ_URL"] = build_rabbitmq_url(runtime_env)


def main() -> int:
    try:
        prepare_provider_environment()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 78

    from core.entrypoints.cli.compact_runtime import main as compact_main

    return compact_main()


if __name__ == "__main__":
    raise SystemExit(main())
