"""Item 5, step 4: ./install.sh --deployment-profile switches an installed system.

The switch is the lifecycle's `config apply`, run in the Scalable CLI container; the
installer prepares what apply does not: the target images, the host trees in the shared
model (Scalable to Compact), a Scalable CLI container (Compact to Scalable), and the
pending change with the data kept where it is.
The lifecycle lock is lent to the apply for its duration and taken back.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.installer import InstallConfig, profile_switch
from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_COMPACT_IMAGE = "sha256:" + "c" * 64


class _Docker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[tuple[str, ...]] = []
        self.lock_free_during_apply: bool | None = None
        self.apply_exit = 0
        self.rendered: list[tuple[str, ...]] = []
        self.render_exit = 0
        self.render_stderr = ""
        self.probe_exit = 0
        self.render_requires: tuple[str, ...] = ()
        self.left: dict[str, int] = {}  # containers of each source service after the apply
        self.stopped_before: dict[str, int] = {}  # stopped leftovers of an earlier switch
        self.render_environments: list[dict[str, str] | None] = []
        # (compose project, container name, bind sources) of every container on the host
        self.containers: list[tuple[str, str, tuple[str, ...]]] = []

    def run(self, command, **kwargs):
        key = tuple(command)
        self.commands.append(key)
        if "shared_trees" in " ".join(key):
            report = {"trees": [{"summary": "Normalized 1 directories and 2 files under x."}]}
            return ProcessOutcome(command=key, returncode=0, stdout=json.dumps(report))
        if any("dst=/run/flow-steward-tree-probe" in part for part in key):
            return ProcessOutcome(command=key, returncode=self.probe_exit)
        if key[:3] == ("docker", "ps", "--all") and "--filter" in key:
            service = next(
                part.rsplit("=", 1)[1]
                for part in key
                if part.startswith("label=com.docker.compose.service=")
            )
            left = self.stopped_before if "status=exited" in key else self.left
            ids = "".join(f"{service}-{index}\n" for index in range(left.get(service, 0)))
            return ProcessOutcome(command=key, returncode=0, stdout=ids)
        if key[:4] == ("docker", "ps", "--all", "--quiet"):
            ids = "".join(f"id{index}\n" for index in range(len(self.containers)))
            return ProcessOutcome(command=key, returncode=0, stdout=ids)
        if key[:2] == ("docker", "inspect"):
            lines = [
                "\t".join(
                    (
                        project,
                        "/" + name,
                        json.dumps([{"Type": "bind", "Source": source} for source in sources]),
                    )
                )
                for project, name, sources in self.containers
            ]
            return ProcessOutcome(command=key, returncode=0, stdout="\n".join(lines) + "\n")
        if key[-1] == "/data/state/worker-mcp-token":
            return ProcessOutcome(command=key, returncode=0, stdout="runtime-token\n")
        if key[-2:] == ("config", "--quiet"):
            self.rendered.append(key)
            environment = kwargs.get("environment")
            self.render_environments.append(environment)
            missing = [name for name in self.render_requires if name not in (environment or {})]
            if missing:
                stderr = "".join(
                    f"required variable {name} is missing a value\n" for name in missing
                )
                return ProcessOutcome(command=key, returncode=15, stderr=stderr)
            return ProcessOutcome(
                command=key, returncode=self.render_exit, stderr=self.render_stderr
            )
        if key[-3:] == ("config", "apply", "--confirm"):
            try:
                with LifecycleStateStore(self.root).operation_lock("apply-1", mutating=True):
                    self.lock_free_during_apply = True
            except Exception:
                self.lock_free_during_apply = False
            return ProcessOutcome(command=key, returncode=self.apply_exit, stderr="apply broke")
        return ProcessOutcome(command=key, returncode=0)


def _gateway(root: Path, docker: _Docker, monkeypatch: pytest.MonkeyPatch):
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    for tree in ("logs/artifacts", "extensions"):
        (root / tree).mkdir(parents=True, exist_ok=True)
    (root / ".env.prod").write_text(f"FS_COMPACT_IMAGE={_COMPACT_IMAGE}\n", encoding="utf-8")
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(
            root / ".flow-steward", lifecycle_state_root=root / ".flow-steward"
        ),
        process=docker,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )
    prepared: list[str] = []
    monkeypatch.setattr(
        gateway, "prepare_core", lambda config, *, log: prepared.append(config.deployment_profile)
    )
    monkeypatch.setattr(
        gateway,
        "ensure_cli_runtime",
        lambda config, *, log: prepared.append("cli:" + config.deployment_profile),
    )
    monkeypatch.setattr(
        gateway,
        "_cli_runtime_context",
        lambda config, *, log: SimpleNamespace(
            compose_prefix=("docker", "compose", "-p", "fs"), environment={}
        ),
    )
    return gateway, prepared


def _log(gateway: DockerInstallerGateway):
    return gateway.state_store.open_log(f"switch-{uuid4().hex}")


_SCALABLE = InstallConfig(environment="production", file_storage="local_fs")


def test_scalable_to_compact_keeps_the_trees_and_lends_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, prepared = _gateway(root, docker, monkeypatch)
    target = profile_switch.switch_target(_SCALABLE, "compact")

    with gateway.state_store.operation_lock():
        changes = profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))
        # taken back once the apply returned
        with (
            pytest.raises(Exception, match=r"install\.sh is running"),
            LifecycleStateStore(root).operation_lock("other", mutating=True),
        ):
            pass

    assert target.data_placement == "host_directories"
    assert prepared == ["compact"]
    normalize = next(command for command in docker.commands if "shared_trees" in " ".join(command))
    assert (
        f"type=bind,src={(root / 'logs/artifacts').resolve()},dst=/run/flow-steward-trees/artifacts"
        in normalize
    )
    assert "type=bind,src=/etc/group,dst=/run/flow-steward-host-group,readonly" in normalize
    assert _COMPACT_IMAGE in normalize
    env = (root / ".env.prod").read_text(encoding="utf-8")
    assert f"FS_LOGS_DIR={(root / 'logs').resolve()}" in env  # absolute, for the overlay
    pending = json.loads((root / ".flow-steward/pending-config.json").read_text(encoding="utf-8"))
    assert pending["deployment_profile"] == "compact"
    assert pending["data_placement"] == "host_directories"
    assert docker.lock_free_during_apply is True
    assert changes[0].startswith("Normalized ")
    assert changes[-1].startswith("Switched from scalable to compact")


def test_compact_to_scalable_prepares_a_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, prepared = _gateway(root, docker, monkeypatch)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    target = profile_switch.switch_target(compact, "scalable")

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, compact, "scalable", log=_log(gateway))

    assert target.data_placement == "compact_volume"
    assert prepared == ["scalable", "cli:scalable"]
    assert not any("install -d" in " ".join(command) for command in docker.commands)
    assert not any("shared_trees" in " ".join(command) for command in docker.commands)


def test_a_bundled_storage_installation_switches_with_its_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 5: SeaweedFS runs beside Compact on the same volumes, so the switch no longer
    stops at bundled storage, and the pending change leaves the storage as it is."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, prepared = _gateway(root, docker, monkeypatch)
    bundled = InstallConfig(environment="production", file_storage="bundled")

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, bundled, "compact", log=_log(gateway))

    assert prepared == ["compact"]
    pending = json.loads((root / ".flow-steward/pending-config.json").read_text(encoding="utf-8"))
    assert "file_storage" not in pending


def test_a_failed_apply_is_named_and_the_lock_is_taken_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.apply_exit = 8
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    with gateway.state_store.operation_lock():
        operation_log = _log(gateway)
        with pytest.raises(InstallerFailure) as failed:
            profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=operation_log)
        with (
            pytest.raises(Exception, match=r"install\.sh is running"),
            LifecycleStateStore(root).operation_lock("other", mutating=True),
        ):
            pass

    assert failed.value.code == "profile_switch_apply_failed"
    assert "apply broke" in failed.value.cause
    assert "Rerun ./install.sh to continue it" in failed.value.remediation


def test_compact_to_scalable_restores_the_worker_credential_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R165: Scalable's compose requires FS_WORKER_MCP_BEARER_TOKEN for every command,
    and on Compact the installer's copy is empty (AC17-R142) -- the first live switch
    failed at its first Scalable call."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(gateway, "prepare_core", lambda config, *, log: order.append("prepare"))
    real_update = gateway.state_store.update_env_values

    def recording_update(path, values):
        if "FS_WORKER_MCP_BEARER_TOKEN" in values:
            order.append("token")
        return real_update(path, values)

    monkeypatch.setattr(gateway.state_store, "update_env_values", recording_update)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, compact, "scalable", log=_log(gateway))

    assert order[:2] == ["token", "prepare"]
    assert "FS_WORKER_MCP_BEARER_TOKEN=runtime-token" in (root / ".env.prod").read_text()
    assert "runtime-token" not in "".join(
        path.read_text() for path in (root / ".flow-steward/logs").glob("*.log")
    )
    assert docker.rendered  # the Scalable configuration was rendered before any change


def test_a_target_that_does_not_render_is_refused_by_name_before_any_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.render_exit = 15
    docker.render_stderr = (
        "error while interpolating services.fs-cli.image: required variable FS_CLI_IMAGE "
        "is missing a value: FS_CLI_IMAGE is required\n"
        "required variable MCP_IMAGE_REVISION is missing a value\n"
    )
    gateway, prepared = _gateway(root, docker, monkeypatch)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    operation_log = _log(gateway)
    with gateway.state_store.operation_lock(), pytest.raises(InstallerFailure) as refused:
        profile_switch.switch_profile(gateway, compact, "scalable", log=operation_log)

    assert refused.value.code == "profile_switch_target_incomplete"
    assert "FS_CLI_IMAGE, MCP_IMAGE_REVISION" in refused.value.cause
    assert "cli:scalable" not in prepared  # nothing started
    assert not (root / ".flow-steward/pending-config.json").exists()
    assert not any(key[-3:] == ("config", "apply", "--confirm") for key in docker.commands)


def test_after_a_switch_to_compact_the_installers_credential_copy_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R142 on the way back: the runtime owns the identity on Compact."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    (root / ".env.prod").write_text(
        f"FS_COMPACT_IMAGE={_COMPACT_IMAGE}\nFS_WORKER_MCP_BEARER_TOKEN=scalable-token\n",
        encoding="utf-8",
    )

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    assert "scalable-token" not in (root / ".env.prod").read_text(encoding="utf-8")


def test_a_failed_switch_to_scalable_removes_the_cli_container_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R166 acceptance: after the failure a Scalable fs-cli ran on on the Compact
    installation."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    def cli_fails(config, *, log):
        raise InstallerFailure(
            code="cli_runtime_unavailable",
            cause="mount contract",
            remediation="",
            exit_code=8,
        )

    monkeypatch.setattr(gateway, "ensure_cli_runtime", cli_fails)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    operation_log = _log(gateway)
    with gateway.state_store.operation_lock(), pytest.raises(InstallerFailure):
        profile_switch.switch_profile(gateway, compact, "scalable", log=operation_log)

    assert any(key[-3:] == ("rm", "-sf", "fs-cli") for key in docker.commands)


def test_a_resumed_switch_on_compact_starts_the_scalable_cli_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, prepared = _gateway(root, docker, monkeypatch)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    with gateway.state_store.operation_lock():
        profile_switch.resume_switch(
            gateway, compact, {"deployment_profile": "scalable"}, log=_log(gateway)
        )

    assert prepared == ["cli:scalable"]
    assert any(key[-3:] == ("config", "apply", "--confirm") for key in docker.commands)


def _switched_back(root: Path) -> InstallConfig:
    """A Compact installation switched to Scalable: its data is in the Compact volume."""
    return profile_switch.switch_target(
        InstallConfig(
            environment="production", deployment_profile="compact", file_storage="local_fs"
        ),
        "scalable",
    )


def test_back_to_compact_from_the_volume_touches_no_host_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R168: the way back from a C->S switch refused on a host directory that is
    not this installation's data at all, which lives in the volume."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, prepared = _gateway(root, docker, monkeypatch)
    (root / "extensions").rmdir()  # not there, and not needed
    scalable = _switched_back(root)
    assert scalable.data_placement == "compact_volume"

    with gateway.state_store.operation_lock():
        changes = profile_switch.switch_profile(gateway, scalable, "compact", log=_log(gateway))

    joined = [" ".join(command) for command in docker.commands]
    assert not any("shared_trees" in command or "tree-probe" in command for command in joined)
    assert "FS_LOGS_DIR" not in (root / ".env.prod").read_text(encoding="utf-8")
    pending = json.loads((root / ".flow-steward/pending-config.json").read_text(encoding="utf-8"))
    assert pending["deployment_profile"] == "compact"
    assert prepared == ["compact"]
    assert changes == (
        "Switched from scalable to compact; the data stayed where it was "
        "(the Compact data volume).",
    )


def test_a_host_tree_outside_the_checkout_is_checked_where_docker_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R168: the installer's container mounts the checkout only; a tree configured
    beside it, such as ../../../shared-extensions, is invisible to the installer. Docker
    sees it; the installer does not."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    beside = root.parent / "shared-extensions"  # not created: there for Docker only
    with (root / ".env.prod").open("a", encoding="utf-8") as env_file:
        env_file.write(f"FS_EXTENSIONS_DIR={beside}\n")

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    probes = [c for c in docker.commands if any("tree-probe" in part for part in c)]
    assert len(probes) == 2
    parent = f"type=bind,src={root.parent.resolve()},dst=/run/flow-steward-tree-probe,readonly"
    assert any(parent in probe and probe[-1] == "shared-extensions" for probe in probes)


def test_a_host_tree_that_is_not_a_real_directory_is_refused_before_any_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.probe_exit = 1
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    operation_log = _log(gateway)
    with gateway.state_store.operation_lock(), pytest.raises(InstallerFailure) as refused:
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=operation_log)

    assert refused.value.code == "profile_switch_tree_missing"
    assert not any("shared_trees" in " ".join(command) for command in docker.commands)
    assert not (root / ".flow-steward/pending-config.json").exists()


def test_a_host_tree_another_installation_mounts_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R168 risk: a tree configured outside the checkout can be shared by every
    installation beside it; the normalization would change its group and modes under the
    neighbour too."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    shared = str((root.parent / "shared-extensions").resolve())
    docker.containers = [("fs-ext", "fs-ext-fs-app-1", (shared,))]
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    with (root / ".env.prod").open("a", encoding="utf-8") as env_file:
        env_file.write(f"FS_EXTENSIONS_DIR={shared}\n")

    operation_log = _log(gateway)
    with gateway.state_store.operation_lock(), pytest.raises(InstallerFailure) as refused:
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=operation_log)

    assert refused.value.code == "profile_switch_tree_shared"
    assert shared in refused.value.cause
    assert "fs-ext" in refused.value.cause
    assert "FS_EXTENSIONS_DIR" in refused.value.remediation
    assert not any("shared_trees" in " ".join(command) for command in docker.commands)
    assert "FS_LOGS_DIR" not in (root / ".env.prod").read_text(encoding="utf-8")


def test_what_mounts_a_tree_from_this_installation_or_from_above_is_not_a_neighbour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green direction: this project's own containers, and another project's mount of
    a directory above the tree, do not refuse."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    own = gateway._project_identity()
    docker.containers = [
        (own, f"{own}-fs-app-1", (str((root / "extensions").resolve()),)),
        ("fs-ext", "fs-ext-fs-cli-1", (str(root.parent.resolve()),)),
    ]

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    assert any("shared_trees" in " ".join(command) for command in docker.commands)


def test_a_bundled_storage_installation_has_no_host_artifacts_to_ask_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R169: fs-ext on bundled storage has no logs/artifacts -- its artifacts are in
    SeaweedFS -- and the switch to Compact refused on the missing directory."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    (root / "logs/artifacts").rmdir()
    bundled = InstallConfig(environment="production", file_storage="bundled")

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, bundled, "compact", log=_log(gateway))

    probes = [c for c in docker.commands if any("tree-probe" in part for part in c)]
    assert sorted(probe[-1] for probe in probes) == ["extensions"]
    normalize = next(command for command in docker.commands if "shared_trees" in " ".join(command))
    assert not any("artifacts" in part for part in normalize)
    assert "FS_LOGS_DIR" not in (root / ".env.prod").read_text(encoding="utf-8")


_DURABLE_SECRETS = (
    'FS_SECRET_KEY="keep fs secret"',
    'ENCRYPTION_KEY="keep encryption key"',
    'FS_BACKUP_ENCRYPTION_KEY="keep backup key"',
    'POSTGRES_PASSWORD="keep postgres password"',
    'RABBITMQ_PASS="keep rabbit password"',
    'FS_METRICS_TOKEN="keep metrics token"',
    'SECURITY_PII_SALT="keep pii salt"',
)


def test_the_target_renders_with_what_the_apply_will_write_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R170: back from C->S the render refused on FS_WORKER_PROCESSES -- a Scalable
    .env.prod has no use for it, and the apply writes it for Compact itself."""
    from core.application.lifecycle.contracts import DeploymentConfig
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.render_requires = ("FS_WORKER_PROCESSES", "FS_RUNTIME_HTTP_PORT")
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    (root / ".env.prod").write_text(
        "\n".join(
            (
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                *_DURABLE_SECRETS,
                "# END FLOW STEWARD INSTALLER MANAGED",
                f"FS_COMPACT_IMAGE={_COMPACT_IMAGE}",
                "",
            )
        ),
        encoding="utf-8",
    )
    store = LifecycleConfigurationStore(root, state_root=root / ".flow-steward")
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    store.write_active(DeploymentConfig.model_validate(_SCALABLE.model_dump(mode="json")))

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    (environment,) = docker.render_environments
    assert environment is not None
    assert environment["FS_WORKER_PROCESSES"] == "2"
    # AC17-R174: rendered exactly as the apply renders -- never from the installed file
    (render,) = docker.rendered
    assert render[render.index("--env-file") + 1] == "/dev/null"
    assert environment["FS_DEPLOYMENT_PROFILE"] == "compact"
    assert "FS_WORKER_PROCESSES" not in (root / ".env.prod").read_text(encoding="utf-8")


