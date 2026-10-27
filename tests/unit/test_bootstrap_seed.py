from __future__ import annotations

import pytest

from core.application.tooling.db import bootstrap_seed


class _FakeRegistryStore:
    def __init__(self) -> None:
        self.guardrails_by_id: dict[str, dict] = {}
        self.agents_by_id: dict[str, dict] = {}
        self.tools_by_id: dict[str, dict] = {}
        self.saved_guardrails: list[dict] = []

    def save_data_contract(self, payload, *, author: str) -> None:
        _ = payload
        _ = author

    def save_field(self, payload, *, author: str) -> None:
        _ = payload
        _ = author

    def apply_entity_snapshot(self, entity_kind: str, entity_id: str, payload, author: str) -> None:
        _ = entity_kind
        _ = entity_id
        _ = payload
        _ = author

    def save_published_block_contract(self, payload, *, author: str) -> None:
        _ = payload
        _ = author

    def save_guardrail(
        self,
        payload,
        *,
        author: str,
        account_id: str,
        actor_id: str,
        actor_type: str,
    ) -> None:
        _ = (account_id, actor_id, actor_type)
        guardrail_id = str(payload["id"]).strip()
        normalized = dict(payload)
        normalized["_agents"] = list(payload.get("agents") or [])
        self.guardrails_by_id[guardrail_id] = normalized
        self.saved_guardrails.append({"author": author, **normalized})
        _ = author

    def get_tool(self, tool_id: str) -> dict | None:
        tool = self.tools_by_id.get(str(tool_id or "").strip())
        return dict(tool) if isinstance(tool, dict) else None

    def save_tool(self, payload, author: str = "system") -> None:
        tool_id = str(payload["tool_id"]).strip()
        self.tools_by_id[tool_id] = {"author": author, **dict(payload)}

    def get_guardrail(
        self, guardrail_id: str, include_trashed: bool = False, *, account_id=None
    ) -> dict | None:
        _ = include_trashed
        guardrail = self.guardrails_by_id.get(str(guardrail_id).strip())
        return dict(guardrail) if isinstance(guardrail, dict) else None

    def get_agent(self, agent_id: str, include_trashed: bool = False) -> dict | None:
        _ = include_trashed
        agent = self.agents_by_id.get(str(agent_id))
        return dict(agent) if isinstance(agent, dict) else None


class _FakeSchemaStateConnection:
    def __init__(self) -> None:
        self.autocommit = False
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_apply_bootstrap_seed_records_canonical_schema_state(monkeypatch) -> None:
    connection = _FakeSchemaStateConnection()
    recorded: list[dict] = []
    seeded: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        bootstrap_seed,
        "apply_lookup_seed",
        lambda *, dsn: seeded.append(("lookup", dsn, "")) or {"accounts": 1},
    )
    monkeypatch.setattr(
        bootstrap_seed,
        "apply_registry_seed",
        lambda *, dsn, author: seeded.append(("registry", dsn, author)) or {"agents": 2},
    )
    monkeypatch.setattr(
        bootstrap_seed.psycopg_runtime,
        "connect",
        lambda dsn: connection if dsn == "postgresql://canonical" else None,
    )
    monkeypatch.setattr(
        bootstrap_seed,
        "record_canonical_schema_state",
        lambda conn, **kwargs: (
            recorded.append({"conn": conn, **kwargs})
            or {"schema_fingerprint": "canonical-fingerprint"}
        ),
    )

    summary = bootstrap_seed.apply_bootstrap_seed(
        dsn="postgresql://canonical",
        author="installer",
    )

    assert seeded == [
        ("lookup", "postgresql://canonical", ""),
        ("registry", "postgresql://canonical", "installer"),
    ]
    assert recorded == [
        {
            "conn": connection,
            "root": bootstrap_seed.REPO_ROOT,
            "applied_by": "installer",
            "metadata": {"source": "flow-steward database bootstrap-seed"},
        }
    ]
    assert connection.autocommit is True
    assert connection.closed is True
    assert summary == {
        "lookup": {"accounts": 1},
        "registry": {"agents": 2},
        "schema_state": {"updated": 1, "fingerprint": "canonical-fingerprint"},
    }


