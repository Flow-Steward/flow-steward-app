from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import parse_file

ENTRYPOINTS_ROOT = Path(__file__).resolve().parents[2] / "core" / "entrypoints"
ENTRYPOINT_DB_IMPORT_FILE_BASELINE = 0
ENTRYPOINT_DB_IMPORT_STATEMENT_BASELINE = 0


def _db_imports() -> tuple[list[str], list[str]]:
    files: list[str] = []
    imports: list[str] = []
    for path in sorted(ENTRYPOINTS_ROOT.rglob("*.py")):
        file_imports: list[str] = []
        for node in parse_file(path).nodes:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [str(node.module or "")]
            for module in modules:
                if module == "core.infrastructure.db" or module.startswith(
                    "core.infrastructure.db."
                ):
                    file_imports.append(
                        f"{path.relative_to(ENTRYPOINTS_ROOT)}:{getattr(node, 'lineno', 0)}"
                    )
        if file_imports:
            files.append(str(path.relative_to(ENTRYPOINTS_ROOT)))
            imports.extend(file_imports)
    return files, imports


def test_entrypoints_do_not_import_database_infrastructure() -> None:
    files, imports = _db_imports()

    assert len(files) <= ENTRYPOINT_DB_IMPORT_FILE_BASELINE, files
    assert len(imports) <= ENTRYPOINT_DB_IMPORT_STATEMENT_BASELINE, imports
