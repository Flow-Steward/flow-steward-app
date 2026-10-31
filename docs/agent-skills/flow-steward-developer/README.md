# Flow Steward Developer

Agent skills for developers who build Flow Steward extensions or change the
core repository. They work from a terminal with the existing Flow Steward CLI,
the extension SDK, Git and Docker. They need no Flow Steward MCP connection and
no token.

| Skill | Use |
| --- | --- |
| `flow-steward-extension-authoring` | Build or change an extension against the target release's manifest, SDK and CLI lifecycle, and validate and test it. |
| `flow-steward-publish-extension` | Release an extension, and later versions, and list it in the public catalog `Flow-Steward/extensions`. |
| `flow-steward-contribute-code` | Fix a bug or propose an improvement in `Flow-Steward/flow-steward-app`, from the issue to a reviewed pull request. |
| `flow-steward-report-issue` | Report a bug or request a feature on GitHub with the System Diagnostics report. |

Building and running automations through MCP is in the separate
**Flow Steward Operator** package (`flow-steward-operator`). Install either
package or both; neither needs the other.

## Before you start

- A client that can run terminal commands and edit files.
- Git, Docker with Compose v2, and a Flow Steward application checkout
  installed in the Development environment for validation and tests.
- GitHub access through your client's own GitHub tools or the `gh` CLI for
  forks, releases and pull requests.

When one of them is missing, the skills say which step it blocks instead of
working around it. This package contains no address, token or server
configuration.

## Install

The directory containing this README is the plugin root: `plugin.json`
(Agent Plugins 1.0.0, also read by the OpenAI plugin directory),
`.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, the package icon
and `skills/`. Install that directory, not only `.claude-plugin/` and not the
whole application repository. Review the files first and keep a copy of the
version you replace.

Each Flow Steward release on GitHub also carries this package as
`flow-steward-developer-<version>.zip` and `.tar.gz`, with the plugin root at
the archive root, and a marketplace archive with both packages,
`flow-steward-agent-skills-<Core version>.zip`. Check a download against the
release's `SHA256SUMS`. Which clients support which setup is listed in the
[compatibility matrix](https://github.com/Flow-Steward/flow-steward-app/blob/main/docs/agent-skills/COMPATIBILITY.md).

The next example uses a local marketplace folder of your choice that holds this
directory at `plugins/flow-steward-developer`.

### Codex

Create or extend `.agents/plugins/marketplace.json` in the marketplace folder;
keep other entries:

```json
{
  "name": "flow-steward-local",
  "plugins": [{
    "name": "flow-steward-developer",
    "source": {"source": "local", "path": "./plugins/flow-steward-developer"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

From the marketplace folder, check your CLI's help, then:

```bash
codex plugin marketplace add .
codex plugin add flow-steward-developer@flow-steward-local
```

Restart the client and find the four skills in its skills listing. To update,
replace the plugin directory and run
`codex plugin marketplace upgrade flow-steward-local`; the client runs a cached
copy, so compare the installed files with the new ones before relying on the
update. To remove the plugin, delete its entry from `marketplace.json` (or run
`codex plugin marketplace remove flow-steward-local` to drop the whole local
marketplace) and restart. See the [Codex plugin documentation](https://developers.openai.com/plugins/build/plugins).

### Cursor

Copy this directory to `~/.cursor/plugins/local/flow-steward-developer`, reload
the window and find the skills under Customize. Your organization must allow
local plugin imports. Cursor reads `.cursor-plugin/plugin.json`. To update,
replace the directory and reload; to remove, delete it and reload. See
[Cursor plugins](https://cursor.com/docs/plugins).

### Claude Code

The application repository's `docs/agent-skills` folder, and the
`flow-steward-agent-skills` folder inside the release's marketplace archive,
are a ready local marketplace named `flow-steward` that lists both packages:

```bash
claude plugin marketplace add /absolute/path/to/flow-steward-agent-skills
claude plugin install flow-steward-developer@flow-steward
claude plugin details flow-steward-developer@flow-steward
```

`details` lists the four skills. In a session they appear as
`/flow-steward-developer:flow-steward-extension-authoring` and so on. For one
session without installing, start the client with
`claude --plugin-dir /absolute/path/to/flow-steward-developer`; the option also
takes the release's `flow-steward-developer-<version>.zip`.

To update, put the new release's marketplace folder in place of the old one
(each release raises `version`), run `claude plugin marketplace update flow-steward`
and `claude plugin update flow-steward-developer@flow-steward`, and
restart. To remove: `claude plugin uninstall flow-steward-developer@flow-steward`,
then `claude plugin marketplace remove flow-steward` if nothing else uses
it.

### Gemini CLI

Extract the release's `flow-steward-developer-<version>.tar.gz` into an empty
folder named `flow-steward-developer`; `gemini-extension.json` is at its root.
Then run `gemini extensions install /absolute/path/to/flow-steward-developer`,
`gemini extensions update flow-steward-developer` after replacing the folder with a
newer release, and `gemini extensions uninstall flow-steward-developer` to remove it.
Google replaced Gemini CLI with Antigravity CLI for unpaid and Google One
accounts; see [Gemini CLI extensions](https://geminicli.com/docs/extensions/reference/).

### Individual skills

You can instead copy a whole folder from `skills/` into the client's own skills
directory (`~/.agents/skills/`, `~/.cursor/skills/` or `~/.claude/skills/`, or
the project equivalents). Do not install the same skill both ways.

## Check that it works

1. The client lists the four skills.
2. Ask, in a Flow Steward application checkout: "Which checks does the
   extension quality gate run?" The agent should answer from the checkout's
   `docs/standards/` and `lint.sh` rather than from memory.

Then, for example: "Create an extension that reads orders from our ERP",
"Publish version 1.2.0 of this extension to the catalog" or "Fix issue 123 and
open a pull request".

## Source

In the application repository this package lives in
`docs/agent-skills/flow-steward-developer`. Its manifests and icon are generated
from `docs/agent-skills/packages.json` and the product icon, and its
`skills/flow-steward-report-issue` is a copy of
`docs/agent-skills/shared/skills/flow-steward-report-issue`, which both
packages ship: change the source and run
`infra/tooling/agent-skills/sync_packages.py`, never the generated file. A unit
test fails when a generated file differs from its source. Release archives are
built from a committed revision with `infra/tooling/agent-skills/build_release.py`.
