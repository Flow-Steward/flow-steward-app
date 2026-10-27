"""Run one bundle's release checks, inside the container the platform starts.

This file is platform-authored but never runs in the platform's process. The
runner mounts it read-only next to a read-only copy of the bundle and starts a
stock Python image with a scrubbed environment: no Core source, no platform DSN,
no Docker socket, no `.env`. Whatever a bundle's `checks.py` does, it does from
there, with the candidate URL and a scoped token as its only reach.

It speaks stdlib only, because the image it runs in has nothing else.

Input arrives as environment variables the runner sets explicitly:

* ``FS_RELEASE_BASE_URL``  — where the candidate answers, on the same network
* ``FS_RELEASE_TOKEN``     — a scoped bearer token minted for this run
* ``FS_RELEASE_ACCOUNT``   — the account the token belongs to
* ``FS_RELEASE_INPUT_*``   — the extra addresses a bundle declared it needs
* ``FS_RELEASE_PROJECT_ID`` — a project the trusted side prepared for this run

Output is one JSON object on stdout: the assertion rows the checks produced, and
the resources they created. A suite cannot delete anything — every ``DELETE`` is
admin-only on the candidate, and this token is not — so it *declares* what it
made and the trusted side removes it. Anything a check raises is recorded as a
failed row rather than lost.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
import urllib.error
import urllib.request

BUNDLE_ROOT = "/bundle"
CHECKS_PATH = f"{BUNDLE_ROOT}/release/checks.py"
#: What a bundle's declared inputs are prefixed with, so the runner can pass them
#: without the harness knowing any bundle's names.
INPUT_PREFIX = "FS_RELEASE_INPUT_"


class Api:
    """The candidate, over HTTP, as the scoped token may see it."""

    def __init__(self, base: str, token: str, account: str) -> None:
        self.base = base.rstrip("/")
        self.account = account
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _send(self, method, path, body, content_type, timeout):
        request = urllib.request.Request(
            self.base + path, data=body, method=method, headers=dict(self._headers)
        )
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except OSError as exc:
            return 0, json.dumps({"error": str(exc)}).encode()

    def request(self, method, path, payload=None, *, timeout=60, **_ignored):
        body = None if payload is None else json.dumps(payload).encode()
        status, raw = self._send(
            method, path, body, "application/json" if body is not None else "", timeout
        )
        try:
            return status, json.loads(raw.decode() or "null")
        except (ValueError, UnicodeDecodeError):
            return status, {}

    def request_raw(self, method, path, body=b"", *, extra_headers=None, timeout=60):
        headers = dict(self._headers)
        headers.update(extra_headers or {})
        saved, self._headers = self._headers, headers
        try:
            return self._send(method, path, body or None, "", timeout)
        finally:
            self._headers = saved


class Evidence:
    """Collects what the checks assert, in the shape the platform folds in.

    `created` is how a suite hands cleanup back: every DELETE on the candidate is
    admin-only and this token is not, so naming what it made is the only way not
    to leak it.
    """

    def __init__(self) -> None:
        self.assertions: list[dict[str, object]] = []
        self.created_resources: list[dict[str, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.assertions.append({"name": str(name), "passed": bool(passed), "detail": str(detail)})

    def created(self, kind: str, resource_id: str, project: str = "") -> None:
        """Declare a resource the trusted side must remove after the run.

        `project` is needed for the kinds addressed under one — a connection is
        `/api/projects/{project}/connections/{id}`, not a top-level resource.
        """
        if str(resource_id or "").strip():
            self.created_resources.append(
                {
                    "kind": str(kind),
                    "id": str(resource_id).strip(),
                    "project": str(project or "").strip(),
                }
            )


def _declared_inputs() -> dict[str, str]:
    return {
        key[len(INPUT_PREFIX) :]: value
        for key, value in os.environ.items()
        if key.startswith(INPUT_PREFIX)
    }


def _load_checks():
    spec = importlib.util.spec_from_file_location("bundle_release_checks_module", CHECKS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"no release checks at {CHECKS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    evidence = Evidence()
    try:
        module = _load_checks()
        checks = dict(getattr(module, "RELEASE_CHECKS", {}) or {})
    except Exception as exc:
        evidence.add("bundle_release_suite_loaded", False, f"{type(exc).__name__}: {exc}")
        print(json.dumps({"assertions": evidence.assertions, "created": []}))
        return 1

    api = Api(
        os.environ.get("FS_RELEASE_BASE_URL", ""),
        os.environ.get("FS_RELEASE_TOKEN", ""),
        os.environ.get("FS_RELEASE_ACCOUNT", ""),
    )
    # A bundle reads its declared inputs from the environment it was given; they
    # are re-exported under their own names so a suite need not know the prefix.
    for name, value in _declared_inputs().items():
        os.environ.setdefault(name, value)

    selected = str(os.environ.get("FS_RELEASE_CHECK_IDS", "")).split(",")
    selected = [value.strip() for value in selected if value.strip()]
    for check_id in sorted(checks):
        if selected and check_id not in selected:
            continue
        try:
            checks[check_id](api, evidence)
        except Exception as exc:
            evidence.add(
                f"{check_id}_runner",
                False,
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
            )
    print(json.dumps({"assertions": evidence.assertions, "created": evidence.created_resources}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
