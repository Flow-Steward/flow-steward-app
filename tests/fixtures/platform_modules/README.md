# Reference platform modules (ADR 0082 prototype)

Test fixtures for the platform module contract. Each directory is a module source
that `tests/support/platform_module_builder.py` stages into a prepared module tree
(stdlib only, Python 3.11+):

- `reference_notes/1.0.0`: a module with a registered settings page, API routes
  and a module-owned table created by a module SQL migration.
- `reference_notes/1.1.0`: an overlay of 1.0.0 that adds a migration, for the
  module upgrade path.
- `reference_entitlements`: the designated entitlement source (it declares
  `"provides": ["entitlement_source"]`); it registers its source in the app role
  (`register`) and the control role (`register_control`) and bundles a
  third-party dependency under `site/`. The broken module below declares nothing,
  so Core refuses the source it tries to register.

`tests/unit/test_reference_platform_modules.py` builds and loads them; the
builder is also used by the loader, activator and module migration tests.

## Reference approval seams module

`reference_approval_seams` (`acme.reference-approval-seams` 1.0.0) is a test-only stub
for `tests/unit/test_approval_module_seams.py`. It proves that Core's approval ports
reach a module: one read-only route over `container.approval_assignments` behind the
`approval_assignees` entitlement and a module route rule, and one
`container.feature_endpoints` registration. It writes nothing and registers no page.

## Reference broken module

`reference_broken` (`acme.reference-broken` 1.0.0) is a test-only module whose
`register` mounts a route, contributes a route rule and an entitlement source and
then raises; its `register_control` registers the source and raises. The tests in
`tests/unit/test_reference_platform_modules.py` prove that Core skips it, rolls
back everything it registered and keeps the other modules running.

## Reference scoped seams module

`reference_scoped_seams` (`acme.reference-scoped-seams` 1.0.0) is a test-only stub for
`tests/unit/test_scoped_roles_module_seams.py`. It proves that Core's scoped roles port
reaches a module: one read-only route over `container.scoped_roles` behind the
`scoped_roles` entitlement and a module route rule, and one
`container.feature_endpoints` registration. It writes nothing and registers no page.
