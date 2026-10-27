#!/usr/bin/env python3
"""Materialize and attest an exact Git commit as a Docker build context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

_LFS_POINTER = re.compile(
    rb"version https://git-lfs\.github\.com/spec/v1\n"
    rb"oid sha256:([0-9a-f]{64})\n"
    rb"size ([0-9]+)\n?\Z"
)


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip()
        raise SystemExit(reason or f"git {' '.join(args)} failed")
    return result.stdout


def _safe_extract(archive: bytes, output: Path) -> None:
    import io

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit("candidate source archive contains an unsafe path")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise SystemExit(
                    f"candidate source archive contains unsupported entry: {member.name}"
                )
        source.extractall(output, filter="data")


def _tree_entries(root: Path, commit: str) -> list[tuple[str, str, str]]:
    raw = _git(root, "ls-tree", "-rz", "--full-tree", commit)
    entries: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        if kind != "blob":
            raise SystemExit(
                f"candidate source contains unsupported Git object {kind}: "
                f"{path_bytes.decode(errors='replace')}"
            )
        entries.append((path_bytes.decode(), mode, object_id))
    return entries


def _materialize_lfs(root: Path, path: Path, pointer: bytes) -> tuple[str, int]:
    match = _LFS_POINTER.fullmatch(pointer)
    if match is None:
        raise SystemExit(f"Git LFS pointer is invalid: {path.as_posix()}")
    expected_digest = match.group(1).decode()
    expected_size = int(match.group(2))
    current = path.read_bytes()
    if current != pointer:
        if len(current) != expected_size or hashlib.sha256(current).hexdigest() != expected_digest:
            raise SystemExit(f"Git LFS object failed digest verification: {path.as_posix()}")
        return expected_digest, expected_size
    if (
        shutil.which("git-lfs") is None
        and subprocess.run(
            ["git", "lfs", "version"], cwd=root, capture_output=True, check=False
        ).returncode
        != 0
    ):
        raise SystemExit("Git LFS content is required but git-lfs is unavailable")
    content = _git(root, "lfs", "smudge", path.as_posix(), input_bytes=pointer)
    if content == pointer:
        raise SystemExit(f"Git LFS object was not materialized: {path.as_posix()}")
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_digest:
        raise SystemExit(f"Git LFS object failed digest verification: {path.as_posix()}")
    path.write_bytes(content)
    return expected_digest, expected_size


def _git_blob_id(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def _lfs_paths(root: Path, paths: list[str]) -> set[str]:
    result = subprocess.run(
        ["git", "check-attr", "--stdin", "-z", "filter"],
        cwd=root,
        input=("\0".join(paths) + "\0").encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.decode(errors="replace").strip() or "git check-attr failed")
    fields = result.stdout.split(b"\0")
    return {
        fields[index].decode()
        for index in range(0, len(fields) - 2, 3)
        if fields[index + 1] == b"filter" and fields[index + 2] == b"lfs"
    }


def prepare_source(root: Path, commit: str, output: Path, manifest: Path) -> dict[str, object]:
    root = root.resolve()
    commit = _git(root, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("candidate source output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    _safe_extract(_git(root, "archive", "--format=tar", commit), output)
    object_format = _git(root, "rev-parse", "--show-object-format").decode().strip()
    if object_format not in {"sha1", "sha256"}:
        raise SystemExit(f"unsupported Git object format: {object_format}")

    tree_entries = _tree_entries(root, commit)
    lfs_paths = _lfs_paths(root, [relative for relative, _, _ in tree_entries])
    records: list[dict[str, object]] = []
    expected_paths: set[str] = set()
    for relative, mode, object_id in tree_entries:
        expected_paths.add(relative)
        path = output / relative
        if mode == "120000":
            if not path.is_symlink():
                raise SystemExit(f"candidate source symlink is not materialized: {relative}")
            data = os.readlink(path).encode()
            committed_data = data
            lfs = None
        else:
            if not path.is_file() or path.is_symlink():
                raise SystemExit(f"candidate source file is not materialized: {relative}")
            data = path.read_bytes()
            if relative in lfs_paths:
                committed_data = _git(root, "cat-file", "blob", object_id)
                lfs = _materialize_lfs(root, path, committed_data)
                data = path.read_bytes()
            else:
                committed_data = data
                lfs = None
        if _git_blob_id(committed_data, object_format) != object_id:
            raise SystemExit(f"candidate source differs from recorded commit: {relative}")
        record: dict[str, object] = {
            "path": relative,
            "mode": mode,
            "git_object": object_id,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        if lfs is not None:
            record["lfs_oid"] = f"sha256:{lfs[0]}"
            record["lfs_size"] = lfs[1]
        records.append(record)

    actual_paths = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    extras = sorted(actual_paths - expected_paths)
    missing = sorted(expected_paths - actual_paths)
    if extras or missing:
        raise SystemExit(
            "candidate source path set differs from recorded commit: "
            f"extra={extras!r} missing={missing!r}"
        )

    identity_payload = {"candidate_commit": commit, "entries": records}
    source_digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "schema_version": "flow-steward-candidate-source-v1",
        **identity_payload,
        "source_sha256": source_digest,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    payload = prepare_source(args.root, args.commit, args.output, args.manifest)
    print(payload["source_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
