from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_FRESHNESS_HELPER = ROOT / "core/entrypoints/cli/scripts/frontend_shell_freshness.sh"
SCALABLE_CONFIG_STATE_ROOT = "/run/flow-steward-install-state"
SCALABLE_COMPOSE_FILES = (
    ROOT / "infra/docker/docker-compose.yaml",
    ROOT / "infra/docker/scalable/cli-host-directories.yaml",
    ROOT / "infra/docker/scalable/cli-host-directories-artifacts.yaml",
)


def _render_scalable_compose_config(
    tmp_path: Path, *, environment: dict[str, str]
) -> dict[str, Any]:
    env_file = tmp_path / "empty-compose.env"
    env_file.write_text("", encoding="utf-8")
    command = ["docker", "compose", "-p", "cli-state-path-contract"]
    for compose_file in SCALABLE_COMPOSE_FILES:
        command.extend(("-f", str(compose_file)))
    command.extend(
        (
            "--env-file",
            str(env_file),
            "--profile",
            "cli",
            "--profile",
            "storage-seaweedfs",
            "config",
            "--format",
            "json",
        )
    )
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose config must render without contacting the daemon: {result.stderr[:1000]}"
    )
    return json.loads(result.stdout)


def _bind_mounts_by_target(service: dict[str, Any]) -> dict[str, str]:
    return {
        str(volume["target"]): str(volume["source"])
        for volume in service.get("volumes", [])
        if isinstance(volume, dict) and volume.get("type") == "bind"
    }


def _frontend_shell_build_required(frontend_dir: Path, *extra_inputs: Path) -> bool:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && frontend_shell_build_required "${@:2}"',
            "frontend-freshness-test",
            str(FRONTEND_FRESHNESS_HELPER),
            str(frontend_dir),
            *(str(path) for path in extra_inputs),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_shell_compose_helper_reads_flow_steward_stack_state() -> None:
    source = (ROOT / "core/entrypoints/cli/scripts/docker_compose_env.sh").read_text(
        encoding="utf-8"
    )

    assert ".flow-steward/compose.env" in source
    assert 'FS_COMPOSE_PROJECT_NAME="${FS_COMPOSE_PROJECT_NAME:-flow-steward}"' in source
    assert "fs_compose_env_file_path()" in source
    assert '"--env-file" "${env_file}"' in source
    assert "FS_ENV_FILE" in source
    assert "FS_COMPOSE_FILES" in source
    assert "FS_COMPOSE_PROFILES" in source
    assert "fs_compose()" in source


def test_nested_compose_keeps_selected_state_as_a_docker_host_path(tmp_path: Path) -> None:
    selected_state = tmp_path / "state outside checkout"
    runtime_state = selected_state / "runtime"
    runtime_state.mkdir(parents=True)
    (runtime_state / "seaweedfs-s3.json").write_text("{}\n", encoding="utf-8")
    selected_state_text = str(selected_state)
    compose_environment = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "development",
        "FS_STACK_ENVIRONMENT": "development",
        "FS_DOCKER_HOST_PROJECT_ROOT": str(ROOT),
        "FS_INSTALLER_STATE_DIR": selected_state_text,
    }

    outer = _render_scalable_compose_config(tmp_path, environment=compose_environment)
    cli = outer["services"]["fs-cli"]
    cli_environment = cli["environment"]
    cli_mounts = _bind_mounts_by_target(cli)
    configuration_root = cli_environment["FS_CONFIGURATION_STATE_ROOT"]
    installer_state_root = cli_environment["FS_INSTALLER_STATE_DIR"]

    assert configuration_root == SCALABLE_CONFIG_STATE_ROOT
    assert cli_mounts.get(configuration_root) == selected_state_text
    assert cli_mounts.get(str(installer_state_root)) == selected_state_text
    assert cli_mounts.get(str(ROOT)) == str(ROOT)

    nested_environment = {
        **compose_environment,
        **{str(key): str(value) for key, value in cli_environment.items() if value is not None},
    }
    nested = _render_scalable_compose_config(tmp_path, environment=nested_environment)
    expected_runtime = str(runtime_state)
    for service_name in ("fs-app", "fs-worker", "control-plane"):
        mounts = _bind_mounts_by_target(nested["services"][service_name])
        assert mounts.get("/run/flow-steward-runtime") == expected_runtime, (
            f"nested {service_name} bind must resolve from the selected Docker-host state"
        )

    seaweed_mounts = _bind_mounts_by_target(nested["services"]["seaweedfs-config"])
    assert seaweed_mounts.get("/run/flow-steward-runtime/seaweedfs-s3.json") == str(
        runtime_state / "seaweedfs-s3.json"
    ), "nested SeaweedFS config bind must resolve from the selected Docker-host state"
    assert installer_state_root == selected_state_text, (
        "fs-cli must retain the selected Docker-host state identity for nested Compose and locks"
    )


