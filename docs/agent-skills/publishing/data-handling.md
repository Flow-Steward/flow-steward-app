# Data handling statement

Applies to `flow-steward-operator` and `flow-steward-developer` 1.0.0. Use this
text for the catalogs' data handling questions and keep it in step with the
packages.

## What the packages are

Each package is Markdown instructions (skills), manifests and an icon. Neither
package contains executables, scripts, hooks, MCP server code, telemetry, an
address, a token or any other credential. Neither package stores data.

## Operator

- **Reads**: the agent reads what your own self-hosted Flow Steward
  installation returns through its MCP server: projects, workflows, agents,
  skills, runs, run results, errors, approvals and reports. These can contain
  personal data that your automations process. Your access token limits what
  is returned.
- **Where it goes**: from your Flow Steward installation to the AI client you
  use, as part of your conversation. The package adds no other destination.
  The client's own data handling applies to the conversation.
- **Connection settings**: you enter your installation's MCP address and
  personal token in the client. In the Claude plugin the token is a masked
  option kept in the system credential store and deleted on uninstall; in other
  clients it stays in the client configuration you write.
- **GitHub**: only on your request and after you approve the text, the agent
  opens an issue in `Flow-Steward/flow-steward-app` or proposes a workflow
  template to `Flow-Steward/workflows`, through your own GitHub tools or `gh`.
  An issue can carry the System Diagnostics report of your installation, which
  Flow Steward redacts and which you review before attaching.
- **Changes**: creating, changing, enabling and running workflows, agents and
  skills happen in your installation, only when you ask and only within your
  token's permissions.

## Developer

- **Reads**: files in your local Flow Steward checkout and extension folders,
  and the output of the Flow Steward CLI, Git and Docker on your computer.
- **Where it goes**: to the AI client you use, as part of your conversation.
- **GitHub**: only on your request, the agent pushes branches, opens pull
  requests in `Flow-Steward/flow-steward-app`, creates extension releases and
  proposes catalog entries to `Flow-Steward/extensions`, through your own
  GitHub tools or `gh`.
- **Credentials**: none in the package; it needs no Flow Steward token.

## Answers for catalog forms

| Question | Answer |
| --- | --- |
| Does the plugin read or store personal data? | It stores none. Operator lets the agent read data from the user's own Flow Steward installation, which may include personal data processed by the user's automations. Developer reads local source files. |
| Does it send data to services other than its declared connectors? | No service of its own. On the user's explicit request the agent uses the user's GitHub tools to post to the public Flow Steward repositories named above. |
| How long does it keep data? | It keeps nothing. The Claude plugin's stored token is deleted when the plugin is uninstalled. |
| Is it intended for people under 18? | No. It is intended for people who operate or develop a Flow Steward installation. |
| Security contact | `security@flow-steward.org`, as `SECURITY.md` in the repository says. |
| Privacy policy | `TODO(owner): privacy policy URL` |
| Support | `TODO(owner): support URL` |
