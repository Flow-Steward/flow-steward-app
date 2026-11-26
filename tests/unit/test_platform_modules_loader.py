from __future__ import annotations

import hashlib
import importlib.machinery
import json
import platform
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.infrastructure.platform_modules import loader as platform_modules
from core.infrastructure.platform_modules.loader import (
    PlatformModuleCompatibilityError,
    PlatformModuleConflictError,
    PlatformModuleImportError,
    PlatformModuleIntegrityError,
    PlatformModuleListError,
    PreparedPlatformModule,
    activate_prepared_platform_modules,
    register_platform_modules_for_role,
)

CACHE_TAG = "cpython-313"
MACHINE = "x86_64"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_module(
    root: Path,
    *,
    module_id: str = "acme.accounts",
    version: str = "1.2.0",
    import_name: str = "fs_accounts",
    relative_path: str | None = None,
    files: dict[str, str] | None = None,
) -> dict:
    relative_path = relative_path or f"{module_id}/{version}"
    module_dir = root / relative_path
    tree = files or {f"src/{import_name}/__init__.py": "VALUE = 1\n"}
    for relative, text in tree.items():
        _write(module_dir / relative, text)
    return {
        "id": module_id,
        "version": version,
        "import": import_name,
        "path": relative_path,
        "python_cache_tag": CACHE_TAG,
        "machine": MACHINE,
        "files_sha256": {relative: _sha(module_dir / relative) for relative in tree},
    }


def _write_list(root: Path, *entries: dict, schema_version: object = 1) -> None:
    _write(
        root / "modules.json",
        json.dumps({"schema_version": schema_version, "modules": list(entries)}),
    )


def _activate(root: Path, *, sys_path: list[str] | None = None, **overrides):
    options = {
        "root": root,
        "sys_path": [] if sys_path is None else sys_path,
        "cache_tag": CACHE_TAG,
        "machine": MACHINE,
        "installed_version": lambda _name: None,
    }
    options.update(overrides)
    return activate_prepared_platform_modules("app", **options)


def test_missing_list_is_plain_core(tmp_path: Path) -> None:
    sys_path = ["/existing"]

    assert _activate(tmp_path, sys_path=sys_path) == ()
    assert _activate(tmp_path / "absent", sys_path=sys_path) == ()
    assert sys_path == ["/existing"]


def test_root_defaults_to_shared_runtime_directory_and_honours_override(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FS_PLATFORM_MODULES_ROOT", raising=False)
    assert platform_modules.platform_modules_root() == Path(
        "/run/flow-steward-runtime/platform-modules"
    )

    monkeypatch.setenv("FS_PLATFORM_MODULES_ROOT", str(tmp_path))
    assert platform_modules.platform_modules_root() == tmp_path


def test_activation_returns_records_and_appends_import_roots_after_existing_entries(
    tmp_path: Path,
) -> None:
    first = _make_module(tmp_path)
    second = _make_module(
        tmp_path, module_id="acme.ledger", version="0.3.1", import_name="fs_ledger"
    )
    _write_list(tmp_path, first, second)
    sys_path = ["/core", "/venv/site-packages"]

    modules = _activate(tmp_path, sys_path=sys_path)

    first_dir = (tmp_path / "acme.accounts/1.2.0").resolve()
    second_dir = (tmp_path / "acme.ledger/0.3.1").resolve()
    assert modules == (
        PreparedPlatformModule("acme.accounts", "1.2.0", "fs_accounts", first_dir),
        PreparedPlatformModule("acme.ledger", "0.3.1", "fs_ledger", second_dir),
    )
    assert sys_path == [
        "/core",
        "/venv/site-packages",
        str(first_dir / "src"),
        str(first_dir / "site"),
        str(second_dir / "src"),
        str(second_dir / "site"),
    ]

    assert _activate(tmp_path, sys_path=sys_path) == modules
    assert len(sys_path) == 6


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not json", "cannot be read as JSON"),
        ("[]", "JSON object"),
        (json.dumps({"schema_version": 2, "modules": []}), "schema_version"),
        (json.dumps({"schema_version": 1}), "'modules' list"),
        (json.dumps({"schema_version": 1, "modules": ["x"]}), "must be an object"),
        (json.dumps({"schema_version": 1, "modules": [{"id": "a"}]}), "missing keys"),
    ],
)
def test_malformed_list_is_refused(tmp_path: Path, payload: str, message: str) -> None:
    _write(tmp_path / "modules.json", payload)

    with pytest.raises(PlatformModuleListError, match=message):
        _activate(tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("import", "acme-accounts", "dotted Python module name"),
        ("id", "", "non-empty string"),
        ("files_sha256", {"src/fs_accounts/__init__.py": "ABC"}, "sha256 digest"),
        ("files_sha256", {"README.md": "0" * 64}, "must live under"),
        ("files_sha256", {"src/../x.py": "0" * 64}, "normalized relative path"),
        ("path", "/abs/module", "normalized relative path"),
        ("path", "../outside", "normalized relative path"),
    ],
)
def test_malformed_entry_is_refused(tmp_path: Path, field: str, value: object, message) -> None:
    entry = _make_module(tmp_path)
    entry[field] = value
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleListError, match=message):
        _activate(tmp_path)


