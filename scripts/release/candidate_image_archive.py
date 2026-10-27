#!/usr/bin/env python3
"""Create and verify the durable candidate image-archive identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEMA_VERSION = "flow-steward-candidate-image-archive-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_images(images: dict[str, str]) -> dict[str, str]:
    normalized = {str(name).strip(): str(image_id).strip() for name, image_id in images.items()}
    if not normalized or any(not name for name in normalized):
        raise ValueError("candidate image identities are required")
    invalid = [name for name, image_id in normalized.items() if not _IMAGE_ID.fullmatch(image_id)]
    if invalid:
        raise ValueError(f"invalid candidate image identities: {', '.join(sorted(invalid))}")
    return dict(sorted(normalized.items()))


def write_manifest(
    *,
    archive_path: Path,
    output_path: Path,
    candidate_commit: str,
    images: dict[str, str],
) -> None:
    """Write an atomic manifest bound to exact archive bytes and image IDs."""
    if not _COMMIT.fullmatch(candidate_commit):
        raise ValueError("candidate commit must be a full lowercase Git commit")
    if not archive_path.is_file():
        raise ValueError(f"candidate image archive is missing: {archive_path}")
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "candidate_commit": candidate_commit,
        "archive_sha256": _sha256(archive_path),
        "archive_size": archive_path.stat().st_size,
        "images": _validated_images(images),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_manifest(
    *,
    archive_path: Path,
    manifest_path: Path,
    candidate_commit: str,
    expected_images: dict[str, str],
) -> dict[str, str]:
    """Verify archive bytes, candidate identity, and every expected image ID."""
    if not archive_path.is_file() or not manifest_path.is_file():
        raise ValueError("candidate image archive and manifest are required")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("candidate image archive schema is unsupported")
    if payload.get("candidate_commit") != candidate_commit:
        raise ValueError("candidate image archive commit does not match")
    images = _validated_images(dict(payload.get("images") or {}))
    if images != _validated_images(expected_images):
        raise ValueError("candidate image archive image identities do not match")
    if payload.get("archive_size") != archive_path.stat().st_size:
        raise ValueError("candidate image archive size does not match")
    if payload.get("archive_sha256") != _sha256(archive_path):
        raise ValueError("candidate image archive sha256 does not match")
    return images


def _image_pairs(values: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        name, separator, image_id = value.partition("=")
        if not separator or name in pairs:
            raise ValueError(f"invalid or duplicate --image value: {value}")
        pairs[name] = image_id
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()
    try:
        images = _image_pairs(args.image)
        if args.mode == "create":
            write_manifest(
                archive_path=args.archive,
                output_path=args.manifest,
                candidate_commit=args.candidate_commit,
                images=images,
            )
        else:
            verified = verify_manifest(
                archive_path=args.archive,
                manifest_path=args.manifest,
                candidate_commit=args.candidate_commit,
                expected_images=images,
            )
            for name, image_id in verified.items():
                print(f"{name}={image_id}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"candidate image archive verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
