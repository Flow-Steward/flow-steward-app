# Flow Steward agent plugin

One portable plugin with six skills. The workflow skill uses your already
configured authenticated Flow Steward MCP connection; the others work from a
terminal with `git`, the `gh` CLI or a browser, and, for development, a Flow
Steward application checkout.

| Skill | Audience and use |
| --- | --- |
| `flow-steward-workflow-authoring` | End users: reuse/compose local workflows, inspect runs, errors and approvals, request operational reports, export or import a workflow. |
| `flow-steward-report-issue` | End users and their agents: report a bug or request a feature on GitHub `Flow-Steward/flow-steward-app`, with the redacted System Diagnostics report attached. |
| `flow-steward-publish-workflow` | Workflow authors: share a workflow template in the public catalog `Flow-Steward/workflows`. |
| `flow-steward-extension-authoring` | Developers: build extensions against current manifests, SDK and supported CLI lifecycle. |
| `flow-steward-publish-extension` | Developers: publish an extension, and its later versions, to the public catalog `Flow-Steward/extensions`. |
| `flow-steward-contribute-code` | Contributors: fix a bug or propose an improvement in the core repository, from the issue to a reviewed pull request. |

The directory containing this README is the actual plugin root. Root
`plugin.json` targets Agent Plugins 1.0.0 for Codex and Cursor;
`.claude-plugin/plugin.json` provides Claude Code metadata. Both discover the
same `skills/` sources. This skills-only package contains no endpoint, token,
server process, hooks or dependencies. Configure MCP separately using your
installation's connection instructions. The workflow, issue and workflow
publishing skills work without a platform checkout; extension work and code
contributions use the target release's contracts and the supported Development
environment.

## Install and verify

Review the package before installing. Extract an archive to obtain
`flow-steward/plugin.json`, `flow-steward/.claude-plugin/plugin.json` and the six
`flow-steward/skills/<name>/SKILL.md` files. Use the extracted `flow-steward`
directory as the plugin root. Do not load only `.claude-plugin/` or install the
whole application repository as a plugin. Inspect an existing installation before
replacing files; keep a rollback copy of the version you use.

### Codex

Put the extracted directory at `plugins/flow-steward` inside a local marketplace
root of your choosing. Create or extend `.agents/plugins/marketplace.json` there
with this entry; preserve other entries:

```json
{
  "name": "flow-steward-local",
  "plugins": [{
    "name": "flow-steward",
    "source": {"source": "local", "path": "./plugins/flow-steward"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

From that marketplace root, inspect your installed CLI's help, then:

```bash
codex plugin marketplace add .
codex plugin list --marketplace flow-steward-local --available --json
codex plugin add flow-steward@flow-steward-local
```

Restart the client and verify the six skills in its available skills listing.
The local marketplace is private; this package is not listed in a public
marketplace. [Codex packaging documentation](https://developers.openai.com/plugins/build/plugins)
explains local sources and cache behavior.

### Cursor

Copy the extracted root to `~/.cursor/plugins/local/flow-steward`, reload the
window and inspect Customize for the six skills. Local imports must be allowed by
your organization. A marketplace installation of the same name takes precedence;
symlinks outside the local plugins directory are skipped. See
[Cursor local plugins](https://cursor.com/docs/plugins).

### Claude Code

For a session-local load, inspect `claude --help` and run:

```bash
claude --plugin-dir /absolute/path/to/flow-steward
```

Verify the skills in the session's skill listing, for example
`/flow-steward:flow-steward-workflow-authoring`,
`/flow-steward:flow-steward-report-issue` and
`/flow-steward:flow-steward-contribute-code`.
`claude plugin validate /absolute/path/to/flow-steward` checks the manifest;
it does not prove tool connectivity or successful use. Persistent installation
uses a configured Claude marketplace and its normal plugin lifecycle. See
[Claude plugin reference](https://code.claude.com/docs/en/plugins-reference).

### Optional individual skill installation

Copy any complete leaf under `skills/` into one native skills directory:
Codex `.agents/skills/` or `~/.agents/skills/`; Cursor `.agents/skills/`,
`.cursor/skills/` or their user equivalents; Claude `.claude/skills/` or
`~/.claude/skills/`. Use the same canonical bytes and keep the leaf name.
Standalone names are `$flow-steward-workflow-authoring` /
`$flow-steward-extension-authoring` in Codex and the corresponding `/` names
in Claude; the other skills follow the same pattern with their leaf names. For plugin installs use the client's discovered names; Claude adds
its plugin namespace. Avoid duplicate standalone and plugin installs.
Native paths are documented for [Codex](https://developers.openai.com/codex/skills),
[Cursor](https://cursor.com/docs/skills) and
[Claude](https://code.claude.com/docs/en/skills).

## Use and update

After client discovery, confirm the configured MCP connects and `tools/list`
returns the expected token-scoped tools. Try a requested scoped read, such as
listing workflows or inspecting a known job; document permission errors as
errors. For example: "Find my project's existing workflow for weekly summaries",
"Show failed runs and pending approvals for this workflow", or "Report activity
for this project for the last seven days". Anyone can ask "Report this error
as a Flow Steward bug" or "Publish this workflow to the Flow Steward catalog".
Developers can ask to build, validate and publish an extension, or to fix a
core bug and open a pull request.

On update, compare package version and hashes, replace the reviewed plugin root
at the same source location, refresh/restart the client and verify skill discovery
again. Codex loads a cached installed copy; confirm the refreshed install's bytes
before claiming an update took effect. Session-local Claude loads use the new
root on the next session. Cursor local copies need a reload. Marketplace-managed
installs use that client's marketplace update lifecycle. Never overwrite MCP
credentials while updating skills.

## Source and archive assembly

In the application repository the canonical root is
`docs/agent-skills/flow-steward`. A standard archive command, run from
`docs/agent-skills`, includes the hidden Claude overlay:

```bash
zip -r /absolute/output/flow-steward-plugin.zip flow-steward
```

Inspect the archive entries and extracted manifests before distribution. Website
downloads must be assembled from this exact root, never a second manual skill
edition. No packaging generator is needed. Client discovery and live execution
must be verified separately from archive/manifest checks; this README is not a
claim of deployment or client runtime verification.
