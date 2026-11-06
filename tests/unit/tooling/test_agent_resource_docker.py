from __future__ import annotations

from copy import deepcopy

import pytest

from core.application.tooling.resource_cleanup.models import (
    DISPOSABLE_LABEL,
    OWNER_LABEL,
    TOKEN_LABEL,
    Ledger,
    Resource,
)
from core.infrastructure.agent_resources.builders import PrivateBuilders
from core.infrastructure.agent_resources.docker import DockerResources

CID = "a" * 64
IID = "sha256:" + "b" * 64
TOKEN = "1" * 32
OWNER = "owner"
TAG = f"fs-owned/{TOKEN}:test"


@pytest.fixture
def ledger():
    return Ledger(OWNER, TOKEN, "/private/tmp/task", "2026-11-06T00:00:00Z")


def labels():
    return {OWNER_LABEL: OWNER, TOKEN_LABEL: TOKEN, DISPOSABLE_LABEL: "true"}


@pytest.fixture
def docker(monkeypatch):
    adapter = DockerResources()
    records = {
        "container": {
            "Id": CID,
            "State": {"Running": False},
            "Config": {"Labels": labels()},
            "Mounts": [],
        },
        "network": {"Id": CID, "Labels": labels(), "Containers": {}},
        "volume": {"Name": "fixture", "CreatedAt": "timestamp", "Labels": labels()},
        "image_tag": {
            "Id": IID,
            "RepoTags": [TAG, "foreign:keep"],
            "Config": {"Labels": labels()},
        },
    }
    records["container"]["Config"]["Env"] = [
        f"FS_RESOURCE_OWNER={OWNER}",
        f"FS_RESOURCE_TOKEN={TOKEN}",
    ]
    records["container"]["Mounts"] = [
        {"Type": "volume", "Name": "fixture", "Destination": "/var/lib/buildkit"}
    ]
    calls = []
    deleted = set()

    def inspect(kind, identity):
        return None if kind in deleted else deepcopy(records[kind])

    def command(*args):
        calls.append(args)
        if args[0] == "info":
            return "daemon-one"
        if args[:2] == ("container", "ls"):
            return ""
        if len(args) > 1 and args[1] == "rm":
            deleted.add({"image": "image_tag"}.get(args[0], args[0]))
        return ""

    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(adapter, "command", command)
    return adapter, records, calls


def test_foreign_label_cannot_be_registered(docker, ledger):
    adapter, records, _ = docker
    records["container"]["Config"]["Labels"][OWNER_LABEL] = "other-project"
    with pytest.raises(ValueError, match="ownership"):
        adapter.capture("container", CID, ledger)


def test_names_are_not_accepted_as_container_identity(docker, ledger):
    adapter, _, _ = docker
    with pytest.raises(ValueError, match="immutable"):
        adapter.capture("container", "fs-looks-owned", ledger)


def test_running_audit_is_preserved_even_after_release(docker, ledger):
    adapter, records, calls = docker
    proof = adapter.capture("container", CID, ledger)
    records["container"]["State"]["Running"] = True
    records["container"]["Config"]["Labels"]["com.docker.compose.service"] = "fs-audit"
    with pytest.raises(ValueError, match="active verification"):
        adapter.remove(ledger, Resource("c" * 32, "container", CID, proof, state="released"))
    assert not any("rm" in c or "stop" in c for c in calls)


def test_shared_image_untags_only_recorded_tag(docker, ledger):
    adapter, _, calls = docker
    proof = adapter.capture("image_tag", TAG, ledger)
    adapter.remove(ledger, Resource("c" * 32, "image_tag", TAG, proof, state="released"))
    assert ("image", "rm", TAG) in calls
    assert not any(IID in c or "--force" in c or "foreign:keep" in c for c in calls)


def test_retargeted_image_tag_is_preserved(docker, ledger):
    adapter, records, calls = docker
    proof = adapter.capture("image_tag", TAG, ledger)
    records["image_tag"]["Id"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="identity"):
        adapter.remove(ledger, Resource("c" * 32, "image_tag", TAG, proof, state="released"))
    assert not any("rm" in c for c in calls)


def test_protected_volume_is_never_owned(docker, ledger):
    with pytest.raises(ValueError, match="protected"):
        docker[0].capture("volume", "flow-steward-quality-cache", ledger)


