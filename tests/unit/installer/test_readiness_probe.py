from __future__ import annotations

import inspect
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from core.application.installer import (
    InstallationEvidence,
    InstallConfig,
    InstallJournal,
    classify_installation,
)
from core.application.lifecycle.configuration_service import ConfigurationService
from core.entrypoints.cli import lifecycle_runtime
from core.infrastructure.installer.readiness_probe import StackReadinessProbe
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore


class NoActiveConfig:
    def read_active(self, *, required: bool = False) -> None:
        del required
        return


class ActiveConfig(NoActiveConfig):
    def read_active(self, *, required: bool = False) -> object:
        del required
        return object()

    def derive_target(self, _config: object) -> object:
        raise AssertionError("incomplete installation must not run installed-stack Doctor")


def _failing_if_called(_options: object) -> dict[str, object]:
    raise AssertionError("installed-stack Doctor must not run before installation")


def _check(
    check_id: str,
    status: str,
    detail: str,
    remediation: str,
) -> dict[str, object]:
    return {
        "id": check_id,
        "category": "core",
        "title": check_id.replace("_", " ").title(),
        "status": status,
        "detail": detail,
        "remediation": remediation,
    }


def test_preinstall_doctor_reports_docker_cause_before_install_hint(tmp_path: Path) -> None:
    probe = StackReadinessProbe(
        configuration_service=NoActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        host_diagnostics=lambda: (
            _check(
                "docker_daemon",
                "fail",
                "Docker daemon is not reachable",
                "Start Docker Engine.",
            ),
        ),
        installation_classification=lambda: classify_installation(InstallationEvidence()),
    )

    checks = probe()["checks"]

    assert [row["id"] for row in checks] == ["docker_daemon", "installation_config"]
    assert checks[0]["remediation"] == "Start Docker Engine."


def test_resumable_doctor_reports_failed_step_and_exact_log(tmp_path: Path) -> None:
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = InstallJournal(
        operation_id="1",
        status="failed",
        project_identity="flow-steward",
        config=InstallConfig(),
        completed_steps=("configuration",),
        failed_step="start_runtime",
        last_error_code="runtime_start_failed",
        retryable=True,
    )
    state.save_journal(journal)
    log_path = tmp_path / ".flow-steward/logs/install-1.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("safe installer failure\n", encoding="utf-8")
    probe = StackReadinessProbe(
        configuration_service=NoActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        host_diagnostics=lambda: (),
        installation_classification=lambda: classify_installation(
            InstallationEvidence(journal_exists=True)
        ),
        installation_journal=state.load_journal,
    )

    checks = probe()["checks"]
    resume = next(row for row in checks if row["id"] == "installation_resume")

    assert "start_runtime" in resume["detail"]
    assert "./install.sh" in resume["remediation"]
    assert ".flow-steward/logs/install-1.log" in resume["detail"]


def test_doctor_blocks_restart_when_interrupted_install_rollback_failed(tmp_path: Path) -> None:
    state = InstallerStateStore(tmp_path / ".flow-steward")
    state.save_journal(
        InstallJournal(
            operation_id="rollback-failed",
            status="recovery_required",
            project_identity="flow-steward",
            config=InstallConfig(),
            failed_step="rollback",
            last_error_code="installation_rollback_failed",
            retryable=True,
        )
    )
    probe = StackReadinessProbe(
        configuration_service=NoActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        host_diagnostics=lambda: (),
        installation_classification=lambda: classify_installation(
            InstallationEvidence(journal_exists=True)
        ),
        installation_journal=state.load_journal,
    )

    finding = probe()["checks"][0]

    assert finding["title"] == "Installation rollback"
    assert "Do not rerun ./install.sh" in finding["remediation"]
    assert "resume" not in str(finding["remediation"]).lower()


def test_resumable_doctor_uses_journal_even_when_configuration_step_completed(
    tmp_path: Path,
) -> None:
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = InstallJournal(
        operation_id="configured",
        status="failed",
        project_identity="flow-steward",
        config=InstallConfig(),
        completed_steps=("configuration",),
        failed_step="start_runtime",
        last_error_code="runtime_start_failed",
        retryable=True,
    )
    state.save_journal(journal)
    probe = StackReadinessProbe(
        configuration_service=ActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        host_diagnostics=lambda: (),
        installation_classification=lambda: classify_installation(
            InstallationEvidence(
                journal_exists=True,
                config_exists=True,
                journal_completed_steps=("configuration",),
            )
        ),
        installation_journal=state.load_journal,
    )

    checks = probe()["checks"]

    assert [row["id"] for row in checks] == ["installation_resume"]


