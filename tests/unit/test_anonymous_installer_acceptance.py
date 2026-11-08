"""Anonymous pulls must prove the published bytes, not publisher access."""

from __future__ import annotations

import json
import subprocess

import pytest

from core.application.tooling import anonymous_installer_acceptance as acceptance

REVISION = "a" * 40
INDEX = "sha256:" + "b" * 64
PLATFORMS = {
    "linux/amd64": {"manifest_digest": "sha256:" + "c" * 64, "config_digest": "sha256:" + "d" * 64},
    "linux/arm64": {"manifest_digest": "sha256:" + "e" * 64, "config_digest": "sha256:" + "f" * 64},
}


class DockerBoundary:
    def __init__(self, fail_pull=False, wrong_config=False):
        self.commands = []
        self.fail_pull = fail_pull
        self.wrong_config = wrong_config

    def __call__(self, args, **kwargs):
        self.commands.append((args, kwargs))
        output = ""
        if "exec" in args and "/usr/local/bin/docker" in args:
            command = args[args.index("/usr/local/bin/docker") + 1 :]
            if command[:2] == ["version", "--format"]:
                output = "1.49"
            elif command == ["image", "ls", "--quiet"]:
                output = ""
            elif command[:2] == ["manifest", "inspect"]:
                output = json.dumps(
                    [
                        {
                            "Descriptor": {
                                "digest": values["manifest_digest"],
                                "platform": {"os": "linux", "architecture": platform.split("/")[1]},
                            }
                        }
                        for platform, values in PLATFORMS.items()
                    ]
                )
            elif command[0] == "pull":
                if self.fail_pull:
                    raise subprocess.CalledProcessError(1, args, stderr="denied: anonymous access")
                output = "Digest: " + INDEX
            elif command[:2] == ["image", "inspect"]:
                platform = command[command.index("--platform") + 1]
                output = json.dumps(
                    [
                        {
                            "Id": "sha256:" + "0" * 64
                            if self.wrong_config
                            else PLATFORMS[platform]["config_digest"],
                            "Os": "linux",
                            "Architecture": platform.split("/")[1],
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.role": "installer",
                                    "org.opencontainers.image.revision": REVISION,
                                }
                            },
                        }
                    ]
                )
        return subprocess.CompletedProcess(args, 0, output, "")


def test_fresh_daemon_pulls_both_platforms_without_credentials_or_helpers(tmp_path, monkeypatch):
    boundary = DockerBoundary()
    monkeypatch.setattr(acceptance.subprocess, "run", boundary)
    result = acceptance.check_registry(
        "ghcr.io/flow-steward/flow-steward-installer:1.2.3",
        {"index_digest": INDEX, "platforms": PLATFORMS},
        REVISION,
        tmp_path,
    )
    assert set(result["platforms"]) == set(PLATFORMS)
    commands = [args for args, _ in boundary.commands]
    assert sum("pull" in args for args in commands) == 2
    creation = next(args for args in commands if "create" in args and "--privileged" in args)
    assert "--privileged" in creation
    assert not any("source=/var/run/docker.sock" in arg for arg in creation)
    initialization = next(args for args in commands if "mkdir" in " ".join(args))
    assert '{"auths":{},"credHelpers":{}}' in initialization[-1]
    executions = [args for args in commands if "/usr/local/bin/docker" in args]
    assert all("PATH=/acceptance/bin" in args for args in executions)
    assert all("DOCKER_CONFIG=/acceptance/client" in args for args in executions)
    assert any(args[:3] == ["docker", "container", "rm"] for args in commands)
    assert any(args[:3] == ["docker", "volume", "rm"] for args in commands)
    assert (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize(
    "fail_pull,wrong_config,exception",
    [(True, False, subprocess.CalledProcessError), (False, True, ValueError)],
)
def test_failed_pull_or_wrong_identity_still_cleans_exact_owned_daemon(
    tmp_path, monkeypatch, fail_pull, wrong_config, exception
):
    boundary = DockerBoundary(fail_pull=fail_pull, wrong_config=wrong_config)
    monkeypatch.setattr(acceptance.subprocess, "run", boundary)
    with pytest.raises(exception):
        acceptance.check_registry(
            "ghcr.io/flow-steward/flow-steward-installer:1.2.3",
            {"index_digest": INDEX, "platforms": PLATFORMS},
            REVISION,
            tmp_path,
        )
    commands = [args for args, _ in boundary.commands]
    assert any(args[:3] == ["docker", "container", "rm"] for args in commands)
    assert any(args[:3] == ["docker", "volume", "rm"] for args in commands)
    assert not any("prune" in args or "--force" in args for args in commands)


def _evidence(path):
    from core.application.release.images import all_release_roles

    namespaces = ("ghcr.io/flow-steward", "docker.io/flowsteward")
    path.write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "revision": REVISION,
                "failures": [],
                "registries": [
                    {
                        "namespace": namespace,
                        "status": "verified",
                        "images": [
                            {
                                "role": role.role,
                                "reference": f"{namespace}/{role.repository}:1.2.3",
                                "index_digest": INDEX,
                                "platforms": PLATFORMS,
                            }
                            for role in all_release_roles()
                        ],
                    }
                    for namespace in namespaces
                ],
            }
        )
    )
    return namespaces


