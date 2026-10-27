"""Structural guardrails for the first public CLI command registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.auth.user_admin import (
    UserActiveUpdateInput,
    UserCreateInput,
    UserListView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.application.extensions.scaffold import (
    ExtensionScaffoldCreateInput,
    ExtensionScaffoldResult,
)
from core.application.management.command_models import (
    ApprovalResumeAcceptedView,
    ExtensionArchiveInstallResultView,
    ExtensionArchiveUninstallResultView,
    ExtensionEnableResultView,
    JobLifecycleResultView,
    ProjectNotificationDispatchResultView,
)
from core.application.management.command_specs.contracts import (
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.database import (
    DatabaseBootstrapSeedRequest,
    DatabaseGenerateMigrationRequest,
    DatabaseOperationResult,
    DatabaseQueryDiagnosticsRequest,
    DatabaseRestoreParityCaptureRequest,
    DatabaseRestoreParityVerifyRequest,
    DatabaseStatusRequest,
    DatabaseTuneEnvRequest,
    DatabaseUpgradeRequest,
)
from core.application.management.command_specs.extensions import (
    ExtensionInstallCommandInput,
    ExtensionShowCommandInput,
    ExtensionsListCommandInput,
)
from core.application.management.command_specs.jobs import (
    JobAcknowledgeCommandInput,
    JobApprovalCommandInput,
    JobCancelCommandInput,
    JobErrorsCommandInput,
    JobHealthSnapshotCommandInput,
    JobHealthSnapshotView,
    JobIdCommandInput,
    JobLogsCommandInput,
    JobMessagesCommandInput,
    JobShowCommandInput,
    JobsListCommandInput,
)
from core.application.management.command_specs.localization import (
    LocalizationBootstrapCatalogRequest,
    LocalizationCompileRequest,
    LocalizationInitLocaleRequest,
    LocalizationRebuildPotRequest,
    LocalizationUpdateRequest,
    LocalizationValidateKeysRequest,
)
from core.application.management.command_specs.quality import (
    QualityCheckCiExcludesRequest,
    QualityCheckNamingRequest,
    QualityLintRequest,
    QualityReleaseGateRequest,
    QualitySonarEnsureRulesRequest,
    QualitySonarExportIssuesRequest,
    QualitySonarGenerateBacklogRequest,
)
from core.application.management.command_specs.reports import (
    ReportGenerateCommandInput,
    ReportGetCommandInput,
    ReportResultView,
)
from core.application.management.command_specs.testing import (
    TestingCutoverPreflightRequest as CutoverPreflightRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingCutoverSmokeRequest as CutoverSmokeRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingFuzzApiRequest as FuzzApiRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingFuzzMcpRequest as FuzzMcpRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingFuzzUiRequest as FuzzUiRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingNightfireRegressionRequest as NightfireRegressionRequestModel,
)
from core.application.management.command_specs.testing import (
    TestingSmokeTestRequest as SmokeTestRequestModel,
)
from core.application.management.query_models_admin import (
    JobsListView,
    ProjectNotificationDeliveriesView,
    ProjectNotificationRoutesView,
)
from core.application.management.query_models_jobs import (
    JobDetailContractView,
    JobErrorsView,
    JobLogsView,
    JobMessagesView,
    JobStatusView,
)
from core.application.management.query_models_project_pages import (
    ExtensionHealthView,
    ProjectCliDetailView,
)
from core.application.management.query_models_registry import (
    ExtensionDetailView,
    ExtensionListView,
)
from core.application.management.query_models_workflows import (
    ProjectsListView,
    ProjectWorkflowCatalogView,
    ProjectWorkflowEntrypointsView,
    WorkflowContractResultView,
    WorkflowSuggestionsResultView,
    WorkflowValidationResultView,
)
from core.application.tooling.contracts import ToolRunResult

_FORBIDDEN_PATH_SEGMENTS = frozenset({"tool", "tooling", "db", "ext", "i18n"})
_PHASE2_USER_IDS = [
    "users.list",
    "users.create",
    "users.set-password",
    "users.force-reset",
    "users.unlock",
    "users.set-active",
    "users.set-roles",
    "users.issue-bootstrap-token",
]
_PHASE2_EXTENSION_IDS = [
    "extensions.list",
    "extensions.show",
    "extensions.health",
    "extensions.install",
    "extensions.uninstall",
    "extensions.enable",
    "extensions.disable",
    "extensions.create",
    "extensions.validate",
    "extensions.test",
    "extensions.bundle",
    "extensions.lock-deps",
]
_PHASE2_PROJECT_IDS = [
    "projects.list",
    "projects.show",
    "projects.notification-routes",
    "projects.notification-deliveries",
    "projects.dispatch-notification",
]
_PHASE2_WORKFLOW_IDS = [
    "workflows.convert-group",
    "workflows.list",
    "workflows.entrypoints",
    "workflows.validate",
    "workflows.contract",
    "workflows.suggestions",
    "workflows.hooks-list",
    "workflows.hook-get",
    "workflows.hook-preview",
    "workflows.hook-bind",
    "workflows.hook-unbind",
    "workflows.hook-create-and-bind",
    "workflows.where-used",
    "workflows.authoring-schema",
    "workflows.draft-validate",
    "workflows.draft-save",
    "workflows.definition",
    "workflows.apply-changes",
    "workflows.export",
    "workflows.import",
    "workflows.run",
    "workflows.stats",
]
_PHASE2_JOB_IDS = [
    "jobs.list",
    "jobs.show",
    "jobs.status",
    "jobs.logs",
    "jobs.messages",
    "jobs.errors",
    "jobs.approve",
    "jobs.reject",
    "jobs.ack",
    "jobs.unack",
    "jobs.cancel",
    "jobs.restart",
    "jobs.health-snapshot",
]
_PHASE2_REPORT_IDS = ["reports.generate", "reports.get", "reports.diagnostics"]
_PHASE4_STACK_IDS = [
    "stack.status",
    "stack.start",
    "stack.stop",
    "stack.restart",
    "stack.rebuild",
    "stack.logs",
]
_PHASE4_ENVIRONMENT_IDS = ["environment.show", "environment.switch"]
_PHASE4_MAINTENANCE_IDS = [
    "maintenance.status",
    "maintenance.enable",
    "maintenance.disable",
]
_PHASE4_RELEASE_IDS = ["release.info", "release.check", "release.upgrade"]
_PHASE4_BACKUP_IDS = ["backup.create", "backup.list", "backup.verify"]
_PHASE4_CONFIG_IDS = [
    "config.show",
    "config.validate",
    "config.set",
    "config.apply",
]
_PHASE2_DATABASE_IDS = [
    "database.status",
    "database.upgrade",
    "database.bootstrap-seed",
    "database.generate-migration",
    "database.query-diagnostics",
    "database.tune-env",
    "database.restore-parity-capture",
    "database.restore-parity-verify",
]
_PHASE2_TESTING_IDS = [
    "testing.smoke-test",
    "testing.nightfire-regression",
    "testing.fuzz-api",
    "testing.fuzz-mcp",
    "testing.fuzz-ui",
    "testing.cutover-preflight",
    "testing.cutover-smoke",
]
_PHASE2_QUALITY_IDS = [
    "quality.verify",
    "quality.lint",
    "quality.check-ci-excludes",
    "quality.check-naming",
    "quality.release-gate",
    "quality.sonar-export-issues",
    "quality.sonar-ensure-rules",
    "quality.sonar-generate-backlog",
]
_PHASE2_LOCALIZATION_IDS = [
    "localization.bootstrap-catalog",
    "localization.rebuild-pot",
    "localization.init-locale",
    "localization.update",
    "localization.compile",
    "localization.validate-keys",
]


def _public_command_specs():
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    return PUBLIC_COMMAND_SPECS


def test_public_command_specifications_have_unique_ids_and_paths() -> None:
    specs = _public_command_specs()

    command_ids = [spec.command_id for spec in specs]
    paths = [spec.path for spec in specs]

    # A census, not an invariant: it moves whenever a command is legitimately added
    # or retired, and it is here so that neither happens without someone noticing.
    # 107 since AC17-R113 added the top-level `proxy-fragment`; 99 since the 1.0
    # removal of built-in software-delivery features retired eight project/database/testing commands;
    # installation Clone adds exactly create and destructive restore;
    # retiring generic task submission removes one public command;
    # `reports diagnostics` adds the installation report for bug reports;
    # the nine workflow authoring adapters (authoring-schema .. stats) make 118.
    assert len(command_ids) == 118
    assert len(command_ids) == len(set(command_ids))
    assert len(paths) == len(set(paths))


def test_every_public_field_has_actionable_guidance_and_a_sample_value() -> None:
    """Keep every generated CLI and TUI input self-explanatory."""
    problems: list[str] = []
    for spec in _public_command_specs():
        for field in spec.fields:
            field_id = f"{spec.command_id}.{field.key}"
            description = " ".join(field.description.split())
            generic_descriptions = {
                field.label.casefold(),
                f"value for {field.label}".casefold(),
                f"{field.label} value".casefold(),
                f"input for {field.label}".casefold(),
            }
            if len(description) < 12 or description.rstrip(".").casefold() in generic_descriptions:
                problems.append(f"{field_id}: missing useful description")
            if not field.placeholder.strip() and not any(
                example.strip() for example in field.examples
            ):
                problems.append(f"{field_id}: missing example or placeholder")
            if field.choices:
                choice_values = {choice.value for choice in field.choices}
                invalid_examples = set(field.examples) - choice_values
                if invalid_examples:
                    problems.append(
                        f"{field_id}: examples are not choices: {sorted(invalid_examples)}"
                    )

    assert not problems, "Public field metadata gaps:\n" + "\n".join(problems)


def test_phase2_projects_workflows_checkpoint_contains_exact_approved_command_specs() -> None:
    specs = _public_command_specs()

    assert [spec.command_id for spec in specs] == [
        "status",
        "doctor",
        "proxy-fragment",
        *_PHASE4_STACK_IDS,
        *_PHASE4_ENVIRONMENT_IDS,
        *_PHASE4_MAINTENANCE_IDS,
        *_PHASE4_RELEASE_IDS,
        *_PHASE4_BACKUP_IDS,
        "clone.create",
        "clone.restore",
        *_PHASE4_CONFIG_IDS,
        *_PHASE2_USER_IDS,
        *_PHASE2_EXTENSION_IDS,
        *_PHASE2_PROJECT_IDS,
        *_PHASE2_WORKFLOW_IDS,
        *_PHASE2_JOB_IDS,
        *_PHASE2_REPORT_IDS,
        *_PHASE2_DATABASE_IDS,
        *_PHASE2_TESTING_IDS,
        *_PHASE2_QUALITY_IDS,
        *_PHASE2_LOCALIZATION_IDS,
    ]
    by_id = {spec.command_id: spec for spec in specs}
    for command_id in (
        "users.list",
        "extensions.list",
        "extensions.show",
        "extensions.health",
        "projects.list",
        "projects.show",
        "projects.notification-routes",
        "projects.notification-deliveries",
        "workflows.list",
        "workflows.entrypoints",
        "workflows.validate",
        "workflows.contract",
        "workflows.suggestions",
        "workflows.authoring-schema",
        "workflows.draft-validate",
        "workflows.definition",
        "workflows.export",
        "workflows.stats",
        "jobs.list",
        "jobs.show",
        "jobs.status",
        "jobs.logs",
        "jobs.messages",
        "jobs.errors",
        "jobs.health-snapshot",
        "reports.get",
        "reports.diagnostics",
        "database.status",
        "database.query-diagnostics",
        "testing.cutover-preflight",
        "quality.check-ci-excludes",
        "quality.check-naming",
        "localization.validate-keys",
        "environment.show",
        "release.info",
        "release.check",
        "backup.list",
        "backup.verify",
        "config.show",
        "config.validate",
    ):
        assert by_id[command_id].operation_type is OperationType.READ
        assert by_id[command_id].interactive_policy is InteractivePolicy.IMMEDIATE
    assert by_id["extensions.uninstall"].operation_type is OperationType.DESTRUCTIVE
    for command_id in set(_PHASE2_USER_IDS + _PHASE2_EXTENSION_IDS) - {
        "users.list",
        "extensions.list",
        "extensions.show",
        "extensions.health",
        "extensions.validate",
        "extensions.test",
    }:
        assert by_id[command_id].interactive_policy is InteractivePolicy.ALWAYS_REVIEW
    for command_id in {
        "projects.dispatch-notification",
        "reports.generate",
        "jobs.approve",
        "jobs.reject",
        "jobs.ack",
        "jobs.unack",
        "jobs.cancel",
        "jobs.restart",
        "database.upgrade",
        "database.bootstrap-seed",
        "database.generate-migration",
        "database.tune-env",
        "database.restore-parity-capture",
        "database.restore-parity-verify",
        "quality.lint",
        "quality.release-gate",
        "quality.sonar-export-issues",
        "quality.sonar-ensure-rules",
        "quality.sonar-generate-backlog",
        "localization.bootstrap-catalog",
        "localization.rebuild-pot",
        "localization.init-locale",
        "localization.update",
        "localization.compile",
        *(
            command_id
            for command_id in _PHASE2_TESTING_IDS
            if command_id != "testing.cutover-preflight"
        ),
    }:
        assert by_id[command_id].interactive_policy is InteractivePolicy.ALWAYS_REVIEW


def test_command_specs_expose_the_approved_field_order_options_and_models() -> None:
    specs = {spec.command_id: spec for spec in _public_command_specs()}

    users_list = specs["users.list"]
    assert [field.key for field in users_list.fields] == [
        "search",
        "include_inactive",
        "include_deleted",
        "limit",
        "offset",
    ]
    assert [field.option_name for field in users_list.fields] == [
        "--search",
        "--include-inactive",
        "--include-deleted",
        "--limit",
        "--offset",
    ]
    assert users_list.input_model.model_validate({}).model_dump() == {
        "search": "",
        "include_inactive": False,
        "include_deleted": False,
        "limit": 50,
        "offset": 0,
    }
    assert users_list.result_model is UserListView

    users_create = specs["users.create"]
    assert [field.key for field in users_create.fields] == [
        "email",
        "password",
        "display_name",
        "roles",
        "is_active",
        "must_change_password",
        "account_id",
        "account_ids",
        "actor",
    ]
    assert [field.option_name for field in users_create.fields] == [
        "--email",
        None,
        "--display-name",
        "--role",
        "--is-active",
        "--must-change-password",
        "--account-id",
        "--account-membership",
        "--actor",
    ]
    assert [field.required for field in users_create.fields] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert [field.advanced for field in users_create.fields] == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    assert users_create.result_model is UserMutationResultView

    extension_create = specs["extensions.create"]
    assert [field.key for field in extension_create.fields] == [
        "extension_id",
        "output_root",
        "display_name",
        "description",
        "vendor_name",
        "category",
    ]
    assert [field.option_name for field in extension_create.fields] == [
        None,
        "--output-root",
        "--display-name",
        "--description",
        "--vendor-name",
        "--category",
    ]
    assert extension_create.result_model is ExtensionScaffoldResult

    assert specs["users.set-password"].input_model is UserPasswordUpdateInput
    assert specs["users.force-reset"].input_model is UserSelectorInput
    assert specs["users.unlock"].input_model is UserSelectorInput
    assert specs["users.set-active"].input_model is UserActiveUpdateInput
    assert specs["users.set-roles"].input_model is UserRolesUpdateInput

    assert specs["extensions.list"].input_model is ExtensionsListCommandInput
    assert specs["extensions.list"].result_model is ExtensionListView
    assert specs["extensions.show"].input_model is ExtensionShowCommandInput
    assert specs["extensions.show"].result_model is ExtensionDetailView
    assert specs["extensions.health"].input_model is ExtensionShowCommandInput
    assert specs["extensions.health"].result_model is ExtensionHealthView
    assert specs["extensions.install"].input_model is ExtensionInstallCommandInput
    assert specs["extensions.install"].result_model is ExtensionArchiveInstallResultView
    assert specs["extensions.uninstall"].result_model is ExtensionArchiveUninstallResultView
    assert specs["extensions.enable"].result_model is ExtensionEnableResultView
    assert specs["extensions.disable"].result_model is ExtensionEnableResultView
    assert "--approve-browser-authority" in {
        field.option_name for field in specs["extensions.enable"].fields
    }
    assert "--approve-browser-authority" not in {
        field.option_name for field in specs["extensions.disable"].fields
    }

    assert [field.key for field in specs["projects.list"].fields] == [
        "page",
        "per_page",
        "search",
    ]
    assert specs["projects.list"].result_model is ProjectsListView
    assert specs["projects.show"].result_model is ProjectCliDetailView
    assert specs["projects.notification-routes"].result_model is ProjectNotificationRoutesView
    assert (
        specs["projects.notification-deliveries"].result_model is ProjectNotificationDeliveriesView
    )
    assert (
        specs["projects.dispatch-notification"].result_model
        is ProjectNotificationDispatchResultView
    )

    assert [field.key for field in specs["workflows.list"].fields] == [
        "project_id",
        "include_disabled",
        "include_validation",
    ]
    assert specs["workflows.list"].result_model is ProjectWorkflowCatalogView
    assert specs["workflows.entrypoints"].result_model is ProjectWorkflowEntrypointsView
    assert specs["workflows.validate"].result_model is WorkflowValidationResultView
    assert specs["workflows.contract"].result_model is WorkflowContractResultView
    assert specs["workflows.suggestions"].result_model is WorkflowSuggestionsResultView

    assert specs["jobs.list"].input_model is JobsListCommandInput
    assert specs["jobs.list"].result_model is JobsListView
    assert specs["jobs.show"].input_model is JobShowCommandInput
    assert specs["jobs.show"].result_model is JobDetailContractView
    assert specs["jobs.status"].result_model is JobStatusView
    assert specs["jobs.logs"].input_model is JobLogsCommandInput
    assert specs["jobs.logs"].result_model is JobLogsView
    assert specs["jobs.messages"].input_model is JobMessagesCommandInput
    assert specs["jobs.messages"].result_model is JobMessagesView
    assert specs["jobs.errors"].input_model is JobErrorsCommandInput
    assert specs["jobs.errors"].result_model is JobErrorsView
    assert specs["jobs.approve"].input_model is JobApprovalCommandInput
    assert specs["jobs.approve"].result_model is ApprovalResumeAcceptedView
    assert specs["jobs.reject"].input_model is JobApprovalCommandInput
    assert specs["jobs.reject"].result_model is ApprovalResumeAcceptedView
    assert specs["jobs.ack"].input_model is JobAcknowledgeCommandInput
    assert specs["jobs.ack"].result_model is JobLifecycleResultView
    assert specs["jobs.unack"].input_model is JobIdCommandInput
    assert specs["jobs.cancel"].input_model is JobCancelCommandInput
    assert specs["jobs.restart"].input_model is JobIdCommandInput
    assert specs["jobs.health-snapshot"].input_model is JobHealthSnapshotCommandInput
    assert specs["jobs.health-snapshot"].result_model is JobHealthSnapshotView

    assert specs["reports.generate"].input_model is ReportGenerateCommandInput
    assert specs["reports.generate"].result_model is ReportResultView
    assert specs["reports.get"].input_model is ReportGetCommandInput
    assert specs["reports.get"].result_model is ReportResultView

    assert specs["database.status"].input_model is DatabaseStatusRequest
    assert specs["database.status"].result_model is DatabaseOperationResult
    assert specs["database.upgrade"].input_model is DatabaseUpgradeRequest
    assert specs["database.bootstrap-seed"].input_model is DatabaseBootstrapSeedRequest
    assert specs["database.generate-migration"].input_model is DatabaseGenerateMigrationRequest
    assert specs["database.query-diagnostics"].input_model is DatabaseQueryDiagnosticsRequest
    assert specs["database.tune-env"].input_model is DatabaseTuneEnvRequest
    assert specs["database.restore-parity-capture"].input_model is (
        DatabaseRestoreParityCaptureRequest
    )
    assert specs["database.restore-parity-verify"].input_model is (
        DatabaseRestoreParityVerifyRequest
    )
    assert [field.key for field in specs["database.generate-migration"].fields] == [
        "name",
        "root",
    ]
    assert [field.option_name for field in specs["database.generate-migration"].fields] == [
        None,
        "--root",
    ]
    assert [field.key for field in specs["database.query-diagnostics"].fields] == [
        "dsn",
        "limit",
        "sort_by",
        "min_rows",
        "max_query_len",
    ]
    assert {field.key: field for field in specs["database.status"].fields}["dsn"].secret is True

    assert specs["testing.smoke-test"].input_model is SmokeTestRequestModel
    assert specs["testing.nightfire-regression"].input_model is NightfireRegressionRequestModel
    assert specs["testing.fuzz-api"].input_model is FuzzApiRequestModel
    assert specs["testing.fuzz-mcp"].input_model is FuzzMcpRequestModel
    assert specs["testing.fuzz-ui"].input_model is FuzzUiRequestModel
    assert specs["testing.cutover-preflight"].input_model is CutoverPreflightRequestModel
    assert specs["testing.cutover-smoke"].input_model is CutoverSmokeRequestModel
    assert all(
        specs[command_id].result_model is ToolRunResult for command_id in _PHASE2_TESTING_IDS
    )
    assert {field.key: field for field in specs["testing.nightfire-regression"].fields}[
        "auth_password"
    ].secret is True
    assert {field.key: field for field in specs["testing.fuzz-mcp"].fields}[
        "bearer_token"
    ].secret is True
    auth_fields = {field.key: field for field in specs["testing.cutover-smoke"].fields}
    assert auth_fields["password"].secret is True
    assert auth_fields["bearer_token"].secret is True
    assert auth_fields["password"].allow_secret_file is True
    assert auth_fields["bearer_token"].allow_secret_stdin is True

    from core.application.tooling.quality.models import QualityVerifyRequest

    assert specs["quality.verify"].input_model is QualityVerifyRequest
    assert specs["quality.lint"].input_model is QualityLintRequest
    assert specs["quality.check-ci-excludes"].input_model is QualityCheckCiExcludesRequest
    assert specs["quality.check-naming"].input_model is QualityCheckNamingRequest
    assert specs["quality.release-gate"].input_model is QualityReleaseGateRequest
    assert specs["quality.sonar-export-issues"].input_model is QualitySonarExportIssuesRequest
    assert specs["quality.sonar-ensure-rules"].input_model is QualitySonarEnsureRulesRequest
    assert specs["quality.sonar-generate-backlog"].input_model is (
        QualitySonarGenerateBacklogRequest
    )
    assert all(
        specs[command_id].result_model is ToolRunResult for command_id in _PHASE2_QUALITY_IDS
    )
    assert {field.key: field for field in specs["quality.sonar-export-issues"].fields}[
        "sonar_token"
    ].secret is True

    assert specs["localization.bootstrap-catalog"].input_model is (
        LocalizationBootstrapCatalogRequest
    )
    assert specs["localization.rebuild-pot"].input_model is LocalizationRebuildPotRequest
    assert specs["localization.init-locale"].input_model is LocalizationInitLocaleRequest
    assert specs["localization.update"].input_model is LocalizationUpdateRequest
    assert specs["localization.compile"].input_model is LocalizationCompileRequest
    assert specs["localization.validate-keys"].input_model is LocalizationValidateKeysRequest
    assert all(
        specs[command_id].result_model is ToolRunResult for command_id in _PHASE2_LOCALIZATION_IDS
    )


def test_sensitive_and_dynamic_field_contracts_are_closed_and_canonical() -> None:
    specs = {spec.command_id: spec for spec in _public_command_specs()}
    users_create = specs["users.create"]
    fields = {field.key: field for field in users_create.fields}

    password = fields["password"]
    assert password.type is FieldType.SECRET
    assert password.secret is True
    assert password.positional is False
    assert password.option_name is None
    assert password.allow_secret_file is True
    assert password.allow_secret_stdin is True
    assert password.allow_persisted_config is False
    assert all(" --password " not in f" {example} " for example in users_create.examples)
    assert all(
        "--password-stdin" in example or ".flow-steward/secret-input/" in example
        for example in users_create.examples
    )

    roles = fields["roles"]
    assert roles.type is FieldType.MULTI_SELECT
    assert [choice.value for choice in roles.choices] == ["admin", "operator", "viewer"]
    assert tuple(roles.default) == ("viewer",)
    assert users_create.input_model is UserCreateInput
    user_input = users_create.input_model.model_validate(
        {
            "email": "operator@example.com",
            "password": "from-isolated-secret-stream",
            "roles": roles.default,
        }
    )
    assert user_input.roles == ["viewer"]

    extension_id = {field.key: field for field in specs["extensions.create"].fields}["extension_id"]
    assert extension_id.type is FieldType.EXTENSION_ID
    assert extension_id.positional is True
    assert extension_id.required is True
    assert specs["extensions.create"].input_model is ExtensionScaffoldCreateInput
    assert (
        specs["extensions.create"]
        .input_model.model_validate({"extension_id": "acme.example", "output_root": "extensions"})
        .extension_id
        == "acme.example"
    )
    with pytest.raises(ValueError):
        specs["extensions.create"].input_model.model_validate(
            {"extension_id": "not canonical", "output_root": "extensions"}
        )

    project = {field.key: field for field in specs["projects.show"].fields}["project_id"]
    assert project.choice_provider == "projects.available"
    assert all(
        {field.key: field for field in specs[command_id].fields}["project_id"].choice_provider
        == "projects.available"
        for command_id in _PHASE2_PROJECT_IDS
        if command_id != "projects.list"
    )
    assert all(
        {field.key: field for field in specs[command_id].fields}["project_id"].choice_provider
        == "projects.available"
        for command_id in _PHASE2_WORKFLOW_IDS
        # The authoring schema is deployment-wide, as GET /api/workflow-authoring/schema is.
        if command_id != "workflows.authoring-schema"
    )
    assert {field.key: field for field in specs["jobs.list"].fields}[
        "project_id"
    ].choice_provider == "projects.available"


def test_first_slice_paths_are_two_segments_without_retired_domains() -> None:
    for spec in _public_command_specs():
        # Top level is the exception and the exception is listed, so adding one is a
        # decision rather than a drift. AC17-R113 adds the third: the launcher
        # dispatches `config` and `stack` to a retained fs-cli container the Compact
        # profile does not have, so a fragment command inside the `config` group could
        # not be run by the only profile that installs today.
        assert len(spec.path) == (
            1 if spec.command_id in {"status", "doctor", "proxy-fragment"} else 2
        )
        assert not _FORBIDDEN_PATH_SEGMENTS.intersection(spec.path)


def test_public_command_specs_have_closed_service_and_renderer_keys() -> None:
    from core.application.management.command_specs.registry import (
        ALLOWED_CHOICE_PROVIDER_REFS,
        ALLOWED_CONFIRMATION_POLICY_REFS,
        ALLOWED_PLAN_BUILDER_REFS,
        ALLOWED_RESULT_RENDERER_REFS,
        ALLOWED_SERVICE_REFS,
    )

    for spec in _public_command_specs():
        assert spec.service_ref in ALLOWED_SERVICE_REFS
        assert spec.result_renderer_ref in ALLOWED_RESULT_RENDERER_REFS
        assert spec.plan_builder_ref in ALLOWED_PLAN_BUILDER_REFS | {None}
        assert spec.confirmation_policy_ref in ALLOWED_CONFIRMATION_POLICY_REFS | {None}
        for field in spec.fields:
            assert field.choice_provider in ALLOWED_CHOICE_PROVIDER_REFS | {None}


def test_registry_allowed_key_sets_are_exact_and_independent_of_spec_contents() -> None:
    from core.application.management.command_specs.registry import (
        ALLOWED_CHOICE_PROVIDER_REFS,
        ALLOWED_CONFIRMATION_POLICY_REFS,
        ALLOWED_PLAN_BUILDER_REFS,
        ALLOWED_RESULT_RENDERER_REFS,
        ALLOWED_SERVICE_REFS,
    )

    expected_refs = frozenset(spec.service_ref for spec in _public_command_specs())
    assert expected_refs == ALLOWED_SERVICE_REFS
    assert (
        frozenset({"accounts.available", "projects.available", "stack.log-services"})
        == ALLOWED_CHOICE_PROVIDER_REFS
    )
    assert (
        frozenset(
            {
                "extensions.uninstall.preview",
                "stack.mutation.preview",
                "config.apply.preview",
                "environment.switch.preview",
                "release.upgrade.preview",
                "clone.restore.preview",
            }
        )
        == ALLOWED_PLAN_BUILDER_REFS
    )
    assert frozenset(
        {
            "match:extension_id",
            "match:project_id,repo_root",
            "match:target_installation_id,target_profile,public_host,plan_digest",
        }
    ) == (ALLOWED_CONFIRMATION_POLICY_REFS)
    assert frozenset(spec.result_renderer_ref for spec in _public_command_specs()) == (
        ALLOWED_RESULT_RENDERER_REFS
    )


# AC17-R115: operations the installer invokes directly, never through a user's command.
_INTERNAL_COMPACT_OPERATIONS = frozenset({"bootstrap", "candidate-preflight"})
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_LAUNCHER = _REPOSITORY_ROOT / "core/entrypoints/cli/scripts/flow_steward_launcher.sh"
_PLATFORM_RUNTIME = _REPOSITORY_ROOT / "core/entrypoints/cli/platform_runtime.py"


def _launcher_compact_arcs() -> set[str]:
    """The operations the launcher will dispatch on Compact, read out of its `case`."""
    import re

    text = _LAUNCHER.read_text(encoding="utf-8")
    arcs = set(
        re.findall(r"^\s*\d:[a-z-]+:[a-z-]*\)\s*\n?\s*compact_operation=([a-z-]+)", text, re.M)
    )
    assert arcs, "no dispatch arcs parsed from the launcher; this test would be vacuous"
    return arcs


def _declared_platform_operations() -> set[str]:
    from core.application.management.command_specs.contracts import CompactSupport

    operations = {
        spec.command_id.replace(".", "-")
        for spec in _public_command_specs()
        if spec.compact_support is CompactSupport.PLATFORM
    }
    assert operations, "no platform commands declared; this test would be vacuous"
    return operations


def test_the_launcher_dispatches_exactly_the_commands_declared_for_compact() -> None:
    """AC17-R115: availability lived in four hand-kept places and in none of them was it a property.

    A command could be right in the registry and refused by the launcher -- which is
    how `config proxy-fragment` reached a live stand and answered "Unsupported Compact
    platform command." Now the spec says how each command is dispatched, and the
    launcher's own `case` has to agree with it.

    The operation name is not a second thing to declare: it is the command id with its
    separator changed, which holds for all thirteen arcs today.
    """
    assert _launcher_compact_arcs() == _declared_platform_operations()


def test_the_image_accepts_exactly_the_operations_the_launcher_can_send() -> None:
    """The second barrier, and the one that would refuse a command the launcher accepted.

    `_OPERATIONS` is the argparse `choices` inside the image, so a command missing here
    fails after the launcher has already agreed to run it -- a failure that looks like
    the image being stale rather than a list being short.
    """
    import re

    source = _PLATFORM_RUNTIME.read_text(encoding="utf-8")
    block = re.search(r"_OPERATIONS = \((.*?)\)", source, re.S)
    assert block, "_OPERATIONS not found; this test would be vacuous"
    accepted = set(re.findall(r'"([a-z-]+)"', block.group(1)))

    assert accepted - _INTERNAL_COMPACT_OPERATIONS == _declared_platform_operations()


def test_the_help_declares_every_command_a_compact_user_cannot_run() -> None:
    """AC17-R115: `environment` and `release` were refused and the help never said so.

    An operator who reads the help and then meets a flat "Unsupported Compact platform
    command." was told the product works when it does not. The groups the help names as
    unavailable must be exactly the groups declared unavailable, so neither side can
    drift from the other again.

    Announcing is not opening: whether those commands should run on Compact is a
    separate decision, and this only requires that the refusal be honest.
    """
    from core.application.management.command_specs.contracts import CompactSupport

    unavailable = {
        spec.command_id.split(".", 1)[0]
        for spec in _public_command_specs()
        if spec.compact_support
        in {
            CompactSupport.TOOLING_SERVICE,
            CompactSupport.DELEGATED_SERVICE,
            CompactSupport.RETAINED_CLI,
            CompactSupport.REFUSED_UNDECLARED,
        }
    }
    assert unavailable, "nothing declared unavailable; this test would be vacuous"

    text = _LAUNCHER.read_text(encoding="utf-8")
    start = text.index("Platform commands:")
    help_text = text[start : text.index("EOF\n}", start)]
    missing = sorted(group for group in unavailable if group not in help_text)

    assert missing == [], (
        "these are refused on Compact and the help does not mention them, so an "
        f"operator meets the refusal with no warning: {missing}"
    )