def test_foreign_container_volume_mount_blocks_deletion(docker, ledger, monkeypatch):
    adapter, records, calls = docker
    proof = adapter.capture("volume", "fixture", ledger)
    original = adapter.command
    monkeypatch.setattr(
        adapter, "command", lambda *a: CID if a[:2] == ("container", "ls") else original(*a)
    )
    records["container"]["Mounts"] = [{"Name": "fixture"}]
    with pytest.raises(ValueError, match="consumer"):
        adapter.remove(ledger, Resource("c" * 32, "volume", "fixture", proof, state="released"))
    assert not any("rm" in c for c in calls)


def test_other_docker_daemon_blocks_deletion(docker, ledger, monkeypatch):
    adapter, _, calls = docker
    proof = adapter.capture("container", CID, ledger)
    original = adapter.command
    monkeypatch.setattr(
        adapter, "command", lambda *a: "daemon-two" if a[0] == "info" else original(*a)
    )
    with pytest.raises(ValueError, match="identity"):
        adapter.remove(ledger, Resource("c" * 32, "container", CID, proof, state="released"))
    assert not any("rm" in c for c in calls)


def test_shared_default_builder_is_never_eligible(docker, ledger):
    with pytest.raises(ValueError, match="task-created"):
        PrivateBuilders(docker[0]).capture(ledger, "default")


def test_private_builder_with_active_build_is_preserved(docker, ledger, monkeypatch):
    adapter, _, calls = docker
    builders = PrivateBuilders(adapter)
    name = builders.name(ledger)
    text = f'Name: {name}\nDriver: docker-container\nEndpoint: desktop-linux\nDriver Options: env.FS_RESOURCE_OWNER="{OWNER}" env.FS_RESOURCE_TOKEN="{TOKEN}"\nStatus: running\n'

    def command(*args):
        calls.append(args)
        if args[0] == "info":
            return "daemon-one"
        if args[:2] == ("container", "ls"):
            return ""
        if "ls" in args and "history" not in args:
            return '{"Name":"' + name + '"}'
        return '{"status":"Running"}' if "history" in args else text

    monkeypatch.setattr(adapter, "command", command)
    proof = builders.capture(ledger, name)
    with pytest.raises(ValueError, match="active build"):
        builders.remove(ledger, Resource("c" * 32, "builder", name, proof, state="released"))
    assert not any("rm" in c for c in calls)


def test_borrowed_protected_volume_can_be_recorded_but_not_owned(docker, ledger):
    adapter, _, _ = docker
    assert adapter.capture("volume", "flow-steward-quality-cache", ledger, borrowed=True)


def test_already_absent_private_builder_is_idempotent(docker, ledger, monkeypatch):
    adapter, _, calls = docker
    builders = PrivateBuilders(adapter)
    monkeypatch.setattr(adapter, "command", lambda *args: "")
    monkeypatch.setattr(adapter, "inspect", lambda *args: None)
    resource = Resource(
        "c" * 32,
        "builder",
        builders.name(ledger),
        {"container_id": CID, "volume_name": "fixture"},
        state="released",
    )
    assert not builders.validate(ledger, resource)
    builders.remove(ledger, resource)
    assert not any("rm" in c for c in calls)


def test_private_builder_creation_never_modifies_existing_builder(docker, ledger, monkeypatch):
    adapter, _, calls = docker
    builders = PrivateBuilders(adapter)
    monkeypatch.setattr(
        adapter, "command", lambda *args: '{"Name":"' + builders.name(ledger) + '"}'
    )
    with pytest.raises(ValueError, match="already exists"):
        builders.create(ledger)
    assert not any("create" in c for c in calls)


def test_audit_starting_during_cleanup_is_not_stopped(docker, ledger, monkeypatch):
    adapter, records, calls = docker
    records["container"]["Config"]["Labels"]["com.docker.compose.service"] = "fs-audit"
    proof = adapter.capture("container", CID, ledger)
    original = adapter.inspect
    inspections = 0

    def inspect(kind, identity):
        nonlocal inspections
        inspections += 1
        if inspections >= 3:
            records["container"]["State"]["Running"] = True
        return original(kind, identity)

    monkeypatch.setattr(adapter, "inspect", inspect)
    with pytest.raises(ValueError, match=r"verification|active"):
        adapter.remove(ledger, Resource("c" * 32, "container", CID, proof, state="released"))
    assert not any("stop" in c for c in calls)


