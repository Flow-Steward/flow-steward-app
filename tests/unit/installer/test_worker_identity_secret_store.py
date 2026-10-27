from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.installer.worker_identity_secret_store import (
    FileWorkerIdentitySecretStore,
)


def test_file_worker_identity_store_persists_owner_only_token(tmp_path: Path) -> None:
    path = tmp_path / "state" / "worker-token"
    store = FileWorkerIdentitySecretStore(path)

    assert store.read() == ""
    store.replace("first-token")
    store.replace("replacement-token")

    assert store.read() == "replacement-token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.glob(".worker-token.*")) == []


def test_file_worker_identity_store_rejects_empty_token(tmp_path: Path) -> None:
    store = FileWorkerIdentitySecretStore(tmp_path / "worker-token")

    with pytest.raises(ValueError, match="required"):
        store.replace("  ")
