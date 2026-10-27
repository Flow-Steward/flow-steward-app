---
name: flow-steward-report-issue
description: Use when a Flow Steward user wants to report a bug, a crash, a wrong result or a confusing behavior, or to request a feature, on GitHub Flow-Steward/flow-steward-app; collects the System Diagnostics report and the facts maintainers need.
---

# Report a Flow Steward Bug or Request a Feature

Issues and feature requests go to
<https://github.com/Flow-Steward/flow-steward-app/issues>. Help the user write
one report that a maintainer can act on without a follow-up question. Work with
the user: you collect and draft, the user confirms what is sent.

**Security vulnerabilities are never public issues.** If the problem lets
someone read, change or run something they should not, stop and tell the user
to email `security@flow-steward.org` with the subject prefix `[SECURITY]`, as
`SECURITY.md` in the repository says. Do not file it on GitHub.

## 1. Search first

Search existing issues, open and closed, with two or three key words from the
error message or the feature:

```bash
gh issue list --repo Flow-Steward/flow-steward-app --state all --search "<words>"
```

Without `gh`, open
`https://github.com/Flow-Steward/flow-steward-app/issues?q=<words>`. If an
issue already describes the problem, offer to add a comment with the new facts
(version, diagnostics, steps) instead of opening a duplicate.

## 2. Get the System Diagnostics report

Every bug report needs the diagnostics JSON. It says how the installation is
built and how it runs: the Flow Steward version and image identity, Compact or
Scalable, CPU architecture and resources, database and broker versions, each
service and its health, the installed extensions with their version and state,
and which settings are customised. It has **no logs, no job content and no
setting values** beyond a short list of deployment-shape settings (such as the
profile, storage backend and log level); every configured secret value is
removed wherever it appears.

Ask the user to get it one of two ways:

- **In the UI** (administrator only): **System → Monitoring**, then
  **Download diagnostics**. The file is
  `flow-steward-diagnostics-<UTC stamp>.json`.
- **From the command line**, in the installation folder on the server:

  ```bash
  ./flow-steward reports diagnostics
  ```

  It also collects host facts and the `doctor` result, writes
  `.flow-steward/diagnostics/flow-steward-diagnostics-<UTC stamp>.json` and
  prints the path.

An agent with an administrator token can also fetch it with
`GET /api/system/diagnostics/report`. The command-line report is the most
complete, because only the host sees Docker and the containers; prefer it when
the user has access to the server. A non-administrator cannot download the
report; ask the user to get it from their administrator. If the stack is down so badly that neither works, say so in the
issue and include the output of `./flow-steward status` and
`./flow-steward doctor` instead.

Even though the report is redacted, **tell the user to open the file and glance
through it before attaching it**: host names, extension ids and customised
setting names may still be things they prefer not to publish. Never edit the
report to hide a problem; leave out a whole section only if the user insists,
and say in the issue that it was trimmed.

## 3. Collect the facts

- **Version**: from the diagnostics file, or `./flow-steward release info`.
- **What happened**: the exact error text, copied, not paraphrased.
- **Steps to reproduce**: numbered, starting from a known state (which page,
  which workflow or extension, what input). Ask the user to repeat them once
  to confirm the problem still happens.
- **Expected and actual result**: one sentence each.
- **Job**: the job id, the workflow id and the failing step name when a run
  failed. Do not paste the job's data.
- **Screenshots**: of the error or the wrong screen, after cropping out
  customer data, tokens and email addresses.
- **Since when**: did it work before (which version), and what changed (an
  update, a new extension, a setting).

A job page also has **Download full report**. Secrets are redacted in it, but
it contains the job's messages, logs, events and step data, which is often
customer data. Do not attach it to a public issue. Mention that it exists and
offer it privately if a maintainer asks.

## 4. Never publish

Passwords, API keys, tokens, cookies, `.env` contents, connection strings,
private keys, setup links, customer names, emails, orders or file contents.
Check everything you paste — log excerpts and screenshots included. Replace a
value with `<redacted>` when the shape matters.

## 5. Draft the issue

Title: what is wrong, where, in plain words, for example
"Workflow import fails when a child workflow uses a Wait step".

```markdown
## What happened
<exact error text and where it appears>

## Steps to reproduce
1. ...

## Expected
...

## Actual
...

## Environment
- Flow Steward version: <x.y.z>
- Profile: Compact | Scalable
- Job id / workflow id: <if any>
- System Diagnostics report: attached (`flow-steward-diagnostics-<stamp>.json`)

## Notes
<since when, what changed, workaround if any>
```

For a feature request, replace the middle sections with: the problem the user
has today, what they would like to do instead, and how they work around it
now. Diagnostics are optional for a feature request.

Show the draft to the user and get an explicit yes before sending it.

## 6. File it

With an authenticated `gh` (`gh auth status`):

```bash
gh issue create --repo Flow-Steward/flow-steward-app --title "<title>" --body-file issue.md
```

`gh issue create` cannot attach files. After creating the issue, give the user
its URL and ask them to drag the diagnostics JSON (and screenshots) into a
comment in the browser. GitHub accepts `.json` attachments; if their browser
refuses, they can zip the file first.

Without `gh`, open
`https://github.com/Flow-Steward/flow-steward-app/issues/new/choose` and pick
**Bug report** (or **Feature request**). The bug form has a required System
Diagnostics field: the user drags the JSON there. Copy each section of the
draft into its field, attach screenshots, and let the user do the final click.

Report the issue URL back. Do not open more than one issue for one problem.
