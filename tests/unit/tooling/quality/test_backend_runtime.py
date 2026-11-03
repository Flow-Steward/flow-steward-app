from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.application.tooling.quality import backend_runtime


@pytest.fixture
def canonical_environment(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(backend_runtime, "_runtime_receipt_identity", lambda: "c" * 64)
    return {
        "FS_TEST_AUDIT_IMAGE_ID": "sha256:" + "a" * 64,
        "FS_TEST_BACKEND_SERVICES_ID": "sha256:" + "b" * 64,
        "FS_AUDIT_ISOLATED": "1",
        "FS_STACK_ENVIRONMENT": "development",
        "PYTHONPATH": "/app",
    }


def test_same_runtime_can_match_across_candidate_shas(canonical_environment) -> None:
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first == second


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FS_TEST_AUDIT_IMAGE_ID", ""),
        ("FS_TEST_AUDIT_IMAGE_ID", "mutable:latest"),
        ("FS_TEST_BACKEND_SERVICES_ID", ""),
        ("FS_TEST_BACKEND_SERVICES_ID", "unavailable"),
        ("FS_AUDIT_ISOLATED", "0"),
        ("PYTEST_ADDOPTS", "-k selected"),
        ("PYTEST_PLUGINS", "custom_external_plugin"),
        ("PYTHONPATH", "/external/source"),
        ("PYTHONHOME", "/external/python"),
        ("COVERAGE_RCFILE", "/external/coverage.ini"),
        ("NODE_OPTIONS", "--require=/external/preload.js"),
        ("npm_config_userconfig", "/external/npmrc"),
        ("FS_PROJECT_CONFIG", "/external/project.yaml"),
        ("FS_STACK_ENV_FILE", "/external/stack.env"),
        ("UNREGISTERED_CONFIG_FILE", "/external/custom.yaml"),
        ("UNREGISTERED_CONFIG_PATH", "custom/config.yaml"),
        ("TARGET_PROJECT_PATH", "/real/external/project"),
        ("LD_PRELOAD", "/external/libcustom.so"),
        ("PATH", "/external/bin:/usr/bin"),
    ],
)
def test_unknown_or_custom_runtime_does_not_match_across_shas(
    canonical_environment, name: str, value: str
) -> None:
    canonical_environment[name] = value

    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first != second


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FS_TEST_BACKEND_SERVICES_ID", "sha256:" + "d" * 64),
        ("FS_COV_FAIL_UNDER", "90"),
        ("FS_ALLOW_UNIT_TEST_SKIPS", "1"),
        ("FS_TEST_DISPOSABLE_DB", "0"),
        ("FS_MEMORY_VECTOR_BACKEND", "custom"),
        ("SECURITY_PII_SALT", "custom-salt"),
        ("PYTHONWARNINGS", "error"),
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
        ("COVERAGE_CORE", "sysmon"),
        ("NODE_ENV", "production"),
        ("TZ", "Pacific/Honolulu"),
        ("APP_ENV", "production"),
        ("SCHEDULED_JOB_SCHEDULER_ID", "custom-scheduler"),
        ("CONTROL_PLANE_INSTANCE_ID", "custom-control-plane"),
        ("PREVIOUSLY_UNKNOWN_VALUE", "custom-setting"),
        ("GITHUB_ACTIONS", "true"),
        ("GITHUB_REPOSITORY", "other/repository"),
    ],
)
def test_runtime_options_change_identity(canonical_environment, name: str, value: str) -> None:
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    canonical_environment[name] = value

    second = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)

    assert first != second


def test_source_rebuild_with_same_runtime_receipt_can_reuse(canonical_environment) -> None:
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    canonical_environment["FS_TEST_AUDIT_IMAGE_ID"] = "sha256:" + "d" * 64

    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first == second


def test_changed_runtime_receipt_invalidates_identity(canonical_environment, monkeypatch) -> None:
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    monkeypatch.setattr(backend_runtime, "_runtime_receipt_identity", lambda: "d" * 64)

    assert first != backend_runtime.backend_runtime_identity(
        "1" * 40, environ=canonical_environment
    )


def test_missing_runtime_receipt_binds_identity_to_candidate(
    canonical_environment, monkeypatch
) -> None:
    monkeypatch.setattr(backend_runtime, "_runtime_receipt_identity", lambda: "unavailable")

    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first != second


@pytest.mark.parametrize("receipt", ["", "mutable:latest", "sha256:" + "a" * 64, "a" * 63])
def test_invalid_baked_runtime_receipt_is_unavailable(tmp_path, monkeypatch, receipt: str) -> None:
    path = tmp_path / "backend-runtime.sha256"
    path.write_text(receipt)
    monkeypatch.setattr(backend_runtime, "_RUNTIME_RECEIPT_PATH", path)

    assert backend_runtime._runtime_receipt_identity() == "unavailable"


