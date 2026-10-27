"""Dispatch each bundle's release checks into an isolated container.

Some release assertions are about a particular extension: its workflows, its
callbacks, the operation it exposes. Those belong to that extension — the
platform's smoke should not know they exist, and they should not break when the
platform ships a different set of bundles.

**A bundle's check is extension code, and it does not run here.** This module
never imports or executes anything under `extensions/`. It scans for bundles that
declare a suite, reads their declaration as data, and starts a container per
bundle:

* a stock Python image — no Core source in it, and none mounted;
* the bundle mounted read-only, and the platform's harness read-only beside it;
* no Docker socket, no platform DSN, no `.env`, no inherited environment;
* an explicit allowlist: the candidate URL on the candidate's own network, a
  bearer token minted for this run with only the scopes the bundle declared, and
  the addresses of the test services it declared it observes;
* dropped capabilities, no new privileges, a read-only root filesystem and no
  network but the candidate's.

The declaration is read by parsing, not by importing: `RELEASE_SCOPES` and
`RELEASE_INPUTS` are literals in the bundle's `checks.py`, and reading them with
`ast` keeps the "extension code does not run here" rule true of discovery too.

The keyword tests in each bundle stay useful — they catch the honest mistake in
review — but this is the boundary, and
`tests/integration/test_bundle_release_isolation.py` proves it by running a
hostile `acme.*` fixture through this runner and checking that every reach it
makes is denied.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: Where a bundle puts them, relative to its own directory.
CHECKS_RELATIVE_PATH = Path("release") / "checks.py"
#: The names a bundle declares, all read as literals.
CHECKS_ATTRIBUTE = "RELEASE_CHECKS"
SCOPES_ATTRIBUTE = "RELEASE_SCOPES"
INPUTS_ATTRIBUTE = "RELEASE_INPUTS"
#: Connections and workflows the trusted side must create for the run. Both are
#: registry/admin writes on the candidate, so a suite cannot make them itself —
#: it declares them and receives the ids.
CONNECTIONS_ATTRIBUTE = "RELEASE_CONNECTIONS"
#: A workflow is declared as metadata plus `changes`, the change operations that
#: build its steps and graph (see `_prepare_workflow`).
WORKFLOWS_ATTRIBUTE = "RELEASE_WORKFLOWS"
#: Aliases of the test services a suite needs on its network. A suite that names
#: none gets a network with the candidate on it and nothing else.
SERVICES_ATTRIBUTE = "RELEASE_SERVICES"
#: Extensions the trusted side must enable for the run and restore afterwards.
#: A suite cannot do it: enable and disable are admin-only routes.
EXTENSIONS_ATTRIBUTE = "RELEASE_EXTENSIONS"

#: What a suite may be given. Every DELETE on the candidate is admin-only, so
#: none of these lets a suite remove anything — including what it made.
NARROW_SUITE_SCOPES = (
    # Declared by the actions themselves — `send_email` asks for `artifact:read`
    # so it can attach one, and an action's own `required_scopes` are the
    # narrowest honest description of what invoking it needs.
    "artifact:read",
    "command:*",
    "extension:invoke",
    "jobs:*",
    "notify:*",
    "projects:*",
    "read:*",
    "registry:*",
    "workflows:*",
)

#: The image the suites run in. Deliberately not a Flow Steward image: those
#: carry Core source, and a suite must not be able to read it.
#:
#: Pinned by digest, not by tag: `python:3.12-slim` is republished, so a tag says
#: nothing about what will actually run, and a suite's isolation must not change
#: underneath a release because an upstream image moved.
DEFAULT_RUNNER_IMAGE = (
    "python@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc"
)
RUNNER_IMAGE_ENV = "FS_SMOKE_BUNDLE_RUNNER_IMAGE"
#: How the candidate answers from the suite's own network, and which container
#: answers there. The alias is what the URL must name.
CANDIDATE_INTERNAL_URL_ENV = "FS_SMOKE_CANDIDATE_INTERNAL_BASE"
CANDIDATE_APP_CONTAINER_ENV = "FS_SMOKE_CANDIDATE_APP_CONTAINER"
CANDIDATE_APP_ALIAS_ENV = "FS_SMOKE_CANDIDATE_APP_ALIAS"
#: `alias=container` pairs a bundle is allowed to reach besides the candidate —
#: the mail sink and nothing else unless it is named here.
RELEASE_SERVICES_ENV = "FS_SMOKE_RELEASE_SERVICE_CONTAINERS"
INPUT_PREFIX = "FS_RELEASE_INPUT_"
#: Long enough for a suite that runs real workflows, bounded so a hung one ends.
DEFAULT_TIMEOUT_SECONDS = 3600
#: What one suite may consume. A release check is not a workload.
RUNNER_CPUS = "1.0"
RUNNER_MEMORY = "512m"
RUNNER_PIDS = "128"
#: Nobody in the image's passwd file, so the process owns nothing on any mount.
RUNNER_USER = "65534:65534"

HARNESS_PATH = Path(__file__).resolve().parent / "bundle_release_harness.py"


class BundleSuite:
    """One bundle's declared release suite, read without executing it."""

    def __init__(
        self,
        name: str,
        path: Path,
        checks: list[str],
        scopes: list[str],
        inputs: list[str],
        extensions: list[str],
        services: list[str],
        connections: list[dict[str, Any]],
        workflows: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.path = path
        self.checks = checks
        self.scopes = scopes
        self.inputs = inputs
        self.extensions = extensions
        self.services = services
        self.connections = connections
        self.workflows = workflows

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"BundleSuite({self.name!r}, checks={self.checks!r})"


def suites_require_model_route(suites: list[BundleSuite]) -> bool:
    """Return whether any selected suite declares both model-route inputs."""
    required = {"FS_SMOKE_MODEL_PROVIDER", "FS_SMOKE_MODEL"}
    return any(required.issubset(set(suite.inputs)) for suite in suites)


def extensions_root(override: str | Path = "") -> Path:
    """Where bundles live on the candidate, overridable for a stand."""
    return Path(str(override) or "/app/extensions")


def _assignments(module: ast.Module) -> Iterator[tuple[str, ast.expr]]:
    """Yield `(name, value)` for each module-level assignment, plain or annotated."""
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id, value


def _literal(module: ast.Module, name: str) -> Any:
    """Read a module-level literal without importing the module."""
    for assigned, value in _assignments(module):
        if assigned == name:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
    return None


def _declared_check_ids(module: ast.Module) -> list[str]:
    """The keys of `RELEASE_CHECKS`, whose values are functions and so not literal."""
    for assigned, value in _assignments(module):
        if assigned == CHECKS_ATTRIBUTE and isinstance(value, ast.Dict):
            return [
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
    return []


def discover_bundle_suites(root: str | Path = "") -> list[BundleSuite]:
    """Return the suites the installed bundles declare, read as data.

    Nothing is named here: whichever bundles ship contribute whichever checks
    they declare, and a candidate that ships none contributes none.
    """
    discovered: list[BundleSuite] = []
    base = extensions_root(root)
    if not base.is_dir():
        return discovered
    seen: dict[str, str] = {}
    for bundle in sorted(path for path in base.iterdir() if path.is_dir()):
        module_path = bundle / CHECKS_RELATIVE_PATH
        if not module_path.is_file():
            continue
        module = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        checks = _declared_check_ids(module)
        for check_id in checks:
            if check_id in seen:
                raise RuntimeError(
                    f"release check '{check_id}' is declared by both "
                    f"'{seen[check_id]}' and '{bundle.name}'"
                )
            seen[check_id] = bundle.name
        discovered.append(
            BundleSuite(
                name=bundle.name,
                path=bundle,
                checks=checks,
                scopes=[str(value) for value in _literal(module, SCOPES_ATTRIBUTE) or []],
                inputs=[str(value) for value in _literal(module, INPUTS_ATTRIBUTE) or []],
                extensions=[str(value) for value in _literal(module, EXTENSIONS_ATTRIBUTE) or []],
                services=[str(value) for value in _literal(module, SERVICES_ATTRIBUTE) or []],
                connections=[
                    dict(value)
                    for value in _literal(module, CONNECTIONS_ATTRIBUTE) or []
                    if isinstance(value, dict)
                ],
                workflows=[
                    dict(value)
                    for value in _literal(module, WORKFLOWS_ATTRIBUTE) or []
                    if isinstance(value, dict)
                ],
            )
        )
    return discovered


def mint_scoped_token(api: Any, suite: BundleSuite) -> tuple[str, str]:
    """Mint a token carrying only the scopes this suite declared.

    Returns the raw token and the id to revoke it by. A suite that declares
    nothing gets nothing, which is the correct answer for a suite that has not
    said what it needs.
    """
    if not suite.scopes:
        return "", ""
    over_broad = sorted(set(suite.scopes) - set(NARROW_SUITE_SCOPES))
    if over_broad:
        # A suite asking for more than the narrow set is asking for rights the
        # trusted side is supposed to keep — refused here rather than minted.
        raise RuntimeError(
            f"{suite.name} declares scopes outside the narrow set: {', '.join(over_broad)}"
        )
    status, payload = api.request(
        "POST",
        "/api/auth/tokens",
        {
            "label": f"release-suite-{suite.name}-{uuid.uuid4().hex[:8]}",
            "scopes": sorted(suite.scopes),
            "expires_in_days": 1,
        },
    )
    if status != 200:
        raise RuntimeError(f"scoped token for {suite.name} failed with HTTP {status}: {payload!r}")
    token = payload.get("token") if isinstance(payload, dict) else {}
    token = token if isinstance(token, dict) else {}
    return str(token.get("raw_token") or ""), str(
        (payload or {}).get("token_id") or token.get("id") or ""
    )


def revoke_token(api: Any, token_id: str) -> None:
    if token_id:
        api.request("POST", f"/api/auth/tokens/{token_id}/revoke", {})


#: How a declared fixture refers to something only the trusted side knows.
#: Deliberately three narrow forms rather than an expression language: a bundle
#: describes what it needs, it does not compute.
_PLACEHOLDER = re.compile(r"\$\{(env|connection|install):([^}]+)\}")


def _resolve_placeholders(value: Any, bindings: dict[str, str]) -> Any:
    """Fill `${env:NAME}`, `${connection:key}` and `${install:extension_id}`.

    A whole-string placeholder keeps the resolved type — a port stays an int —
    while one embedded in text substitutes into it.
    """
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item, bindings) for item in value]
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER.fullmatch(value)
    if whole is not None:
        resolved = bindings.get(f"{whole.group(1)}:{whole.group(2)}", "")
        if whole.group(1) == "env" and resolved.isdigit():
            return int(resolved)
        return resolved
    return _PLACEHOLDER.sub(
        lambda match: bindings.get(f"{match.group(1)}:{match.group(2)}", ""), value
    )