def test_duplicate_module_is_refused(tmp_path: Path) -> None:
    entry = _make_module(tmp_path)
    _write_list(tmp_path, entry, dict(entry))

    with pytest.raises(PlatformModuleListError, match="twice"):
        _activate(tmp_path)


def test_digest_mismatch_is_refused_without_touching_sys_path(tmp_path: Path) -> None:
    entry = _make_module(tmp_path)
    _write_list(tmp_path, entry)
    _write(tmp_path / "acme.accounts/1.2.0/src/fs_accounts/__init__.py", "VALUE = 2\n")
    sys_path = ["/core"]

    with pytest.raises(PlatformModuleIntegrityError, match="has sha256"):
        _activate(tmp_path, sys_path=sys_path)
    assert sys_path == ["/core"]


def test_missing_listed_file_is_refused(tmp_path: Path) -> None:
    entry = _make_module(tmp_path)
    entry["files_sha256"]["src/fs_accounts/extra.py"] = "0" * 64
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="missing listed files"):
        _activate(tmp_path)


@pytest.mark.parametrize(
    "unlisted",
    [
        "src/fs_accounts/injected.py",
        "site/vendored/__init__.py",
        "src/fs_accounts/__pycache__/injected.cpython-313.pyc",
    ],
)
def test_unlisted_file_is_refused(tmp_path: Path, unlisted: str) -> None:
    entry = _make_module(tmp_path)
    _write_list(tmp_path, entry)
    _write(tmp_path / "acme.accounts/1.2.0" / unlisted, "X = 1\n")

    with pytest.raises(PlatformModuleIntegrityError, match="not in the module list"):
        _activate(tmp_path)


def test_pth_file_is_refused_even_when_listed(tmp_path: Path) -> None:
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "VALUE = 1\n",
            "site/evil.pth": "import os\n",
        },
    )
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleIntegrityError, match=r"\.pth file"):
        _activate(tmp_path)


def test_module_directory_symlink_escaping_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    entry = _make_module(outside, relative_path="acme.accounts")
    root.mkdir()
    (root / "acme.accounts").symlink_to(outside / "acme.accounts", target_is_directory=True)
    _write_list(root, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="escapes the platform module root"):
        _activate(root)


def test_symlinked_file_inside_a_module_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    entry = _make_module(root)
    secret = _write(tmp_path / "outside.py", "SECRET = 1\n")
    link = root / "acme.accounts/1.2.0/src/fs_accounts/linked.py"
    link.symlink_to(secret)
    entry["files_sha256"]["src/fs_accounts/linked.py"] = _sha(secret)
    _write_list(root, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="symlink"):
        _activate(root)


def test_cache_tag_mismatch_is_refused(tmp_path: Path) -> None:
    _write_list(tmp_path, _make_module(tmp_path))

    with pytest.raises(PlatformModuleCompatibilityError, match="cpython-312"):
        _activate(tmp_path, cache_tag="cpython-312")


def test_machine_mismatch_is_refused(tmp_path: Path) -> None:
    _write_list(tmp_path, _make_module(tmp_path))

    with pytest.raises(PlatformModuleCompatibilityError, match="aarch64"):
        _activate(tmp_path, machine="aarch64")


def _module_with_distribution(root: Path, *, version: str = "2.1.0") -> dict:
    return _make_module(
        root,
        files={
            "src/fs_accounts/__init__.py": "VALUE = 1\n",
            "site/authlib/__init__.py": "",
            f"site/Authlib-{version}.dist-info/METADATA": (
                f"Metadata-Version: 2.1\nName: Authlib\nVersion: {version}\n"
            ),
            f"site/Authlib-{version}.dist-info/top_level.txt": "authlib\n",
        },
    )


