"""Platform modules on Compact: the in-image command and its delegating activator (ADR 0082 §5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_models import StackGatewayError
from core.application.release.platform_modules import NoModuleActivator, PreparedModuleRef
from core.entrypoints.cli.ops import platform_modules as manage
from core.infrastructure.platform_modules.module_store import (
    ListOwnership,
    ModuleStoreError,
    PlatformModuleStore,
)
from core.infrastructure.release.platform_module_activator import (
    COMPACT_MODULE_SERVICES,
    CompactModuleActivator,
    FilesystemModuleActivator,
)
from core.infrastructure.release.upgrade_gateway import (
    PLATFORM_MODULE_COMMAND,
    run_platform_module_command,
)
from tests.unit.test_platform_module_activator import CACHE_TAG, MACHINE, FakeRestarter, _stage


@pytest.fixture
def root(tmp_path):
    modules_root = tmp_path / "runtime" / "platform-modules"
    modules_root.mkdir(parents=True)
    return modules_root


def _store(root, **overrides):
    return PlatformModuleStore(
        root,
        cache_tag=CACHE_TAG,
        machine=MACHINE,
        image_checks=overrides.pop("image_checks", True),
        **overrides,
    )


def _run(capsys, store, *argv):
    code = manage.main(list(argv), store=store)
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert len(lines) == 1, output
    return code, json.loads(lines[0])


def _set(*refs: PreparedModuleRef) -> str:
    return json.dumps([ref.model_dump(mode="json") for ref in refs])


# -- the in-image command ---------------------------------------------------------


def test_installed_and_staged_report_json(capsys, root):
    ledger = _stage(root, "acme.ledger", "1.0.0")
    newer = _stage(root, "acme.ledger", "1.1.0")

    code, installed = _run(capsys, _store(root), "installed")
    assert code == 0
    assert installed == {"ok": True, "command": "installed", "result": {"modules": []}}

    code, staged = _run(capsys, _store(root), "staged")
    assert code == 0
    assert staged["result"]["modules"] == [
        ledger.model_dump(mode="json"),
        newer.model_dump(mode="json"),
    ]


def test_activate_writes_the_list_atomically_and_reports_it(capsys, root):
    ledger = _stage(root, "acme.ledger", "1.0.0")

    code, payload = _run(capsys, _store(root), "activate", "--modules", _set(ledger))

    assert code == 0
    assert payload["result"] == {"modules": [ledger.model_dump(mode="json")], "changed": True}
    assert json.loads((root / "modules.json").read_text())["modules"][0]["id"] == "acme.ledger"
    assert sorted(path.name for path in root.iterdir()) == ["modules.json", "staged"]

    code, again = _run(capsys, _store(root), "activate", "--modules", _set(ledger))
    assert code == 0
    assert again["result"]["changed"] is False

    code, installed = _run(capsys, _store(root), "installed")
    assert installed["result"]["modules"] == [ledger.model_dump(mode="json")]


def test_a_digest_mismatch_is_refused_before_any_write(capsys, root, monkeypatch):
    staged = _stage(root, "acme.ledger", "1.0.0")
    pinned = staged.model_copy(update={"digest": "sha256:" + "0" * 64})
    writes = []
    monkeypatch.setattr(PlatformModuleStore, "write_list", lambda self, data: writes.append(data))

    code, payload = _run(capsys, _store(root), "activate", "--modules", _set(pinned))

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "release_module_digest_mismatch"
    assert payload["error"]["hint"]
    assert writes == []
    assert not (root / "modules.json").exists()
    assert not [path for path in root.iterdir() if path.name.startswith(".")]


def test_verify_checks_without_writing(capsys, root):
    ledger = _stage(root, "acme.ledger", "1.0.0")
    foreign = _stage(
        root, "acme.reports", descriptor_overrides={"machine": "riscv64"}, import_name="acme_rp"
    )

    code, accepted = _run(capsys, _store(root), "verify", "--modules", _set(ledger))
    assert code == 0
    assert accepted["result"]["modules"] == [ledger.model_dump(mode="json")]

    code, refused = _run(capsys, _store(root), "verify", "--modules", _set(ledger, foreign))
    assert code == 1
    assert refused["error"]["code"] == "release_module_set_rejected"
    assert not (root / "modules.json").exists()


def test_adds_migrations_compares_two_sets(capsys, root):
    before = _stage(root, "acme.ledger", "1.0.0")
    after = _stage(
        root,
        "acme.ledger",
        "1.1.0",
        files={
            "src/acme_ledger_activator_probe/__init__.py": "VERSION = '1.1.0'\n",
            "db/migrations/20261125000000_ledger.sql": "SELECT 1;\n",
        },
    )

    code, payload = _run(
        capsys,
        _store(root),
        "adds-migrations",
        "--previous",
        _set(before),
        "--target",
        _set(after),
    )

    assert code == 0
    assert payload["result"] == {"adds_migrations": True}


def test_malformed_arguments_are_a_json_refusal(capsys, root):
    code, payload = _run(capsys, _store(root), "activate", "--modules", "not json")
    assert code == 1
    assert payload["error"]["code"] == "release_module_set_invalid"

    code, payload = _run(capsys, _store(root), "remove-everything")
    assert code == 2
    assert payload["error"]["code"] == "release_module_command_invalid"


def test_compact_image_writes_the_list_with_the_runtime_ownership(monkeypatch, root):
    """The roles read through fs-runtime; none of them may write the list."""
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setitem(runtime_env, "FS_DEPLOYMENT_PROFILE", "compact")
    ownership = manage._ownership()
    assert ownership == ListOwnership(uid=10007, gid=10102, mode=0o640)

    chowned = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((Path(path), uid, gid)))
    ledger = _stage(root, "acme.ledger", "1.0.0")

    store = _store(root, ownership=ownership)
    assert store.activate([manage.ModuleRef(**ledger.model_dump())]) is True

    assert len(chowned) == 1
    path, uid, gid = chowned[0]
    assert path.parent == root and path.name.startswith(".modules.json.")
    assert (uid, gid) == (10007, 10102)
    assert oct((root / "modules.json").stat().st_mode & 0o777) == oct(0o640)


def test_a_non_root_writer_must_already_be_the_operator(monkeypatch, root):
    ledger = _stage(root, "acme.ledger", "1.0.0")
    store = _store(root, ownership=ListOwnership(uid=os.geteuid() + 1, gid=0, mode=0o640))
    monkeypatch.setattr(os, "geteuid", lambda: 4242)

    with pytest.raises(ModuleStoreError) as raised:
        store.activate([manage.ModuleRef(**ledger.model_dump())])

    assert raised.value.code == "release_module_list_ownership"
    assert not (root / "modules.json").exists()
    assert not [path for path in root.iterdir() if path.name.startswith(".")]


def test_scalable_keeps_the_previous_list_rule(monkeypatch):
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setitem(runtime_env, "FS_DEPLOYMENT_PROFILE", "scalable")
    assert manage._ownership() is None


# -- the gateway invocation ---------------------------------------------------------


def _compact_target(profile="compact"):
    return SimpleNamespace(deployment_profile=profile, runtime_services=COMPACT_MODULE_SERVICES)


def test_gateway_runs_the_command_like_the_compact_database_upgrade():
    calls = []

    def run_compose(target, arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(
            stdout='{"ok": true, "command": "installed", "result": {"modules": []}}'
        )

    result = run_platform_module_command(
        _compact_target(),
        ("installed",),
        timeout_seconds=90,
        run_compose=run_compose,
        environment={"FS_COMPACT_IMAGE": "image"},
    )

    assert result == {"modules": []}
    arguments, kwargs = calls[0]
    assert arguments == (
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "compact-runtime",
        "-m",
        PLATFORM_MODULE_COMMAND,
        "installed",
    )
    assert kwargs["separate_stderr"] is True
    assert kwargs["environment"] == {"FS_COMPACT_IMAGE": "image"}
    assert kwargs["timeout_seconds"] == 90


@pytest.mark.parametrize("stdout", ["", "not json", '{"ok": false}', '{"ok": true}'])
def test_gateway_refuses_an_invalid_answer(stdout):
    def run_compose(target, arguments, **kwargs):
        return SimpleNamespace(stdout=stdout)

    with pytest.raises(StackGatewayError):
        run_platform_module_command(
            _compact_target(),
            ("installed",),
            timeout_seconds=1,
            run_compose=run_compose,
            environment={},
        )


def test_gateway_is_compact_only():
    with pytest.raises(StackGatewayError):
        run_platform_module_command(
            _compact_target("scalable"),
            ("installed",),
            timeout_seconds=1,
            run_compose=lambda *args, **kwargs: pytest.fail("must not run"),
            environment={},
        )


# -- the delegating activator ---------------------------------------------------------


def _ref(module_id="acme.ledger", version="1.0.0"):
    return PreparedModuleRef(id=module_id, version=version, digest="sha256:" + "ab" * 32)


class FakeGateway:
    def __init__(self, results=None, failure=None):
        self.calls: list[tuple[str, ...]] = []
        self.results = results or {}
        self.failure = failure

    def __call__(self, arguments):
        self.calls.append(arguments)
        if self.failure is not None:
            raise self.failure
        return self.results.get(arguments[0], {})


def _activator(gateway, *, profile="compact", restarter=None):
    return CompactModuleActivator(
        run_command=gateway,
        restarter=restarter or FakeRestarter(roles=COMPACT_MODULE_SERVICES),
        target_provider=lambda: _compact_target(profile),
    )


def test_compact_activator_issues_the_in_image_commands():
    ledger = _ref()
    listing = {"modules": [ledger.model_dump(mode="json")]}
    gateway = FakeGateway(
        results={
            "installed": listing,
            "staged": listing,
            "activate": {**listing, "changed": True},
            "verify": listing,
            "adds-migrations": {"adds_migrations": False},
        }
    )
    activator = _activator(gateway)

    assert activator.supports_activation is True
    assert activator.supports_role_verification is True
    assert activator.installed_set() == (ledger,)
    assert activator.staged_modules() == (ledger,)
    activator.verify_in_roles((ledger,))
    activator.activate((ledger,))
    assert activator.adds_module_migrations((), (ledger,)) is False

    pinned = json.dumps([ledger.model_dump(mode="json")], separators=(",", ":"))
    assert gateway.calls == [
        ("installed",),
        ("staged",),
        ("verify", "--modules", pinned),
        ("activate", "--modules", pinned),
        ("adds-migrations", "--previous", "[]", "--target", pinned),
    ]


def test_compact_activator_maps_a_reported_refusal_to_its_lifecycle_code():
    gateway = FakeGateway(
        failure=StackGatewayError(
            action="platform_modules",
            detail="release_module_digest_mismatch: Staged platform module acme.ledger@1.0.0 "
            "has digest sha256:00; the upgrade pinned sha256:ab. Keep maintenance enabled.",
        )
    )

    with pytest.raises(LifecycleError) as raised:
        _activator(gateway).activate((_ref(),))

    assert raised.value.code == "release_module_digest_mismatch"
    assert raised.value.cause.startswith("Staged platform module acme.ledger@1.0.0")
    assert raised.value.step == "modules_activate"


def test_compact_activator_maps_a_docker_failure_to_a_typed_error():
    gateway = FakeGateway(
        failure=StackGatewayError(action="platform_modules", detail="Docker exited with code 125.")
    )

    with pytest.raises(LifecycleError) as raised:
        _activator(gateway).installed_set()

    assert raised.value.code == "release_module_command_failed"
    assert "125" in raised.value.cause


def test_compact_activator_is_conservative_when_migrations_cannot_be_compared():
    gateway = FakeGateway(failure=StackGatewayError(action="platform_modules", detail="gone"))

    assert _activator(gateway).adds_module_migrations((_ref(),), (_ref(version="2.0.0"),)) is True


def test_compact_activator_refuses_an_invalid_listing():
    gateway = FakeGateway(results={"installed": {"modules": [{"id": "x"}]}})

    with pytest.raises(LifecycleError) as raised:
        _activator(gateway).installed_set()

    assert raised.value.code == "release_module_command_invalid"


def test_compact_activator_stops_and_restarts_the_runtime_container():
    restarter = FakeRestarter(roles=COMPACT_MODULE_SERVICES)
    activator = _activator(FakeGateway(), restarter=restarter)

    activator.stop_roles()
    activator.restart_roles()

    assert restarter.stopped == ["compact-runtime"]
    assert restarter.restarted == ["compact-runtime"]


def test_compact_activator_needs_a_compact_target():
    activator = _activator(FakeGateway(), profile="scalable")

    assert activator.supports_activation is False
    assert "not a Compact deployment" in activator.unavailable_reason


# -- profile wiring -------------------------------------------------------------------


def _runtime(monkeypatch, profile):
    from core.application.installer.contracts import InstallConfig
    from core.entrypoints.cli.runtime import CLIRuntime

    active = InstallConfig(environment="production", deployment_profile=profile)
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda required=False: active,
        store=SimpleNamespace(read_durable_environment=lambda config: {}),
    )
    runtime.__dict__["maintenance_service"] = SimpleNamespace()
    return runtime


def test_compact_host_management_delegates_to_the_runtime_image(monkeypatch):
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setitem(
        runtime_env, "FS_CONFIGURATION_STATE_ROOT", "/run/flow-steward-install-state"
    )
    runtime = _runtime(monkeypatch, "compact")

    assert isinstance(runtime._platform_module_activator("compact"), CompactModuleActivator)


def test_compact_console_inside_the_runtime_cannot_change_modules(monkeypatch):
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setitem(runtime_env, "FS_CONFIGURATION_STATE_ROOT", "")
    activator = _runtime(monkeypatch, "compact")._platform_module_activator("compact")

    assert isinstance(activator, NoModuleActivator)
    assert "host management CLI" in activator.unavailable_reason


def test_scalable_keeps_the_direct_filesystem_activator(monkeypatch, tmp_path):
    import core.infrastructure.platform_modules.loader as loader

    root = tmp_path / "runtime" / "platform-modules"
    root.parent.mkdir()
    monkeypatch.setattr(loader, "platform_modules_root", lambda: root)

    activator = _runtime(monkeypatch, "scalable")._platform_module_activator("scalable")

    assert isinstance(activator, FilesystemModuleActivator)
    assert activator.root == root


def test_scalable_without_a_runtime_directory_says_so(monkeypatch, tmp_path):
    import core.infrastructure.platform_modules.loader as loader

    monkeypatch.setattr(loader, "platform_modules_root", lambda: tmp_path / "absent" / "pm")

    activator = _runtime(monkeypatch, "scalable")._platform_module_activator("scalable")

    assert isinstance(activator, NoModuleActivator)
    assert "does not exist" in activator.unavailable_reason
