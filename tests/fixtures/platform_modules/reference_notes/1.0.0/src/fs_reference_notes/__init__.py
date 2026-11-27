"""Reference platform module: account-scoped notes (ADR 0082 reference module).

It exists to exercise the platform-module mechanism end to end: an API under the
module's own tree guarded by a feature, contributed route permission rules, a
host-rendered settings page and a table in the module's own schema.

This package root imports only the standard library. The worker and control
images ship a slice of Core and may lack the web stack, so the HTTP surface is
imported lazily inside :func:`register`, which only the app role calls.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)


def register(app: Any, container: Any) -> None:
    """App role entrypoint: mount the notes API and its settings page."""
    from fs_reference_notes.api import install

    install(app, container)


def register_worker(context: Mapping[str, Any]) -> None:
    """Worker role entrypoint: the module has no background work, it only reports."""
    _log_role(context, "worker")


def register_control(context: Mapping[str, Any]) -> None:
    """Control role entrypoint: the module has no control work, it only reports."""
    _log_role(context, "control")


def _log_role(context: Mapping[str, Any], fallback_role: str) -> None:
    identity = context.get("platform_module") or {}
    _logger.info(
        "Platform module %s %s registered for role %s",
        identity.get("id", "?"),
        identity.get("version", "?"),
        context.get("role") or fallback_role,
    )
