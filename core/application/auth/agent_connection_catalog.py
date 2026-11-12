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
#: A copy field that holds nothing but the MCP endpoint URL.
MCP_URL_TEMPLATE = "{mcp_url}"

#: ``package`` steps: how the browser lays out the archive it builds from the package
#: files bundled with the web UI. The server never sees the archive or the token.
#: Every layout but the skills folder puts several entries at the archive root, so
#: unpacking it on any system makes one folder named after the archive.
#: A ``.claude-plugin`` marketplace named after the package.
ARCHIVE_CLAUDE_MARKETPLACE = "claude_marketplace"
#: A ``.agents/plugins`` marketplace named after the package; the step's
#: ``manifest_dir`` names the folder that holds the client's plugin manifest.
ARCHIVE_AGENTS_MARKETPLACE = "agents_marketplace"
#: The package folder as it is (a client plugin folder), with ``mcp.json`` beside it.
ARCHIVE_PLUGIN_FOLDER = "plugin_folder"
#: The skill folders alone, for a client's skills directory.
ARCHIVE_SKILLS_FOLDER = "skills_folder"
ARCHIVE_LAYOUTS = frozenset(
    {
        ARCHIVE_CLAUDE_MARKETPLACE,
        ARCHIVE_AGENTS_MARKETPLACE,
        ARCHIVE_PLUGIN_FOLDER,
        ARCHIVE_SKILLS_FOLDER,
    }
)

#: The usual ``mcp.json`` shape most MCP clients read, a Claude plugin's included.
MCP_JSON_TEMPLATE = (
    '{\n  "mcpServers": {\n    "flow-steward": {\n'
    '      "type": "http",\n'
    '      "url": "{mcp_url}",\n'
    '      "headers": {"Authorization": "Bearer {token}"}\n'
    "    }\n  }\n}\n"
)
#: Cursor's ``mcp.json``: a remote entry without ``type``.
CURSOR_MCP_JSON_TEMPLATE = (
    '{\n  "mcpServers": {\n    "flow-steward": {\n'
    '      "url": "{mcp_url}",\n'
    '      "headers": {"Authorization": "Bearer {token}"}\n'
    "    }\n  }\n}\n"
)
#: The OpenAI CLI's plugin ``.mcp.json``: a static header is ``http_headers``.
OPENAI_CLI_MCP_JSON_TEMPLATE = (
    '{\n  "mcpServers": {\n    "flow-steward": {\n'
    '      "type": "http",\n'
    '      "url": "{mcp_url}",\n'
    '      "http_headers": {"Authorization": "Bearer {token}"}\n'
    "    }\n  }\n}\n"
)
_TOKEN_IN_FILE = " It holds your token: keep it like a password, do not share it or put it in Git."
_INSTALL_PLUGIN_TITLE_KEY = "react.agent_access.step_install_plugin"
_INSTALL_PLUGIN_TITLE = "Install it"

#: ``platform`` steps for Microsoft 365 Copilot: the preset of
#: ``inbound_oauth_platform_clients`` an administrator registers.
MICROSOFT_365_PRESET = "microsoft_365"

# External client products the user picks; Flow Steward runs none of them. Their names
# appear only here (tests/unit/test_retired_executor_absence.py allows these lines).
CLAUDE_CODE_LABEL = "Claude Code"
OPENAI_CLI_ID = "codex"
OPENAI_CLI_LABEL = "Codex"
#: The folder of the OpenAI CLI's plugin manifest.
OPENAI_CLI_PLUGIN_MANIFEST_DIR = f".{OPENAI_CLI_ID}-plugin"


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
    #: ``link`` (open the release or README), ``command``, ``file``, ``value``, ``notice``,
    #: ``package`` (an archive the browser builds; ``file_name`` names it) or
    #: ``platform`` (an administrator registers a platform OAuth client from
    #: ``platform_preset`` and the browser builds the client's package, ADR 0063).
    kind: str
    title_key: str
    title: str
    description_key: str = ""
    description: str = ""
    #: A note under the step's block: what to change in it, or how to update later.
    hint_key: str = ""
    hint: str = ""
    template: str = ""
    file_name: str = ""
    #: ``link`` steps: which URL the button opens (``release_url`` or ``readme_url``).
    link: str = ""
    #: The release file the description names as ``{archive}``.
    archive: str = ""
    #: ``package`` steps: one of :data:`ARCHIVE_LAYOUTS`.
    archive_layout: str = ""
    #: :data:`ARCHIVE_AGENTS_MARKETPLACE` steps: the client's plugin manifest folder.
    manifest_dir: str = ""
    #: ``platform`` steps: the platform client preset an administrator registers.
    platform_preset: str = ""

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
        version="1.1.0",
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


