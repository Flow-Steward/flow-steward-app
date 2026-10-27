"""Structural contracts for shared graph-resume dependency wiring."""

from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

from core.infrastructure.runtime import graph_resume_wiring


def test_graph_resume_wiring_exposes_typed_dependency_groups() -> None:
    runtime_ports = getattr(graph_resume_wiring, "GraphResumeRuntimePorts", None)
    thread_config = getattr(graph_resume_wiring, "GraphResumeThreadConfig", None)

    assert runtime_ports is not None, "runtime callbacks must be grouped in a typed port object"
    assert thread_config is not None, "thread state must be grouped in a typed config object"
    assert is_dataclass(runtime_ports)
    assert is_dataclass(thread_config)
    assert [field.name for field in fields(runtime_ports)] == [
        "resume_shutdown_in_progress",
        "get_checkpointer",
        "build_graph",
        "get_logger_only",
        "job_thread_store",
        "to_json_obj",
        "append_approval_history",
        "build_approval_entry",
        "logger",
    ]
    assert [field.name for field in fields(thread_config)] == [
        "unregister_resume_thread",
        "resume_threads",
        "resume_threads_lock",
        "execution_mode",
    ]

    signature = inspect.signature(graph_resume_wiring.build_graph_resume_runtime_hooks)
    assert list(signature.parameters) == [
        "ports",
        "thread_config",
        "run_graph_resume_for_thread",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    for contract in (
        runtime_ports,
        thread_config,
        graph_resume_wiring.GraphResumeRuntimeHooks,
    ):
        assert Any not in get_type_hints(contract).values()

    source = Path(graph_resume_wiring.__file__).read_text(encoding="utf-8")
    assert "from typing import Any" not in source
    assert ": Any" not in source
