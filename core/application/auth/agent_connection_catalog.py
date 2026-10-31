"""Which AI clients the "connect an AI agent" wizard offers, and what each one receives.

Task 4 (Settings → Account → AI agents). This is the machine-readable form of the
compatibility matrix ``docs/agent-skills/COMPATIBILITY.md`` and of the package
metadata ``docs/agent-skills/packages.json``: neither file ships in the runtime image,
so the wizard reads this module, and ``tests/unit/test_agent_connection_catalog.py``
fails when a client, a package row's auth or verified status, or a package field
here disagrees with them. The React page renders what this module describes and
holds no client table of its own.

Step templates use ``{name}`` placeholders. The service fills every one of them
except ``{token}``: the secret exists only in the browser, for the one response
that created it, and never in this payload, a URL or a log.
"""

from __future__ import annotations

from dataclasses import dataclass

REPOSITORY_URL = "https://github.com/Flow-Steward/flow-steward-app"
OPERATOR_PACKAGE_ID = "flow-steward-operator"
DEVELOPER_PACKAGE_ID = "flow-steward-developer"
MARKETPLACE_ARCHIVE = "flow-steward-agent-skills-{core_version}.zip"

AUTH_PERSONAL_TOKEN = "personal_token"
AUTH_OAUTH = "oauth"
AUTH_NONE = "none"

CONNECTION_LOCAL = "local"
CONNECTION_CLOUD = "cloud"

#: Placeholder the browser replaces with the one-time secret.
TOKEN_PLACEHOLDER = "{token}"

# External client products the user picks; Flow Steward runs none of them. Their names
# appear only here (tests/unit/test_retired_executor_absence.py allows these lines).
CLAUDE_CODE_LABEL = "Claude Code"
OPENAI_CLI_ID = "codex"
OPENAI_CLI_LABEL = "Codex"
OPENAI_CLI_CONFIG_PATH = "~/.codex/config.toml"


@dataclass(frozen=True, slots=True)
class AgentSkillPackage:
    """One package, as ``docs/agent-skills/packages.json`` describes it."""

    id: str
    display_name: str
    version: str
    short_description: str
    #: The package works through this installation's MCP server (it needs access).
    needs_mcp: bool
    description_key: str
    description: str


@dataclass(frozen=True, slots=True)
class SetupStep:
    """One thing the user does: download, run a command, add a file, paste a value."""

    id: str
    #: ``link`` (open the release or README), ``command``, ``file``, ``value`` or ``notice``.
    kind: str
    title_key: str
    title: str
    description_key: str = ""
    description: str = ""
    template: str = ""
    file_name: str = ""
    #: ``link`` steps: which URL the button opens (``release_url`` or ``readme_url``).
    link: str = ""
    #: The release file the description names as ``{archive}``.
    archive: str = ""

    @property
    def uses_token(self) -> bool:
        return TOKEN_PLACEHOLDER in self.template