def test_vendored_distribution_with_a_different_installed_version_is_a_conflict(
    tmp_path: Path,
) -> None:
    _write_list(tmp_path, _module_with_distribution(tmp_path))
    sys_path = ["/core"]

    with pytest.raises(PlatformModuleConflictError, match=r"Authlib 2\.1\.0.*Authlib 1\.3\.0"):
        _activate(
            tmp_path,
            sys_path=sys_path,
            installed_version=lambda name: "1.3.0" if name == "Authlib" else None,
        )
    assert sys_path == ["/core"]


def test_vendored_distribution_with_the_same_installed_version_is_accepted(
    tmp_path: Path,
) -> None:
    _write_list(tmp_path, _module_with_distribution(tmp_path))

    modules = _activate(
        tmp_path,
        installed_version=lambda name: "2.1.0" if name == "Authlib" else None,
    )

    assert [module.module_id for module in modules] == ["acme.accounts"]


def test_two_modules_shipping_different_versions_of_one_distribution_conflict(
    tmp_path: Path,
) -> None:
    first = _module_with_distribution(tmp_path, version="2.1.0")
    second = _make_module(
        tmp_path,
        module_id="acme.ledger",
        import_name="fs_ledger",
        files={
            "src/fs_ledger/__init__.py": "",
            "site/authlib-2.0.0.dist-info/METADATA": "Name: authlib\nVersion: 2.0.0\n",
        },
    )
    _write_list(tmp_path, first, second)

    with pytest.raises(PlatformModuleConflictError, match=re.escape("acme.accounts")):
        _activate(tmp_path)


def test_import_package_already_importable_elsewhere_is_a_conflict(tmp_path: Path) -> None:
    core_root = tmp_path / "core-site"
    _write(core_root / "fs_accounts/__init__.py", "")
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _make_module(modules_root))

    with pytest.raises(PlatformModuleConflictError, match="already importable"):
        _activate(modules_root, sys_path=[str(core_root)])


def test_unknown_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(platform_modules.PlatformModuleError, match="unknown platform module role"):
        activate_prepared_platform_modules("installer", root=tmp_path)


def _host_module(root: Path, **kwargs) -> dict:
    entry = _make_module(root, **kwargs)
    entry["python_cache_tag"] = sys.implementation.cache_tag
    entry["machine"] = platform.machine()
    return entry


def _imported_from(
    root: Path, module_id: str, import_name: str, *, version: str = "1.2.0", **hooks
) -> SimpleNamespace:
    """A stand-in for the imported package, loaded from the module's own src/."""
    origin = root / module_id / version / "src" / import_name / "__init__.py"
    return SimpleNamespace(__file__=str(origin), **hooks)


