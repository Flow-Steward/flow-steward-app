"""Clone's exact-release contract uses immutable whole-registry fallback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import PublishedRelease, ReleaseCoordinates, ReleaseVersion
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.application.release.service import OfficialReleaseSelector
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import OfficialReleaseResolver

_PRIMARY = "ghcr.io/example"
_MIRROR = "docker.io/example"
_REVISION = "a" * 40
_VERSION = "1.2.3"


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _RegistryMetadata:
    archived: ReleaseIdentity
    outputs: dict[tuple[str, ...], str]
    calls: list[tuple[str, ...]] = field(default_factory=list)
    blocked_namespaces: tuple[str, ...] = (_PRIMARY,)
    unavailable: set[tuple[str, ...]] = field(default_factory=set)

    def run(self, command: Sequence[str]) -> ProcessOutcome:
        key = tuple(command)
        self.calls.append(key)
        assert key[:4] == ("docker", "buildx", "imagetools", "inspect")
        if key in self.unavailable or any(
            key[-1].startswith(namespace + "/") for namespace in self.blocked_namespaces
        ):
            return ProcessOutcome(command=key, returncode=1, stderr="primary registry unavailable")
        assert key in self.outputs, f"Unexpected registry command: {key}"
        return ProcessOutcome(command=key, returncode=0, stdout=self.outputs[key])


def _metadata(profile: str, platform: str, *, corruption: str = "") -> _RegistryMetadata:
    architecture = platform.split("/")[1]
    outputs: dict[tuple[str, ...], str] = {}
    images = []
    for role in required_product_roles(profile):
        first = not images
        image_raw = _json(
            {
                "os": "linux",
                "architecture": architecture,
                "config": {
                    "Labels": {
                        "org.opencontainers.image.revision": _REVISION,
                        "org.opencontainers.image.role": role.role,
                    }
                },
            }
        )
        if first and corruption.startswith("image-"):
            image = json.loads(image_raw)
            if corruption == "image-array":
                image = [image]
            elif corruption == "image-fields-array":
                image["config"] = [image["config"]]
            elif corruption == "image-labels-array":
                image["config"]["Labels"] = [image["config"]["Labels"]]
            elif corruption == "image-revision":
                image["config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
            elif corruption == "image-role":
                image["config"]["Labels"]["org.opencontainers.image.role"] = "wrong-role"
            elif corruption == "image-platform":
                image["architecture"] = "arm64" if architecture == "amd64" else "amd64"
            image_raw = _json(image)
        if first and corruption == "image-invalid-json":
            image_raw = "{"
        config_digest = _digest(image_raw)
        manifest_raw = _json(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": config_digest},
                "layers": [],
            }
        )
        if first and corruption == "manifest-array":
            manifest_raw = _json([json.loads(manifest_raw)])
        elif first and corruption == "manifest-config-array":
            manifest = json.loads(manifest_raw)
            manifest["config"] = [manifest["config"]]
            manifest_raw = _json(manifest)
        elif first and corruption == "manifest-invalid-json":
            manifest_raw = "{"
        manifest_digest = _digest(manifest_raw)
        index_raw = _json(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "digest": manifest_digest,
                        "platform": {"os": "linux", "architecture": architecture},
                    }
                ],
            }
        )
        if first and corruption == "index-array":
            index_raw = _json([json.loads(index_raw)])
        elif first and corruption == "index-invalid-json":
            index_raw = "{"
        elif first and corruption.startswith("index-platform-"):
            index = json.loads(index_raw)
            index["manifests"][0]["platform"] = {
                "index-platform-null": None,
                "index-platform-list": ["linux", architecture],
                "index-platform-string": platform,
            }[corruption]
            index_raw = _json(index)
        index_digest = _digest(index_raw)
        images.append(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"{_PRIMARY}/{role.repository}@{manifest_digest}",
                index_digest=index_digest,
                manifest_digest=manifest_digest,
                config_digest=config_digest,
                revision=_REVISION,
                platform=platform,
            )
        )
        for namespace in (_PRIMARY, _MIRROR):
            repository = f"{namespace}/{role.repository}"
            for reference in (f"{repository}:{_VERSION}", f"{repository}@{index_digest}"):
                outputs[("docker", "buildx", "imagetools", "inspect", "--raw", reference)] = (
                    index_raw
                )
            child_ref = f"{repository}@{manifest_digest}"
            outputs[("docker", "buildx", "imagetools", "inspect", "--raw", child_ref)] = (
                manifest_raw
            )
            outputs[
                (
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    "--format",
                    "{{json .Image}}",
                    child_ref,
                )
            ] = image_raw
    return _RegistryMetadata(
        archived=ReleaseIdentity(
            version=_VERSION,
            tag="v" + _VERSION,
            commit_sha=_REVISION,
            registry=_PRIMARY,
            profile=profile,
            platform=platform,
            images=tuple(images),
        ),
        outputs=outputs,
    )


class _UnusedDiscovery:
    def latest(self) -> PublishedRelease:
        raise AssertionError("Exact Clone verification must not discover a mutable release")

    def for_version(self, version: str) -> PublishedRelease:
        raise AssertionError("Exact Clone verification must not discover a mutable release")


class _UnusedState:
    def load_installed_release(self) -> ReleaseIdentity | None:
        raise AssertionError("Exact Clone verification must not replace the archived identity")

    def load_pending_release(self) -> ReleaseIdentity | None:
        raise AssertionError("Exact Clone verification must not replace the archived identity")


def test_normal_release_fallback_fixture_has_one_complete_verified_role_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    resolver = OfficialReleaseResolver()
    monkeypatch.setenv("FS_RELEASE_REGISTRIES", _PRIMARY + "," + _MIRROR)
    monkeypatch.setattr(resolver._executor, "run", fixture.run)

    resolved = resolver.resolve_coordinates(
        ReleaseCoordinates(
            repository="example/releases",
            version=ReleaseVersion.parse(_VERSION),
            tag="v" + _VERSION,
            commit_sha=_REVISION,
            source="synthetic registry metadata control",
        ),
        profile="scalable",
        platform="linux/amd64",
    )

    assert resolved.registry == _MIRROR
    assert {image.role for image in resolved.images} == {
        role.role for role in required_product_roles("scalable")
    }
    assert all(image.registry_ref.startswith(_MIRROR + "/") for image in resolved.images)


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_clone_exact_identity_falls_back_using_only_archived_index_digests(
    monkeypatch: pytest.MonkeyPatch, profile: str, platform: str
) -> None:
    fixture = _metadata(profile, platform)
    resolver = OfficialReleaseResolver()
    monkeypatch.setenv("FS_RELEASE_REGISTRIES", _PRIMARY + "," + _MIRROR)
    monkeypatch.setattr(resolver._executor, "run", fixture.run)
    selector = OfficialReleaseSelector(
        discovery=_UnusedDiscovery(),
        state=_UnusedState(),
        resolver=resolver,
        profile=profile,
        platform=platform,
    )

    resolved = selector.for_exact_identity(fixture.archived)

    assert resolved.registry == _MIRROR
    expected = {image.role: image for image in fixture.archived.images}
    assert set(expected) == {image.role for image in resolved.images}
    for image in resolved.images:
        archived = expected[image.role]
        assert image.registry_ref == archived.registry_ref.replace(_PRIMARY + "/", _MIRROR + "/")
        assert image.model_dump(exclude={"registry_ref"}) == archived.model_dump(
            exclude={"registry_ref"}
        )
    assert all("@sha256:" in command[-1] for command in fixture.calls)


def _resolver(
    monkeypatch: pytest.MonkeyPatch, fixture: _RegistryMetadata
) -> OfficialReleaseResolver:
    resolver = OfficialReleaseResolver()
    monkeypatch.setenv("FS_RELEASE_REGISTRIES", _PRIMARY + "," + _MIRROR)
    monkeypatch.setattr(resolver._executor, "run", fixture.run)
    return resolver


def _index_command(fixture: _RegistryMetadata, namespace: str, position: int) -> tuple[str, ...]:
    role = required_product_roles(fixture.archived.profile)[position]
    image = next(image for image in fixture.archived.images if image.role == role.role)
    return (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--raw",
        f"{namespace}/{role.repository}@{image.index_digest}",
    )


@pytest.mark.parametrize("first_namespace", [_PRIMARY, _MIRROR])
def test_exact_verification_respects_configured_registry_order(
    monkeypatch: pytest.MonkeyPatch,
    first_namespace: str,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    fixture.blocked_namespaces = ()
    resolver = _resolver(monkeypatch, fixture)
    second = _MIRROR if first_namespace == _PRIMARY else _PRIMARY
    monkeypatch.setenv("FS_RELEASE_REGISTRIES", first_namespace + "," + second)

    result = resolver.verify_exact_identity(fixture.archived)

    assert result.registry == first_namespace
    assert all(command[-1].startswith(first_namespace + "/") for command in fixture.calls)
    assert all("@sha256:" in command[-1] for command in fixture.calls)


def test_exact_verification_discards_a_partial_primary_without_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    fixture.blocked_namespaces = ()
    fixture.unavailable.add(_index_command(fixture, _PRIMARY, 2))
    resolver = _resolver(monkeypatch, fixture)

    result = resolver.verify_exact_identity(fixture.archived)

    assert result.registry == _MIRROR
    assert all(image.registry_ref.startswith(_MIRROR + "/") for image in result.images)
    assert _index_command(fixture, _PRIMARY, 0) in fixture.calls
    assert all(
        _index_command(fixture, _MIRROR, position) in fixture.calls
        for position in range(len(fixture.archived.images))
    )


def test_exact_verification_cannot_join_two_incomplete_registry_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    fixture.blocked_namespaces = ()
    fixture.unavailable.update(
        {_index_command(fixture, _PRIMARY, 2), _index_command(fixture, _MIRROR, 0)}
    )
    resolver = _resolver(monkeypatch, fixture)

    with pytest.raises(ValueError, match="No complete archived exact image set"):
        resolver.verify_exact_identity(fixture.archived)


@pytest.mark.parametrize(
    "corruption",
    [
        "index-array",
        "manifest-array",
        "manifest-config-array",
        "image-array",
        "image-fields-array",
        "image-labels-array",
        "image-revision",
        "image-role",
        "image-platform",
        "index-invalid-json",
        "manifest-invalid-json",
        "image-invalid-json",
        "index-platform-null",
        "index-platform-list",
        "index-platform-string",
    ],
)
def test_exact_verification_rejects_malformed_or_mismatched_metadata(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    fixture = _metadata("scalable", "linux/amd64", corruption=corruption)
    resolver = _resolver(monkeypatch, fixture)

    with pytest.raises(ValueError, match="No complete archived exact image set"):
        resolver.verify_exact_identity(fixture.archived)


@pytest.mark.parametrize("digest_field", ["index_digest", "manifest_digest", "config_digest"])
def test_exact_verification_rejects_changed_archived_digests(
    monkeypatch: pytest.MonkeyPatch,
    digest_field: str,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    resolver = _resolver(monkeypatch, fixture)
    first = fixture.archived.images[0]
    if digest_field == "index_digest":
        command = _index_command(fixture, _MIRROR, 0)
        index = json.loads(fixture.outputs[command])
        index["unexpected"] = "changed OCI content"
        fixture.outputs[command] = _json(index)
    elif digest_field == "manifest_digest":
        command = (
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            first.registry_ref.replace(_PRIMARY + "/", _MIRROR + "/"),
        )
        manifest = json.loads(fixture.outputs[command])
        manifest["unexpected"] = "changed OCI content"
        fixture.outputs[command] = _json(manifest)
    else:
        fixture.archived = fixture.archived.model_copy(
            update={
                "images": (
                    first.model_copy(update={"config_digest": "sha256:" + "f" * 64}),
                    *fixture.archived.images[1:],
                )
            }
        )

    with pytest.raises(ValueError, match="No complete archived exact image set"):
        resolver.verify_exact_identity(fixture.archived)


def test_exact_verification_rejects_noncanonical_archived_role_repositories_before_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    first = fixture.archived.images[0]
    fixture.archived = fixture.archived.model_copy(
        update={
            "images": (
                first.model_copy(
                    update={"registry_ref": f"{_PRIMARY}/unrelated@{first.manifest_digest}"}
                ),
                *fixture.archived.images[1:],
            )
        }
    )
    resolver = _resolver(monkeypatch, fixture)

    with pytest.raises(ValueError, match="noncanonical repository"):
        resolver.verify_exact_identity(fixture.archived)
    assert fixture.calls == []


def test_clone_exact_selector_keeps_target_context_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    selector = OfficialReleaseSelector(
        discovery=_UnusedDiscovery(),
        state=_UnusedState(),
        resolver=_resolver(monkeypatch, fixture),
        profile="compact",
        platform="linux/amd64",
    )

    with pytest.raises(LifecycleError) as error:
        selector.for_exact_identity(fixture.archived)
    assert error.value.code == "release_target_context_mismatch"


def test_generic_verification_does_not_change_archived_namespace_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    resolver = _resolver(monkeypatch, fixture)

    with pytest.raises(ValueError, match="OCI image command failed"):
        resolver.verify(fixture.archived)
    assert fixture.calls == [_index_command(fixture, _PRIMARY, 0)]


def test_exact_verification_falls_back_after_malformed_primary_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    fixture.blocked_namespaces = ()
    first = fixture.archived.images[0]
    image_command = (
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--format",
        "{{json .Image}}",
        first.registry_ref,
    )
    fixture.outputs[image_command] = _json([json.loads(fixture.outputs[image_command])])
    resolver = _resolver(monkeypatch, fixture)

    result = resolver.verify_exact_identity(fixture.archived)

    assert result.registry == _MIRROR
    assert image_command in fixture.calls
    assert all(image.registry_ref.startswith(_MIRROR + "/") for image in result.images)
    assert all("@sha256:" in command[-1] for command in fixture.calls)


def test_exact_verification_does_not_swallow_unexpected_executor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    resolver = _resolver(monkeypatch, fixture)

    def unexpected_failure(command: Sequence[str]) -> ProcessOutcome:
        raise RuntimeError("unexpected executor implementation failure")

    monkeypatch.setattr(resolver._executor, "run", unexpected_failure)
    with pytest.raises(RuntimeError, match="unexpected executor implementation failure"):
        resolver.verify_exact_identity(fixture.archived)


def test_clone_exact_selector_keeps_immutable_content_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _metadata("scalable", "linux/amd64")
    resolver = _resolver(monkeypatch, fixture)
    first = fixture.archived.images[0]
    wrong = fixture.archived.model_copy(
        update={
            "images": (
                first.model_copy(update={"config_digest": "sha256:" + "f" * 64}),
                *fixture.archived.images[1:],
            )
        }
    )
    monkeypatch.setattr(resolver, "verify_exact_identity", lambda _release: wrong)
    selector = OfficialReleaseSelector(
        discovery=_UnusedDiscovery(),
        state=_UnusedState(),
        resolver=resolver,
        profile="scalable",
        platform="linux/amd64",
    )

    with pytest.raises(LifecycleError) as error:
        selector.for_exact_identity(fixture.archived)
    assert error.value.code == "release_exact_image_set_mismatch"
