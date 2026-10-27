"""A query-capable data connection must be backed by a Data Connector extension.

Regression for the workflow E2E ledger finding F30, "`dataset_query` has no
builtin execution path". The platform seeded its own PostgreSQL as a workspace
data connection with `connector_id: platform.postgres` and empty `install_id` /
`extension_id` / `extension_key` / `source_extension_id`. The dataset catalog
happily built a dataset and a saved query on it, and only at runtime — after the
job had started — did the step die with `connector_runtime_unavailable`.

`list_dataset_connectors` only ever returns connectors provided by active
extension installs, so the `platform.postgres` connector the seed fabricated does
not exist in the catalog at all. The fixtures below name no real extension: Core
knows connections only by the identities the runtime resolves through. The internal application database is platform
infrastructure; every query-capable connection resolves through an installed Data
Connector extension and its configured driver.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.registry.data_connection_binding import (
    EXTENSION_BINDING_KEYS,
    connection_extension_binding_issue,
    is_query_capable_connection,
)
from tests._workflow_definitions import linear_workflow

_EXTENSION_REF = {
    "install_id": "e8e80d3d-2904-4730-9298-675c44a77677",
    "release_id": "fd43bd78-86cd-4d2b-86c9-ebaa92a60a95",
    "connector_id": "extension.e8e80d3d-2904-4730-9298-675c44a77677.example_database",
    "extension_id": "example.data-connector",
    "local_connector_id": "example_postgres",
}

_PLATFORM_REF = {
    "install_id": "",
    "connector_id": "platform.postgres",
    "display_name": "PostgreSQL database source",
    "extension_id": "",
    "extension_key": "",
    "source_install_id": "",
    "local_connector_id": "postgres",
    "source_extension_id": "",
}


def _connection(ref: dict, **overrides) -> dict:
    payload = {
        "connection_id": "conn-under-test",
        "display_name": "Connection under test",
        "enabled": True,
        "health_status": "healthy",
        "connector_ref_jsonb": dict(ref),
    }
    payload.update(overrides)
    return payload


class _Resolver:
    """Stands in for the runtime's extension lookup, recording what it was asked."""

    def __init__(self, installs=None, extensions=None):
        self.installs = dict(installs or {})
        self.extensions = dict(extensions or {})
        self.asked: list[tuple[str, str]] = []

    def __call__(self, *, kind, value, account_id=""):
        self.asked.append((kind, value))
        source = self.installs if kind == "install_id" else self.extensions
        return source.get(value)


def test_an_extension_backed_connection_whose_install_resolves_is_query_capable():
    resolver = _Resolver(installs={_EXTENSION_REF["install_id"]: {"enabled": True}})
    connection = _connection(_EXTENSION_REF)

    assert is_query_capable_connection(connection, resolve_extension=resolver) is True
    assert connection_extension_binding_issue(connection, resolve_extension=resolver) == ""
    assert set(resolver.asked) == {("install_id", _EXTENSION_REF["install_id"])}


def test_a_binding_whose_install_does_not_resolve_is_not_query_capable():
    """A connection can name an install that was uninstalled; the runtime fails on it."""
    resolver = _Resolver(installs={})
    connection = _connection(_EXTENSION_REF)

    assert is_query_capable_connection(connection, resolve_extension=resolver) is False
    issue = connection_extension_binding_issue(connection, resolve_extension=resolver)
    assert "not installed" in issue or "no longer installed" in issue


def test_a_binding_whose_extension_is_disabled_is_not_query_capable():
    resolver = _Resolver(installs={_EXTENSION_REF["install_id"]: {"enabled": False}})
    connection = _connection(_EXTENSION_REF)

    assert is_query_capable_connection(connection, resolve_extension=resolver) is False
    assert (
        "disabled"
        in connection_extension_binding_issue(connection, resolve_extension=resolver).lower()
    )


def test_install_id_does_not_fall_back_to_the_other_identities():
    """The runtime returns early on install_id; readiness must not be more generous."""
    ref = {**_EXTENSION_REF, "install_id": "gone-install"}
    resolver = _Resolver(
        installs={}, extensions={_EXTENSION_REF["extension_id"]: {"enabled": True}}
    )

    assert is_query_capable_connection(_connection(ref), resolve_extension=resolver) is False
    assert resolver.asked == [("install_id", "gone-install")]


