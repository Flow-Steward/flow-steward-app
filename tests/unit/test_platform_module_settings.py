"""Deployment-level settings declared by platform modules (ADR 0082, module settings)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.application.installer.host_settings import (
    applied_operator_settings,
    checked_operator_settings,
)
from core.application.installer.service import InstallerService
from core.application.lifecycle.configuration_models import ConfigSetRequest
from core.application.lifecycle.contracts import ConfigurationPatch, DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.module_settings import (
    check_module_settings,
    module_setting_rows,
)
from core.application.lifecycle.operator_settings import OperatorSettingError
from core.entrypoints.cli.module_settings_source import (
    filesystem_module_settings,
    module_settings_source,
)
from core.entrypoints.cli.ops import platform_modules as manage
from core.entrypoints.cli.platform.renderers import _config_show_lines
from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.platform_modules import loader
from core.infrastructure.platform_modules.module_settings import (
    MODULE_SETTING_MAX_VALUE_CHARS,
    MODULE_SETTINGS_ENV,
    ModuleSettingDeclaration,
    declarations_from_listing,
    decode_module_settings,
    encode_module_settings,
    module_set_setting_conflicts,
    module_setting_name_prefix,
    parse_module_settings,
    publish_module_settings,
)
from core.infrastructure.platform_modules.module_store import (
    ModuleStoreError,
    PlatformModuleStore,
    canonical_module_descriptor,
    module_descriptor_digest,
)
from core.infrastructure.runtime.compact import process_spec
from tests.unit.lifecycle.test_configuration_service import (
    _service,
    _write_lifecycle_secret_environment,
)
from tests.unit.test_platform_module_activator import CACHE_TAG, MACHINE, _stage
from tests.unit.test_platform_modules_loader import CACHE_TAG as LOADER_CACHE_TAG
from tests.unit.test_platform_modules_loader import _activate, _make_module, _write_list

POLICY = "FS_MODULE_ACME_LEDGER_POLICY_FILE"
DEV_MODE = "FS_MODULE_ACME_LEDGER_DEV_MODE"
#: A second module whose own prefix also covers POLICY: the only way two modules
#: can declare one name under the module-id prefix rule.
OVERLAPPING_MODULE = "acme-ledger.policy"
SETTINGS = [
    {"name": POLICY, "description": "Path of the ACME policy file."},
    {
        "name": DEV_MODE,
        "description": "Development-only switch.",
        "default": "0",
        "allowed_values": ["0", "1"],
        "secret": False,
    },
]


def _declared(module_id: str = "acme.ledger") -> tuple[ModuleSettingDeclaration, ...]:
    return parse_module_settings(SETTINGS, module_id=module_id, where="settings")


def _by_name(module_id: str = "acme.ledger") -> tuple[ModuleSettingDeclaration, ...]:
    """What the store reports: the declarations ordered by name."""
    return tuple(sorted(_declared(module_id), key=lambda declaration: declaration.name))


# -- the descriptor contract --------------------------------------------------------


def test_a_descriptor_declares_named_described_settings_with_defaults_and_choices() -> None:
    policy, dev_mode = _declared()

    assert policy == ModuleSettingDeclaration(
        name=POLICY, description="Path of the ACME policy file.", module_id="acme.ledger"
    )
    assert dev_mode.default == "0"
    assert dev_mode.allowed_values == ("0", "1")
    assert dev_mode.to_json()["module"] == "acme.ledger"
    assert ModuleSettingDeclaration.from_json(dev_mode.to_json()) == dev_mode


@pytest.mark.parametrize(
    ("settings", "message"),
    (
        ({"x": 1}, "must be a list"),
        ([{"name": "FS_SECRET_KEY", "description": "core"}], "start with FS_MODULE_"),
        ([{"name": "FS_MODULE_lower", "description": "lower"}], "start with FS_MODULE_"),
        ([{"name": POLICY}], "missing keys"),
        ([{"name": POLICY, "description": "x", "kind": "path"}], "unknown keys"),
        ([{"name": POLICY, "description": "  "}], "description"),
        ([{"name": POLICY, "description": "x", "secret": True}], "carry no secrets"),
        ([{"name": POLICY, "description": "x", "default": 1}], "default must be text"),
        ([{"name": POLICY, "description": "x", "allowed_values": []}], "non-empty list"),
        ([{"name": POLICY, "description": "x", "allowed_values": ["a", "a"]}], "twice"),
        (
            [{"name": POLICY, "description": "x", "default": "2", "allowed_values": ["0"]}],
            "not one of its allowed_values",
        ),
        ([SETTINGS[0], SETTINGS[0]], "declares FS_MODULE_ACME_LEDGER_POLICY_FILE twice"),
    ),
)
def test_a_malformed_settings_declaration_is_refused(settings: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_module_settings(settings, module_id="acme.ledger", where="settings")


def test_the_digest_of_a_descriptor_without_settings_is_unchanged() -> None:
    descriptor = {
        "id": "acme.ledger",
        "version": "1.0.0",
        "import": "acme_ledger",
        "python_cache_tag": CACHE_TAG,
        "machine": MACHINE,
        "files_sha256": {"src/acme_ledger/__init__.py": "0" * 64},
    }
    expected = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert canonical_module_descriptor(descriptor) == json.dumps(
        descriptor, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert module_descriptor_digest(descriptor) == f"sha256:{expected}"
    # Declared settings are pinned with the module.
    assert module_descriptor_digest({**descriptor, "settings": SETTINGS}) != (
        module_descriptor_digest(descriptor)
    )


# -- the module store ---------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    modules_root = tmp_path / "runtime" / "platform-modules"
    modules_root.mkdir(parents=True)
    return modules_root


def _store(root: Path) -> PlatformModuleStore:
    return PlatformModuleStore(root, cache_tag=CACHE_TAG, machine=MACHINE, image_checks=False)


def test_the_store_records_the_settings_of_the_active_set(root: Path) -> None:
    plain = _stage(root, "acme.plain", "1.0.0", import_name="acme_plain_settings_probe")
    ledger = _stage(
        root,
        "acme.ledger",
        "1.0.0",
        import_name="acme_ledger_settings_probe",
        descriptor_overrides={"settings": SETTINGS},
    )
    store = _store(root)

    assert store.declared_settings() == ()
    assert store.activate([plain, ledger]) is True

    assert store.declared_settings() == _by_name("acme.ledger")
    # The active entry's digest is still the staged descriptor's.
    assert {ref.digest for ref in store.installed_refs()} == {plain.digest, ledger.digest}


def test_two_modules_declaring_one_setting_are_refused_before_any_write(root: Path) -> None:
    first = _stage(
        root,
        "acme.ledger",
        import_name="acme_ledger_settings_probe",
        descriptor_overrides={"settings": SETTINGS[:1]},
    )
    second = _stage(
        root,
        OVERLAPPING_MODULE,
        import_name="acme_reports_settings_probe",
        descriptor_overrides={"settings": SETTINGS[:1]},
    )

    with pytest.raises(ModuleStoreError) as refused:
        _store(root).activate([first, second])

    assert refused.value.code == "release_module_set_rejected"
    assert POLICY in refused.value.cause
    assert not (root / "modules.json").exists()


def test_a_name_under_another_modules_longer_prefix_is_refused() -> None:
    """``acme.login`` may not declare ``FS_MODULE_ACME_LOGIN_SYNC_*`` beside ``acme.login-sync``."""
    engine = parse_module_settings(
        [{"name": "FS_MODULE_ACME_LOGIN_SYNC_TENANT", "description": "x"}],
        module_id="acme.login",
        where="settings",
    )

    [problem] = module_set_setting_conflicts(["acme.login", "acme.login-sync"], engine)
    assert "FS_MODULE_ACME_LOGIN_SYNC_TENANT" in problem
    assert "acme.login-sync" in problem
    # Alone, or with modules whose prefixes it does not enter, the name is fine.
    assert module_set_setting_conflicts(["acme.login"], engine) == []
    assert module_set_setting_conflicts(["acme.login", "acme.loginx"], engine) == []
    # The add-on's own names under its own prefix are fine as well.
    addon = parse_module_settings(
        [{"name": "FS_MODULE_ACME_LOGIN_SYNC_TENANT", "description": "x"}],
        module_id="acme.login-sync",
        where="settings",
    )
    assert module_set_setting_conflicts(["acme.login", "acme.login-sync"], addon) == []


def test_module_ids_with_one_setting_prefix_cannot_share_a_set() -> None:
    [problem] = module_set_setting_conflicts(["acme.b-c", "acme.b.c", "acme.other"])

    assert "acme.b-c" in problem and "acme.b.c" in problem
    assert "FS_MODULE_ACME_B_C_" in problem


def test_the_store_refuses_colliding_ids_and_overlapping_names_before_any_write(
    root: Path,
) -> None:
    first = _stage(root, "acme.b-c", import_name="acme_bc_dash_probe")
    second = _stage(root, "acme.b.c", import_name="acme_bc_dot_probe")

    with pytest.raises(ModuleStoreError) as refused:
        _store(root).activate([first, second])

    assert refused.value.code == "release_module_set_rejected"
    assert "FS_MODULE_ACME_B_C_" in refused.value.cause
    assert not (root / "modules.json").exists()

    engine = _stage(
        root,
        "acme.login",
        import_name="acme_login_engine_probe",
        descriptor_overrides={
            "settings": [{"name": "FS_MODULE_ACME_LOGIN_SYNC_TENANT", "description": "x"}]
        },
    )
    addon = _stage(root, "acme.login-sync", import_name="acme_login_sync_probe")
    with pytest.raises(ModuleStoreError) as refused:
        _store(root).activate([engine, addon])
    assert "FS_MODULE_ACME_LOGIN_SYNC_TENANT" in refused.value.cause
    assert not (root / "modules.json").exists()


def test_a_staged_module_with_malformed_settings_is_refused(root: Path) -> None:
    broken = _stage(
        root,
        import_name="acme_ledger_settings_probe",
        descriptor_overrides={"settings": [{"name": "LEDGER", "description": "x"}]},
    )

    with pytest.raises(ModuleStoreError) as refused:
        _store(root).activate([broken])

    assert refused.value.code == "release_module_artifact_invalid"


def test_the_in_image_settings_command_lists_the_declared_settings(capsys, root: Path) -> None:
    ledger = _stage(
        root,
        import_name="acme_ledger_settings_probe",
        descriptor_overrides={"settings": SETTINGS},
    )
    store = _store(root)
    store.activate([ledger])

    assert manage.main(["settings"], store=store) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert declarations_from_listing(payload["result"]) == _by_name("acme.ledger")


def test_the_filesystem_source_reads_the_active_list(root: Path) -> None:
    ledger = _stage(
        root,
        import_name="acme_ledger_settings_probe",
        descriptor_overrides={"settings": SETTINGS},
    )
    _store(root).activate([ledger])

    assert filesystem_module_settings(root) == _by_name("acme.ledger")
    with pytest.raises(RuntimeError, match="not available"):
        filesystem_module_settings(root / "absent" / "platform-modules")


def test_the_compact_host_source_asks_the_runtime_image(tmp_path: Path) -> None:
    calls = []

    class _Docker:
        def platform_module_command(self, target, arguments, *, environment, timeout_seconds):
            calls.append((target, arguments))
            return {"settings": [item.to_json() for item in _declared()]}

    service = SimpleNamespace(
        derive_target=lambda config: "compact-target",
        store=SimpleNamespace(read_durable_environment=lambda config: {}),
    )
    source = module_settings_source(service, docker=_Docker, compact_from_host=True)

    assert source(DeploymentConfig(environment="production", deployment_profile="compact")) == (
        _declared()
    )
    assert calls == [("compact-target", ("settings",))]


# -- the loader and the roles -------------------------------------------------------


def test_the_loader_reads_settings_and_refuses_a_name_two_modules_declare(tmp_path: Path) -> None:
    entry = _make_module(tmp_path, module_id="acme.ledger", import_name="fs_settings_ledger")
    entry["settings"] = SETTINGS
    _write_list(tmp_path, entry)

    (module,) = _activate(tmp_path)
    assert module.settings == _declared("acme.ledger")

    other = _make_module(tmp_path, module_id=OVERLAPPING_MODULE, import_name="fs_settings_reports")
    other["settings"] = SETTINGS[:1]
    _write_list(tmp_path, entry, other)
    with pytest.raises(loader.PlatformModuleListError, match="more than one module"):
        _activate(tmp_path)

    entry["settings"] = [{"name": "FS_SECRET_KEY", "description": "x"}]
    _write_list(tmp_path, entry)
    with pytest.raises(loader.PlatformModuleListError, match="FS_MODULE_"):
        _activate(tmp_path)


def test_the_loader_refuses_a_list_whose_module_settings_overlap(tmp_path: Path) -> None:
    dash = _make_module(tmp_path, module_id="acme.b-c", import_name="fs_settings_bc_dash")
    dot = _make_module(tmp_path, module_id="acme.b.c", import_name="fs_settings_bc_dot")
    _write_list(tmp_path, dash, dot)

    with pytest.raises(loader.PlatformModuleListError, match="FS_MODULE_ACME_B_C_"):
        _activate(tmp_path)


def test_a_role_publishes_its_modules_settings_only_when_it_activates_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = _make_module(tmp_path, module_id="acme.ledger", import_name="fs_settings_ledger")
    entry["settings"] = SETTINGS
    _write_list(tmp_path, entry)
    published = []
    monkeypatch.setattr(
        loader,
        "publish_module_settings",
        lambda environment, declarations: published.append(tuple(declarations)),
    )

    _activate(tmp_path)  # a verification with its own path list publishes nothing
    assert published == []

    # A role starting activates into the process's own sys.path.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    loader.activate_prepared_platform_modules(
        "worker",
        root=tmp_path,
        cache_tag=LOADER_CACHE_TAG,
        machine=MACHINE,
        installed_version=lambda _name: None,
    )
    assert published == [_declared("acme.ledger")]


def test_publishing_uses_the_operator_value_then_the_default_and_nothing_undeclared() -> None:
    environment = {
        MODULE_SETTINGS_ENV: encode_module_settings(
            {POLICY: "/etc/acme/policy.json", "FS_MODULE_OTHER": "x", "FS_SECRET_KEY": "no"}
        )
    }

    published = publish_module_settings(environment, _declared())

    assert published == {POLICY: "/etc/acme/policy.json", DEV_MODE: "0"}
    assert environment[POLICY] == "/etc/acme/policy.json"
    assert environment[DEV_MODE] == "0"
    assert "FS_MODULE_OTHER" not in environment
    assert decode_module_settings(environment[MODULE_SETTINGS_ENV]) == {
        POLICY: "/etc/acme/policy.json",
        "FS_MODULE_OTHER": "x",
    }

    assert encode_module_settings({}) == ""


def test_a_malformed_carrier_falls_back_to_the_defaults_with_a_warning(caplog) -> None:
    for malformed in ("{not json", "base64:%%%", "base64:" + "e30K"[:-1] + "!"):
        caplog.clear()
        environment = {MODULE_SETTINGS_ENV: malformed}
        with caplog.at_level("WARNING"):
            assert publish_module_settings(environment, _declared()) == {DEV_MODE: "0"}
        assert MODULE_SETTINGS_ENV in caplog.text
        assert "not applied" in caplog.text


def test_the_carrier_survives_quotes_comments_and_spaces_in_an_env_file() -> None:
    value = """it's "quoted" # not a comment  ${HOME} `x` \\ end"""
    carrier = encode_module_settings({POLICY: value})

    # Only characters every dotenv reader and shell takes literally.
    assert re.fullmatch(r"base64:[A-Za-z0-9+/=]+", carrier)
    assert shlex.quote(carrier) == carrier
    assert decode_module_settings(carrier) == {POLICY: value}
    # A development run may still export the JSON object by hand.
    assert decode_module_settings(json.dumps({POLICY: "x"})) == {POLICY: "x"}


