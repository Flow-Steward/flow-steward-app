from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.application.installer import (
    BootstrapVerification,
    ImageCurrency,
    InstallationEvidence,
    InstallationKind,
    InstallConfig,
    InstallJournal,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.service import InstallerService
from core.application.readiness import CoreReadinessService
from core.infrastructure.installer.state_store import InstallerStateStore


class FakeGateway:
    def __init__(self, evidence: InstallationEvidence | None = None) -> None:
        self.evidence = evidence or InstallationEvidence()
        self.calls: list[str] = []
        self.held: set[str] = set()
        self.fail_step: str | None = None
        self.unknown_failure = False
        self.storage_read_only: bool | None = None
        self.rollback_failure = False
        self.rollback_operation_id = ""
        self.rollback_created_paths: tuple[str, ...] = ()
        self.bootstrap_token = "one-time-secret"
        # AC17-R141: explicit, not left to __getattr__ below, which would answer None
        # and make every installed-system test pass a question it never asked.
        self.currency = ImageCurrency(source_revision="a" * 40, running_revision="a" * 40)
        self.active_config: InstallConfig | None = None
        self.notices: list[str] = []
        self.prune_summary = ""
        self.converged = ""
        self.started = ""
        self.unfinished_switch: dict | None = None
        self.pending_switch = None
        self.converge_changed: bool | None = None
        self.pruned_with: tuple[str, str] | None = None

    def inspect(self):
        self.calls.append("inspect")
        return self.evidence

    def image_currency(self, _config, *, log):
        del log
        self.calls.append("image_currency")
        return self.currency

    def postcondition_holds(self, step_id, _config):
        self.calls.append(f"postcondition:{step_id}")
        return step_id in self.held

    def product_url(self, config):
        return f"https://{config.public_host}:{config.public_port}"

    def installed_config(self, recorded):
        return self.active_config or recorded

    def take_notices(self):
        return tuple(self.notices)

    def record_installed_releases(self, *, current, previous):
        self.calls.append("record_installed_releases")
        self.recorded_releases = (current, previous)

    def publish_release_environment(self, _config, release):
        self.calls.append("publish_release_environment")
        self.published_release = release

    def set_compact_worker_processes(self, config, *, processes, log):
        del log
        self.calls.append(f"set_compact_worker_processes:{processes}")
        return config.model_copy(update={"worker_processes": processes})

    def converge_compact_worker_processes(self, config, *, log, changed=False):
        del log
        self.calls.append(f"converge_compact_worker_processes:{config.worker_processes}")
        self.converge_changed = changed
        return self.converged

    def start_stopped_services(self, _config, *, log):
        del log
        self.calls.append("start_stopped_services")
        return self.started

    def unfinished_profile_switch(self):
        return self.unfinished_switch

    def record_switch_host_trees(self, _installed, *, log):
        del log
        self.calls.append("record_switch_host_trees")
        return {"artifacts": "/srv/fs/logs/artifacts"}

    def normalize_switch_host_trees(self, _target, _roots, *, log):
        del log
        self.calls.append("normalize_switch_host_trees")
        return ["Normalized 1 directories and 2 files under /srv/fs/logs/artifacts."]

    def restore_switch_worker_token(self, _installed, *, log):
        del log
        self.calls.append("restore_switch_worker_token")

    def check_switch_target_renders(self, _target, *, log):
        del log
        self.calls.append("check_switch_target_renders")

    def remove_switch_target_leftovers(self, _installed, _target, *, log):
        del log
        self.calls.append("remove_switch_target_leftovers")

    def remove_switch_source_containers(self, _source, _target, *, log):
        del log
        self.calls.append("remove_switch_source_containers")
        return ""

    def write_switch_pending(self, patch):
        self.calls.append("write_switch_pending")
        self.pending_switch = patch

    def apply_switch_in_cli(self, scalable, *, log):
        del log
        self.calls.append(f"apply_switch_in_cli:{scalable.deployment_profile}")
        profile = self.pending_switch.deployment_profile if self.pending_switch else "compact"
        self.active_config = InstallConfig.model_validate(
            {
                **scalable.model_dump(mode="json"),
                "deployment_profile": profile,
                "worker_replicas": 1,
            }
        )
        self.unfinished_switch = None

    def prune_superseded_releases(self, _config, *, current, previous, log):
        del log
        self.calls.append("prune_superseded_releases")
        self.pruned_with = (current, previous)
        return self.prune_summary

    def project_identity(self):
        return "flow-steward-test"

    def verify_storage(self, _config, *, log, read_only=False):
        del log
        self.calls.append("verify_storage")
        self.storage_read_only = read_only

    def verify_bootstrap(self, _config, *, log, require_first_admin=True):
        del log
        self.calls.append("verify_bootstrap")
        self.require_first_admin = require_first_admin
        return BootstrapVerification(
            bootstrap_required=require_first_admin,
            token=(
                self.bootstrap_token
                if require_first_admin and _config.public_host != "localhost"
                else ""
            ),
            token_path=(
                "/tmp/bootstrap-token"
                if require_first_admin and _config.public_host != "localhost"
                else None
            ),
        )

    def reissue_bootstrap_token(self, _config, *, log):
        del log
        self.calls.append("reissue_bootstrap_token")

    def rollback_initial_install(self, journal, *, log):
        del log
        self.calls.append("rollback_initial_install")
        self.rollback_operation_id = journal.operation_id
        self.rollback_created_paths = journal.created_paths
        if self.rollback_failure:
            raise InstallerFailure(
                code="installation_rollback_failed",
                cause="rollback failed",
                remediation="inspect rollback evidence",
                exit_code=10,
                retryable=True,
                step="rollback",
            )

    def __getattr__(self, name):
        def call(_config, *, log, **_kwargs):
            del log
            self.calls.append(name)
            if self.fail_step == name:
                if self.unknown_failure:
                    raise OSError("unexpected disk failure")
                raise InstallerFailure(
                    code="known_failure",
                    cause="known cause",
                    remediation="known remediation",
                    exit_code=8,
                    step=name,
                )

        return call


def _service(
    tmp_path: Path,
    gateway: FakeGateway,
    *,
    readiness_service: CoreReadinessService | None = None,
) -> tuple[InstallerService, InstallerStateStore]:
    store = InstallerStateStore(tmp_path / ".flow-steward")
    return InstallerService(
        gateway=gateway,
        state=store,
        project_identity="flow-steward-test",
        readiness_service=readiness_service,
    ), store


def test_clean_install_executes_ordered_core_steps_without_ai_configuration(tmp_path: Path) -> None:
    gateway = FakeGateway()
    readiness = CoreReadinessService(
        probe=lambda: (
            gateway.calls.append("shared_readiness")
            or {"checks": [{"id": "database", "category": "core", "status": "pass"}]}
        )
    )
    service, store = _service(tmp_path, gateway, readiness_service=readiness)

    result = service.install(InstallConfig(public_host="localhost"))

    assert result.status == "installed"
    assert result.classification is InstallationKind.FRESH
    assert gateway.calls.index("ensure_tls") < gateway.calls.index("start_core")
    assert gateway.calls.index("start_core") < gateway.calls.index("verify_schema")
    assert gateway.calls.index("verify_schema") < gateway.calls.index("start_runtime")
    assert gateway.calls.index("ensure_cli_runtime") < gateway.calls.index("verify_storage")
    assert gateway.calls.index("ensure_cli_runtime") < gateway.calls.index("shared_readiness")
    assert gateway.calls.index("shared_readiness") < gateway.calls.index("capture_initial_manifest")
    assert "configure_ai" not in gateway.calls
    assert "extensions" not in " ".join(gateway.calls)
    journal = store.load_journal()
    assert journal.status == "completed"
    assert journal.completed_steps[-1] == "manifest"
    assert journal.completed_steps.index("cli_runtime") < journal.completed_steps.index("storage")
    assert result.bootstrap_url == "https://localhost:8099/bootstrap"
    assert "create the first administrator" in result.bootstrap_instruction


def test_invalid_scoped_host_cannot_poison_journal_before_corrected_install(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    service, store = _service(tmp_path, gateway)

    with pytest.raises(ValidationError, match="scope"):
        InstallConfig.model_validate(
            {
                "environment": "production",
                "public_host": "fe80::1%eth0",
                "public_port": 443,
            }
        )

    assert store.load_journal() is None

    corrected = InstallConfig(
        environment="production",
        public_host="flow.example.test",
        public_port=443,
    )
    result = service.install(corrected)

    assert result.status == "installed"
    assert service.last_config == corrected
    assert store.load_journal().config == corrected


def test_resume_rechecks_postconditions_and_reuses_journal_config(tmp_path: Path) -> None:
    evidence = InstallationEvidence(
        journal_exists=True,
        config_exists=True,
        env_exists=True,
        project_identity="flow-steward-test",
        observed_project_identity="flow-steward-test",
        durable_secrets_present=True,
    )
    gateway = FakeGateway(evidence)
    gateway.held = {"configuration", "secrets", "tls"}
    service, store = _service(tmp_path, gateway)
    saved_config = InstallConfig(environment="production", public_host="internal.example")
    store.save_journal(
        InstallJournal(
            operation_id="existing-op",
            project_identity="flow-steward-test",
            config=saved_config,
            completed_steps=("configuration", "secrets", "tls"),
        )
    )

    result = service.install(InstallConfig(environment="development", public_host="ignored"))

    assert result.operation_id == "existing-op"
    assert "ensure_configuration" not in gateway.calls
    assert "ensure_secrets" not in gateway.calls
    assert "ensure_tls" not in gateway.calls
    assert gateway.calls.count("postcondition:secrets") == 1
    assert service.last_config == saved_config


def test_completed_install_repairs_worker_identity_before_verification(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    readiness = CoreReadinessService(
        probe=lambda: (
            gateway.calls.append("shared_readiness")
            or {"checks": [{"id": "database", "category": "core", "status": "pass"}]}
        )
    )
    service, store = _service(tmp_path, gateway, readiness_service=readiness)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(),
            completed_steps=("manifest",),
        )
    )

    result = service.install(InstallConfig())

    assert result.status == "already_installed"
    assert gateway.calls == [
        "inspect",
        # AC17-R141: asked first, so a stale installation is updated rather than
        # restarted around its old image and reported as current.
        "image_currency",
        # W2: before anything brings fs-worker up at its recorded count.
        "refuse_unrecorded_worker_scale",
        # AC17-R162: a stopped stack comes back before anything needs its database
        "start_stopped_services",
        "ensure_worker_identity",
        "activate_worker_identity",
        "verify_health",
        "ensure_cli_runtime",
        "verify_storage",
        "verify_public_route",
        "verify_bootstrap",
        "shared_readiness",
        # AC17-R158: finishes a release cleanup an interrupted update left
        "prune_superseded_releases",
    ]
    assert gateway.storage_read_only is True
    assert result.bootstrap_url == ""
    assert "sign in" in result.bootstrap_instruction


def test_completed_install_retries_worker_activation_after_failure(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(),
            completed_steps=("manifest",),
        )
    )

    gateway.fail_step = "activate_worker_identity"
    _raises_input_245_1 = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(_raises_input_245_1)
    gateway.fail_step = None

    result = service.install(InstallConfig())

    assert result.status == "already_installed"
    assert gateway.calls.count("ensure_worker_identity") == 2
    assert gateway.calls.count("activate_worker_identity") == 2
    assert gateway.calls.count("verify_health") == 1


