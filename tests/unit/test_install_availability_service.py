from __future__ import annotations

from contextlib import contextmanager

from core.application.workflows.install_availability_service import InstallAvailabilityService


class _RegistryCursor:
    def __init__(self) -> None:
        self.description = []
        self._rows = []

    def execute(self, query, _params) -> None:
        if "FROM extension_installs" in query:
            self.description = [
                ("install_id",),
                ("account_id",),
                ("extension_id",),
                ("state",),
                ("runtime_status",),
            ]
            self._rows = [("inst-1", "acc-1", "extension.demo", "active", "compatible")]
        else:
            self.description = [("install_id",), ("account_id",), ("status",)]
            self._rows = [("inst-1", "acc-1", "trial_active")]

    def fetchall(self):
        return self._rows


class _RegistryStore:
    def _table_exists(self, _table_name: str) -> bool:
        return True

    @contextmanager
    def _cursor(self):
        yield _RegistryCursor()


def test_install_availability_returns_removed_when_install_missing() -> None:
    service = InstallAvailabilityService(
        get_logger=lambda: None,
        fetch_install_rows=lambda _ids, _account_id: {},
        fetch_entitlement_rows=lambda _ids, _account_id: {},
    )

    payload = service.get_install_availability("inst-1", account_id="acc-1")

    assert payload["availability_status"] == "removed"
    assert payload["availability_reason_code"] == "install_uninstalled"
    assert payload["new_runs_blocked"] is True
    assert payload["visible_in_catalog"] is False


def test_install_availability_prioritizes_uninstall_pending_drain() -> None:
    service = InstallAvailabilityService(
        get_logger=lambda: None,
        fetch_install_rows=lambda _ids, _account_id: {
            "inst-1": {
                "install_id": "inst-1",
                "state": "uninstall_pending_drain",
                "extension_id": "extension.demo",
            }
        },
        fetch_entitlement_rows=lambda _ids, _account_id: {
            "inst-1": {"install_id": "inst-1", "status": "active"}
        },
    )

    payload = service.get_install_availability("inst-1", account_id="acc-1")

    assert payload["availability_status"] == "draining"
    assert payload["availability_reason_code"] == "install_uninstall_pending_drain"
    assert payload["new_runs_blocked"] is True


def test_install_availability_blocks_for_entitlement_state() -> None:
    service = InstallAvailabilityService(
        get_logger=lambda: None,
        fetch_install_rows=lambda _ids, _account_id: {
            "inst-1": {"install_id": "inst-1", "state": "active", "extension_id": "extension.demo"}
        },
        fetch_entitlement_rows=lambda _ids, _account_id: {
            "inst-1": {"install_id": "inst-1", "status": "trial_expired"}
        },
    )

    payload = service.get_install_availability("inst-1", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "trial_expired"
    assert payload["new_runs_blocked"] is True


def test_install_availability_returns_available_when_all_checks_pass() -> None:
    service = InstallAvailabilityService(
        get_logger=lambda: None,
        fetch_install_rows=lambda _ids, _account_id: {
            "inst-1": {
                "install_id": "inst-1",
                "state": "active",
                "extension_id": "extension.demo",
                "runtime_status": "compatible",
                "health_status": "ok",
                "handshake_status": "ok",
            }
        },
        fetch_entitlement_rows=lambda _ids, _account_id: {
            "inst-1": {"install_id": "inst-1", "status": "active"}
        },
    )

    payload = service.get_install_availability("inst-1", account_id="acc-1")

    assert payload["availability_status"] == "available"
    assert payload["availability_reason_code"] == "none"
    assert payload["new_runs_blocked"] is False
    assert payload["visible_in_catalog"] is True


def test_install_availability_queries_registry_store_cursor_without_conn_attribute() -> None:
    service = InstallAvailabilityService(get_logger=_RegistryStore)

    payload = service.get_install_availability("inst-1", account_id="acc-1")

    assert payload["availability_status"] == "available"
    assert payload["extension_id"] == "extension.demo"
