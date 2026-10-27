"""AC17-R167: a profile switch writes the target profile's own values, not the source's.

Live: C->S on fs-r123 reached the Scalable candidate with Compact's
ARTIFACT_LOCAL_ROOT_DIR=/data/artifacts, and fs-app died on `makedirs('/data')` --
Scalable mounts the artifacts at /app/logs/artifacts. `config apply` carried the value
over from the source's .env.prod; only the installer knew it was the profile's.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import PROFILE_OWNED_ENVIRONMENT, DeploymentConfig
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_ROOT = Path(__file__).resolve().parents[3]
_DOCKER = _ROOT / "infra" / "docker"
_CONTAINER_PATH_DEFAULT = re.compile(r"^\$\{([A-Z0-9_]+):-(/[^}]*)\}$")

# What a real installation's .env.prod holds for each profile (fs-ac19-bench, Compact,
# 2026-09-24; the Scalable value is what ensure_secrets has always written there).
_INSTALLED_VALUES = {"compact": "/data/artifacts", "scalable": "/app/logs/artifacts"}
_OTHER = {"compact": "scalable", "scalable": "compact"}


def _service(tmp_path: Path, profile: str) -> ConfigurationService:
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text(
        "\n".join(
            (
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                'FS_SECRET_KEY="keep fs secret"',
                'ENCRYPTION_KEY="keep encryption key"',
                'FS_BACKUP_ENCRYPTION_KEY="keep backup key"',
                'POSTGRES_PASSWORD="keep postgres password"',
                'RABBITMQ_PASS="keep rabbit password"',
                'FS_METRICS_TOKEN="keep metrics token"',
                'SECURITY_PII_SALT="keep pii salt"',
                f"ARTIFACT_LOCAL_ROOT_DIR={_INSTALLED_VALUES[profile]}",
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            )
        ),
        encoding="utf-8",
    )
    return ConfigurationService(store=store, lifecycle_state=LifecycleStateStore(tmp_path))


@pytest.mark.parametrize("source", ["compact", "scalable"])
def test_a_switch_renders_the_target_profiles_own_values(tmp_path: Path, source: str) -> None:
    target = _OTHER[source]
    service = _service(tmp_path, source)
    active = DeploymentConfig(
        environment="production", deployment_profile=source, file_storage="local_fs"
    )
    candidate = active.model_copy(update={"deployment_profile": target})

    prepared = service.prepare_transition_environment(active, candidate)
    service.stage_transition_environment(active, candidate, prepared)
    staged = service.store.read_env_values(tmp_path / ".env.prod")

    for name, values in PROFILE_OWNED_ENVIRONMENT.items():
        assert prepared[2][name] == values[target]
        assert staged[name] == values[target], f"{name} kept the {source} value"


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_staying_on_a_profile_keeps_its_values(tmp_path: Path, profile: str) -> None:
    """The green direction: an installation that is not switching is not changed."""
    service = _service(tmp_path, profile)
    active = DeploymentConfig(
        environment="production", deployment_profile=profile, file_storage="local_fs"
    )

    _target, _path, environment = service.prepare_environment(active)

    assert environment["ARTIFACT_LOCAL_ROOT_DIR"] == _INSTALLED_VALUES[profile]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _environment_mappings(document: dict[str, Any]) -> list[dict[str, Any]]:
    mappings = [value for key, value in document.items() if key.startswith("x-")]
    mappings += [
        service.get("environment") or {} for service in (document.get("services") or {}).values()
    ]
    return [mapping for mapping in mappings if isinstance(mapping, dict)]


def test_every_container_path_scalable_reads_from_the_environment_is_profile_owned() -> None:
    """Derived: a new `${NAME:-/container/path}` in Scalable's Compose joins the table."""
    found: dict[str, set[str]] = {}
    for mapping in _environment_mappings(_load(_DOCKER / "docker-compose.prod.yaml")):
        for value in mapping.values():
            match = _CONTAINER_PATH_DEFAULT.match(str(value))
            if match:
                found.setdefault(match.group(1), set()).add(match.group(2))

    assert "ARTIFACT_LOCAL_ROOT_DIR" in found  # non-vacuity: the parse reaches it
    assert set(found) <= set(PROFILE_OWNED_ENVIRONMENT), set(found) - set(PROFILE_OWNED_ENVIRONMENT)
    for name, defaults in found.items():
        assert defaults == {PROFILE_OWNED_ENVIRONMENT[name]["scalable"]}


def test_compact_hard_codes_its_own_values() -> None:
    compact = _environment_mappings(_load(_DOCKER / "docker-compose.compact.yaml"))
    for name, values in PROFILE_OWNED_ENVIRONMENT.items():
        seen = {str(mapping[name]) for mapping in compact if name in mapping}
        assert seen == {values["compact"]}, name


def _target(mount: str) -> str:
    """The container side of a short-syntax mount, whose source may hold `${X:-y}`."""
    parts = mount.split(":")
    return parts[-2] if parts[-1] in {"ro", "rw"} else parts[-1]


