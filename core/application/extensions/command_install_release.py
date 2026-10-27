from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from core.application.management.command_models import (
    ExtensionInstallResultView,
    ExtensionReleaseInstallInput,
)
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext

from .browser_authority_approval import (
    PreparedBrowserAuthorityApproval,
    with_browser_authority_approval,
)
from .command_context import ExtensionCommandContext
from .extension_runtime import EntitlementStatus
from .health_gate import require_extension_health_ready
from .install_browser_authority import prepare_install_browser_authority
from .install_release_models import (
    DependencyValidator,
    ExtensionInstallLifecyclePort,
    HandshakeRunner,
    PreparedInstall,
    PreviousInstallState,
    ProjectionPublisher,
    RemoteUrlValidator,
)
from .lifecycle_ports import (
    require_global_extension_lifecycle_scope as _require_global_extension_lifecycle_scope,
)
from .oauth_release_upgrade_integration import prepare_oauth_release_transition
from .semver import normalize_semver
from .service_support import (
    _normalize_text,
    _release_visible_to_account,
    _service_scope,
    _validate_install_version_transition,
)
from .update_failure_reporting import _CompensationFailureCapture

_ALLOWED_ENTITLEMENT_STATUSES = tuple(status.value for status in EntitlementStatus)
_ALLOWED_ENTITLEMENT_STATUS_SET = frozenset(_ALLOWED_ENTITLEMENT_STATUSES)


