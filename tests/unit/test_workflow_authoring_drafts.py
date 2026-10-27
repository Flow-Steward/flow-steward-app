"""Shared preparation, scope, disabled persistence and safe retry contracts."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest
import yaml

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceConflictError, ServiceForbiddenError
from core.application.registry.contract_validation_workflow_readiness import (
    validate_workflow_readiness,
)
from core.application.workflows.authoring.drafts import WorkflowAuthoringDrafts
from core.application.workflows.authoring.schema import WorkflowAuthoringSchema
from core.application.workflows.workflow_portability import WorkflowPortabilityError
from tests._workflow_definitions import linear_graph
from tests.unit.test_workflow_authoring_catalog import SCOPE, CatalogDB


class DraftDB(CatalogDB):
    def __init__(self):
        super().__init__()
        self.bindings = []
        self.history = []
        self.fail_binding = False

    @contextmanager
    def workflow_import_transaction(self):
        snapshot = deepcopy((self.workflows, self.bindings, self.history))
        try:
            yield
        except Exception:
            self.workflows, self.bindings, self.history = snapshot
            raise

    def save_workflow(self, data, author="admin", *, create_only=False):
        assert create_only, "authoring must use atomic create-only persistence"
        workflow_id = data["workflow_id"]
        if workflow_id in self.workflows:
            raise ServiceConflictError("Already exists")
        self.workflows[workflow_id] = deepcopy(data)
        self.history.append({"workflow_id": workflow_id, "action": "create", "author": author})
        return 1

    def add_project_workflow_binding(self, project_id, workflow_id):
        if self.fail_binding:
            raise RuntimeError("binding failed")
        self.bindings.append((project_id, workflow_id))


def bundle(**changes):
    parsed = yaml.safe_load(WorkflowAuthoringSchema().get()["minimal_bundle_yaml"])
    parsed["workflows"][0].update(changes)
    return yaml.safe_dump(parsed)


def lead_with(parsed: dict, steps):
    """Put *steps* before the minimal bundle's terminal step and route them in order."""
    workflow = parsed["workflows"][0]
    if not isinstance(steps, list):
        workflow["steps"] = steps
        return
    done = workflow["steps"][-1]
    workflow["steps"] = [*steps, done]
    workflow["execution_graph"] = linear_graph([step["step_id"] for step in workflow["steps"]])


def service(db, *, creator=True):
    def authorize(scope):
        if not creator:
            raise ServiceForbiddenError("Access denied")

    return WorkflowAuthoringDrafts(db, creation_authorizer=authorize)


def test_validation_does_no_writes_and_save_uses_same_disabled_payload():
    db = DraftDB()
    drafts = service(db)
    text = bundle(enabled=True, account_id="foreign", project_id="foreign")
    result = drafts.validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert result["can_save"] is True, result
    assert db.workflows == {}
    assert db.history == []
    assert db.bindings == []
    saved = drafts.save(
        scope={**SCOPE, "actor_id": "creator"}, project_id="project", bundle_yaml=text
    )
    assert saved["enabled"] is False
    assert saved["workflow_id"] == "new_workflow"
    assert saved["editor_url"] == "/registry/workflows/new_workflow/builder"
    row = db.workflows["new_workflow"]
    assert row["enabled"] is False
    assert row["account_id"] == DEFAULT_ACCOUNT_ID
    assert row["project_id"] == "project"
    assert row["scope_kind"] == "account"
    assert db.bindings == [("project", "new_workflow")]
    assert db.history == [{"workflow_id": "new_workflow", "action": "create", "author": "creator"}]


