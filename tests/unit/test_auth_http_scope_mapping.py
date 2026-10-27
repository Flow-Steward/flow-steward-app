import pytest

from core.application.auth.auth_http_runtime import _required_http_scopes


def _scopes(path: str, method: str) -> list[str]:
    return _required_http_scopes(
        path,
        method,
        admin_required=False,
        project_callback_write=False,
        registry_read=False,
        registry_write=False,
    )


def test_project_callback_writes_have_project_scopes_without_widening_near_misses() -> None:
    assert _required_http_scopes(
        "/api/extensions/fixture.extension/callbacks/updates",
        "POST",
        admin_required=False,
        project_callback_write=True,
        registry_read=True,
        registry_write=True,
    ) == ["projects.write", "projects:*", "command:*", "admin:all"]
    assert _required_http_scopes(
        "/api/extensions/fixture.extension/callbacks/updates/delete",
        "POST",
        admin_required=False,
        project_callback_write=False,
        registry_read=True,
        registry_write=True,
    ) == ["admin:all"]


def test_job_report_notification_and_task_routes_match_transport_scopes() -> None:
    assert _scopes("/api/jobs/job-1/cancel", "POST") == [
        "jobs.write",
        "jobs:*",
        "command:*",
        "admin:all",
    ]
    assert _scopes("/api/reports/report-1", "GET") == [
        "reports.read",
        "reports:*",
        "read:*",
        "admin:all",
    ]
    assert _scopes("/api/projects/p1/notification-routes", "GET") == [
        "notify.read",
        "notify:*",
        "read:*",
        "admin:all",
    ]
    # The read-only overview is a notification read like the routes it lists.
    assert _scopes("/api/projects/p1/notification-overview", "GET") == [
        "notify.read",
        "notify:*",
        "read:*",
        "admin:all",
    ]
    assert "jobs.write" not in _scopes("/api/workflow-runs", "POST")
    assert "workflows.write" in _scopes("/api/workflow-runs", "POST")


_PROJECT_WRITE = ["projects.write", "projects:*", "command:*", "admin:all"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/projects"),
        ("PUT", "/api/projects/p1"),
        ("PUT", "/api/projects/p1/workflows"),
        ("PUT", "/api/projects/p1/policy"),
    ],
)
def test_a_projects_own_configuration_is_written_with_projects_write(method, path) -> None:
    """T1: the UI offers projects.write, and creating a project fell through to command:*,
    which no UI-created token can hold."""
    assert _scopes(path, method) == _PROJECT_WRITE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/projects/p1/connections"),
        ("PUT", "/api/projects/p1/connections/c1"),
        ("POST", "/api/projects/p1/connections/c1/secrets"),
        ("POST", "/api/projects/p1/move-workspace"),
        ("POST", "/api/projects/p1/scheduled-jobs"),
        ("PATCH", "/api/projects/p1/schedule-timezone"),
        ("PUT", "/api/projects/p1/extension-state/e/n/k"),
    ],
)
def test_what_is_not_the_projects_configuration_still_needs_command_scope(method, path) -> None:
    assert _scopes(path, method) == ["command:*"]


def test_deleting_a_project_stays_an_administrators() -> None:
    assert _scopes("/api/projects/p1", "DELETE") == ["admin:all"]


@pytest.mark.parametrize(
    ("path", "scopes"),
    [
        ("/api/v2/jobs/j1", ["jobs.read", "jobs:*", "read:*", "admin:all"]),
        ("/api/v2/projects/p1/jobs", ["jobs.read", "jobs:*", "read:*", "admin:all"]),
        ("/api/events", ["jobs.read", "jobs:*", "read:*", "admin:all"]),
        # AC19's sampler reads it with a UI-created token
        ("/api/workload", ["jobs.read", "jobs:*", "read:*", "admin:all"]),
        ("/api/v2/projects", ["projects.read", "projects:*", "read:*", "admin:all"]),
        ("/api/changelog/stats", ["registry.read", "registry:*", "read:*", "admin:all"]),
        ("/api/security/findings", ["read:*", "admin:all"]),
        ("/api/search", ["read:*", "admin:all"]),
        ("/api/auth/me", []),
        ("/api/auth/introspect", []),
    ],
)
def test_every_read_belongs_to_a_scope_family(path, scopes) -> None:
    """Review (P1): a PAT refused /api/jobs/{id} read the same job through /api/v2."""
    assert _scopes(path, "GET") == scopes


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        (
            "GET",
            "/api/changelog/tool/owned/history",
            ["registry.read", "registry:*", "read:*", "admin:all"],
        ),
        (
            "HEAD",
            "/api/changelog/tool/owned/history",
            ["registry.read", "registry:*", "read:*", "admin:all"],
        ),
        (
            "OPTIONS",
            "/api/changelog/tool/owned/history",
            ["registry.read", "registry:*", "read:*", "admin:all"],
        ),
        ("POST", "/api/changelog/tool/owned/restore", ["command:*"]),
        ("DELETE", "/api/changelog/tool/owned/restore", ["admin:all"]),
    ],
)
def test_changelog_role_classification_preserves_legacy_token_scope_contract(
    method: str, path: str, expected: list[str]
) -> None:
    from core.infrastructure.runtime import http_app_auth_runtime

    registry_route = http_app_auth_runtime._is_registry_read_route(path)
    assert (
        _required_http_scopes(
            path,
            method,
            admin_required=http_app_auth_runtime._is_admin_only_route(path) or method == "DELETE",
            project_callback_write=False,
            registry_read=registry_route,
            registry_write=registry_route and method not in {"GET", "HEAD", "OPTIONS"},
        )
        == expected
    )


