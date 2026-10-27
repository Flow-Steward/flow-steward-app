"""Name the container an operator can actually inspect on the running profile.

AC17-R112: this lives in the infrastructure layer because that is where its one
dependency already points -- it reads FS_DEPLOYMENT_PROFILE from the process
environment. Under core/application/lifecycle/ the installer and the readiness
runtime, both infrastructure, imported application code, which the
infrastructure-to-application budget refuses to let grow; raising that budget for
a module that belongs on the other side of it would have been the wrong answer to
a check that was right.

It sits beside runtime_env.py rather than inside the runtime package for the
reason that module gives for its own placement: core/infrastructure/runtime/
initialises by importing the control-plane package, and the management CLI --
which needs a container name for its messages -- has no business carrying the
control plane to get one.

AC17-R106: remediation text was written when every runtime role had a container of
its own, and it kept naming those containers after Compact collapsed five of them
into one. An operator following `docker compose logs fs-worker` on a Compact
installation is told to inspect something that does not exist, at the moment their
installation is already broken -- which is when advice is worth the most and is
hardest to second-guess.
"""

from __future__ import annotations

COMPACT_RUNTIME_SERVICE = "compact-runtime"

# Declared by the Scalable compose and absent from the Compact overlay: on that
# profile these run as sibling processes inside COMPACT_RUNTIME_SERVICE. Deliberately
# not "every service that looks Scalable" -- rabbitmq-fs, pg-fs, traefik and fs-cli
# are containers on both profiles, and advice naming them is correct as it stands.
# tests/unit/test_compact_operator_advice.py derives this set from the compose files
# rather than trusting the list here, so the two cannot drift apart.
COLLAPSED_ON_COMPACT = frozenset(
    {
        "fs-app",
        "fs-worker",
        "fs-mcp",
        "control-plane",
        "fs-extension-installer",
        "fs-extension-manager",
    }
)


def operator_container(service: str, *, deployment_profile: str) -> str:
    """Return the container name to put in front of an operator on this profile."""
    if str(deployment_profile or "").strip().lower() != "compact":
        return service
    return COMPACT_RUNTIME_SERVICE if service in COLLAPSED_ON_COMPACT else service


def current_deployment_profile() -> str:
    """The profile this process is running under, for advice built inside the runtime."""
    from core.infrastructure.config.runtime_env import runtime_env

    return str(runtime_env.get("FS_DEPLOYMENT_PROFILE") or "scalable").strip().lower()
