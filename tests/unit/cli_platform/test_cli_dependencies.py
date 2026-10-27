"""Dependency and image contracts for the standalone CLI runtime."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).parents[3]
REQUIREMENT_LINE = re.compile(r"^[a-z0-9][a-z0-9_.-]*==", re.MULTILINE)


# A hang guard, not a speed budget: the parallel backend suite (ADR 0037) runs
# these interpreter boots next to three other busy workers, where a cold CLI
# start has taken longer than 15 s without anything being wrong.
_CLI_BOOT_TIMEOUT_SECONDS = 60


def _stage_body(dockerfile: str, stage: str, next_stage: str) -> str:
    return dockerfile.split(stage, 1)[1].split(next_stage, 1)[0]


def _requirement_stanzas(lock: str) -> list[tuple[str, str]]:
    headers = list(REQUIREMENT_LINE.finditer(lock))
    return [
        (
            header.group().removesuffix("=="),
            lock[
                header.end() : headers[index + 1].start() if index + 1 < len(headers) else len(lock)
            ],
        )
        for index, header in enumerate(headers)
    ]


def _materialize_cli_copy_inputs(destination: Path) -> None:
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    cli_body = _stage_body(dockerfile, "FROM base AS cli", "FROM base AS runtime-dependencies")
    logical_lines = cli_body.replace("\\\n", " ").splitlines()
    for line in logical_lines:
        if not line.startswith("COPY "):
            continue
        copy_tokens = shlex.split(line)[1:]
        if any(token.startswith("--from=") for token in copy_tokens):
            continue
        tokens = [token for token in copy_tokens if not token.startswith("--")]
        sources, target = tokens[:-1], tokens[-1]
        target_path = destination / target.lstrip("./")
        for source in sources:
            source_paths = tuple(ROOT.glob(source)) if "*" in source else (ROOT / source,)
            for source_path in source_paths:
                if source_path.is_dir():
                    shutil.copytree(source_path, target_path, dirs_exist_ok=True)
                    continue
                resolved_target = (
                    target_path / source_path.name if len(sources) > 1 else target_path
                )
                if target.endswith("/") or target in {".", "./"}:
                    resolved_target = target_path / source_path.name
                resolved_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, resolved_target)


def test_cli_copy_materializer_ignores_multistage_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    (source_root / "core").mkdir(parents=True)
    (source_root / "core/__init__.py").write_text("", encoding="utf-8")
    (source_root / "infra/docker").mkdir(parents=True)
    (source_root / "infra/docker/Dockerfile").write_text(
        "FROM scratch AS source-slices\n"
        "FROM base AS cli\n"
        "COPY core/__init__.py core/\n"
        "FROM base AS runtime-dependencies\n"
        "COPY --from=source-slices /out/app/ /app/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", source_root)

    _materialize_cli_copy_inputs(output_root)

    assert (output_root / "core/__init__.py").is_file()
    assert not (output_root / "app").exists()


def test_cli_requirements_are_independent_from_the_core_runtime_graph() -> None:
    requirements = (ROOT / "requirements-cli.txt").read_text(encoding="utf-8")

    assert "-r requirements-runtime.txt" not in requirements
    for required in ("click==8.4.2", "textual==8.2.8", "rich==15.0.0", "pydantic==2.12.5"):
        assert required in requirements


def test_cli_direct_roots_are_reachable_from_retained_lifecycle_code() -> None:
    dependency_text = "\n".join(
        (ROOT / name).read_text(encoding="utf-8").lower()
        for name in ("requirements-cli.txt", "requirements-cli.lock")
    )

    for stale in ("jsonschema", "packaging", "python-dotenv", "pyyaml", "requests"):
        assert f"{stale}==" not in dependency_text


def test_localization_babel_is_owned_by_the_product_runtime() -> None:
    requirements = (ROOT / "requirements-cli.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-cli.lock").read_text(encoding="utf-8")
    runtime_requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    runtime_lock = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")

    assert "babel==" not in requirements.lower()
    assert "babel==" not in lock.lower()
    assert "babel==2.17.0" in runtime_requirements.lower()
    assert "babel==2.17.0" in runtime_lock.lower()


def test_product_runtime_includes_presentation_dependencies_for_delegated_tui() -> None:
    """Removing Textual from fs-app must break interactive product-command routing."""
    requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")

    assert "textual==8.2.8" in requirements
    assert "rich==15.0.0" in requirements


def test_cli_lock_excludes_product_runtime_packages() -> None:
    lock = (ROOT / "requirements-cli.lock").read_text(encoding="utf-8").lower()

    forbidden = (
        "anthropic",
        "fastapi",
        "langchain",
        "langgraph",
        "mcp",
        "numpy",
        "openai",
        "openpyxl",
        "pika",
        "polars",
        "scikit-network",
        "scipy",
        "tree-sitter",
        "uvicorn",
    )
    locked_names = {name for name, _stanza in _requirement_stanzas(lock)}

    assert locked_names.isdisjoint(forbidden)


def test_cli_lock_preserves_runtime_pydantic_pin_and_hashes_every_requirement() -> None:
    runtime = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-cli.lock").read_text(encoding="utf-8")
    pydantic_version = re.search(r"^pydantic==(\S+)$", runtime, re.MULTILINE)

    assert pydantic_version is not None
    assert f"pydantic=={pydantic_version.group(1)}" in lock
    assert "# WARNING: The following packages were not pinned" not in lock
    requirement_stanzas = _requirement_stanzas(lock)
    assert requirement_stanzas
    for requirement, stanza in requirement_stanzas:
        assert "--hash=sha256:" in stanza, f"{requirement} requirement stanza has no SHA-256 hash"


def test_cli_lock_rejects_requirement_stanza_without_sha256_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements-runtime.txt").write_text("pydantic==2.12.5\n", encoding="utf-8")
    (tmp_path / "requirements-cli.lock").write_text(
        "pydantic==2.12.5 \\\n"
        "    --hash=sha256:abc123\n"
        "setuptools==83.0.0 \\\n"
        "    --hash=sha256:def456\n"
        "missing-hashes==1.0 \\\n"
        "    # via fixture\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)

    with pytest.raises(AssertionError, match="missing-hashes"):
        test_cli_lock_preserves_runtime_pydantic_pin_and_hashes_every_requirement()


def test_cli_dependency_files_exclude_alternate_prompt_frameworks() -> None:
    dependency_text = "\n".join(
        (ROOT / filename).read_text(encoding="utf-8")
        for filename in ("requirements-cli.txt", "requirements-cli.lock")
    ).lower()

    for forbidden in ("questionary", "prompt-toolkit", "typer", "inquirer", "asciimatics"):
        assert forbidden not in dependency_text


def test_cli_lock_preserves_runtime_cryptography_security_pin() -> None:
    """The shipped CLI must not lag the audited runtime dependency floor."""
    runtime = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-cli.lock").read_text(encoding="utf-8")
    runtime_match = re.search(r"^cryptography==([^\s]+)$", runtime, re.MULTILINE)
    lock_match = re.search(r"^cryptography==([^\s\\]+)", lock, re.MULTILINE)

    assert runtime_match is not None
    assert lock_match is not None
    assert Version(lock_match.group(1)) >= Version(runtime_match.group(1))


def test_every_cli_direct_root_has_a_reachable_retained_consumer(tmp_path: Path) -> None:
    requirements = (ROOT / "requirements-cli.txt").read_text(encoding="utf-8")
    _materialize_cli_copy_inputs(tmp_path)
    expected_consumers = {
        "click": ROOT / "core/entrypoints/cli/platform/click_adapter.py",
        "pydantic": ROOT / "core/entrypoints/cli/platform/service_bindings/common.py",
        "rich": ROOT / "core/entrypoints/cli/platform/renderers.py",
        "textual": ROOT / "core/entrypoints/cli/tui/form_app.py",
        "psycopg": ROOT / "core/infrastructure/lifecycle/preservation_repository.py",
        "boto3": ROOT / "core/infrastructure/lifecycle/storage_gateway.py",
        "botocore": ROOT / "core/infrastructure/lifecycle/configuration_gateway.py",
        "cryptography": ROOT / "core/application/lifecycle/configuration_validation.py",
    }
    direct_roots = set(re.findall(r"^([a-z0-9-]+)(?:\[[^\]]+\])?==", requirements, re.MULTILINE))

    assert direct_roots == set(expected_consumers)
    for requirement, consumer in expected_consumers.items():
        relative_consumer = consumer.relative_to(ROOT).as_posix()

        assert (tmp_path / relative_consumer).is_file()
        assert requirement in consumer.read_text(encoding="utf-8")


def test_cli_lifecycle_dependencies_do_not_justify_product_environment_export() -> None:
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        compose = (ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8")
        match = re.search(
            r"\n  fs-cli:\n(?P<body>.*?)(?=\n  [A-Za-z0-9_-]+:\n)", compose, re.DOTALL
        )

        assert match is not None
        service = match.group("body")
        assert "env_file:" not in service
        for forbidden in (
            "FS_DB_DSN:",
            "LANGGRAPH_POSTGRES_URI:",
            "ARTIFACT_S3_ACCESS_KEY:",
            "ARTIFACT_S3_SECRET_KEY:",
            "RABBITMQ_URL:",
            "FS_SECRET_KEY:",
            "FS_WORKER_MCP_BEARER_TOKEN:",
        ):
            assert forbidden not in service


def test_semgrep_needs_no_pyjwt_risk_acceptance() -> None:
    """Semgrep 1.179.0 permits the patched PyJWT; its tool exception is gone, not extended."""
    acceptance = (ROOT / "infra/tooling/quality/pip-audit-risk-acceptance.md").read_text(
        encoding="utf-8"
    )

    assert "### pyjwt /" not in acceptance
    assert "Semgrep 1.179.0" in acceptance


def test_cli_docker_target_is_isolated_hashed_runtime_with_docker_tools() -> None:
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    flagged_cli_body = _stage_body(
        dockerfile,
        "FROM base AS cli",
        "FROM base AS runtime-dependencies",
    )
    # AC17-R144: every COPY in this stage normalises file modes, which
    # test_image_file_modes.py enforces. The literal checks below are about what is
    # copied and who owns it, so they read the stage with that one flag removed --
    # otherwise the negative ones would pass by never matching anything.
    assert "COPY --chmod=u=rwX,go=rX requirements-cli.lock ." in flagged_cli_body
    cli_body = flagged_cli_body.replace("COPY --chmod=u=rwX,go=rX ", "COPY ")
    assert "--chmod" not in cli_body

    assert "git config --system --add safe.directory /app" in dockerfile
    assert "git config --global --add safe.directory '*'" not in dockerfile
    assert "COPY requirements-cli.lock ." in cli_body
    assert (
        "--mount=type=cache,id=flow-steward-pip-python312,"
        "target=/root/.cache/pip,sharing=locked" in cli_body
    )
    assert "pip install --require-hashes -r requirements-cli.lock" in cli_body
    assert "--no-cache-dir" not in cli_body
    for docker_package in (
        "docker-ce-cli",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ):
        assert docker_package in cli_body
    assert "USER root" in cli_body
    assert "USER fs" in cli_body
    assert "COPY . ." not in cli_body
    assert "chown -R fs:fs /app" not in cli_body
    assert "COPY --chown=fs:fs" in cli_body
    assert "ARG FS_BUILD_REVISION=unknown" in cli_body
    assert 'LABEL org.opencontainers.image.revision="${FS_BUILD_REVISION}"' in cli_body
    assert 'ENTRYPOINT ["python", "-m", "core.entrypoints.cli.lifecycle_click_app"]' in cli_body
    for broad_copy in (
        "COPY --chown=fs:fs core/ core/",
        "COPY --chown=fs:fs db/ db/",
        "COPY --chown=fs:fs infra/docker/ infra/docker/",
    ):
        assert broad_copy not in cli_body
    for required_copy in (
        "core/entrypoints/cli/lifecycle_click_app.py",
        "core/entrypoints/cli/lifecycle_runtime.py",
        "core/entrypoints/cli/compact_platform_runtime.py",
        "core/entrypoints/cli/platform_runtime.py",
        "core/entrypoints/cli/platform/service_bindings/database.py",
        "core/application/jobs/schema_metadata.py",
        "core/application/management/command_specs/",
        "core/application/lifecycle/",
        "core/infrastructure/lifecycle/",
        "core/infrastructure/release/",
        "db/schema/",
    ):
        assert required_copy in cli_body
    for forbidden_source in (
        "core/entrypoints/cli/runtime.py",
        "core/entrypoints/cli/worker_process.py",
        "core/entrypoints/cli/control_process.py",
        "core/entrypoints/cli/approval_command_runtime.py",
        "core/entrypoints/cli/platform/service_bindings/users.py",
        "core/entrypoints/cli/platform/service_bindings/projects.py",
        "core/entrypoints/cli/platform/service_bindings/workflows.py",
        "core/entrypoints/cli/platform/service_bindings/tasks.py",
        "core/entrypoints/cli/platform/service_bindings/jobs.py",
        "core/entrypoints/cli/platform/service_bindings/reports.py",
        "core/entrypoints/cli/platform/service_bindings/localization.py",
    ):
        assert forbidden_source not in cli_body
    first_retired_name = "co" + "dex"
    second_retired_name = "clau" + "de"
    for forbidden in (
        "nodejs",
        f"@anthropic-ai/{second_retired_name}-code",
        f"@openai/{first_retired_name}",
        "playwright",
        "requirements-audit.txt",
        "--from=asset-build",
        "COPY assets",
        "COPY docs",
        "COPY extensions",
        "COPY tests",
    ):
        assert forbidden not in cli_body


def test_retained_cli_container_does_not_overlay_the_image_with_full_repo_source() -> None:
    for compose_file in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        compose = (ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8")
        match = re.search(
            r"\n  fs-cli:\n(?P<body>.*?)(?=\n  [A-Za-z0-9_-]+:\n)", compose, re.DOTALL
        )

        assert match is not None
        assert "- ../../:/app" not in match.group("body")


def test_lifecycle_runtime_has_no_product_composition_surface() -> None:
    # `database_tooling_service` is deliberately absent from this list. The lifecycle CLI now
    # owns the `database` domain: it is in lifecycle_click_app's LIFECYCLE_COMMAND_DOMAINS, and
    # `service_bindings/database.py` was moved out of the forbidden set and into the required
    # copies of test_cli_docker_target_is_isolated_hashed_runtime_with_docker_tools above. This
    # barrier is about product composition leaking into the lifecycle runtime; the harder
    # invariant -- that the artifact stays bootable without fastapi, langchain or langgraph --
    # is held by test_cli_copy_allowlist_is_a_bootable_lifecycle_artifact, which imports it.
    runtime = (ROOT / "core/entrypoints/cli/lifecycle_runtime.py").read_text(encoding="utf-8")

    for forbidden in (
        "identity_store",
        "project_store",
        "job_store",
        "reporting_store",
        "workflow_operator_service",
        "extension_binding_service",
        "testing_tooling_service",
        "quality_tooling_service",
        "localization_tooling_service",
    ):
        assert forbidden not in runtime


def test_cli_copy_allowlist_is_a_bootable_lifecycle_artifact(tmp_path: Path) -> None:
    _materialize_cli_copy_inputs(tmp_path)
    (tmp_path / "sitecustomize.py").write_text(
        """