def test_the_artifact_root_is_where_each_profile_mounts_the_artifacts() -> None:
    root = PROFILE_OWNED_ENVIRONMENT["ARTIFACT_LOCAL_ROOT_DIR"]
    overlay = _load(_DOCKER / "scalable" / "data-compact_volume.yaml")
    artifact_targets = {
        mount["target"]
        for service in overlay["services"].values()
        for mount in service.get("volumes") or ()
        if isinstance(mount, dict) and (mount.get("volume") or {}).get("subpath") == "artifacts"
    }
    assert artifact_targets == {root["scalable"]}

    prod = _load(_DOCKER / "docker-compose.prod.yaml")
    for service in ("fs-app", "fs-worker"):
        targets = {_target(mount) for mount in prod["services"][service]["volumes"]}
        assert any(root["scalable"].startswith(target + "/") for target in targets), service

    compact = _load(_DOCKER / "docker-compose.compact.yaml")
    runtime_targets = {
        _target(mount) for mount in compact["services"]["compact-runtime"]["volumes"]
    }
    assert any(root["compact"].startswith(target + "/") for target in runtime_targets)


# What the installer itself writes before it renders the target of a switch, by owner.
# Everything else a target profile's Compose requires, the apply must write (AC17-R170).
_WRITTEN_BY_THE_INSTALLER_FIRST = {
    "compact": {
        # prepare_core: the exact Compact image and its provenance
        "FS_COMPACT_IMAGE",
        "FS_COMPACT_IMAGE_CONFIG_DIGEST",
        "FS_COMPACT_IMAGE_INDEX_DIGEST",
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST",
        "FS_COMPACT_IMAGE_PLATFORM",
        "FS_COMPACT_IMAGE_REVISION",
    },
    "scalable": {
        # prepare_core: the Scalable release images
        "FS_APP_IMAGE_DIGEST",
        "FS_BUILD_SHA",
        "FS_CLI_IMAGE",
        "FS_CONTROL_IMAGE_DIGEST",
        "FS_WORKER_IMAGE_DIGEST",
        "MCP_IMAGE_DIGEST",
        "MCP_IMAGE_REVISION",
        # restore_switch_worker_token (AC17-R165)
        "FS_WORKER_MCP_BEARER_TOKEN",
    },
}
_PROFILE_COMPOSE = {
    "compact": ("docker-compose.compact.yaml", "compact"),
    "scalable": ("docker-compose.prod.yaml", "scalable"),
}


@pytest.mark.parametrize("target", ["compact", "scalable"])
def test_a_switch_writes_every_value_the_target_compose_requires(
    tmp_path: Path, target: str
) -> None:
    """AC17-R170: S->C refused on FS_WORKER_PROCESSES, which only Compact requires. Every
    `${NAME:?}` of the target profile's Compose files (every overlay, selected or not) is
    written by the apply for the target, or by the installer first -- nothing is left for
    an installation to happen to have."""
    main, overlays = _PROFILE_COMPOSE[target]
    required: set[str] = set()
    for path in (_DOCKER / main, *sorted((_DOCKER / overlays).glob("*.yaml"))):
        required |= set(re.findall(r"\$\{([A-Z0-9_]+):\?", path.read_text(encoding="utf-8")))
    source = _OTHER[target]
    service = _service(tmp_path, source)
    active = DeploymentConfig(
        environment="production", deployment_profile=source, file_storage="local_fs"
    )
    candidate = active.model_copy(update={"deployment_profile": target})

    _target, _path, written = service.prepare_transition_environment(active, candidate)

    # non-vacuity: the value that failed live is required, and written by the apply
    assert target != "compact" or {"FS_WORKER_PROCESSES", "FS_RUNTIME_HTTP_PORT"} <= required
    assert target != "compact" or written["FS_WORKER_PROCESSES"] == "2"
    assert required - set(written) == _WRITTEN_BY_THE_INSTALLER_FIRST[target]


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [("/srv/ext", "/srv/ext"), (None, "../../extensions")],
)
def test_a_switch_to_compact_on_host_trees_renders_where_they_are(
    tmp_path: Path, recorded: str | None, expected: str
) -> None:
    """AC17-R174: the apply renders from these values alone (--env-file /dev/null): the
    trees the installer recorded, else Scalable's own defaults."""
    service = _service(tmp_path, "scalable")
    if recorded is not None:  # where record_switch_host_trees writes it: after the block
        with (tmp_path / ".env.prod").open("a", encoding="utf-8") as stream:
            stream.write(f"FS_EXTENSIONS_DIR={recorded}\n")
    active = DeploymentConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )
    candidate = active.model_copy(update={"deployment_profile": "compact"})
    assert candidate.data_placement == "host_directories"

    _target, _path, written = service.prepare_transition_environment(active, candidate)

    assert written["FS_EXTENSIONS_DIR"] == expected
    assert written["FS_LOGS_DIR"] == "../../logs"
    assert "FS_TEST_PROJECT_ROOT" not in written


def test_the_host_tree_defaults_are_scalables_own() -> None:
    from core.application.lifecycle.configuration_rendering import _SCALABLE_HOST_TREES

    text = (_DOCKER / "docker-compose.prod.yaml").read_text(encoding="utf-8")
    defaults = dict(re.findall(r"\$\{(FS_[A-Z_]+):-([^}]*)\}", text))
    assert {name: defaults[name] for name in _SCALABLE_HOST_TREES} == _SCALABLE_HOST_TREES