def test_expired_non_loopback_handoff_never_names_a_command_compact_refuses(
    tmp_path: Path,
) -> None:
    # AC17-R50: this instruction used to say `./flow-steward users issue-bootstrap-token`.
    # A Compact launcher answers that with "Unsupported Compact platform command." and exit
    # code 2, so the only screen a fresh server install has pointed at a dead end. The
    # fallback text must name something that works on every profile.
    gateway = FakeGateway()

    def expired(_config, *, log, require_first_admin=True):
        del log, require_first_admin
        gateway.calls.append("verify_bootstrap")
        return BootstrapVerification(bootstrap_required=True, token_expired=True)

    gateway.verify_bootstrap = expired
    service, _store = _service(tmp_path, gateway)

    result = service.install(InstallConfig(public_host="flow.example.test"))

    assert result.bootstrap_url == ""
    assert "users issue-bootstrap-token" not in result.bootstrap_instruction
    assert "./install.sh" in result.bootstrap_instruction
    assert result.bootstrap_token_path is None


def test_installed_rerun_replaces_an_expired_setup_token_instead_of_advising(
    tmp_path: Path,
) -> None:
    # The operator installed on a server, was called away for an hour, and came back. The
    # already-installed path never reaches ensure_secrets, so nothing else in the product can
    # issue a replacement -- on Compact, not even the CLI.
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    attempts = {"count": 0}

    def expiring_then_fresh(_config, *, log, require_first_admin=True):
        del require_first_admin
        del log
        gateway.calls.append("verify_bootstrap")
        attempts["count"] += 1
        if attempts["count"] == 1:
            return BootstrapVerification(bootstrap_required=True, token_expired=True)
        return BootstrapVerification(
            bootstrap_required=True,
            token="replacement-token",
            token_path="/tmp/bootstrap-token",
        )

    gateway.verify_bootstrap = expiring_then_fresh
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(public_host="flow.example.test"),
            completed_steps=("manifest",),
        )
    )

    result = service.install(InstallConfig(public_host="flow.example.test"))

    assert result.status == "already_installed"
    assert gateway.calls.count("reissue_bootstrap_token") == 1
    assert result.bootstrap_setup_url.endswith("#setup-token=replacement-token")
    assert "within 60 minutes" in result.bootstrap_instruction
    assert "users issue-bootstrap-token" not in result.bootstrap_instruction


