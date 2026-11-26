"""Delete flow of project-scoped extension OAuth configurations.

The delete half of `OAuthConfigurationService`, kept in its own module: preview the
impact, confirm, revoke provider tokens, and remove the configuration with its
connections and stored secrets. The service provides the repository, the secret
store, the transaction factory, the token callbacks and `_resolve_provider`.
"""

from __future__ import annotations

import hmac
from typing import Any

from core.application.extensions.oauth_configuration_models import (
    OAuthBoundWorkflowStep,
    OAuthConfigurationConnectionImpact,
    OAuthConfigurationImpact,
    _PreparedDelete,
)
from core.application.extensions.oauth_configuration_policy import (
    _MESSAGE_CONFIGURATION_NOT_FOUND,
    _delete_confirmation_token,
    _raw_repository_scope,
    _refs,
    _reject_disconnecting_connections,
    _require_role,
)
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_lifecycle import (
    PreparedOAuthRevocation,
    execute_extension_oauth_revocation,
    prepare_extension_oauth_revocation,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceValidationError,
)
from core.application.management.project_connection_secrets import connection_secret_ref_key
from core.application.management.scope import ScopeContext

_MESSAGE_CONFIGURATION_REVISION_CONFLICT = "Configuration revision conflict"


class OAuthConfigurationDeleteMixin:
    """Preview and perform the delete of one OAuth configuration."""

    _repository: Any
    _secret_store: Any
    _confirmation_key: bytes
    _transaction_factory: Any
    _token_snapshot: Any
    _configuration_secret_value: Any
    _revoke_token: Any
    _revocation_audit: Any
    _resolve_provider: Any
    _resolve_extension_id: Any

    def preview_configuration_delete(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        *,
        expected_revision: int,
    ) -> OAuthConfigurationImpact:
        with self._transaction_factory():
            return self._preview_configuration_delete(
                scope,
                install_id,
                provider_id,
                configuration_id,
                expected_revision=expected_revision,
            )

    def _preview_configuration_delete(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        *,
        expected_revision: int,
    ) -> OAuthConfigurationImpact:
        _require_role(scope, "operator")
        extension_id = self._resolve_extension_id(scope, install_id, concealed_missing=True)
        repository_scope = _raw_repository_scope(scope, install_id, extension_id, provider_id)
        row = self._repository.get_configuration_for_scope(
            **repository_scope, configuration_id=configuration_id
        )
        if row is None:
            raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
        if int(row.get("configuration_revision") or 0) != expected_revision:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        connections = sorted(
            self._repository.list_connections_for_configuration(
                **repository_scope, configuration_id=configuration_id
            ),
            key=lambda item: str(item.get("connection_id") or ""),
        )
        linked = self._linked_connections(configuration_id)
        views = [
            OAuthConfigurationConnectionImpact(
                connection_id=str(item.get("connection_id") or ""),
                display_name=str(item.get("display_name") or ""),
                connection_type=str(item.get("connection_type") or ""),
            )
            for item in connections
        ] + [
            OAuthConfigurationConnectionImpact(
                connection_id=str(item.get("connection_id") or ""),
                display_name=str(item.get("display_name") or ""),
                connection_type=str(item.get("connection_type") or ""),
                link="credentials",
            )
            for item in linked
        ]
        views.sort(key=lambda item: item.connection_id)
        requires_confirmation = bool(connections or linked)
        list_bound_steps = getattr(
            self._repository, "list_workflow_steps_bound_to_connections", None
        )
        bound_steps = (
            list_bound_steps(
                account_id=repository_scope["account_id"],
                connection_ids=[view.connection_id for view in views if view.connection_id],
            )
            if callable(list_bound_steps) and views
            else []
        )
        return OAuthConfigurationImpact(
            workflow_steps=[
                OAuthBoundWorkflowStep(
                    workflow_id=str(row.get("workflow_id") or ""),
                    workflow_name=str(row.get("workflow_name") or row.get("workflow_id") or ""),
                    group_name=str(row.get("group_name") or ""),
                    step_name=str(row.get("step_name") or row.get("step_id") or ""),
                    connection_id=str(row.get("connection_id") or ""),
                )
                for row in bound_steps
            ],
            configuration_id=configuration_id,
            expected_revision=expected_revision,
            changed_fields=["__delete__"],
            change_policies=["reauthorization_required"] if connections or linked else [],
            connections=views,
            requires_confirmation=requires_confirmation,
            confirmation_token=_delete_confirmation_token(
                self._confirmation_key,
                scope,
                install_id,
                extension_id,
                provider_id,
                configuration_id,
                expected_revision,
                connections + linked,
            )
            if requires_confirmation
            else None,
        )

    def _linked_connections(self, configuration_id: str) -> list[dict[str, Any]]:
        """Connections that take their credentials from this configuration."""
        if getattr(self._repository, "conn", None) is None:
            return []
        from core.application.extensions.connection_credentials import list_linked_connections

        return list_linked_connections(self._repository, configuration_id=configuration_id)

    def delete_configuration(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        *,
        expected_revision: int,
        confirmation_token: str | None = None,
    ) -> None:
        from core.application.extensions.system_email_sender import (
            require_configuration_change_allowed,
        )

        # Read in the service's own transaction, so the revocation below runs on an
        # idle connection (a provider call never waits inside a database transaction).
        with self._transaction_factory():
            require_configuration_change_allowed(
                self._repository, scope, configuration_id=str(configuration_id)
            )
        prepared: _PreparedDelete
        with self._transaction_factory():
            prepared = self._prepare_configuration_delete(
                scope,
                install_id,
                provider_id,
                configuration_id,
                expected_revision=expected_revision,
                confirmation_token=confirmation_token,
            )
        outcomes: list[bool] = []
        provider_current = False
        try:
            with self._transaction_factory():
                current = self._resolve_provider(
                    scope, install_id, provider_id, concealed_missing=True
                )
            provider_current = (
                current.release_id == prepared.extension_release_id
                and hmac.compare_digest(
                    provider_security_fingerprint(current.provider),
                    prepared.provider_security_fingerprint,
                )
                and hmac.compare_digest(
                    provider_revocation_fingerprint(current.provider),
                    prepared.provider_revocation_fingerprint,
                )
            )
        except (ServiceNotFoundError, ServiceValidationError):
            provider_current = False
        if self._revoke_token is not None and provider_current:
            for revocation in prepared.revocations:
                outcomes.append(
                    execute_extension_oauth_revocation(
                        revocation,
                        revoke_token=self._revoke_token,
                    )
                )
        with self._transaction_factory():
            self._delete_configuration(
                scope,
                install_id,
                provider_id,
                configuration_id,
                extension_id=prepared.extension_id,
                expected_revision=prepared.blocked_revision,
                expected_connection_ids=prepared.connection_ids,
            )
        if self._revocation_audit is not None:
            outcome = "not_available"
            if prepared.revocations:
                outcome = "succeeded" if outcomes and all(outcomes) else "failed"
            self._revocation_audit(
                extension_id=prepared.extension_id,
                account_id=str(scope.account_id),
                project_id=str(scope.project_id or ""),
                event="configuration_delete",
                outcome=outcome,
                resource_id=configuration_id,
            )

    def _prepare_configuration_delete(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        *,
        expected_revision: int,
        confirmation_token: str | None,
    ) -> _PreparedDelete:
        _require_role(scope, "operator")
        extension_id = self._resolve_extension_id(scope, install_id, concealed_missing=True)
        repository_scope = _raw_repository_scope(scope, install_id, extension_id, provider_id)
        row = self._repository.get_configuration_for_scope_for_update(
            **repository_scope, configuration_id=configuration_id
        )
        if row is None:
            raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
        if int(row.get("configuration_revision") or 0) != expected_revision:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        connections = sorted(
            self._repository.list_connections_for_configuration_for_update(
                **repository_scope, configuration_id=configuration_id
            ),
            key=lambda item: str(item.get("connection_id") or ""),
        )
        _reject_disconnecting_connections(connections)
        revocations: list[PreparedOAuthRevocation] = []
        if self._token_snapshot is not None and self._configuration_secret_value is not None:
            try:
                resolved = self._resolve_provider(
                    scope, install_id, provider_id, concealed_missing=True
                )
            except (ServiceNotFoundError, ServiceValidationError):
                resolved = None
            if (
                resolved is not None
                and str(row.get("extension_release_id") or "") == resolved.release_id
            ):
                for connection in connections:
                    prepared = prepare_extension_oauth_revocation(
                        provider=resolved.provider,
                        configuration=row,
                        connection=connection,
                        token_snapshot=self._token_snapshot,
                        configuration_secret_value=self._configuration_secret_value,
                    )
                    if prepared is not None:
                        revocations.append(prepared)
        linked = self._linked_connections(configuration_id)
        if connections or linked:
            expected_confirmation = _delete_confirmation_token(
                self._confirmation_key,
                scope,
                install_id,
                extension_id,
                provider_id,
                configuration_id,
                expected_revision,
                connections + linked,
            )
            if not hmac.compare_digest(str(confirmation_token or ""), expected_confirmation):
                impact = OAuthConfigurationImpact(
                    configuration_id=configuration_id,
                    expected_revision=expected_revision,
                    changed_fields=["__delete__"],
                    change_policies=["reauthorization_required"],
                    connections=[
                        OAuthConfigurationConnectionImpact(
                            connection_id=str(item.get("connection_id") or ""),
                            display_name=str(item.get("display_name") or ""),
                            connection_type=str(item.get("connection_type") or ""),
                        )
                        for item in connections
                    ]
                    + [
                        OAuthConfigurationConnectionImpact(
                            connection_id=str(item.get("connection_id") or ""),
                            display_name=str(item.get("display_name") or ""),
                            connection_type=str(item.get("connection_type") or ""),
                            link="credentials",
                        )
                        for item in linked
                    ],
                    requires_confirmation=True,
                    confirmation_token=expected_confirmation,
                )
                raise ServicePreconditionError(
                    "Configuration delete confirmation required",
                    payload={"error": "confirmation_required", "impact": impact.model_dump()},
                )
        self._repository.invalidate_pending_states(
            **repository_scope, configuration_id=configuration_id
        )
        self._repository.mark_connections_reauthorization_required(
            **repository_scope, configuration_id=configuration_id
        )
        blocked_revision = expected_revision + 1
        blocked = self._repository.update_configuration(
            **repository_scope,
            configuration_id=configuration_id,
            expected_configuration_revision=expected_revision,
            extension_release_id=str(row.get("extension_release_id") or ""),
            display_name=str(row.get("display_name") or ""),
            configuration_revision=blocked_revision,
            authorization_revision=int(row.get("authorization_revision") or 1) + 1,
            status="needs_attention",
            non_secret_config=dict(row.get("non_secret_config") or {}),
            secret_refs=dict(row.get("secret_refs") or {}),
            provider_security_fingerprint=str(row.get("provider_security_fingerprint") or ""),
            provider_revocation_fingerprint=str(row.get("provider_revocation_fingerprint") or ""),
            last_error={"code": "configuration_deleting"},
        )
        if blocked is None:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        return _PreparedDelete(
            extension_id=extension_id,
            extension_release_id=str(row.get("extension_release_id") or ""),
            provider_security_fingerprint=str(row.get("provider_security_fingerprint") or ""),
            provider_revocation_fingerprint=str(row.get("provider_revocation_fingerprint") or ""),
            blocked_revision=blocked_revision,
            connection_ids=tuple(str(item.get("connection_id") or "") for item in connections),
            revocations=tuple(revocations),
        )

    def _delete_configuration(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        *,
        extension_id: str,
        expected_revision: int,
        expected_connection_ids: tuple[str, ...],
    ) -> None:
        repository_scope = _raw_repository_scope(scope, install_id, extension_id, provider_id)
        row = self._repository.get_configuration_for_scope_for_update(
            **repository_scope, configuration_id=configuration_id
        )
        if row is None or int(row.get("configuration_revision") or 0) != expected_revision:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        connections = sorted(
            self._repository.list_connections_for_configuration_for_update(
                **repository_scope, configuration_id=configuration_id
            ),
            key=lambda item: str(item.get("connection_id") or ""),
        )
        _reject_disconnecting_connections(connections)
        current_connection_ids = tuple(str(item.get("connection_id") or "") for item in connections)
        if current_connection_ids != expected_connection_ids:
            raise ServiceConflictError("Configuration delete impact changed")
        configuration_refs = _refs(row.get("secret_refs"))
        refs = [
            ref_key
            for field_id, ref_key in configuration_refs.items()
            if ref_key == configuration_secret_ref_key(configuration_id, field_id)
        ]
        for connection in connections:
            connection_id = str(connection.get("connection_id") or "")
            refs.extend(
                ref_key
                for secret_name, ref_key in _refs(connection.get("secret_refs")).items()
                if ref_key == connection_secret_ref_key(connection_id, secret_name)
            )
        self._repository.delete_connections_for_configuration(
            **repository_scope, configuration_id=configuration_id
        )
        deleted = self._repository.delete_configuration(
            **repository_scope,
            configuration_id=configuration_id,
            expected_configuration_revision=expected_revision,
        )
        if not deleted:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        for ref_key in sorted(set(refs)):
            self._secret_store.delete(account_id=str(scope.account_id), ref_key=ref_key)


__all__ = ["OAuthConfigurationDeleteMixin"]
