"""The fs-mcp image ships only some modules; telemetry must not need the rest.

Found on a live Scalable stand: a lazy import of a module the image does not
ship made log export drop every record silently and process metrics fail.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MCP_DOCKERFILE = REPO_ROOT / "infra" / "mcp" / "Dockerfile"
OBSERVABILITY = REPO_ROOT / "core" / "infrastructure" / "observability"
# Loaded only by the control plane, never by fs-mcp.
NOT_LOADED_BY_MCP = frozenset({"otel_installation_metrics.py"})


def _shipped_patterns() -> list[str]:
    patterns = []
    for line in MCP_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY "):
            continue
        sources = [part for part in line.split()[1:] if not part.startswith("--")][:-1]
        patterns.extend(source.rstrip("/") for source in sources if source.startswith("core"))
    return patterns


def _shipped(module: str, patterns: list[str]) -> bool:
    path = module.replace(".", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(f"{path}.py", pattern) or path.startswith(pattern + "/"):
            return True
    return False


def _guarded_by_import_error(node: ast.AST, parents: dict) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try):
            for handler in current.handlers:
                names = ast.unparse(handler.type) if handler.type is not None else ""
                if re.search(r"\b(ImportError|ModuleNotFoundError)\b", names):
                    return True
        current = parents.get(current)
    return False


def test_otel_modules_import_only_what_the_mcp_image_ships() -> None:
    patterns = _shipped_patterns()
    assert any("otel_" in pattern for pattern in patterns), patterns
    unshipped = []
    for path in sorted(OBSERVABILITY.glob("otel_*.py")):
        if path.name in NOT_LOADED_BY_MCP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("core."):
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names if alias.name.startswith("core.")]
            else:
                continue
            for module in modules:
                shipped = _shipped(module, patterns) or any(
                    _shipped(f"{module}.{alias.name}", patterns) for alias in node.names
                )
                if not shipped and not _guarded_by_import_error(node, parents):
                    unshipped.append(f"{path.name}:{node.lineno} {module}")
    assert unshipped == []


def test_the_mcp_runtime_never_loads_the_control_plane_metrics() -> None:
    for path in sorted(OBSERVABILITY.glob("otel_*.py")):
        if path.name not in NOT_LOADED_BY_MCP:
            assert "otel_installation_metrics" not in path.read_text(encoding="utf-8"), path.name
