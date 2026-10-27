from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
APPLICATION_EXECUTION_MODULES = (
    ROOT / "core/application/jobs/execution_contracts.py",
    ROOT / "core/application/jobs/task_execution_service.py",
    ROOT / "core/application/jobs/task_execution_request.py",
    ROOT / "core/application/jobs/workflow_resume_state.py",
    ROOT / "core/application/jobs/workflow_resume_execution_service.py",
    ROOT / "core/application/orchestration/child_execution_runtime.py",
)


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return imports


def test_application_job_execution_modules_have_no_infrastructure_imports() -> None:
    missing = [
        str(path.relative_to(ROOT)) for path in APPLICATION_EXECUTION_MODULES if not path.is_file()
    ]
    assert missing == []

    violations = [
        f"{path.relative_to(ROOT)}: {module}"
        for path in APPLICATION_EXECUTION_MODULES
        for module in _imported_modules(path)
        if module == "core.infrastructure" or module.startswith("core.infrastructure.")
    ]

    assert violations == []


def test_child_workflow_application_runtime_has_no_worker_imports() -> None:
    paths = (
        ROOT / "core/application/orchestration/child_execution_runtime.py",
        ROOT / "core/application/orchestration/invoke_workflow_step_node_runtime.py",
    )

    violations = [
        f"{path.relative_to(ROOT)}: {module}"
        for path in paths
        for module in _imported_modules(path)
        if module == "core.infrastructure.runtime.worker"
        or module.startswith("core.infrastructure.runtime.worker.")
    ]

    assert violations == []


def test_invoke_workflow_node_does_not_import_worker_execution_adapter() -> None:
    path = ROOT / "core/application/orchestration/invoke_workflow_step_node.py"

    assert "core.infrastructure.runtime.worker.worker_execution_adapters" not in _imported_modules(
        path
    )


def test_job_invocation_artifact_plane_uses_bound_child_runtime() -> None:
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.child_execution_runtime import (
        ChildExecutionRuntime,
        bind_child_execution_runtime,
    )

    artifact_plane = object()
    runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _task_message: {"action": "ack"},
        artifact_plane_provider=lambda: artifact_plane,
    )

    with bind_child_execution_runtime(runtime):
        assert node._job_invocation_artifact_plane() is artifact_plane


def test_workflow_resume_state_moved_without_management_compatibility_shim() -> None:
    assert (ROOT / "core/application/jobs/workflow_resume_state.py").is_file()
    assert not (ROOT / "core/application/management/workflow_resume_state.py").exists()
    adr = (ROOT / "docs/architecture/adr/0002-runtime-ownership.md").read_text(encoding="utf-8")
    assert "core/application/jobs/workflow_resume_state.py" in adr
    assert "core/application/management/workflow_resume_state.py" not in adr


def test_worker_adapter_contains_no_raw_sql_or_business_outcome_policy() -> None:
    path = ROOT / "core/infrastructure/runtime/worker/worker_task_execution.py"
    source = path.read_text(encoding="utf-8")

    assert _raw_sql_or_cursor_violations(source, filename=str(path)) == []
    assert "unexpected_graph_status" not in source
    assert "workflow_resume_revision_mismatch" not in source


def test_worker_runtime_contains_no_workflow_resume_compatibility_wiring() -> None:
    path = ROOT / "core/infrastructure/runtime/worker/worker_task_execution.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    declared = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.ClassDef)
    }
    forbidden = {
        "_CompatibilityApprovalRepository",
        "_CompatibilityApprovalGraph",
        "_CompatibilityApprovalLease",
        "_NoopApprovalLease",
        "_execute_task",
        "_execute_workflow_resume_message",
        "_compatibility_terminal",
        "_claim_workflow_resume_job",
        "_renew_workflow_resume_lease",
        "_mark_workflow_resume_completed",
        "_claim_workflow_resume_finalizer",
        "_renew_workflow_resume_finalizer",
        "_mark_workflow_resume_finalization_step",
        "_release_workflow_resume_finalizer",
        "_mark_workflow_resume_retryable",
        "_mark_workflow_resume_finalizing",
        "_pending_workflow_resume_rows",
        "_mark_workflow_resume_publish_state",
        "_start_workflow_resume_lease_heartbeat",
        "_start_workflow_resume_finalizer_heartbeat",
        "_start_heartbeat",
        "_stop_lease_heartbeat",
        "_build_worker_graph_resume_runner",
        "_without_lease_seconds",
    }

    assert sorted(declared & forbidden) == []


