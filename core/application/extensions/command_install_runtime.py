from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.application.extensions.host_contract import HOST_CONTRACT_VERSION
from core.application.management.command_models import (
    ExtensionRuntimeHealthResultView,
    ExtensionRuntimeTokenInput,
    ExtensionRuntimeTokenResultView,
)
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext

from .command_context import ExtensionCommandContext
from .lifecycle_ports import require_role as _require_role
from .service_support import (
    _normalize_text,
    _platform_compatibility_error,
    _runtime_platform_version,
    _service_scope,
    _sign_runtime_token,
    _token_secret,
)

HttpProbe = Callable[[str], tuple[bool, str]]
RuntimeConnectionLister = Callable[..., list[dict[str, Any]]]


@dataclass
class _RuntimeState:
    runtime_status: str
    handshake_status: str
    health_status: str
    last_error: str = ""


def _release_incompatibility(release: Any, descriptor: dict[str, Any]) -> str:
    """Why this host cannot run the release: another host contract or platform."""
    contract_version = _normalize_text(getattr(release, "contract_version", ""))
    if contract_version != HOST_CONTRACT_VERSION:
        return f"Unsupported runtime contract '{contract_version}'"
    return _platform_compatibility_error(descriptor, platform_version=_runtime_platform_version())


class ExtensionInstallRuntimeService:
    """Own extension runtime checks and runtime bearer issuance."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        http_probe: HttpProbe,
        list_runtime_connections: RuntimeConnectionLister,
    ) -> None:
        self._context = context
        self._http_probe = http_probe
        self._list_runtime_connections = list_runtime_connections

    def run_handshake(
        self,
        *,
        install_id: str,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeHealthResultView:
        scope = _service_scope(scope)
        account_id = scope.account_id or "acc_default"
        install_store = self._context.install_store()
        install_record = self._load_install(
            install_store,
            install_id=install_id,
            account_id=account_id,
        )
        from core.application.settings import llm_provider_extension_repository as llm_store

        logger_connection = getattr(self._context.logger(), "conn", None)
        runtime_snapshot = (
            llm_store.get_install_runtime_credential_snapshot(
                logger_connection,
                account_id=account_id,
                extension_install_id=install_record.install_id,
            )
            if logger_connection is not None
            else {}
        )
        if not isinstance(runtime_snapshot, dict):
            runtime_snapshot = {}
        runtime_store = self._context.runtime_store()
        release_id = _normalize_text(runtime_snapshot.get("release_id")) or _normalize_text(
            install_record.release_id
        )
        release = (
            runtime_store.get_release(release_id)
            if release_id and hasattr(runtime_store, "get_release")
            else None
        )
        descriptor = dict(getattr(release, "descriptor", {}) or {}) if release else {}
        state = self._initial_runtime_state(install_record, release, descriptor=descriptor)
        runtime_mode = (
            _normalize_text(runtime_snapshot.get("runtime_mode"))
            or _normalize_text(getattr(install_record, "runtime_mode", ""))
        ).lower()
        remote_base_url = _normalize_text(
            runtime_snapshot.get("remote_base_url")
        ) or _normalize_text(getattr(install_record, "remote_base_url", ""))
        if runtime_mode == "remote_https" and state.runtime_status == "incompatible":
            # A release that is already incompatible is never called "to try".
            state.handshake_status = "failed"
        elif runtime_mode == "remote_https":
            state = self._probe_remote_runtime(
                install_record,
                descriptor=descriptor,
                state=state,
                account_id=account_id,
                runtime_snapshot=runtime_snapshot,
                remote_base_url=remote_base_url,
            )
        install_record = self._persist_runtime_state(
            install_store,
            install_record=install_record,
            runtime_mode=runtime_mode,
            remote_base_url=remote_base_url,
            state=state,
        )
        return self._health_view(install_record, state=state)

    @staticmethod
    def _load_install(install_store: Any, *, install_id: str, account_id: str) -> Any:
        install_record = (
            install_store.get_by_install_id(install_id)
            if hasattr(install_store, "get_by_install_id")
            else None
        )
        if not install_record:
            raise ServiceNotFoundError(f"Install '{install_id}' not found")
        if _normalize_text(install_record.account_id) != _normalize_text(account_id):
            raise ServiceNotFoundError(f"Install '{install_id}' not found")
        return install_record

    @staticmethod
    def _initial_runtime_state(
        install_record: Any,
        release: Any | None,
        *,
        descriptor: dict[str, Any],
    ) -> _RuntimeState:
        state = _RuntimeState(
            runtime_status="unknown",
            handshake_status=(
                _normalize_text(getattr(install_record, "handshake_status", "")) or "unknown"
            ),
            health_status=(
                _normalize_text(getattr(install_record, "health_status", "")) or "unknown"
            ),
        )
        if release is None:
            return state
        error = _release_incompatibility(release, descriptor)
        if error:
            state.runtime_status = "incompatible"
            state.last_error = error
        else:
            state.runtime_status = "compatible"
        return state

    def _probe_remote_runtime(
        self,
        install_record: Any,
        *,
        descriptor: dict[str, Any],
        state: _RuntimeState,
        account_id: str,
        runtime_snapshot: dict[str, Any],
        remote_base_url: str,
    ) -> _RuntimeState:
        runtime_block = descriptor.get("runtime") if isinstance(descriptor, dict) else {}
        remote_block = runtime_block.get("remote") if isinstance(runtime_block, dict) else {}
        if not isinstance(remote_block, dict):
            remote_block = {}
        endpoints = remote_block.get("endpoints") if isinstance(remote_block, dict) else {}
        base_url = remote_base_url or _normalize_text(remote_block.get("base_url"))
        handshake_path = _normalize_text((endpoints or {}).get("handshake"))
        health_path = _normalize_text((endpoints or {}).get("health"))
        if not base_url:
            state.handshake_status = state.health_status = "failed"
            state.last_error = "remote_base_url is not configured"
            return state
        if not handshake_path or not health_path:
            state.handshake_status = state.health_status = "failed"
            state.last_error = "remote handshake/health endpoint templates are missing"
            return state
        from core.infrastructure.db.remote_extension_runtime import remote_handshake, remote_health

        runtime_extension: dict[str, Any] = {
            "runtime_mode": "remote_https",
            "remote_base_url": base_url,
            "runtime": {"remote": {"endpoints": endpoints}},
        }
        bearer = self._runtime_bearer(
            install_record=install_record,
            account_id=account_id,
            runtime_snapshot=runtime_snapshot,
        )
        if bearer:
            runtime_extension["remote_bearer_token"] = bearer
        handshake = remote_handshake(
            runtime_extension,
            payload={"install_id": install_record.install_id},
        )
        state.handshake_status = "ok" if handshake.get("status") == "succeeded" else "failed"
        if handshake.get("contract_compatible") is False:
            # The runtime answered but did not confirm this host's contract. A
            # health check proves nothing about compatibility, so none is made.
            state.runtime_status = "incompatible"
            state.last_error = str(handshake.get("contract_error") or "").strip() or (
                f"Remote runtime did not confirm contract '{HOST_CONTRACT_VERSION}'"
            )
            return state
        if state.handshake_status != "ok":
            state.last_error = f"Handshake failed: {handshake.get('detail') or 'remote runtime rejected handshake'}"
        health = remote_health(runtime_extension)
        state.health_status = "ok" if health.get("status") == "succeeded" else "failed"
        if state.health_status != "ok" and not state.last_error:
            state.last_error = (
                f"Health check failed: {health.get('detail') or 'remote runtime health failed'}"
            )
        return state

    def _runtime_bearer(
        self,
        *,
        install_record: Any,
        account_id: str,
        runtime_snapshot: dict[str, Any],
    ) -> str:
        try:
            from core.application.auth.secrets import decrypt_secret
            from core.application.settings import llm_provider_extension_repository as llm_store
            from core.application.settings.llm_provider_extension_service import credential_ref_key

            logger_connection = getattr(self._context.logger(), "conn", None)
            if runtime_snapshot:
                ref_key = _normalize_text(runtime_snapshot.get("credential_secret_ref"))
                ciphertext = str(runtime_snapshot.get("encrypted_value") or "")
            elif logger_connection is not None:
                ref_key, ciphertext = llm_store.get_install_current_credential_snapshot(
                    logger_connection,
                    account_id=account_id,
                    extension_install_id=install_record.install_id,
                )
            else:
                ref_key, ciphertext = "", ""
            if not ref_key and not runtime_snapshot and logger_connection is not None:
                ref_key = credential_ref_key(install_record.install_id)
                ciphertext = llm_store.read_secret_ref_ciphertext(
                    logger_connection,
                    account_id=account_id,
                    ref_key=ref_key,
                )
            if ciphertext:
                credentials = json.loads(decrypt_secret(ciphertext))
                return str(
                    credentials.get("runtime_bearer_token", "")
                    if isinstance(credentials, dict)
                    else ""
                ).strip()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _persist_runtime_state(
        install_store: Any,
        *,
        install_record: Any,
        runtime_mode: str,
        remote_base_url: str,
        state: _RuntimeState,
    ) -> Any:
        if not hasattr(install_store, "set_runtime_state"):
            return install_record
        updated = install_store.set_runtime_state(
            install_id=install_record.install_id,
            runtime_mode=runtime_mode or None,
            remote_base_url=remote_base_url or None,
            handshake_status=state.handshake_status,
            health_status=state.health_status,
            runtime_status=state.runtime_status,
            last_error=state.last_error,
        )
        return updated if updated is not None else install_record

    @staticmethod
    def _health_view(
        install_record: Any,
        *,
        state: _RuntimeState,
    ) -> ExtensionRuntimeHealthResultView:
        return ExtensionRuntimeHealthResultView(
            status="ok",
            install_id=install_record.install_id,
            handshake_status=(
                _normalize_text(getattr(install_record, "handshake_status", state.handshake_status))
                or state.handshake_status
            ),
            health_status=(
                _normalize_text(getattr(install_record, "health_status", state.health_status))
                or state.health_status
            ),
            runtime_status=(
                _normalize_text(getattr(install_record, "runtime_status", state.runtime_status))
                or state.runtime_status
            ),
            last_error=(
                _normalize_text(getattr(install_record, "last_error", state.last_error))
                or state.last_error
            ),
        )

    def issue_token(
        self,
        inp: ExtensionRuntimeTokenInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeTokenResultView:
        command = ExtensionRuntimeTokenInput.model_validate(inp)
        scope = _service_scope(scope)
        _require_role(scope, "admin")
        account_id = scope.account_id or "acc_default"
        install = self._load_install(
            self._context.install_store(),
            install_id=command.install_id,
            account_id=account_id,
        )
        self._require_project_access(command.project_id, scope=scope)
        denial_reason = self.token_denial_reason(
            install=install,
            account_id=account_id,
            connection_id=command.connection_id,
            project_id=command.project_id,
        )
        if denial_reason:
            return self._denied_token(command, install=install, reason=denial_reason)
        secret = _token_secret()
        if not secret:
            return self._denied_token(
                command,
                install=install,
                reason="Runtime token signing secret is not configured",
            )
        return self._mint_token(command, install=install, account_id=account_id, secret=secret)

    def _require_project_access(self, project_id: str | None, *, scope: ScopeContext) -> None:
        if project_id is None:
            return
        logger = self._context.logger()
        project = logger.get_project(project_id) if hasattr(logger, "get_project") else None
        if not isinstance(project, dict) or not project:
            raise ServiceNotFoundError(f"Project '{project_id}' not found")
        from core.application.management.guards import require_project_access

        require_project_access(scope, project)

    @staticmethod
    def _denied_token(
        command: ExtensionRuntimeTokenInput,
        *,
        install: Any,
        reason: str,
    ) -> ExtensionRuntimeTokenResultView:
        return ExtensionRuntimeTokenResultView(
            issued=False,
            install_id=install.install_id,
            ttl_seconds=max(0, int(command.ttl_seconds or 0)),
            reason=reason,
        )

    @staticmethod
    def _mint_token(
        command: ExtensionRuntimeTokenInput,
        *,
        install: Any,
        account_id: str,
        secret: str,
    ) -> ExtensionRuntimeTokenResultView:
        now = int(time.time())
        ttl_seconds = max(60, min(int(command.ttl_seconds or 3600), 3600))
        expires_at = now + ttl_seconds
        payload = {
            "iss": "flow-steward-extension-runtime",
            "sub": install.install_id,
            "aud": "extension-runtime",
            "account_id": account_id,
            "extension_id": install.extension_id,
            "connection_id": _normalize_text(command.connection_id),
            "project_id": command.project_id,
            "iat": now,
            "exp": expires_at,
        }
        return ExtensionRuntimeTokenResultView(
            issued=True,
            token=_sign_runtime_token(payload, secret=secret),
            token_type="Bearer",
            install_id=install.install_id,
            expires_at=float(expires_at),
            ttl_seconds=ttl_seconds,
            reason="",
        )

    def token_denial_reason(
        self,
        *,
        install: Any,
        account_id: str,
        connection_id: str,
        project_id: str | None,
    ) -> str:
        state_error = self._install_state_denial(install)
        if state_error:
            return state_error
        compatibility_error = self._release_compatibility_denial(install)
        if compatibility_error:
            return compatibility_error
        entitlement_error = self._entitlement_denial(install)
        if entitlement_error:
            return entitlement_error
        rows = self._list_runtime_connections(
            self._context.logger(),
            account_id=account_id,
            install_id=install.install_id,
            connection_id=connection_id,
            project_id=project_id,
            enabled_only=True,
        )
        if not rows:
            return "No enabled extension_runtime connection is available for this install"
        if any(self._connection_is_usable(row) for row in rows):
            return ""
        return "No compatible and healthy extension_runtime connection is available"

    @staticmethod
    def _install_state_denial(install: Any) -> str:
        install_state = getattr(install, "state", "")
        if hasattr(install_state, "value"):
            install_state = install_state.value
        if _normalize_text(install_state) != "active":
            return "Install is not active"
        if _normalize_text(getattr(install, "runtime_status", "")).lower() == "incompatible":
            return "Install is marked incompatible"
        if _normalize_text(getattr(install, "runtime_mode", "")).lower() == "remote_https":
            if _normalize_text(getattr(install, "health_status", "")).lower() != "ok":
                return "Install runtime health is not OK"
            if _normalize_text(getattr(install, "handshake_status", "")).lower() != "ok":
                return "Install handshake has not succeeded"
        return ""

    def _release_compatibility_denial(self, install: Any) -> str:
        runtime_store = self._context.runtime_store()
        if not _normalize_text(getattr(install, "release_id", "")) or not hasattr(
            runtime_store, "get_release"
        ):
            return ""
        release = runtime_store.get_release(install.release_id)
        if release is None:
            return ""
        return _release_incompatibility(release, dict(getattr(release, "descriptor", {}) or {}))

    def _entitlement_denial(self, install: Any) -> str:
        runtime_store = self._context.runtime_store()
        entitlement = (
            runtime_store.get_entitlement(install.install_id)
            if hasattr(runtime_store, "get_entitlement")
            else None
        )
        if entitlement is None:
            return "Entitlement not found"
        status = getattr(entitlement, "status", "")
        if hasattr(status, "value"):
            status = status.value
        normalized = _normalize_text(status).lower()
        if normalized not in {"trial_active", "active", "grace_period"}:
            return (
                f"Entitlement status '{normalized or 'unknown'}' "
                "does not allow runtime token issuance"
            )
        return ""

    @staticmethod
    def _connection_is_usable(row: dict[str, Any]) -> bool:
        if _normalize_text(row.get("runtime_status")).lower() == "incompatible":
            return False
        runtime_mode = _normalize_text(row.get("runtime_mode")).lower()
        health = _normalize_text(row.get("health_status")).lower()
        return runtime_mode != "remote_https" or not health or health == "ok"
