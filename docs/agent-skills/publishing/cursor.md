# Cursor Marketplace submission

The Cursor Marketplace indexes a public, open-source Git repository and reviews
every update before publishing. A repository with several plugins needs
`.cursor-plugin/marketplace.json` at its root. This repository's root does not
change for a catalog, so the listing comes from a separate publication
repository that holds only built package files.

## Publication repository

- Name: `TODO(owner): Cursor publication repository`, for example
  `Flow-Steward/agent-skills`, public, Apache-2.0.
- Content: the `flow-steward-agent-skills` folder of the release's marketplace
  archive, unchanged, plus a root `.cursor-plugin/marketplace.json`:

  ```json
  {
    "name": "flow-steward",
    "owner": {"name": "Flow Steward"},
    "metadata": {"description": "Flow Steward agent skill packages"},
    "plugins": [
      {"name": "flow-steward-operator", "source": "flow-steward-operator"},
      {"name": "flow-steward-developer", "source": "flow-steward-developer"}
    ]
  }
  ```

- Every update replaces the two plugin folders with those of a new release and
  names the Core tag and commit from `release-manifest.json` in the commit
  message. Nobody edits skills there; changes go to
  `Flow-Steward/flow-steward-app`.

Each plugin entry reads `<folder>/.cursor-plugin/plugin.json` (name, version,
description, author, license, keywords, `logo`), which is generated from
`docs/agent-skills/packages.json`. Neither plugin carries an MCP server or
variables: users add their own Flow Steward server in Cursor's MCP settings, as
[COMPATIBILITY.md](../COMPATIBILITY.md) shows.

## Checklist from the Cursor reference

- Valid `.cursor-plugin/plugin.json` in each plugin folder: generated.
- Unique lowercase kebab-case names: `flow-steward-operator`,
  `flow-steward-developer`.
- README with usage and configuration: the package README.
- Logo committed and referenced by a relative path: the package icon.
- Relative manifest paths only, no `..`: generated.
- Tested locally: not yet, Cursor is not installed on the verification machine.

## Review notes

- Skills only; no rules, hooks, commands, MCP servers or variables.
- Operator works with a Flow Steward MCP server the user adds; Developer needs
  a terminal, Git and Docker.
- Data handling: [data-handling.md](data-handling.md).

## Steps for the publisher account owner

1. Create the public publication repository and fill it as described above
   from the release's marketplace archive.
2. Install both plugins from it in Cursor (or copy them to
   `~/.cursor/plugins/local/`), check the skill list, then remove them.
3. Submit the repository at cursor.com/marketplace/publish with the support and
   privacy links (`TODO(owner)`, see [README.md](README.md)).
4. Set the statuses in [COMPATIBILITY.md](../COMPATIBILITY.md).

Sources: [Cursor plugins](https://cursor.com/docs/plugins),
[plugin reference](https://cursor.com/docs/reference/plugins).
