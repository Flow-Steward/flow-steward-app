from __future__ import annotations

import ast
from pathlib import Path

DB_ROOT = Path(__file__).resolve().parents[2] / "core" / "infrastructure" / "db"
PLATFORM_LOGGER_MIXIN_BASELINE = 0
PLATFORM_LOGGER_PUBLIC_METHOD_BASELINE = 0


def _class_index() -> dict[str, ast.ClassDef]:
    classes: dict[str, ast.ClassDef] = {}
    for path in sorted(DB_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node
    return classes


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _platform_logger_class_family() -> tuple[set[str], dict[str, ast.ClassDef]]:
    classes = _class_index()
    reachable: set[str] = set()
    pending = ["PlatformLogger"]
    while pending:
        class_name = pending.pop()
        if class_name in reachable or class_name not in classes:
            continue
        reachable.add(class_name)
        pending.extend(
            base_name
            for base in classes[class_name].bases
            if (base_name := _base_name(base)) is not None
        )
    return reachable, classes


def test_platform_logger_god_object_surface_is_eliminated() -> None:
    class_family, classes = _platform_logger_class_family()
    mixins = sorted(class_family - {"PlatformLogger"})
    public_methods = sorted(
        f"{class_name}.{node.name}"
        for class_name in class_family
        for node in classes[class_name].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    )

    assert len(mixins) == PLATFORM_LOGGER_MIXIN_BASELINE, mixins
    assert len(public_methods) == PLATFORM_LOGGER_PUBLIC_METHOD_BASELINE, public_methods
