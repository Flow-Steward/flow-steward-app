"""
Unit tests for the guardrails system.

Tests:
- guardrails/deterministic.py — pattern-based checks
- guardrails/semantic.py — LLM-based checks (mocked)
- guardrails/checker.py — orchestration of checks
- hub_node guardrail integration — routing on violations
- prompt_builder guardrail injection — constraint insertion
- admin API — CRUD endpoints

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_guardrails.py -v
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID

if "langgraph.types" not in sys.modules:
    fake_langgraph = ModuleType("langgraph")
    fake_types = ModuleType("langgraph.types")

    class _FakeCommand:
        # Mirror real langgraph.types.Command (graph, update, resume, goto) so
        # callers passing resume= don't hit TypeError if this stub ever activates.
        def __init__(self, *, graph=None, update=None, resume=None, goto=(), **kwargs):
            self.graph = graph
            self.update = update or {}
            self.resume = resume
            self.goto = goto
            for key, value in kwargs.items():
                setattr(self, key, value)

    fake_types.Command = _FakeCommand
    fake_langgraph.types = fake_types
    sys.modules["langgraph"] = fake_langgraph
    sys.modules["langgraph.types"] = fake_types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_state(**overrides) -> dict:
    """Minimal orchestrator state for guardrail tests."""
    base = {
        "task_id": "test-001",
        "task_summary": "Fix migration issue",
        "task_source": "manual",
        "job_id": 0,
        "project_name": "test-project",
        "project_root": "/target",
        "container_root": "/target",
        "project_config": {"id": 1},
        "mode": "brownfield",
        "complexity": "bugfix",
        "iteration_count": 0,
        "strategy_version": 0,
        "agent_messages": [],
        "phase_stack": [],
        "orchestrator_journal": [],
        "user_approved": None,
        "review_feedback": None,
        "status": "in_progress",
        "messages": [],
        "git_diff": None,
        "outputs": {},
        "guardrail_violations": None,
    }
    base.update(overrides)
    outputs = base.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}
        base["outputs"] = outputs
    if base.get("git_diff") and not outputs.get("git_diff"):
        outputs["git_diff"] = base.get("git_diff")
    if base.get("review_notes") and not outputs.get("review_notes"):
        outputs["review_notes"] = base.get("review_notes")
    raw_messages = base.get("agent_messages")
    if isinstance(raw_messages, dict):
        converted = []
        for idx, (agent_id, payload) in enumerate(raw_messages.items(), start=1):
            if not isinstance(payload, dict):
                continue
            artifacts = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if not isinstance(artifacts, dict):
                artifacts = {}
            if isinstance(payload.get("artifacts"), dict):
                artifacts = dict(payload.get("artifacts") or {})
            for key in ("raw_output", "review_result", "review_verdict"):
                if key in payload and key not in artifacts:
                    artifacts[key] = payload[key]
            converted.append(
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": str(agent_id),
                    "to_agent": "orchestrator",
                    "phase_id": "analysis",
                    "skill": str(payload.get("skill") or "test_skill"),
                    "status": str(payload.get("status") or "success"),
                    "correlation_id": f"test-{agent_id}-{idx}",
                    "artifacts": artifacts,
                    "summary": str(payload.get("summary") or ""),
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            )
        base["agent_messages"] = converted
    elif not isinstance(raw_messages, list):
        base["agent_messages"] = []
    return base


def make_rule(
    rule_id: int = 1,
    display_name: str = "Test Rule",
    mode: str = "deterministic",
    rule_type: str = "forbidden_patterns",
    severity: str = "error",
    config: dict | None = None,
    prompt_text: str = "",
    agent_prompt: str = "Do not break things.",
    enabled: bool = True,
) -> dict:
    """Build a guardrail rule dict (as returned by db_store)."""
    return {
        "id": rule_id,
        "display_name": display_name,
        "description": f"Test rule: {display_name}",
        "enabled": enabled,
        "mode": mode,
        "rule_type": rule_type,
        "severity": severity,
        "config": config or {},
        "prompt_text": prompt_text,
        "agent_prompt": agent_prompt,
        "project_id": None,
    }


class _FakeGuardrailSaveCursor:
    def __init__(self, explicit_id: int) -> None:
        self.explicit_id = explicit_id
        self.rowcount = 0
        self.statements: list[str] = []
        self._fetchone_calls = 0
        self._last_statement = ""

    def execute(self, sql: str, params=None) -> None:
        _ = params
        statement = " ".join(str(sql).split())
        self._last_statement = statement
        self.statements.append(statement)
        if statement.startswith("UPDATE registry_guardrails SET"):
            self.rowcount = 0
            return
        self.rowcount = 1

    def fetchone(self) -> dict[str, str]:
        self._fetchone_calls += 1
        if self._last_statement.startswith("SELECT 1 FROM registry_guardrails"):
            return None
        return {"id": self.explicit_id}

    def fetchall(self) -> list[dict[str, str]]:
        if self._last_statement.startswith("SELECT agent_id FROM registry_agents"):
            return [{"agent_id": "reviewer"}]
        return []


def test_save_guardrail_with_explicit_id_inserts_missing_row_without_existing_record(
    monkeypatch,
) -> None:
    from core.application.registry.db_store import RegistryDBStore

    explicit_id = "11111111-1111-1111-1111-111111111105"
    store = RegistryDBStore("postgresql://unused")
    cursor = _FakeGuardrailSaveCursor(explicit_id)
    logged: dict[str, str] = {}

    @contextmanager
    def _fake_transaction():
        yield cursor

    monkeypatch.setattr(store, "_transaction", _fake_transaction)
    monkeypatch.setattr(store, "_require_guardrail_ownership_schema", lambda: None)
    monkeypatch.setattr(
        store,
        "_log_change_cur",
        lambda _cur, _entity_type, _entity_id, action, _data, _author, **_audit: logged.setdefault(
            "action", action
        ),
    )

    created_id = store.save_guardrail(
        {
            "id": explicit_id,
            "display_name": "Explicit Guardrail Seed Restore",
            "description": "Regression test for explicit-id bootstrap restore.",
            "mode": "deterministic",
            "rule_type": "explicit_guardrail_seed_restore",
            "severity": "warning",
            "agents": ["reviewer"],
        },
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="system",
        actor_type="system",
    )

    sql_trace = "\n".join(cursor.statements)
    assert created_id == str(explicit_id)
    assert "UPDATE registry_guardrails SET" in sql_trace
    assert "INSERT INTO registry_guardrails" in sql_trace
    assert "SELECT setval(" not in sql_trace
    assert logged["action"] == "create"


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------


class TestDeterministicRequiredSections:
    """Test check_required_sections."""

    def test_all_sections_present(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="required_sections",
            config={"sections": ["VERDICT", "SUMMARY"]},
        )
        state = make_state(
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviewer",
                    "to_agent": "orchestrator",
                    "phase_id": "review",
                    "skill": "review_code",
                    "status": "pass",
                    "correlation_id": "corr-cr-1",
                    "artifacts": {"raw_output": "## VERDICT\npass\n\n## SUMMARY\nLooks good."},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        )
        violations = run_deterministic_checks([rule], "reviewer", state)
        assert len(violations) == 0

    def test_missing_section_fails(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="required_sections",
            config={"sections": ["VERDICT", "SUMMARY"]},
        )
        state = make_state(
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviewer",
                    "to_agent": "orchestrator",
                    "phase_id": "review",
                    "skill": "review_code",
                    "status": "pass",
                    "correlation_id": "corr-cr-2",
                    "artifacts": {"raw_output": "## SUMMARY\nLooks good overall. No issues found."},
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        )
        violations = run_deterministic_checks([rule], "reviewer", state)
        assert len(violations) == 1
        assert "VERDICT" in violations[0].message

    def test_structured_verdict_artifact_satisfies_required_section(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="required_sections",
            config={"sections": ["VERDICT"]},
        )
        state = make_state(
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviewer",
                    "to_agent": "orchestrator",
                    "phase_id": "review",
                    "skill": "review_code",
                    "status": "pass",
                    "correlation_id": "corr-cr-structured",
                    "artifacts": {
                        "raw_output": "VERDICT: pass\nLooks good overall. No blocking findings.",
                    },
                    "summary": "done",
                    "errors": [],
                    "timestamps": {
                        "started_at": None,
                        "completed_at": None,
                        "published_at": "2026-03-10T00:00:00Z",
                    },
                }
            ],
        )
        violations = run_deterministic_checks([rule], "reviewer", state)
        assert len(violations) == 0


def _agent_output_message(agent_id: str, raw_output: str, **artifacts) -> dict:
    """Build one agent_message_v1 entry carrying the agent's raw output."""
    return {
        "schema_version": "agent_message_v1",
        "from_agent": agent_id,
        "to_agent": "orchestrator",
        "phase_id": "drafting",
        "skill": "draft_summary",
        "status": "pass",
        "correlation_id": f"corr-{agent_id}-output",
        "artifacts": {"raw_output": raw_output, **artifacts},
        "summary": "done",
        "errors": [],
        "timestamps": {
            "started_at": None,
            "completed_at": None,
            "published_at": "2026-03-10T00:00:00Z",
        },
    }


