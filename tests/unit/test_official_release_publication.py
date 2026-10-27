from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from core.application.release.images import all_release_roles
from scripts.release import official_release

_REVISION = "1" * 40


def _write_oci_artifact(
    artifact: Path, *, platform_name: str, revision: str, role: str
) -> dict[str, str]:
    operating_system, architecture = platform_name.split("/", 1)
    config_raw = json.dumps(
        {
            "os": operating_system,
            "architecture": architecture,
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.role": role,
                }
            },
        },
        sort_keys=True,
    ).encode()
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest_raw = json.dumps(
        {"schemaVersion": 2, "config": {"digest": config_digest}}, sort_keys=True
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    index_raw = json.dumps(
        {"schemaVersion": 2, "manifests": [{"digest": manifest_digest}]},
        sort_keys=True,
    ).encode()
    with tarfile.open(artifact, "w") as archive:
        for name, payload in (
            ("index.json", index_raw),
            (f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}", manifest_raw),
            (f"blobs/sha256/{config_digest.removeprefix('sha256:')}", config_raw),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return {"manifest_digest": manifest_digest, "config_digest": config_digest}


def test_native_platform_rejects_non_linux_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(official_release.platform, "system", lambda: "Darwin")

    with pytest.raises(ValueError, match="Linux host"):
        official_release.native_platform()


def test_validate_release_requires_exact_version_tag_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    def run(command, *, cwd):
        del cwd
        if tuple(command) == ("git", "status", "--porcelain", "--untracked-files=all"):
            return ""
        return _REVISION + "\n"

    monkeypatch.setattr(official_release, "_run", run)

    official_release.validate_release(tmp_path, "1.2.3", "v1.2.3", _REVISION)

    with pytest.raises(ValueError, match=r"vX\.Y\.Z"):
        official_release.validate_release(tmp_path, "1.2.3", "release-1.2.3", _REVISION)


def test_validate_release_rejects_checkout_at_a_different_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    def run(command, *, cwd):
        del cwd
        if tuple(command) == ("git", "rev-parse", "HEAD"):
            return "2" * 40 + "\n"
        if tuple(command) == ("git", "status", "--porcelain", "--untracked-files=all"):
            return ""
        return _REVISION + "\n"

    monkeypatch.setattr(official_release, "_run", run)

    with pytest.raises(ValueError, match="checkout HEAD"):
        official_release.validate_release(tmp_path, "1.2.3", "v1.2.3", _REVISION)


def test_validate_release_rejects_dirty_build_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    def run(command, *, cwd):
        del cwd
        if tuple(command) == ("git", "status", "--porcelain", "--untracked-files=all"):
            return "?? untracked-build-input\n"
        return _REVISION + "\n"

    monkeypatch.setattr(official_release, "_run", run)

    with pytest.raises(ValueError, match="working tree"):
        official_release.validate_release(tmp_path, "1.2.3", "v1.2.3", _REVISION)


def test_native_build_builds_every_role_once_and_writes_checksummed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def run(command, *, cwd):
        del cwd
        command = tuple(command)
        commands.append(command)
        if command[:3] == ("docker", "buildx", "inspect"):
            assert command == ("docker", "buildx", "inspect", "--bootstrap")
            return (
                "Name: release\nDriver: docker-container\nNodes:\n"
                "Name: release0\nEndpoint: desktop-linux\n"
            )
        if command == ("docker", "context", "show"):
            return "desktop-linux\n"
        if command[:3] == ("docker", "context", "inspect"):
            return "unix:///var/run/docker.sock\n"
        if command[:2] == ("docker", "info"):
            assert command == (
                "docker",
                "info",
                "--format",
                "{{.OSType}}/{{.Architecture}}",
            )
            return "linux/amd64\n"
        output = next(item for item in command if item.startswith("type=oci,dest="))
        role = next(
            item.removeprefix("org.opencontainers.image.role=")
            for item in command
            if item.startswith("org.opencontainers.image.role=")
        )
        _write_oci_artifact(
            Path(output.removeprefix("type=oci,dest=")),
            platform_name="linux/amd64",
            revision=_REVISION,
            role=role,
        )
        return ""

    monkeypatch.setattr(official_release, "_run", run)
    monkeypatch.setattr(official_release, "native_platform", lambda: "linux/amd64")
    output = tmp_path / "artifacts"

    official_release.build(tmp_path, "1.2.3", _REVISION, "linux/amd64", output)

    builds = [command for command in commands if command[:3] == ("docker", "buildx", "build")]
    assert len(builds) == len(all_release_roles())
    assert {
        command[command.index("--target") + 1] for command in builds if "--target" in command
    } == {
        "compact-runtime",
        "app-runtime",
        "worker-runtime",
        "control-runtime",
        "cli",
        "runtime",
    }
    postgres = next(
        command for command in builds if "org.opencontainers.image.role=postgres" in command
    )
    assert "--target" not in postgres
    assert postgres[postgres.index("--file") + 1] == "infra/docker/postgres-extended/Dockerfile"
    assert "AGE_REF=PG17/v1.6.0-rc0" in postgres
    assert f"org.opencontainers.image.revision={_REVISION}" in postgres
    assert postgres[-1] == "."
    compact = next(command for command in builds if "compact-runtime" in command)
    assert f"BUILD_SHA={_REVISION}" in compact
    platform_argument = "FS_IMAGE_" + "PLATFORM=linux/amd64"
    assert platform_argument in compact
    assert all(platform_argument not in command for command in builds if command is not compact)
    metadata = json.loads((output / "build-amd64.json").read_text(encoding="utf-8"))
    assert len(metadata["images"]) == len(all_release_roles())
    assert all(item["sha256"] for item in metadata["images"])


def test_build_rejects_non_native_selected_docker_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(official_release, "native_platform", lambda: "linux/amd64")

    def run(command, *, cwd):
        del cwd
        if tuple(command) == ("docker", "buildx", "inspect", "--bootstrap"):
            return "Name: release\nDriver: docker-container\nEndpoint: desktop-linux\n"
        if tuple(command) == ("docker", "context", "show"):
            return "desktop-linux\n"
        if tuple(command) == (
            "docker",
            "context",
            "inspect",
            "desktop-linux",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ):
            return "unix:///var/run/docker.sock\n"
        if tuple(command) == (
            "docker",
            "info",
            "--format",
            "{{.OSType}}/{{.Architecture}}",
        ):
            return "linux/arm64\n"
        raise AssertionError(command)

    monkeypatch.setattr(official_release, "_run", run)

    with pytest.raises(ValueError, match="not the requested native platform"):
        official_release.build(tmp_path, "1.2.3", _REVISION, "linux/amd64", tmp_path)


def test_artifact_identity_rejects_wrong_direct_manifest_platform(tmp_path: Path) -> None:
    config = {
        "os": "linux",
        "architecture": "arm64",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": _REVISION,
                "org.opencontainers.image.role": "fs-app",
            }
        },
    }
    config_raw = json.dumps(config, sort_keys=True).encode()
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest = {"config": {"digest": config_digest}}
    manifest_raw = json.dumps(manifest, sort_keys=True).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    index_raw = json.dumps({"manifests": [{"digest": manifest_digest}]}).encode()
    artifact = tmp_path / "wrong-platform.oci.tar"
    with tarfile.open(artifact, "w") as archive:
        for name, payload in (
            ("index.json", index_raw),
            (f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}", manifest_raw),
            (f"blobs/sha256/{config_digest.removeprefix('sha256:')}", config_raw),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="wrong platform config"):
        official_release._artifact_identity(
            artifact,
            platform_name="linux/amd64",
            revision=_REVISION,
            role="fs-app",
        )


