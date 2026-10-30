"""Importing a bundle creates the agents, skills, contracts and guardrails it carries."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest
import yaml

from core.application.registry.db_store_common import RegistryCreateConflictError
from core.application.workflows.workflow_portability import (
    SCHEMA_VERSION,
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from tests._workflow_definitions import linear_workflow

ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
OTHER_ACCOUNT = "00000000-0000-0000-0000-0000000000b2"
PROJECT = "proj-target"
GLOBAL_RULE = "11111111-1111-4111-8111-111111111111"
PROJECT_RULE = "22222222-2222-4222-8222-222222222222"
NEW_RULE = "55555555-5555-4555-8555-555555555555"


class _Store:
    """A registry with create-only saves and one transaction that rolls everything back."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {
            "agent": {},
            "skill": {},
            "data_contract": {},
            "guardrail": {},
            "workflow": {},
            "tool": {"search_docs": {"tool_id": "search_docs"}},
        }
        self.trash: dict[str, set[str]] = {kind: set() for kind in self.rows}
        self.bindings: list[tuple[str, str]] = []
        self.fail_workflow_save = False
        self.race: tuple[str, str] | None = None
        self.saves: list[tuple[str, str]] = []

    @contextmanager
    def workflow_import_transaction(self):
        before = deepcopy((self.rows, self.bindings, self.saves))
        try:
            yield
        except Exception:
            self.rows, self.bindings, self.saves = before
            raise

    def registry_entity_reserved(self, kind, entity_id):
        return entity_id in self.rows[kind] or entity_id in self.trash[kind]

    def _create(self, kind, entity_id, data):
        if self.race == (kind, entity_id) or self.registry_entity_reserved(kind, entity_id):
            raise RegistryCreateConflictError(kind, entity_id)
        self.rows[kind][entity_id] = deepcopy(data)
        self.saves.append((kind, entity_id))

    def save_data_contract(self, data, author="admin", *, create_only=False):
        assert create_only
        self._create("data_contract", data["contract_id"], data)

    def save_skill(self, data, author="admin", *, create_only=False):
        assert create_only
        self._create("skill", data["skill_id"], data)

    def save_agent(self, data, author="admin", *, sync_relationships=True, create_only=False):
        assert create_only
        assert sync_relationships
        self._create("agent", data["agent_id"], data)

    def save_guardrail(
        self, data, author="admin", *, account_id, actor_id, actor_type, create_only=False
    ):
        assert create_only
        self._create("guardrail", data["id"], {**data, "account_id": account_id})
        return data["id"]

    def get_agent(self, agent_id):
        return deepcopy(self.rows["agent"].get(agent_id))

    def get_skill(self, skill_id):
        return deepcopy(self.rows["skill"].get(skill_id))

    def get_tool(self, tool_id):
        return deepcopy(self.rows["tool"].get(tool_id))

    def get_data_contract(self, contract_id, include_trashed=False):
        return deepcopy(self.rows["data_contract"].get(contract_id))

    def get_guardrail(self, guardrail_id, include_trashed=False, *, account_id=None):
        row = self.rows["guardrail"].get(guardrail_id)
        return deepcopy(row) if row and row.get("account_id") == account_id else None

    def get_workflow(self, workflow_id, *, include_trashed=False):
        return deepcopy(self.rows["workflow"].get(workflow_id))

    def save_workflow(self, data, author="admin", *, create_only=False, expected_revision=None):
        if self.fail_workflow_save:
            raise RuntimeError("workflow save failed")
        self.rows["workflow"][data["workflow_id"]] = deepcopy(data)

    def add_project_workflow_binding(self, project_id, workflow_id):
        self.bindings.append((project_id, workflow_id))


class _Projects:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def get_project_run_config(self, project_id):
        if not self.ready:
            return {"agents_config": {}}
        return {"agents_config": {"default_provider": "openai", "default_model": "gpt-5"}}


