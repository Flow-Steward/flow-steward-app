from __future__ import annotations

import ast
import gettext
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "infra" / "docker" / "materialize_source_slice.py"
MANIFEST_ROOT = ROOT / "infra" / "docker" / "source-slices"
ROLES = ("app", "worker", "control")
FORBIDDEN_PREFIXES = (
    "backups/",
    "docs/",
    "extensions/",
    "logs/",
    "tests/",
)
ALLOWED_PREFIXES = ("core/", "db/", "locales/")
FORBIDDEN_BINARY_SUFFIXES = {
    ".a",
    ".dylib",
    ".dll",
    ".exe",
    ".gz",
    ".so",
    ".tar",
    ".whl",
    ".zip",
}
ROLE_FORBIDDEN_ENTRYPOINTS = {
    "app": {
        "core/entrypoints/cli/control_process.py",
        "core/entrypoints/cli/worker_process.py",
    },
    "worker": {
        "core/entrypoints/cli/control_process.py",
    },
    "control": {
        "core/entrypoints/cli/worker_process.py",
    },
}
ROLE_FORBIDDEN_ENTRYPOINT_PREFIXES = {
    "app": (),
    "worker": (
        "core/entrypoints/admin/",
        "core/entrypoints/api/",
        "core/entrypoints/web/",
        "core/entrypoints/ws/",
    ),
    "control": (
        "core/entrypoints/admin/",
        "core/entrypoints/api/",
        "core/entrypoints/web/",
        "core/entrypoints/ws/",
    ),
}
REQUIRED_PACKAGE_DATA = {
    "app": {
        "VERSION",
        "core/entrypoints/cli/scripts/frontend_shell_freshness.sh",
        "core/entrypoints/cli/scripts/start_server.sh",
        "core/infrastructure/config/flow_steward_env_manifest.json",
        "core/infrastructure/security/default_policy.yaml",
        "db/seeds/lookup_seed.json",
        "db/seeds/registry_seed.json",
    },
    "worker": {
        "VERSION",
        "core/infrastructure/config/flow_steward_env_manifest.json",
        "core/infrastructure/security/default_policy.yaml",
        "db/seeds/lookup_seed.json",
        "db/seeds/registry_seed.json",
    },
    "control": {
        "VERSION",
        "core/infrastructure/config/flow_steward_env_manifest.json",
        "core/infrastructure/security/default_policy.yaml",
        "db/seeds/lookup_seed.json",
        "db/seeds/registry_seed.json",
    },
}
CANONICAL_MIGRATION_PATHS = {
    path.relative_to(ROOT).as_posix() for path in (ROOT / "db" / "migrations").glob("*.sql")
}
CONTROL_FORBIDDEN_SOURCE_PREFIXES = (
    "core/application/indexing/",
    "core/application/orchestration/model_provider",
    "core/application/orchestration/tabular_feed_step_runtime.py",
    "core/application/tabular_feed/",
    "core/infrastructure/tabular_feed/",
)
WORKER_RUNTIME_PRIMITIVES = {
    "core/infrastructure/runtime/worker/__init__.py",
    "core/infrastructure/runtime/worker/consumer_observer.py",
    "core/infrastructure/runtime/worker/queue.py",
    # Same transport primitives as queue.py; execution stays outside app/control.
    "core/infrastructure/runtime/worker/queue_broker.py",
    "core/infrastructure/runtime/worker/queue_messages.py",
    "core/infrastructure/runtime/worker/queue_publisher.py",
    "core/infrastructure/runtime/worker/queue_lease_store.py",
    "core/infrastructure/runtime/worker/queue_names.py",
    "core/infrastructure/runtime/worker/queue_pika_compat.py",
}
# Every retained internal tool module is statically reachable, so the resolver contributes
# no dynamic path of its own.
RUNTIME_TOOL_RESOLVER_DYNAMIC_PATHS: dict[str, str] = {}


def _source_slice_module():
    assert HELPER.is_file(), "the build-only source-slice helper is missing"
    import importlib.util

    spec = importlib.util.spec_from_file_location("runtime_source_slice", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest_payload(role: str) -> dict:
    path = MANIFEST_ROOT / f"{role}.json"
    assert path.is_file(), f"missing committed source-slice manifest: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _all_manifest_paths(payload: dict) -> set[str]:
    static_paths = set(payload["static_paths"])
    dynamic_paths = {item["path"] for item in payload["dynamic_paths"]}
    package_data_paths = {item["path"] for item in payload["package_data_paths"]}
    return static_paths | dynamic_paths | package_data_paths


def test_source_slice_helper_materializes_only_declared_files_deterministically(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    source_root = tmp_path / "source"
    output_root = tmp_path / "out"
    manifest_path = tmp_path / "manifest.json"
    (source_root / "core" / "demo").mkdir(parents=True)
    source_file = source_root / "core" / "demo" / "worker.py"
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    source_file.chmod(0o744)
    (source_root / "core" / "demo" / "ignored.py").write_text("IGNORED = True\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "worker",
                "entrypoints": ["core.demo.worker"],
                "static_paths": ["core/demo/worker.py"],
                "dynamic_paths": [],
                "package_data_paths": [],
            }
        ),
        encoding="utf-8",
    )

    module.materialize_source_slice(
        source_root=source_root,
        manifest_path=manifest_path,
        output_root=output_root,
    )

    copied = output_root / "core" / "demo" / "worker.py"
    assert copied.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (output_root / "core" / "demo" / "ignored.py").exists()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert copied.stat().st_mtime == 0