def test_publication_copies_native_artifacts_then_creates_two_platform_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []
    child_raws: dict[str, str] = {}
    for platform_name in ("linux/amd64", "linux/arm64"):
        architecture = platform_name.split("/", 1)[1]
        images = []
        for role in all_release_roles():
            artifact = tmp_path / f"{role.role}-{architecture}.oci.tar"
            artifact.write_bytes(f"{role.role}-{architecture}".encode())
            child = f"{role.role}-{architecture}"
            child_raws[child] = json.dumps({"child": child}, sort_keys=True)
            images.append(
                {
                    "role": role.role,
                    "repository": role.repository,
                    "platform": platform_name,
                    "artifact": artifact.name,
                    "sha256": official_release.hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "manifest_digest": "sha256:"
                    + hashlib.sha256(child_raws[child].encode()).hexdigest(),
                    "config_digest": "sha256:" + "c" * 64,
                }
            )
        (tmp_path / f"build-{architecture}.json").write_text(
            json.dumps(
                {
                    "version": "1.2.3",
                    "revision": _REVISION,
                    "platform": platform_name,
                    "images": images,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("FS_RELEASE_GHCR_NAMESPACE", "ghcr.io/example")
    monkeypatch.setenv("FS_RELEASE_DOCKERHUB_NAMESPACE", "docker.io/example")

    def run(command, *, cwd):
        del cwd
        command = tuple(command)
        commands.append(command)
        if command[:3] == ("skopeo", "inspect", "--raw"):
            reference = command[-1].removeprefix("docker://")
            leaf = reference.rsplit("/", 1)[1]
            repository, architecture = leaf.rsplit(":1.2.3-", 1)
            role = next(item.role for item in all_release_roles() if item.repository == repository)
            return child_raws[f"{role}-{architecture}"]
        return ""

    monkeypatch.setattr(official_release, "_run", run)

    official_release.publish(tmp_path, "1.2.3", _REVISION, tmp_path)

    copies = [command for command in commands if command[:2] == ("skopeo", "copy")]
    indexes = [command for command in commands if command[:3] == ("docker", "buildx", "imagetools")]
    assert len(copies) == 2 * 2 * len(all_release_roles())
    assert len(indexes) == 2 * len(all_release_roles())
    assert {command[command.index("--tag") + 1] for command in indexes} >= {
        "ghcr.io/example/flow-steward-postgres:1.2.3",
        "docker.io/example/flow-steward-postgres:1.2.3",
    }
    assert not any(part == "build" for command in copies for part in command)
    for command in copies:
        architecture = command[command.index("--override-arch") + 1]
        assert command[:7] == (
            "skopeo",
            "copy",
            "--preserve-digests",
            "--override-os",
            "linux",
            "--override-arch",
            architecture,
        )
        assert architecture in {"amd64", "arm64"}
    for command in indexes:
        tag_index = command.index("--tag")
        tagged_repository = command[tag_index + 1].rsplit(":", 1)[0]
        repository = tagged_repository.rsplit("/", 1)[1]
        role = next(item.role for item in all_release_roles() if item.repository == repository)
        expected = tuple(
            f"{tagged_repository}@sha256:"
            + hashlib.sha256(child_raws[f"{role}-{architecture}"].encode()).hexdigest()
            for architecture in ("amd64", "arm64")
        )
        assert command[tag_index + 2 :] == expected


def test_verification_accepts_one_complete_registry_and_records_incomplete_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_RELEASE_GHCR_NAMESPACE", "ghcr.io/example")
    monkeypatch.setenv("FS_RELEASE_DOCKERHUB_NAMESPACE", "docker.io/example")
    child_raws = {
        "linux/amd64": json.dumps({"config": {"digest": "sha256:" + "e" * 64}}, sort_keys=True),
        "linux/arm64": json.dumps({"config": {"digest": "sha256:" + "f" * 64}}, sort_keys=True),
    }
    child_digests = {
        platform: "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        for platform, raw in child_raws.items()
    }
    platforms = {
        "manifests": [
            {
                "digest": child_digests["linux/amd64"],
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": child_digests["linux/arm64"],
                "platform": {"os": "linux", "architecture": "arm64"},
            },
            {
                "digest": "sha256:" + "c" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            },
        ]
    }

    def run(command, *, cwd):
        del cwd
        reference = command[-1]
        if reference.startswith("docker://docker.io/example/"):
            raise official_release.subprocess.CalledProcessError(1, command)
        if "--format" in command:
            return "sha256:" + "d" * 64 + "\n"
        if "--config" in command:
            role = reference.split("@", 1)[0].rsplit("/", 1)[1]
            digest = reference.rsplit("@", 1)[1]
            platform_name = next(
                platform for platform, expected in child_digests.items() if expected == digest
            )
            operating_system, architecture = platform_name.split("/", 1)
            expected_role = next(
                item.role for item in all_release_roles() if item.repository == role
            )
            return json.dumps(
                {
                    "os": operating_system,
                    "architecture": architecture,
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": _REVISION,
                            "org.opencontainers.image.role": expected_role,
                        }
                    },
                }
            )
        if "@sha256:" in reference:
            digest = reference.rsplit("@", 1)[1]
            platform = next(
                platform for platform, expected in child_digests.items() if expected == digest
            )
            return child_raws[platform]
        return json.dumps(platforms, sort_keys=True)

    monkeypatch.setattr(official_release, "_run", run)
    evidence = tmp_path / "verification.json"

    official_release.verify(tmp_path, "1.2.3", _REVISION, evidence)

    result = json.loads(evidence.read_text(encoding="utf-8"))
    assert [item["status"] for item in result["registries"]] == [
        "verified",
        "incomplete",
    ]
    assert len(result["registries"][0]["images"]) == len(all_release_roles())
    assert {(item["role"], item["reference"]) for item in result["registries"][0]["images"]} >= {
        ("postgres", "ghcr.io/example/flow-steward-postgres:1.2.3")
    }
    assert result["registries"][0]["images"][0]["platforms"] == {
        "linux/amd64": {
            "config_digest": "sha256:" + "e" * 64,
            "manifest_digest": child_digests["linux/amd64"],
        },
        "linux/arm64": {
            "config_digest": "sha256:" + "f" * 64,
            "manifest_digest": child_digests["linux/arm64"],
        },
    }
    assert result["failures"]
    assert "docker.io/example" in result["failures"][0]


def test_verification_rejects_duplicate_runtime_platform_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_RELEASE_GHCR_NAMESPACE", "ghcr.io/example")
    monkeypatch.delenv("FS_RELEASE_DOCKERHUB_NAMESPACE", raising=False)
    duplicated = {
        "manifests": [
            {
                "digest": "sha256:" + "a" * 64,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": "sha256:" + "b" * 64,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": "sha256:" + "c" * 64,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ]
    }
    monkeypatch.setattr(
        official_release,
        "_run",
        lambda command, *, cwd: json.dumps(duplicated),
    )

    with pytest.raises(ValueError, match="complete verified release set"):
        official_release.verify(tmp_path, "1.2.3", _REVISION, tmp_path / "evidence.json")


def test_verification_marks_a_complete_but_digest_divergent_mirror_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_RELEASE_GHCR_NAMESPACE", "ghcr.io/example")
    monkeypatch.setenv("FS_RELEASE_DOCKERHUB_NAMESPACE", "docker.io/example")

    primary_raws = {
        "linux/amd64": json.dumps({"config": {"digest": "sha256:" + "3" * 64}}, sort_keys=True),
        "linux/arm64": json.dumps({"config": {"digest": "sha256:" + "4" * 64}}, sort_keys=True),
    }
    mirror_raws = {
        "linux/amd64": primary_raws["linux/amd64"],
        "linux/arm64": json.dumps({"config": {"digest": "sha256:" + "9" * 64}}, sort_keys=True),
    }

    def digests(raws):
        return {
            platform: "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
            for platform, raw in raws.items()
        }

    primary_digests = digests(primary_raws)
    mirror_digests = digests(mirror_raws)

    def run(command, *, cwd):
        del cwd
        reference = command[-1]
        mirror = reference.startswith("docker://docker.io/")
        if "--format" in command:
            return "sha256:" + ("1" if mirror else "2") * 64
        if "--config" in command:
            repository = reference.split("@", 1)[0].rsplit("/", 1)[1]
            digest = reference.rsplit("@", 1)[1]
            selected_digests = mirror_digests if mirror else primary_digests
            platform_name = next(
                platform for platform, expected in selected_digests.items() if expected == digest
            )
            operating_system, architecture = platform_name.split("/", 1)
            expected_role = next(
                item.role for item in all_release_roles() if item.repository == repository
            )
            return json.dumps(
                {
                    "os": operating_system,
                    "architecture": architecture,
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": _REVISION,
                            "org.opencontainers.image.role": expected_role,
                        }
                    },
                }
            )
        selected_raws = mirror_raws if mirror else primary_raws
        selected_digests = mirror_digests if mirror else primary_digests
        if "@sha256:" in reference:
            digest = reference.rsplit("@", 1)[1]
            platform = next(
                platform for platform, expected in selected_digests.items() if expected == digest
            )
            return selected_raws[platform]
        return json.dumps(
            {
                "manifests": [
                    {
                        "digest": selected_digests["linux/amd64"],
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "digest": selected_digests["linux/arm64"],
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                ]
            }
        )

    monkeypatch.setattr(official_release, "_run", run)
    evidence = tmp_path / "verification.json"

    official_release.verify(tmp_path, "1.2.3", _REVISION, evidence)

    result = json.loads(evidence.read_text(encoding="utf-8"))
    assert [item["status"] for item in result["registries"]] == [
        "verified",
        "incomplete",
    ]
    assert "does not match verified child digests" in result["failures"][0]


def test_postgres_release_build_uses_the_age_ref_every_compose_build_uses() -> None:
    """The published PostgreSQL image must be the image a source install builds."""
    root = Path(__file__).resolve().parents[2]
    age_ref = official_release._POSTGRES_AGE_REF
    for compose in (
        "infra/docker/docker-compose.yaml",
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/docker-compose.postgres-extended.yaml",
    ):
        text = (root / compose).read_text(encoding="utf-8")
        assert "AGE_REF: ${AGE_REF:-" + age_ref + "}" in text, compose
    dockerfile = root / "infra/docker/postgres-extended/Dockerfile"
    assert f"ARG AGE_REF={age_ref}\n" in dockerfile.read_text(encoding="utf-8")


def test_every_release_role_has_a_build_target() -> None:
    assert set(official_release._TARGETS) == {role.role for role in all_release_roles()}