def test_role_registration_calls_hooks_in_order_and_skips_modules_without_one(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    _write_list(
        tmp_path,
        _host_module(tmp_path, module_id="acme.mod-a", import_name="fs_proto_mod_a"),
        _host_module(tmp_path, module_id="acme.mod-b", import_name="fs_proto_mod_b"),
    )
    calls: list[tuple[str, dict]] = []
    imported = {
        "fs_proto_mod_a": _imported_from(
            tmp_path,
            "acme.mod-a",
            "fs_proto_mod_a",
            register_worker=lambda context: calls.append(("fs_proto_mod_a", dict(context))),
        ),
        "fs_proto_mod_b": _imported_from(tmp_path, "acme.mod-b", "fs_proto_mod_b"),
    }
    config = {"fs_db_dsn": "postgresql://example"}

    modules = register_platform_modules_for_role(
        "worker", config=config, root=tmp_path, import_module=imported.__getitem__
    )

    assert [module.module_id for module in modules] == ["acme.mod-a", "acme.mod-b"]
    assert calls == [
        (
            "fs_proto_mod_a",
            {
                "role": "worker",
                "config": config,
                "platform_module": {"id": "acme.mod-a", "version": "1.2.0"},
            },
        )
    ]


def test_role_hooks_receive_the_identity_core_loaded_each_module_under(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    _write_list(
        tmp_path,
        _host_module(tmp_path, module_id="acme.ledger", import_name="fs_proto_ledger"),
        _host_module(
            tmp_path, module_id="acme.reports", version="0.4.1", import_name="fs_proto_reports"
        ),
    )
    contexts: list = []
    imported = {
        "fs_proto_ledger": _imported_from(
            tmp_path, "acme.ledger", "fs_proto_ledger", register_control=contexts.append
        ),
        "fs_proto_reports": _imported_from(
            tmp_path,
            "acme.reports",
            "fs_proto_reports",
            version="0.4.1",
            register_control=contexts.append,
        ),
    }

    modules = register_platform_modules_for_role(
        "control",
        config={},
        root=tmp_path,
        import_module=imported.__getitem__,
    )

    assert [module.identity() for module in modules] == [
        {"id": "acme.ledger", "version": "1.2.0"},
        {"id": "acme.reports", "version": "0.4.1"},
    ]
    assert [dict(context["platform_module"]) for context in contexts] == [
        {"id": "acme.ledger", "version": "1.2.0"},
        {"id": "acme.reports", "version": "0.4.1"},
    ]
    for context in contexts:
        assert context["role"] == "control"
        with pytest.raises(TypeError):
            context["platform_module"]["id"] = "acme.other"
        with pytest.raises(TypeError):
            context["platform_module"] = {"id": "acme.other"}


def test_role_registration_import_failure_is_fatal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    _write_list(tmp_path, _host_module(tmp_path, import_name="fs_proto_broken"))

    def _broken(_name: str):
        raise ImportError("boom")

    with pytest.raises(PlatformModuleImportError, match=r"acme\.accounts.*boom"):
        register_platform_modules_for_role(
            "control", config={}, root=tmp_path, import_module=_broken
        )


def test_role_registration_is_a_noop_without_a_list(tmp_path: Path) -> None:
    assert register_platform_modules_for_role("control", config={}, root=tmp_path) == ()


def _module_with_migration(root: Path) -> dict:
    return _make_module(
        root,
        files={
            "src/fs_accounts/__init__.py": "VALUE = 1\n",
            "db/migrations/20261125000000_accounts.sql": "CREATE TABLE accounts (id int);\n",
        },
    )


def test_listed_migration_is_verified_and_never_added_to_sys_path(tmp_path: Path) -> None:
    _write_list(tmp_path, _module_with_migration(tmp_path))
    sys_path = ["/core"]

    (module,) = _activate(tmp_path, sys_path=sys_path)

    assert sys_path == ["/core", str(module.path / "src"), str(module.path / "site")]
    assert not any(entry.endswith("/db") or "/db/" in entry for entry in sys_path)


def test_tampered_migration_is_refused(tmp_path: Path) -> None:
    _write_list(tmp_path, _module_with_migration(tmp_path))
    _write(
        tmp_path / "acme.accounts/1.2.0/db/migrations/20261125000000_accounts.sql",
        "DROP TABLE platform_users;\n",
    )

    with pytest.raises(PlatformModuleIntegrityError, match="has sha256"):
        _activate(tmp_path)


def test_unlisted_migration_is_refused(tmp_path: Path) -> None:
    _write_list(tmp_path, _module_with_migration(tmp_path))
    _write(
        tmp_path / "acme.accounts/1.2.0/db/migrations/20261126000000_injected.sql",
        "SELECT 1;\n",
    )

    with pytest.raises(PlatformModuleIntegrityError, match="not in the module list"):
        _activate(tmp_path)


def test_symlinked_migration_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    entry = _module_with_migration(root)
    outside = _write(tmp_path / "outside.sql", "SELECT 1;\n")
    relative = "db/migrations/20261126000000_linked.sql"
    (root / "acme.accounts/1.2.0" / relative).symlink_to(outside)
    entry["files_sha256"][relative] = _sha(outside)
    _write_list(root, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="symlink"):
        _activate(root)


def test_control_role_verification_with_a_path_copy_covers_migrations(tmp_path: Path) -> None:
    _write_list(tmp_path, _module_with_migration(tmp_path))
    _write(tmp_path / "acme.accounts/1.2.0/db/extra.sql", "SELECT 1;\n")

    with pytest.raises(PlatformModuleIntegrityError, match=re.escape("db/extra.sql")):
        activate_prepared_platform_modules(
            role="control",
            root=tmp_path,
            sys_path=list(sys.path),
            cache_tag=CACHE_TAG,
            machine=MACHINE,
            installed_version=lambda _name: None,
        )


def test_listed_file_outside_the_verified_trees_is_refused(tmp_path: Path) -> None:
    entry = _make_module(tmp_path)
    _write(tmp_path / "acme.accounts/1.2.0/bin/run.sh", "echo hi\n")
    entry["files_sha256"]["bin/run.sh"] = _sha(tmp_path / "acme.accounts/1.2.0/bin/run.sh")
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleListError, match="must live under src, site, db/"):
        _activate(tmp_path)


@pytest.mark.parametrize("module_id", ["billing", "Acme.Billing", "acme.billing_x", "acme..x"])
def test_module_id_outside_the_adr_0013_grammar_is_refused(tmp_path: Path, module_id) -> None:
    entry = _make_module(tmp_path, module_id="acme.billing")
    entry["id"] = module_id
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleListError, match="ADR 0013"):
        _activate(tmp_path)


