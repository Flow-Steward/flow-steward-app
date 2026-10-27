from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.application.clone.contracts import (
    CloneDataClass,
    CloneEntry,
    CloneManifest,
)
from core.application.clone.manifest import (
    build_source_allowlist,
    validate_clone_manifest_completeness,
)
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles

_COMMIT = "a" * 40
_INDEX = "sha256:" + "1" * 64
_MANIFEST = "sha256:" + "2" * 64
_CONFIG = "sha256:" + "3" * 64


def _release(profile: str = "scalable") -> ReleaseIdentity:
    platform = "linux/arm64"
    registry = "ghcr.io/example"
    images = tuple(
        ReleaseImageIdentity(
            role=role.role,
            registry_ref=f"{registry}/{role.repository}@{_MANIFEST}",
            index_digest=_INDEX,
            manifest_digest=_MANIFEST,
            config_digest=_CONFIG,
            revision=_COMMIT,
            platform=platform,
        )
        for role in required_product_roles(profile)
    )
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=_COMMIT,
        registry=registry,
        profile=profile,
        platform=platform,
        images=images,
    )


def _entry(**updates: object) -> CloneEntry:
    values: dict[str, object] = {
        "data_class": CloneDataClass.EXTENSIONS,
        "path": "extensions/acme/config.json",
        "entry_type": "file",
        "size": 3,
        "mode": 0o640,
        "sha256": "0" * 64,
    }
    values.update(updates)
    return CloneEntry.model_validate(values)


def _manifest(*entries: CloneEntry, **updates: object) -> CloneManifest:
    selected = entries or (_entry(),)
    values: dict[str, object] = {
        "created_at": datetime(2026, 9, 26, tzinfo=UTC),
        "release": _release(),
        "deployment_profile": "scalable",
        "data_placement": "host_directories",
        "file_storage": "local_fs",
        "source_installation_id": "source-installation",
        "entries": selected,
        "entry_count": len(selected),
        "aggregate_size": sum(entry.size for entry in selected),
    }
    values.update(updates)
    return CloneManifest.model_validate(values)


def test_clone_manifest_is_closed_and_requires_exact_entry_evidence() -> None:
    manifest = _manifest()
    unknown = {**manifest.model_dump(), "unknown": True}

    with pytest.raises(ValidationError):
        CloneManifest.model_validate(unknown)
    with pytest.raises(ValidationError, match="entry_count"):
        _manifest(entry_count=2)
    with pytest.raises(ValidationError, match="aggregate_size"):
        _manifest(aggregate_size=4)
    duplicate = _entry()
    with pytest.raises(ValidationError, match="unique"):
        _manifest(duplicate, duplicate)
    with pytest.raises(ValidationError, match="ownership"):
        _entry(data_class=CloneDataClass.LOCAL_ARTIFACTS)


@pytest.mark.parametrize(
    "path",
    (
        "/absolute",
        "../parent",
        "extensions/../parent",
        "extensions//duplicate",
        "extensions/./duplicate",
        "extensions/trailing/",
        "extensions/nul\x00name",
        "extensions/" + "x" * 505,
    ),
)
def test_clone_entry_rejects_unsafe_or_noncanonical_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        _entry(path=path)


def test_clone_entry_rejects_invalid_sizes_checksums_and_modes() -> None:
    with pytest.raises(ValidationError):
        _entry(size=8 * 1024**3 + 1)
    with pytest.raises(ValidationError):
        _entry(sha256="not-a-digest")
    with pytest.raises(ValidationError):
        _entry(mode=0o4755)
    with pytest.raises(ValidationError):
        _entry(entry_type="directory", size=1, sha256=None)
    with pytest.raises(ValidationError):
        _entry(entry_type="symlink", size=0, sha256=None, link_target="/escape")


def test_clone_entry_preserves_setgid_on_directories_only() -> None:
    directory = _entry(
        path="extensions/acme",
        entry_type="directory",
        size=0,
        mode=0o2770,
        sha256=None,
    )

    assert directory.mode == 0o2770
    with pytest.raises(ValidationError, match="special mode"):
        _entry(mode=0o2640)


def test_clone_entry_authenticates_numeric_ownership() -> None:
    entry = _entry(uid=10001, gid=10101)

    assert (entry.uid, entry.gid) == (10001, 10101)
    with pytest.raises(ValidationError):
        _entry(uid=-1)