def test_source_install_id_alone_is_not_a_binding():
    """`source_install_id` is not one of the keys the runtime resolves through."""
    resolver = _Resolver(installs={"inst-1": {"enabled": True}})
    connection = _connection({"connector_id": "x.y", "source_install_id": "inst-1"})

    assert is_query_capable_connection(connection, resolve_extension=resolver) is False
    assert resolver.asked == []


def test_a_connection_without_an_extension_is_not_query_capable():
    """A connection no Data Connector extension backs must not read as ready to query."""
    resolver = _Resolver()
    connection = _connection(_PLATFORM_REF, display_name="Acme Warehouse Database")

    assert is_query_capable_connection(connection, resolve_extension=resolver) is False
    issue = connection_extension_binding_issue(connection, resolve_extension=resolver)
    assert issue
    assert "Data Connector extension" in issue
    assert "Acme Warehouse Database" in issue
    assert resolver.asked == []


def test_a_disabled_connection_is_not_query_capable():
    resolver = _Resolver(installs={_EXTENSION_REF["install_id"]: {"enabled": True}})
    connection = _connection(_EXTENSION_REF, enabled=False)

    assert is_query_capable_connection(connection, resolve_extension=resolver) is False
    assert (
        "disabled"
        in connection_extension_binding_issue(connection, resolve_extension=resolver).lower()
    )


@pytest.mark.parametrize("binding_key", ["extension_key", "extension_id", "source_extension_id"])
def test_the_non_install_identities_are_resolved_as_extensions(binding_key):
    resolver = _Resolver(extensions={"bound-value": {"enabled": True}})
    connection = _connection({"connector_id": "x.y", binding_key: "bound-value"})

    assert is_query_capable_connection(connection, resolve_extension=resolver) is True
    assert resolver.asked == [("extension", "bound-value")]


def test_a_stale_extension_key_falls_through_to_a_working_extension_id():
    """The runtime tries every candidate until one resolves; readiness must too.

    Checking only the first non-empty identity reported RED for a connection the
    runtime resolves and runs perfectly well.
    """
    resolver = _Resolver(extensions={"live-extension": {"enabled": True}})
    connection = _connection(
        {
            "connector_id": "x.y",
            "extension_key": "stale-key",
            "extension_id": "live-extension",
        }
    )

    assert connection_extension_binding_issue(connection, resolve_extension=resolver) == ""
    assert is_query_capable_connection(connection, resolve_extension=resolver) is True
    assert resolver.asked[:2] == [("extension", "stale-key"), ("extension", "live-extension")]


def test_the_candidate_order_matches_the_runtime():
    resolver = _Resolver(extensions={"third": {"enabled": True}})
    connection = _connection(
        {
            "connector_id": "x.y",
            "extension_key": "first",
            "extension_id": "second",
            "source_extension_id": "third",
        }
    )

    assert is_query_capable_connection(connection, resolve_extension=resolver) is True
    assert resolver.asked == [
        ("extension", "first"),
        ("extension", "second"),
        ("extension", "third"),
    ]


def test_the_first_candidate_that_resolves_wins_even_when_disabled():
    """The runtime returns the first resolved record, then fails it for being disabled."""
    resolver = _Resolver(extensions={"first": {"enabled": False}, "second": {"enabled": True}})
    connection = _connection(
        {"connector_id": "x.y", "extension_key": "first", "extension_id": "second"}
    )

    issue = connection_extension_binding_issue(connection, resolve_extension=resolver)
    assert "disabled" in issue.lower()
    assert resolver.asked == [("extension", "first")]


def test_no_candidate_resolving_still_reports_not_installed():
    resolver = _Resolver(extensions={})
    connection = _connection(
        {"connector_id": "x.y", "extension_key": "gone-a", "extension_id": "gone-b"}
    )

    issue = connection_extension_binding_issue(connection, resolve_extension=resolver)
    assert "not installed" in issue
    assert resolver.asked == [("extension", "gone-a"), ("extension", "gone-b")]


def test_a_connection_with_no_connector_ref_is_not_query_capable():
    resolver = _Resolver()
    assert (
        is_query_capable_connection(
            {"connection_id": "c", "enabled": True}, resolve_extension=resolver
        )
        is False
    )
    assert is_query_capable_connection(None, resolve_extension=resolver) is False