@dataclass(frozen=True, slots=True)
class ClientPackageSetup:
    """What one client receives for one package (one row of the matrix)."""

    package_id: str
    #: The matrix lists this package for the client (a "Not offered" row is False).
    offered: bool
    auth: str
    #: The matrix's "Verified" column says Yes.
    verified: bool
    #: Why the package cannot be used in this client now (``offered`` False, or no listing).
    message_key: str = ""
    message: str = ""
    readme_anchor: str = ""
    steps: tuple[SetupStep, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentClient:
    id: str
    label: str
    #: ``local``: the MCP connection is opened from the user's computer;
    #: ``cloud``: the client's own servers open it, so OAuth and a public HTTPS address.
    connection: str
    #: The heading of this client's section in COMPATIBILITY.md.
    matrix_section: str
    description_key: str
    description: str
    packages: tuple[ClientPackageSetup, ...]


PACKAGES: tuple[AgentSkillPackage, ...] = (
    AgentSkillPackage(
        id=OPERATOR_PACKAGE_ID,
        display_name="Flow Steward Operator",
        version="1.0.0",
        short_description="Build and run automations",
        needs_mcp=True,
        description_key="react.agent_access.package_operator_description",
        description=(
            "For people who build and run automations by talking to an AI agent. The agent "
            "works through this installation's MCP server, so it needs access."
        ),
    ),
    AgentSkillPackage(
        id=DEVELOPER_PACKAGE_ID,
        display_name="Flow Steward Developer",
        version="1.0.0",
        short_description="Build Flow Steward extensions",
        needs_mcp=False,
        description_key="react.agent_access.package_developer_description",
        description=(
            "For developers who build extensions and contribute code with the CLI and SDK in "
            "a terminal. It does not connect to this installation and needs no token."
        ),
    ),
)

_DOWNLOAD_MARKETPLACE = SetupStep(
    id="download_marketplace",
    kind="link",
    link="release_url",
    archive="marketplace_archive",
    title_key="react.agent_access.step_download_marketplace",
    title="Download the packages",
    description_key="react.agent_access.step_download_marketplace_description",
    description=(
        "Download {archive} from the Flow Steward {core_version} release and extract it. "
        "Check it against the release's SHA256SUMS."
    ),
)


def _install_claude_code(package_id: str) -> SetupStep:
    return SetupStep(
        id="install_plugin",
        kind="command",
        title_key="react.agent_access.step_install_claude_plugin",
        title="Install the plugin",
        description_key="react.agent_access.step_install_claude_plugin_description",
        description="Replace the path with the extracted flow-steward-agent-skills folder.",
        template=(
            "claude plugin marketplace add /absolute/path/to/flow-steward-agent-skills\n"
            f"claude plugin install {package_id}@flow-steward"
        ),
    )


def _download_package(archive: str) -> SetupStep:
    return SetupStep(
        id="download_package",
        kind="link",
        link="release_url",
        archive=archive,
        title_key="react.agent_access.step_download_package",
        title="Download the package",
        description_key="react.agent_access.step_download_package_description",
        description=(
            "Download {archive} from the Flow Steward {core_version} release and install it "
            "as the package README describes for {client}."
        ),
    )


_OPEN_README = SetupStep(
    id="open_readme",
    kind="link",
    link="readme_url",
    title_key="react.agent_access.step_open_readme",
    title="Follow the install steps",
    description_key="react.agent_access.step_open_readme_description",
    description="The package README has the install, update and removal steps for this client.",
)

_TOKEN_ENVIRONMENT = SetupStep(
    id="token_environment",
    kind="command",
    title_key="react.agent_access.step_token_environment",
    title="Make the token available to the client",
    description_key="react.agent_access.step_token_environment_description",
    description=(
        "Set the variable where the client starts, for example in your shell profile. "
        "Anyone who can read it can use the token."
    ),
    template='export FLOW_STEWARD_TOKEN="{token}"',
)


def _local_developer(anchor: str, *, archive: str) -> ClientPackageSetup:
    return ClientPackageSetup(
        package_id=DEVELOPER_PACKAGE_ID,
        offered=True,
        auth=AUTH_NONE,
        verified=False,
        readme_anchor=anchor,
        steps=(_download_package(archive), _OPEN_README),
    )


CLIENTS: tuple[AgentClient, ...] = (
    AgentClient(
        id="claude_code",
        label=CLAUDE_CODE_LABEL,
        connection=CONNECTION_LOCAL,
        matrix_section=CLAUDE_CODE_LABEL,
        description_key="react.agent_access.client_claude_code_description",
        description="Terminal, IDE extensions and the Code tab of the Claude desktop app.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_PERSONAL_TOKEN,
                verified=True,
                readme_anchor="claude-code",
                steps=(
                    _DOWNLOAD_MARKETPLACE,
                    _install_claude_code(OPERATOR_PACKAGE_ID),
                    SetupStep(
                        id="configure_plugin",
                        kind="command",
                        title_key="react.agent_access.step_configure_claude_plugin",
                        title="Enter the address and the token",
                        description_key=(
                            "react.agent_access.step_configure_claude_plugin_description"
                        ),
                        description=(
                            "Run this in a session of the client and enter the two values below. "
                            "The client keeps the token in your system credential store."
                        ),
                        template=f"/plugin configure {OPERATOR_PACKAGE_ID}@flow-steward",
                    ),
                    SetupStep(
                        id="mcp_url",
                        kind="value",
                        title_key="react.agent_access.value_mcp_address",
                        title="Flow Steward MCP address",
                        template="{mcp_url}",
                    ),
                    SetupStep(
                        id="mcp_token",
                        kind="value",
                        title_key="react.agent_access.value_personal_token",
                        title="Flow Steward personal access token",
                        template=TOKEN_PLACEHOLDER,
                    ),
                ),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=True,
                readme_anchor="claude-code",
                steps=(_DOWNLOAD_MARKETPLACE, _install_claude_code(DEVELOPER_PACKAGE_ID)),
            ),
        ),
    ),
    AgentClient(
        id=OPENAI_CLI_ID,
        label=OPENAI_CLI_LABEL,
        connection=CONNECTION_LOCAL,
        matrix_section=OPENAI_CLI_LABEL,
        description_key="react.agent_access.client_openai_agent_description",
        description="OpenAI's command-line coding agent and the ChatGPT desktop app's coding agent.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_PERSONAL_TOKEN,
                verified=False,
                readme_anchor=OPENAI_CLI_ID,
                steps=(
                    _download_package("package_archive"),
                    _OPEN_README,
                    SetupStep(
                        id="mcp_config",
                        kind="file",
                        file_name="config.toml",
                        title_key="react.agent_access.step_toml_config",
                        title=f"Add the server to {OPENAI_CLI_CONFIG_PATH}",
                        description_key="react.agent_access.step_toml_config_description",
                        description=(
                            "Add this section and keep the rest of the file. It names the "
                            "variable that holds the token, not the token."
                        ),
                        template=(
                            "[mcp_servers.flow-steward]\n"
                            'url = "{mcp_url}"\n'
                            'bearer_token_env_var = "FLOW_STEWARD_TOKEN"\n'
                        ),
                    ),
                    _TOKEN_ENVIRONMENT,
                ),
            ),
            _local_developer(OPENAI_CLI_ID, archive="package_archive"),
        ),
    ),
    AgentClient(
        id="cursor",
        label="Cursor",
        connection=CONNECTION_LOCAL,
        matrix_section="Cursor",
        description_key="react.agent_access.client_cursor_description",
        description="The Cursor desktop app.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_PERSONAL_TOKEN,
                verified=False,
                readme_anchor="cursor",
                steps=(
                    _download_package("package_archive"),
                    _OPEN_README,
                    SetupStep(
                        id="mcp_config",
                        kind="file",
                        file_name="mcp.json",
                        title_key="react.agent_access.step_cursor_config",
                        title="Add the server to ~/.cursor/mcp.json",
                        description_key="react.agent_access.step_cursor_config_description",
                        description=(
                            "Merge this entry into the file's mcpServers. Cursor reads the "
                            "token from the variable, so the file holds no secret."
                        ),
                        template=(
                            "{\n"
                            '  "mcpServers": {\n'
                            '    "flow-steward": {\n'
                            '      "url": "{mcp_url}",\n'
                            '      "headers": {"Authorization": "Bearer ${env:FLOW_STEWARD_TOKEN}"}\n'
                            "    }\n"
                            "  }\n"
                            "}\n"
                        ),
                    ),
                    _TOKEN_ENVIRONMENT,
                ),
            ),
            _local_developer("cursor", archive="package_archive"),
        ),
    ),
    AgentClient(
        id="gemini_cli",
        label="Gemini CLI",
        connection=CONNECTION_LOCAL,
        matrix_section="Gemini CLI",
        description_key="react.agent_access.client_gemini_cli_description",
        description="The Gemini CLI terminal client.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_PERSONAL_TOKEN,
                verified=False,
                readme_anchor="gemini-cli",
                steps=(
                    _download_package("package_tarball"),
                    _OPEN_README,
                    SetupStep(
                        id="mcp_add",
                        kind="command",
                        title_key="react.agent_access.step_gemini_add",
                        title="Add the server",
                        description_key="react.agent_access.step_gemini_add_description",
                        description=(
                            "Gemini CLI keeps the token in its own settings file. Run the command "
                            "only on your own computer."
                        ),
                        template=(
                            "gemini mcp add --transport http "
                            '--header "Authorization: Bearer {token}" flow-steward {mcp_url}'
                        ),
                    ),
                ),
            ),
            _local_developer("gemini-cli", archive="package_tarball"),
        ),
    ),
    AgentClient(
        id="claude_ai",
        label="Claude.ai and Cowork",
        connection=CONNECTION_CLOUD,
        matrix_section="Claude.ai and Cowork",
        description_key="react.agent_access.client_claude_ai_description",
        description="Claude chat on the web, desktop and mobile, and Cowork tasks.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_OAUTH,
                verified=False,
                steps=(
                    SetupStep(
                        id="upload_plugin",
                        kind="link",
                        link="release_url",
                        archive="package_archive",
                        title_key="react.agent_access.step_claude_upload",
                        title="Upload the package",
                        description_key="react.agent_access.step_claude_upload_description",
                        description=(
                            "Download {archive} from the Flow Steward {core_version} release and "
                            "upload it in Claude under Customize > Plugins > Upload plugin."
                        ),
                    ),
                    SetupStep(
                        id="custom_connector",
                        kind="value",
                        template="{mcp_url}",
                        title_key="react.agent_access.step_claude_connector",
                        title="Add Flow Steward as a custom connector",
                        description_key="react.agent_access.step_claude_connector_description",
                        description=(
                            "Add a custom connector with this address. Claude opens Flow "
                            "Steward: sign in, choose the access and approve it. You copy no "
                            "token."
                        ),
                    ),
                ),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=False,
                auth=AUTH_NONE,
                verified=False,
                message_key="react.agent_access.reason_no_terminal",
                message=(
                    "Chat has no terminal, and file and terminal access in Cowork is not "
                    "verified for this package."
                ),
            ),
        ),
    ),
    AgentClient(
        id="chatgpt",
        label="ChatGPT",
        connection=CONNECTION_CLOUD,
        matrix_section="ChatGPT",
        description_key="react.agent_access.client_chatgpt_description",
        description="ChatGPT chat and Work on the web, desktop and mobile.",
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_OAUTH,
                verified=False,
                steps=(
                    SetupStep(
                        id="listing_pending",
                        kind="notice",
                        title_key="react.agent_access.step_chatgpt_listing",
                        title="The package is not in the OpenAI plugin directory yet",
                        description_key="react.agent_access.step_chatgpt_listing_description",
                        description=(
                            "Until it is listed, ChatGPT works with the MCP tools alone, without "
                            "the package's instructions."
                        ),
                    ),
                    SetupStep(
                        id="custom_connector",
                        kind="value",
                        template="{mcp_url}",
                        title_key="react.agent_access.step_chatgpt_connector",
                        title="Add Flow Steward as a custom MCP connector",
                        description_key="react.agent_access.step_chatgpt_connector_description",
                        description=(
                            "Add a custom MCP connector with this address. ChatGPT opens Flow "
                            "Steward: sign in, choose the access and approve it. You copy no "
                            "token."
                        ),
                    ),
                ),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=False,
                message_key="react.agent_access.reason_listing_not_published",
                message=(
                    "The Developer listing for ChatGPT's desktop coding agent is not published "
                    "yet. Chat itself has no terminal."
                ),
            ),
        ),
    ),
)

