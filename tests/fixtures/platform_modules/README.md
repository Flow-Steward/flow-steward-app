# Reference platform modules (ADR 0082 prototype)

Test fixtures for the platform module contract. Each directory is a module source
that `tests/support/platform_module_builder.py` stages into a prepared module tree
(stdlib only, Python 3.11+):

- `reference_notes/1.0.0`: a module with a registered settings page, API routes
  and a module-owned table created by a module SQL migration.
- `reference_notes/1.1.0`: an overlay of 1.0.0 that adds a migration, for the
  module upgrade path.
- `reference_entitlements`: a module that registers an entitlement source and
  bundles a third-party dependency under `site/`.

`tests/unit/test_reference_platform_modules.py` builds and loads them; the
builder is also used by the loader, activator and module migration tests.
