"""Phase 4 lifecycle code must never construct destructive operations."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).parents[3]
LIFECYCLE_SOURCE_ROOTS = (
    ROOT / "core" / "application" / "lifecycle",
    ROOT / "core" / "infrastructure" / "lifecycle",
)
LIFECYCLE_SOURCE_FILES = (
    ROOT / "core" / "entrypoints" / "cli" / "platform" / "lifecycle.py",
    ROOT / "core" / "entrypoints" / "cli" / "platform" / "service_bindings" / "lifecycle.py",
)
FORBIDDEN_ARGV_SEQUENCES = (
    ("docker", "compose", "down", "-v"),
    ("docker", "volume", "rm"),
    ("pg_restore", "--clean"),
    ("pg_restore", "--create"),
    ("pg_restore", "-d"),
    ("pg_restore", "--dbname"),
)
FORBIDDEN_SOURCE_PATTERNS = (
    re.compile(r"docker\s+compose\s+down\s+-v", re.IGNORECASE),
    re.compile(r"docker\s+volume\s+rm", re.IGNORECASE),
    re.compile(r"\b(?:drop\s+schema|drop\s+database)\b", re.IGNORECASE),
    re.compile(r"\b(?:create|recreate)\s+(?:schema|database)\b", re.IGNORECASE),
    re.compile(r"\b(?:recreate|reset)_(?:schema|database)\b", re.IGNORECASE),
    re.compile(r"\b(?:live|automatic)\s+(?:database\s+)?restore\b", re.IGNORECASE),
    re.compile(
        r"\b(?:compose_project_name|(?:durable_)?volume_(?:name|identity))\s*=", re.IGNORECASE
    ),
    re.compile(r"\b(?:secrets\.)?token_(?:hex|urlsafe|bytes)\b", re.IGNORECASE),
    re.compile(r"\b(?:rotate|regenerate)_(?:durable_)?(?:secret|key)\b", re.IGNORECASE),
)
DURABLE_SECRET_KEYS = frozenset(
    {
        "FS_SECRET_KEY",
        "ENCRYPTION_KEY",
        "FS_BACKUP_ENCRYPTION_KEY",
        "POSTGRES_PASSWORD",
        "RABBITMQ_DEFAULT_PASS",
    }
)
DURABLE_IDENTITY_KEYS = frozenset(
    {
        "FS_COMPOSE_PROJECT_NAME",
        "COMPOSE_PROJECT_NAME",
        "FS_POSTGRES_VOLUME_NAME",
        "FS_RABBITMQ_VOLUME_NAME",
        "FS_SEAWEEDFS_VOLUME_NAME",
    }
)


def _contains_sequence(argv: tuple[str, ...], forbidden: tuple[str, ...]) -> bool:
    width = len(forbidden)
    return any(argv[index : index + width] == forbidden for index in range(len(argv) - width + 1))


def _forbidden_argv(argv: Iterable[object]) -> tuple[str, ...] | None:
    normalized = tuple(str(item) for item in argv)
    return next(
        (
            forbidden
            for forbidden in FORBIDDEN_ARGV_SEQUENCES
            if _contains_sequence(normalized, forbidden)
        ),
        None,
    )


def _string_sequence(
    node: ast.AST, values_by_name: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        values = tuple(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
        return values if len(values) == len(node.elts) else None
    if isinstance(node, ast.Name):
        return values_by_name.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_sequence(node.left, values_by_name)
        right = _string_sequence(node.right, values_by_name)
        return (*left, *right) if left is not None and right is not None else None
    return None


def _constructed_argvs(tree: ast.AST) -> list[tuple[str, ...]]:
    values_by_name: dict[str, tuple[str, ...]] = {}
    argvs: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            literal_values = _string_sequence(node, values_by_name)
            if literal_values is not None:
                argvs.append(literal_values)
            continue
        if isinstance(node, ast.Assign):
            values = _string_sequence(node.value, values_by_name)
            if values is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values_by_name[target.id] = values
                    argvs.append(values)
            continue
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if not isinstance(node.op, ast.Add):
                continue
            extension = _string_sequence(node.value, values_by_name)
            existing = values_by_name.get(node.target.id)
            if extension is not None and existing is not None:
                values_by_name[node.target.id] = (*existing, *extension)
                argvs.append(values_by_name[node.target.id])
            continue
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or not node.args:
            continue
        command_name = node.func.value.id
        existing = values_by_name.get(command_name)
        if existing is None:
            continue
        if node.func.attr == "extend":
            extension = _string_sequence(node.args[0], values_by_name)
        elif node.func.attr == "append" and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            extension = (value,) if isinstance(value, str) else None
        else:
            continue
        if extension is not None:
            values_by_name[command_name] = (*existing, *extension)
            argvs.append(values_by_name[command_name])
    return argvs


def _mutated_mapping_keys(tree: ast.AST, protected_keys: frozenset[str]) -> set[str]:
    mutated: set[str] = set()
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else (node.target,)
            if isinstance(node, ast.AugAssign)
            else ()
        )
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            slice_value = target.slice
            if (
                isinstance(slice_value, ast.Constant)
                and isinstance(slice_value.value, str)
                and slice_value.value in protected_keys
            ):
                mutated.add(slice_value.value)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"setdefault", "__setitem__"} and node.args:
            key = node.args[0]
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in protected_keys
            ):
                mutated.add(key.value)
        if node.func.attr == "update" and node.args and isinstance(node.args[0], ast.Dict):
            for key in node.args[0].keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in protected_keys
                ):
                    mutated.add(key.value)
    return mutated


def _lifecycle_source_paths() -> list[Path]:
    paths = [
        source_path
        for source_root in LIFECYCLE_SOURCE_ROOTS
        if source_root.exists()
        for source_path in source_root.rglob("*.py")
    ]
    paths.extend(source_path for source_path in LIFECYCLE_SOURCE_FILES if source_path.exists())
    return sorted(paths)


def _display_path(source_path: Path) -> str:
    try:
        return source_path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(source_path)


def _prohibited_operation_violations(source_path: Path) -> list[str]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    violations = [
        f"{_display_path(source_path)} contains {pattern.pattern!r}"
        for pattern in FORBIDDEN_SOURCE_PATTERNS
        if pattern.search(source)
    ]
    violations.extend(
        f"{_display_path(source_path)} constructs forbidden argv {forbidden!r}"
        for argv in _constructed_argvs(tree)
        if (forbidden := _forbidden_argv(argv)) is not None
    )
    violations.extend(
        f"{_display_path(source_path)} assigns durable secret {secret_key}"
        for secret_key in sorted(_mutated_mapping_keys(tree, DURABLE_SECRET_KEYS))
    )
    violations.extend(
        f"{_display_path(source_path)} mutates Compose/volume identity {identity_key}"
        for identity_key in sorted(_mutated_mapping_keys(tree, DURABLE_IDENTITY_KEYS))
    )
    return violations


def test_forbidden_lifecycle_argv_sequences_are_detected() -> None:
    for forbidden in FORBIDDEN_ARGV_SEQUENCES:
        assert _forbidden_argv(("prefix", *forbidden, "suffix")) == forbidden

    assert _forbidden_argv(("docker", "compose", "down")) is None
    assert _forbidden_argv(("pg_restore", "--list")) is None


def test_lifecycle_source_guard_detects_each_prohibited_operation(tmp_path: Path) -> None:
    cases = (
        ('command = "docker compose down -v"\n', "contains"),
        ('command = ["docker", "volume", "rm", "flow-steward"]\n', "forbidden argv"),
        ('subprocess.run(["docker", "compose", "down", "-v"])\n', "forbidden argv"),
        (
            'command = ["docker", "compose"]\ncommand.extend(["down", "-v"])\n',
            "forbidden argv",
        ),
        ('operation = "live database restore"\n', "contains"),
        ('statement = "DROP SCHEMA public CASCADE"\n', "contains"),
        ("recreate_schema()\n", "contains"),
        ('environment["FS_COMPOSE_PROJECT_NAME"] = "replacement"\n', "identity"),
        ('durable_volume_name = "replacement"\n', "identity"),
        ('environment["FS_SECRET_KEY"] = "replacement"\n', "assigns durable secret"),
        ('environment.update({"FS_SECRET_KEY": "replacement"})\n', "assigns durable secret"),
        ("rotated = secrets.token_urlsafe()\n", "contains"),
    )

    for index, (source, expected) in enumerate(cases):
        source_path = tmp_path / f"prohibited_{index}.py"
        source_path.write_text(source, encoding="utf-8")

        assert any(
            expected in violation for violation in _prohibited_operation_violations(source_path)
        )


def test_lifecycle_sources_do_not_construct_prohibited_operations() -> None:
    violations = [
        violation
        for source_path in _lifecycle_source_paths()
        for violation in _prohibited_operation_violations(source_path)
    ]

    assert violations == []
