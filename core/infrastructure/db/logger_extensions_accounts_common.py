from __future__ import annotations

import uuid
from typing import Any

ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL = "account_id = EXCLUDED.account_id"
ACCOUNT_ID_IS_REQUIRED_MESSAGE = "account_id is required"
REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL = (
    "required_scopes_jsonb = EXCLUDED.required_scopes_jsonb"
)

SQL_AND = " AND "
SQL_ACCOUNT_ID_EQ = "account_id = %s"
SQL_JOB_ID_EQ = "job_id = %s"


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _as_dict(value):
    return _helpers()._as_dict(value)


def _as_list(value) -> list:
    return _helpers()._as_list(value)


def _coerce_bool(value, default: bool = False) -> bool:
    return _helpers()._coerce_bool(value, default=default)


def _coerce_int(value, default: int) -> int:
    return _helpers()._coerce_int(value, default)


def _default_account_id() -> str:
    return _helpers()._default_account_id()


def _normalize_int_list(value) -> list[str]:
    return _helpers()._normalize_int_list(value)


def _normalize_string_list(value) -> list[str]:
    return _helpers()._normalize_string_list(value)


def _redact_payload(payload: dict, *, max_depth: int = 8) -> dict:
    return _helpers()._redact_payload(payload, max_depth=max_depth)


def _scopes_within(parent_scopes: list[str], child_scopes: list[str]) -> bool:
    return _helpers()._scopes_within(parent_scopes, child_scopes)


def _trace_id_for_job(job_id) -> str | None:
    return _helpers()._trace_id_for_job(job_id)


def _trace_json_value(value, *, default):
    return _helpers()._trace_json_value(value, default=default)