def test_installed_rerun_leaves_a_live_setup_token_alone(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(public_host="flow.example.test"),
            completed_steps=("manifest",),
        )
    )

    result = service.install(InstallConfig(public_host="flow.example.test"))

    assert result.status == "already_installed"
    assert "reissue_bootstrap_token" not in gateway.calls


def test_ambiguous_state_refuses_before_any_mutation(tmp_path: Path) -> None:
    gateway = FakeGateway(InstallationEvidence(owned_volumes=("flow-steward_pg-fs",)))
    service, _store = _service(tmp_path, gateway)

    _raises_input_279_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_279_1)

    assert raised.value.code == "ambiguous_installation_state"
    assert raised.value.exit_code == 9
    assert raised.value.log_path
    assert Path(raised.value.log_path).is_file()
    assert "preserved" in raised.value.remediation.lower()
    assert gateway.calls == ["inspect"]


def test_pre_phase3_stack_refusal_has_safe_non_destructive_recovery(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            config_exists=True,
            env_exists=True,
            durable_secrets_present=True,
            owned_volumes=("flow-steward_pg-fs",),
        )
    )
    service, _store = _service(tmp_path, gateway)

    _raises_input_301_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_301_1)

    assert "pre-Phase3 stack was preserved" in raised.value.remediation
    assert "Do not delete" in raised.value.remediation
    assert "clean checkout" in raised.value.remediation
    assert gateway.calls == ["inspect"]


def test_concurrent_installer_fails_before_classification_with_diagnostic_log(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    service, store = _service(tmp_path, gateway)

    _raises_input_316_1 = InstallConfig()
    with store.operation_lock(), pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_316_1)

    assert raised.value.code == "installer_operation_active"
    assert raised.value.log_path
    assert Path(raised.value.log_path).is_file()
    assert gateway.calls == []


def test_completed_install_health_failure_is_mapped_with_log_path(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    gateway.fail_step = "verify_health"
    gateway.unknown_failure = True
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(),
        )
    )

    _raises_input_349_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_349_1)

    assert raised.value.code == "unexpected_internal_error"
    assert raised.value.log_path
    assert "unexpected disk failure" in raised.value.cause


def test_shared_readiness_blocks_only_core_failures(tmp_path: Path) -> None:
    gateway = FakeGateway()
    readiness = CoreReadinessService(
        probe=lambda: {
            "checks": [
                {"id": "database", "category": "core", "status": "fail"},
                {"id": "llm_provider", "category": "ai", "status": "warn"},
                {"id": "extensions", "category": "extensions", "status": "fail"},
            ]
        }
    )
    service, store = _service(tmp_path, gateway, readiness_service=readiness)

    _raises_input_370_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_370_1)

    assert raised.value.code == "core_readiness_blocked"
    assert store.load_journal().failed_step == "readiness"


def test_shared_readiness_keeps_ai_warning_non_blocking(tmp_path: Path) -> None:
    gateway = FakeGateway()
    readiness = CoreReadinessService(
        probe=lambda: {
            "checks": [
                {"id": "database", "category": "core", "status": "pass"},
                {"id": "embedding_provider", "category": "ai", "status": "warn"},
            ]
        }
    )
    service, _store = _service(tmp_path, gateway, readiness_service=readiness)

    result = service.install(InstallConfig())

    assert result.status == "installed"
    assert any("Embedding" in warning for warning in result.warnings)


@pytest.mark.parametrize("unknown", [False, True])
def test_failures_record_visible_cause_remediation_and_log_path(
    tmp_path: Path, unknown: bool
) -> None:
    gateway = FakeGateway()
    gateway.fail_step = "start_core"
    gateway.unknown_failure = unknown
    service, store = _service(tmp_path, gateway)

    _raises_input_404_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_404_1)

    journal = store.load_journal()
    assert raised.value.cause
    assert raised.value.remediation
    assert raised.value.log_path
    assert journal.status == "failed"
    assert journal.failed_step == "start"
    assert (
        "unexpected disk failure" in raised.value.cause
        if unknown
        else "known cause" in raised.value.cause
    )


def test_interrupt_rolls_back_and_next_install_starts_new_operation(tmp_path: Path) -> None:
    gateway = FakeGateway()

    def interrupt(_config, *, log):
        del log
        raise KeyboardInterrupt

    gateway.prepare_core = interrupt
    service, store = _service(tmp_path, gateway)

    _raises_input_430_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_430_1)

    assert raised.value.code == "installation_interrupted"
    assert raised.value.exit_code == 10
    assert "rollback_initial_install" in gateway.calls
    assert store.load_journal() is None

    first_operation_id = gateway.rollback_operation_id
    del gateway.prepare_core
    gateway.calls.clear()

    result = service.install(InstallConfig())

    assert result.status == "installed"
    assert result.operation_id != first_operation_id
    assert "preflight" in gateway.calls


def test_interrupt_rollback_failure_keeps_recovery_journal(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.rollback_failure = True

    def interrupt(_config, *, log):
        del log
        raise KeyboardInterrupt

    gateway.prepare_core = interrupt
    service, store = _service(tmp_path, gateway)

    _raises_input_460_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_460_1)

    journal = store.load_journal()
    assert raised.value.code == "installation_rollback_failed"
    assert journal is not None
    assert journal.status == "recovery_required"
    assert journal.failed_step == "rollback"
    assert journal.last_error_code == "installation_rollback_failed"


def test_recovery_required_journal_refuses_rerun_before_gateway_mutation(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="recovery",
            status="recovery_required",
            project_identity="flow-steward-test",
            config=InstallConfig(),
            failed_step="rollback",
            last_error_code="installation_rollback_failed",
        )
    )

    _raises_input_494_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_494_1)

    assert raised.value.code == "installation_rollback_failed"
    assert gateway.calls == ["inspect"]
    assert store.load_journal().status == "recovery_required"


def test_interrupt_plans_current_step_paths_before_entering_mutation(tmp_path: Path) -> None:
    gateway = FakeGateway()

    def interrupt_configuration(_config, *, log):
        del log
        raise KeyboardInterrupt

    gateway.ensure_configuration = interrupt_configuration
    service, _store = _service(tmp_path, gateway)

    _raises_input_512_1 = InstallConfig()
    with pytest.raises(InstallerFailure, match="interrupted"):
        service.install(_raises_input_512_1)

    assert ".flow-steward/stack.toml" in gateway.rollback_created_paths
    assert ".flow-steward/compose.env" in gateway.rollback_created_paths


