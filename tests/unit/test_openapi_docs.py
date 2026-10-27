from __future__ import annotations

import importlib
from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from core.entrypoints.api.jobs_control import create_jobs_control_router

PUBLIC_PATHS = {
    "/api/workflow-runs": "post",
    "/api/jobs": "get",
    "/api/jobs/{job_id}": "get",
    "/api/jobs/{job_id}/cancel": "post",
    "/api/jobs/{job_id}/approval": "get",
    "/api/events/stream": "get",
    "/api/approvals/{thread_id}": "post",
    "/api/projects/{project_id}/workflow-catalog": "get",
    "/api/projects/{project_id}/workflow-entrypoints": "get",
    "/api/projects/{project_id}/workflows/{workflow_id}/contract": "get",
}

PUBLIC_OPERATION_IDS = {
    ("post", "/api/workflow-runs"): "startWorkflowRun",
    ("get", "/api/jobs"): "listJobs",
    ("get", "/api/jobs/{job_id}"): "getJob",
    ("post", "/api/jobs/{job_id}/cancel"): "cancelJob",
    ("get", "/api/jobs/{job_id}/approval"): "getJobApproval",
    ("get", "/api/events/stream"): "streamJobEvents",
    ("post", "/api/approvals/{thread_id}"): "resumeApproval",
    ("get", "/api/projects/{project_id}/workflow-catalog"): "listProjectWorkflowCatalog",
    ("get", "/api/projects/{project_id}/workflow-entrypoints"): "listProjectWorkflowEntrypoints",
    ("get", "/api/projects/{project_id}/workflows/{workflow_id}/contract"): (
        "getProjectWorkflowContract"
    ),
}

INTERNAL_NON_JSON_SUCCESS_EXCEPTIONS = {
    ("GET", "/api/events/stream", "200"): "Server-sent events stream.",
    (
        "GET",
        "/api/jobs/{job_id}/attachments/{attachment_id}",
        "200",
    ): "Binary attachment download.",
    ("GET", "/api/workflows/{workflow_id}/export.yaml", "200"): "YAML workflow export.",
}


def _reload_http_app_module():
    entrypoint = importlib.import_module("core.entrypoints.api.app")
    entrypoint = importlib.reload(entrypoint)
    test_surface = importlib.import_module("tests.api_runtime_test_surface")
    test_surface.create_app = entrypoint.create_app
    return test_surface


def _create_client(monkeypatch, **env: str) -> tuple[TestClient, object]:
    from core.infrastructure.runtime import app_factory as runtime_app_factory

    monkeypatch.setattr(
        runtime_app_factory,
        "validate_runtime_service_ports",
        lambda _container, _contracts: {},
    )
    for key in (
        "FS_OPENAPI_PUBLIC_ENABLED",
        "FS_DOCS_PUBLIC_ENABLED",
        "FS_OPENAPI_INTERNAL_ENABLED",
        "FS_DOCS_INTERNAL_ENABLED",
        "FS_OPENAPI_PUBLIC_REQUIRE_AUTH",
        "FS_OPENAPI_INTERNAL_REQUIRE_AUTH",
        "FS_LOCAL_AUTH_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    base_env = {
        "FS_OPENAPI_PUBLIC_ENABLED": "0",
        "FS_DOCS_PUBLIC_ENABLED": "0",
        "FS_OPENAPI_INTERNAL_ENABLED": "0",
        "FS_DOCS_INTERNAL_ENABLED": "0",
        "FS_OPENAPI_PUBLIC_REQUIRE_AUTH": "1",
        "FS_OPENAPI_INTERNAL_REQUIRE_AUTH": "1",
    }
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = _reload_http_app_module()
    return TestClient(module.create_app()), module


def test_default_curated_docs_endpoints_are_disabled(monkeypatch):
    client, _module = _create_client(monkeypatch)

    assert client.get("/openapi-public.json").status_code == 404
    assert client.get("/docs/api").status_code == 404
    assert client.get("/openapi-internal.json").status_code == 404
    assert client.get("/docs/internal").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.app.openapi_url is None
    assert client.app.docs_url is None
    assert client.app.redoc_url is None