def _definitions() -> dict:
    return {
        "data_contracts": [
            {"contract_id": "brief.v1", "display_name": "Brief", "schema": {"type": "object"}}
        ],
        "skills": [
            {
                "skill_id": "draft",
                "display_name": "Draft",
                "category": "marketing",
                "skill_type": "base",
                "expected_inputs": [
                    {"name": "topic", "type": "text", "required": True},
                    {"name": "brief", "type": "json", "required": False, "contract_id": "brief.v1"},
                ],
                "expected_outputs": [{"name": "draft", "type": "json", "required": True}],
            },
            {"skill_id": "polish", "display_name": "Polish"},
        ],
        "agents": [
            {
                "agent_id": "writer",
                "display_name": "Writer",
                "enabled": True,
                "category": "marketing",
                "prompts": {"system": "Write; the token is a word."},
                "config": {"max_tokens": 4000},
                "structured_inputs": [{"name": "topic", "type": "text", "required": True}],
                "skills": [
                    {"skill_id": "draft", "skill_prompt": "Draft first."},
                    {"skill_id": "polish", "skill_prompt": "Then polish."},
                ],
                "tools": ["search_docs"],
            }
        ],
        "guardrails": [
            {
                "id": GLOBAL_RULE,
                "display_name": "No PII",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": ["ssn"]},
                "agents": ["writer"],
                "scope": "global",
            }
        ],
    }


def _bundle(definitions: dict | None = None) -> str:
    workflow = linear_workflow(
        {
            "step_id": "write",
            "step_kind": "agent",
            "participant_key": "author",
            "agent_id": "writer",
            "skill_id": "draft",
        },
        workflow_id="brief_flow",
        settings={"participants": [{"participant_key": "author", "agent_id": "writer"}]},
    )
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": "workflow_bundle",
        "metadata": {"source_workflow_id": "brief_flow"},
        "workflows": [workflow],
        "dependencies": {"agents": [{"agent_id": "writer"}], "skills": [{"skill_id": "draft"}]},
        "import_hints": {"default_enabled": False},
    }
    if definitions is not None:
        bundle["definitions"] = definitions
    return yaml.safe_dump(bundle, sort_keys=False)


def _service(store: _Store, *, ready: bool = True) -> WorkflowPortabilityService:
    return WorkflowPortabilityService(
        store, get_project_store=lambda: _Projects(ready), account_id=ACCOUNT
    )


def _rows(preflight: dict) -> dict[str, dict]:
    return {row["dependency_key"]: row for row in preflight["dependencies"]}


def _apply(
    store: _Store, definitions: dict | None = None, *, resolutions=None, ready=True, project=PROJECT
):
    return _service(store, ready=ready).apply_import(
        _bundle(_definitions() if definitions is None else definitions),
        resolutions=resolutions or [],
        author="tester",
        owner_account_id=ACCOUNT,
        owner_project_id=project,
    )


def test_clean_import_creates_every_definition_before_the_workflow() -> None:
    store = _Store()
    preflight = _service(store).preflight_import(_bundle(_definitions()), target_project_id=PROJECT)
    rows = _rows(preflight)

    assert preflight["can_apply"] is True
    assert rows["agent:writer"]["status"] == "create_from_bundle"
    assert rows["agent:writer"]["blocking"] is False
    assert rows["data_contract:brief.v1"]["status"] == "create_from_bundle"

    result = _apply(store)

    assert [kind for kind, _ in store.saves] == [
        "data_contract",
        "skill",
        "skill",
        "agent",
        "guardrail",
    ]
    agent = store.rows["agent"]["writer"]
    assert agent["skills"] == ["draft", "polish"]
    assert agent["prompts"]["skill_prompts"] == {"draft": "Draft first.", "polish": "Then polish."}
    assert agent["tools"] == ["search_docs"]
    assert agent["structured_inputs"] == [
        {"name": "topic", "type": "text", "description": "", "required": True}
    ]
    assert agent["account_id"] == ACCOUNT
    assert agent["enabled"] is True
    assert "provider" not in agent["config"]
    assert agent["config"]["max_tokens"] == 4000
    draft = store.rows["skill"]["draft"]
    assert [field["name"] for field in draft["expected_inputs"]] == ["topic", "brief"]
    assert draft["expected_inputs"][1]["contract_id"] == "brief.v1"
    rule = store.rows["guardrail"][GLOBAL_RULE]
    assert rule["project_id"] is None
    assert rule["agents"] == ["writer"]
    assert store.rows["workflow"]["brief_flow"]["enabled"] is False
    assert {item["id"] for item in result["definitions"]["created"]} == {
        "brief.v1",
        "draft",
        "polish",
        "writer",
        GLOBAL_RULE,
    }


