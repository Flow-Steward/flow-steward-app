"""Task 4: the wizard's client catalog is the matrix and the package metadata, not a copy.

``core/application/auth/agent_connection_catalog.py`` exists because neither
``docs/agent-skills/COMPATIBILITY.md`` nor ``docs/agent-skills/packages.json`` ships
in the runtime image. These tests fail when the two drift apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.application.auth import agent_connection_catalog as catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "docs" / "agent-skills" / "COMPATIBILITY.md"
PACKAGES_JSON = REPO_ROOT / "docs" / "agent-skills" / "packages.json"
_ROW_PACKAGE = {"Operator": catalog.OPERATOR_PACKAGE_ID, "Developer": catalog.DEVELOPER_PACKAGE_ID}


def _matrix_rows() -> dict[str, dict[str, dict[str, str]]]:
    """``{section: {package_id: {column: cell}}}`` for the sections under "## Clients"."""
    text = MATRIX.read_text(encoding="utf-8")
    clients = text.split("## Clients", 1)[1].split("\n## ", 1)[0]
    sections: dict[str, dict[str, dict[str, str]]] = {}
    for block in re.split(r"^### ", clients, flags=re.MULTILINE)[1:]:
        heading, _, body = block.partition("\n")
        header: list[str] = []
        rows: dict[str, dict[str, str]] = {}
        for line in body.splitlines():
            if not line.startswith("|") or set(line) <= {"|", "-", " "}:
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[0] == "Package":
                header = cells
                continue
            rows[_ROW_PACKAGE[cells[0]]] = dict(zip(header, cells, strict=True))
        sections[heading.strip()] = rows
    return sections


def test_every_matrix_client_is_in_the_catalog_and_nothing_else() -> None:
    assert {client.matrix_section for client in catalog.CLIENTS} == set(_matrix_rows())
    assert len({client.id for client in catalog.CLIENTS}) == len(catalog.CLIENTS)


@pytest.mark.parametrize("client", catalog.CLIENTS, ids=lambda client: client.id)
def test_each_client_row_matches_the_matrix(client: catalog.AgentClient) -> None:
    rows = _matrix_rows()[client.matrix_section]
    assert {setup.package_id for setup in client.packages} == set(rows)
    for setup in client.packages:
        row = rows[setup.package_id]
        assert setup.offered == (not row["Functions"].startswith("Not offered")), row
        assert setup.verified == row["Verified"].startswith("Yes"), row
        auth = row["Auth"]
        if setup.auth == catalog.AUTH_PERSONAL_TOKEN:
            assert auth.startswith("Personal token"), row
        elif setup.auth == catalog.AUTH_OAUTH:
            assert "OAuth" in auth, row
        else:
            assert auth in {"None", "—"}, row
    operator = next(s for s in client.packages if s.package_id == catalog.OPERATOR_PACKAGE_ID)
    expected = (
        catalog.AUTH_OAUTH
        if client.connection == catalog.CONNECTION_CLOUD
        else (catalog.AUTH_PERSONAL_TOKEN)
    )
    assert operator.auth == expected


def test_packages_match_packages_json() -> None:
    sources = json.loads(PACKAGES_JSON.read_text(encoding="utf-8"))["packages"]
    assert [package.id for package in catalog.PACKAGES] == list(sources)
    for package in catalog.PACKAGES:
        source = sources[package.id]
        assert package.display_name == source["display_name"]
        assert package.version == source["version"]
        assert package.short_description == source["short_description"]
        assert package.needs_mcp == ("mcp_connection" in source)


def test_only_a_personal_token_setup_asks_for_the_token() -> None:
    for client in catalog.CLIENTS:
        for setup in client.packages:
            uses_token = any(step.uses_token for step in setup.steps)
            assert uses_token == (setup.auth == catalog.AUTH_PERSONAL_TOKEN), (client.id, setup)
            if setup.auth == catalog.AUTH_PERSONAL_TOKEN:
                assert any("{mcp_url}" in step.template for step in setup.steps)


def test_a_package_without_mcp_never_gets_a_token() -> None:
    for client in catalog.CLIENTS:
        for setup in client.packages:
            package = catalog.agent_skill_package(setup.package_id)
            assert package is not None
            if not package.needs_mcp:
                assert setup.auth == catalog.AUTH_NONE


def test_every_text_has_a_translation_key() -> None:
    steps = [
        step for client in catalog.CLIENTS for setup in client.packages for step in setup.steps
    ]
    for step in [*steps, catalog.FIRST_REQUEST_STEP]:
        assert step.title_key.startswith("react.agent_access.")
        assert bool(step.description_key) == bool(step.description)
    for client in catalog.CLIENTS:
        assert client.description_key.startswith("react.agent_access.")
        for setup in client.packages:
            assert bool(setup.message_key) == bool(setup.message)


def test_package_steps_name_a_known_layout_and_an_archive() -> None:
    for client in catalog.CLIENTS:
        for setup in client.packages:
            for step in setup.steps:
                if step.kind != "package":
                    assert not step.archive_layout, (client.id, step.id)
                    continue
                assert step.archive_layout in catalog.ARCHIVE_LAYOUTS, (client.id, step.id)
                assert step.file_name.endswith(".zip"), (client.id, step.id)
                # Only a Claude plugin carries the server, and so the token, inside it.
                carries_server = step.archive_layout == catalog.ARCHIVE_CLAUDE_MARKETPLACE
                assert step.uses_token is carries_server, (client.id, step.id)
                if carries_server:
                    filled = step.template.replace("{mcp_url}", "https://x/mcp").replace(
                        "{token}", "t"
                    )
                    assert json.loads(filled)["flow-steward"]["headers"] == {
                        "Authorization": "Bearer t"
                    }


def test_every_mcp_json_template_is_valid_json_with_the_token_header() -> None:
    for template in (catalog.MCP_JSON_TEMPLATE, catalog.CURSOR_MCP_JSON_TEMPLATE):
        filled = template.replace("{mcp_url}", "https://x/mcp").replace("{token}", "t")
        server = json.loads(filled)["mcpServers"]["flow-steward"]
        assert server["url"] == "https://x/mcp"
        assert server["headers"] == {"Authorization": "Bearer t"}
