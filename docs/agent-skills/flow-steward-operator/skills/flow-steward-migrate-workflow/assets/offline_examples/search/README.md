# Unpublished search drafts

Three callable child drafts, version 1.0.2, for SerpAPI Google search, Tavily
basic search and Exa auto search. Required inputs: `query` and a secret
`provider_credential` with no default. Output: raw provider JSON in `response`.
Import hints keep these as drafts; no automatic trigger is defined. These files
are authored candidates, not exports after a live save, and are not published.

The HTTP path was selected for the inspected installation, where no qualified
ready search operation was found. Refresh connector/extension discovery for each
new migration. A parent workflow and its Invoke mappings still need their own test.

Guards reject malformed provider envelopes. SerpAPI requires metadata status
`Success`; its documented `Fully empty` result may include a top-level `error`
and is accepted as empty success. Exa checks the results array and item title/URL
string types; URI syntax is not fully validated. Tavily checks the results array
of objects and error fields; conditional answer fields and conflicting documented
response-time types are not required. These are explicitly bounded envelope
checks, not full validation of every optional provider field.

Each adapter returns one synchronous response, without follow-up pagination.
Tavily requests max_results=5; Exa requests numResults=5. SerpAPI does not promise
an exact result count. Query length <=2000, timeout 45 seconds and decoded response
limit 1 MiB are local adapter policies. The production HTTP retry policy may retry
safe SerpAPI GET failures; POST uncertainty is not hidden by forced retries.

Official request/response evidence and synthetic fixtures are recorded in
[provider-contracts.json](provider-contracts.json), checked 2026-11-03. Baseline
hashes identify the older drafts inspected before these fixes. The fixture
harness report identifies the exact corrected YAML hashes it executed.

Live provider authentication, account permissions, quota and actual response
behavior remain unverified. No provider key is included. Configure secrets and
perform separately authorized live tests before treating these as live-qualified
catalog integrations. See [executor verification](../../../references/executor-verification.md).
