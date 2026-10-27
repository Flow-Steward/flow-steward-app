"""Durable configuration apply steps, verification and recovery."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.configuration_models import (
    ConfigurationApplyGateway,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationDefinition,
    LifecycleOperationStep,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import (
    HeartbeatSink,
    LifecycleState,
    TechnicalSink,
)

if TYPE_CHECKING:
    from core.application.lifecycle.configuration_service import ConfigurationService

_MESSAGE_FLOW_STEWARD_CONFIG_APPLY = "flow-steward config apply"


_SAFE_FAILURE_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _configuration_failure_details(cause: Exception) -> dict[str, object]:
    details: dict[str, object] = {}
    action = getattr(cause, "action", None)
    raw_detail = getattr(cause, "detail", None)
    if isinstance(action, str) and _SAFE_FAILURE_ACTION.fullmatch(action):
        details["failed_action"] = action
    if isinstance(raw_detail, str) and raw_detail.strip():
        detail = redact_installer_text(raw_detail.strip())[:600]
        if detail:
            details["failure_detail"] = detail
    return details


class _ConfigurationOperationContext:
    def __init__(
        self,
        *,
        operation_id: str,
        service: ConfigurationService,
        previous: DeploymentConfig,
        candidate: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
        previous_environment: Mapping[str, str],
        lifecycle_state: LifecycleState,
        durable_completed_steps: tuple[str, ...],
        before_manifest: Mapping[str, object] | None,
        runtime_snapshot: Mapping[str, object] | None,
        pending_revision: str,
    ) -> None:
        self.operation_id = operation_id
        self.service = service
        self.previous = previous
        self.candidate = candidate
        self.previous_target = previous_target
        self.target = target
        self.prepared = prepared
        self.previous_environment = dict(previous_environment)
        self.lifecycle_state = lifecycle_state
        self.durable_completed_steps = frozenset(durable_completed_steps)
        self.before_manifest = before_manifest
        self.runtime_snapshot = runtime_snapshot
        self.pending_revision = pending_revision


class _ConfigurationOperationGateway:
    """Interpret durable config-apply steps through the injected technical adapter."""

    def __init__(
        self,
        *,
        adapter: ConfigurationApplyGateway,
        context: _ConfigurationOperationContext,
    ) -> None:
        self.adapter = adapter
        self.context = context
        self.after_manifest: Mapping[str, object] | None = None
        self.mutated = False
        self.preservation_verified = False
        # AC17-R167: the rollback writes into the operation log of the step it undoes.
        self._output_sink: TechnicalSink = _discard_line

    def postcondition_holds(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        if step.step_id == "verify_pending_revision":
            return "verify_pending_revision" in self.context.durable_completed_steps
        if step.step_id == "capture_before":
            return bool(
                self.context.before_manifest is not None
                and self.context.runtime_snapshot is not None
                and self.adapter.recovery_available(self.context.operation_id)
            )
        if step.step_id == "render_configuration":
            return (
                "render_configuration" in self.context.durable_completed_steps
                and self.context.service.read_active() == self.context.candidate
            )
        if step.step_id == "change_runtime":
            completed = "change_runtime" in self.context.durable_completed_steps
            self.mutated = completed
            return completed
        if step.step_id == "verify_health":
            try:
                return self.adapter.healthy(self.context.candidate, self.context.target)
            except Exception:
                return False
        if step.step_id == "commit_pending":
            return (
                not self.context.service.store.pending_revision_matches(
                    self.context.pending_revision
                )
                and self.context.service.read_active() == self.context.candidate
            )
        return False

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del heartbeat, cancellation
        self._output_sink = output_sink
        if step.step_id == "verify_pending_revision":
            self._execute_verify_pending_revision()
            return
        if step.step_id == "validate_target":
            self._execute_validate_target()
            return
        if step.step_id == "capture_before":
            self._execute_capture_before()
            return
        if step.step_id == "render_configuration":
            self._execute_render_configuration()
            return
        if step.step_id == "change_runtime":
            self._execute_change_runtime()
            return
        if step.step_id == "verify_health":
            self._execute_verify_health(output_sink)
            return
        if step.step_id == "capture_after":
            self._execute_capture_after()
            return
        if step.step_id == "verify_preservation":
            self._execute_verify_preservation(definition, step)
            return
        if step.step_id == "commit_pending":
            self._execute_commit_pending()
            return
        raise LifecycleError.internal(
            step=step.step_id,
            recovery_command=definition.recovery_command,
        )

    def _execute_verify_pending_revision(self) -> None:
        if not self.context.service.store.pending_revision_matches(self.context.pending_revision):
            raise LifecycleError.stable_failure(
                code="pending_configuration_changed",
                cause="The pending configuration changed before apply acquired the mutation lock.",
                remediation="Review the current pending diff and run config apply again.",
                step="pending_revision",
            )

    def _execute_validate_target(self) -> None:
        try:
            self.adapter.validate_target(
                self.context.candidate,
                self.context.target,
                self.context.prepared[2],
                managed_target=self.context.previous_target,
            )
        except Exception as exc:
            raise LifecycleError.stable_failure(
                code="configuration_invalid",
                cause="The pending configuration or rendered Compose target is invalid.",
                remediation="Run flow-steward config validate and correct the reported issue.",
                step="validate_target",
                details=_configuration_failure_details(exc),  # AC17-R174: say what, too
            ) from exc

    def _execute_capture_before(self) -> None:
        try:
            if self.context.runtime_snapshot is None:
                self.context.runtime_snapshot = self.adapter.capture_recovery(
                    self.context.previous_target,
                    self.context.target,
                    operation_id=self.context.operation_id,
                )
            if self.context.before_manifest is None:
                self.context.before_manifest = self.adapter.capture_manifest(
                    self.context.previous,
                    self.context.previous_target,
                    operation_id=self.context.operation_id,
                    stage="before",
                )
            self.adapter.validate_preservation_baseline(self.context.before_manifest)
            self.context.lifecycle_state.save_previous_config(
                self.context.operation_id,
                {
                    "configuration": self.context.previous.model_dump(mode="json"),
                    "pending_revision": self.context.pending_revision,
                    "runtime": dict(self.context.runtime_snapshot),
                },
            )
        except Exception as exc:
            failure = LifecycleError.stable_failure(
                code="configuration_preservation_capture_failed",
                cause="The pre-apply preservation manifest could not be captured.",
                remediation="Repair the reported observation before applying configuration.",
                step="capture_before",
            )
            raw_failed_fields = getattr(exc, "failed_fields", ())
            if isinstance(raw_failed_fields, (list, tuple)):
                failed_fields = sorted(
                    {
                        str(field)
                        for field in raw_failed_fields
                        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(field))
                    }
                )
                if failed_fields:
                    failure.details["failed_fields"] = failed_fields
            raise failure from exc
        self._refuse_unrecorded_worker_scale()

    def _refuse_unrecorded_worker_scale(self) -> None:
        """Refuse to apply over a worker count nobody recorded.

        Review P2: Scalable used to be scaled with a bare `--scale fs-worker=N`, and every
        lifecycle `compose up` returned fs-worker to its declared count -- so an apply of
        any unrelated change quietly took the extra workers away. The count is now
        configuration; a running count that matches neither the recorded one nor the one
        being applied is named here, before anything changes.
        """
        previous = self.context.previous
        if previous.deployment_profile != "scalable" or self.context.runtime_snapshot is None:
            return
        # The rollback snapshot as the adapter recorded it (stack_service owns the model
        # and imports this module, so it is read as the mapping it is persisted as).
        recorded_services = self.context.runtime_snapshot.get("services")
        running = next(
            (
                int(item.get("replicas") or 1)
                for item in (
                    recorded_services if isinstance(recorded_services, (list, tuple)) else ()
                )
                if isinstance(item, Mapping)
                and item.get("service") == "fs-worker"
                and item.get("topology") != "absent"
            ),
            None,
        )
        if running is None or running in {
            previous.worker_replicas,
            self.context.candidate.worker_replicas,
        }:
            return
        raise LifecycleError.stable_failure(
            code="worker_replicas_unrecorded",
            cause=(
                f"fs-worker runs {running} containers, but the configuration records "
                f"{previous.worker_replicas}. Applying would change the worker count "
                "without anyone asking for it."
            ),
            remediation=(
                "Record the running count with ./flow-steward config set --worker-replicas "
                f"{running}, or return to {previous.worker_replicas}, then apply again."
            ),
            step="capture_before",
        )

    def _execute_render_configuration(self) -> None:
        self.mutated = True
        try:
            self.context.service.write_active_configuration(
                self.context.candidate,
                prepared=self.context.prepared,
            )
            self.adapter.apply_artifacts(
                self.context.candidate,
                operation_id=self.context.operation_id,
            )
        except Exception as exc:
            self._recover_and_raise(exc)

    def _execute_change_runtime(self) -> None:
        self.mutated = True
        try:
            if self.context.runtime_snapshot is None:
                raise ValueError("Runtime recovery inventory is unavailable")
            self.adapter.activate(
                self.context.previous,
                self.context.candidate,
                self.context.previous_target,
                self.context.target,
                self.context.runtime_snapshot,
                source_environment=self.context.previous_environment,
            )
        except Exception as exc:
            self._recover_and_raise(exc)

    def _execute_verify_health(self, output_sink: TechnicalSink) -> None:
        try:
            healthy = self.adapter.healthy(self.context.candidate, self.context.target)
        except Exception as exc:
            self._keep_candidate_evidence(output_sink)
            self._recover_and_raise(exc)
        if not healthy:
            self._keep_candidate_evidence(output_sink)
            self._recover_and_raise(RuntimeError("target runtime did not become healthy"))

    def _keep_candidate_evidence(self, output_sink: TechnicalSink) -> None:
        """Copy what the candidate said into the operation log before rollback removes it.

        AC17-R152: the rollback recreates the previous containers, so the candidate's own
        log -- where the PermissionError that decided the apply was -- was gone, and the
        operation log held one line. Only a watcher running beside the apply saw it.
        Evidence must never replace the failure it explains, so its own errors are noted
        and swallowed.
        """
        try:
            self.adapter.candidate_diagnostics(
                self.context.candidate, self.context.target, output_sink
            )
        except Exception as exc:  # the primary failure is what the operator must get
            with suppress(Exception):
                output_sink(f"Candidate log could not be read: {type(exc).__name__}")

    def _execute_capture_after(self) -> None:
        try:
            self.after_manifest = self.adapter.capture_manifest(
                self.context.candidate,
                self.context.target,
                operation_id=self.context.operation_id,
                stage="after",
            )
        except Exception as exc:
            self._recover_and_raise(exc)

    def _execute_verify_preservation(
        self, definition: LifecycleOperationDefinition, step: LifecycleOperationStep
    ) -> None:
        if self.context.before_manifest is None or self.after_manifest is None:
            raise LifecycleError.internal(
                step=step.step_id,
                recovery_command=definition.recovery_command,
            )
        try:
            snapshot = self.context.runtime_snapshot
            if snapshot is None:
                raise ValueError("No runtime image evidence was recorded before config apply")
            if not self.adapter.runtime_images_match(self.context.target, snapshot):
                # AC17-R167 (live): a bare ValueError reached neither the log nor the
                # result, so which container changed, and to what, was nowhere.
                drift = self.adapter.runtime_image_drift(self.context.target, snapshot)
                raise _ImageDriftError(
                    "A retained container changed its image during config apply: "
                    + ("; ".join(drift) or "no change was seen on a second look")
                )
            self.adapter.compare_manifests(
                self.context.before_manifest,
                self.after_manifest,
                profile_transition=(
                    self.context.previous.deployment_profile
                    != self.context.candidate.deployment_profile
                ),
            )
        except Exception as exc:
            self._recover_and_raise(exc)
        self.preservation_verified = True

    def _execute_commit_pending(self) -> None:
        try:
            self.context.service.store.clear_pending_if_revision(self.context.pending_revision)
        except Exception as exc:
            self._recover_and_raise(exc)

    def _recover_and_raise(self, cause: Exception) -> NoReturn:
        failure_details = _configuration_failure_details(cause)
        restored = self._restore_previous()
        if restored:
            failure_cause = "The target configuration failed; the previous runtime was restored."
            diagnostic = ": ".join(
                str(failure_details[key])
                for key in ("failed_action", "failure_detail")
                if key in failure_details
            )
            if diagnostic:
                failure_cause = f"{failure_cause} Primary failure: {diagnostic}"
            raise LifecycleError.stable_failure(
                code="configuration_apply_failed",
                cause=failure_cause,
                remediation="Inspect the operation log, correct the pending patch, and retry.",
                step="recovery",
                details=failure_details,
            ) from cause
        failure = LifecycleError.recovery_required(
            code="configuration_recovery_required",
            cause="The target configuration failed and the previous runtime is not healthy.",
            remediation="Inspect the operation log and restore the previous runtime.",
            step="recovery",
            recovery_command=_MESSAGE_FLOW_STEWARD_CONFIG_APPLY,
        )
        failure.details.update(failure_details)
        raise failure from cause

    def _restore_previous(self) -> bool:
        """Restore the previous runtime; every stage says in the operation log how it went.

        AC17-R167: a failed profile switch logged only "the previous runtime was
        restored" -- what was restored, and how that was verified, was nowhere, and the
        restored runtime answered 502 until the next ./install.sh.
        """
        if not self.mutated or self.context.runtime_snapshot is None:
            return False
        say = self._output_sink
        say("Rollback: " + _rollback_plan_words(self.context.runtime_snapshot))
        try:
            if not self.adapter.restore_recovery(
                self.context.previous_target,
                operation_id=self.context.operation_id,
                snapshot=self.context.runtime_snapshot,
                output_sink=say,
            ):
                say("Rollback: the previous containers could not be restored exactly.")
                return False
            say("Rollback: the previous containers are back; waiting until they are ready.")
            if not self.adapter.recovery_healthy(
                self.context.previous,
                self.context.previous_target,
                self.context.runtime_snapshot,
            ):
                say("Rollback: the restored primary service is not running or not healthy.")
                return False
            say("Rollback: the previous runtime is ready.")
            if self.context.before_manifest is None:
                say("Rollback: no pre-apply preservation evidence to compare with.")
                return False
            recovered = self.adapter.capture_manifest(
                self.context.previous,
                self.context.previous_target,
                operation_id=self.context.operation_id,
                stage="after",
            )
            self.adapter.compare_manifests(
                self.context.before_manifest,
                recovered,
                profile_transition=(
                    self.context.previous.deployment_profile
                    != self.context.candidate.deployment_profile
                ),
            )
            say("Rollback: verified; the data is as it was before the apply.")
            return True
        except Exception as exc:
            with suppress(Exception):
                say(f"Rollback failed: {type(exc).__name__}: {redact_installer_text(str(exc))}")
            return False

    def recover(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        del heartbeat
        self._output_sink = output_sink
        self.mutated = True
        if not self._restore_previous():
            raise LifecycleError.recovery_required(
                code="configuration_recovery_required",
                cause="Exact previous configuration and runtime recovery was not verified.",
                remediation="Inspect the operation log and restore the previous runtime.",
                step="recovery",
                recovery_command=_MESSAGE_FLOW_STEWARD_CONFIG_APPLY,
            )
        self.preservation_verified = True


class _ImageDriftError(ValueError):
    """Named for the failure details: which container changed, and to what."""

    action = "image_drift"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _discard_line(_line: str) -> None:
    return None


def _rollback_plan_words(snapshot: Mapping[str, object]) -> str:
    """What the rollback restores: each service, its recorded state and exact image."""
    services = snapshot.get("services")
    selected = snapshot.get("selected_services")
    parts: list[str] = []
    if isinstance(services, list) and isinstance(selected, list):
        for item in services:
            if isinstance(item, Mapping) and item.get("service") in selected:
                image = str(item.get("image_id") or "-")[:19]
                parts.append(f"{item.get('service')} {item.get('topology')} {image}")
    return "restoring " + (", ".join(parts) or "no recorded services") + "."


def _configuration_operation_steps() -> tuple[LifecycleOperationStep, ...]:
    return tuple(
        LifecycleOperationStep(step_id=step_id, label=label)
        for step_id, label in (
            ("verify_pending_revision", "Verify the selected pending configuration revision"),
            ("validate_target", "Validate configuration and Compose rendering"),
            ("capture_before", "Capture previous configuration and preservation state"),
            ("render_configuration", "Render active configuration and Compose environment"),
            ("change_runtime", "Apply the selected runtime configuration"),
            ("verify_health", "Verify runtime health"),
            ("capture_after", "Capture post-apply preservation state"),
            ("verify_preservation", "Verify preserved durable state"),
            ("commit_pending", "Commit the pending configuration"),
        )
    )


def _configuration_journal_error(
    *,
    operation_id: str,
    cause: str,
    recovery_command: str,
) -> LifecycleError:
    failure = LifecycleError.recovery_required(
        code="configuration_recovery_required",
        cause=cause,
        remediation=f"Inspect the lifecycle journal, then run exactly: {recovery_command}",
        step="recovery",
        recovery_command=recovery_command,
    )
    failure.operation_id = operation_id
    return failure
