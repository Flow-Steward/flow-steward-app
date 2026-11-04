# Agent skill package compatibility

Which AI clients can use the **Flow Steward Operator** (`flow-steward-operator`)
and **Flow Steward Developer** (`flow-steward-developer`) packages, in which
format, with which setup and limits. Operator package version 1.0.2; Developer
package version 1.0.0. Client documentation checked on 2026-10-31.

Every setup connects to **your own** Flow Steward installation. No released
package contains an address, a token or a demonstration server, and no catalog
listing replaces your address with a fixed one. Only the files the AI agents
page builds for you carry your address and token: the personal plugin, and
the MCP settings it shows for the other clients.

## Status legend

Each row carries four separate statuses:

- **Prepared**: the package files and instructions for this client are in this
  repository and in the release archives.
- **Verified**: install, skill discovery, update and removal were run in the
  client; the evidence is named in the row. "Not verified" says why.
- **Submitted**: sent to the catalog's review by the publisher account owner.
- **Published**: listed in the catalog.

Submission is a separate step after the materials in
[publishing/](publishing/README.md) are complete and the publisher account is
ready; a positive review decision is not guaranteed.

**OAuth sign-in (task 5)** marks a cloud client: the client's servers open the
MCP connection and accept only OAuth sign-in. Flow Steward offers it when the
installation has a public HTTPS address and an administrator has turned on cloud
AI apps (Settings → Security); you then sign in to Flow Steward and approve the
access in the window the client opens. Its skills can be installed without it.
No cloud client has been verified against a live installation yet.

Each installation's **Settings → Account → AI agents** page walks through the
rows below with the installation's own MCP address filled in, creates the
personal token where a row uses one, puts it straight into what the user
downloads or copies, and names what blocks a cloud client. The page builds its
downloads in the browser from the package files bundled with the web UI, so the
token never travels back to the server. Options a client or a role cannot use
are not shown.

**Developer** functions need a terminal and file access. They are listed only
for clients that have both. The Operator offline workflow converter also needs
a terminal, Python and file access; skill discovery alone does not establish
that a cloud client can run it.

## Clients

### Claude Code

Terminal, IDE extensions and the desktop app's Code tab; the MCP connection is
opened from your computer. Format: Claude plugin (`.claude-plugin/plugin.json`),
installed from the `flow-steward` marketplace in `docs/agent-skills` or the
release's marketplace archive, or loaded for one session from a release zip.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | All four skills; MCP tools through the server the plugin declares | Personal token: in the personal plugin from the AI agents page it is written into the plugin's server entry as `Authorization: Bearer`; the released plugin asks for it as the masked option `mcp_token`, kept in the system credential store | AI agents page: download the personal plugin, then `claude plugin marketplace add` and `claude plugin install` as the page shows; or the package README with `/plugin configure flow-steward-operator@flow-steward` | The released plugin's server starts only when both options are set; an existing manual server entry for the same installation should be removed; the personal plugin file holds the token | Yes | 1.0.0 and its three skills verified with CLI 2.1.222 (1); 1.0.1, 1.0.2 and the migration skill not verified | No | No |
| Developer | All four skills; terminal and files | None | Package README | Needs Git, Docker and a Flow Steward checkout | Yes | Yes, CLI 2.1.222 in an isolated profile (1) | No | No |

(1) Historical verification covered Operator 1.0.0 with three skills and
Developer 1.0.0: `claude plugin validate` on both packages and the marketplace;
marketplace add, install, `details` (skill inventory), an update exercise,
uninstall and marketplace removal; session load of both release zips. The update
exercise preceded the addition of the migration skill and does not verify the
current Operator 1.0.1 package or its four-skill inventory. For
Operator: install without options leaves the MCP server out and prints a
reminder; with options set, the server connected to a local MCP probe at the
configured address and sent the configured token as `Bearer`; the token stayed
out of the settings file, survived the update and was deleted on uninstall.
Skill listing inside a signed-in session and the interactive `/plugin
configure` dialog were not run: the isolated profile has no sign-in.