def _install_id(api: Any, extension_id: str) -> str:
    _, detail = api.request("GET", f"/api/extensions/{extension_id}")
    return str((detail or {}).get("install_id") or "")


@contextmanager
def prepared_fixtures(api: Any, suite: BundleSuite) -> Iterator[dict[str, str]]:
    """Do for a suite what its token may not, and undo it afterwards.

    Enabling an extension and deleting anything are admin-only on the candidate.
    Handing a suite a token that could do either would give it the run of the
    platform, so the trusted side does both: it enables what the bundle declared,
    creates the project the suite works in, and removes it when the run ends —
    restoring every extension to the state it was found in.
    """
    restore: list[tuple[str, str]] = []
    project_id = ""
    workflow_ids: list[str] = []
    try:
        bindings: dict[str, str] = {
            f"env:{name}": str(os.environ.get(name) or "") for name in suite.inputs
        }
        for extension_id in suite.extensions:
            status, detail = api.request("GET", f"/api/extensions/{extension_id}")
            state = str((detail or {}).get("state") or "") if status == 200 else ""
            api.request("POST", f"/api/extensions/{extension_id}/enable", {})
            restore.append((extension_id, state))
            bindings[f"install:{extension_id}"] = _install_id(api, extension_id)
        workspace_id = _workspace_id(api)
        project_id = _prepare_project(api, suite, workspace_id)
        fixtures = {
            # `/api/workspaces` is admin-only, so a suite cannot look one up; it
            # is handed the one its project was made in.
            "FS_RELEASE_PROJECT_ID": project_id,
            "FS_RELEASE_WORKSPACE_ID": workspace_id,
        }
        for spec in suite.connections:
            key, connection_id = _prepare_connection(api, spec, project_id, bindings)
            bindings[f"connection:{key}"] = connection_id
            fixtures[f"FS_RELEASE_CONNECTION_{key.upper()}"] = connection_id
        for spec in suite.workflows:
            key, workflow_id = _prepare_workflow(api, spec, project_id, bindings)
            workflow_ids.append(workflow_id)
            fixtures[f"FS_RELEASE_WORKFLOW_{key.upper()}"] = workflow_id
        yield fixtures
    finally:
        # Reverse of creation: the project holds the bindings, so it goes first.
        if project_id:
            api.request("DELETE", f"/api/projects/{project_id}")
        for workflow_id in reversed(workflow_ids):
            api.request("DELETE", f"/api/workflows/{workflow_id}")
            api.request("POST", f"/api/trash/registry_workflow/{workflow_id}/hard-delete", {})
        for extension_id, state in reversed(restore):
            if state == "disabled":
                api.request("POST", f"/api/extensions/{extension_id}/disable", {})