@pytest.mark.parametrize("drift", ["missing", "extra"])
def test_materialization_refuses_stale_static_closure_before_creating_output(tmp_path, drift):
    module = _source_slice_module()
    source = tmp_path / "source"
    package = source / "core/demo"
    package.mkdir(parents=True)
    (package / "worker.py").write_text("from core.demo import dependency\n")
    (package / "dependency.py").write_text("VALUE = 1\n")
    (package / "unused.py").write_text("UNUSED = True\n")
    paths = ["core/demo/worker.py", "core/demo/dependency.py"]
    if drift == "missing":
        paths.remove("core/demo/dependency.py")
    else:
        paths.append("core/demo/unused.py")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "worker",
                "entrypoints": ["core.demo.worker"],
                "static_paths": paths,
                "dynamic_paths": [],
                "package_data_paths": [],
            }
        )
    )
    output = tmp_path / "not-created/output"
    with pytest.raises(ValueError, match="static closure mismatch"):
        module.materialize_source_slice(
            source_root=source, manifest_path=manifest, output_root=output
        )
    assert not output.parent.exists()


def test_source_slice_helper_rejects_paths_outside_the_source_root(tmp_path: Path) -> None:
    module = _source_slice_module()
    source_root = tmp_path / "source"
    source_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "app",
                "entrypoints": ["core.entrypoints.api.app"],
                "static_paths": ["../secret.txt"],
                "dynamic_paths": [],
                "package_data_paths": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relative canonical path"):
        module.materialize_source_slice(
            source_root=source_root,
            manifest_path=manifest_path,
            output_root=tmp_path / "out",
        )


