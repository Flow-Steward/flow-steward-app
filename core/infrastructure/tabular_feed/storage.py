"""Storage wiring for first-party tabular feed execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import create_storage_adapter
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.psycopg_runtime.extensions import TRANSACTION_STATUS_INERROR


def build_tabular_artifact_storage(
    *, environ: dict[str, str] | None = None
) -> ArtifactDatasetPlane:
    env = dict(environ or _runtime_env)
    from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

    return build_artifact_storage(
        environ=env,
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )


def plane_from_state_or_env(state: dict[str, Any]) -> ArtifactDatasetPlane:
    plane = state.get("_artifact_plane") or state.get("artifact_plane")
    if isinstance(plane, ArtifactDatasetPlane):
        return plane
    if plane is not None and hasattr(plane, "get_artifact") and hasattr(plane, "get_dataset"):
        return plane
    plane = build_tabular_artifact_storage(environ=dict(_runtime_env))
    state["_artifact_plane"] = plane
    return plane


@contextmanager
def snapshot_repository_from_state_or_env(
    state: dict[str, Any], *, operation: str, snapshot_mode: str = "compare"
) -> Iterator[SnapshotRepository | None]:
    """Compose a short transaction only for operations requiring DB authority.

    A repository object can be supplied by an internal host/test composition.
    Serialized state, user configuration, and dictionaries cannot supply one.
    Production connections remain local to this context and never enter state.
    """
    if operation not in {"checksum", "commit_snapshot"} and not (
        operation == "diff" and snapshot_mode == "publish"
    ):
        yield None
        return
    injected = state.get("_snapshot_repository")
    if isinstance(injected, SnapshotRepository):
        yield injected
        return
    dsn = str(CONFIG.get("fs_db_dsn") or "").strip()
    if not dsn:
        yield None
        return
    try:
        conn = psycopg_runtime.connect(dsn)
    except psycopg.Error:
        yield None
        return
    try:
        with conn:
            yield SnapshotRepository(conn)
            # The service translates DB failures into an envelope. Only this
            # composition owns the connection whose failed transaction it clears.
            if conn.get_transaction_status() == TRANSACTION_STATUS_INERROR:
                conn.rollback()
    finally:
        conn.close()
