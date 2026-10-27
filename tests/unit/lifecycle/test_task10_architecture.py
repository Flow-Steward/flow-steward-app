"""Architecture guardrails for the Phase 4 lifecycle cutover."""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import pytest

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from tests.support.source_index import parse_file

ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = ROOT / "core/application"
LIFECYCLE_ROOT = APPLICATION_ROOT / "lifecycle"
CLI_ROOT = ROOT / "core/entrypoints/cli"
CLI_BINDINGS_ROOT = CLI_ROOT / "platform/service_bindings"
CLI_HOST_LIFECYCLE_ADAPTER = CLI_ROOT / "lifecycle_runtime.py"
CLI_COMPOSITION_FILES = tuple(
    sorted(
        {
            CLI_ROOT / "click_app.py",
            *CLI_ROOT.rglob("*registry*.py"),
            *CLI_ROOT.rglob("*runtime*.py"),
        }
        - {CLI_HOST_LIFECYCLE_ADAPTER}
    )
)
RETIRED_INSTALLERS = (
    ROOT / "core/application/tooling/ops/stack_installer.py",
    ROOT / "core/entrypoints/cli/ops/stack_installer.py",
)
PUBLIC_MANAGEMENT_DOCS = (
    ROOT / "README.md",
    ROOT / "docs/GUIDE.md",
    ROOT / "docs/operator-postgresql-restore-runbook.md",
    ROOT / "docs/operations/scheduled-maintenance.md",
    ROOT / "docs/operations/installation-clone.md",
)
CANONICAL_CONFIG = (Path("core/application/lifecycle/contracts.py"), "DeploymentConfig")
ALLOWED_CONFIG_PROJECTIONS = {
    (Path("core/application/lifecycle/contracts.py"), "ConfigurationPatch"),
    (Path("core/application/lifecycle/environment_service.py"), "EnvironmentSwitchRequest"),
}
LIFECYCLE_CONFIG_FIELDS = frozenset(
    {
        "environment",
        "file_storage",
        "storage",
        "storage_backend",
        "development_tools",
        "tls",
        "tls_mode",
        "public_host",
        "public_port",
        "reverse_proxy_backend_host",
        "reverse_proxy_backend_port",
        "certificate_path",
        "private_key_path",
        "release",
        "release_version",
        "s3_endpoint",
        "s3_bucket",
        "s3_region",
        "s3_credentials_path",
        "allow_insecure_s3_http",
    }
)
_CONFIG_SIGNATURE_THRESHOLD = 4
_COMPOSE_COMMAND_VERBS = frozenset(
    {"build", "config", "down", "exec", "logs", "ps", "pull", "restart", "run", "stop", "up"}
)


def _imports(path: Path) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in parse_file(path).nodes_of(ast.Import, ast.ImportFrom):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.lineno, node.module or ""))
    return imports