def test_source_slice_graph_reports_the_shortest_import_predecessor_path(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    source_root = tmp_path / "source"
    package = source_root / "core" / "demo"
    package.mkdir(parents=True)
    (source_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "entry.py").write_text(
        "from core.demo import middle\nfrom core.demo import longer\n",
        encoding="utf-8",
    )
    (package / "middle.py").write_text("from core.demo import target\n", encoding="utf-8")
    (package / "longer.py").write_text("from core.demo import detour\n", encoding="utf-8")
    (package / "detour.py").write_text("from core.demo import target\n", encoding="utf-8")
    (package / "target.py").write_text("VALUE = 1\n", encoding="utf-8")

    paths = module.shortest_local_import_paths(
        source_root=source_root,
        entrypoint="core.demo.entry",
        target_modules=("core.demo.target",),
    )

    assert paths["core.demo.target"] == (
        "core.demo.entry",
        "core.demo.middle",
        "core.demo.target",
    )


def test_static_source_slice_closure_follows_package_initializer_imports(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    source_root = tmp_path / "source"
    package = source_root / "core" / "demo"
    package.mkdir(parents=True)
    (source_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("from core.demo.contract import VALUE\n", encoding="utf-8")
    (package / "contract.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "entry.py").write_text("VALUE = 2\n", encoding="utf-8")

    closure = module.static_python_paths(
        source_root=source_root,
        entrypoints=("core.demo.entry",),
    )

    assert "core/demo/__init__.py" in closure
    assert "core/demo/contract.py" in closure


def test_refresh_promotes_new_static_imports_out_of_documented_non_static_sections(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    source_root = tmp_path / "source"
    package = source_root / "core" / "demo"
    package.mkdir(parents=True)
    (source_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "entry.py").write_text("from core.demo import promoted\n", encoding="utf-8")
    (package / "promoted.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "dynamic.py").write_text("VALUE = 2\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "app",
                "entrypoints": ["core.demo.entry"],
                "static_paths": [],
                "dynamic_paths": [{"path": "core/demo/dynamic.py", "consumer": "runtime import"}],
                "package_data_paths": [
                    {"path": "core/demo/promoted.py", "consumer": "legacy package data"}
                ],
            }
        ),
        encoding="utf-8",
    )

    module.refresh_manifest_static_paths(
        source_root=source_root,
        manifest_path=manifest_path,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "core/demo/promoted.py" in payload["static_paths"]
    assert payload["dynamic_paths"] == [
        {"path": "core/demo/dynamic.py", "consumer": "runtime import"}
    ]
    assert payload["package_data_paths"] == []


@pytest.mark.parametrize("role", ROLES)
def test_source_slice_helper_materializes_the_committed_role_manifest(
    role: str, tmp_path: Path
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    payload = _manifest_payload(role)

    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )

    materialized = {
        path.relative_to(output_root).as_posix()
        for path in output_root.rglob("*")
        if path.is_file()
    }
    assert materialized == _all_manifest_paths(payload)


@pytest.mark.parametrize("role", ("app", "compact"))
def test_materialized_ui_slice_packages_english_and_lithuanian_catalogs(
    role: str, tmp_path: Path
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    locales_root = output_root / "locales"
    assert {path.name for path in locales_root.iterdir() if path.is_dir()} == {"en", "lt"}
    for locale, expected_label in (("en", "Workflows"), ("lt", "Darbo eigos")):
        catalog = gettext.translation("ui", localedir=locales_root, languages=[locale])
        assert catalog.gettext("shell.nav.workflows") == expected_label
        metadata = json.loads((locales_root / locale / "meta.json").read_text(encoding="utf-8"))
        assert metadata["code"] == locale


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_contains_the_real_static_entrypoint_closure(role: str) -> None:
    module = _source_slice_module()
    payload = _manifest_payload(role)

    actual_closure = module.static_python_paths(
        source_root=ROOT,
        entrypoints=tuple(payload["entrypoints"]),
    )

    assert actual_closure <= set(payload["static_paths"])


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_has_no_unexplained_static_extras(role: str) -> None:
    module = _source_slice_module()
    payload = _manifest_payload(role)
    actual_closure = module.static_python_paths(
        source_root=ROOT,
        entrypoints=tuple(payload["entrypoints"]),
    )

    assert set(payload["static_paths"]) == actual_closure


def test_compact_manifest_is_an_exact_checked_runtime_union() -> None:
    module = _source_slice_module()
    payload = _manifest_payload("compact")

    actual_closure = module.static_python_paths(
        source_root=ROOT,
        entrypoints=tuple(payload["entrypoints"]),
    )
    paths = _all_manifest_paths(payload)

    assert set(payload["static_paths"]) == actual_closure
    assert {
        "core.entrypoints.cli.compact_provider_runtime",
        "core.entrypoints.cli.compact_runtime",
        "core.entrypoints.api.app",
        "core.entrypoints.cli.worker_process",
        "core.entrypoints.cli.control_process",
        "core.entrypoints.mcp.server",
    } <= set(payload["entrypoints"])
    assert not any(path.startswith(FORBIDDEN_PREFIXES) for path in paths)
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    compact_stage = dockerfile.split("FROM base AS compact-runtime", 1)[1].split(
        "FROM audit-tooling AS audit", 1
    )[0]
    assert "COPY core/" not in compact_stage
    assert "COPY extensions/" not in compact_stage
    assert "COPY --from=source-slices /out/compact/ /app/" in compact_stage
    assert (
        'ENTRYPOINT ["python", "-m", "core.entrypoints.cli.compact_provider_runtime"]'
        in compact_stage
    )
    compact_stage_lines = {
        line.strip().removeprefix("&& ").removesuffix(" \\") for line in compact_stage.splitlines()
    }
    assert "ln -s /data/projects /project" not in compact_stage_lines
    assert "ln -s /data/projects /projects" not in compact_stage_lines
    assert "core/infrastructure/runtime/compact/router.yaml.tmpl" in {
        item["path"] for item in payload["package_data_paths"]
    }


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_contains_encrypted_external_effect_replay_closure(role: str) -> None:
    payload = _manifest_payload(role)
    paths = set(payload["static_paths"])

    assert {
        "core/application/extensions/external_effect_replay.py",
        "core/application/extensions/external_effects.py",
        "core/infrastructure/security/external_effect_replay_codec.py",
    } <= paths

    if role in {"app", "worker"}:
        assert {
            "core/application/extensions/operation_service.py",
            "core/application/management/command_services_dispatch.py",
            "core/application/orchestration/connector_step_runtime.py",
            "core/application/notifications/delivery.py",
        } <= paths
    else:
        assert "core/application/notifications/delivery.py" in paths


def test_app_manifest_contains_the_installer_database_bootstrap_closure() -> None:
    module = _source_slice_module()
    payload = _manifest_payload("app")
    required_entrypoints = {
        "core.application.tooling.db.bootstrap_seed",
        "core.application.tooling.db.upgrade_db",
    }

    assert required_entrypoints <= set(payload["entrypoints"])
    bootstrap_closure = module.static_python_paths(
        source_root=ROOT,
        entrypoints=tuple(sorted(required_entrypoints)),
    )
    assert bootstrap_closure <= set(payload["static_paths"])
    assert {
        "core/application/tooling/db/init_db.py",
        "core/application/tooling/db/migration_runner.py",
        "core/application/tooling/db/upgrade_db.py",
        "core/application/tooling/db/vector_lifecycle.py",
    } <= bootstrap_closure


def test_app_manifest_contains_the_worker_identity_bootstrap_closure() -> None:
    module = _source_slice_module()
    payload = _manifest_payload("app")
    entrypoint = "core.entrypoints.cli.ops.worker_identity_bootstrap"

    assert entrypoint in payload["entrypoints"]
    assert "core.application.installer" in payload["entrypoints"]
    assert "core.application.lifecycle" in payload["entrypoints"]
    bootstrap_closure = module.static_python_paths(
        source_root=ROOT,
        entrypoints=(entrypoint,),
    )
    assert bootstrap_closure <= set(payload["static_paths"])
    assert {
        "core/application/auth/worker_identity_bootstrap.py",
        "core/entrypoints/cli/ops/worker_identity_bootstrap.py",
        "core/infrastructure/installer/worker_identity_secret_store.py",
    } <= bootstrap_closure


def test_refresh_retains_the_declared_extension_dependencies_compose_module(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    payload = _manifest_payload("app")
    compose_module = "core.infrastructure.extension_runtime.bundled_dependencies"
    manifest_path = tmp_path / "app.json"

    assert compose_module in payload["entrypoints"]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    module.refresh_manifest_static_paths(source_root=ROOT, manifest_path=manifest_path)

    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert (
        "core/infrastructure/extension_runtime/bundled_dependencies.py" in refreshed["static_paths"]
    )


def test_app_slice_declares_product_cli_entrypoint() -> None:
    payload = _manifest_payload("app")

    assert "core.entrypoints.cli.click_app" in payload["entrypoints"]


def test_materialized_app_slice_can_render_product_cli_help_without_repository_fallback(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / "app"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "app.json",
        output_root=output_root,
    )
    isolated_module_run = (
        "import runpy, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "sys.argv = ['flow-steward', 'extensions', 'install', '--help']; "
        "runpy.run_module('core.entrypoints.cli.click_app', run_name='__main__')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_run, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Install an extension archive" in result.stdout


@pytest.mark.parametrize(
    "domain",
    (
        "users",
        "extensions",
        "projects",
        "workflows",
        "jobs",
        "reports",
        "database",
        "release",
        "clone",
        "localization",
    ),
)
def test_materialized_app_slice_imports_product_cli_binding_without_repository_fallback(
    tmp_path: Path,
    domain: str,
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / "app"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "app.json",
        output_root=output_root,
    )
    isolated_module_import = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module(sys.argv[2])"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            isolated_module_import,
            str(output_root),
            f"core.entrypoints.cli.platform.service_bindings.{domain}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_materialized_app_slice_imports_worker_identity_bootstrap_without_repository_fallback(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / "app"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "app.json",
        output_root=output_root,
    )
    isolated_module_import = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module('core.entrypoints.cli.ops.worker_identity_bootstrap')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_import, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", ("app", "worker", "compact"))
def test_materialized_runtime_slice_imports_installer_state_without_repository_fallback(
    tmp_path: Path,
    role: str,
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    isolated_module_import = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module('core.infrastructure.installer.state_store')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_import, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", ROLES)
def test_materialized_role_slice_imports_extension_lifecycle_ports_without_repository_fallback(
    tmp_path: Path, role: str
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    isolated_module_import = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module('core.application.extensions.lifecycle_ports')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_import, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", ("worker", "control"))
def test_materialized_background_role_slice_contains_runtime_env_contract(
    tmp_path: Path, role: str
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )

    materialized = output_root / "core/infrastructure/config/flow_steward_env_manifest.json"
    assert (
        materialized.read_bytes()
        == (ROOT / "core/infrastructure/config/flow_steward_env_manifest.json").read_bytes()
    )


@pytest.mark.parametrize(
    ("role", "entrypoint"),
    (
        ("worker", "core.entrypoints.cli.worker_process"),
        ("control", "core.entrypoints.cli.control_process"),
    ),
)
def test_materialized_background_role_imports_entrypoint_without_repository_fallback(
    tmp_path: Path, role: str, entrypoint: str
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    isolated_module_import = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module(sys.argv[2])"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_import, str(output_root), entrypoint],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_app_registry_package_initializer_does_not_pull_manifest_facades() -> None:
    module = _source_slice_module()
    payload = _manifest_payload("app")
    registry_root = "core.application.registry"

    assert registry_root in payload["entrypoints"]
    entrypoints_without_registry = tuple(
        entrypoint for entrypoint in payload["entrypoints"] if entrypoint != registry_root
    )
    closure_without_registry = module.static_python_paths(
        source_root=ROOT,
        entrypoints=entrypoints_without_registry,
    )
    closure_with_registry = module.static_python_paths(
        source_root=ROOT,
        entrypoints=tuple(payload["entrypoints"]),
    )
    registry_delta = closure_with_registry - closure_without_registry

    assert registry_delta == set()


@pytest.mark.parametrize("role", (*ROLES, "compact"))
def test_materialized_runtime_slice_inspects_malformed_boundary_entry_without_repository_fallback(
    tmp_path: Path, role: str
) -> None:
    """Omitting the boundary-contract owner breaks installed bootstrap/read projections."""
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    isolated_inspection = (
        "import json, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from core.application.registry.io_resolver import IOResolver; "
        "from core.application.registry.boundary_contracts import boundary_entry_inspection; "
        "step = {'step_id': 'first', 'boundary_contract': {'entry': ['payload']}}; "
        "result = boundary_entry_inspection({'steps': [step]}, step); "
        "valid_step = {'step_id': 'second', 'boundary_contract': {'entry': "
        "[{'name': 'payload', 'type': 'json', 'required': True, 'source': 'state.inputs.payload'}]}}; "
        "valid = boundary_entry_inspection({'steps': [valid_step]}, valid_step); "
        "print(json.dumps({'invalid': result, 'valid': valid}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_inspection, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    inspection = payload["invalid"]
    assert inspection["expected_inputs"] == []
    assert inspection["raw_expected_inputs"] == ["payload"]
    assert inspection["expected_input_issues"] == [
        {
            "code": "contract.item_not_object",
            "message": "Contract item must be an object",
            "path": "workflow.steps.first.boundary_contract.entry[0]",
            "severity": "error",
        }
    ]
    assert payload["valid"] == {
        "expected_inputs": [
            {"name": "payload", "type": "json", "required": True, "source": "state.inputs.payload"}
        ]
    }


def test_materialized_app_slice_imports_installer_seed_without_repository_fallback(
    tmp_path: Path,
) -> None:
    module = _source_slice_module()
    output_root = tmp_path / "app"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "app.json",
        output_root=output_root,
    )
    isolated_module_run = (
        "import runpy, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "sys.argv = ['bootstrap_seed', '--help']; "
        "runpy.run_module('core.application.tooling.db.bootstrap_seed', run_name='__main__')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_module_run, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--author" in result.stdout


@pytest.mark.parametrize("role", ("app", "compact"))
def test_materialized_database_upgrade_imports_its_full_maintenance_closure(
    tmp_path: Path, role: str
) -> None:
    """Canonical upgrades retain seeds, AGE, extensions, and checkpoint initialization."""
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / f"{role}.json",
        output_root=output_root,
    )
    isolated_import = """
import importlib
import sys

sys.path.insert(0, sys.argv[1])
modules = (
    "core.application.tooling.db.upgrade_db",
    "core.application.tooling.db.bootstrap_seed",
    "core.application.extensions.install",
    "core.application.extensions.extension_scan_lifecycle",
    "core.infrastructure.db.workflow_knowledge_store",
    "core.infrastructure.extension_runtime.bundle_cleanup",
    "core.infrastructure.extension_runtime.extension_runtime",
    "core.infrastructure.extension_runtime.lifecycle_lock",
    "core.infrastructure.extension_runtime.publication_recovery",
)
for name in modules:
    imported = importlib.import_module(name)
    assert imported.__file__.startswith(sys.argv[1]), (name, imported.__file__)
from core.application.tooling.db.bootstrap_seed import apply_bootstrap_seed
from core.application.tooling.db.upgrade_db import upgrade_database
from langgraph.checkpoint.postgres import PostgresSaver
assert callable(apply_bootstrap_seed)
assert callable(upgrade_database)
assert callable(PostgresSaver.setup)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", isolated_import, str(output_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("role", (*ROLES, "compact"))
def test_role_manifest_documents_literal_dynamic_imports(role: str) -> None:
    module = _source_slice_module()
    payload = _manifest_payload(role)
    dynamic_imports = module.literal_dynamic_python_paths(
        source_root=ROOT,
        python_paths=tuple(payload["static_paths"]),
    )
    documented = {item["path"]: item["consumer"] for item in payload["dynamic_paths"]}

    assert set(dynamic_imports) - set(payload["static_paths"]) <= set(documented)
    for path, consumer in dynamic_imports.items():
        if path not in payload["static_paths"]:
            assert documented[path] == consumer


@pytest.mark.parametrize("role", ("app", "worker"))
def test_runtime_tool_resolver_fallback_modules_are_packaged(role: str) -> None:
    payload = _manifest_payload(role)
    documented = {item["path"]: item["consumer"] for item in payload["dynamic_paths"]}
    resolver_tree = ast.parse(
        (ROOT / "core/application/orchestration/tools/tool_resolver.py").read_text(encoding="utf-8")
    )
    fallback_modules: dict[str, str] | None = None
    for statement in resolver_tree.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if isinstance(statement.target, ast.Name) and (
            statement.target.id == "_INTERNAL_TOOL_MODULE_FALLBACKS"
        ):
            fallback_modules = ast.literal_eval(statement.value)
            break

    assert fallback_modules is not None
    static_paths = set(payload["static_paths"])
    expected_dynamic = {
        f"{module_name.replace('.', '/')}.py": (
            "core.application.orchestration.tools.tool_resolver:"
            f"_INTERNAL_TOOL_MODULE_FALLBACKS[{tool_id}]"
        )
        for tool_id, module_name in fallback_modules.items()
        if f"{module_name.replace('.', '/')}.py" not in static_paths
    }

    assert expected_dynamic == RUNTIME_TOOL_RESOLVER_DYNAMIC_PATHS
    resolver_documented = {
        path: consumer
        for path, consumer in documented.items()
        if consumer.startswith("core.application.orchestration.tools.tool_resolver:")
    }
    assert resolver_documented == expected_dynamic


@pytest.mark.parametrize("role", ("app", "worker", "compact"))
def test_builtin_standard_tool_modules_are_packaged(role: str) -> None:
    """A Standard Tool Action names its module in data, so no import reaches it.

    Without it every Standard Tool Action step failed with "No module named
    core.application.orchestration.tools.standard_tools".
    """
    from core.application.registry.builtin_standard_tools import (
        builtin_standard_tool_manifests,
    )

    payload = _manifest_payload(role)
    packaged = _all_manifest_paths(payload)
    modules = {
        str(manifest["implementation"]["module"])
        for manifest in builtin_standard_tool_manifests().values()
    }

    assert modules
    for module_name in modules:
        assert f"{module_name.replace('.', '/')}.py" in packaged


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_paths_are_exact_existing_allowed_files(role: str) -> None:
    payload = _manifest_payload(role)
    paths = _all_manifest_paths(payload)

    assert paths
    assert len(paths) == (
        len(payload["static_paths"])
        + len(payload["dynamic_paths"])
        + len(payload["package_data_paths"])
    )
    for relative in paths:
        path = Path(relative)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert relative == path.as_posix()
        assert relative == relative.strip("/")
        assert not relative.startswith(FORBIDDEN_PREFIXES)
        assert relative == "VERSION" or relative.startswith(ALLOWED_PREFIXES)
        assert path.suffix.lower() not in FORBIDDEN_BINARY_SUFFIXES
        assert (ROOT / path).is_file(), f"{role} manifest path does not exist: {relative}"

    assert paths.isdisjoint(ROLE_FORBIDDEN_ENTRYPOINTS[role])
    assert not any(path.startswith(ROLE_FORBIDDEN_ENTRYPOINT_PREFIXES[role]) for path in paths)


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_documents_every_non_static_consumer(role: str) -> None:
    payload = _manifest_payload(role)

    for section in ("dynamic_paths", "package_data_paths"):
        for item in payload[section]:
            assert set(item) == {"path", "consumer"}
            assert str(item["consumer"]).strip()


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_contains_required_runtime_package_data(role: str) -> None:
    payload = _manifest_payload(role)
    package_data = {item["path"] for item in payload["package_data_paths"]}

    assert REQUIRED_PACKAGE_DATA[role] <= package_data


_FILE_NAME = re.compile(r"[\w.-]+(?:/[\w.-]+)*")


def _module_relative_resources(module: str) -> set[str]:
    """Files a module names by a string relative to its own directory -- the way
    Textual's CSS_PATH, and any `Path(__file__).parent / "x.yaml"`, reach them."""
    base = Path(module).parent
    found: set[str] = set()
    for node in ast.walk(ast.parse((ROOT / module).read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value = node.value
        if "." not in value or value.startswith("/") or not _FILE_NAME.fullmatch(value):
            continue
        candidate = base / value
        if ".." in candidate.parts:
            continue
        if candidate.suffix != ".py" and (ROOT / candidate).is_file():
            found.add(candidate.as_posix())
    return found


@pytest.mark.parametrize("role", (*ROLES, "compact"))
def test_every_resource_a_packaged_module_names_is_packaged_too(role: str) -> None:
    """AC17-R154: theme.tcss stayed in the checkout while form_app.py, which names it,
    was packaged, so every interactive command failed on an installed image. The
    closure follows imports only; a file named next to the module was invisible to it."""
    payload = _manifest_payload(role)
    packaged = _all_manifest_paths(payload)
    named = {
        resource: module
        for module in payload["static_paths"]
        for resource in _module_relative_resources(module)
    }

    # non-vacuity: every role reads its security policy this way
    assert "core/infrastructure/security/default_policy.yaml" in named
    missing = {resource: module for resource, module in named.items() if resource not in packaged}
    assert missing == {}


@pytest.mark.parametrize("role", ROLES)
def test_schema_contract_roles_include_canonical_migration_history(role: str) -> None:
    payload = _manifest_payload(role)
    static_paths = set(payload["static_paths"])
    package_data = {item["path"] for item in payload["package_data_paths"]}

    assert "core/infrastructure/db/schema_contract.py" in static_paths
    assert package_data >= CANONICAL_MIGRATION_PATHS


def test_control_manifest_excludes_product_execution_families() -> None:
    paths = _all_manifest_paths(_manifest_payload("control"))

    forbidden = {
        path
        for path in paths
        if path.startswith(CONTROL_FORBIDDEN_SOURCE_PREFIXES)
        or (
            path.startswith("core/infrastructure/runtime/worker/")
            and path not in WORKER_RUNTIME_PRIMITIVES
        )
    }

    assert not forbidden


def test_app_manifest_excludes_worker_execution_modules() -> None:
    paths = _all_manifest_paths(_manifest_payload("app"))

    forbidden = {
        path
        for path in paths
        if path.startswith("core/infrastructure/runtime/worker/")
        and path not in WORKER_RUNTIME_PRIMITIVES
    }

    assert not forbidden


@pytest.mark.parametrize("role", ROLES)
def test_role_manifest_includes_python_package_ancestors(role: str) -> None:
    payload = _manifest_payload(role)
    paths = _all_manifest_paths(payload)

    for relative in paths:
        path = Path(relative)
        if path.suffix != ".py":
            continue
        parent = path.parent
        while parent != Path("."):
            init_path = (parent / "__init__.py").as_posix()
            if (ROOT / init_path).is_file():
                assert init_path in paths, f"{role} omits package ancestor {init_path}"
            parent = parent.parent


# AC17-R112: the one import that is lazy on purpose. command_specs/__init__.py resolves
# the registry inside the functions that need it so contracts-only callers do not pull
# it in, and says so in its own docstring. Named here rather than pattern-matched, so
# adding a second deliberate exception is a decision someone has to write down.
_DELIBERATELY_LAZY = {
    "core/application/management/command_specs/__init__.py": frozenset(
        {"core.application.management.command_specs.registry"}
    )
}


def _runtime_core_imports(path: Path) -> set[str]:
    """Every core module this file imports when it runs, at any nesting depth.

    Deliberately not the static closure: a `from ... import ...` inside a function
    body is exactly what the manifest generator cannot see, and exactly what shipped
    an image without a module five callers needed. TYPE_CHECKING blocks are skipped
    because nothing imports them at runtime.

    Relative imports are resolved against the file's own package rather than ignored.
    The first version matched only `node.level == 0`, which left the intersection of
    the two blind spots open -- the generator does not follow imports inside
    functions, this did not follow relative ones, and `from .neighbour import thing`
    inside a function was invisible to both. core/ has 95 of that exact shape today.

    This stays an analysis rather than an execution check, and the distinction is not
    a preference: importing a module does not run its function bodies, so materialising
    a slice and importing everything in it would not have caught AC17-R112 at all --
    the import that was missing sat inside a function nobody called during import. The
    two approaches see different things and neither replaces the other.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    type_only: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = getattr(test, "id", None) or getattr(test, "attr", None)
            if name == "TYPE_CHECKING":
                for child in node.body:
                    for inner in ast.walk(child):
                        type_only.add(id(inner))
    package = path.relative_to(ROOT).parent.parts
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_only:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join((*anchor, node.module)) if node.module else ".".join(anchor)
            if not base.startswith("core."):
                continue
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("core."))
    return found


@pytest.mark.parametrize("role", (*ROLES, "compact"))
def test_every_module_a_packaged_file_imports_is_packaged_too(role: str) -> None:
    """A file in the image whose import is not in the image fails at the first request.

    AC17-R112: runtime_containers was imported by five live modules, was correct in
    the tree, passed its tests, and was absent from every slice, because the imports
    were written inside the functions that used them and the manifest generator only
    follows module-level ones. Task submission, the monitoring summary and the
    deployment doctor all answered 500 on the built image -- the doctor being the tool
    that exists for exactly that situation.

    The closure generator remains the mechanism; this only refuses to let an import
    hide from it.
    """
    packaged = set(
        json.loads((MANIFEST_ROOT / f"{role}.json").read_text(encoding="utf-8"))["static_paths"]
    )
    assert packaged, f"{role}.json declares no static paths; this test would be vacuous"

    def resolved(target: str) -> bool:
        path = target.replace(".", "/")
        return f"{path}.py" in packaged or f"{path}/__init__.py" in packaged

    unpackaged: dict[str, str] = {}
    for relative in sorted(packaged):
        source = ROOT / relative
        if source.suffix != ".py" or not source.is_file():
            continue
        allowed = _DELIBERATELY_LAZY.get(relative, frozenset())
        for target in sorted(_runtime_core_imports(source)):
            # the exception names a module; `from that module import name` is the
            # same import seen from one level down
            if any(target == name or target.startswith(f"{name}.") for name in allowed):
                continue
            if resolved(target):
                continue
            # `from package import name` where the package itself is packaged
            parent = target.rsplit(".", 1)[0]
            if parent != target and resolved(parent):
                continue
            unpackaged.setdefault(target, relative)

    assert unpackaged == {}, f"the {role} image imports modules it does not contain: " + "; ".join(
        f"{module} (from {source})" for module, source in sorted(unpackaged.items())
    )


def _command_service_modules() -> dict[str, set[str]]:
    """Binding module -> the reachable commands that name it, read from the registry.

    AC17-R118: a command names its service with a string that ServiceRegistry resolves
    at call time, so nothing imports the binding module and the static closure cannot
    see it. service_bindings/__init__.py says why there is no aggregate import --
    loading one binding must not initialise every command domain in the standalone CLI
    -- so this is the boundary of import analysis rather than a hole in it.
    """
    from core.application.management.command_specs.contracts import CompactSupport
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    reachable: dict[str, set[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if spec.compact_support not in {CompactSupport.PLATFORM, CompactSupport.PRODUCT}:
            continue
        reachable.setdefault(spec.service_ref.split(".", 1)[0], set()).add(spec.command_id)
    return reachable


def test_the_compact_image_contains_the_binding_of_every_command_it_offers() -> None:
    """AC17-R118: the specification shipped and the code that executes it did not.

    `flow-steward proxy-fragment` passed all four availability surfaces and answered
    "An unexpected internal error occurred", because service_bindings/config.py was in
    no slice. The import guard could not have caught it: the dependency is a string,
    not an import. Declaring availability and packaging the executor are the same
    question, so this asks it once -- every command declared reachable on Compact must
    have its binding module in the image.
    """
    reachable = _command_service_modules()
    assert reachable, "no reachable commands found; this test would be vacuous"

    manifest = json.loads((MANIFEST_ROOT / "compact.json").read_text(encoding="utf-8"))
    packaged = {
        entry.rsplit("/", 1)[-1].removesuffix(".py")
        for entry in (
            *manifest["static_paths"],
            *(item["path"] for item in manifest.get("dynamic_paths") or ()),
        )
        if "/service_bindings/" in entry
    }
    assert packaged, "no binding modules packaged; this test would be vacuous"

    missing = {
        module: sorted(commands)
        for module, commands in sorted(reachable.items())
        if module not in packaged
    }

    assert missing == {}, (
        f"these commands are offered on Compact and their executor is not in the image: {missing}"
    )


def test_materialized_compact_imports_maintenance_readiness_and_clone_without_repository(tmp_path):
    module = _source_slice_module()
    output = tmp_path / "compact"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "compact.json",
        output_root=output,
    )
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime, _readiness_payload; "
        "from core.application.maintenance.compact_client import CompactMaintenanceClient; "
        "from core.entrypoints.cli.clone_composition import build_installed_clone_service; "
        "assert callable(_readiness_payload) and callable(build_installed_clone_service); "
        "assert callable(CompactMaintenanceClient.status); "
        "print('compact-imports-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(output)],
        cwd=tmp_path,
        env={"PATH": str(tmp_path / "no-docker"), "FS_DEPLOYMENT_PROFILE": "compact"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "compact-imports-ok"


def test_materialized_compact_slice_runs_the_doctor_without_repository_fallback(
    tmp_path: Path,
) -> None:
    """D4 live on fs-acc (02352066b): inside the Compact image `python -m
    core.entrypoints.cli.doctor` answered "No module named", and `platform_runtime doctor`
    failed importing it -- the slice had no doctor at all. Run from the slice alone,
    as the container runs it: no Docker on PATH, the image's profile."""
    module = _source_slice_module()
    output_root = tmp_path / "compact"
    module.materialize_source_slice(
        source_root=ROOT,
        manifest_path=MANIFEST_ROOT / "compact.json",
        output_root=output_root,
    )
    run_doctor = (
        "import runpy, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "sys.argv = ['doctor', '--json']; "
        "runpy.run_module('core.entrypoints.cli.doctor', run_name='__main__')"
    )
    console_imports = (
        "import importlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module('core.entrypoints.cli.platform_runtime'); "
        "importlib.import_module('core.entrypoints.cli.doctor')"
    )
    environment = {"PATH": str(tmp_path / "no-docker-here"), "FS_DEPLOYMENT_PROFILE": "compact"}

    doctor = subprocess.run(
        [sys.executable, "-I", "-c", run_doctor, str(output_root)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    console = subprocess.run(
        [sys.executable, "-I", "-c", console_imports, str(output_root)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert doctor.returncode == 5, doctor.stderr  # the installation was not checked
    assert json.loads(doctor.stdout)["mode"] == "no_docker"
    assert console.returncode == 0, console.stderr


@pytest.mark.parametrize("role", ["app", "compact"])
def test_every_ui_language_catalog_is_packaged_where_the_ui_is_served(role: str) -> None:
    # The interface lists the languages it finds on disk. A catalog left out of
    # the image is a language users can never pick, with no error anywhere.
    catalogs = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "locales").glob("*/**/*")
        if path.is_file()
    }
    payload = _manifest_payload(role)

    assert catalogs <= {item["path"] for item in payload["package_data_paths"]}


@pytest.mark.parametrize(
    "role,modules",
    [
        (
            "app",
            [
                "core.application.workflows.authoring.service",
                "core.application.workflows.workflow_knowledge_sync",
                "core.application.workflows.contract_sanitation",
                "core.application.reports.reporting_service",
                "core.application.reports.reporting_helpers",
            ],
        ),
        ("worker", ["core.application.workflows.workflow_knowledge_sync"]),
        (
            "compact",
            [
                "core.application.workflows.authoring.service",
                "core.application.workflows.workflow_knowledge_sync",
                "core.application.workflows.contract_sanitation",
                "core.application.reports.reporting_service",
                "core.application.reports.reporting_helpers",
            ],
        ),
    ],
)
def test_runtime_services_import_only_from_materialized_role_slice(role, modules, tmp_path):
    module = _source_slice_module()
    output_root = tmp_path / role
    module.materialize_source_slice(
        source_root=ROOT, manifest_path=MANIFEST_ROOT / f"{role}.json", output_root=output_root
    )
    code = """
import importlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path = [str(root), *[value for value in sys.path if value and not pathlib.Path(value).resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve())]]
for name in json.loads(sys.argv[3]):
    loaded = importlib.import_module(name)
    assert pathlib.Path(loaded.__file__).resolve().is_relative_to(root), loaded.__file__
assert not any(name.startswith('core.application.workflows.ai_builder') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(output_root), str(ROOT), json.dumps(modules)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