def test_agent_without_a_project_model_is_created_disabled_with_a_reason() -> None:
    store = _Store()
    result = _apply(store, ready=False)

    assert store.rows["agent"]["writer"]["enabled"] is False
    assert result["definitions"]["agents_left_disabled"][0]["agent_id"] == "writer"


def test_taken_id_needs_a_decision_and_reuse_writes_nothing() -> None:
    store = _Store()
    store.rows["agent"]["writer"] = {
        "agent_id": "writer",
        "account_id": ACCOUNT,
        "display_name": "Mine",
    }
    preflight = _service(store).preflight_import(_bundle(_definitions()), target_project_id=PROJECT)
    row = _rows(preflight)["agent:writer"]

    assert preflight["can_apply"] is False
    assert row["status"] == "conflict"
    assert row["resolution_actions"] == ["use_existing", "rename", "abort"]

    # The existing agent does not carry the bundle's rule: reuse it without the rule.
    definitions = _definitions()
    definitions["guardrails"] = []
    _apply(
        store,
        definitions,
        resolutions=[{"dependency_key": "agent:writer", "action": "use_existing"}],
    )

    assert store.rows["agent"]["writer"] == {
        "agent_id": "writer",
        "account_id": ACCOUNT,
        "display_name": "Mine",
    }
    assert ("agent", "writer") not in store.saves
    assert store.rows["workflow"]["brief_flow"]["steps"][0]["agent_id"] == "writer"


@pytest.mark.parametrize("taken", ["foreign", "trash"])
def test_foreign_or_trashed_id_cannot_be_reused(taken: str) -> None:
    store = _Store()
    if taken == "foreign":
        store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": OTHER_ACCOUNT}
    else:
        store.trash["agent"].add("writer")
    service = _service(store)
    row = _rows(service.preflight_import(_bundle(_definitions()), target_project_id=PROJECT))[
        "agent:writer"
    ]

    assert row["resolution_actions"] == ["rename", "abort"]
    reused = service.preflight_import(
        _bundle(_definitions()),
        resolutions=[{"dependency_key": "agent:writer", "action": "use_existing"}],
        target_project_id=PROJECT,
    )
    assert _rows(reused)["agent:writer"]["status"] == "conflict"
    assert reused["can_apply"] is False


def test_rename_rewrites_every_structural_reference() -> None:
    store = _Store()
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": ACCOUNT}
    store.rows["skill"]["draft"] = {"skill_id": "draft", "account_id": ACCOUNT}
    store.rows["data_contract"]["brief.v1"] = {"contract_id": "brief.v1"}
    store.rows["guardrail"][GLOBAL_RULE] = {"id": GLOBAL_RULE, "account_id": ACCOUNT}
    _apply(
        store,
        resolutions=[
            {"dependency_key": "agent:writer", "action": "rename", "target_ref": "writer_2"},
            {"dependency_key": "skill:draft", "action": "rename", "target_ref": "draft_2"},
            {
                "dependency_key": "data_contract:brief.v1",
                "action": "rename",
                "target_ref": "brief.v2",
            },
            {
                "dependency_key": f"guardrail:{GLOBAL_RULE}",
                "action": "rename",
                "target_ref": NEW_RULE,
            },
        ],
    )

    workflow = store.rows["workflow"]["brief_flow"]
    assert workflow["steps"][0]["agent_id"] == "writer_2"
    assert workflow["settings"]["participants"][0]["agent_id"] == "writer_2"
    agent = store.rows["agent"]["writer_2"]
    assert agent["skills"] == ["draft_2", "polish"]
    assert set(agent["prompts"]["skill_prompts"]) == {"draft_2", "polish"}
    assert store.rows["skill"]["draft_2"]["expected_inputs"][1]["contract_id"] == "brief.v2"
    assert store.rows["guardrail"][NEW_RULE]["agents"] == ["writer_2"]
    # Text is never rewritten, and the records already here are untouched.
    assert agent["prompts"]["system"] == "Write; the token is a word."
    assert store.rows["agent"]["writer"] == {"agent_id": "writer", "account_id": ACCOUNT}


