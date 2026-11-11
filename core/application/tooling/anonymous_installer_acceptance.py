"""Prove anonymous bootstrap downloads in disposable, empty Docker daemons."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from core.application.tooling.release_automation import require_complete_release
from core.application.tooling.resource_cleanup.models import (
    DISPOSABLE_LABEL,
    OWNER_LABEL,
    TOKEN_LABEL,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_PLATFORMS = ("linux/amd64", "linux/arm64")


def _run(args: list[str], *, log: Path, timeout: int = 120) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout.strip()


def check_registry(reference: str, expected: dict, revision: str, directory: Path) -> dict:
    """Pull both platforms without sharing publisher credentials or image stores."""
    directory.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    name = f"fs-anonymous-{identity}"
    volume = name + "-images"
    labels = [
        "--label",
        f"{OWNER_LABEL}=" + (_runtime_env.get("FS_TASK_RESOURCE_OWNER", "") or name),
        "--label",
        f"{TOKEN_LABEL}=" + (_runtime_env.get("FS_TASK_RESOURCE_TOKEN", "") or identity),
        "--label",
        f"{DISPOSABLE_LABEL}=true",
    ]
    created_volume = created_container = False
    report: dict = {"reference": reference, "daemon": name, "volume": volume, "platforms": {}}

    def inner(*args: str, log: str, timeout: int = 120) -> str:
        return _run(
            [
                "docker",
                "exec",
                "--env",
                "DOCKER_CONFIG=/acceptance/client",
                "--env",
                "PATH=/acceptance/bin",
                "--env",
                "DOCKER_HOST=unix:///var/run/docker.sock",
                name,
                "/usr/local/bin/docker",
                *args,
            ],
            log=directory / log,
            timeout=timeout,
        )

    try:
        _run(["docker", "volume", "create", *labels, volume], log=directory / "create-volume.log")
        created_volume = True
        # No host socket, credentials, source checkout or shared image store is mounted.
        _run(
            [
                "docker",
                "container",
                "create",
                "--privileged",
                "--name",
                name,
                *labels,
                "--env",
                "DOCKER_TLS_CERTDIR=",
                "--mount",
                f"type=volume,source={volume},target=/var/lib/docker",
                "docker:28-dind",
                "--host=unix:///var/run/docker.sock",
            ],
            log=directory / "create-daemon.log",
        )
        created_container = True
        _run(["docker", "container", "start", name], log=directory / "start-daemon.log")
        _run(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-ec",
                "mkdir -p /acceptance/client /acceptance/bin; "
                "ln -s /usr/local/bin/docker /acceptance/bin/docker; "
                "printf '%s\\n' '{\"auths\":{},\"credHelpers\":{}}' > /acceptance/client/config.json",
            ],
            log=directory / "initialize-client.log",
        )
        for attempt in range(60):
            try:
                api = inner(
                    "version", "--format", "{{.Server.APIVersion}}", log="daemon-api.log", timeout=5
                )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == 59:
                    raise
                time.sleep(1)
        if tuple(int(value) for value in api.split(".")) < (1, 49):
            raise ValueError("Anonymous platform inspection requires Docker API 1.49+")
        if inner("image", "ls", "--quiet", log="initial-image-store.log"):
            raise ValueError("Anonymous acceptance daemon must have an empty image store")
        manifests = json.loads(
            inner("manifest", "inspect", "--verbose", reference, log="manifest.json")
        )
        observed = {}
        for item in manifests:
            descriptor = item["Descriptor"]
            platform = descriptor.get("platform", {})
            key = platform.get("os", "") + "/" + platform.get("architecture", "")
            if key in _PLATFORMS:
                if key in observed:
                    raise ValueError("Duplicate installer platform in anonymous manifest")
                observed[key] = descriptor["digest"]
        if observed != {
            platform: expected["platforms"][platform]["manifest_digest"] for platform in _PLATFORMS
        }:
            raise ValueError(
                "Anonymous installer manifests differ from authenticated native evidence"
            )
        for platform in _PLATFORMS:
            arch = platform.split("/")[1]
            pull = inner(
                "pull", "--platform", platform, reference, log=f"pull-{arch}.log", timeout=600
            )
            if re.findall(r"^Digest: (sha256:[a-f0-9]{64})$", pull, re.M) != [
                expected["index_digest"]
            ]:
                raise ValueError(
                    "Anonymous pulled installer index differs from authenticated evidence"
                )
            image = json.loads(
                inner(
                    "image", "inspect", "--platform", platform, reference, log=f"image-{arch}.json"
                )
            )[0]
            labels_seen = image.get("Config", {}).get("Labels", {}) or {}
            if (
                image.get("Os") + "/" + image.get("Architecture") != platform
                or image.get("Id") != expected["platforms"][platform]["config_digest"]
                or labels_seen.get("org.opencontainers.image.role") != "installer"
                or labels_seen.get("org.opencontainers.image.revision") != revision
            ):
                raise ValueError(
                    "Anonymous installer image identity differs from authenticated native evidence"
                )
            report["platforms"][platform] = dict(expected["platforms"][platform])
        report.update(
            status="verified",
            index_digest=expected["index_digest"],
            empty_store=True,
            credential_free=True,
        )
        return report
    finally:
        # Exact task-owned objects only. Never prune or remove another daemon's images.
        commands = []
        if created_container:
            commands.extend(
                [
                    (["docker", "logs", name], "daemon.log"),
                    (["docker", "container", "stop", "--time", "10", name], "stop-daemon.log"),
                    (["docker", "container", "rm", name], "remove-daemon.log"),
                ]
            )
        if created_volume:
            commands.append((["docker", "volume", "rm", volume], "remove-volume.log"))
        cleanup_failures = []
        for command, log in commands:
            try:
                _run(command, log=directory / log)
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_failures.append(str(exc))
        report.setdefault("status", "failed")
        report["cleanup_failures"] = cleanup_failures
        (directory / "result.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        if cleanup_failures:
            raise RuntimeError(
                "Anonymous acceptance resource cleanup failed; inspect retained evidence"
            )


def accept_installers(
    path: Path,
    *,
    version: str,
    revision: str,
    namespaces: tuple[str, str],
    artifact_dir: Path | None,
    directory: Path,
) -> None:
    """Downgrade an inaccessible mirror; reject identity conflicts and no public set."""
    require_complete_release(
        path, version=version, revision=revision, namespaces=namespaces, artifact_dir=artifact_dir
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    baseline = next(
        image
        for registry in document["registries"]
        if registry["status"] == "verified"
        for image in registry["images"]
        if image["role"] == "installer"
    )
    invalid = False
    for registry in document["registries"]:
        namespace = registry["namespace"]
        expected = next(
            (image for image in registry["images"] if image["role"] == "installer"), baseline
        )
        reference = f"{namespace}/flow-steward-installer:{version}"
        registry["authenticated_status"] = registry["status"]
        try:
            registry["anonymous_installer"] = check_registry(
                reference, expected, revision, directory / namespace
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            registry["anonymous_installer"] = {
                "status": "unavailable",
                "reference": reference,
                "detail": str(exc),
            }
            registry["status"] = "incomplete"
            document["failures"].append(f"{namespace}: anonymous installer download unavailable")
        except (ValueError, KeyError, TypeError) as exc:
            registry["anonymous_installer"] = {
                "status": "invalid",
                "reference": reference,
                "detail": str(exc),
            }
            registry["status"] = "invalid"
            invalid = True
        finally:
            path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if invalid:
        raise ValueError("Anonymous installer identity does not match release provenance")
    require_complete_release(
        path, version=version, revision=revision, namespaces=namespaces, artifact_dir=artifact_dir
    )