def test_shell_compose_helper_parses_stack_env_without_sourcing_it(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scripts_dir = root / "core" / "entrypoints" / "cli" / "scripts"
    scripts_dir.mkdir(parents=True)
    helper = scripts_dir / "docker_compose_env.sh"
    helper.write_bytes((ROOT / "core/entrypoints/cli/scripts/docker_compose_env.sh").read_bytes())

    marker = tmp_path / "stack-env-was-executed"
    (root / ".flow-steward").mkdir()
    (root / ".flow-steward" / "compose.env").write_text(
        "\n".join(
            [
                "FS_COMPOSE_PROJECT_NAME=task-owned-project",
                "FS_COMPOSE_PROFILES=storage-seaweedfs cli development test audit quality",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.yaml",
                f"UNSUPPORTED=$(touch {marker})",
                "",
            ]
        ),
        encoding="utf-8",
    )

    command = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(helper))}",
            'printf "project=%s\\n" "${FS_COMPOSE_PROJECT_NAME}"',
            'printf "profiles=%s\\n" "${FS_COMPOSE_PROFILES}"',
            'printf "unsupported=%s\\n" "${UNSUPPORTED-unset}"',
        ]
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "project=task-owned-project",
        "profiles=storage-seaweedfs cli development test audit quality",
        "unsupported=unset",
    ]
    assert not marker.exists()


def test_shell_secret_env_file_helper_is_opt_in_and_single_key_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scripts_dir = root / "core" / "entrypoints" / "cli" / "scripts"
    scripts_dir.mkdir(parents=True)
    helper = scripts_dir / "docker_compose_env.sh"
    helper.write_bytes((ROOT / "core/entrypoints/cli/scripts/docker_compose_env.sh").read_bytes())

    marker = tmp_path / "env-file-was-executed"
    (root / ".flow-steward").mkdir()
    (root / ".flow-steward" / "compose.env").write_text(
        "FS_ENV_FILE=ops.env\n",
        encoding="utf-8",
    )
    (root / "ops.env").write_text(
        "\n".join(
            [
                "FS_SECRET_KEY=file-secret-key",
                f"OTHER_SECRET=$(touch {marker})",
                "",
            ]
        ),
        encoding="utf-8",
    )

    command = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(helper))}",
            'printf "before_secret=%s\\n" "${FS_SECRET_KEY-unset}"',
            'printf "before_other=%s\\n" "${OTHER_SECRET-unset}"',
            "fs_export_fs_secret_key_from_env_file",
            'printf "after_secret=%s\\n" "${FS_SECRET_KEY-unset}"',
            'printf "after_other=%s\\n" "${OTHER_SECRET-unset}"',
        ]
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "")},
    )

    assert completed.stdout.splitlines() == [
        "before_secret=unset",
        "before_other=unset",
        "after_secret=file-secret-key",
        "after_other=unset",
    ]
    assert not marker.exists()


