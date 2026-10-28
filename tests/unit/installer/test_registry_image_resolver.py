from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import all_release_roles, required_product_roles
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import (
    OfficialRegistry,
    OfficialReleaseResolver,
    RegistryImageIdentity,
    configured_official_registries,
    probe_digest_pinned_image,
    resolve_digest_pinned_image,
    resolve_local_image_archive,
    resolve_official_image_set,
)


def test_official_registry_override_preserves_order_without_mixing() -> None:
    configured = configured_official_registries(
        "registry.internal.example/flow, mirror.example/flow"
    )

    assert tuple(item.namespace for item in configured) == (
        "registry.internal.example/flow",
        "mirror.example/flow",
    )
    with pytest.raises(ValueError, match="unique"):
        configured_official_registries("registry.example/flow,registry.example/flow")


def _sha(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def test_official_release_verification_rejects_changed_index_provenance(monkeypatch) -> None:
    revision = "a" * 40
    images = tuple(
        ReleaseImageIdentity(
            role=role.role,
            registry_ref=f"registry.example/flow/{role.repository}@sha256:{index + 1:064x}",
            index_digest=f"sha256:{index + 101:064x}",
            manifest_digest=f"sha256:{index + 1:064x}",
            config_digest=f"sha256:{index + 201:064x}",
            revision=revision,
            platform="linux/amd64",
        )
        for index, role in enumerate(required_product_roles("compact"))
    )
    release = ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=revision,
        registry="registry.example/flow",
        profile="compact",
        platform="linux/amd64",
        images=images,
    )

    def probe(**kwargs) -> RegistryImageIdentity:
        expected = next(image for image in images if image.role == kwargs["expected_role"])
        requested_digest = str(kwargs["registry_ref"]).rpartition("@")[2]
        observed_index = (
            "sha256:" + "f" * 64
            if requested_digest == expected.index_digest
            else expected.index_digest
        )
        return RegistryImageIdentity(
            registry_ref=expected.registry_ref,
            index_digest=observed_index,
            manifest_digest=expected.manifest_digest,
            config_digest=expected.config_digest,
            revision=expected.revision,
            role=expected.role,
            platform=expected.platform,
            local_image_id=expected.config_digest,
        )

    monkeypatch.setattr(
        "core.infrastructure.installer.registry_image_resolver.probe_digest_pinned_image",
        probe,
    )
    resolver = OfficialReleaseResolver()

    with pytest.raises(ValueError, match="durable identity"):
        resolver.verify(release)


def _fixture(*, role: str = "compact-runtime", architecture: str = "amd64"):
    revision = "a" * 40
    config_digest = "sha256:" + "c" * 64
    manifest_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config_digest},
            "layers": [],
        },
        separators=(",", ":"),
    )
    manifest_digest = _sha(manifest_raw)
    index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": manifest_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        },
        separators=(",", ":"),
    )
    index_digest = _sha(index_raw)
    registry_ref = f"registry.example/flow-steward@{index_digest}"
    manifest_ref = f"registry.example/flow-steward@{manifest_digest}"
    inspection = json.dumps(
        [
            {
                "Id": manifest_digest,
                "Descriptor": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                },
                "Os": "linux",
                "Architecture": architecture,
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": revision,
                        "org.opencontainers.image.role": role,
                    }
                },
            }
        ]
    )
    outputs = {
        ("docker", "buildx", "imagetools", "inspect", "--raw", registry_ref): index_raw,
        ("docker", "buildx", "imagetools", "inspect", "--raw", manifest_ref): manifest_raw,
        ("docker", "pull", "--platform", "linux/amd64", manifest_ref): "pulled",
        ("docker", "image", "inspect", manifest_ref): inspection,
    }
    return revision, registry_ref, manifest_digest, config_digest, outputs