def _python_sources(binding_root: Path, composition_files: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(sorted(binding_root.rglob("*.py"))) + tuple(composition_files)


def _docker_policy_offenders(
    binding_root: Path,
    composition_files: tuple[Path, ...],
) -> list[str]:
    offenders: list[str] = []
    binding_files = set(binding_root.rglob("*.py"))
    for path in _python_sources(binding_root, composition_files):
        parsed = parse_file(path)
        relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        for line, module in _imports(path):
            if module == "subprocess" or module.startswith("subprocess."):
                offenders.append(f"{relative}:{line}: subprocess import")
            if path in binding_files and module.startswith("core.infrastructure.lifecycle"):
                offenders.append(f"{relative}:{line}: infrastructure lifecycle import")
        for node in parsed.nodes_of(ast.Constant, ast.List, ast.Tuple):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.strip().lower()
                if (
                    normalized == "docker"
                    or normalized == "docker-compose"
                    or re.search(r"\bdocker\s+compose\b", normalized)
                ):
                    offenders.append(f"{relative}:{node.lineno}: Docker command token")
            if isinstance(node, (ast.List, ast.Tuple)):
                tokens = {
                    item.value.strip().lower()
                    for item in node.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                if "compose" in tokens and tokens & _COMPOSE_COMMAND_VERBS:
                    offenders.append(f"{relative}:{node.lineno}: Compose command array")
    return sorted(set(offenders))


def _assert_cli_docker_policy(
    binding_root: Path,
    composition_files: tuple[Path, ...],
) -> None:
    assert _docker_policy_offenders(binding_root, composition_files) == []


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _assignment_value(node: ast.Assign | ast.AnnAssign) -> ast.expr | None:
    return node.value


def _mapping_signature(node: ast.expr | None) -> frozenset[str]:
    if not isinstance(node, ast.Dict):
        return frozenset()
    return frozenset(
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and key.value in LIFECYCLE_CONFIG_FIELDS
    )


def _class_fields(node: ast.ClassDef) -> frozenset[str]:
    return frozenset(
        name
        for statement in node.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for name in _assigned_names(statement)
        if name in LIFECYCLE_CONFIG_FIELDS
    )


def _function_factory_signatures(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    descendants: tuple[ast.AST, ...],
) -> list[tuple[int, frozenset[str]]]:
    assigned_mappings: dict[str, frozenset[str]] = {}
    for statement in node.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            signature = _mapping_signature(_assignment_value(statement))
            for name in _assigned_names(statement):
                assigned_mappings[name] = signature
    signatures: list[tuple[int, frozenset[str]]] = []
    for descendant in descendants:
        if not isinstance(descendant, ast.Return):
            continue
        signature = _mapping_signature(descendant.value)
        if isinstance(descendant.value, ast.Name):
            signature = assigned_mappings.get(descendant.value.id, frozenset())
        if len(signature) >= _CONFIG_SIGNATURE_THRESHOLD:
            signatures.append((descendant.lineno, signature))
    return signatures


def _config_model_offenders(
    paths: tuple[Path, ...],
    *,
    source_root: Path = ROOT,
) -> list[str]:
    offenders: list[str] = []
    canonical_seen: list[tuple[Path, str]] = []
    for path in paths:
        parsed = parse_file(path)
        relative = path.relative_to(source_root) if path.is_relative_to(source_root) else path
        for node in parsed.nodes_of(
            ast.ClassDef, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef
        ):
            if isinstance(node, ast.ClassDef):
                identity = (relative, node.name)
                fields = _class_fields(node)
                if node.name == "DeploymentConfig":
                    canonical_seen.append(identity)
                if (
                    len(fields) >= _CONFIG_SIGNATURE_THRESHOLD
                    and identity != CANONICAL_CONFIG
                    and identity not in ALLOWED_CONFIG_PROJECTIONS
                ):
                    offenders.append(
                        f"{relative}:{node.lineno}: duplicate class {node.name} "
                        f"({', '.join(sorted(fields))})"
                    )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = _assignment_value(node)
                names = _assigned_names(node)
                if isinstance(value, (ast.Name, ast.Attribute)):
                    source_name = value.id if isinstance(value, ast.Name) else value.attr
                    for name in names:
                        if name.endswith("Config") and source_name.endswith("Config"):
                            offenders.append(
                                f"{relative}:{node.lineno}: duplicate config alias {name}"
                            )
                signature = _mapping_signature(value)
                if len(signature) >= _CONFIG_SIGNATURE_THRESHOLD:
                    for name in names:
                        offenders.append(
                            f"{relative}:{node.lineno}: duplicate config mapping {name}"
                        )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for line, signature in _function_factory_signatures(node, parsed.subtree(node)):
                    offenders.append(
                        f"{relative}:{line}: duplicate config factory {node.name} "
                        f"({', '.join(sorted(signature))})"
                    )
    if canonical_seen != [CANONICAL_CONFIG]:
        offenders.append(f"canonical DeploymentConfig definitions: {canonical_seen!r}")
    return sorted(set(offenders))


def _assert_single_config_source(
    paths: tuple[Path, ...],
    *,
    source_root: Path = ROOT,
) -> None:
    assert _config_model_offenders(paths, source_root=source_root) == []


def _documented_command_offenders(
    docs: tuple[Path, ...],
    public_paths: set[tuple[str, ...]],
    *,
    source_root: Path = ROOT,
) -> list[str]:
    first_tokens = {path[0] for path in public_paths}
    command_pattern = re.compile(r"\./flow-steward(?P<arguments>[^\n`]*)")
    offenders: list[str] = []
    for path in docs:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in command_pattern.finditer(line):
                try:
                    tokens = shlex.split(match.group("arguments").replace("\\", " "))
                except ValueError:
                    tokens = match.group("arguments").split()
                while tokens and tokens[0].startswith("-"):
                    tokens.pop(0)
                if not tokens or tokens[0] not in first_tokens:
                    if not tokens:
                        continue
                    relative = (
                        path.relative_to(source_root) if path.is_relative_to(source_root) else path
                    )
                    offenders.append(f"{relative}:{line_number}: {' '.join(tokens[:2])}")
                    continue
                if (len(tokens) == 1 or (len(tokens) > 1 and tokens[1] == "--help")) and any(
                    candidate[0] == tokens[0] for candidate in public_paths
                ):
                    continue
                if not any(
                    tuple(tokens[: len(candidate)]) == candidate for candidate in public_paths
                ):
                    relative = (
                        path.relative_to(source_root) if path.is_relative_to(source_root) else path
                    )
                    offenders.append(f"{relative}:{line_number}: {' '.join(tokens[:2])}")
    return offenders


def test_application_layer_does_not_import_entrypoints() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{line}: {module}"
        for path in sorted(APPLICATION_ROOT.rglob("*.py"))
        for line, module in _imports(path)
        if module == "core.entrypoints" or module.startswith("core.entrypoints.")
    ]

    assert offenders == []


def test_lifecycle_application_has_no_process_or_transport_dependencies() -> None:
    forbidden = {"click", "subprocess", "textual"}
    offenders = [
        f"{path.relative_to(ROOT)}:{line}: {module}"
        for path in sorted(LIFECYCLE_ROOT.rglob("*.py"))
        for line, module in _imports(path)
        if module.split(".", 1)[0] in forbidden
    ]

    assert offenders == []


def test_cli_transport_and_composition_do_not_make_docker_decisions() -> None:
    _assert_cli_docker_policy(CLI_BINDINGS_ROOT, CLI_COMPOSITION_FILES)


@pytest.mark.parametrize("location", ["binding", "runtime"])
def test_docker_policy_rejects_synthetic_future_cli_violations(
    tmp_path: Path,
    location: str,
) -> None:
    binding_root = tmp_path / "core/entrypoints/cli/platform/service_bindings"
    binding_root.mkdir(parents=True)
    composition_file = tmp_path / "core/entrypoints/cli/runtime.py"
    composition_file.parent.mkdir(parents=True, exist_ok=True)
    safe_source = "gateway = InfrastructureGateway()\n"
    violation = 'import subprocess\ncommand = ["docker", "compose", "ps"]\n'
    (binding_root / "future.py").write_text(
        violation if location == "binding" else safe_source,
        encoding="utf-8",
    )
    composition_file.write_text(
        violation if location == "runtime" else safe_source,
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match=r"Docker command token|subprocess import"):
        _assert_cli_docker_policy(binding_root, (composition_file,))


def test_lifecycle_configuration_has_one_source_of_truth() -> None:
    _assert_single_config_source(tuple(sorted((ROOT / "core").rglob("*.py"))))
    assert not any(path.exists() for path in RETIRED_INSTALLERS)


@pytest.mark.parametrize(
    "source",
    [
        """\
from pydantic import BaseModel
class LifecycleConfig(BaseModel):
    environment: str
    storage_backend: str
    public_host: str
    tls_mode: str
""",
        """\
from dataclasses import dataclass
@dataclass
class StackConfig:
    environment: str
    file_storage: str
    public_host: str
    release: str
""",
        """\
class Namespace:
    class LifecycleConfig:
        environment: str
        file_storage: str
        public_host: str
        release: str
""",
        """\
StackConfig = DeploymentConfig
""",
        """\
LIFECYCLE_CONFIG = {
    "environment": "development",
    "file_storage": "bundled",
    "public_host": "localhost",
    "tls_mode": "local",
}
""",
        """\
def build_lifecycle_config():
    return {
        "environment": "development",
        "storage": "bundled",
        "public_host": "localhost",
        "release_version": "current",
    }
""",
    ],
    ids=("pydantic", "dataclass", "nested", "alias", "mapping", "factory"),
)
def test_single_config_guard_rejects_synthetic_duplicate_shapes(
    tmp_path: Path,
    source: str,
) -> None:
    canonical = tmp_path / "core/application/lifecycle/contracts.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        """\
class DeploymentConfig:
    environment: str
    file_storage: str
    public_host: str
    tls_mode: str
""",
        encoding="utf-8",
    )
    duplicate = tmp_path / "core/application/lifecycle/future.py"
    duplicate.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="duplicate"):
        _assert_single_config_source((canonical, duplicate), source_root=tmp_path)


def test_public_management_docs_do_not_advertise_internal_command_paths() -> None:
    forbidden = (
        "python -m core.entrypoints.cli",
        "python3 -m core.entrypoints.cli",
        "python -m core.application.tooling.ops",
        "python3 -m core.application.tooling.ops",
        "core/entrypoints/cli/scripts/db_backup.sh",
        "core/entrypoints/cli/scripts/db_backup_validate.sh",
    )
    offenders = [
        f"{path.relative_to(ROOT)}: {value}"
        for path in PUBLIC_MANAGEMENT_DOCS
        for value in forbidden
        if value in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_documented_public_commands_exist_in_the_registry() -> None:
    public_paths = {tuple(spec.path) for spec in PUBLIC_COMMAND_SPECS}

    assert _documented_command_offenders(PUBLIC_MANAGEMENT_DOCS, public_paths) == []


def test_documented_command_guard_rejects_synthetic_unregistered_command(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "operator.md"
    doc.write_text("Run `./flow-steward future repair` now.\n", encoding="utf-8")
    public_paths = {tuple(spec.path) for spec in PUBLIC_COMMAND_SPECS}

    assert _documented_command_offenders((doc,), public_paths, source_root=tmp_path) != []


def test_registered_backup_commands_are_documented_as_available() -> None:
    public_paths = {tuple(spec.path) for spec in PUBLIC_COMMAND_SPECS}
    backup_paths = {("backup", "create"), ("backup", "list"), ("backup", "verify")}
    combined = "\n".join(path.read_text(encoding="utf-8") for path in PUBLIC_MANAGEMENT_DOCS)

    assert backup_paths.issubset(public_paths)
    assert "Phase 4 pending" not in combined
    assert "./flow-steward backup create" in combined
    assert "./flow-steward backup list" in combined
    assert "./flow-steward backup verify" in combined


def test_public_backup_boundary_is_documented() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in PUBLIC_MANAGEMENT_DOCS)

    assert "PostgreSQL-only" in combined
    assert "Storage objects are not copied" in combined
    assert "PostgreSQL restore runbook" in combined
    assert "Extensions are unrelated to core environment readiness" in combined
