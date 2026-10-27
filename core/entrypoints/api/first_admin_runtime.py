"""API composition for first-administrator creation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.first_admin import FirstAdminService
from core.application.auth.first_admin_repository import PostgresFirstAdminRepository
from core.infrastructure.runtime.entrypoint_db_adapters import database_transaction


def build_first_admin_service(
    *, logger: Any, seed_default_roles: Callable[[Any], None], password_hash: Callable[[str], str]
) -> FirstAdminService:
    repository = PostgresFirstAdminRepository(
        logger=logger,
        seed_default_roles=seed_default_roles,
        transaction_connection_factory=database_transaction,
    )
    return FirstAdminService(repository=repository, password_hash=password_hash)
