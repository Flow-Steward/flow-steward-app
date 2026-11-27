"""Shared read-only release status and forward-only upgrade policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.catalog import ReleaseDiscoveryError, ReleaseVersion
from core.application.release.contracts import (
    ReleaseIdentity,
    release_identity_from_resolution,
)
from core.application.release.installed_origin import (
    InstalledReleaseOrigin,
    InstalledReleaseOriginPort,
    SourceInstallationOrigin,
)
from core.application.release.platform_modules import (
    ModuleActivator,
    NoModuleActivator,
    highest_staged_modules,
    select_staged_modules,
)
from core.application.release.selector import (
    _INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
    _TARGET_CONTEXT_MISMATCH_CAUSE,
    _TARGET_CONTEXT_MISMATCH_REMEDIATION,
    OfficialReleaseSelector,
    ReleaseDiscoveryPort,
    ReleaseResolverPort,
    ReleaseStatePort,
)
from core.application.release.service_models import (
    _SUPPORTED_RELEASE_PLATFORMS,
    DeploymentProfile,
    PreparedReleaseUpgrade,
    ReleaseActivatorPort,
    ReleaseCheckRequest,
    ReleaseInfoRequest,
    ReleaseInstallRequest,
    ReleaseMaintenancePort,
    ReleaseModulesApplyRequest,
    ReleaseModulesListRequest,
    ReleaseModulesResult,
    ReleasePlatform,
    ReleaseResult,
    ReleaseTransitionBinding,
    ReleaseUpgradeRequest,
    release_platform_for_machine,
)
from core.application.release.upgrade_review import (
    MAINTENANCE_DISABLE_COMMAND as _MAINTENANCE_DISABLE_COMMAND,
)
from core.application.release.upgrade_review import upgrade_review

if TYPE_CHECKING:
    from core.application.release.upgrade_workflow import ReleaseUpgradeWorkflow

_MAINTENANCE_ENABLE_COMMAND = "flow-steward maintenance enable --confirm"
_BACKUP_CREATE_COMMAND = "flow-steward backup create"
_DATABASE_PREFLIGHT_COMMAND = "flow-steward database preflight"
_DATABASE_UPGRADE_COMMAND = "flow-steward database upgrade --maintenance-confirmed --confirm"


class ReleaseService:
    """Apply one policy to Compact and Scalable around shared release resolution."""

    def __init__(
        self,
        *,
        discovery: ReleaseDiscoveryPort,
        state: ReleaseStatePort,
        resolver: ReleaseResolverPort,
        maintenance: ReleaseMaintenancePort,
        activator: ReleaseActivatorPort,
        profile: DeploymentProfile,
        platform: ReleasePlatform,
        origin: InstalledReleaseOriginPort,
        workflow: ReleaseUpgradeWorkflow | None = None,
        environment: Literal["development", "production"] | None = None,
    ) -> None:
        if profile not in {"compact", "scalable"}:
            raise ValueError("unsupported deployment profile")
        if platform not in _SUPPORTED_RELEASE_PLATFORMS:
            raise ValueError("unsupported release platform")
        self.discovery = discovery
        self.state = state
        self.resolver = resolver
        self.maintenance = maintenance
        self.activator = activator
        self.profile = profile
        self.platform = platform
        self.origin = origin
        self.workflow = workflow
        # Owner 2026-11-21: development upgrades like production; results say which
        # environment they act on, and an upgrade never changes it.
        self.environment = environment
        self.selector = OfficialReleaseSelector(
            discovery=discovery,
            state=state,
            resolver=resolver,
            profile=profile,
            platform=platform,
        )

    def prepare_install(self, request: ReleaseInstallRequest) -> ReleaseIdentity:
        return self.selector.prepare_install(request)

    def require_official_installation(self) -> ReleaseIdentity:
        """Return the exact official identity required by portable Clone."""
        origin = self._load_origin()
        if origin.kind == "official":
            return origin.release
        raise LifecycleError.stable_failure(
            code="clone_official_release_required",
            cause="Portable Clone requires a verified official release installation.",
            remediation=(
                "Run `flow-steward release check`, enable maintenance, and complete "
                "`flow-steward release upgrade --confirm` before using Clone."
            ),
            step="clone_prerequisite",
        )

    def modules_list(self, request: ReleaseModulesListRequest) -> ReleaseModulesResult:
        """ADR 0082 §5: the active set and every staged artifact; changes nothing."""
        del request
        modules = self._module_activator()
        if not modules.supports_activation:
            return ReleaseModulesResult(
                deployment_profile=self.profile,
                detail=str(getattr(modules, "unavailable_reason", "") or "").strip()
                or "This deployment cannot activate platform modules.",
            )
        active = modules.installed_set()
        staged = modules.staged_modules()
        highest = highest_staged_modules(staged) if staged else ()
        return ReleaseModulesResult(
            deployment_profile=self.profile,
            activation_supported=True,
            active_modules=active,
            staged_modules=staged,
            next_steps=(
                ("flow-steward release modules-apply --confirm",)
                if highest and highest != active
                else ()
            ),
        )

    def module_upgrade_request(self, request: ReleaseModulesApplyRequest) -> ReleaseUpgradeRequest:
        """Translate a module change into the journaled upgrade of the installed release.

        The target release is the installed one, so the workflow runs its module-only
        path: maintenance, backup, stop the roles, write the list, database upgrade,
        start the roles, health check, reopen (ADR 0082 §5.6).
        """
        origin = self._load_origin()
        if origin.kind != "official":
            raise LifecycleError.stable_failure(
                code="release_modules_source_installation",
                cause=(
                    "A platform-module change keeps the installed official release; this "
                    "installation was built from source and has none."
                ),
                remediation=(
                    "Move this installation to an official release with flow-steward "
                    "release upgrade first, then change its platform modules."
                ),
                step="release_policy",
            )
        installed = origin.release.version
        if request.restore_previous:
            return ReleaseUpgradeRequest(target_version=installed, restore_previous_modules=True)
        modules = self._module_activator()
        if not modules.supports_activation:
            reason = str(getattr(modules, "unavailable_reason", "") or "").strip()
            raise LifecycleError.stable_failure(
                code="release_modules_unsupported",
                cause=" ".join(
                    part
                    for part in ("This deployment cannot activate platform modules.", reason)
                    if part
                ),
                remediation="Run this command where the deployment's runtime directory is managed.",
                step="release_policy",
            )
        staged = modules.staged_modules()
        selection = tuple(item.strip() for item in request.modules.split(",") if item.strip())
        target = (
            select_staged_modules(staged, selection)
            if selection
            else highest_staged_modules(staged)
        )
        if not target:
            raise LifecycleError.stable_failure(
                code="release_modules_none_staged",
                cause="No platform module is staged on this deployment.",
                remediation=(
                    "Let the deployment manager stage the platform modules, then run "
                    "flow-steward release modules-list."
                ),
                step="release_policy",
            )
        return ReleaseUpgradeRequest(target_version=installed, target_modules=target)

    def _module_activator(self) -> ModuleActivator:
        if self.workflow is None:
            return NoModuleActivator(reason="This CLI has no journaled release-upgrade workflow.")
        return self.workflow.modules

    def info(self, request: ReleaseInfoRequest) -> ReleaseResult:
        del request
        origin = self._load_origin()
        installed = origin.release if origin.kind == "official" else None
        pending = self._info_pending(installed)
        if pending is not None:
            # An upgrade that stopped half way is the one thing an operator reading
            # `release info` must not miss: the installed identity alone looked current.
            return self._result(
                status="upgrade_pending",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                target=pending,
                detail=f"Release {pending.version} is prepared but not fully applied.",
                remediation=(
                    f"Keep maintenance enabled and repeat `flow-steward release upgrade "
                    f"--target-version {pending.version} --confirm`."
                ),
            )
        if origin.kind == "official":
            return self._result(status="installed", installed=installed)
        return self._result(
            status="installed",
            installed=None,
            installed_origin="source",
            source_commit_sha=origin.commit_sha,
        )

    def _info_pending(self, installed: ReleaseIdentity | None) -> ReleaseIdentity | None:
        """The release an unfinished upgrade or source transition is waiting to apply."""
        if installed is not None:
            if self.selector.profile_switch_leftover(installed) is not None:
                return None
            pending = self._effective_pending(installed)
            return pending if pending is not None and pending != installed else None
        binding = self._load_transition_binding()
        phase = getattr(self._load_transition_handoff(), "phase", "")
        if isinstance(binding, ReleaseTransitionBinding) and phase != "installer_completed":
            return binding.target_release
        return None

    def check(self, request: ReleaseCheckRequest) -> ReleaseResult:
        """Compare durable applied state without mutating Git, Docker, or lifecycle state."""
        del request
        origin = self._load_origin()
        installed = origin.release if origin.kind == "official" else None
        if origin.kind == "source":
            self._require_source_repository(origin)
        if installed is not None:
            self._refuse_profile_switch_leftover(installed)
        pending = self._effective_pending(installed) if installed is not None else None
        if installed is not None and pending is not None and pending != installed:
            self._require_target_context(pending)
            return self._result(
                status="upgrade_pending",
                installed=installed,
                target=pending,
                detail=f"Release {pending.version} is prepared but not fully applied.",
                remediation=(
                    f"Keep maintenance enabled and repeat `flow-steward release upgrade "
                    f"--target-version {pending.version} --confirm`."
                ),
            )
        try:
            target, source = self.selector.latest()
        except LifecycleError:
            raise
        except ReleaseDiscoveryError as exc:
            return self._result(
                status="check_unavailable",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                detail=str(exc),
                remediation=exc.remediation,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._result(
                status="check_unavailable",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                detail=f"Stable release check is unavailable: {exc}",
                remediation="Retry after GitHub and an official registry are reachable.",
            )
        if origin.kind == "source":
            return self._result(
                status="transition_available",
                installed=None,
                installed_origin="source",
                source_commit_sha=origin.commit_sha,
                target=target,
                source=source,
                detail=(
                    "The installed source commit and published release have unknown semantic "
                    "ordering; an explicit transition to the verified official release is "
                    "available."
                ),
                remediation=(
                    f"Run `flow-steward release upgrade "
                    f"--target-version {target.version} --confirm`."
                ),
            )
        status: Literal["current", "upgrade_available"] = "upgrade_available"
        if installed is not None and ReleaseVersion.parse(target.version) <= ReleaseVersion.parse(
            installed.version
        ):
            status = "current"
        return self._result(
            status=status,
            installed=installed,
            target=target,
            source=source,
            remediation=(
                "No release action is required."
                if status == "current"
                else f"Run `flow-steward release upgrade "
                f"--target-version {target.version} --confirm`."
            ),
        )

    def prepare_upgrade(self, request: ReleaseUpgradeRequest) -> PreparedReleaseUpgrade:
        if self.workflow is not None:
            resumed_workflow = self.workflow.prepare_resume(self, request)
            if resumed_workflow is not None:
                return resumed_workflow
        resumed = self._resume_source_transition(request)
        if resumed is not None:
            return resumed
        origin = self._load_origin()
        if origin.kind == "source":
            self._require_source_repository(origin)
            target, source = self._select_source_transition(request)
            binding = ReleaseTransitionBinding(
                installed_origin=origin,
                configuration_sha256=origin.configuration_sha256,
                release_repository=origin.release_repository,
                deployment_profile=self.profile,
                platform=self.platform,
                target_release=target,
            )
            return PreparedReleaseUpgrade(
                installed_release=None,
                target_release=target,
                transition_binding=binding,
                release_source=source,
                deployment_profile=self.profile,
                platform=self.platform,
                dry_run=request.dry_run,
                skip_db_backup=request.skip_db_backup,
                timeout_seconds=request.timeout_seconds,
                target_modules=request.target_modules,
                restore_previous_modules=request.restore_previous_modules,
            )
        installed, target, source = self._select_upgrade(request)
        return PreparedReleaseUpgrade(
            installed_release=installed,
            target_release=target,
            release_source=source,
            deployment_profile=self.profile,
            platform=self.platform,
            dry_run=request.dry_run,
            skip_db_backup=request.skip_db_backup,
            timeout_seconds=request.timeout_seconds,
            target_modules=request.target_modules,
            restore_previous_modules=request.restore_previous_modules,
        )

    def review_upgrade(self, prepared: PreparedReleaseUpgrade) -> dict[str, object]:
        review = upgrade_review(prepared)
        if self.workflow is not None:
            review.update(self.workflow.review(prepared))
        if self.environment is not None:
            review["active_environment"] = f"{self.environment} (unchanged)"
        return review

    def plan_upgrade(self, request: ReleaseUpgradeRequest) -> dict[str, object]:
        """Compatibility wrapper for callers that only need a review document."""
        return self.review_upgrade(self.prepare_upgrade(request))

    def upgrade(
        self,
        request: ReleaseUpgradeRequest | PreparedReleaseUpgrade,
        **operation_sinks: object,
    ) -> ReleaseResult:
        if self.workflow is not None:
            prepared = (
                request
                if isinstance(request, PreparedReleaseUpgrade)
                else self.prepare_upgrade(request)
            )
            return self.workflow.run(self, prepared, **operation_sinks)
        return self._activate_upgrade(request, **operation_sinks)

    def _activate_upgrade(
        self,
        request: ReleaseUpgradeRequest | PreparedReleaseUpgrade,
        **operation_sinks: object,
    ) -> ReleaseResult:
        prepared = (
            request
            if isinstance(request, PreparedReleaseUpgrade)
            else self.prepare_upgrade(request)
        )
        target = prepared.target_release
        binding = prepared.transition_binding
        durable_transition = self._load_transition_handoff()
        durable_binding = self._load_transition_binding()
        if (
            binding is not None
            and durable_binding == binding
            and (
                durable_transition is None
                or (
                    getattr(durable_transition, "target", None) == target
                    and getattr(durable_transition, "phase", "") != "installer_completed"
                )
            )
        ):
            origin: InstalledReleaseOrigin = binding.installed_origin
        else:
            origin = self.origin.load()
        if binding is not None:
            self._require_source_repository(binding.installed_origin)
            if (
                origin.kind != "source"
                or origin != binding.installed_origin
                or target != binding.target_release
                or prepared.installed_release is not None
            ):
                raise self._upgrade_state_changed()
            installed = None
        else:
            if (
                origin.kind != "official"
                or prepared.installed_release is None
                or origin.release != prepared.installed_release
            ):
                raise self._upgrade_state_changed()
            installed = origin.release
        self._require_target_context(target)
        if installed is not None and ReleaseVersion.parse(target.version) < ReleaseVersion.parse(
            installed.version
        ):
            raise self._downgrade_refused(installed.version, target.version)
        source = prepared.release_source
        if installed is not None:
            self._refuse_profile_switch_leftover(installed)
        pending = self._effective_pending(installed) if installed is not None else None
        if pending is not None and pending != target:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {pending.version} is already pending.",
                remediation=(
                    f"Resume it with --target-version {pending.version} before selecting another."
                ),
                step="release_policy",
            )
        if installed is not None and target == installed:
            return self._result(
                status="already_current",
                installed=installed,
                target=target,
                source=source,
                maintenance=self.maintenance.status(),
                remediation="The requested release is already installed.",
            )
        if not prepared.dry_run and self.maintenance.status().status != "enabled":
            raise self._maintenance_required()
        if prepared.dry_run:
            return self._result(
                status="planned",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                target=target,
                source=source,
                maintenance=self.maintenance.status(),
                remediation="Rerun without --dry-run to activate this exact release.",
                dry_run=True,
            )
        if binding is not None:
            operation_sinks["transition_binding"] = binding
        result = self.activator.activate(
            target, timeout_seconds=prepared.timeout_seconds, **operation_sinks
        )
        next_steps = result.next_steps
        if not next_steps and result.status == "migration_required":
            next_steps = (
                _BACKUP_CREATE_COMMAND,
                _DATABASE_PREFLIGHT_COMMAND,
                _DATABASE_UPGRADE_COMMAND,
                f"flow-steward release upgrade --target-version {target.version} --confirm",
            )
        elif not next_steps and result.status == "upgraded":
            next_steps = (_MAINTENANCE_DISABLE_COMMAND,)
        current = target if result.status == "upgraded" else installed
        return result.model_copy(
            update={
                "current_release": result.current_release or current,
                "current_version": result.current_version or (current.version if current else ""),
                "installed_origin": ("official" if result.status == "upgraded" else origin.kind),
                "source_commit_sha": (
                    origin.commit_sha
                    if result.status != "upgraded" and origin.kind == "source"
                    else ""
                ),
                "target_release": result.target_release or target,
                "target_version": result.target_version or target.version,
                "release_source": result.release_source or source,
                "deployment_profile": result.deployment_profile or self.profile,
                "platform": result.platform or self.platform,
                "maintenance_status": result.maintenance_status or self.maintenance.status(),
                "next_steps": next_steps,
            }
        )

    def _select_source_transition(
        self, request: ReleaseUpgradeRequest
    ) -> tuple[ReleaseIdentity, str]:
        if self.workflow is None and self.maintenance.status().status != "enabled":
            raise self._maintenance_required()
        try:
            published = (
                self.discovery.for_version(request.target_version)
                if request.target_version
                else self.discovery.latest()
            )
        except ReleaseDiscoveryError as exc:
            factory = LifecycleError.dependency if exc.retryable else LifecycleError.stable_failure
            raise factory(
                code=exc.code,
                cause=str(exc),
                remediation=exc.remediation,
                step="release_discovery",
            ) from exc
        return self.selector.resolve(published), published.source

    def _resume_source_transition(
        self, request: ReleaseUpgradeRequest
    ) -> PreparedReleaseUpgrade | None:
        journal = self._load_transition_handoff()
        binding = self._load_transition_binding()
        journal_target = getattr(journal, "target", None)
        phase = getattr(journal, "phase", "")
        if not isinstance(binding, ReleaseTransitionBinding) or phase == "installer_completed":
            return None
        target = binding.target_release
        if journal_target is not None and journal_target != target:
            raise self._upgrade_state_changed()
        self._require_source_repository(binding.installed_origin)
        requested = request.target_version
        if requested and requested != target.version:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {target.version} is already pending.",
                remediation=(
                    f"Resume it with --target-version {target.version} before selecting another."
                ),
                step="release_policy",
            )
        verified = self.selector.verify(target)
        if verified != target:
            raise self._upgrade_state_changed()
        return PreparedReleaseUpgrade(
            installed_release=None,
            target_release=target,
            transition_binding=binding,
            release_source="durable source transition handoff",
            deployment_profile=binding.deployment_profile,
            platform=binding.platform,
            dry_run=request.dry_run,
            skip_db_backup=request.skip_db_backup,
            timeout_seconds=request.timeout_seconds,
            target_modules=request.target_modules,
            restore_previous_modules=request.restore_previous_modules,
        )

    def _load_transition_handoff(self) -> object | None:
        load = getattr(self.state, "load_release_handoff", None)
        return load() if callable(load) else None

    def _load_transition_binding(self) -> object | None:
        load = getattr(self.state, "load_release_transition_binding", None)
        return load() if callable(load) else None

    def _require_source_repository(self, origin: SourceInstallationOrigin) -> None:
        if origin.release_repository == self.selector.release_repository:
            return
        raise LifecycleError.stable_failure(
            code="release_source_repository_mismatch",
            cause=(
                "The configured release repository differs from the repository bound to "
                "the installed source origin."
            ),
            remediation=(
                "Restore FS_RELEASE_REPOSITORY to the installed durable value, then rerun "
                "release check and review the transition again."
            ),
            step="release_policy",
        )

    def _select_upgrade(
        self, request: ReleaseUpgradeRequest
    ) -> tuple[ReleaseIdentity, ReleaseIdentity, str]:
        installed = self._require_installed()
        self._refuse_profile_switch_leftover(installed)
        pending = self._effective_pending(installed)
        requested = request.target_version
        if pending is not None and (not requested or requested == pending.version):
            target = self.selector.verify(pending)
            source = "durable pending release identity"
        else:
            try:
                published = (
                    self.discovery.for_version(requested) if requested else self.discovery.latest()
                )
            except ReleaseDiscoveryError as exc:
                factory = (
                    LifecycleError.dependency if exc.retryable else LifecycleError.stable_failure
                )
                raise factory(
                    code=exc.code,
                    cause=str(exc),
                    remediation=exc.remediation,
                    step="release_discovery",
                ) from exc
            target_version = published.version
            if target_version < ReleaseVersion.parse(installed.version):
                raise self._downgrade_refused(installed.version, str(target_version))
            if str(target_version) == installed.version and pending is None:
                return installed, installed, published.source
            if pending is not None and pending.version != str(target_version):
                raise LifecycleError.stable_failure(
                    code="release_upgrade_pending",
                    cause=f"Release {pending.version} is already pending.",
                    remediation=(
                        f"Resume it with --target-version {pending.version} before selecting another."
                    ),
                    step="release_policy",
                )
            if self.workflow is None and self.maintenance.status().status != "enabled":
                raise self._maintenance_required()
            target = self.selector.resolve(published)
            source = published.source
        if (
            self.workflow is None
            and target.version != installed.version
            and self.maintenance.status().status != "enabled"
        ):
            raise self._maintenance_required()
        if ReleaseVersion.parse(target.version) < ReleaseVersion.parse(installed.version):
            raise self._downgrade_refused(installed.version, target.version)
        if target.profile != self.profile or target.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_target_context_mismatch",
                cause=_TARGET_CONTEXT_MISMATCH_CAUSE,
                remediation=_TARGET_CONTEXT_MISMATCH_REMEDIATION,
                step="release_policy",
            )
        return installed, target, source

    def _effective_pending(self, installed: ReleaseIdentity) -> ReleaseIdentity | None:
        """Treat a crash-stale marker for the installed identity as already committed."""
        return self.selector.effective_pending(installed)

    def _require_installed(self) -> ReleaseIdentity:
        installed = self.state.load_installed_release()
        if installed is None:
            raise LifecycleError.stable_failure(
                code="release_installed_state_missing",
                cause="No successfully applied release identity is recorded.",
                remediation="Complete an official installation before using release commands.",
                step="release_state",
            )
        if installed.profile != self.profile or installed.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_installed_context_mismatch",
                cause="Installed release identity does not match the active profile and platform.",
                remediation=_INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
                step="release_state",
            )
        return installed

    def _load_origin(self) -> InstalledReleaseOrigin:
        origin = self.origin.load()
        if origin.kind == "official":
            installed = origin.release
            if installed.profile != self.profile or installed.platform != self.platform:
                raise LifecycleError.stable_failure(
                    code="release_installed_context_mismatch",
                    cause=(
                        "Installed release identity does not match the active profile and platform."
                    ),
                    remediation=_INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
                    step="release_state",
                )
            return origin
        if origin.deployment_profile != self.profile or origin.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_installed_context_mismatch",
                cause="Installed source origin does not match the active profile and platform.",
                remediation="Repair the active installation configuration or source provenance.",
                step="release_state",
            )
        return origin

    def _require_target_context(self, target: ReleaseIdentity) -> None:
        if target.profile != self.profile or target.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_target_context_mismatch",
                cause=_TARGET_CONTEXT_MISMATCH_CAUSE,
                remediation=_TARGET_CONTEXT_MISMATCH_REMEDIATION,
                step="release_policy",
            )

    def _result(
        self,
        *,
        status: Any,
        installed: ReleaseIdentity | None,
        target: ReleaseIdentity | None = None,
        source: str = "",
        detail: str = "",
        remediation: str = "",
        maintenance: MaintenanceStatusResult | None = None,
        dry_run: bool = False,
        installed_origin: Literal["official", "source"] | None = None,
        source_commit_sha: str = "",
    ) -> ReleaseResult:
        return ReleaseResult(
            status=status,
            active_environment=self.environment,
            installed_origin=installed_origin or ("official" if installed else None),
            source_commit_sha=source_commit_sha,
            deployment_profile=self.profile,
            platform=self.platform,
            current_version=installed.version if installed else "",
            current_release=installed,
            target_version=target.version if target else "",
            target_release=target,
            release_source=source,
            detail=detail,
            remediation=remediation,
            maintenance_status=maintenance,
            dry_run=dry_run,
            next_steps=(_MAINTENANCE_DISABLE_COMMAND,) if maintenance else (),
        )

    @staticmethod
    def _maintenance_required() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_maintenance_required",
            cause="Release upgrade requires maintenance to be fully enabled.",
            remediation=f"Run `{_MAINTENANCE_ENABLE_COMMAND}`, then repeat the upgrade.",
            step="maintenance",
        )

    @staticmethod
    def _downgrade_refused(installed: str, target: str) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_downgrade_refused",
            cause=(
                f"Public release upgrade cannot move from {installed} to older release {target}."
            ),
            remediation="Use clone restore for a validated destructive older-version restore.",
            step="release_policy",
        )

    def _refuse_profile_switch_leftover(self, installed: ReleaseIdentity) -> None:
        """Name an interrupted profile switch instead of calling it a pending upgrade.

        Its staged release is the installed one on the other profile, so "resume it
        with --target-version" only failed on the context mismatch, in a circle.
        """
        leftover = self.selector.profile_switch_leftover(installed)
        if leftover is None:
            return
        raise LifecycleError.stable_failure(
            code="profile_switch_unfinished",
            cause=(
                f"A switch to the {leftover.profile} profile did not finish: release "
                f"{leftover.version} is staged for {leftover.profile} while "
                f"{installed.profile} is installed."
            ),
            remediation=(
                f"Run `./install.sh --deployment-profile {leftover.profile}` to finish the "
                "switch, then repeat this command."
            ),
            step="release_policy",
        )

    @staticmethod
    def _upgrade_state_changed() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_upgrade_state_changed",
            cause="Installed origin or the exact target changed after upgrade review.",
            remediation="Run the release upgrade command again and review the new plan.",
            step="release_policy",
        )


__all__ = [
    "PreparedReleaseUpgrade",
    "ReleaseActivatorPort",
    "ReleaseCheckRequest",
    "ReleaseInfoRequest",
    "ReleaseInstallRequest",
    "ReleaseResult",
    "ReleaseService",
    "ReleaseTransitionBinding",
    "ReleaseUpgradeRequest",
    "release_identity_from_resolution",
    "release_platform_for_machine",
]
