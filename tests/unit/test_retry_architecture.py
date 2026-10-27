from __future__ import annotations

import ast
import json
import os
from collections import Counter
from pathlib import Path

from tests.support.source_index import parse_file

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", REPO_ROOT)).resolve()
INVENTORY_PATH = REPO_ROOT / "docs/architecture/outbound-http-inventory.json"
ALLOWED_OWNERS = {
    "orchestrator",
    "local_reconciliation",
    "no_retry_control_exchange",
}


def _call_name(node: ast.expr) -> tuple[str, str, bool]:
    if isinstance(node, ast.Name):
        return node.id, node.id, True
    if not isinstance(node, ast.Attribute):
        return "", "", False
    receiver = node.value
    direct_receiver = isinstance(receiver, ast.Name)
    while isinstance(receiver, ast.Attribute):
        receiver = receiver.value
    return node.attr, receiver.id if isinstance(receiver, ast.Name) else "", direct_receiver


def _function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return "module"


def _annotation_names(annotation: ast.expr | None) -> set[str]:
    if annotation is None:
        return set()
    return {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(annotation)
        if isinstance(node, (ast.Name, ast.Attribute))
    }


def _scan_file(path: Path, *, relative_path: str) -> Counter[str]:
    parsed = parse_file(path)
    source = parsed.text
    direct_openers: set[str] = set()
    network_modules: set[str] = set()
    http_clients: set[str] = set()
    http_client_constructors: set[str] = set()
    http_client_factories: set[str] = set()
    connection_clients: set[str] = set()
    connection_constructors: set[str] = set()
    connection_classes: set[str] = set()
    connection_factories: set[str] = set()

    for node in parsed.nodes_of(ast.Import, ast.ImportFrom):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"httpx", "requests", "urllib.request"}:
                    network_modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "urllib.request",
            "core.application.extensions.url_safety",
            "flowsteward_extension_sdk",
            "flowsteward_extension_sdk.http",
            "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk",
        }:
            for alias in node.names:
                if alias.name in {
                    "urlopen",
                    "open_pinned_url",
                    "open_platform_artifact_url",
                }:
                    direct_openers.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {"httpx", "requests"}:
            for alias in node.names:
                if alias.name in {"Client", "AsyncClient", "Session"}:
                    http_client_constructors.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "http.client":
            for alias in node.names:
                if alias.name in {"HTTPConnection", "HTTPSConnection"}:
                    connection_constructors.add(alias.asname or alias.name)

    changed = True
    while changed:
        changed = False
        known_constructors = {
            "HTTPConnection",
            "HTTPSConnection",
            *connection_constructors,
        }
        for node in parsed.nodes_of(ast.Assign, ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = {target.id for target in targets if isinstance(target, ast.Name)}
            value = node.value
            selected: set[str] = set()
            if isinstance(value, ast.IfExp):
                selected = {_call_name(value.body)[0], _call_name(value.orelse)[0]}
            elif isinstance(value, (ast.Name, ast.Attribute)):
                selected = {_call_name(value)[0]}
            if target_names and selected and selected.issubset(known_constructors):
                before = len(connection_constructors)
                connection_constructors.update(target_names)
                changed = changed or len(connection_constructors) != before

    class_bases: dict[str, set[str]] = {}
    for node in parsed.nodes_of(ast.ClassDef):
        class_bases[node.name] = {_call_name(base)[0] for base in node.bases if _call_name(base)[0]}
    changed = True
    while changed:
        changed = False
        known = {
            "HTTPConnection",
            "HTTPSConnection",
            *connection_constructors,
            *connection_classes,
        }
        for class_name, bases in class_bases.items():
            if class_name not in connection_classes and bases.intersection(known):
                connection_classes.add(class_name)
                changed = True

    function_returns: dict[str, set[str]] = {}
    for node in parsed.nodes_of(ast.FunctionDef, ast.AsyncFunctionDef):
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        for parameter in parameters:
            if _annotation_names(parameter.annotation).intersection(
                {"Client", "AsyncClient", "Session", *http_client_constructors}
            ):
                http_clients.add(parameter.arg)
        if _annotation_names(node.returns).intersection(
            {"Client", "AsyncClient", "Session", *http_client_constructors}
        ):
            http_client_factories.add(node.name)
        returned = {
            _call_name(child.value.func)[0]
            for child in parsed.subtree(node)
            if isinstance(child, ast.Return)
            and isinstance(child.value, ast.Call)
            and _call_name(child.value.func)[0]
        }
        function_returns[node.name] = returned
    changed = True
    while changed:
        changed = False
        known = {
            "HTTPConnection",
            "HTTPSConnection",
            *connection_constructors,
            *connection_classes,
            *connection_factories,
        }
        for function_name, returned in function_returns.items():
            if function_name not in connection_factories and returned.intersection(known):
                connection_factories.add(function_name)
                changed = True
    changed = True
    while changed:
        changed = False
        known_http_factories = {
            "Client",
            "AsyncClient",
            "Session",
            *http_client_constructors,
            *http_client_factories,
        }
        for function_name, returned in function_returns.items():
            if function_name not in http_client_factories and returned.intersection(
                known_http_factories
            ):
                http_client_factories.add(function_name)
                changed = True

    for node in parsed.nodes_of(ast.Assign, ast.AnnAssign, ast.With, ast.AsyncWith):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            callee, _, _ = _call_name(value.func)
            if callee in {
                "Client",
                "AsyncClient",
                "Session",
                *http_client_constructors,
                *http_client_factories,
            }:
                http_clients.update(names)
            elif callee in {
                "HTTPConnection",
                "HTTPSConnection",
                *connection_constructors,
                *connection_classes,
                *connection_factories,
            }:
                connection_clients.update(names)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                value = item.context_expr
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr in {"Client", "AsyncClient", "Session"}
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    http_clients.add(item.optional_vars.id)

    found: Counter[str] = Counter()
    parents: dict[ast.AST, ast.AST] | None = None
    for node in parsed.nodes_of(ast.Call):
        callee, root, direct_receiver = _call_name(node.func)
        outbound = (
            callee in direct_openers
            or callee in {"open_pinned_url", "open_platform_artifact_url"}
            or (callee == "urlopen" and (root in network_modules or "urllib" in source))
            or (
                direct_receiver
                and callee in {"get", "post", "put", "delete", "patch", "request", "send", "stream"}
                and root in network_modules | http_clients
            )
            or (direct_receiver and callee == "request" and root in connection_clients)
            or (
                relative_path.endswith("/release/checks.py")
                and direct_receiver
                and root == "api"
                and callee == "request"
            )
            or (callee == "_opener" and "open_pinned_url" in source)
            or (callee == "opener" and "open_pinned_url" in source)
        )
        if outbound:
            if parents is None:
                # Only files with an outbound call need the parent map.
                parents = {
                    child: parent
                    for parent in parsed.nodes
                    for child in ast.iter_child_nodes(parent)
                }
            key = f"{relative_path}|{_function_name(node, parents)}|{callee}"
            found[key] += 1
    return found


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in ("core", "extensions"):
        for path in (SOURCE_ROOT / root_name).rglob("*.py"):
            if any(part in {"tests", "test", "dev-wheels", "__pycache__"} for part in path.parts):
                continue
            files.append(path)
    return sorted(files)


def _scan_production_callers() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in _production_python_files():
        found.update(_scan_file(path, relative_path=path.relative_to(SOURCE_ROOT).as_posix()))
    return found


def test_every_production_outbound_http_caller_has_one_retry_owner() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert set(inventory["owners"]) == ALLOWED_OWNERS
    registered = Counter({key: int(value["count"]) for key, value in inventory["callers"].items()})
    assert all(value["owner"] in ALLOWED_OWNERS for value in inventory["callers"].values())
    assert _scan_production_callers() == registered


def test_inventory_scanner_rejects_an_unregistered_new_caller(tmp_path: Path) -> None:
    source = tmp_path / "new_transport.py"
    source.write_text(
        "import urllib.request\n\n"
        "def call():\n"
        "    return urllib.request.urlopen('https://example.test')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/application/new_transport.py")

    assert found == Counter({"core/application/new_transport.py|call|urlopen": 1})


def test_inventory_scanner_detects_direct_import_http_connection(tmp_path: Path) -> None:
    source = tmp_path / "direct_connection.py"
    source.write_text(
        "from http.client import HTTPConnection as Connection\n\n"
        "def call():\n"
        "    client = Connection('example.test')\n"
        "    client.request('GET', '/')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/entrypoints/cli/direct_connection.py")

    assert found == Counter({"core/entrypoints/cli/direct_connection.py|call|request": 1})


def test_inventory_scanner_detects_connection_subclass_and_factory(tmp_path: Path) -> None:
    source = tmp_path / "custom_connection.py"
    source.write_text(
        "import http.client\n\n"
        "class Routed(http.client.HTTPSConnection):\n"
        "    pass\n\n"
        "def make_client():\n"
        "    return Routed('example.test')\n\n"
        "def call():\n"
        "    client = make_client()\n"
        "    client.request('GET', '/')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/infrastructure/custom_connection.py")

    assert found == Counter({"core/infrastructure/custom_connection.py|call|request": 1})


def test_inventory_scanner_detects_dynamic_connection_constructor(tmp_path: Path) -> None:
    source = tmp_path / "catalog.py"
    source.write_text(
        "from http.client import HTTPConnection, HTTPSConnection\n\n"
        "def call(scheme):\n"
        "    connection_class = HTTPSConnection if scheme == 'https' else HTTPConnection\n"
        "    connection = connection_class('example.test')\n"
        "    connection.request('GET', '/')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/application/release/catalog.py")

    assert found == Counter({"core/application/release/catalog.py|call|request": 1})


def test_inventory_scanner_detects_injected_annotated_session(tmp_path: Path) -> None:
    source = tmp_path / "injected_session.py"
    source.write_text(
        "import requests\n\n"
        "def call(session: requests.Session):\n"
        "    return session.get('https://example.test')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/application/tooling/injected_session.py")

    assert found == Counter({"core/application/tooling/injected_session.py|call|get": 1})


def test_inventory_scanner_detects_session_created_by_local_factory(tmp_path: Path) -> None:
    source = tmp_path / "session_factory.py"
    source.write_text(
        "import requests\n\n"
        "def make_session() -> requests.Session:\n"
        "    return requests.Session()\n\n"
        "def call():\n"
        "    client = make_session()\n"
        "    return client.post('https://example.test')\n",
        encoding="utf-8",
    )

    found = _scan_file(source, relative_path="core/application/tooling/session_factory.py")

    assert found == Counter({"core/application/tooling/session_factory.py|call|post": 1})


def test_inventory_scanner_detects_release_harness_api_requests(tmp_path: Path) -> None:
    source = tmp_path / "checks.py"
    source.write_text(
        "from typing import Any\n\n"
        "def check(api: Any):\n"
        "    api.request('GET', '/api/jobs/1')\n"
        "    api.request('POST', '/api/jobs/1/cancel', {})\n",
        encoding="utf-8",
    )

    found = _scan_file(
        source,
        relative_path="extensions/example/release/checks.py",
    )

    assert found == Counter({"extensions/example/release/checks.py|check|request": 2})


def test_production_inventory_includes_cli_installer_and_tooling_trees() -> None:
    relative_paths = {
        path.relative_to(SOURCE_ROOT).as_posix() for path in _production_python_files()
    }

    assert any(path.startswith("core/entrypoints/cli/") for path in relative_paths)
    assert any(path.startswith("core/infrastructure/installer/") for path in relative_paths)
    assert any(path.startswith("core/application/tooling/") for path in relative_paths)
    assert "core/application/release/catalog.py" in relative_paths


def test_canonical_retry_modules_are_provider_neutral() -> None:
    canonical = [
        REPO_ROOT / "core/application/orchestration/retry_policy.py",
        REPO_ROOT / "core/application/orchestration/step_failures.py",
        REPO_ROOT / "core/application/orchestration/step_operation_retry.py",
        REPO_ROOT / "core/application/orchestration/step_hub.py",
    ]
    provider_names = {
        "google",
        "dropbox",
        "onedrive",
    }
    for path in canonical:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {str(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not any(name == "extensions" or name.startswith("extensions.") for name in imports)
        lowered = source.lower()
        assert not provider_names.intersection(lowered.split()), path
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            condition = ast.unparse(node.test)
            assert not any(
                field in condition for field in ("extension_id", "provider_name", "provider_id")
            ), f"provider branch in {path}: {condition}"
