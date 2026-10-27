#!/usr/bin/env python3
"""Fail closed when a candidate's recorded release input graph has drifted."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

DIGEST = re.compile(r"(?:.+@)?sha256:[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _architecture() -> str:
    machine = platform.machine().lower()
    aliases = {"aarch64": "arm64", "x86_64": "amd64"}
    return f"linux/{aliases.get(machine, machine)}"


def verify(
    *,
    root: Path,
    provenance_path: Path,
    source_manifest_path: Path,
    commit: str,
    verify_images: bool,
) -> None:
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    manifest_bytes = source_manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if provenance.get("schema_version") != "flow-steward-packaged-provenance-v2":
        raise SystemExit("candidate provenance schema is incomplete")
    if provenance.get("candidate_commit") != commit or manifest.get("candidate_commit") != commit:
        raise SystemExit("candidate provenance commit mismatch")
    if provenance.get("source_sha256") != manifest.get("source_sha256"):
        raise SystemExit("candidate source identity mismatch")
    if provenance.get("source_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise SystemExit("candidate source manifest digest mismatch")

    expected_lfs = [
        {"path": item["path"], "oid": item["lfs_oid"], "size": item["lfs_size"]}
        for item in manifest.get("entries") or []
        if "lfs_oid" in item
    ]
    if provenance.get("git_lfs_objects") != expected_lfs:
        raise SystemExit("candidate Git LFS identity mismatch")

    release_inputs = provenance.get("release_inputs")
    if not isinstance(release_inputs, dict) or not release_inputs:
        raise SystemExit("candidate release input graph is missing")
    for relative, record in release_inputs.items():
        path = root / relative
        if (
            not path.is_file()
            or not isinstance(record, dict)
            or record.get("sha256") != _sha256(path)
            or record.get("size") != path.stat().st_size
        ):
            raise SystemExit(f"candidate release input differs: {relative}")

    if provenance.get("architecture") != _architecture():
        raise SystemExit("candidate architecture mismatch")
    for family in ("base_images", "infrastructure_images", "image_digests"):
        images = provenance.get(family)
        if not isinstance(images, dict) or not images:
            raise SystemExit(f"candidate {family} are missing")
        for name, image in images.items():
            if not isinstance(image, str) or not DIGEST.fullmatch(image):
                raise SystemExit(f"candidate image is not digest pinned: {family}.{name}")
            if verify_images:
                command = ["docker", "image", "inspect", image]
                if family == "image_digests":
                    command.extend(
                        [
                            "--format",
                            '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                        ]
                    )
                result = subprocess.run(
                    command,
                    check=False,
                    text=family == "image_digests",
                    stdout=subprocess.PIPE if family == "image_digests" else subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise SystemExit(f"candidate image is unavailable: {family}.{name}")
                if family == "image_digests" and result.stdout.strip() != commit:
                    raise SystemExit(f"candidate image revision mismatch: {family}.{name}")
    if not isinstance(provenance.get("build_arguments"), dict) or not provenance["build_arguments"]:
        raise SystemExit("candidate build arguments are missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--verify-images", action="store_true")
    args = parser.parse_args()
    verify(
        root=args.root.resolve(),
        provenance_path=args.provenance.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        commit=args.candidate_commit,
        verify_images=args.verify_images,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