def test_dotted_module_id_is_accepted(tmp_path: Path) -> None:
    _write_list(tmp_path, _make_module(tmp_path, module_id="com.acme.billing-sync"))

    (module,) = _activate(tmp_path)

    assert module.module_id == "com.acme.billing-sync"


def test_activation_on_the_live_path_disables_bytecode_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_list(tmp_path, _make_module(tmp_path))
    live_path = list(sys.path)
    monkeypatch.setattr(sys, "path", live_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    activate_prepared_platform_modules(
        "app",
        root=tmp_path,
        cache_tag=CACHE_TAG,
        machine=MACHINE,
        installed_version=lambda _name: None,
    )

    assert sys.dont_write_bytecode is True
    assert str((tmp_path / "acme.accounts/1.2.0/src").resolve()) in live_path


def test_verification_with_a_throwaway_path_keeps_bytecode_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_list(tmp_path, _make_module(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    _activate(tmp_path)

    assert sys.dont_write_bytecode is False


# --- one module cannot ship another module's import names (ADR 0082) ---------------


def test_a_module_shipping_another_module_package_in_its_site_is_refused(tmp_path: Path) -> None:
    hijacker = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/fs_ledger/__init__.py": "HIJACKED = True\n",
            "site/fs_ledger-9.0.dist-info/METADATA": "Name: fs-ledger\nVersion: 9.0\n",
            "site/fs_ledger-9.0.dist-info/top_level.txt": "fs_ledger\n",
        },
    )
    victim = _make_module(tmp_path, module_id="acme.ledger", import_name="fs_ledger")
    _write_list(tmp_path, hijacker, victim)
    sys_path = ["/core"]

    with pytest.raises(
        PlatformModuleConflictError,
        match=r"'acme\.accounts' and 'acme\.ledger' both provide the top-level import name "
        r"'fs_ledger'",
    ):
        _activate(tmp_path, sys_path=sys_path)
    # The whole set is refused before any path is appended.
    assert sys_path == ["/core"]


@pytest.mark.parametrize(
    "extra",
    [
        "src/fs_ledger/__init__.py",
        "src/helper.py",
        "src/_native.cpython-313-x86_64-linux-gnu.so",
        "src/data.json",
    ],
)
def test_src_may_hold_only_the_declared_import_package(tmp_path: Path, extra: str) -> None:
    entry = _make_module(tmp_path, files={"src/fs_accounts/__init__.py": "", extra: "X = 1\n"})
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="src/ may contain only"):
        _activate(tmp_path)


def test_single_file_import_module_in_src_is_accepted(tmp_path: Path) -> None:
    _write_list(tmp_path, _make_module(tmp_path, files={"src/fs_accounts.py": "VALUE = 1\n"}))

    (module,) = _activate(tmp_path)

    assert module.import_name == "fs_accounts"


@pytest.mark.parametrize(
    "unaccounted",
    [
        "site/vendored/__init__.py",
        "site/six.py",
        "site/_speedups.cpython-313-x86_64-linux-gnu.so",
    ],
)
def test_site_name_without_a_declaring_distribution_is_refused(
    tmp_path: Path, unaccounted: str
) -> None:
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/authlib/__init__.py": "",
            "site/Authlib-2.1.0.dist-info/METADATA": "Name: Authlib\nVersion: 2.1.0\n",
            "site/Authlib-2.1.0.dist-info/top_level.txt": "authlib\n",
            unaccounted: "",
        },
    )
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleIntegrityError, match="no vendored \\*\\.dist-info"):
        _activate(tmp_path)


