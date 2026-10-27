"""Cross-mode vertical-slice contracts for the schema-driven public CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from core.application.management.command_specs.contracts import (
    CommandSpec,
    InteractivePolicy,
    OperationProgressEvent,
    OperationStepStatus,
)
from core.application.management.command_specs.extensions import ExtensionUninstallCommandInput
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.modes import TerminalFacts

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CLI_CASE_IDS_IN_REGISTRY_ORDER = tuple(spec.command_id for spec in PUBLIC_COMMAND_SPECS)


@dataclass(frozen=True)
class CommandCase:
    command_id: str
    args: list[str]
    textual_args: list[str]
    expected: dict[str, Any]
    secret_root: Path | None = None
    stdin: str = ""
    invocation_model_class: type[Any] | None = None
    prepared_clone: Any = None
    form_values: dict[str, Any] | None = None
    confirmation_value: str = ""


@dataclass
class ServiceCall:
    key: str
    model_class: type[Any]
    values: dict[str, Any]


class RecordingRegistry:
    def __init__(self, *, prepared_clone: Any = None) -> None:
        self.calls: list[ServiceCall] = []
        self.progress_events: list[OperationProgressEvent] = []
        self.fail_with: BaseException | None = None
        self.prepared_clone = prepared_clone
        self.clone_staging_cleaned = False

    async def build_plan(self, key: str, value: Any) -> Any:
        from core.entrypoints.cli.platform.service_bindings.common import PreparedCommand

        if key == "clone.restore.preview":
            from core.application.management.command_specs.clone import CloneRestoreExecutionInput

            assert self.prepared_clone is not None
            assert value.archive_path == self.prepared_clone.archive_path
            assert value.passphrase_stdin is True
            assert value.operation_id == self.prepared_clone.operation_id
            plan = self.prepared_clone.plan
            execution = CloneRestoreExecutionInput(
                prepared=self.prepared_clone,
                target_installation_id=plan.target_installation_id,
                target_profile=plan.target_profile,
                public_host=plan.retained_shell.configuration.public_host,
                plan_digest=plan.plan_digest,
            )
            return PreparedCommand(
                execution_input=execution,
                review=execution.model_dump(exclude={"prepared"}),
                cleanup=self.discard_clone_staging,
            )
        if key == "extensions.uninstall.preview":
            return PreparedCommand(
                execution_input=ExtensionUninstallCommandInput(
                    extension_id=value.extension_id,
                    plan_revision="rev-1",
                ),
                review={
                    "extension_id": value.extension_id,
                    "revision": "rev-1",
                    "remove": [],
                    "affected": [],
                    "retained": [],
                },
            )
        return PreparedCommand(execution_input=value, review={"operation": key})

    def discard_clone_staging(self) -> None:
        self.clone_staging_cleaned = True

    async def invoke(
        self,
        key: str,
        value: Any,
        *,
        progress_sink: Any = None,
        technical_sink: Any = None,
        cancellation: Any = None,
    ) -> dict[str, Any]:
        del cancellation
        self.calls.append(
            ServiceCall(
                key=key,
                model_class=type(value),
                values=value.model_dump(mode="json"),
            )
        )
        if self.fail_with is not None:
            raise self.fail_with
        if key in {"readiness.status", "readiness.doctor"}:
            return {
                "status": "ready",
                "findings": [],
                "core_passed": 1,
                "core_failed": 0,
                "optional_warnings": 0,
                "recommendations": 0,
                "process_exit_code": 0,
            }
        if key == "maintenance.status":
            return {
                "status": "disabled",
                "activated_at": None,
                "owner_kind": None,
            }
        if key == "maintenance.enable":
            return {
                "status": "enabled",
                "changed": True,
                "activated_at": "2026-08-25T00:00:00Z",
                "owner_kind": "manual",
                "next_steps": (),
            }
        if key == "maintenance.disable":
            return {
                "status": "disabled",
                "changed": True,
                "activated_at": None,
                "owner_kind": None,
                "next_steps": (),
            }
        if key == "users.issue-bootstrap-token":
            return {
                "status": "issued",
                "output_path": ".flow-steward/secret-output/bootstrap-token",
                "expires_at": "2026-07-23T12:00:00Z",
                "header_name": "x-platform-setup-token",
            }
        if key == "users.list":
            return {
                "users": [
                    {
                        "id": "u_1",
                        "username": "operator@example.com",
                        "email": "operator@example.com",
                        "roles": ["operator"],
                    }
                ],
                "total": 1,
            }
        if key == "users.create":
            return {
                "status": "created",
                "user": {
                    "id": "u_2",
                    "email": value.email,
                    "roles": value.roles,
                },
            }
        if key.startswith("users."):
            return {
                "status": key.removeprefix("users.").replace("-", "_"),
                "user": {
                    "id": "u_2",
                    "email": getattr(value, "email", "operator@example.com"),
                    "roles": getattr(value, "roles", []),
                    "is_active": getattr(value, "is_active", True),
                },
                "revoked_sessions": 1 if key == "users.set-password" else 0,
            }
        if key == "extensions.list":
            return {"extensions": [{"extension_id": "acme.crm-sync", "state": value.state}]}
        if key == "extensions.show":
            return {"extension_id": value.extension_id, "extension_name": "CRM Sync"}
        if key == "extensions.health":
            return {
                "extension_id": value.extension_id,
                "health": {"status": "healthy"},
            }
        if key == "extensions.install":
            return {
                "extension": {"extension_id": "acme.crm-sync"},
                "installed_path": str(value.archive),
            }
        if key == "extensions.uninstall":
            return {"extension_id": value.extension_id, "state": "uninstalled", "removed_data": []}
        if key in {"extensions.enable", "extensions.disable"}:
            return {
                "extension": {
                    "extension_id": value.extension_id,
                    "state": "active" if key == "extensions.enable" else "disabled",
                }
            }
        if key == "extensions.create":
            return {
                "status": "created",
                "extension_id": value.extension_id,
                "path": str(Path(value.output_root) / value.extension_id.replace(".", "_")),
                "files_created": 6,
            }
        if key in {
            "extensions.validate",
            "extensions.test",
            "extensions.bundle",
            "extensions.lock-deps",
        }:
            return {
                "status": "passed" if key in {"extensions.validate", "extensions.test"} else "ok",
                "extension_id": value.extension_id,
                "exit_code": 0,
                "output": "",
            }
        if key == "projects.list":
            return {
                "projects": [{"id": "proj_1", "name": "Project One"}],
                "total": 1,
                "page": value.page,
                "per_page": value.per_page,
                "total_pages": 1,
            }
        if key == "projects.show":
            return {"project": {"id": value.project_id, "name": "Project One"}, "jobs": []}
        if key == "projects.notification-routes":
            return {
                "project_id": value.project_id,
                "notification_routes": [{"id": "route_1", "enabled": value.enabled_only}],
            }
        if key == "projects.notification-deliveries":
            return {
                "project_id": value.project_id,
                "job_id": value.job_id or None,
                "notification_deliveries": [{"id": "delivery_1"}],
            }
        if key == "projects.dispatch-notification":
            return {
                "project_id": value.project_id,
                "notification_deliveries": [{"id": "delivery_1", "event_type": value.event_type}],
            }
        if key == "quality.lint":
            if progress_sink is not None:
                for event in (
                    OperationProgressEvent(
                        step_id="queued",
                        status=OperationStepStatus.RUNNING,
                        label="Lint queued",
                    ),
                    OperationProgressEvent(
                        step_id="complete",
                        status=OperationStepStatus.SUCCEEDED,
                        label="Complete",
                        detail="4/4 checks",
                        completed=4,
                        total=4,
                    ),
                ):
                    self.progress_events.append(event)
                    progress_sink(event)
            if technical_sink is not None:
                technical_sink("worker detail password=StrongPass_123!")
        if key == "workflows.list":
            return {
                "project_id": value.project_id,
                "workflows": [
                    {
                        "workflow_id": "wf.release",
                        "display_name": "Release",
                        "enabled": not value.include_disabled,
                    }
                ],
            }
        if key == "workflows.entrypoints":
            return {
                "project_id": value.project_id,
                "entrypoints": [{"entry_key": "release", "workflow_id": "wf.release"}],
            }
        if key == "workflows.validate":
            return {"workflow_id": value.workflow_id, "validation": {"valid": True}, "issues": []}
        if key == "workflows.contract":
            return {"workflow_id": value.workflow_id, "interface": {"input": {}}}
        if key == "workflows.suggestions":
            return {"workflow_id": value.workflow_id, "suggestions": []}
        if key == "jobs.list":
            return {
                "jobs": [{"job_id": "job_1", "status": value.status or "in_progress"}],
                "total": 1,
                "page": 1,
                "per_page": value.limit,
                "total_pages": 1,
                "project_id": value.project_id or None,
                "status": value.status or None,
            }
        if key == "jobs.show":
            return {"id": value.job_id, "status": "awaiting_approval", "thread_id": "thread_1"}
        if key == "jobs.status":
            return {"id": value.job_id, "status": "in_progress", "task_summary": "Run task"}
        if key == "jobs.logs":
            return {"logs": [{"id": value.since_id or "1"}], "status": "in_progress"}
        if key == "jobs.messages":
            return {"messages": [{"id": value.since_id or "1"}], "last_id": "1"}
        if key == "jobs.errors":
            return {"job_id": value.job_id, "errors": [{"id": "err_1"}]}
        if key in {"jobs.approve", "jobs.reject"}:
            return {
                "job_id": "job_1",
                "thread_id": value.thread_id,
                "approval_id": value.approval_id,
                "approved": key == "jobs.approve",
                "action": value.action,
                "comment": value.comment,
                "override_content": "",
                "actor": "operator",
                "status": "in_progress",
            }
        if key == "jobs.ack":
            return {"job_id": value.job_id, "status": "acknowledged"}
        if key == "jobs.unack":
            return {"job_id": value.job_id, "status": "unacknowledged"}
        if key == "jobs.cancel":
            return {"job_id": value.job_id, "status": "cancelled", "cancelled": True}
        if key == "jobs.restart":
            return {"job_id": value.job_id, "status": "pending", "new_job_id": "job_2"}
        if key == "jobs.health-snapshot":
            return {"ok": True, "service": {"ok": True}, "database": {"ok": True}}
        if key == "reports.generate":
            return {
                "id": None if not value.persist else "report_1",
                "report_type": value.report_type,
                "status": "generated",
                "params": value.params,
                "summary_text": "ok",
                "payload": {},
                "chart_payload": {},
                "directives": [],
                "created_at": "2026-07-17T00:00:00+00:00",
            }
        if key == "reports.get":
            return {
                "id": value.report_id,
                "report_type": "activity_daily",
                "status": "generated",
                "params": {},
                "summary_text": "saved",
                "payload": {},
                "chart_payload": {},
                "directives": [],
                "created_at": "2026-07-17T00:00:00+00:00",
            }
        if key == "reports.diagnostics":
            return {"report": {"schema": "flow-steward.diagnostics.v1", "host": value.host}}
        if key.startswith("database."):
            return {
                "operation": key,
                "status": "completed",
                "changed": key
                not in {"database.status", "database.query-diagnostics", "database.tune-env"},
                "message": "ok",
                "details": value.model_dump(mode="json"),
                "artifacts": [],
            }
        if key.startswith(("testing.", "quality.", "localization.")):
            return {
                "operation": key,
                "status": "completed",
                "exit_code": 0,
                "command": [],
                "stdout": "",
                "stderr": "",
                "artifacts": [],
                "details": value.model_dump(mode="json"),
            }
        if key == "stack.status":
            return {
                "action": "status",
                "status": "observed",
                "project_name": "installed-project",
            }
        if key in {"stack.start", "stack.stop", "stack.restart", "stack.rebuild"}:
            action = key.removeprefix("stack.")
            return {
                "action": action,
                "status": "completed",
                "project_name": "installed-project",
                "target_services": ["fs-app"],
                "operation_id": f"operation-{action}",
                "journal_path": f"/tmp/{action}.json",
                "log_path": f"/tmp/{action}.log",
                "preservation_verified": True,
            }
        if key == "stack.logs":
            return {
                "service": value.service,
                "tail": value.tail,
                "follow": value.follow,
                "lines_streamed": 1,
            }
        if key == "environment.show":
            return {
                "status": "active",
                "active": {"environment": "development"},
                "active_environment": "development",
                "source_release": "4.8.2",
                "target_release": "4.8.2",
            }
        if key == "environment.switch":
            return {
                "status": "active",
                "active": {"environment": value.target},
                "active_environment": value.target,
                "source_environment": "development",
                "source_release": "4.8.2",
                "target_release": "4.8.2",
                "operation_id": "environment-operation",
                "journal_path": "/tmp/environment-operation.json",
                "log_path": "/tmp/environment-operation.log",
                "preservation_verified": True,
            }
        if key == "release.info":
            return {
                "status": "installed",
                "active_environment": "development",
                "current_version": "1.0.0",
            }
        if key == "release.check":
            return {
                "status": "upgrade_available",
                "active_environment": "development",
                "current_version": "1.0.0",
                "target_version": "2.0.0",
            }
        if key == "release.upgrade":
            return {
                "status": "upgraded",
                "active_environment": "development",
                "current_version": "1.0.0",
                "target_version": value.target_version or "2.0.0",
                "operation_id": "release-operation",
                "log_path": "/tmp/release-operation.log",
            }
        if key == "clone.create":
            assert self.prepared_clone is not None
            return {
                "operation_id": "clone-create-test",
                "path": "backups/installation.fsclone",
                "size_bytes": 4096,
                "sha256": "e" * 64,
                "release": self.prepared_clone.exact_release.model_dump(mode="json"),
                "log_path": "/tmp/clone-create-test.log",
            }
        if key == "clone.restore":
            return {
                "status": "restored",
                "operation_id": value.prepared.operation_id,
                "target_installation_id": value.target_installation_id,
                "release_version": "1.2.3",
                "plan_digest": value.plan_digest,
                "maintenance_retained": True,
                "log_path": "/tmp/clone-restore-test.log",
            }
        if key in {"backup.create", "backup.verify"}:
            return {
                "path": "backups/flow-steward-20260725T060000Z.dump.enc",
                "size_bytes": 4096,
                "created_at": "2026-07-25T06:00:00Z",
                "verification_status": "verified",
                "verified_at": "2026-07-25T06:00:00Z",
                "log_path": "logs/runtime/backup.log",
            }
        if key == "backup.list":
            return {"backups": []}
        if key == "config.proxy-fragment":
            return {
                "proxy_kind": "nginx",
                "proxy_placement": "host",
                "public_host": "flow.example.test",
                "app_backend": "127.0.0.1:8099",
                "mcp_backend": "127.0.0.1:8100",
                "fragment": "location / { proxy_pass http://127.0.0.1:8099; }",
            }
        if key == "config.show":
            return {"active": {"public_host": "flow.example.test"}}
        if key == "config.validate":
            return {"valid": True, "issues": []}
        if key == "config.set":
            return {"pending": value.model_dump(mode="json")}
        if key == "config.apply":
            return {
                "active": {"public_host": "flow.example.test", "public_port": 443},
                "operation_id": "config-operation",
                "journal_path": "/tmp/config-operation.json",
                "log_path": "/tmp/config-operation.log",
                "preservation_verified": True,
            }
        raise AssertionError(f"Unexpected service key {key!r}")

    async def choices(self, key: str) -> tuple[Any, ...]:
        from core.application.management.command_specs.contracts import ChoiceItem

        if key == "accounts.available":
            return (ChoiceItem(value="acct_1", label="Account One"),)
        if key == "projects.available":
            return (ChoiceItem(value="proj_1", label="Project One"),)
        return ()


def _specs() -> dict[str, CommandSpec]:
    return {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS}


def assert_case_registry_parity(
    specs: tuple[CommandSpec, ...],
    cases: dict[str, CommandCase],
    *,
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    spec_ids = {
        spec.command_id for spec in specs if not spec.command_id.startswith(excluded_prefixes)
    }
    case_ids = {command_id for command_id in cases if not command_id.startswith(excluded_prefixes)}
    assert case_ids == spec_ids, {
        "missing": sorted(spec_ids - case_ids),
        "stale": sorted(case_ids - spec_ids),
    }


def _app(registry: RecordingRegistry | None = None) -> tuple[Any, RecordingRegistry]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    registry = registry or RecordingRegistry()
    return build_click_app(registry, SimpleNamespace()), registry


def _terminal(*, stdin: bool, stdout: bool, columns: int = 100, rows: int = 30) -> TerminalFacts:
    return TerminalFacts(
        stdin_isatty=stdin,
        stdout_isatty=stdout,
        columns=columns,
        rows=rows,
        term="xterm-256color",
        no_color_env=False,
    )


def _json_payload(output: str) -> dict[str, Any]:
    return json.loads(output.splitlines()[-1])


def _strip_ansi(output: str) -> str:
    return ANSI_RE.sub("", output)


def _clone_restore_fixture(tmp_path: Path):
    """Typed, already-staged boundary fixture; never reads/restores an installation."""
    from datetime import UTC, datetime

    from core.application.clone.contracts import CloneDataClass, CloneEntry, CloneManifest
    from core.application.clone.planning import (
        CloneReplacementPlan,
        CloneTargetShell,
        PreparedCloneRestore,
    )
    from core.application.lifecycle.contracts import DeploymentConfig
    from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
    from core.application.release.images import required_product_roles

    digest = "sha256:" + "1" * 64
    release = ReleaseIdentity(
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
    manifest = CloneManifest(
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
        release=release,
        deployment_profile="scalable",
        data_placement="host_directories",
        file_storage="local_fs",
        source_installation_id="source-installation",
        entries=(
            CloneEntry(
                data_class=CloneDataClass.POSTGRESQL,
                path="postgresql/database.dump",
                entry_type="file",
                size=5,
                mode=0o600,
                sha256="f" * 64,
            ),
        ),
        entry_count=1,
        aggregate_size=5,
    )
    shell = CloneTargetShell.from_config(
        installation_id="target-installation",
        compose_project="flow-steward-target",
        config=DeploymentConfig(public_host="target.example.test"),
        host_uid=1000,
        host_gid=1000,
        filesystem_roots={"extensions": "/fixture/extensions"},
        postgres_roles=("flow_steward",),
        postgres_identity_fingerprint="f" * 64,
    )
    plan = CloneReplacementPlan.build(
        source_installation_id="source-installation",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="e" * 64,
        target_shell=shell,
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql",),
        external_effect_categories=(),
    )
    return PreparedCloneRestore(
        archive_path=str(tmp_path / "clone.fsclone"),
        operation_id="clone-restore-test",
        staged_root=str(tmp_path / "staged"),
        manifest=manifest,
        source_configuration=DeploymentConfig(
            deployment_profile="scalable",
            data_placement="host_directories",
            file_storage="local_fs",
        ),
        exact_release=release,
        plan=plan,
    )


@pytest.fixture
def command_cases(tmp_path: Path) -> dict[str, CommandCase]:
    from core.application.management.command_specs.clone import CloneRestoreExecutionInput

    prepared_clone = _clone_restore_fixture(tmp_path)
    secret_root = tmp_path / ".flow-steward/secret-input"
    secret_root.mkdir(parents=True)
    password_file = secret_root / "operator-password"
    password_file.write_text("StrongPass_123!\n", encoding="utf-8")
    password_file.chmod(0o600)
    dsn_file = secret_root / "db-dsn"
    dsn_file.write_text("postgres://example\n", encoding="utf-8")
    dsn_file.chmod(0o600)
    bearer_file = secret_root / "service-bearer"
    bearer_file.write_text("service-token\n", encoding="utf-8")
    bearer_file.chmod(0o600)
    archive = tmp_path / "acme.zip"
    archive.write_bytes(b"zip-bytes")
    output_root = tmp_path / "extensions"
    backup_path = tmp_path / "backups" / "flow-steward.dump.enc"
    cases = {
        "status": CommandCase(
            command_id="status",
            args=["status"],
            textual_args=["status"],
            expected={},
        ),
        "doctor": CommandCase(
            command_id="doctor",
            args=["doctor"],
            textual_args=["doctor"],
            expected={},
        ),
        "proxy-fragment": CommandCase(
            command_id="proxy-fragment",
            args=["proxy-fragment"],
            textual_args=["proxy-fragment"],
            expected={},
        ),
        "stack.status": CommandCase(
            command_id="stack.status",
            args=["stack", "status"],
            textual_args=["stack", "status"],
            expected={},
        ),
        "stack.start": CommandCase(
            command_id="stack.start",
            args=["stack", "start"],
            textual_args=["stack", "start"],
            expected={"adopt_image_pins": False},
        ),
        "stack.stop": CommandCase(
            command_id="stack.stop",
            args=["stack", "stop"],
            textual_args=["stack", "stop"],
            expected={},
        ),
        "stack.restart": CommandCase(
            command_id="stack.restart",
            args=["stack", "restart"],
            textual_args=["stack", "restart"],
            expected={},
        ),
        "stack.rebuild": CommandCase(
            command_id="stack.rebuild",
            args=["stack", "rebuild"],
            textual_args=["stack", "rebuild"],
            expected={},
        ),
        "stack.logs": CommandCase(
            command_id="stack.logs",
            args=["stack", "logs", "--service", "fs-app", "--tail", "20", "--follow"],
            textual_args=[
                "stack",
                "logs",
                "--service",
                "fs-app",
                "--tail",
                "20",
                "--follow",
            ],
            expected={"service": "fs-app", "tail": 20, "follow": True},
        ),
        "environment.show": CommandCase(
            command_id="environment.show",
            args=["environment", "show"],
            textual_args=["environment", "show"],
            expected={},
        ),
        "environment.switch": CommandCase(
            command_id="environment.switch",
            args=["environment", "switch", "production"],
            textual_args=["environment", "switch"],
            expected={
                "target": "production",
                "development_tools": [],
                "file_storage": "keep",
                "s3_endpoint": "",
                "s3_bucket": "",
                "s3_region": "",
                "s3_credentials_file": "",
                "allow_insecure_s3_http": False,
                "artifact_decision": None,
            },
        ),
        "maintenance.status": CommandCase(
            command_id="maintenance.status",
            args=["maintenance", "status"],
            textual_args=["maintenance", "status"],
            expected={},
        ),
        "maintenance.enable": CommandCase(
            command_id="maintenance.enable",
            args=["maintenance", "enable", "--confirm"],
            textual_args=["maintenance", "enable"],
            expected={},
        ),
        "maintenance.disable": CommandCase(
            command_id="maintenance.disable",
            args=["maintenance", "disable", "--confirm"],
            textual_args=["maintenance", "disable"],
            expected={},
        ),
        "release.info": CommandCase(
            command_id="release.info",
            args=["release", "info"],
            textual_args=["release", "info"],
            expected={},
        ),
        "release.check": CommandCase(
            command_id="release.check",
            args=["release", "check"],
            textual_args=["release", "check"],
            expected={},
        ),
        "release.upgrade": CommandCase(
            command_id="release.upgrade",
            args=["release", "upgrade"],
            textual_args=["release", "upgrade"],
            expected={"target_version": "", "dry_run": False, "timeout_seconds": 60},
        ),
        "clone.create": CommandCase(
            command_id="clone.create",
            args=["clone", "create", "--output-directory", "backups", "--passphrase-stdin"],
            textual_args=["clone", "create", "--passphrase-stdin"],
            expected={"output_directory": "backups", "passphrase_stdin": True},
            prepared_clone=prepared_clone,
        ),
        "clone.restore": CommandCase(
            command_id="clone.restore",
            args=[
                "clone",
                "restore",
                prepared_clone.archive_path,
                "--passphrase-stdin",
                "--operation-id",
                "clone-restore-test",
            ],
            textual_args=[
                "clone",
                "restore",
                prepared_clone.archive_path,
                "--passphrase-stdin",
                "--operation-id",
                "clone-restore-test",
            ],
            form_values={
                "archive_path": prepared_clone.archive_path,
                "passphrase_stdin": True,
                "operation_id": "clone-restore-test",
            },
            expected={
                "prepared": prepared_clone.model_dump(mode="json"),
                "target_installation_id": "target-installation",
                "target_profile": "scalable",
                "public_host": "target.example.test",
                "plan_digest": prepared_clone.plan.plan_digest,
            },
            invocation_model_class=CloneRestoreExecutionInput,
            prepared_clone=prepared_clone,
            confirmation_value="target-installation|scalable|target.example.test|"
            + prepared_clone.plan.plan_digest,
        ),
        "backup.create": CommandCase(
            command_id="backup.create",
            args=["backup", "create"],
            textual_args=["backup", "create"],
            expected={"operation_id": None},
        ),
        "backup.list": CommandCase(
            command_id="backup.list",
            args=["backup", "list"],
            textual_args=["backup", "list"],
            expected={},
        ),
        "backup.verify": CommandCase(
            command_id="backup.verify",
            args=["backup", "verify", str(backup_path)],
            textual_args=["backup", "verify", str(backup_path)],
            expected={"backup": str(backup_path)},
        ),
        "config.show": CommandCase(
            command_id="config.show",
            args=["config", "show"],
            textual_args=["config", "show"],
            expected={},
        ),
        "config.validate": CommandCase(
            command_id="config.validate",
            args=["config", "validate"],
            textual_args=["config", "validate"],
            expected={},
        ),
        "config.set": CommandCase(
            command_id="config.set",
            args=["config", "set", "--public-host", "flow.example.test"],
            textual_args=["config", "set", "--public-host", "flow.example.test"],
            expected={
                "deployment_profile": None,
                "runtime_http_port": None,
                "worker_processes": None,
                "worker_replicas": None,
                "data_placement": None,
                "file_storage": None,
                "ingress_mode": None,
                "public_host": "flow.example.test",
                "public_port": None,
                "tls_mode": None,
                "reverse_proxy_backend_host": None,
                "reverse_proxy_backend_port": None,
                "reverse_proxy_mcp_port": None,
                "proxy_placement": None,
                "proxy_kind": None,
                "proxy_trusted_ips": None,
                "traefik_network": None,
                "traefik_https_entrypoint": None,
                "traefik_http_entrypoint": None,
                "traefik_cert_resolver": None,
                "proxy_network": None,
                "acme_contact": None,
                "acme_directory_url": None,
                "extension_installer_origin": None,
                "certificate_path": None,
                "private_key_path": None,
            },
        ),
        "config.apply": CommandCase(
            command_id="config.apply",
            args=["config", "apply"],
            textual_args=["config", "apply"],
            expected={},
        ),
        "users.list": CommandCase(
            command_id="users.list",
            args=["users", "list", "--search", "operator", "--include-inactive"],
            textual_args=["users", "list", "--search", "operator", "--include-inactive"],
            expected={
                "search": "operator",
                "include_inactive": True,
                "include_deleted": False,
                "limit": 50,
                "offset": 0,
            },
        ),
        "users.create": CommandCase(
            command_id="users.create",
            args=[
                "users",
                "create",
                "--email",
                "operator@example.com",
                "--role",
                "operator",
                "--password-file",
                str(password_file),
            ],
            textual_args=["users", "create", "--email", "operator@example.com"],
            expected={
                "email": "operator@example.com",
                "password": "StrongPass_123!",
                "display_name": "",
                "roles": ["operator"],
                "is_active": True,
                "must_change_password": False,
                "account_id": "",
                "account_ids": [],
                "actor": "cli",
            },
            secret_root=secret_root,
        ),
        "users.set-password": CommandCase(
            command_id="users.set-password",
            args=[
                "users",
                "set-password",
                "--email",
                "operator@example.com",
                "--password-file",
                str(password_file),
                "--must-change-password",
                "--revoke-sessions",
            ],
            textual_args=["users", "set-password", "--email", "operator@example.com"],
            expected={
                "user_id": None,
                "email": "operator@example.com",
                "password": "StrongPass_123!",
                "must_change_password": True,
                "revoke_sessions": True,
                "actor": "cli",
            },
            secret_root=secret_root,
        ),
        "users.force-reset": CommandCase(
            command_id="users.force-reset",
            args=["users", "force-reset", "--email", "operator@example.com"],
            textual_args=["users", "force-reset", "--email", "operator@example.com"],
            expected={"user_id": None, "email": "operator@example.com", "actor": "cli"},
        ),
        "users.unlock": CommandCase(
            command_id="users.unlock",
            args=["users", "unlock", "--email", "operator@example.com"],
            textual_args=["users", "unlock", "--email", "operator@example.com"],
            expected={"user_id": None, "email": "operator@example.com", "actor": "cli"},
        ),
        "users.set-active": CommandCase(
            command_id="users.set-active",
            args=["users", "set-active", "--email", "operator@example.com", "--no-is-active"],
            textual_args=["users", "set-active", "--email", "operator@example.com"],
            expected={
                "user_id": None,
                "email": "operator@example.com",
                "is_active": False,
                "actor": "cli",
            },
        ),
        "users.set-roles": CommandCase(
            command_id="users.set-roles",
            args=["users", "set-roles", "--email", "operator@example.com", "--role", "operator"],
            textual_args=[
                "users",
                "set-roles",
                "--email",
                "operator@example.com",
                "--role",
                "operator",
            ],
            expected={
                "user_id": None,
                "email": "operator@example.com",
                "roles": ["operator"],
                "actor": "cli",
            },
        ),
        "users.issue-bootstrap-token": CommandCase(
            command_id="users.issue-bootstrap-token",
            args=["users", "issue-bootstrap-token"],
            textual_args=["users", "issue-bootstrap-token"],
            expected={},
        ),
        "extensions.list": CommandCase(
            command_id="extensions.list",
            args=["extensions", "list", "--state", "disabled"],
            textual_args=["extensions", "list", "--state", "disabled"],
            expected={"state": "disabled"},
        ),
        "extensions.show": CommandCase(
            command_id="extensions.show",
            args=["extensions", "show", "acme.crm-sync"],
            textual_args=["extensions", "show", "acme.crm-sync"],
            expected={"extension_id": "acme.crm-sync"},
        ),
        "extensions.health": CommandCase(
            command_id="extensions.health",
            args=["extensions", "health", "acme.crm-sync"],
            textual_args=["extensions", "health", "acme.crm-sync"],
            expected={"extension_id": "acme.crm-sync"},
        ),
        "extensions.install": CommandCase(
            command_id="extensions.install",
            args=["extensions", "install", "--archive", str(archive)],
            textual_args=["extensions", "install"],
            expected={"archive": str(archive), "approved_browser_authority_impact_hash": ""},
        ),
        "extensions.uninstall": CommandCase(
            command_id="extensions.uninstall",
            args=["extensions", "uninstall", "acme.crm-sync"],
            textual_args=["extensions", "uninstall", "acme.crm-sync"],
            expected={"extension_id": "acme.crm-sync", "plan_revision": "rev-1"},
            invocation_model_class=ExtensionUninstallCommandInput,
        ),
        "extensions.enable": CommandCase(
            command_id="extensions.enable",
            args=["extensions", "enable", "acme.crm-sync"],
            textual_args=["extensions", "enable", "acme.crm-sync"],
            expected={
                "extension_id": "acme.crm-sync",
                "version": "",
                "config": {},
                "approved_browser_authority_impact_hash": "",
            },
        ),
        "extensions.disable": CommandCase(
            command_id="extensions.disable",
            args=["extensions", "disable", "acme.crm-sync"],
            textual_args=["extensions", "disable", "acme.crm-sync"],
            expected={
                "extension_id": "acme.crm-sync",
                "version": "",
                "config": {},
                "approved_browser_authority_impact_hash": "",
            },
        ),
        "extensions.create": CommandCase(
            command_id="extensions.create",
            args=[
                "extensions",
                "create",
                "acme.crm-sync",
                "--output-root",
                str(output_root),
                "--display-name",
                "CRM Sync",
                "--vendor-name",
                "Acme",
                "--category",
                "sales",
            ],
            textual_args=[
                "extensions",
                "create",
                "--output-root",
                str(output_root),
            ],
            expected={
                "extension_id": "acme.crm-sync",
                "output_root": str(output_root),
                "display_name": "CRM Sync",
                "description": "",
                "vendor_name": "Acme",
                "category": "sales",
            },
        ),
        "extensions.validate": CommandCase(
            command_id="extensions.validate",
            args=["extensions", "validate", "acme.crm-sync", "--health"],
            textual_args=["extensions", "validate", "acme.crm-sync", "--health"],
            expected={
                "extension_id": "acme.crm-sync",
                "root": "",
                "health": True,
                "marketplace": False,
            },
        ),
        "extensions.test": CommandCase(
            command_id="extensions.test",
            args=["extensions", "test", "acme.crm-sync", "--allow-no-pytest"],
            textual_args=["extensions", "test", "acme.crm-sync", "--allow-no-pytest"],
            expected={
                "extension_id": "acme.crm-sync",
                "root": "",
                "skip_validate": False,
                "skip_smoke": False,
                "skip_pytest": False,
                "allow_no_pytest": True,
                "test_env": [],
            },
        ),
        "extensions.bundle": CommandCase(
            command_id="extensions.bundle",
            args=["extensions", "bundle", "acme.crm-sync", "--output-dir", str(tmp_path / "dist")],
            textual_args=[
                "extensions",
                "bundle",
                "acme.crm-sync",
                "--output-dir",
                str(tmp_path / "dist"),
            ],
            expected={
                "extension_id": "acme.crm-sync",
                "root": "",
                "out_dir": str(tmp_path / "dist"),
                "version": "",
                "runtime_target": "",
                "with_wheels": False,
                "platforms": "",
            },
        ),
        "extensions.lock-deps": CommandCase(
            command_id="extensions.lock-deps",
            args=["extensions", "lock-deps", "acme.crm-sync", "--input", str(tmp_path / "req.in")],
            textual_args=[
                "extensions",
                "lock-deps",
                "acme.crm-sync",
                "--input",
                str(tmp_path / "req.in"),
            ],
            expected={
                "extension_id": "acme.crm-sync",
                "root": "",
                "input": str(tmp_path / "req.in"),
                "platforms": "",
            },
        ),
        "projects.list": CommandCase(
            command_id="projects.list",
            args=[
                "projects",
                "list",
                "--page",
                "2",
                "--per-page",
                "5",
                "--search",
                "one",
            ],
            textual_args=[
                "projects",
                "list",
                "--page",
                "2",
                "--per-page",
                "5",
                "--search",
                "one",
            ],
            expected={"page": 2, "per_page": 5, "search": "one"},
        ),
        "projects.show": CommandCase(
            command_id="projects.show",
            args=["projects", "show", "--project-id", "proj_1"],
            textual_args=["projects", "show", "--project-id", "proj_1"],
            expected={
                "project_id": "proj_1",
                "include_schema": False,
                "schema_page": 1,
                "schema_per_page": 25,
            },
        ),
        "projects.notification-routes": CommandCase(
            command_id="projects.notification-routes",
            args=[
                "projects",
                "notification-routes",
                "--project-id",
                "proj_1",
                "--enabled-only",
            ],
            textual_args=[
                "projects",
                "notification-routes",
                "--project-id",
                "proj_1",
                "--enabled-only",
            ],
            expected={"project_id": "proj_1", "enabled_only": True},
        ),
        "projects.notification-deliveries": CommandCase(
            command_id="projects.notification-deliveries",
            args=[
                "projects",
                "notification-deliveries",
                "--project-id",
                "proj_1",
                "--job-id",
                "job_1",
                "--limit",
                "5",
            ],
            textual_args=[
                "projects",
                "notification-deliveries",
                "--project-id",
                "proj_1",
                "--job-id",
                "job_1",
                "--limit",
                "5",
            ],
            expected={"project_id": "proj_1", "job_id": "job_1", "limit": 5},
        ),
        "projects.dispatch-notification": CommandCase(
            command_id="projects.dispatch-notification",
            args=[
                "projects",
                "dispatch-notification",
                "--project-id",
                "proj_1",
                "--event-type",
                "workflow.completed",
                "--workflow-id",
                "wf.release",
                "--payload",
                '{"ok": true}',
            ],
            textual_args=["projects", "dispatch-notification"],
            expected={
                "project_id": "proj_1",
                "event_type": "workflow.completed",
                "severity": "info",
                "workflow_id": "wf.release",
                "job_id": "",
                "payload": {"ok": True},
            },
        ),
        "workflows.list": CommandCase(
            command_id="workflows.list",
            args=["workflows", "list", "--project-id", "proj_1", "--include-disabled"],
            textual_args=["workflows", "list", "--project-id", "proj_1", "--include-disabled"],
            expected={
                "project_id": "proj_1",
                "include_disabled": True,
                "include_validation": False,
            },
        ),
        "workflows.entrypoints": CommandCase(
            command_id="workflows.entrypoints",
            args=["workflows", "entrypoints", "--project-id", "proj_1"],
            textual_args=["workflows", "entrypoints", "--project-id", "proj_1"],
            expected={"project_id": "proj_1", "include_disabled": False},
        ),
        "workflows.validate": CommandCase(
            command_id="workflows.validate",
            args=["workflows", "validate", "--project-id", "proj_1", "wf.release"],
            textual_args=["workflows", "validate", "--project-id", "proj_1", "wf.release"],
            expected={"project_id": "proj_1", "workflow_id": "wf.release"},
        ),
        "workflows.contract": CommandCase(
            command_id="workflows.contract",
            args=["workflows", "contract", "--project-id", "proj_1", "wf.release"],
            textual_args=["workflows", "contract", "--project-id", "proj_1", "wf.release"],
            expected={"project_id": "proj_1", "workflow_id": "wf.release"},
        ),
        "workflows.suggestions": CommandCase(
            command_id="workflows.suggestions",
            args=["workflows", "suggestions", "--project-id", "proj_1", "wf.release"],
            textual_args=["workflows", "suggestions", "--project-id", "proj_1", "wf.release"],
            expected={"project_id": "proj_1", "workflow_id": "wf.release"},
        ),
        "jobs.list": CommandCase(
            command_id="jobs.list",
            args=[
                "jobs",
                "list",
                "--project-id",
                "proj_1",
                "--status",
                "in_progress",
                "--limit",
                "5",
            ],
            textual_args=[
                "jobs",
                "list",
                "--project-id",
                "proj_1",
                "--status",
                "in_progress",
                "--limit",
                "5",
            ],
            expected={"project_id": "proj_1", "status": "in_progress", "limit": 5, "offset": 0},
        ),
        "jobs.show": CommandCase(
            command_id="jobs.show",
            args=["jobs", "show", "job_1"],
            textual_args=["jobs", "show", "job_1"],
            expected={"job_id": "job_1", "include_details": False},
        ),
        "jobs.status": CommandCase(
            command_id="jobs.status",
            args=["jobs", "status", "job_1"],
            textual_args=["jobs", "status", "job_1"],
            expected={"job_id": "job_1"},
        ),
        "jobs.logs": CommandCase(
            command_id="jobs.logs",
            args=["jobs", "logs", "job_1", "--since-id", "10", "--limit", "5"],
            textual_args=["jobs", "logs", "job_1", "--since-id", "10", "--limit", "5"],
            expected={
                "job_id": "job_1",
                "since_id": "10",
                "before_id": "",
                "limit": 5,
                "newest": False,
            },
        ),
        "jobs.messages": CommandCase(
            command_id="jobs.messages",
            args=[
                "jobs",
                "messages",
                "job_1",
                "--step-id",
                "step_1",
                "--producer",
                "planner",
            ],
            textual_args=[
                "jobs",
                "messages",
                "job_1",
                "--step-id",
                "step_1",
                "--producer",
                "planner",
            ],
            expected={
                "job_id": "job_1",
                "since_id": "",
                "before_id": "",
                "limit": 25,
                "newest": False,
                "step_id": "step_1",
                "producer": "planner",
                "skill": "",
                "status": "",
            },
        ),
        "jobs.errors": CommandCase(
            command_id="jobs.errors",
            args=["jobs", "errors", "job_1", "--limit", "3"],
            textual_args=["jobs", "errors", "job_1", "--limit", "3"],
            expected={"job_id": "job_1", "limit": 3},
        ),
        "jobs.approve": CommandCase(
            command_id="jobs.approve",
            args=[
                "jobs",
                "approve",
                "thread_1",
                "--approval-id",
                "approval_1",
                "--comment",
                "ok",
                "--action",
                "approve",
            ],
            textual_args=["jobs", "approve"],
            expected={
                "thread_id": "thread_1",
                "approval_id": "approval_1",
                "comment": "ok",
                "action": "approve",
            },
        ),
        "jobs.reject": CommandCase(
            command_id="jobs.reject",
            args=[
                "jobs",
                "reject",
                "thread_1",
                "--approval-id",
                "approval_1",
                "--comment",
                "needs changes",
            ],
            textual_args=["jobs", "reject"],
            expected={
                "thread_id": "thread_1",
                "approval_id": "approval_1",
                "comment": "needs changes",
                "action": "reject",
            },
        ),
        "jobs.ack": CommandCase(
            command_id="jobs.ack",
            args=["jobs", "ack", "job_1", "--actor", "cli"],
            textual_args=["jobs", "ack", "job_1", "--actor", "cli"],
            expected={"job_id": "job_1", "actor": "cli"},
        ),
        "jobs.unack": CommandCase(
            command_id="jobs.unack",
            args=["jobs", "unack", "job_1"],
            textual_args=["jobs", "unack", "job_1"],
            expected={"job_id": "job_1"},
        ),
        "jobs.cancel": CommandCase(
            command_id="jobs.cancel",
            args=["jobs", "cancel", "job_1", "--actor", "operator", "--comment", "stop"],
            textual_args=["jobs", "cancel"],
            expected={"job_id": "job_1", "actor": "operator", "comment": "stop"},
        ),
        "jobs.restart": CommandCase(
            command_id="jobs.restart",
            args=["jobs", "restart", "job_1"],
            textual_args=["jobs", "restart"],
            expected={"job_id": "job_1"},
        ),
        "jobs.health-snapshot": CommandCase(
            command_id="jobs.health-snapshot",
            args=[
                "jobs",
                "health-snapshot",
                "--base-url",
                "https://flow.example",
                "--verify-ssl",
                "--timeout-seconds",
                "5",
                "--dsn-file",
                str(dsn_file),
                "--stale-heartbeat-seconds",
                "60",
            ],
            textual_args=[
                "jobs",
                "health-snapshot",
                "--base-url",
                "https://flow.example",
                "--verify-ssl",
                "--timeout-seconds",
                "5",
                "--dsn-file",
                str(dsn_file),
                "--stale-heartbeat-seconds",
                "60",
            ],
            expected={
                "base_url": "https://flow.example",
                "verify_ssl": True,
                "timeout_seconds": 5,
                "dsn": "postgres://example",
                "stale_heartbeat_seconds": 60,
            },
            secret_root=secret_root,
        ),
        "reports.generate": CommandCase(
            command_id="reports.generate",
            args=[
                "reports",
                "generate",
                "activity_daily",
                "--params",
                '{"project_id": "proj_1"}',
                "--no-persist",
            ],
            textual_args=["reports", "generate"],
            expected={
                "report_type": "activity_daily",
                "params": {"project_id": "proj_1"},
                "persist": False,
            },
        ),
        "reports.get": CommandCase(
            command_id="reports.get",
            args=["reports", "get", "report_1"],
            textual_args=["reports", "get", "report_1"],
            expected={"report_id": "report_1"},
        ),
        "reports.diagnostics": CommandCase(
            command_id="reports.diagnostics",
            # Host and doctor facts arrive from the launcher through --input-json.
            args=["reports", "diagnostics"],
            textual_args=["reports", "diagnostics"],
            expected={"host": None, "doctor": None},
        ),
        "database.status": CommandCase(
            command_id="database.status",
            args=["database", "status"],
            textual_args=["database", "status"],
            expected={"dsn": ""},
        ),
        "database.upgrade": CommandCase(
            command_id="database.upgrade",
            args=["database", "upgrade", "--author", "operator", "--maintenance-confirmed"],
            textual_args=["database", "upgrade", "--author", "operator", "--maintenance-confirmed"],
            expected={"dsn": "", "author": "operator", "maintenance_confirmed": True},
        ),
        "database.bootstrap-seed": CommandCase(
            command_id="database.bootstrap-seed",
            args=["database", "bootstrap-seed", "--author", "operator"],
            textual_args=["database", "bootstrap-seed", "--author", "operator"],
            expected={"dsn": "", "author": "operator"},
        ),
        "database.generate-migration": CommandCase(
            command_id="database.generate-migration",
            args=["database", "generate-migration", "add audit", "--root", str(tmp_path)],
            textual_args=["database", "generate-migration"],
            expected={"name": "add audit", "root": str(tmp_path)},
        ),
        "database.query-diagnostics": CommandCase(
            command_id="database.query-diagnostics",
            args=[
                "database",
                "query-diagnostics",
                "--limit",
                "7",
                "--sort-by",
                "total",
                "--min-rows",
                "42",
                "--max-query-len",
                "80",
            ],
            textual_args=[
                "database",
                "query-diagnostics",
                "--limit",
                "7",
                "--sort-by",
                "total",
                "--min-rows",
                "42",
                "--max-query-len",
                "80",
            ],
            expected={
                "dsn": "",
                "limit": 7,
                "sort_by": "total",
                "min_rows": 42,
                "max_query_len": 80,
            },
        ),
        "database.tune-env": CommandCase(
            command_id="database.tune-env",
            args=[
                "database",
                "tune-env",
                "--memory-gb",
                "8",
                "--max-connections",
                "50",
                "--storage",
                "hdd",
            ],
            textual_args=[
                "database",
                "tune-env",
                "--memory-gb",
                "8",
                "--max-connections",
                "50",
                "--storage",
                "hdd",
            ],
            expected={"memory_gb": 8.0, "max_connections": 50, "storage": "hdd"},
        ),
        "database.restore-parity-capture": CommandCase(
            command_id="database.restore-parity-capture",
            args=[
                "database",
                "restore-parity-capture",
                "--output",
                str(tmp_path / "baseline.json"),
                "--label",
                "base",
            ],
            textual_args=["database", "restore-parity-capture"],
            expected={"dsn": "", "label": "base", "output": str(tmp_path / "baseline.json")},
        ),
        "database.restore-parity-verify": CommandCase(
            command_id="database.restore-parity-verify",
            args=[
                "database",
                "restore-parity-verify",
                "--baseline",
                str(tmp_path / "baseline.json"),
                "--output",
                str(tmp_path / "restored.json"),
                "--label",
                "restored",
            ],
            textual_args=["database", "restore-parity-verify"],
            expected={
                "dsn": "",
                "label": "restored",
                "baseline": str(tmp_path / "baseline.json"),
                "output": str(tmp_path / "restored.json"),
            },
        ),
        "testing.smoke-test": CommandCase(
            command_id="testing.smoke-test",
            args=[
                "testing",
                "smoke-test",
                "--project-id",
                "proj_1",
                "--lane",
                "ui",
                "--verify-ssl",
            ],
            textual_args=["testing", "smoke-test"],
            expected={
                "base_url": "",
                "project_id": "proj_1",
                "workflow_id": "",
                "lane": "ui",
                "verify_ssl": True,
                "timeout_seconds": 1800,
            },
        ),
        "testing.nightfire-regression": CommandCase(
            command_id="testing.nightfire-regression",
            args=[
                "testing",
                "nightfire-regression",
                "--lane",
                "backend",
                "--project-id",
                "proj_1",
                "--auth-role",
                "operator",
                "--with-cutover-smoke",
                "--fail-fast",
            ],
            textual_args=["testing", "nightfire-regression"],
            expected={
                "lane": "backend",
                "project_id": "proj_1",
                "base_url": "",
                "verify_ssl": False,
                "auth_role": "operator",
                "auth_username": "",
                "auth_password": "",
                "auth_display_name": "",
                "source_workflow_id": "",
                "report_path": "",
                "with_cutover_smoke": True,
                "with_full_ui": False,
                "fail_fast": True,
                "timeout_seconds": 7200,
            },
        ),
        "testing.fuzz-api": CommandCase(
            command_id="testing.fuzz-api",
            args=[
                "testing",
                "fuzz-api",
                "--base-url",
                "http://localhost:8099",
                "--seed",
                "42",
                "--max-text-size",
                "512",
                "--timeout",
                "3.5",
                "--role",
                "viewer",
                "--skip-auth",
                "--strict",
            ],
            textual_args=["testing", "fuzz-api"],
            expected={
                "base_url": "http://localhost:8099",
                "seed": 42,
                "max_text_size": 512,
                "timeout": 3.5,
                "report_path": "",
                "role": "viewer",
                "username": "",
                "password": "",
                "skip_auth": True,
                "strict": True,
            },
        ),
        "testing.fuzz-mcp": CommandCase(
            command_id="testing.fuzz-mcp",
            args=[
                "testing",
                "fuzz-mcp",
                "--app-base-url",
                "http://localhost:8099",
                "--mcp-base-url",
                "http://localhost:8100/mcp",
                "--burst-size",
                "3",
                "--skip-auth",
            ],
            textual_args=["testing", "fuzz-mcp"],
            expected={
                "app_base_url": "http://localhost:8099",
                "mcp_base_url": "http://localhost:8100/mcp",
                "seed": 9917,
                "timeout": 15.0,
                "burst_size": 3,
                "report_path": "",
                "role": "admin",
                "username": "",
                "password": "",
                "bearer_token": "",
                "skip_auth": True,
                "strict": False,
            },
        ),
        "testing.fuzz-ui": CommandCase(
            command_id="testing.fuzz-ui",
            args=[
                "testing",
                "fuzz-ui",
                "--project-id",
                "proj_1",
                "--base-url",
                "http://localhost:8099",
                "--skip-lighthouse",
                "--run-chrome-mcp",
                "--strict",
            ],
            textual_args=["testing", "fuzz-ui"],
            expected={
                "base_url": "http://localhost:8099",
                "seed": 8123,
                "timeout": 20.0,
                "report_path": "",
                "role": "admin",
                "username": "",
                "password": "",
                "skip_auth": False,
                "project_id": "proj_1",
                "routes": "/login,/dashboard,/projects,/jobs,/settings,/registry,/projects/{project_id}",
                "lighthouse_routes": "/login,/dashboard,/projects,/jobs,/settings",
                "lighthouse_threshold": 0.9,
                "lighthouse_timeout": 60.0,
                "skip_lighthouse": True,
                "run_chrome_mcp": True,
                "chrome_mcp_script": "",
                "strict": True,
            },
        ),
        "testing.cutover-preflight": CommandCase(
            command_id="testing.cutover-preflight",
            args=[
                "testing",
                "cutover-preflight",
                "--project-id",
                "proj_1",
                "--base-url",
                "http://localhost:8099",
                "--verify-ssl",
            ],
            textual_args=["testing", "cutover-preflight"],
            expected={
                "project_id": "proj_1",
                "base_url": "http://localhost:8099",
                "verify_ssl": True,
                "timeout_seconds": 300,
            },
        ),
        "testing.cutover-smoke": CommandCase(
            command_id="testing.cutover-smoke",
            args=[
                "testing",
                "cutover-smoke",
                "--project-id",
                "proj_1",
                "--summary",
                "Smoke",
                "--poll-seconds",
                "2",
                "--bearer-token-file",
                str(bearer_file),
            ],
            textual_args=["testing", "cutover-smoke"],
            expected={
                "project_id": "proj_1",
                "base_url": "http://localhost:8099",
                "verify_ssl": False,
                "summary": "Smoke",
                "timeout_seconds": 900,
                "poll_seconds": 2,
                "username": "",
                "password": "",
                "bearer_token": "service-token",
            },
            secret_root=secret_root,
        ),
        "quality.verify": CommandCase(
            command_id="quality.verify",
            args=[
                "quality",
                "verify",
                "--tier",
                "3",
                "--evidence-mode",
                "report",
            ],
            textual_args=["quality", "verify", "--tier", "3"],
            expected={
                "tier": 3,
                "evidence_mode": "report",
                "fingerprint_source": "worktree",
                "trust": "local",
                "commit_sha": "",
                "repository_root": ".",
                "evidence_root": "logs/quality/evidence",
                "timeout_seconds": 7200,
            },
        ),
        "quality.lint": CommandCase(
            command_id="quality.lint",
            args=["quality", "lint", "--mode", "fast", "--checks", "ruff"],
            textual_args=["quality", "lint"],
            expected={"mode": "fast", "checks": "ruff", "timeout_seconds": 3600},
        ),
        "quality.check-ci-excludes": CommandCase(
            command_id="quality.check-ci-excludes",
            args=[
                "quality",
                "check-ci-excludes",
                "--exclude-file",
                "tests/CI_EXCLUDES.txt",
            ],
            textual_args=[
                "quality",
                "check-ci-excludes",
                "--exclude-file",
                "tests/CI_EXCLUDES.txt",
            ],
            expected={"path": "tests/CI_EXCLUDES.txt", "timeout_seconds": 300},
        ),
        "quality.check-naming": CommandCase(
            command_id="quality.check-naming",
            args=[
                "quality",
                "check-naming",
                "--base",
                "origin/main",
                "--files",
                "a.py,b.py",
            ],
            textual_args=[
                "quality",
                "check-naming",
                "--base",
                "origin/main",
                "--files",
                "a.py,b.py",
            ],
            expected={"base": "origin/main", "files": "a.py,b.py", "timeout_seconds": 300},
        ),
        "quality.release-gate": CommandCase(
            command_id="quality.release-gate",
            args=["quality", "release-gate"],
            textual_args=["quality", "release-gate"],
            expected={"timeout_seconds": 3600},
        ),
        "quality.sonar-export-issues": CommandCase(
            command_id="quality.sonar-export-issues",
            args=[
                "quality",
                "sonar-export-issues",
                "--host-url",
                "http://sonar.example",
                "--project-key",
                "flow-steward",
                "--severities",
                "MAJOR",
                "--types",
                "BUG",
                "--page-size",
                "25",
                "--output-dir",
                str(tmp_path / "sonar"),
            ],
            textual_args=["quality", "sonar-export-issues"],
            expected={
                "host_url": "http://sonar.example",
                "project_key": "flow-steward",
                "statuses": "OPEN,CONFIRMED,REOPENED",
                "severities": "MAJOR",
                "types": "BUG",
                "page_size": 25,
                "output_dir": str(tmp_path / "sonar"),
                "sonar_token": "",
                "timeout_seconds": 900,
            },
        ),
        "quality.sonar-ensure-rules": CommandCase(
            command_id="quality.sonar-ensure-rules",
            args=["quality", "sonar-ensure-rules", "--rules", "python:S1"],
            textual_args=["quality", "sonar-ensure-rules"],
            expected={
                "host_url": "http://localhost:9000",
                "rules": "python:S1",
                "sonar_token": "",
                "timeout_seconds": 900,
            },
        ),
        "quality.sonar-generate-backlog": CommandCase(
            command_id="quality.sonar-generate-backlog",
            args=[
                "quality",
                "sonar-generate-backlog",
                "--input-dir",
                str(tmp_path / "sonar"),
                "--top-rules",
                "3",
                "--top-files",
                "4",
                "--max-p0",
                "5",
                "--max-p1",
                "6",
            ],
            textual_args=["quality", "sonar-generate-backlog"],
            expected={
                "input_csv": "",
                "input_dir": str(tmp_path / "sonar"),
                "output": "logs/quality/sonar/sonar-dev-backlog.md",
                "top_rules": 3,
                "top_files": 4,
                "max_p0": 5,
                "max_p1": 6,
                "timeout_seconds": 300,
            },
        ),
        "localization.bootstrap-catalog": CommandCase(
            command_id="localization.bootstrap-catalog",
            args=[
                "localization",
                "bootstrap-catalog",
                "--source",
                "core/application/settings/ui_i18n.py",
            ],
            textual_args=["localization", "bootstrap-catalog"],
            expected={"source": "core/application/settings/ui_i18n.py", "timeout_seconds": 300},
        ),
        "localization.rebuild-pot": CommandCase(
            command_id="localization.rebuild-pot",
            args=["localization", "rebuild-pot"],
            textual_args=["localization", "rebuild-pot"],
            expected={"timeout_seconds": 300},
        ),
        "localization.init-locale": CommandCase(
            command_id="localization.init-locale",
            args=[
                "localization",
                "init-locale",
                "es",
                "--label",
                "Spanish",
                "--native-label",
                "Español",
            ],
            textual_args=["localization", "init-locale"],
            expected={
                "code": "es",
                "label": "Spanish",
                "native_label": "Español",
                "timeout_seconds": 300,
            },
        ),
        "localization.update": CommandCase(
            command_id="localization.update",
            args=["localization", "update"],
            textual_args=["localization", "update"],
            expected={"timeout_seconds": 300},
        ),
        "localization.compile": CommandCase(
            command_id="localization.compile",
            args=["localization", "compile", "--force"],
            textual_args=["localization", "compile"],
            expected={"force": True, "timeout_seconds": 300},
        ),
        "localization.validate-keys": CommandCase(
            command_id="localization.validate-keys",
            args=["localization", "validate-keys"],
            textual_args=["localization", "validate-keys"],
            expected={"timeout_seconds": 300},
        ),
    }
    assert_case_registry_parity(PUBLIC_COMMAND_SPECS, cases)
    return {command_id: cases[command_id] for command_id in CLI_CASE_IDS_IN_REGISTRY_ORDER}


def test_every_public_command_has_exactly_one_case(
    command_cases: dict[str, CommandCase],
) -> None:
    assert_case_registry_parity(PUBLIC_COMMAND_SPECS, command_cases)
    assert tuple(command_cases) == CLI_CASE_IDS_IN_REGISTRY_ORDER


@pytest.mark.parametrize("command_id", CLI_CASE_IDS_IN_REGISTRY_ORDER)
def test_help_for_each_public_command_is_side_effect_free(
    command_id: str, command_cases: dict[str, CommandCase]
) -> None:
    app, registry = _app()
    spec = _specs()[command_id]

    result = CliRunner().invoke(app, [*command_cases[command_id].args[:2], "--help"])

    assert result.exit_code == 0
    assert registry.calls == []
    assert (
        "Required inputs" in result.output
        or "Optional inputs" in result.output
        or "Advanced inputs" in result.output
        or not spec.fields
        or command_id
        in {
            "status",
            "doctor",
            "users.list",
            "users.issue-bootstrap-token",
            "extensions.list",
        }
    )


@pytest.mark.parametrize("prefix, expected_color", [([], False), (["--tui"], True)])
def test_help_is_side_effect_free_and_color_is_explicit(
    prefix: list[str],
    expected_color: bool,
) -> None:
    app, registry = _app()
    runner = CliRunner()
    env = {"NO_COLOR": None, "TERM": "xterm-256color"}

    root = runner.invoke(app, [*prefix, "--help"], color=True, env=env)
    group = runner.invoke(app, [*prefix, "users", "--help"], color=True, env=env)
    leaf = runner.invoke(app, [*prefix, "users", "create", "--help"], color=True, env=env)

    for result in (root, group, leaf):
        assert result.exit_code == 0, result.output
        assert bool(ANSI_RE.search(result.output)) is expected_color
    assert registry.calls == []
    assert "users" in _strip_ansi(root.output)
    assert "create" in _strip_ansi(group.output)
    assert "Required inputs" in _strip_ansi(leaf.output)
    assert "Optional inputs" in _strip_ansi(leaf.output)


@pytest.mark.parametrize("command_id", CLI_CASE_IDS_IN_REGISTRY_ORDER)
@pytest.mark.parametrize(
    ("mode_name", "prefix", "suffix", "terminal", "stdin"),
    [
        ("textual_or_immediate", ["--tui"], [], _terminal(stdin=True, stdout=True), "y\n"),
        ("plain", ["--plain"], [], _terminal(stdin=True, stdout=True), "y\n"),
        ("non_interactive", ["--non-interactive"], [], _terminal(stdin=True, stdout=True), ""),
        ("json", ["--json"], [], _terminal(stdin=False, stdout=False), ""),
        ("redirected", [], [], _terminal(stdin=False, stdout=False), ""),
    ],
)
def test_every_mode_uses_same_spec_service_and_normalized_values(
    command_id: str,
    mode_name: str,
    prefix: list[str],
    suffix: list[str],
    terminal: TerminalFacts,
    stdin: str,
    command_cases: dict[str, CommandCase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    specs = _specs()
    case = command_cases[command_id]
    app, registry = _app(RecordingRegistry(prepared_clone=case.prepared_clone))
    form_calls: list[dict[str, Any]] = []

    def fake_form(**kwargs: Any) -> SimpleNamespace:
        form_calls.append(kwargs)
        return SimpleNamespace(status="confirmed", values=case.form_values or case.expected)

    monkeypatch.setattr(click_adapter, "_terminal_facts", lambda: terminal)
    monkeypatch.setattr(click_adapter, "_run_textual_form", fake_form, raising=False)
    if case.secret_root is not None:
        monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", case.secret_root)

    command_args = (
        case.textual_args
        if (
            mode_name == "textual_or_immediate"
            and command_id != "users.list"
            and not command_id.startswith(("testing.", "quality.", "localization."))
        )
        else case.args
    )
    if specs[command_id].interactive_policy is InteractivePolicy.ALWAYS_REVIEW and mode_name in {
        "non_interactive",
        "json",
        "redirected",
    }:
        confirmation = []
        if specs[command_id].confirmation_policy_ref:
            keys = specs[command_id].confirmation_policy_ref.removeprefix("match:").split(",")
            confirmation = [
                case.confirmation_value
                or str(next(case.expected[key] for key in keys if case.expected.get(key)))
            ]
        command_args = [*command_args, "--confirm", *confirmation]
    effective_stdin = stdin
    if specs[command_id].confirmation_policy_ref and mode_name in {
        "textual_or_immediate",
        "plain",
    }:
        keys = specs[command_id].confirmation_policy_ref.removeprefix("match:").split(",")
        effective_stdin = (
            case.confirmation_value
            or str(next(case.expected[key] for key in keys if case.expected.get(key)))
        ) + "\n"
    result = CliRunner().invoke(app, [*prefix, *command_args, *suffix], input=effective_stdin)

    assert result.exit_code == 0, result.output
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call.key == specs[command_id].service_ref
    assert call.model_class is (case.invocation_model_class or specs[command_id].input_model)
    assert call.values == case.expected
    if command_id == "quality.lint":
        assert [(event.step_id, event.status.value) for event in registry.progress_events] == [
            ("queued", "running"),
            ("complete", "succeeded"),
        ]
    if mode_name == "json":
        assert _json_payload(result.output)["command"] == command_id
    if command_id == "users.create":
        assert "StrongPass_123!" not in result.output
    if form_calls:
        assert mode_name == "textual_or_immediate"
        assert form_calls[0]["spec"] is specs[command_id]
    else:
        assert form_calls == []


@pytest.mark.parametrize(
    "confirmation", ["target-installation", "target-installation|compact|target.example.test|wrong"]
)
def test_clone_matrix_rejects_incomplete_or_changed_target_confirmation(
    confirmation: str,
    command_cases: dict[str, CommandCase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    case = command_cases["clone.restore"]
    app, registry = _app(RecordingRegistry(prepared_clone=case.prepared_clone))
    monkeypatch.setattr(
        click_adapter, "_terminal_facts", lambda: _terminal(stdin=False, stdout=False)
    )
    result = CliRunner().invoke(app, ["--non-interactive", *case.args, "--confirm", confirmation])

    assert result.exit_code != 0
    assert registry.calls == []
    assert registry.clone_staging_cleaned


@pytest.mark.parametrize("command", ["create", "restore"])
def test_clone_matrix_has_no_plaintext_passphrase_option(command: str) -> None:
    app, registry = _app()
    result = CliRunner().invoke(app, ["clone", command, "--passphrase", "secret-never-log"])

    assert result.exit_code != 0
    assert registry.calls == []
    assert "secret-never-log" not in result.output


@pytest.mark.parametrize(
    "flagged_args",
    [
        ["--non-interactive", "users", "list"],
        ["users", "--non-interactive", "list"],
        ["users", "list", "--non-interactive"],
    ],
)
def test_global_non_interactive_option_works_at_every_command_depth(
    flagged_args: list[str],
) -> None:
    app, registry = _app()

    result = CliRunner().invoke(app, flagged_args)

    assert result.exit_code == 0, result.output
    assert [call.key for call in registry.calls] == ["users.list"]


@pytest.mark.parametrize("prefix", [[], ["--plain"], ["--json"], ["--non-interactive"]])
def test_input_json_precedence_matches_click_values_across_modes(
    prefix: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    input_json = tmp_path / "input.json"
    input_json.write_text(
        json.dumps({"mode": "full", "checks": "bandit"}),
        encoding="utf-8",
    )
    app, registry = _app()
    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: _terminal(stdin=True, stdout=True),
    )

    result = CliRunner().invoke(
        app,
        [
            *prefix,
            "quality",
            "lint",
            "--input-json",
            str(input_json),
            "--mode",
            "audit",
            "--confirm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert registry.calls[0].values == {
        "mode": "audit",
        "checks": "bandit",
        "timeout_seconds": 3600,
    }


@pytest.mark.parametrize("prefix", [["--json"], ["--plain"], ["--non-interactive"]])
def test_validation_failure_and_cancellation_do_not_invoke_services(
    prefix: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    app, registry = _app()
    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: _terminal(stdin=True, stdout=True),
    )

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text(
        json.dumps({"unknown_a": 1, "unknown_b": 2}),
        encoding="utf-8",
    )

    invalid = CliRunner().invoke(
        app,
        [*prefix, "quality", "lint", "--input-json", str(invalid_json), "--confirm"],
        input="n\n",
    )

    assert invalid.exit_code == 3
    assert registry.calls == []
    assert "unknown_a" in invalid.output
    assert "unknown_b" in invalid.output

    if prefix == ["--plain"]:
        cancelled = CliRunner().invoke(
            app,
            ["--plain", "jobs", "restart", "job_1"],
            input="n\n",
        )
        assert cancelled.exit_code == 10
        assert registry.calls == []


@pytest.mark.parametrize("arguments", [["ext"], ["tooling"], ["db"], ["extensions", "new"]])
def test_removed_aliases_remain_unresolved(arguments: list[str]) -> None:
    app, registry = _app()

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    assert registry.calls == []