def _prepare_connection(
    api: Any, spec: dict[str, Any], project_id: str, bindings: dict[str, str]
) -> tuple[str, str]:
    """Create one connection the bundle declared, in the prepared project."""
    body = _resolve_placeholders({k: v for k, v in spec.items() if k != "key"}, bindings)
    status, payload = api.request("POST", f"/api/projects/{project_id}/connections", body)
    if status not in {200, 201}:
        raise RuntimeError(f"release fixture connection failed with HTTP {status}: {payload!r}")
    return str(spec.get("key") or "default"), str((payload or {}).get("connection_id") or "")


#: Definition fields a workflow fixture never sends with its metadata: Core
#: authors steps, groups and the graph only through /api/workflows/<id>/changes.
_WORKFLOW_DEFINITION_FIELDS = frozenset({"phases", "_phases", "steps", "groups"})


def _prepare_workflow(
    api: Any, spec: dict[str, Any], project_id: str, bindings: dict[str, str]
) -> tuple[str, str]:
    """Create one workflow the bundle declared, and bind it to the project.

    Authoring a workflow is a registry write, and every registry write on the
    candidate is admin-only — so the definition is the bundle's and the write is
    the trusted side's. The declaration is the workflow's metadata plus
    ``changes``: the change operations that build its steps and graph. Core
    takes them the way every client authors a workflow — metadata first,
    disabled, then one change set at its revision, then ``enabled`` if the
    declaration asked for it.
    """
    key = str(spec.get("key") or "default")
    declared = _resolve_placeholders({k: v for k, v in spec.items() if k != "key"}, bindings)
    definition_fields = sorted(set(declared) & _WORKFLOW_DEFINITION_FIELDS)
    if definition_fields:
        raise RuntimeError(
            f"declared workflow '{key}' carries {', '.join(definition_fields)}; "
            "declare its steps and graph as 'changes' (change operations) instead"
        )
    changes = declared.pop("changes", None) or []
    if not isinstance(changes, list) or not changes:
        raise RuntimeError(f"declared workflow '{key}' has no 'changes' that build its steps")
    enabled = bool(declared.pop("enabled", False))
    workflow_id = str(declared.get("workflow_id") or "")
    if not workflow_id:
        raise RuntimeError(f"declared workflow '{key}' has no workflow_id")
    workflow_id = f"{workflow_id}_{uuid.uuid4().hex[:8]}"
    declared["workflow_id"] = workflow_id
    declared["enabled"] = False
    status, payload = api.request("POST", "/api/workflows", declared)
    if status not in {200, 201}:
        raise RuntimeError(f"release fixture workflow failed with HTTP {status}: {payload!r}")
    read_status, current = api.request("GET", f"/api/workflows/{workflow_id}")
    if read_status != 200 or not isinstance(current, dict):
        raise RuntimeError(f"reading fixture workflow failed with HTTP {read_status}: {current!r}")
    change_status, change_payload = api.request(
        "POST",
        f"/api/workflows/{workflow_id}/changes",
        {"expected_revision": current.get("revision"), "operations": changes},
    )
    if change_status != 200:
        raise RuntimeError(
            f"building fixture workflow '{key}' failed with HTTP {change_status}: {change_payload!r}"
        )
    if enabled:
        enable_status, enable_payload = api.request(
            "PATCH",
            f"/api/workflows/{workflow_id}",
            {"enabled": True, "expected_revision": (change_payload or {}).get("revision")},
        )
        if enable_status != 200:
            raise RuntimeError(
                f"enabling fixture workflow failed with HTTP {enable_status}: {enable_payload!r}"
            )
    bind_status, bind_payload = api.request(
        "PUT", f"/api/projects/{project_id}/workflows", {"workflow_ids": [workflow_id]}
    )
    if bind_status != 200:
        raise RuntimeError(
            f"binding fixture workflow failed with HTTP {bind_status}: {bind_payload!r}"
        )
    return key, workflow_id