def test_site_names_may_be_declared_by_record_instead_of_top_level(tmp_path: Path) -> None:
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/six.py": "",
            "site/six-1.16.0.dist-info/METADATA": "Name: six\nVersion: 1.16.0\n",
            "site/six-1.16.0.dist-info/RECORD": (
                "six.py,sha256=x,1\nsix-1.16.0.dist-info/METADATA,,\n"
                "six-1.16.0.dist-info/RECORD,,\n"
            ),
        },
    )
    _write_list(tmp_path, entry)

    (module,) = _activate(tmp_path)

    assert module.module_id == "acme.accounts"


def test_distribution_without_top_level_or_record_is_refused(tmp_path: Path) -> None:
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/authlib/__init__.py": "",
            "site/Authlib-2.1.0.dist-info/METADATA": "Name: Authlib\nVersion: 2.1.0\n",
        },
    )
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleIntegrityError, match=r"without top_level\.txt or RECORD"):
        _activate(tmp_path)


@pytest.mark.parametrize(
    "core_entry",
    ["authlib/__init__.py", "authlib.py"],
)
def test_vendored_name_already_importable_from_core_is_a_conflict(
    tmp_path: Path, core_entry: str
) -> None:
    core_root = tmp_path / "core-site"
    _write(core_root / core_entry, "")
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _module_with_distribution(modules_root))

    with pytest.raises(PlatformModuleConflictError, match="'authlib' is already importable"):
        _activate(modules_root, sys_path=[str(core_root)])


def test_vendored_name_shadowing_a_builtin_module_is_a_conflict(tmp_path: Path) -> None:
    builtin = sorted(name for name in sys.builtin_module_names if name.isidentifier())[0]
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            f"site/{builtin}.py": "",
            "site/shadow-1.0.dist-info/METADATA": "Name: shadow\nVersion: 1.0\n",
            "site/shadow-1.0.dist-info/top_level.txt": f"{builtin}\n",
        },
    )
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleConflictError, match="built-in module"):
        _activate(tmp_path)


def test_imported_package_from_another_module_tree_is_refused(tmp_path: Path) -> None:
    module = PreparedPlatformModule(
        module_id="acme.ledger",
        version="1.0.0",
        import_name="fs_ledger",
        path=tmp_path / "acme.ledger/1.0.0",
    )
    elsewhere = SimpleNamespace(
        __file__=str(tmp_path / "acme.accounts/1.2.0/site/fs_ledger/__init__.py")
    )

    with pytest.raises(PlatformModuleImportError, match="outside the module's own"):
        platform_modules.import_prepared_platform_module(
            module, import_module=lambda _name: elsewhere
        )


def test_namespace_package_import_is_refused(tmp_path: Path) -> None:
    module = PreparedPlatformModule(
        module_id="acme.ledger",
        version="1.0.0",
        import_name="fs_ledger",
        path=tmp_path / "acme.ledger/1.0.0",
    )

    with pytest.raises(PlatformModuleImportError, match="has no __file__"):
        platform_modules.import_prepared_platform_module(
            module, import_module=lambda _name: SimpleNamespace(__file__=None)
        )