_ROUTE_TABLE = """
import json
from core.entrypoints.api import app_factory
from core.entrypoints.api.app import app
from core.infrastructure.runtime.http_app_auth_runtime import (
    _is_admin_only_route, _is_registry_read_route,
)
rows = []
for route in app.routes:
    path = getattr(route, "path", None)
    for method in sorted(getattr(route, "methods", None) or ()):
        if path and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            sample = path.replace("{", "").replace("}", "")
            rows.append({
                "method": method,
                "path": path,
                "sample": sample,
                "public": bool(app_factory._is_public_auth_path(app, sample)),
                "admin": bool(_is_admin_only_route(sample)),
                "registry": bool(_is_registry_read_route(sample)),
            })
print(json.dumps(rows))
"""


def test_no_route_of_the_application_is_open_to_every_token() -> None:
    """Derived from the application's own route table: a token needs a scope for every
    route that is neither public (health, sign-in, static, the shell, capability-token
    callbacks) nor a principal's read about itself."""
    import json
    import os
    import subprocess
    import sys

    from core.application.auth.auth_http_runtime import (
        _SELF_SERVICE_READ_PATHS,
        _is_project_extension_action_write,
        _is_project_extension_callback_write,
    )

    environment = dict(os.environ, CONTROL_LOOPS_ENABLED="0", FS_MEMORY_VECTOR_BACKEND="null")
    listing = subprocess.run(
        [sys.executable, "-c", _ROUTE_TABLE],
        capture_output=True,
        text=True,
        timeout=180,
        env=environment,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr[-2000:]
    rows = json.loads(listing.stdout.strip().splitlines()[-1])
    assert len(rows) > 500  # non-vacuity: the real application, not a stub

    open_to_every_token = []
    for row in rows:
        if row["public"] and row["method"] == "GET":
            continue
        method, sample = row["method"], row["sample"]
        scopes = _required_http_scopes(
            sample,
            method,
            admin_required=row["admin"] or method == "DELETE",
            project_callback_write=_is_project_extension_callback_write(sample, method),
            project_action_write=_is_project_extension_action_write(sample, method),
            registry_read=row["registry"],
            registry_write=row["registry"] and method != "GET",
        )
        self_read = row["path"] in _SELF_SERVICE_READ_PATHS or row["path"].startswith(
            "/api/users/me/"
        )
        if not scopes and not self_read:
            open_to_every_token.append(f"{method} {row['path']}")

    assert open_to_every_token == []
    routes = {row["path"] for row in rows}
    assert routes >= _SELF_SERVICE_READ_PATHS  # no dead exemption


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/workflow-authoring/schema"),
        ("GET", "/api/projects/p1/workflow-authoring/capabilities"),
        ("GET", "/api/projects/p1/workflow-authoring/capability"),
        ("GET", "/api/projects/p1/workflow-authoring/examples"),
        ("POST", "/api/projects/p1/workflow-authoring/validate"),
    ],
)
def test_exact_authoring_reads_use_existing_workflow_read_authority(method, path):
    assert _scopes(path, method) == [
        "workflows.read",
        "workflows:*",
        "registry.read",
        "registry:*",
        "read:*",
        "admin:all",
    ]


def test_authoring_draft_creation_requires_current_admin_authority():
    assert _scopes("/api/projects/p1/workflow-authoring/drafts", "POST") == ["admin:all"]


def test_project_workflow_import_requires_the_draft_creation_authority():
    assert _scopes("/api/projects/p1/workflow-imports", "POST") == ["admin:all"]
    assert _scopes("/api/projects/p1/workflow-imports/extra", "POST") == ["command:*"]


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/projects/p1/workflow-authoring/validate/extra", ["command:*"]),
        ("PUT", "/api/projects/p1/workflow-authoring/validate", ["command:*"]),
        (
            "GET",
            "/api/projects/p1/workflow-authoring/drafts",
            ["projects.read", "projects:*", "read:*", "admin:all"],
        ),
        ("POST", "/api/workflow-authoring/schema", ["command:*"]),
    ],
)
def test_authoring_read_classification_does_not_widen_near_misses(method, path, expected):
    assert _scopes(path, method) == expected
