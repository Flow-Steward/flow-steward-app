"""Installer fixture cleanup must not acquire ownership from daemon snapshots."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.support import disposable_installer as support

ROOT = Path(__file__).resolve().parents[3]
OWNER = "a" * 32
PROJECT = f"fs-inst-{OWNER}"
OWNED_TAG = f"fs-app-{OWNER}:acceptance"
FOREIGN_IMAGE = "sha256:" + "b" * 64
BORROWED_TAG = "registry.example/shared:current"
CACHE_ID = "c" * 25


class DockerState:
    """Stateful Docker boundary; cleanup decisions and manifest updates stay real."""

    def __init__(self) -> None:
        labels = {"com.docker.compose.project": PROJECT}
        self.resources = {
            "container": {
                "owned-container": {
                    "Config": {"Labels": labels},
                    "anonymous_volumes": ["owned-anonymous"],
                }
            },
            "network": {"owned-network": {"Labels": labels}},
            "volume": {
                "owned-volume": {"Labels": labels},
                "owned-anonymous": {"Labels": {"com.docker.volume.anonymous": ""}},
                "foreign-anonymous": {"Labels": {"com.docker.volume.anonymous": ""}},
            },
            "image": {
                OWNED_TAG: {"Id": "sha256:" + "a" * 64, "RepoTags": [OWNED_TAG]},
                FOREIGN_IMAGE: {"Id": FOREIGN_IMAGE, "RepoTags": []},
                BORROWED_TAG: {"Id": "sha256:" + "d" * 64, "RepoTags": [BORROWED_TAG]},
            },
        }
        self.cache = {CACHE_ID: {"ID": CACHE_ID, "Reclaimable": True, "Shared": False}}
        self.calls: list[list[str]] = []

    def run(self, command, **_kwargs):
        self.calls.append(command)
        kind, action = command[1:3]
        output, status = "", 0
        if kind == "buildx":
            if action == "du":
                output = "\n".join(json.dumps(value) for value in self.cache.values())
            elif action == "prune":
                self.cache.pop(command[-2].removeprefix("id="))
            else:
                pytest.fail(f"Unexpected Docker command: {command}")
        elif action == "rm":
            removed = self.resources[kind].pop(command[-1])
            if kind == "container" and "--volumes" in command:
                for name in removed["anonymous_volumes"]:
                    self.resources["volume"].pop(name)
        elif action == "inspect":
            value = self.resources[kind].get(command[-1])
            output, status = (json.dumps([value]), 0) if value is not None else ("", 1)
        elif action == "ls":
            selected = self.resources[kind]
            filters = [command[index + 1] for index, arg in enumerate(command) if arg == "--filter"]
            for expression in filters:
                if not expression.startswith("label="):
                    selected = {}
                    break
                name, _, value = expression.removeprefix("label=").partition("=")
                selected = {
                    key: item
                    for key, item in selected.items()
                    if name in (labels := item.get("Config", item).get("Labels", {}))
                    and (not value or labels[name] == value)
                }
            if kind == "image" and "--format" in command:
                output = "\n".join(
                    json.dumps(
                        {
                            "Repository": tag.rsplit(":", 1)[0],
                            "Tag": tag.rsplit(":", 1)[1],
                            "ID": item["Id"],
                        }
                    )
                    for item in selected.values()
                    for tag in item["RepoTags"]
                )
            else:
                output = "\n".join(
                    item["Id"] if kind == "image" else key for key, item in selected.items()
                )
        else:
            pytest.fail(f"Unexpected Docker command: {command}")
        return subprocess.CompletedProcess(command, status, output, "")


@pytest.fixture
def cleanup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docker = DockerState()
    owned = {
        "containers": ["owned-container"],
        "networks": ["owned-network"],
        "volumes": ["owned-volume"],
        "images": [OWNED_TAG],
        "paths": [],
    }
    manifest = {
        "owner_token": OWNER,
        "project_name": PROJECT,
        "planned_resources": owned,
        "active_resources": owned,
        "production_image_tags": {"app": OWNED_TAG},
        "observed_image_ids": {},
        "baseline_inventory": {
            kind: [] for kind in ("containers", "networks", "volumes", "images")
        },
        "baseline_image_tags": {},
        "baseline_build_cache": [],
        "dependency_images": [BORROWED_TAG],
        "observed_dependency_image_ids": {BORROWED_TAG: "sha256:" + "d" * 64},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(support, "_run", docker.run)
    return support.DisposableInstallerProject(manifest_path=path, source_root=ROOT), docker


def test_cleanup_preserves_concurrent_foreign_images_and_shared_build_cache(
    cleanup_project,
) -> None:
    project, docker = cleanup_project
    project.cleanup()
    assert FOREIGN_IMAGE in docker.resources["image"]
    assert CACHE_ID in docker.cache


def test_cleanup_preserves_unowned_volumes_and_borrowed_dependencies(cleanup_project) -> None:
    project, docker = cleanup_project
    project.cleanup()
    assert "foreign-anonymous" in docker.resources["volume"]
    assert BORROWED_TAG in docker.resources["image"]


def test_cleanup_still_removes_recorded_owned_resources(cleanup_project) -> None:
    project, docker = cleanup_project
    project.cleanup()
    assert "owned-container" not in docker.resources["container"]
    assert "owned-network" not in docker.resources["network"]
    assert "owned-volume" not in docker.resources["volume"]
    assert "owned-anonymous" not in docker.resources["volume"]
    assert OWNED_TAG not in docker.resources["image"]
    assert all(not values for values in project.cleanup_proof().values())


def test_create_does_not_enumerate_shared_build_cache(tmp_path: Path, monkeypatch) -> None:
    docker = DockerState()
    monkeypatch.setattr(support, "_run", docker.run)
    monkeypatch.setattr(support, "_git_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(
        support,
        "_reserve_unique_ports",
        lambda keys: {key: 20000 + i for i, key in enumerate(keys)},
    )
    support.DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)
    assert not any(command[:3] == ["docker", "buildx", "du"] for command in docker.calls)