class TestDeterministicForbiddenPatterns:
    """Test check_forbidden_patterns — regex patterns scanned in the agent output."""

    def test_forbidden_pattern_detected(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="forbidden_patterns",
            config={"patterns": ["TODO:\\s*HACK", "FIXME"]},
        )
        state = make_state(
            agent_messages=[
                _agent_output_message("writer", "Draft ready.\n# TODO: HACK this needs fixing")
            ],
        )
        violations = run_deterministic_checks([rule], "writer", state)
        assert len(violations) == 1
        assert violations[0].rule_type == "forbidden_patterns"
        assert "TODO:\\s*HACK" in violations[0].message
        assert violations[0].details == {"pattern": "TODO:\\s*HACK"}

    def test_every_matching_pattern_is_reported_case_insensitively(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="forbidden_patterns",
            config={"patterns": ["drop table", "FIXME"]},
        )
        state = make_state(
            agent_messages=[_agent_output_message("writer", "DROP TABLE users; -- fixme later")],
        )
        violations = run_deterministic_checks([rule], "writer", state)
        assert [v.details["pattern"] for v in violations] == ["drop table", "FIXME"]

    def test_no_forbidden_patterns_passes(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="forbidden_patterns",
            config={"patterns": ["TODO:\\s*HACK"]},
        )
        state = make_state(
            agent_messages=[_agent_output_message("writer", "Clean weekly summary.")],
        )
        violations = run_deterministic_checks([rule], "writer", state)
        assert len(violations) == 0

    def test_only_the_checked_agent_output_is_scanned(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="forbidden_patterns",
            config={"patterns": ["FIXME"]},
        )
        state = make_state(
            agent_messages=[
                _agent_output_message("researcher", "FIXME: upstream notes"),
                _agent_output_message("writer", "Final summary without markers."),
            ],
        )
        assert run_deterministic_checks([rule], "writer", state) == []
        assert len(run_deterministic_checks([rule], "researcher", state)) == 1

    def test_invalid_regex_is_skipped_without_failing_other_patterns(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_type="forbidden_patterns",
            config={"patterns": ["(unclosed", "FIXME"]},
        )
        state = make_state(
            agent_messages=[_agent_output_message("writer", "FIXME: tidy the intro")],
        )
        violations = run_deterministic_checks([rule], "writer", state)
        assert [v.details["pattern"] for v in violations] == ["FIXME"]

    def test_violation_is_stamped_with_rule_metadata(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(
            rule_id=7,
            display_name="No Placeholders",
            rule_type="forbidden_patterns",
            severity="warning",
            config={"patterns": ["lorem ipsum"]},
        )
        state = make_state(
            agent_messages=[_agent_output_message("writer", "Lorem ipsum dolor sit amet.")],
        )
        violations = run_deterministic_checks([rule], "writer", state)
        assert len(violations) == 1
        assert violations[0].guardrail_id == 7
        assert violations[0].display_name == "No Placeholders"
        assert violations[0].severity == "warning"


class TestDeterministicUnknownRuleType:
    """Test that unknown rule types are silently skipped."""

    def test_unknown_rule_type_skipped(self):
        from core.application.orchestration.guardrails.deterministic import run_deterministic_checks

        rule = make_rule(rule_type="nonexistent_checker")
        state = make_state()
        violations = run_deterministic_checks([rule], "writer", state)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Semantic checks (mocked LLM)
# ---------------------------------------------------------------------------


class TestSemanticChecks:
    """Test run_semantic_checks with mocked LLM."""

    def test_semantic_pass(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        # Parser expects {"results": [...]} with 1-based rule_index
        response = json.dumps(
            {"results": [{"rule_index": 1, "passed": True, "reason": "All changes are relevant."}]}
        )
        fake_llm = make_fake_llm(response)

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=fake_llm
        ):
            rule = make_rule(
                mode="semantic",
                rule_type="no_unrelated_changes",
                prompt_text="Are all changes related to the task?",
            )
            state = make_state(
                git_diff="diff --git a/fix.py b/fix.py\n+fixed",
                agent_messages={"writer": {"raw_output": "Fixed the bug"}},
            )
            violations = run_semantic_checks([rule], "writer", state)
            assert len(violations) == 0

    def test_semantic_fail(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        response = json.dumps(
            {
                "results": [
                    {"rule_index": 1, "passed": False, "reason": "Unrelated UI changes detected."}
                ]
            }
        )
        fake_llm = make_fake_llm(response)

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=fake_llm
        ):
            rule = make_rule(
                mode="semantic",
                rule_type="no_unrelated_changes",
                severity="error",
                prompt_text="Are all changes related to the task?",
            )
            state = make_state(
                git_diff="diff --git a/style.css b/style.css\n+color: red",
                agent_messages={"writer": {"raw_output": "Fixed DB migration"}},
            )
            violations = run_semantic_checks([rule], "writer", state)
            assert len(violations) == 1
            assert violations[0].severity == "error"
            assert "Unrelated" in violations[0].message

    def test_semantic_multiple_rules(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        response = json.dumps(
            {
                "results": [
                    {"rule_index": 1, "passed": True, "reason": "OK"},
                    {"rule_index": 2, "passed": False, "reason": "Missing requirement X"},
                ]
            }
        )
        fake_llm = make_fake_llm(response)

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=fake_llm
        ):
            rules = [
                make_rule(
                    rule_id=1,
                    mode="semantic",
                    rule_type="r1",
                    prompt_text="Check 1",
                    severity="error",
                ),
                make_rule(
                    rule_id=2,
                    mode="semantic",
                    rule_type="r2",
                    display_name="Requirements Coverage",
                    prompt_text="Check 2",
                    severity="error",
                ),
            ]
            state = make_state(agent_messages={"researcher": {"raw_output": "Summary here"}})
            violations = run_semantic_checks(rules, "researcher", state)
            assert len(violations) == 1
            assert violations[0].display_name == "Requirements Coverage"

    def test_semantic_prompt_prefers_draft_context_for_researcher(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Covered"}]}
                    )
                )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            rule = make_rule(
                mode="semantic",
                rule_type="requirements_quality",
                display_name="Requirements Quality",
                prompt_text="Review the produced requirements document for completeness and clarity.",
            )
            state = make_state(
                task_summary="Create settings page and fix primary button label",
                agent_messages={
                    "researcher": {
                        "raw_output": "High-level notes only.",
                        "artifacts": {
                            "draft": (
                                "Build a settings page and ensure the primary button label reads "
                                "'Save changes' everywhere on that screen."
                            ),
                            "outline": "Settings UI plus button-label correction.",
                        },
                    }
                },
            )
            violations = run_semantic_checks([rule], "researcher", state)

        assert violations == []
        assert "Create settings page and fix primary button label" in captured["prompt"]
        assert "requirements document" in captured["prompt"].lower()

    def test_output_addresses_input_uses_llm_for_requirements_artifact(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Compare the original task request with the produced requirements document.",
        )
        state = make_state(
            task_summary="Create settings page and fix primary button label before handoff",
            agent_messages={
                "researcher": {
                    "artifacts": {
                        "draft": (
                            "Build a settings page for authenticated users and ensure the primary "
                            "action button label reads Save changes before handoff."
                        )
                    }
                }
            },
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=make_fake_llm(
                json.dumps({"results": [{"rule_index": 1, "passed": True, "reason": "Covered"}]})
            ),
        ) as mock_model:
            violations = run_semantic_checks([rule], "researcher", state)

        assert violations == []
        mock_model.assert_called_once()

    def test_output_addresses_input_uses_llm_for_generic_artifact_context(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Compare the original task request with the produced implementation evidence.",
        )
        state = make_state(
            task_summary="Create boards API and add list endpoint before handoff",
            agent_messages={
                "facilitator": {
                    "summary": "Child workflow completed successfully.",
                    "artifacts": {
                        "checker_report": (
                            "Verified the boards list endpoint exists, returns board records, "
                            "and is covered by regression tests before handoff."
                        ),
                        "developer_verification": {
                            "implemented": [
                                "boards API",
                                "list endpoint",
                            ]
                        },
                    },
                }
            },
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=make_fake_llm(
                json.dumps({"results": [{"rule_index": 1, "passed": True, "reason": "Covered"}]})
            ),
        ) as mock_model:
            violations = run_semantic_checks([rule], "facilitator", state)

        assert violations == []
        mock_model.assert_called_once()

    def test_output_addresses_input_heuristic_uses_supporting_workflow_evidence(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Check that the workflow output still addresses the original request.",
        )
        state = make_state(
            task_summary="Create boards API and add list endpoint before handoff",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "writer",
                    "to_agent": "orchestrator",
                    "phase_id": "development",
                    "skill": "write_section",
                    "status": "pass",
                    "correlation_id": "test-dev-1",
                    "artifacts": {
                        "raw_output": (
                            "Implemented the boards API and added the list endpoint with "
                            "regression coverage."
                        )
                    },
                    "summary": "Developer completed the boards API work.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                },
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "checker",
                    "to_agent": "orchestrator",
                    "phase_id": "quality",
                    "skill": "review_summary",
                    "status": "pass",
                    "correlation_id": "test-review-1",
                    "artifacts": {
                        "findings": (
                            "Validated the current diff with targeted automated checks. "
                            "No blocking issues found."
                        )
                    },
                    "summary": "Validated the current diff with targeted automated checks.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                },
            ],
        )

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_model:
            violations = run_semantic_checks([rule], "checker", state)

        assert violations == []
        mock_model.assert_called_once()

    def test_output_addresses_input_prompt_adapts_document_rule_without_document_artifact(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Covered"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Compare the original task request with the produced requirements document.",
        )
        state = make_state(
            task_summary="Build a simple notes app with create, edit, delete, and search",
            current_item={
                "id": "STORY-001",
                "title": "Create note",
                "description": "Allow users to create a note with a title and body.",
                "acceptance_criteria": [
                    "POST /notes accepts title and body",
                    "Created notes are persisted",
                    "Created notes are returned from list and search endpoints",
                ],
            },
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "writer",
                    "to_agent": "orchestrator",
                    "phase_id": "development",
                    "skill": "write_section",
                    "status": "pass",
                    "correlation_id": "test-dev-no-summary",
                    "artifacts": {
                        "raw_output": "Implemented POST /notes with body persistence.",
                    },
                    "summary": "Implemented the create note endpoint.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                },
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "checker",
                    "to_agent": "orchestrator",
                    "phase_id": "quality",
                    "skill": "review_summary",
                    "status": "pass",
                    "correlation_id": "test-review-no-summary",
                    "artifacts": {
                        "findings": "Ran tests for note creation and persistence.",
                    },
                    "summary": "Validated note creation tests.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                },
            ],
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            violations = run_semantic_checks([rule], "checker", state)

        assert violations == []
        assert (
            "Compare the original task request with the produced requirements document"
            in captured["prompt"]
        )
        assert "When a Current Scoped Work Item is present" in captured["prompt"]
        assert "Current Scoped Work Item" in captured["prompt"]

    def test_output_addresses_input_heuristic_handles_rework_supporting_evidence(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Check that the workflow output still addresses the original request.",
        )
        state = make_state(
            task_summary=(
                "Backend: Add priority field to task model and persist it in "
                "database, schemas, and repository"
            ),
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "writer",
                    "to_agent": "orchestrator",
                    "phase_id": "development",
                    "skill": "write_section",
                    "status": "pass",
                    "correlation_id": "test-dev-rework-supporting",
                    "artifacts": {
                        "raw_output": (
                            "Implemented STORY-001 by adding a persisted non-negative integer "
                            "priority field to the task ORM, request/response schemas, "
                            "repository create/update flows, and database initialization "
                            "upgrade path. Added a canonical schema artifact at "
                            "db/schema/*.sql."
                        )
                    },
                    "summary": "Implemented task priority across the stack.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                },
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviser",
                    "to_agent": "orchestrator",
                    "phase_id": "rework",
                    "skill": "apply_revision",
                    "status": "pass",
                    "correlation_id": "test-rework-supporting",
                    "artifacts": {
                        "raw_output": (
                            "Closed the non-SQLite schema convergence gap for task priority "
                            "persistence by tightening startup upgrade behavior and adding "
                            "focused regression coverage."
                        )
                    },
                    "summary": "Closed the non-SQLite schema convergence gap.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                },
            ],
        )

        with patch("core.application.orchestration.model_provider.get_chat_model") as mock_model:
            violations = run_semantic_checks([rule], "reviser", state)

        assert violations == []
        mock_model.assert_not_called()

    def test_no_unrelated_changes_additive_diff_still_uses_llm(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        state = make_state(
            mode="greenfield",
            task_summary=(
                "Create a minimal launch-ready settings surface and correct a mislabeled "
                "primary action before handoff"
            ),
            git_diff="""diff --git a/app.py b/app.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/app.py
@@ -0,0 +1,6 @@
+def create_app():
+    return \"ok\"
diff --git a/templates/settings.html b/templates/settings.html
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/templates/settings.html
@@ -0,0 +1,4 @@
+<button>Save changes</button>
""",
            agent_messages={
                "writer": {
                    "artifacts": {
                        "raw_output": "Created a minimal settings surface with a corrected primary action label.",
                    }
                }
            },
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=make_fake_llm(
                json.dumps({"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]})
            ),
        ) as mock_model:
            violations = run_semantic_checks([rule], "writer", state)

        assert violations == []
        mock_model.assert_called_once()

    def test_no_unrelated_changes_greenfield_existing_file_still_uses_llm(self, make_fake_llm):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        state = make_state(
            mode="greenfield",
            task_summary="Create a minimal launch-ready settings surface before handoff",
            git_diff="""diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
""",
            agent_messages={
                "writer": {"artifacts": {"raw_output": "Updated an existing app file."}}
            },
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model",
            return_value=make_fake_llm(
                json.dumps({"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]})
            ),
        ) as mock_model:
            violations = run_semantic_checks([rule], "writer", state)

        assert violations == []
        mock_model.assert_called_once()

    def test_semantic_prompt_includes_scoped_work_item_for_parent_invoke(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        state = make_state(
            mode="greenfield",
            task_summary="Build a simple notes app with create, edit, delete, and search",
            current_item={
                "id": "E1-S1",
                "title": "Initialize project skeleton and homepage scaffold",
                "description": "Create the initial homepage and mount static assets before CRUD stories.",
                "acceptance_criteria": [
                    "GET / returns an HTML homepage scaffold",
                    "Static assets are mounted from the repository assets directory",
                ],
            },
            git_diff="""diff --git a/app/main.py b/app/main.py
index 1111111..2222222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1 +1,5 @@
-from fastapi import FastAPI
+from fastapi import FastAPI
+from fastapi.responses import HTMLResponse
+from fastapi.staticfiles import StaticFiles
""",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "invoke_workflow",
                    "to_agent": "orchestrator",
                    "phase_id": "story_loop",
                    "skill": "invoke_workflow",
                    "status": "pass",
                    "correlation_id": "test-parent-invoke-story-scope",
                    "artifacts": {
                        "git_diff": "PARENT_CHILD_STORY_DIFF",
                        "review_verdict": "pass",
                        "review_report": "VERDICT: pass. Scope limited to the homepage scaffold.",
                        "checker_report": (
                            "Implemented the homepage scaffold and mounted repository static assets."
                        ),
                    },
                    "summary": "Child workflow 'review_cycle' completed successfully.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                }
            ],
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            violations = run_semantic_checks([rule], "invoke_workflow", state)

        assert violations == []
        assert "## Current Scoped Work Item" in captured["prompt"]
        assert "Initialize project skeleton and homepage scaffold" in captured["prompt"]
        assert "GET / returns an HTML homepage scaffold" in captured["prompt"]

    def test_semantic_prompt_prefers_latest_agent_git_diff_over_state_git_diff(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        state = make_state(
            task_summary="Add priority column to tasks table and keep the scope focused",
            git_diff="STALE_ORDERS_ADDED",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviser",
                    "to_agent": "orchestrator",
                    "phase_id": "rework",
                    "skill": "apply_revision",
                    "status": "pass",
                    "correlation_id": "test-rework-stale-git-diff",
                    "artifacts": {
                        "raw_output": (
                            "Removed the unrelated orders DDL from the canonical schema "
                            "artifact and kept only the requested task priority schema work."
                        ),
                        "git_diff": "FRESH_ORDERS_REMOVED",
                    },
                    "summary": "Removed the unrelated orders schema artifact.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                }
            ],
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            violations = run_semantic_checks([rule], "reviser", state)

        assert violations == []
        assert "FRESH_ORDERS_REMOVED" in captured["prompt"]
        assert "STALE_ORDERS_ADDED" not in captured["prompt"]

    def test_semantic_prompt_includes_failed_review_feedback_for_rework(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        state = make_state(
            task_summary="Add priority column to tasks table and migration support",
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviewer",
                    "to_agent": "orchestrator",
                    "phase_id": "review",
                    "skill": "review_summary",
                    "status": "fail",
                    "correlation_id": "test-review-feedback",
                    "artifacts": {
                        "raw_output": (
                            "The branch is not ready because startup seeding is unrelated to "
                            "the requested migration and should be removed."
                        )
                    },
                    "summary": "Review requested removal of unrelated startup seeding.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                },
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviser",
                    "to_agent": "orchestrator",
                    "phase_id": "rework",
                    "skill": "apply_revision",
                    "status": "pass",
                    "correlation_id": "test-rework-cleanup",
                    "artifacts": {
                        "raw_output": (
                            "Removed the unintended startup seeding regression and kept only "
                            "the requested migration behavior."
                        ),
                        "git_diff": "REMOVED_STARTUP_SEEDING",
                    },
                    "summary": "Removed the unrelated startup seeding regression.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                },
            ],
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            violations = run_semantic_checks([rule], "reviser", state)

        assert violations == []
        assert "## Recent Workflow Feedback / Evidence" in captured["prompt"]
        assert "reviewer feedback (status=fail)" in captured["prompt"]
        assert "startup seeding is unrelated to the requested migration" in captured["prompt"]
        assert "Removed the unintended startup seeding regression" in captured["prompt"]

    def test_semantic_prompt_includes_active_guardrail_feedback(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Focused"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="no_unrelated_changes",
            display_name="No Unrelated Changes",
            prompt_text="Check that the implementation stays focused on the requested work.",
        )
        feedback = (
            "## Guardrail Violations\n\n"
            "- **No Unrelated Changes**: Remove out-of-scope CRUD/search surfaces from "
            "the previous attempt; keep only create and list behavior."
        )
        state = make_state(
            task_summary="Build a simple notes app with create, edit, delete, and search",
            current_item={
                "id": "STORY-002",
                "title": "Create and list notes",
                "description": "Implement POST /notes and GET /notes only.",
            },
            feedback=feedback,
            invoke_parent_feedback=(
                "The child workflow added CRUD/search beyond this story; narrow the diff."
            ),
            guardrail_violations=[
                {
                    "rule": "No Unrelated Changes",
                    "rule_type": "no_unrelated_changes",
                    "message": (
                        "CRUD/search additions from the previous attempt are unrelated to "
                        "the current create/list story."
                    ),
                }
            ],
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "writer",
                    "to_agent": "orchestrator",
                    "phase_id": "development",
                    "skill": "write_section",
                    "status": "pass",
                    "correlation_id": "test-active-guardrail-feedback",
                    "artifacts": {
                        "raw_output": (
                            "Removed the previous out-of-scope CRUD/search endpoints and kept "
                            "the create/list notes API."
                        ),
                        "git_diff": "REMOVED_OUT_OF_SCOPE_CRUD_SEARCH",
                    },
                    "summary": "Narrowed the implementation to the active guardrail feedback.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                }
            ],
        )

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=_FakeLLM()
        ):
            violations = run_semantic_checks([rule], "writer", state)

        assert violations == []
        assert "## Active Retry / Parent Feedback" in captured["prompt"]
        assert "Remove out-of-scope CRUD/search surfaces" in captured["prompt"]
        assert "CRUD/search additions from the previous attempt" in captured["prompt"]
        assert "The child workflow added CRUD/search beyond this story" in captured["prompt"]
        assert (
            "If Active Retry / Parent Feedback is present, treat it as the current attempt's "
            "authoritative feedback." in captured["prompt"]
        )
        assert "## Current Scoped Work Item" in captured["prompt"]
        assert "redaction placeholders" in captured["prompt"]
        assert "[INJECTION:...:REDACTED]" in captured["prompt"]

    def test_output_addresses_input_prefers_scoped_item_over_parent_requirements(self):
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        captured: dict[str, str] = {}

        class _FakeResponse:
            def __init__(self, content: str):
                self.content = content

        class _FakeLLM:
            def invoke(self, messages):
                captured["prompt"] = str(messages[-1].content)
                return _FakeResponse(
                    json.dumps(
                        {"results": [{"rule_index": 1, "passed": True, "reason": "Scoped pass"}]}
                    )
                )

        rule = make_rule(
            mode="semantic",
            rule_type="output_addresses_input",
            display_name="Output Addresses Request",
            prompt_text="Check that the branch implements the requested requirements.",
        )
        state = make_state(
            mode="greenfield",
            task_summary="Build a notes app with create, edit, delete, list, and search.",
            current_item={
                "id": "S2",
                "title": "Persistent storage for notes",
                "description": "Persist created notes and support read-by-id.",
                "acceptance_criteria": [
                    "POST /notes stores a note in durable storage",
                    "GET /notes/{note_id} reads the persisted note by identifier",
                    "Listing, editing, deletion, and search are out of scope for S2",
                ],
            },
            agent_messages=[
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "checker",
                    "to_agent": "orchestrator",
                    "phase_id": "quality",
                    "skill": "review_summary",
                    "status": "fail",
                    "correlation_id": "test-scoped-review-feedback",
                    "artifacts": {
                        "review_notes": (
                            "S2 requirements explicitly mark listing, editing, deletion, "
                            "and search as out of scope."
                        ),
                    },
                    "summary": "Review failed because parent-story CRUD/search leaked into S2.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:00:00Z"},
                },
                {
                    "schema_version": "agent_message_v1",
                    "from_agent": "reviser",
                    "to_agent": "orchestrator",
                    "phase_id": "rework",
                    "skill": "apply_revision",
                    "status": "pass",
                    "correlation_id": "test-scoped-rework",
                    "artifacts": {
                        "draft": (
                            "Parent roadmap: the complete notes app eventually supports "
                            "create, edit, delete, list, and search."
                        ),
                        "raw_output": (
                            "Removed out-of-scope list/search/update/delete routes and kept "
                            "persistent create/read-by-id behavior for S2."
                        ),
                        "git_diff": "REMOVED_PARENT_CRUD_SEARCH_FROM_S2",
                    },
                    "summary": "Narrowed S2 to scoped persistence requirements.",
                    "errors": [],
                    "timestamps": {"published_at": "2026-03-10T00:01:00Z"},
                },
            ],
        )

        with (
            patch(
                "core.application.orchestration.guardrails.semantic._heuristic_output_addresses_input",
                return_value=None,
            ),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                return_value=_FakeLLM(),
            ),
        ):
            violations = run_semantic_checks([rule], "reviser", state)

        assert violations == []
        assert "## Current Scoped Work Item" in captured["prompt"]
        assert (
            "Listing, editing, deletion, and search are out of scope for S2" in captured["prompt"]
        )
        assert "## Recent Workflow Feedback / Evidence" in captured["prompt"]
        assert "Review failed because parent-story CRUD/search leaked into S2" in captured["prompt"]
        assert "Parent roadmap" in captured["prompt"]
        assert (
            "Current Scoped Work Item is present, use it as the primary coverage"
            in captured["prompt"]
        )
        assert (
            "statements in the Current Scoped Work Item or latest review feedback are binding"
            in (captured["prompt"])
        )
        assert "broader parent context" in captured["prompt"]
        assert (
            "Do not fail a rule solely because redaction placeholders appear" in captured["prompt"]
        )

    def test_semantic_llm_error_non_fatal(self, make_fake_llm):
        """If LLM returns bad JSON, no violations should be raised (non-fatal)."""
        from core.application.orchestration.guardrails.semantic import run_semantic_checks

        fake_llm = make_fake_llm("This is not JSON at all!")

        with patch(
            "core.application.orchestration.model_provider.get_chat_model", return_value=fake_llm
        ):
            rule = make_rule(mode="semantic", rule_type="test_rule", prompt_text="Some check")
            state = make_state()
            violations = run_semantic_checks([rule], "writer", state)
            # Should not crash, may return empty or best-effort
            assert isinstance(violations, list)


# ---------------------------------------------------------------------------
# Checker (orchestrator of deterministic + semantic)
# ---------------------------------------------------------------------------


class TestGuardrailChecker:
    """Test check_guardrails — full pipeline."""

    def test_returns_none_when_no_rules(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = []

        state = make_state()
        result = check_guardrails("writer", state, mock_store)
        assert result is None

    def test_deterministic_violation_skips_semantic(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(
                rule_id=1,
                mode="deterministic",
                rule_type="forbidden_patterns",
                config={"patterns": ["DROP TABLE"]},
            ),
            make_rule(
                rule_id=2, mode="semantic", rule_type="no_unrelated", prompt_text="Check relevance"
            ),
        ]
        state = make_state(
            agent_messages=[_agent_output_message("writer", "Cleanup plan: DROP TABLE orders;")],
        )

        # Semantic checks should NOT be called since deterministic found errors
        with patch(
            "core.application.orchestration.guardrails.semantic.run_semantic_checks"
        ) as mock_semantic:
            result = check_guardrails("writer", state, mock_store)
            assert result is not None
            assert not result.passed
            assert len(result.violations) >= 1
            mock_semantic.assert_not_called()

    def test_workflow_test_run_skips_semantic_guardrails(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(
                rule_id=2, mode="semantic", rule_type="no_unrelated", prompt_text="Check relevance"
            ),
        ]
        state = make_state(task_source="workflow_test_run")

        with patch(
            "core.application.orchestration.guardrails.semantic.run_semantic_checks"
        ) as mock_semantic:
            result = check_guardrails("writer", state, mock_store)

        assert result is not None
        assert result.passed is True
        assert result.violations == []
        mock_semantic.assert_not_called()

    def test_passed_result_on_clean_state(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(
                rule_type="forbidden_patterns",
                config={"patterns": ["DROP TABLE"]},
            ),
        ]
        # Clean output — no forbidden pattern
        state = make_state(
            agent_messages=[_agent_output_message("writer", "Weekly report is ready.")],
        )
        result = check_guardrails("writer", state, mock_store)
        assert result is not None
        assert result.passed
        assert result.violations == []
        assert result.warnings == []

    def test_project_config_override_merges(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(
                rule_type="forbidden_patterns",
                severity="warning",
                config={"patterns": ["NEVER_PRESENT_MARKER"]},  # default
            ),
        ]
        # Project override: patterns=["lorem ipsum"] replaces the rule default
        state = make_state(
            project_config={
                "id": 1,
                "guardrails_config": {
                    "forbidden_patterns": {"patterns": ["lorem ipsum"]},
                },
            },
            agent_messages=[_agent_output_message("writer", "Lorem ipsum placeholder text.")],
        )
        result = check_guardrails("writer", state, mock_store)
        assert result is not None
        # The project override pattern matches → one non-blocking warning
        assert result.passed is True
        assert len(result.warnings) == 1
        assert result.warnings[0].details == {"pattern": "lorem ipsum"}

    def test_checker_passes_account_scope_to_guardrail_lookup(self):
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = []

        state = make_state(scope_context={"account_id": "acc_team"})
        result = check_guardrails("writer", state, mock_store)

        assert result is None
        mock_store.list_guardrails_for_agent.assert_called_once_with(
            "writer",
            "1",
            account_id="acc_team",
        )

    def test_feedback_text_formatting(self):
        from core.application.orchestration.guardrails.checker import (
            GuardrailResult,
            GuardrailViolation,
        )

        result = GuardrailResult(
            passed=False,
            violations=[
                GuardrailViolation(
                    guardrail_id=1,
                    display_name="Immutable Migrations",
                    rule_type="immutable_paths",
                    severity="error",
                    message="Modified existing migration file",
                ),
            ],
            warnings=[
                GuardrailViolation(
                    guardrail_id=2,
                    display_name="Max Files",
                    rule_type="max_files_changed",
                    severity="warning",
                    message="Changed 25 files",
                ),
            ],
        )
        text = result.feedback_text
        assert "Immutable Migrations" in text
        assert "Modified existing migration file" in text
        assert "Max Files" in text
        assert "Changed 25 files" in text

    def test_to_dicts_serialization(self):
        from core.application.orchestration.guardrails.checker import (
            GuardrailResult,
            GuardrailViolation,
        )

        result = GuardrailResult(
            passed=False,
            violations=[
                GuardrailViolation(
                    guardrail_id=1,
                    display_name="Test",
                    rule_type="test_type",
                    severity="error",
                    message="Bad thing happened",
                ),
            ],
        )
        dicts = result.to_dicts()
        assert len(dicts) == 1
        assert dicts[0]["display_name"] == "Test"
        assert dicts[0]["message"] == "Bad thing happened"
        assert dicts[0]["severity"] == "error"

    def test_exception_returns_none(self):
        """check_guardrails should be non-fatal — exception → None."""
        from core.application.orchestration.guardrails.checker import check_guardrails

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.side_effect = RuntimeError("DB error")

        state = make_state()
        result = check_guardrails("writer", state, mock_store)
        assert result is None


# ---------------------------------------------------------------------------
# PromptBuilder integration
# ---------------------------------------------------------------------------


class TestPromptBuilderGuardrails:
    """Test PromptBuilder._build_guardrail_constraints."""

    def test_builds_constraints_section(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(agent_prompt="Never modify existing migrations."),
            make_rule(rule_id=2, agent_prompt="Keep changes focused."),
        ]

        mock_rm = MagicMock()
        mock_rm._db_store = mock_store

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.return_value = mock_rm
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints("writer", make_state())

        assert "## Critical Constraints" in constraints
        assert "Never modify existing migrations" in constraints
        assert "Keep changes focused" in constraints

    def test_empty_for_no_rules(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = []

        mock_rm = MagicMock()
        mock_rm._db_store = mock_store

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.return_value = mock_rm
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints("checker", make_state())

        assert constraints == ""

    def test_empty_for_no_db_store(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        mock_rm = MagicMock()
        mock_rm._db_store = None

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.return_value = mock_rm
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints("writer", make_state())

        assert constraints == ""

    def test_skips_rules_without_agent_prompt(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(agent_prompt="Keep changes focused."),
            make_rule(rule_id=2, agent_prompt=""),  # empty prompt
            make_rule(rule_id=3, agent_prompt="Always test your code."),
        ]

        mock_rm = MagicMock()
        mock_rm._db_store = mock_store

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.return_value = mock_rm
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints("writer", make_state())

        # Should have 2 constraints, not 3
        assert constraints.count("- ") == 2
        assert "Keep changes focused" in constraints
        assert "Always test your code" in constraints

    def test_build_constraints_uses_scope_account_for_guardrail_lookup(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        mock_store = MagicMock()
        mock_store.list_guardrails_for_agent.return_value = [
            make_rule(agent_prompt="Keep changes focused."),
        ]

        mock_rm = MagicMock()
        mock_rm._db_store = mock_store

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.return_value = mock_rm
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints(
                "writer",
                make_state(scope_context={"account_id": "acc_ops"}),
            )

        assert "Keep changes focused" in constraints
        mock_store.list_guardrails_for_agent.assert_called_once_with(
            "writer",
            1,
            account_id="acc_ops",
        )

    def test_exception_returns_empty_string(self):
        from core.application.orchestration.agents.prompt_builder import PromptBuilder

        with patch("core.application.registry.registry_manager.RegistryManager") as mock_rm_cls:
            mock_rm_cls.instance.side_effect = RuntimeError("RegistryManager not available")
            builder = PromptBuilder()
            constraints = builder._build_guardrail_constraints("writer", make_state())

        assert constraints == ""


class TestGuardrailsAPI:
    """Test /api/guardrails/* endpoints."""

    def _draft_store(self):
        store = self._FakeGuardrailStore()
        gid = store.save_guardrail(
            {
                "display_name": "Draft",
                "enabled": False,
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": []},
                "agents": ["writer"],
            },
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            actor_type="user",
        )
        return store, gid

    def test_partial_guardrail_update_preserves_postgres_project_uuid(self, client):
        from uuid import UUID

        import core.entrypoints.admin.guardrails as routes

        store, gid = self._draft_store()
        project_id = UUID("00000000-0000-0000-0000-000000000731")
        store._guardrails[gid]["project_id"] = project_id
        with patch.object(routes, "_get_db", return_value=store):
            response = client.patch(
                f"/api/guardrails/{gid}",
                json={
                    "display_name": "Проверка",
                    "description": "Unicode edit",
                    "severity": "warning",
                },
            )
            assert response.status_code == 200, response.text
            reopened = client.get(f"/api/guardrails/{gid}").json()
        assert reopened["display_name"] == "Проверка"
        assert reopened["description"] == "Unicode edit"
        assert reopened["severity"] == "warning"
        assert reopened["project_id"] == str(project_id)
        assert reopened["_agents"] == ["writer"]

    @pytest.mark.parametrize("operation", ["regex", "enable", "toggle"])
    def test_guardrail_write_validation_errors_preserve_disabled_draft(self, client, operation):
        import core.entrypoints.admin.guardrails as routes

        store, gid = self._draft_store()
        with patch.object(routes, "_get_db", return_value=store):
            if operation == "toggle":
                response = client.patch(f"/api/guardrails/{gid}/toggle")
            else:
                payload = (
                    {"config": {"patterns": ["("]}} if operation == "regex" else {"enabled": True}
                )
                response = client.patch(f"/api/guardrails/{gid}", json=payload)
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "validation_error"
            assert response.json()["details"]["errors"]
            reopened = client.get(f"/api/guardrails/{gid}").json()
        assert reopened["enabled"] is False
        assert reopened["config"] == {"patterns": []}

    @pytest.mark.parametrize("agent_id", ["missing-agent", "foreign-agent"])
    def test_rejected_agent_assignment_is_typed_and_keeps_mapping(self, client, agent_id):
        import core.entrypoints.admin.guardrails as routes

        store, gid = self._draft_store()
        with (
            patch.object(routes, "_get_db", return_value=store),
            patch.object(
                store,
                "set_guardrail_agents",
                side_effect=ValueError("guardrail agents are not available in the owning account"),
            ),
        ):
            response = client.put(f"/api/guardrails/{gid}/agents", json={"agent_ids": [agent_id]})
            assert response.status_code == 400, response.text
            reopened = client.get(f"/api/guardrails/{gid}").json()
        assert reopened["_agents"] == ["writer"]

    class _FakeGuardrailStore:
        def __init__(self) -> None:
            self._guardrails: dict[int, dict] = {}
            self._next_id = 1

        def list_guardrails(self, project_id=None, enabled_only: bool = False, *, account_id=None):
            rows = []
            for guardrail in self._guardrails.values():
                if project_id is not None and guardrail.get("project_id") != project_id:
                    continue
                if enabled_only and not guardrail.get("enabled", True):
                    continue
                rows.append(dict(guardrail))
            return sorted(rows, key=lambda row: (row.get("sort_order", 0), row.get("id", 0)))

        def get_guardrail(self, guardrail_id: int, *, account_id=None):
            guardrail = self._guardrails.get(int(guardrail_id))
            return dict(guardrail) if guardrail else None

        def save_guardrail(
            self,
            data: dict,
            author: str = "admin",
            *,
            account_id: str,
            actor_id: str,
            actor_type: str,
        ) -> int:
            _ = (account_id, actor_id, actor_type)
            self.last_save_author = author
            guardrail_id = int(data.get("id") or self._next_id)
            self._next_id = max(self._next_id, guardrail_id + 1)
            existing = self._guardrails.get(guardrail_id, {})
            agents = data.get("agents", existing.get("_agents", []))
            guardrail = {
                "id": guardrail_id,
                "display_name": data.get("display_name", existing.get("display_name", "")),
                "description": data.get("description", existing.get("description", "")),
                "enabled": data.get("enabled", existing.get("enabled", True)),
                "mode": data.get("mode", existing.get("mode", "deterministic")),
                "rule_type": data.get("rule_type", existing.get("rule_type", "custom")),
                "severity": data.get("severity", existing.get("severity", "error")),
                "config": data.get("config", existing.get("config", {})),
                "prompt_text": data.get("prompt_text", existing.get("prompt_text", "")),
                "agent_prompt": data.get("agent_prompt", existing.get("agent_prompt", "")),
                "project_id": data.get("project_id", existing.get("project_id")),
                "sort_order": data.get("sort_order", existing.get("sort_order", 0)),
                "_agents": list(agents or []),
            }
            self._guardrails[guardrail_id] = guardrail
            return guardrail_id

        def delete_guardrail(
            self,
            guardrail_id: int,
            author: str = "",
            *,
            account_id: str,
            actor_id: str,
            actor_type: str,
        ) -> bool:
            _ = (author, account_id, actor_id, actor_type)
            return self._guardrails.pop(int(guardrail_id), None) is not None

        def toggle_guardrail(
            self, guardrail_id: int, *, account_id: str, actor_id: str, actor_type: str
        ):
            _ = (account_id, actor_id, actor_type)
            guardrail = self._guardrails.get(int(guardrail_id))
            if not guardrail:
                return None
            guardrail["enabled"] = not bool(guardrail.get("enabled", True))
            return guardrail["enabled"]

        def set_guardrail_agents(
            self,
            guardrail_id: int,
            agent_ids: list[str],
            *,
            account_id: str,
            actor_id: str,
            actor_type: str,
        ) -> None:
            _ = (account_id, actor_id, actor_type)
            guardrail = self._guardrails[int(guardrail_id)]
            guardrail["_agents"] = list(agent_ids)

    @pytest.fixture(scope="class")
    def client(self):
        try:
            from unittest.mock import patch

            from fastapi.testclient import TestClient

            import core.entrypoints.admin.guardrails as guardrails_api
            from core.entrypoints.api.app_factory import create_app

            fake_registry = MagicMock()
            fake_registry.db_store = self._FakeGuardrailStore()
            app = create_app()
            app.state.local_auth_enabled_override = lambda: False
            app.state.external_auth_enabled_override = lambda: False

            with (
                patch.object(guardrails_api, "_get_registry", return_value=fake_registry),
                patch("core.application.auth.enforce.local_auth_enabled", return_value=False),
                patch("core.application.auth.enforce.auth_enforced", return_value=False),
            ):
                yield TestClient(app, raise_server_exceptions=False)
        except Exception as e:
            pytest.skip(f"Could not create TestClient: {e}")

    def test_list_guardrails(self, client):
        resp = client.get("/api/guardrails")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_list_guardrails_includes_extension_runtime_projection_rows(self, client):
        import core.entrypoints.admin.guardrails as guardrails_api

        fake_db = MagicMock()
        fake_db.list_guardrails.return_value = []
        projection_row = {
            "id": "guardrail_extension_demo",
            "display_name": "Extension guardrail",
            "mode": "deterministic",
            "rule_type": "extension_policy",
            "severity": "warning",
            "enabled": True,
            "config": {},
            "_agents": [],
            "source_install_id": "inst-extension-demo",
        }

        with (
            patch.object(guardrails_api, "_get_db", return_value=fake_db),
            patch.object(
                guardrails_api,
                "_extension_guardrail_rows",
                return_value=[projection_row],
            ),
        ):
            resp = client.get("/api/guardrails")

        assert resp.status_code == 200
        body = resp.json()
        assert any(item["id"] == "guardrail_extension_demo" for item in body)

    def test_list_guardrails_uses_request_account_scope_for_extension_rows(self, client):
        import core.entrypoints.admin.guardrails as guardrails_api

        fake_db = MagicMock()
        fake_db.list_guardrails.return_value = []

        with (
            patch.object(guardrails_api, "_get_db", return_value=fake_db),
            patch.object(
                guardrails_api,
                "_request_scope",
                return_value=SimpleNamespace(account_id="acc_team"),
            ),
            patch.object(
                guardrails_api,
                "_extension_guardrail_rows",
                return_value=[],
            ) as mock_extension_rows,
        ):
            resp = client.get("/api/guardrails")

        assert resp.status_code == 200
        mock_extension_rows.assert_called_once_with(
            fake_db,
            enabled_only=False,
            account_id="acc_team",
        )

    def test_list_has_expected_fields(self, client):
        resp = client.get("/api/guardrails")
        if resp.status_code == 200:
            data = resp.json()
            if data:
                g = data[0]
                for field in [
                    "id",
                    "display_name",
                    "mode",
                    "rule_type",
                    "severity",
                    "enabled",
                    "config",
                    "_agents",
                ]:
                    assert field in g, f"Missing field: {field}"

    def test_get_existing_guardrail(self, client):
        list_resp = client.get("/api/guardrails")
        if list_resp.status_code != 200:
            pytest.skip("Cannot list guardrails")
        guardrails = list_resp.json()
        created_id = None
        if guardrails:
            gid = guardrails[0]["id"]
        else:
            create_resp = client.post(
                "/api/guardrails",
                json={
                    "display_name": "Detail Fetch Test",
                    "mode": "deterministic",
                    "rule_type": "required_sections",
                    "severity": "warning",
                    "config": {"sections": ["summary"]},
                    "agents": ["writer"],
                },
            )
            assert create_resp.status_code in (200, 201)
            created_id = create_resp.json()["id"]
            gid = created_id
        try:
            resp = client.get(f"/api/guardrails/{gid}")
            assert resp.status_code == 200
            data = resp.json()
            assert str(data["id"]) == str(gid)
            assert "_agents" in data
        finally:
            if created_id is not None:
                client.delete(f"/api/guardrails/{created_id}")

    def test_get_guardrail_falls_back_to_extension_runtime_projection(self, client):
        import core.entrypoints.admin.guardrails as guardrails_api

        fake_db = MagicMock()
        fake_db.get_guardrail.return_value = None
        projection_row = {
            "id": "guardrail_extension_demo",
            "display_name": "Extension guardrail",
            "mode": "deterministic",
            "rule_type": "extension_policy",
            "severity": "warning",
            "enabled": True,
            "config": {},
            "_agents": [],
            "source_install_id": "inst-extension-demo",
        }

        with (
            patch.object(guardrails_api, "_get_db", return_value=fake_db),
            patch.object(
                guardrails_api,
                "_resolve_guardrail_row",
                return_value=projection_row,
            ),
        ):
            resp = client.get("/api/guardrails/guardrail_extension_demo")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "guardrail_extension_demo"
        assert body["source_install_id"] == "inst-extension-demo"

    def test_get_guardrail_uses_request_account_scope_for_extension_projection(self, client):
        import core.entrypoints.admin.guardrails as guardrails_api

        fake_db = MagicMock()
        fake_db.get_guardrail.return_value = None

        with (
            patch.object(guardrails_api, "_get_db", return_value=fake_db),
            patch.object(
                guardrails_api,
                "_request_scope",
                return_value=SimpleNamespace(account_id="acc_team"),
            ),
            patch.object(
                guardrails_api,
                "_resolve_guardrail_row",
                return_value={"id": "guardrail_extension_demo"},
            ) as mock_resolve,
        ):
            resp = client.get("/api/guardrails/guardrail_extension_demo")

        assert resp.status_code == 200
        mock_resolve.assert_called_once_with(
            fake_db,
            "guardrail_extension_demo",
            account_id="acc_team",
        )

    def test_get_nonexistent_guardrail(self, client):
        resp = client.get("/api/guardrails/99999")
        assert resp.status_code == 404

    def test_create_guardrail(self, client):
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Test Guardrail (unit test)",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "severity": "warning",
                "config": {"patterns": ["unit-test-secret"]},
                "agents": ["writer"],
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "id" in data
        assert data["status"] == "created"

        # Verify the created guardrail via GET
        detail = client.get(f"/api/guardrails/{data['id']}")
        assert detail.status_code == 200
        assert detail.json()["display_name"] == "Test Guardrail (unit test)"

        # Cleanup
        client.delete(f"/api/guardrails/{data['id']}")

    def test_create_guardrail_records_the_authenticated_actor_as_changelog_author(self, client):
        """B15 regression: the changelog author must not fall back to "admin".

        save_guardrail defaults author to "admin". The create/update/patch
        endpoints did not pass one, so every guardrail change in the registry
        changelog was attributed to "admin" instead of the person who made it.
        """
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Authored guardrail (unit test)",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "warning",
                "agents": ["writer"],
                "config": {"sections": ["Summary"]},
            },
        )
        assert resp.status_code in (200, 201)
        import core.entrypoints.admin.guardrails as guardrails_api

        store = guardrails_api._get_registry().db_store
        author = getattr(store, "last_save_author", None)
        assert author, "no author was passed to save_guardrail"
        assert author != "admin", "changelog author fell back to the 'admin' default"

        client.delete(f"/api/guardrails/{resp.json()['id']}")

    def test_create_guardrail_rejects_empty_agent_scope(self, client):
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Explicit scope required",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "warning",
                "agents": [],
                "config": {"sections": ["Summary"]},
            },
        )

        assert resp.status_code == 422
        assert "agents" in resp.text

    def test_create_guardrail_requires_agent_scope(self, client):
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Explicit scope required",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "warning",
                "config": {"sections": ["Summary"]},
            },
        )

        assert resp.status_code == 422
        assert "agents" in resp.text

    @pytest.mark.parametrize(
        ("payload", "expected_field"),
        [
            (
                {
                    "display_name": "",
                    "mode": "deterministic",
                    "rule_type": "required_sections",
                    "severity": "warning",
                    "config": {"sections": ["Summary"]},
                },
                "display_name",
            ),
            (
                {
                    "display_name": "Test Guardrail (unit test)",
                    "mode": "deterministic",
                    "rule_type": "   ",
                    "severity": "warning",
                },
                "rule_type",
            ),
        ],
    )
    def test_create_guardrail_rejects_blank_required_fields(self, client, payload, expected_field):
        resp = client.post("/api/guardrails", json=payload)
        assert resp.status_code == 422
        assert expected_field in resp.text

    def test_toggle_guardrail(self, client):
        # Create one to toggle
        create_resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Toggle Test",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "error",
                "config": {"sections": ["Summary"]},
                "agents": ["writer"],
            },
        )
        assert create_resp.status_code in (200, 201), create_resp.text

        gid = create_resp.json()["id"]

        # Toggle off
        resp = client.patch(f"/api/guardrails/{gid}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # Toggle back on
        resp = client.patch(f"/api/guardrails/{gid}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        # Cleanup
        client.delete(f"/api/guardrails/{gid}")

    def test_delete_guardrail(self, client):
        create_resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Delete Test",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "error",
                "config": {"sections": ["Summary"]},
                "agents": ["writer"],
            },
        )
        assert create_resp.status_code in (200, 201), create_resp.text

        gid = create_resp.json()["id"]
        resp = client.delete(f"/api/guardrails/{gid}")
        assert resp.status_code == 200

        # Verify gone
        resp = client.get(f"/api/guardrails/{gid}")
        assert resp.status_code == 404

    def test_set_agent_scope(self, client):
        create_resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Scope Test",
                "mode": "deterministic",
                "rule_type": "required_sections",
                "severity": "error",
                "config": {"sections": ["Summary"]},
                "agents": ["writer"],
            },
        )
        assert create_resp.status_code in (200, 201), create_resp.text

        gid = create_resp.json()["id"]

        # Set scope
        resp = client.put(
            f"/api/guardrails/{gid}/agents",
            json={
                "agent_ids": ["writer", "checker"],
            },
        )
        assert resp.status_code == 200

        # Verify scope
        detail = client.get(f"/api/guardrails/{gid}").json()
        assert sorted(detail["_agents"]) == ["checker", "writer"]

        # Cleanup
        client.delete(f"/api/guardrails/{gid}")

    def test_set_agent_scope_rejects_empty_selection(self, client):
        resp = client.put(
            "/api/guardrails/1/agents",
            json={"agent_ids": []},
        )

        assert resp.status_code == 422
        assert "agent_ids" in resp.text