### Claude.ai and Cowork

Chat on the web, desktop and mobile, and Cowork tasks; plugins come from the
claude.ai account. Format: the same Claude plugin, uploaded as a release zip
(**Customize > Plugins > Upload plugin**) or, after listing, from the Anthropic
directory.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | Skills only; the plugin's MCP server entry is ignored there because its address is a user option | OAuth sign-in (task 5): Flow Steward is added as a custom connector with your address | Upload the zip; add the connector with your address | MCP tools need a public HTTPS address and cloud AI apps turned on; the offline converter requires terminal, Python and file access, which are unverified in Cowork and unavailable in chat; a connector with your own address is added by you, it is not listed in the directory | Yes | Not verified: needs a claude.ai account session | No | No |
| Developer | Not offered: chat has no terminal; Cowork file and terminal access for this package is unverified | — | — | — | — | — | — | — |

### Codex

CLI and the ChatGPT desktop app; the MCP connection is opened from your
computer. Format: Agent Plugins `plugin.json` from a local marketplace (package
README), or later from the OpenAI plugin directory. The IDE extension does not
support plugins, so neither package is offered there; cloud tasks are not
claimed.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | All four skills; MCP tools from a server you add yourself | Personal token in a static header: `http_headers` in `config.toml` (`bearer_token_env_var` also works) | AI agents page: the `[mcp_servers.flow-steward]` section with `url = "<your address>/mcp"` and `http_headers = { "Authorization" = "Bearer <token>" }` filled in, and the skills archive for `~/.agents/skills`; or the package README | The plugin carries no MCP server; `config.toml` then holds the token | Yes | Not verified: the client is not installed on the verification machine | No | No |
| Developer | All four skills; terminal and files | None | Package README | Needs Git, Docker and a Flow Steward checkout | Yes | Not verified: the client is not installed on the verification machine | No | No |

### ChatGPT

Chat and Work on the web, desktop and mobile; OpenAI's servers open the MCP
connection. Format: the OpenAI plugin directory listing, built from the release
zip (Agent Plugins `plugin.json` with OpenAI metadata under `extensions`).

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | Skills-only listing; MCP tools after you connect Flow Steward as a custom MCP server | OAuth sign-in (task 5) | Install from the directory; custom MCP server with your address | A listing with an MCP server needs one public address, and per-customer address templates are reserved for trusted developers, so the listing stays skills-only; an MCP server cannot be added to a skills-only listing later; the offline converter requires terminal, Python and file access and is not claimed for chat | Yes | Not verified: no publisher account or ChatGPT session | No | No |
| Developer | Skills-only listing for the desktop coding agent; not for chat, which has no terminal | None | Install from the directory | Same listing rules | Yes | Not verified: no publisher account or ChatGPT session | No | No |

### Cursor

Desktop app; the MCP connection is opened from your computer. Format: Cursor
plugin (`.cursor-plugin/plugin.json`) copied to `~/.cursor/plugins/local/`;
Cursor can also read the Agent Plugins `plugin.json`. Cloud Agents are not
claimed.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | All four skills; MCP tools from a server you add yourself | Personal token in the MCP headers of `~/.cursor/mcp.json` (`${env:…}` also works) | AI agents page: a `flow-steward` entry with `"url": "<your address>/mcp"` and `"headers": {"Authorization": "Bearer <token>"}` filled in, and the plugin archive for `~/.cursor/plugins/local`; or the package README | The plugin carries no MCP server; `mcp.json` then holds the token; an organization admin may have to allow local plugin imports | Yes | Not verified: the client is not installed on the verification machine | No | No |
| Developer | All four skills; terminal and files | None | Package README | Needs Git, Docker and a Flow Steward checkout | Yes | Not verified: the client is not installed on the verification machine | No | No |

### Gemini CLI

