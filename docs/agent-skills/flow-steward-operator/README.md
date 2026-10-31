# Flow Steward Operator

Agent skills for people who build and run automations in Flow Steward through
an AI agent. The agent works through your Flow Steward MCP connection; it needs
no source checkout and no terminal.

| Skill | Use |
| --- | --- |
| `flow-steward-workflow-authoring` | Find what exists, create skills and agents, compose, validate and save workflows as disabled drafts, enable and run them on request, change existing workflows, repair disabled objects, and answer questions about runs, errors, approvals and reports. |
| `flow-steward-report-issue` | Report a bug or request a feature on GitHub `Flow-Steward/flow-steward-app` with the System Diagnostics report, without changing anything. |
| `flow-steward-publish-workflow` | Share a workflow as a template in the public catalog `Flow-Steward/workflows`. |

Extension development and code contributions are in the separate
**Flow Steward Developer** package (`flow-steward-developer`). Install either
package or both; neither needs the other.

## Before you start

- Connect your Flow Steward MCP server in the client, with your installation's
  address and a personal token, as your installation's AI-agent setup
  describes. The package contains no address and no token. Only its Claude
  plugin manifest declares the server, and it asks you for both values (see
  below); in every other client you add the server yourself. Updating the
  package never changes your connection.
- The token decides what the agent may do, not the package. Reading runs and
  reports needs the matching read scopes; creating, changing and enabling
  agents, skills and workflows needs an administrator user and a token with
  `admin:all`.
- Reporting issues and publishing templates use GitHub through your client's
  own GitHub tools or the `gh` CLI. Without them the skills prepare the text and
  you submit it in the browser.

## Install

The directory containing this README is the plugin root: `plugin.json`
(Agent Plugins 1.0.0, also read by the OpenAI plugin directory),
`.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, the package icon
and `skills/`. Install that directory, not only `.claude-plugin/` and not the
whole application repository. Review the files first and keep a copy of the
version you replace.

Each Flow Steward release on GitHub also carries this package as
`flow-steward-operator-<version>.zip` and `.tar.gz`, with the plugin root at
the archive root, and a marketplace archive with both packages,
`flow-steward-agent-skills-<Core version>.zip`. Check a download against the
release's `SHA256SUMS`. Which clients support which setup is listed in the
[compatibility matrix](https://github.com/Flow-Steward/flow-steward-app/blob/main/docs/agent-skills/COMPATIBILITY.md).

The next example uses a local marketplace folder of your choice that holds this
directory at `plugins/flow-steward-operator`.

### Codex

Create or extend `.agents/plugins/marketplace.json` in the marketplace folder;
keep other entries:

```json
{
  "name": "flow-steward-local",
  "plugins": [{
    "name": "flow-steward-operator",
    "source": {"source": "local", "path": "./plugins/flow-steward-operator"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

From the marketplace folder, check your CLI's help, then:

```bash
codex plugin marketplace add .
codex plugin add flow-steward-operator@flow-steward-local
```

Restart the client and find the three skills in its skills listing. To update,
replace the plugin directory and run
`codex plugin marketplace upgrade flow-steward-local`; the client runs a cached
copy, so compare the installed files with the new ones before relying on the
update. To remove the plugin, delete its entry from `marketplace.json` (or run
`codex plugin marketplace remove flow-steward-local` to drop the whole local
marketplace) and restart. See the [Codex plugin documentation](https://developers.openai.com/plugins/build/plugins).

### Cursor

Copy this directory to `~/.cursor/plugins/local/flow-steward-operator`, reload
the window and find the skills under Customize. Your organization must allow
local plugin imports. Cursor reads `.cursor-plugin/plugin.json`. To update,
replace the directory and reload; to remove, delete it and reload. See
[Cursor plugins](https://cursor.com/docs/plugins).

The Cursor plugin carries no MCP server: add the Flow Steward server in Cursor's
own MCP settings, as the compatibility matrix shows.

### Claude Code

The application repository's `docs/agent-skills` folder, and the
`flow-steward-agent-skills` folder inside the release's marketplace archive,
are a ready local marketplace named `flow-steward` that lists both packages:

```bash
claude plugin marketplace add /absolute/path/to/flow-steward-agent-skills
claude plugin install flow-steward-operator@flow-steward
claude plugin details flow-steward-operator@flow-steward
```

`details` lists the three skills. In a session they appear as
`/flow-steward-operator:flow-steward-workflow-authoring` and so on. For one
session without installing, start the client with
`claude --plugin-dir /absolute/path/to/flow-steward-operator`; the option also
takes the release's `flow-steward-operator-<version>.zip`.

The plugin declares the Flow Steward MCP server `flow-steward` with two
options: your installation's MCP address and your personal token. The token is
masked and kept in the system credential store, never in settings or in the
plugin. Until both are set the server is not started and only the skills load;
the install command prints a reminder. Set them in a session with
`/plugin configure flow-steward-operator@flow-steward`. If you added the same
server earlier with `claude mcp add`, remove that entry so the agent does not
see every tool twice. The options and the token survive plugin updates and are
deleted when you uninstall the plugin.

To update, put the new release's marketplace folder in place of the old one
(each release raises `version`), run `claude plugin marketplace update flow-steward`
and `claude plugin update flow-steward-operator@flow-steward`, and
restart. To remove: `claude plugin uninstall flow-steward-operator@flow-steward`,
then `claude plugin marketplace remove flow-steward` if nothing else uses
it.

### Gemini CLI

Extract the release's `flow-steward-operator-<version>.tar.gz` into an empty
folder named `flow-steward-operator`; `gemini-extension.json` is at its root.
Then run `gemini extensions install /absolute/path/to/flow-steward-operator`,
`gemini extensions update flow-steward-operator` after replacing the folder with a
newer release, and `gemini extensions uninstall flow-steward-operator` to remove it.
Google replaced Gemini CLI with Antigravity CLI for unpaid and Google One
accounts; see [Gemini CLI extensions](https://geminicli.com/docs/extensions/reference/).
The extension carries no MCP server: add the Flow Steward server to Gemini
CLI's own settings.

### Individual skills

You can instead copy a whole folder from `skills/` into the client's own skills
directory (`~/.agents/skills/`, `~/.cursor/skills/` or `~/.claude/skills/`, or
the project equivalents). Copy `flow-steward-workflow-authoring` together with
its `references/` folder. Do not install the same skill both ways.

## Check that it works

1. The client lists the three skills.
2. The Flow Steward MCP server is connected and its tool list contains the
   tools your token allows, for example `project_list` and `workflow_list`.
3. Ask: "List my Flow Steward projects and their enabled workflows." The agent
   should use the MCP tools and report a permission error as an error.

Then, for example: "Build a workflow that summarises new support emails every
morning", "Why did last night's run of the stock import fail?", "Show pending
approvals for this project" or "Report this error as a Flow Steward bug".

## Source

In the application repository this package lives in
`docs/agent-skills/flow-steward-operator`. Its manifests and icon are generated
from `docs/agent-skills/packages.json` and the product icon, and its
`skills/flow-steward-report-issue` is a copy of
`docs/agent-skills/shared/skills/flow-steward-report-issue`, which both
packages ship: change the source and run
`infra/tooling/agent-skills/sync_packages.py`, never the generated file. A unit
test fails when a generated file differs from its source. Release archives are
built from a committed revision with `infra/tooling/agent-skills/build_release.py`.
