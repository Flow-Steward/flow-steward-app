from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass, CloneEntry, CloneManifest
from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.clone.archive_gateway import (
    CloneArchiveGateway,
    TrustedPublicationMetadata,
)
from core.infrastructure.clone.installation_gateway import (
    DockerCloneVolumeTransfer,
)
from core.infrastructure.clone.installation_restore_effects import InstalledCloneRestoreEffects
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CANDIDATE_CORE = Path("/app/core")
_PASSPHRASE = b"named volume round trip"


def _release() -> ReleaseIdentity:
    commit = "a" * 40
    registry = "ghcr.io/example"
    platform = "linux/arm64"
    digest = "sha256:" + "b" * 64
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=commit,
        registry=registry,
        profile="compact",
        platform=platform,
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"{registry}/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=commit,
                platform=platform,
            )
            for role in required_product_roles("compact")
        ),
    )


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _tool_image() -> str:
    image = os.environ.get("FS_TEST_AUDIT_IMAGE_ID", "")
    if _IMAGE_ID.fullmatch(image) is None:
        raise RuntimeError(
            "FS_TEST_AUDIT_IMAGE_ID requires a full immutable ID from canonical lint.sh"
        )
    try:
        observed = _run("docker", "image", "inspect", "--format", "{{.Id}}", image)
    except subprocess.CalledProcessError as error:
        raise RuntimeError("FS_TEST_AUDIT_IMAGE_ID is unavailable locally") from error
    if observed.stdout.strip() != image:
        raise RuntimeError("FS_TEST_AUDIT_IMAGE_ID does not match the local immutable image")
    return image


def _cleanup_owned(*resources: tuple[str, str]) -> None:
    failures = []
    for kind, identity in resources:
        prefix = ("docker",) if kind == "container" else ("docker", kind)
        removed = _run(*prefix, "rm", "--force", identity, check=False)
        absent = _run(*prefix, "inspect", identity, check=False)
        print(
            json.dumps(
                {
                    "cleanup_kind": kind,
                    "identity": identity,
                    "remove_exit": removed.returncode,
                    "remove_stderr": removed.stderr,
                    "absence_exit": absent.returncode,
                    "absence_stderr": absent.stderr,
                }
            )
        )
        removed_missing = any(word in removed.stderr.lower() for word in ("no such", "not found"))
        absent_missing = any(word in absent.stderr.lower() for word in ("no such", "not found"))
        if (removed.returncode and not removed_missing) or not (
            absent.returncode and absent_missing
        ):
            failures.append(f"{kind} {identity}")
    if failures:
        raise RuntimeError("Owned Docker cleanup failed: " + ", ".join(failures))


