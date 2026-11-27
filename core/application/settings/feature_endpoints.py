"""Where the browser finds the API of a feature a platform module delivers (ADR 0082).

Core's own screens carry the controls of such a feature (section 3a) but not its
API: the module that delivers the feature registers the base path of its routes
here, and the bootstrap payload sends it as ``feature_endpoints`` for the features
the caller's account is entitled to. A control whose endpoint is absent stays
hidden even when its feature flag is true.

A platform module registers from its ``register`` entrypoint, inside the
registration scope Core's module loader opens; the base path must lie in that
module's own API tree, ``/api/modules/<module id>/``. Registration closes when the
composition root seals the page registries. A feature has one endpoint: a second
registration of the same feature is refused, whoever makes it. The endpoints of a
module whose loading fails are rolled back before sealing.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping

from core.application.auth.route_permissions import module_api_prefix
from core.application.reports.ui_page_registry import (
    PageRegistrySealedError,
    current_registration_module,
)
from core.application.settings.platform_contracts import normalize_entitlement_name

_BASE_PATH = re.compile(r"^/api/modules/[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)+$")


class FeatureEndpointRegistry:
    """Feature name -> base URL of the module API that serves it; sealed after loading."""

    __slots__ = ("_endpoints", "_lock", "_sealed")

    def __init__(self) -> None:
        self._endpoints: dict[str, str] = {}
        self._lock = threading.Lock()
        self._sealed = False

    def register(self, feature: str, base_path: str) -> None:
        """Record where ``feature``'s API lives.

        Raises :class:`PageRegistrySealedError` after sealing and ``ValueError`` for
        a caller outside a prepared module's registration scope, a path outside the
        registering module's ``/api/modules/<module id>/`` tree, or a feature that
        already has an endpoint.
        """
        name = normalize_entitlement_name(feature)
        if not name:
            raise ValueError("a feature endpoint needs a feature name")
        module_id = current_registration_module()
        if not module_id:
            raise ValueError(
                f"feature endpoint {name!r} must be registered by a platform module "
                "from its register entrypoint"
            )
        path = str(base_path or "").strip().rstrip("/")
        prefix = module_api_prefix(module_id)
        if not path.startswith(prefix) or not _BASE_PATH.match(path) or ".." in path:
            raise ValueError(
                f"feature endpoint {name!r} must lie under {prefix}, the registering "
                "module's own API tree"
            )
        with self._lock:
            if self._sealed:
                raise PageRegistrySealedError(
                    f"feature endpoint {name!r} cannot be registered: registration closed "
                    "when startup finished"
                )
            existing = self._endpoints.get(name)
            if existing is not None:
                raise ValueError(f"feature {name!r} already has an endpoint: {existing}")
            self._endpoints[name] = path

    def seal(self) -> None:
        """Refuse every later registration. Idempotent."""
        with self._lock:
            self._sealed = True

    def loading_checkpoint(self) -> dict[str, str]:
        """The registered endpoints, for the module loader's rollback."""
        return self.snapshot()

    def restore_loading_checkpoint(self, checkpoint: Mapping[str, str]) -> None:
        """Drop every endpoint registered after ``checkpoint`` was taken.

        Only Core's module loader calls it, before sealing, when a platform module
        fails to load: a feature of a failed module never gets an endpoint.
        """
        with self._lock:
            self._endpoints = {
                name: path for name, path in self._endpoints.items() if name in checkpoint
            }

    @property
    def sealed(self) -> bool:
        return self._sealed

    def snapshot(self) -> dict[str, str]:
        """Every registered feature and its base path."""
        with self._lock:
            return dict(self._endpoints)


def entitled_feature_endpoints(
    endpoints: Mapping[str, str], feature_flags: Mapping[str, object]
) -> dict[str, str]:
    """The endpoints of the features ``feature_flags`` marks entitled (true), only."""
    return {
        feature: path
        for feature, path in sorted(endpoints.items())
        if feature_flags.get(feature) is True and isinstance(path, str) and path
    }


__all__ = ["FeatureEndpointRegistry", "entitled_feature_endpoints"]
