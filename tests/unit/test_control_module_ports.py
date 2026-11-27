"""The ports the control role hands platform modules (ADR 0082, 2026-11-27)."""

from __future__ import annotations

import base64

import pytest

from core.application.auth.external_session_revocation import ExternalSignInRevocationPort
from core.entrypoints.cli import control_process
from core.infrastructure.platform_modules import loader as platform_modules
from core.infrastructure.platform_modules.module_jobs import ModuleJobScheduler
from core.infrastructure.runtime import entrypoint_db_adapters

DSN = "postgresql://flow@db.invalid/flow"


class AuditStore:
    def __init__(self, name: str = "store") -> None:
        self.name = name
        self.actions: list[dict] = []

    def log_action(self, **kwargs) -> None:
        self.actions.append(kwargs)


def test_the_audit_writer_writes_the_same_auth_event_as_the_app_role(monkeypatch) -> None:
    from core.application.auth.audit import log_auth_audit_event

    role_store = AuditStore("role")
    monkeypatch.setattr(entrypoint_db_adapters, "create_identity_store", lambda _dsn: role_store)
    ports = control_process._control_module_ports(DSN)

    ports["log_auth_audit_event"](
        action="Directory_Sync", username="ada", actor="acme.sync", detail={"accounts": 2}
    )
    expected = AuditStore()
    log_auth_audit_event(
        expected, action="Directory_Sync", username="ada", actor="acme.sync", detail={"accounts": 2}
    )

    assert role_store.actions == expected.actions
    assert role_store.actions[0]["action_type"] == "auth_directory_sync"
    assert role_store.actions[0]["meta"] == {"audit_category": "auth"}
    # A store the module passes is used as is.
    own = AuditStore("own")
    ports["log_auth_audit_event"](own, action="revoked")
    assert own.actions[0]["action_type"] == "auth_revoked"
    assert len(role_store.actions) == 1


def test_the_secret_codec_round_trips_with_cores_key_for_the_bound_module(monkeypatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode())
    ports = platform_modules.bind_platform_module_services(
        control_process._control_module_ports(DSN), "acme.login"
    )

    sealed = ports["encrypt_secret"]("client-secret")

    assert sealed.startswith("fsmod1:acme.login:")
    assert ports["decrypt_secret"](sealed) == "client-secret"
    assert ports["decrypt_secret"]("") == ""


def test_the_secret_codec_is_scoped_to_the_module_and_the_modules_it_requires(
    monkeypatch,
) -> None:
    from core.application.auth.secrets import encrypt_secret
    from core.infrastructure.security.module_secret_codec import ModuleSecretScopeError

    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode())
    unbound = control_process._control_module_ports(DSN)
    engine = platform_modules.bind_platform_module_services(unbound, "acme.login")
    addon = platform_modules.bind_platform_module_services(
        unbound, "acme.login-sync", ("acme.login",)
    )
    other = platform_modules.bind_platform_module_services(unbound, "acme.other")
    sealed = engine["encrypt_secret"]("client-secret")

    # The add-on that requires the engine reads the engine's value; nobody else does.
    assert addon["decrypt_secret"](sealed) == "client-secret"
    with pytest.raises(ModuleSecretScopeError, match=r"acme\.login"):
        other["decrypt_secret"](sealed)
    # The engine cannot read the add-on's values: requires is one way.
    with pytest.raises(ModuleSecretScopeError):
        engine["decrypt_secret"](addon["encrypt_secret"]("x"))
    # A value Core encrypted (a connection secret, say) is never a module's.
    with pytest.raises(ModuleSecretScopeError, match="not one"):
        engine["decrypt_secret"](encrypt_secret("core-connection-secret"))
    # A relabelled value fails authentication instead of decrypting under another key.
    forged = sealed.replace("fsmod1:acme.login:", "fsmod1:acme.other:", 1)
    with pytest.raises(ModuleSecretScopeError, match="does not decrypt"):
        other["decrypt_secret"](forged)
    # Without a module identity the port refuses.
    with pytest.raises(ModuleSecretScopeError):
        unbound["decrypt_secret"](sealed)


def test_the_revocation_port_revokes_inside_the_callers_transaction() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.statements: list = []
            self.rows = [(True,)]

        def execute(self, sql, params=None) -> None:
            self.statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return self.rows[0]

    cursor = Cursor()
    port = control_process._control_module_ports(DSN)["external_sign_in_revocation"]

    assert isinstance(port, ExternalSignInRevocationPort)
    assert port.revoke_grants(cursor, ["u2", "u1", ""]) is True
    updates = [params for sql, params in cursor.statements if sql.startswith("UPDATE")]
    assert updates == [(["u1", "u2"], "external"), (["u1", "u2"], "external")]


def test_control_hooks_receive_the_ports_and_the_schedule_port(monkeypatch) -> None:
    monkeypatch.delenv("FS_COMPOSE_PROJECT_NAME", raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        platform_modules,
        "register_platform_modules_for_role",
        lambda _role, **kwargs: calls.append(kwargs) or (),
    )
    monkeypatch.setattr(control_process, "_CONTROL_MODULE_JOBS", None)

    control_process._register_control_platform_modules({}, DSN)

    services = calls[0]["services"]
    for name in (
        "entitlement_service",
        "secret_backends",
        "db",
        "log_auth_audit_event",
        "encrypt_secret",
        "decrypt_secret",
        "external_sign_in_revocation",
    ):
        assert name in services
    scheduler = services["schedule"]
    assert isinstance(scheduler, ModuleJobScheduler)
    assert scheduler is control_process._CONTROL_MODULE_JOBS
    # Loading has finished: no job can be added any more.
    with scheduler.platform_module_scope("acme.sync", frozenset()):
        try:
            scheduler("late", 60, lambda _ctx: None)
        except ValueError as exc:
            assert "only while modules load" in str(exc)
        else:
            raise AssertionError("a sealed scheduler accepted a job")