def test_rename_to_a_taken_or_invalid_id_stays_a_conflict() -> None:
    store = _Store()
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": ACCOUNT}
    store.rows["agent"]["other"] = {"agent_id": "other", "account_id": ACCOUNT}
    service = _service(store)
    for target in ("other", "", "bad id!"):
        preflight = service.preflight_import(
            _bundle(_definitions()),
            resolutions=[
                {"dependency_key": "agent:writer", "action": "rename", "target_ref": target}
            ],
            target_project_id=PROJECT,
        )
        assert _rows(preflight)["agent:writer"]["status"] == "conflict", target


def test_project_guardrails_require_the_chosen_project() -> None:
    store = _Store()
    definitions = _definitions()
    definitions["guardrails"].append(
        {
            "id": PROJECT_RULE,
            "display_name": "Tone",
            "mode": "semantic",
            "rule_type": "llm_judge",
            "agents": ["writer"],
            "scope": "project",
            "source_project_name": "Shop",
        }
    )
    without = _service(store).preflight_import(_bundle(definitions))
    row = _rows(without)["guardrail_project:project"]
    assert without["can_apply"] is False
    assert row["metadata"]["rules"][0]["source_project_name"] == "Shop"
    with pytest.raises(WorkflowPortabilityError):
        _apply(store, definitions, project="")

    _apply(store, definitions)

    assert store.rows["guardrail"][PROJECT_RULE]["project_id"] == PROJECT
    assert store.rows["guardrail"][GLOBAL_RULE]["project_id"] is None


def test_global_only_rules_keep_the_project_optional() -> None:
    preflight = _service(_Store()).preflight_import(_bundle(_definitions()))

    assert preflight["can_apply"] is True
    assert "guardrail_project:project" not in _rows(preflight)


def test_reused_guardrail_must_cover_the_same_scope_and_agents() -> None:
    store = _Store()
    store.rows["guardrail"][GLOBAL_RULE] = {
        "id": GLOBAL_RULE,
        "account_id": ACCOUNT,
        "project_id": "proj-other",
        "_agents": ["writer"],
    }
    preflight = _service(store).preflight_import(
        _bundle(_definitions()),
        resolutions=[{"dependency_key": f"guardrail:{GLOBAL_RULE}", "action": "use_existing"}],
        target_project_id=PROJECT,
    )

    assert _rows(preflight)[f"guardrail:{GLOBAL_RULE}"]["status"] == "conflict"


def test_unknown_category_blocks_with_a_reason() -> None:
    definitions = _definitions()
    definitions["agents"][0]["category"] = "analysis"
    row = _rows(_service(_Store()).preflight_import(_bundle(definitions)))["agent:writer"]

    assert row["status"] == "blocked"
    assert "analysis" in row["message"]


def test_missing_external_contract_blocks_with_its_path() -> None:
    definitions = _definitions()
    definitions["data_contracts"] = []
    row = _rows(_service(_Store()).preflight_import(_bundle(definitions)))["data_contract:brief.v1"]

    assert row["status"] == "blocked"
    assert row["sources"] == ["definitions.skills.draft.expected_inputs.brief"]


def test_file_without_definitions_still_asks_for_mapping() -> None:
    preflight = _service(_Store()).preflight_import(_bundle(None))
    row = _rows(preflight)["agent:writer"]

    assert row["status"] == "requires_mapping"
    assert preflight["can_apply"] is False


def test_late_failure_leaves_nothing_behind() -> None:
    store = _Store()
    store.fail_workflow_save = True

    with pytest.raises(RuntimeError):
        _apply(store)

    assert all(not store.rows[kind] for kind in ("agent", "skill", "data_contract", "guardrail"))
    assert store.saves == []


def test_an_id_taken_during_apply_aborts_without_overwriting() -> None:
    store = _Store()
    store.race = ("agent", "writer")

    with pytest.raises(WorkflowPortabilityError) as excinfo:
        _apply(store)

    assert excinfo.value.code == "workflow_import_definition_conflict"
    assert excinfo.value.status_code == 409
    assert store.saves == []
    assert not store.rows["skill"]