# ---------------------------------------------------------------------------
# DB Store integration
# ---------------------------------------------------------------------------


class TestDBStoreGuardrails:
    """Test RegistryDBStore guardrail methods against real DB."""

    @pytest.fixture(autouse=True)
    def setup(self, registry):
        """Get DB store from registry fixture."""
        self.store = registry.db_store

    def test_list_guardrails_returns_list(self):
        result = self.store.list_guardrails(account_id=DEFAULT_ACCOUNT_ID)
        assert isinstance(result, list)

    def test_list_guardrails_has_agents(self):
        result = self.store.list_guardrails(account_id=DEFAULT_ACCOUNT_ID)
        if result:
            assert "_agents" in result[0]

    def test_list_guardrails_for_agent(self):
        result = self.store.list_guardrails_for_agent("writer", None)
        assert isinstance(result, list)

    def test_list_for_agent_filters_correctly(self):
        result = self.store.list_guardrails_for_agent("nonexistent_agent_xyz", None)
        assert result == []

    def test_get_guardrail_existing(self):
        import uuid

        # The 1.0 seed ships no agents or guardrails, so the test owns its agent.
        agent_id = f"guardrail_detail_writer_{uuid.uuid4().hex[:8]}"
        self.store.save_agent(
            {
                "agent_id": agent_id,
                "display_name": "Guardrail Detail Writer",
                "category": "ai",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        created_id = None
        try:
            created_id = self.store.save_guardrail(
                {
                    "display_name": "DB Detail Fetch Test",
                    "mode": "deterministic",
                    "rule_type": "db_detail_fetch_test",
                    "severity": "warning",
                    "agents": [agent_id],
                },
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="system",
                actor_type="system",
            )

            result = self.store.get_guardrail(created_id, account_id=DEFAULT_ACCOUNT_ID)
            assert result is not None
            assert str(result["id"]) == created_id
            assert agent_id in result["_agents"]
        finally:
            if created_id is not None:
                self.store.delete_guardrail(
                    created_id,
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="system",
                    actor_type="system",
                )
            self.store.delete_agent(agent_id)

    def test_get_guardrail_nonexistent(self):
        result = self.store.get_guardrail(99999, account_id=DEFAULT_ACCOUNT_ID)
        assert result is None

    def test_stats_includes_guardrails(self):
        stats = self.store.get_stats()
        assert "guardrails" in stats
        assert "total" in stats["guardrails"]
        assert "enabled" in stats["guardrails"]


def test_extension_runtime_guardrails_skip_non_uuid_account_scope():
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    runtime_store = MagicMock()
    store._extension_runtime_store = lambda: runtime_store

    result = store._extension_guardrails_for_agent("writer", account_id="acc_demo")

    assert result == []
    runtime_store.list_projections.assert_not_called()


def test_extension_runtime_guardrails_normalize_legacy_default_account_scope():
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    runtime_store = MagicMock()
    runtime_store.list_projections.return_value = []
    store._extension_runtime_store = lambda: runtime_store

    result = store._extension_guardrails_for_agent("writer", account_id="acc_default")

    assert result == []
    runtime_store.list_projections.assert_called_once_with(
        account_id=DEFAULT_ACCOUNT_ID,
        artifact_kind="guardrail",
        only_active=True,
    )


def test_extension_runtime_guardrails_ignore_empty_agent_scope():
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    runtime_store = MagicMock()
    runtime_store.list_projections.return_value = [
        {
            "artifact_id": "guardrail_without_scope",
            "snapshot": {
                "id": "guardrail_without_scope",
                "display_name": "Unassigned extension guardrail",
                "enabled": True,
                "_agents": [],
            },
        }
    ]
    store._extension_runtime_store = lambda: runtime_store

    result = store._extension_guardrails_for_agent(
        "writer",
        account_id=DEFAULT_ACCOUNT_ID,
    )

    assert result == []