def test_incomplete_disabled_draft_retains_nonblocking_execution_readiness_issue():
    drafts = service(DraftDB())
    result = drafts.validate(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    assert result["can_save"] is True
    assert result["readiness"]["valid"] is False
    issue = next(
        item for item in result["issues"] if item["code"] == "workflow.execution_bindings_missing"
    )
    assert issue["blocking"] is False
    assert issue["severity"] == "warning"
    assert issue["path"] == "workflow.steps"
    assert "at least one real execution step" in issue["message"]
    assert issue["repair_guidance"]
    saved = drafts.save(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    assert saved["enabled"] is False
    assert saved["issues"] == result["issues"]


def test_execution_readiness_producer_marks_its_generated_message_for_localization():
    db = DraftDB()
    service(db).save(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    readiness = validate_workflow_readiness(db, "new_workflow")
    issue = next(
        item
        for item in readiness["errors"]
        if item["code"] == "workflow.execution_bindings_missing"
    )

    assert issue.get("message_key") == "react.workflows.execution_bindings_missing"
    assert issue.get("message_params") == {}
    assert "at least one real execution step" in issue["message"]
    assert issue["path"] == "workflow.steps"


def test_save_retry_returns_conflict_without_overwriting_original():
    db = DraftDB()
    drafts = service(db)
    drafts.save(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    workflows_before, bindings_before, history_before = deepcopy(
        (db.workflows, db.bindings, db.history)
    )
    overwrite = bundle(display_name="Overwrite")
    with pytest.raises(ServiceConflictError):
        drafts.save(scope=SCOPE, project_id="project", bundle_yaml=overwrite)
    assert db.workflows == workflows_before
    assert db.bindings == bindings_before
    assert db.history == history_before


@pytest.mark.parametrize(
    "action",
    [
        "create_connection_placeholder",
        "install_extension_required",
        "rename_workflow",
        "skip_optional",
        "abort",
        "unknown",
    ],
)
def test_unsafe_resolution_rejected_before_side_effects(action):
    db = DraftDB()
    drafts = service(db)
    text = bundle()
    resolutions = [{"dependency_type": "tool", "ref": "missing", "action": action}]
    with pytest.raises(WorkflowPortabilityError):
        drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text, resolutions=resolutions)
    assert db.workflows == {}
    assert db.bindings == []
    assert db.history == []


def test_multiple_definitions_and_protected_reference_are_not_importable():
    db = DraftDB()
    parsed = yaml.safe_load(bundle())
    parsed["workflows"].append({"workflow_id": "second"})
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    parsed["workflows"] = [
        {"workflow_id": "protected", "portability_kind": "extension_workflow_ref"}
    ]
    assert (
        service(db).validate(scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed))[
            "can_save"
        ]
        is False
    )
    assert db.workflows == {}


def test_a_draft_with_hook_bindings_is_refused_instead_of_dropping_them():
    db = DraftDB()
    parsed = yaml.safe_load(bundle())
    parsed["hook_bindings"] = [
        {
            "hook_id": "before_invoice",
            "owner_workflow_id": parsed["workflows"][0]["workflow_id"],
            "hook_contract_version": 1,
            "hook_semantic_digest": "digest",
            "target_workflow_id": "buyer_fix",
            "input_mapping": {},
            "output_mapping": {},
        }
    ]
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    assert [issue["code"] for issue in result["issues"]] == [
        "authoring.hook_bindings_not_supported"
    ]
    assert db.workflows == {}


def test_invalid_persisted_config_validation_save_parity():
    db = DraftDB()
    parsed = yaml.safe_load(bundle())
    lead_with(
        parsed,
        [
            {
                "step_kind": "set_variable",
                "step_id": "assign",
                "override": {"set_variable": {"target": "", "value": "x", "invented_key": True}},
            }
        ],
    )
    text = yaml.safe_dump(parsed)
    result = service(db).validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert result["can_save"] is False
    assert any("unknown" in str(issue.get("code")) for issue in result["issues"])
    drafts = service(db)
    with pytest.raises(WorkflowPortabilityError):
        drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert db.workflows == {}


def test_create_authority_is_required_even_for_launch_capable_scope():
    db = DraftDB()
    non_creator = service(db, creator=False)
    launch_scope = {**SCOPE, "token_scopes": {"workflows.write"}}
    text = bundle()
    with pytest.raises(ServiceForbiddenError):
        non_creator.save(scope=launch_scope, project_id="project", bundle_yaml=text)
    unauthorized = WorkflowAuthoringDrafts(db)
    with pytest.raises(ServiceForbiddenError):
        unauthorized.save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert db.workflows == {}


def test_binding_failure_rolls_back_workflow_and_history():
    db = DraftDB()
    db.fail_binding = True
    drafts = service(db)
    text = bundle()
    with pytest.raises(RuntimeError, match="binding failed"):
        drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert db.workflows == {}
    assert db.history == []
    assert db.bindings == []


def test_project_restriction_rejects_before_preparation():
    db = DraftDB()
    drafts = service(db)
    restricted = {**SCOPE, "allowed_project_ids": {"other"}}
    text = bundle()
    with pytest.raises(ServiceForbiddenError):
        drafts.validate(scope=restricted, project_id="project", bundle_yaml=text)


def test_resolution_target_must_be_visible_even_when_registry_row_exists():
    from tests.unit.test_workflow_authoring_catalog import tool

    db = DraftDB()
    db.tools["foreign"] = tool("foreign", account_id="other")
    parsed = yaml.safe_load(bundle())
    parsed["dependencies"] = {"tools": [{"tool_id": "missing"}]}
    result = service(db).validate(
        scope=SCOPE,
        project_id="project",
        bundle_yaml=yaml.safe_dump(parsed),
        resolutions=[
            {
                "dependency_type": "tool",
                "ref": "missing",
                "action": "map_existing",
                "target_ref": "foreign",
            }
        ],
    )
    assert result["can_save"] is False
    assert any(issue["code"] == "authoring.reference_not_permitted" for issue in result["issues"])
    assert db.workflows == {}


def test_validation_target_workflow_scope_matches_save():
    db = DraftDB()
    restricted = {**SCOPE, "allowed_workflow_ids": {"other"}}
    drafts = service(db)
    text = bundle()
    with pytest.raises(ServiceForbiddenError):
        drafts.validate(scope=restricted, project_id="project", bundle_yaml=text)
    with pytest.raises(ServiceForbiddenError):
        drafts.save(scope=restricted, project_id="project", bundle_yaml=text)
    assert db.workflows == {}


def test_denied_child_does_not_hydrate_private_contract_diagnostics(monkeypatch):
    db = DraftDB()
    db.workflows["private"] = {
        "workflow_id": "private",
        "enabled": True,
        "account_id": "foreign",
        "scope_kind": "account",
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [
                    {"name": "private_payroll_secret", "required": True, "type": "string"}
                ],
            }
        },
    }
    parsed = yaml.safe_load(bundle())
    lead_with(
        parsed,
        [
            {
                "step_id": "call",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": "private"}},
            }
        ],
    )
    import core.application.workflows.authoring.drafts as draft_module

    def forbidden_contract_hydration(*args, **kwargs):
        pytest.fail("readiness must be skipped after denied dependency scope")

    monkeypatch.setattr(draft_module, "validate_workflow_readiness", forbidden_contract_hydration)
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    assert "private_payroll_secret" not in str(result)
    assert "contracts" not in result["performed_checks"]
    assert "contracts" in result["unperformed_checks"]


