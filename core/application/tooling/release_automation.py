"""Fail-closed contracts for automated dual-registry release publication."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from core.application.release.images import all_release_roles

_TAG = re.compile(r"v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))")
_SHA = re.compile(r"[a-f0-9]{40}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_PLATFORMS = ("linux/amd64", "linux/arm64")


def stable_version(tag: str) -> str:
    """Extract a stable semantic version without accepting shell/ref syntax."""
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError("release tag must be stable vX.Y.Z without leading zeroes")
    return match.group(1)


def release_identity(root: Path, tag: str) -> tuple[str, str]:
    """Require a clean tagged checkout contained in public main history."""
    version = stable_version(tag)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    revision = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    if _SHA.fullmatch(revision) is None or git("rev-parse", "HEAD") != revision:
        raise ValueError("release checkout differs from tagged revision")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("release tag differs from VERSION")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release checkout must be clean")
    try:
        git("merge-base", "--is-ancestor", revision, "refs/remotes/origin/main")
    except subprocess.CalledProcessError as exc:
        raise ValueError("release revision is outside public main history") from exc
    return version, revision


def registry_namespaces(ghcr: str, dockerhub: str) -> tuple[str, str]:
    """Require explicitly configured, distinct registry namespace owners."""
    for value, registry in ((ghcr, "ghcr.io"), (dockerhub, "docker.io")):
        if re.fullmatch(re.escape(registry) + r"/[a-z0-9][a-z0-9_-]*", value) is None:
            raise ValueError(f"configure a lowercase {registry}/owner release namespace")
    return ghcr, dockerhub


def require_complete_release(
    path: Path, *, version: str, revision: str, namespaces: tuple[str, str]
) -> None:
    """Reject partial mirrors even when the underlying manual verifier succeeds."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != version or document.get("revision") != revision:
        raise ValueError("verification evidence has the wrong release identity")
    registries = document.get("registries", [])
    if document.get("failures") or len(registries) != 2:
        raise ValueError("both registries must verify without failures")
    if {item.get("namespace") for item in registries} != set(namespaces):
        raise ValueError("verification evidence has the wrong registry set")
    roles = {role.role: role.repository for role in all_release_roles()}
    children = []
    for registry in registries:
        images = registry.get("images", [])
        if registry.get("status") != "verified" or len(images) != len(roles):
            raise ValueError("registry does not contain the complete verified role set")
        if {image.get("role") for image in images} != set(roles):
            raise ValueError("registry contains missing, duplicate or unexpected roles")
        identities = {}
        for image in images:
            role = image["role"]
            reference = f"{registry['namespace']}/{roles[role]}:{version}"
            if image.get("reference") != reference or not _DIGEST.fullmatch(
                str(image.get("index_digest", ""))
            ):
                raise ValueError("registry reference or index digest is invalid")
            platforms = image.get("platforms", {})
            if set(platforms) != set(_PLATFORMS):
                raise ValueError("registry lacks the exact native platform set")
            for identity in platforms.values():
                if set(identity) != {"manifest_digest", "config_digest"} or any(
                    _DIGEST.fullmatch(str(digest)) is None for digest in identity.values()
                ):
                    raise ValueError("registry child identity is invalid")
            identities[role] = platforms
        children.append(identities)
    if children[0] != children[1]:
        raise ValueError("registry child digests differ")


def _artifact_children(directory: Path, version: str, revision: str) -> dict[str, dict[str, str]]:
    expected: dict[str, dict[str, str]] = {}
    roles = {role.role: role.repository for role in all_release_roles()}
    for platform in _PLATFORMS:
        architecture = platform.split("/")[1]
        record = json.loads((directory / f"build-{architecture}.json").read_text(encoding="utf-8"))
        if (record.get("version"), record.get("revision"), record.get("platform")) != (
            version,
            revision,
            platform,
        ):
            raise ValueError("artifact metadata has the wrong release identity")
        images = record.get("images", [])
        if len(images) != len(roles) or {image.get("role") for image in images} != set(roles):
            raise ValueError("artifact metadata has the wrong role set")
        for image in images:
            role = image["role"]
            name = f"{role}-{architecture}.oci.tar"
            if image.get("artifact") != name or image.get("repository") != roles[role]:
                raise ValueError("artifact path or role repository differs")
            if image.get("platform") != platform or not _DIGEST.fullmatch(
                str(image.get("manifest_digest", ""))
            ):
                raise ValueError("artifact platform or digest is invalid")
            path = directory / name
            if path.is_symlink():
                raise ValueError("artifact may not be a symlink")
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksum != image.get("sha256"):
                raise ValueError(f"artifact checksum mismatch: {name}")
            expected.setdefault(roles[role], {})[platform] = image["manifest_digest"]
    return expected


def inspect_reference(reference: str) -> str | None:
    """Return missing only for registry not-found; auth/network failures abort."""
    try:
        return subprocess.check_output(
            ["skopeo", "inspect", "--raw", f"docker://{reference}"],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        if re.search(
            r"manifest unknown|name unknown|manifest_unknown|name_unknown", exc.stderr.lower()
        ):
            return None
        raise ValueError(f"cannot inspect release reference {reference}") from exc


def preflight_registry(
    directory: Path,
    version: str,
    revision: str,
    namespaces: tuple[str, str],
    *,
    inspect: Callable[[str], str | None] = inspect_reference,
) -> None:
    """Validate all archives and forbid replacing existing immutable versions."""
    expected = _artifact_children(directory, version, revision)
    for namespace in namespaces:
        for repository, platforms in expected.items():
            reference = f"{namespace}/{repository}:{version}"
            for platform, digest in platforms.items():
                raw = inspect(f"{reference}-{platform.split('/')[1]}")
                if (
                    raw is not None
                    and "sha256:" + hashlib.sha256(raw.encode()).hexdigest() != digest
                ):
                    raise ValueError(f"immutable release child differs: {reference}")
            raw = inspect(reference)
            if raw is None:
                continue
            descriptors = json.loads(raw).get("manifests", [])
            observed = {
                f"{item.get('platform', {}).get('os')}/{item.get('platform', {}).get('architecture')}": item.get(
                    "digest"
                )
                for item in descriptors
            }
            if len(descriptors) != 2 or observed != platforms:
                raise ValueError(f"immutable release index differs: {reference}")