def test_role_registration_refuses_a_hook_loaded_from_outside_src(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    _write_list(tmp_path, _host_module(tmp_path, import_name="fs_proto_elsewhere"))
    calls: list = []
    foreign = SimpleNamespace(
        __file__=str(tmp_path / "other/fs_proto_elsewhere/__init__.py"),
        register_control=calls.append,
    )

    with pytest.raises(PlatformModuleImportError, match="outside the module's own"):
        register_platform_modules_for_role(
            "control", config={}, root=tmp_path, import_module=lambda _name: foreign
        )
    assert calls == []


def test_real_import_from_own_src_passes_the_origin_check(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _write_list(
        tmp_path,
        _host_module(
            tmp_path,
            import_name="fs_proto_origin_ok",
            files={"src/fs_proto_origin_ok/__init__.py": "VALUE = 7\n"},
        ),
    )
    monkeypatch.delitem(sys.modules, "fs_proto_origin_ok", raising=False)

    (module,) = platform_modules.activate_prepared_platform_modules("control", root=tmp_path)
    try:
        imported = platform_modules.import_prepared_platform_module(module)
    finally:
        sys.modules.pop("fs_proto_origin_ok", None)

    assert imported.VALUE == 7


# --- a vendored distribution may be shared at exactly the same version -------------


def _image_distribution(core_root: Path, *, version: str = "2.1.0") -> None:
    """Authlib as the running image would have it installed."""
    _write(core_root / "authlib/__init__.py", "")
    _write(
        core_root / f"Authlib-{version}.dist-info/METADATA",
        f"Metadata-Version: 2.1\nName: Authlib\nVersion: {version}\n",
    )
    _write(core_root / f"Authlib-{version}.dist-info/top_level.txt", "authlib\n")


def _bundling_module(
    root: Path, module_id: str, import_name: str, *, version: str = "2.1.0", body: str = ""
) -> dict:
    return _make_module(
        root,
        module_id=module_id,
        import_name=import_name,
        files={
            f"src/{import_name}/__init__.py": "",
            "site/authlib/__init__.py": body,
            f"site/Authlib-{version}.dist-info/METADATA": (
                f"Metadata-Version: 2.1\nName: Authlib\nVersion: {version}\n"
            ),
            f"site/Authlib-{version}.dist-info/top_level.txt": "authlib\n",
        },
    )


def test_same_version_distribution_in_the_image_is_accepted_and_the_image_copy_wins(
    tmp_path: Path,
) -> None:
    core_root = tmp_path / "core-site"
    _image_distribution(core_root)
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _module_with_distribution(modules_root))
    sys_path = [str(core_root)]

    (module,) = _activate(modules_root, sys_path=sys_path, installed_version=None)

    assert sys_path == [str(core_root), str(module.path / "src"), str(module.path / "site")]
    spec = importlib.machinery.PathFinder.find_spec("authlib", sys_path)
    assert spec is not None and Path(spec.origin).is_relative_to(core_root)


def test_different_version_distribution_in_the_image_is_refused(tmp_path: Path) -> None:
    core_root = tmp_path / "core-site"
    _image_distribution(core_root, version="1.3.0")
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _module_with_distribution(modules_root))

    with pytest.raises(PlatformModuleConflictError, match=r"Authlib 2\.1\.0.*Authlib 1\.3\.0"):
        _activate(modules_root, sys_path=[str(core_root)], installed_version=None)


def test_vendored_name_shadowing_a_core_source_package_is_refused(tmp_path: Path) -> None:
    core_root = tmp_path / "core-site"
    # Importable from Core, but not from an installed Authlib distribution.
    _write(core_root / "authlib/__init__.py", "")
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _module_with_distribution(modules_root))

    with pytest.raises(PlatformModuleConflictError, match=r"not Authlib 2\.1\.0 installed"):
        _activate(
            modules_root,
            sys_path=[str(core_root)],
            installed_version=lambda name: "2.1.0" if name == "Authlib" else None,
        )


def test_vendored_name_shadowing_the_standard_library_is_refused(tmp_path: Path) -> None:
    entry = _make_module(
        tmp_path,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/json/__init__.py": "",
            "site/json_shadow-1.0.dist-info/METADATA": "Name: json-shadow\nVersion: 1.0\n",
            "site/json_shadow-1.0.dist-info/top_level.txt": "json\n",
        },
    )
    _write_list(tmp_path, entry)

    with pytest.raises(PlatformModuleConflictError, match="standard library"):
        _activate(tmp_path)


def test_two_modules_with_an_identical_bundled_distribution_are_accepted(tmp_path: Path) -> None:
    first = _bundling_module(tmp_path, "acme.accounts", "fs_accounts")
    second = _bundling_module(tmp_path, "acme.ledger", "fs_ledger")
    _write_list(tmp_path, first, second)
    sys_path = ["/core"]

    modules = _activate(tmp_path, sys_path=sys_path)

    assert [module.module_id for module in modules] == ["acme.accounts", "acme.ledger"]
    # The first module's copy wins by path order; the copies are identical.
    assert sys_path.index(str(modules[0].path / "site")) < sys_path.index(
        str(modules[1].path / "site")
    )


