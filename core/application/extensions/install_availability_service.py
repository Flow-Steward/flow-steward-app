"""Install availability resolution for workflow launch gating and UI annotation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

_ENTITLEMENT_BLOCKED = {"trial_expired", "suspended_nonpayment", "disabled"}

_REASON_TEXT = {
    "none": "",
    "trial_expired": "Install trial has expired.",
    "suspended_nonpayment": "Install is suspended due to nonpayment.",
    "disabled": "Install entitlement is disabled.",
    "install_disabled": "Install is disabled.",
    "install_uninstall_pending_drain": "Install is draining before uninstall.",
    "install_uninstalled": "Install has been uninstalled.",
    "install_incompatible": "Install runtime is incompatible.",
    "install_health_failed": "Install health check failed.",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _row_to_dict(row: Any, description: Any | None = None) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if description is None:
        return {}
    cols = [col[0] if hasattr(col, "__getitem__") else col.name for col in description]
    return dict(zip(cols, row, strict=False))


class InstallAvailabilityService:
    """Resolve install-level availability from install + entitlement + runtime state."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        fetch_install_rows: Callable[[set[str], str], dict[str, dict[str, Any]]] | None = None,
        fetch_entitlement_rows: Callable[[set[str], str], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._fetch_install_rows = fetch_install_rows
        self._fetch_entitlement_rows = fetch_entitlement_rows

    def _table_exists(self, bl: Any, table_name: str) -> bool:
        if bl is None:
            return False
        if hasattr(bl, "_table_exists"):
            try:
                return bool(bl._table_exists(table_name))
            except Exception:
                return False
        conn = getattr(bl, "conn", None)
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (table_name,))
                row = cur.fetchone()
                if row is None:
                    return False
                if isinstance(row, dict):
                    return bool(next(iter(row.values()), None))
                return bool(row[0])
        except Exception:
            return False

    def _query_install_rows(
        self,
        install_ids: set[str],
        *,
        account_id: str,
    ) -> dict[str, dict[str, Any]]:
        bl = self._get_logger()
        conn = getattr(bl, "conn", None) if bl is not None else None
        cursor_factory = getattr(bl, "_cursor", None) if bl is not None else None
        if (
            (conn is None and not callable(cursor_factory))
            or not self._table_exists(bl, "extension_installs")
            or not install_ids
        ):
            return {}

        placeholders = ", ".join(["%s"] * len(install_ids))
        params: list[Any] = [*sorted(install_ids)]
        account_filter = ""
        normalized_account_id = _normalize_text(account_id)
        if normalized_account_id:
            account_filter = " AND account_id = %s"
            params.append(normalized_account_id)

        if callable(cursor_factory):
            cursor_context = cursor_factory()
        else:
            assert conn is not None
            cursor_context = conn.cursor()
        with cursor_context as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM extension_installs
                 WHERE install_id IN ({placeholders})
                       {account_filter}
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            description = cur.description

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = _row_to_dict(row, description)
            install_id = _normalize_text(payload.get("install_id"))
            if install_id:
                result[install_id] = payload
        return result

    def _query_entitlement_rows(
        self,
        install_ids: set[str],
        *,
        account_id: str,
    ) -> dict[str, dict[str, Any]]:
        bl = self._get_logger()
        conn = getattr(bl, "conn", None) if bl is not None else None
        cursor_factory = getattr(bl, "_cursor", None) if bl is not None else None
        if (
            (conn is None and not callable(cursor_factory))
            or not self._table_exists(bl, "extension_entitlements")
            or not install_ids
        ):
            return {}

        placeholders = ", ".join(["%s"] * len(install_ids))
        params: list[Any] = [*sorted(install_ids)]
        account_filter = ""
        normalized_account_id = _normalize_text(account_id)
        if normalized_account_id:
            account_filter = " AND account_id = %s"
            params.append(normalized_account_id)

        if callable(cursor_factory):
            cursor_context = cursor_factory()
        else:
            assert conn is not None
            cursor_context = conn.cursor()
        with cursor_context as cur:
            cur.execute(
                f"""
                SELECT *
                  FROM extension_entitlements
                 WHERE install_id IN ({placeholders})
                       {account_filter}
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            description = cur.description

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = _row_to_dict(row, description)
            install_id = _normalize_text(payload.get("install_id"))
            if install_id:
                result[install_id] = payload
        return result

    @staticmethod
    def _result(
        *,
        install_id: str,
        account_id: str,
        extension_id: str,
        state: str,
        entitlement_status: str,
        availability_status: str,
        reason_code: str,
        visible_in_catalog: bool,
    ) -> dict[str, Any]:
        normalized_availability = _normalize_lower(availability_status) or "unavailable"
        normalized_reason_code = _normalize_lower(reason_code) or "none"
        return {
            "install_id": _normalize_text(install_id),
            "account_id": _normalize_text(account_id),
            "extension_id": _normalize_text(extension_id),
            "state": _normalize_lower(state),
            "entitlement_status": _normalize_lower(entitlement_status),
            "availability_status": normalized_availability,
            "availability_reason_code": normalized_reason_code,
            "availability_reason_text": _REASON_TEXT.get(normalized_reason_code, ""),
            "new_runs_blocked": normalized_availability != "available",
            "visible_in_catalog": bool(visible_in_catalog),
        }

    @classmethod
    def _evaluate_install_availability(
        cls,
        *,
        install_id: str,
        account_id: str,
        install_row: dict[str, Any] | None,
        entitlement_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        install_payload = dict(install_row or {})
        entitlement_payload = dict(entitlement_row or {})
        extension_id = _normalize_text(install_payload.get("extension_id"))
        state = _normalize_lower(install_payload.get("state") or "active")
        entitlement_status = _normalize_lower(entitlement_payload.get("status"))
        runtime_status = _normalize_lower(install_payload.get("runtime_status"))
        health_status = _normalize_lower(install_payload.get("health_status"))
        handshake_status = _normalize_lower(install_payload.get("handshake_status"))

        if not install_payload:
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state="uninstalled",
                entitlement_status=entitlement_status,
                availability_status="removed",
                reason_code="install_uninstalled",
                visible_in_catalog=False,
            )
        if state == "uninstalled" or install_payload.get("uninstalled_at") is not None:
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="removed",
                reason_code="install_uninstalled",
                visible_in_catalog=False,
            )
        if state == "uninstall_pending_drain":
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="draining",
                reason_code="install_uninstall_pending_drain",
                visible_in_catalog=True,
            )
        if entitlement_status in _ENTITLEMENT_BLOCKED:
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="unavailable",
                reason_code=entitlement_status,
                visible_in_catalog=True,
            )
        if state == "disabled":
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="unavailable",
                reason_code="install_disabled",
                visible_in_catalog=True,
            )
        if runtime_status == "incompatible":
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="unavailable",
                reason_code="install_incompatible",
                visible_in_catalog=True,
            )
        if health_status == "failed" or handshake_status == "failed":
            return cls._result(
                install_id=install_id,
                account_id=account_id,
                extension_id=extension_id,
                state=state,
                entitlement_status=entitlement_status,
                availability_status="unavailable",
                reason_code="install_health_failed",
                visible_in_catalog=True,
            )
        return cls._result(
            install_id=install_id,
            account_id=account_id,
            extension_id=extension_id,
            state=state or "active",
            entitlement_status=entitlement_status,
            availability_status="available",
            reason_code="none",
            visible_in_catalog=True,
        )

    def batch_install_availability(
        self,
        install_ids: Iterable[str],
        *,
        account_id: str,
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = {_normalize_text(item) for item in install_ids if _normalize_text(item)}
        if not normalized_ids:
            return {}

        if self._fetch_install_rows is not None:
            install_rows = self._fetch_install_rows(normalized_ids, _normalize_text(account_id))
        else:
            install_rows = self._query_install_rows(
                normalized_ids, account_id=_normalize_text(account_id)
            )

        if self._fetch_entitlement_rows is not None:
            entitlement_rows = self._fetch_entitlement_rows(
                normalized_ids, _normalize_text(account_id)
            )
        else:
            entitlement_rows = self._query_entitlement_rows(
                normalized_ids, account_id=_normalize_text(account_id)
            )

        resolved: dict[str, dict[str, Any]] = {}
        for install_id in sorted(normalized_ids):
            install_row = install_rows.get(install_id)
            entitlement_row = entitlement_rows.get(install_id)
            resolved[install_id] = self._evaluate_install_availability(
                install_id=install_id,
                account_id=_normalize_text(
                    (install_row or {}).get("account_id")
                    or (entitlement_row or {}).get("account_id")
                    or account_id
                ),
                install_row=install_row,
                entitlement_row=entitlement_row,
            )
        return resolved

    def get_install_availability(
        self,
        install_id: str,
        *,
        account_id: str,
    ) -> dict[str, Any]:
        normalized_install_id = _normalize_text(install_id)
        if not normalized_install_id:
            return self._evaluate_install_availability(
                install_id="",
                account_id=_normalize_text(account_id),
                install_row=None,
                entitlement_row=None,
            )
        return self.batch_install_availability(
            [normalized_install_id],
            account_id=_normalize_text(account_id),
        ).get(normalized_install_id, {})
