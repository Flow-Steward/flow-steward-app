from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.application.tooling.quality.backend_partition import (
    _Analyzer,
    _startup_tree,
    partition_backend_tests,
)


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_partition_is_deterministic_disjoint_and_complete(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_pure.py", "def test_value(): assert 1\n")
    _write(tmp_path, "tests/unit/nested/test_ui.py", "SOURCE = 'assets/frontend/src/main.tsx'\n")
    _write(tmp_path, "tests/unit/stress/test_load.py", "assert False\n")
    _write(tmp_path, "tests/unit/helper.py", "assert False\n")
    _write(tmp_path, "tests/unit/value_test.py", "def test_value(): assert 2\n")

    result = partition_backend_tests(tmp_path)

    assert result == partition_backend_tests(tmp_path)
    assert result.independent == ("tests/unit/test_pure.py", "tests/unit/value_test.py")
    assert result.frontend_contract == ("tests/unit/nested/test_ui.py",)
    assert set(result.independent).isdisjoint(result.frontend_contract)
    assert set(result.independent + result.frontend_contract) == {
        "tests/unit/test_pure.py",
        "tests/unit/nested/test_ui.py",
        "tests/unit/value_test.py",
    }


@pytest.mark.parametrize(
    "source",
    [
        "Path('assets') / 'frontend' / 'src'",
        "Path('assets/frontend/src/main.tsx').read_text()",
        "Path('assets/frontend/dist/index.html').read_text()",
        "Path('assets/frontend/package-lock.json').read_text()",
        "Path('assets/frontend/public/favicon.svg').read_bytes()",
        "Path('assets/platform-kit/components/platform-markdown.js').read_text()",
        "ROOT = Path(__file__).parents[2]; paths = ROOT.rglob('*')",
        "root = os.environ.get('PYTEST_SOURCE_REPO_ROOT')",
        "subprocess.run(['bash', 'scripts/check.sh'])",
        "importlib.import_module(module_name)",
        "Path(path_from_configuration).read_text()",
    ],
)
def test_frontend_source_shell_and_unbounded_inputs_are_contracts(
    tmp_path: Path, source: str
) -> None:
    _write(tmp_path, "tests/unit/test_contract.py", source + "\n")

    result = partition_backend_tests(tmp_path)

    assert result.frontend_contract == ("tests/unit/test_contract.py",)


def test_import_closure_catches_relative_imports_and_package_initializers(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_contract.py", "from core.service import execute\n")
    _write(
        tmp_path, "core/service.py", "from . import helper\ndef execute(): return helper.VALUE\n"
    )
    _write(tmp_path, "core/helper.py", "VALUE = 'assets/frontend/src/main.tsx'\n")
    _write(tmp_path, "tests/unit/test_package.py", "import core.other\n")
    _write(tmp_path, "core/__init__.py", "from . import helper\n")
    _write(tmp_path, "core/other.py", "VALUE = 1\n")

    result = partition_backend_tests(tmp_path)

    assert result.independent == ()
    assert result.frontend_contract == (
        "tests/unit/test_contract.py",
        "tests/unit/test_package.py",
    )


def test_constant_dynamic_imports_follow_local_module_closure(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_contract.py", "importlib.import_module('core.service')\n")
    _write(tmp_path, "core/service.py", "SOURCE = 'assets/frontend/src/main.tsx'\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_contract.py",)


def test_temporary_files_and_explicit_backend_artifacts_are_independent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/unit/test_backend.py",
        "from pathlib import Path\n"
        "ROOT = Path(__file__).parents[2]\n"
        "def test_value(tmp_path):\n"
        "    fixture = tmp_path / 'fixture.json'\n"
        "    fixture.write_text('{}')\n"
        "    assert fixture.read_text() == '{}'\n"
        "    assert (ROOT / 'db/schema/schema.sql').read_text()\n",
    )

    assert partition_backend_tests(tmp_path).independent == ("tests/unit/test_backend.py",)


def test_sibling_backend_data_file_does_not_taint_configuration_imports(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_backend.py", "from core.config import DATA\n")
    _write(
        tmp_path,
        "core/config.py",
        "from pathlib import Path\nDATA = Path(__file__).with_name('manifest.json').read_text()\n",
    )

    assert partition_backend_tests(tmp_path).independent == ("tests/unit/test_backend.py",)


def test_unresolved_local_import_is_not_assumed_independent(tmp_path: Path) -> None:
    _write(tmp_path, "core/__init__.py", "")
    _write(tmp_path, "tests/unit/test_missing.py", "import core.missing\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_missing.py",)


def test_optional_shared_fixture_only_taints_modules_that_request_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        "import pytest\n"
        "@pytest.fixture\n"
        "def shell():\n"
        "    return Path('assets/frontend/dist/index.html').read_text()\n"
        "@pytest.fixture\n"
        "def page(shell): return shell\n",
    )
    _write(tmp_path, "tests/unit/test_pure.py", "def test_value(): assert 1\n")
    _write(tmp_path, "tests/unit/test_page.py", "def test_value(page): assert page\n")

    result = partition_backend_tests(tmp_path)

    assert result.independent == ("tests/unit/test_pure.py",)
    assert result.frontend_contract == ("tests/unit/test_page.py",)


@pytest.mark.parametrize(
    "prefix",
    [
        "import pytest\n@pytest.fixture(autouse=True)\ndef startup():\n    ",
        "def pytest_configure(config):\n    ",
        "",
    ],
)
def test_autouse_hooks_and_module_initialization_apply_to_every_descendant(
    tmp_path: Path, prefix: str
) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        prefix + "value = Path('assets/frontend/dist/index.html').read_text()\n",
    )
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): assert 1\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_unknown_fixture_and_dynamic_fixture_lookup_fail_closed(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_unknown.py", "def test_value(unregistered_fixture): pass\n")
    _write(
        tmp_path,
        "tests/unit/test_dynamic.py",
        "def test_value(request): return request.getfixturevalue(fixture_name)\n",
    )

    result = partition_backend_tests(tmp_path)

    assert result.independent == ()
    assert len(result.frontend_contract) == 2


def test_bad_source_is_retained_in_contract_partition(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_broken.py", "def invalid(\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_broken.py",)


def test_shared_imports_inspect_startup_without_running_dormant_runtime_apis(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "tests/conftest.py", "from core.service import Value\n")
    _write(
        tmp_path,
        "core/service.py",
        "class Value:\n    def page(self): return Path('assets/frontend').read_text()\n",
    )
    _write(tmp_path, "tests/unit/test_pure.py", "def test_value(): assert 1\n")

    assert partition_backend_tests(tmp_path).independent == ("tests/unit/test_pure.py",)


def test_shared_imports_follow_functions_executed_by_module_initialization(tmp_path: Path) -> None:
    _write(tmp_path, "tests/conftest.py", "from core.helper import DATA\n")
    _write(
        tmp_path,
        "core/helper.py",
        "def initialize(): return Path('assets/frontend').read_text()\nDATA = initialize()\n",
    )
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_shared_imports_follow_imported_functions_executed_by_module_initialization(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "tests/conftest.py", "import core.helper\n")
    _write(tmp_path, "core/helper.py", "from core.loader import load\nDATA = load()\n")
    _write(tmp_path, "core/loader.py", "def load(): return Path('assets/frontend').read_text()\n")
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_requested_fixture_follows_called_runtime_api(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        "import pytest\n@pytest.fixture\ndef value():\n"
        "    from core.service import get_value\n    return get_value()\n",
    )
    _write(
        tmp_path, "core/service.py", "def get_value(): return Path('assets/frontend').read_text()\n"
    )
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(value): assert value\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


@pytest.mark.parametrize(
    "caller",
    [
        "import pytest\n@pytest.fixture(autouse=True)\ndef startup(): load()\n",
        "def pytest_configure(config): load()\n",
    ],
)
def test_shared_hooks_and_autouse_fixtures_follow_globally_imported_helpers(
    tmp_path: Path, caller: str
) -> None:
    _write(tmp_path, "tests/conftest.py", "from core.helper import load\n" + caller)
    _write(tmp_path, "core/helper.py", "def load(): return Path('assets/frontend').read_text()\n")
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_test_package_initializers_are_dependencies(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/__init__.py", "VALUE = Path('assets/frontend').read_text()\n")
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_aliased_subprocess_calls_are_contracts(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/unit/test_value.py",
        "from subprocess import run as execute\nexecute(['node', 'script.js'])\n",
    )

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_shared_fixture_method_names_do_not_assume_builtin_container_behavior(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        "import pytest\nfrom core.helper import cache\n"
        "@pytest.fixture(autouse=True)\ndef startup(): cache.clear()\n",
    )
    _write(
        tmp_path,
        "core/helper.py",
        "class Cache:\n    def clear(self): return Path('assets/frontend').read_text()\ncache = Cache()\n",
    )
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_audited_frontend_dependency_families_remain_contracts() -> None:
    root = Path(__file__).resolve().parents[4]
    analyzer = _Analyzer(root)
    audited = {
        "tests/unit/test_registry_admin_api.py",
        "tests/unit/test_pre_010_cleanup_guardrails.py",
        "tests/unit/test_retired_executor_absence.py",
        "tests/unit/test_postgres_only_release_boundary.py",
        "tests/unit/test_frontend_shell_runtime.py",
        "tests/unit/test_extension_browser_authority_approval.py",
        "tests/unit/test_extension_runtime.py",
        "tests/unit/test_api_reactions.py",
        "tests/unit/test_platform_version_consistency.py",
        "tests/unit/test_public_workflow_module_boundaries.py",
        "tests/unit/test_shared_confirmation_dialog.py",
        "tests/unit/test_sonar_hotspot_regressions.py",
        "tests/unit/tooling/test_agent_skill_packages.py",
        "tests/unit/tooling/test_agent_skill_release.py",
    }

    # Exhaustiveness/disjointness are covered by the focused synthetic tree.
    # Audit these real boundaries directly instead of rebuilding the complete
    # graph a second time on every acceptance run.
    for path in sorted(audited):
        assert analyzer.closure_reasons({root / path}), path


def test_independent_runtime_guard_excludes_only_shared_workflow_bootstrap(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        "import os\nimport pytest\n"
        "def _initialize_workflow_test_patch_surface():\n"
        "    import core.workflows\n"
        "if os.environ.get('FS_QUALITY_BACKEND_PARTITION') != 'independent':\n"
        "    _initialize_workflow_test_patch_surface()\n"
        "@pytest.fixture\ndef workflow():\n"
        "    from core.workflows import load\n    return load()\n",
    )
    _write(
        tmp_path, "core/workflows.py", "def load(): return Path('assets/frontend').read_text()\n"
    )
    _write(tmp_path, "tests/unit/test_pure.py", "def test_value(): assert 1\n")
    _write(tmp_path, "tests/unit/test_import.py", "import core.workflows\n")
    _write(tmp_path, "tests/unit/test_fixture.py", "def test_value(workflow): assert workflow\n")

    result = partition_backend_tests(tmp_path)

    assert result.independent == ("tests/unit/test_pure.py",)
    assert result.frontend_contract == (
        "tests/unit/test_fixture.py",
        "tests/unit/test_import.py",
    )


def test_unrecognized_shared_environment_guard_is_not_evaluated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/conftest.py",
        "import os\nif os.environ.get('OTHER_CONTEXT') != 'independent':\n"
        "    value = Path('assets/frontend').read_text()\n",
    )
    _write(tmp_path, "tests/unit/test_value.py", "def test_value(): pass\n")

    assert partition_backend_tests(tmp_path).frontend_contract == ("tests/unit/test_value.py",)


def test_startup_pruning_preserves_original_tree_and_called_local_functions() -> None:
    tree = ast.parse(
        "def dormant(): return Path('assets/frontend').read_text()\n"
        "def initialize(): return 7\nDATA = initialize()\n"
    )
    original = ast.dump(tree)

    startup = _startup_tree(tree)

    assert ast.dump(tree) == original
    assert isinstance(startup.body[0], ast.FunctionDef)
    assert isinstance(startup.body[1], ast.FunctionDef)
    assert isinstance(startup.body[0].body[0], ast.Pass)
    assert isinstance(startup.body[1].body[0], ast.Return)
