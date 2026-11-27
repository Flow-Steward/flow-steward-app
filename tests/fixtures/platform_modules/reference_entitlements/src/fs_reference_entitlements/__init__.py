"""Reference platform module: entitlements read from a file (ADR 0082 reference module).

It stands in for whatever supplies entitlements on a real deployment. Core stays
the only decider; this module only registers an ``EntitlementSource`` that reads
``<platform modules root>/reference-entitlements.json``::

    {
      "accounts": {"<account id>": {"features": ["..."], "limits": {"name": 3}}},
      "default": {"features": [], "limits": {}}
    }

The file is read again whenever its modification time, size or inode changes, so
an edit takes effect without a restart. A missing file entitles nothing; an
unreadable or malformed file makes the source fail, which Core answers as "not
entitled" for access and as "unknown" where an unknown answer must not delete
anything (the approval journal retention pass).

The source is registered in the app role (``register``) and in the control role
(``register_control``), whose retention pass asks it whether an account keeps
approval journal rows past their jobs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

ENTITLEMENTS_FILENAME = "reference-entitlements.json"

_logger = logging.getLogger(__name__)


def _file_source() -> Any:
    from fs_reference_entitlements.source import FileEntitlementSource, default_entitlements_path

    return FileEntitlementSource(default_entitlements_path())


def register(app: Any, container: Any) -> None:
    """App role entrypoint: register the file-backed entitlement source."""
    del app
    container.service_factories["entitlement_service"]().register_entitlement_source(_file_source())


def register_control(context: Mapping[str, Any]) -> None:
    """Control role entrypoint: register the same source into the role's entitlement service."""
    service = context.get("entitlement_service")
    if service is None:
        _logger.warning(
            "Platform module %s: the control role offers no entitlement service; "
            "no entitlement source registered",
            context.get("platform_module", {}).get("id"),
        )
        return
    service.register_entitlement_source(_file_source())
