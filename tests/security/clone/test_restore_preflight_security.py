from __future__ import annotations

from pathlib import Path

from core.application.clone.planning import CloneReplacementPlan, CloneTargetShell
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.application.release.service import release_platform_for_machine
from core.infrastructure.clone.archive_gateway import CloneRestoreArchiveGateway


def _shell(**updates: object) -> CloneTargetShell:
    shell = CloneTargetShell.from_config(
        installation_id="target-installation",
        compose_project="flow-steward-target",
        config=DeploymentConfig(public_host="target.example.test"),
        host_uid=1000,
        host_gid=1000,
        filesystem_roots={"extensions": "/srv/extensions"},
        postgres_roles=("flow_steward",),
        postgres_identity_fingerprint="f" * 64,
    )
    return shell.model_copy(update=updates)


def _release() -> ReleaseIdentity:
    digest = "sha256:" + "1" * 64
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha="a" * 40,
        registry="ghcr.io/example",
        profile="scalable",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/example/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="a" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )


def test_replacement_plan_digest_is_canonical_and_bound_to_target_shell() -> None:
    release = _release()
    first = CloneReplacementPlan.build(
        source_installation_id="source",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="e" * 64,
        target_shell=_shell(),
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql", "extensions"),
        external_effect_categories=("webhooks",),
    )
    same = CloneReplacementPlan.build(
        source_installation_id="source",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="e" * 64,
        target_shell=_shell(filesystem_roots={"extensions": "/srv/extensions"}),
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql", "extensions"),
        external_effect_categories=("webhooks",),
    )
    changed = CloneReplacementPlan.build(
        source_installation_id="source",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="e" * 64,
        target_shell=_shell(configuration=DeploymentConfig(public_host="changed.example.test")),
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql", "extensions"),
        external_effect_categories=("webhooks",),
    )
    changed_archive = CloneReplacementPlan.build(
        source_installation_id="source",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="d" * 64,
        target_shell=_shell(),
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql", "extensions"),
        external_effect_categories=("webhooks",),
    )

    assert first.plan_digest == same.plan_digest
    assert first.plan_digest != changed.plan_digest
    assert first.plan_digest != changed_archive.plan_digest
    assert "passphrase" not in first.model_dump_json()


def test_restore_platform_normalization_accepts_only_supported_linux_architectures() -> None:
    assert release_platform_for_machine("x86_64") == "linux/amd64"
    assert release_platform_for_machine("aarch64") == "linux/arm64"


def test_restore_archive_path_rejects_symlinks_before_decryption(tmp_path: Path) -> None:
    archive = tmp_path / "archive.fsclone"
    archive.write_bytes(b"not opened through alias")
    alias = tmp_path / "alias.fsclone"
    alias.symlink_to(archive)
    gateway = CloneRestoreArchiveGateway(staging_parent=tmp_path / "staging")

    try:
        gateway.artifact_size(str(alias))
    except ValueError as exc:
        assert "symbolic link" in str(exc)
    else:  # pragma: no cover - security assertion
        raise AssertionError("Clone archive symlink was accepted")
