"""Standalone preflight catches authoring hazards without executing bundle text."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
import yaml

from tests.unit.workflow_migration_skill_helpers import SKILL_ROOT, convert_n8n


def invoke(tmp_path, bundle, *, raw=False, report=False):
    scripts = tmp_path / "standalone"
    shutil.copytree(SKILL_ROOT / "scripts", scripts)
    source = tmp_path / "source.yaml"
    source.write_text(bundle if raw else yaml.safe_dump(bundle), encoding="utf-8")
    output = tmp_path / "report.json"
    command = [sys.executable, "-I", str(scripts / "check_bundle.py"), str(source)]
    if report:
        command.extend(["--report", str(output)])
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    return result, output


def converted_bundle():
    cases = json.loads((SKILL_ROOT / "assets/source_examples/n8n_http_branches.json").read_text())
    result = convert_n8n(
        json.dumps(cases[0]["workflow"]),
        workflow_id="preflight_contract",
        object_response_nodes=[1],
    )
    assert result["status"] == "converted", result["issues"]
    return yaml.safe_load(result["bundle_yaml"])


def bundle_with(step, edges=None):
    bundle = converted_bundle()
    workflow = bundle["workflows"][0]
    workflow["steps"] = [step]
    workflow["execution_graph"]["edges"] = edges or []
    return bundle


def test_real_converter_bundle_passes_preflight(tmp_path):
    bundle = converted_bundle()
    assert "edges" not in bundle["workflows"][0]
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["diagnostics"] == []


@pytest.mark.parametrize("example", ["maps_email_2567", "maps_email_5385"])
def test_existing_bundled_examples_allow_normal_yaml_aliases(tmp_path, example):
    source = (SKILL_ROOT / "assets/adapted_examples" / example / "workflow.yaml").read_text()
    assert "&id" in source and "*id" in source
    result, _ = invoke(tmp_path, source, raw=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runtime_verified"] is False


def test_aliases_cannot_multiply_workflow_analysis_without_a_bound(tmp_path):
    bundle = converted_bundle()
    workflow = bundle["workflows"][0]
    workflow["steps"] = [{"step_kind": "continue"}] * 100
    bundle["workflows"] = [workflow] * 300
    source = yaml.safe_dump(bundle)
    assert len(source) < 1_000_000
    result, _ = invoke(tmp_path, source, raw=True)
    assert result.returncode == 2
    assert "Cannot parse a bounded UTF-8 YAML bundle." in result.stderr


def test_shared_acyclic_alias_dag_is_not_expanded_as_a_tree(tmp_path):
    bundle = converted_bundle()
    shared = []
    for _ in range(24):
        shared = [shared, shared]
    bundle["metadata"]["untrusted"] = shared
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 0, result.stderr


def test_document_unique_node_count_is_bounded(tmp_path):
    source = "metadata:\n  padding:\n" + "  - []\n" * 100_001
    assert len(source) < 1_000_000
    result, _ = invoke(tmp_path, source, raw=True)
    assert result.returncode == 2
    assert "Cannot parse a bounded UTF-8 YAML bundle." in result.stderr


def test_excessive_document_depth_is_rejected_without_recursion(tmp_path):
    bundle = converted_bundle()
    nested = []
    for _ in range(80):
        nested = [nested]
    bundle["metadata"]["untrusted"] = nested
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 2
    assert "Cannot parse a bounded UTF-8 YAML bundle." in result.stderr


def edge(outcome="success", target="next"):
    return {
        "source": {"kind": "step", "id": "first"},
        "outcome": {"name": outcome},
        "target": {"kind": "step", "id": target},
    }


def test_preflight_rejects_duplicate_outcome_even_when_targets_differ(tmp_path):
    bundle = bundle_with(
        {"step_id": "first", "step_kind": "connector"}, [edge(), edge(target="other")]
    )
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["runtime_verified"] is False
    assert report["target_validation"] == "required"
    assert [row["code"] for row in report["diagnostics"]] == ["duplicate_source_outcome"]


@pytest.mark.parametrize(
    ("step", "edges", "code"),
    [
        ({"step_id": "first", "step_kind": "continue"}, [edge()], "continue_has_outgoing_edge"),
        (
            {"step_kind": "transform", "override": {"transform": {"input": {"source": "state"}}}},
            [],
            "transform_bare_state_source",
        ),
    ],
)
def test_preflight_catches_definite_authoring_errors(tmp_path, step, edges, code):
    result, _ = invoke(tmp_path, bundle_with(step, edges))
    assert result.returncode == 1, result.stderr
    assert [(row["code"], row["severity"]) for row in json.loads(result.stdout)["diagnostics"]] == [
        (code, "error")
    ]


@pytest.mark.parametrize(
    "step",
    [
        {"step_kind": "connector", "output_mapping_jsonb": {"result.metrics.2026": "state.metric"}},
        {"step_kind": "connector", "output_mapping_jsonb": {"rows.0.id": "state.id"}},
        {
            "step_kind": "connector",
            "override": {"connector": {"output_mapping": {"rows.12": "state.row"}}},
        },
    ],
    ids=["legitimate_numeric_object_key", "possible_list_index", "override_possible_list_index"],
)
def test_numeric_selector_warns_without_claiming_unverified_output_shape(tmp_path, step):
    result, _ = invoke(tmp_path, bundle_with(step))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["runtime_verified"] is False
    assert [(row["code"], row["severity"]) for row in report["diagnostics"]] == [
        ("connector_output_numeric_segment", "warning")
    ]
    assert "Verify the output type" in report["diagnostics"][0]["message"]


@pytest.mark.parametrize("owner", ["definitions", "authoring_definitions"])
def test_skill_input_source_is_not_a_portable_contract_field(tmp_path, owner):
    bundle = bundle_with({"step_kind": "agent"})
    definitions = {"skills": [{"expected_inputs": [{"name": "body", "source": "state.body"}]}]}
    if owner == "definitions":
        bundle["definitions"] = definitions
    else:
        bundle["metadata"]["authoring_definitions"] = definitions
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "skill_input_source_unsupported"


@pytest.mark.parametrize(
    ("required", "selector", "expected"),
    [(True, "state.email_rows", 1), (False, "state.email_rows", 0), (True, "state.messages", 0)],
)
def test_messages_warning_requires_business_contract_and_explicit_remapping(
    tmp_path, required, selector, expected
):
    bundle = bundle_with(
        {
            "step_kind": "agent",
            "skill_id": "summarize",
            "input_mapping_jsonb": {"messages": selector},
        }
    )
    bundle["metadata"] = {
        "authoring_definitions": {
            "skills": [
                {
                    "skill_id": "summarize",
                    "expected_inputs": [{"name": "messages", "required": required}],
                }
            ]
        }
    }
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 0, result.stderr
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert len(diagnostics) == expected
    if expected:
        assert diagnostics[0]["code"] == "agent_messages_runtime_collision"
        assert diagnostics[0]["severity"] == "warning"


def test_explicit_empty_agent_mapping_suppresses_override_fallback(tmp_path):
    bundle = bundle_with(
        {
            "step_kind": "agent",
            "skill_id": "summarize",
            "input_mapping_jsonb": {},
            "override": {"agent": {"input_mapping": {"messages": "state.email_rows"}}},
        }
    )
    bundle["definitions"] = {
        "skills": [
            {"skill_id": "summarize", "expected_inputs": [{"name": "messages", "required": True}]}
        ]
    }
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["diagnostics"] == []


def test_whole_connector_rows_and_state_field_transform_pass_without_runtime_claim(tmp_path):
    bundle = bundle_with({"step_kind": "connector", "output_mapping_jsonb": {"rows": "state.rows"}})
    bundle["workflows"][0]["steps"].append(
        {"step_kind": "transform", "override": {"transform": {"input": {"source": "state.rows"}}}}
    )
    result, output = invoke(tmp_path, bundle, report=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {
        "status": "passed",
        "runtime_verified": False,
        "target_validation": "required",
        "compatibility_profile": {"audited_target": "target21601831", "core_version": "0.9.9"},
        "diagnostics": [],
    }


@pytest.mark.parametrize(
    ("raw", "exit_code"),
    [
        ("workflows: [invalid", 2),
        ("!!python/object/apply:os.system ['touch SENTINEL']", 2),
        ("- not-a-bundle", 1),
        ("a" * 1_000_001, 2),
        ("loop: &cycle [*cycle]", 2),
    ],
    ids=["syntax", "constructor", "shape", "oversized", "recursive_alias"],
)
def test_untrusted_or_invalid_input_fails_without_echoing_source(tmp_path, raw, exit_code):
    result, _ = invoke(tmp_path, raw, raw=True)
    assert result.returncode == exit_code
    if exit_code == 1:
        assert json.loads(result.stdout)["diagnostics"][0]["code"] == "bundle_shape_invalid"
    else:
        assert "Cannot parse a bounded UTF-8 YAML bundle." in result.stderr
    assert "SENTINEL" not in result.stdout + result.stderr
    assert not (tmp_path / "SENTINEL").exists()


def test_diagnostics_do_not_echo_attacker_controlled_values(tmp_path):
    secret = "SOURCE_SECRET_CANARY"  # pragma: allowlist secret -- test canary
    bundle = bundle_with(
        {"step_kind": "connector", "output_mapping_jsonb": {f"{secret}.0.id": "state.id"}}
    )
    result, _ = invoke(tmp_path, bundle)
    assert result.returncode == 0
    assert secret not in result.stdout + result.stderr


def test_cli_refuses_existing_report_without_overwrite(tmp_path):
    scripts = tmp_path / "standalone"
    shutil.copytree(SKILL_ROOT / "scripts", scripts)
    source = tmp_path / "source.yaml"
    source.write_text(yaml.safe_dump(bundle_with({"step_kind": "continue"})))
    report = tmp_path / "report.json"
    report.write_text("existing")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(scripts / "check_bundle.py"),
            str(source),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert report.read_text() == "existing"