def test_public_openapi_schema_uses_exact_allowlist(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_DOCS_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    response = client.get("/openapi-public.json")
    assert response.status_code == 200
    schema = response.json()

    assert set(schema["paths"]) == set(PUBLIC_PATHS)
    for path, method in PUBLIC_PATHS.items():
        assert schema["paths"][path][method]["operationId"] == PUBLIC_OPERATION_IDS[(method, path)]

    excluded_paths = {
        "/api/projects/{project_id}/workflows/{workflow_id}/validate",
        "/api/projects/{project_id}/workflows/{workflow_id}/suggestions",
        "/api/jobs/{job_id}/restart",
        "/api/jobs/{job_id}/acknowledge",
        "/api/jobs/{job_id}/unacknowledge",
        "/api/workflow-entrypoints/{entry_key}",
    }
    for path in excluded_paths:
        assert path not in schema["paths"]
    assert not any(path.startswith("/api/reports/") for path in schema["paths"])

    assert schema["info"]["title"] == "Flow Steward Public API"
    assert schema["info"]["version"] == "v1"
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert schema["security"] == [{"BearerAuth": []}]
    assert [tag["name"] for tag in schema["tags"]] == ["Jobs", "Approvals", "Workflows"]
    jobs_get = schema["paths"]["/api/jobs"]["get"]
    assert {parameter["name"] for parameter in jobs_get.get("parameters", [])} == {
        "project_id",
        "limit",
        "page",
        "status",
    }
    assert jobs_get["summary"] == "List jobs."
    assert "attention-board" not in str(jobs_get.get("description", "")).lower()
    jobs_schema = jobs_get["responses"]["200"]["content"]["application/json"]["schema"]
    assert jobs_schema == {"$ref": "#/components/schemas/JobListResponse"}
    cancel_post = schema["paths"]["/api/jobs/{job_id}/cancel"]["post"]
    assert cancel_post["summary"] == "Cancel a job."
    assert "ajax" not in str(cancel_post.get("description", "")).lower()
    assert "redirect" not in str(cancel_post.get("description", "")).lower()
    catalog_get = schema["paths"]["/api/projects/{project_id}/workflow-catalog"]["get"]
    assert {parameter["name"] for parameter in catalog_get.get("parameters", [])} == {
        "project_id",
        "enabled_only",
        "include_validation",
    }
    workflow_item = schema["components"]["schemas"]["WorkflowCatalogItem"]["properties"]
    assert "validation" not in workflow_item


def test_public_docs_page_uses_public_schema(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_DOCS_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    response = client.get("/docs/api")
    assert response.status_code == 200
    assert "/openapi-public.json" in response.text
    assert "/openapi.json" not in response.text


def test_internal_openapi_schema_excludes_raw_ingress_and_public_auth_routes(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    response = client.get("/openapi-internal.json")
    assert response.status_code == 200
    schema = response.json()

    assert "/api/workflow-runs" in schema["paths"]
    assert "/api/tasks" not in schema["paths"]
    assert "/api/projects/{project_id}/tasks" not in schema["paths"]
    assert "/api/projects/{project_id}/tasks/with-files" not in schema["paths"]
    assert "/api/workflow-entrypoints/{entry_key}" not in schema["paths"]
    assert "/api/health" not in schema["paths"]
    assert "/api/auth/login" not in schema["paths"]
    assert "/webhook/providers/{provider_key}" not in schema["paths"]

    docs_response = client.get("/docs/internal")
    assert docs_response.status_code == 200
    assert "/openapi-internal.json" in docs_response.text


def test_internal_success_responses_have_non_empty_json_schemas(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()
    bad: list[tuple[str, str, str]] = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            for status_code, response in (operation.get("responses") or {}).items():
                if not str(status_code).startswith(("2", "3")):
                    continue
                key = (method.upper(), path, str(status_code))
                content = (response or {}).get("content") or {}
                json_content = content.get("application/json")
                if json_content is None:
                    assert key in INTERNAL_NON_JSON_SUCCESS_EXCEPTIONS, key
                    continue
                response_schema = json_content.get("schema")
                if response_schema in (None, {}):
                    bad.append(key)
    assert not bad, bad
    assert "Dynamic JSON Response" not in str(schema)

    agents_schema = schema["paths"]["/api/agents"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert agents_schema["type"] == "array"
    assert agents_schema["items"]["$ref"] == "#/components/schemas/AgentRecordView"

    skills_schema = schema["paths"]["/api/skills"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert skills_schema["type"] == "array"
    assert skills_schema["items"]["$ref"] == "#/components/schemas/SkillRecordView"

    tools_schema = schema["paths"]["/api/tools"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert tools_schema["type"] == "array"
    assert tools_schema["items"]["$ref"] == "#/components/schemas/ToolRecordView"
    assert (
        schema["paths"]["/api/projects"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ProjectsListView"
    )
    assert (
        schema["paths"]["/api/auth/users"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "AuthUsersListView"
    )
    assert (
        schema["paths"]["/api/auth/tokens"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "AuthTokensListView"
    )
    assert (
        schema["paths"]["/api/auth/service-accounts"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "AuthServiceAccountsListView"
    )
    assert (
        schema["paths"]["/api/datasets"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["title"]
        == "DatasetsListView"
    )
    assert (
        schema["paths"]["/api/datasets/data-connections"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "DatasetDataConnectionsListView"
    )
    assert (
        schema["paths"]["/api/datasets/query-templates"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "DatasetQueryTemplatesListView"
    )
    assert (
        schema["paths"]["/api/datasets/{dataset_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "DatasetRecordView"
    )
    assert (
        schema["paths"]["/api/v2/jobs"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ApiV2JobsEnvelopeView"
    )
    assert (
        schema["paths"]["/api/v2/jobs/{job_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ApiV2JobDetailEnvelopeView"
    )
    assert (
        schema["paths"]["/api/v2/projects"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ApiV2ProjectsEnvelopeView"
    )
    assert (
        schema["paths"]["/api/v2/projects/{project_id}/jobs"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ApiV2ProjectJobsEnvelopeView"
    )
    assert (
        schema["paths"]["/api/v2/projects/{project_id}/jobs/bulk-delete"]["post"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ApiV2ProjectJobsBulkDeleteEnvelopeView"
    )
    assert (
        schema["paths"]["/api/v2/dashboard/summary"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ApiV2DashboardSummaryEnvelopeView"
    )


def test_internal_api_v2_examples_are_non_empty_and_match_envelopes(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()
    v2_paths = {
        ("get", "/api/v2/jobs"),
        ("get", "/api/v2/jobs/{job_id}"),
        ("get", "/api/v2/projects"),
        ("get", "/api/v2/projects/{project_id}/jobs"),
        ("post", "/api/v2/projects/{project_id}/jobs/bulk-delete"),
        ("get", "/api/v2/dashboard/summary"),
    }

    for method, path in v2_paths:
        payload = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"]
        assert payload["example"] != {}
        assert payload["schema"]["$ref"].startswith("#/components/schemas/ApiV2")
    assert (
        schema["paths"]["/api/workflows/import/preflight"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "WorkflowImportPreflightView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/triggers"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["title"]
        == "WorkflowTriggersView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/triggers/webhooks"]["post"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]["title"]
        == "WorkflowTriggerEntrypointView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/triggers/providers"]["post"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]["title"]
        == "WorkflowProviderTriggerView"
    )
    assert (
        schema["paths"]["/api/extensions"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "ExtensionListView"
    )
    assert (
        schema["paths"]["/api/extensions/marketplace"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "ExtensionMarketplaceListView"
    )
    assert (
        schema["paths"]["/api/extensions/{extension_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "ExtensionDetailView"
    )
    assert not any("/phases" in path for path in schema["paths"]), (
        "workflows have no phases; steps and groups change through /changes"
    )
    assert "post" in schema["paths"]["/api/workflows/{workflow_id}/changes"]

    step_outputs_schema = schema["paths"][
        "/api/workflows/{workflow_id}/steps/{step_id}/available-outputs"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert step_outputs_schema["type"] == "array"
    assert step_outputs_schema["items"]["$ref"] == "#/components/schemas/WorkflowEditorOutputView"

    step_io_validation_schema = schema["paths"][
        "/api/workflows/{workflow_id}/steps/{step_id}/io-validation"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert step_io_validation_schema["type"] == "array"
    assert (
        step_io_validation_schema["items"]["$ref"]
        == "#/components/schemas/WorkflowEditorIOIssueView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/steps/{step_id}/available-variables"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowEditorAvailableVariablesView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/steps/{step_id}/input-defaults"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowEditorInputDefaultsView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/step-contract-preview"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowEditorStepContractPreviewView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/simulate"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowEditorSimulationResultView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/test-run"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowEditorTestRunResultView"
    )
    workflow_test_run_schema = schema["components"]["schemas"]["WorkflowEditorTestRunResultView"]
    assert "approval_id" in workflow_test_run_schema["properties"]
    assert "thread_id" in workflow_test_run_schema["properties"]
    assert "needs_user_approval" in workflow_test_run_schema["properties"]
    simulation_schema = schema["components"]["schemas"]["WorkflowEditorSimulationResultView"]
    assert simulation_schema["example"]["workflow_id"] == "release-report"
    assert simulation_schema["example"]["steps"][0]["step_id"] == "draft_release_notes"
    assert "phase_plan" not in simulation_schema["example"]

    simulate_request_example = schema["paths"]["/api/workflows/{workflow_id}/simulate"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["examples"][0]
    assert simulate_request_example["task_envelope"]["requested_workflow_id"] == "release-report"

    test_run_request_example = schema["paths"]["/api/workflows/{workflow_id}/test-run"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["examples"][0]
    assert test_run_request_example["project_id"] == "proj_42"

    step_test_run_request_example = schema["paths"][
        "/api/workflows/{workflow_id}/steps/{step_id}/test-run"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]["examples"][0]
    assert step_test_run_request_example["task_envelope"]["metadata"]["source"] == "step_test_run"
    assert (
        schema["paths"]["/api/tools/{tool_id}/history"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ToolHistoryEntriesView"
    )
    assert (
        schema["paths"]["/api/tools/{tool_id}/history/{revision}"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ToolRecordView"
    )
    assert (
        schema["paths"]["/api/skills/{skill_id}/toggle"]["patch"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/SkillMutationResultView"
    )
    assert (
        schema["paths"]["/api/workflows/{workflow_id}/reactions"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["title"]
        == "WorkflowReactionsView"
    )
    assert (
        schema["paths"]["/api/workspaces"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "WorkspacesListView"
    )
    assert (
        schema["paths"]["/api/data-contracts/{contract_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "DataContractRecordView"
    )
    assert (
        schema["paths"]["/api/fields/{field_key}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "FieldRecordView"
    )
    assert (
        schema["paths"]["/api/published-blocks/{block_key}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "PublishedBlockRecordView"
    )
    assert (
        schema["paths"]["/api/guardrails/{guardrail_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "GuardrailRecordView"
    )
    assert (
        schema["paths"]["/api/changelog/stats"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "RegistryStatsView"
    )
    assert (
        schema["paths"]["/api/security/posture"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "SecurityPostureView"
    )
    assert (
        schema["paths"]["/api/mcp"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["title"]
        == "McpConnectorListView"
    )
    assert (
        schema["paths"]["/api/extensions"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["title"]
        == "ExtensionListView"
    )
    assert (
        schema["paths"]["/api/commands"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["title"]
        == "CommandDispatchResultView"
    )
    assert schema["components"]["schemas"]["ToolRecordView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["ToolHistoryEntriesView"].get("additionalProperties")
        is None
    )
    assert schema["components"]["schemas"]["AuthUserView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["AuthTokenRecordView"].get("additionalProperties") is None
    )
    assert schema["components"]["schemas"]["AuthAccountView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["AuthServiceAccountView"].get("additionalProperties")
        is None
    )
    assert schema["components"]["schemas"]["DatasetRecordView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["WorkspaceRecordView"].get("additionalProperties") is None
    )
    assert (
        schema["components"]["schemas"]["DataContractRecordView"].get("additionalProperties")
        is None
    )
    assert schema["components"]["schemas"]["FieldRecordView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["PublishedBlockRecordView"].get("additionalProperties")
        is None
    )
    assert (
        schema["components"]["schemas"]["GuardrailRecordView"].get("additionalProperties") is None
    )
    assert schema["components"]["schemas"]["McpConnectorView"].get("additionalProperties") is None
    assert (
        schema["components"]["schemas"]["DatasetConnectionView"].get("additionalProperties") is None
    )
    assert (
        schema["components"]["schemas"]["DatasetQueryTemplateView"].get("additionalProperties")
        is None
    )
    assert (
        schema["components"]["schemas"]["WorkflowTriggerEntrypointView"].get("additionalProperties")
        is None
    )
    assert (
        schema["components"]["schemas"]["WorkflowProviderTriggerView"].get("additionalProperties")
        is None
    )


def _rewrite_component_refs(node: object) -> object:
    if isinstance(node, dict):
        rewritten = deepcopy(node)
        ref = rewritten.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            rewritten["$ref"] = f"urn:component:{ref.split('/')[-1]}"
        for key, value in list(rewritten.items()):
            rewritten[key] = _rewrite_component_refs(value)
        return rewritten
    if isinstance(node, list):
        return [_rewrite_component_refs(value) for value in node]
    return node


def _openapi_component_registry(spec: dict) -> Registry:
    components = (spec.get("components") or {}).get("schemas") or {}
    resources = [
        (f"urn:component:{name}", DRAFT202012.create_resource(_rewrite_component_refs(schema)))
        for name, schema in components.items()
    ]
    return Registry().with_resources(resources)


def test_internal_operation_examples_validate_against_response_schemas(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()
    registry = _openapi_component_registry(schema)
    failures: list[tuple[str, str, str, str]] = []

    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            for status_code, response in (operation.get("responses") or {}).items():
                if not str(status_code).startswith(("2", "3")):
                    continue
                content = (response or {}).get("content") or {}
                json_content = content.get("application/json")
                if not isinstance(json_content, dict):
                    continue
                example = json_content.get("example")
                response_schema = json_content.get("schema")
                if example is None or not isinstance(response_schema, dict):
                    continue
                try:
                    Draft202012Validator(
                        _rewrite_component_refs(response_schema),
                        registry=registry,
                    ).validate(example)
                except Exception as exc:
                    failures.append(
                        (method.upper(), path, str(status_code), str(exc).splitlines()[0])
                    )

    assert not failures, failures


def test_internal_json_success_responses_publish_operation_examples(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()
    missing: list[tuple[str, str, str]] = []

    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            for status_code, response in (operation.get("responses") or {}).items():
                if not str(status_code).startswith(("2", "3")):
                    continue
                json_content = ((response or {}).get("content") or {}).get("application/json")
                if not isinstance(json_content, dict):
                    continue
                if "example" not in json_content and "examples" not in json_content:
                    missing.append((method.upper(), path, str(status_code)))

    assert not missing, missing
    assert (
        schema["components"]["schemas"]["ExtensionSummaryView"].get("additionalProperties") is None
    )
    assert (
        schema["components"]["schemas"]["WorkflowDefinitionChangeResultView"].get(
            "additionalProperties"
        )
        is None
    )


def test_internal_mutation_operation_examples_match_runtime_semantics(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()

    delete_checks = [
        ("/api/agents/{agent_id}", "delete", "agent_id", "release_writer"),
        ("/api/skills/{skill_id}", "delete", "skill_id", "draft_release_notes"),
        ("/api/tools/{tool_id}", "delete", "tool_id", "github_issue_lookup"),
    ]
    for path, method, id_field, expected_id in delete_checks:
        example = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "example"
        ]
        assert example["status"] == "deleted"
        assert example[id_field] == expected_id
        assert "enabled" not in example

    toggle_checks = [
        ("/api/agents/{agent_id}/toggle", "patch", "agent_id", "release_writer"),
        ("/api/skills/{skill_id}/toggle", "patch", "skill_id", "draft_release_notes"),
        ("/api/tools/{tool_id}/toggle", "patch", "tool_id", "github_issue_lookup"),
    ]
    for path, method, id_field, expected_id in toggle_checks:
        example = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "example"
        ]
        assert example["status"] == "toggled"
        assert example[id_field] == expected_id
        assert example["enabled"] is True

    clone_checks = [
        (
            "/api/agents/{agent_id}/clone",
            "post",
            {
                "status": "cloned",
                "agent_id": "release_writer_copy",
                "source_agent_id": "release_writer",
            },
        ),
        (
            "/api/skills/{skill_id}/clone",
            "post",
            {
                "status": "cloned",
                "skill_id": "draft_release_notes_copy",
                "source_skill_id": "draft_release_notes",
            },
        ),
        (
            "/api/tools/{tool_id}/clone",
            "post",
            {
                "status": "cloned",
                "tool_id": "github_issue_lookup_copy",
                "source_tool_id": "github_issue_lookup",
            },
        ),
    ]
    for path, method, expected in clone_checks:
        example = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "example"
        ]
        for key, value in expected.items():
            assert example[key] == value

    restore_example = schema["paths"]["/api/guardrails/{guardrail_id}/restore"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]
    assert restore_example["status"] == "restored"
    assert restore_example["guardrail_id"] == "guardrail_secret_scan"
    assert restore_example["restored_from_entry_id"] == "72"


def test_internal_mutation_request_examples_exist_and_validate(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_DOCS_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-internal.json").json()
    registry = _openapi_component_registry(schema)
    mutation_paths = [
        ("post", "/api/agents"),
        ("put", "/api/agents/{agent_id}"),
        ("patch", "/api/agents/{agent_id}"),
        ("post", "/api/agents/{agent_id}/clone"),
        ("post", "/api/agents/{agent_id}/restore"),
        ("put", "/api/agents/{agent_id}/skills/{skill_id}/prompt"),
        ("post", "/api/skills"),
        ("put", "/api/skills/{skill_id}"),
        ("patch", "/api/skills/{skill_id}"),
        ("post", "/api/skills/{skill_id}/clone"),
        ("post", "/api/skills/{skill_id}/restore"),
        ("post", "/api/tools"),
        ("put", "/api/tools/{tool_id}"),
        ("patch", "/api/tools/{tool_id}"),
        ("post", "/api/tools/{tool_id}/clone"),
        ("post", "/api/tools/{tool_id}/restore"),
        ("post", "/api/guardrails"),
        ("put", "/api/guardrails/{guardrail_id}"),
        ("patch", "/api/guardrails/{guardrail_id}"),
        ("put", "/api/guardrails/{guardrail_id}/agents"),
        ("post", "/api/guardrails/{guardrail_id}/restore"),
    ]

    for method, path in mutation_paths:
        operation = schema["paths"][path][method]
        json_body = operation["requestBody"]["content"]["application/json"]
        if "example" in json_body:
            example = json_body["example"]
        else:
            examples = json_body.get("examples") or {}
            assert examples, (method, path)
            first = next(iter(examples.values()))
            example = first["value"]
        assert example not in ({}, [], None), (method, path)
        Draft202012Validator(
            _rewrite_component_refs(json_body["schema"]),
            registry=registry,
        ).validate(example)


def test_public_openapi_auth_required_returns_auth_failure(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="1",
        FS_LOCAL_AUTH_ENABLED="1",
    )
    from core.entrypoints.api import route_registration

    monkeypatch.setitem(
        client.app.state.runtime_container.service_factories, "db", lambda: object()
    )
    monkeypatch.setattr(
        route_registration, "_local_auth_bootstrap_required", lambda bl=None, **_kwargs: False
    )

    response = client.get("/openapi-public.json")
    assert response.status_code == 401
    assert response.json()["error"] == "Authentication required"


def test_internal_openapi_auth_required_returns_auth_failure(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_INTERNAL_ENABLED="1",
        FS_OPENAPI_INTERNAL_REQUIRE_AUTH="1",
        FS_LOCAL_AUTH_ENABLED="1",
    )
    from core.entrypoints.api import route_registration

    monkeypatch.setitem(
        client.app.state.runtime_container.service_factories, "db", lambda: object()
    )
    monkeypatch.setattr(
        route_registration, "_local_auth_bootstrap_required", lambda bl=None, **_kwargs: False
    )

    response = client.get("/openapi-internal.json")
    assert response.status_code == 401
    assert response.json()["error"] == "Authentication required"


def test_public_success_responses_have_non_empty_typed_schemas(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-public.json").json()
    for path, method in PUBLIC_PATHS.items():
        operation = schema["paths"][path][method]
        success_codes = [code for code in operation["responses"] if code.startswith("2")]
        assert success_codes
        for status_code in success_codes:
            if path == "/api/events/stream":
                assert "text/event-stream" in operation["responses"][status_code].get("content", {})
                continue
            response_schema = (
                operation["responses"][status_code]
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            assert response_schema
            assert response_schema != {}
            if "$ref" in response_schema:
                assert (
                    not response_schema["$ref"].endswith("View")
                    or path == "/api/jobs/{job_id}/approval"
                )


def test_public_openapi_contains_operation_level_examples_and_descriptions(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-public.json").json()

    workflow_run = schema["paths"]["/api/workflow-runs"]["post"]
    assert "MCP" in workflow_run["description"]
    assert set(workflow_run["requestBody"]["content"]) == {"application/json"}
    request_schema = schema["components"]["schemas"]["WorkflowRunRequest"]
    assert set(request_schema["required"]) == {"project_id", "workflow_id"}
    assert set(request_schema["properties"]) == {
        "project_id",
        "workflow_id",
        "inputs",
        "client_context",
        "test_mode",
        "files",
    }
    assert request_schema["additionalProperties"] is False

    jobs_get = schema["paths"]["/api/jobs"]["get"]
    assert jobs_get["responses"]["200"]["description"] == (
        "Paginated list of jobs that match the requested filters."
    )
    jobs_example = jobs_get["responses"]["200"]["content"]["application/json"]["example"]
    assert jobs_example["jobs"][0]["job_id"] == "job-987"
    assert jobs_example["jobs"][0]["status"] == "in_progress"
    assert "attention-board" not in str(jobs_get["description"]).lower()

    job_detail = schema["paths"]["/api/jobs/{job_id}"]["get"]
    assert job_detail["responses"]["200"]["description"] == (
        "Current top-level job state and supporting execution context."
    )
    assert (
        job_detail["responses"]["200"]["content"]["application/json"]["example"]["approval_state"]
        == "pending"
    )
    workflow_run_example = job_detail["responses"]["200"]["content"]["application/json"]["example"][
        "workflow_run"
    ]
    assert workflow_run_example["schema_version"] == "1"
    assert workflow_run_example["status"] == "awaiting_approval"
    assert "stable integration projection" in job_detail["description"].lower()
    assert "WorkflowRunStateV1" in schema["components"]["schemas"]

    cancel_job = schema["paths"]["/api/jobs/{job_id}/cancel"]["post"]
    assert cancel_job["responses"]["200"]["description"] == (
        "Cancellation request accepted for the specified job."
    )
    assert cancel_job["responses"]["200"]["content"]["application/json"]["example"] == {
        "job_id": "job-987",
        "cancelled": True,
    }

    approval = schema["paths"]["/api/approvals/{thread_id}"]["post"]
    params = {param["name"]: param for param in approval["parameters"]}
    assert set(params) == {"thread_id", "Idempotency-Key"}
    assert params["Idempotency-Key"]["required"] is True
    assert approval["responses"]["202"]["content"]["application/json"]["example"]["thread_id"] == (
        "thread-7"
    )
    assert approval["responses"]["202"]["content"]["application/json"]["example"]["job_id"] == (
        "42"
    )
    assert "Idempotency-Key" in params
    approval_body = approval["requestBody"]["content"]["application/json"]
    assert "approval_id" in approval_body["schema"]["properties"]
    assert "Idempotency-Key in the request header" in approval["description"]
    assert "Idempotency-Key in JSON" not in approval["description"]

    stream = schema["paths"]["/api/events/stream"]["get"]
    stream_parameters = {parameter["name"]: parameter for parameter in stream["parameters"]}
    assert set(stream_parameters) == {"job_id", "cursor", "Last-Event-ID"}
    assert stream_parameters["Last-Event-ID"]["in"] == "header"
    assert "opaque public-state marker" in stream_parameters["cursor"]["description"]
    assert "takes precedence" in stream_parameters["Last-Event-ID"]["description"]
    for status_code in ("400", "403", "404", "409", "410", "429", "503"):
        assert status_code in stream["responses"]
    assert "Retry-After" in stream["responses"]["429"]["headers"]


def test_public_openapi_uses_strict_token_approval_contract(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-public.json").json()
    decision = schema["paths"]["/api/approvals/{thread_id}"]["post"]
    parameters = {item["name"]: item for item in decision["parameters"]}
    assert set(parameters) == {"thread_id", "Idempotency-Key"}
    assert parameters["Idempotency-Key"]["required"] is True
    body = decision["requestBody"]
    assert body["required"] is True
    decision_schema = body["content"]["application/json"]["schema"]
    assert "approval_id" in decision_schema["required"]
    assert "selected_stories" not in decision_schema["properties"]
    assert "override_content" not in decision_schema["properties"]
    # Runtime accepts a valid ``approved`` decision that also carries an
    # optional action, so the public contract must not use exclusive oneOf.
    assert decision_schema["anyOf"] == [
        {"required": ["approved"]},
        {"required": ["action"]},
    ]
    assert "oneOf" not in decision_schema

    gate_schema = schema["paths"]["/api/jobs/{job_id}/approval"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert gate_schema == {"$ref": "#/components/schemas/TokenApprovalGateV1"}
    token_gate = schema["components"]["schemas"]["TokenApprovalGateV1"]
    assert "approval_id" in token_gate["required"]
    assert token_gate["properties"]["approval_id"] == {"type": "string"}
    assert "payload" not in token_gate["properties"]
    assert "payload" not in token_gate["required"]
    assert token_gate["additionalProperties"] is False
    for name, limit in {
        "gate_kind": 128,
        "recovery_kind": 128,
        "approve_label": 255,
        "reject_label": 255,
    }.items():
        assert token_gate["properties"][name] == {"type": ["string", "null"], "maxLength": limit}
    for name in (
        "comment_required_on_approve",
        "comment_required_on_reject",
        "override_content_supported",
    ):
        assert token_gate["properties"][name] == {"type": "boolean"}
    assert (
        not {"external_effect", "source_ref", "technical_details"} & token_gate["properties"].keys()
    )
    approval_body = body["content"]["application/json"]
    assert "external_actor" in approval_body["schema"]["properties"]
    assert "explicit approved or action" in approval_body["description"].lower()

    catalog = schema["paths"]["/api/projects/{project_id}/workflow-catalog"]["get"]
    assert catalog["responses"]["200"]["description"] == (
        "Integration-facing workflow catalog for the requested project."
    )
    assert (
        "validation"
        not in str(catalog["responses"]["200"]["content"]["application/json"]["example"]).lower()
    )

    entrypoints = schema["paths"]["/api/projects/{project_id}/workflow-entrypoints"]["get"]
    assert entrypoints["responses"]["200"]["description"] == (
        "Callable workflow entrypoints available for the requested project."
    )
    assert (
        entrypoints["responses"]["200"]["content"]["application/json"]["example"]["entrypoints"][0][
            "entry_key"
        ]
        == "release-report-webhook"
    )

    contract = schema["paths"]["/api/projects/{project_id}/workflows/{workflow_id}/contract"]["get"]
    assert contract["responses"]["200"]["description"] == (
        "Machine-readable workflow contract for the requested workflow."
    )
    contract_example = contract["responses"]["200"]["content"]["application/json"]["example"]
    assert contract_example["interface"]["input"]["required"] == ["release_version"]


def test_public_component_schemas_have_human_readable_titles_and_descriptions(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-public.json").json()
    components = schema["components"]["schemas"]

    expected = {
        "WorkflowRunRequest": "Workflow Run Request",
        "WorkflowRunResult": "Workflow Run Result",
        "JobListResponse": "Job List Response",
        "JobListItem": "Job List Item",
        "JobDetailResponse": "Job Detail Response",
        "JobCancellationResult": "Job Cancellation Result",
        "ApprovalDecisionResult": "Approval Decision Result",
        "WorkflowCatalogResponse": "Workflow Catalog Response",
        "WorkflowCatalogItem": "Workflow Catalog Item",
        "WorkflowEntrypointsResponse": "Workflow Entrypoints Response",
        "WorkflowEntrypoint": "Workflow Entrypoint",
        "WorkflowContractResponse": "Workflow Contract Response",
    }

    for schema_name, title in expected.items():
        component = components[schema_name]
        assert component.get("title") == title, (
            f"{schema_name}: expected title {title!r}, got {component.get('title')!r}; "
            f"component keys={sorted(component)}"
        )
        assert component["description"]
        assert not component["title"].endswith("View")
    detail = components["JobDetailResponse"]
    assert detail["type"] == "object"
    assert {"id", "status", "cost", "workflow_run"} <= set(detail["properties"])
    assert {"$ref": "#/components/schemas/JobCostSummaryView"} in detail["properties"]["cost"][
        "anyOf"
    ]
    assert {"$ref": "#/components/schemas/WorkflowRunStateV1"} in detail["properties"][
        "workflow_run"
    ]["anyOf"]
    assert "cost" not in detail.get("required", [])
    assert "JobCancelAcceptedView" not in components
    assert "JobsListView" not in components


def test_public_schema_refs_use_public_aliases(monkeypatch):
    client, _module = _create_client(
        monkeypatch,
        FS_OPENAPI_PUBLIC_ENABLED="1",
        FS_OPENAPI_PUBLIC_REQUIRE_AUTH="0",
    )

    schema = client.get("/openapi-public.json").json()

    assert (
        schema["paths"]["/api/jobs/{job_id}/cancel"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/JobCancellationResult"
    )
    assert (
        schema["paths"]["/api/jobs"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/JobListResponse"
    )
    assert (
        schema["paths"]["/api/projects/{project_id}/workflow-catalog"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WorkflowCatalogResponse"
    )


def test_public_api_cancel_is_json_first_even_without_accept_header():
    class _Payload:
        project_id = "proj-1"

    class _JobCommandService:
        def cancel_job(self, *args, **kwargs):
            return _Payload()

    app = FastAPI()
    app.include_router(
        create_jobs_control_router(
            project_command_service=lambda: None,
            approval_command_service=lambda: None,
            job_command_service=lambda: _JobCommandService(),
            request_scope=lambda request, **kwargs: None,
            service_error_response=lambda exc: None,
            api_error_response=lambda exc: None,
            logger=None,
        )
    )

    response = TestClient(app).post("/api/jobs/job-1/cancel", follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "job_id": "job-1"}
    assert "location" not in response.headers
    assert TestClient(app).post("/jobs/job-1/cancel", follow_redirects=False).status_code == 404
    assert TestClient(app).post("/jobs/job-1/restart", follow_redirects=False).status_code == 404