def test_registry_resolver_retains_index_manifest_config_and_local_id() -> None:
    revision, registry_ref, manifest_digest, config_digest, outputs = _fixture()
    calls: list[tuple[str, ...]] = []

    def run(command: Sequence[str]) -> ProcessOutcome:
        key = tuple(command)
        calls.append(key)
        return ProcessOutcome(command=key, returncode=0, stdout=outputs[key])

    identity = resolve_digest_pinned_image(
        registry_ref=registry_ref,
        platform="linux/amd64",
        expected_revision=revision,
        expected_role="compact-runtime",
        run=run,
    )

    assert identity.index_digest == registry_ref.rpartition("@")[2]
    assert identity.registry_ref == registry_ref.rpartition("@")[0] + "@" + manifest_digest
    assert identity.manifest_digest == manifest_digest
    assert identity.config_digest == config_digest
    assert identity.local_image_id == manifest_digest
    assert calls[2][:3] == ("docker", "pull", "--platform")


def test_read_only_probe_verifies_remote_config_without_pull() -> None:
    revision, registry_ref, manifest_digest, _config_digest, outputs = _fixture()
    manifest_ref = registry_ref.rpartition("@")[0] + "@" + manifest_digest
    image_command = (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--format",
        "{{json .Image}}",
        manifest_ref,
    )
    outputs[image_command] = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.role": "compact-runtime",
                }
            },
        }
    )
    calls: list[tuple[str, ...]] = []

    def run(command: Sequence[str]) -> ProcessOutcome:
        key = tuple(command)
        calls.append(key)
        return ProcessOutcome(command=key, returncode=0, stdout=outputs[key])

    identity = probe_digest_pinned_image(
        registry_ref=registry_ref,
        platform="linux/amd64",
        expected_revision=revision,
        expected_role="compact-runtime",
        run=run,
    )

    assert identity.role == "compact-runtime"
    assert identity.registry_ref == manifest_ref
    assert identity.index_digest != identity.manifest_digest
    assert not any(command[:2] == ("docker", "pull") for command in calls)
    assert not any(command[:3] == ("docker", "image", "inspect") for command in calls)


def test_read_only_probe_rejects_wrong_platform_direct_manifest() -> None:
    revision, _registry_ref, _manifest_digest, _config_digest, outputs = _fixture()
    manifest_raw = next(
        value
        for command, value in outputs.items()
        if command[:5] == ("docker", "buildx", "imagetools", "inspect", "--raw")
        and '"config"' in value
        and '"manifests"' not in value
    )
    direct_ref = f"registry.example/flow-steward@{_sha(manifest_raw)}"
    direct_outputs = {
        ("docker", "buildx", "imagetools", "inspect", "--raw", direct_ref): manifest_raw,
        (
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{json .Image}}",
            direct_ref,
        ): json.dumps(
            {
                "os": "linux",
                "architecture": "arm64",
                "config": {
                    "Labels": {
                        "org.opencontainers.image.revision": revision,
                        "org.opencontainers.image.role": "compact-runtime",
                    }
                },
            }
        ),
    }

    with pytest.raises(ValueError, match="requested platform"):
        probe_digest_pinned_image(
            registry_ref=direct_ref,
            platform="linux/amd64",
            expected_revision=revision,
            expected_role="compact-runtime",
            run=lambda command: ProcessOutcome(
                tuple(command), 0, stdout=direct_outputs[tuple(command)]
            ),
        )