def test_a_module_declares_only_names_under_its_own_id() -> None:
    assert module_setting_name_prefix("acme.sign-in") == "FS_MODULE_ACME_SIGN_IN_"
    assert module_setting_name_prefix("acme-ledger.policy") == "FS_MODULE_ACME_LEDGER_POLICY_"
    for name in ("FS_MODULE_SIGN_IN_ALLOW_PRIVATE_PROVIDERS", "FS_MODULE_ACME_SIGN_IN_"):
        with pytest.raises(ValueError, match="must start with FS_MODULE_ACME_SIGN_IN_"):
            parse_module_settings(
                [{"name": name, "description": "x"}], module_id="acme.sign-in", where="s"
            )
    (declared,) = parse_module_settings(
        [{"name": "FS_MODULE_ACME_SIGN_IN_ALLOW_PRIVATE_PROVIDERS", "description": "x"}],
        module_id="acme.sign-in",
        where="s",
    )
    record = dict(declared.to_json(), module="acme.ledger")
    with pytest.raises(ValueError, match="FS_MODULE_ACME_LEDGER_"):
        ModuleSettingDeclaration.from_json(record)


def test_an_operator_value_is_at_most_the_module_setting_limit() -> None:
    declared = _declared()
    limit = "x" * MODULE_SETTING_MAX_VALUE_CHARS
    assert check_module_settings({POLICY: limit}, declared, active={}) == {POLICY: limit}
    with pytest.raises(OperatorSettingError, match="at most 4096 characters"):
        check_module_settings({POLICY: limit + "x"}, declared, active={})