def test_valid_baked_runtime_receipt_is_read(tmp_path, monkeypatch) -> None:
    path = tmp_path / "backend-runtime.sha256"
    path.write_text("a" * 64 + "\n")
    monkeypatch.setattr(backend_runtime, "_RUNTIME_RECEIPT_PATH", path)

    assert backend_runtime._runtime_receipt_identity() == "a" * 64


def test_missing_baked_runtime_receipt_is_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backend_runtime, "_RUNTIME_RECEIPT_PATH", tmp_path / "missing")

    assert backend_runtime._runtime_receipt_identity() == "unavailable"


def test_operational_candidate_and_workspace_identifiers_do_not_prevent_reuse(
    canonical_environment,
) -> None:
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    canonical_environment.update(
        GITHUB_SHA="2" * 40,
        GITHUB_RUN_ID="12345",
        GITHUB_RUN_ATTEMPT="2",
        GITHUB_WORKSPACE="/runner/another-workspace",
        GITHUB_OUTPUT="/runner/temp/another-output",
        RUNNER_TEMP="/runner/another-temp",
        HOSTNAME="another-disposable-container",
        HOME="/root",
        PWD="/another/launch-directory",
        OLDPWD="/another/previous-directory",
        SHLVL="3",
        FS_BUILD_SHA="2" * 40,
        FS_HOST_ROOT_DIR="/external/worktree",
        FS_COMPOSE_PROJECT_NAME="disposable-quality-12345",
        FS_QUALITY_CACHE_ROOT="/external/runner-temp/cache",
        FS_QUALITY_EVIDENCE_MODE="reuse",
        FS_BOUNDARY_TRACKED_FILES_MANIFEST="/app/logs/quality/tracked-files.txt",
    )
    canonical_environment["_"] = "/usr/bin/another-launcher"

    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first == second


def test_known_isolated_resource_paths_do_not_force_candidate_scope(canonical_environment) -> None:
    canonical_environment.update(
        ARTIFACT_LOCAL_ROOT_DIR="/tmp/flow-steward-audit-artifacts",
        TARGET_PROJECT_PATH="/absolute/path/to/your/project",
        FS_ARTIFACT_ROOT="/tmp/flow-steward-audit-ops",
        FS_STACK_ENV_FILE="/dev/null",
    )

    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first == second


def test_environment_identity_does_not_return_secrets(canonical_environment) -> None:
    canonical_environment["FS_SECRET_KEY"] = "private-sentinel-secret"

    result = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)

    assert "private-sentinel-secret" not in json.dumps(result)
    changed_environment = {**canonical_environment, "FS_SECRET_KEY": "changed-secret"}
    assert result != backend_runtime.backend_runtime_identity("1" * 40, environ=changed_environment)


def test_quickstart_marker_is_external_if_it_actually_exists(
    canonical_environment, monkeypatch
) -> None:
    canonical_environment["TARGET_PROJECT_PATH"] = "/absolute/path/to/your/project"
    monkeypatch.setattr(backend_runtime.Path, "exists", lambda _path: True)
    assert backend_runtime._has_external_inputs(canonical_environment)


def test_changed_installed_distributions_invalidate_identity(
    canonical_environment, monkeypatch
) -> None:
    monkeypatch.setattr(
        backend_runtime.metadata,
        "distributions",
        lambda: [SimpleNamespace(metadata={"Name": "pytest"}, version="8.0.0")],
    )
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    monkeypatch.setattr(
        backend_runtime.metadata,
        "distributions",
        lambda: [SimpleNamespace(metadata={"Name": "pytest"}, version="8.0.1")],
    )

    assert first != backend_runtime.backend_runtime_identity(
        "1" * 40, environ=canonical_environment
    )


def test_distribution_listing_order_does_not_prevent_reuse(
    canonical_environment, monkeypatch
) -> None:
    packages = [
        SimpleNamespace(metadata={"Name": "pytest"}, version="8.0.0"),
        SimpleNamespace(metadata={"Name": "coverage"}, version="7.0.0"),
    ]
    monkeypatch.setattr(backend_runtime.metadata, "distributions", lambda: packages)
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    packages.reverse()

    assert first == backend_runtime.backend_runtime_identity(
        "2" * 40, environ=canonical_environment
    )


def test_unreadable_distribution_identity_falls_back_to_candidate(
    canonical_environment, monkeypatch
) -> None:
    def unavailable():
        raise OSError("metadata unavailable")

    monkeypatch.setattr(backend_runtime.metadata, "distributions", unavailable)

    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    second = backend_runtime.backend_runtime_identity("2" * 40, environ=canonical_environment)

    assert first != second


def test_changed_platform_invalidates_identity(canonical_environment, monkeypatch) -> None:
    monkeypatch.setattr(backend_runtime.platform, "machine", lambda: "x86_64")
    first = backend_runtime.backend_runtime_identity("1" * 40, environ=canonical_environment)
    monkeypatch.setattr(backend_runtime.platform, "machine", lambda: "aarch64")

    assert first != backend_runtime.backend_runtime_identity(
        "1" * 40, environ=canonical_environment
    )