def test_shell_secret_env_file_helper_preserves_process_secret(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scripts_dir = root / "core" / "entrypoints" / "cli" / "scripts"
    scripts_dir.mkdir(parents=True)
    helper = scripts_dir / "docker_compose_env.sh"
    helper.write_bytes((ROOT / "core/entrypoints/cli/scripts/docker_compose_env.sh").read_bytes())

    (root / ".flow-steward").mkdir()
    (root / ".flow-steward" / "compose.env").write_text(
        "FS_ENV_FILE=ops.env\n",
        encoding="utf-8",
    )
    (root / "ops.env").write_text("FS_SECRET_KEY=file-secret-key\n", encoding="utf-8")

    command = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(helper))}",
            "fs_export_fs_secret_key_from_env_file",
            'printf "%s\\n" "${FS_SECRET_KEY}"',
        ]
    )
    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "FS_SECRET_KEY": "process-secret-key"},
    )

    assert completed.stdout.strip() == "process-secret-key"


def test_seaweedfs_is_profile_gated() -> None:
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )
    seaweedfs = payload["services"]["seaweedfs"]

    assert seaweedfs["profiles"] == ["storage-seaweedfs"]
    assert "-volume.max=${SEAWEEDFS_VOLUME_MAX:-100}" in seaweedfs["command"]


@pytest.mark.parametrize(
    "compose_file",
    ("docker-compose.yaml", "docker-compose.prod.yaml"),
)
def test_seaweedfs_uses_stable_single_node_identity_across_restarts(
    compose_file: str,
) -> None:
    payload = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))
    command = payload["services"]["seaweedfs"]["command"]

    assert "-ip=seaweedfs" in command
    assert "-ip.bind=0.0.0.0" in command
    assert "-master.peers=none" in command
    assert payload["services"]["seaweedfs"]["stop_grace_period"] == "30s"


@pytest.mark.parametrize(
    "compose_file",
    ("docker-compose.yaml", "docker-compose.prod.yaml"),
)
def test_seaweedfs_health_covers_the_port_the_product_uses(compose_file: str) -> None:
    """The master answers `/cluster/healthz` while the S3 gateway is still
    refusing connections, and S3 is the port every artifact read and write goes
    through. A health signal from the master alone lets dependents start against
    a gateway that is not listening, which is the failure it exists to prevent.
    Production had no check at all."""
    payload = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))
    healthcheck = payload["services"]["seaweedfs"].get("healthcheck") or {}
    probe = " ".join(healthcheck.get("test") or [])

    assert "9333/cluster/healthz" in probe, compose_file
    assert "8333" in probe, compose_file


def test_production_compose_contains_no_development_tooling() -> None:
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )
    forbidden = {
        "fs-frontend",
        "mailpit",
        "fs-test-runner",
        "fs-audit",
        "sonarqube",
        "sonar-scanner",
    }

    assert forbidden.isdisjoint(payload["services"])
    assert payload["services"]["fs-cli"]["profiles"] == ["cli"]


@pytest.mark.parametrize(
    "compose_file",
    ("docker-compose.yaml", "docker-compose.prod.yaml"),
)
def test_app_bootstrap_state_file_is_selected_by_installer_environment(
    compose_file: str,
) -> None:
    payload = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))

    assert payload["services"]["fs-app"]["environment"]["FS_AUTH_BOOTSTRAP_STATE_FILE"] == (
        "${FS_AUTH_BOOTSTRAP_STATE_FILE:-}"
    )


def test_development_distinguishes_running_and_on_demand_services() -> None:
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )
    services = payload["services"]

    assert services["fs-frontend"]["profiles"] == ["development"]
    assert services["mailpit"]["profiles"] == ["development"]
    assert services["fs-test-runner"]["profiles"] == ["test"]
    assert services["fs-audit"]["profiles"] == ["audit"]
    assert services["sonarqube"]["profiles"] == ["quality"]
    assert services["sonar-scanner"]["profiles"] == ["quality"]


def test_each_local_build_cache_has_exactly_one_writer() -> None:
    """OPS-003: two builders exporting to one cache directory raced on rename.

    `extension-dependencies` and `fs-app` build the same target from the same
    context. Both exported to build-cache/app, and a parallel `compose build`
    exited non-zero while the images themselves were fine — an advertised clean
    build that is unreliable is worse than one that is slow.
    """
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )

    writers: dict[str, list[str]] = {}
    for name, service in payload["services"].items():
        for entry in (service.get("build") or {}).get("cache_to") or []:
            destination = str(entry)
            writers.setdefault(destination, []).append(name)

    shared = {destination: names for destination, names in writers.items() if len(names) > 1}
    assert not shared, f"more than one builder writes these caches: {shared}"