class ExtensionReleaseInstallWorkflow:
    """Coordinate atomic installation of a validated extension release."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        lifecycle: Callable[[], ExtensionInstallLifecyclePort],
        validate_dependencies: DependencyValidator,
        run_handshake: HandshakeRunner,
        project_release: ProjectionPublisher,
        materialize_bridges: ProjectionPublisher,
        validate_remote_url: RemoteUrlValidator,
    ) -> None:
        self._context = context
        self._lifecycle = lifecycle
        self._validate_dependencies = validate_dependencies
        self._run_handshake = run_handshake
        self._project_release = project_release
        self._materialize_bridges = materialize_bridges
        self._validate_remote_url = validate_remote_url

    def install(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView:
        prepared = self._prepare(
            inp,
            scope=scope,
            allow_same_version_enrichment=allow_same_version_enrichment,
            _browser_authority_handoff=_browser_authority_handoff,
        )
        try:
            install_record = self._persist_install(prepared)
            self._publish_install(prepared, install_record=install_record)
        except BaseException as exc:
            compensation_failures: list[BaseException] = []
            with _CompensationFailureCapture(
                compensation_failures,
                "Install release compensation failed after the mutation aborted.",
            ):
                self._rollback(prepared, install_record=locals().get("install_record"))
            if compensation_failures:
                raise BaseExceptionGroup(
                    "install release mutation and compensation failed",
                    [exc, *compensation_failures],
                ) from None
            raise
        return self._result(prepared, install_record=install_record)

    def _prepare(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None,
        allow_same_version_enrichment: bool,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None,
    ) -> PreparedInstall:
        command = ExtensionReleaseInstallInput.model_validate(inp)
        normalized_scope = _service_scope(scope)
        _require_global_extension_lifecycle_scope(normalized_scope)
        account_id = normalized_scope.account_id or "acc_default"
        install_store = self._context.install_store()
        lock_release_membership = getattr(install_store, "lock_release_membership", None)
        if callable(lock_release_membership):
            lock_release_membership(command.release_id)
        runtime_store = self._context.runtime_store()
        release = self._load_release(
            runtime_store,
            release_id=command.release_id,
            account_id=account_id,
        )
        runtime_mode, remote_base_url = self._resolve_runtime(command, release=release)
        existing = self._load_existing(
            install_store,
            release=release,
            account_id=account_id,
            allow_same_version_enrichment=allow_same_version_enrichment,
        )
        warnings = self._validate_dependencies(
            release=release,
            account_id=account_id,
            runtime_store=runtime_store,
            install_store=install_store,
        )
        if runtime_mode != "remote_https":
            require_extension_health_ready(dict(release.descriptor or {}))
        previous = self._snapshot_previous(existing, runtime_store=runtime_store)
        browser_impact, browser_approval = prepare_install_browser_authority(
            command=command,
            scope=normalized_scope,
            release=release,
            existing=existing,
            handoff=_browser_authority_handoff,
        )
        oauth_transition = prepare_oauth_release_transition(
            context=self._context,
            command=command,
            existing=existing,
            previous_release=previous.release,
            candidate_release=release,
        )
        return PreparedInstall(
            command=command,
            scope=normalized_scope,
            account_id=account_id,
            actor_id=normalized_scope.actor_id or "api",
            runtime_store=runtime_store,
            install_store=install_store,
            release=release,
            existing=existing,
            runtime_mode=runtime_mode,
            remote_base_url=remote_base_url,
            dependency_warnings=warnings,
            previous=previous,
            oauth_transition=oauth_transition,
            browser_authority_impact=browser_impact,
            browser_authority_approval=browser_approval,
        )

    @staticmethod
    def _load_release(runtime_store: Any, *, release_id: str, account_id: str) -> Any:
        release = (
            runtime_store.get_release(release_id) if hasattr(runtime_store, "get_release") else None
        )
        if not release or not _release_visible_to_account(
            release,
            account_id=account_id,
            allow_marketplace_public=True,
        ):
            raise ServiceNotFoundError(f"Release '{release_id}' not found")
        return release

    def _resolve_runtime(
        self,
        command: ExtensionReleaseInstallInput,
        *,
        release: Any,
    ) -> tuple[str, str]:
        descriptor = dict(release.descriptor or {})
        distribution = descriptor.get("distribution") if isinstance(descriptor, dict) else {}
        raw_modes = distribution.get("execution_modes") if isinstance(distribution, dict) else []
        execution_modes = [
            _normalize_text(mode).lower()
            for mode in (raw_modes if isinstance(raw_modes, list) else [])
            if _normalize_text(mode)
        ]
        runtime_mode = _normalize_text(command.runtime_mode).lower()
        runtime_mode = runtime_mode or (execution_modes[0] if execution_modes else "")
        if execution_modes and runtime_mode not in execution_modes:
            raise ServiceValidationError(
                f"runtime_mode '{runtime_mode}' is not supported by release execution_modes {execution_modes}"
            )
        if not runtime_mode:
            raise ServiceValidationError("runtime_mode cannot be resolved for this release")
        remote_base_url = self._remote_base_url(command, descriptor=descriptor)
        if runtime_mode == "remote_https":
            if not remote_base_url:
                raise ServiceValidationError("remote_base_url is required for remote_https install")
            try:
                remote_base_url = self._validate_remote_url(
                    remote_base_url,
                    purpose="remote_base_url",
                )
            except Exception as exc:
                raise ServiceValidationError(f"remote_base_url is not allowed: {exc}") from exc
        return runtime_mode, remote_base_url

    @staticmethod
    def _remote_base_url(
        command: ExtensionReleaseInstallInput,
        *,
        descriptor: dict[str, Any],
    ) -> str:
        runtime_block = descriptor.get("runtime") if isinstance(descriptor, dict) else {}
        remote_block = runtime_block.get("remote") if isinstance(runtime_block, dict) else {}
        if not isinstance(remote_block, dict):
            remote_block = {}
        return _normalize_text(command.remote_base_url) or _normalize_text(
            remote_block.get("base_url")
        )

    @staticmethod
    def _load_existing(
        install_store: Any,
        *,
        release: Any,
        account_id: str,
        allow_same_version_enrichment: bool,
    ) -> Any | None:
        existing = (
            install_store.get(extension_id=release.extension_id, account_id=account_id)
            if hasattr(install_store, "get")
            else None
        )
        same_version_enrichment = bool(
            allow_same_version_enrichment
            and existing is not None
            and _normalize_text(getattr(existing, "descriptor_source", "")) == "extension_scan"
            and _normalize_text(getattr(release, "source_kind", "")) == "extension_scan"
            and normalize_semver(
                _normalize_text(getattr(existing, "version", "")),
                "current_version",
            )
            == normalize_semver(release.release_version, "candidate_version")
        )
        if existing is not None and not same_version_enrichment:
            _validate_install_version_transition(
                extension_id=release.extension_id,
                current_version=_normalize_text(getattr(existing, "version", "")),
                candidate_version=release.release_version,
            )
        return existing

    def _snapshot_previous(
        self,
        existing: Any | None,
        *,
        runtime_store: Any,
    ) -> PreviousInstallState:
        previous_release = None
        previous_entitlement = None
        previous_projections: list[Any] = []
        if existing is not None and hasattr(runtime_store, "get_release"):
            previous_release = runtime_store.get_release(
                _normalize_text(getattr(existing, "release_id", ""))
            )
        if existing is not None and hasattr(runtime_store, "get_entitlement"):
            previous_entitlement = runtime_store.get_entitlement(existing.install_id)
        if existing is not None and hasattr(runtime_store, "list_projections"):
            previous_projections = [
                row
                for row in runtime_store.list_projections(
                    account_id=existing.account_id,
                    only_active=False,
                )
                if _normalize_text(getattr(row, "install_id", ""))
                == _normalize_text(getattr(existing, "install_id", ""))
            ]
        return PreviousInstallState(
            payload=self._lifecycle().install_record_payload(existing),
            release=previous_release,
            entitlement=previous_entitlement,
            projections=previous_projections,
        )

    def _persist_install(self, prepared: PreparedInstall) -> Any:
        release = prepared.release
        existing = prepared.existing
        descriptor_origin = _normalize_text((release.provenance or {}).get("origin")) or "imported"
        runtime_identity_changed = bool(
            existing is not None
            and (
                _normalize_text(getattr(existing, "release_id", "")) != release.release_id
                or _normalize_text(getattr(existing, "runtime_mode", "")).lower()
                != prepared.runtime_mode
                or _normalize_text(getattr(existing, "remote_base_url", ""))
                != prepared.remote_base_url
            )
        )
        install_record = prepared.install_store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=prepared.account_id,
            visibility=(_normalize_text(getattr(existing, "visibility", "")) or "account"),
            installed_by=prepared.actor_id,
            install_id=_normalize_text(getattr(existing, "install_id", "")),
            release_id=release.release_id,
            descriptor_schema_version=release.descriptor_schema_version,
            descriptor_origin=descriptor_origin,
            descriptor_source=release.source_kind,
            provenance=with_browser_authority_approval(
                release.provenance,
                approval=prepared.browser_authority_approval,
            ),
            runtime_mode=prepared.runtime_mode,
            remote_base_url=prepared.remote_base_url,
            handshake_status="unknown",
            health_status="unknown",
            runtime_status="unknown",
            applied_data_schema_version=int(
                getattr(existing, "applied_data_schema_version", 1) or 1
            ),
            config=dict(getattr(existing, "config", {}) or {}),
        )
        if prepared.oauth_transition is not None:
            prepared.oauth_transition.apply()
        logger_connection = getattr(self._context.logger(), "conn", None)
        if runtime_identity_changed and logger_connection is not None:
            from core.application.settings import llm_provider_extension_repository as llm_store

            llm_store.mark_install_models_stale(
                logger_connection,
                account_id=prepared.account_id,
                extension_install_id=install_record.install_id,
            )
            llm_store.set_install_provider_state_revision(
                logger_connection,
                account_id=prepared.account_id,
                extension_install_id=install_record.install_id,
                revision=uuid.uuid4().hex,
            )
        self._upsert_entitlement(prepared, install_record=install_record)
        return install_record

    @staticmethod
    def _upsert_entitlement(prepared: PreparedInstall, *, install_record: Any) -> None:
        if not hasattr(prepared.runtime_store, "upsert_entitlement"):
            return
        status = (
            _normalize_text(prepared.command.entitlement_status).lower()
            or EntitlementStatus.trial_active.value
        )
        if status not in _ALLOWED_ENTITLEMENT_STATUS_SET:
            raise ServiceValidationError(
                f"Unsupported entitlement_status '{prepared.command.entitlement_status}'",
                payload={"allowed": list(_ALLOWED_ENTITLEMENT_STATUSES)},
            )
        prepared.runtime_store.upsert_entitlement(
            install_id=install_record.install_id,
            account_id=prepared.account_id,
            status=status,
            updated_by=prepared.actor_id,
        )

    def _publish_install(self, prepared: PreparedInstall, *, install_record: Any) -> None:
        self._run_handshake(install_id=install_record.install_id, scope=prepared.scope)
        self._project_release(
            install_record=install_record,
            release=prepared.release,
            account_id=prepared.account_id,
        )
        self._lifecycle().remove_obsolete_install_projections(
            runtime_store=prepared.runtime_store,
            account_id=prepared.account_id,
            install_id=install_record.install_id,
            release_id=prepared.release.release_id,
        )
        self._materialize_bridges(
            install_record=install_record,
            release=prepared.release,
            account_id=prepared.account_id,
        )

    def _rollback(self, prepared: PreparedInstall, *, install_record: Any | None) -> None:
        transaction_connection = self._context.persistence_connection()
        if transaction_connection is not None and all(
            callable(participates := getattr(store, "participates_in_transaction", None))
            and bool(participates(transaction_connection))
            for store in (prepared.install_store, prepared.runtime_store)
        ):
            return
        self._lifecycle().rollback_install_release_failure(
            install_store=prepared.install_store,
            runtime_store=prepared.runtime_store,
            extension_id=prepared.release.extension_id,
            account_id=prepared.account_id,
            failed_install_id=_normalize_text(
                getattr(install_record, "install_id", "")
                or getattr(prepared.existing, "install_id", "")
            ),
            previous=prepared.previous.payload,
            previous_release=prepared.previous.release,
            previous_entitlement=prepared.previous.entitlement,
            previous_projections=prepared.previous.projections,
        )

    def _cleanup_previous_release(self, prepared: PreparedInstall) -> None:
        previous_release_id = _normalize_text(getattr(prepared.existing, "release_id", ""))
        if not previous_release_id or previous_release_id == prepared.release.release_id:
            return
        self._lifecycle().cleanup_release_if_unreferenced(
            install_store=prepared.install_store,
            release_id=previous_release_id,
            extension_id=prepared.release.extension_id,
        )

    @staticmethod
    def _result(prepared: PreparedInstall, *, install_record: Any) -> ExtensionInstallResultView:
        refreshed = (
            prepared.install_store.get_by_install_id(install_record.install_id)
            if hasattr(prepared.install_store, "get_by_install_id")
            else install_record
        )
        return ExtensionInstallResultView(
            status="installed",
            extension_id=refreshed.extension_id,
            version=refreshed.version,
            account_id=refreshed.account_id,
            install_id=refreshed.install_id,
            release_id=refreshed.release_id,
            dependency_warnings=prepared.dependency_warnings,
        )