def test_bootstrap_seed_main_forwards_the_installer_author(monkeypatch, capsys) -> None:
    captured: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        bootstrap_seed,
        "apply_bootstrap_seed",
        lambda **kwargs: captured.append(kwargs) or {"lookup": {"accounts": 1}},
    )

    result = bootstrap_seed.main(["--author=installer"])

    assert result == 0
    assert captured == [{"author": "installer"}]
    assert '"accounts": 1' in capsys.readouterr().out


def test_canonical_guardrail_rows_reject_empty_agent_scope() -> None:
    payload = {
        "guardrails": [
            {
                "id": "329f5c90-2928-520c-9ad8-6a7dbf633cf1",
                "display_name": "No Migration Chain",
                "agents": [],
            }
        ]
    }

    with pytest.raises(ValueError, match="at least one agent"):
        bootstrap_seed._canonical_guardrail_rows(payload)


def test_apply_registry_seed_ignores_removed_workflow_task_class_section(monkeypatch) -> None:
    fake_store = _FakeRegistryStore()

    monkeypatch.setattr(
        bootstrap_seed,
        "_load_seed",
        lambda _path: {
            "data_contracts": [],
            "fields": [],
            "tools": [],
            "skills": [],
            "agents": [],
            "workflows": [
                {"workflow_id": "weekly_report"},
                {"workflow_id": "maintenance"},
            ],
            "published_block_contracts": [],
            "guardrails": [],
        },
    )
    monkeypatch.setattr(bootstrap_seed, "RegistryDBStore", lambda _dsn: fake_store)
    monkeypatch.setattr(bootstrap_seed, "sync_builtin_mcp_tools", lambda _store: None)

    counts = bootstrap_seed.apply_registry_seed(dsn="postgresql://unused", author="tester")

    assert counts["workflows"] == 2
    assert "workflow_task_classes" not in counts


def test_apply_registry_seed_normalizes_published_block_field_keys(monkeypatch) -> None:
    fake_store = _FakeRegistryStore()
    captured_blocks: list[dict] = []

    def _capture_block(payload, *, author: str) -> None:
        _ = author
        captured_blocks.append(payload)

    fake_store.save_published_block_contract = _capture_block  # type: ignore[method-assign]

    monkeypatch.setattr(
        bootstrap_seed,
        "_load_seed",
        lambda _path: {
            "data_contracts": [],
            "fields": [],
            "tools": [],
            "skills": [],
            "agents": [],
            "workflows": [],
            "workflow_task_classes": [],
            "published_block_contracts": [
                {
                    "block_key": "agent.demo.run",
                    "source_kind": "agent",
                    "source_ref": "demo::run",
                    "inputs": [
                        {
                            "id": 11,
                            "field_id": 101,
                            "field_key": "story_spec",
                            "required": True,
                        }
                    ],
                    "outputs": [
                        {
                            "id": 12,
                            "field_id": 202,
                            "field_key": "source_brief",
                            "required_by_default": True,
                            "exposed_by_default": True,
                        }
                    ],
                }
            ],
            "guardrails": [],
        },
    )
    monkeypatch.setattr(bootstrap_seed, "RegistryDBStore", lambda _dsn: fake_store)
    monkeypatch.setattr(bootstrap_seed, "sync_builtin_mcp_tools", lambda _store: None)

    counts = bootstrap_seed.apply_registry_seed(dsn="postgresql://unused", author="tester")

    assert counts["published_block_contracts"] == 1
    assert len(captured_blocks) == 1
    payload = captured_blocks[0]
    assert payload["inputs"] == [{"field_key": "story_spec", "required": True}]
    assert payload["outputs"] == [
        {
            "field_key": "source_brief",
            "required_by_default": True,
            "exposed_by_default": True,
        }
    ]


