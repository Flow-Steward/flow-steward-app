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
                    assert not step.manifest_dir, (client.id, step.id)
                    continue
                assert step.archive_layout in catalog.ARCHIVE_LAYOUTS, (client.id, step.id)
                assert step.file_name.startswith(f"{setup.package_id}-"), (client.id, step.id)
                assert step.file_name.endswith(".zip"), (client.id, step.id)
                agents = step.archive_layout == catalog.ARCHIVE_AGENTS_MARKETPLACE
                assert bool(step.manifest_dir) is agents, (client.id, step.id)
                # A plugin carries the server, and so the token, exactly when the
                # package works through MCP; the skills folder never does.
                carries_server = setup.auth == catalog.AUTH_PERSONAL_TOKEN and (
                    step.archive_layout != catalog.ARCHIVE_SKILLS_FOLDER
                )
                assert step.uses_token is carries_server, (client.id, step.id)
                if carries_server:
                    filled = step.template.replace("{mcp_url}", "https://x/mcp").replace(
                        "{token}", "t"
                    )
                    server = json.loads(filled)["mcpServers"]["flow-steward"]
                    assert server["url"] == "https://x/mcp"
                    assert (server.get("headers") or server.get("http_headers")) == {
                        "Authorization": "Bearer t"
                    }


def test_every_mcp_json_template_is_valid_json_with_the_token_header() -> None:
    templates = {
        catalog.MCP_JSON_TEMPLATE: "headers",
        catalog.CURSOR_MCP_JSON_TEMPLATE: "headers",
        catalog.OPENAI_CLI_MCP_JSON_TEMPLATE: "http_headers",
    }
    for template, header_field in templates.items():
        filled = template.replace("{mcp_url}", "https://x/mcp").replace("{token}", "t")
        server = json.loads(filled)["mcpServers"]["flow-steward"]
        assert server["url"] == "https://x/mcp"
        assert server[header_field] == {"Authorization": "Bearer t"}


def test_install_commands_are_the_same_on_every_system() -> None:
    """No Downloads path, no unzip: the user unpacks the archive and the commands run from ~."""
    for client in catalog.CLIENTS:
        for setup in client.packages:
            for step in setup.steps:
                if step.kind != "command" or step.id != "install_plugin":
                    continue
                lines = step.template.splitlines()
                assert lines[0] == "cd ~", (client.id, setup.package_id)
                folder = next(s for s in setup.steps if s.kind == "package").file_name
                assert f"./{folder.removesuffix('.zip')}" in lines[1], (client.id, lines)
                assert lines[2].endswith(f"{setup.package_id}@{setup.package_id}")
                assert "unzip" not in step.template
                assert "Downloads" not in step.template


def test_platform_steps_name_a_registered_preset_and_only_cloud_oauth_uses_them() -> None:
    from core.application.auth.inbound_oauth_platform_clients import PLATFORM_PRESETS

    found = []
    for client in catalog.CLIENTS:
        for setup in client.packages:
            for step in setup.steps:
                if step.kind != "platform":
                    assert not step.platform_preset, (client.id, step.id)
                    continue
                found.append(step)
                assert step.platform_preset in PLATFORM_PRESETS, (client.id, step.id)
                assert client.connection == catalog.CONNECTION_CLOUD, client.id
                assert setup.auth == catalog.AUTH_OAUTH, client.id
                assert step.template == catalog.MCP_URL_TEMPLATE
                assert step.file_name.startswith(f"{setup.package_id}-")
                assert step.file_name.endswith(".zip")
    assert [step.platform_preset for step in found] == [catalog.MICROSOFT_365_PRESET]
