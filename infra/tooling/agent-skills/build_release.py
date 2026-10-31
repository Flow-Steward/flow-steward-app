#!/usr/bin/env python3
"""Build the release archives of the Operator and Developer agent skill packages.

The archives are built from one committed Core revision (`--ref`, default
`HEAD`), never from the working tree, so each one maps to exactly one Core
commit. Before packing, the tool renders every generated package file from its
canonical source at that revision and refuses to build when a committed file
differs (run `sync_packages.py`, commit, build again).

For every package it writes `<package>-<version>.zip` and `.tar.gz` with the
plugin root at the archive root, plus `gemini-extension.json`. It also writes
`flow-steward-agent-skills-<core version>.zip`, a local marketplace folder that
holds both packages, `SHA256SUMS` and `release-manifest.json`, which records the
Core repository, commit, tag and version. Output goes to
`dist/agent-skills/<commit>/` (ignored by Git) unless `--output` says otherwise.
Building twice from the same commit gives byte-identical files.

Nothing is uploaded: the owner attaches the files to the GitHub Release of the
Core tag, as `docs/agent-skills/publishing/README.md` describes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = Path("dist/agent-skills")
CORE_REPOSITORY = "Flow-Steward/flow-steward-app"
MANIFEST_SCHEMA = "flow-steward-agent-skills-release-v1"
MARKETPLACE_FOLDER = "flow-steward-agent-skills"
GEMINI_MANIFEST = "gemini-extension.json"
_ZIP_EPOCH_FLOOR = 315532800  # 1980-01-01, the earliest time a zip entry can carry


def _sync_module() -> ModuleType:
    path = Path(__file__).with_name("sync_packages.py")
    spec = importlib.util.spec_from_file_location("sync_packages", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


SYNC = _sync_module()


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def core_revision(repo_root: Path, ref: str, tag: str | None) -> dict[str, Any]:
    """Commit, tag, version and commit time of the Core revision the archives come from."""
    commit = _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if tag is None:
        tags = _git(repo_root, "tag", "--points-at", commit, "--list", "v*").split()
        tag = tags[0] if len(tags) == 1 else None
    elif _git(repo_root, "rev-parse", "--verify", f"{tag}^{{commit}}") != commit:
        raise RuntimeError(f"tag {tag} does not point at {commit}")
    return {
        "repository": CORE_REPOSITORY,
        "commit": commit,
        "tag": tag,
        "version": _git(repo_root, "show", f"{commit}:VERSION"),
        "commit_time": int(_git(repo_root, "show", "-s", "--format=%ct", commit)),
    }


def export_tree(repo_root: Path, commit: str, destination: Path) -> None:
    """The committed files the packages are rendered and packed from."""
    sources = json.loads(_git(repo_root, "show", f"{commit}:{SYNC.SOURCES_FILE.as_posix()}"))
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "archive",
            "--format=tar",
            commit,
            SYNC.SKILLS_DIR.as_posix(),
            sources["icon_source"],
        ],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def package_files(tree_root: Path, package: str, sources: dict[str, Any]) -> dict[str, bytes]:
    files = _files_under(tree_root / SYNC.SKILLS_DIR / package)
    files[GEMINI_MANIFEST] = SYNC._json_bytes(SYNC.gemini_manifest(sources, package))
    return dict(sorted(files.items()))


def marketplace_files(tree_root: Path) -> dict[str, bytes]:
    skills = tree_root / SYNC.SKILLS_DIR
    files = {
        f"{MARKETPLACE_FOLDER}/.claude-plugin/marketplace.json": (
            tree_root / SYNC.MARKETPLACE_FILE
        ).read_bytes()
    }
    for package in SYNC.PACKAGES:
        for relative, content in _files_under(skills / package).items():
            files[f"{MARKETPLACE_FOLDER}/{package}/{relative}"] = content
    return dict(sorted(files.items()))


def write_zip(path: Path, files: dict[str, bytes], epoch: int) -> None:
    stamp = time.gmtime(max(epoch, _ZIP_EPOCH_FLOOR))[:6]
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, content, compresslevel=9)


def write_tar_gz(path: Path, files: dict[str, bytes], epoch: int) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = epoch
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(content))
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(buffer.getvalue())


def _describe(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {"file": path.name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def build(tree_root: Path, core: dict[str, Any], output: Path) -> dict[str, Any]:
    """Write every release file for `core` into the empty `output`; return the manifest."""
    problems = SYNC.drift(tree_root)
    if problems:
        raise RuntimeError(
            "committed package files differ from their canonical sources; run "
            "infra/tooling/agent-skills/sync_packages.py and commit:\n" + "\n".join(problems)
        )
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"{output} is not empty")
    output.mkdir(parents=True, exist_ok=True)
    sources = SYNC.load_sources(tree_root)
    epoch = int(core["commit_time"])
    packages = []
    for package in SYNC.PACKAGES:
        files = package_files(tree_root, package, sources)
        stem = f"{package}-{sources['packages'][package]['version']}"
        write_zip(output / f"{stem}.zip", files, epoch)
        write_tar_gz(output / f"{stem}.tar.gz", files, epoch)
        packages.append(
            {
                "name": package,
                "version": sources["packages"][package]["version"],
                "files": sorted(files),
                "archives": [
                    _describe(output / f"{stem}.zip"),
                    _describe(output / f"{stem}.tar.gz"),
                ],
            }
        )
    marketplace = output / f"{MARKETPLACE_FOLDER}-{core['version']}.zip"
    write_zip(marketplace, marketplace_files(tree_root), epoch)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "core": {key: core[key] for key in ("repository", "commit", "tag", "version")},
        "packages": packages,
        "marketplace": {
            "name": sources["marketplace"]["name"],
            "folder": MARKETPLACE_FOLDER,
            **_describe(marketplace),
        },
    }
    archives = sorted(
        [entry for package in packages for entry in package["archives"]]
        + [manifest["marketplace"]],
        key=lambda entry: entry["file"],
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{entry['sha256']}  {entry['file']}\n" for entry in archives), encoding="utf-8"
    )
    (output / "release-manifest.json").write_bytes(SYNC._json_bytes(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ref", default="HEAD", help="Core commit, branch or tag to pack")
    parser.add_argument("--tag", help="Core release tag; must point at --ref")
    parser.add_argument("--output", type=Path, help="empty output folder")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        core = core_revision(args.repo_root, args.ref, args.tag)
        output = args.output or args.repo_root / OUTPUT_ROOT / core["commit"][:12]
        with tempfile.TemporaryDirectory(prefix="flow-steward-agent-skills-") as workdir:
            export_tree(args.repo_root, core["commit"], Path(workdir))
            build(Path(workdir), core, output)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Core {core['commit']} (tag {core['tag'] or 'none'}, version {core['version']})")
    for path in sorted(output.iterdir()):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