def test_the_binding_keys_match_what_the_runtime_resolves_through():
    """Drift here is how readiness starts reporting a GREEN the runtime cannot honour."""
    import inspect

    from core.application.orchestration import dataset_query_step_runtime as runtime

    source = inspect.getsource(runtime._extension_for_connection)
    for key in EXTENSION_BINDING_KEYS:
        assert f'ref.get("{key}")' in source, f"{key} is not read by the runtime"
    assert 'ref.get("source_install_id")' not in source
    # The runtime walks its candidates and returns on the first that resolves.
    # Readiness mirrors that; checking only the first candidate reported RED for
    # connections the runtime runs.
    assert "for candidate in" in source
    assert "if extension:" in source


# ---------------------------------------------------------------------------
# The missing binding must be found before the workflow runs
# ---------------------------------------------------------------------------


def _dataset_query_workflow(query_template_id: str = "qt-1") -> dict:
    return linear_workflow(
        {
            "step_id": "seed",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.ready", "value": True}},
        },
        {
            "step_id": "run_query",
            "step_kind": "dataset_query",
            "override": {
                "dataset_query": {
                    "query_template_id": query_template_id,
                    "params": {},
                }
            },
        },
        workflow_id="wf-dataset-query",
    )


class _DatasetQueryDB:
    """The readiness path resolves the extension exactly as the runtime does."""

    def __init__(
        self,
        connector_ref: dict,
        *,
        enabled: bool = True,
        health_status: str = "healthy",
        install_enabled: bool | None = True,
        project_scoped: bool = False,
        projection_active: bool = True,
    ):
        self._workflow = _dataset_query_workflow()
        self._connector_ref = connector_ref
        self._enabled = enabled
        self._health_status = health_status
        self._install_enabled = install_enabled
        self._project_scoped = project_scoped
        self._projection_active = projection_active
        self._projection_install_id = _EXTENSION_REF["install_id"]
        self._projection_release_id = _EXTENSION_REF["release_id"]
        self._projection_artifact_id = "ds-1"
        self._dataset_id = (
            projection_bridge_id(self._projection_install_id, self._projection_artifact_id)
            if project_scoped
            else "ds-1"
        )
        self.dsn = "postgresql://readiness-stub"

    def get_extension_by_install_id(self, install_id, *_args, **_kwargs):
        if self._install_enabled is None:
            return None
        return {"install_id": install_id, "enabled": self._install_enabled}

    def get_workflow(self, workflow_id: str):
        if str(self._workflow.get("workflow_id")) != str(workflow_id or ""):
            return None
        return self._workflow

    def get_dataset_query_template(self, query_template_id: str):
        if query_template_id != "qt-1":
            return None
        return {
            "query_template_id": "qt-1",
            "dataset_id": self._dataset_id,
            "params_schema_jsonb": {"type": "object", "properties": {}},
        }

    def get_dataset(self, dataset_id: str, include_trashed: bool = False):
        if dataset_id != self._dataset_id:
            return None
        if self._project_scoped:
            return {
                "dataset_id": self._dataset_id,
                "connector_id": self._connector_ref["connector_id"],
                "metadata_jsonb": {
                    "connection_resolution": "project_scope",
                    "projection_bridge": {
                        "immutable": True,
                        "install_id": self._projection_install_id,
                        "release_id": self._projection_release_id,
                        "artifact_kind": "dataset",
                        "artifact_id": self._projection_artifact_id,
                    },
                },
            }
        return {"dataset_id": "ds-1", "connection_id": "conn-1"}

    def get_extension_registry_projection(self, **kwargs):
        if not self._project_scoped:
            return None
        expected = {
            "account_id": "account-1",
            "install_id": self._projection_install_id,
            "artifact_kind": "dataset",
            "artifact_id": self._projection_artifact_id,
        }
        if any(str(kwargs.get(key) or "") != value for key, value in expected.items()):
            return None
        return {
            **expected,
            "release_id": self._projection_release_id,
            "is_active": self._projection_active,
            "availability_status": "available",
            "new_runs_blocked": False,
            "projection": {"dataset_id": self._projection_artifact_id},
        }

    def get_workspace_data_connection(self, connection_id: str, include_trashed: bool = False):
        if connection_id != "conn-1":
            return None
        return {
            "connection_id": "conn-1",
            "display_name": "Acme Warehouse Database",
            "enabled": self._enabled,
            "health_status": self._health_status,
            "connector_ref_jsonb": dict(self._connector_ref),
        }

    def list_project_workflows(self, *, project_id=None, enabled_only=True):
        assert enabled_only is True
        rows = [{"project_id": "project-1", "workflow_id": "wf-dataset-query"}]
        return [row for row in rows if project_id is None or row["project_id"] == project_id]

    def get_project(self, project_id: str):
        if project_id != "project-1":
            return None
        return {
            "id": "project-1",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "workspace_id": "workspace-1",
        }

    def list_workspace_data_connections(self, *, workspace_id=None):
        if not self._project_scoped:
            return []
        connection = {
            "connection_id": "conn-1",
            "display_name": "Project-scoped connection",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "workspace_id": "workspace-1",
            "enabled": self._enabled,
            "health_status": self._health_status,
            "connector_ref_jsonb": dict(self._connector_ref),
        }
        if workspace_id and workspace_id != connection["workspace_id"]:
            return []
        return [connection]


