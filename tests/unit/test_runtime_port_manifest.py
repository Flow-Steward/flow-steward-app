from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi import FastAPI

from core.entrypoints.api.runtime_port_manifest import (
    RUNTIME_GUARD_CLASSIFICATIONS,
    RUNTIME_SERVICE_PORT_CONTRACTS,
)
from core.infrastructure.runtime.app_factory import (
    build_runtime_container,
    validate_runtime_service_ports,
)


def _extension_callback_guards() -> set[tuple[str, str]]:
    source = (
        Path(__file__).resolve().parents[2]
        / "core"
        / "entrypoints"
        / "api"
        / "extension_callbacks.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    guards: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "hasattr" or len(node.args) < 2:
            continue
        receiver, method = node.args[:2]
        if (
            isinstance(receiver, ast.Name)
            and receiver.id in {"bl", "project_repository"}
            and isinstance(method, ast.Constant)
        ):
            guards.add((receiver.id, str(method.value)))
    return guards


def test_extension_callback_store_guards_are_classified_by_the_manifest() -> None:
    classified = {
        (item.receiver, item.method)
        for item in RUNTIME_GUARD_CLASSIFICATIONS
        if item.source_file == "extension_callbacks.py"
    }
    assert _extension_callback_guards() == classified
    assert all(
        item.mandatory or item.rationale.strip()
        for item in RUNTIME_GUARD_CLASSIFICATIONS
        if item.source_file == "extension_callbacks.py"
    )


def test_callback_project_authorization_contract_names_the_exact_missing_port() -> None:
    contract = next(
        item
        for item in RUNTIME_SERVICE_PORT_CONTRACTS
        if item.consumer == "api.extension_callbacks.project_authorization"
    )
    container = build_runtime_container(
        app=FastAPI(),
        config={},
        service_factories={"project_store": lambda: object()},
    )

    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "required runtime service seam 'api.extension_callbacks.project_authorization' "
            "dependency 'project_store' is missing methods: get_project"
        ),
    ):
        validate_runtime_service_ports(container, (contract,))


def test_extension_oauth_contract_names_the_exact_integration_store_ports() -> None:
    contract = next(
        item
        for item in RUNTIME_SERVICE_PORT_CONTRACTS
        if item.consumer == "api.extension_oauth.project_and_install_scope"
    )
    integration_store = type(
        "IntegrationStoreWithoutOAuthInstallLookup",
        (),
        {"get_project": lambda self, _project_id: None},
    )()
    container = build_runtime_container(
        app=FastAPI(),
        config={},
        service_factories={"integration_store": lambda: integration_store},
    )

    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "required runtime service seam 'api.extension_oauth.project_and_install_scope' "
            "dependency 'integration_store' is missing methods: "
            "get_extension_install_for_operator"
        ),
    ):
        validate_runtime_service_ports(container, (contract,))
