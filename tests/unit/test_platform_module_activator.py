from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_models import StackGatewayError
from core.application.release.platform_modules import (
    PreparedModuleRef,
    canonical_module_descriptor,
    module_descriptor_digest,
    staged_module_refs,
)
from core.infrastructure.platform_modules.loader import activate_prepared_platform_modules
from core.infrastructure.release.platform_module_activator import (
    ComposeRoleRestarter,
    FilesystemModuleActivator,
    FilesystemStagedModules,
)

CACHE_TAG = "cpython-312"
MACHINE = "x86_64"


class FakeRestarter:
    def __init__(self, roles=("control-plane", "fs-worker", "fs-extension-installer", "fs-app")):
        self.roles = tuple(roles)
        self.restarted: list[str] = []
        self.stopped: list[str] = []

    def module_roles(self):
        return self.roles

    def stop_role(self, role):
        self.stopped.append(role)

    def restart_role(self, role):
        self.restarted.append(role)


def _stage(
    root: Path,
    module_id: str = "acme.ledger",
    version: str = "1.0.0",
    *,
    import_name: str = "acme_ledger_activator_probe",
    files: dict[str, str] | None = None,
    descriptor_overrides: dict | None = None,
) -> PreparedModuleRef:
    module_dir = root / "staged" / module_id / version
    tree = files or {f"src/{import_name}/__init__.py": f"VERSION = {version!r}\n"}
    for relative, text in tree.items():
        path = module_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    descriptor = {
        "id": module_id,
        "version": version,
        "import": import_name,
        "python_cache_tag": CACHE_TAG,
        "machine": MACHINE,
        "files_sha256": {
            relative: hashlib.sha256((module_dir / relative).read_bytes()).hexdigest()
            for relative in tree
        },
        **(descriptor_overrides or {}),
    }
    # Pretty-printed on disk: the digest covers the canonical form, not the formatting.
    (module_dir / "module.json").write_text(json.dumps(descriptor, indent=4), encoding="utf-8")
    return PreparedModuleRef(
        id=module_id, version=version, digest=module_descriptor_digest(descriptor)
    )


@pytest.fixture
def root(tmp_path):
    runtime = tmp_path / "runtime"
    modules_root = runtime / "platform-modules"
    modules_root.mkdir(parents=True)
    return modules_root


@pytest.fixture
def activator(root):
    return FilesystemModuleActivator(
        root, restarter=FakeRestarter(), cache_tag=CACHE_TAG, machine=MACHINE
    )


def _list_identity(root: Path):
    info = (root / "modules.json").stat()
    return info.st_ino, info.st_mtime_ns


def test_canonical_descriptor_is_formatting_independent():
    left = {"b": 1, "a": {"y": 2, "x": 1}}
    right = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_module_descriptor(left) == canonical_module_descriptor(right)
    assert canonical_module_descriptor(left) == b'{"a":{"x":1,"y":2},"b":1}'
    assert module_descriptor_digest(left).startswith("sha256:")


def test_supports_activation_needs_a_writable_runtime_directory(tmp_path):
    present = FilesystemModuleActivator(tmp_path / "platform-modules", restarter=FakeRestarter())
    absent = FilesystemModuleActivator(
        tmp_path / "missing" / "platform-modules", restarter=FakeRestarter()
    )
    assert present.supports_activation is True
    assert absent.supports_activation is False


def test_installed_set_is_empty_without_a_module_list(activator):
    assert activator.installed_set() == ()


