"""Resolve a digest-pinned OCI reference into verified local Docker identity."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.application.release.catalog import PublishedRelease, ReleaseCoordinates, ReleaseVersion
from core.application.release.contracts import (
    ReleaseIdentity,
    parse_digest_pinned_reference,
    release_identity_from_resolution,
)
from core.application.release.images import required_product_roles
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_OCI_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_OCI_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_MAX_OCI_METADATA_BYTES = 16 * 1024 * 1024
_LINUX_PLATFORM_PREFIX = "linux/"


@dataclass(frozen=True, slots=True)
class RegistryImageIdentity:
    registry_ref: str
    index_digest: str
    manifest_digest: str
    config_digest: str
    revision: str
    role: str
    platform: str
    local_image_id: str


@dataclass(frozen=True, slots=True)
class LocalImageContentIdentity:
    index_digest: str
    manifest_digest: str
    config_digest: str


@dataclass(frozen=True, slots=True)
class OfficialRegistry:
    """One ordered official registry namespace."""

    name: str
    namespace: str

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        namespace = str(self.namespace or "").strip().rstrip("/")
        if not name or not namespace or "@" in namespace or "://" in namespace:
            raise ValueError("official registry requires a safe name and image namespace")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "namespace", namespace)


DEFAULT_OFFICIAL_REGISTRIES = (
    OfficialRegistry(name="ghcr", namespace="ghcr.io/flow-steward"),
    OfficialRegistry(name="docker_hub", namespace="docker.io/flowsteward"),
)


def configured_official_registries(value: str = "") -> tuple[OfficialRegistry, ...]:
    """Return an ordered whole-set registry policy from the optional runtime override."""
    configured = tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())
    if not configured:
        return DEFAULT_OFFICIAL_REGISTRIES
    if len(configured) != len(set(configured)):
        raise ValueError("official release registries must be unique and ordered")
    return tuple(
        OfficialRegistry(name=f"configured_{index}", namespace=namespace)
        for index, namespace in enumerate(configured, start=1)
    )


@dataclass(frozen=True, slots=True)
class ResolvedImageSet:
    """Complete immutable role set selected from exactly one registry."""

    registry: OfficialRegistry
    version: str
    revision: str
    platform: str
    identities: tuple[RegistryImageIdentity, ...]


class OfficialReleaseResolver:
    """Docker-backed adapter for canonical official release image selection."""

    def __init__(self) -> None:
        self._executor = SubprocessExecutor()

    def resolve(self, release: PublishedRelease, *, profile: str, platform: str) -> ReleaseIdentity:
        return self.resolve_coordinates(release.coordinates, profile=profile, platform=platform)

    def probe(self, release: PublishedRelease, *, profile: str, platform: str) -> ReleaseIdentity:
        return self.resolve(release, profile=profile, platform=platform)

    def resolve_coordinates(
        self, coordinates: ReleaseCoordinates, *, profile: str, platform: str
    ) -> ReleaseIdentity:
        return self._resolve(coordinates, profile=profile, platform=platform)

    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity:
        for expected in release.images:
            repository, _manifest_digest = parse_digest_pinned_reference(expected.registry_ref)
            actual = probe_digest_pinned_image(
                registry_ref=f"{repository}@{expected.index_digest}",
                platform=expected.platform,
                expected_revision=expected.revision,
                expected_role=expected.role,
                run=self._executor.run,
            )
            if (
                actual.registry_ref != expected.registry_ref
                or actual.index_digest != expected.index_digest
                or actual.manifest_digest != expected.manifest_digest
                or actual.config_digest != expected.config_digest
                or actual.revision != expected.revision
                or actual.role != expected.role
                or actual.platform != expected.platform
            ):
                raise ValueError(
                    f"Official image {expected.role} no longer matches its durable identity"
                )
        return release

    def verify_exact_identity(self, release: ReleaseIdentity) -> ReleaseIdentity:
        """Select one complete official mirror of the archived immutable image set."""
        expected_images = {image.role: image for image in release.images}
        roles = required_product_roles(release.profile)
        for role in roles:
            expected = expected_images[role.role]
            canonical_ref = (
                f"{release.registry.rstrip('/')}/{role.repository}@{expected.manifest_digest}"
            )
            if expected.registry_ref != canonical_ref:
                raise ValueError(
                    f"Archived official image {role.role} has a noncanonical repository"
                )

        failures: list[str] = []
        for registry in configured_official_registries(
            str(runtime_env.get("FS_RELEASE_REGISTRIES") or "")
        ):
            selected_images = []
            try:
                for role in roles:
                    expected = expected_images[role.role]
                    repository = f"{registry.namespace}/{role.repository}"
                    actual = probe_digest_pinned_image(
                        registry_ref=f"{repository}@{expected.index_digest}",
                        platform=expected.platform,
                        expected_revision=expected.revision,
                        expected_role=expected.role,
                        run=self._executor.run,
                    )
                    if (
                        actual.registry_ref != f"{repository}@{expected.manifest_digest}"
                        or actual.index_digest != expected.index_digest
                        or actual.manifest_digest != expected.manifest_digest
                        or actual.config_digest != expected.config_digest
                        or actual.revision != expected.revision
                        or actual.role != expected.role
                        or actual.platform != expected.platform
                    ):
                        raise ValueError(
                            f"Official image {role.role} differs from its archived immutable identity"
                        )
                    selected_images.append(
                        expected.model_copy(update={"registry_ref": actual.registry_ref})
                    )
            except ValueError as exc:
                failures.append(f"{registry.name}: {exc}")
                continue
            return ReleaseIdentity.model_validate(
                {**release.model_dump(), "registry": registry.namespace, "images": selected_images}
            )
        raise ValueError(
            "No complete archived exact image set is available: " + "; ".join(failures)
        )

    def _resolve(
        self, release: ReleaseCoordinates, *, profile: str, platform: str
    ) -> ReleaseIdentity:
        resolved = resolve_official_image_set(
            profile=profile,
            version=str(release.version),
            revision=release.commit_sha,
            platform=platform,
            run=self._executor.run,
            resolve_image=probe_digest_pinned_image,
            registries=configured_official_registries(
                str(runtime_env.get("FS_RELEASE_REGISTRIES") or "")
            ),
        )
        return release_identity_from_resolution(
            release,
            registry=resolved.registry.namespace,
            profile=cast(Any, profile),
            platform=resolved.platform,
            identities=resolved.identities,
        )


def _verified_archive_member(archive: tarfile.TarFile, name: str) -> bytes:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise ValueError(f"local image archive lacks {name}") from exc
    if not member.isfile() or member.size > _MAX_OCI_METADATA_BYTES:
        raise ValueError(f"local image archive contains invalid metadata member {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"local image archive cannot read {name}")
    with stream:
        return stream.read(_MAX_OCI_METADATA_BYTES + 1)


def _verified_blob(archive: tarfile.TarFile, digest: str) -> bytes:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("local image archive contains an invalid OCI digest")
    payload = _verified_archive_member(archive, f"blobs/sha256/{digest.removeprefix('sha256:')}")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("local image archive OCI blob digest does not match its content")
    return payload


def _json_object(payload: bytes, *, description: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"local image archive contains invalid {description} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"local image archive {description} is not an object")
    return value


def resolve_local_image_archive(
    *,
    archive_path: Path,
    descriptor_digest: str,
    platform: str,
) -> LocalImageContentIdentity:
    """Resolve distinct OCI digests from a Docker image archive without extracting it."""
    if _DIGEST.fullmatch(descriptor_digest) is None:
        raise ValueError("local Docker image descriptor digest is invalid")
    if not platform.startswith(_LINUX_PLATFORM_PREFIX) or platform.count("/") != 1:
        raise ValueError("local Docker image platform must be linux/<architecture>")
    requested_os, requested_arch = platform.split("/", 1)
    try:
        archive = tarfile.open(archive_path, mode="r")  # noqa: SIM115
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("local Docker image archive is unreadable") from exc
    with archive:
        archive_index = _json_object(
            _verified_archive_member(archive, "index.json"), description="OCI index"
        )
        descriptors = archive_index.get("manifests")
        if not isinstance(descriptors, list):
            raise ValueError("local image archive index has no manifests")
        roots = [
            item
            for item in descriptors
            if isinstance(item, dict) and item.get("digest") == descriptor_digest
        ]
        if not roots:
            # Only the containerd image store reports the root descriptor digest as the
            # image ID; a classic overlay2 store reports the config digest instead. The
            # archive is the same on both, so fall back to its sole root and let the
            # caller bind it to the inspected image.
            roots = [
                item
                for item in descriptors
                if isinstance(item, dict) and str(item.get("digest") or "").strip()
            ]
        if len(roots) != 1:
            raise ValueError("local image archive does not contain the inspected descriptor")
        root = roots[0]
        descriptor_digest = str(root.get("digest") or "")
        root_media_type = str(root.get("mediaType") or "")
        root_payload = _verified_blob(archive, descriptor_digest)
        root_document = _json_object(root_payload, description="OCI descriptor")

        if root_media_type in _OCI_MANIFEST_MEDIA_TYPES:
            index_digest = descriptor_digest
            manifest_digest = descriptor_digest
            manifest = root_document
        elif root_media_type in _OCI_INDEX_MEDIA_TYPES:
            index_digest = descriptor_digest
            manifests = root_document.get("manifests")
            if not isinstance(manifests, list):
                raise ValueError("local OCI image index has no manifests")
            candidates = [
                item
                for item in manifests
                if isinstance(item, dict)
                and isinstance(item.get("platform"), dict)
                and item["platform"].get("os") == requested_os
                and item["platform"].get("architecture") == requested_arch
                and not item["platform"].get("variant")
            ]
            if len(candidates) != 1:
                raise ValueError("local OCI image index does not contain one exact platform")
            if str(candidates[0].get("mediaType") or "") not in _OCI_MANIFEST_MEDIA_TYPES:
                raise ValueError("local OCI platform descriptor is not an image manifest")
            manifest_digest = str(candidates[0].get("digest") or "")
            manifest = _json_object(
                _verified_blob(archive, manifest_digest), description="OCI manifest"
            )
        else:
            raise ValueError("local Docker image descriptor has an unsupported media type")

        config_descriptor = manifest.get("config")
        if not isinstance(config_descriptor, dict):
            raise ValueError("local OCI manifest lacks a config digest")
        config_digest = str(config_descriptor.get("digest") or "")
        _verified_blob(archive, config_digest)
        docker_manifest = json.loads(_verified_archive_member(archive, "manifest.json"))
        if not isinstance(docker_manifest, list) or len(docker_manifest) != 1:
            raise ValueError("local Docker image archive must contain one image")
        docker_record = docker_manifest[0]
        if not isinstance(docker_record, dict):
            raise ValueError("local Docker image archive manifest is invalid")
        archived_config = str(docker_record.get("Config") or "")
        if archived_config != f"blobs/sha256/{config_digest.removeprefix('sha256:')}":
            raise ValueError("local Docker archive config does not match the OCI manifest")
        return LocalImageContentIdentity(
            index_digest=index_digest,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
        )


def _checked(run: Callable[[Sequence[str]], ProcessOutcome], command: list[str]) -> str:
    outcome = run(command)
    if outcome.returncode != 0:
        raise ValueError(f"OCI image command failed: {' '.join(command[:4])}")
    return outcome.stdout


def _digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.strip().encode("utf-8")).hexdigest()


def is_digest_pinned_reference(reference: str) -> bool:
    """Whether a reference names one immutable registry image: <repository>@sha256:<digest>.

    AC17-R139. The one definition, so the installer's preflight and the resolver that
    later pulls the image cannot disagree about what "pinned" means. A tag is not
    pinned: it can be moved after it is checked, which is exactly what provenance has
    to rule out.
    """
    try:
        parse_digest_pinned_reference(reference)
    except ValueError:
        return False
    return True


def resolve_official_image_set(
    *,
    profile: str,
    version: str,
    revision: str,
    platform: str,
    run: Callable[[Sequence[str]], ProcessOutcome],
    registries: Sequence[OfficialRegistry] = DEFAULT_OFFICIAL_REGISTRIES,
    resolve_image: Callable[..., RegistryImageIdentity] | None = None,
) -> ResolvedImageSet:
    """Resolve a complete profile role set from the first usable registry.

    A registry is selected only after every role has resolved and passed OCI
    revision, role, and platform verification. A partial primary never contributes
    identities to a fallback result.
    """

    canonical_version = str(ReleaseVersion.parse(version))
    if _REVISION.fullmatch(str(revision or "").strip().lower()) is None:
        raise ValueError("official image revision must be a full Git revision")
    if platform not in {f"{_LINUX_PLATFORM_PREFIX}amd64", f"{_LINUX_PLATFORM_PREFIX}arm64"}:
        raise ValueError("official images support linux/amd64 or linux/arm64")
    ordered_registries = tuple(registries)
    if not ordered_registries:
        raise ValueError("at least one official registry must be configured")
    resolver = resolve_image or resolve_digest_pinned_image
    failures: list[str] = []
    roles = required_product_roles(profile)

    for registry in ordered_registries:
        identities: list[RegistryImageIdentity] = []
        try:
            for role in roles:
                tagged_ref = f"{registry.namespace}/{role.repository}:{canonical_version}"
                index_raw = _checked(
                    run,
                    [
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        "--raw",
                        tagged_ref,
                    ],
                )
                pinned_ref = f"{registry.namespace}/{role.repository}@{_digest(index_raw)}"
                identities.append(
                    resolver(
                        registry_ref=pinned_ref,
                        platform=platform,
                        expected_revision=revision,
                        expected_role=role.role,
                        run=run,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"{registry.name}: {exc}")
            continue
        return ResolvedImageSet(
            registry=registry,
            version=canonical_version,
            revision=revision,
            platform=platform,
            identities=tuple(identities),
        )

    attempted = ", ".join(registry.name for registry in ordered_registries)
    detail = "; ".join(failures)
    raise ValueError(
        f"No configured registry ({attempted}) provides a complete official image set"
        + (f": {detail}" if detail else "")
    )


def resolve_digest_pinned_image(
    *,
    registry_ref: str,
    platform: str,
    expected_revision: str,
    expected_role: str,
    run: Callable[[Sequence[str]], ProcessOutcome],
) -> RegistryImageIdentity:
    """Pull and verify one platform image without weakening local-ID checks."""
    if not is_digest_pinned_reference(registry_ref):
        raise ValueError("registry image reference must be pinned by sha256 digest")
    repository, _, index_digest = registry_ref.strip().rpartition("@")
    if not platform.startswith(_LINUX_PLATFORM_PREFIX) or platform.count("/") != 1:
        raise ValueError("registry image platform must be linux/<architecture>")
    if _REVISION.fullmatch(expected_revision) is None:
        raise ValueError("expected image revision must be a full Git revision")

    index_raw = _checked(run, ["docker", "buildx", "imagetools", "inspect", "--raw", registry_ref])
    if _digest(index_raw) != index_digest:
        raise ValueError("registry index digest does not match returned OCI content")
    try:
        index = json.loads(index_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("registry returned invalid OCI index JSON") from exc

    requested_os, requested_arch = platform.split("/", 1)
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if isinstance(manifests, list):
        candidates = [
            item
            for item in manifests
            if isinstance(item, dict)
            and isinstance(item.get("platform"), dict)
            and item["platform"].get("os") == requested_os
            and item["platform"].get("architecture") == requested_arch
            and not item["platform"].get("variant")
        ]
        if len(candidates) != 1:
            raise ValueError("registry index does not contain one exact requested platform")
        manifest_digest = str(candidates[0].get("digest") or "")
        if _DIGEST.fullmatch(manifest_digest) is None:
            raise ValueError("registry platform manifest digest is invalid")
        manifest_ref = f"{repository}@{manifest_digest}"
        manifest_raw = _checked(
            run, ["docker", "buildx", "imagetools", "inspect", "--raw", manifest_ref]
        )
        if _digest(manifest_raw) != manifest_digest:
            raise ValueError("registry manifest digest does not match returned OCI content")
        manifest = json.loads(manifest_raw)
    else:
        manifest_digest = index_digest
        manifest_ref = registry_ref
        manifest = index

    try:
        config_digest = str(manifest["config"]["digest"])
    except (KeyError, TypeError) as exc:
        raise ValueError("registry manifest lacks a config digest") from exc
    if _DIGEST.fullmatch(config_digest) is None:
        raise ValueError("registry config digest is invalid")

    _checked(run, ["docker", "pull", "--platform", platform, manifest_ref])
    inspection_raw = _checked(run, ["docker", "image", "inspect", manifest_ref])
    try:
        details = json.loads(inspection_raw)[0]
        local_image_id = str(details["Id"])
        descriptor = details.get("Descriptor")
        actual_os = str(details.get("Os") or "linux")
        actual_arch = str(details["Architecture"])
        labels = details["Config"]["Labels"]
        actual_revision = str(labels["org.opencontainers.image.revision"])
        actual_role = str(labels["org.opencontainers.image.role"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("local Docker image metadata is incomplete") from exc
    if descriptor is None:
        if local_image_id != config_digest:
            raise ValueError("legacy local Docker image ID does not match OCI config digest")
    else:
        if not isinstance(descriptor, dict):
            raise ValueError("local Docker image descriptor is invalid")
        local_descriptor_digest = str(descriptor.get("digest") or "")
        local_descriptor_media_type = str(descriptor.get("mediaType") or "")
        if local_descriptor_digest != manifest_digest:
            raise ValueError("local Docker image descriptor does not match OCI manifest digest")
        if local_descriptor_media_type not in _OCI_MANIFEST_MEDIA_TYPES:
            raise ValueError("local Docker image descriptor is not an OCI image manifest")
        if local_image_id != local_descriptor_digest:
            raise ValueError("local Docker image ID does not match its descriptor digest")
    if (actual_os, actual_arch) != (requested_os, requested_arch):
        raise ValueError("local Docker image platform does not match requested platform")
    if actual_revision != expected_revision:
        raise ValueError("local Docker image revision does not match expected revision")
    if actual_role != expected_role:
        raise ValueError("local Docker image role does not match expected role")

    return RegistryImageIdentity(
        registry_ref=manifest_ref,
        index_digest=index_digest,
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        revision=actual_revision,
        role=actual_role,
        platform=platform,
        local_image_id=local_image_id,
    )


def _registry_metadata_object(value: object, *, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Registry {description} must be a JSON object")
    return value


def probe_digest_pinned_image(
    *,
    registry_ref: str,
    platform: str,
    expected_revision: str,
    expected_role: str,
    run: Callable[[Sequence[str]], ProcessOutcome],
) -> RegistryImageIdentity:
    """Verify registry metadata for release check without pulling into local Docker."""
    if not is_digest_pinned_reference(registry_ref):
        raise ValueError("registry image reference must be pinned by sha256 digest")
    if not platform.startswith(_LINUX_PLATFORM_PREFIX) or platform.count("/") != 1:
        raise ValueError("registry image platform must be linux/<architecture>")
    repository, _, index_digest = registry_ref.strip().rpartition("@")
    requested_os, requested_arch = platform.split("/", 1)
    index_raw = _checked(run, ["docker", "buildx", "imagetools", "inspect", "--raw", registry_ref])
    if _digest(index_raw) != index_digest:
        raise ValueError("registry index digest does not match returned OCI content")
    index = _registry_metadata_object(json.loads(index_raw), description="index")
    manifests = index.get("manifests")
    if isinstance(manifests, list):
        candidates = [
            item
            for item in manifests
            if isinstance(item, dict)
            and isinstance(item.get("platform"), dict)
            and item["platform"].get("os") == requested_os
            and item["platform"].get("architecture") == requested_arch
            and not item["platform"].get("variant")
        ]
        if len(candidates) != 1:
            raise ValueError("registry index does not contain one exact requested platform")
        manifest_digest = str(candidates[0].get("digest") or "")
        manifest_ref = f"{repository}@{manifest_digest}"
        manifest_raw = _checked(
            run, ["docker", "buildx", "imagetools", "inspect", "--raw", manifest_ref]
        )
        if _digest(manifest_raw) != manifest_digest:
            raise ValueError("registry manifest digest does not match returned OCI content")
        manifest = _registry_metadata_object(json.loads(manifest_raw), description="manifest")
    else:
        manifest_digest = index_digest
        manifest_ref = registry_ref
        manifest = index
    manifest_config = _registry_metadata_object(
        manifest.get("config"), description="manifest config"
    )
    config_digest = str(manifest_config.get("digest") or "")
    if _DIGEST.fullmatch(config_digest) is None:
        raise ValueError("registry manifest lacks a valid config digest")
    image_raw = _checked(
        run,
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{json .Image}}",
            manifest_ref,
        ],
    )
    image = _registry_metadata_object(json.loads(image_raw), description="image config")
    observed_os = image.get("os") or image.get("Os")
    observed_architecture = image.get("architecture") or image.get("Architecture")
    if observed_os != requested_os or observed_architecture != requested_arch:
        raise ValueError("registry image config does not match requested platform")
    config = _registry_metadata_object(
        image.get("config") or image.get("Config") or {}, description="image config fields"
    )
    labels = _registry_metadata_object(
        config.get("Labels") or config.get("labels") or {}, description="image labels"
    )
    if labels.get("org.opencontainers.image.revision") != expected_revision:
        raise ValueError("registry image revision does not match expected revision")
    if labels.get("org.opencontainers.image.role") != expected_role:
        raise ValueError("registry image role does not match expected role")
    return RegistryImageIdentity(
        registry_ref=manifest_ref,
        index_digest=index_digest,
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        revision=expected_revision,
        role=expected_role,
        platform=platform,
        local_image_id=config_digest,
    )