def test_the_second_app_builder_still_reads_the_shared_cache() -> None:
    """Removing the writer must not cost the reuse it was there for."""
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )
    build = payload["services"]["extension-dependencies"]["build"]

    assert any("build-cache/app" in str(entry) for entry in build.get("cache_from") or [])
    assert not build.get("cache_to")


# ---------------------------------------------------------------------------
# Clean-development bootstrap
#
# `app-runtime` is a Python image with no Node in it, and it ships the built
# frontend at `assets/frontend/dist`. Compose then bind-mounts the repository
# over `/app`, which hides that directory: on a clean checkout there is no host
# `dist`, so start_server.sh finds no manifest, tries to build one, and the
# image has no npm to do it with. `set -e` kills the container, `restart:
# always` starts it again, and a first `--no-cache` build/up crash-loops with
# nothing on screen that names the cause.
# ---------------------------------------------------------------------------


def _compose() -> dict:
    return yaml.safe_load((ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8"))


def test_the_app_bind_mount_hides_the_image_frontend_build() -> None:
    """The premise of the two tests below. If this ever stops being true, the
    bootstrap service they require is no longer needed."""
    services = _compose()["services"]

    assert "../../:/app" in services["fs-app"]["volumes"]
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build /app/assets/frontend/dist"
        in dockerfile
    )


def test_existing_but_stale_frontend_dist_requires_bootstrap_rebuild(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    source = frontend / "src/main.tsx"
    manifest = frontend / "dist/.vite/manifest.json"
    source.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    source.write_text("export const version = 2;", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    os.utime(manifest, (1, 1))
    os.utime(source, (2, 2))

    assert _frontend_shell_build_required(frontend) is True

    os.utime(manifest, (3, 3))

    assert _frontend_shell_build_required(frontend) is False


def test_platform_asset_changes_require_frontend_bootstrap_rebuild(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    manifest = frontend / "dist/.vite/manifest.json"
    platform_kit = tmp_path / "platform-kit"
    platform_source = platform_kit / "index.ts"
    platform_styles = tmp_path / "platform-components.css"
    manifest.parent.mkdir(parents=True)
    platform_kit.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    platform_source.write_text("export const version = 2;", encoding="utf-8")
    platform_styles.write_text(":root {}", encoding="utf-8")
    os.utime(manifest, (1, 1))
    os.utime(platform_source, (2, 2))
    os.utime(platform_styles, (1, 1))

    assert _frontend_shell_build_required(frontend, platform_kit, platform_styles) is True

    os.utime(manifest, (3, 3))

    assert _frontend_shell_build_required(frontend, platform_kit, platform_styles) is False


def test_a_service_builds_the_host_frontend_before_the_app_starts() -> None:
    services = _compose()["services"]
    fs_app_dependencies = services["fs-app"]["depends_on"]

    assert "frontend-assets" in fs_app_dependencies, (
        "the app must not start before the asset it serves exists on the host"
    )
    assert fs_app_dependencies["frontend-assets"]["condition"] == "service_completed_successfully"


def test_the_bootstrap_service_has_node_and_writes_into_the_bind_mount() -> None:
    """Building the assets from the Python image is what fails today, so the
    bootstrap has to run in the frontend image and write where the app reads."""
    services = _compose()["services"]
    bootstrap = services["frontend-assets"]

    assert bootstrap["build"]["target"] == "frontend-development"
    assert bootstrap["volumes"] == [
        "../../assets/frontend:/host-frontend",
        "../../assets:/host-assets:ro",
    ]
    command = bootstrap["command"][2]
    assert "source /usr/local/lib/frontend_shell_freshness.sh" in command
    assert (
        "frontend_shell_build_required /host-frontend "
        "/host-assets/platform-kit /host-assets/platform-components.css"
    ) in command
    assert "mktemp -d /tmp/flow-steward-frontend-assets" in command
    assert "tar -C /host-frontend" in command
    assert "tar -C /app/assets/frontend" not in command
    assert "cp -R /host-assets/platform-kit" in command
    assert "cp /host-assets/platform-components.css" in command
    assert "ln -s /app/assets/frontend/node_modules" in command
    assert "mv /host-frontend/.dist.frontend-assets-staging /host-frontend/dist" in command
    assert bootstrap["restart"] == "no"

    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert "frontend_shell_freshness.sh /usr/local/lib/frontend_shell_freshness.sh" in dockerfile


def test_frontend_bootstrap_mounts_do_not_nest_under_a_host_bind() -> None:
    """Docker Desktop can silently omit a named volume nested below a parent
    bind mount, leaving a clean checkout without the image's Node toolchain.
    The bootstrap workspace and its host publication mount must be disjoint.
    """
    volumes = _compose()["services"]["frontend-assets"]["volumes"]
    targets = [entry.split(":", 2)[1].rstrip("/") for entry in volumes]

    nested = [
        (parent, child)
        for parent in targets
        for child in targets
        if parent != child and child.startswith(f"{parent}/")
    ]

    assert nested == []


def test_the_bootstrap_service_does_not_use_a_login_shell() -> None:
    """A login shell runs Debian's ``clear_console`` logout hook after the
    bootstrap command exits. Under the host UID used by Compose that hook can
    turn the successful prebuilt-assets path into exit code 1, which blocks
    ``fs-app`` behind ``service_completed_successfully``.
    """
    command = _compose()["services"]["frontend-assets"]["command"]

    assert command[:2] == ["bash", "-c"]


def test_the_app_startup_names_the_cause_instead_of_crash_looping() -> None:
    """The asset really is missing, so startup still fails — but under
    `restart: always` a bare "npm: command not found" is a loop that explains
    nothing, on the one screen a developer is watching during a first build."""
    script = (ROOT / "core/entrypoints/cli/scripts/start_server.sh").read_text(encoding="utf-8")

    assert "command -v npm" in script
    assert "frontend-assets" in script, "the message must name the service that fixes it"


@pytest.mark.parametrize(
    "compose_file", ["infra/docker/docker-compose.prod.yaml", "infra/docker/docker-compose.yaml"]
)
def test_the_storage_image_keeps_the_user_it_chose_for_itself(compose_file: str) -> None:
    """AC17-R116, tenth obstacle, and the correction of my own ninth answer.

    The SeaweedFS entrypoint starts as root, fixes the ownership of a mounted /data
    for its `seaweed` user, and only then drops privileges with su-exec. Setting
    `user:` on the service makes `id -u` non-zero and skips that branch entirely, so
    the image's own remedy is disabled and the service cannot write to its volume --
    Docker initializes a new named volume from the directory inside the image, not
    from `user:`, which was measured on the image itself.

    The shape is worth naming because it is not the one the previous rounds had: not
    "a layer changed and its neighbour was not told" but "a layer was replaced by our
    own solution without noticing it had one". Reading a third-party image's
    entrypoint before overriding its user is the check that would have caught it.
    """
    compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    seaweedfs = compose["services"]["seaweedfs"]

    assert "user" not in seaweedfs, (
        "seaweedfs must run as the user its own entrypoint selects; overriding it "
        "skips the branch that fixes /data ownership and drops privileges"
    )


@pytest.mark.parametrize(
    "compose_file", ["infra/docker/docker-compose.prod.yaml", "infra/docker/docker-compose.yaml"]
)
def test_the_owner_only_storage_identity_is_handed_over_rather_than_shared(
    compose_file: str,
) -> None:
    """The problem `user:` was solving, solved without disabling the image.

    The identity file holds S3 credentials and is written by the installer as the
    operator, 0600; storage_identity.py refuses it if any group or other bit is set,
    so it cannot simply be made readable. A root step therefore installs one copy for
    the user the server actually runs as, and the server reads that copy.

    Every part of this is checked from the file rather than remembered: which service
    consumes a config, where that config lives, and that something producing it runs
    before it as root. The owner must be asked of the image rather than written down,
    because a literal uid here would be right until the image changed it.
    """
    compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    services = compose["services"]
    seaweedfs = services["seaweedfs"]

    configured = re.search(r"-s3\.config=(\S+)", str(seaweedfs.get("command") or ""))
    assert configured, "seaweedfs must be given an S3 identity file; without one it accepts anyone"
    config_path = configured.group(1)

    mounted = {
        item.split(":")[1]: item.split(":")[0]
        for item in seaweedfs.get("volumes") or []
        if isinstance(item, str) and item.count(":") >= 1
    }
    holder = next(
        (volume for path, volume in mounted.items() if config_path.startswith(path.rstrip("/"))),
        None,
    )
    assert holder in (compose.get("volumes") or {}), (
        f"{config_path} must come from a named volume this compose file declares, not "
        "from a host path whose ownership we cannot choose"
    )

    gates = {
        name
        for name, condition in (seaweedfs.get("depends_on") or {}).items()
        if isinstance(condition, dict)
        and condition.get("condition") == "service_completed_successfully"
    }
    producers = [
        name
        for name in gates
        if any(
            isinstance(item, str) and item.split(":")[0] == holder
            for item in (services[name].get("volumes") or [])
        )
    ]
    assert producers, f"nothing runs before seaweedfs to put an identity into {holder}"

    for name in producers:
        producer = services[name]
        assert str(producer.get("user") or "") == "0:0", (
            f"{name} has to choose the owner of a file, which only root can do"
        )
        assert producer.get("profiles") == seaweedfs.get("profiles"), (
            f"{name} and seaweedfs must share a profile, or the step is absent exactly "
            "when the profile that needs it is selected"
        )
        command = "".join(producer.get("command") or [])
        assert "id -u seaweed" in command, (
            f"{name} must ask the image who its user is; a literal uid would be correct "
            "only until the image changed it"
        )
        assert "0600" in command, (
            f"{name} must keep the copy owner-only; the original is 0600 for a reason"
        )


def test_everything_writing_to_the_shared_log_directory_runs_as_the_same_user() -> None:
    """AC17-R116, eleventh obstacle: the worker crash-looped on its own log file.

    The host log directory is a single bind mount shared by four services and owned by
    the operator. While FS_HOST_UID was unset they all defaulted to the image's own
    1000 and agreed by accident; recording the operator's real uid split them in half,
    and on a host whose operator is not 1000 two of the four could no longer write.

    Derived from the mount rather than from the list of services: whoever shares a
    writable host directory has to be the same user, because the directory has exactly
    one owner. Adding a fifth writer with a different user fails here rather than in a
    restart loop. The uid is not asserted -- only that there is one of it.
    """
    compose = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    # Splitting the mount on ":" does not work here: the source is
    # `${FS_LOGS_DIR:-../../logs}`, whose own default contains a colon, so a naive
    # split puts "-../../logs}" where the container path should be. Found by this
    # guard failing vacuously on its first run rather than by reading it.
    writers = {}
    for name, spec in services.items():
        for item in spec.get("volumes") or []:
            if not isinstance(item, str):
                continue
            match = re.search(r":(/app/logs)(?::(\w+))?$", item)
            if match and match.group(2) != "ro":
                writers[name] = str(spec.get("user") or "")
    assert len(writers) >= 2, "fewer than two services share the log directory; test is vacuous"

    users = set(writers.values())
    assert len(users) == 1, (
        "services sharing the host log directory disagree about who they run as, and "
        f"the directory has one owner: {dict(sorted(writers.items()))}"
    )
    assert users != {""}, (
        "the shared log directory is written by services that take whatever user their "
        "image chose; pin them to the operator who owns the directory"
    )


@pytest.mark.parametrize(
    "compose_file", ["infra/docker/docker-compose.prod.yaml", "infra/docker/docker-compose.yaml"]
)
def test_every_service_running_as_the_operator_has_a_writable_home(compose_file: str) -> None:
    """A uid absent from the image's /etc/passwd has no home to resolve.

    Four of the seven production services that override their user declared
    `HOME: /tmp` and three did not, with no comment either way -- the same shape as
    the shared log directory an hour earlier: an asymmetry that reads as a decision
    to whoever finds it next.

    Both compose files, and that is the correction rather than a detail. The first
    version of this guard read the production file alone, which is a list of one
    dressed as a derivation: it could not see the two development services pinned to
    the same uid without a HOME, and it was the review session that found them, not
    this test. A rule stated as derivable has to be asked of every file it governs.

    Not speculative for the dependency step in particular: `_pip_download_env()`
    builds its subprocess environment from scratch out of an allowlist that includes
    HOME, so with none set here pip runs with no HOME at all and resolves `~` through
    a passwd lookup that has no entry for this uid. Said precisely because the obvious
    reading -- that something sets it to `/` -- sends the next reader looking for a
    line that does not exist. The same file's frontend build shells out to npm, which
    reads $HOME/.npmrc and writes $HOME/.npm.
    """
    compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))

    operator_services = {
        name: spec
        for name, spec in compose["services"].items()
        if "FS_HOST_UID" in str(spec.get("user") or "")
    }
    assert len(operator_services) >= 2, (
        f"{compose_file} shows fewer than two services pinned to the operator; the "
        "parse is broken rather than the file being clean"
    )

    missing = sorted(
        name
        for name, spec in operator_services.items()
        if not str((spec.get("environment") or {}).get("HOME") or "").strip()
    )
    assert not missing, (
        f"in {compose_file}, {missing} run as a uid the image does not know and are "
        "given no HOME, so anything resolving ~ under them has nowhere to write"
    )


_EXTENSION_STEP_MARKER = "core/application/orchestration/connector_grants.py"


def _roles_that_run_extension_steps() -> set[str]:
    """Role images carrying the module that mints artifact grants."""
    import json

    roles = set()
    for manifest in sorted((ROOT / "infra/docker/source-slices").glob("*.json")):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if _EXTENSION_STEP_MARKER in set(payload["static_paths"]):
            roles.add(manifest.stem)
    return roles


def test_every_role_that_can_mint_an_artifact_grant_knows_where_to_serve_it() -> None:
    """AC17-R123: the delivery has two sides and each covers what the other cannot.

    A role that runs extension steps may have to hand one a URL for an artifact this
    deployment's storage cannot presign, and the platform serves that URL itself -- so
    the role has to know its own address. Which roles those are is read from the
    slices rather than listed: `connector_grants.py` is in app, worker and
    compact and not in control, so control is correctly left out without anyone
    deciding that by hand.

    Compose alone would ship this working on Scalable and silently missing on the
    platform deployments, which is the shape of three findings in one day. The
    supervisor alone would be the mirror: `process_spec.py` ships only in the compact
    image, so on Scalable it does not exist at all.
    """
    roles = _roles_that_run_extension_steps()
    assert {"app", "worker", "compact"} <= roles, roles
    assert "control" not in roles

    supervisor = (ROOT / "core/infrastructure/runtime/compact/process_spec.py").read_text(
        encoding="utf-8"
    )
    assert supervisor.count('"FS_APP_INTERNAL_URL"') >= 3, (
        "the Compact supervisor must give the address to the app and worker roles as "
        "well as to mcp; Railway runs this image and has no compose to fall back on"
    )

    for compose_file in (
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/docker-compose.yaml",
    ):
        compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
        told = {
            name
            for name, spec in compose["services"].items()
            if str((spec.get("environment") or {}).get("FS_APP_INTERNAL_URL") or "").strip()
        }
        assert {"fs-app", "fs-worker"} <= told, (
            f"{compose_file}: {sorted({'fs-app', 'fs-worker'} - told)} run extension steps "
            "and are not told the address the platform serves artifacts from"
        )


# ---------------------------------------------------------------------------
# Local build cache: stays in the checkout and is pruned after every build
# ---------------------------------------------------------------------------

_COMPOSE_ENV_HELPER = ROOT / "core/entrypoints/cli/scripts/docker_compose_env.sh"


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's own tags (`!override`, `!reset`) as their plain values."""


_ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, _suffix, node: (
        loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_scalar(node)
    ),
)


def _local_cache_paths() -> list[tuple[str, str, str]]:
    """(compose file, service, path) for every local BuildKit cache import or export."""
    found = []
    for compose_file in sorted((ROOT / "infra/docker").rglob("*.y*ml")):
        payload = yaml.load(compose_file.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}
        for name, service in (payload.get("services") or {}).items():
            build = (service or {}).get("build") or {}
            if not isinstance(build, dict):
                continue
            for entry in [*(build.get("cache_from") or []), *(build.get("cache_to") or [])]:
                fields = dict(part.split("=", 1) for part in str(entry).split(",") if "=" in part)
                if fields.get("type") == "local":
                    path = fields.get("src") or fields.get("dest") or ""
                    found.append((compose_file.relative_to(ROOT).as_posix(), name, path))
    return found


def test_every_local_build_cache_lives_in_the_pruned_root_inside_the_checkout() -> None:
    """BuildKit resolves a relative cache path against the caller's working directory.

    The old default `../../.flow-steward` therefore wrote two directories above
    the repository — the user's home for ~/www/<repo> — where nothing pruned it,
    and 74 GB of unreferenced layers piled up (2026-10-01). Every cache must sit
    under `<state>/build-cache/`, the root `fs_prune_build_cache` cleans.
    """
    paths = _local_cache_paths()
    assert paths, "the dev Compose file is expected to declare local build caches"
    misplaced = [
        (compose_file, service, path)
        for compose_file, service, path in paths
        if not path.startswith("${FS_INSTALLER_STATE_DIR:-.flow-steward}/build-cache/")
    ]
    assert not misplaced, f"local build caches outside <state>/build-cache: {misplaced}"


def _write_cache(cache: Path, *, orphan: bytes) -> str:
    """One referenced blob and one old unreferenced one; returns the orphan's digest."""
    blobs = cache / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    kept = hashlib.sha256(b"kept").hexdigest()
    (blobs / kept).write_bytes(b"kept")
    (cache / "index.json").write_text(json.dumps({"manifests": [{"digest": f"sha256:{kept}"}]}))
    digest = hashlib.sha256(orphan).hexdigest()
    (blobs / digest).write_bytes(orphan)
    old = time.time() - 7200
    os.utime(blobs / digest, (old, old))
    return digest


def _run_fs_compose(tmp_path: Path, *args: str, state_dir: str | None) -> tuple[int, str]:
    """Run `fs_compose <args>` against a stub `docker` that records what it was given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    seen = tmp_path / "docker-saw-state-dir"
    stub = bin_dir / "docker"
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$FS_INSTALLER_STATE_DIR" > "{seen}"\nexit 3\n')
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FS_COMPOSE_PROJECT_NAME": "t"}
    env.pop("FS_INSTALLER_STATE_DIR", None)
    if state_dir is not None:
        env["FS_INSTALLER_STATE_DIR"] = state_dir
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && fs_compose "${@:2}"', "t", str(_COMPOSE_ENV_HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, seen.read_text() if seen.exists() else ""


def test_a_compose_build_prunes_the_cache_and_keeps_the_command_status(tmp_path: Path) -> None:
    state = tmp_path / "state"
    orphan = _write_cache(state / "build-cache" / "audit", orphan=b"replaced layer")

    status, docker_state_dir = _run_fs_compose(tmp_path, "build", "fs-audit", state_dir=str(state))

    assert status == 3, "the prune must not hide Compose's own exit status"
    assert docker_state_dir == str(state)
    assert not (state / "build-cache" / "audit" / "blobs" / "sha256" / orphan).exists()


def test_a_compose_command_that_cannot_build_does_not_prune(tmp_path: Path) -> None:
    state = tmp_path / "state"
    orphan = _write_cache(state / "build-cache" / "audit", orphan=b"replaced layer")

    _run_fs_compose(tmp_path, "ps", state_dir=str(state))

    assert (state / "build-cache" / "audit" / "blobs" / "sha256" / orphan).exists()


def test_compose_receives_an_absolute_state_dir_inside_the_checkout(tmp_path: Path) -> None:
    _status, docker_state_dir = _run_fs_compose(tmp_path, "ps", state_dir=None)

    assert docker_state_dir == str(ROOT / ".flow-steward")
