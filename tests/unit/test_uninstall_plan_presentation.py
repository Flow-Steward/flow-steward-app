"""The uninstall plan speaks in names a person recognises, not storage ids.

Seen on a stand for the Ollama extension: the dialog listed "Release —
d0593697-…", "Projection — e7360f4c-…", "Enabled State Record — false" and
"Project Workflow Assignment — e7360f4c-…". These tests pin the plain rows.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.application.extensions.uninstall_descriptor_dependencies import (
    collect_descriptor_only_dependents,
)
from core.application.extensions.uninstall_plan_presentation import (
    PlanSubject,
    human_text,
    present_uninstall_plan,
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
INSTALL_ID = "e7360f4c-1111-4222-8333-444455556666"
RELEASE_ID = "d0593697-aaaa-4bbb-8ccc-ddddeeeeffff"
SUBJECT = PlanSubject(
    extension_id="acme.model-provider", name="Remote Ollama LLM Provider", version="1.0.0"
)


class _Store:
    """A logger whose cursor answers the project-name lookup."""

    def __init__(self, projects: dict[str, str] | None = None) -> None:
        self.projects = projects or {}
        self.queries: list[str] = []

    def _table_exists(self, table: str) -> bool:
        return table == "projects"

    @contextmanager
    def cursor(self):
        cursor = MagicMock()

        def execute(sql, params):
            self.queries.append(sql)
            cursor.fetchall.return_value = [
                {"project_id": project_id, "name": self.projects[project_id]}
                for project_id in params[0]
                if project_id in self.projects
            ]

        cursor.execute.side_effect = execute
        yield cursor


def _visible(rows: list[dict]) -> list[tuple[str, str]]:
    return [(row["label"], row["display_name"]) for row in rows]


def _assert_no_internal_text(rows: list[dict]) -> None:
    for row in rows:
        shown = " ".join(
            str(row.get(key) or "")
            for key in ("label", "display_name", "project_name", "explanation")
        )
        assert not _UUID.search(shown), row
        assert not re.search(r"\b(true|false)\b", shown), row
        assert "_" not in row["label"], row


def test_ollama_plan_reads_as_names_not_ids() -> None:
    remove = [
        {"kind": "runtime_package", "id": SUBJECT.extension_id, "count": 1},
        {"kind": "release", "id": RELEASE_ID, "count": 1},
        {"kind": "projection", "id": INSTALL_ID, "count": 0},
        {"kind": "bundle_record", "id": "b-1", "display_name": "Remote Ollama", "count": 1},
        {"kind": "version_record", "id": "v-1", "display_name": "1.0.0", "count": 1},
        {"kind": "enabled_state_record", "id": "e-1", "display_name": "false", "count": 1},
        {"kind": "invocation_log", "id": "i-1", "display_name": "query", "count": 1},
        {"kind": "invocation_log", "id": "i-2", "display_name": "query", "count": 1},
        {"kind": "llm_provider_source", "id": "p-1", "display_name": "Ollama", "count": 1},
        {"kind": "credential_ref", "id": f"{INSTALL_ID}:llm_provider", "count": 2},
        {"kind": "project_workflow_assignment", "id": INSTALL_ID, "count": 0},
        {"kind": "project_dataset_assignment", "id": INSTALL_ID, "count": 0},
    ]

    presented, affected, retained = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=remove, affected=[], retained=[]
    )

    assert _visible(presented) == [
        ("Extension files", "Remote Ollama LLM Provider 1.0.0"),
        ("Extension registration and history", "Settings, installed versions and activity log"),
        ("Model provider", "Ollama"),
        ("Saved model provider credentials", "API keys and secrets for this provider"),
    ]
    records = next(row for row in presented if row["kind"] == "extension_records")
    assert records["count"] == 5
    # Rows with nothing in them do not appear at all.
    assert not any(row["kind"].startswith("project_") for row in presented)
    assert not any(row["kind"] in {"projection", "release"} for row in presented)
    assert affected == retained == []
    _assert_no_internal_text(presented)


def test_kind_and_id_survive_for_rows_that_stay() -> None:
    remove = [{"kind": "llm_provider_source", "id": "p-1", "display_name": "Ollama", "count": 1}]

    presented, _, _ = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=remove, affected=[], retained=[]
    )

    assert presented[0]["kind"] == "llm_provider_source"
    assert presented[0]["id"] == "p-1"


def test_projected_artifacts_are_listed_once_by_name() -> None:
    remove = [
        {
            "kind": "projection_record",
            "id": "workflow:daily_sync",
            "artifact_kind": "workflow",
            "artifact_id": "daily_sync",
            "display_name": "Daily sync",
            "count": 1,
        },
        {
            "kind": "projection_record",
            "id": "dataset:orders",
            "artifact_kind": "dataset",
            "artifact_id": "orders",
            "display_name": "Orders",
            "count": 1,
        },
        {"kind": "workflow", "id": "daily_sync", "display_name": "Daily sync", "count": 1},
    ]

    presented, _, _ = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=remove, affected=[], retained=[]
    )

    assert _visible(presented) == [("Dataset", "Orders"), ("Workflow", "Daily sync")]


def test_project_rows_carry_the_project_name() -> None:
    project_a = "11111111-2222-4333-8444-555555555555"
    project_b = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    store = _Store({project_a: "Alpha", project_b: "Beta"})
    remove = [
        {
            "kind": "project_connection",
            "id": "conn-1",
            "display_name": "Catalog primary",
            "project_id": project_a,
            "count": 1,
        },
        {
            "kind": "callback_registration",
            "id": "cb-1",
            "display_name": "order_created",
            "project_id": project_b,
            "project_name": "Beta",
            "count": 1,
        },
        # Three saved keys in one project read as one line, counted.
        *(
            {
                "kind": "project_state",
                "id": f"state-{index}",
                "display_name": f"sync/cursor-{index}",
                "project_id": project_a,
                "count": 1,
            }
            for index in range(3)
        ),
    ]

    presented, _, _ = present_uninstall_plan(
        bl=store, subject=SUBJECT, remove=remove, affected=[], retained=[]
    )

    assert [
        (row["label"], row["display_name"], row.get("project_name"), row["count"])
        for row in presented
    ] == [
        ("Project connection", "Catalog primary", "Alpha", 1),
        ("Incoming event hook", "Order created", "Beta", 1),
        ("Saved extension data", "Data the extension saved for this project", "Alpha", 3),
    ]
    # Only the row without a joined name needed the lookup; one query serves all.
    assert len(store.queries) == 1
    _assert_no_internal_text(presented)


def test_affected_and_retained_rows_use_names() -> None:
    affected = [
        {
            "kind": "workflow",
            "id": "8f2c1e60-0000-4000-8000-000000000001",
            "display_name": "",
            "owner_install_id": "other",
            "action": "preserve_and_disable",
            "reason": f"Missing required extension: {SUBJECT.extension_id}",
        },
        {
            "kind": "extension_install",
            "id": "acme.consumer",
            "display_name": "Consumer tools",
            "owner_install_id": "other-2",
            "action": "preserve_and_disable",
            "reason": f"Missing required extension: {SUBJECT.extension_id}",
        },
    ]
    retained = [
        {"kind": "runtime_package", "id": SUBJECT.extension_id, "count": 1},
        {"kind": "release", "id": RELEASE_ID, "display_name": "Shared release", "count": 1},
        {"kind": "external_effect_audit", "id": INSTALL_ID, "count": 0},
    ]

    _, shown_affected, shown_retained = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=[], affected=affected, retained=retained
    )

    assert _visible(shown_affected) == [
        ("Workflow", "Unnamed workflow"),
        ("Extension", "Consumer tools"),
    ]
    assert {row["explanation"] for row in shown_affected} == {
        "Needs Remote Ollama LLM Provider; it stops working until that extension is "
        "installed again."
    }
    # The stored reason is what confirm persists as availability text: unchanged.
    assert {row["reason"] for row in shown_affected} == {
        f"Missing required extension: {SUBJECT.extension_id}"
    }
    # The confirm step keys affected rows by kind/id/owner: those stay.
    assert [(row["kind"], row["id"], row["owner_install_id"]) for row in shown_affected] == [
        (row["kind"], row["id"], row["owner_install_id"]) for row in affected
    ]
    assert _visible(shown_retained) == [
        ("Extension files (another install still uses them)", "Remote Ollama LLM Provider 1.0.0")
    ]
    _assert_no_internal_text([*shown_affected, *shown_retained])


def test_the_plan_is_deterministic_for_the_revision_hash() -> None:
    remove = [
        {"kind": "runtime_package", "id": SUBJECT.extension_id, "count": 1},
        {"kind": "usage_record", "id": "u-1", "display_name": "query", "count": 1},
    ]

    first = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=list(remove), affected=[], retained=[]
    )
    second = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=list(remove), affected=[], retained=[]
    )

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_an_unnamed_dependent_reads_its_slug_in_words() -> None:
    affected = [{"kind": "workflow", "id": "consume_provider_data", "reason": "x"}]

    _, shown, _ = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=[], affected=affected, retained=[]
    )

    assert shown[0]["display_name"] == "Consume provider data"
    assert "explanation" not in shown[0]


def test_human_text_drops_ids_and_booleans() -> None:
    assert human_text(INSTALL_ID) == ""
    assert human_text("false") == ""
    assert human_text(" Daily sync ") == "Daily sync"


def test_dependent_extension_is_named_from_its_descriptor() -> None:
    install = SimpleNamespace(
        install_id="pi-consumer", extension_id="acme.consumer", release_id="pr-c", account_id="a"
    )
    release = SimpleNamespace(
        release_id="pr-c",
        descriptor={
            "identity": {"display_name": "Consumer tools"},
            "dependencies": {"extensions": [{"extension_id": SUBJECT.extension_id}]},
        },
    )
    install_store = SimpleNamespace(list_for_account=lambda *_args, **_kwargs: [install])
    runtime_store = SimpleNamespace(list_releases=lambda **_kwargs: [release])

    rows = collect_descriptor_only_dependents(
        extension_id=SUBJECT.extension_id,
        removed_install_id=INSTALL_ID,
        account_id="a",
        include_consumer_accounts=False,
        runtime_store=runtime_store,
        install_store=install_store,
        projected_install_ids=set(),
        alternative_install_available=lambda **_kwargs: False,
    )

    assert rows[0]["display_name"] == "Consumer tools"
    assert rows[0]["id"] == "acme.consumer"


def _provider_rows(names: list[str]) -> list[dict]:
    return [
        {"kind": "llm_provider_source", "id": f"src-{index}", "display_name": name, "count": 1}
        for index, name in enumerate(names)
    ]


def test_one_model_provider_keeps_its_own_row() -> None:
    presented, _, _ = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=_provider_rows(["Ollama"]), affected=[], retained=[]
    )

    assert presented == [
        {
            "kind": "llm_provider_source",
            "id": "src-0",
            "display_name": "Ollama",
            "count": 1,
            "label": "Model provider",
        }
    ]


def test_many_model_providers_read_as_one_line() -> None:
    """Seen with OpenRouter: 65 rows of "Model provider — Venice (OpenRouter)"."""
    names = [f"Provider {index:02d} (OpenRouter)" for index in range(62)] + [
        "OpenAI (OpenRouter)",
        "Anthropic (OpenRouter)",
        "Google (OpenRouter)",
    ]
    remove = [
        {"kind": "runtime_package", "id": SUBJECT.extension_id, "count": 1},
        *_provider_rows(names),
    ]

    presented, _, _ = present_uninstall_plan(
        bl=_Store(), subject=SUBJECT, remove=remove, affected=[], retained=[]
    )

    providers = [row for row in presented if row["kind"] == "llm_provider_source"]
    assert providers == [
        {
            "kind": "llm_provider_source",
            "id": "acme.model-provider:llm_provider_sources",
            "label": "Model providers",
            "display_name": "Anthropic, Google, OpenAI and 62 more",
            "count": 65,
        }
    ]
    assert [row["kind"] for row in presented] == ["runtime_package", "llm_provider_source"]
