"""Modules hear about deleted users, and a failed module's subscription is rolled back."""

from __future__ import annotations

import pytest

from core.application.auth import user_lifecycle_events as events
from core.application.auth.external_sign_in_ports import ExternalSignInPort
from core.infrastructure.platform_modules import loader, registration_rollback


@pytest.fixture(autouse=True)
def _empty_registry():
    checkpoint = events.loading_checkpoint()
    events.restore_loading_checkpoint(None)
    yield
    events.restore_loading_checkpoint(checkpoint)


def test_every_subscriber_hears_and_a_failing_one_does_not_stop_the_rest() -> None:
    heard: list[str] = []

    def failing(_user_id: str) -> None:
        raise RuntimeError("module bug")

    events.subscribe_user_deleted(failing)
    events.subscribe_user_deleted(heard.append)

    events.notify_user_deleted("user-1")

    assert heard == ["user-1"]


def test_the_port_subscribes_through_the_same_registry() -> None:
    heard: list[str] = []
    ExternalSignInPort(lambda: None).subscribe_user_deleted(heard.append)

    events.notify_user_deleted("user-2")

    assert heard == ["user-2"]


def test_the_loader_rolls_back_a_failed_modules_subscription() -> None:
    assert (
        "core.application.auth.user_lifecycle_events"
        in registration_rollback._CORE_GLOBAL_REGISTRY_MODULES
    )
    port = ExternalSignInPort(lambda: None)
    container = type("Container", (), {"external_sign_in": port, "registries": {}})()
    rollback = loader.ModuleRegistrationRollback.capture(container)

    events.subscribe_user_deleted(lambda _user_id: None)

    class Resolver:
        def resolve(self, identity):
            return None

    port.set_identity_resolver(Resolver())
    rollback.restore()

    assert events.loading_checkpoint() == ()
    assert type(port.resolver).__name__ == "LinkedIdentityResolver"


def test_subscribing_something_that_is_not_callable_is_refused() -> None:
    with pytest.raises(TypeError):
        events.subscribe_user_deleted("not callable")  # type: ignore[arg-type]
