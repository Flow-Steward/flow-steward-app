# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.settings.model_provider_verification_rate_limit import (
    consume_model_provider_verification_limit,
)

from .command_services_common import *  # NOSONAR

_MESSAGE_MODEL_PROVIDER_NOT_FOUND = "Model provider not found"


class ModelProviderCommandService:
    """CRUD for account-scoped encrypted model-provider configurations."""

    def __init__(self, get_logger: Callable[[], Any]):
        self._logger = get_logger

    def _validated_base_url(self, value: Any) -> str | None:
        base_url = str(value or "").strip()
        if not base_url:
            return None
        from core.application.extensions.url_safety import assert_safe_remote_https_url

        try:
            return assert_safe_remote_https_url(base_url, purpose="model provider base_url")
        except ValueError as exc:
            raise ServiceValidationError(f"model provider base_url is not allowed: {exc}") from exc

    def _validated_endpoint_fields(
        self, provider_type: str, command: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        from core.application.settings.model_provider_endpoint_policy import (
            validate_provider_endpoint_fields,
        )

        try:
            endpoint = validate_provider_endpoint_fields(
                provider_type,
                base_url=command.get("base_url"),
                api_version=command.get("api_version"),
                deployment_name=command.get("deployment_name"),
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return (
            self._validated_base_url(endpoint.base_url),
            endpoint.api_version,
            endpoint.deployment_name,
        )

    def _validated_config(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ServiceValidationError("config must be an object")
        from core.application.extensions.extension_descriptor import llm_plaintext_secret_paths

        if llm_plaintext_secret_paths(value, path="config"):
            raise ServiceValidationError("config must not contain plaintext secret fields")
        if value:
            raise ServiceValidationError(
                "config does not accept arbitrary fields; use the typed provider fields"
            )
        return {}

    def _verify_selected_model(
        self, *, conn: Any, account_id: str, provider_id: str
    ) -> dict[str, Any]:
        """Run the same no-repair capability suite before publishing a custom route."""
        import uuid

        from core.application.orchestration.model_provider import get_chat_model
        from core.application.settings.llm_capability_verifier import (
            normalize_verification_error,
            run_capability_suite,
        )
        from core.infrastructure.db.db_pool import transaction_connection

        # Claim the exact persisted route before any paid upstream work. The
        # short transaction commits the pending generation before the probe.
        generation = uuid.uuid4().hex
        with transaction_connection(conn) as claim_conn:
            snapshot = model_provider_store.get_model_provider(
                claim_conn, provider_id=provider_id, account_id=account_id
            )
            if snapshot is None:
                return {"verification_status": "stale"}
            secret_ref = str(snapshot.get("api_key_ref") or "").strip()
            if secret_ref:
                snapshot["secret_ciphertext"] = model_provider_store.get_secret_ref_ciphertext(
                    claim_conn, account_id=account_id, ref_key=secret_ref
                )
            claimed = model_provider_store.claim_model_provider_verification(
                claim_conn,
                account_id=account_id,
                provider_id=provider_id,
                generation=generation,
                expected_provider=snapshot,
            )
        if claimed is None:
            return {"verification_status": "stale"}

        def persist_result(
            *, status: str, capabilities: list[str] | None = None, safe_error: str | None = None
        ) -> bool:
            with transaction_connection(conn) as result_conn:
                return model_provider_store.set_model_provider_verification(
                    result_conn,
                    account_id=account_id,
                    provider_id=provider_id,
                    generation=generation,
                    status=status,
                    verified_capabilities=capabilities,
                    safe_error=safe_error,
                )

        def release_claim() -> None:
            with transaction_connection(conn) as cancel_conn:
                model_provider_store.cancel_model_provider_verification(
                    cancel_conn,
                    account_id=account_id,
                    provider_id=provider_id,
                    generation=generation,
                )

        result_published = False
        try:
            claimed_snapshot = dict(snapshot)
            ciphertext = str(claimed_snapshot.pop("secret_ciphertext", "") or "")
            if ciphertext:
                from core.application.auth.secrets import decrypt_secret

                claimed_snapshot["_decrypted_api_key"] = decrypt_secret(ciphertext)
            model = get_chat_model(
                {
                    "account_id": account_id,
                    "agents_config": {
                        "default_provider": f"custom:{provider_id}",
                        "default_model": str(snapshot.get("default_model") or ""),
                        "model_provider_id": provider_id,
                        "max_retries": 0,
                    },
                },
                timeout_seconds=60,
                allow_unverified_custom=True,
                claimed_custom_provider=claimed_snapshot,
            )
            capabilities = sorted(run_capability_suite(model, timeout_seconds=60))
        except Exception as exc:
            safe_error = normalize_verification_error(exc)
            result_published = persist_result(
                status="failed",
                safe_error=safe_error,
            )
            if not result_published:
                return {"verification_status": "stale"}
            return {
                "verification_status": "failed",
                "verification_error_safe": safe_error,
            }
        else:
            result_published = persist_result(
                status="passed",
                capabilities=capabilities,
            )
            if not result_published:
                return {"verification_status": "stale"}
            return {"verification_status": "passed", "verified_capabilities": capabilities}
        finally:
            if not result_published:
                release_claim()

    def create_model_provider(
        self, command: dict[str, Any], *, scope: ScopeContext | None = None
    ) -> dict[str, Any]:
        scope = _service_scope(scope)
        bl = self._logger()
        conn = bl.conn

        from core.application.auth.secrets import encrypt_secret

        name = str(command.get("name") or command.get("display_name") or "").strip()
        if not name:
            raise ServiceValidationError("name is required")
        provider_type = str(command.get("provider_type", "")).strip()
        base_url, api_version, deployment_name = self._validated_endpoint_fields(
            provider_type, command
        )
        account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        api_key = str(command.get("api_key", "")).strip()
        # The ref is opaque and never derived from a mutable display name.
        # This prevents two providers from sharing or deleting each other's key.
        import uuid

        ref_key = f"provider_{uuid.uuid4().hex}_key" if api_key else ""
        config_payload = self._validated_config(command.get("config"))

        encrypted_api_key = encrypt_secret(api_key) if api_key else None
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(conn) as transaction_conn:
            provider_id = model_provider_store.insert_model_provider(
                transaction_conn,
                account_id=account_id,
                name=name,
                provider_type=provider_type,
                base_url=base_url,
                api_key_ref=ref_key or None,
                api_version=api_version,
                deployment_name=deployment_name,
                default_model=str(command.get("default_model") or "").strip() or None,
                upstream_revision=str(command.get("upstream_revision") or "").strip() or None,
                config=config_payload,
            )
            if encrypted_api_key is not None:
                model_provider_store.upsert_secret_ref(
                    transaction_conn,
                    account_id=account_id,
                    ref_key=ref_key,
                    encrypted_value=encrypted_api_key,
                )
        verification = self._verify_selected_model(
            conn=conn, account_id=account_id, provider_id=str(provider_id or "")
        )
        return {
            "id": provider_id,
            "name": name,
            "provider_type": provider_type,
            "status": "created",
            **verification,
        }

    def list_model_providers(
        self, *, account_id: str = DEFAULT_ACCOUNT_ID, scope: ScopeContext | None = None
    ) -> list[dict]:
        _ = scope
        bl = self._logger()
        providers = model_provider_store.list_enabled_model_providers(
            bl.conn,
            account_id=account_id,
        )
        from core.application.settings.llm_verification_policy import (
            MODEL_VERIFICATION_EXPIRED_ERROR,
            model_verification_is_fresh,
        )

        for provider in providers:
            if str(
                provider.get("verification_status") or ""
            ).strip() == "passed" and not model_verification_is_fresh(provider):
                provider["verification_status"] = "stale"
                provider["verification_error_safe"] = MODEL_VERIFICATION_EXPIRED_ERROR
        return providers

    def reverify_model_provider(
        self, provider_id: str, *, scope: ScopeContext | None = None
    ) -> dict[str, Any]:
        """Explicitly renew one exact custom-provider verification snapshot."""
        scope = _service_scope(scope)
        account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        conn = self._logger().conn
        if (
            model_provider_store.get_model_provider(
                conn, provider_id=str(provider_id), account_id=account_id
            )
            is None
        ):
            raise ServiceNotFoundError(_MESSAGE_MODEL_PROVIDER_NOT_FOUND)
        allowed, retry_after = consume_model_provider_verification_limit(
            conn,
            account_id=account_id,
            provider_id=str(provider_id),
        )
        if not allowed:
            raise ServiceError(
                "Model provider verification rate limit exceeded",
                status_code=429,
                payload={
                    "code": "rate_limited",
                    "message": "Model provider verification rate limit exceeded",
                    "retry_after": retry_after,
                },
            )
        verification = self._verify_selected_model(
            conn=conn, account_id=account_id, provider_id=str(provider_id)
        )
        return {"id": str(provider_id), **verification}

    def update_model_provider(
        self,
        provider_id: str,
        command: dict[str, Any],
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = _service_scope(scope)
        bl = self._logger()
        conn = bl.conn
        account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip()

        from core.application.auth.secrets import encrypt_secret

        name = str(command.get("name") or command.get("display_name") or "").strip()
        if not name:
            raise ServiceValidationError("name is required")
        provider_type = str(command.get("provider_type", "")).strip()
        base_url, api_version, deployment_name = self._validated_endpoint_fields(
            provider_type, command
        )
        api_key = str(command.get("api_key", "")).strip()
        import uuid

        # Rotate the pointer, not the ciphertext beneath an existing pointer.
        # A probe holding the old snapshot can no longer pass its CAS between
        # stale marking and the later secret write.
        ref_key = f"provider_{uuid.uuid4().hex}_key" if api_key else None
        config_payload = self._validated_config(command.get("config"))
        default_model = str(command.get("default_model") or "").strip() or None
        upstream_revision = str(command.get("upstream_revision") or "").strip() or None

        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(conn) as transaction_conn:
            existing = model_provider_store.lock_model_provider_for_update(
                transaction_conn,
                provider_id=str(provider_id),
                account_id=account_id,
            )
            if existing is None:
                raise ServiceNotFoundError(_MESSAGE_MODEL_PROVIDER_NOT_FOUND)
            previous_ref = str(existing.get("api_key_ref") or "").strip()
            effective_ref = ref_key or previous_ref or None
            routing_changed = bool(api_key) or any(
                (
                    existing.get("provider_type") != provider_type,
                    existing.get("base_url") != base_url,
                    existing.get("api_version") != api_version,
                    existing.get("deployment_name") != deployment_name,
                    existing.get("default_model") != default_model,
                    existing.get("upstream_revision") != upstream_revision,
                    (existing.get("config_jsonb") or {}) != config_payload,
                )
            )
            if api_key and ref_key:
                model_provider_store.upsert_secret_ref(
                    transaction_conn,
                    account_id=account_id,
                    ref_key=ref_key,
                    encrypted_value=encrypt_secret(api_key),
                )
            updated = model_provider_store.update_model_provider(
                transaction_conn,
                provider_id=str(provider_id),
                name=name,
                provider_type=provider_type,
                account_id=account_id,
                base_url=base_url,
                api_key_ref=effective_ref,
                api_version=api_version,
                deployment_name=deployment_name,
                default_model=default_model,
                upstream_revision=upstream_revision,
                config=config_payload,
                invalidate_verification=routing_changed,
            )
            if not updated:
                raise ServiceError("Failed to update model provider")
            if api_key and ref_key and previous_ref and previous_ref != ref_key:
                model_provider_store.delete_secret_ref(
                    transaction_conn,
                    ref_key=previous_ref,
                    account_id=account_id,
                )

        if routing_changed:
            verification = self._verify_selected_model(
                conn=conn, account_id=account_id, provider_id=str(provider_id)
            )
        else:
            capabilities = existing.get("verified_capabilities_jsonb") or []
            if isinstance(capabilities, str):
                try:
                    capabilities = json.loads(capabilities)
                except (TypeError, ValueError):
                    capabilities = []
            verification = {
                "verification_status": str(existing.get("verification_status") or "stale"),
                "verified_capabilities": list(capabilities)
                if isinstance(capabilities, list)
                else [],
            }
            safe_error = str(existing.get("verification_error_safe") or "").strip()
            if safe_error:
                verification["verification_error_safe"] = safe_error
        return {"id": str(provider_id), "status": "updated", "provider": updated, **verification}

    def delete_model_provider(
        self, provider_id: str, *, scope: ScopeContext | None = None
    ) -> dict[str, Any]:
        scope = _service_scope(scope)
        bl = self._logger()
        account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl.conn) as transaction_conn:
            row = model_provider_store.lock_model_provider_for_update(
                transaction_conn,
                provider_id=str(provider_id),
                account_id=account_id,
            )
            if row is None:
                raise ServiceNotFoundError(_MESSAGE_MODEL_PROVIDER_NOT_FOUND)
            ref_key = str(row.get("api_key_ref") or "").strip()
            model_provider_store.delete_model_provider(
                transaction_conn,
                provider_id=str(provider_id),
                account_id=account_id,
            )
            if ref_key:
                model_provider_store.delete_secret_ref(
                    transaction_conn, ref_key=ref_key, account_id=account_id
                )
        return {"id": str(provider_id), "status": "deleted"}


__all__ = [name for name in globals() if not name.startswith("__")]