def _archive_name(package_id: str, client_slug: str) -> str:
    """The personal archive of one package for one client; unpacked, a folder of this name."""
    return f"{package_id}-{client_slug}.zip"


def _plugin_download(
    package_id: str,
    client_slug: str,
    layout: str,
    *,
    template: str = "",
    manifest_dir: str = "",
) -> SetupStep:
    """The plugin archive. With a ``template`` it carries the MCP server, and so the token."""
    common = {
        "id": "download_plugin",
        "kind": "package",
        "archive_layout": layout,
        "file_name": _archive_name(package_id, client_slug),
        "template": template,
        "manifest_dir": manifest_dir,
    }
    if template:
        return SetupStep(
            **common,
            title_key="react.agent_access.step_personal_plugin",
            title="Download the plugin",
            description_key="react.agent_access.step_personal_plugin_description",
            description=_TOKEN_IN_FILE.strip(),
        )
    return SetupStep(
        **common,
        title_key="react.agent_access.step_download_plugin",
        title="Download the plugin",
        description_key="react.agent_access.step_download_plugin_description",
        description="It holds the package's skills. It needs no address and no token.",
    )


def _install_marketplace(package_id: str, client_slug: str, cli: str, verb: str) -> SetupStep:
    """The same commands on every system: from the home folder, add the unpacked folder."""
    folder = _archive_name(package_id, client_slug).removesuffix(".zip")
    template = (
        "cd ~\n"
        f"{cli} plugin marketplace add ./{folder}\n"
        f"{cli} plugin {verb} {package_id}@{package_id}"
    )
    if client_slug == OPENAI_CLI_ID:
        # The OpenAI CLI copies the plugin when it adds it and takes a new copy on a repeat.
        return SetupStep(
            id="install_plugin",
            kind="command",
            title_key=_INSTALL_PLUGIN_TITLE_KEY,
            title=_INSTALL_PLUGIN_TITLE,
            description_key="react.agent_access.step_install_copied_plugin_description",
            description=(
                "Unpack the archive and move the folder it makes to your home folder. Then run "
                "this in a terminal (PowerShell on Windows) and start a new session. The "
                "commands need the {client} command-line tool."
            ),
            hint_key="react.agent_access.step_install_copied_plugin_hint",
            hint=(
                "If the folder has another name, change it in the command. To install a newer "
                "download, run the same commands again."
            ),
            template=template,
        )
    # The claude command loads the plugin from the folder and keeps the first copy of a version.
    return SetupStep(
        id="install_plugin",
        kind="command",
        title_key=_INSTALL_PLUGIN_TITLE_KEY,
        title=_INSTALL_PLUGIN_TITLE,
        description_key="react.agent_access.step_install_kept_plugin_description",
        description=(
            "Unpack the archive and move its folder to your home folder: {client} loads the "
            "plugin from there. Then run this in a terminal (PowerShell on Windows) and start "
            "a new session."
        ),
        hint_key="react.agent_access.step_install_kept_plugin_hint",
        hint=(
            "If the folder has another name, change it in the command. To install a newer "
            "download, first run `claude plugin marketplace remove {package}`."
        ),
        template=template,
    )