def test_local_archive_retains_distinct_index_manifest_and_config_digests(
    tmp_path: Path,
) -> None:
    config_raw = json.dumps(
        {"architecture": "arm64", "os": "linux"}, separators=(",", ":")
    ).encode()
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config_digest},
            "layers": [],
        },
        separators=(",", ":"),
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    image_index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    index_digest = "sha256:" + hashlib.sha256(image_index_raw).hexdigest()
    archive_index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "digest": index_digest,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    docker_manifest_raw = json.dumps(
        [{"Config": f"blobs/sha256/{config_digest.removeprefix('sha256:')}", "Layers": []}],
        separators=(",", ":"),
    ).encode()
    archive_path = tmp_path / "image.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, payload in {
            "index.json": archive_index_raw,
            "manifest.json": docker_manifest_raw,
            f"blobs/sha256/{index_digest.removeprefix('sha256:')}": image_index_raw,
            f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}": manifest_raw,
            f"blobs/sha256/{config_digest.removeprefix('sha256:')}": config_raw,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    identity = resolve_local_image_archive(
        archive_path=archive_path,
        descriptor_digest=index_digest,
        platform="linux/arm64",
    )

    assert identity.index_digest == index_digest
    assert identity.manifest_digest == manifest_digest
    assert identity.config_digest == config_digest


@pytest.mark.parametrize(
    ("role", "architecture", "message"),
    [
        ("worker", "amd64", "role"),
        ("compact-runtime", "arm64", "platform"),
    ],
)
def test_registry_resolver_rejects_wrong_role_or_platform(
    role: str, architecture: str, message: str
) -> None:
    revision, registry_ref, _manifest, _config, outputs = _fixture(
        role=role, architecture=architecture
    )

    def run(command: Sequence[str]) -> ProcessOutcome:
        key = tuple(command)
        return ProcessOutcome(command=key, returncode=0, stdout=outputs[key])

    with pytest.raises(ValueError, match=message):
        resolve_digest_pinned_image(
            registry_ref=registry_ref,
            platform="linux/amd64",
            expected_revision=revision,
            expected_role="compact-runtime",
            run=run,
        )


def test_registry_resolver_rejects_mutable_reference_before_commands() -> None:
    with pytest.raises(ValueError, match="pinned"):
        resolve_digest_pinned_image(
            registry_ref="registry.example/flow-steward:latest",
            platform="linux/amd64",
            expected_revision="a" * 40,
            expected_role="compact-runtime",
            run=lambda command: ProcessOutcome(command=tuple(command), returncode=0),
        )


@pytest.mark.parametrize(
    ("reference", "pinned"),
    (
        ("registry.example/flow-steward@sha256:" + "b" * 64, True),
        ("localhost:5000/flow-steward@sha256:" + "b" * 64, True),
        ("fs-compact:47b080d61", False),
        ("registry.example/flow-steward@sha256:abc", False),
        ("@sha256:" + "b" * 64, False),
        ("registry.example/app@unexpected@sha256:" + "b" * 64, False),
        # A local image ID is pinned too, but it is not a registry reference: it names
        # nothing that can be pulled, and it takes a different path in the installer.
        ("sha256:" + "b" * 64, False),
        ("", False),
    ),
)
def test_a_reference_is_pinned_only_by_a_full_digest_after_a_repository(
    reference: str, pinned: bool
) -> None:
    """AC17-R139: the one definition the installer preflight and the resolver share."""
    from core.infrastructure.installer.registry_image_resolver import is_digest_pinned_reference

    assert is_digest_pinned_reference(reference) is pinned


def test_profile_role_map_covers_complete_compact_and_scalable_product_sets() -> None:
    assert tuple(role.role for role in required_product_roles("compact")) == (
        "compact-runtime",
        "fs-cli",
        "fs-mcp",
    )
    assert tuple(role.role for role in required_product_roles("scalable")) == (
        "fs-app",
        "fs-worker",
        "control-plane",
        "fs-cli",
        "fs-mcp",
    )
    assert {role.repository for role in required_product_roles("scalable")} == {
        "flow-steward-app",
        "flow-steward-worker",
        "flow-steward-control",
        "flow-steward-cli",
        "flow-steward-mcp",
    }
    with pytest.raises(ValueError, match="deployment profile"):
        required_product_roles("hobby")


