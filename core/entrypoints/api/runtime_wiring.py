# ruff: noqa: F401, F405, I001, RUF100
# mypy: ignore-errors
from core.infrastructure.runtime.http_app_runtime_helpers import *  # noqa: F403,F401  # NOSONAR

from core.application.jobs.submission_admission import public_submission_pause_active


_compute_readiness_with_dependencies = _compute_readiness


def _compute_readiness(bl) -> dict:
    return _compute_readiness_with_dependencies(
        bl,
        lifecycle_pause_active_fn=public_submission_pause_active,
    )


_redirect = lambda url, flash=None, flash_type="info": _serve_request_runtime.redirect_with_flash(  # noqa: E731
    url,
    flash=flash,
    flash_type=flash_type,
    redirect_response_cls=RedirectResponse,
)


def _write_project_profile_to_kb(project_row: dict) -> None:
    from core.application.management.project_profile_kb import write_project_profile_to_kb
    from core.application.memory.project_context import sync_project_context

    return write_project_profile_to_kb(
        project_row,
        logger=logger,
        write_project_profile=sync_project_context,
    )


from core.entrypoints.admin import deps as _route_deps  # noqa: E402
from core.entrypoints.admin.ui_route_bindings import (  # noqa: E402
    register_ui_routes as _register_ui_routes,
)
from core.entrypoints.api.auth_route_bindings import (  # noqa: E402
    register_auth_routes as _register_auth_routes,
)
from core.entrypoints.api.events_stream import router as _events_stream_router  # noqa: E402
from core.entrypoints.api.health import router as _health_router  # noqa: E402
from core.entrypoints.api.route_bindings import (  # noqa: E402
    register_api_routes as _register_api_routes,
)
from core.entrypoints.api.security import router as _security_router  # noqa: E402
from core.entrypoints.api.workload import router as _workload_router  # noqa: E402

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