def _core_manifest(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted((root, *root.rglob("*"))):
        info = path.lstat()
        row: dict[str, object] = {
            "path": str(path.relative_to(root)),
            "mode": stat.S_IMODE(info.st_mode),
        }
        if path.is_symlink():
            target = os.readlink(path)
            data = os.fsencode(target)
            row.update(
                kind="symlink",
                target=target,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        elif path.is_file():
            data = path.read_bytes()
            row.update(kind="file", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        elif path.is_dir():
            row["kind"] = "directory"
        else:
            raise RuntimeError(f"Unsupported candidate core path: {path}")
        rows.append(row)
    return rows


def _volume(name: str, project: str, *, volume_key: str = "fs-rabbitmq") -> None:
    _tool_image()
    _run(
        "docker",
        "volume",
        "create",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"com.docker.compose.volume={volume_key}",
        name,
    )


def _populate(volume: str, script: str) -> None:
    _run(
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "-v",
        f"{volume}:/volume",
        "--entrypoint",
        "sh",
        _tool_image(),
        "-c",
        script,
    )


@pytest.fixture(scope="module")
def clone_tool_image() -> Iterator[str]:
    base_image = _tool_image()
    expected_core = _core_manifest(_CANDIDATE_CORE)
    token = uuid.uuid4().hex[:12]
    image = f"flow-steward-clone-tool-test:{token}"
    existing = _run("docker", "image", "inspect", image, check=False)
    if existing.returncode == 0 or "No such image" not in existing.stderr:
        raise RuntimeError("Disposable clone image tag must be absent before construction")
    container = _run(
        "docker",
        "create",
        "--pull=never",
        "--user",
        "0:0",
        "--label",
        f"com.flow-steward.test-owner={image}",
        "--entrypoint",
        "sh",
        base_image,
        "-c",
        "rm -rf /app/core && mkdir -p /app/core",
    ).stdout.strip()
    image_attempted = False
    try:
        try:
            _run("docker", "start", "--attach", container)
            _run("docker", "cp", str(_CANDIDATE_CORE) + "/.", f"{container}:/app/core")
            image_attempted = True
            committed = _run("docker", "commit", container, image).stdout.strip()
            if _IMAGE_ID.fullmatch(committed) is None:
                raise RuntimeError("Docker commit did not return a full immutable clone image ID")
            actual_id = _run("docker", "image", "inspect", "--format", "{{.Id}}", committed)
            if actual_id.stdout.strip() != committed:
                raise RuntimeError("Committed clone image is unavailable or has the wrong identity")
            script = (
                "import hashlib, json, os, stat\nfrom pathlib import Path\n"
                + inspect.getsource(_core_manifest)
                + '\nprint(json.dumps(_core_manifest(Path("/app/core"))))'
            )
            observed = _run(
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--label",
                f"com.flow-steward.test-owner={image}",
                "--entrypoint",
                "python",
                committed,
                "-c",
                script,
            )
            if (
                json.loads(observed.stdout) != expected_core
                or _core_manifest(_CANDIDATE_CORE) != expected_core
            ):
                raise RuntimeError(
                    "Derived clone image core differs from the proven candidate core"
                )
            print(
                json.dumps(
                    {
                        "clone_image_tag": image,
                        "base_image_id": base_image,
                        "clone_image_id": committed,
                        "container_id": container,
                        "core_rows": len(expected_core),
                        "core_manifest_sha256": hashlib.sha256(
                            json.dumps(expected_core, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
            )
        finally:
            _cleanup_owned(("container", container))
        yield committed
    finally:
        if image_attempted:
            _cleanup_owned(("image", image))


@pytest.fixture(scope="module")
def clone_test_project() -> Iterator[str]:
    project = f"clone-volume-source-{uuid.uuid4().hex[:12]}"
    compact_volume = f"{project}_fs-compact-data"
    _volume(compact_volume, project, volume_key="fs-compact-data")
    try:
        yield project
    finally:
        _cleanup_owned(("volume", compact_volume))


def test_compact_role_owned_state_is_not_readable_by_host_cli_identity(
    clone_test_project: str,
) -> None:
    volume = f"{clone_test_project}_fs-compact-data"
    _populate(
        volume,
        " && ".join(
            (
                "mkdir -p /volume/state/.flow-steward",
                "printf synthetic-state > /volume/state/.flow-steward/evidence.txt",
                "chown 10007:10102 /volume/state /volume/state/.flow-steward /volume/state/.flow-steward/evidence.txt",
                "chmod 0750 /volume/state /volume/state/.flow-steward",
                "chmod 0640 /volume/state/.flow-steward/evidence.txt",
            )
        ),
    )
    command = (
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "1001:1002",
        "--group-add",
        "1003",
        "-v",
        f"{volume}:/source:ro",
        "--entrypoint",
        "sh",
        _tool_image(),
        "-c",
        "cat /source/state/.flow-steward/evidence.txt",
    )
    denied = _run(*command, check=False)
    assert denied.returncode != 0
    assert "Permission denied" in denied.stderr
    role_command = list(command)
    role_command[role_command.index("--user") + 1] = "10007:10102"
    readable = _run(*role_command)
    assert readable.stdout == "synthetic-state"


def test_clone_volume_round_trip_preserves_service_ownership_and_modes(
    clone_tool_image: str,
    clone_test_project: str,
    tmp_path: Path,
) -> None:
    token = uuid.uuid4().hex[:12]
    source_project = clone_test_project
    target_project = f"clone-volume-target-{token}"
    source_volume = f"{source_project}-rabbitmq"
    target_volume = f"{target_project}-rabbitmq"
    service_uid = 12345
    service_gid = 12346
    child_uid = 12347
    child_gid = 12348
    payload = b"durable-volume-state"
    staging_parent = tmp_path / f"volume-test-{token}"
    captured = staging_parent / "rabbitmq"
    try:
        staging_parent.mkdir(parents=True)
        _volume(source_volume, source_project)
        _volume(target_volume, target_project)
        _populate(
            source_volume,
            " && ".join(
                (
                    "printf durable-volume-state > /volume/state.bin",
                    f"chown {child_uid}:{child_gid} /volume/state.bin",
                    "chmod 0640 /volume/state.bin",
                    f"chown {service_uid}:{service_gid} /volume",
                    "chmod 0710 /volume",
                )
            ),
        )
        _populate(
            target_volume,
            " && ".join(
                (
                    "printf stale > /volume/stale.bin",
                    f"chown {service_uid}:{service_gid} /volume",
                    "chmod 0750 /volume",
                )
            ),
        )

        entries = DockerCloneVolumeTransfer(
            project_name=source_project,
            tool_image=clone_tool_image,
        ).capture("rabbitmq", captured)

        assert (entries[0].uid, entries[0].gid, entries[0].mode) == (
            service_uid,
            service_gid,
            0o710,
        )
        assert (captured.stat().st_uid, captured.stat().st_gid) == (
            staging_parent.stat().st_uid,
            staging_parent.stat().st_gid,
        )
        state = captured / "state.bin"
        assert state.read_bytes() == payload
        assert (entries[1].uid, entries[1].gid, entries[1].mode) == (
            child_uid,
            child_gid,
            0o640,
        )
        target = DockerCloneVolumeTransfer(
            project_name=target_project,
            tool_image=clone_tool_image,
        )
        target.replace("rabbitmq", captured, entries)

        observed = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "-v",
            f"{target_volume}:/volume:ro",
            "--entrypoint",
            "sh",
            _tool_image(),
            "-c",
            "stat -c '%u:%g:%a' /volume /volume/state.bin; cat /volume/state.bin; "
            "test ! -e /volume/stale.bin",
        ).stdout.splitlines()
        assert observed == [
            f"{service_uid}:{service_gid}:710",
            f"{child_uid}:{child_gid}:640",
            payload.decode(),
        ]
        assert target.matches("rabbitmq", captured, entries) is True
    finally:
        _cleanup_owned(("volume", source_volume), ("volume", target_volume))


def test_compact_create_capture_reads_service_owned_trees_as_non_root(
    clone_tool_image: str,
    clone_test_project: str,
) -> None:
    source_project = clone_test_project
    volume = f"{source_project}_fs-compact-data"
    client_uid = 11001
    client_gid = 11002
    staging_volume = f"{source_project}-nonroot-staging"
    _run("docker", "volume", "create", staging_volume)
    _populate(
        volume,
        " && ".join(
            (
                "rm -rf /volume/artifacts /volume/extensions /volume/state",
                "mkdir -p /volume/artifacts/node_modules",
                "mkdir -p /volume/extensions/.git /volume/state/.flow-steward/clone",
                "mkdir -p /volume/state/runtime /volume/state/product",
                "printf artifact > /volume/artifacts/item.bin",
                "printf generated > /volume/artifacts/node_modules/generated.bin",
                "printf extension > /volume/extensions/extension.bin",
                "printf excluded-git > /volume/extensions/.git/HEAD",
                "printf retained > /volume/state/.flow-steward/clone/journal.json",
                "printf runtime > /volume/state/runtime/cache.bin",
                "printf durable > /volume/state/product/state.bin",
                "chown -R 10007:10100 /volume/artifacts",
                "chown -R 10005:10103 /volume/extensions",
                "chown -R 10007:10102 /volume/state",
                "chmod 2770 /volume/artifacts",
                "chmod 2775 /volume/extensions",
                "chmod 0750 /volume/state",
                "find /volume/artifacts /volume/state -type d -mindepth 1 -exec chmod 0750 {} +",
                "find /volume/extensions -type d -mindepth 1 -exec chmod 0755 {} +",
                "find /volume -type f -exec chmod 0640 {} +",
            )
        ),
    )
    _populate(
        staging_volume,
        f"chown {client_uid}:{client_gid} /volume && chmod 0700 /volume",
    )
    client_program = """
import json
from pathlib import Path
from core.application.clone.contracts import CloneDataClass
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer

root = Path('/staging/source')
transfer = DockerCloneVolumeTransfer(
    project_name=__PROJECT_NAME__,
    tool_image=__TOOL_IMAGE__,
)
items = (
    (CloneDataClass.LOCAL_ARTIFACTS, 'artifacts'),
    (CloneDataClass.EXTENSIONS, 'extensions'),
    (CloneDataClass.DURABLE_STATE, 'state'),
)
transfer.validate_compact_filesystems(items)
entries = transfer.capture_compact_filesystems(root, items)
paths = (root, *root.rglob('*'))
print(json.dumps({
    'entries': [entry.model_dump(mode='json') for entry in entries],
    'owners': sorted({path.lstat().st_uid for path in paths}),
    'artifact': (root / 'artifacts/item.bin').read_text(),
    'extension': (root / 'extensions/extension.bin').read_text(),
    'state': (root / 'state/product/state.bin').read_text(),
    'excluded': all(not path.exists() for path in (
        root / 'artifacts/node_modules',
        root / 'extensions/.git',
        root / 'state/.flow-steward/clone',
        root / 'state/runtime',
    )),
}, sort_keys=True))
"""
    client_program = client_program.replace("__PROJECT_NAME__", repr(source_project)).replace(
        "__TOOL_IMAGE__", repr(clone_tool_image)
    )
    socket_gid = Path("/var/run/docker.sock").stat().st_gid
    try:
        outcome = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            f"{client_uid}:{client_gid}",
            "--group-add",
            str(socket_gid),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{staging_volume}:/staging",
            "-w",
            "/app",
            "--entrypoint",
            "python",
            clone_tool_image,
            "-c",
            client_program,
        )
        observed = json.loads(outcome.stdout)
        entries = tuple(CloneEntry.model_validate(item) for item in observed["entries"])
        roots = {entry.path: entry for entry in entries if "/" not in entry.path}
        assert observed["owners"] == [client_uid]
        assert observed["artifact"] == "artifact"
        assert observed["extension"] == "extension"
        assert observed["state"] == "durable"
        assert observed["excluded"] is True
        assert (roots["artifacts"].uid, roots["artifacts"].gid) == (10007, 10100)
        assert (roots["extensions"].uid, roots["extensions"].gid) == (10005, 10103)
        assert (roots["state"].uid, roots["state"].gid) == (10007, 10102)
    finally:
        _cleanup_owned(("volume", staging_volume))


@pytest.mark.parametrize("component", ["state", ".flow-steward"])
def test_mixed_compact_transfer_rejects_original_symlinks_and_removes_helpers(
    clone_tool_image: str, tmp_path: Path, component: str
) -> None:
    project = f"clone-mixed-symlink-{uuid.uuid4().hex[:12]}"
    volume = f"{project}-data"
    _volume(volume, project, volume_key="fs-compact-data")
    try:
        if component == "state":
            script = "mkdir -p /volume/other/.flow-steward && ln -s other /volume/state"
        else:
            script = (
                "mkdir -p /volume/state /volume/runtime-secrets "
                "&& printf excluded > /volume/runtime-secrets/secret "
                "&& ln -s ../runtime-secrets /volume/state/.flow-steward"
            )
        _populate(volume, script)
        transfer = DockerCloneVolumeTransfer(project_name=project, tool_image=clone_tool_image)
        destination = tmp_path / "captured"
        with pytest.raises(RuntimeError, match="Docker operation failed"):
            transfer.capture_compact_filesystems(
                destination, ((CloneDataClass.DURABLE_STATE, "state"),), durable_state_only=True
            )
        assert not destination.exists()
        assert not _run("docker", "ps", "-aq", "--filter", f"volume={volume}").stdout.strip()
    finally:
        _cleanup_owned(("volume", volume))


def test_mixed_compact_state_encrypted_round_trip_retains_target_shell(
    clone_tool_image: str,
    tmp_path: Path,
) -> None:
    token = uuid.uuid4().hex[:12]
    source_project = f"clone-mixed-source-{token}"
    target_project = f"clone-mixed-target-{token}"
    source_volume = f"{source_project}-data"
    target_volume = f"{target_project}-data"
    host_extensions_volume = f"{target_project}-host-extensions"
    _volume(source_volume, source_project, volume_key="fs-compact-data")
    try:
        _volume(target_volume, target_project, volume_key="fs-compact-data")
        _volume(host_extensions_volume, target_project, volume_key="test-host-extensions")
        _populate(
            source_volume,
            "mkdir -p /volume/state/.flow-steward /volume/state/runtime-secrets "
            "&& printf synthetic-product > /volume/state/.flow-steward/product.json "
            "&& printf source-shell > /volume/state/.flow-steward/stack.toml "
            "&& printf excluded > /volume/state/bootstrap.json "
            "&& printf excluded > /volume/state/runtime-secrets/secret "
            "&& chown -R 10007:10102 /volume/state "
            "&& chmod 0750 /volume/state /volume/state/.flow-steward "
            "&& chmod 0640 /volume/state/.flow-steward/product.json",
        )
        _populate(
            target_volume,
            "mkdir -p /volume/state/.flow-steward/clone /volume/extensions "
            "&& printf target-shell > /volume/state/.flow-steward/stack.toml "
            "&& printf target-operation > /volume/state/.flow-steward/clone/operation "
            "&& printf obsolete > /volume/state/.flow-steward/obsolete "
            "&& printf target-bootstrap > /volume/state/bootstrap.json "
            "&& printf host-tree > /volume/extensions/marker "
            "&& chown -R 10007:10102 /volume/state "
            "&& chmod 0750 /volume/state /volume/state/.flow-steward",
        )
        source = DockerCloneVolumeTransfer(project_name=source_project, tool_image=clone_tool_image)
        requests = ((CloneDataClass.DURABLE_STATE, "state"),)
        source.validate_compact_filesystems(requests, durable_state_only=True)
        staging = tmp_path / "source"
        entries = source.capture_compact_filesystems(staging, requests, durable_state_only=True)
        assert [entry.path for entry in entries] == ["state", "state/product.json"]
        assert all((entry.uid, entry.gid) == (10007, 10102) for entry in entries)
        state_entries = entries
        host_extensions = staging / "extensions"
        host_extensions.mkdir()
        (host_extensions / "product-extension").write_text("source-host-tree")
        entries = tuple(
            sorted(
                (
                    *entries,
                    *inventory_volume_metadata(
                        host_extensions,
                        archive_root="extensions",
                        data_class=CloneDataClass.EXTENSIONS,
                    ),
                ),
                key=lambda entry: entry.path,
            )
        )
        manifest = CloneManifest(
            created_at=datetime(2026, 10, 5, tzinfo=UTC),
            release=_release(),
            deployment_profile="compact",
            data_placement="host_directories",
            file_storage="local_fs",
            source_installation_id=source_project,
            entries=entries,
            entry_count=len(entries),
            aggregate_size=sum(entry.size for entry in entries),
        )
        archive = CloneArchiveGateway()
        payload = BytesIO()
        archive.write_archive(
            staging,
            manifest,
            payload,
            passphrase_source=lambda: _PASSPHRASE,
            trusted_publication_metadata=TrustedPublicationMetadata(entries=state_entries),
        )
        payload.seek(0)
        verified_parent = tmp_path / "verified"
        verified_parent.mkdir()
        verified = archive.stage_verified(
            payload, staging_parent=verified_parent, passphrase_source=lambda: _PASSPHRASE
        )
        try:
            target = DockerCloneVolumeTransfer(
                project_name=target_project, tool_image=clone_tool_image
            )
            assert target.publish_compact_data(
                verified.root, state_entries, plan_digest="a" * 64, durable_state_only=True
            )
            assert target.publish_compact_data(
                verified.root,
                state_entries,
                plan_digest="a" * 64,
                verify=True,
                durable_state_only=True,
            )
            _populate(host_extensions_volume, "printf target-host-tree > /volume/target-extension")
            # The publication bind is resolved by Docker, not by this caller's tmpfs.
            target_extensions = Path(
                _run(
                    "docker",
                    "volume",
                    "inspect",
                    "--format",
                    "{{.Mountpoint}}",
                    host_extensions_volume,
                ).stdout.strip()
            )
            assert target_extensions.is_absolute()

            def inspect_host_extensions(script: str) -> str:
                return _run(
                    "docker",
                    "run",
                    "--pull=never",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "0:0",
                    "-v",
                    f"{host_extensions_volume}:/volume:ro",
                    "--entrypoint",
                    "sh",
                    clone_tool_image,
                    "-c",
                    script,
                ).stdout

            config = DeploymentConfig(
                deployment_profile="compact",
                environment="production",
                data_placement="host_directories",
                file_storage="local_fs",
            )
            shell = CloneTargetShell.from_config(
                installation_id=target_project,
                compose_project=target_project,
                config=config,
                host_uid=os.getuid(),
                host_gid=os.getgid(),
                filesystem_roots={
                    "durable_state": "volume:fs-compact-data/state/.flow-steward",
                    "extensions": str(target_extensions),
                },
                postgres_roles=("synthetic",),
                postgres_identity_fingerprint="f" * 64,
            )
            plan = CloneReplacementPlan.build(
                source_installation_id=source_project,
                source_release_version="1.2.3",
                source_release=manifest.release,
                archive_sha256=hashlib.sha256(payload.getvalue()).hexdigest(),
                target_shell=shell,
                target_platform="linux/arm64",
                replaced_data_classes=("durable_state", "extensions"),
                external_effect_categories=(),
            )
            prepared = PreparedCloneRestore(
                archive_path=str(tmp_path / "synthetic.fsclone"),
                operation_id="mixed-restore",
                staged_root=str(verified.root),
                manifest=manifest,
                source_configuration=config,
                exact_release=manifest.release,
                plan=plan,
            )
            effects = InstalledCloneRestoreEffects(
                operation_root=tmp_path / "operations",
                services=None,
                service_names=(),
                volumes=target,
                target_roots={
                    CloneDataClass.DURABLE_STATE: Path("/unavailable-role-owned-state"),
                    CloneDataClass.EXTENSIONS: target_extensions,
                },
                backup=None,
                import_secrets=lambda _value: None,
                regenerate_configuration=lambda _value: None,
                activate_release=lambda _value: None,
                verify_database=lambda _value: True,
                secrets_match=lambda _value: True,
                configuration_matches=lambda _value: True,
                release_activated=lambda _value: True,
                verify_target=lambda _value: None,
            )
            _populate(
                target_volume,
                "mv /volume/state/.flow-steward /volume/state/valid-state "
                "&& ln -s valid-state /volume/state/.flow-steward",
            )
            with pytest.raises(RuntimeError, match="Docker operation failed"):
                effects.replace_filesystems(prepared)
            assert (
                inspect_host_extensions(
                    "test ! -e /volume/product-extension && cat /volume/target-extension"
                )
                == "target-host-tree"
            )
            _populate(
                target_volume,
                "unlink /volume/state/.flow-steward "
                "&& mv /volume/state/valid-state /volume/state/.flow-steward",
            )
            effects.replace_filesystems(prepared)
            assert effects.filesystems_replaced(prepared)
            assert (
                inspect_host_extensions(
                    "test ! -e /volume/target-extension && cat /volume/product-extension"
                )
                == "source-host-tree"
            )
        finally:
            verified.cleanup()
        observed = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "-v",
            f"{target_volume}:/volume:ro",
            "--entrypoint",
            "sh",
            clone_tool_image,
            "-c",
            "test ! -e /volume/state/.flow-steward/obsolete "
            "&& cat /volume/state/.flow-steward/product.json "
            "/volume/state/.flow-steward/stack.toml "
            "/volume/state/.flow-steward/clone/operation /volume/state/bootstrap.json "
            "/volume/extensions/marker "
            "&& stat -c '%u:%g:%a' /volume/state/.flow-steward/product.json",
        )
        assert observed.stdout == (
            "synthetic-producttarget-shelltarget-operationtarget-bootstraphost-tree10007:10102:640\n"
        )
    finally:
        _cleanup_owned(
            ("volume", host_extensions_volume), ("volume", target_volume), ("volume", source_volume)
        )


def test_named_volume_capture_assembles_and_verifies_with_publication_metadata(
    clone_tool_image: str,
    clone_test_project: str,
    tmp_path: Path,
) -> None:
    source_project = clone_test_project
    rabbitmq_volume = f"{source_project}-archive-rabbitmq"
    compact_volume = f"{source_project}_fs-compact-data"
    staging = tmp_path / "source"
    _volume(rabbitmq_volume, source_project)
    try:
        _populate(
            rabbitmq_volume,
            "printf broker > /volume/state.bin && chown -R 12345:12346 /volume "
            "&& chmod 0710 /volume && chmod 0640 /volume/state.bin",
        )
        _populate(
            compact_volume,
            "rm -rf /volume/artifacts /volume/extensions /volume/state "
            "&& mkdir -p /volume/artifacts /volume/extensions /volume/state/product "
            "&& printf artifact > /volume/artifacts/item.bin "
            "&& printf durable > /volume/state/product/item.bin "
            "&& chown -R 10007:10100 /volume/artifacts "
            "&& chown -R 10005:10103 /volume/extensions "
            "&& chown -R 10007:10102 /volume/state "
            "&& chmod 2770 /volume/artifacts "
            "&& chmod 2775 /volume/extensions && chmod 0750 /volume/state /volume/state/product "
            "&& chmod 0640 /volume/artifacts/item.bin /volume/state/product/item.bin",
        )
        transfer = DockerCloneVolumeTransfer(
            project_name=source_project,
            tool_image=clone_tool_image,
        )
        compact_entries = transfer.capture_compact_filesystems(
            staging,
            (
                (CloneDataClass.LOCAL_ARTIFACTS, "artifacts"),
                (CloneDataClass.EXTENSIONS, "extensions"),
                (CloneDataClass.DURABLE_STATE, "state"),
            ),
        )
        rabbitmq_entries = transfer.capture("rabbitmq", staging / "rabbitmq")
        entries = tuple(sorted((*compact_entries, *rabbitmq_entries), key=lambda item: item.path))
        manifest = CloneManifest(
            created_at=datetime(2026, 9, 26, tzinfo=UTC),
            release=_release(),
            deployment_profile="compact",
            data_placement="compact_volume",
            file_storage="local_fs",
            source_installation_id="source-installation",
            entries=entries,
            entry_count=len(entries),
            aggregate_size=sum(entry.size for entry in entries),
        )
        trusted = TrustedPublicationMetadata(entries=entries)
        archive = CloneArchiveGateway()
        payload = BytesIO()

        archive.write_archive(
            staging,
            manifest,
            payload,
            passphrase_source=lambda: _PASSPHRASE,
            trusted_publication_metadata=trusted,
        )
        payload.seek(0)
        verified_parent = tmp_path / "verified"
        verified_parent.mkdir()
        verified = archive.stage_verified(
            payload,
            staging_parent=verified_parent,
            passphrase_source=lambda: _PASSPHRASE,
        )
        try:
            assert (verified.root / "rabbitmq/state.bin").read_bytes() == b"broker"
            assert (verified.root / "artifacts/item.bin").read_bytes() == b"artifact"
            assert (verified.root / "state/product/item.bin").read_bytes() == b"durable"
        finally:
            verified.cleanup()

        (staging / "artifacts/item.bin").write_bytes(b"tampered")
        destination = BytesIO()
        with pytest.raises(ValueError, match=r"size changed|checksum changed"):
            archive.write_archive(
                staging,
                manifest,
                destination,
                passphrase_source=lambda: _PASSPHRASE,
                trusted_publication_metadata=trusted,
            )
    finally:
        _cleanup_owned(("volume", rabbitmq_volume))


def test_compact_clone_publication_preserves_role_ownership_and_target_shell(
    clone_tool_image: str,
) -> None:
    token = uuid.uuid4().hex[:12]
    project = f"clone-compact-target-{token}"
    volume = f"{project}-data"
    installer_state_volume = f"{project}-installer-state"
    digest = "c" * 64
    client_uid = 11001
    client_gid = 11002
    try:
        _volume(volume, project, volume_key="fs-compact-data")
        _run("docker", "volume", "create", installer_state_volume)
        _populate(
            volume,
            " && ".join(
                (
                    "mkdir -p /volume/artifacts /volume/extensions",
                    "mkdir -p /volume/state/.flow-steward",
                    "printf old > /volume/artifacts/old.bin",
                    "printf target-shell > /volume/state/.flow-steward/stack.toml",
                    "chown 10007:10100 /volume/artifacts",
                    "chmod 2770 /volume/artifacts",
                    "chown 10005:10103 /volume/extensions",
                    "chmod 2775 /volume/extensions",
                    "chown 10007:10102 /volume/state /volume/state/.flow-steward",
                    "chmod 0750 /volume/state /volume/state/.flow-steward",
                )
            ),
        )
        payloads = {
            "artifacts/item.bin": b"source-artifact",
            "extensions/item.bin": b"source-extension",
            "state/.flow-steward/source-owned.json": b"source-state",
        }
        _populate(
            installer_state_volume,
            " && ".join(
                (
                    "mkdir -p /volume/clone/restore-staging/operation/artifacts",
                    "mkdir -p /volume/clone/restore-staging/operation/extensions",
                    "mkdir -p /volume/clone/restore-staging/operation/state/.flow-steward",
                    "printf source-artifact > /volume/clone/restore-staging/operation/artifacts/item.bin",
                    "printf source-extension > /volume/clone/restore-staging/operation/extensions/item.bin",
                    "printf source-state > /volume/clone/restore-staging/operation/state/.flow-steward/source-owned.json",
                    f"chown -R {client_uid}:{client_gid} /volume",
                    "chmod -R u+rwX,go-rwx /volume",
                )
            ),
        )
        entries = (
            CloneEntry(
                data_class=CloneDataClass.LOCAL_ARTIFACTS,
                path="artifacts",
                entry_type="directory",
                size=0,
                mode=0o2770,
                uid=10007,
                gid=10100,
            ),
            CloneEntry(
                data_class=CloneDataClass.LOCAL_ARTIFACTS,
                path="artifacts/item.bin",
                entry_type="file",
                size=len(payloads["artifacts/item.bin"]),
                mode=0o640,
                uid=10007,
                gid=10100,
                sha256=hashlib.sha256(payloads["artifacts/item.bin"]).hexdigest(),
            ),
            CloneEntry(
                data_class=CloneDataClass.EXTENSIONS,
                path="extensions",
                entry_type="directory",
                size=0,
                mode=0o2775,
                uid=10005,
                gid=10103,
            ),
            CloneEntry(
                data_class=CloneDataClass.EXTENSIONS,
                path="extensions/item.bin",
                entry_type="file",
                size=len(payloads["extensions/item.bin"]),
                mode=0o644,
                uid=10005,
                gid=10103,
                sha256=hashlib.sha256(payloads["extensions/item.bin"]).hexdigest(),
            ),
            CloneEntry(
                data_class=CloneDataClass.DURABLE_STATE,
                path="state",
                entry_type="directory",
                size=0,
                mode=0o750,
                uid=10007,
                gid=10102,
            ),
            CloneEntry(
                data_class=CloneDataClass.DURABLE_STATE,
                path="state/.flow-steward",
                entry_type="directory",
                size=0,
                mode=0o750,
                uid=10007,
                gid=10102,
            ),
            CloneEntry(
                data_class=CloneDataClass.DURABLE_STATE,
                path="state/.flow-steward/source-owned.json",
                entry_type="file",
                size=len(payloads["state/.flow-steward/source-owned.json"]),
                mode=0o640,
                uid=10007,
                gid=10102,
                sha256=hashlib.sha256(
                    payloads["state/.flow-steward/source-owned.json"]
                ).hexdigest(),
            ),
        )
        client_program = """
import json
from pathlib import Path
from core.application.clone.contracts import CloneEntry
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer

entries = tuple(CloneEntry.model_validate(item) for item in json.loads(__ENTRIES__))
transfer = DockerCloneVolumeTransfer(project_name=__PROJECT__, tool_image=__TOOL_IMAGE__)
staged = Path('/run/flow-steward-install-state/clone/restore-staging/operation')
assert transfer.publish_compact_data(staged, entries, plan_digest=__DIGEST__)
assert transfer.publish_compact_data(staged, entries, plan_digest=__DIGEST__, verify=True)
"""
        client_program = (
            client_program.replace(
                "__ENTRIES__",
                repr(json.dumps([entry.model_dump(mode="json") for entry in entries])),
            )
            .replace("__PROJECT__", repr(project))
            .replace("__TOOL_IMAGE__", repr(clone_tool_image))
            .replace("__DIGEST__", repr(digest))
        )
        socket_gid = Path("/var/run/docker.sock").stat().st_gid
        _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{client_uid}:{client_gid}",
            "--group-add",
            str(socket_gid),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{installer_state_volume}:/run/flow-steward-install-state",
            "-w",
            "/app",
            "--entrypoint",
            "python",
            clone_tool_image,
            "-c",
            client_program,
        )

        observed = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "-v",
            f"{volume}:/volume:ro",
            "--entrypoint",
            "sh",
            _tool_image(),
            "-c",
            "stat -c '%u:%g:%a' /volume/artifacts /volume/extensions "
            "/volume/state /volume/state/.flow-steward; "
            "cat /volume/state/.flow-steward/stack.toml; echo; "
            "cat /volume/state/.flow-steward/source-owned.json; echo; "
            "test ! -e /volume/artifacts/old.bin",
        ).stdout.splitlines()
        assert observed == [
            "10007:10100:2770",
            "10005:10103:2775",
            "10007:10102:750",
            "10007:10102:750",
            "target-shell",
            "source-state",
        ]
    finally:
        _cleanup_owned(("volume", volume), ("volume", installer_state_volume))


def test_compact_production_clone_wiring_runs_as_non_root(
    clone_tool_image: str,
) -> None:
    token = uuid.uuid4().hex[:12]
    project = f"clone-compact-wiring-{token}"
    compact_volume = f"{project}-data"
    rabbitmq_volume = f"{project}-rabbitmq"
    installer_state_volume = f"{project}-installer-state"
    client_uid = 11001
    client_gid = 11002
    _volume(compact_volume, project, volume_key="fs-compact-data")
    _volume(rabbitmq_volume, project)
    _run("docker", "volume", "create", installer_state_volume)
    try:
        _populate(
            compact_volume,
            " && ".join(
                (
                    "mkdir -p /volume/artifacts /volume/extensions",
                    "mkdir -p /volume/state/.flow-steward /volume/state/product /volume/backups",
                    "printf artifact > /volume/artifacts/item.bin",
                    "printf durable > /volume/state/product/item.bin",
                    "printf retained > /volume/state/.flow-steward/stack.toml",
                    "chown -R 10007:10100 /volume/artifacts",
                    "chown -R 10005:10103 /volume/extensions",
                    "chown -R 10007:10102 /volume/state",
                    "chown -R 10007:0 /volume/backups",
                    "chmod 2770 /volume/artifacts",
                    "chmod 2775 /volume/extensions",
                    "chmod 0750 /volume/state /volume/state/.flow-steward /volume/state/product",
                    "chmod 0700 /volume/backups",
                    "chmod 0640 /volume/artifacts/item.bin /volume/state/product/item.bin",
                )
            ),
        )
        _populate(
            rabbitmq_volume,
            "printf broker > /volume/state.bin && chown -R 12345:12346 /volume "
            "&& chmod 0710 /volume && chmod 0640 /volume/state.bin",
        )
        _populate(
            installer_state_volume,
            f"chown {client_uid}:{client_gid} /volume && chmod 0700 /volume",
        )
        client_program = """
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from core.application.clone.contracts import CloneDataClass
from core.application.clone.manifest import build_source_allowlist
from core.application.clone.service import CloneCreatePlan
from core.application.lifecycle.backup_service import BackupService
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity
from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime, _native_backup_gateway
from core.infrastructure.clone.archive_gateway import CloneArchiveGateway
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer
from core.infrastructure.clone.runtime_gateway import CloneRuntimeGateway

project = __PROJECT__
tool_image = __TOOL_IMAGE__
passphrase = b'compact production wiring'
installer_root = Path('/run/flow-steward-install-state')
platform = CompactPlatformRuntime()
workspace_root = platform._clone_workspace_root()
assert workspace_root == installer_root / 'clone/workspace'
workspace_root.mkdir(parents=True, mode=0o700)

def backup_runner(command, **kwargs):
    if command[0] == 'pg_dump':
        kwargs['stdout'].write(b'PGDMP production wiring')
    elif command[:2] == ['pg_restore', '-l']:
        kwargs['stdin'].read()
    return SimpleNamespace(returncode=0, stderr=b'')

backup_gateway = _native_backup_gateway(runner=backup_runner)
backup_service = BackupService(gateway=backup_gateway)

class StoppedServices:
    def service_states(self, names):
        return {name: False for name in names}
    def stop(self, _names):
        return None
    def start(self, _names):
        return None

config = DeploymentConfig(
    environment='production',
    deployment_profile='compact',
    data_placement='compact_volume',
    file_storage='local_fs',
)
release = ReleaseIdentity.model_validate_json(__RELEASE__)
output_root = installer_root / 'clone/outbox'
output_root.mkdir(parents=True, mode=0o700)
plan = CloneCreatePlan(
    operation_id='production-wiring',
    created_at=datetime(2026, 9, 26, tzinfo=UTC),
    output_path=str(output_root / 'production-wiring.fsclone'),
    source_installation_id=project,
    config=config,
    release=release,
    source_plan=build_source_allowlist(config, release),
)
transfer = DockerCloneVolumeTransfer(project_name=project, tool_image=tool_image)
archive = CloneArchiveGateway()
runtime = CloneRuntimeGateway(
    staging_root=workspace_root / 'create',
    output_root=output_root,
    backup=backup_service,
    services=StoppedServices(),
    volumes=transfer,
    source_roots={
        CloneDataClass.LOCAL_ARTIFACTS: Path('/data/artifacts'),
        CloneDataClass.EXTENSIONS: Path('/data/extensions'),
        CloneDataClass.DURABLE_STATE: Path('/data/state'),
    },
    archive=archive,
    capture_database_parity=lambda output: output.write_text('{}'),
    compact_data_volume=True,
)
runtime.preflight(plan)
runtime.capture_source(plan)
runtime.assemble_archive(plan, passphrase_source=lambda: passphrase)
runtime.verify_archive(plan, passphrase_source=lambda: passphrase)
workspace = workspace_root / 'create/production-wiring'
restore_parent = workspace_root / 'restore-staging'
with (workspace / 'archive.incomplete').open('rb') as source:
    verified = archive.stage_verified(
        source,
        staging_parent=restore_parent,
        passphrase_source=lambda: passphrase,
    )
try:
    assert transfer.publish_compact_data(
        verified.root,
        verified.manifest.entries,
        plan_digest='e' * 64,
    )
finally:
    verified.cleanup()
runtime.cleanup(plan)
assert not any((installer_root / 'clone/database-staging').rglob('*.dump'))
logs = tuple((installer_root / 'clone/database-logs').glob('clone-database-*.log'))
assert len(logs) == 1
assert logs[0].stat().st_mode & 0o777 == 0o600
print(json.dumps({
    'logs': str(backup_gateway._logs_root),
    'staging': str(backup_gateway._staging_root),
    'workspace': str(workspace_root),
}))
"""
        client_program = (
            client_program.replace("__PROJECT__", repr(project))
            .replace("__TOOL_IMAGE__", repr(clone_tool_image))
            .replace("__RELEASE__", repr(_release().model_dump_json()))
        )
        socket_gid = Path("/var/run/docker.sock").stat().st_gid
        observed = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            f"{client_uid}:{client_gid}",
            "--group-add",
            str(socket_gid),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "FS_INSTALLER_STATE_DIR=/run/flow-steward-install-state",  # pragma: allowlist secret - test path
            "-e",
            "FS_DB_DSN=dbname=flow user=flow password=secret host=pg-fs port=5432",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{compact_volume}:/data",
            "-v",
            f"{installer_state_volume}:/run/flow-steward-install-state",
            "-w",
            "/app",
            "--entrypoint",
            "python",
            clone_tool_image,
            "-c",
            client_program,
        )
        wiring = json.loads(observed.stdout)
        assert wiring == {
            "logs": "/run/flow-steward-install-state/clone/database-logs",
            "staging": "/run/flow-steward-install-state/clone/database-staging",
            "workspace": "/run/flow-steward-install-state/clone/workspace",
        }
        final_state = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "-v",
            f"{compact_volume}:/volume:ro",
            "--entrypoint",
            "sh",
            _tool_image(),
            "-c",
            "stat -c '%u:%g:%a' /volume/state /volume/state/.flow-steward /volume/backups; "
            "cat /volume/artifacts/item.bin /volume/state/product/item.bin; "
            "test ! -e /volume/state/.flow-steward/clone",
        ).stdout.splitlines()
        assert final_state == [
            "10007:10102:750",
            "10007:10102:750",
            "10007:0:700",
            "artifactdurable",
        ]
    finally:
        _cleanup_owned(
            ("volume", rabbitmq_volume),
            ("volume", compact_volume),
            ("volume", installer_state_volume),
        )


