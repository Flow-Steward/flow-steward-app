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
for Docker socket access. A host Python installation is not required.

Check prerequisites as the deployment user:

```bash
docker --version
docker compose version
docker info
```

## Install and open

Use a full (not shallow) Git clone, then run:

```bash
git clone https://github.com/Flow-Steward/flow-steward-app.git flow-steward
cd flow-steward
./install.sh
./flow-steward status
./flow-steward doctor
```

What a fresh installation installs depends on the checkout:

- **A published release.** Check out its tag (`git checkout v<version>`, chosen from the
  repository's Releases page) before `./install.sh`. A clean checkout exactly at its
  `v<VERSION>` tag installs that stable release's official, digest-pinned images from
  GitHub Container Registry (or Docker Hub when GHCR lacks the complete set). Nothing
  is built locally, and the result is an official installation. If that tag is not
  yet published as a stable release with its images, or GitHub or the registry is
  unreachable, the installer stops with `release_images_unavailable` instead of
  building: wait for publication, or check out a non-release commit.
- **Anything else** (`main`, another commit, or a tag with local changes). The installer
  builds the current local checkout in Docker and **does not require** a GitHub Release
  lookup. Docker still downloads required base images and dependencies. This creates
  a source installation; changing to an official release is a separate, reviewed
  [release transition](docs/GUIDE.md#maintenance-and-release-upgrade).

The installer prints which of the two it chose before preparing images.
The canonical database baseline lives in `db/schema/`, with canonical seed data in
`db/seeds/`; post-1.0 SQL migrations live separately in `db/migrations/`.
The plain questionnaire is the default; `--tui` opts into the full-screen interface.
Choose your profile, public domain/IP, storage, ingress, and TLS during installation.

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

[Project documentation index](docs/README.md) lists the maintained guides,
contracts, engineering standards and architectural decisions.

[docs.flowsteward.org](https://docs.flowsteward.org) is the official documentation site
(source: [Flow-Steward/documentation-site](https://github.com/Flow-Steward/documentation-site)).
The repository guides:

- [User guide and CLI configuration](docs/GUIDE.md)
- [Flow Steward agent skills package](docs/agent-skills/flow-steward/README.md)
  ([setup in the user guide](docs/GUIDE.md#developer-toolkit-local-agent-skills))
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
