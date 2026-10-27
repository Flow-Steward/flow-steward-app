"""Extension operations must run on a store that can persist what they write.

Actions and queries were handed the lifecycle store. That store resolves
installed extensions and provider secrets and deliberately exposes nothing
about project connections, notification routes, deliveries or ingress — so
every messaging-channel save answered `project_connection_storage_unavailable` in
production while the suite stayed green, because the fakes offered methods the
real store does not have.

These tests read the production classes and composition roots, not fixtures: a
double cannot be missing a method the real object lacks unless it is built from
the real object, and a wiring bug lives where the wiring is.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.infrastructure.db.platform_stores import (
    PlatformIdentityStore,
    PlatformIntegrationStore,
)

ROOT = Path(__file__).resolve().parents[2]

#: Everything extension operation execution has to be able to do, named in the
#: design as capabilities rather than conveniences. Provider secrets are listed
#: even though the identity store has them too: the operation store must carry
#: them itself, or moving operations onto it would break token save and getMe.
REQUIRED_OPERATION_STORE_METHODS = (
    # scope enforcement
    "get_project",
    # project connections
    "get_project_connection",
    "upsert_project_connection",
    # project-scoped provider secrets
    "save_provider_secret",
    "get_provider_secret",
    # notification routes and delivery logs
    "list_notification_routes",
    "save_notification_routes",
    "list_notification_delivery_logs",
    "log_notification_delivery",
    # redacted extension action audit records
    "log_action",
)

#: The subset the identity store must keep lacking for the split to be needed.
OPERATION_ONLY_METHODS = (
    "get_project",
    "get_project_connection",
    "upsert_project_connection",
    "list_notification_routes",
    "save_notification_routes",
    "list_notification_delivery_logs",
    "log_notification_delivery",
)


def test_the_identity_store_cannot_serve_extension_operations() -> None:
    """The premise. If this becomes false the separate store is unnecessary."""
    present = [name for name in OPERATION_ONLY_METHODS if hasattr(PlatformIdentityStore, name)]

    assert present == [], (
        f"the identity store now exposes {present}; revisit whether the boundary is still needed"
    )


@pytest.mark.parametrize("method_name", REQUIRED_OPERATION_STORE_METHODS)
def test_the_integration_store_implements_every_required_capability(method_name: str) -> None:
    """Checked against the real class, however the method reaches it. Provider
    secrets arrive transitively through the scheduled-providers mixin, which is
    exactly the kind of fact a reading of the base list gets wrong."""
    assert callable(getattr(PlatformIntegrationStore, method_name, None)), method_name


def test_a_double_shaped_like_the_identity_store_reproduces_the_failure() -> None:
    """The narrow double the design asks for: only what the identity store really
    has. An operation store built from it lacks every method a messaging-channel save
    needs, which is the production failure and not a fixture artefact."""
    identity_surface = {name for name in dir(PlatformIdentityStore) if not name.startswith("_")}
    double = type(
        "IdentityShapedStore", (), {name: (lambda *a, **k: None) for name in identity_surface}
    )()

    missing = [name for name in OPERATION_ONLY_METHODS if not hasattr(double, name)]

    assert missing == list(OPERATION_ONLY_METHODS), missing


def test_the_binding_service_takes_a_separate_operation_store() -> None:
    """Lifecycle and operations are different jobs with different needs, and one
    `get_logger` for both is what silently gave operations the wrong store."""
    from core.application.extensions.binding_service import ExtensionBindingService

    assert "get_operation_store" in inspect.signature(ExtensionBindingService.__init__).parameters


def test_the_operation_service_takes_the_operation_store() -> None:
    from core.application.extensions.operation_service import ExtensionOperationService

    assert "get_operation_store" in inspect.signature(ExtensionOperationService.__init__).parameters


def test_operations_receive_the_operation_store_not_the_lifecycle_one() -> None:
    """Wired end to end through the factory the composition roots use."""
    from core.application.management.command_service_runtime import (
        create_extension_binding_service,
    )

    lifecycle_store = object()
    operation_store = object()

    service = create_extension_binding_service(
        get_logger=lambda: lifecycle_store,
        get_operation_store=lambda: operation_store,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda _extension: "",
        extension_operation_handler_mode=lambda *_a, **_k: "extension",
        extension_operation_failure_status=lambda _extension: 500,
    )

    assert service._operation_service._get_operation_store() is operation_store
    assert service._get_logger() is lifecycle_store


def _wiring_block(source: str, start_marker: str) -> str:
    return source.split(start_marker, 1)[1].split("\n    )\n", 1)[0]


def test_the_cli_composition_gives_operations_the_integration_store() -> None:
    source = (ROOT / "core/entrypoints/cli/runtime.py").read_text(encoding="utf-8")
    binding = _wiring_block(source, "def extension_binding_service")

    assert "get_operation_store=self.get_integration_store" in binding
    assert "get_logger=self.get_identity_store" in binding, "lifecycle keeps the identity store"
    resolver = source.split("def _resolve_extension_runtime_record", 1)[1]
    assert "get_logger_only=self.get_integration_store" in resolver


def test_the_api_composition_gives_operations_the_integration_store() -> None:
    """The path the interface actually uses. Fixing only the CLI root would have
    left the browser on the store that cannot save a connection."""
    source = (ROOT / "core/entrypoints/api/app_factory.py").read_text(encoding="utf-8")
    binding = source.split("build_extension_binding_service_factory(", 1)[1].split(
        "extension_identifier=", 1
    )[0]

    assert "get_operation_store=get_integration_store" in binding
    assert "get_logger=get_identity_store" in binding, "lifecycle keeps the identity store"
    # The runtime record carries the store an operation persists through.
    assert "get_logger_only=get_integration_store" in binding
    assert "get_logger_only=get_identity_store" not in binding


def test_the_split_http_runtime_composition_gives_operations_the_integration_store() -> None:
    source = (ROOT / "core/infrastructure/runtime/http_app_request_helpers.py").read_text(
        encoding="utf-8"
    )
    binding = source.split("build_extension_binding_service_factory(", 1)[1].split(
        "extension_identifier=", 1
    )[0]

    assert "get_operation_store=lambda: _get_integration_store()" in binding
    assert "get_logger=lambda: _get_identity_store()" in binding
