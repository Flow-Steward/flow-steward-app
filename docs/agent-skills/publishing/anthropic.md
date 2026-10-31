# Anthropic directory submission

Two **Plugin bundle** submissions from this repository, one per package. The
directory reads only the plugin folder, so no separate repository is needed.
A listing serves chat, Cowork and Claude's terminal and IDE clients; each
surface loads the parts it supports (see [COMPATIBILITY.md](../COMPATIBILITY.md)).

The Flow Steward MCP server is **not** submitted as an MCP connector: every
installation has its own address, and a connector listing for arbitrary
customer domains is not offered. The Operator plugin asks each user for their
own address and token instead.

## Source step

| Field | Operator | Developer |
| --- | --- | --- |
| Repository | `Flow-Steward/flow-steward-app` | `Flow-Steward/flow-steward-app` |
| Plugin path | `docs/agent-skills/flow-steward-operator` | `docs/agent-skills/flow-steward-developer` |
| Branch or tag | The release tag, for example `v0.9.7`; move it per release or follow `main` | Same |

## Listing details

Read by the portal from `.claude-plugin/plugin.json` and the package README;
both are generated from `docs/agent-skills/packages.json`:

| Field | Operator | Developer |
| --- | --- | --- |
| `name` | `flow-steward-operator` | `flow-steward-developer` |
| `displayName` | Flow Steward Operator | Flow Steward Developer |
| Description | `description` in `packages.json` | `description` in `packages.json` |
| `license` | `Apache-2.0` | `Apache-2.0` |
| `icon` | the package icon (generated from the product favicon) | same |
| `supportUrl`, `privacyPolicyUrl`, `termsOfServiceUrl`, `documentationUrl` | `TODO(owner)`, see [README.md](README.md) | same |

Before the first submission, add the owner's URLs as directory listing fields:
extend `claude_manifest` in `infra/tooling/agent-skills/sync_packages.py` and
`packages.json` with them, regenerate and commit. `claude plugin validate` before version
2.1.281 reports these fields, and `icon`, as unknown-field warnings; the
plugins still load.

## Data handling and compliance steps

Answer from [data-handling.md](data-handling.md). Contact email:
`TODO(owner): publisher contact email`.

## Review notes

- The plugins contain only skills, manifests, an icon and a README: no hooks,
  scripts, executables, bundled MCP server code or package launchers.
- Operator declares one remote MCP server whose URL is the user option
  `${user_config.mcp_url}` and whose `Authorization` header uses the sensitive
  option `${user_config.mcp_token}`. There is no fixed endpoint: Flow Steward is
  self-hosted, and each user connects to their own installation. In chat the
  entry is ignored and the skills work without it.
- The skills post to GitHub (`Flow-Steward/flow-steward-app`,
  `Flow-Steward/workflows`, `Flow-Steward/extensions`) only when the user asks
  and approves the text, through the user's own GitHub tools.
- How to test without an installation: install a package, open its skill list,
  and ask "How do I report a Flow Steward bug?" (both packages). With an
  installation: set the Operator options and ask "List my Flow Steward
  projects".

## Steps for the publisher account owner

1. Make sure the organization's plan and your role can submit, and connect your
   GitHub account with push access to `Flow-Steward/flow-steward-app` in that
   organization.
2. Locally, from the repository root at the tag:
   `claude plugin validate docs/agent-skills/flow-steward-operator` and the same
   for `flow-steward-developer`.
3. At claude.ai/directory/manage: **Submit new > Plugin bundle**, fill the
   Source step from the table above, **Validate**, fix any blocking finding in
   the repository, push, **Re-validate**.
4. Check **Listing details**, answer **Data handling**, complete **Compliance**.
5. In **Review and submit** choose how new versions arrive (push webhook needs
   repository admin access, or scheduled check) and **Submit for review**.
6. Repeat for the second package; each folder is its own submission.
7. When a version passes, select **Publish**; set the statuses in
   [COMPATIBILITY.md](../COMPATIBILITY.md).

Sources: [submit a plugin](https://claude.com/docs/plugins/submit),
[pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist),
[platform support](https://claude.com/docs/plugins/platform-support),
[manifest reference](https://code.claude.com/docs/en/plugins-reference).