@pytest.mark.parametrize("storage", ("external_s3", "bundled"))
def test_compact_publication_without_local_artifacts_preserves_target_artifacts(
    clone_tool_image: str,
    tmp_path: Path,
    storage: str,
) -> None:
    token = uuid.uuid4().hex[:12]
    project = f"clone-compact-{storage}-{token}"
    compact_volume = f"{project}-data"
    bundled_volume = f"{project}-seaweedfs"
    staged = tmp_path / "staged"
    for root_name in ("extensions", "state"):
        (staged / root_name).mkdir(parents=True)
    (staged / "state/product.bin").write_bytes(b"restored-state")
    entries = tuple(
        entry
        for data_class, root_name in (
            (CloneDataClass.EXTENSIONS, "extensions"),
            (CloneDataClass.DURABLE_STATE, "state"),
        )
        for entry in inventory_volume_metadata(
            staged / root_name,
            archive_root=root_name,
            data_class=data_class,
        )
    )
    _volume(compact_volume, project, volume_key="fs-compact-data")
    if storage == "bundled":
        _volume(bundled_volume, project, volume_key="seaweedfs-data")
    try:
        _populate(
            compact_volume,
            "mkdir -p /volume/artifacts /volume/extensions "
            "/volume/state/.flow-steward && printf retained-artifact > "
            "/volume/artifacts/retained.bin",
        )
        transfer = DockerCloneVolumeTransfer(project_name=project, tool_image=clone_tool_image)

        assert transfer.publish_compact_data(staged, entries, plan_digest="f" * 64)
        assert transfer.publish_compact_data(
            staged,
            entries,
            plan_digest="f" * 64,
            verify=True,
        )
        retained = _run(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "-v",
            f"{compact_volume}:/volume:ro",
            "--entrypoint",
            "cat",
            _tool_image(),
            "/volume/artifacts/retained.bin",
        ).stdout
        assert retained == "retained-artifact"

        if storage == "bundled":
            bundled = tmp_path / "bundled-storage"
            bundled.mkdir()
            (bundled / "object.bin").write_bytes(b"bundled-object")
            bundled_entries = inventory_volume_metadata(
                bundled,
                archive_root="bundled-storage",
                data_class=CloneDataClass.BUNDLED_STORAGE,
            )
            # A new Docker volume is root-owned while audit staging is owned by
            # fs. Model a compatible target role before exercising replacement;
            # production correctly rejects mismatched numeric volume ownership.
            owner = bundled.stat()
            _populate(bundled_volume, f"chown {owner.st_uid}:{owner.st_gid} /volume")
            transfer.replace("seaweedfs", bundled, bundled_entries)
            assert transfer.matches("seaweedfs", bundled, bundled_entries) is True
    finally:
        resources = [("volume", compact_volume)]
        if storage == "bundled":
            resources.append(("volume", bundled_volume))
        _cleanup_owned(*resources)
