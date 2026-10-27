from __future__ import annotations

from core.entrypoints.api.route_bindings import build_global_search_service


def test_build_global_search_service_uses_shared_logger_callable() -> None:
    marker = object()

    def _get_logger_only():
        return marker

    service = build_global_search_service(get_logger_only=_get_logger_only)

    assert service._get_logger is _get_logger_only
    assert service._get_logger() is marker
