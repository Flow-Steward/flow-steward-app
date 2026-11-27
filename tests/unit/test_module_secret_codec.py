"""Core's secret codec as platform modules see it: bound to the module (ADR 0082 §12).

A module encrypts values bound to itself and decrypts only its own values and those
of the modules it requires; values Core or another module encrypted are refused.
Values a module stored before the binding are re-encrypted by
``reseal_legacy_secrets``, which only touches the module's own schema.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core.application.auth.external_sign_in_ports import (
    ExternalSignInPort,
    ModuleExternalSignInPort,
)
from core.application.auth.secrets import encrypt_secret
from core.application.settings.platform_module_views import PlatformModuleViews
from core.infrastructure.platform_modules.loader import PreparedPlatformModule
from core.infrastructure.platform_modules.module_jobs import ModuleJobScheduler
from core.infrastructure.security.module_secret_codec import (
    ModuleSecretCodec,
    ModuleSecretPort,
    ModuleSecretScopeError,
    module_secret_owner,
)

ENGINE = "acme.login"
ADDON = "acme.login-sync"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"m" * 32).decode())


@dataclass
class Container:
    auth_helpers: dict[str, Any] = field(default_factory=dict)
    service_factories: dict[str, Any] = field(default_factory=dict)
    registries: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    external_sign_in: Any = None


def _module(module_id: str, requires: tuple[str, ...] = ()) -> PreparedPlatformModule:
    return PreparedPlatformModule(
        module_id=module_id,
        version="1.0.0",
        import_name=module_id.replace(".", "_").replace("-", "_"),
        path=Path("/nonexistent"),
        requires=requires,
    )


def test_each_module_view_carries_the_sign_in_port_bound_to_that_module() -> None:
    shared = ExternalSignInPort(lambda: None)
    container = Container(external_sign_in=shared)
    views = PlatformModuleViews()

    with views.module_view(_module(ENGINE), container) as engine_view:
        engine_port = engine_view.external_sign_in
    with views.module_view(_module(ADDON, (ENGINE,)), container) as addon_view:
        addon_port = addon_view.external_sign_in

    assert isinstance(engine_port, ModuleExternalSignInPort)
    assert engine_port.module_id == ENGINE
    # Core's container keeps Core's own port.
    assert container.external_sign_in is shared
    # Everything but the codec is Core's port.
    assert engine_port.sign_in_methods == shared.sign_in_methods
    sealed = engine_port.encrypt_secret("client-secret")
    assert module_secret_owner(sealed) == ENGINE
    assert engine_port.decrypt_secret(sealed) == "client-secret"
    assert addon_port.decrypt_secret(sealed) == "client-secret"
    with pytest.raises(ModuleSecretScopeError):
        engine_port.decrypt_secret(addon_port.encrypt_secret("x"))
    with pytest.raises(ModuleSecretScopeError):
        engine_port.decrypt_secret(encrypt_secret("core-value"))
    # A bound port cannot be re-bound to another identity.
    with pytest.raises(AttributeError):
        engine_port.for_platform_module(ADDON)


def test_cores_own_port_refuses_the_codec() -> None:
    shared = ExternalSignInPort(lambda: None)

    with pytest.raises(ModuleSecretScopeError):
        shared.encrypt_secret("x")
    with pytest.raises(ModuleSecretScopeError):
        shared.decrypt_secret(encrypt_secret("x"))


def test_an_environment_listed_module_without_identity_gets_no_bound_codec() -> None:
    shared = ExternalSignInPort(lambda: None)
    container = Container(external_sign_in=shared)

    with PlatformModuleViews().module_view(None, container) as view:
        assert view.external_sign_in is shared


def test_a_job_receives_the_codec_bound_to_its_module_and_requirements() -> None:
    seen: list = []
    scheduler = ModuleJobScheduler(
        services={
            "encrypt_secret": ModuleSecretPort("encrypt"),
            "decrypt_secret": ModuleSecretPort("decrypt"),
        },
        clock=lambda: 1000.0,
        jitter=lambda: 0.0,
        start_thread=lambda target, _name: target() or None,
    )
    engine_sealed = ModuleSecretCodec(ENGINE).encrypt("engine-secret")
    with scheduler.platform_module_scope(ADDON, frozenset()):
        scheduler("sync", 60, seen.append)
    scheduler.seal({ADDON: "1.0.0"}, {ADDON: (ENGINE,)})

    scheduler.run_pending()

    [ctx] = seen
    assert module_secret_owner(ctx["encrypt_secret"]("x")) == ADDON
    assert ctx["decrypt_secret"](engine_sealed) == "engine-secret"


class RecordingCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((" ".join(sql.split()), tuple(params)))

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


def test_reseal_rebinds_values_stored_before_the_binding_in_the_modules_own_schema() -> None:
    legacy = encrypt_secret("client-secret")
    cursor = RecordingCursor([(legacy,), (legacy,)])
    codec = ModuleSecretCodec(ENGINE)

    assert (
        codec.reseal_legacy_secrets(
            cursor, table="identity_providers", column="config", json_key="client_secret_encrypted"
        )
        == 1
    )

    select, update = cursor.statements
    assert select[0].startswith(
        'SELECT ("config" ->> %s) FROM "mod_acme_login"."identity_providers"'
    )
    assert select[0].endswith("FOR UPDATE")
    assert select[1][-1] == "fsmod1:%"
    assert update[0].startswith(
        'UPDATE "mod_acme_login"."identity_providers" SET "config" = jsonb_set'
    )
    key, resealed, _key_again, old = update[1]
    assert key == "client_secret_encrypted" and old == legacy
    assert codec.decrypt(resealed) == "client-secret"


@pytest.mark.parametrize(
    ("table", "column"),
    [("other_schema.providers", "config"), ("providers", 'config"; DROP'), ("", "config")],
)
def test_reseal_refuses_anything_but_a_plain_table_of_the_own_schema(table, column) -> None:
    with pytest.raises(ModuleSecretScopeError):
        ModuleSecretCodec(ENGINE).reseal_legacy_secrets(
            RecordingCursor([]), table=table, column=column
        )


def test_a_module_id_that_is_not_one_cannot_bind_the_codec() -> None:
    with pytest.raises(ModuleSecretScopeError):
        ModuleSecretCodec("")
    with pytest.raises(ModuleSecretScopeError):
        ModuleSecretPort("decrypt")("value")
