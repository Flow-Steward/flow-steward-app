from __future__ import annotations

from typing import Any, cast

import pytest

from core.application.release.preflight import (
    GIB,
    StoragePreflightError,
    StoragePreflightObservation,
    assert_storage_preflight,
    required_postgres_free_bytes,
)


def test_required_postgres_free_bytes_reserves_reindex_headroom() -> None:
    assert required_postgres_free_bytes(largest_hnsw_bytes=4 * GIB) == 10 * GIB
    assert required_postgres_free_bytes(largest_hnsw_bytes=8 * GIB) == 20 * GIB


def test_storage_preflight_combines_image_and_backup_on_shared_filesystem() -> None:
    result = assert_storage_preflight(
        host_free_bytes=40 * GIB,
        image_prepare_estimate_bytes=8 * GIB,
        backup_free_bytes=24 * GIB,
        backup_required_bytes=8 * GIB,
        backup_shares_host_filesystem=True,
        postgres_free_bytes=20 * GIB,
        largest_hnsw_bytes=4 * GIB,
        docker_storage_readable=True,
    )

    assert result["host_required_bytes"] == 30 * GIB
    assert result["postgres_required_bytes"] == 10 * GIB


def test_typed_storage_observation_uses_the_same_capacity_policy() -> None:
    result = StoragePreflightObservation(
        host_free_bytes=40 * GIB,
        image_prepare_estimate_bytes=8 * GIB,
        backup_free_bytes=24 * GIB,
        backup_required_bytes=8 * GIB,
        backup_shares_host_filesystem=True,
        postgres_free_bytes=20 * GIB,
        largest_hnsw_bytes=4 * GIB,
        docker_storage_readable=True,
    ).assert_sufficient()

    assert result["host_required_bytes"] == 30 * GIB


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"docker_storage_readable": False}, "Docker storage is not readable"),
        ({"host_free_bytes": 1}, "host free bytes"),
        (
            {
                "backup_shares_host_filesystem": False,
                "backup_free_bytes": 1,
            },
            "backup free bytes",
        ),
        ({"postgres_free_bytes": 1}, "PostgreSQL free bytes"),
    ],
)
def test_storage_preflight_fails_closed(overrides: dict, message: str) -> None:
    values: dict[str, int | bool] = {
        "host_free_bytes": 40 * GIB,
        "image_prepare_estimate_bytes": 10 * GIB,
        "backup_free_bytes": 40 * GIB,
        "backup_required_bytes": 4 * GIB,
        "backup_shares_host_filesystem": True,
        "postgres_free_bytes": 40 * GIB,
        "largest_hnsw_bytes": 4 * GIB,
        "docker_storage_readable": True,
    }
    values.update(overrides)

    _raises_input_80_1 = cast(Any, values)
    with pytest.raises(StoragePreflightError, match=message):
        assert_storage_preflight(**_raises_input_80_1)