@pytest.mark.parametrize(
    "override",
    [
        "scalar",
        {"invoke_workflow": "scalar"},
        {"dataset_query": []},
        {"collect": "scalar"},
        {"batch": [1]},
    ],
)
def test_malformed_override_returns_structured_repair_issue(override):
    db = DraftDB()
    parsed = yaml.safe_load(bundle())
    lead_with(
        parsed, [{"step_id": "malformed", "step_kind": "invoke_workflow", "override": override}]
    )
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    assert any("override" in issue["path"] for issue in result["issues"])
    assert db.workflows == {}


def test_unsupported_resolution_reports_accurate_checks_and_safe_path():
    result = service(DraftDB()).validate(
        scope=SCOPE,
        project_id="project",
        bundle_yaml=bundle(),
        resolutions=[
            {"dependency_type": "tool", "ref": "missing", "action": "install_extension_required"}
        ],
    )
    assert result["can_save"] is False
    assert "bundle_schema" not in result["performed_checks"]
    assert result["issues"][0]["path"] == "resolutions[0].action"


@pytest.mark.parametrize(
    "field,value",
    [
        ("settings", "scalar"),
        ("trigger", "scalar"),
        ("steps", "scalar"),
        ("participants", ["scalar"]),
        ("participants", "scalar"),
    ],
)
def test_malformed_workflow_field_is_structured_rejection(field, value):
    db = DraftDB()
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=bundle(**{field: value})
    )
    assert result["can_save"] is False
    assert any(field in issue["path"] for issue in result["issues"])
    assert db.workflows == {}


def test_malformed_steps_is_structured_rejection():
    db = DraftDB()
    parsed = yaml.safe_load(bundle())
    lead_with(parsed, "scalar")
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    assert any("steps" in issue["path"] for issue in result["issues"])
    assert db.workflows == {}