@pytest.mark.parametrize("failed_registries", [1, 2])
def test_only_anonymously_accessible_complete_registry_can_publish(
    tmp_path, monkeypatch, failed_registries
):
    path = tmp_path / "evidence.json"
    namespaces = _evidence(path)
    calls = []

    def download(reference, expected, revision, directory):
        calls.append(reference)
        if len(calls) <= failed_registries:
            raise subprocess.CalledProcessError(1, ["docker", "pull"], stderr="denied")
        return {"status": "verified", "platforms": PLATFORMS}

    monkeypatch.setattr(acceptance, "check_registry", download)
    if failed_registries == 2:
        with pytest.raises(ValueError, match="no complete verified registry"):
            acceptance.accept_installers(
                path,
                version="1.2.3",
                revision=REVISION,
                namespaces=namespaces,
                artifact_dir=None,
                directory=tmp_path / "pulls",
            )
    else:
        acceptance.accept_installers(
            path,
            version="1.2.3",
            revision=REVISION,
            namespaces=namespaces,
            artifact_dir=None,
            directory=tmp_path / "pulls",
        )
    data = json.loads(path.read_text())
    assert data["registries"][0]["status"] == "incomplete"
    assert data["registries"][0]["authenticated_status"] == "verified"
    assert len(calls) == 2
    assert len(data["failures"]) == failed_registries


def test_identity_conflict_is_fatal_even_with_healthy_other_registry(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    namespaces = _evidence(path)

    def download(reference, expected, revision, directory):
        if reference.startswith(namespaces[0]):
            raise ValueError("wrong native config digest")
        return {"status": "verified"}

    monkeypatch.setattr(acceptance, "check_registry", download)
    with pytest.raises(ValueError, match="does not match release provenance"):
        acceptance.accept_installers(
            path,
            version="1.2.3",
            revision=REVISION,
            namespaces=namespaces,
            artifact_dir=None,
            directory=tmp_path / "pulls",
        )
    assert json.loads(path.read_text())["registries"][0]["status"] == "invalid"


def test_cleanup_failure_attempts_remaining_resources_and_stops_publication(tmp_path, monkeypatch):
    boundary = DockerBoundary()

    def docker(args, **kwargs):
        if args[:3] == ["docker", "container", "stop"]:
            boundary.commands.append((args, kwargs))
            raise subprocess.CalledProcessError(1, args, stderr="stop failed")
        return boundary(args, **kwargs)

    monkeypatch.setattr(acceptance.subprocess, "run", docker)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        acceptance.check_registry(
            "ghcr.io/flow-steward/flow-steward-installer:1.2.3",
            {"index_digest": INDEX, "platforms": PLATFORMS},
            REVISION,
            tmp_path,
        )
    assert any(args[:3] == ["docker", "container", "rm"] for args, _ in boundary.commands)
    assert any(args[:3] == ["docker", "volume", "rm"] for args, _ in boundary.commands)
    assert json.loads((tmp_path / "result.json").read_text())["cleanup_failures"]