class _StoreWithRuntimeRows(_Store):
    """A target that already holds a workflow under the bundle's id, wired to its own records."""

    def list_workflow_runtime_dependencies(self, workflow_id):
        if workflow_id != "brief_flow":
            return []
        return [
            {"dependency_kind": "project_connection", "dependency_ref": "conn_stored_here"},
            {"dependency_kind": "extension_install", "install_id": "install-stored-here"},
        ]


def test_import_judges_the_file_without_the_stored_workflow_of_the_same_id() -> None:
    """Re-importing a template must not demand mappings for the earlier copy's connections."""
    store = _StoreWithRuntimeRows()
    preflight = _service(store).preflight_import(
        _bundle(_definitions()),
        target_workflow_id="brief_flow_copy",
        target_project_id=PROJECT,
    )

    keys = set(_rows(preflight))
    assert "project_connection:conn_stored_here" not in keys
    assert "extension:install-stored-here" not in keys
    assert preflight["can_apply"] is True


def test_export_still_reads_the_stored_runtime_rows() -> None:
    service = _service(_StoreWithRuntimeRows())
    workflow = yaml.safe_load(_bundle())["workflows"][0]

    exported = {
        row["dependency_type"] + ":" + row["ref"] for row in service.discover_dependencies(workflow)
    }
    imported = {
        row["dependency_type"] + ":" + row["ref"]
        for row in service.discover_dependencies(workflow, include_stored=False)
    }

    assert {"project_connection:conn_stored_here", "extension:install-stored-here"} <= exported
    assert not imported & {"project_connection:conn_stored_here", "extension:install-stored-here"}


def test_rename_on_an_ordinary_dependency_is_refused() -> None:
    """Only a definition can be renamed; elsewhere the rewrite would follow an unchecked id."""
    store = _Store()
    store.rows["workflow"]["child"] = {"workflow_id": "child"}
    service = _service(store)
    row = {"dependency_type": "child_workflow", "ref": "child", "required": True}
    index = service._resolution_index(
        [{"dependency_key": "child_workflow:child", "action": "rename", "target_ref": "nowhere"}]
    )

    classified = service._classify_dependency(row, index)

    assert classified["status"] == "conflict"
    assert classified["blocking"] is True
    mapped = service._classify_dependency(
        row,
        service._resolution_index(
            [
                {
                    "dependency_key": "child_workflow:child",
                    "action": "map_existing",
                    "target_ref": "child",
                }
            ]
        ),
    )
    assert mapped["status"] == "resolved"


def test_reusing_a_definition_cannot_point_at_another_id() -> None:
    store = _Store()
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": ACCOUNT}
    store.rows["agent"]["someone_else"] = {"agent_id": "someone_else", "account_id": ACCOUNT}
    definitions = _definitions()
    definitions["guardrails"] = []
    rows = _rows(
        _service(store).preflight_import(
            _bundle(definitions),
            resolutions=[
                {
                    "dependency_key": "agent:writer",
                    "action": "use_existing",
                    "target_ref": "someone_else",
                }
            ],
            target_project_id=PROJECT,
        )
    )

    assert rows["agent:writer"]["status"] == "conflict"


@pytest.mark.parametrize("action", ["map_existing", "create_connection_placeholder"])
def test_a_definition_refuses_actions_it_does_not_offer(action: str) -> None:
    rows = _rows(
        _service(_Store()).preflight_import(
            _bundle(_definitions()),
            resolutions=[
                {"dependency_key": "agent:writer", "action": action, "target_ref": "elsewhere"}
            ],
            target_project_id=PROJECT,
        )
    )

    assert rows["agent:writer"]["status"] == "conflict"


def test_preflight_and_rewrite_read_the_same_target_key() -> None:
    """A rename naming its target as target_workflow_id is checked under that id."""
    store = _Store()
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": ACCOUNT}
    store.rows["agent"]["taken"] = {"agent_id": "taken", "account_id": ACCOUNT}
    definitions = _definitions()
    definitions["guardrails"] = []
    rows = _rows(
        _service(store).preflight_import(
            _bundle(definitions),
            resolutions=[
                {
                    "dependency_key": "agent:writer",
                    "action": "rename",
                    "target_workflow_id": "taken",
                }
            ],
            target_project_id=PROJECT,
        )
    )

    assert rows["agent:writer"]["status"] == "conflict"


