"""Application workflows for account-scoped LLM provider extensions."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from core.application.settings import llm_provider_connection_status as connection_status
from core.application.settings import llm_provider_extension_repository as store

logger = logging.getLogger(__name__)

_MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH = (
    "A newer extension release or descriptor superseded this refresh"
)


@dataclass(slots=True)
class LlmProviderExtensionError(Exception):
    status_code: int
    detail: str


def without_cleared_fields(value: dict[str, Any]) -> dict[str, Any]:
    """Drop fields saved as an empty string: clearing a field restores its default."""
    return {
        key: item for key, item in value.items() if not (isinstance(item, str) and not item.strip())
    }


def effective_connection_config(contract: Any, value: dict[str, Any]) -> dict[str, Any]:
    """The settings the extension will actually use, for comparing two saves.

    An empty field means the manifest's declared default, and a URL's trailing
    slash means nothing, so re-saving the same settings, or clearing a field
    that held its default, compares equal. Only a real change of an effective
    value should invalidate verified models.
    """
    defaults = dict(getattr(contract, "connection_config_defaults", None) or {})
    merged = {**defaults, **without_cleared_fields(dict(value or {}))}
    return {
        str(key): (item.strip().rstrip("/") if isinstance(item, str) else item)
        for key, item in merged.items()
    }


def credential_ref_for_save(
    base_ref_key: str, *, credentials_changed: bool, has_sources: bool
) -> str:
    """Keep first-save credentials discoverable; rotate only an existing pointer."""
    if credentials_changed and has_sources:
        return f"{base_ref_key}_{uuid.uuid4().hex[:12]}"
    return base_ref_key


class LlmProviderExtensionUseCases:
    """Complete blocking transaction units, invoked through the API threadpool."""

    def __init__(self, conn: Any, *, get_logger_only) -> None:
        self.conn = conn
        self.get_logger_only = get_logger_only

    def _resolve_install(self, install_id: str, account_id: str):
        from core.application.extensions.extension_descriptor import (
            extract_llm_provider_contract,
        )

        logger = self.get_logger_only()
        record = logger.get_extension_install_for_operator(
            install_id,
            account_id=account_id,
        )
        if not record:
            raise LlmProviderExtensionError(404, "Extension install not found")
        contract = None
        for candidate in (record, record.get("compiled_manifest")):
            try:
                contract = extract_llm_provider_contract(candidate) if candidate else None
            except Exception:
                contract = None
            if contract is not None:
                break
        if contract is None:
            raise LlmProviderExtensionError(
                422, "Install does not declare an llm_provider contract"
            )
        canonical_id = str(record.get("install_id") or install_id)
        extension_id = str(record.get("extension_id") or record.get("extension_key") or "").strip()
        display_name = str(record.get("display_name") or extension_id or canonical_id)
        return record, contract, canonical_id, extension_id, display_name

    @staticmethod
    def _connection_config(contract: Any, value: dict[str, Any]) -> dict[str, Any]:
        from core.application.extensions.extension_descriptor import (
            LLM_PROVIDER_CONNECTION_CONFIG_FIELDS,
            canonical_llm_field_name,
        )
        from core.application.settings.llm_provider_extension_service import (
            declared_connection_config_fields,
        )

        allowed = (
            declared_connection_config_fields(contract) & LLM_PROVIDER_CONNECTION_CONFIG_FIELDS
        )
        supplied_raw = {str(field).strip() for field in value}
        supplied = {canonical_llm_field_name(field) for field in supplied_raw}
        if supplied - allowed:
            raise LlmProviderExtensionError(
                422, "This provider has no such setting; reload the page and try again"
            )
        if any(field != canonical_llm_field_name(field) for field in supplied_raw):
            raise LlmProviderExtensionError(
                422,
                "connection_config fields must use canonical snake_case names",
            )
        return {str(key).strip(): item for key, item in value.items()}

    @staticmethod
    def _safe_upstream_config(value: dict[str, Any]) -> dict[str, Any]:
        upstream_url = str(value.get("upstream_base_url") or "").strip()
        if not upstream_url:
            return value
        from core.application.extensions.url_safety import (
            allow_private_provider_addresses,
            assert_safe_provider_url,
        )

        try:
            safe_url = assert_safe_provider_url(
                upstream_url,
                purpose="Base URL",
                allow_private_addresses=allow_private_provider_addresses(),
            )
        except ValueError as exc:
            # The guard's own reasons are written for the user ("must use https
            # for a public address", "... allow private addresses with
            # FS_ALLOW_PRIVATE_REMOTE_URLS=1") and carry no secrets.
            raise LlmProviderExtensionError(422, str(exc)) from exc
        except Exception as exc:
            raise LlmProviderExtensionError(422, "Base URL can't be checked right now") from exc
        return {**value, "upstream_base_url": safe_url}

    @staticmethod
    def _safe_runtime_url(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        from core.application.extensions.url_safety import assert_safe_remote_https_url

        try:
            return assert_safe_remote_https_url(normalized, purpose="llm_provider_remote_base_url")
        except Exception as exc:
            raise LlmProviderExtensionError(422, "remote_base_url is not allowed") from exc

    def list_sources(self, account_id: str) -> dict[str, Any]:
        from core.application.settings.llm_verification_policy import (
            MODEL_VERIFICATION_EXPIRED_ERROR,
            REQUIRED_ROUTING_CAPABILITIES,
            missing_routing_capabilities,
            model_verification_is_fresh,
        )

        sources = store.list_sources(self.conn, account_id=account_id)
        models = store.list_models(self.conn, account_id=account_id, provider_source_id=None)
        models_by_source: dict[str, list[dict[str, Any]]] = {}
        for model in models:
            if str(
                model.get("verification_status") or ""
            ) == "passed" and not model_verification_is_fresh(model):
                model = {
                    **model,
                    "verification_status": "stale",
                    "verification_error_safe": MODEL_VERIFICATION_EXPIRED_ERROR,
                }
            # What the provider itself reports the model cannot do; the UI hides
            # such models by default. Verification remains the final authority.
            model = {
                **model,
                "missing_capabilities": missing_routing_capabilities(
                    model.get("declared_capabilities_jsonb")
                ),
            }
            models_by_source.setdefault(str(model.get("provider_source_id") or ""), []).append(
                model
            )
        result = []
        for source in sources:
            redacted = {
                key: item
                for key, item in source.items()
                if key not in {"credential_secret_ref", "connection_config_jsonb"}
            }
            redacted["has_credentials"] = bool(source.get("credential_secret_ref"))
            connection_config = source.get("connection_config_jsonb")
            redacted["connection_config"] = (
                dict(connection_config) if isinstance(connection_config, dict) else {}
            )
            result.append(
                {
                    **redacted,
                    "models": models_by_source.get(str(source.get("id") or ""), []),
                }
            )
        return {
            "sources": result,
            "required_capabilities": sorted(REQUIRED_ROUTING_CAPABILITIES),
        }

    def save_credentials(
        self,
        install_id: str,
        account_id: str,
        *,
        secrets: dict[str, Any],
        connection_config: dict[str, Any],
        remote_base_url: str = "",
    ) -> dict[str, Any]:
        from core.application.auth.secrets import decrypt_secret, encrypt_secret
        from core.application.settings.llm_provider_extension_service import (
            credential_ref_key,
            encode_credentials,
            require_llm_provider_connection_config,
        )
        from core.infrastructure.db import model_provider_store
        from core.infrastructure.db.db_pool import transaction_connection

        record, contract, canonical_id, _, _ = self._resolve_install(install_id, account_id)
        connection_config = self._safe_upstream_config(
            self._connection_config(contract, connection_config)
        )
        remote_base_url = self._safe_runtime_url(remote_base_url)
        runtime_mode = str(record.get("runtime_mode") or "").strip().lower()
        if remote_base_url and runtime_mode != "remote_https":
            raise LlmProviderExtensionError(
                422, "remote_base_url applies only to a remote_https install"
            )
        if not secrets and not connection_config and not remote_base_url:
            raise LlmProviderExtensionError(422, "Nothing to save: enter a setting or a key first.")
        base_ref_key = credential_ref_key(canonical_id)
        with transaction_connection(self.conn) as transaction_conn:
            install_config = store.lock_install_for_credential_rotation(
                transaction_conn,
                account_id=account_id,
                extension_install_id=canonical_id,
            )
            if install_config is None:
                raise LlmProviderExtensionError(404, "Extension install not found")
            install_sources = [
                source
                for source in store.list_sources(transaction_conn, account_id=account_id)
                if str(source.get("extension_install_id") or "") == canonical_id
            ]
            install_ref = str(install_config.get(store.INSTALL_CREDENTIAL_REF_KEY) or "").strip()
            previous_refs = list(
                dict.fromkeys(
                    ref
                    for ref in [
                        install_ref,
                        *(
                            str(source.get("credential_secret_ref") or "").strip()
                            for source in install_sources
                        ),
                    ]
                    if ref
                )
            ) or [base_ref_key]
            ref_key = previous_refs[0]
            existing_credentials: dict[str, Any] = {}
            ciphertext = store.read_secret_ref_ciphertext(
                transaction_conn, account_id=account_id, ref_key=ref_key
            )
            if ciphertext:
                try:
                    decoded = json.loads(decrypt_secret(ciphertext))
                except Exception as exc:
                    raise LlmProviderExtensionError(
                        409, "Stored extension credentials are unavailable"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise LlmProviderExtensionError(
                        409, "Stored extension credentials are unavailable"
                    )
                existing_credentials = decoded
            existing_config = install_config.get("llm_provider_connection_config", {})
            if not isinstance(existing_config, dict):
                existing_config = {}
            # An empty field is a deliberate clear back to the provider default.
            effective_config = without_cleared_fields({**existing_config, **connection_config})
            try:
                effective_config = require_llm_provider_connection_config(
                    contract,
                    effective_config,
                )
            except ValueError as exc:
                raise LlmProviderExtensionError(422, str(exc)) from exc
            current_runtime_url = str(install_config.get("remote_base_url") or "").strip()
            declared_secrets = {
                str(field).strip()
                for field in (
                    list(getattr(contract, "required_provider_secrets", None) or [])
                    + list(getattr(contract, "optional_provider_secrets", None) or [])
                )
                if str(field).strip()
            }
            if {str(key).strip() for key in secrets} - declared_secrets:
                raise LlmProviderExtensionError(
                    422, "This provider has no such key; reload the page and try again"
                )
            merged = {**existing_credentials, **secrets}
            required = {
                str(field).strip()
                for field in (getattr(contract, "required_provider_secrets", None) or [])
                if str(field).strip()
            }
            if any(not str(merged.get(field) or "").strip() for field in required):
                from core.application.settings.llm_provider_extension_service import (
                    llm_field_label,
                )

                missing = [
                    field for field in sorted(required) if not str(merged.get(field) or "").strip()
                ]
                raise LlmProviderExtensionError(
                    422,
                    "Enter the required key: " + ", ".join(llm_field_label(f) for f in missing),
                )
            credentials_changed = bool(secrets) and merged != existing_credentials
            config_changed = effective_connection_config(
                contract, effective_config
            ) != effective_connection_config(contract, existing_config)
            runtime_changed = bool(remote_base_url) and remote_base_url != current_runtime_url
            if credentials_changed:
                ref_key = credential_ref_for_save(
                    base_ref_key,
                    credentials_changed=True,
                    has_sources=bool(install_sources),
                )
            if credentials_changed or config_changed or runtime_changed:
                store.mark_install_models_stale(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    reason=(
                        "credentials_changed"
                        if credentials_changed and not (config_changed or runtime_changed)
                        else "configuration_changed"
                    ),
                )
                store.mark_install_runtime_unknown(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                )
                connection_status.clear_last_check(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                )
            if secrets:
                connection_status.record_saved_secrets(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    names=[name for name, value in secrets.items() if str(value or "").strip()],
                )
                model_provider_store.upsert_secret_ref(
                    transaction_conn,
                    account_id=account_id,
                    ref_key=ref_key,
                    encrypted_value=encrypt_secret(encode_credentials(merged)),
                )
                store.set_install_credential_ref(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    credential_secret_ref=ref_key,
                )
            store.set_install_connection_config(
                transaction_conn,
                account_id=account_id,
                extension_install_id=canonical_id,
                connection_config=effective_config,
            )
            if remote_base_url:
                store.set_install_remote_base_url(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    remote_base_url=remote_base_url,
                )
            if credentials_changed or config_changed or runtime_changed:
                store.set_install_provider_state_revision(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    revision=uuid.uuid4().hex,
                )
            for source in install_sources:
                source_id = str(source.get("id") or "")
                store.set_source_credential_ref(
                    transaction_conn,
                    account_id=account_id,
                    source_id=source_id,
                    credential_secret_ref=(
                        ref_key if secrets else str(source.get("credential_secret_ref") or "")
                    ),
                )
                stored_config = source.get("connection_config_jsonb")
                store.set_source_connection_config(
                    transaction_conn,
                    account_id=account_id,
                    source_id=source_id,
                    connection_config=without_cleared_fields(
                        {
                            **(stored_config if isinstance(stored_config, dict) else {}),
                            **connection_config,
                        }
                    ),
                )
            if credentials_changed and ref_key not in previous_refs:
                store.delete_install_credential_refs_except(
                    transaction_conn,
                    account_id=account_id,
                    base_ref_key=base_ref_key,
                    keep_ref_keys=[*previous_refs, ref_key],
                )
        changed = bool(credentials_changed or config_changed or runtime_changed)
        return {
            "status": "saved",
            "install_id": canonical_id,
            # The panel says "No changes" when the save left everything as it was,
            # and "previous verification is stale" only when it is.
            "changed": changed,
            "verification_invalidated": changed,
        }

    def connection_status(self, install_id: str, account_id: str) -> dict[str, Any]:
        """Saved secret names (never values) and the last discovery check."""
        from core.infrastructure.db.db_pool import transaction_connection

        _record, contract, canonical_id, _, _ = self._resolve_install(install_id, account_id)
        with transaction_connection(self.conn) as read_conn:
            install_config = connection_status.read_install_config(
                read_conn, account_id=account_id, extension_install_id=canonical_id
            )
            saved_names = connection_status.saved_secret_names(
                read_conn, account_id=account_id, extension_install_id=canonical_id
            )
        return connection_status.connection_status_payload(
            install_id=canonical_id,
            contract=contract,
            install_config=install_config,
            saved_names=saved_names,
        )

    def _record_failed_check(self, account_id: str, install_id: str, category: str) -> None:
        """Keep the failed check's category for the panel; never mask the failure."""
        from core.infrastructure.db.db_pool import transaction_connection

        try:
            with transaction_connection(self.conn) as transaction_conn:
                connection_status.record_last_check(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=install_id,
                    status=category,
                )
        except Exception:
            logger.warning(
                "Could not record the failed LLM provider check for install %s",
                install_id,
                exc_info=True,
            )

    def refresh_models(
        self,
        install_id: str,
        account_id: str,
        *,
        connection_config: dict[str, Any],
    ) -> dict[str, Any]:
        from core.application.auth.secrets import decrypt_secret
        from core.application.settings import llm_provider_extension_service as service
        from core.infrastructure.db.db_pool import transaction_connection

        record, _resolved_contract, canonical_id, _extension_id, _display_name = (
            self._resolve_install(install_id, account_id)
        )
        expected_release_id = str(record.get("release_id") or "").strip()
        expected_descriptor_sha256 = str(record.get("descriptor_sha256") or "").strip()
        if not expected_release_id or not expected_descriptor_sha256:
            raise LlmProviderExtensionError(
                409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
            )
        supplied_config = dict(connection_config)
        generation = uuid.uuid4().hex
        with transaction_connection(self.conn) as snapshot_conn:
            try:
                install_snapshot = store.begin_install_discovery(
                    snapshot_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    generation=generation,
                    expected_release_id=expected_release_id,
                    expected_descriptor_sha256=expected_descriptor_sha256,
                )
            except store.InstallDiscoveryReleaseMismatchError as exc:
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                ) from exc
            if install_snapshot is None:
                raise LlmProviderExtensionError(
                    409, "The extension install was superseded before model discovery"
                )
            claimed_release_id = str(install_snapshot.get("release_id") or "").strip()
            claimed_descriptor_sha256 = str(install_snapshot.get("descriptor_sha256") or "").strip()
            if (
                claimed_release_id != expected_release_id
                or not claimed_descriptor_sha256
                or claimed_descriptor_sha256 != expected_descriptor_sha256
            ):
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                )
            descriptor = install_snapshot.get("descriptor_jsonb")
            if not isinstance(descriptor, dict) or not descriptor:
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                )
            from core.application.extensions.extension_descriptor import (
                extract_llm_provider_contract,
            )

            try:
                contract = extract_llm_provider_contract(descriptor)
            except Exception as exc:
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                ) from exc
            if contract is None:
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                )
            extension_id = str(install_snapshot.get("extension_id") or "").strip()
            if (
                not extension_id
                or str(descriptor.get("extension_id") or "").strip() != extension_id
            ):
                raise LlmProviderExtensionError(
                    409, _MESSAGE_A_NEWER_EXTENSION_RELEASE_OR_DESCRIPTOR_SUPERSEDED_THIS_REFRESH
                )
            identity = descriptor.get("identity")
            identity = identity if isinstance(identity, dict) else {}
            display_name = str(
                descriptor.get("display_name") or identity.get("display_name") or extension_id
            ).strip()
            connection_config = self._connection_config(contract, connection_config)
            endpoint = str(install_snapshot.get("remote_base_url") or "").strip()
            runtime_mode = str(record.get("runtime_mode") or "").strip().lower()
            if runtime_mode == "remote_https" and not endpoint:
                raise LlmProviderExtensionError(422, "runtime_endpoint_required")
            request_record = {
                **record,
                "extension_id": extension_id,
                "extension_key": extension_id,
                "release_id": claimed_release_id,
                "descriptor_sha256": claimed_descriptor_sha256,
                "manifest": descriptor,
                "compiled_manifest": descriptor,
                "entrypoint": descriptor.get("entrypoint"),
                "action_definitions": descriptor.get("action_definitions"),
                "query_definitions": descriptor.get("query_definitions"),
                "ui_manifest": descriptor.get("ui_manifest"),
                "remote_base_url": endpoint,
                "install_id": canonical_id,
            }
            ref_key = str(
                install_snapshot.get("credential_secret_ref") or ""
            ).strip() or service.credential_ref_key(canonical_id)
            ciphertext = str(install_snapshot.get("encrypted_value") or "")
            existing_source_config = install_snapshot.get("connection_config_jsonb")
            credentials: dict[str, Any] = {}
            if ciphertext:
                try:
                    decoded = json.loads(decrypt_secret(ciphertext))
                    if isinstance(decoded, dict):
                        credentials = decoded
                except Exception:
                    credentials = {}
            if not isinstance(existing_source_config, dict):
                existing_source_config = {}
            if not connection_config:
                connection_config = dict(existing_source_config)
            if not connection_config:
                stored = install_snapshot.get("llm_provider_connection_config")
                if isinstance(stored, dict):
                    connection_config = dict(stored)
            state_revision = str(install_snapshot.get("llm_provider_state_revision") or "")
        effective_config = {}
        stored = install_snapshot.get("llm_provider_connection_config")
        if isinstance(stored, dict):
            effective_config.update(stored)
        effective_config.update(existing_source_config)
        previous_config = dict(effective_config)
        effective_config.update(connection_config)
        connection_config = self._safe_upstream_config(effective_config)
        try:
            connection_config = service.require_llm_provider_connection_config(
                contract,
                connection_config,
            )
        except ValueError as exc:
            raise LlmProviderExtensionError(422, str(exc)) from exc
        # Compare what the extension will use before and after, not the raw
        # request against one stored copy: the panel re-sends the settings it
        # shows, and that alone must not make verified models stale.
        config_changed = bool(supplied_config) and effective_connection_config(
            contract, connection_config
        ) != effective_connection_config(contract, previous_config)
        try:
            models = service.fetch_models_via_remote_runtime(
                request_record,
                contract,
                account_id=account_id,
                credentials=credentials,
                connection_config=connection_config,
                get_logger_only=self.get_logger_only,
            )
        except Exception as exc:
            from core.application.settings.llm_capability_verifier import (
                normalize_verification_error,
            )

            category = normalize_verification_error(exc)
            self._record_failed_check(account_id, canonical_id, category)
            raise LlmProviderExtensionError(502, category) from exc
        groups = service.group_discovered_models(
            contract,
            models,
            install_id=canonical_id,
            install_display_name=display_name,
        )
        with transaction_connection(self.conn) as transaction_conn:
            if not store.lock_install_discovery_generation_current(
                transaction_conn,
                account_id=account_id,
                extension_install_id=canonical_id,
                generation=generation,
                state_revision=state_revision,
                release_id=expected_release_id,
                descriptor_sha256=expected_descriptor_sha256,
            ):
                raise LlmProviderExtensionError(
                    409, "A newer model discovery superseded this refresh"
                )
            summary = service.persist_provider_groups(
                transaction_conn,
                account_id=account_id,
                install_id=canonical_id,
                extension_id=extension_id,
                contract=contract,
                groups=groups,
                credential_ref=ref_key if ciphertext else None,
                connection_config=connection_config,
                _transaction_wrapped=True,
            )
            connection_status.record_last_check(
                transaction_conn,
                account_id=account_id,
                extension_install_id=canonical_id,
                status=connection_status.CHECK_OK,
            )
            if config_changed:
                store.set_install_connection_config(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    connection_config=connection_config,
                )
                store.set_install_provider_state_revision(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                    revision=uuid.uuid4().hex,
                )
                store.mark_install_models_stale(
                    transaction_conn,
                    account_id=account_id,
                    extension_install_id=canonical_id,
                )
        if hasattr(self.conn, "commit"):
            self.conn.commit()
        return {"status": "ok", "discovered_total": len(models), **summary}

    def set_source_state(self, source_id: str, account_id: str, *, enabled: bool) -> dict[str, Any]:
        if not store.set_source_enabled(
            self.conn,
            account_id=account_id,
            source_id=source_id,
            enabled=enabled,
        ):
            raise LlmProviderExtensionError(404, "Provider source not found")
        if hasattr(self.conn, "commit"):
            self.conn.commit()
        return {"status": "ok", "source_id": source_id, "enabled": enabled}

    def set_model_state(self, model_id: str, account_id: str, *, enabled: bool) -> dict[str, Any]:
        from core.application.settings.llm_verification_policy import (
            model_verification_is_fresh,
        )

        model = store.get_model(self.conn, account_id=account_id, model_row_id=model_id)
        if not model:
            raise LlmProviderExtensionError(404, "Model not found")
        if enabled and not bool(model.get("supports_structured_output")):
            raise LlmProviderExtensionError(
                422,
                "Model cannot be enabled for global routing: it does not support "
                "structured output (required in v1).",
            )
        if enabled and str(model.get("lifecycle") or "active") != "active":
            raise LlmProviderExtensionError(422, "Model lifecycle is not active")
        if (
            enabled
            and bool(model.get("enabled"))
            and str(model.get("verification_status") or "") == "passed"
            and model_verification_is_fresh(model)
        ):
            return {"status": "ok", "model_id": model_id, "enabled": True}
        if enabled:
            self._verify_and_enable(model_id, account_id, model)
        else:
            store.set_model_enabled(
                self.conn,
                account_id=account_id,
                model_row_id=model_id,
                enabled=False,
            )
        if hasattr(self.conn, "commit"):
            self.conn.commit()
        return {"status": "ok", "model_id": model_id, "enabled": enabled}

    def _verify_and_enable(self, model_id: str, account_id: str, model: dict[str, Any]) -> None:
        from core.application.orchestration.extension_chat_model import ExtensionChatModel
        from core.application.settings.llm_capability_verifier import (
            run_capability_suite,
            verification_failure_code,
        )

        source = store.get_source(
            self.conn,
            account_id=account_id,
            source_id=str(model.get("provider_source_id") or ""),
        )
        if not source:
            raise LlmProviderExtensionError(409, "Provider source is unavailable")
        generation = uuid.uuid4().hex
        claimed = store.claim_model_verification(
            self.conn,
            account_id=account_id,
            model_row_id=model_id,
            generation=generation,
            expected_model_updated_at=model.get("updated_at"),
            expected_source_updated_at=source.get("updated_at"),
            expected_upstream_revision=model.get("upstream_revision"),
            expected_upstream_digest=model.get("upstream_digest"),
        )
        if hasattr(self.conn, "commit"):
            self.conn.commit()
        if not claimed:
            raise LlmProviderExtensionError(
                409, "Model verification is already pending or configuration changed"
            )
        adapter = ExtensionChatModel(
            account_id=account_id,
            provider_key=str(source.get("provider_key") or ""),
            model_id=str(model.get("model_id") or ""),
            chat_action_id=str(source.get("chat_action_id") or "llm.chat"),
            structured_chat_action_id=source.get("structured_chat_action_id"),
            extension_install_id=str(source.get("extension_install_id") or ""),
            extension_id=str(source.get("extension_id") or ""),
            verification_generation=generation,
        )
        try:
            capabilities = run_capability_suite(adapter)
        except Exception as exc:
            persisted = store.set_model_verification_if_current(
                self.conn,
                account_id=account_id,
                model_row_id=model_id,
                status="failed",
                verified_capabilities=[],
                safe_error=verification_failure_code(exc),
                enabled=False,
                expected_generation=generation,
                expected_source_updated_at=source.get("updated_at"),
                expected_upstream_revision=model.get("upstream_revision"),
                expected_upstream_digest=model.get("upstream_digest"),
            )
            if not persisted:
                store.cancel_model_verification_generation(
                    self.conn,
                    account_id=account_id,
                    model_row_id=model_id,
                    expected_generation=generation,
                )
            if hasattr(self.conn, "commit"):
                self.conn.commit()
            if not persisted:
                raise LlmProviderExtensionError(
                    409, "Model configuration changed during verification"
                ) from exc
            # The row carries the failed check; the detail lets the panel name
            # it right away, before the model list is read again.
            raise LlmProviderExtensionError(
                422, f"Capability verification failed: {verification_failure_code(exc)}"
            ) from exc
        persisted = store.set_model_verification_if_current(
            self.conn,
            account_id=account_id,
            model_row_id=model_id,
            status="passed",
            verified_capabilities=sorted(capabilities),
            safe_error=None,
            enabled=True,
            expected_generation=generation,
            expected_source_updated_at=source.get("updated_at"),
            expected_upstream_revision=model.get("upstream_revision"),
            expected_upstream_digest=model.get("upstream_digest"),
        )
        if not persisted:
            store.cancel_model_verification_generation(
                self.conn,
                account_id=account_id,
                model_row_id=model_id,
                expected_generation=generation,
            )
            if hasattr(self.conn, "commit"):
                self.conn.commit()
            raise LlmProviderExtensionError(409, "Model configuration changed during verification")
