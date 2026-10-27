"""Extension schema and secret-reference contract for project connections."""

from __future__ import annotations

from typing import Any

from core.application.management.command_services_common import _json_schema_validate_value
from core.application.management.errors import ServiceValidationError


class ProjectConnectionContract:
    @staticmethod
    def _normalize_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_secret_refs(value: Any) -> dict[str, Any]:
        from core.application.management.project_connection import normalize_secret_refs_payload

        return normalize_secret_refs_payload(value)

    @staticmethod
    def _is_internal_secret_ref_name(name: Any) -> bool:
        return str(name or "").strip().startswith("_oauth_")

    def _secret_ref_names(self, value: Any) -> list[str]:
        refs = self._normalize_secret_refs(value).get("refs")
        if not isinstance(refs, dict):
            return []
        return [
            str(key or "").strip()
            for key in refs
            if str(key or "").strip() and not self._is_internal_secret_ref_name(key)
        ]

    def _sanitize_public_secret_refs(
        self,
        value: Any,
        *,
        connection_id: str = "",
        existing_secret_refs: Any = None,
        platform_secret_ref_names: set[str] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_secret_refs(value)
        refs = normalized.get("refs")
        if refs is None:
            return {}
        if not isinstance(refs, dict):
            raise ServiceValidationError("secret_refs.refs: expected object")

        existing_refs = self._normalize_secret_refs(existing_secret_refs).get("refs")
        existing_map = dict(existing_refs) if isinstance(existing_refs, dict) else {}
        normalized_connection_id = self._normalize_text(connection_id)
        platform_names = {
            self._normalize_text(name)
            for name in (platform_secret_ref_names or set())
            if self._normalize_text(name)
        }

        sanitized: dict[str, str] = {}
        errors: list[str] = []
        for raw_name, raw_ref in refs.items():
            name = self._normalize_text(raw_name)
            ref_value = self._normalize_text(raw_ref) or name
            if not name:
                continue
            if self._is_internal_secret_ref_name(name) or name in platform_names:
                errors.append(f"secret_refs.{name}: platform-owned OAuth secret refs are read-only")
                continue

            existing_ref = self._normalize_text(existing_map.get(name))
            expected_owned_ref = (
                f"project_connection:{normalized_connection_id}:{name}"
                if normalized_connection_id
                else ""
            )
            if ref_value == name:
                sanitized[name] = expected_owned_ref or ref_value
                continue
            if expected_owned_ref and ref_value == expected_owned_ref:
                sanitized[name] = ref_value
                continue
            if (
                existing_ref
                and ref_value == existing_ref
                and (
                    existing_ref == name
                    or (expected_owned_ref and existing_ref == expected_owned_ref)
                )
            ):
                sanitized[name] = ref_value
                continue

            errors.append(
                f"secret_refs.{name}: raw secret ref values are not accepted; "
                "save secret values through /connections/{id}/secrets"
            )

        if errors:
            raise ServiceValidationError("; ".join(errors[:8]))
        return {"refs": sanitized} if sanitized else {}

    def _merge_existing_internal_secret_refs(
        self,
        *,
        secret_refs: Any,
        existing_secret_refs: Any,
        platform_secret_ref_names: set[str] | None = None,
    ) -> dict[str, Any]:
        merged = self._normalize_secret_refs(secret_refs)
        refs = dict(merged.get("refs") or {})
        existing_refs = self._normalize_secret_refs(existing_secret_refs).get("refs")
        platform_names = {
            self._normalize_text(name)
            for name in (platform_secret_ref_names or set())
            if self._normalize_text(name)
        }
        if isinstance(existing_refs, dict):
            for raw_name, raw_ref in existing_refs.items():
                name = self._normalize_text(raw_name)
                ref = self._normalize_text(raw_ref)
                if (
                    name
                    and ref
                    and (self._is_internal_secret_ref_name(name) or name in platform_names)
                ):
                    refs[name] = ref
        return {"refs": refs} if refs else {}

    def _sanitize_public_secret_values(
        self,
        secrets: dict[str, Any],
        *,
        platform_secret_ref_names: set[str] | None = None,
    ) -> dict[str, Any]:
        platform_names = {
            self._normalize_text(name)
            for name in (platform_secret_ref_names or set())
            if self._normalize_text(name)
        }
        sanitized: dict[str, Any] = {}
        errors: list[str] = []
        for raw_name, secret_value in (secrets or {}).items():
            name = self._normalize_text(raw_name)
            if not name or secret_value is None:
                continue
            if self._is_internal_secret_ref_name(name) or name in platform_names:
                errors.append(f"secrets.{name}: platform-owned OAuth secret refs are read-only")
                continue
            sanitized[name] = secret_value
        if errors:
            raise ServiceValidationError("; ".join(errors[:8]))
        return sanitized

    def _oauth_platform_secret_ref_names(
        self,
        *,
        bl: Any,
        extension_id: str | None,
        connection_type: str,
        account_id: str,
        connection_config: Any,
    ) -> set[str]:
        normalized_extension_id = self._normalize_text(extension_id)
        normalized_connection_type = self._normalize_text(connection_type)
        if not normalized_extension_id or not normalized_connection_type:
            return set()
        try:
            from core.application.auth.oauth_client import oauth_auth_config_for_connection
        except Exception:
            return set()
        try:
            auth_config = oauth_auth_config_for_connection(
                bl,
                {
                    "extension_id": normalized_extension_id,
                    "connection_type": normalized_connection_type,
                    "account_id": self._normalize_text(account_id),
                    "connection_config": dict(connection_config)
                    if isinstance(connection_config, dict)
                    else {},
                },
            )
        except Exception:
            return set()
        secret_names = auth_config.get("secret_names") if isinstance(auth_config, dict) else {}
        if not isinstance(secret_names, dict):
            return set()
        return {
            self._normalize_text(name)
            for name in secret_names.values()
            if self._normalize_text(name)
        }

    def _extension_connection_schemas(
        self,
        *,
        bl: Any,
        extension_id: str,
        connection_type: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_extension_id = self._normalize_text(extension_id)
        if not normalized_extension_id:
            return {}, {}

        extension_row: dict[str, Any] = {}
        if bl is not None and hasattr(bl, "get_extension"):
            try:
                candidate = bl.get_extension(normalized_extension_id)
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                extension_row = dict(candidate)

        if not extension_row:
            try:
                from core.infrastructure.extension_runtime.extension_runtime_helpers import (
                    load_extension_from_filesystem,
                )

                candidate = load_extension_from_filesystem(normalized_extension_id)
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                extension_row = dict(candidate)

        if not extension_row:
            return {}, {}

        try:
            from core.infrastructure.extension_runtime.extension_runtime import (
                normalize_extension_row_for_runtime,
            )

            normalized_candidate = normalize_extension_row_for_runtime(extension_row)
            normalized_row = (
                dict(normalized_candidate)
                if isinstance(normalized_candidate, dict)
                else dict(extension_row)
            )
        except Exception:
            normalized_row = dict(extension_row)

        compiled_manifest_raw = normalized_row.get("compiled_manifest")
        compiled_manifest = (
            dict(compiled_manifest_raw) if isinstance(compiled_manifest_raw, dict) else {}
        )
        contract_type_schemas = self._contract_connection_type_schemas(
            normalized_row=normalized_row,
            compiled_manifest=compiled_manifest,
            extension_id=normalized_extension_id,
            connection_type=connection_type,
        )
        if contract_type_schemas is not None:
            return contract_type_schemas

        schemas_payload = compiled_manifest.get("schemas")
        if not isinstance(schemas_payload, dict):
            schemas_payload = normalized_row.get("schemas")
        schemas = dict(schemas_payload) if isinstance(schemas_payload, dict) else {}

        config_schema = schemas.get("config")
        secrets_schema = schemas.get("secrets")
        return (
            dict(config_schema) if isinstance(config_schema, dict) else {},
            dict(secrets_schema) if isinstance(secrets_schema, dict) else {},
        )

    def _contract_connection_type_schemas(
        self,
        *,
        normalized_row: dict[str, Any],
        compiled_manifest: dict[str, Any],
        extension_id: str,
        connection_type: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        normalized_connection_type = self._normalize_text(connection_type)
        descriptors: list[dict[str, Any]] = []
        for candidate in (
            normalized_row.get("manifest"),
            normalized_row,
            compiled_manifest,
        ):
            if isinstance(candidate, dict) and candidate:
                descriptors.append(dict(candidate))

        bundle_root = self._normalize_text(
            compiled_manifest.get("bundle_root") or normalized_row.get("bundle_root")
        )
        for descriptor in descriptors:
            try:
                from core.application.extensions.extension_contract_v2 import (
                    validate_extension_contract_v2,
                )

                validation = validate_extension_contract_v2(
                    descriptor,
                    bundle_root=bundle_root or None,
                )
            except Exception:
                continue

            if not validation.get("present"):
                continue
            if not validation.get("valid"):
                if normalized_connection_type:
                    errors = [
                        str((row or {}).get("message") or "").strip()
                        for row in (validation.get("errors") or [])
                        if str((row or {}).get("message") or "").strip()
                    ]
                    detail = "; ".join(errors[:4]) or "invalid Extension Contract v2"
                    raise ServiceValidationError(
                        f"Connection payload for extension '{extension_id}' failed schema validation: {detail}"
                    )
                return None

            contract = validation.get("normalized_contract")
            connection_types = (
                list(contract.get("connection_types") or []) if isinstance(contract, dict) else []
            )
            if not connection_types:
                return None

            selected: dict[str, Any] | None = None
            if normalized_connection_type:
                for candidate in connection_types:
                    if normalized_connection_type in {
                        self._normalize_text(candidate.get("connection_type_id")),
                        self._normalize_text(candidate.get("connection_type")),
                    }:
                        selected = dict(candidate)
                        break
                if selected is None:
                    raise ServiceValidationError(
                        f"Connection payload for extension '{extension_id}' failed schema validation: "
                        f"unknown connection_type '{normalized_connection_type}'"
                    )
            elif len(connection_types) == 1:
                selected = dict(connection_types[0])
            else:
                raise ServiceValidationError(
                    f"Connection payload for extension '{extension_id}' failed schema validation: "
                    "connection_type is required for extensions with multiple Contract v2 connection types"
                )

            config_schema = selected.get("config_schema")
            secrets_schema = selected.get("secret_schema")
            auth = selected.get("auth")
            auth_type = (
                self._normalize_text(auth.get("type")).lower() if isinstance(auth, dict) else ""
            )
            if auth_type == "oauth2":
                secret_names = auth.get("secret_names") if isinstance(auth, dict) else {}
                oauth_secret_properties = {
                    self._normalize_text(name): {"type": "string"}
                    for name in (secret_names.values() if isinstance(secret_names, dict) else [])
                    if self._normalize_text(name)
                }
                if isinstance(secrets_schema, dict):
                    secrets_schema = dict(secrets_schema)
                    properties = secrets_schema.get("properties")
                    properties_map = dict(properties) if isinstance(properties, dict) else {}
                    properties_map.update(oauth_secret_properties)
                    secrets_schema["properties"] = properties_map
                else:
                    secrets_schema = {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": oauth_secret_properties,
                    }
            return (
                dict(config_schema) if isinstance(config_schema, dict) else {},
                dict(secrets_schema) if isinstance(secrets_schema, dict) else {},
            )

        return None

    def _validate_secret_refs_schema(
        self,
        *,
        schema: dict[str, Any],
        secret_refs: list[str],
    ) -> list[str]:
        errors: list[str] = []
        if not schema:
            return errors

        declared_types: set[str] = set()
        raw_type = schema.get("type")
        if isinstance(raw_type, str):
            normalized_type = raw_type.strip().lower()
            if normalized_type:
                declared_types.add(normalized_type)
        elif isinstance(raw_type, list):
            declared_types = {
                str(item).strip().lower()
                for item in raw_type
                if isinstance(item, str) and str(item).strip()
            }
        if declared_types and "object" not in declared_types:
            errors.append("schemas.secrets: expected schema type object")
            return errors

        required_keys = {
            str(item or "").strip()
            for item in (schema.get("required") or [])
            if str(item or "").strip()
        }
        properties = schema.get("properties")
        properties_map = dict(properties) if isinstance(properties, dict) else {}
        allowed_keys = {str(key or "").strip() for key in properties_map if str(key or "").strip()}
        provided_keys = set(secret_refs)

        for missing_key in sorted(required_keys - provided_keys):
            errors.append(f"secret_refs: missing required secret '{missing_key}'")

        additional_properties = schema.get("additionalProperties", True)
        if additional_properties is False:
            for unknown_key in sorted(provided_keys - allowed_keys):
                errors.append(f"secret_refs: unknown secret '{unknown_key}'")

        return errors

    def _validate_connection_contract_against_extension_schemas(
        self,
        *,
        bl: Any,
        extension_id: str | None,
        connection_type: str = "",
        connection_config: Any,
        secret_refs: Any,
    ) -> None:
        normalized_extension_id = self._normalize_text(extension_id)

        normalized_config = dict(connection_config) if isinstance(connection_config, dict) else {}
        if "oauth_auth" in normalized_config:
            raise ServiceValidationError(
                "connection_config.oauth_auth is platform-owned and must be declared by the extension contract"
            )
        if not normalized_extension_id:
            return

        raw_secret_refs = self._normalize_secret_refs(secret_refs)
        normalized_secret_refs = self._secret_ref_names(raw_secret_refs)

        errors: list[str] = []
        refs_map = raw_secret_refs.get("refs")
        if refs_map is not None and not isinstance(refs_map, dict):
            errors.append("secret_refs.refs: expected object")

        config_schema, secrets_schema = self._extension_connection_schemas(
            bl=bl,
            extension_id=normalized_extension_id,
            connection_type=connection_type,
        )

        if config_schema:
            _json_schema_validate_value(
                schema=config_schema,
                value=normalized_config,
                path="connection_config",
                errors=errors,
            )
        if secrets_schema:
            errors.extend(
                self._validate_secret_refs_schema(
                    schema=secrets_schema,
                    secret_refs=normalized_secret_refs,
                )
            )

        if errors:
            detail = "; ".join(errors[:8]).strip()
            raise ServiceValidationError(
                f"Connection payload for extension '{normalized_extension_id}' failed schema validation: {detail}"
            )