_WORKER_RUNTIME_MODULE_ALIASES = {"_task_execution_runtime", "_schema_compat_runtime"}
_WORKER_RUNTIME_MODULE_NAMES = {"worker_task_execution", "worker_schema_compat"}
_DICT_MUTATION_METHODS = {"clear", "pop", "popitem", "setdefault", "update", "__setitem__"}


def _expression_root_name(expression: ast.expr) -> str:
    current = expression
    while isinstance(current, ast.Attribute | ast.Subscript):
        current = current.value
    return current.id if isinstance(current, ast.Name) else ""


def _runtime_module_aliases(tree: ast.AST) -> set[str]:
    aliases = set(_WORKER_RUNTIME_MODULE_ALIASES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name.rsplit(".", 1)[-1] in _WORKER_RUNTIME_MODULE_NAMES:
                    aliases.add(imported.asname or imported.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in _WORKER_RUNTIME_MODULE_NAMES:
                    aliases.add(imported.asname or imported.name)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            if not isinstance(value, ast.Name) or value.id not in aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _is_runtime_dict_expression(expression: ast.expr, module_aliases: set[str]) -> bool:
    if (
        isinstance(expression, ast.Attribute)
        and expression.attr == "__dict__"
        and _expression_root_name(expression) in module_aliases
    ):
        return True
    return bool(
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "vars"
        and expression.args
        and _expression_root_name(expression.args[0]) in module_aliases
    )


def _runtime_dict_aliases(tree: ast.AST, module_aliases: set[str]) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            is_runtime_dict = _is_runtime_dict_expression(value, module_aliases) or (
                isinstance(value, ast.Name) and value.id in aliases
            )
            if not is_runtime_dict:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _target_mutates_runtime_module(
    target: ast.expr,
    *,
    module_aliases: set[str],
    dict_aliases: set[str],
) -> bool:
    return any(
        (
            isinstance(node, ast.Attribute | ast.Subscript)
            and _expression_root_name(node) in module_aliases
        )
        or (
            isinstance(node, ast.Subscript)
            and (
                _expression_root_name(node) in dict_aliases
                or _is_runtime_dict_expression(node.value, module_aliases)
            )
        )
        for node in ast.walk(target)
    )


def _runtime_module_mutation_violations(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    module_aliases = _runtime_module_aliases(tree)
    dict_aliases = _runtime_dict_aliases(tree, module_aliases)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            protected = sorted(set(node.names) & (module_aliases | dict_aliases))
            if protected:
                violations.append(f"{filename}:{node.lineno}: global {','.join(protected)}")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets.append(node.target)
        elif isinstance(node, ast.Delete):
            targets.extend(node.targets)
        if any(
            _target_mutates_runtime_module(
                target,
                module_aliases=module_aliases,
                dict_aliases=dict_aliases,
            )
            for target in targets
        ):
            violations.append(f"{filename}:{node.lineno}: runtime module assignment/deletion")
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and node.args
            and _expression_root_name(node.args[0]) in module_aliases
        ):
            violations.append(f"{filename}:{node.lineno}: {node.func.id} runtime module")
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        root_name = _expression_root_name(node.func)
        mutation_method = node.func.attr in {
            "set",
            "configure",
            "patch",
        } or node.func.attr.startswith(("set_", "configure_", "patch_"))
        dict_mutation = node.func.attr in _DICT_MUTATION_METHODS and (
            root_name in dict_aliases
            or _is_runtime_dict_expression(node.func.value, module_aliases)
        )
        if (root_name in module_aliases and mutation_method) or dict_mutation:
            violations.append(
                f"{filename}:{node.lineno}: runtime module mutation call {node.func.attr}"
            )
    return violations


_SQL_DML_PATTERN = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


def _static_string(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left = _static_string(expression.left)
        right = _static_string(expression.right)
        return left + right if left is not None and right is not None else None
    if isinstance(expression, ast.JoinedStr):
        values = [_static_string(value) for value in expression.values]
        return "".join(values) if all(value is not None for value in values) else None
    return None


def _raw_sql_or_cursor_violations(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            receiver_name = (
                _expression_root_name(node.func.value)
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            database_execute = call_name in {"execute", "executemany"} and receiver_name in {
                "conn",
                "connection",
                "cur",
                "cursor",
                "db",
                "database",
            }
            if call_name == "cursor" or database_execute:
                violations.append(f"{filename}:{node.lineno}: database call {call_name}")
        if isinstance(node, ast.expr):
            value = _static_string(node)
            if value is not None and _SQL_DML_PATTERN.search(value):
                violations.append(f"{filename}:{node.lineno}: raw SQL literal")
    return sorted(set(violations))


def test_worker_and_schema_composition_never_mutate_imported_modules_or_globals() -> None:
    paths = (
        ROOT / "core/infrastructure/runtime/worker/worker.py",
        ROOT / "core/infrastructure/runtime/worker/worker_schema_compat.py",
    )
    violations: list[str] = []
    for path in paths:
        violations.extend(
            _runtime_module_mutation_violations(
                path.read_text(encoding="utf-8"),
                filename=path.name,
            )
        )

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "_task_execution_runtime.worker_id = fake_worker_id",
        "setattr(_schema_compat_runtime, 'run_check', fake_check)",
        "_task_execution_runtime.set_dependencies(fake_dependencies)",
        "_schema_compat_runtime.__dict__.update({'run_check': fake_check})",
        "del _task_execution_runtime.__dict__['worker_id']",
        "runtime_alias = _task_execution_runtime\nruntime_alias.worker_id = fake_worker_id",
        "runtime_alias = _schema_compat_runtime\nruntime_alias.configure(fake_check)",
        "runtime_dict = vars(_task_execution_runtime)\nruntime_dict.update({'worker_id': fake})",
        "runtime_dict = _schema_compat_runtime.__dict__\nruntime_dict['run_check'] = fake",
        (
            "from core.infrastructure.runtime.worker import worker_task_execution as runtime_alias\n"
            "setattr(runtime_alias, 'worker_id', fake_worker_id)"
        ),
    ],
)
def test_global_mutation_guard_detects_assignments_setters_and_dict_mutation(
    source: str,
) -> None:
    assert _runtime_module_mutation_violations(source, filename="synthetic.py")


@pytest.mark.parametrize(
    "source",
    [
        "connection.cursor()",
        "cursor.execute(query)",
        'query = "UP" + "DATE jobs SET status = 1"',
    ],
)
def test_worker_sql_guard_detects_database_calls_and_split_dml_literals(source: str) -> None:
    assert _raw_sql_or_cursor_violations(source, filename="synthetic.py")


def _approval_factory_call(path: Path, call_name: str) -> ast.Call:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == call_name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == call_name)
        )
    ]
    assert len(calls) == 1
    return calls[0]


def test_http_and_non_http_approval_composition_omit_retired_resume_plumbing() -> None:
    compositions = (
        (
            ROOT / "core/entrypoints/api/app_factory.py",
            "build_approval_command_service_factory",
        ),
        (
            ROOT / "core/entrypoints/cli/approval_command_runtime.py",
            "create_approval_command_service",
        ),
    )

    for path, call_name in compositions:
        call = _approval_factory_call(path, call_name)
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert "workflow_resume_repository_factory" not in keyword_names, path
        assert "enqueue_workflow_resume" not in keyword_names, path
        assert "start_graph_resume_thread" not in keyword_names, path
        assert "set_job_status_by_thread_id" not in keyword_names, path
