"""Pure storage-capacity checks for protected release upgrades."""

from __future__ import annotations

import math
from dataclasses import dataclass

GIB = 1024**3
MIN_POSTGRES_FREE_BYTES = 10 * GIB
MIN_IMAGE_PREP_FREE_BYTES = 10 * GIB
MIN_BACKUP_FREE_BYTES = 2 * GIB
REINDEX_MULTIPLIER = 2.5


class StoragePreflightError(RuntimeError):
    """Release storage is insufficient or cannot be inspected safely."""


@dataclass(frozen=True)
class StoragePreflightObservation:
    """Closed capacity evidence collected by the release infrastructure adapter."""

    host_free_bytes: int
    image_prepare_estimate_bytes: int
    backup_free_bytes: int
    backup_required_bytes: int
    backup_shares_host_filesystem: bool
    postgres_free_bytes: int
    largest_hnsw_bytes: int
    docker_storage_readable: bool

    def assert_sufficient(self) -> dict[str, int | bool]:
        return assert_storage_preflight(
            host_free_bytes=self.host_free_bytes,
            image_prepare_estimate_bytes=self.image_prepare_estimate_bytes,
            backup_free_bytes=self.backup_free_bytes,
            backup_required_bytes=self.backup_required_bytes,
            backup_shares_host_filesystem=self.backup_shares_host_filesystem,
            postgres_free_bytes=self.postgres_free_bytes,
            largest_hnsw_bytes=self.largest_hnsw_bytes,
            docker_storage_readable=self.docker_storage_readable,
        )


def required_postgres_free_bytes(*, largest_hnsw_bytes: int) -> int:
    return max(
        MIN_POSTGRES_FREE_BYTES,
        math.ceil(max(0, largest_hnsw_bytes) * REINDEX_MULTIPLIER),
    )


def assert_storage_preflight(
    *,
    host_free_bytes: int,
    image_prepare_estimate_bytes: int,
    backup_free_bytes: int,
    backup_required_bytes: int,
    backup_shares_host_filesystem: bool,
    postgres_free_bytes: int,
    largest_hnsw_bytes: int,
    docker_storage_readable: bool,
) -> dict[str, int | bool]:
    postgres_required = required_postgres_free_bytes(largest_hnsw_bytes=largest_hnsw_bytes)
    image_required = max(MIN_IMAGE_PREP_FREE_BYTES, image_prepare_estimate_bytes)
    backup_required = max(MIN_BACKUP_FREE_BYTES, backup_required_bytes)
    # pg-fs is a local Docker service, so Docker Desktop's virtual filesystem
    # and the host ultimately consume the same physical disk even when the
    # container reports a much larger virtual capacity. Reserve reindex
    # headroom on the host as well and use the limiting free-space value.
    host_required = image_required + postgres_required + 2 * GIB
    if backup_shares_host_filesystem:
        host_required += backup_required
    effective_postgres_free = min(
        postgres_free_bytes,
        max(
            0,
            host_free_bytes
            - image_required
            - (backup_required if backup_shares_host_filesystem else 0),
        ),
    )

    failures: list[str] = []
    if not docker_storage_readable:
        failures.append("Docker storage is not readable")
    if host_free_bytes < host_required:
        failures.append(f"host free bytes {host_free_bytes} < required {host_required}")
    if not backup_shares_host_filesystem and backup_free_bytes < backup_required:
        failures.append(f"backup free bytes {backup_free_bytes} < required {backup_required}")
    if effective_postgres_free < postgres_required:
        failures.append(
            f"effective PostgreSQL free bytes {effective_postgres_free} < required "
            f"{postgres_required}"
        )
    if failures:
        raise StoragePreflightError("Release storage preflight failed: " + "; ".join(failures))
    return {
        "docker_storage_readable": docker_storage_readable,
        "host_free_bytes": host_free_bytes,
        "host_required_bytes": host_required,
        "image_prepare_required_bytes": image_required,
        "backup_free_bytes": backup_free_bytes,
        "backup_required_bytes": backup_required,
        "backup_shares_host_filesystem": backup_shares_host_filesystem,
        "postgres_free_bytes": postgres_free_bytes,
        "effective_postgres_free_bytes": effective_postgres_free,
        "postgres_required_bytes": postgres_required,
        "largest_hnsw_bytes": largest_hnsw_bytes,
    }