def test_journal_clear_failure_is_typed_and_closes_log(tmp_path: Path, monkeypatch) -> None:
    gateway = FakeGateway()

    def interrupt(_config, *, log):
        del log
        raise KeyboardInterrupt

    gateway.preflight = interrupt
    service, store = _service(tmp_path, gateway)
    opened_logs = []
    open_log = store.open_log

    def capture_log(operation_id: str):
        log = open_log(operation_id)
        opened_logs.append(log)
        return log

    monkeypatch.setattr(store, "open_log", capture_log)

    original_save = store.save_journal
    persistence_read_only = False

    def guarded_save(journal) -> None:
        if persistence_read_only:
            raise PermissionError("state directory is read-only")
        original_save(journal)

    def refuse_clear() -> None:
        nonlocal persistence_read_only
        persistence_read_only = True
        raise PermissionError("journal is read-only")

    monkeypatch.setattr(store, "save_journal", guarded_save)
    monkeypatch.setattr(store, "clear_journal", refuse_clear)

    _raises_input_554_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_554_1)

    journal = store.load_journal()
    assert raised.value.code == "installation_rollback_failed"
    assert journal is not None
    assert journal.status == "recovery_required"
    assert journal.last_error_code == "installation_rollback_failed"
    assert "journal is read-only" in raised.value.cause
    assert opened_logs
    assert opened_logs[-1]._fd == -1


def test_journal_records_observed_identities_and_managed_paths(tmp_path: Path) -> None:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            owned_containers=("flow-steward-test-fs-app-1",),
            owned_volumes=("flow-steward-test-pg-fs",),
            durable_secrets_present=True,
        )
    )
    service, store = _service(tmp_path, gateway)
    store.save_journal(
        InstallJournal(
            operation_id="observed",
            project_identity="flow-steward-test",
            config=InstallConfig(),
        )
    )

    service.install(InstallConfig())

    journal = store.load_journal()
    assert journal.observed_project_identity == "flow-steward-test"
    assert journal.observed_volumes == ("flow-steward-test-pg-fs",)
    assert ".flow-steward/stack.toml" in journal.created_paths
    assert ".ssl/cert.pem" in journal.created_paths
    assert ".flow-steward/runtime" in journal.created_paths