def test_ensure_canonical_guardrails_seeded_restores_missing_rows_and_scope(monkeypatch) -> None:
    fake_store = _FakeRegistryStore()
    legacy_guardrail_id = "329f5c90-2928-520c-9ad8-6a7dbf633cf1"
    verdict_guardrail_id = "a8ebf547-d459-5b22-a99c-bae6ed1e05e3"
    fake_store.agents_by_id["writer"] = {"agent_id": "writer"}
    fake_store.agents_by_id["reviewer"] = {"agent_id": "reviewer"}
    fake_store.guardrails_by_id[verdict_guardrail_id] = {
        "id": verdict_guardrail_id,
        "display_name": "Code Review Must Have Verdict",
        "_agents": [],
    }

    monkeypatch.setattr(
        bootstrap_seed,
        "_load_seed",
        lambda _path: {
            "guardrails": [
                {
                    "id": legacy_guardrail_id,
                    "display_name": "No Migration Chain",
                    "description": "release guardrail",
                    "enabled": True,
                    "mode": "deterministic",
                    "rule_type": "forbidden_patterns",
                    "severity": "error",
                    "config": {},
                    "prompt_text": "",
                    "agent_prompt": "",
                    "project_id": None,
                    "sort_order": 10,
                    "_agents": ["writer"],
                },
                {
                    "id": verdict_guardrail_id,
                    "display_name": "Code Review Must Have Verdict",
                    "description": "release guardrail",
                    "enabled": True,
                    "mode": "deterministic",
                    "rule_type": "required_verdict",
                    "severity": "error",
                    "config": {},
                    "prompt_text": "",
                    "agent_prompt": "",
                    "project_id": None,
                    "sort_order": 50,
                    "_agents": ["reviewer"],
                },
            ]
        },
    )
    monkeypatch.setattr(bootstrap_seed, "RegistryDBStore", lambda _dsn: fake_store)

    summary = bootstrap_seed.ensure_canonical_guardrails_seeded(
        dsn="postgresql://unused",
        author="tester",
    )

    assert summary == {"expected": 2, "restored": 1, "scope_repaired": 1}
    assert fake_store.guardrails_by_id[legacy_guardrail_id]["display_name"] == "No Migration Chain"
    assert fake_store.guardrails_by_id[verdict_guardrail_id]["_agents"] == ["reviewer"]
    assert [item["id"] for item in fake_store.saved_guardrails] == [
        legacy_guardrail_id,
        verdict_guardrail_id,
    ]


def test_ensure_canonical_guardrails_seeded_skips_until_registry_agents_exist(monkeypatch) -> None:
    fake_store = _FakeRegistryStore()

    monkeypatch.setattr(
        bootstrap_seed,
        "_load_seed",
        lambda _path: {
            "guardrails": [
                {
                    "id": "a8ebf547-d459-5b22-a99c-bae6ed1e05e3",
                    "display_name": "Code Review Must Have Verdict",
                    "description": "release guardrail",
                    "enabled": True,
                    "mode": "deterministic",
                    "rule_type": "required_verdict",
                    "severity": "error",
                    "config": {},
                    "prompt_text": "",
                    "agent_prompt": "",
                    "project_id": None,
                    "sort_order": 50,
                    "_agents": ["reviewer"],
                }
            ]
        },
    )
    monkeypatch.setattr(bootstrap_seed, "RegistryDBStore", lambda _dsn: fake_store)

    summary = bootstrap_seed.ensure_canonical_guardrails_seeded(
        dsn="postgresql://unused",
        author="tester",
    )

    assert summary == {"expected": 1, "restored": 0, "scope_repaired": 0}
    assert fake_store.saved_guardrails == []
