"""Startup extension synchronization composition regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.infrastructure.runtime import http_app_lifecycle


def test_startup_reuses_outer_store_and_scanned_rows_without_nested_rescan() -> None:
    calls: list[tuple[Any, list[dict[str, Any]]]] = []
    reconciled: list[Any] = []
    outer_store = object()
    scanned = [{"extension_key": "fixture.extension"}]

    class ExtensionBindingService:
        def scan_extensions(self, **_kwargs: Any) -> None:
            raise AssertionError("startup must not reacquire the extension lifecycle lock")

        def sync_extension_scan(
            self,
            rows: list[dict[str, Any]],
            *,
            _bl: Any,
        ) -> str:
            calls.append((_bl, rows))
            return "synced"

    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_container=SimpleNamespace(
                service_factories={
                    "extension_binding_service": lambda: ExtensionBindingService(),
                }
            )
        )
    )

    result = http_app_lifecycle._sync_extension_scan_for_app(
        app,
        outer_store,
        scanned,
        reconcile_extension_providers=lambda store: reconciled.append(store) or [],
    )

    assert result == "synced"
    assert calls == [(outer_store, scanned)]
    assert reconciled == [outer_store]


def test_startup_does_not_swallow_unexpected_reconcile_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        http_app_lifecycle,
        "_sync_extension_scan_without_providers",
        lambda *_args: "synced",
    )

    def unexpected_failure(_store: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unexpected reconcile failure")

    _raises_input_64_1 = SimpleNamespace()
    _raises_input_64_2 = object()
    with pytest.raises(RuntimeError, match="unexpected reconcile failure"):
        http_app_lifecycle._sync_extension_scan_for_app(
            _raises_input_64_1,
            _raises_input_64_2,
            [],
            reconcile_extension_providers=unexpected_failure,
        )
