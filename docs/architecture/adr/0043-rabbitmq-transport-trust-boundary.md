# ADR 0043: RabbitMQ transport trust boundary

- Status: Accepted
- Date: 2026-10-13

## Context

Job delivery and runtime heartbeat reports share RabbitMQ transport but have
separate client owners. Accepting any plaintext broker URL at either owner can
send credentials and application data over an untrusted network. Checking only
the URL scheme also misses client query options that alter the effective TLS
context. Canonical Compose deployments intentionally keep their owned broker on
the deployment network; replacing those deployments with a new TLS stack is not
necessary to enforce the boundary for remote brokers.

## Decision

Own transport validation in `core/infrastructure/config/rabbitmq_transport.py`
and reuse it before either client opens a connection. TLS is the default. Check
both the authored endpoint and the effective client parameters; a URL parser or
query option must not substitute a different endpoint or weaken authentication.
Malformed or disallowed configuration fails closed with a fixed diagnostic that
does not include the URL, userinfo, or parser exception details.

TLS requires certificate-chain verification and hostname checking. Trust uses
the client's default CA store or an explicitly supplied CA bundle. The verified
peer name is the canonical URL host: normalize an omitted client server name to
that host and refuse an explicit different name. Numeric IP endpoints therefore
need an IP certificate identity; using an unrelated DNS certificate name is not
a workaround. No local deployment mode disables these TLS checks.

Plaintext is permitted only by an explicit deployment-owner assertion matching
the exact endpoint owned by that context. The canonical schema and allowed mode
values remain in the environment registry; their endpoint constraints are
explained in the [configuration reference](../../user/configuration-reference.md#rabbitmq-transport).
There is no general insecure flag, private-address heuristic, or arbitrary DNS
allowlist. A remote endpoint override does not inherit permission to use
plaintext from the deployment's local broker setting.

This decision trusts the operator and isolation of the selected local context,
not the spelling of a hostname. Canonical production Compose keeps broker ports
off the host; development publishes them only on numeric loopback; the disposable
audit broker publishes no host ports and owns a separate temporary vhost and
storage. The operator must preserve those boundaries. A service on the trusted
network or a privileged host administrator may still observe local plaintext;
this mode provides neither encryption nor isolation from a compromised host or
network peer. Remote or untrusted-network brokers require verified TLS.

Installer and Compose owners declare their local context explicitly. Platform
and other unmanaged deployments retain the secure default. Doctor reuses the
same configuration validation before its socket reachability check; a successful
TCP probe alone is not proof of a TLS handshake or broker authentication.
Dependency probes that use the broker URL must retain its validated TLS context
and peer identity. They do not own a second transport policy.

## Consequences and rollout

An existing explicit plaintext URL outside an approved local context will stop
connecting after upgrade. The runtime does not silently rewrite it, fall back to
plaintext, disable certificate verification, or infer trust from a private IP.
Operators must provision the remote broker's TLS listener and trust material, or
retain the exact canonical local deployment context, before replacing runtime
images. Queue and heartbeat clients must receive the same effective configuration.
The [deployment rollout procedure](../../operations/compact-scalable-deployment.md#rabbitmq-transport-policy-rollout)
records preparation and acceptance checks without changing stored jobs, broker
vhosts, or credentials automatically.

This is a bounded decision for the owned local transports, not a blanket waiver
for plaintext scanner findings. Each other plaintext path still needs its own
source-to-sink and deployment review. This record does not certify release gates
or a deployed candidate.

## Alternatives considered

- Trust all private addresses or a familiar broker hostname: neither establishes
  ownership of the endpoint or network.
- Offer an unrestricted insecure opt-out: transfers the remote security boundary
  into ambient configuration without a bounded deployment contract.
- Require a new TLS broker stack for every local Compose deployment: adds key and
  certificate lifecycle machinery where an explicit owned-network boundary
  already covers the intended deployment.
- Check only the URL scheme: allows effective client options to bypass certificate
  or hostname verification.

## Related decisions

- [ADR 0029: Compact and Scalable deployment](0029-compact-and-scalable-deployment.md)
- [ADR 0031: Runtime heartbeat monitoring](0031-runtime-heartbeat-monitoring.md)
- [Configuration standard](../../standards/configuration-standard.md)
- [Security standard](../../standards/security-standard.md)