@pytest.mark.parametrize(
    ("section", "id_field", "ref"),
    [
        ("data_contracts", "contract_id", "auto.brief.v1"),
        ("agents", "agent_id", "extension.writer"),
        ("skills", "skill_id", "extension.draft"),
    ],
)
def test_a_file_cannot_create_ids_the_platform_derives(
    section: str, id_field: str, ref: str
) -> None:
    definitions = _definitions()
    definitions[section][0] = {**definitions[section][0], id_field: ref}
    preflight = _service(_Store()).preflight_import(_bundle(definitions), target_project_id=PROJECT)
    kind = {"data_contracts": "data_contract", "agents": "agent", "skills": "skill"}[section]

    row = _rows(preflight)[f"{kind}:{ref}"]
    assert row["status"] == "blocked"
    assert "reserved" in row["message"]
    assert preflight["can_apply"] is False


def test_a_contract_gets_the_checks_of_a_manual_save() -> None:
    definitions = _definitions()
    definitions["data_contracts"][0] = {
        "contract_id": "brief.v1",
        "display_name": "Brief",
        "kind": "list",
        "schema": {"type": "object"},
    }
    row = _rows(
        _service(_Store()).preflight_import(_bundle(definitions), target_project_id=PROJECT)
    )["data_contract:brief.v1"]

    assert row["status"] == "blocked"
    assert "schema.type=array" in row["message"]


def test_a_guardrail_on_an_agent_this_account_does_not_own_is_refused() -> None:
    store = _Store()
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": None}
    rows = _rows(
        _service(store).preflight_import(
            _bundle(_definitions()),
            resolutions=[{"dependency_key": "agent:writer", "action": "use_existing"}],
            target_project_id=PROJECT,
        )
    )

    assert rows[f"guardrail:{GLOBAL_RULE}"]["status"] == "blocked"
    assert "not one of this account's agents" in rows[f"guardrail:{GLOBAL_RULE}"]["message"]


def test_a_save_refused_after_preflight_is_a_portability_error() -> None:
    store = _Store()

    def refuse(*args, **kwargs):
        raise ValueError("guardrail agents are not available in the owning account: writer")

    store.save_guardrail = refuse
    with pytest.raises(WorkflowPortabilityError) as caught:
        _apply(store)

    assert caught.value.code == "workflow_import_definition_invalid"
    assert store.rows["agent"] == {}
    assert store.rows["workflow"] == {}


def test_an_import_without_a_project_says_so() -> None:
    store = _Store()
    result = _apply(store, project="")

    reason = result["definitions"]["agents_left_disabled"][0]["reason"]
    assert reason.startswith("No project was chosen")


def test_an_unreadable_project_model_is_not_reported_as_missing() -> None:
    class _Broken:
        def get_project_run_config(self, project_id):
            raise RuntimeError("database is down")

    store = _Store()
    service = WorkflowPortabilityService(
        store, get_project_store=lambda: _Broken(), account_id=ACCOUNT
    )
    result = service.apply_import(
        _bundle(_definitions()),
        resolutions=[],
        author="tester",
        owner_account_id=ACCOUNT,
        owner_project_id=PROJECT,
    )

    assert "could not be checked" in result["definitions"]["agents_left_disabled"][0]["reason"]


def test_agent_depends_on_follows_renames_and_names_what_the_file_lacks() -> None:
    definitions = _definitions()
    definitions["agents"][0]["depends_on"] = {"soft": ["writer"], "hard": ["reviewer"]}
    store = _Store()
    preflight = _service(store).preflight_import(_bundle(definitions), target_project_id=PROJECT)

    assert _rows(preflight)["agent:reviewer"]["status"] == "requires_mapping"

    store.rows["agent"]["reviewer"] = {"agent_id": "reviewer", "account_id": ACCOUNT}
    store.rows["agent"]["writer"] = {"agent_id": "writer", "account_id": ACCOUNT}
    definitions["guardrails"] = []
    _apply(
        store,
        definitions,
        resolutions=[
            {"dependency_key": "agent:writer", "action": "rename", "target_ref": "writer_2"}
        ],
    )

    assert store.rows["agent"]["writer_2"]["depends_on"] == {
        "soft": ["writer_2"],
        "hard": ["reviewer"],
    }
