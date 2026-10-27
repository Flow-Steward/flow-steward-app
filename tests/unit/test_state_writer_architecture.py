from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import parse_file

APPLICATION_ROOT = Path(__file__).parents[2] / "core" / "application"


def test_user_target_state_writers_delegate_to_the_guarded_setter() -> None:
    """Every state writer accepting a user target must cross the one reserved-root guard."""
    bypasses: list[str] = []
    for path in APPLICATION_ROOT.rglob("*.py"):
        parsed = parse_file(path)
        for node in parsed.nodes_of(ast.FunctionDef, ast.AsyncFunctionDef):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            argument_names = {arg.arg for arg in [*node.args.args, *node.args.kwonlyargs]}
            if not ({"target", "state_target", "item_name"} & argument_names):
                continue
            if not ({"update", "updates", "state_obj", "state_updates"} & argument_names):
                continue
            if path.name == "step_templating.py" and node.name == "set_state_path":
                continue
            delegates = any(
                isinstance(child, ast.Call)
                and (isinstance(child.func, ast.Name) and child.func.id == "set_state_path")
                for child in parsed.subtree(node)
            )
            if not delegates:
                bypasses.append(f"{path.relative_to(APPLICATION_ROOT)}:{node.lineno}:{node.name}")

    assert bypasses == [], "unguarded user-target state writers: " + ", ".join(bypasses)


def test_response_mapping_writers_delegate_to_the_guarded_setter() -> None:
    """Nested response-mapping readers must not feed dynamic direct state writes."""
    bypasses: list[str] = []
    for path in APPLICATION_ROOT.rglob("*.py"):
        parsed = parse_file(path)
        for node in parsed.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            reads_response_mapping = any(
                isinstance(child, ast.Constant) and child.value == "response_mapping"
                for child in parsed.subtree(node)
            )
            writes_dynamic_target = any(
                isinstance(child, ast.Assign)
                and any(
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Name)
                    and "target" in target.slice.id
                    for target in child.targets
                )
                for child in parsed.subtree(node)
            )
            if not (reads_response_mapping and writes_dynamic_target):
                continue
            delegates = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "set_state_path"
                for child in parsed.subtree(node)
            )
            if not delegates:
                bypasses.append(f"{path.relative_to(APPLICATION_ROOT)}:{node.lineno}:{node.name}")

    assert bypasses == [], "unguarded response-mapping state writers: " + ", ".join(bypasses)