def _workspace_id(api: Any) -> str:
    _, workspaces = api.request("GET", "/api/workspaces")
    rows = workspaces.get("workspaces") if isinstance(workspaces, dict) else workspaces
    row = next((item for item in rows or [] if isinstance(item, dict)), {})
    return str(row.get("workspace_id") or row.get("id") or "")


def resolve_model_route(api: Any) -> tuple[str, str, str]:
    """The provider and model a fixture project should run on.

    Named explicitly when `FS_SMOKE_MODEL_PROVIDER` and `FS_SMOKE_MODEL` are set,
    because a stand with several routes must say which one it means. When they
    are not and the candidate offers exactly one route, that is the answer — an
    operator who configured one model did not leave anything to choose between.
    Several routes and no names is ambiguous, and guessing there would run the
    checks against a model nobody picked.

    Returns `(provider, model, how)`; `how` is recorded so the run says which of
    the three happened.
    """
    provider = str(os.environ.get("FS_SMOKE_MODEL_PROVIDER") or "").strip()
    model = str(os.environ.get("FS_SMOKE_MODEL") or "").strip()
    if provider and model:
        return provider, model, "named by the run"

    status, payload = api.request("GET", "/api/model-selection/options")
    if status != 200:
        return "", "", f"model options unavailable: HTTP {status}"
    options = payload if isinstance(payload, dict) else {}
    providers = [
        str(row.get("provider_id") or "")
        for row in options.get("provider_options") or []
        if isinstance(row, dict) and str(row.get("provider_id") or "").strip()
    ]
    models_by_provider = options.get("provider_models")
    models_by_provider = models_by_provider if isinstance(models_by_provider, dict) else {}
    usable = [
        (candidate, str((rows or [{}])[0].get("model_id") or ""))
        for candidate in providers
        for rows in [models_by_provider.get(candidate) or []]
        if rows and str((rows[0] or {}).get("model_id") or "").strip()
    ]
    if not usable:
        return "", "", "the candidate has no configured model route"
    if len(usable) > 1:
        return (
            "",
            "",
            (
                f"the candidate offers {len(usable)} model routes; name one with "
                "FS_SMOKE_MODEL_PROVIDER and FS_SMOKE_MODEL"
            ),
        )
    resolved_provider, resolved_model = usable[0]
    return resolved_provider, resolved_model, "the candidate's only model route"


