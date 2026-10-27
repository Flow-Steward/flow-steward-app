from __future__ import annotations

from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass
from core.infrastructure.clone import compact_volume_publication
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata


def test_mixed_compact_publication_preserves_volume_siblings_and_target_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    staged = source / "state"
    staged.mkdir(parents=True)
    (staged / "product.json").write_text("synthetic-new")
    (staged / "product.json").chmod(0o640)
    target = tmp_path / "target"
    shell = target / "state/.flow-steward"
    shell.mkdir(parents=True)
    (shell / "old-product").write_text("obsolete")
    (shell / "stack.toml").write_text("target-retained")
    (shell / "clone").mkdir()
    (shell / "clone/operation").write_text("target-operation")
    (target / "state/bootstrap.json").write_text("target-bootstrap")
    (target / "extensions").mkdir()
    (target / "extensions/host-marker").write_text("host-tree")
    entries = inventory_volume_metadata(
        staged, archive_root="state", data_class=CloneDataClass.DURABLE_STATE
    )
    monkeypatch.setattr(compact_volume_publication, "_TARGET", target)

    compact_volume_publication.publish(
        source, entries, plan_digest="a" * 64, durable_state_only=True
    )

    assert (shell / "product.json").read_text() == "synthetic-new"
    assert (shell / "product.json").stat().st_mode & 0o777 == 0o640
    assert not (shell / "old-product").exists()
    assert (shell / "stack.toml").read_text() == "target-retained"
    assert (shell / "clone/operation").read_text() == "target-operation"
    assert (target / "state/bootstrap.json").read_text() == "target-bootstrap"
    assert (target / "extensions/host-marker").read_text() == "host-tree"
    assert compact_volume_publication.publication_matches(source, entries, durable_state_only=True)
    (shell / "product.json").write_text("tampered")
    assert not compact_volume_publication.publication_matches(
        source, entries, durable_state_only=True
    )


def test_mixed_compact_publication_rejects_nonstate_metadata_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    staged = source / "state"
    staged.mkdir(parents=True)
    (staged / "product.json").write_text("synthetic-new")
    extensions = source / "extensions"
    extensions.mkdir()
    target = tmp_path / "target"
    shell = target / "state/.flow-steward"
    shell.mkdir(parents=True)
    (shell / "product.json").write_text("target-original")
    entries = (
        *inventory_volume_metadata(
            staged, archive_root="state", data_class=CloneDataClass.DURABLE_STATE
        ),
        *inventory_volume_metadata(
            extensions, archive_root="extensions", data_class=CloneDataClass.EXTENSIONS
        ),
    )
    monkeypatch.setattr(compact_volume_publication, "_TARGET", target)

    with pytest.raises(ValueError, match="state-only"):
        compact_volume_publication.publish(
            source, entries, plan_digest="a" * 64, durable_state_only=True
        )
    assert (shell / "product.json").read_text() == "target-original"
