# ADR 0064: RabbitMQ on a provider private network

- Status: Accepted
- Date: 2026-11-10
- Amends: [ADR 0043](0043-rabbitmq-transport-trust-boundary.md)

## Context

ADR 0043 allows plaintext AMQP only to endpoints owned by a Flow Steward
deployment context: `rabbitmq-fs` on the Compose network, `rabbitmq-audit` for
the disposable audit stack, and numeric loopback for host development. Every
other broker needs verified TLS.

Compact on Railway, Fly.io and Render runs the broker as a separate provider
service and reaches it over the provider's private network, for example
`rabbitmq.railway.internal`. After ADR 0043 the documented platform
installations could no longer start: the runtime refused the plaintext broker
URL and the supervisor restarted in a loop. Making these installations use TLS
would mean a private certificate authority per installation, a custom broker
start command to write certificates from variables (Railway has no file
mounts), a CA bundle delivered into Compact, and certificate rotation before
expiry. Each provider does this differently, and an expired certificate stops
the installation.

The owner chose, on 2026-11-10, an explicit mode for one declared provider
private-network host instead of TLS for platform installations.

## Decision

Add the transport mode `platform-private`. With it, plaintext `amqp` is allowed
only when the URL host equals `FS_RABBITMQ_PRIVATE_HOST` exactly and the URL
names port `5672` explicitly. Every other endpoint, including TLS endpoints,
follows ADR 0043 unchanged: verified TLS with certificate-chain and hostname
checks is still accepted in this mode, and no mode disables them.

`FS_RABBITMQ_PRIVATE_HOST` must be a DNS host name made of DNS labels. IP
literals, empty values, user information, ports and paths are refused, so the
declaration cannot widen into a network range. The variable is ignored in every
other mode; a local mode never inherits it, and this mode never inherits a local
mode's endpoint. A mode without a valid declaration refuses every plaintext URL
with the same fixed diagnostic, which never contains the URL or credentials.

The trust is an operator assertion about the provider network, not about the
host name's spelling. The operator selects this mode only where the provider
routes the named service on a private network that is not reachable from the
internet. Platform documentation states each provider's property:

- Railway encrypts private networking with WireGuard and isolates each project
  environment.
- Fly.io carries its private network over WireGuard; every application in the
  same organization can reach it.
- Render's private network spans a workspace and region; Render does not
  document encryption of that traffic.

Compact passes both variables to its supervised processes. Deployment doctor and
the dependency probes reuse the same validation. The installer and canonical
Compose files never select this mode.

## Consequences

Documented platform installations can use the stock RabbitMQ image without
certificate management. Broker credentials and job payloads cross the
provider's private network in plaintext AMQP; on Render that traffic may be
unencrypted inside the provider. Anyone who can reach the private network, such
as another service in the same Railway environment or another application in
the same Fly organization, can observe or reach the broker as on a Compose
network. Operators who need protection from those peers, or a provider whose
private network does not fit the assertion, use verified TLS or a managed TLS
broker.

Changing the broker URL to another host while keeping this mode fails closed;
the declaration has to change with it.

## Alternatives considered

- TLS with a per-installation private CA: keeps ADR 0043 unchanged and works
  with already-published images, but adds certificate generation, delivery and
  rotation that differ per provider, and fails on certificate expiry.
- A managed TLS broker: no code change, but an external paid dependency, and job
  data leaves the provider. Documented as an option, not as the default.
- Trust a provider host-name suffix such as `.railway.internal`: rejected for
  the reason in ADR 0043; a name's spelling is not evidence of the network, and
  Render's private names have no suffix.

## Related decisions

- [ADR 0043: RabbitMQ transport trust boundary](0043-rabbitmq-transport-trust-boundary.md)
- [ADR 0029: Compact and Scalable deployment](0029-compact-and-scalable-deployment.md)