def _readiness(db):
    from core.application.registry.contract_validation import validate_workflow_readiness

    def _resolver(_db):
        def _resolve(*, kind, value, account_id=""):
            return db.get_extension_by_install_id(value) if kind == "install_id" else None

        return _resolve

    with patch(
        "core.application.registry.contract_validation_workflow_readiness"
        ".runtime_extension_resolver",
        _resolver,
    ):
        return validate_workflow_readiness(db, "wf-dataset-query")


def _codes(validation: dict) -> set[str]:
    return {
        str(item.get("code") or "").strip()
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    }


def test_a_dataset_query_on_an_unbound_connection_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_PLATFORM_REF))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)
    message = " ".join(
        str(item.get("message") or "")
        for item in validation["errors"]
        if item.get("code") == "workflow.step_dataset_query_connection_unbound"
    )
    assert "Data Connector extension" in message
    assert "Step 'run_query'" in message
    assert {
        item["path"]
        for item in validation["errors"]
        if item.get("code") == "workflow.step_dataset_query_connection_unbound"
    } == {"workflow.steps.run_query.override.dataset_query.query_template_id"}


def test_a_dataset_query_on_an_extension_backed_connection_is_accepted():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF))

    assert "workflow.step_dataset_query_connection_unbound" not in _codes(validation)


def test_a_dataset_query_on_a_disabled_connection_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, enabled=False))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_a_dataset_query_on_an_unhealthy_connection_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, health_status="unhealthy"))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_a_dataset_query_whose_install_is_gone_is_rejected_before_the_run():
    """A false GREEN here is a job that dies on connector_runtime_unavailable."""
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, install_enabled=None))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_a_dataset_query_whose_install_is_disabled_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, install_enabled=False))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_project_scoped_dataset_on_platform_connection_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_PLATFORM_REF, project_scoped=True))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_project_scoped_dataset_on_extension_connection_is_accepted():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, project_scoped=True))

    assert "workflow.step_dataset_query_connection_unbound" not in _codes(validation)


def test_plain_dataset_cannot_opt_into_project_connection_resolution_with_metadata():
    db = _DatasetQueryDB(_EXTENSION_REF, project_scoped=True)
    db._dataset_id = "ordinary-dataset"
    db.get_extension_registry_projection = lambda **_kwargs: None

    validation = _readiness(db)

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_dataset_without_a_data_connection_binding_is_rejected_before_the_run():
    db = _DatasetQueryDB(_EXTENSION_REF)
    db.get_dataset = lambda _dataset_id: {"dataset_id": "ds-1"}

    validation = _readiness(db)

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_stale_projected_dataset_requires_rebind_before_the_run():
    validation = _readiness(
        _DatasetQueryDB(
            _EXTENSION_REF,
            project_scoped=True,
            projection_active=False,
        )
    )

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_projected_dataset_with_a_stale_release_requires_rebind_before_the_run():
    db = _DatasetQueryDB(_EXTENSION_REF, project_scoped=True)
    original_get_projection = db.get_extension_registry_projection

    def _stale_release(**kwargs):
        return {**original_get_projection(**kwargs), "release_id": "replaced-release"}

    db.get_extension_registry_projection = _stale_release

    validation = _readiness(db)

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


def test_project_scoped_dataset_on_disabled_connection_is_rejected_before_the_run():
    validation = _readiness(_DatasetQueryDB(_EXTENSION_REF, enabled=False, project_scoped=True))

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)


@pytest.mark.parametrize("install_enabled", [None, False])
def test_project_scoped_dataset_with_unavailable_install_is_rejected_before_the_run(
    install_enabled,
):
    validation = _readiness(
        _DatasetQueryDB(
            _EXTENSION_REF,
            install_enabled=install_enabled,
            project_scoped=True,
        )
    )

    assert "workflow.step_dataset_query_connection_unbound" in _codes(validation)
