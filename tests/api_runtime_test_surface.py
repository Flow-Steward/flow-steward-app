# ruff: noqa: I001, RUF100
"""Read-only pytest import surface for API runtime helper unit tests."""

from core.entrypoints.api.app import app, create_app  # noqa: F401

from core.infrastructure.runtime.http_app_imports import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_constants import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_request_helpers import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_lifecycle import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_auth_runtime import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_runtime_helpers import *  # noqa: F403,F401  # NOSONAR
from core.entrypoints.api.runtime_wiring import *  # noqa: F403,F401  # NOSONAR
from core.entrypoints.api.route_registration import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_indexing_workflows import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_reactions_extensions import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_scheduled_jobs import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime.http_app_events_resume import *  # noqa: F403,F401  # NOSONAR


def _external_auth_enabled() -> bool:
    """Default external-auth state for tests that replace this explicit patch seam."""
    return False


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