def _prepare_project(api: Any, suite: BundleSuite, workspace_id: str) -> str:
    """A project bound to everything the enabled extensions publish."""
    _, listing = api.request("GET", "/api/workflows?per_page=500")
    published = listing if isinstance(listing, list) else (listing or {}).get("workflows") or []
    workflow_ids = sorted(
        str(item.get("workflow_id") or "")
        for item in published
        if isinstance(item, dict) and str(item.get("workflow_id") or "").startswith("extension.")
    )
    body: dict[str, Any] = {
        "name": f"release-suite-{suite.name[:24]}-{uuid.uuid4().hex[:8]}",
        "workspace_id": workspace_id,
        "workflow_ids": workflow_ids,
    }
    provider, model, _how = resolve_model_route(api)
    if provider and model:
        body["default_provider"] = provider
        body["default_model"] = model
    status, created = api.request("POST", "/api/projects", body)
    if status != 201:
        raise RuntimeError(f"release fixture project failed with HTTP {status}: {created!r}")
    return str((created or {}).get("project_id") or "")


def remove_created_resources(api: Any, created: list[dict[str, str]]) -> list[str]:
    """Delete what a suite declared it made, and report what would not go."""
    paths = {
        "project": "/api/projects/{id}",
        "dataset": "/api/datasets/{id}",
        "connection": "/api/projects/{project}/connections/{id}",
        "query_template": "/api/datasets/query-templates/{id}",
    }
    failures: list[str] = []
    for row in created or []:
        kind = str((row or {}).get("kind") or "")
        resource_id = str((row or {}).get("id") or "")
        project = str((row or {}).get("project") or "")
        template = paths.get(kind)
        if not template or not resource_id or ("{project}" in template and not project):
            failures.append(f"{kind or '<unknown>'}:{resource_id or '<missing>'}")
            continue
        status, _ = api.request("DELETE", template.format(id=resource_id, project=project))
        if status not in {200, 202, 204, 404}:
            failures.append(f"{kind}:{resource_id}:HTTP {status}")
    return failures


