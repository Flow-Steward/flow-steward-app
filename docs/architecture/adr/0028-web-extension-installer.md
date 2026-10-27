# ADR 0028: Browser access to the isolated extension installer

Status: Accepted — restoring browser installation was approved on 2026-09-15.

Amended 2026-10-01: there is no host install policy or publisher tier. An
extension's rights over its own data come from manifest `runtime_grants`,
limited by `own_runtime_grants` to its own namespace; see the
[manifest standard](../../standards/extension-manifest-standard.yaml).

## Context

Application and worker mounts intentionally expose extensions read-only. This
disabled both the browser file picker and drag-and-drop despite an existing
archive installation service. CLI installation uses a separate writable process.

## Decision

Run `fs-extension-installer` as a managed runtime service using the same immutable
app image and the existing lifecycle services and authorization stack. Only its
extension directory is writable relative to application source. Grants are
manifest-owned; there is no install-policy file. It receives no Docker socket or
published host port.

Its ASGI entrypoint exposes only archive lifecycle capabilities, upload
installation, catalog installation, scan for committed uninstall file
reconciliation, and revisioned uninstall/preview. It does not expose arbitrary
actions, login or the rest of the application API. Bootstrap loops are disabled.

The main API, inside its normal auth middleware, forwards these routes to the
fixed private service address when `FS_EXTENSION_INSTALLER_PROXY_ENABLED=1`.
The installer independently authenticates the original caller. This works with
the default gateway, a custom reverse proxy and the frontend development proxy.
Request/response bodies are bounded. Redirects and automatic retries are disabled;
an unavailable response requires checking installed state before retrying.

For successful POST writes, the proxy attempts to refresh the serving
application's browser permission and registry projections before returning the
upstream response. If refresh raises, it logs the failure and still returns that
response; the local refresh failure does not roll back the installer write.
Each serving process also compares active install/release signatures before
serving authenticated app/API requests selected by the projection-sync boundary,
excluding authentication endpoints, `/api/health`, and `/api/app/bootstrap`.
A changed signature triggers refresh and is recorded only after refresh succeeds,
so a later matching request retries a failed refresh. Multiple web workers can
observe installations and removals without restarting. Existing browser-authority
approval, archive validation, atomic release and uninstall revision checks are
retained. The CLI command and its one-shot manager remain supported.

## Consequences

The installer is included in initial installation and stack lifecycle service
lists. Docker users no longer need writable mounts on the public application.
Standalone writable API deployments retain local installation by default.
The new process reuses the normal API composition but restricts its HTTP surface;
no parallel archive installer or new authentication protocol is introduced.