def _removed(docker: _Docker) -> list[str]:
    return sorted(
        part for key in docker.commands if key[:3] == ("docker", "rm", "-f") for part in key[3:]
    )


def test_after_a_switch_to_compact_no_scalable_container_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live on fs-ac19-bench: after S->C the Scalable fs-cli -- a management container
    with the Docker socket -- still ran on the Compact installation, and the five
    Scalable services were left exited. Removed now; the volumes stay (no -v)."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.left = {
        "fs-app": 1,
        "fs-worker": 2,
        "fs-mcp": 1,
        "control-plane": 1,
        "fs-extension-installer": 1,
        "fs-cli": 1,
        "pg-fs": 1,
        "traefik": 1,
    }
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    assert _removed(docker) == [
        "control-plane-0",
        "fs-app-0",
        "fs-cli-0",
        "fs-extension-installer-0",
        "fs-mcp-0",
        "fs-worker-0",
        "fs-worker-1",
    ]  # never the database, the broker or the shared ingress
    assert not any("-v" in key for key in docker.commands if key[:2] == ("docker", "rm"))


def test_after_a_switch_to_scalable_the_compact_runtime_container_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.left = {"compact-runtime": 1, "fs-cli": 1, "pg-fs": 1}
    gateway, _prepared = _gateway(root, docker, monkeypatch)
    compact = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, compact, "scalable", log=_log(gateway))

    assert _removed(docker) == ["compact-runtime-0"]  # Scalable's own fs-cli stays


