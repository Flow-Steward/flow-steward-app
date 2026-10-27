#!/usr/bin/env python3
"""Build, mirror, index, and verify official Flow Steward release images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
if str(_DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEFAULT_ROOT))

from core.application.release.images import all_product_roles  # noqa: E402

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORMS = ("linux/amd64", "linux/arm64")
_TARGETS = {
    "compact-runtime": ("infra/docker/Dockerfile", "compact-runtime", "BUILD_SHA"),
    "fs-app": ("infra/docker/Dockerfile", "app-runtime", "BUILD_SHA"),
    "fs-worker": ("infra/docker/Dockerfile", "worker-runtime", "BUILD_SHA"),
    "control-plane": ("infra/docker/Dockerfile", "control-runtime", "BUILD_SHA"),
    "fs-cli": ("infra/docker/Dockerfile", "cli", "FS_BUILD_REVISION"),
    "fs-mcp": ("infra/mcp/Dockerfile", "runtime", "FS_BUILD_REVISION"),
}


def _run(command: Sequence[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout


def validate_release(root: Path, version: str, tag: str, revision: str) -> None:
    if _VERSION.fullmatch(version) is None or tag != f"v{version}":
        raise ValueError("release identity must be VERSION X.Y.Z and tag vX.Y.Z")
    if _REVISION.fullmatch(revision) is None:
        raise ValueError("release revision must be a full lowercase Git commit SHA")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("VERSION does not match the requested release")
    observed = _run(("git", "rev-parse", f"refs/tags/{tag}^{{commit}}"), cwd=root).strip()
    if observed != revision:
        raise ValueError("the immutable source tag does not resolve to the requested revision")
    head = _run(("git", "rev-parse", "HEAD"), cwd=root).strip()
    if head != revision:
        raise ValueError("checkout HEAD does not match the requested release revision")
    dirty = _run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
    ).strip()
    if dirty:
        raise ValueError("release working tree and Docker build context must be clean")


def native_platform() -> str:
    if platform.system().lower() != "linux":
        raise ValueError("official native images must be built on a Linux host")
    architecture = platform.machine().lower()
    try:
        return {
            "amd64": "linux/amd64",
            "x86_64": "linux/amd64",
            "arm64": "linux/arm64",
            "aarch64": "linux/arm64",
        }[architecture]
    except KeyError as exc:
        raise ValueError(f"unsupported native release architecture: {architecture}") from exc


def _verify_native_buildx_worker(root: Path, requested_platform: str) -> None:
    inspection = _run(("docker", "buildx", "inspect", "--bootstrap"), cwd=root)
    if re.search(r"(?m)^Driver:\s+docker-container\s*$", inspection) is None:
        raise ValueError("official native OCI export requires a selected docker-container builder")
    context = _run(("docker", "context", "show"), cwd=root).strip()
    endpoint = _run(
        (
            "docker",
            "context",
            "inspect",
            context,
            "--format",
            "{{.Endpoints.docker.Host}}",
        ),
        cwd=root,
    ).strip()
    builder_endpoints = set(re.findall(r"(?m)^Endpoint:\s+(\S+)\s*$", inspection))
    if (
        not context
        or not endpoint
        or not builder_endpoints
        or not builder_endpoints.issubset({context, endpoint})
    ):
        raise ValueError("official native build requires a builder on the active Docker endpoint")
    worker_platform = _run(
        ("docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"), cwd=root
    ).strip()
    normalized = {
        "linux/x86_64": "linux/amd64",
        "linux/aarch64": "linux/arm64",
    }.get(worker_platform, worker_platform)
    if normalized != requested_platform:
        raise ValueError("selected Docker worker architecture is not the requested native platform")


def _artifact_identity(
    artifact: Path, *, platform_name: str, revision: str, role: str
) -> dict[str, str]:
    def blob(archive: tarfile.TarFile, digest: str) -> bytes:
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"artifact {artifact.name} contains an invalid digest")
        member = archive.extractfile(f"blobs/sha256/{digest.removeprefix('sha256:')}")
        if member is None:
            raise ValueError(f"artifact {artifact.name} lacks OCI blob {digest}")
        payload = member.read()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"artifact {artifact.name} contains a corrupt OCI blob")
        return payload

    with tarfile.open(artifact, mode="r") as archive:
        index_member = archive.extractfile("index.json")
        if index_member is None:
            raise ValueError(f"artifact {artifact.name} lacks index.json")
        index = json.loads(index_member.read())
        roots = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], dict):
            raise ValueError(f"artifact {artifact.name} must contain one OCI root")
        root_digest = str(roots[0].get("digest") or "")
        root_raw = blob(archive, root_digest)
        root = json.loads(root_raw)
        descriptors = root.get("manifests") if isinstance(root, dict) else None
        if isinstance(descriptors, list):
            os_name, architecture = platform_name.split("/", 1)
            candidates = [
                item
                for item in descriptors
                if isinstance(item, dict)
                and isinstance(item.get("platform"), dict)
                and item["platform"].get("os") == os_name
                and item["platform"].get("architecture") == architecture
                and not item["platform"].get("variant")
            ]
            if len(candidates) != 1:
                raise ValueError(f"artifact {artifact.name} lacks one exact native manifest")
            manifest_digest = str(candidates[0].get("digest") or "")
            manifest_raw = blob(archive, manifest_digest)
            manifest = json.loads(manifest_raw)
        else:
            manifest_digest = root_digest
            manifest = root
        if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
            raise ValueError(f"artifact {artifact.name} lacks an OCI config")
        config_digest = str(manifest["config"].get("digest") or "")
        config = json.loads(blob(archive, config_digest))
        requested_os, requested_arch = platform_name.split("/", 1)
        if (
            not isinstance(config, dict)
            or config.get("os") != requested_os
            or config.get("architecture") != requested_arch
        ):
            raise ValueError(f"artifact {artifact.name} has the wrong platform config")
        labels = config.get("config", {}).get("Labels", {}) if isinstance(config, dict) else {}
        if labels.get("org.opencontainers.image.revision") != revision:
            raise ValueError(f"artifact {artifact.name} has the wrong revision label")
        if labels.get("org.opencontainers.image.role") != role:
            raise ValueError(f"artifact {artifact.name} has the wrong role label")
        return {
            "manifest_digest": manifest_digest,
            "config_digest": config_digest,
        }


def build(root: Path, version: str, revision: str, requested_platform: str, output: Path) -> None:
    if requested_platform not in _PLATFORMS or requested_platform != native_platform():
        raise ValueError("official images must be built on their matching native Linux host")
    _verify_native_buildx_worker(root, requested_platform)
    output.mkdir(parents=True, exist_ok=True)
    architecture = requested_platform.split("/", 1)[1]
    records: list[dict[str, str]] = []
    for role in all_product_roles():
        dockerfile, target, build_arg = _TARGETS[role.role]
        artifact = output / f"{role.role}-{architecture}.oci.tar"
        build_arguments = ["--build-arg", f"{build_arg}={revision}"]
        if role.role == "compact-runtime":
            build_arguments.extend(("--build-arg", f"FS_IMAGE_PLATFORM={requested_platform}"))
        _run(
            (
                "docker",
                "buildx",
                "build",
                "--platform",
                requested_platform,
                "--provenance=false",
                "--target",
                target,
                *build_arguments,
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--label",
                f"org.opencontainers.image.role={role.role}",
                "--file",
                dockerfile,
                "--output",
                f"type=oci,dest={artifact}",
                ".",
            ),
            cwd=root,
        )
        records.append(
            {
                "role": role.role,
                "repository": role.repository,
                "platform": requested_platform,
                "artifact": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                **_artifact_identity(
                    artifact,
                    platform_name=requested_platform,
                    revision=revision,
                    role=role.role,
                ),
            }
        )
    (output / f"build-{architecture}.json").write_text(
        json.dumps(
            {
                "version": version,
                "revision": revision,
                "platform": requested_platform,
                "images": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _namespaces() -> tuple[str, ...]:
    values = tuple(
        value.strip().rstrip("/")
        for value in (
            os.environ.get("FS_RELEASE_GHCR_NAMESPACE", ""),
            os.environ.get("FS_RELEASE_DOCKERHUB_NAMESPACE", ""),
        )
        if value.strip()
    )
    if not values:
        raise ValueError("configure at least one official registry namespace in the environment")
    return values


def publish(root: Path, version: str, revision: str, artifact_dir: Path) -> None:
    manifests = [
        json.loads((artifact_dir / f"build-{arch}.json").read_text(encoding="utf-8"))
        for arch in ("amd64", "arm64")
    ]
    if any(
        item.get("version") != version or item.get("revision") != revision for item in manifests
    ):
        raise ValueError("native build metadata does not match the requested release")
    by_platform = {item["platform"]: item for item in manifests}
    if set(by_platform) != set(_PLATFORMS):
        raise ValueError("publication requires exactly the two supported native builds")
    expected_roles = {role.role: role.repository for role in all_product_roles()}
    for platform_name, manifest in by_platform.items():
        records = manifest.get("images")
        if not isinstance(records, list):
            raise ValueError(f"native build {platform_name} has no image records")
        roles = [str(record.get("role") or "") for record in records if isinstance(record, dict)]
        if len(roles) != len(set(roles)) or set(roles) != set(expected_roles):
            raise ValueError(f"native build {platform_name} has an invalid canonical role set")
        for record in records:
            role = str(record.get("role") or "")
            if (
                record.get("repository") != expected_roles[role]
                or record.get("platform") != platform_name
                or _DIGEST.fullmatch(str(record.get("manifest_digest") or "")) is None
                or _DIGEST.fullmatch(str(record.get("config_digest") or "")) is None
            ):
                raise ValueError(f"native build {platform_name} has invalid {role} identity")
    for namespace in _namespaces():
        for role in all_product_roles():
            children: list[str] = []
            for platform_name in _PLATFORMS:
                architecture = platform_name.split("/", 1)[1]
                record = next(
                    item
                    for item in by_platform[platform_name]["images"]
                    if item["role"] == role.role
                )
                artifact = artifact_dir / record["artifact"]
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != record["sha256"]:
                    raise ValueError(f"artifact checksum mismatch: {artifact.name}")
                child = f"{namespace}/{role.repository}:{version}-{architecture}"
                _run(
                    (
                        "skopeo",
                        "copy",
                        "--preserve-digests",
                        "--override-os",
                        "linux",
                        "--override-arch",
                        architecture,
                        f"oci-archive:{artifact}",
                        f"docker://{child}",
                    ),
                    cwd=root,
                )
                expected_digest = str(record["manifest_digest"])
                copied_raw = _run(("skopeo", "inspect", "--raw", f"docker://{child}"), cwd=root)
                copied_digest = "sha256:" + hashlib.sha256(copied_raw.encode("utf-8")).hexdigest()
                if copied_digest != expected_digest:
                    raise ValueError(f"published child does not match artifact: {child}")
                children.append(f"{namespace}/{role.repository}@{expected_digest}")
            _run(
                (
                    "docker",
                    "buildx",
                    "imagetools",
                    "create",
                    "--tag",
                    f"{namespace}/{role.repository}:{version}",
                    *children,
                ),
                cwd=root,
            )


def _runtime_manifest_digests(document: object, reference: str) -> dict[str, str]:
    if not isinstance(document, dict) or not isinstance(document.get("manifests"), list):
        raise ValueError(f"{reference} is not an OCI image index")
    selected: dict[str, str] = {}
    for descriptor in document["manifests"]:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("platform"), dict):
            raise ValueError(f"{reference} contains a manifest without a platform")
        platform_record = descriptor["platform"]
        platform_name = f"{platform_record.get('os', '')}/{platform_record.get('architecture', '')}"
        if platform_name == "unknown/unknown":
            annotations = descriptor.get("annotations")
            reference_type = (
                str(annotations.get("vnd.docker.reference.type") or "")
                if isinstance(annotations, dict)
                else ""
            )
            if reference_type != "attestation-manifest":
                raise ValueError(f"{reference} contains an unrecognized unknown/unknown manifest")
            continue
        if platform_name not in _PLATFORMS:
            raise ValueError(f"{reference} contains unsupported runtime platform {platform_name}")
        if platform_name in selected:
            raise ValueError(f"{reference} contains duplicate runtime platform {platform_name}")
        digest = str(descriptor.get("digest") or "").lower()
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"{reference} has an invalid {platform_name} manifest digest")
        selected[platform_name] = digest
    if set(selected) != set(_PLATFORMS):
        raise ValueError(f"{reference} does not expose exactly {_PLATFORMS}")
    return selected


def _verified_child_identity(
    *,
    root: Path,
    reference: str,
    platform_name: str,
    manifest_digest: str,
    revision: str,
    role: str,
) -> dict[str, str]:
    repository = reference.rsplit(":", 1)[0]
    child_reference = f"{repository}@{manifest_digest}"
    manifest_raw = _run(
        ("skopeo", "inspect", "--raw", f"docker://{child_reference}"),
        cwd=root,
    )
    observed_manifest_digest = "sha256:" + hashlib.sha256(manifest_raw.encode("utf-8")).hexdigest()
    if observed_manifest_digest != manifest_digest:
        raise ValueError(f"{child_reference} content does not match its descriptor digest")
    manifest = json.loads(manifest_raw)
    config_digest = (
        str(manifest.get("config", {}).get("digest") or "").lower()
        if isinstance(manifest, dict) and isinstance(manifest.get("config"), dict)
        else ""
    )
    if _DIGEST.fullmatch(config_digest) is None:
        raise ValueError(f"{reference} has an invalid {platform_name} config digest")
    config = json.loads(
        _run(
            ("skopeo", "inspect", "--config", f"docker://{child_reference}"),
            cwd=root,
        )
    )
    expected_os, expected_architecture = platform_name.split("/", 1)
    if config.get("os") != expected_os or config.get("architecture") != expected_architecture:
        raise ValueError(f"{reference} has the wrong {platform_name} child config platform")
    labels = config.get("config", {}).get("Labels", {})
    if labels.get("org.opencontainers.image.revision") != revision:
        raise ValueError(f"{reference} has the wrong revision label")
    if labels.get("org.opencontainers.image.role") != role:
        raise ValueError(f"{reference} has the wrong role label")
    return {
        "manifest_digest": manifest_digest,
        "config_digest": config_digest,
    }


def verify(root: Path, version: str, revision: str, evidence: Path) -> None:
    registries: list[dict[str, object]] = []
    failures: list[str] = []
    verified_children: dict[str, dict[str, dict[str, str]]] | None = None
    for namespace in _namespaces():
        registry_record: dict[str, object] = {"namespace": namespace, "images": []}
        try:
            registry_children: dict[str, dict[str, dict[str, str]]] = {}
            for role in all_product_roles():
                reference = f"{namespace}/{role.repository}:{version}"
                raw = _run(("skopeo", "inspect", "--raw", f"docker://{reference}"), cwd=root)
                document = json.loads(raw)
                manifest_digests = _runtime_manifest_digests(document, reference)
                child_identities: dict[str, dict[str, str]] = {}
                for platform_name in _PLATFORMS:
                    child_identities[platform_name] = _verified_child_identity(
                        root=root,
                        reference=reference,
                        platform_name=platform_name,
                        manifest_digest=manifest_digests[platform_name],
                        revision=revision,
                        role=role.role,
                    )
                index_digest = (
                    _run(
                        (
                            "skopeo",
                            "inspect",
                            "--format",
                            "{{.Digest}}",
                            f"docker://{reference}",
                        ),
                        cwd=root,
                    )
                    .strip()
                    .lower()
                )
                if _DIGEST.fullmatch(index_digest) is None:
                    raise ValueError(f"{reference} has an invalid index digest")
                registry_children[role.role] = child_identities
                registry_record["images"].append(
                    {
                        "role": role.role,
                        "reference": reference,
                        "index_digest": index_digest,
                        "platforms": child_identities,
                    }
                )
            if verified_children is not None and registry_children != verified_children:
                raise ValueError(
                    f"{namespace} does not match verified child digests from the primary registry"
                )
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{namespace}: {exc}")
            registry_record["status"] = "incomplete"
        else:
            registry_record["status"] = "verified"
            if verified_children is None:
                verified_children = registry_children
        registries.append(registry_record)
    if not any(item.get("status") == "verified" for item in registries):
        raise ValueError("no official registry contains a complete verified release set")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "version": version,
                "revision": revision,
                "registries": registries,
                "failures": failures,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "build", "publish", "verify"))
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--platform", choices=_PLATFORMS)
    parser.add_argument("--artifact-dir", type=Path, default=Path("release-artifacts"))
    parser.add_argument("--evidence", type=Path, default=Path("release-verification.json"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    validate_release(root, args.version, args.tag or f"v{args.version}", args.revision)
    if args.action == "build":
        build(
            root, args.version, args.revision, args.platform or native_platform(), args.artifact_dir
        )
    elif args.action == "publish":
        publish(root, args.version, args.revision, args.artifact_dir)
    elif args.action == "verify":
        verify(root, args.version, args.revision, args.evidence)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"official release failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
