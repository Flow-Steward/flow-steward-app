# 0050: Account-scoped cloud provider routing settings

Status: Proposed

## Context

The LLM provider v1 contract permits only `upstream_base_url` as plaintext connection configuration. Vertex AI requests address both a Google Cloud project and a location, which are not credentials. The existing host already persists connection configuration per account and install and supplies it separately from encrypted provider secrets to the extension runtime. A URL or credential blob is not an appropriate carrier for these independent settings.

## Decision

Allow extensions to declare bounded plaintext string settings using the existing required and optional lists. Optional `connection_config_schema` metadata supports only `type: string`, `title`, `minLength`, `maxLength`, and `enum`. The host rejects undeclared or secret-like names and validates these generic constraints before persistence and discovery. Provider-specific syntax is owned by the extension. Credential material remains in encrypted, account-scoped provider secrets. Changing these settings invalidates existing verification through the existing effective configuration comparison. Require extension host contract 1.4.0 for nonlegacy fields or schema metadata so older hosts do not advertise compatibility. Version 1.4.0 is unpublished, so this evolves that contract without changing the application version.

The provider must use the declared location and project to build the documented Vertex endpoint, must construct credentials only from the account-scoped secret snapshot, and must not use ambient application default credentials. The set of Google locations changes independently of Flow Steward; location is free text with help, not a stale enum.

## JSON object provider secrets

Within the unpublished 1.4 contract batch, providers may declare `provider_secret_formats` mapping declared secret names to `json_object`. The host rejects undeclared names and unsupported formats and requires `platform_min` 1.4.0 when the map is present. This is not a backport to older hosts: their descriptor validators reject the new field. Omitting the map leaves existing opaque password fields and portable descriptors unchanged.

For this format the UI uses a native password control while hidden and an explicitly revealed multiline editor when requested. New nonblank values must be JSON objects in both client and server; errors identify only the field, never parser details or secret contents. Blank replacement retains the existing encrypted secret. Store and pass the original string without normalization; providers retain responsibility for credential-specific semantic validation.

## Consequences

The host supports at most 32 safe canonical field names of at most 64 characters. Values are limited to 4096 Unicode code points; titles to 128, and enums to 32 unique nonempty values of at most 256 code points. No regular expressions, references, or general JSON Schema engine are accepted. Only the host-consumed `upstream_base_url` retains URL validation and slash normalization. This proposal does not grant credentials new privileges, guarantee that a project has enabled a model, or support workload identity federation. Model capability checks remain mandatory. Existing releases are unaffected.

## References

- [Vertex generateContent REST reference](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.publishers.models/generateContent)
- [Vertex regional and global endpoints](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations)