def test_cli_runtime_failure_before_readiness_and_manifest_is_resumable_without_rollback(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.fail_step = "ensure_cli_runtime"
    service, store = _service(tmp_path, gateway)

    _raises_input_606_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        service.install(_raises_input_606_1)

    journal = store.load_journal()
    assert raised.value.code == "known_failure"
    assert journal.status == "failed"
    assert journal.failed_step == "cli_runtime"
    assert "manifest" not in journal.completed_steps
    assert "cli_runtime" not in journal.completed_steps
    assert not any(
        token in " ".join(gateway.calls)
        for token in ("rollback", "down", "volume", "database reset", "drop")
    )


def test_cli_runtime_resume_retries_exact_runtime_then_finishes_remaining_steps(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.fail_step = "ensure_cli_runtime"
    service, store = _service(tmp_path, gateway)

    _raises_input_628_1 = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(_raises_input_628_1)

    failed_journal = store.load_journal()
    gateway.fail_step = None
    gateway.held = set(failed_journal.completed_steps)
    gateway.calls.clear()

    result = service.install(InstallConfig())

    assert result.status == "installed"
    assert gateway.calls.index("ensure_cli_runtime") < gateway.calls.index(
        "capture_initial_manifest"
    )
    assert "ensure_secrets" not in gateway.calls
    assert "verify_schema" not in gateway.calls
    assert gateway.calls.count("ensure_cli_runtime") == 1
    assert store.load_journal().completed_steps[-1] == "manifest"


def test_readiness_failure_resumes_with_same_operation_and_reuses_exact_cli_runtime(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    readiness_checks = [
        {"checks": [{"id": "database", "category": "core", "status": "fail"}]},
        {"checks": [{"id": "database", "category": "core", "status": "pass"}]},
    ]
    readiness = CoreReadinessService(probe=lambda: readiness_checks.pop(0))
    service, store = _service(tmp_path, gateway, readiness_service=readiness)

    _raises_input_659_1 = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(_raises_input_659_1)

    failed = store.load_journal()
    assert failed.failed_step == "readiness"
    assert "cli_runtime" in failed.completed_steps
    assert failed.completed_steps[-1] == "bootstrap"
    assert "manifest" not in failed.completed_steps
    assert gateway.calls.count("ensure_cli_runtime") == 1

    gateway.held = set(failed.completed_steps)
    result = service.install(InstallConfig())

    assert result.operation_id == failed.operation_id
    assert gateway.calls.count("ensure_cli_runtime") == 1
    assert gateway.calls.count("postcondition:cli_runtime") == 1
    assert store.load_journal().completed_steps[-1] == "manifest"


def test_non_loopback_install_hands_over_a_url_the_operator_can_open(tmp_path: Path) -> None:
    """AC17-R46: the first screen of a new installation must not 403 on the first click.

    The installer holds the product URL and the token at the same moment; emitting the
    bare URL next to prose asking the operator to assemble the working one made the only
    entry point of a fresh install unusable.
    """
    gateway = FakeGateway()
    service, _store = _service(tmp_path, gateway)

    result = service.install(InstallConfig(public_host="fs.example.eu", public_port=443))

    assert (
        result.bootstrap_setup_url
        == "https://fs.example.eu:443/bootstrap#setup-token=one-time-secret"
    )
    assert "#setup-token=<token>" not in result.bootstrap_instruction
    assert "secret" in result.bootstrap_instruction.lower()
    # the dumped field stays free of the token
    assert result.bootstrap_url == "https://fs.example.eu:443/bootstrap"
    # the stored path stays available for recovery
    assert result.bootstrap_token_path == "/tmp/bootstrap-token"


def test_bootstrap_token_never_reaches_a_serialized_result_or_log(tmp_path: Path) -> None:
    """The token belongs in the URL the operator opens, nowhere else."""
    gateway = FakeGateway()
    service, store = _service(tmp_path, gateway)

    result = service.install(InstallConfig(public_host="fs.example.eu", public_port=443))

    state = BootstrapVerification(
        bootstrap_required=True, token_path="/tmp/bootstrap-token", token="one-time-secret"
    )
    assert "token" not in state.model_dump()
    assert "one-time-secret" not in repr(state)
    log_text = Path(result.log_path).read_text(encoding="utf-8")
    assert "one-time-secret" not in log_text
    persisted = [
        path.read_text(encoding="utf-8", errors="ignore")
        for path in store.root.rglob("*")
        if path.is_file() and path.name != "bootstrap-token"
    ]
    assert persisted, "the installer wrote no state to check"
    assert all("one-time-secret" not in text for text in persisted)
    # `install.sh --json > result.json` must not capture a live setup token
    assert "one-time-secret" not in json.dumps(result.model_dump(mode="json"))
    # only the excluded field, which the CLI writes to stderr, carries it
    assert "one-time-secret" in result.bootstrap_setup_url


def test_loopback_install_still_needs_no_setup_token(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service, _store = _service(tmp_path, gateway)

    result = service.install(InstallConfig(public_host="localhost"))

    assert result.bootstrap_url == "https://localhost:8099/bootstrap"
    assert result.bootstrap_setup_url == ""


# --- AC17-R141: a rerun on a completed installation that the sources have moved past ---

_OLD = "1" * 40
_NEW = "2" * 40


def _release_identity(version: str, marker: str, profile: str = "scalable") -> ReleaseIdentity:
    from core.application.release.images import required_product_roles

    digest = f"sha256:{marker * 64}"
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=marker * 40,
        registry="ghcr.io/flow-steward",
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=marker * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles(profile)
        ),
    )


def _installed_service(tmp_path: Path, gateway: FakeGateway) -> InstallerService:
    readiness = CoreReadinessService(
        probe=lambda: {"checks": [{"id": "database", "category": "core", "status": "pass"}]}
    )
    service, store = _service(tmp_path, gateway, readiness_service=readiness)
    store.save_journal(
        InstallJournal(
            operation_id="done",
            status="completed",
            project_identity="flow-steward-test",
            config=InstallConfig(),
            completed_steps=("manifest",),
        )
    )
    return service


def _installed_gateway(currency: ImageCurrency) -> FakeGateway:
    gateway = FakeGateway(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward-test",
            observed_project_identity="flow-steward-test",
            durable_secrets_present=True,
        )
    )
    gateway.currency = currency
    return gateway


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize("path", ["fresh", "installed", "update"])
def test_installed_update_refreshes_failed_derived_capabilities_before_final_readiness(
    tmp_path: Path, profile: str, path: str
) -> None:
    """An installed update must request real refresh, not bypass its stale evidence."""

    class ReconciliationGateway(FakeGateway):
        refreshed = False
        requests: list[tuple[str, tuple[str, ...]]]

        def reconcile_readiness_capabilities(self, config, *, check_ids, log):
            del log
            self.requests.append((config.deployment_profile, check_ids))
            self.refreshed = True

    gateway = ReconciliationGateway(
        None if path == "fresh" else _installed_gateway(_stale()).evidence
    )
    gateway.currency = (
        _stale() if path == "update" else ImageCurrency(source_revision=_NEW, running_revision=_NEW)
    )
    gateway.requests = []
    service = (
        _service(tmp_path, gateway)[0] if path == "fresh" else _installed_service(tmp_path, gateway)
    )
    gateway.active_config = InstallConfig(
        deployment_profile=profile, environment="production", file_storage="local_fs"
    )
    probe_calls = []

    def probe():
        probe_calls.append(gateway.refreshed)
        return {
            "checks": [
                {"id": "database", "category": "core", "status": "pass"},
                {
                    "id": "security_posture",
                    "category": "core",
                    "status": "pass" if gateway.refreshed else "fail",
                },
                {
                    "id": "workflow_knowledge_sync",
                    "category": "core",
                    "status": "pass" if gateway.refreshed else "fail",
                },
            ]
        }

    service.readiness_service = CoreReadinessService(probe=probe)
    service.readiness_reprobe = lambda remaining: CoreReadinessService(probe=probe).check()

    result = service.install(gateway.active_config)

    assert result.status == ("already_installed" if path == "installed" else "installed")
    assert gateway.requests == [(profile, ("security_posture", "workflow_knowledge_sync"))]
    assert probe_calls == [False, True]
    if path != "installed":
        assert gateway.calls.index("start_runtime") < gateway.calls.index("verify_public_route")


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize(
    "scenario",
    ["delayed", "still_stale", "slow_probe", "late_green", "scan_failure", "unrelated", "healthy"],
)
def test_installer_reconciliation_waits_for_real_evidence_without_bypassing_blockers(
    tmp_path: Path, profile: str, scenario: str
) -> None:
    class ReconciliationGateway(FakeGateway):
        requests: list[tuple[str, ...]]

        def reconcile_readiness_capabilities(self, config, *, check_ids, log):
            del log
            assert config.deployment_profile == profile
            self.requests.append(check_ids)
            if scenario == "scan_failure":
                raise InstallerFailure(
                    code="readiness_reconciliation_failed",
                    cause="derived scan failed",
                    remediation="Inspect the real failure.",
                    exit_code=4,
                    step="readiness",
                )

    gateway = ReconciliationGateway(_installed_gateway(_stale()).evidence)
    gateway.currency = _stale()
    gateway.requests = []
    existing_service = _installed_service(tmp_path, gateway)
    config = InstallConfig(
        deployment_profile=profile, environment="production", file_storage="local_fs"
    )
    gateway.active_config = config
    now = [0.0]
    probes = []
    sleeps = []
    budgets = []

    def wait(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def probe():
        probes.append(now[0])
        if scenario in {"slow_probe", "late_green"} and gateway.requests:
            now[0] += 60.0
        ready = (
            scenario == "healthy"
            or (scenario == "delayed" and bool(gateway.requests) and now[0] >= 4.0)
            or (scenario == "late_green" and bool(gateway.requests))
        )
        checks = [
            {"id": check_id, "category": "core", "status": "pass" if ready else "fail"}
            for check_id in ("security_posture", "workflow_knowledge_sync")
        ]
        checks.append(
            {
                "id": "database",
                "category": "core",
                "status": "fail" if scenario == "unrelated" else "pass",
            }
        )
        return {"checks": checks}

    def bounded_probe(remaining):
        budgets.append(remaining)
        return CoreReadinessService(probe=probe).check()

    service = InstallerService(
        gateway=gateway,
        state=existing_service.state,
        project_identity="flow-steward-test",
        readiness_service=CoreReadinessService(probe=probe),
        readiness_reprobe=bounded_probe,
        readiness_sleep=wait,
        readiness_clock=lambda: now[0],
    )
    release = _release_identity("0.9.1", "2", profile)
    if scenario in {"delayed", "healthy"}:
        assert service.install(config, target_release=release).status == "installed"
        assert existing_service.state.load_installed_release() == release
    else:
        with pytest.raises(InstallerFailure) as raised:
            service.install(config, target_release=release)
        assert raised.value.code == (
            "readiness_reconciliation_failed"
            if scenario == "scan_failure"
            else "core_readiness_blocked"
        )
        assert existing_service.state.load_installed_release() is None
        assert "publish_release_environment" not in gateway.calls
    assert gateway.requests == (
        []
        if scenario in {"unrelated", "healthy"}
        else [("security_posture", "workflow_knowledge_sync")]
    )
    if scenario == "delayed":
        assert budgets == [60.0, 58.0, 56.0]
        assert sleeps == [2.0, 2.0]
        assert probes == [0.0, 0.0, 2.0, 4.0]
    elif scenario in {"slow_probe", "late_green"}:
        assert budgets == [60.0]
        assert probes == [0.0, 0.0]
        assert sleeps == []
    elif scenario == "still_stale":
        assert len(probes) == 31
        assert now[0] == 58.0
    else:
        assert len(probes) == 1
        assert sleeps == []


def test_installer_missing_bounded_reprobe_refuses_refresh_and_publication(tmp_path: Path) -> None:
    gateway = _installed_gateway(_stale())
    existing_service = _installed_service(tmp_path, gateway)
    service = InstallerService(
        gateway=gateway,
        state=existing_service.state,
        project_identity="flow-steward-test",
        readiness_service=CoreReadinessService(
            probe=lambda: {
                "checks": [{"id": "security_posture", "category": "core", "status": "fail"}]
            }
        ),
    )
    config = InstallConfig()
    target_release = _release_identity("0.9.1", "2")
    with pytest.raises(InstallerFailure) as raised:
        service.install(config, target_release=target_release)
    assert raised.value.code == "core_readiness_blocked"
    assert "publish_release_environment" not in gateway.calls
    assert existing_service.state.load_installed_release() is None


def _stale(*, resumes: bool = False) -> ImageCurrency:
    return ImageCurrency(
        source_revision=_NEW,
        running_revision=_OLD,
        update_required=True,
        resumes_prepared_update=resumes,
        reason="the sources moved",
    )


def test_a_stale_installation_is_updated_not_reported_as_already_installed(
    tmp_path: Path,
) -> None:
    """The measured case: "already installed", runtime recreated on the old image."""
    gateway = _installed_gateway(_stale())
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "installed"
    assert result.updated_from_revision == _OLD
    assert result.updated_to_revision == _NEW
    calls = gateway.calls
    # The image is prepared, the schema is checked by the new image, and only then does
    # a runtime start on it.
    assert calls.index("prepare_core") < calls.index("verify_schema") < calls.index("start_runtime")
    # The fresh-install preflight refuses a running installation's own ports.
    assert "update_preflight" in calls
    assert "preflight" not in calls
    # The update is the owner's decision to keep conservative: nothing that belongs to
    # the installation is regenerated, and no step applies a migration or a backup.
    for untouched in (
        "ensure_configuration",
        "ensure_secrets",
        "ensure_tls",
        "capture_initial_manifest",
    ):
        assert untouched not in calls, untouched
    # The "already installed" path restarts the runtime from the recorded image; on a
    # stale installation that is the defect, not a step.
    assert "activate_worker_identity" not in calls
    assert gateway.storage_read_only is True
    # The install loop requires a pending first-administrator bootstrap, and the real
    # gateway raises when there is none -- which on an installation in use is always.
    assert gateway.require_first_admin is False
    assert any("no database migration" in warning for warning in result.warnings)


def test_failed_update_keeps_previous_installed_identity_and_pending_target(
    tmp_path: Path,
) -> None:
    gateway = _installed_gateway(_stale())
    gateway.fail_step = "verify_public_route"
    service = _installed_service(tmp_path, gateway)
    store = service.state
    previous = _release_identity("1.2.3", "1")
    target = _release_identity("1.3.0", "2")
    store.stage_pending_release(previous)
    store.publish_installed_release(previous)
    config = InstallConfig()

    with pytest.raises(InstallerFailure):
        service.install(config, target_release=target)

    assert store.load_installed_release() == previous
    assert store.load_pending_release() == target


def test_verified_update_publishes_target_identity_after_shared_readiness(
    tmp_path: Path,
) -> None:
    gateway = _installed_gateway(_stale())
    service = _installed_service(tmp_path, gateway)
    store = service.state
    target = _release_identity("1.3.0", "2")

    result = service.install(InstallConfig(), target_release=target)

    assert result.status == "installed"
    assert store.load_installed_release() == target
    assert store.load_pending_release() is None
    assert "ensure_cli_runtime" not in gateway.calls
    log_text = next(store.logs_dir.glob("*.log")).read_text(encoding="utf-8")
    assert "Deferred reusable CLI replacement" in log_text


@pytest.mark.parametrize("previous_official", (False, True))
def test_changed_release_identity_prepares_images_even_when_source_revision_is_current(
    tmp_path: Path, previous_official: bool
) -> None:
    """Same-commit source and official artifacts are not the same installed release."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)
    if previous_official:
        previous = _release_identity("1.2.3", "2")
        service.state.stage_pending_release(previous)
        service.state.publish_installed_release(previous)
    target = _release_identity("1.3.0", "2")

    result = service.install(InstallConfig(), target_release=target)

    assert result.status == "installed"
    calls = gateway.calls
    assert (
        calls.index("prepare_core")
        < calls.index("precheck_migrations")
        < calls.index("start_core")
        < calls.index("ensure_worker_identity")
        < calls.index("start_runtime")
    )
    assert "activate_worker_identity" not in calls
    assert "ensure_cli_runtime" not in calls
    assert service.state.load_installed_release() == target
    assert service.state.load_pending_release() is None
    assert any("Applied official release 1.3.0" in warning for warning in result.warnings)


def test_current_official_identity_does_not_repeat_release_preparation(tmp_path: Path) -> None:
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)
    target = _release_identity("1.3.0", "2")
    service.state.stage_pending_release(target)
    service.state.publish_installed_release(target)

    result = service.install(InstallConfig(), target_release=target)

    assert result.status == "already_installed"
    assert "prepare_core" not in gateway.calls
    assert service.state.load_installed_release() == target
    assert service.state.load_pending_release() is None


def test_an_update_that_needs_a_migration_leaves_the_running_version_alone(
    tmp_path: Path,
) -> None:
    """The safety property: a schema refusal must stop before any runtime restarts."""
    gateway = _installed_gateway(_stale())
    gateway.fail_step = "verify_schema"
    service = _installed_service(tmp_path, gateway)

    config = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(config)

    assert "verify_schema" in gateway.calls
    assert "start_runtime" not in gateway.calls
    assert "activate_worker_identity" not in gateway.calls


def test_finishing_a_prepared_update_does_not_claim_there_was_no_migration(
    tmp_path: Path,
) -> None:
    """The run after a schema refusal may be the one after the operator migrated."""
    gateway = _installed_gateway(_stale(resumes=True))
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "installed"
    assert result.updated_to_revision == _NEW
    assert not any("no database migration" in warning for warning in result.warnings)


def test_an_installation_on_pinned_images_behind_the_sources_says_so(tmp_path: Path) -> None:
    """Not ours to rebuild, and not ours to call current either."""
    gateway = _installed_gateway(
        ImageCurrency(source_revision=_NEW, running_revision=_OLD, update_required=False)
    )
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "already_installed"
    assert "prepare_core" not in gateway.calls
    assert any(_OLD in warning and _NEW in warning for warning in result.warnings)


def test_a_current_installation_carries_no_update_notice(tmp_path: Path) -> None:
    """The green direction: an up-to-date installation must not be told it is behind."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "already_installed"
    assert result.updated_to_revision == ""
    assert not any(_NEW in warning for warning in result.warnings)


def test_an_update_drops_the_superseded_worker_credential_before_the_runtime_restarts(
    tmp_path: Path,
) -> None:
    """AC17-R142: done where the runtime is recreated anyway, and nowhere else."""
    gateway = _installed_gateway(_stale())
    service = _installed_service(tmp_path, gateway)

    service.install(InstallConfig())

    calls = gateway.calls
    assert calls.index("drop_superseded_worker_credential") < calls.index("start_runtime")


def test_a_current_installation_leaves_the_env_file_alone(tmp_path: Path) -> None:
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "already_installed"  # non-vacuity: the no-op path ran
    assert "drop_superseded_worker_credential" not in gateway.calls


def test_an_already_installed_run_refuses_an_unrecorded_worker_count_first(
    tmp_path: Path,
) -> None:
    """W2 (auditor): this path silently brought a bare `--scale` back to the recorded count."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.fail_step = "refuse_unrecorded_worker_scale"
    service = _installed_service(tmp_path, gateway)

    config = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(config)

    assert "refuse_unrecorded_worker_scale" in gateway.calls
    assert "activate_worker_identity" not in gateway.calls  # nothing was brought up


_APPLIED = InstallConfig(public_host="flow.example.test", public_port=8443)


@pytest.mark.parametrize(
    ("currency", "status"),
    [
        (ImageCurrency(source_revision=_NEW, running_revision=_NEW), "already_installed"),
        (_stale(), "installed"),
    ],
)
def test_an_installed_run_works_from_the_applied_configuration_not_the_install_time_one(
    tmp_path: Path, currency: ImageCurrency, status: str
) -> None:
    """The auditor saw install-state.json keep the install-time proxy_trusted_ips after
    `config apply` had changed them. The journal is a record of the install; what the
    installation runs with is the applied stack.toml, and `config set` changes the
    public route, storage and ingress there too."""
    gateway = _installed_gateway(currency)
    gateway.active_config = _APPLIED
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == status  # non-vacuity: the branch under test ran
    assert result.product_url == "https://flow.example.test:8443"
    assert service.last_config == _APPLIED


def test_an_installed_run_without_an_applied_configuration_keeps_the_recorded_one(
    tmp_path: Path,
) -> None:
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert result.status == "already_installed"
    assert service.last_config == InstallConfig()


def _compact(**fields) -> InstallConfig:
    return InstallConfig(environment="production", deployment_profile="compact", **fields)


def test_an_installed_compact_applies_a_new_worker_process_count(tmp_path: Path) -> None:
    """AC17-R157, the owner's decision: `./install.sh --worker-processes N` on an
    installed Compact writes N, recreates the runtime, checks that N workers registered,
    and says so. The step-1 refusal stays for every other setting."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.active_config = _compact(worker_processes=2)
    gateway.converged = "Compact worker processes: 2 -> 1."
    service = _installed_service(tmp_path, gateway)

    result = service.install(
        _compact(worker_processes=1),
        explicit_fields=frozenset({"worker_processes", "deployment_profile"}),
    )

    assert result.status == "already_installed"
    calls = gateway.calls
    assert calls.index("set_compact_worker_processes:1") < calls.index(
        "converge_compact_worker_processes:1"
    )
    assert "Compact worker processes: 2 -> 1." in result.changes  # said on screen


def test_an_installed_run_refuses_a_setting_it_would_not_apply(tmp_path: Path) -> None:
    """AC17-R157: a flag an installed run would drop is refused by name, never ignored."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.active_config = _compact(worker_processes=2)
    service = _installed_service(tmp_path, gateway)

    config = _compact(worker_processes=1, public_port=8443)
    fields = frozenset({"worker_processes", "public_port"})
    with pytest.raises(InstallerFailure) as raised:
        service.install(config, explicit_fields=fields)

    assert raised.value.code == "installed_setting_not_applied"
    assert "Public port: 8099 -> 8443" in raised.value.cause
    assert "worker processes" not in raised.value.cause.lower()  # that one is applied
    assert not any(call.startswith("set_compact_worker_processes") for call in gateway.calls)


@pytest.mark.parametrize(
    "explicit_fields",
    [frozenset(), frozenset({"worker_processes"})],
    ids=["no-flags", "same-value"],
)
def test_an_installed_run_with_nothing_to_change_proceeds(
    tmp_path: Path, explicit_fields: frozenset[str]
) -> None:
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.active_config = _compact(worker_processes=2)
    service = _installed_service(tmp_path, gateway)

    result = service.install(
        _compact(worker_processes=2),
        explicit_fields=explicit_fields,
    )

    assert result.status == "already_installed"


def test_a_verified_update_prunes_superseded_releases_keeping_its_rollback(
    tmp_path: Path,
) -> None:
    """AC17-R158: 48 GB of releases after one day of updates on a live Scalable."""
    gateway = _installed_gateway(_stale())
    gateway.prune_summary = "Removed 2 superseded releases and 10 candidate images."
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert gateway.pruned_with == (_NEW, _OLD)  # the new release and the one it replaced
    assert gateway.recorded_releases == (_NEW, _OLD)  # AC17-R159: what later runs keep
    assert gateway.prune_summary in result.changes
    calls = gateway.calls
    assert calls.index("prune_superseded_releases") > calls.index("verify_health")


def test_a_failed_update_prunes_nothing(tmp_path: Path) -> None:
    gateway = _installed_gateway(_stale())
    gateway.fail_step = "start_runtime"
    service = _installed_service(tmp_path, gateway)

    config = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(config)

    assert "prune_superseded_releases" not in gateway.calls


def test_an_update_checks_its_migrations_before_restarting_any_core_service(
    tmp_path: Path,
) -> None:
    """AC17-R160: an update with a migration recreated Postgres (502s on the live site)
    and only then stopped at the schema check."""
    gateway = _installed_gateway(_stale())
    gateway.fail_step = "precheck_migrations"
    service = _installed_service(tmp_path, gateway)

    config = InstallConfig()
    with pytest.raises(InstallerFailure):
        service.install(config)

    assert "precheck_migrations" in gateway.calls
    assert "start_core" not in gateway.calls  # nothing was restarted


def test_an_update_that_passes_its_precheck_starts_the_core_after_it(tmp_path: Path) -> None:
    gateway = _installed_gateway(_stale())
    service = _installed_service(tmp_path, gateway)

    service.install(InstallConfig())

    assert gateway.calls.index("precheck_migrations") < gateway.calls.index("start_core")


def test_finishing_an_update_an_earlier_run_left_is_not_reported_as_a_to_a(
    tmp_path: Path,
) -> None:
    """R159 acceptance: "Updated from A to A" after a stopped update."""
    gateway = _installed_gateway(
        ImageCurrency(
            source_revision=_NEW,
            running_revision=_NEW,
            update_required=True,
            resumes_prepared_update=True,
            reason="the recorded images are not this release's",
        )
    )
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert not any("Updated from" in line for line in result.warnings)
    assert any("that an earlier run left unfinished" in line for line in result.warnings)


def test_an_installed_run_brings_a_stopped_stack_back_up(tmp_path: Path) -> None:
    """AC17-R162: after `stack stop` (the runbook's restore) ./install.sh failed with
    "fs-app: missing" -- it only checked health -- and the site stayed down."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.started = "Started the stopped services: control-plane, fs-app."
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    calls = gateway.calls
    assert calls.index("start_stopped_services") < calls.index("ensure_worker_identity")
    assert "Started the stopped services: control-plane, fs-app." in result.changes


def test_a_worker_count_applied_this_run_is_reported_whoever_recreated_the_runtime(
    tmp_path: Path,
) -> None:
    """R157 acceptance on Compact: an earlier step recreated the runtime with the new
    count, so convergence found nothing to do and the change was never said."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.active_config = _compact(worker_processes=2)
    gateway.converged = ""  # already running the new count
    service = _installed_service(tmp_path, gateway)

    result = service.install(
        _compact(worker_processes=1), explicit_fields=frozenset({"worker_processes"})
    )

    assert "Compact worker processes: 2 -> 1." in result.changes
    assert gateway.converge_changed is True  # still waits until exactly 1 registered


def test_an_update_of_a_stopped_runtime_does_not_name_an_image_that_never_existed(
    tmp_path: Path,
) -> None:
    """AC17-R162: after a stop or a restore, "Updated from an undated image"."""
    gateway = _installed_gateway(
        ImageCurrency(
            source_revision=_NEW,
            running_revision="",
            update_required=True,
            reason="the images were built from older sources",
        )
    )
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert not any("undated" in line or "Updated from" in line for line in result.warnings)
    assert any(
        line.startswith(f"Updated to {_NEW};") and "not known" in line for line in result.warnings
    )


def test_images_pinned_in_the_shell_are_named_and_the_way_out_said(tmp_path: Path) -> None:
    """AC17-R162: the runbook's `export FS_COMPACT_IMAGE=...` before a rollback's
    ./install.sh made the newer images the operator's -- "already installed", runtime
    unchanged, nothing on screen to say why."""
    gateway = _installed_gateway(
        ImageCurrency(
            source_revision=_NEW,
            running_revision=_OLD,
            shell_pins=("FS_COMPACT_IMAGE",),
        )
    )
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    (notice,) = [line for line in result.warnings if "shell that ran ./install.sh" in line]
    assert "FS_COMPACT_IMAGE is set" in notice
    assert "Unset FS_COMPACT_IMAGE and rerun ./install.sh" in notice


def test_only_real_warnings_reach_the_operator_and_they_say_what_to_do(tmp_path: Path) -> None:
    """AC17-R162 acceptance: the "already installed" screen showed recommendations as bare
    Title Case names -- "Warning: Worker Availability" -- which told nobody anything."""
    gateway = FakeGateway()
    readiness = CoreReadinessService(
        probe=lambda: {
            "checks": [
                {"id": "database", "category": "core", "status": "pass"},
                {
                    "id": "embedding_provider",
                    "category": "ai",
                    "status": "warn",
                    "title": "embedding provider",
                    "detail": "No embedding provider is configured.",
                    "remediation": "Add one in Provider settings.",
                },
                # what the auditor saw as "Warning: Worker Availability" was a passing check
                {"id": "worker_manager", "title": "Worker availability", "status": "pass"},
                {"id": "sonarqube", "category": "ai", "status": "pass"},
            ]
        }
    )
    service, _store = _service(tmp_path, gateway, readiness_service=readiness)

    result = service.install(InstallConfig())

    readiness_lines = [line for line in result.warnings if "provider" in line.lower()]
    assert readiness_lines == [
        "Embedding provider: No embedding provider is configured. Add one in Provider settings."
    ]
    assert not any("Worker" in line or "Sonarqube" in line for line in result.warnings)


def test_a_requested_profile_switch_runs_and_then_verifies_the_new_profile(
    tmp_path: Path,
) -> None:
    """Item 5, step 4: ./install.sh --deployment-profile compact on an installed Scalable
    switches, then verifies the installation as it now is -- no refusal by name."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.active_config = InstallConfig(environment="production", file_storage="local_fs")
    service = _installed_service(tmp_path, gateway)

    result = service.install(
        _compact(file_storage="local_fs"), explicit_fields=frozenset({"deployment_profile"})
    )

    calls = gateway.calls
    assert calls.index("normalize_switch_host_trees") < calls.index("write_switch_pending")
    assert calls.index("apply_switch_in_cli:scalable") < calls.index("verify_health")
    assert gateway.pending_switch.data_placement == "host_directories"
    assert result.changes[0].startswith("Normalized 1 directories")
    assert result.changes[1].startswith("Switched from scalable to compact")
    assert result.status == "already_installed"


def test_an_unfinished_switch_is_continued_whatever_the_run_was_asked(tmp_path: Path) -> None:
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    gateway.unfinished_switch = {"deployment_profile": "compact"}
    gateway.active_config = InstallConfig(environment="production", file_storage="local_fs")
    service = _installed_service(tmp_path, gateway)

    result = service.install(InstallConfig())

    assert gateway.calls.count("apply_switch_in_cli:scalable") == 1
    assert "write_switch_pending" not in gateway.calls  # continued, not started again
    assert result.changes[0].startswith("Completed the switch to compact")


def test_no_switch_is_asked_of_an_installation_that_stays(tmp_path: Path) -> None:
    """The green direction: the same profile, given or not, is no switch."""
    gateway = _installed_gateway(ImageCurrency(source_revision=_NEW, running_revision=_NEW))
    service = _installed_service(tmp_path, gateway)

    service.install(InstallConfig(), explicit_fields=frozenset({"deployment_profile"}))

    assert not any("switch" in call for call in gateway.calls)