_INSTALL_PLUGIN_FOLDER = SetupStep(
    id="install_plugin",
    kind="notice",
    title_key=_INSTALL_PLUGIN_TITLE_KEY,
    title=_INSTALL_PLUGIN_TITLE,
    description_key="react.agent_access.step_install_plugin_folder_description",
    description=(
        "Unpack the archive and move the folder it makes into plugins/local inside the .cursor "
        "folder of your home folder; create plugins/local if it is missing. On a Mac, press "
        "Cmd+Shift+. in Finder to see .cursor. Then run Developer: Reload Window in {client}. "
        "An organization admin may have to allow local plugin imports. To install a newer "
        "download, replace the folder."
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


def _skills_download(package_id: str) -> SetupStep:
    return SetupStep(
        id="download_skills",
        kind="package",
        archive_layout=ARCHIVE_SKILLS_FOLDER,
        file_name=_archive_name(package_id, "skills"),
        title_key="react.agent_access.step_download_skills",
        title="Download the skills",
        description_key="react.agent_access.step_download_skills_description",
        description="The package's skills, one folder each, for your client's skills folder.",
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


def _claude_code(package_id: str, template: str = "") -> tuple[SetupStep, ...]:
    return (
        _plugin_download(package_id, "claude-code", ARCHIVE_CLAUDE_MARKETPLACE, template=template),
        _install_marketplace(package_id, "claude-code", "claude", "install"),
    )


def _openai_cli(package_id: str, template: str = "") -> tuple[SetupStep, ...]:
    return (
        _plugin_download(
            package_id,
            OPENAI_CLI_ID,
            ARCHIVE_AGENTS_MARKETPLACE,
            template=template,
            manifest_dir=OPENAI_CLI_PLUGIN_MANIFEST_DIR,
        ),
        _install_marketplace(package_id, OPENAI_CLI_ID, OPENAI_CLI_ID, "add"),
    )


def _cursor(package_id: str, template: str = "") -> tuple[SetupStep, ...]:
    return (
        _plugin_download(package_id, "cursor", ARCHIVE_PLUGIN_FOLDER, template=template),
        _INSTALL_PLUGIN_FOLDER,
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
                verified=False,
                readme_anchor="claude-code",
                steps=_claude_code(OPERATOR_PACKAGE_ID, MCP_JSON_TEMPLATE),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=True,
                readme_anchor="claude-code",
                steps=_claude_code(DEVELOPER_PACKAGE_ID),
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
                steps=_openai_cli(OPERATOR_PACKAGE_ID, OPENAI_CLI_MCP_JSON_TEMPLATE),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=True,
                readme_anchor=OPENAI_CLI_ID,
                steps=_openai_cli(DEVELOPER_PACKAGE_ID),
            ),
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
                steps=_cursor(OPERATOR_PACKAGE_ID, CURSOR_MCP_JSON_TEMPLATE),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=False,
                readme_anchor="cursor",
                steps=_cursor(DEVELOPER_PACKAGE_ID),
            ),
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
        id="other",
        label="Another MCP client",
        connection=CONNECTION_LOCAL,
        matrix_section="Another MCP client",
        description_key="react.agent_access.client_other_description",
        description=(
            "A client that is not listed: one that connects to an MCP server over HTTP and, "
            "if it can, reads skills from a folder."
        ),
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_PERSONAL_TOKEN,
                verified=False,
                readme_anchor="individual-skills",
                steps=(
                    SetupStep(
                        id="mcp_config",
                        kind="file",
                        file_name="mcp.json",
                        title_key="react.agent_access.step_other_config",
                        title="Add the server to the client's MCP settings",
                        description_key="react.agent_access.step_other_config_description",
                        description=(
                            "Most clients take this JSON. If yours uses another format, keep "
                            "the address and the Authorization header." + _TOKEN_IN_FILE
                        ),
                        template=MCP_JSON_TEMPLATE,
                    ),
                    _skills_download(OPERATOR_PACKAGE_ID),
                ),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=True,
                auth=AUTH_NONE,
                verified=False,
                readme_anchor="individual-skills",
                steps=(_skills_download(DEVELOPER_PACKAGE_ID),),
            ),
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
                        template=MCP_URL_TEMPLATE,
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
                        template=MCP_URL_TEMPLATE,
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
    AgentClient(
        id="microsoft_365_copilot",
        label="Microsoft 365 Copilot",
        connection=CONNECTION_CLOUD,
        matrix_section="Microsoft 365 Copilot",
        description_key="react.agent_access.client_m365_description",
        description=(
            "Copilot Chat for work accounts in Microsoft 365, Teams and Outlook, as an agent "
            "your organisation's administrator adds."
        ),
        packages=(
            ClientPackageSetup(
                package_id=OPERATOR_PACKAGE_ID,
                offered=True,
                auth=AUTH_OAUTH,
                verified=False,
                steps=(
                    SetupStep(
                        id="admin_setup",
                        kind="notice",
                        title_key="react.agent_access.step_m365_admins",
                        title="Two administrators set this up once, in four steps",
                        description_key="react.agent_access.step_m365_admins_description",
                        description=(
                            "1. A Flow Steward administrator creates the Microsoft 365 client "
                            "below. 2. A Microsoft 365 administrator registers it in the Teams "
                            "Developer Portal and gets an OAuth client registration ID. 3. That "
                            "ID is entered below to build the agent package. 4. The Microsoft 365 "
                            "administrator uploads the package and makes the agent available. "
                            "Making the agent available grants nothing in Flow Steward: each "
                            "person still signs in, approves the access, and gets at most what "
                            "their own Flow Steward role allows."
                        ),
                    ),
                    SetupStep(
                        id="platform_client",
                        kind="platform",
                        platform_preset=MICROSOFT_365_PRESET,
                        template=MCP_URL_TEMPLATE,
                        file_name="flow-steward-operator-m365-copilot.zip",
                        title_key="react.agent_access.step_m365_package",
                        title="Register the sign-in and build the agent",
                        description_key="react.agent_access.step_m365_package_description",
                        description=(
                            "Create the Microsoft 365 client here, enter its values in the Teams "
                            "Developer Portal under Tools > OAuth client registration, paste the "
                            "registration ID it gives back, and download the agent package."
                        ),
                    ),
                    SetupStep(
                        id="upload_agent",
                        kind="notice",
                        title_key="react.agent_access.step_m365_upload",
                        title="Upload the agent in Microsoft 365",
                        description_key="react.agent_access.step_m365_upload_description",
                        description=(
                            "In the Microsoft 365 admin center open Agents, choose Upload custom "
                            "agent and pick the downloaded zip, then make it available to the "
                            "people who need it. To try it alone first, upload it in Teams under "
                            "Apps > Manage your apps > Upload a custom app."
                        ),
                    ),
                    SetupStep(
                        id="federated_connector",
                        kind="value",
                        template=MCP_URL_TEMPLATE,
                        title_key="react.agent_access.step_m365_connector",
                        title="Not verified: a connector instead of the agent",
                        description_key="react.agent_access.step_m365_connector_description",
                        description=(
                            "With Microsoft 365 Copilot licences, an administrator can try a "
                            "custom connector (admin center > Copilot > Connectors > Connect to "
                            "MCP server) with this address. Microsoft documents its sign-in form "
                            "with a client secret, and the Microsoft 365 client here has none, "
                            "so this may not work; use the agent package. A connector answers "
                            "in Copilot's own words, without the agent's instructions or report "
                            "view."
                        ),
                    ),
                ),
            ),
            ClientPackageSetup(
                package_id=DEVELOPER_PACKAGE_ID,
                offered=False,
                auth=AUTH_NONE,
                verified=False,
                message_key="react.agent_access.reason_no_terminal_m365",
                message="Copilot Chat has no terminal or file access for this package.",
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
    "ARCHIVE_AGENTS_MARKETPLACE",
    "ARCHIVE_CLAUDE_MARKETPLACE",
    "ARCHIVE_LAYOUTS",
    "ARCHIVE_PLUGIN_FOLDER",
    "ARCHIVE_SKILLS_FOLDER",
    "AUTH_NONE",
    "AUTH_OAUTH",
    "AUTH_PERSONAL_TOKEN",
    "CLIENTS",
    "CONNECTION_CLOUD",
    "CONNECTION_LOCAL",
    "CURSOR_MCP_JSON_TEMPLATE",
    "DEVELOPER_PACKAGE_ID",
    "FIRST_REQUEST_STEP",
    "MARKETPLACE_ARCHIVE",
    "MCP_JSON_TEMPLATE",
    "MICROSOFT_365_PRESET",
    "OPENAI_CLI_MCP_JSON_TEMPLATE",
    "OPENAI_CLI_PLUGIN_MANIFEST_DIR",
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
