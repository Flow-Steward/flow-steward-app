"""Public reverse-proxy fragment command specification.

AC17-R113 put this in the `config` family, where it reads naturally and where no
Compact user can reach it: the launcher dispatches `config` and `stack` to a retained
fs-cli container the default profile does not have, and says so in its own help. The
command was written to give the renderer a path to a user, so being one step further
down a path that ends in a wall does not close it. Top level, alongside `status` and
`doctor`, is where the launcher already runs commands inside the installation itself.
"""

from __future__ import annotations

from core.application.lifecycle.configuration_service import (
    ConfigProxyFragmentRequest,
    ConfigProxyFragmentResult,
)
from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    InteractivePolicy,
    OperationType,
)

PROXY_FRAGMENT_SPEC = CommandSpec(
    command_id="proxy-fragment",
    compact_support=CompactSupport.PLATFORM,
    path=("proxy-fragment",),
    summary="Print the reverse-proxy fragment for this installation.",
    description=(
        "Render the Nginx or Apache fragment for an installation that fronts itself "
        "with an existing reverse proxy, using the proxy kind, public host and backend "
        "ports already recorded, and report the backends it assumed."
    ),
    examples=("flow-steward proxy-fragment", "flow-steward proxy-fragment --json"),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(),
    input_model=ConfigProxyFragmentRequest,
    result_model=ConfigProxyFragmentResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    # Domain-qualified so the registry finds the binding module, exactly as
    # readiness.status does for the top-level `status`; the renderer key is the bare
    # command id, also as there.
    service_ref="config.proxy-fragment",
    result_renderer_ref="proxy-fragment",
)

PROXY_COMMAND_SPECS: tuple[CommandSpec, ...] = (PROXY_FRAGMENT_SPEC,)