def test_activate_writes_exactly_the_list_the_loader_accepts(activator, root):
    notes = _stage(root, "acme.notes", import_name="acme_notes_activator_probe")
    ledger = _stage(root, "acme.ledger")

    activator.activate((notes, ledger))

    payload = json.loads((root / "modules.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert [entry["id"] for entry in payload["modules"]] == ["acme.ledger", "acme.notes"]
    assert payload["modules"][0]["path"] == "staged/acme.ledger/1.0.0"
    assert oct((root / "modules.json").stat().st_mode & 0o777) == oct(0o644)
    assert not [path for path in root.iterdir() if path.name.startswith(".")]

    sys_path: list[str] = []
    prepared = activate_prepared_platform_modules(
        "control", root=root, sys_path=sys_path, cache_tag=CACHE_TAG, machine=MACHINE
    )
    assert [module.module_id for module in prepared] == ["acme.ledger", "acme.notes"]
    assert str((root / "staged/acme.ledger/1.0.0/src").resolve()) in sys_path
    assert activator.installed_set() == (ledger, notes)


@pytest.mark.parametrize("failure", ["digest", "missing", "wrong_id", "wrong_version"])
def test_invalid_artifact_raises_before_any_write(activator, root, failure):
    previous = _stage(root, "acme.ledger", "1.0.0")
    activator.activate((previous,))
    before = (root / "modules.json").read_bytes()
    identity = _list_identity(root)

    if failure == "digest":
        staged = _stage(root, "acme.ledger", "2.0.0")
        target = staged.model_copy(update={"digest": "sha256:" + "0" * 64})
        code = "release_module_digest_mismatch"
    elif failure == "missing":
        target = previous.model_copy(update={"version": "3.0.0"})
        code = "release_module_artifact_missing"
    elif failure == "wrong_id":
        staged = _stage(root, "acme.ledger", "2.0.0", descriptor_overrides={"id": "acme.reports"})
        target = staged
        code = "release_module_artifact_invalid"
    else:
        staged = _stage(root, "acme.ledger", "2.0.0", descriptor_overrides={"version": "2.0.1"})
        target = staged
        code = "release_module_artifact_invalid"

    with pytest.raises(LifecycleError) as raised:
        activator.activate((target,))

    assert raised.value.code == code
    assert (root / "modules.json").read_bytes() == before
    assert _list_identity(root) == identity


def test_a_set_the_loader_refuses_is_never_activated(activator, root):
    previous = _stage(root, "acme.ledger", "1.0.0")
    activator.activate((previous,))
    before = (root / "modules.json").read_bytes()
    foreign = _stage(
        root, "acme.reports", descriptor_overrides={"machine": "riscv64"}, import_name="acme_rp"
    )

    with pytest.raises(LifecycleError) as raised:
        activator.activate((previous, foreign))

    assert raised.value.code == "release_module_set_rejected"
    assert (root / "modules.json").read_bytes() == before
    assert not [path for path in root.iterdir() if path.name.startswith(".verify-")]


@pytest.mark.parametrize(
    ("requires", "message"),
    [
        ({"acme.one": ["acme.base"]}, "acme.one@1.0.0 requires acme.base (not in the module set)"),
        ({"acme.one": ["acme.two"], "acme.two": ["acme.one"]}, "dependency cycle"),
        ({"acme.one": ["acme.two>=2.0"]}, "requires acme.two>=2.0 (acme.two 1.0.0 is in the set)"),
    ],
)
def test_a_set_with_an_unmet_dependency_is_never_activated(activator, root, requires, message):
    refs = [
        _stage(
            root,
            module_id,
            import_name=f"acme_dep_probe_{module_id.split('.')[1]}",
            descriptor_overrides={"requires": requires[module_id]}
            if module_id in requires
            else None,
        )
        for module_id in ("acme.one", "acme.two")
    ]

    with pytest.raises(LifecycleError) as raised:
        activator.activate(refs)

    assert raised.value.code == "release_module_set_rejected"
    assert message in raised.value.cause
    assert not (root / "modules.json").exists()


def test_a_set_whose_dependencies_are_met_lists_by_id_and_loads_in_dependency_order(
    activator, root
):
    addon = _stage(
        root,
        "acme.addon",
        import_name="acme_dep_probe_addon",
        descriptor_overrides={"requires": ["acme.zbase>=1"]},
    )
    base = _stage(root, "acme.zbase", import_name="acme_dep_probe_zbase")

    activator.activate((addon, base))

    payload = json.loads((root / "modules.json").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in payload["modules"]] == ["acme.addon", "acme.zbase"]
    assert payload["modules"][0]["requires"] == ["acme.zbase>=1"]
    prepared = activate_prepared_platform_modules(
        "control", root=root, sys_path=[], cache_tag=CACHE_TAG, machine=MACHINE
    )
    assert [module.module_id for module in prepared] == ["acme.zbase", "acme.addon"]


def test_a_symlink_inside_a_staged_tree_is_refused(activator, root, tmp_path):
    module = _stage(root, "acme.ledger")
    outside = tmp_path / "outside.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    os.symlink(outside, root / "staged/acme.ledger/1.0.0/src/acme_ledger_activator_probe/x.py")

    with pytest.raises(LifecycleError) as raised:
        activator.activate((module,))

    assert raised.value.code == "release_module_set_rejected"
    assert not (root / "modules.json").exists()


def test_failure_before_replace_keeps_the_previous_list(activator, root, monkeypatch):
    previous = _stage(root, "acme.ledger", "1.0.0")
    activator.activate((previous,))
    before = (root / "modules.json").read_bytes()
    target = _stage(root, "acme.ledger", "2.0.0")

    def failing_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        activator.activate((target,))

    assert (root / "modules.json").read_bytes() == before
    assert not [path for path in root.iterdir() if path.name.startswith(".modules.json.")]


def test_reactivating_the_active_set_writes_nothing(activator, root, monkeypatch):
    module = _stage(root, "acme.ledger")
    activator.activate((module,))
    identity = _list_identity(root)
    writes = []
    monkeypatch.setattr(activator, "_write_list", lambda rendered: writes.append(rendered))

    activator.activate((module,))

    assert writes == []
    assert _list_identity(root) == identity


def test_empty_set_removes_the_list_and_keeps_staged_artifacts(activator, root):
    module = _stage(root, "acme.ledger")
    activator.activate((module,))

    activator.activate(())
    activator.activate(())

    assert not (root / "modules.json").exists()
    assert (root / "staged/acme.ledger/1.0.0/module.json").is_file()
    assert activator.installed_set() == ()


def test_restart_roles_restarts_each_role_once_per_call(root):
    restarter = FakeRestarter()
    activator = FilesystemModuleActivator(root, restarter=restarter)

    activator.restart_roles()
    assert restarter.restarted == list(restarter.roles)

    activator.restart_roles()
    assert restarter.restarted == list(restarter.roles) * 2


def _target(profile, services):
    return ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        deployment_profile=profile,
        env_file=".env",
        compose_files=("docker-compose.yaml",),
        runtime_services=services,
    )


@pytest.mark.parametrize(
    ("profile", "services", "expected"),
    [
        (
            "scalable",
            ("traefik", "fs-app", "fs-worker", "control-plane", "fs-extension-installer", "fs-mcp"),
            ("control-plane", "fs-worker", "fs-extension-installer", "fs-mcp", "fs-app"),
        ),
        ("compact", ("compact-runtime", "pg-fs"), ("compact-runtime",)),
    ],
)
def test_compose_restarter_restarts_only_module_roles_with_the_health_gate(
    profile, services, expected
):
    calls = []

    def restart(target, selected, *, timeout_seconds, output_sink, heartbeat):
        calls.append((target.deployment_profile, selected, timeout_seconds))

    stops = []

    def stop(target, selected, *, timeout_seconds, output_sink, heartbeat):
        stops.append(selected)

    restarter = ComposeRoleRestarter(
        target_provider=lambda: _target(profile, services), restart=restart, stop=stop
    )
    activator = FilesystemModuleActivator(Path("/nonexistent"), restarter=restarter)

    activator.stop_roles()
    activator.restart_roles()

    assert stops == [(role,) for role in reversed(expected)]
    assert [selected for _profile, selected, _timeout in calls] == [(role,) for role in expected]


def test_compose_restarter_failure_is_a_typed_resumable_error():
    def restart(*_args, **_kwargs):
        raise StackGatewayError(action="restart", detail="fs-app did not become healthy")

    restarter = ComposeRoleRestarter(
        target_provider=lambda: _target("scalable", ("fs-app",)), restart=restart, stop=restart
    )
    with pytest.raises(LifecycleError) as raised:
        restarter.restart_role("fs-app")
    assert raised.value.code == "release_module_restart_failed"
    assert raised.value.step == "modules_restart"
    with pytest.raises(LifecycleError) as raised:
        restarter.stop_role("fs-app")
    assert raised.value.code == "release_module_stop_failed"
    assert raised.value.step == "modules_quiesce"


def test_role_image_verification_is_declared_but_not_implemented(activator, root):
    module = _stage(root, "acme.ledger")

    assert activator.supports_role_verification is False
    with pytest.raises(LifecycleError) as raised:
        activator.verify_in_roles((module,))

    assert raised.value.code == "release_module_role_verification_unsupported"
    assert not (root / "modules.json").exists()


def _stage_with_migrations(root, version, migrations):
    files = {"src/acme_ledger_activator_probe/__init__.py": f"VERSION = {version!r}\n"}
    files.update({f"db/migrations/{name}": text for name, text in migrations.items()})
    return _stage(root, "acme.ledger", version, files=files)


@pytest.mark.parametrize(
    ("target_migrations", "expected"),
    [
        ({"20261101000000_init.sql": "SELECT 1;\n"}, False),
        (
            {
                "20261101000000_init.sql": "SELECT 1;\n",
                "20261120000000_add_ledger.sql": "SELECT 2;\n",
            },
            True,
        ),
        ({"20261101000000_init.sql": "SELECT 3;\n"}, True),
        ({}, False),
    ],
)
def test_adds_module_migrations_compares_staged_migration_files(
    activator, root, target_migrations, expected
):
    previous = _stage_with_migrations(root, "1.0.0", {"20261101000000_init.sql": "SELECT 1;\n"})
    target = _stage_with_migrations(root, "1.1.0", target_migrations)

    assert activator.adds_module_migrations((previous,), (target,)) is expected


def test_adds_module_migrations_is_conservative_without_the_staged_artifact(activator, root):
    target = _stage_with_migrations(root, "1.1.0", {})
    missing = target.model_copy(update={"version": "0.9.0"})

    assert activator.adds_module_migrations((missing,), (target,)) is True


def test_stop_roles_stops_each_role_http_app_first(root):
    restarter = FakeRestarter()
    activator = FilesystemModuleActivator(root, restarter=restarter)

    activator.stop_roles()

    assert restarter.stopped == list(reversed(restarter.roles))
    assert restarter.restarted == []


def test_staged_module_refs_picks_the_highest_version(root):
    _stage(root, "acme.ledger", "1.9.0")
    newest = _stage(root, "acme.ledger", "1.10.0")
    _stage(root, "acme.ledger", "1.10.0-rc.1", import_name="acme_ledger_rc")
    notes = _stage(root, "acme.notes", "0.1.0", import_name="acme_notes_probe")

    assert staged_module_refs(FilesystemStagedModules(root)) == (newest, notes)


def test_staged_module_refs_is_empty_without_staged_artifacts(root):
    assert staged_module_refs(FilesystemStagedModules(root)) == ()


def test_staged_module_refs_refuses_a_non_semantic_version(root):
    _stage(root, "acme.ledger", "1.0.0")
    _stage(root, "acme.ledger", "latest")
    with pytest.raises(LifecycleError) as raised:
        staged_module_refs(FilesystemStagedModules(root))
    assert raised.value.code == "release_module_artifact_invalid"


def test_supports_activation_is_total_when_the_runtime_directory_cannot_be_inspected(
    tmp_path, monkeypatch
):
    """Evidence 2026-11-25: a probe raising PermissionError answered nothing at all."""
    runtime = tmp_path / "runtime"
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == runtime:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    activator = FilesystemModuleActivator(runtime / "platform-modules", restarter=FakeRestarter())

    assert activator.supports_activation is False
    assert str(runtime) in activator.unavailable_reason


def test_staged_modules_lists_every_staged_version(activator, root):
    old = _stage(root, "acme.ledger", "1.0.0")
    new = _stage(root, "acme.ledger", "1.1.0")
    notes = _stage(root, "acme.notes", import_name="acme_notes_activator_probe")

    assert set(activator.staged_modules()) == {old, new, notes}


def test_a_vendored_distribution_the_cli_can_import_is_accepted(
    activator, root, tmp_path, monkeypatch
):
    """Image conflicts are the roles' check, not the management image's.

    The operator CLI's own interpreter can import ``acme_vendored_probe`` at the same
    version the module vendors; activation must not refuse the set for that, because
    the CLI image is not the image any role runs.
    """
    host_site = tmp_path / "host-site"
    (host_site / "acme_vendored_probe").mkdir(parents=True)
    (host_site / "acme_vendored_probe/__init__.py").write_text("X = 1\n", encoding="utf-8")
    host_dist = host_site / "acme_vendored_probe-1.0.dist-info"
    host_dist.mkdir()
    (host_dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: acme-vendored-probe\nVersion: 1.0\n", encoding="utf-8"
    )
    (host_dist / "top_level.txt").write_text("acme_vendored_probe\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(host_site))

    module = _stage(
        root,
        "acme.ledger",
        files={
            "src/acme_ledger_activator_probe/__init__.py": "VERSION = '1.0.0'\n",
            "site/acme_vendored_probe/__init__.py": "X = 1\n",
            "site/acme_vendored_probe-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: acme-vendored-probe\nVersion: 1.0\n"
            ),
            "site/acme_vendored_probe-1.0.dist-info/top_level.txt": "acme_vendored_probe\n",
        },
    )

    activator.activate((module,))

    assert activator.installed_set() == (module,)
