from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = ROOT / "core/entrypoints/mcp"
MCP_DOCKERFILE = ROOT / "infra/mcp/Dockerfile"
MCP_REQUIREMENTS = ROOT / "requirements-mcp.txt"
MCP_LOCK = ROOT / "requirements-mcp.lock"
BASE_COMPOSE = ROOT / "infra/docker/docker-compose.yaml"
PROD_COMPOSE = ROOT / "infra/docker/docker-compose.prod.yaml"
DEV_COMPOSE = ROOT / "infra/docker/docker-compose.dev.yaml"

FORBIDDEN_IMPORT_PREFIXES = (
    "core.infrastructure.db",
    "core.infrastructure.runtime",
    "core.application.orchestration",
    "core.application.indexing",
    "core.application.llm",
    "core.application.providers",
    "boto3",
    "botocore",
    "langchain",
    "langgraph",
    "numpy",
    "openai",
    "pika",
    "polars",
    "psycopg",
    "scipy",
    "sknetwork",
)

FORBIDDEN_SOURCE_TOKENS = (
    "FS_DB_DSN",
    "LANGGRAPH_POSTGRES_URI",
    "create_project_store",
    "create_job_store",
    "create_event_store",
    "create_observability_store",
    "get_chat_model",
    "task_prep_bridge",
)


def _mcp_sources() -> list[Path]:
    return sorted(MCP_ROOT.glob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _matches_forbidden_import(imported: str) -> bool:
    return any(
        imported == prefix or imported.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def test_mcp_entrypoint_has_no_direct_product_runtime_imports() -> None:
    violations: list[str] = []
    for path in _mcp_sources():
        for imported in sorted(_imports(path)):
            if _matches_forbidden_import(imported):
                violations.append(f"{path.relative_to(ROOT)}: {imported}")

    assert not violations, "MCP must delegate product work to fs-app:\n" + "\n".join(violations)


def test_mcp_entrypoint_has_no_direct_product_connection_or_composition_tokens() -> None:
    violations: list[str] = []
    for path in _mcp_sources():
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_SOURCE_TOKENS:
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")

    assert not violations, "MCP still owns product runtime paths:\n" + "\n".join(violations)


def test_mcp_has_a_dedicated_minimal_hashed_dependency_lock() -> None:
    assert MCP_REQUIREMENTS.is_file()
    assert MCP_LOCK.is_file()
    requirements = MCP_REQUIREMENTS.read_text(encoding="utf-8").lower()
    lock = MCP_LOCK.read_text(encoding="utf-8").lower()

    assert "requirements-runtime" not in requirements + lock
    assert "mcp==2.2.0" in requirements
    assert "httpx" in requirements
    for package in (
        "boto3",
        "botocore",
        "fastapi",
        "langchain",
        "langgraph",
        "numpy",
        "openai",
        "pika",
        "polars",
        "psycopg",
        "scipy",
        "sknetwork",
        "tree-sitter",
    ):
        assert package not in lock, f"forbidden MCP package retained: {package}"


def test_mcp_image_uses_only_the_minimal_lock_and_bounded_copy_layers() -> None:
    flagged_source = MCP_DOCKERFILE.read_text(encoding="utf-8")
    # AC17-R144: every COPY normalises file modes (test_image_file_modes.py). The checks
    # here are about what is copied, so they read the file with that flag removed --
    # otherwise the negative ones would pass by never matching anything.
    assert "COPY --chmod=u=rwX,go=rX --chown=fs:fs" in flagged_source
    source = flagged_source.replace("COPY --chmod=u=rwX,go=rX ", "COPY ")
    assert "--chmod" not in source

    assert "requirements-mcp.lock" in source
    assert "requirements-runtime.lock" not in source
    assert "COPY . ." not in source
    assert "chown -R" not in source
    assert "COPY --chown=fs:fs" in source
    assert (
        "COPY --chown=fs:fs core/infrastructure/mcp_dev_proxy.py "
        "core/infrastructure/mcp_dev_proxy.py"
    ) in source
    for excluded_tree in ("extensions", "tests", "docs", "assets"):
        assert f"COPY {excluded_tree}" not in source


def test_local_dev_override_enables_the_same_explicit_proxy_contract_on_both_sides() -> None:
    services = yaml.safe_load(DEV_COMPOSE.read_text(encoding="utf-8"))["services"]

    expected = "${FS_MCP_AUTH_DISABLED:-1}"
    assert services["fs-mcp"]["environment"]["FS_MCP_AUTH_DISABLED"] == expected
    assert services["fs-app"]["environment"]["FS_MCP_AUTH_DISABLED"] == expected


def test_mcp_compose_environment_is_a_closed_transport_allowlist() -> None:
    base = yaml.safe_load(BASE_COMPOSE.read_text(encoding="utf-8"))["services"]["fs-mcp"]
    production = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))["services"]["fs-mcp"]

    assert "env_file" not in base
    assert "env_file" not in production
    assert set(base.get("environment") or {}) == {
        "ENVIRONMENT",
        "FS_APP_INTERNAL_URL",
        "FS_DEPLOYMENT_REVISION",
        "FS_MCP_ISSUER_URL",
        "FS_MCP_RESOURCE_URL",
        "FS_STACK_ENVIRONMENT",
        "PYTHONPYCACHEPREFIX",
    }
    assert set(production.get("environment") or {}) == {
        "ENVIRONMENT",
        "FS_APP_INTERNAL_URL",
        "FS_DEPLOYMENT_REVISION",
        "FS_IMAGE_DIGEST",
        "FS_MCP_AUTH_DISABLED",
        "FS_MCP_ISSUER_URL",
        "FS_MCP_RESOURCE_URL",
        "FS_STACK_ENVIRONMENT",
        # P1-5: the server derives its public issuer and resource from this origin.
        "FS_PUBLIC_BASE_URL",
        "PYTHONPYCACHEPREFIX",
    }
    for service in (base, production):
        assert (
            service["environment"]["ENVIRONMENT"]
            == "${ENVIRONMENT-${FS_STACK_ENVIRONMENT-production}}"
        )
        assert (
            service["environment"]["FS_STACK_ENVIRONMENT"]
            == "${FS_STACK_ENVIRONMENT-${ENVIRONMENT-production}}"
        )
        assert not service.get("volumes")
        assert not service.get("depends_on")