def _compact_specs(parent: dict[str, str]):
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    return process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_STACK_ENVIRONMENT": "production",
            "ENVIRONMENT": "production",
            **parent,
        },
        public_port=8080,
        observation_fd=7,
    )


def test_compact_passes_the_module_settings_to_the_module_roles_only() -> None:
    carrier = encode_module_settings({DEV_MODE: "1"})

    specs = {spec.role: spec for spec in _compact_specs({MODULE_SETTINGS_ENV: carrier})}

    for role in ("app", "control", "worker-0"):
        assert specs[role].environment[MODULE_SETTINGS_ENV] == carrier
    for role in ("router", "installer", "mcp"):
        assert MODULE_SETTINGS_ENV not in specs[role].environment
    # A module name set directly on the container is still not inherited.
    direct = {spec.role: spec for spec in _compact_specs({DEV_MODE: "1"})}
    assert all(DEV_MODE not in spec.environment for spec in direct.values())
    assert MODULE_SETTINGS_ENV in process_spec.delivered_environment_names()


# -- config set / show / apply ------------------------------------------------------


def test_config_set_routes_module_names_and_still_refuses_unknown_ones() -> None:
    request = ConfigSetRequest.model_validate({"assignments": [f"{DEV_MODE}=1"]})
    assert request.operator_settings == {DEV_MODE: "1"}

    with pytest.raises(ValidationError, match="not a setting you can change"):
        ConfigSetRequest.model_validate({"assignments": ["FS_MODULEX=1"]})
    assert checked_operator_settings({DEV_MODE: "1"}) == {DEV_MODE: "1"}


