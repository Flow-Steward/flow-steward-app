"""Postcondition-driven one-time installer state machine."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic, sleep
from uuid import uuid4

from core.application.installer import profile_switch
from core.application.installer.classification import (
    classify_installation,
    require_unambiguous_installation,
)
from core.application.installer.contracts import (
    BootstrapVerification,
    ImageCurrency,
    InstallationClassification,
    InstallationKind,
    InstallConfig,
    InstallJournal,
    InstallRunResult,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.host_settings import (
    applied_operator_settings,
    checked_operator_settings,
)
from core.application.installer.ports import (
    BoundedInstallerReadiness,
    InstallerGateway,
    InstallerLog,
    InstallerState,
    ProgressSink,
)
from core.application.installer.readiness_wait import (
    READINESS_RECONCILIATION_ATTEMPTS,
    READINESS_RECONCILIATION_DELAY_SECONDS,
    READINESS_RECONCILIATION_TIMEOUT_SECONDS,
    RECONCILABLE_READINESS_IDS,
    core_failed_ids,
    core_failures_text,
    wait_for_runtime_readiness,
    warning_text,
)
from core.application.installer.spec import INSTALLER_SPEC
from core.application.installer.step_plan import (
    _STEP_LABELS,
    _STEP_METHODS,
    _UPDATE_METHODS,
    _UPDATE_STEPS,
)
from core.application.readiness import CoreReadinessService, ReadinessArea
from core.application.release.contracts import ReleaseIdentity


class InstallerService:
    """Classify before mutation and resume only from verified postconditions."""

    def __init__(
        self,
        *,
        gateway: InstallerGateway,
        state: InstallerState,
        project_identity: str,
        progress_sink: ProgressSink | None = None,
        readiness_service: CoreReadinessService | None = None,
        readiness_reprobe: BoundedInstallerReadiness | None = None,
        readiness_sleep: Callable[[float], None] = sleep,
        readiness_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.gateway = gateway
        self.state = state
        self.project_identity = project_identity
        self.progress_sink = progress_sink
        self.readiness_service = readiness_service
        self.readiness_reprobe = readiness_reprobe
        self._readiness_sleep = readiness_sleep
        self._readiness_clock = readiness_clock
        self.last_config: InstallConfig | None = None

    def inspect(self) -> InstallationClassification:
        return classify_installation(self.gateway.inspect())

    def install(
        self,
        requested_config: InstallConfig,
        *,
        explicit_fields: frozenset[str] = frozenset(),
        target_release: ReleaseIdentity | None = None,
    ) -> InstallRunResult:
        """Install, resume or update; `explicit_fields` are the settings the operator gave."""
        try:
            with self.state.operation_lock():
                return self._install_locked(requested_config, explicit_fields, target_release)
        except InstallerFailure as exc:
            if exc.code not in {"installer_operation_active", "lifecycle_operation_active"}:
                raise
            log = self._open_log(uuid4().hex)
            exc.log_path = str(log.path)
            log.write(f"REFUSED {exc.code}: {exc.cause}")
            log.close()
            raise

    def _check_module_settings(
        self, installed: InstallConfig | None, changes: dict[str, str]
    ) -> None:
        """Refuse ``--setting FS_MODULE_*=...`` that no active platform module declares."""
        from core.application.lifecycle.module_settings import module_setting_names
        from core.application.lifecycle.operator_settings import OperatorSettingError

        requested = module_setting_names(changes)
        if not requested:
            return
        try:
            if installed is None:
                raise OperatorSettingError(
                    f"{', '.join(sorted(requested))}: platform-module settings exist only "
                    "while an installed platform module declares them; install first."
                )
            self.gateway.check_module_settings(installed, requested)
        except OperatorSettingError as exc:
            raise InstallerFailure(
                code="installer_setting_invalid",
                cause=str(exc),
                remediation=(
                    "./install.sh --setting accepts a FS_MODULE_* name only while an active "
                    "platform module declares it; run ./flow-steward config show for them."
                ),
                exit_code=3,
                step="preflight",
            ) from exc

    def _install_locked(
        self,
        requested_config: InstallConfig,
        explicit_fields: frozenset[str],
        target_release: ReleaseIdentity | None,
    ) -> InstallRunResult:
        if "operator_settings" in explicit_fields:
            checked_operator_settings(requested_config.operator_settings)
        evidence = self.gateway.inspect()
        classification = classify_installation(evidence)
        if (
            "operator_settings" in explicit_fields
            and classification.kind is not InstallationKind.INSTALLED
        ):
            # No platform module is active before the installation exists.
            self._check_module_settings(None, requested_config.operator_settings)
        operation_id = uuid4().hex

        try:
            require_unambiguous_installation(classification)
        except InstallerFailure as failure:
            log = self._open_log(operation_id)
            failure.log_path = str(log.path)
            log.write(f"REFUSED {failure.code}: {failure.cause}")
            log.close()
            raise

        existing = self.state.load_journal()
        if existing is not None and existing.status == "recovery_required":
            operation_id = existing.operation_id
            log = self._open_log(operation_id)
            failure = InstallerFailure(
                code=existing.last_error_code or "installation_rollback_failed",
                cause="A previous interrupted installation still requires manual recovery.",
                remediation=(
                    "Run ./flow-steward doctor and resolve only the reported task-owned "
                    "rollback resources before running ./install.sh again."
                ),
                exit_code=10,
                retryable=False,
                step="rollback",
                log_path=str(log.path),
            )
            log.write(f"REFUSED {failure.code}: {failure.cause}")
            log.close()
            raise failure
        if target_release is not None:
            self.state.stage_pending_release(target_release)
        config = existing.config if existing is not None else requested_config
        self.last_config = config
        operation_id = existing.operation_id if existing is not None else operation_id

        log = self._open_log(operation_id)
        if classification.kind is InstallationKind.INSTALLED:
            try:
                # The journal records what was installed; `config apply` changes what runs
                # -- the public route, storage and ingress among it -- in stack.toml only.
                config = self.gateway.installed_config(config)
                self.last_config = config
                # Item 5, step 4: a profile switch, or the rest of one an earlier run
                # left -- resumed whatever this run was asked, from the apply's journal.
                switch_changes: tuple[str, ...] = ()
                unfinished = self.gateway.unfinished_profile_switch()
                if unfinished is not None:
                    switch_changes = profile_switch.resume_switch(
                        self.gateway, config, unfinished, log=log
                    )
                elif (
                    "deployment_profile" in explicit_fields
                    and requested_config.deployment_profile != config.deployment_profile
                ):
                    try:
                        switch_changes = profile_switch.switch_profile(
                            self.gateway, config, requested_config.deployment_profile, log=log
                        )
                    except InstallerFailure:
                        # The release staged above names the target profile's images.
                        # With no switch left to resume it is no release upgrade either,
                        # and left behind it refused every later ./install.sh and
                        # `release upgrade` as "release X is pending".
                        if (
                            target_release is not None
                            and self.gateway.unfinished_profile_switch() is None
                        ):
                            self.state.clear_pending_release(target_release)
                        raise
                if switch_changes:
                    config = self.gateway.installed_config(config)
                    self.last_config = config
                compact = config.deployment_profile == "compact"
                # AC17-R157, the owner's decision: the one setting ./install.sh applies to
                # an installed Compact. Host settings (--setting) apply on either profile.
                # Everything else stays a refusal by name.
                applied_here = frozenset(
                    {"operator_settings", *(("worker_processes",) if compact else ())}
                )
                recorded_change = ""
                self._refuse_unapplied_settings(
                    requested_config,
                    config,
                    explicit_fields - applied_here,
                    profile=config.deployment_profile,
                )
                if (
                    compact
                    and "worker_processes" in explicit_fields
                    and requested_config.worker_processes != config.worker_processes
                ):
                    # Said here, not by convergence: an earlier step of this run may
                    # already recreate the runtime with the new count.
                    recorded_change = (
                        f"Compact worker processes: {config.worker_processes} -> "
                        f"{requested_config.worker_processes}."
                    )
                    config = self.gateway.set_compact_worker_processes(
                        config, processes=requested_config.worker_processes, log=log
                    )
                    self.last_config = config
                settings_change = ""
                if "operator_settings" in explicit_fields:
                    self._check_module_settings(config, requested_config.operator_settings)
                    applied = applied_operator_settings(config, requested_config.operator_settings)
                    if applied is not None:
                        candidate, environment = applied
                        config = self.gateway.set_operator_settings(
                            candidate, environment=environment, log=log
                        )
                        self.last_config = config
                        settings_change = "Host settings: " + ", ".join(
                            f"{name}={value or 'default'}"
                            for name, value in sorted(requested_config.operator_settings.items())
                        )
                currency = self.gateway.image_currency(config, log=log)
                # A source build and an official release can share a commit but not
                # artifact identity. An explicit new release must materialize its
                # images before any one-shot bootstrap or runtime consumes them.
                release_changed = (
                    target_release is not None
                    and self.state.load_installed_release() != target_release
                )
                if currency.update_required or release_changed:
                    return self._apply_image_update(
                        config,
                        currency,
                        classification=classification,
                        operation_id=operation_id,
                        log=log,
                        target_release=target_release,
                    )
                # W2 (auditor): this path brings fs-worker up too, and Compose returns it
                # to its recorded count -- a bare `--scale` was undone without a word, and
                # in the other direction live workers would have been stopped.
                self.gateway.refuse_unrecorded_worker_scale(config, log=log)
                # AC17-R162: a stack stopped on this revision comes back up here, before
                # the worker identity is checked against its database.
                started = self.gateway.start_stopped_services(config, log=log)
                self.gateway.ensure_worker_identity(config, log=log)
                self.gateway.activate_worker_identity(config, log=log)
                self.gateway.verify_health(config, log=log)
                self.gateway.ensure_cli_runtime(config, log=log)
                self.gateway.verify_storage(config, log=log, read_only=True)
                self.gateway.verify_public_route(config, log=log)
                bootstrap_state = self.gateway.verify_bootstrap(
                    config, log=log, require_first_admin=False
                )
                if bootstrap_state.bootstrap_required and bootstrap_state.token_expired:
                    # AC17-R50: this is the operator who installed on a server, was called
                    # away for an hour, and came back. Naming a command here instead of
                    # acting leaves a Compact install with no route to a first administrator
                    # at all, because the installer is the only thing that can issue one.
                    self.gateway.reissue_bootstrap_token(config, log=log)
                    bootstrap_state = self.gateway.verify_bootstrap(
                        config, log=log, require_first_admin=False
                    )
                readiness_warnings = self._verify_shared_readiness(config, log=log)
                converged = self._converge_compact_workers(
                    config, log=log, changed=bool(recorded_change)
                )
                if target_release is not None:
                    self._publish_release(config, target_release, log=log)
                finished_from = self._finish_unrecorded_update(currency, log=log)
                # AC17-R158: also finishes a cleanup an interrupted update left.
                pruned = self.gateway.prune_superseded_releases(
                    config, current=currency.source_revision, previous=finished_from, log=log
                )
                changes = tuple(
                    item
                    for item in (
                        *switch_changes,
                        started,
                        recorded_change,
                        settings_change,
                        converged,
                        (
                            f"Recorded {currency.source_revision} as the installed release, "
                            f"replacing {finished_from}."
                            if finished_from
                            else ""
                        ),
                        pruned,
                        *self.gateway.take_notices(),
                    )
                    if item
                )
                behind = self._behind_sources_notice(currency)
                if behind:
                    log.write(behind)
                    readiness_warnings = (*readiness_warnings, behind)
                bootstrap_url, bootstrap_setup_url, bootstrap_instruction = self._bootstrap_handoff(
                    config, bootstrap_state
                )
                return InstallRunResult(
                    classification=classification.kind,
                    status="already_installed",
                    product_url=self._product_url(config),
                    operation_id=operation_id,
                    log_path=str(log.path),
                    bootstrap_token_path=bootstrap_state.token_path,
                    bootstrap_url=bootstrap_url,
                    bootstrap_setup_url=bootstrap_setup_url,
                    bootstrap_instruction=bootstrap_instruction,
                    completed_steps=existing.completed_steps if existing else (),
                    warnings=readiness_warnings,
                    changes=changes,
                )
            except InstallerFailure as exc:
                exc.log_path = str(log.path)
                log.write(f"FAILED {exc.code}: {exc.cause}")
                raise
            except Exception as exc:
                failure = InstallerFailure(
                    code="unexpected_internal_error",
                    cause=f"{type(exc).__name__}: {exc}",
                    remediation="Inspect the installer log, correct the cause, and rerun ./install.sh.",
                    exit_code=70,
                    retryable=False,
                    step="verification",
                    log_path=str(log.path),
                )
                log.write(f"FAILED {failure.code}: {failure.cause}")
                raise failure from exc
            finally:
                log.close()

        journal = (
            existing.model_copy(
                update={
                    "observed_project_identity": evidence.observed_project_identity,
                    "observed_containers": evidence.owned_containers,
                    "observed_volumes": evidence.owned_volumes,
                }
            )
            if existing is not None
            else InstallJournal(
                operation_id=operation_id,
                project_identity=self._project_identity(),
                config=config,
                observed_project_identity=evidence.observed_project_identity,
                observed_containers=evidence.owned_containers,
                observed_volumes=evidence.owned_volumes,
            )
        )
        completed = list(journal.completed_steps)
        bootstrap_state: BootstrapVerification | None = None
        readiness_warnings: tuple[str, ...] = ()
        current_step = "classification"
        try:
            for step_id, label, method_name in _STEP_METHODS:
                if step_id == "manifest":
                    current_step = "readiness"
                    self._emit("readiness", "running", "Verify shared core readiness")
                    readiness_warnings = self._verify_shared_readiness(config, log=log)
                    self._emit("readiness", "succeeded", "Shared core readiness verified")
                current_step = step_id
                if step_id in completed and self.gateway.postcondition_holds(step_id, config):
                    self._emit(step_id, "skipped", f"{label}: postcondition already holds")
                    continue
                created_paths = tuple(
                    dict.fromkeys((*journal.created_paths, *self._step_paths(step_id, config)))
                )
                if created_paths != journal.created_paths:
                    journal = journal.model_copy(
                        update={"created_paths": created_paths, "updated_at": datetime.now(UTC)}
                    )
                    self.state.save_journal(journal)
                self._emit(step_id, "running", label)
                method: Callable[..., object] = getattr(self.gateway, method_name)
                value = method(config, log=log)
                if step_id == "bootstrap" and isinstance(value, BootstrapVerification):
                    bootstrap_state = value
                if step_id not in completed:
                    completed.append(step_id)
                journal = journal.model_copy(
                    update={
                        "status": "running",
                        "completed_steps": tuple(completed),
                        "failed_step": None,
                        "last_error_code": None,
                        "created_paths": created_paths,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self.state.save_journal(journal)
                self._emit(step_id, "succeeded", label)
            journal = journal.model_copy(
                update={
                    "status": "completed",
                    "retryable": False,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.state.save_journal(journal)
            if target_release is not None:
                self._publish_release(config, target_release, log=log)
            if bootstrap_state is None:
                bootstrap_state = BootstrapVerification(bootstrap_required=True)
            bootstrap_url, bootstrap_setup_url, bootstrap_instruction = self._bootstrap_handoff(
                config, bootstrap_state
            )
            result = InstallRunResult(
                classification=classification.kind,
                status="installed",
                product_url=self._product_url(config),
                operation_id=operation_id,
                log_path=str(log.path),
                bootstrap_token_path=bootstrap_state.token_path,
                bootstrap_url=bootstrap_url,
                bootstrap_setup_url=bootstrap_setup_url,
                bootstrap_instruction=bootstrap_instruction,
                completed_steps=tuple(completed),
                warnings=readiness_warnings
                or ("AI-backed features remain inactive until configured after login.",),
            )
            log.close()
            return result
        except InstallerFailure as exc:
            failure = exc
        except KeyboardInterrupt:
            failure = InstallerFailure(
                code="installation_interrupted",
                cause="The installer was interrupted before the current step completed.",
                remediation="Rerun ./install.sh to start a new installation from the beginning.",
                exit_code=10,
                retryable=True,
                step=current_step,
            )
            failure.log_path = str(log.path)
            try:
                journal = journal.model_copy(
                    update={
                        "status": "recovery_required",
                        "completed_steps": tuple(completed),
                        "failed_step": "rollback",
                        "last_error_code": "installation_rollback_failed",
                        "retryable": True,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self.state.save_journal(journal)
                self.gateway.rollback_initial_install(journal, log=log)
                log.write("ROLLED BACK installation_interrupted: task-owned runtime removed.")
                self.state.clear_journal()
            except InstallerFailure as rollback_failure:
                current_step = "rollback"
                failure = rollback_failure
            except Exception as exc:
                current_step = "rollback"
                failure = InstallerFailure(
                    code="installation_rollback_failed",
                    cause=f"{type(exc).__name__}: {exc}",
                    remediation=(
                        "Inspect the installer log and task-owned Docker resources before "
                        "running ./install.sh again."
                    ),
                    exit_code=10,
                    retryable=True,
                    step="rollback",
                )
            else:
                log.close()
                raise failure
        except Exception as exc:
            failure = InstallerFailure(
                code="unexpected_internal_error",
                cause=f"{type(exc).__name__}: {exc}",
                remediation="Inspect the installer log, correct the cause, and rerun ./install.sh to resume safely.",
                exit_code=70,
                retryable=False,
                step=current_step,
            )
        failure.log_path = str(log.path)
        journal = journal.model_copy(
            update={
                "status": (
                    "recovery_required"
                    if failure.code in {"installation_interrupted", "installation_rollback_failed"}
                    else "failed"
                ),
                "completed_steps": tuple(completed),
                "failed_step": current_step,
                "last_error_code": failure.code,
                "retryable": failure.retryable,
                "updated_at": datetime.now(UTC),
            }
        )
        persistence_error: Exception | None = None
        try:
            self.state.save_journal(journal)
        except Exception as exc:
            persistence_error = exc
        finally:
            with suppress(OSError):
                log.write(f"FAILED {failure.code}: {failure.cause}")
            with suppress(OSError):
                log.close()
        if persistence_error is not None:
            if failure.code == "installation_rollback_failed":
                failure.cause = (
                    f"{failure.cause} Recovery journal persistence also failed: "
                    f"{type(persistence_error).__name__}: {persistence_error}"
                )
                raise failure from persistence_error
            raise persistence_error
        raise failure

    def _project_identity(self) -> str:
        value = getattr(self.gateway, "project_identity", None)
        if callable(value):
            return str(value())
        return self.project_identity

    def _open_log(self, operation_id: str) -> InstallerLog:
        try:
            return self.state.open_log(operation_id)
        except FileExistsError:
            return self.state.open_log(f"{operation_id}-{uuid4().hex[:8]}")

    def _step_paths(self, step_id: str, config: InstallConfig) -> tuple[str, ...]:
        tls_paths: tuple[str, ...] = ()
        if config.tls_mode != "reverse_proxy":
            configured = (
                ()
                if config.tls_mode == "provided"
                else (config.certificate_path, config.private_key_path)
            )
            runtime = (
                ".ssl/cert.pem",
                ".ssl/key.pem",
            )
            tls_paths = tuple(dict.fromkeys((*configured, *runtime)))
        paths: dict[str, tuple[str, ...]] = {
            "configuration": (
                ".flow-steward/stack.toml",
                ".flow-steward/compose.env",
                *(
                    (".flow-steward/installer.compose.override.yaml",)
                    if config.tls_mode == "reverse_proxy"
                    else ()
                ),
            ),
            "secrets": (
                ".env" if config.environment == "development" else ".env.prod",
                *(
                    (".flow-steward/secret-output/bootstrap-token",)
                    if config.public_host not in {"localhost", "127.0.0.1", "::1"}
                    else ()
                ),
            ),
            "tls": tls_paths,
            "manifest": (".flow-steward/initial-preservation-manifest.json",),
            "cli_runtime": (".flow-steward/runtime",),
        }
        return paths.get(step_id, ())

    def _apply_image_update(
        self,
        config: InstallConfig,
        currency: ImageCurrency,
        *,
        classification: InstallationClassification,
        operation_id: str,
        log: InstallerLog,
        target_release: ReleaseIdentity | None,
    ) -> InstallRunResult:
        """Bring a completed installation onto the images these sources produce.

        AC17-R141. The order is the install loop's, and it is what keeps a database
        migration safe: the new image checks the schema before any runtime is
        restarted on it, and a release whose migration the database does not have yet
        stops there, with the running application still on its old image and its old
        schema. Nothing here applies a migration or takes a backup -- both are the
        operator's, by the owner's decision, and the schema refusal names them.
        """
        log.write(currency.reason)
        if currency.running_revision and currency.running_revision != currency.source_revision:
            # What a rerun reads if this run switches the runtime and stops before it
            # is verified: the record below is only written at the end.
            log.write(f"Updating from {currency.running_revision} to {currency.source_revision}.")
        for step_id in _UPDATE_STEPS:
            label, method_name = _STEP_LABELS[step_id]
            method_name = _UPDATE_METHODS.get(step_id, method_name)
            self._emit(step_id, "running", label)
            method: Callable[..., object] = getattr(self.gateway, method_name)
            method(config, log=log)
            if step_id == "worker_identity":
                # AC17-R142: before the runtime step, whose recreate absorbs the env
                # change. The "already installed" path must not do this: there the
                # change alone would recreate a runtime nothing else asked to restart.
                self.gateway.drop_superseded_worker_credential(config, log=log)
            self._emit(step_id, "succeeded", label)
        self.gateway.verify_storage(config, log=log, read_only=True)
        self.gateway.verify_public_route(config, log=log)
        bootstrap_state = self.gateway.verify_bootstrap(config, log=log, require_first_admin=False)
        if bootstrap_state.bootstrap_required and bootstrap_state.token_expired:
            self.gateway.reissue_bootstrap_token(config, log=log)
            bootstrap_state = self.gateway.verify_bootstrap(
                config, log=log, require_first_admin=False
            )
        readiness_warnings = self._verify_shared_readiness(config, log=log)
        converged = self._converge_compact_workers(config, log=log)
        if target_release is not None:
            self._publish_release(config, target_release, log=log)
        # AC17-R158: only once the update is verified, keeping the rollback it came from.
        self.gateway.record_installed_releases(
            current=currency.source_revision, previous=currency.running_revision
        )
        pruned = self.gateway.prune_superseded_releases(
            config,
            current=currency.source_revision,
            previous=currency.running_revision,
            log=log,
        )
        changes = tuple(item for item in (converged, pruned, *self.gateway.take_notices()) if item)
        if target_release is not None and currency.running_revision == currency.source_revision:
            applied = (
                f"Applied official release {target_release.version} at {currency.source_revision}."
            )
        elif currency.running_revision == currency.source_revision:
            # R159 acceptance: "Updated from A to A" after a stopped update -- the run
            # replaced images that update had recorded, not the running version.
            applied = (
                f"Completed the update to {currency.source_revision} that an earlier run "
                "left unfinished."
            )
        elif not currency.running_revision:
            # AC17-R162: after a stop or a restore nothing runs to read a revision from;
            # "an undated image" named an image that did not exist.
            applied = (
                f"Updated to {currency.source_revision}; the runtime was not running, so "
                "the revision it replaced is not known."
            )
        else:
            applied = f"Updated from {currency.running_revision} to {currency.source_revision}."
        if not currency.resumes_prepared_update:
            # Only true of a run that found the sources ahead and got through the
            # schema check itself. A run finishing an update an earlier run prepared
            # may be the one after the operator migrated the database.
            # A release upgrade resumed after a separate `database upgrade` passes
            # this check too; "carried no migration" told that operator his six
            # migrations never happened (0.9.12 -> 0.9.19 jump, 2026-11-19).
            applied += (
                " The database already matched this release, so no migration was left "
                "to apply at activation."
            )
        log.write(applied)
        bootstrap_url, bootstrap_setup_url, bootstrap_instruction = self._bootstrap_handoff(
            config, bootstrap_state
        )
        return InstallRunResult(
            classification=classification.kind,
            status="installed",
            product_url=self._product_url(config),
            operation_id=operation_id,
            log_path=str(log.path),
            bootstrap_token_path=bootstrap_state.token_path,
            bootstrap_url=bootstrap_url,
            bootstrap_setup_url=bootstrap_setup_url,
            bootstrap_instruction=bootstrap_instruction,
            completed_steps=_UPDATE_STEPS,
            warnings=(*readiness_warnings, applied),
            changes=changes,
            updated_from_revision=currency.running_revision,
            updated_to_revision=currency.source_revision,
        )

    def _finish_unrecorded_update(self, currency: ImageCurrency, *, log: InstallerLog) -> str:
        """Write the release record an update left behind; the revision it replaced, or "".

        An update writes the record only once it is verified. One that switched the
        runtime and then stopped -- readiness failed while the runtime was still
        starting, 2026-11-20 -- leaves the new images running, so its rerun has nothing
        to update and comes here, as "already installed", with the record still naming
        the old revision as current. Only a verified run of these sources' own images
        finishes it: what an operator pinned, or a runtime behind the sources, is not
        what the update installed.
        """
        if not currency.running_revision or currency.running_revision != currency.source_revision:
            return ""
        previous = self.gateway.finish_installed_releases_record(current=currency.source_revision)
        if previous:
            log.write(
                f"Updated from {previous} to {currency.source_revision}. An earlier run "
                "switched the runtime and stopped before verifying it; this run verified it "
                "and recorded the release."
            )
        return previous

    def _converge_compact_workers(
        self, config: InstallConfig, *, log: InstallerLog, changed: bool = False
    ) -> str:
        """Bring the Compact runtime to the applied worker count, on every installed run.

        Every run, not only the one that changed the count: a run killed after the new
        count was written and before the runtime was recreated is finished by the next.
        """
        if config.deployment_profile != "compact":
            return ""
        return self.gateway.converge_compact_worker_processes(config, log=log, changed=changed)

    @staticmethod
    def _refuse_unapplied_settings(
        requested: InstallConfig,
        installed: InstallConfig,
        explicit_fields: frozenset[str],
        *,
        profile: str,
    ) -> None:
        """Name every setting the operator gave that this run would not apply.

        AC17-R157: on an installed system ./install.sh runs from the applied configuration,
        so `--worker-processes 1` on a Compact with two printed "already installed" and
        changed nothing. A flag that is dropped without a word is worse than a refusal.
        """
        wanted = requested.model_dump(mode="json")
        current = installed.model_dump(mode="json")
        labels = {field.key: field for field in INSTALLER_SPEC.fields}
        changes = [
            (labels[key], current[key], wanted[key])
            for key in sorted(explicit_fields)
            if key in labels and key in wanted and wanted[key] != current.get(key)
        ]
        if not changes:
            return
        described = "; ".join(f"{field.label}: {old} -> {new}" for field, old, new in changes)
        options = " ".join(
            field.option_name or f"--{field.key.replace('_', '-')}" for field, _old, _new in changes
        )
        if profile == "compact":
            remediation = (
                f"An installed Compact is not reconfigured by ./install.sh. Rerun it without "
                f"{options} to update or verify the installation."
            )
        else:
            remediation = (
                f"Change it with ./flow-steward config set {options} ... and "
                "./flow-steward config apply --confirm, then rerun ./install.sh without them."
            )
        raise InstallerFailure(
            code="installed_setting_not_applied",
            cause=f"This installation is already installed and ./install.sh would not apply: {described}.",
            remediation=remediation,
            exit_code=7,
            retryable=False,
            step="preflight",
        )

    @staticmethod
    def _behind_sources_notice(currency: ImageCurrency) -> str:
        """Say so when the runtime is behind the sources and this run left it there.

        That happens only for images the installer did not build -- an operator's own
        pins -- which it must not rebuild underneath them. Saying nothing would repeat
        the false success R141 is about in the one case where acting is not ours.
        """
        running = currency.running_revision
        if not running or running == currency.source_revision:
            return ""
        if currency.official_release:
            return (
                f"This checkout is at {currency.source_revision}, and the official release "
                f"running here was built from {running}. Official images are never rebuilt "
                "from a checkout: run `flow-steward release upgrade --confirm` to move to a "
                "newer release, or check out the installed release tag again."
            )
        if currency.shell_pins:
            names = ", ".join(currency.shell_pins)
            return (
                f"These sources are at {currency.source_revision}, and the running images "
                f"were built from {running}. {names} is set in the shell that ran "
                "./install.sh, so the images it names are the operator's and were left in "
                f"place, not rebuilt. Unset {names} and rerun ./install.sh to build and "
                "run these sources."
            )
        return (
            f"These sources are at {currency.source_revision}, and the running images "
            f"were built from {running}. They were chosen rather than built by this "
            "installer, so they were left in place: supply images built from these "
            "sources, or remove the pins to let the installer build them."
        )

    def _bootstrap_handoff(
        self, config: InstallConfig, state: BootstrapVerification
    ) -> tuple[str, str, str]:
        """Return the dump-safe URL, the complete one-time URL, and the instruction."""
        if not state.bootstrap_required:
            return (
                "",
                "",
                "First-administrator bootstrap is complete; open the product URL to sign in.",
            )
        url = self._product_url(config).rstrip("/") + "/bootstrap"
        if state.token_expired:
            # Reached only when issuing a replacement did not take effect; the installed path
            # re-issues before composing the handoff. The remedy named here has to work on
            # every profile, and `users issue-bootstrap-token` does not: on Compact it is an
            # installer operation, writing host installation state and recreating the runtime
            # container, so the launcher refuses it there (AC17-R88).
            return (
                "",
                "",
                "The non-loopback setup token expired and could not be replaced automatically. "
                "Rerun ./install.sh to issue a new one-time setup link.",
            )
        if state.token_path:
            # The installer holds both halves at this point. Emitting the bare URL next to
            # prose asking the operator to assemble the working one guarantees a 403 on the
            # first click, which is the only screen a new installation has.
            if state.token:
                return (
                    url,
                    f"{url}#setup-token={state.token}",
                    "Open the one-time setup link printed above within 60 minutes to create "
                    "the first administrator. It carries a setup token, so treat the link as "
                    f"a secret. The token is also stored at {state.token_path}.",
                )
            return (
                url,
                "",
                f"Read the owner-only token from {state.token_path}, append it as "
                "#setup-token=<token>, and open the resulting URL within 60 minutes.",
            )
        return url, "", "Open this URL to create the first administrator."

    def _product_url(self, config: InstallConfig) -> str:
        product_url = getattr(self.gateway, "product_url", None)
        if callable(product_url):
            return str(product_url(config))
        host = f"[{config.public_host}]" if ":" in config.public_host else config.public_host
        return f"https://{host}:{config.public_port}"

    def _publish_release(
        self, config: InstallConfig, release: ReleaseIdentity, *, log: InstallerLog
    ) -> None:
        """Verify final CLI identity before committing an official installed release."""
        self.gateway.publish_release_environment(config, release)
        if config.deployment_profile == "scalable":
            self.gateway.ensure_cli_runtime(config, log=log)
        self.state.publish_installed_release(release)

    def _verify_shared_readiness(
        self, config: InstallConfig, *, log: InstallerLog
    ) -> tuple[str, ...]:
        if self.readiness_service is None:
            return ()
        report = wait_for_runtime_readiness(
            self.readiness_service.check,
            self.readiness_reprobe,
            sleep=self._readiness_sleep,
            clock=self._readiness_clock,
            log=log,
        )
        failed_ids = core_failed_ids(report)
        if failed_ids and failed_ids.issubset(RECONCILABLE_READINESS_IDS):
            if self.readiness_reprobe is None:
                raise InstallerFailure(
                    code="core_readiness_blocked",
                    cause="A bounded installed-runtime readiness probe is unavailable.",
                    remediation="Restore the installer readiness adapter and resume ./install.sh.",
                    exit_code=4,
                    retryable=True,
                    step="readiness",
                )
            # This is an installer mutation phase, never a Doctor/probe repair.
            # The existing derived-sync worker commits asynchronously even while
            # ordinary job consumption is paused by maintenance.
            self.gateway.reconcile_readiness_capabilities(
                config, check_ids=tuple(sorted(failed_ids)), log=log
            )
            deadline = self._readiness_clock() + READINESS_RECONCILIATION_TIMEOUT_SECONDS
            for attempt in range(READINESS_RECONCILIATION_ATTEMPTS):
                remaining = deadline - self._readiness_clock()
                if remaining <= 0:
                    break
                report = self.readiness_reprobe(remaining)
                if self._readiness_clock() >= deadline:
                    raise InstallerFailure(
                        code="core_readiness_blocked",
                        cause="Installed-runtime readiness exceeded its reconciliation deadline.",
                        remediation="Run ./flow-steward doctor and resume ./install.sh.",
                        exit_code=4,
                        retryable=True,
                        step="readiness",
                    )
                failed_ids = core_failed_ids(report)
                if not report.core_failed or not failed_ids.issubset(RECONCILABLE_READINESS_IDS):
                    break
                remaining = deadline - self._readiness_clock()
                if remaining <= 0:
                    break
                if attempt + 1 < READINESS_RECONCILIATION_ATTEMPTS:
                    self._readiness_sleep(min(READINESS_RECONCILIATION_DELAY_SECONDS, remaining))
        write = getattr(log, "write", None)
        if callable(write):
            write(
                "Shared readiness: "
                f"core_failed={report.core_failed}, optional_warnings={report.optional_warnings}."
            )
        if report.core_failed:
            raise InstallerFailure(
                code="core_readiness_blocked",
                cause="Core readiness blockers remain: " + core_failures_text(report),
                remediation="Run ./flow-steward doctor, resolve core blockers, and resume ./install.sh.",
                exit_code=4,
                retryable=True,
                step="readiness",
            )
        # What the operator reads under "already installed" (AC17-R162 acceptance): only
        # optional findings that warn or fail, each with what it found and what to do.
        # Recommendations are not warnings -- status and doctor list them -- and a bare
        # title such as "Worker Availability" told nobody anything.
        return tuple(
            warning_text(finding)
            for finding in report.findings
            if finding.area is ReadinessArea.OPTIONAL
            and finding.status.value in {"warning", "failed"}
        )

    def _emit(self, step_id: str, status: str, detail: str) -> None:
        if self.progress_sink is not None:
            self.progress_sink(step_id, status, detail)
