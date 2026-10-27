"""Where-used labels resolve active projections without widening read authority."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.application.management.command_specs.workflows import WorkflowWhereUsedResult
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.management.workflow_operator_service import WorkflowOperatorService

ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
OTHER_ACCOUNT = "00000000-0000-0000-0000-0000000000a2"
PROJECT = "project-invoices"
OTHER_PROJECT = "project-private"
INSTALL = "11111111-1111-4111-8111-111111111111"
EXTENSION = "acme.invoice_showcase"
HOOK_STEP = "adjust_order_stage"
PRESENTATION_FIELDS = (
    "caller_extension_id",
    "caller_extension_name",
    "caller_extension_version",
    "hook_display_name",
    "step_display_name",
)
CALLER = f"extension.{INSTALL}.prepare_invoice"
TARGET = "enrich_invoice"


class _ConsumerCursor:
    """Only the persistence boundary is replaced; the reverse query stays real."""

    def __init__(self, hook_rows: list[dict], invoke_rows: list[dict]) -> None:
        self.hook_rows = hook_rows
        self.invoke_rows = invoke_rows
        self.rows: list[dict] = []

    def execute(self, sql: str, _params: tuple) -> None:
        if "FROM workflow_runtime_dependencies" in sql:
            self.rows = self.invoke_rows
        elif "FROM registry_workflow_hook_bindings" in sql:
            self.rows = self.hook_rows
        else:
            raise AssertionError(f"Unexpected persistence query: {sql}")

    def fetchall(self) -> list[dict]:
        return self.rows


class _ReadStore:
    def __init__(self, rows: dict[str, dict], projections: dict[str, list[dict]]) -> None:
        self.rows = rows
        self.projections = projections
        self.release_reads: list[str] = []
        self.releases = {}
        self.hook_rows = [
            {
                "caller_workflow_id": CALLER,
                "enabled": True,
                "project_id": PROJECT,
                "hook_id": "adjust_invoice",
                "source_install_id": INSTALL,
                "local_workflow_id": "prepare_invoice",
                "revision": 1,
            }
        ]
        self.invoke_rows: list[dict] = []
        self.extension_runtime_store = SimpleNamespace(
            list_projections=lambda *, account_id, only_active: projections.get(account_id, []),
            get_release=self.get_release,
        )

    def get_release(self, release_id: str):
        self.release_reads.append(release_id)
        return self.releases.get(release_id)

    def get_workflow(self, workflow_id: str) -> dict | None:
        row = self.rows.get(workflow_id)
        return dict(row) if row else None

    @contextmanager
    def _transaction(self):
        yield _ConsumerCursor(self.hook_rows, self.invoke_rows)


@pytest.fixture
def named_usage(monkeypatch):
    # Readiness is an unrelated external store; these projections have no dependencies.
    monkeypatch.setattr(
        "core.application.extensions.service_support.get_default_extension_install_store",
        lambda **_kwargs: SimpleNamespace(list_for_account=lambda *_args, **_options: []),
    )
    projected = {
        "artifact_kind": "workflow",
        "artifact_id": "prepare_invoice",
        "install_id": INSTALL,
        "release_id": "pr_showcase",
        "projection": {
            "workflow_id": "prepare_invoice",
            "display_name": "Prepare invoice — Showcase",
            "enabled": True,
            "steps": [
                {
                    "step_id": HOOK_STEP,
                    "display_name": "Order adjustment stage",
                    "step_kind": "invoke_workflow",
                    "override": {
                        "invoke_workflow": {
                            "hook": {
                                "hook_id": "adjust_invoice",
                                "display_name": "Adjust order",
                                "contract_version": 1,
                                "input_schema": {"type": "object", "properties": {}},
                                "input_mapping": {},
                                "stage_contract": {
                                    "summary": "Adjust the order before invoicing",
                                    "effects_before": [],
                                    "effects_after": ["invoice.create"],
                                },
                                "mutable_fields": [],
                                "additional_outputs": {},
                            }
                        }
                    },
                }
            ],
        },
    }
    store = _ReadStore(
        {
            TARGET: {
                "workflow_id": TARGET,
                "account_id": ACCOUNT,
                "display_name": "Enrich invoice",
            },
            CALLER: {
                "workflow_id": CALLER,
                "account_id": ACCOUNT,
                "projection_mode": "derived",
                "source_install_id": INSTALL,
                "display_name": "Stale anchor label",
            },
        },
        {ACCOUNT: [projected]},
    )

    store.releases["pr_showcase"] = SimpleNamespace(
        release_id="pr_showcase",
        extension_id=EXTENSION,
        version="2.1.0",
        descriptor={
            "identity": {"display_name": "Invoice Showcase"},
            "locale_bundles": {
                "en": {"ext.display_name": "Invoice Showcase"},
                "lt": {"ext.display_name": "Sąskaitų pavyzdžiai"},
            },
        },
    )

    def bindings(project_id, *, scope):
        if project_id != PROJECT or scope.account_id != ACCOUNT:
            raise ServiceNotFoundError("Project not found")
        return SimpleNamespace(project_workflows=[{"workflow_id": TARGET}, {"workflow_id": CALLER}])

    service = WorkflowOperatorService(
        get_logger=lambda: store,
        registry_db_provider=lambda: store,
        project_query_service=lambda: SimpleNamespace(get_workflow_bindings=bindings),
    )
    return service, store


def _where_used(service, **scope_fields) -> dict:
    result = service.where_used(
        project_id=PROJECT,
        workflow_id=TARGET,
        scope=ScopeContext(account_id=ACCOUNT, **scope_fields),
    )
    # The API/MCP/CLI result model must preserve the same optional labels.
    return WorkflowWhereUsedResult.model_validate(result).model_dump()


def test_where_used_names_protected_extension_callers_from_active_projection(named_usage) -> None:
    service, _store = named_usage

    result = _where_used(service)

    assert result["consumers"][0]["caller_workflow_id"] == CALLER
    assert result["consumers"][0].get("caller_display_name") == "Prepare invoice — Showcase"
    assert result.get("workflow_display_name") == "Enrich invoice"


def test_where_used_includes_release_extension_and_declared_hook_labels(named_usage) -> None:
    service, store = named_usage

    consumer = _where_used(service)["consumers"][0]

    assert consumer["caller_workflow_id"] == CALLER
    assert consumer["source_install_id"] == INSTALL
    assert consumer["local_workflow_id"] == "prepare_invoice"
    assert consumer["hook_id"] == "adjust_invoice"
    assert consumer["step_id"] == HOOK_STEP
    assert consumer["binding_revision"] == 1
    assert consumer.get("caller_display_name") == "Prepare invoice — Showcase"
    assert consumer.get("caller_extension_id") == EXTENSION
    assert consumer.get("caller_extension_name") == "Invoice Showcase"
    assert consumer.get("caller_extension_version") == "2.1.0"
    assert consumer.get("hook_display_name") == "Adjust order"
    assert consumer.get("step_display_name") == "Order adjustment stage"
    assert set(store.release_reads) == {"pr_showcase"}


def test_where_used_names_personal_invoke_steps_without_inventing_an_extension(named_usage) -> None:
    service, store = named_usage
    store.hook_rows = []
    store.invoke_rows = [
        {"workflow_id": "monthly_report", "step_id": "invoke_92ab", "enabled": True}
    ]
    store.rows["monthly_report"] = {
        "workflow_id": "monthly_report",
        "account_id": ACCOUNT,
        "display_name": "Monthly report",
        "steps": [
            {
                "step_id": "invoke_92ab",
                "display_name": "Enrich customer details",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": TARGET}},
            }
        ],
    }

    consumer = _where_used(service)["consumers"][0]

    assert consumer["caller_workflow_id"] == "monthly_report"
    assert consumer["step_id"] == "invoke_92ab"
    assert consumer.get("caller_display_name") == "Monthly report"
    assert consumer.get("step_display_name") == "Enrich customer details"
    assert consumer.get("caller_extension_name") is None
    assert consumer.get("caller_extension_id") is None


def test_where_used_resolves_projected_target_name(named_usage) -> None:
    service, store = named_usage
    store.rows[TARGET]["projection_mode"] = "derived"
    store.rows[TARGET]["display_name"] = "Stale child anchor"
    store.extension_runtime_store = SimpleNamespace(
        list_projections=lambda *, account_id, only_active: (
            [
                {
                    "artifact_kind": "workflow",
                    "artifact_id": "enrich_invoice",
                    "install_id": INSTALL,
                    "projection": {
                        "workflow_id": "enrich_invoice",
                        "display_name": "Shared enrichment",
                    },
                }
            ]
            if account_id == ACCOUNT
            else []
        )
    )
    projected_id = f"extension.{INSTALL}.enrich_invoice"
    store.rows[projected_id] = {**store.rows[TARGET], "workflow_id": projected_id}
    # Use the public scoped operator with this target linked in the current project.
    service._project_query_service = lambda: SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope: SimpleNamespace(
            project_workflows=[{"workflow_id": projected_id}]
        )
    )

    result = service.where_used(
        project_id=PROJECT, workflow_id=projected_id, scope=ScopeContext(account_id=ACCOUNT)
    )

    assert result["workflow_id"] == projected_id
    assert result.get("workflow_display_name") == "Shared enrichment"


def test_where_used_keeps_identity_when_projection_is_unavailable(named_usage) -> None:
    service, store = named_usage
    store.extension_runtime_store = SimpleNamespace(
        list_projections=lambda *, account_id, only_active: []
    )

    consumer = _where_used(service)["consumers"][0]

    assert consumer["caller_workflow_id"] == CALLER
    assert consumer.get("caller_display_name") is None
    assert consumer["hook_id"] == "adjust_invoice"
    assert all(consumer.get(field) is None for field in PRESENTATION_FIELDS)


def test_where_used_never_labels_consumers_from_a_foreign_account_row(named_usage) -> None:
    service, store = named_usage
    store.rows[CALLER] = {
        "workflow_id": CALLER,
        "account_id": OTHER_ACCOUNT,
        "display_name": "Private customer workflow",
    }
    store.rows[TARGET]["account_id"] = OTHER_ACCOUNT
    store.rows[TARGET]["display_name"] = "Private target"

    result = _where_used(service)

    assert result["consumers"][0].get("caller_display_name") is None
    assert all(result["consumers"][0].get(field) is None for field in PRESENTATION_FIELDS)
    assert result.get("workflow_display_name") is None


def test_where_used_does_not_label_callers_outside_workflow_scope(named_usage) -> None:
    service, _store = named_usage

    result = _where_used(service, allowed_workflow_ids=frozenset({TARGET}))

    assert result["consumers"][0]["caller_workflow_id"] == CALLER
    assert result["consumers"][0].get("caller_display_name") is None
    assert all(result["consumers"][0].get(field) is None for field in PRESENTATION_FIELDS)
    assert result.get("workflow_display_name") == "Enrich invoice"


def test_where_used_does_not_label_hook_callers_in_inaccessible_projects(named_usage) -> None:
    service, store = named_usage
    store.hook_rows[0]["project_id"] = OTHER_PROJECT

    result = _where_used(service, allowed_project_ids=frozenset({PROJECT}))

    assert result["consumers"][0]["project_id"] == OTHER_PROJECT
    assert result["consumers"][0].get("caller_display_name") is None
    assert all(result["consumers"][0].get(field) is None for field in PRESENTATION_FIELDS)


def test_where_used_still_requires_target_project_access(named_usage) -> None:
    service, _store = named_usage
    scope = ScopeContext(account_id=ACCOUNT)
    allowed_workflow_ids = frozenset({CALLER})

    with pytest.raises(ServiceNotFoundError, match="Project not found"):
        service.where_used(
            project_id=OTHER_PROJECT,
            workflow_id=TARGET,
            scope=scope,
        )

    with pytest.raises(ServiceNotFoundError, match="linked to project"):
        _where_used(service, allowed_workflow_ids=allowed_workflow_ids)


@pytest.mark.parametrize(
    ("language_fields", "expected_name"),
    [({}, "Invoice Showcase"), ({"locale": "lt"}, "Sąskaitų pavyzdžiai")],
)
def test_where_used_cli_binding_preserves_locale_and_omitted_locale(
    named_usage, monkeypatch, language_fields: dict, expected_name: str
) -> None:
    from core.entrypoints.cli.platform.service_bindings import workflows as bindings

    service, _store = named_usage

    def operator_scope(*, project_id, workflow_id):
        assert project_id == PROJECT
        assert workflow_id == TARGET
        return ScopeContext(account_id=ACCOUNT)

    # Only the process's operator authority is replaced; validator, adapter and query stay real.
    monkeypatch.setattr(bindings, "cli_operator_scope", operator_scope)
    result = asyncio.run(
        bindings.workflows_where_used(
            SimpleNamespace(workflow_operator_service=service),
            {"project_id": PROJECT, "workflow_id": TARGET, **language_fields},
        )
    )

    assert result["project_id"] == PROJECT
    assert result["workflow_id"] == TARGET
    consumer = result["consumers"][0]
    assert consumer["caller_workflow_id"] == CALLER
    assert consumer["hook_id"] == "adjust_invoice"
    assert consumer["step_id"] == HOOK_STEP
    assert consumer["caller_extension_name"] == expected_name


def test_where_used_names_projection_after_canonical_project_binding_anchor_creation(named_usage):
    from core.application.management.command_services import ProjectCommandService
    from core.application.registry.db_store_common import _system_registry_account_id
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    service, store = named_usage
    del store.rows[CALLER]

    def persist_anchor(payload, **_options):
        # SQL persistence is the only boundary replaced: use its actual account
        # conversion, so a source-install anchor is not silently made account-owned.
        store.rows[payload["workflow_id"]] = {
            **payload,
            "account_id": _system_registry_account_id(payload),
            "revision": 1,
        }

    registry = SimpleNamespace(get_workflow=store.get_workflow, save_workflow=persist_anchor)
    bl = SimpleNamespace(db_store=registry)
    commands = ProjectCommandService(
        get_logger=lambda: bl,
        list_workflow_choices=lambda **options: projected_artifact_rows(
            store, "workflow", "workflow_id", account_id=options["account_id"]
        ),
    )
    commands._ensure_extension_workflow_anchors(
        bl=bl,
        workflow_ids=[CALLER],
        account_id=ACCOUNT,
        actor_id="metadata-test",
        transaction_conn=None,
    )

    consumer = _where_used(service)["consumers"][0]

    assert consumer["caller_workflow_id"] == CALLER
    assert consumer["source_install_id"] == INSTALL
    assert consumer["caller_display_name"] == "Prepare invoice — Showcase"
    assert consumer["caller_extension_name"] == "Invoice Showcase"
    assert consumer["caller_extension_version"] == "2.1.0"
    assert consumer["hook_display_name"] == "Adjust order"
    assert consumer["step_display_name"] == "Order adjustment stage"
    assert consumer["step_id"] == HOOK_STEP
    assert set(store.release_reads) == {"pr_showcase"}


def test_where_used_still_denies_a_foreign_derived_anchor_even_with_local_projection(named_usage):
    service, store = named_usage
    # This remains a real foreign account row, not a global identity anchor.
    store.rows[CALLER]["account_id"] = OTHER_ACCOUNT

    consumer = _where_used(service)["consumers"][0]

    assert consumer["caller_workflow_id"] == CALLER
    assert consumer.get("caller_display_name") is None
    assert all(consumer.get(field) is None for field in PRESENTATION_FIELDS)
    assert store.release_reads == []


def test_where_used_does_not_treat_an_unowned_manual_row_as_a_projection_anchor(named_usage):
    service, store = named_usage
    store.rows[CALLER].update(account_id=None, projection_mode="manual", source_install_id="")

    consumer = _where_used(service)["consumers"][0]

    assert consumer.get("caller_display_name") is None
    assert all(consumer.get(field) is None for field in PRESENTATION_FIELDS)
    assert store.release_reads == []


def test_where_used_does_not_name_an_inactive_global_derived_anchor(named_usage):
    service, store = named_usage
    store.rows[CALLER]["account_id"] = None
    store.extension_runtime_store = SimpleNamespace(
        list_projections=lambda *, account_id, only_active: []
    )

    consumer = _where_used(service)["consumers"][0]

    assert consumer.get("caller_display_name") is None
    assert all(consumer.get(field) is None for field in PRESENTATION_FIELDS)
    assert store.release_reads == []
