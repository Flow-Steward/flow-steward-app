# OpenAI plugin directory submission

Two **skills-only** plugins, one per package, uploaded as the release zips
`flow-steward-operator-<version>.zip` and `flow-steward-developer-<version>.zip`.
One listing serves ChatGPT and the desktop coding agent.

The Flow Steward MCP server is **not** part of either listing. A listing with an
MCP server needs one stable public address, and per-customer address templates
are available only to trusted developers; this status is not requested. Users
connect their own installation as a custom MCP server instead (in ChatGPT after
OAuth support, task 5). An MCP server cannot be added to a skills-only listing
later; if that changes, a new plugin is needed.

## Package fields

Read from the root `plugin.json`, `extensions["com.openai"].interface`,
generated from `docs/agent-skills/packages.json`:

| Field | Limit | Operator | Developer |
| --- | --- | --- | --- |
| `displayName` | 30 | Flow Steward Operator | Flow Steward Developer |
| `shortDescription` | 30 | Build and run automations | Build Flow Steward extensions |
| `longDescription` | 4000 | `long_description` in `packages.json` | `long_description` in `packages.json` |
| `developerName` | 80 | Flow Steward | Flow Steward |
| `category` | — | Productivity | Productivity |
| `logo`, `composerIcon` | square, at least 48 px | the package icon (SVG) | same |
| `websiteURL`, `supportURL`, `privacyPolicyURL`, `termsOfServiceURL` | HTTPS | `TODO(owner)`, entered in the dashboard | same |

The zips contain no `apps`, `.app.json`, hooks, MCP configuration or secrets,
which the upload would refuse.

## Review notes

- Skills-only plugins: no MCP review cases or demo recording are required.
- Operator's skills work with a Flow Steward MCP server the user connects
  separately; without it they explain how to connect and do not invent
  results.
- The skills post to GitHub only when the user asks and approves the text.
- Data handling: [data-handling.md](data-handling.md).

## Steps for the publisher account owner

1. In the OpenAI organization, be the owner or get **Apps Management Write**,
   and complete business verification for "Flow Steward".
2. Release the archives for the Core tag ([README.md](README.md)) and verify
   the zips against `SHA256SUMS`.
3. Plugins > **Upload new or existing plugin**, choose the verified developer
   identity, upload `flow-steward-operator-<version>.zip`.
4. Enter the four URLs, upload the primary icon (the same SVG), wait for the
   skill scans, fix any finding in the repository, rebuild and re-upload.
5. **Submit for review** and complete the policy attestations; feedback comes
   by email to `TODO(owner): publisher contact email`.
6. After approval, **Publish plugin**. Repeat for the Developer zip.
7. For each later package version, upload the new zip to the existing plugin.
8. Set the statuses in [COMPATIBILITY.md](../COMPATIBILITY.md).

Sources: [plugin submission](https://developers.openai.com/plugins/deploy/submission),
[build plugins](https://developers.openai.com/plugins/build/plugins),
[plugins in ChatGPT](https://learn.chatgpt.com/docs/plugins).
