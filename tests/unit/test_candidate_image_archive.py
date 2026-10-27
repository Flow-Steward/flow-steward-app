from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release.candidate_image_archive import verify_manifest, write_manifest


def test_candidate_image_archive_manifest_is_content_and_identity_bound(tmp_path: Path) -> None:
    archive = tmp_path / "candidate-images.tar"
    archive.write_bytes(b"content-addressed-image-archive")
    manifest = tmp_path / "candidate-images.json"
    commit = "a" * 40
    images = {
        "fs-app": "sha256:" + "1" * 64,
        "fs-cli": "sha256:" + "2" * 64,
        "fs-mcp": "sha256:" + "3" * 64,
        "postgres": "sha256:" + "4" * 64,
    }

    write_manifest(
        archive_path=archive,
        output_path=manifest,
        candidate_commit=commit,
        images=images,
    )

    assert (
        verify_manifest(
            archive_path=archive,
            manifest_path=manifest,
            candidate_commit=commit,
            expected_images=images,
        )
        == images
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "flow-steward-candidate-image-archive-v1"
    assert payload["archive_size"] == archive.stat().st_size


def test_candidate_image_archive_rejects_tampering_and_identity_drift(tmp_path: Path) -> None:
    archive = tmp_path / "candidate-images.tar"
    archive.write_bytes(b"original")
    manifest = tmp_path / "candidate-images.json"
    commit = "b" * 40
    images = {"fs-app": "sha256:" + "1" * 64}
    write_manifest(
        archive_path=archive,
        output_path=manifest,
        candidate_commit=commit,
        images=images,
    )

    archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="archive sha256"):
        verify_manifest(
            archive_path=archive,
            manifest_path=manifest,
            candidate_commit=commit,
            expected_images=images,
        )

    archive.write_bytes(b"original")
    with pytest.raises(ValueError, match="image identities"):
        verify_manifest(
            archive_path=archive,
            manifest_path=manifest,
            candidate_commit=commit,
            expected_images={"fs-app": "sha256:" + "9" * 64},
        )
