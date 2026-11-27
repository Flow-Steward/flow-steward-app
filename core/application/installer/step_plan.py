"""Ordered install and update steps of the installer state machine.

Each install step names its id, the label shown to the operator and the
:class:`core.application.installer.service.InstallerService` method that ensures
its postcondition.
"""

from __future__ import annotations

_STEP_METHODS: tuple[tuple[str, str, str], ...] = (
    ("preflight", "Docker and host preflight", "preflight"),
    ("configuration", "Save installation configuration", "ensure_configuration"),
    ("secrets", "Generate durable internal secrets", "ensure_secrets"),
    ("tls", "Prepare TLS certificate", "ensure_tls"),
    ("prepare", "Prepare core images", "prepare_core"),
    ("start", "Start core services", "start_core"),
    ("schema", "Verify schema readiness", "verify_schema"),
    ("worker_identity", "Provision worker machine identity", "ensure_worker_identity"),
    ("runtime", "Start application runtime", "start_runtime"),
    ("health", "Verify core health", "verify_health"),
    ("cli_runtime", "Start reusable CLI runtime", "ensure_cli_runtime"),
    ("storage", "Verify selected file storage", "verify_storage"),
    ("public_route", "Verify public route", "verify_public_route"),
    ("bootstrap", "Verify first-admin bootstrap", "verify_bootstrap"),
    ("manifest", "Capture initial preservation manifest", "capture_initial_manifest"),
)

# AC17-R141: what an update of a completed installation runs, in the install loop's
# order. Not the loop itself, for three reasons each of which the loop would get wrong
# on a system that is already in use:
#   - configuration, secrets and TLS already hold and are the installation's own;
#   - the initial preservation manifest describes the first install, not this one;
#   - verify_bootstrap in the loop requires a pending first-administrator bootstrap and
#     raises when there is none, which on an installation in use is always.
# The storage, public-route and bootstrap checks after the runtime starts are run the
# way the "already installed" path runs them, below.
_UPDATE_STEPS: tuple[str, ...] = (
    "preflight",
    "prepare",
    # AC17-R160: before `start` recreates the core services on the release's images --
    # Postgres among them, which answered 502 for seconds -- ask whether this release
    # can run on the database at all. An update carrying a migration stops here now,
    # with nothing restarted.
    "migrations",
    "start",
    "schema",
    "worker_identity",
    "runtime",
    "health",
    "cli_runtime",
)
_STEP_LABELS = {step_id: (label, method) for step_id, label, method in _STEP_METHODS}
_STEP_LABELS["migrations"] = (
    "Check the release's migrations against the database",
    "precheck_migrations",
)
# The one step an update runs differently: the fresh-install preflight treats every
# bound port as someone else's, and a running installation always holds its own.
_UPDATE_METHODS = {"preflight": "update_preflight"}
