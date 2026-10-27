"""Item 5, step 2: where the shared trees live is recorded, and each profile can mount
them where the other profile left them.

ADR 0029, decision B: a profile switch keeps artifacts and extensions
in place -- Scalable's host directories or the Compact data volume -- and the other
profile mounts them there. This step only records the placement and adds the mounts;
the switch that keeps a placement is ./install.sh's (step 4). So, the R163 rule: an
installation the previous release wrote, on either profile, reads its own placement
and keeps its original Compose target apart from the current retained CLI transfer
shell required for Scalable host-directory operations.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import ConfigurationPatch, DeploymentConfig
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

ROOT = Path(__file__).resolve().parents[3]
PREVIOUS = ROOT / "tests/fixtures/lifecycle/previous-release-d9ca8ea45"
COMPOSE = ROOT / "infra/docker"
_OWN = {"scalable": "host_directories", "compact": "compact_volume"}
_COMPACT_OVERLAY = "infra/docker/compact/data-host_directories.yaml"
_COMPACT_ARTIFACTS_OVERLAY = "infra/docker/compact/data-host_directories-artifacts.yaml"
_SCALABLE_OVERLAY = "infra/docker/scalable/data-compact_volume.yaml"


def _previous(profile: str) -> DeploymentConfig:
    return DeploymentConfig.model_validate(
        tomllib.loads((PREVIOUS / f"stack-{profile}.toml").read_text(encoding="utf-8"))
    )


def _target(tmp_path: Path, config: DeploymentConfig) -> dict:
    service = ConfigurationService(store=LifecycleConfigurationStore(tmp_path))
    return service.derive_target(config, project_name_override="fs-prev").model_dump(mode="json")


@pytest.mark.parametrize("profile", sorted(_OWN))
def test_previous_placement_preserves_runtime_target_with_current_cli_transfer_shell(
    tmp_path: Path, profile: str
) -> None:
    before = json.loads(
        (PREVIOUS / "compose-target-before-data-placement.json").read_text(encoding="utf-8")
    )[profile]
    config = _previous(profile)

    assert config.data_placement == _OWN[profile]
    if profile == "scalable":
        assert config.data_placement == "host_directories"
        assert config.file_storage == "bundled"
        # APP01's retained CLI shell is supplemental; bundled storage still
        # selects no host-artifact data overlay. The historical receipt stays
        # immutable, and every other target field must remain unchanged.
        before = {
            **before,
            "compose_files": [
                *before["compose_files"],
                "infra/docker/scalable/cli-host-directories.yaml",
            ],
        }
    assert _target(tmp_path, config) == before


@pytest.mark.parametrize("profile", sorted(_OWN))
def test_the_previous_journal_configuration_reads_its_own_placement(profile: str) -> None:
    journal = json.loads(
        (PREVIOUS / f"install-state-config-{profile}.json").read_text(encoding="utf-8")
    )

    assert DeploymentConfig.model_validate(journal).data_placement == _OWN[profile]


@pytest.mark.parametrize(
    ("profile", "placement", "overlay"),
    [
        ("compact", "host_directories", _COMPACT_ARTIFACTS_OVERLAY),  # local_fs: both
        ("scalable", "compact_volume", _SCALABLE_OVERLAY),
    ],
)
def test_the_other_profiles_placement_selects_its_mounts(
    tmp_path: Path, profile: str, placement: str, overlay: str
) -> None:
    config = DeploymentConfig.model_validate(
        {**_previous(profile).model_dump(mode="json"), "data_placement": placement}
    )

    files = _target(tmp_path, config)["compose_files"]

    assert files[-1] == overlay
    assert (ROOT / overlay).is_file()


def test_the_compact_volume_placement_is_production_only() -> None:
    with pytest.raises(ValueError, match="production-only"):
        DeploymentConfig(environment="development", data_placement="compact_volume")


def test_the_documented_config_apply_switch_still_moves_to_the_targets_own_place() -> None:
    """The green direction of "nothing changes": `config apply` copies the trees (the
    runbook's switch), so its candidate takes the target profile's own placement."""
    scalable = _previous("scalable")

    candidate = ConfigurationService._candidate(
        scalable, ConfigurationPatch(deployment_profile="compact", file_storage="local_fs")
    )
    same_profile = ConfigurationService._candidate(scalable, ConfigurationPatch(public_port=8443))

    assert candidate.data_placement == "compact_volume"
    assert same_profile.data_placement == "host_directories"


def _prod_services() -> dict:
    return yaml.safe_load((COMPOSE / "docker-compose.prod.yaml").read_text(encoding="utf-8"))[
        "services"
    ]


_SCALABLE_TREES = {
    "${FS_EXTENSIONS_DIR": ("/app/extensions", "extensions"),
    # artifacts are the logs directory's artifacts/ (ARTIFACT_LOCAL_ROOT_DIR)
    "${FS_LOGS_DIR": ("/app/logs/artifacts", "artifacts"),
}


def test_every_scalable_service_on_a_shared_tree_mounts_it_from_the_compact_volume() -> None:
    """Derived from the Scalable compose file: each bind of a shared tree has a volume
    mount for the same place, with the same access, from the Compact tree of that name."""
    overlay = yaml.safe_load((ROOT / _SCALABLE_OVERLAY).read_text(encoding="utf-8"))
    expected: dict[str, dict[str, tuple[str, bool]]] = {}
    for name, service in _prod_services().items():
        for volume in service.get("volumes") or []:
            text = str(volume)
            for variable, (target, subpath) in _SCALABLE_TREES.items():
                if text.startswith(variable):
                    read_only = text.endswith(":ro")
                    expected.setdefault(name, {})[target] = (subpath, read_only)
    assert {"fs-app", "fs-worker", "fs-extension-installer"} <= set(expected)  # non-vacuity
    # AC17-R166: fs-cli's mounts are an exact contract the installer and the launcher
    # verify, and it serves no workspace, so it binds no shared tree at all.
    assert "fs-cli" not in expected

    mounted = {
        name: {
            mount["target"]: (mount["volume"]["subpath"], bool(mount.get("read_only")))
            for mount in service["volumes"]
        }
        for name, service in overlay["services"].items()
    }
    assert mounted == expected
    assert all(
        mount["source"] == "fs-compact-data"
        for service in overlay["services"].values()
        for mount in service["volumes"]
    )
    assert "fs-compact-data" in overlay["volumes"]


def test_the_compact_overlay_binds_the_trees_where_compact_keeps_them() -> None:
    """Derived from the Compact compose file's own tree locations."""
    compact = yaml.safe_load((COMPOSE / "docker-compose.compact.yaml").read_text(encoding="utf-8"))
    runtime_env = compact["services"]["compact-runtime"]["environment"]
    places = {
        runtime_env["ARTIFACT_LOCAL_ROOT_DIR"],
        compact["x-compact-runtime-environment"]["FS_EXTENSIONS_ROOT"],
    }
    overlays = [
        yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        for name in (_COMPACT_OVERLAY, _COMPACT_ARTIFACTS_OVERLAY)
    ]

    for service in ("compact-runtime", "fs-cli"):
        binds = [bind for overlay in overlays for bind in overlay["services"][service]["volumes"]]
        assert {bind.rsplit(":", 1)[1] for bind in binds} == places, service
        sources = {re.match(r"\$\{(\w+):\?", bind).group(1) for bind in binds}
        assert sources == {"FS_LOGS_DIR", "FS_EXTENSIONS_DIR"}
        # AC17-R169: the artifacts, and nothing else, are bound only with local_fs
        (artifacts,) = overlays[1]["services"][service]["volumes"]
        assert artifacts.rsplit(":", 1)[1] == runtime_env["ARTIFACT_LOCAL_ROOT_DIR"]


@pytest.mark.parametrize(
    ("file_storage", "expected"),
    [
        ("local_fs", [_COMPACT_OVERLAY, _COMPACT_ARTIFACTS_OVERLAY]),
        ("bundled", [_COMPACT_OVERLAY]),
        ("external_s3", [_COMPACT_OVERLAY]),
    ],
)
def test_the_host_artifacts_are_bound_only_where_they_are_the_data(
    tmp_path: Path, file_storage: str, expected: list[str]
) -> None:
    """AC17-R169: a Scalable on bundled storage has no logs/artifacts tree at all; the
    switch to Compact demanded it and refused."""
    values = {
        **_previous("compact").model_dump(mode="json"),
        "data_placement": "host_directories",
        "file_storage": file_storage,
    }
    if file_storage == "external_s3":
        values.update(s3_endpoint="https://s3.example.test", s3_bucket="fs", s3_region="eu-1")
    files = _target(tmp_path, DeploymentConfig.model_validate(values))["compose_files"]

    assert [name for name in files if "data-host_directories" in name] == expected


@pytest.mark.parametrize("profile", sorted(_OWN))
def test_the_files_an_existing_installation_writes_carry_no_new_key(
    tmp_path: Path, profile: str
) -> None:
    """The previous release reads stack.toml and the installer journal as closed shapes:
    a key it does not know would make a rollback's installation unreadable."""
    from core.application.installer import InstallConfig

    config = _previous(profile)
    store = LifecycleConfigurationStore(tmp_path)
    store.write_active(config)

    written = tomllib.loads((tmp_path / ".flow-steward/stack.toml").read_text(encoding="utf-8"))
    assert "data_placement" not in written
    assert "data_placement" not in InstallConfig.model_validate(written).model_dump(mode="json")
    assert store.read_active().data_placement == _OWN[profile]  # still read as its own


def test_a_switched_installation_writes_and_reads_its_placement(tmp_path: Path) -> None:
    config = DeploymentConfig.model_validate(
        {**_previous("compact").model_dump(mode="json"), "data_placement": "host_directories"}
    )
    store = LifecycleConfigurationStore(tmp_path)
    store.write_active(config)

    written = tomllib.loads((tmp_path / ".flow-steward/stack.toml").read_text(encoding="utf-8"))
    assert written["data_placement"] == "host_directories"
    assert store.read_active().data_placement == "host_directories"


def test_the_installer_switch_keeps_the_placement_it_names() -> None:
    """Step 4: ./install.sh --deployment-profile writes the placement into the patch."""
    scalable = _previous("scalable")

    candidate = ConfigurationService._candidate(
        scalable,
        ConfigurationPatch(
            deployment_profile="compact",
            file_storage="local_fs",
            data_placement="host_directories",
        ),
    )

    assert candidate.deployment_profile == "compact"
    assert candidate.data_placement == "host_directories"


def test_config_set_refuses_the_placement_by_name() -> None:
    from core.application.lifecycle.configuration_service import ConfigSetRequest

    with pytest.raises(ValueError, match=r"chosen by \./install\.sh --deployment-profile"):
        ConfigSetRequest(data_placement="compact_volume")


def test_compact_with_bundled_storage_runs_seaweedfs_beside_it(tmp_path: Path) -> None:
    """Item 5, step 5."""
    config = DeploymentConfig.model_validate(
        {**_previous("compact").model_dump(mode="json"), "file_storage": "bundled"}
    )

    target = _target(tmp_path, config)

    assert "infra/docker/compact/storage-bundled.yaml" in target["compose_files"]
    assert "seaweedfs" in target["runtime_services"]
    assert "storage-seaweedfs" in target["compose_profiles"]


def test_compacts_seaweedfs_shares_storage_but_uses_bounded_restart() -> None:
    """Derived: a drift between the two would store files differently per profile."""
    scalable = yaml.safe_load((COMPOSE / "docker-compose.prod.yaml").read_text(encoding="utf-8"))
    compact = yaml.safe_load(
        (ROOT / "infra/docker/compact/storage-bundled.yaml").read_text(encoding="utf-8")
    )

    for service in ("seaweedfs-config", "seaweedfs"):
        expected = dict(scalable["services"][service])
        if service == "seaweedfs":
            expected["restart"] = "on-failure:3"
        assert compact["services"][service] == expected, service
    assert compact["networks"]["artifact-storage"] == {"internal": True}
    assert set(compact["volumes"]) == {"seaweedfs-config", "seaweedfs-data"}
    runtime_networks = compact["services"]["compact-runtime"]["networks"]
    assert set(runtime_networks) == {"default", "artifact-storage"}  # keeps the ingress side