def test_resumable_doctor_never_links_another_operations_prefix_log(tmp_path: Path) -> None:
    state = InstallerStateStore(tmp_path / ".flow-steward")
    journal = InstallJournal(
        operation_id="operation-1",
        status="failed",
        project_identity="flow-steward",
        config=InstallConfig(),
        failed_step="start_runtime",
    )
    state.save_journal(journal)
    unrelated = tmp_path / ".flow-steward/logs/install-20260823-operation-10.log"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("unrelated\n", encoding="utf-8")
    probe = StackReadinessProbe(
        configuration_service=NoActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        host_diagnostics=lambda: (),
        installation_classification=lambda: classify_installation(
            InstallationEvidence(journal_exists=True)
        ),
        installation_journal=state.load_journal,
    )

    resume = probe()["checks"][0]

    assert "Log: unavailable." in resume["detail"]
    assert "operation-10.log" not in resume["detail"]


def test_runtime_uses_launcher_host_snapshot_for_preinstall_doctor(
    monkeypatch,
) -> None:
    host_findings = (
        _check(
            "host_platform",
            "pass",
            "Detected Darwin on arm64.",
            "No action required.",
        ),
        _check(
            "default_ports",
            "fail",
            "Default host ports are already in use: 55432/tcp.",
            "Free port 55432/tcp.",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["click_app.py", "--json", "doctor"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(host_findings)))

    probe = lifecycle_runtime._stack_readiness_probe(NoActiveConfig())

    assert probe.host_diagnostics() == host_findings


def _explicit_config_probe(tmp_path: Path) -> StackReadinessProbe:
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=explicit-readiness-test\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "RABBITMQ_URL=amqp://development:secret@rabbitmq-fs:5672/\n",
        encoding="utf-8",
    )
    configuration_service = ConfigurationService(store=store)
    return StackReadinessProbe(
        configuration_service=configuration_service,
        doctor=lambda _options: {
            "checks": [
                {
                    "id": "database",
                    "category": "core",
                    "title": "Database",
                    "status": "pass",
                    "detail": "Database is ready.",
                    "remediation": "No action required.",
                }
            ]
        },
        doctor_options_factory=lambda **values: SimpleNamespace(**values),
        repo_root=tmp_path,
    )


def test_explicit_config_readiness_does_not_require_transition_gateway(tmp_path: Path) -> None:
    probe = _explicit_config_probe(tmp_path)

    checks = probe.check_config(InstallConfig())["checks"]

    assert [row["id"] for row in checks] == ["database"]


def test_explicit_config_readiness_keeps_broker_client_out_of_cli_boundary(
    tmp_path: Path,
) -> None:
    config = InstallConfig(environment="production")
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=explicit-readiness-test\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.prod").write_text(
        "RABBITMQ_URL=amqp://candidate:secret@rabbitmq-fs:5672/\n",
        encoding="utf-8",
    )
    probe = StackReadinessProbe(
        configuration_service=ConfigurationService(store=store),
        doctor=lambda _options: {"checks": []},
        doctor_options_factory=lambda **values: SimpleNamespace(**values),
        repo_root=tmp_path,
    )

    checks = probe.check_config(config)["checks"]

    assert "dispatch_smoke" not in inspect.signature(StackReadinessProbe).parameters
    assert not any(row["id"] == "job_dispatch" for row in checks)


def test_explicit_invalid_config_keeps_core_configuration_finding(tmp_path: Path) -> None:
    probe = _explicit_config_probe(tmp_path)

    checks = probe.check_config(InstallConfig(public_host=""))["checks"]

    configuration = next(row for row in checks if row["id"] == "configuration_validity")
    assert configuration["status"] == "fail"
    assert configuration["detail"] == "public_host must not be empty"