def _runner_environment(
    *, base_url: str, token: str, account: str, check_ids: list[str], inputs: dict[str, str]
) -> list[str]:
    """Exactly what the container is told, and nothing that was not asked for."""
    declared = {
        "FS_RELEASE_BASE_URL": base_url,
        "FS_RELEASE_TOKEN": token,
        "FS_RELEASE_ACCOUNT": account,
        "FS_RELEASE_CHECK_IDS": ",".join(sorted(check_ids)),
    }
    for name, value in sorted(inputs.items()):
        declared[f"{INPUT_PREFIX}{name}"] = value
    flags: list[str] = []
    for name, value in declared.items():
        flags.extend(["--env", f"{name}={value}"])
    return flags


def _service_bindings(raw: str) -> list[tuple[str, str]]:
    """Parse `alias=container` pairs, ignoring anything that is not one."""
    bindings: list[tuple[str, str]] = []
    for entry in str(raw or "").split(","):
        alias, _, container = entry.partition("=")
        alias, container = alias.strip(), container.strip()
        if alias and container:
            bindings.append((alias, container))
    return bindings


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=check
    )


@contextmanager
def suite_network(
    *,
    app_container: str,
    app_alias: str,
    services: list[tuple[str, str]],
    on_missing_service: Callable[[str, str, str], None] | None = None,
) -> Iterator[str]:
    """A network holding the candidate and the declared test services, and nothing else.

    `--internal` is the point: the candidate's own Compose network carries
    Postgres, RabbitMQ, the object store and a route off the host, and a suite
    attached to it could reach every one of them. Here the only things on the
    wire are the containers connected below, and there is no egress at all.

    Torn down in `finally`, so a failed run leaves no network and no container
    attached to one.

    A declared service that is not on this host is reported through
    `on_missing_service` and left out, rather than aborting the run: the suite
    that needs it says so itself, by name, which is more use than a stack trace
    about `docker network connect`. The candidate is not optional — without it
    there is nothing to test against.
    """
    name = f"fs-release-suite-{uuid.uuid4().hex[:12]}"
    _docker("network", "create", "--internal", name)
    connected: list[str] = []
    try:
        _docker("network", "connect", "--alias", app_alias, name, app_container)
        connected.append(app_container)
        for alias, container in services:
            result = _docker("network", "connect", "--alias", alias, name, container, check=False)
            if result.returncode == 0:
                connected.append(container)
            elif on_missing_service is not None:
                on_missing_service(alias, container, (result.stderr or "").strip()[:200])
        yield name
    finally:
        for container in connected:
            _docker("network", "disconnect", "--force", name, container, check=False)
        _docker("network", "rm", name, check=False)