class PlatformLoggerExtensionsAccountsCommonMixin:
    conn: Any
    _table_exists: Any
    _column_exists: Any

    def _accounts_identity_column(self) -> str:
        try:
            if self._column_exists("accounts", "account_id"):
                return "account_id"
            if self._column_exists("accounts", "id"):
                return "id"
        except Exception:
            return "account_id"
        return "account_id"

    def _accounts_identity_expr(self) -> str:
        return self._accounts_identity_column()

    def _account_lookup_filter(self, account_id: str) -> tuple[str, tuple[Any, ...]]:
        normalized_account_id = str(account_id or "").strip()
        identity_column = self._accounts_identity_column()
        if identity_column != "id":
            return "account_id = %s", (normalized_account_id,)
        try:
            return "id = %s", (str(uuid.UUID(normalized_account_id)),)
        except (AttributeError, TypeError, ValueError):
            try:
                if self._column_exists("accounts", "account_key"):
                    return "account_key = %s", (normalized_account_id,)
            except Exception:
                pass
            return "id::text = %s", (normalized_account_id,)

    def _account_is_active(self, account_id: str) -> bool:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return False
        if not self._table_exists("accounts"):
            return True
        identity_column = self._accounts_identity_column()
        with self.conn.cursor() as cur:
            if identity_column == "account_id":
                cur.execute(
                    """
                    SELECT COALESCE(is_active, true) AS is_active
                    FROM accounts
                    WHERE account_id = %s
                    LIMIT 1
                    """,
                    (normalized_account_id,),
                )
            else:
                try:
                    account_uuid = str(uuid.UUID(normalized_account_id))
                except (AttributeError, TypeError, ValueError):
                    if self._column_exists("accounts", "account_key"):
                        cur.execute(
                            """
                            SELECT COALESCE(is_active, true) AS is_active
                            FROM accounts
                            WHERE account_key = %s
                            LIMIT 1
                            """,
                            (normalized_account_id,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT COALESCE(is_active, true) AS is_active
                            FROM accounts
                            WHERE id::text = %s
                            LIMIT 1
                            """,
                            (normalized_account_id,),
                        )
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(is_active, true) AS is_active
                        FROM accounts
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (account_uuid,),
                    )
            row = cur.fetchone()
        if not row:
            return False
        if isinstance(row, dict):
            return bool(row.get("is_active", True))
        return bool(row[0])

    def _user_has_active_account_membership(self, user_id: str, account_id: str) -> bool:
        normalized_user_id = str(user_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        if not normalized_user_id or not normalized_account_id:
            return False
        if not self._table_exists("user_account_memberships"):
            return True
        if not self._account_is_active(normalized_account_id):
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM user_account_memberships
                WHERE user_id = %s
                  AND account_id = %s
                LIMIT 1
                """,
                (normalized_user_id, normalized_account_id),
            )
            return bool(cur.fetchone())

    @staticmethod
    def _normalize_extension_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["manifest"] = _as_dict(payload.get("manifest") or payload.get("manifest_jsonb"))
        payload["features"] = _normalize_string_list(
            payload.get("features") or payload.get("features_jsonb")
        )
        payload["required_scopes"] = _normalize_string_list(
            payload.get("required_scopes") or payload.get("required_scopes_jsonb")
        )
        payload["supported_events"] = _normalize_string_list(
            payload.get("supported_events") or payload.get("supported_events_jsonb")
        )
        payload["schemas"] = _as_dict(payload.get("schemas") or payload.get("schemas_jsonb"))
        payload["healthcheck"] = _as_dict(
            payload.get("healthcheck") or payload.get("healthcheck_jsonb")
        )
        payload["entrypoint"] = _as_dict(
            payload.get("entrypoint") or payload.get("entrypoint_jsonb")
        )
        payload["ui_manifest"] = _as_dict(
            payload.get("ui_manifest") or payload.get("ui_manifest_jsonb")
        )
        payload["query_definitions"] = _as_list(
            payload.get("query_definitions") or payload.get("query_definitions_jsonb")
        )
        payload["action_definitions"] = _as_list(
            payload.get("action_definitions") or payload.get("action_definitions_jsonb")
        )
        payload["compiled_manifest"] = _as_dict(
            payload.get("compiled_manifest") or payload.get("compiled_manifest_jsonb")
        )
        payload["kind"] = str(
            payload.get("kind")
            or payload["compiled_manifest"].get("kind")
            or payload["manifest"].get("kind")
            or ""
        ).strip()
        payload["compile_errors"] = _as_list(
            payload.get("compile_errors") or payload.get("compile_errors_jsonb")
        )
        payload["compile_warnings"] = _as_list(
            payload.get("compile_warnings") or payload.get("compile_warnings_jsonb")
        )
        payload["validation_errors"] = _as_list(
            payload.get("validation_errors") or payload.get("validation_errors_jsonb")
        )
        payload["validation_warnings"] = _as_list(
            payload.get("validation_warnings") or payload.get("validation_warnings_jsonb")
        )
        payload["machine_auth"] = _as_dict(
            payload.get("machine_auth") or payload.get("machine_auth_jsonb")
        )
        payload["account_scope"] = _as_dict(
            payload.get("account_scope") or payload.get("account_scope_jsonb")
        )
        payload["callbacks"] = _as_list(
            payload.get("callbacks")
            or payload.get("callbacks_jsonb")
            or payload["compiled_manifest"].get("callbacks")
        )
        payload["manifest_version"] = _coerce_int(payload.get("manifest_version"), 0)
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["enabled"] = _coerce_bool(payload.get("enabled"), False)
        payload["config"] = _as_dict(payload.get("config") or payload.get("config_jsonb"))
        payload["extension_id"] = str(
            payload.get("extension_id")
            or payload.get("extension_key")
            or payload["manifest"].get("extension_id")
            or payload.get("id")
            or ""
        ).strip()
        payload.pop("manifest_jsonb", None)
        payload.pop("features_jsonb", None)
        payload.pop("required_scopes_jsonb", None)
        payload.pop("supported_events_jsonb", None)
        payload.pop("schemas_jsonb", None)
        payload.pop("healthcheck_jsonb", None)
        payload.pop("entrypoint_jsonb", None)
        payload.pop("ui_manifest_jsonb", None)
        payload.pop("query_definitions_jsonb", None)
        payload.pop("action_definitions_jsonb", None)
        payload.pop("compiled_manifest_jsonb", None)
        payload.pop("compile_errors_jsonb", None)
        payload.pop("compile_warnings_jsonb", None)
        payload.pop("validation_errors_jsonb", None)
        payload.pop("validation_warnings_jsonb", None)
        payload.pop("machine_auth_jsonb", None)
        payload.pop("account_scope_jsonb", None)
        payload.pop("callbacks_jsonb", None)
        payload.pop("config_jsonb", None)
        return payload

    @staticmethod
    def _normalize_extension_callback_registration(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["auth_config"] = _as_dict(
            payload.get("auth_config") or payload.get("auth_config_jsonb")
        )
        payload["enabled"] = _coerce_bool(payload.get("enabled"), False)
        payload["id"] = str(payload.get("id") or "").strip()
        payload["extension_id"] = str(payload.get("extension_id") or "").strip()
        payload["callback_id"] = str(payload.get("callback_id") or "").strip()
        payload["callback_token_hash"] = str(payload.get("callback_token_hash") or "").strip()
        payload["project_id"] = str(payload.get("project_id") or "").strip()
        payload["connection_id"] = str(payload.get("connection_id") or "").strip() or None
        payload["auth_type"] = str(payload.get("auth_type") or "none").strip().lower() or "none"
        payload["last_error"] = str(payload.get("last_error") or "").strip()
        payload.pop("auth_config_jsonb", None)
        return payload

    @staticmethod
    def _normalize_extension_invocation_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["request_jsonb"] = _trace_json_value(payload.get("request_jsonb"), default={})
        payload["response_jsonb"] = _trace_json_value(payload.get("response_jsonb"), default={})
        payload["request_context_jsonb"] = _trace_json_value(
            payload.get("request_context_jsonb"), default={}
        )
        request_context = _as_dict(payload.get("request_context_jsonb"))
        if not str(request_context.get("trace_id") or "").strip():
            trace_id = _trace_id_for_job(payload.get("job_id"))
            if trace_id:
                request_context["trace_id"] = trace_id
        payload["request_context_jsonb"] = request_context
        return payload

    @staticmethod
    def _normalize_account_row(row: dict | None) -> dict:
        payload = dict(row or {})
        account_id = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["account_id"] = account_id
        payload["display_name"] = (
            str(payload.get("display_name") or account_id).strip() or account_id
        )
        payload["is_active"] = _coerce_bool(payload.get("is_active"), True)
        return payload

    @staticmethod
    def _normalize_service_account_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["scopes"] = _normalize_string_list(
            payload.get("scopes") or payload.get("scopes_jsonb")
        )
        payload["id"] = str(payload.get("id") or "").strip()
        payload["allowed_project_ids"] = _normalize_string_list(
            payload.get("allowed_project_ids") or payload.get("allowed_project_ids_jsonb")
        )
        payload["allowed_workflow_ids"] = _normalize_string_list(
            payload.get("allowed_workflow_ids") or payload.get("allowed_workflow_ids_jsonb")
        )
        payload["is_active"] = _coerce_bool(payload.get("is_active"), True)
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload.pop("scopes_jsonb", None)
        payload.pop("allowed_project_ids_jsonb", None)
        payload.pop("allowed_workflow_ids_jsonb", None)
        return payload

    @staticmethod
    def _normalize_machine_token_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["scopes"] = _normalize_string_list(
            payload.get("scopes") or payload.get("scopes_jsonb")
        )
        payload["id"] = str(payload.get("id") or "").strip()
        payload["service_account_id"] = str(payload.get("service_account_id") or "").strip()
        payload["allowed_project_ids"] = _normalize_string_list(
            payload.get("allowed_project_ids") or payload.get("allowed_project_ids_jsonb")
        )
        payload["allowed_workflow_ids"] = _normalize_string_list(
            payload.get("allowed_workflow_ids") or payload.get("allowed_workflow_ids_jsonb")
        )
        payload["revoked"] = _coerce_bool(payload.get("revoked"), False)
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload.pop("scopes_jsonb", None)
        payload.pop("allowed_project_ids_jsonb", None)
        payload.pop("allowed_workflow_ids_jsonb", None)
        payload.pop("token_hash", None)
        return payload