@pytest.mark.parametrize("mode", (0o1777, 0o3770))
def test_clone_directory_preserves_sticky_without_allowing_setuid(mode: int) -> None:
    directory = _entry(
        data_class=CloneDataClass.RABBITMQ,
        path="rabbitmq",
        entry_type="directory",
        size=0,
        mode=mode,
        sha256=None,
    )
    assert directory.mode == mode
    with pytest.raises(ValidationError):
        _entry(entry_type="directory", size=0, sha256=None, mode=0o4777)
    with pytest.raises(ValidationError, match="special mode"):
        _entry(mode=0o1640)


@pytest.mark.parametrize(
    ("data_class", "path"),
    (
        (CloneDataClass.EXTENSIONS, "extensions/acme/node_modules/pkg/index.js"),
        (CloneDataClass.EXTENSIONS, "extensions/acme/.venv/bin/python"),
        (CloneDataClass.EXTENSIONS, "extensions/acme/__pycache__/module.pyc"),
        (CloneDataClass.DURABLE_STATE, "state/logs/app.log"),
        (CloneDataClass.DURABLE_STATE, "state/backups/old.fsclone"),
        (CloneDataClass.DURABLE_STATE, "state/journals/operation.json"),
        (CloneDataClass.DURABLE_STATE, "state/recovery/pending.json"),
        (CloneDataClass.DURABLE_STATE, "state/.git/config"),
        (CloneDataClass.DURABLE_STATE, "state/install-state.json"),
        (CloneDataClass.DURABLE_STATE, "state/pending-release.json"),
        (CloneDataClass.DURABLE_STATE, "state/release-handoff.json"),
        (CloneDataClass.DURABLE_STATE, "state/release-transition-binding.json"),
        (CloneDataClass.DURABLE_STATE, "state/install.lock"),
        (CloneDataClass.DURABLE_STATE, "state/secret-input"),
        (CloneDataClass.DURABLE_STATE, "state/secret-input/github-release-token"),
        (CloneDataClass.DURABLE_STATE, "state/secret-output/bootstrap-token"),
    ),
)
def test_clone_entry_rejects_generated_or_ephemeral_state(
    data_class: CloneDataClass,
    path: str,
) -> None:
    with pytest.raises(ValidationError, match=r"excluded|generated|ephemeral"):
        _entry(data_class=data_class, path=path)


@pytest.mark.parametrize(
    ("storage", "included", "excluded"),
    (
        ("local_fs", CloneDataClass.LOCAL_ARTIFACTS, CloneDataClass.BUNDLED_STORAGE),
        ("bundled", CloneDataClass.BUNDLED_STORAGE, CloneDataClass.LOCAL_ARTIFACTS),
        ("external_s3", None, CloneDataClass.LOCAL_ARTIFACTS),
    ),
)
def test_source_allowlist_selects_only_installation_owned_storage(
    storage: str,
    included: CloneDataClass | None,
    excluded: CloneDataClass,
) -> None:
    config = DeploymentConfig(file_storage=storage)
    release = _release()

    plan = build_source_allowlist(config, release)
    classes = {item.data_class for item in plan.items}

    assert CloneDataClass.POSTGRESQL in classes
    assert CloneDataClass.RABBITMQ in classes
    assert CloneDataClass.EXTENSIONS in classes
    assert CloneDataClass.DURABLE_STATE in classes
    assert CloneDataClass.SOURCE_SECRETS in classes
    assert CloneDataClass.RELEASE_EVIDENCE in classes
    assert excluded not in classes
    if included is not None:
        assert included in classes
    if storage == "external_s3":
        assert CloneDataClass.BUNDLED_STORAGE not in classes

    forbidden = {
        "core",
        ".git",
        "images",
        "logs",
        "caches",
        "backups",
        "journals",
        "node_modules",
    }
    assert forbidden.isdisjoint({item.archive_root for item in plan.items})


def test_source_allowlist_rejects_release_profile_mismatch() -> None:
    config = DeploymentConfig()
    release = _release("compact")
    with pytest.raises(ValueError, match="profile"):
        build_source_allowlist(config, release)


def test_manifest_never_serializes_secret_values() -> None:
    manifest = _manifest(
        imported_secret_names=("FS_MASTER_KEY", "RABBITMQ_PASSWORD"),
        source_secret_fingerprints={"FS_MASTER_KEY": "a" * 64},
    )

    serialized = manifest.model_dump_json()
    assert "actual-secret-value" not in serialized
    assert "FS_MASTER_KEY" in serialized


def test_manifest_completeness_rejects_missing_restore_payloads() -> None:
    manifest = _manifest()
    with pytest.raises(ValueError, match="missing required Clone payload"):
        validate_clone_manifest_completeness(manifest)