@pytest.mark.parametrize(
    ("second_version", "second_body", "message"),
    [
        ("2.0.0", "", "ships Authlib"),
        ("2.1.0", "PATCHED = True\n", "different distribution versions or contents"),
    ],
)
def test_two_modules_with_a_differing_bundled_distribution_are_refused(
    tmp_path: Path, second_version: str, second_body: str, message: str
) -> None:
    first = _bundling_module(tmp_path, "acme.accounts", "fs_accounts")
    second = _bundling_module(
        tmp_path, "acme.ledger", "fs_ledger", version=second_version, body=second_body
    )
    _write_list(tmp_path, first, second)

    with pytest.raises(PlatformModuleConflictError, match=message):
        _activate(tmp_path)


def test_own_package_stays_refused_even_when_an_installed_distribution_provides_it(
    tmp_path: Path,
) -> None:
    core_root = tmp_path / "core-site"
    _write(core_root / "fs_accounts/__init__.py", "")
    _write(
        core_root / "fs_accounts-1.2.0.dist-info/METADATA", "Name: fs-accounts\nVersion: 1.2.0\n"
    )
    _write(core_root / "fs_accounts-1.2.0.dist-info/top_level.txt", "fs_accounts\n")
    modules_root = tmp_path / "modules"
    _write_list(modules_root, _make_module(modules_root))

    with pytest.raises(PlatformModuleConflictError, match="'fs_accounts' is already importable"):
        _activate(modules_root, sys_path=[str(core_root)], installed_version=None)


# --- image_checks=False: artifact-only verification for the management image -------


def _module_vendoring_pytest(root: Path) -> dict:
    """Vendors a distribution this test process imports at another version."""
    return _make_module(
        root,
        files={
            "src/fs_accounts/__init__.py": "",
            "site/pytest/__init__.py": "",
            "site/pytest-0.0.1.dist-info/METADATA": "Name: pytest\nVersion: 0.0.1\n",
            "site/pytest-0.0.1.dist-info/top_level.txt": "pytest\n",
        },
    )


def test_image_checks_off_accepts_a_distribution_the_image_has_at_another_version(
    tmp_path: Path,
) -> None:
    _write_list(tmp_path, _module_vendoring_pytest(tmp_path))
    sys_path = list(sys.path)

    (module,) = _activate(tmp_path, sys_path=sys_path, installed_version=None, image_checks=False)

    assert module.module_id == "acme.accounts"
    assert sys_path[-2:] == [str(module.path / "src"), str(module.path / "site")]


def test_image_checks_on_refuses_a_distribution_the_image_has_at_another_version(
    tmp_path: Path,
) -> None:
    _write_list(tmp_path, _module_vendoring_pytest(tmp_path))
    sys_path = list(sys.path)

    with pytest.raises(PlatformModuleConflictError, match=r"ships pytest 0\.0\.1"):
        _activate(tmp_path, sys_path=sys_path, installed_version=None)


@pytest.mark.parametrize("image_checks", [True, False])
def test_integrity_failures_are_refused_with_and_without_image_checks(
    tmp_path: Path, image_checks: bool
) -> None:
    _write_list(tmp_path, _make_module(tmp_path))
    _write(tmp_path / "acme.accounts/1.2.0/src/fs_accounts/__init__.py", "VALUE = 2\n")

    with pytest.raises(PlatformModuleIntegrityError, match="has sha256"):
        _activate(tmp_path, image_checks=image_checks)


@pytest.mark.parametrize("image_checks", [True, False])
def test_artifact_name_rules_apply_with_and_without_image_checks(
    tmp_path: Path, image_checks: bool
) -> None:
    first = _bundling_module(tmp_path, "acme.accounts", "fs_accounts")
    second = _bundling_module(tmp_path, "acme.ledger", "fs_ledger", body="PATCHED = True\n")
    _write_list(tmp_path, first, second)

    with pytest.raises(PlatformModuleConflictError, match="different distribution versions"):
        _activate(tmp_path, image_checks=image_checks)


# --- installed distributions without RECORD or top_level.txt ------------------------


def test_installed_names_fall_back_to_packages_distributions(tmp_path: Path) -> None:
    site = tmp_path / "os-site"
    _write(site / "FakeDist-1.0.dist-info/METADATA", "Name: FakeDist\nVersion: 1.0\n")

    names = platform_modules._installed_top_level_names(
        "FakeDist",
        [str(site)],
        packages_distributions=lambda: {"fakedist": ["fakedist"], "other": ["Other"]},
    )
    missing = platform_modules._installed_top_level_names(
        "FakeDist", [str(site)], packages_distributions=lambda: {"other": ["Other"]}
    )

    assert names == {"fakedist"}
    assert missing == set()
