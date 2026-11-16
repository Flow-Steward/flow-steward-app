# Released API reference assets

The `Publish API reference` workflow runs after successful official image
publication. It exports `openapi-public.json`, `openapi-application.json`, and
`openapi-manifest.json` into the existing stable GitHub Release, then dispatches
`api-reference-release` (release and revision) to
`Flow-Steward/documentation-site` with the existing DOCS_SYNC GitHub App.

To bootstrap an already published stable release without rebuilding its images:

```sh
gh workflow run publish-api-reference.yml \
  --repo Flow-Steward/flow-steward-app --ref main -f tag=v0.9.18
```

The workflow resolves annotated tags to their exact commit and validates the
release's retained `registry-verification-<run>-<attempt>.json` evidence with the
canonical release validator. Export uses the verified `fs-app` Linux AMD64 child
manifest digest, checks the recorded OCI config digest and image revision label,
and checks the application's embedded version and revision. Conflicting verified
receipts, an incomplete registry set, or a moved release identity fail closed.
No running demo or product installation is consulted.

Export tooling is mounted read-only alongside the released image. Only the small
artifact-contract module is loaded from that mount; all application, route,
policy, model and schema-builder imports come from the released `/app` tree.
The one-shot process has no network and never serves HTTP or enters the app
lifespan. Its construction fixture temporarily skips only the eager
`validate_runtime_service_ports` call, which otherwise instantiates live database
stores before registering routes. The original validator is restored immediately
in the context manager. All real route/dependency registration and both canonical
schema builders execute. This fixture is solely for offline documentation
construction and changes no runtime validation or released source files.

Public API policy, allowlist, descriptions, security and schema aliases are
preserved. The Application API uses the canonical internal builder and its
normal route exclusions and response normalization. API contract versions remain
in schema `info.version`; product release and revision live in the manifest.
Generation verifies local component references, response objects, operation/path
counts and exact SHA-256 checksums. Instance server overrides are rejected.
A runner-only pinned SwaggerParser 13.1.0 installation performs full OpenAPI
validation before release upload, with external/file/HTTP resolution disabled.

v0.9.18 places approval request prose on a Media Type Object, which OpenAPI
does not allow. Export alone moves that prose to its Request Body description,
preserving existing descriptions and all schemas, examples and request/response
contracts. Neither released application code nor API behavior changes.

The tooling produces artifacts outside the source tree. No generated schemas or
Swagger frontend are added to product distribution, and all live docs flags stay
disabled. Artifact scratch storage is isolated in `/tmp` inside the container.

Asset upload preflights every existing asset's exact bytes before writing any
missing ones. Identical assets are reused; differing assets are never overwritten
or deleted. The manifest is uploaded last. Rerunning after a partial upload or a
failed docs dispatch is safe. To change already published snapshots, investigate
the mismatch rather than replacing immutable assets.

This workflow does not request a quality gate or rebuild images. Its permissions
are release-asset write and package read; docs dispatch uses a separate token
scoped to the documentation repository.