#: The request the user makes first, the same for every client with MCP access.
FIRST_REQUEST_STEP = SetupStep(
    id="first_request",
    kind="value",
    title_key="react.agent_access.step_first_request",
    title="Make the first request",
    description_key="react.agent_access.step_first_request_description",
    description=(
        "Ask your agent this. It should answer from the MCP tools and report a permission "
        "error as an error."
    ),
    template="List my Flow Steward projects and their enabled workflows.",
)


def agent_client(client_id: str) -> AgentClient | None:
    normalized = str(client_id or "").strip().lower()
    return next((client for client in CLIENTS if client.id == normalized), None)


def agent_skill_package(package_id: str) -> AgentSkillPackage | None:
    normalized = str(package_id or "").strip().lower()
    return next((package for package in PACKAGES if package.id == normalized), None)


def client_package_setup(client: AgentClient, package_id: str) -> ClientPackageSetup | None:
    return next((setup for setup in client.packages if setup.package_id == package_id), None)


__all__ = [
    "AUTH_NONE",
    "AUTH_OAUTH",
    "AUTH_PERSONAL_TOKEN",
    "CLIENTS",
    "CONNECTION_CLOUD",
    "CONNECTION_LOCAL",
    "DEVELOPER_PACKAGE_ID",
    "FIRST_REQUEST_STEP",
    "MARKETPLACE_ARCHIVE",
    "OPERATOR_PACKAGE_ID",
    "PACKAGES",
    "REPOSITORY_URL",
    "TOKEN_PLACEHOLDER",
    "AgentClient",
    "AgentSkillPackage",
    "ClientPackageSetup",
    "SetupStep",
    "agent_client",
    "agent_skill_package",
    "client_package_setup",
]
