from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.extensions.artifact_storage import create_storage_adapter
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.artifacts import (
    create_storage_adapter as create_public_storage_adapter,
)
from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage


@pytest.fixture(
    params=[create_storage_adapter, create_public_storage_adapter],
    ids=["application", "public-sdk"],
)
def adapter_factory(request: pytest.FixtureRequest) -> Callable[..., Any]:
    return request.param


@pytest.mark.parametrize("backend", ["disk_typo", "local-fs", "s33"])
def test_unknown_direct_backend_is_rejected(
    backend: str, adapter_factory: Callable[..., Any]
) -> None:
    with pytest.raises(ValueError, match="backend"):
        adapter_factory({"backend": backend})


@pytest.mark.parametrize("backend", ["disk_typo", "local-fs", "s33"])
def test_unknown_runtime_backend_is_rejected_before_construction(backend: str) -> None:
    def unexpected_adapter(_config: dict[str, Any]) -> Any:
        pytest.fail("Invalid backend reached adapter construction")

    def unexpected_plane(*, storage: Any) -> Any:
        pytest.fail("Invalid backend reached artifact plane construction")

    with pytest.raises(ValueError, match="ARTIFACT_STORAGE_BACKEND"):
        build_artifact_storage(
            create_storage_adapter=unexpected_adapter,
            artifact_plane_factory=unexpected_plane,
            environ={"ARTIFACT_STORAGE_BACKEND": backend},
        )


@pytest.mark.parametrize("backend", ["memory", " MEMORY "])
def test_explicit_memory_stores_and_reads_bytes(
    backend: str, adapter_factory: Callable[..., Any]
) -> None:
    storage = adapter_factory({"backend": backend})
    storage.put("synthetic.json", b'{"count":2}', content_type="application/json")

    assert storage.get("synthetic.json") == b'{"count":2}'


def test_existing_no_config_factory_api_retains_memory_behavior(
    adapter_factory: Callable[..., Any],
) -> None:
    storage = adapter_factory()
    storage.put("synthetic.txt", b"direct factory", content_type="text/plain")

    assert storage.get("synthetic.txt") == b"direct factory"


@pytest.mark.parametrize("backend", ["local_fs", " LOCAL_FS "])
def test_local_fs_bytes_survive_adapter_recreation(
    tmp_path: Path, backend: str, adapter_factory: Callable[..., Any]
) -> None:
    config = {"backend": backend, "root_dir": str(tmp_path), "key_prefix": "synthetic"}
    writer = adapter_factory(config)
    writer.put("data.json", b'{"value":false}', content_type="application/json")

    reader = adapter_factory(config)

    assert reader.get("data.json") == b'{"value":false}'


def test_explicit_runtime_memory_retains_storage_behavior() -> None:
    storage = build_artifact_storage(
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=lambda *, storage: storage,
        environ={"ARTIFACT_STORAGE_BACKEND": "memory"},
    )
    storage.put("synthetic.txt", b"intentional memory", content_type="text/plain")

    assert storage.get("synthetic.txt") == b"intentional memory"


def test_s3_factory_reads_remote_bytes(
    monkeypatch: pytest.MonkeyPatch, adapter_factory: Callable[..., Any]
) -> None:
    import boto3

    client = MagicMock()
    client.get_object.return_value = {"Body": BytesIO(b"persisted synthetic remote bytes")}
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    storage = adapter_factory(
        {"backend": "s3", "bucket": "synthetic", "access_key": "", "secret_key": ""}
    )

    assert storage.get("synthetic.txt") == b"persisted synthetic remote bytes"