def test_malformed_yaml_never_echoes_source_secret():
    result = service(DraftDB()).validate(
        scope=SCOPE, project_id="project", bundle_yaml="[secret_marker_9349: ["
    )
    assert result["can_save"] is False
    assert "secret_marker_9349" not in str(result)
    assert result["issues"][0]["code"] == "bundle_yaml_invalid"


@pytest.mark.parametrize("binding_field", ["connection_binding", "connection_binding_jsonb"])
def test_nested_connector_connection_reference_is_scope_checked(binding_field):
    from tests.unit.test_workflow_authoring_catalog import tool

    db = DraftDB()
    db.tools["public_tool"] = tool(
        "public_tool", interface={"operations": [{"operation_id": "send"}]}
    )
    db.get_project_connection = lambda **kwargs: {
        "connection_id": "private_connection",
        "project_id": "foreign",
        "account_id": "foreign",
    }
    parsed = yaml.safe_load(bundle())
    lead_with(
        parsed,
        [
            {
                "step_id": "send",
                "step_kind": "connector",
                "connector_id": "public_tool",
                "override": {
                    "connector": {
                        "operation_id": "send",
                        binding_field: {"connection_id": "private_connection"},
                    }
                },
            }
        ],
    )
    result = service(db).validate(
        scope=SCOPE, project_id="project", bundle_yaml=yaml.safe_dump(parsed)
    )
    assert result["can_save"] is False
    assert any(issue["code"] == "authoring.reference_not_permitted" for issue in result["issues"])
    assert "contracts" not in result["performed_checks"]


def test_projected_agent_draft_validates_without_writes_and_saves_disabled():
    from tests.unit.test_workflow_authoring_catalog import PROJECTED_AGENT, install_projection_store

    db = DraftDB()
    # The real physical registry exposes these getters even when virtual rows have no entries.
    for getter in (
        "get_agent_skills",
        "get_agent_tools",
        "get_agent_inputs",
        "get_agent_outputs",
        "get_skill_inputs",
        "get_skill_outputs",
    ):
        setattr(db, getter, lambda _: [])
    install_projection_store(db)
    parsed = yaml.safe_load(bundle())
    lead_with(parsed, [{"step_id": "copy", "step_kind": "agent", "agent_id": PROJECTED_AGENT}])
    text = yaml.safe_dump(parsed)
    drafts = service(db)
    validation = drafts.validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert validation["can_save"] is True, validation
    assert db.workflows == {}
    assert db.history == []
    assert db.bindings == []
    saved = drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert saved["enabled"] is False
    step = db.workflows["new_workflow"]["steps"][0]
    assert step["agent_id"] == PROJECTED_AGENT
    assert not step.get("skill_id")
    assert db.bindings == [("project", "new_workflow")]


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("has_hook", [False, True])
def test_draft_hook_creation_uses_actual_server_mode(monkeypatch, mode, has_hook):
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    parsed = yaml.safe_load(bundle())
    if has_hook:
        hook = declaration(
            input_schema={"type": "object", "properties": {}},
            input_mapping={},
            mutable_fields=[],
            additional_outputs={},
        )
        lead_with(
            parsed,
            [
                {
                    "step_id": "before",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"hook": hook}},
                }
            ],
        )
    text = yaml.safe_dump(parsed)
    db = DraftDB()
    drafts = service(db)
    result = drafts.validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    blocked = mode == "production" and has_hook
    assert result["can_save"] is (not blocked), result
    assert db.workflows == {}
    assert db.history == []
    assert db.bindings == []
    if blocked:
        issues = [
            issue
            for issue in result["issues"]
            if issue.get("code") == "workflow.hook.declaration_requires_development"
        ]
        assert issues
        assert all(
            issue["blocking"] and issue["severity"] == "error" and issue["path"] for issue in issues
        )
        with pytest.raises(WorkflowPortabilityError) as raised:
            drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
        assert raised.value.code == "workflow_draft_invalid"
        assert raised.value.status_code == 400
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            for issue in raised.value.errors
        )
        assert db.workflows == {}
        assert db.history == []
        assert db.bindings == []
    else:
        saved = drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
        row = db.workflows[saved["workflow_id"]]
        assert row["enabled"] is False
        assert row["account_id"] == DEFAULT_ACCOUNT_ID
        assert db.bindings == [("project", saved["workflow_id"])]
        assert len(db.history) == 1