def _installed_service(tmp_path: Path, declared=None, **active):
    service = _service(tmp_path)
    service.store.write_active(DeploymentConfig(**active))
    if declared is not None:
        service.module_settings = lambda config: declared
    return service


def test_config_set_accepts_a_setting_an_active_module_declares(tmp_path: Path) -> None:
    service = _installed_service(tmp_path, _declared())

    pending = service.set(ConfigurationPatch(operator_settings=f"{DEV_MODE}=1")).pending

    assert pending.operator_settings == {DEV_MODE: "1"}


@pytest.mark.parametrize(
    ("declared", "assignment", "message"),
    (
        ((), f"{DEV_MODE}=1", "no active platform module declares it"),
        (None, f"{DEV_MODE}=1", "cannot be checked here"),
        ("declared", f"{DEV_MODE}=yes", "accepts 0, 1"),
    ),
)
def test_config_set_refuses_an_undeclared_or_invalid_module_setting(
    tmp_path: Path, declared, assignment: str, message: str
) -> None:
    service = _installed_service(tmp_path, _declared() if declared == "declared" else declared)

    with pytest.raises(LifecycleError) as refused:
        service.set(ConfigurationPatch(operator_settings=assignment))

    assert refused.value.code == "configuration_setting_invalid"
    assert message in refused.value.cause
    assert not service.store.pending_path.exists()