def test_installer_and_public_readiness_share_internal_url_but_not_bootstrap_allowance(
    tmp_path: Path,
) -> None:
    config = InstallConfig(
        environment="production",
        public_host="public.example.test",
        tls_mode="reverse_proxy",
        reverse_proxy_backend_port=18099,
    )
    store = LifecycleConfigurationStore(tmp_path)
    store.write_active(config)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=readiness-phase-test\n",
        encoding="utf-8",
    )
    public_env = (
        "FS_PUBLIC_BASE_URL=https://public.example.test\n"
        "FS_AUTH_BOOTSTRAP_ENABLED=1\n"
        "RABBITMQ_URL=amqp://production:secret@rabbitmq-fs:5672/\n"
    )
    (tmp_path / ".env.prod").write_text(public_env, encoding="utf-8")
    options: list[SimpleNamespace] = []

    def doctor(value: SimpleNamespace) -> dict[str, object]:
        options.append(value)
        return {"checks": []}

    probe = StackReadinessProbe(
        configuration_service=ConfigurationService(store=store),
        doctor=doctor,
        doctor_options_factory=lambda **values: SimpleNamespace(**values),
        repo_root=tmp_path,
        installation_journal=lambda: SimpleNamespace(status="completed"),
    )
    active_before = store.read_active()

    probe.check_config(
        config,
        allow_initial_auth_bootstrap=True,
        allow_lifecycle_mutation=True,
    )
    probe()

    assert [value.base_url for value in options] == [
        "http://fs-app:8099",
        "http://fs-app:8099",
    ]
    assert [value.allow_initial_auth_bootstrap for value in options] == [True, False]
    assert [value.allow_lifecycle_mutation for value in options] == [True, False]
    assert [value.compose_project for value in options] == [
        "readiness-phase-test",
        "readiness-phase-test",
    ]
    assert store.read_active() == active_before
    assert (tmp_path / ".env.prod").read_text(encoding="utf-8") == public_env


class _InstalledProbe(StackReadinessProbe):
    """The installed-stack path, with the stack's own checks already green."""

    def check_config(self, config, **_kwargs):
        del config
        return {"checks": [_check("api_ready", "pass", "", "")]}


def _installed_probe(tmp_path: Path, lifecycle_state) -> StackReadinessProbe:
    return _InstalledProbe(
        configuration_service=ActiveConfig(),
        doctor=_failing_if_called,
        doctor_options_factory=object,
        repo_root=tmp_path,
        installation_journal=lambda: None,
        lifecycle_state=lifecycle_state,
        active_lifecycle_operation=lambda: lifecycle_runtime._active_lifecycle_operation(
            lifecycle_state
        ),
        # as the CLI composes it (lifecycle_runtime._stack_readiness_probe)
        complete_command=lifecycle_runtime.complete_operator_command,
    )


def _journal(operation_id: str):
    from core.application.lifecycle.contracts import (
        LifecycleOperationJournal,
        LifecycleOperationKind,
    )

    return LifecycleOperationJournal(
        operation_id=operation_id,
        kind=LifecycleOperationKind.CONFIG_APPLY,
        current_step="change_runtime",
        recovery_command="flow-steward config apply",
    )


def test_status_names_an_interrupted_config_apply_and_how_to_continue_it(tmp_path: Path) -> None:
    """W1 (auditor): after a kill -9, status named only missing services, or said READY."""
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    state = LifecycleStateStore(tmp_path)
    state.save_journal(_journal("op-interrupted"))

    checks = _installed_probe(tmp_path, state)()["checks"]

    finding = next(row for row in checks if row["id"] == "lifecycle_operation")
    assert finding["status"] == "fail"
    assert "op-interrupted was interrupted at step change_runtime" in finding["detail"]
    assert finding["remediation"] == "Continue it with ./flow-steward config apply --confirm."


def test_a_running_operation_is_reported_as_running_not_interrupted(tmp_path: Path) -> None:
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    state = LifecycleStateStore(tmp_path)
    state.save_journal(_journal("op-live"))

    with state.operation_lock("op-live", mutating=True):
        checks = _installed_probe(tmp_path, LifecycleStateStore(tmp_path))()["checks"]

    finding = next(row for row in checks if row["id"] == "lifecycle_operation")
    assert finding["status"] == "warn"
    assert "op-live is running" in finding["detail"]


def test_no_unfinished_operation_adds_nothing(tmp_path: Path) -> None:
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    checks = _installed_probe(tmp_path, LifecycleStateStore(tmp_path))()["checks"]

    assert [row["id"] for row in checks] == ["api_ready"]
