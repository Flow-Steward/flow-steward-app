"""Read-only adapter from selected stack state to shared readiness input."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.infrastructure.installer.host_diagnostics import collect_host_diagnostics
from core.infrastructure.installer.state_store import InstallerStateStore

_REPO_ROOT = Path(__file__).resolve().parents[3]


class StackReadinessProbe:
    def __init__(
        self,
        *,
        configuration_service: Any,
        doctor: Callable[[Any], dict[str, Any]],
        doctor_options_factory: Callable[..., Any],
        repo_root: Path = _REPO_ROOT,
        host_diagnostics: Callable[[], tuple[dict[str, object], ...]] | None = None,
        installation_classification: Callable[[], Any] | None = None,
        installation_journal: Callable[[], Any | None] | None = None,
        installer_state_root: Path | None = None,
        lifecycle_state: Any | None = None,
        active_lifecycle_operation: Callable[[], str | None] | None = None,
        complete_command: Callable[[str], str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.configuration_service = configuration_service
        self.doctor = doctor
        self.doctor_options_factory = doctor_options_factory
        self.host_diagnostics = host_diagnostics or (
            lambda: collect_host_diagnostics(self.repo_root)
        )
        self.installer_state_root = Path(
            installer_state_root or self.repo_root / ".flow-steward"
        ).resolve()
        self.installation_journal = (
            installation_journal or InstallerStateStore(self.installer_state_root).load_journal
        )
        self.installation_classification = installation_classification
        if lifecycle_state is None:
            from core.infrastructure.lifecycle.state_store import LifecycleStateStore

            lifecycle_state = LifecycleStateStore(self.repo_root)
        self.lifecycle_state = lifecycle_state
        # The id of the operation holding the lifecycle lock; composed by the CLI, which
        # owns the lock's vocabulary. Without it every unfinished journal reads as
        # interrupted -- a loud answer, never a silent one.
        self.active_lifecycle_operation = active_lifecycle_operation or (lambda: None)
        # How a recovery command is typed -- ./ and --confirm where the command takes it;
        # composed by the CLI, which owns the command specs.
        self.complete_command = complete_command or _with_launcher_path

    def __call__(self) -> dict[str, Any]:
        config = self.configuration_service.read_active(required=False)
        if config is not None:
            try:
                journal = self.installation_journal()
            except (OSError, ValueError):
                return {"checks": self._preinstall_checks()}
            if journal is None or journal.status == "completed":
                payload = self.check_config(config)
                payload["checks"] = [
                    *(payload.get("checks") or []),
                    *self._lifecycle_operation_findings(),
                ]
                return payload
        return {"checks": self._preinstall_checks()}

    def _lifecycle_operation_findings(self) -> list[dict[str, object]]:
        """Name a lifecycle operation that was interrupted, and how to continue it.

        W1 (auditor): after `config apply` was killed, `status` said only that services
        were missing -- or READY, when it was killed before any container changed -- and
        the operation stayed "running" in its journal with no mention anywhere. What had
        saved the stand was rerunning `config apply --confirm`, which nothing suggested.
        An operation whose journal is unfinished while nobody holds the lifecycle lock is
        not running: the lock is a flock and dies with its process.
        """
        try:
            journals = tuple(self.lifecycle_state.unfinished_journals())
            running = self.active_lifecycle_operation()
        except (OSError, ValueError):
            return [
                {
                    "id": "lifecycle_operation",
                    "category": "core",
                    "title": "Lifecycle operations",
                    "status": "fail",
                    "detail": "Lifecycle operation journals could not be read safely.",
                    "remediation": "Inspect .flow-steward/operations without deleting it.",
                }
            ]
        findings: list[dict[str, object]] = []
        for journal in journals:
            kind = str(getattr(journal.kind, "value", journal.kind)).replace("_", " ")
            step = journal.current_step or journal.failed_step or "unknown"
            state = str(getattr(journal.status, "value", journal.status))
            if running and running == journal.operation_id:
                findings.append(
                    {
                        "id": "lifecycle_operation",
                        "category": "core",
                        "title": "Lifecycle operation in progress",
                        "status": "warn",
                        "detail": f"{kind} {journal.operation_id} is running (step {step}).",
                        "remediation": "Let it finish; its log is " + str(journal.log_path or ""),
                    }
                )
                continue
            command = self.complete_command(str(journal.recovery_command or "").strip())
            findings.append(
                {
                    "id": "lifecycle_operation",
                    "category": "core",
                    "title": "Interrupted lifecycle operation",
                    "status": "fail",
                    "detail": (
                        f"{kind} {journal.operation_id} was interrupted at step {step} "
                        f"({state}) and nothing is running it now."
                    ),
                    "remediation": f"Continue it with {command}."
                    if command
                    else "Inspect the operation journal and its log.",
                }
            )
        return findings

    def _preinstall_checks(self) -> list[dict[str, object]]:
        try:
            checks = [dict(finding) for finding in self.host_diagnostics()]
        except (OSError, ValueError):
            checks = [
                {
                    "id": "host_diagnostics",
                    "category": "core",
                    "title": "Host diagnostics",
                    "status": "fail",
                    "detail": "Host prerequisites could not be inspected safely.",
                    "remediation": "Verify repository access and rerun Doctor.",
                }
            ]
        try:
            if self.installation_classification is None:
                raise ValueError("installation classification is not configured")
            classification = self.installation_classification()
        except (OSError, ValueError):
            checks.append(
                {
                    "id": "installation_state",
                    "category": "core",
                    "title": "Installation state",
                    "status": "fail",
                    "detail": "Installation evidence could not be read safely.",
                    "remediation": (
                        "Inspect the owner-only installer state without deleting resources, "
                        "then rerun Doctor."
                    ),
                }
            )
            return checks
        classification_kind = str(getattr(classification, "kind", "") or "").strip()
        if classification_kind == "fresh":
            checks.append(
                {
                    "id": "installation_config",
                    "category": "core",
                    "title": "Installation configuration",
                    "status": "fail",
                    "detail": "Flow Steward installation has not started.",
                    "remediation": "Run ./install.sh after resolving the host findings above.",
                }
            )
        elif classification_kind == "resumable":
            checks.append(self._resume_finding())
        else:
            checks.append(self._installation_state_finding(classification))
        return checks

    def _resume_finding(self) -> dict[str, object]:
        try:
            journal = self.installation_journal()
        except (OSError, ValueError):
            journal = None
        if journal is None:
            return {
                "id": "installation_resume",
                "category": "core",
                "title": "Installation resume",
                "status": "fail",
                "detail": "Installation is incomplete, but its journal could not be read.",
                "remediation": (
                    "Inspect .flow-steward/install-state.json without deleting resources, "
                    "then rerun Doctor."
                ),
            }
        completed_step = journal.completed_steps[-1] if journal.completed_steps else "none"
        failed_step = journal.failed_step or "unknown"
        error_code = journal.last_error_code or "unknown"
        retryability = "retryable" if journal.retryable else "not marked retryable"
        log_path = self._journal_log_path(str(journal.operation_id))
        detail = (
            f"Installation is incomplete ({retryability}). Last completed step: {completed_step}. "
            f"Failed step: {failed_step}. Error: {error_code}. Log: {log_path}."
        )
        if error_code == "installation_rollback_failed":
            return {
                "id": "installation_resume",
                "category": "core",
                "title": "Installation rollback",
                "status": "fail",
                "detail": detail,
                "remediation": (
                    "Do not rerun ./install.sh until the exact installer log has been "
                    "reviewed and only the interrupted install's task-owned resources have "
                    "been removed."
                ),
            }
        remediation = (
            "Resolve the reported failure, then rerun ./install.sh to resume safely."
            if journal.retryable
            else (
                "Inspect the exact installer log and resolve the ambiguous state without "
                "deleting volumes; rerun Doctor before attempting ./install.sh."
            )
        )
        return {
            "id": "installation_resume",
            "category": "core",
            "title": "Installation resume",
            "status": "fail",
            "detail": detail,
            "remediation": remediation,
        }

    def _journal_log_path(self, operation_id: str) -> str:
        safe_id = "".join(
            character for character in operation_id if character.isalnum() or character in "-_"
        )
        if not safe_id:
            return "unavailable"
        logs_root = self.installer_state_root / "logs"
        candidates = sorted(
            {
                *logs_root.glob(f"install-*-{safe_id}.log"),
                *logs_root.glob(f"install-*-{safe_id}-????????.log"),
                *logs_root.glob(f"install-{safe_id}.log"),
            }
        )
        if not candidates:
            return "unavailable"
        selected = candidates[-1]
        try:
            return selected.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(selected)

    @staticmethod
    def _installation_state_finding(classification: Any) -> dict[str, object]:
        reasons = " ".join(classification.reasons) or (
            "Installer state reports completion, but active configuration is unavailable."
        )
        resources = (
            " Preserved resources: " + ", ".join(classification.resources) + "."
            if classification.resources
            else ""
        )
        return {
            "id": "installation_state",
            "category": "core",
            "title": "Installation state",
            "status": "fail",
            "detail": reasons + resources,
            "remediation": (
                "Inspect the installer journal and exact log without deleting volumes or "
                "rotating secrets, then rerun Doctor."
            ),
        }

    def check_config(
        self,
        config: Any,
        *,
        allow_initial_auth_bootstrap: bool = False,
        allow_lifecycle_mutation: bool = False,
        allow_release_transition: bool = False,
    ) -> dict[str, Any]:
        """Run canonical readiness against an explicit staged configuration."""
        target = self.configuration_service.derive_target(config)
        payload = self.doctor(
            self.doctor_options_factory(
                repo_root=self.repo_root,
                env_file=self.repo_root / target.env_file,
                compose_files=tuple(
                    self.repo_root / compose_file for compose_file in target.compose_files
                ),
                compose_profiles=target.compose_profiles,
                compose_project=target.project_name,
                required_compose_services=target.required_services,
                runtime_compose_services=target.runtime_services,
                stack_environment=(
                    "local_development"
                    if target.stack_environment == "development"
                    else "production"
                ),
                base_url="http://fs-app:8099",
                allow_initial_auth_bootstrap=allow_initial_auth_bootstrap,
                allow_lifecycle_mutation=allow_lifecycle_mutation,
                allow_release_transition=allow_release_transition,
                static_only=False,
                run_compose_config=True,
            )
        )
        checks = list(payload.get("checks") or [])
        try:
            validation = self.configuration_service.validate_config(config)
        except Exception:
            checks.append(
                {
                    "id": "configuration_validity",
                    "category": "core",
                    "title": "Active configuration validity",
                    "status": "fail",
                    "detail": "The active configuration could not be validated.",
                    "remediation": "Run flow-steward config validate and repair the reported fields.",
                }
            )
        else:
            if not validation.valid:
                checks.append(
                    {
                        "id": "configuration_validity",
                        "category": "core",
                        "title": "Active configuration validity",
                        "status": "fail",
                        "detail": "; ".join(validation.issues)
                        or "The active configuration is invalid.",
                        "remediation": "Run flow-steward config validate and repair the reported fields.",
                    }
                )
        return {**payload, "checks": checks}


def _with_launcher_path(command: str) -> str:
    return f"./{command}" if command.startswith("flow-steward") else command
