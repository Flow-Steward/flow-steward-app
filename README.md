# Flow Steward

A self-hosted AI workflow automation platform powered by LangGraph. Build
workflows from steps connected by one execution graph, with groups (one
sequence or parallel lanes) and For each loops where needed, connect external
systems through extensions, and run them through declared workflow triggers,
REST API, webhooks, MCP, or schedules. Agents use `workflow_trigger` to launch a concrete workflow,
then track its job and request human decisions through the approval tools.
See
[the workflow launch guide](docs/GUIDE.md#step-3--run-a-workflow) and
[ADR 0032](docs/architecture/adr/0032-workflow-launch.md).
Approval and Wait steps, datasets, artifacts, and internal project context keep
automation observable and under your control. Project context is assembled by
the runtime from canonical memory; it is not exposed as a public KB API or MCP tool.

Production supports **Compact** (single runtime) and **Scalable** (separate
services). Core provides generic integration contracts; extensions own their
provider-specific logic. Core does not provide repository code search,
code indexing, or a built-in code-writing engine.

Core ships no extensions. Install them from the in-product Extensions
Marketplace, which reads the
[Flow-Steward/extensions](https://github.com/Flow-Steward/extensions) catalog;
workflow templates come from
[Flow-Steward/workflows](https://github.com/Flow-Steward/workflows) and MCP
server entries from [Flow-Steward/mcps](https://github.com/Flow-Steward/mcps).

## Before installation

Supported hosts: Linux and macOS on amd64 (x86_64) or arm64. On Windows, use
Docker Desktop with Linux containers and run the shell launchers from WSL or
Git Bash.

You need a running Docker engine using Linux containers and **Docker Compose v2**.
Cloning this repository does not install Docker. Use the official instructions:

- Linux servers: [Docker Engine](https://docs.docker.com/engine/install/),
  [Ubuntu](https://docs.docker.com/engine/install/ubuntu/), and
  [Compose plugin](https://docs.docker.com/compose/install/linux/).
- Desktops: [Docker Desktop](https://docs.docker.com/desktop/).

Do not run Flow Steward as root; consult [Linux post-install setup](https://docs.docker.com/engine/install/linux-postinstall/)
for Docker socket access. A host Python installation is not required. Docker owns autostart; the installer
does not create a systemd service or request sudo.

Check prerequisites as the deployment user:

```bash
docker --version
docker compose version
docker info
```

## Install and open

The guided installer asks how to run the application and how to publish its
website. **Compact** keeps the app and background workers together and uses
fewer resources. **Scalable** runs separate services so workers can scale
independently. Both use included **SeaweedFS** by default; external S3 is optional.
Technical settings stay behind `./install.sh --advanced`.

If Nginx or Apache already serves the host, choose that option and the actual web
server. HTTPS settings are derived automatically. At completion, the installer
prints a complete new-site configuration and an alternative for an existing
HTTPS site before the first-administrator link. Applying host web-server changes
remains a separate operator step.

Use a full (not shallow) Git clone, then run:

```bash
git clone --branch v0.9.16 https://github.com/Flow-Steward/flow-steward-app.git flow-steward
cd flow-steward
./install.sh
./flow-steward status
./flow-steward doctor
```

Production installation downloads the published installer and complete official
release image set from GHCR, with Docker Hub as the alternate registry. Production
uses a clean checkout at the published release tag, keeping the installer, local
management code and product images on that exact release.
Download or provenance failures stop installation. There is no automatic source
fallback, and no image is built locally, including the installer and PostgreSQL.
A host Python installation is not required. Docker owns autostart; the installer
does not create a systemd service or request sudo.

Only `./install.sh --from-source` builds the current checkout; for development use
`./install.sh --from-source --environment development --deployment-profile scalable`. Repeat the flag to resume
or update a source installation. Switching it to official uses the reviewed
[release transition](docs/GUIDE.md#maintenance-and-release-upgrade).

The canonical database baseline lives in `db/schema/`, with canonical seed data in
`db/seeds/`; post-1.0 SQL migrations live separately in `db/migrations/`.
The installer asks one question at a time; pick a choice with the arrow keys and Enter.
Choose your profile, public domain/IP, storage, ingress, and TLS during installation.

Manage the installation with `./flow-steward`. Every command prints readable text
(add `--json` when a script reads the output). For Tab completion add one line to
your shell profile, for example `eval "$(/path/to/flow-steward completion zsh)"`
(or `bash`, `fish`); `./flow-steward shell` opens an interactive prompt with Tab
completion and history.

**Open the one-time setup link printed at completion to create the first
administrator.** Treat it as a secret and use it within 60 minutes. Core
installation does not require AI provider credentials; configure them afterward.

Full installation instructions: [User guide, Step 1](docs/GUIDE.md#step-1--initial-setup)
and [Compact and Scalable deployment](docs/operations/compact-scalable-deployment.md).

## Report a bug

Attach a System Diagnostics file to your report. Download it from
**Monitoring → Download diagnostics** in the web UI, or run:

```bash
./flow-steward reports diagnostics
```

The command prints the path of the JSON file it writes. The report describes
the installation (version, images, profile, architecture, service health,
installed extensions, customised settings) and holds no secrets, logs or job
content. Open the issue in
[Flow-Steward/flow-steward-app](https://github.com/Flow-Steward/flow-steward-app/issues).
Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Documentation

Contributors: [remote quality checks](docs/operations/remote-quality.md) describes
local Tier 1, hosted Tier 2/Tier 3 and the full SonarQube pilot.

[Project documentation index](docs/README.md) lists the maintained guides,
contracts, engineering standards and architectural decisions.

[docs.flowsteward.org](https://docs.flowsteward.org) is the official documentation site
(source: [Flow-Steward/documentation-site](https://github.com/Flow-Steward/documentation-site)).
The repository guides:

- [User guide and CLI configuration](docs/GUIDE.md)
- Agent skill packages: [Flow Steward Operator](docs/agent-skills/flow-steward-operator/README.md)
  and [Flow Steward Developer](docs/agent-skills/flow-steward-developer/README.md)
  ([setup in the user guide](docs/GUIDE.md#agent-skill-packages))
- [Compact and Scalable deployment](docs/operations/compact-scalable-deployment.md)
- [Official release upgrade](docs/GUIDE.md#maintenance-and-release-upgrade)
- [Installation Clone](docs/operations/installation-clone.md)
- [Scheduled maintenance: what runs by itself, what to schedule](docs/operations/scheduled-maintenance.md)
- [Detailed checkout and contributor reference](docs/operations/checkout-reference.md)
- [Engineering standards](docs/standards/README.md)
- [Contributing](CONTRIBUTING.md)

## Legal and community

[Apache-2.0 license](LICENSE) · [Notice](NOTICE) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) ·
[Code of conduct](CODE_OF_CONDUCT.md)