def test_official_resolver_falls_back_with_a_complete_set_without_mixing() -> None:
    revision = "a" * 40
    registries = (
        OfficialRegistry(name="ghcr", namespace="ghcr.io/acme"),
        OfficialRegistry(name="docker_hub", namespace="docker.io/acme"),
    )
    inspected_tags: list[str] = []
    verified: list[tuple[str, str]] = []

    def run(command: Sequence[str]) -> ProcessOutcome:
        reference = str(command[-1])
        inspected_tags.append(reference)
        if reference == "ghcr.io/acme/flow-steward-cli:1.0.10":
            return ProcessOutcome(command=tuple(command), returncode=1, stderr="missing")
        raw = json.dumps({"schemaVersion": 2, "config": {"digest": "sha256:" + "c" * 64}})
        return ProcessOutcome(command=tuple(command), returncode=0, stdout=raw)

    def resolve_image(**values: object) -> RegistryImageIdentity:
        registry_ref = str(values["registry_ref"])
        role = str(values["expected_role"])
        verified.append((registry_ref, role))
        digest = registry_ref.rpartition("@")[2]
        return RegistryImageIdentity(
            registry_ref=registry_ref,
            index_digest=digest,
            manifest_digest="sha256:" + "d" * 64,
            config_digest="sha256:" + "c" * 64,
            revision=revision,
            role=role,
            platform="linux/amd64",
            local_image_id="sha256:" + "d" * 64,
        )

    resolved = resolve_official_image_set(
        profile="compact",
        version="1.0.10",
        revision=revision,
        platform="linux/amd64",
        registries=registries,
        run=run,
        resolve_image=resolve_image,
    )

    assert resolved.registry == registries[1]
    assert tuple(identity.role for identity in resolved.identities) == (
        "compact-runtime",
        "fs-cli",
        "fs-mcp",
        "postgres",
    )
    assert all(
        identity.registry_ref.startswith("docker.io/acme/") for identity in resolved.identities
    )
    assert any(ref.startswith("ghcr.io/acme/") for ref in inspected_tags)
    assert any(ref.startswith("docker.io/acme/") for ref in inspected_tags)
    assert any(ref.startswith("ghcr.io/acme/") for ref, _role in verified)
    assert all(ref.startswith(("ghcr.io/acme/", "docker.io/acme/")) for ref, _role in verified)


def test_official_resolver_fails_when_no_registry_has_the_complete_role_set() -> None:
    def run(command: Sequence[str]) -> ProcessOutcome:
        return ProcessOutcome(command=tuple(command), returncode=1, stderr="not found")

    registries = (
        OfficialRegistry(name="ghcr", namespace="ghcr.io/acme"),
        OfficialRegistry(name="docker_hub", namespace="docker.io/acme"),
    )

    with pytest.raises(ValueError, match="complete official image set"):
        resolve_official_image_set(
            profile="scalable",
            version="1.0.10",
            revision="a" * 40,
            platform="linux/arm64",
            registries=registries,
            run=run,
        )


def _tag_resolution(missing: frozenset[str] = frozenset()):
    inspected: list[str] = []

    def run(command: Sequence[str]) -> ProcessOutcome:
        reference = str(command[-1])
        inspected.append(reference)
        if reference in missing:
            return ProcessOutcome(command=tuple(command), returncode=1, stderr="not found")
        raw = json.dumps({"schemaVersion": 2, "reference": reference})
        return ProcessOutcome(command=tuple(command), returncode=0, stdout=raw)

    def resolve_image(**values: object) -> RegistryImageIdentity:
        registry_ref = str(values["registry_ref"])
        return RegistryImageIdentity(
            registry_ref=registry_ref,
            index_digest=registry_ref.rpartition("@")[2],
            manifest_digest="sha256:" + "d" * 64,
            config_digest="sha256:" + "c" * 64,
            revision=str(values["expected_revision"]),
            role=str(values["expected_role"]),
            platform=str(values["platform"]),
            local_image_id="sha256:" + "d" * 64,
        )

    return run, resolve_image, inspected


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_release_published_before_the_postgres_role_resolves_without_it(profile: str) -> None:
    """0.9.0 and 0.9.1 never published flow-steward-postgres; they must still install."""
    run, resolve_image, inspected = _tag_resolution()

    resolved = resolve_official_image_set(
        profile=profile,
        version="0.9.1",
        revision="a" * 40,
        platform="linux/amd64",
        registries=(OfficialRegistry(name="ghcr", namespace="ghcr.io/acme"),),
        run=run,
        resolve_image=resolve_image,
    )

    assert "postgres" not in {identity.role for identity in resolved.identities}
    assert not any("flow-steward-postgres" in reference for reference in inspected)


