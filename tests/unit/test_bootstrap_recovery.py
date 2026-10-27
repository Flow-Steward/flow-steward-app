"""Synthetic recovery uses real state/configuration writers and fake process boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.management.errors import ServicePreconditionError
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.public_probe import BufferedPublicResponse
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore


def recovery_fixture(root, *, environment="development", public_host="localhost", state_root=None):
    from core.application.auth.bootstrap_recovery import BootstrapRecoveryService
    from core.application.auth.bootstrap_recovery_gateway import BootstrapRecoveryGateway

    root.mkdir(parents=True, exist_ok=True)
    state = InstallerStateStore(state_root or root / ".flow-steward")
    store = LifecycleConfigurationStore(root, state_root=state.root)
    configuration = ConfigurationService(store=store)
    config = DeploymentConfig(
        environment=environment, deployment_profile="scalable", public_host=public_host
    )
    store.write_active(config)
    env = store.env_path(config)
    env.write_text(
        "# preserve me\nFS_COMPOSE_PROJECT_NAME=recovery-test\nFS_LOCAL_AUTH_ENABLED=1\nFS_AUTH_BOOTSTRAP_ENABLED=0\n"
    )
    env.chmod(0o600)
    target = configuration.derive_target(config)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=recovery-test\n")
    for relative in target.compose_files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")
    revision = store.begin_active_publication(
        config, config, target_env_path=env, compose_values={}
    )
    store.commit_active_publication(config, revision=revision, target_env_path=env)
    state.write_bootstrap_token_state(
        token="previous-token", expires_at=datetime(2999, 1, 1, tzinfo=UTC)
    )
    state.write_secret_output("bootstrap-token", "previous-token")
    process = RecoveryProcess(root, env, state)
    installer = DockerInstallerGateway(
        repo_root=root,
        state_store=state,
        process=process,
        configuration_service=configuration,
        environment={"FS_COMPOSE_PROJECT_NAME": "recovery-test"},
        docker_cli_available=lambda: True,
        sleep=lambda _: None,
    )
    # The synthetic HTTP boundary has no real certificate or network endpoint.
    installer._public_tls_context = lambda _config: None
    installer._open_public_probe = process.open_public_probe
    installer._probe_bootstrap_required = lambda *a, **kw: (
        process.active.get("FS_AUTH_BOOTSTRAP_ENABLED") == "1"
    )
    selection = {
        "FS_ENV_FILE": str(env),
        "FS_COMPOSE_PROJECT_NAME": "recovery-test",
        "FS_COMPOSE_FILES": " ".join(str(root / p) for p in target.compose_files),
        "FS_COMPOSE_PROFILES": " ".join(target.compose_profiles),
        "FS_STACK_ENVIRONMENT": environment,
        "FS_INSTALLER_STATE_DIR": str(state.root),
    }
    gateway = BootstrapRecoveryGateway(installer=installer, selection=selection)
    return BootstrapRecoveryService(gateway=gateway), gateway, process, store, config


class RecoveryProcess:
    def __init__(self, root, env, state):
        self.root, self.env, self.state = root, env, state
        self.active = state.read_env_values(env)
        self.commands = []
        self.counts = [0, 0, 0]
        self.fail_activation = False
        self.fail_compensation = False
        self.activation_count = 0
        self.preflight_failure = None
        self.compose_version = "2.39.0"
        self.container_state = {"Running": True}
        self.health_by_activation = {}
        self.public_probes = []

    def open_public_probe(self, url, **kwargs):
        self.public_probes.append((self.activation_count, url))
        return BufferedPublicResponse(
            self.health_by_activation.get(self.activation_count, 200), b"{}"
        )

    def run(self, command, **kwargs):
        command = tuple(command)
        self.commands.append(command)
        text = ""
        if self.preflight_failure and command[:2] == self.preflight_failure:
            raise FileNotFoundError("synthetic Docker failure")
        if command[:3] == ("docker", "compose", "version"):
            text = self.compose_version
        elif command[:2] == ("docker", "context"):
            text = "unix:///var/run/docker.sock"
        elif command[:2] == ("docker", "info"):
            text = "audit-daemon"
        elif "--images" in command:
            text = "synthetic-app:current"
        elif command[:3] == ("docker", "image", "inspect"):
            text = "sha256:old-image"
        elif "ps" in command:
            text = "test-container"
        elif command[:3] == ("docker", "container", "inspect"):
            text = json.dumps(
                {
                    "Id": "test-container",
                    "Image": "sha256:old-image",
                    "State": self.container_state,
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "recovery-test",
                            "com.docker.compose.service": "fs-app",
                        }
                    },
                    "Mounts": [
                        {
                            "Source": str(self.state.root / "runtime"),
                            "Destination": "/run/flow-steward-runtime",
                        }
                    ],
                }
            )
        elif command[-1] == "count":
            text = json.dumps(
                {"schema_version": 1, "count": self.counts.pop(0) if self.counts else 0}
            )
        elif command[-1] == "status":
            from core.application.auth.bootstrap_queries import bootstrap_runtime_status

            text = json.dumps(
                bootstrap_runtime_status(
                    self.active, state_path=self.state.root / "runtime/bootstrap-token-state.json"
                )
            )
        elif "--force-recreate" in command:
            self.activation_count += 1
            self.active = self.state.read_env_values(self.env)
            if self.fail_activation and (self.activation_count == 1 or self.fail_compensation):
                return ProcessOutcome(
                    command, 1, stderr="synthetic activation failure containing private data"
                )
        return ProcessOutcome(command, 0, stdout=text)


def artifacts(store):
    paths = [
        store.env_path(store._read_active_file()),
        store.active_revision_path,
        store.state_root / "runtime/bootstrap-token-state.json",
        store.state_root / "secret-output/bootstrap-token",
    ]
    return {str(path): (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths}


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("public_host,local_only", [("localhost", "1"), ("flow.example.test", "0")])
def test_recovery_enables_disabled_bootstrap_and_preserves_active_publication(
    tmp_path, environment, public_host, local_only
):
    service, _gateway, process, store, config = recovery_fixture(
        tmp_path, environment=environment, public_host=public_host
    )
    result = service.issue()
    values = store.read_env_values(store.env_path(config))
    assert values["FS_AUTH_BOOTSTRAP_ENABLED"] == "1"
    assert values["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"] == local_only
    assert values["FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY"] == "0"
    assert len(values["FS_AUTH_BOOTSTRAP_TOKEN"]) >= 43
    assert store.read_active() == config
    assert process.activation_count == 1
    assert "# preserve me" in store.env_path(config).read_text()
    output = tmp_path / result.output_path
    assert output.read_text().strip() == values["FS_AUTH_BOOTSTRAP_TOKEN"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] not in result.model_dump_json()
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] not in repr(process.commands)


@pytest.mark.parametrize("counts", [[1], [False], [-1], ["0"], [0, 1]])
def test_recovery_rejects_nonzero_or_invalid_counts_before_writes(tmp_path, counts):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.counts = counts
    with pytest.raises((InstallerFailure, ServicePreconditionError)):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


@pytest.mark.parametrize("failure", ["activation", "post_count"])
def test_recovery_failure_restores_every_artifact_and_reactivates_previous_settings(
    tmp_path, failure
):
    service, _gateway, process, store, config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.fail_activation = failure == "activation"
    if failure == "post_count":
        process.counts = [0, 0, 1]
    with pytest.raises((InstallerFailure, ServicePreconditionError)):
        service.issue()
    assert artifacts(store) == before
    assert process.active["FS_AUTH_BOOTSTRAP_ENABLED"] == "0"
    assert process.activation_count == 2
    assert store.read_active() == config


def test_failed_compensation_retains_owner_only_recovery_material(tmp_path):
    service, gateway, process, _store, _config = recovery_fixture(tmp_path)
    process.fail_activation = process.fail_compensation = True
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_recovery_needed"
    assert gateway.snapshot_path.exists()
    assert gateway.snapshot_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "command", [("docker", "context"), ("docker", "compose"), ("docker", "info")]
)
def test_preflight_process_failure_changes_no_bootstrap_artifacts(tmp_path, command):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.preflight_failure = command
    with pytest.raises(InstallerFailure, match="unavailable"):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_selected_descriptor_disagreement_changes_no_artifacts(tmp_path):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    gateway.selection["FS_COMPOSE_PROJECT_NAME"] = "other-project"
    with pytest.raises(InstallerFailure, match="descriptor"):
        service.issue()
    assert artifacts(store) == before
    assert process.commands == []


def test_local_auth_disabled_is_not_enabled_by_recovery(tmp_path):
    service, gateway, process, store, config = recovery_fixture(tmp_path)
    gateway.state.update_env_values(store.env_path(config), {"FS_LOCAL_AUTH_ENABLED": "0"})
    store.refresh_active_environment(config, env_path=store.env_path(config))
    before = artifacts(store)
    with pytest.raises(InstallerFailure, match="authentication"):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


@pytest.mark.parametrize("failure", [False, True])
def test_secret_symlink_target_is_untouched_and_link_is_restored_on_failure(tmp_path, failure):
    service, gateway, process, _store, _config = recovery_fixture(tmp_path)
    output = gateway.state.root / "secret-output/bootstrap-token"
    output.unlink()
    victim = tmp_path / "victim"
    victim.write_text("do not change")
    output.symlink_to(victim)
    process.fail_activation = failure
    if failure:
        with pytest.raises(InstallerFailure):
            service.issue()
        assert output.is_symlink()
        assert output.readlink() == victim
    else:
        service.issue()
        assert not output.is_symlink()
        assert output.stat().st_mode & 0o777 == 0o600
    assert victim.read_text() == "do not change"


def test_parent_symlink_is_refused_before_publication(tmp_path):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    output_dir = gateway.state.root / "secret-output"
    external = tmp_path / "external-secrets"
    output_dir.rename(external)
    output_dir.symlink_to(external, target_is_directory=True)
    before = artifacts(store)
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_failed_second_issue_restores_first_successful_token(tmp_path):
    from core.application.auth.bootstrap_tokens import bootstrap_token_state_request_valid

    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    first = service.issue()
    before = artifacts(store)
    token = (tmp_path / first.output_path).read_text().strip()
    process.fail_activation = True
    process.activation_count = 0
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert bootstrap_token_state_request_valid(
        token, state_path=gateway.state.root / "runtime/bootstrap-token-state.json"
    ) == (True, "")


def test_second_success_replaces_first_token(tmp_path):
    from core.application.auth.bootstrap_tokens import bootstrap_token_state_request_valid

    service, gateway, _process, _store, _config = recovery_fixture(tmp_path)
    first = service.issue()
    token = (tmp_path / first.output_path).read_text().strip()
    second = service.issue()
    assert (tmp_path / second.output_path).read_text().strip() != token
    assert (
        bootstrap_token_state_request_valid(
            token, state_path=gateway.state.root / "runtime/bootstrap-token-state.json"
        )[0]
        is False
    )


@pytest.mark.parametrize("boundary", ["state", "secret", "env", "publication"])
def test_write_boundary_failure_restores_bytes_without_activation(tmp_path, monkeypatch, boundary):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    owner, method = {
        "state": (gateway.state, "write_bootstrap_token_state"),
        "secret": (gateway.state, "write_secret_output"),
        "env": (gateway.state, "update_env_values"),
        "publication": (store, "refresh_active_environment"),
    }[boundary]

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_new_artifacts_are_removed_after_failed_first_issue(tmp_path):
    service, gateway, process, store, config = recovery_fixture(tmp_path)
    secret = gateway.state.root / "secret-output/bootstrap-token"
    state = gateway.state.root / "runtime/bootstrap-token-state.json"
    secret.unlink()
    secret.parent.rmdir()
    state.unlink()
    process.fail_activation = True
    before_env = store.env_path(config).read_bytes()
    with pytest.raises(InstallerFailure):
        service.issue()
    assert not secret.exists() and not secret.parent.exists()
    assert not state.exists()
    assert store.env_path(config).read_bytes() == before_env


def test_existing_pending_config_is_preserved(tmp_path):
    service, _gateway, _process, store, _config = recovery_fixture(tmp_path)
    store.pending_path.write_text('{"public_host":"future.example.test"}')
    before = store.pending_path.read_bytes()
    service.issue()
    assert store.pending_path.read_bytes() == before


def test_distinct_selected_and_canonical_env_roles_stay_synchronized(tmp_path):
    service, gateway, process, store, config = recovery_fixture(tmp_path)
    selected = tmp_path / "custom.env"
    selected.write_text(store.env_path(config).read_text() + "SELECTED_ONLY=keep\n")
    gateway.selection["FS_ENV_FILE"] = str(selected)
    store.compose_env_path.write_text(
        f"FS_COMPOSE_PROJECT_NAME=recovery-test\nFS_ENV_FILE={selected}\n"
    )
    revision = store.begin_active_publication(
        config, config, target_env_path=store.env_path(config), compose_values={}
    )
    store.commit_active_publication(
        config, revision=revision, target_env_path=store.env_path(config)
    )
    process.env = selected
    service.issue()
    canonical = store.read_env_values(store.env_path(config))
    values = store.read_env_values(selected)
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] == canonical["FS_AUTH_BOOTSTRAP_TOKEN"]
    assert (
        values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]
        == canonical["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]
    )
    assert values["SELECTED_ONLY"] == "keep"
    assert "SELECTED_ONLY" not in canonical
    assert any(
        str(selected) in command and "--force-recreate" in command for command in process.commands
    )


def test_env_role_symlink_alias_is_written_once_and_preserved(tmp_path, monkeypatch):
    service, gateway, _process, store, config = recovery_fixture(tmp_path)
    env = store.env_path(config)
    actual = tmp_path / "actual.env"
    env.rename(actual)
    env.symlink_to(actual)
    revision = store.begin_active_publication(
        config, config, target_env_path=actual, compose_values={}
    )
    store.commit_active_publication(config, revision=revision, target_env_path=actual)
    writes = []
    original = gateway.state.update_env_values

    def update(path, values):
        writes.append(path)
        original(path, values)

    monkeypatch.setattr(gateway.state, "update_env_values", update)
    service.issue()
    assert writes == [actual]
    assert env.is_symlink()
    assert env.readlink() == actual


@pytest.mark.parametrize(
    "response",
    [
        "",
        "[]",
        '{"schema_version":1,"count":0,"extra":true}',
        '{"schema_version":true,"count":0}',
        "not-json",
    ],
)
def test_malformed_count_transport_never_grants_recovery(tmp_path, monkeypatch, response):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    original = process.run

    def run(command, **kwargs):
        if command[-1] == "count":
            return ProcessOutcome(tuple(command), 0, stdout=response)
        return original(command, **kwargs)

    monkeypatch.setattr(process, "run", run)
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_recovery_refuses_a_held_installer_lock(tmp_path):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    with gateway.state.operation_lock(), pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.commands == []


def test_state_file_bind_mount_fallback_restores_all_bytes(tmp_path, monkeypatch):
    import errno
    import os

    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    state_path = gateway.state.root / "runtime/bootstrap-token-state.json"
    replace = os.replace

    def mounted_replace(source, destination, **kwargs):
        if Path(destination) == state_path:
            raise OSError(errno.EBUSY, "synthetic file bind mount")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", mounted_replace)
    process.fail_activation = True
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert not list(state_path.parent.glob(".*.tmp"))


def test_cleanup_failure_does_not_report_issued(tmp_path, monkeypatch):
    service, gateway, process, _store, _config = recovery_fixture(tmp_path)

    def fail_finish():
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(gateway, "finish", fail_finish)
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_recovery_needed"
    assert process.activation_count == 1
    assert gateway.snapshot_path.is_dir()


def test_healthy_old_container_cannot_verify_new_disk_environment(tmp_path, monkeypatch):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    original = process.run
    previous_environment = process.active.copy()

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if "--force-recreate" in command and process.activation_count == 1:
            process.active = previous_environment
        return result

    monkeypatch.setattr(process, "run", run)
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_token_activation_failed"
    assert process.activation_count == 2
    assert artifacts(store) == before
    assert not gateway.snapshot_path.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", "forbidden"),
        ("bootstrap_enabled", "true"),
        ("state_sha256", "bad"),
        ("state_path", "/foreign"),
        ("expires_at", "bad"),
    ],
)
def test_malformed_status_is_refused_before_writes(tmp_path, monkeypatch, field, value):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    original = process.run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[-1] == "status":
            payload = json.loads(result.stdout)
            payload[field] = value
            return ProcessOutcome(tuple(command), 0, stdout=json.dumps(payload))
        return result

    monkeypatch.setattr(process, "run", run)
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_custom_state_root_returns_the_existing_absolute_secret_path_contract(tmp_path):
    service, gateway, _process, _store, _config = recovery_fixture(
        tmp_path / "project", state_root=tmp_path / "selected-state"
    )
    result = service.issue()
    assert result.output_path == str(gateway.state.root / "secret-output/bootstrap-token")
    assert Path(result.output_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("version", ["5.5.1", "2.39.0", "v5.5.1\n", "v2.39.0-beta.1+desktop.2"])
def test_current_compose_plugin_versions_support_recovery(tmp_path, version):
    service, _gateway, process, _store, _config = recovery_fixture(tmp_path)
    process.compose_version = version
    assert service.issue().status == "issued"


@pytest.mark.parametrize(
    "version",
    [
        "1.29.2",
        "",
        "vv2.39.0",
        "2.39",
        "2.39.0junk",
        "2.39.0-bad suffix",
        "2.39.0\n5.5.1",
        "2.39.0\n\n",
        "02.39.0",
        "2.39.0-01",
        "2.39.0+",
    ],
)
def test_legacy_or_malformed_compose_versions_refuse_before_mutation(tmp_path, version):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.compose_version = version
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_recovery_precondition"
    assert process.activation_count == 0
    assert artifacts(store) == before


@pytest.mark.parametrize("health", [None, {"Status": "healthy"}])
def test_canonical_scalable_container_without_docker_health_can_recover(tmp_path, health):
    service, _gateway, process, _store, _config = recovery_fixture(tmp_path)
    process.container_state = {"Running": True}
    if health is not None:
        process.container_state["Health"] = health
    result = service.issue()
    assert result.output_path
    assert process.activation_count == 1
    assert {attempt for attempt, _ in process.public_probes} == {0, 1}
    assert any(url.endswith("/health") for _, url in process.public_probes)
    inspect = next(
        command for command in process.commands if command[:3] == ("docker", "container", "inspect")
    )
    assert "{{if .State.Health}}" in inspect[4]


@pytest.mark.parametrize(
    "state",
    [
        {"Running": False},
        {"Running": True, "Health": {"Status": "unhealthy"}},
        {"Running": True, "Health": {"Status": "starting"}},
        {"Running": True, "Health": {}},
        {"Running": True, "Health": None},
    ],
)
def test_recovery_rejects_not_running_or_unhealthy_container_before_publication(tmp_path, state):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.container_state = state
    with pytest.raises(InstallerFailure):
        service.issue()
    assert artifacts(store) == before
    assert process.activation_count == 0


def test_recovery_requires_public_health_before_publication(tmp_path):
    service, _gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.health_by_activation[0] = 503
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_recovery_precondition"
    assert artifacts(store) == before
    assert process.activation_count == 0
    assert process.public_probes


def test_failed_public_health_compensates_and_checks_restored_readiness(tmp_path):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.health_by_activation[1] = 503
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_token_activation_failed"
    assert artifacts(store) == before
    assert process.activation_count == 2
    assert {attempt for attempt, _ in process.public_probes} == {0, 1, 2}
    assert not gateway.snapshot_path.exists()


def test_compensation_public_health_failure_retains_recovery_material(tmp_path):
    service, gateway, process, store, _config = recovery_fixture(tmp_path)
    before = artifacts(store)
    process.fail_activation = True
    process.health_by_activation[2] = 503
    with pytest.raises(InstallerFailure) as raised:
        service.issue()
    assert raised.value.code == "bootstrap_recovery_needed"
    assert artifacts(store) == before
    assert gateway.snapshot_path.exists()
    assert process.activation_count == 2
    assert any(attempt == 2 for attempt, _ in process.public_probes)
