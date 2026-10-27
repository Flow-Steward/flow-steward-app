"""Contracts for the isolated reusable CLI keepalive process."""

from __future__ import annotations

import importlib
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _warm_runtime():
    return importlib.import_module("core.entrypoints.cli.warm_runtime")


@pytest.mark.parametrize("shutdown_signal", (signal.SIGTERM, signal.SIGINT))
def test_keepalive_shutdown_signals_request_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    shutdown_signal: signal.Signals,
) -> None:
    """A stopped reusable container must let its keepalive process exit cleanly."""
    warm_runtime = _warm_runtime()
    handlers: dict[int, object] = {}
    handlers_registered = threading.Event()
    stop = threading.Event()

    def register_handler(signum: int, handler: object) -> None:
        handlers[signum] = handler
        if {signal.SIGTERM, signal.SIGINT}.issubset(handlers):
            handlers_registered.set()

    monkeypatch.setattr(warm_runtime.signal, "signal", register_handler)
    worker = threading.Thread(target=warm_runtime.wait_for_shutdown, args=(stop,))
    worker.start()

    assert handlers_registered.wait(timeout=1), "keepalive did not register both shutdown handlers"
    handler = handlers[shutdown_signal]
    assert callable(handler)
    handler(shutdown_signal, None)
    worker.join(timeout=1)

    assert stop.is_set()
    assert not worker.is_alive()


def test_importing_keepalive_does_not_load_cli_application_or_infrastructure() -> None:
    """The idle container process must stay independent of product runtime imports."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, json, sys; "
                "before = set(sys.modules); "
                "importlib.import_module('core.entrypoints.cli.warm_runtime'); "
                "print(json.dumps(sorted(set(sys.modules) - before)))"
            ),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    imported_by_keepalive = json.loads(completed.stdout)
    forbidden_prefixes = (
        "core.entrypoints.cli.click_app",
        "core.application",
        "core.infrastructure.db",
        "docker",
    )
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported_by_keepalive
        for prefix in forbidden_prefixes
    )