Terminal; the MCP connection is opened from your computer. Google replaced
Gemini CLI with Antigravity CLI for unpaid and Google One accounts on
2026-06-18. Format: the release's `.tar.gz` archive with `gemini-extension.json`
at its root, installed from a local folder. The Gemini chat app is not claimed.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | All four skills; MCP tools from a server you add yourself | Personal token in an `Authorization` header of the server entry; Gemini CLI keeps it in its settings file | Package README for the skills; `gemini mcp add --transport http --header "Authorization: Bearer <token>" flow-steward <your address>/mcp` | The extension carries no MCP server; no gallery listing | Yes | Not verified: the client is not installed on the verification machine | — | — |
| Developer | All four skills; terminal and files | None | Package README | No gallery listing | Yes | Not verified: the client is not installed on the verification machine | — | — |

### Another MCP client

Any client that is not listed above, opens an MCP connection over HTTP from
your computer and, if it can, reads skills from a folder. Format: the usual
`mcp.json` server entry and the skill folders.

| Package | Functions | Auth | Setup | Limits | Prepared | Verified | Submitted | Published |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Operator | MCP tools; the skills where the client reads skill folders | Personal token in the `Authorization: Bearer` header of the server entry | AI agents page: `{"mcpServers": {"flow-steward": {"type": "http", "url": "<your address>/mcp", "headers": {"Authorization": "Bearer <token>"}}}}` filled in, and the skills archive | A client with another settings format needs the same address and header in its own form; the settings then hold the token | Yes | Not verified: depends on the client | — | — |
| Developer | All four skills; terminal and files | None | Package README | Needs Git, Docker and a Flow Steward checkout | Yes | Not verified: depends on the client | — | — |

## Catalogs and what each one indexes

A zip attached to a GitHub Release of `Flow-Steward/flow-steward-app` is an
independent install channel; it is not a catalog listing.

| Catalog | What it reads | Two listings | Status |
| --- | --- | --- | --- |
| Anthropic directory (chat, Cowork, Claude plugins) | This repository, plugin path `docs/agent-skills/flow-steward-operator` or `docs/agent-skills/flow-steward-developer`, on a tracked branch or tag | One submission per plugin folder | Materials prepared; not submitted |
| OpenAI plugin directory (ChatGPT and the desktop coding agent) | An uploaded zip: the release's `<package>-<version>.zip` | One plugin per zip | Materials prepared; not submitted |
| Cursor Marketplace | A public Git repository with `.cursor-plugin/marketplace.json` at its root | One marketplace entry per plugin folder; a separate publication repository is needed because this repository's root does not change for a catalog | Materials prepared; publication repository not created; not submitted |
| Gemini CLI extension gallery | A public repository tagged `gemini-cli-extension` with `gemini-extension.json` at its root | Would need two publication repositories | Deferred; GitHub Release archive only |

Each release archive maps to one Core commit: `release-manifest.json` in the
release records the Core repository, commit, tag and version.

## Sources

- Anthropic: [plugin manifest reference](https://code.claude.com/docs/en/plugins-reference),
  [plugin commands](https://code.claude.com/docs/en/plugins/cli-reference),
  [marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference),
  [platform support](https://claude.com/docs/plugins/platform-support),
  [submit a plugin](https://claude.com/docs/plugins/submit),
  [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist).
- OpenAI: [build plugins](https://developers.openai.com/plugins/build/plugins),
  [plugin submission](https://developers.openai.com/plugins/deploy/submission),
  [plugins in ChatGPT](https://learn.chatgpt.com/docs/plugins),
  [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
- Cursor: [plugins](https://cursor.com/docs/plugins),
  [plugin reference](https://cursor.com/docs/reference/plugins),
  [MCP](https://cursor.com/docs/context/mcp).
- Gemini CLI: [extension reference](https://geminicli.com/docs/extensions/reference/),
  [releasing extensions](https://geminicli.com/docs/extensions/releasing/),
  [MCP servers](https://geminicli.com/docs/tools/mcp-server/).
- [Agent Plugins schema 1.0.0](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json).