import importlib.abc
import sys

FORBIDDEN = {
    "fastapi", "langchain", "langgraph", "mcp", "numpy", "openai",
    "openpyxl", "pika", "polars", "scipy", "sknetwork", "tree_sitter", "uvicorn",
}

class BlockProductDependency(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in FORBIDDEN:
            raise ModuleNotFoundError(f"forbidden CLI dependency: {fullname}")
        return None

sys.meta_path.insert(0, BlockProductDependency())
""",
        encoding="utf-8",
    )

    for forbidden in (
        "core/entrypoints/cli/platform/service_bindings/users.py",
        "core/entrypoints/cli/runtime.py",
        "core/entrypoints/cli/worker_process.py",
        "core/infrastructure/runtime/entrypoint_db_adapters.py",
    ):
        assert not (tmp_path / forbidden).exists()

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli.lifecycle_click_app", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Flow Steward public management commands" in result.stdout

    interactive_import = subprocess.run(
        [
            sys.executable,
            "-c",
            ("from core.entrypoints.cli.tui.form_app import CommandFormApp; assert CommandFormApp"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )

    assert interactive_import.returncode == 0, interactive_import.stderr

    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            "--json",
            "--verbose",
            "status",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )

    assert "No module named" not in status.stderr
    assert status.returncode in {0, 2, 3, 4, 5, 6, 8}


@pytest.mark.parametrize("operation", ["release-info", "doctor"])
def test_cli_copy_allowlist_supports_compact_release_dispatch(
    tmp_path: Path, operation: str
) -> None:
    _materialize_cli_copy_inputs(tmp_path)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.entrypoints.cli.platform_runtime",
            operation,
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )

    assert "No module named" not in result.stderr
    assert result.returncode in {0, 2, 3, 4, 5, 6, 8}


def test_cli_copy_allowlist_preflight_does_not_import_product_database_composition(
    tmp_path: Path,
) -> None:
    """The management preflight must delegate to the installed Compact runtime."""
    _materialize_cli_copy_inputs(tmp_path)
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(tmp_path),
        FS_CONFIGURATION_STATE_ROOT=str(tmp_path / "missing-installation"),
        FS_DOCKER_HOST_PROJECT_ROOT=str(tmp_path / "installation"),
        FS_LOAD_REPO_DOTENV="0",
    )
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli.platform_runtime", "candidate-preflight"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "Installed configuration is unavailable" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_cli_image_can_answer_the_launchers_lifecycle_probe(tmp_path: Path) -> None:
    """AC17-R156: the launcher asks the running fs-cli whether an operation holds the
    lifecycle lock before replacing it; a probe missing from the image would read as
    "nothing running" and bring the killed apply back."""
    _materialize_cli_copy_inputs(tmp_path)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    environment["FS_DOCKER_HOST_PROJECT_ROOT"] = str(tmp_path / "installation")
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli.lifecycle_lock_probe"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 3, result.stderr  # answered: nothing holds the lock


def test_cli_image_can_open_an_interactive_form_in_both_themes(tmp_path: Path) -> None:
    """AC17-R154: importing the form proved nothing. Textual reads the stylesheet only
    when the app starts, so the image passed the import check above and every
    interactive command failed with "unable to read CSS file ... theme.tcss"."""
    pytest.importorskip("textual")
    _materialize_cli_copy_inputs(tmp_path)
    script = """
import asyncio
from core.application.management.command_specs.users import USERS_SET_PASSWORD_SPEC
from core.entrypoints.cli.tui.form_app import CommandFormApp

async def main():
    for color_enabled in (True, False):
        app = CommandFormApp(
            spec=USERS_SET_PASSWORD_SPEC,
            initial_values={},
            choice_loader=lambda _key: (),
            terminal_size=(100, 40),
            color_enabled=color_enabled,
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
        print("opened", color_enabled)

asyncio.run(main())
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["opened", "True", "opened", "False"]


def test_docker_build_context_excludes_local_flow_steward_state() -> None:
    """Operation logs and secret-input files must never enter Docker image layers."""
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".flow-steward/" in dockerignore


def test_materialized_cli_imports_exact_bootstrap_recovery_without_product_composition(tmp_path):
    _materialize_cli_copy_inputs(tmp_path)
    code = """
import sys
from core.entrypoints.cli.bootstrap_recovery import BootstrapRecoveryRegistry, BootstrapRecoveryRuntime
from core.application.auth.bootstrap_recovery_gateway import BootstrapRecoveryGateway
assert 'core.entrypoints.cli.runtime' not in sys.modules
assert 'core.entrypoints.cli.platform.service_bindings.users' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "FS_LOAD_REPO_DOTENV": "0"},
        text=True,
        capture_output=True,
        check=False,
        timeout=_CLI_BOOT_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