def test_registry_lacking_the_postgres_image_is_incomplete_for_a_release_that_publishes_it() -> (
    None
):
    run, resolve_image, inspected = _tag_resolution(
        frozenset({"ghcr.io/acme/flow-steward-postgres:0.9.2"})
    )
    registries = (
        OfficialRegistry(name="ghcr", namespace="ghcr.io/acme"),
        OfficialRegistry(name="docker_hub", namespace="docker.io/acme"),
    )

    resolved = resolve_official_image_set(
        profile="scalable",
        version="0.9.2",
        revision="a" * 40,
        platform="linux/amd64",
        registries=registries,
        run=run,
        resolve_image=resolve_image,
    )

    assert resolved.registry == registries[1]
    postgres = next(identity for identity in resolved.identities if identity.role == "postgres")
    assert postgres.registry_ref.startswith("docker.io/acme/flow-steward-postgres@sha256:")
    assert all(
        identity.registry_ref.startswith("docker.io/acme/") for identity in resolved.identities
    )
    assert "ghcr.io/acme/flow-steward-postgres:0.9.2" in inspected


@pytest.mark.parametrize("with_postgres", (True, False))
def test_exact_identity_verification_follows_the_archived_role_set(
    monkeypatch: pytest.MonkeyPatch, with_postgres: bool
) -> None:
    revision = "a" * 40
    roles = [*required_product_roles("compact")]
    if with_postgres:
        roles.append(next(role for role in all_release_roles() if role.role == "postgres"))
    images = tuple(
        ReleaseImageIdentity(
            role=role.role,
            registry_ref=f"ghcr.io/flow-steward/{role.repository}@sha256:{index + 1:064x}",
            index_digest=f"sha256:{index + 101:064x}",
            manifest_digest=f"sha256:{index + 1:064x}",
            config_digest=f"sha256:{index + 201:064x}",
            revision=revision,
            platform="linux/amd64",
        )
        for index, role in enumerate(roles)
    )
    archived = ReleaseIdentity(
        version="0.9.2",
        tag="v0.9.2",
        commit_sha=revision,
        registry="ghcr.io/flow-steward",
        profile="compact",
        platform="linux/amd64",
        images=images,
    )
    probed: list[str] = []

    def probe(**values) -> RegistryImageIdentity:
        expected = next(image for image in images if image.role == values["expected_role"])
        probed.append(expected.role)
        return RegistryImageIdentity(
            registry_ref=f"{str(values['registry_ref']).partition('@')[0]}@{expected.manifest_digest}",
            index_digest=expected.index_digest,
            manifest_digest=expected.manifest_digest,
            config_digest=expected.config_digest,
            revision=expected.revision,
            role=expected.role,
            platform=expected.platform,
            local_image_id=expected.config_digest,
        )

    monkeypatch.setattr(
        "core.infrastructure.installer.registry_image_resolver.probe_digest_pinned_image",
        probe,
    )
    monkeypatch.setattr(
        "core.infrastructure.installer.registry_image_resolver.runtime_env",
        {"FS_RELEASE_REGISTRIES": "ghcr.io/flow-steward"},
    )

    verified = OfficialReleaseResolver().verify_exact_identity(archived)

    assert ("postgres" in probed) is with_postgres
    assert [image.role for image in verified.images] == [image.role for image in images]