def test_image_retargeted_while_consumers_are_scanned_is_not_untagged(docker, ledger, monkeypatch):
    adapter, records, calls = docker
    proof = adapter.capture("image_tag", TAG, ledger)
    original = adapter.command

    def command(*args):
        if args[:2] == ("container", "ls"):
            records["image_tag"]["Id"] = "sha256:" + "d" * 64
        return original(*args)

    monkeypatch.setattr(adapter, "command", command)
    with pytest.raises(ValueError, match="identity"):
        adapter.remove(ledger, Resource("c" * 32, "image_tag", TAG, proof, state="released"))
    assert not any("rm" in c for c in calls)


def test_builder_proof_includes_native_container_and_daemon(docker, ledger, monkeypatch):
    adapter, records, _ = docker
    builders = PrivateBuilders(adapter)
    name = builders.name(ledger)
    text = f'Name: {name}\nDriver: docker-container\nEndpoint: desktop-linux\nDriver Options: env.FS_RESOURCE_OWNER="{OWNER}" env.FS_RESOURCE_TOKEN="{TOKEN}"\nStatus: running\n'
    original_command = adapter.command
    monkeypatch.setattr(
        adapter,
        "command",
        lambda *a: text if a[:2] == ("buildx", "inspect") else original_command(*a),
    )
    records["container"]["Config"]["Env"] = [
        f"FS_RESOURCE_OWNER={OWNER}",
        f"FS_RESOURCE_TOKEN={TOKEN}",
    ]
    records["container"]["Mounts"] = [
        {"Type": "volume", "Name": "fixture", "Destination": "/var/lib/buildkit"}
    ]
    proof = builders.capture(ledger, name)
    assert proof["container_id"] == CID
    assert proof["daemon_id"] == "daemon-one"
    assert proof["volume_created"] == "timestamp"


def test_owned_image_tags_must_use_the_exclusive_task_namespace(docker, ledger):
    adapter, records, _ = docker
    records["image_tag"]["RepoTags"] = ["generic:latest"]
    with pytest.raises(ValueError, match="namespace"):
        adapter.capture("image_tag", "generic:latest", ledger)


def test_sidecar_supporting_an_active_audit_is_not_stopped(docker, ledger, monkeypatch):
    adapter, records, calls = docker
    records["container"]["State"]["Running"] = True
    records["container"]["Config"]["Cmd"] = ["postgres"]
    records["container"]["NetworkSettings"] = {
        "Networks": {"private": {"NetworkID": "private-network"}}
    }
    proof = adapter.capture("container", CID, ledger)
    audit_id = "e" * 64
    inspect = adapter.inspect
    command = adapter.command

    def peer(kind, identity):
        if kind == "container" and identity == audit_id:
            return {
                "Id": audit_id,
                "State": {"Running": True},
                "Config": {"Labels": {"com.docker.compose.service": "fs-audit"}},
                "NetworkSettings": {"Networks": {"private": {"NetworkID": "private-network"}}},
            }
        return inspect(kind, identity)

    def invoke(*args):
        if args[:2] == ("container", "ls"):
            return audit_id
        if args[0] == "top":
            return "postgres"
        return command(*args)

    monkeypatch.setattr(adapter, "inspect", peer)
    monkeypatch.setattr(adapter, "command", invoke)
    with pytest.raises(ValueError, match=r"active|verification"):
        adapter.remove(ledger, Resource("c" * 32, "container", CID, proof, state="released"))
    assert not any("stop" in c or "rm" in c for c in calls)


def test_missing_builder_configuration_does_not_hide_retained_cache(docker, ledger, monkeypatch):
    adapter, _, _ = docker
    builders = PrivateBuilders(adapter)
    monkeypatch.setattr(builders, "names", lambda: set())
    resource = Resource(
        "b" * 32,
        "builder",
        builders.name(ledger),
        {"container_id": CID, "volume_name": "fixture"},
        state="released",
    )
    with pytest.raises(ValueError, match=r"backing|cache"):
        builders.validate(ledger, resource)


def test_native_missing_network_response_is_idempotent(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, "[]", "Error response from daemon: network " + CID + " not found"
        ),
    )
    assert DockerResources().inspect("network", CID) is None


def test_immutable_image_id_is_accepted_only_as_borrowed(docker, ledger):
    adapter, _, _ = docker
    assert adapter.capture("image_tag", IID, ledger, borrowed=True)["native_id"] == IID
    with pytest.raises(ValueError, match="tag"):
        adapter.capture("image_tag", IID, ledger)