def run_bundle_suite(
    suite: BundleSuite,
    *,
    base_url: str,
    token: str,
    account: str,
    network: str,
    inputs: dict[str, str],
    image: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Run one suite in its own container.

    Returns the assertion rows it produced and the resources it declared it
    created, which only the trusted side can remove.
    """
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        # The bundle is input, not a workspace: it may not be written to, and the
        # harness beside it is the platform's, mounted separately.
        "--volume",
        f"{suite.path.resolve()}:/bundle:ro",
        "--volume",
        f"{HARNESS_PATH}:/harness/run.py:ro",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=16m,noexec,nosuid,nodev",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        # Not root: root in the container owns root's view of every mount, and a
        # read-only mount is the only thing then standing between a suite and the
        # bundle it was handed.
        "--user",
        RUNNER_USER,
        # A release check is not a workload; a suite that spins cannot take the
        # host with it.
        "--cpus",
        RUNNER_CPUS,
        "--memory",
        RUNNER_MEMORY,
        "--pids-limit",
        RUNNER_PIDS,
        "--workdir",
        "/harness",
        *_runner_environment(
            base_url=base_url,
            token=token,
            account=account,
            check_ids=suite.checks,
            inputs=inputs,
        ),
        image or os.environ.get(RUNNER_IMAGE_ENV) or DEFAULT_RUNNER_IMAGE,
        "python",
        "-I",
        "/harness/run.py",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    for line in reversed((result.stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        rows = payload.get("assertions") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            created = payload.get("created") if isinstance(payload, dict) else []
            return (
                [row for row in rows if isinstance(row, dict)],
                [row for row in created or [] if isinstance(row, dict)],
            )
    return (
        [
            {
                "name": f"{suite.name}_release_suite",
                "passed": False,
                "detail": (
                    f"isolated run produced no result (exit {result.returncode}): "
                    f"{(result.stderr or result.stdout or '')[-400:]}"
                ),
            }
        ],
        [],
    )


__all__ = [
    "CANDIDATE_APP_ALIAS_ENV",
    "CANDIDATE_APP_CONTAINER_ENV",
    "CANDIDATE_INTERNAL_URL_ENV",
    "CHECKS_ATTRIBUTE",
    "CHECKS_RELATIVE_PATH",
    "CONNECTIONS_ATTRIBUTE",
    "DEFAULT_RUNNER_IMAGE",
    "EXTENSIONS_ATTRIBUTE",
    "INPUTS_ATTRIBUTE",
    "INPUT_PREFIX",
    "NARROW_SUITE_SCOPES",
    "RELEASE_SERVICES_ENV",
    "SCOPES_ATTRIBUTE",
    "SERVICES_ATTRIBUTE",
    "WORKFLOWS_ATTRIBUTE",
    "BundleSuite",
    "discover_bundle_suites",
    "extensions_root",
    "mint_scoped_token",
    "prepared_fixtures",
    "remove_created_resources",
    "resolve_model_route",
    "revoke_token",
    "run_bundle_suite",
    "suite_network",
    "suites_require_model_route",
]