def test_a_failed_switch_leaves_the_source_containers_for_its_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.apply_exit = 8
    docker.left = {"fs-app": 1}
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    operation_log = _log(gateway)
    with gateway.state_store.operation_lock(), pytest.raises(InstallerFailure):
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=operation_log)

    assert _removed(docker) == []


def test_a_stopped_leftover_of_an_earlier_switch_goes_before_the_apply_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R171 (fs-r123): C->S at one revision left a stopped compact-runtime; after an
    update the way back recorded it as a container to keep and refused on its image."""
    root = tmp_path / "repo"
    docker = _Docker(root)
    docker.stopped_before = {"compact-runtime": 1, "pg-fs": 1}
    gateway, _prepared = _gateway(root, docker, monkeypatch)

    with gateway.state_store.operation_lock():
        profile_switch.switch_profile(gateway, _SCALABLE, "compact", log=_log(gateway))

    removal = next(
        index
        for index, key in enumerate(docker.commands)
        if key == ("docker", "rm", "-f", "compact-runtime-0")
    )
    apply = next(
        index
        for index, key in enumerate(docker.commands)
        if key[-3:] == ("config", "apply", "--confirm")
    )
    assert removal < apply  # gone before the apply captures what it must keep
    queried = [key for key in docker.commands if "status=exited" in key]
    assert [
        part
        for key in queried
        for part in key
        if part.startswith("label=com.docker.compose.service=")
    ] == [
        "label=com.docker.compose.service=compact-runtime"
    ]  # only the target's own services; never the shared ones