def test_a_stored_value_no_module_declares_any_more_can_be_reset(tmp_path: Path) -> None:
    service = _installed_service(tmp_path, (), operator_settings={DEV_MODE: "1"})

    pending = service.set(ConfigurationPatch(operator_settings=f"{DEV_MODE}=")).pending

    assert pending.operator_settings == {DEV_MODE: ""}
    with pytest.raises(OperatorSettingError):
        check_module_settings({POLICY: ""}, (), active={DEV_MODE: "1"})


def test_apply_renders_the_module_settings_into_the_one_carrier(tmp_path: Path) -> None:
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service = _service(tmp_path)
    active = DeploymentConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )

    candidate = service._candidate(
        active, ConfigurationPatch(operator_settings=f"{DEV_MODE}=1,{POLICY}=/etc/p.json")
    )
    environment = service.prepare_environment(candidate)[2]

    assert decode_module_settings(environment[MODULE_SETTINGS_ENV]) == {
        DEV_MODE: "1",
        POLICY: "/etc/p.json",
    }
    assert DEV_MODE not in environment
    reset = service._candidate(
        candidate, ConfigurationPatch(operator_settings=f"{DEV_MODE}=,{POLICY}=")
    )
    # Rendered empty rather than left out, so a reset value cannot survive in the file.
    assert service.prepare_environment(reset)[2][MODULE_SETTINGS_ENV] == ""


