"""Project-scoped use cases for extension-owned OAuth configurations."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, cast
from uuid import UUID, uuid4

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.oauth_configuration_models import (
    OAuthBoundWorkflowStep as OAuthBoundWorkflowStep,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationConnectionImpact as OAuthConfigurationConnectionImpact,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationFieldView as OAuthConfigurationFieldView,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationImpact as OAuthConfigurationImpact,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationRepository as OAuthConfigurationRepository,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationSave as OAuthConfigurationSave,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationSecretStore as OAuthConfigurationSecretStore,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationStatus as OAuthConfigurationStatus,
)
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationView as OAuthConfigurationView,
)
from core.application.extensions.oauth_configuration_models import (
    _PreparedChange,
    _PreparedDelete,
    _ResolvedProvider,
)
from core.application.extensions.oauth_configuration_policy import (
    _MESSAGE_CONFIGURATION_NOT_FOUND,
    _confirmation_token,
    _delete_confirmation_token,
    _raw_repository_scope,
    _refs,
    _reject_disconnecting_connections,
    _repository_scope,
    _require_current_contract,
    _require_role,
    _validate_value,
)
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    compile_oauth_providers,
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

_MESSAGE_OAUTH_PROVIDER_NOT_FOUND = "OAuth provider not found"
_MESSAGE_CONFIGURATION_REVISION_CONFLICT = "Configuration revision conflict"


class OAuthConfigurationService:
    def __init__(
        self,
        *,
        repository: OAuthConfigurationRepository,
        secret_store: OAuthConfigurationSecretStore,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        confirmation_key: bytes,
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        token_snapshot: Callable[..., dict[str, Any]] | None = None,
        configuration_secret_value: Callable[..., str] | None = None,
        revoke_token: Callable[..., None] | None = None,
        revocation_audit: Callable[..., None] | None = None,
    ) -> None:
        if not confirmation_key:
            raise ValueError("confirmation_key is required")
        self._repository = repository
        self._secret_store = secret_store
        self._install_loader = install_loader
        self._confirmation_key = bytes(confirmation_key)
        self._transaction_factory = transaction_factory
        self._token_snapshot = token_snapshot
        self._configuration_secret_value = configuration_secret_value
        self._revoke_token = revoke_token
        self._revocation_audit = revocation_audit

    def _resolve_provider(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        *,
        concealed_missing: bool = False,
    ) -> _ResolvedProvider:
        install = self._install_loader(str(install_id or "").strip(), str(scope.account_id))
        if not isinstance(install, Mapping):
            message = (
                _MESSAGE_CONFIGURATION_NOT_FOUND
                if concealed_missing
                else _MESSAGE_OAUTH_PROVIDER_NOT_FOUND
            )
            raise ServiceNotFoundError(message)
        extension_id = str(install.get("extension_id") or "").strip()
        release_id = str(install.get("release_id") or "").strip()
        descriptor = install.get("compiled_manifest") or install.get("manifest") or {}
        if not extension_id or not release_id or not isinstance(descriptor, dict):
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        try:
            providers = compile_oauth_providers(descriptor)
        except ValueError as exc:
            raise ServiceValidationError("Installed OAuth provider contract is invalid") from exc
        provider = next((row for row in providers if row.provider_id == provider_id), None)
        if provider is None:
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        return _ResolvedProvider(
            extension_id=extension_id,
            release_id=release_id,
            provider=provider,
        )

    def _resolve_extension_id(
        self, scope: ScopeContext, install_id: str, *, concealed_missing: bool
    ) -> str:
        install = self._install_loader(str(install_id or "").strip(), str(scope.account_id))
        extension_id = (
            str(install.get("extension_id") or "").strip() if isinstance(install, Mapping) else ""
        )
        if not extension_id:
            message = (
                _MESSAGE_CONFIGURATION_NOT_FOUND
                if concealed_missing
                else _MESSAGE_OAUTH_PROVIDER_NOT_FOUND
            )
            raise ServiceNotFoundError(message)
        return extension_id

    def _view(
        self,
        row: Mapping[str, Any],
        provider: OAuthProviderContract,
        *,
        include_credentials: bool = True,
    ) -> OAuthConfigurationView:
        values = row.get("non_secret_config")
        values = dict(values) if isinstance(values, Mapping) else {}
        refs = _refs(row.get("secret_refs"))
        account_id = str(row.get("account_id") or "").strip()
        fields: list[OAuthConfigurationFieldView] = []
        for field in provider.credential_fields if include_credentials else ():
            if field.secret:
                ref_key = refs.get(field.field_id, "")
                fields.append(
                    OAuthConfigurationFieldView(
                        field_id=field.field_id,
                        secret_present=bool(ref_key)
                        and self._secret_store.present(account_id=account_id, ref_key=ref_key),
                    )
                )
            else:
                fields.append(
                    OAuthConfigurationFieldView(
                        field_id=field.field_id,
                        value=str(values.get(field.field_id) or ""),
                    )
                )
        return OAuthConfigurationView(
            configuration_id=str(row.get("configuration_id")),
            display_name=str(row.get("display_name") or ""),
            status=cast(OAuthConfigurationStatus, str(row.get("status") or "incomplete")),
            fields=fields,
            revision=int(row.get("configuration_revision") or 1),
            authorization_revision=int(row.get("authorization_revision") or 1),
        )

    def list_configurations(
        self, scope: ScopeContext, install_id: str, provider_id: str
    ) -> list[OAuthConfigurationView]:
        _require_role(scope, "viewer")
        resolved = self._resolve_provider(scope, install_id, provider_id)
        rows = self._repository.list_configurations(
            **_repository_scope(scope, install_id, resolved)
        )
        include_credentials = principal_type_has_minimum_role(scope.principal_type, "operator")
        views = [
            self._view(row, resolved.provider, include_credentials=include_credentials)
            for row in rows
        ]
        return sorted(views, key=lambda row: (row.display_name.casefold(), row.configuration_id))

    def reconcile_release(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
    ) -> OAuthConfigurationView:
        """Move a configuration left on an earlier release to the installed one.

        An extension update that did not move its OAuth configuration left it
        refusing authorization ("release transition required") and its
        connections refusing disconnect, and deleting the configuration was the
        only way out, which removed the connections the workflow steps were
        bound to. When this release signs in exactly as the earlier one did
        (same security and revocation fingerprints), nothing the user approved
        has changed: the configuration and its connections are moved as they
        are, keeping their ids and credentials. A provider that changed how it
        signs in still needs a fresh approval and is refused here.
        """
        _require_role(scope, "operator")
        resolved = self._resolve_provider(scope, install_id, provider_id)
        repository_scope = {
            **_repository_scope(scope, install_id, resolved),
            "configuration_id": configuration_id,
        }
        with self._transaction_factory():
            existing = self._repository.get_configuration_for_scope_for_update(**repository_scope)
            if existing is None:
                raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
            same_sign_in = str(
                existing.get("provider_security_fingerprint") or ""
            ) == provider_security_fingerprint(resolved.provider) and str(
                existing.get("provider_revocation_fingerprint") or ""
            ) == provider_revocation_fingerprint(resolved.provider)
            if not same_sign_in:
                raise ServicePreconditionError(
                    "This release changed how the extension signs in; set up the OAuth app again",
                    payload={"error": "oauth_release_reapproval_required"},
                )
            if str(existing.get("extension_release_id") or "") == resolved.release_id:
                return self._view(existing, resolved.provider)
            revision = int(existing.get("configuration_revision") or 1)
            last_error = existing.get("last_error")
            updated = self._repository.update_configuration(
                **repository_scope,
                expected_configuration_revision=revision,
                extension_release_id=resolved.release_id,
                display_name=str(existing.get("display_name") or ""),
                configuration_revision=revision + 1,
                authorization_revision=int(existing.get("authorization_revision") or 1),
                status=str(existing.get("status") or "incomplete"),
                non_secret_config=dict(existing.get("non_secret_config") or {}),
                secret_refs=dict(existing.get("secret_refs") or {}),
                provider_security_fingerprint=provider_security_fingerprint(resolved.provider),
                provider_revocation_fingerprint=provider_revocation_fingerprint(resolved.provider),
                last_error=dict(last_error) if isinstance(last_error, Mapping) else {},
            )
            if updated is None:
                raise ServiceConflictError("OAuth configuration changed; retry")
            self._repository.repin_connections_to_release(
                **repository_scope, extension_release_id=resolved.release_id
            )
        return self._view(updated, resolved.provider)

    def _prepare(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthConfigurationSave,
        *,
        expected_revision: int | None,
        lock_existing: bool = False,
    ) -> _PreparedChange:
        resolved = self._resolve_provider(
            scope,
            install_id,
            provider_id,
            concealed_missing=command.configuration_id is not None,
        )
        repository_scope = _repository_scope(scope, install_id, resolved)
        existing: dict[str, Any] | None = None
        if command.configuration_id is not None:
            get_configuration = (
                self._repository.get_configuration_for_scope_for_update
                if lock_existing
                else self._repository.get_configuration_for_scope
            )
            existing = get_configuration(
                **repository_scope,
                configuration_id=command.configuration_id,
            )
            if existing is None:
                raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
            current_revision = int(existing.get("configuration_revision") or 0)
            if expected_revision is None or expected_revision != current_revision:
                raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
            _require_current_contract(existing, resolved)
            configuration_id = str(command.configuration_id)
        else:
            if expected_revision is not None:
                raise ServiceValidationError("expected_revision must be omitted for create")
            configuration_id = str(uuid4())

        fields = {field.field_id: field for field in resolved.provider.credential_fields}
        unknown = sorted((set(command.values) | set(command.secret_replacements)) - set(fields))
        if unknown:
            raise ServiceValidationError(
                f"Unknown credential field: {unknown[0]}",
                payload={
                    "error": "invalid_credential_field",
                    "field_id": unknown[0],
                    "reason": "unknown_field",
                },
            )
        wrong_value_fields = sorted(key for key in command.values if fields[key].secret)
        wrong_secret_fields = sorted(
            key for key in command.secret_replacements if not fields[key].secret
        )
        if wrong_value_fields or wrong_secret_fields:
            field_id = (wrong_value_fields or wrong_secret_fields)[0]
            raise ServiceValidationError(
                f"Invalid credential channel for {field_id}",
                payload={
                    "error": "invalid_credential_field",
                    "field_id": field_id,
                    "reason": "invalid_channel",
                },
            )

        existing_values = (
            dict(existing.get("non_secret_config") or {}) if existing is not None else {}
        )
        existing_refs = _refs(existing.get("secret_refs")) if existing is not None else {}
        candidate_values = {str(key): str(value).strip() for key, value in existing_values.items()}
        candidate_values.update({key: str(value).strip() for key, value in command.values.items()})
        candidate_refs = dict(existing_refs)
        replacements = {
            field_id: str(value)
            for field_id, value in command.secret_replacements.items()
            if str(value) != ""
        }
        for field_id in replacements:
            candidate_refs[field_id] = configuration_secret_ref_key(configuration_id, field_id)

        changed_fields: list[str] = []
        policies: set[str] = set()
        for field in resolved.provider.credential_fields:
            if field.secret:
                stored = candidate_refs.get(field.field_id, "")
                present = bool(stored) and (
                    field.field_id in replacements
                    or self._secret_store.present(account_id=str(scope.account_id), ref_key=stored)
                )
                value = replacements.get(field.field_id, "")
                if field.field_id in replacements:
                    _validate_value(field, value, present=True)
                elif field.required and not present:
                    _validate_value(field, "", present=False)
                changed = field.field_id in replacements
            else:
                value = str(candidate_values.get(field.field_id) or "")
                present = bool(value)
                _validate_value(field, value, present=present)
                old_value = str(existing_values.get(field.field_id) or "").strip()
                changed = value != old_value
            if changed:
                changed_fields.append(field.field_id)
                policies.add(field.change_policy)

        connections = []
        if existing is not None:
            list_connections = self._repository.list_connections_for_configuration
            if lock_existing:
                list_connections = self._repository.list_connections_for_configuration_for_update
            connections = list_connections(
                **repository_scope,
                configuration_id=configuration_id,
            )
        return _PreparedChange(
            resolved=resolved,
            existing=existing,
            configuration_id=configuration_id,
            non_secret_config=candidate_values,
            secret_refs=candidate_refs,
            secret_replacements=replacements,
            changed_fields=sorted(changed_fields),
            change_policies=sorted(policies),
            connections=connections,
        )

    def _impact(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthConfigurationSave,
        expected_revision: int | None,
        prepared: _PreparedChange,
    ) -> OAuthConfigurationImpact:
        requires_confirmation = (
            prepared.existing is not None and "reauthorization_required" in prepared.change_policies
        )
        connection_views = [
            OAuthConfigurationConnectionImpact(
                connection_id=str(row.get("connection_id") or ""),
                display_name=str(row.get("display_name") or ""),
                connection_type=str(row.get("connection_type") or ""),
            )
            for row in prepared.connections
        ]
        connection_views.sort(key=lambda row: row.connection_id)
        return OAuthConfigurationImpact(
            configuration_id=(
                str(command.configuration_id) if command.configuration_id is not None else None
            ),
            expected_revision=expected_revision,
            changed_fields=prepared.changed_fields,
            change_policies=prepared.change_policies,
            connections=connection_views,
            requires_confirmation=requires_confirmation,
            confirmation_token=_confirmation_token(
                self._confirmation_key,
                scope,
                install_id,
                provider_id,
                command,
                expected_revision,
                prepared,
            )
            if requires_confirmation
            else None,
        )

    def preview_configuration_change(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthConfigurationSave,
        *,
        expected_revision: int | None,
    ) -> OAuthConfigurationImpact:
        _require_role(scope, "operator")
        prepared = self._prepare(
            scope,
            install_id,
            provider_id,
            command,
            expected_revision=expected_revision,
        )
        return self._impact(scope, install_id, provider_id, command, expected_revision, prepared)

    def save_configuration(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthConfigurationSave,
        *,
        expected_revision: int | None,
        confirmation_token: str | None = None,
    ) -> OAuthConfigurationView:
        with self._transaction_factory():
            return self._save_configuration(
                scope,
                install_id,
                provider_id,
                command,
                expected_revision=expected_revision,
                confirmation_token=confirmation_token,
            )

    def _save_configuration(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthConfigurationSave,
        *,
        expected_revision: int | None,
        confirmation_token: str | None,
    ) -> OAuthConfigurationView:
        _require_role(scope, "operator")
        prepared = self._prepare(
            scope,
            install_id,
            provider_id,
            command,
            expected_revision=expected_revision,
            lock_existing=True,
        )
        impact = self._impact(scope, install_id, provider_id, command, expected_revision, prepared)
        if impact.requires_confirmation and not hmac.compare_digest(
            str(confirmation_token or ""), str(impact.confirmation_token or "")
        ):
            raise ServicePreconditionError(
                "Configuration change confirmation required",
                payload={"error": "confirmation_required", "impact": impact.model_dump()},
            )

        repository_scope = _repository_scope(scope, install_id, prepared.resolved)
        runtime_revalidate = "runtime_revalidate" in prepared.change_policies
        reauthorization_required = "reauthorization_required" in prepared.change_policies
        invalidates_pending_authorization = runtime_revalidate or reauthorization_required
        if reauthorization_required:
            _reject_disconnecting_connections(prepared.connections)
        if prepared.existing is None:
            row = self._repository.create_configuration(
                configuration_id=UUID(prepared.configuration_id),
                **repository_scope,
                extension_release_id=prepared.resolved.release_id,
                display_name=command.display_name,
                configuration_revision=1,
                authorization_revision=1,
                status="ready_for_authorization",
                non_secret_config=prepared.non_secret_config,
                secret_refs={"refs": prepared.secret_refs},
                provider_security_fingerprint=provider_security_fingerprint(
                    prepared.resolved.provider
                ),
                provider_revocation_fingerprint=provider_revocation_fingerprint(
                    prepared.resolved.provider
                ),
                last_error={},
            )
        else:
            current_revision = int(prepared.existing.get("configuration_revision") or 1)
            current_auth_revision = int(prepared.existing.get("authorization_revision") or 1)
            any_change = bool(prepared.changed_fields) or (
                command.display_name != str(prepared.existing.get("display_name") or "")
            )
            if not any_change:
                return self._view(prepared.existing, prepared.resolved.provider)
            status = str(prepared.existing.get("status") or "incomplete")
            if reauthorization_required or runtime_revalidate:
                status = "ready_for_authorization"
            row = self._repository.update_configuration(
                **repository_scope,
                configuration_id=prepared.configuration_id,
                expected_configuration_revision=current_revision,
                extension_release_id=str(prepared.existing.get("extension_release_id") or ""),
                display_name=command.display_name,
                configuration_revision=current_revision + 1,
                authorization_revision=current_auth_revision
                + (1 if reauthorization_required else 0),
                status=status,
                non_secret_config=prepared.non_secret_config,
                secret_refs={"refs": prepared.secret_refs},
                provider_security_fingerprint=str(
                    prepared.existing.get("provider_security_fingerprint") or ""
                ),
                provider_revocation_fingerprint=str(
                    prepared.existing.get("provider_revocation_fingerprint") or ""
                ),
                last_error={},
            )
            if row is None:
                raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
            if invalidates_pending_authorization:
                self._repository.invalidate_pending_states(
                    **repository_scope,
                    configuration_id=prepared.configuration_id,
                )
            if reauthorization_required:
                self._repository.mark_connections_reauthorization_required(
                    **repository_scope,
                    configuration_id=prepared.configuration_id,
                )
        if row is None:
            raise ServiceConflictError("Configuration could not be saved")
        for field_id, value in prepared.secret_replacements.items():
            self._secret_store.write(
                account_id=str(scope.account_id),
                ref_key=prepared.secret_refs[field_id],
                value=value,
            )
        return self._view(row, prepared.resolved.provider)

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
        views = [
            OAuthConfigurationConnectionImpact(
                connection_id=str(item.get("connection_id") or ""),
                display_name=str(item.get("display_name") or ""),
                connection_type=str(item.get("connection_type") or ""),
            )
            for item in connections
        ]
        views.sort(key=lambda item: item.connection_id)
        requires_confirmation = bool(connections)
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
            change_policies=["reauthorization_required"] if connections else [],
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
                connections,
            )
            if requires_confirmation
            else None,
        )

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
        if connections:
            expected_confirmation = _delete_confirmation_token(
                self._confirmation_key,
                scope,
                install_id,
                extension_id,
                provider_id,
                configuration_id,
                expected_revision,
                connections,
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

    def mark_invalid_client(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
    ) -> OAuthConfigurationView:
        with self._transaction_factory():
            return self._mark_invalid_client(scope, install_id, provider_id, configuration_id)

    def _mark_invalid_client(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
    ) -> OAuthConfigurationView:
        _require_role(scope, "operator")
        resolved = self._resolve_provider(scope, install_id, provider_id, concealed_missing=True)
        repository_scope = _repository_scope(scope, install_id, resolved)
        row = self._repository.get_configuration_for_scope(
            **repository_scope,
            configuration_id=configuration_id,
        )
        if row is None:
            raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
        revision = int(row.get("configuration_revision") or 1)
        updated = self._repository.update_configuration(
            **repository_scope,
            configuration_id=configuration_id,
            expected_configuration_revision=revision,
            extension_release_id=str(row.get("extension_release_id") or resolved.release_id),
            display_name=str(row.get("display_name") or ""),
            configuration_revision=revision + 1,
            authorization_revision=int(row.get("authorization_revision") or 1),
            status="needs_attention",
            non_secret_config=dict(row.get("non_secret_config") or {}),
            secret_refs=dict(row.get("secret_refs") or {}),
            provider_security_fingerprint=str(row.get("provider_security_fingerprint") or ""),
            provider_revocation_fingerprint=str(row.get("provider_revocation_fingerprint") or ""),
            last_error={"code": "invalid_client"},
        )
        if updated is None:
            raise ServiceConflictError(_MESSAGE_CONFIGURATION_REVISION_CONFLICT)
        return self._view(updated, resolved.provider)


__all__ = [
    "OAuthConfigurationConnectionImpact",
    "OAuthConfigurationFieldView",
    "OAuthConfigurationImpact",
    "OAuthConfigurationSave",
    "OAuthConfigurationService",
    "OAuthConfigurationView",
]
