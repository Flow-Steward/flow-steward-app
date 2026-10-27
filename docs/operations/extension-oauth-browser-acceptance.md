# Extension OAuth Browser Acceptance

This lane exercises the real React application, FastAPI services, PostgreSQL
repositories, callback registry, PKCE/state handling, token exchange, and the
extension subprocess against a deterministic provider. The Playwright spec does
not intercept product API requests.

## One-command run

Run from the repository root. Use a unique project name when another agent may
be testing concurrently:

```bash
FS_COMPOSE_PROJECT_NAME=fs-oauth-generic-20260830 \
  ./core/entrypoints/cli/scripts/run-extension-oauth-browser-acceptance.sh
```

The command builds exact app, frontend, and audit candidates from the current
worktree with `--pull=false`, pinned base-image digests, and the repository's
locked dependency inputs, then starts them with `--no-build --pull never`.
Docker build-time networking remains available so uncached apt, pip, npm, and
Playwright prerequisites can be retrieved; the build context remains the local
candidate worktree and the candidate fingerprint covers its exact contents.
Runtime is separate: the isolated Docker network is internal, publishes no host
ports, and has disposable PostgreSQL, RabbitMQ, certificate, extension,
frontend, runtime, project, log, and seed volumes. The script always runs
`docker compose down --volumes --remove-orphans`, removes only its exact
candidate image tags, and verifies that no container, network, or volume with
its exact Compose project label remains. It also compares pre/post Git status so
the lane cannot silently write into the worktree.

The environment generates a one-day test CA and a server certificate for
`traefik`, `oauth-provider`, and `localhost`. The CA is trusted by the app,
seeder, Node runtime, and Chromium. Production HTTPS and SSRF checks remain
active; only `FS_ALLOW_PRIVATE_REMOTE_URLS=1` permits the isolated provider's
private Docker address.

## Deterministic seed

The seed creates these local users in the disposable database:

| Role | Email | Password |
| --- | --- | --- |
| Admin | `oauth-admin@example.test` | `OAuthAcceptanceAdmin_123!` |
| Operator | `oauth-operator@example.test` | `OAuthAcceptanceOperator_123!` |
| Viewer | `oauth-viewer@example.test` | `OAuthAcceptanceViewer_123!` |

It also creates `OAuth Acceptance Alpha` and `OAuth Acceptance Beta`, installs
the neutral OAuth fixture extension, creates two configurations in Alpha and one isolated
configuration in Beta, and writes opaque IDs to the disposable
`oauth-browser-state` volume. The browser reads that state rather than relying
on fixed database identifiers.

The provider maps the declared `email_hint` local part to deterministic cases:

- `success@example.test` — authorization and token exchange succeed;
- `deny@example.test` — authorization is cancelled;
- `invalid_client@example.test` — the selected configuration needs attention;
- `invalid_grant@example.test` — only the selected reconnect requires authorization;
- `authorization_required@example.test` — reconnect authorization is required;
- `post_connect_failure@example.test` — the first post-connect action fails, then a real expired-token refresh lets Retry succeed;
- `unavailable@example.test` — the provider returns a normalized unavailable response.

The spec is `assets/frontend/tests/e2e/extension-oauth.spec.ts`. Extensions
installed from the catalog keep their own browser specs in their own
repositories. The spec verifies
real operator/viewer sessions; exact configuration selection and cosmetic rename
without secret replacement; text, secret, email, and plain-text intent inputs;
visible and semantic required markers; min/max and HTTPS-origin validation;
focus, red invalid styling, and ARIA error association; server-side bypass
rejection; project isolation and viewer read-only controls; PKCE token exchange;
success, cancellation plus explicit retry, provider unavailability, runtime
revalidation, authorization-required reconnect plus successful reconnect,
post-connect needs-attention plus Retry, selected-configuration invalid-client
handling, revocation protocol behavior, and secret-safe final URLs/messages.

## Manual inspection

The normal automated lane exposes no host ports. The tracked manual lane is an
explicit opt-in and requires a clean committed worktree. Choose a unique project
and unused loopback port:

```bash
FS_COMPOSE_PROJECT_NAME=fs-oauth-generic-manual-01 \
FS_OAUTH_MANUAL_PORT=18443 \
FS_OAUTH_MANUAL_PROVIDER_PORT=18444 \
  ./core/entrypoints/cli/scripts/run-extension-oauth-browser-manual.sh up
```

The command waits for the authenticated TLS seed to finish and prints the exact
URL and accounts. Export and trust only that project's one-day CA on macOS:

```bash
FS_COMPOSE_PROJECT_NAME=fs-oauth-generic-manual-01 \
FS_OAUTH_MANUAL_PORT=18443 \
FS_OAUTH_MANUAL_PROVIDER_PORT=18444 \
  ./core/entrypoints/cli/scripts/run-extension-oauth-browser-manual.sh ca
security add-trusted-cert -d -r trustRoot \
  -k "$HOME/Library/Keychains/login.keychain-db" \
  /tmp/fs-oauth-generic-manual-01-ca.crt
```

Open
`https://flow-steward.localhost:18443/app/extensions/fixture.extension-oauth`
in Chrome. The app and browser use `https://oauth.127.0.0.1.nip.io:18444` for the
same fake-provider endpoints, so redirects, callback validation, token exchange,
and refresh all use host- and container-resolvable TLS names.
Do not bypass the TLS warning. When the audit is complete, remove the exact
Compose project, its volumes, and its candidate images:

```bash
FS_COMPOSE_PROJECT_NAME=fs-oauth-generic-manual-01 \
FS_OAUTH_MANUAL_PORT=18443 \
FS_OAUTH_MANUAL_PROVIDER_PORT=18444 \
  ./core/entrypoints/cli/scripts/run-extension-oauth-browser-manual.sh down
CA_SHA256="$(openssl x509 \
  -in /tmp/fs-oauth-generic-manual-01-ca.crt \
  -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':')"
security delete-certificate -Z "$CA_SHA256" \
  "$HOME/Library/Keychains/login.keychain-db"
```

## Reset and cleanup

The one-command runner starts from empty project resources. If it is interrupted
outside its cleanup trap, remove only its exact project:

```bash
docker compose --project-name fs-oauth-generic-20260830-oauth-browser \
  --file infra/docker/docker-compose.yaml \
  --file infra/docker/docker-compose.oauth-browser.yaml \
  down --volumes --remove-orphans
```

Verify the cleanup without pruning shared Docker resources:

```bash
docker ps -aq --filter label=com.docker.compose.project=fs-oauth-generic-20260830-oauth-browser
docker network ls -q --filter label=com.docker.compose.project=fs-oauth-generic-20260830-oauth-browser
docker volume ls -q --filter label=com.docker.compose.project=fs-oauth-generic-20260830-oauth-browser
```

All three commands must print nothing.