def test_config_show_lists_module_settings_with_their_module(tmp_path: Path) -> None:
    service = _installed_service(
        tmp_path, _declared(), operator_settings={DEV_MODE: "1", "FS_MODULE_GONE": "x"}
    )

    shown = service.show()
    rows = {row.name: row for row in shown.module_settings}

    assert rows[DEV_MODE].module == "acme.ledger"
    assert (rows[DEV_MODE].value, rows[DEV_MODE].state) == ("1", "set")
    assert (rows[POLICY].value, rows[POLICY].state) == ("", "default")
    assert rows["FS_MODULE_GONE"].state == "undeclared"

    text = "\n".join(_config_show_lines(shown.model_dump(mode="json")))
    module_section = text.split("Platform module settings", 1)[1].split("Pending changes", 1)[0]
    host_section = text.split("Host settings set by the operator", 1)[1].split(
        "Platform module settings", 1
    )[0]
    dev_line = next(line for line in module_section.splitlines() if DEV_MODE in line)
    assert dev_line.endswith("1  [module acme.ledger; one of 0, 1]")
    policy_line = next(line for line in module_section.splitlines() if POLICY in line)
    assert policy_line.endswith("none (default)  [module acme.ledger]")
    assert "FS_MODULE_GONE" in module_section
    assert DEV_MODE not in host_section


def test_config_show_still_lists_stored_values_when_the_module_set_is_unreadable() -> None:
    rows = module_setting_rows({DEV_MODE: "1", "FS_ALLOW_PRIVATE_REMOTE_URLS": "1"}, None)

    assert [(row.name, row.module, row.state) for row in rows] == [(DEV_MODE, "", "set")]


# -- ./install.sh --setting ---------------------------------------------------------


def _compact(**fields) -> InstallConfig:
    return InstallConfig(environment="production", deployment_profile="compact", **fields)


def test_install_sh_setting_writes_the_carrier_for_module_settings() -> None:
    config = _compact(operator_settings={POLICY: "/etc/p.json"})

    candidate, keys = applied_operator_settings(config, {DEV_MODE: "1"})

    assert candidate.operator_settings == {POLICY: "/etc/p.json", DEV_MODE: "1"}
    assert decode_module_settings(keys.pop(MODULE_SETTINGS_ENV)) == {
        POLICY: "/etc/p.json",
        DEV_MODE: "1",
    }
    assert keys == {}


def test_install_sh_setting_checks_module_settings_against_the_installation() -> None:
    checked = []

    class _Gateway:
        def check_module_settings(self, config, changes):
            checked.append(dict(changes))
            if changes.get(DEV_MODE) == "yes":
                raise OperatorSettingError(f"{DEV_MODE} accepts 0, 1")

    service = object.__new__(InstallerService)
    service.gateway = _Gateway()
    installed = _compact()

    service._check_module_settings(installed, {"FS_ALLOW_PRIVATE_REMOTE_URLS": "1"})
    service._check_module_settings(installed, {DEV_MODE: "1", "FS_ALLOW_PRIVATE_REMOTE_URLS": "1"})
    assert checked == [{DEV_MODE: "1"}]

    with pytest.raises(InstallerFailure) as refused:
        service._check_module_settings(installed, {DEV_MODE: "yes"})
    assert refused.value.code == "installer_setting_invalid"

    with pytest.raises(InstallerFailure, match="install first"):
        service._check_module_settings(None, {DEV_MODE: "1"})
