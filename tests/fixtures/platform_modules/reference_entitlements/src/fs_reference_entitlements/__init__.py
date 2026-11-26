"""Reference platform module: entitlements read from a file (ADR 0082 reference module).

It stands in for whatever supplies entitlements on a real deployment. Core stays
the only decider; this module only registers an ``EntitlementSource`` that reads
``<platform modules root>/reference-entitlements.json``::

    {
      "accounts": {"<account id>": {"features": ["..."], "limits": {"name": 3}}},
      "default": {"features": [], "limits": {}}
    }

The file is read again whenever its modification time, size or inode changes, so
an edit takes effect without a restart. A missing or malformed file entitles
nothing.
"""

from __future__ import annotations

from typing import Any

ENTITLEMENTS_FILENAME = "reference-entitlements.json"


def register(app: Any, container: Any) -> None:
    """App role entrypoint: register the file-backed entitlement source."""
    del app
    from fs_reference_entitlements.source import FileEntitlementSource, default_entitlements_path

    source = FileEntitlementSource(default_entitlements_path())
    container.service_factories["entitlement_service"]().register_entitlement_source(source)
