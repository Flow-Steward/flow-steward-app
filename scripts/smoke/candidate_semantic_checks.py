#!/usr/bin/env python3
"""Candidate-bound runtime assertions for release checks A13, A19-A27, D09-D10, and E12.

The script intentionally uses only the packaged HTTP API plus the explicitly
named candidate PostgreSQL container.  It is a durable release input, not a
pytest alias, and every fixture is created in the disposable candidate stack.
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
import json
import os
import ssl
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.smoke.bundle_release_checks import (
    CANDIDATE_APP_ALIAS_ENV,
    CANDIDATE_APP_CONTAINER_ENV,
    CANDIDATE_INTERNAL_URL_ENV,
    RELEASE_SERVICES_ENV,
    _service_bindings,
    discover_bundle_suites,
    mint_scoped_token,
    prepared_fixtures,
    remove_created_resources,
    resolve_model_route,
    revoke_token,
    run_bundle_suite,
    suite_network,
    suites_require_model_route,
)
from scripts.smoke.synthetic_extension import ACTION_ID as PROBE_ACTION_ID
from scripts.smoke.synthetic_extension import (
    CALLBACK_DELIVERY_HEADER as PROBE_CALLBACK_DELIVERY_HEADER,
)
from scripts.smoke.synthetic_extension import CALLBACK_ID as PROBE_CALLBACK_ID
from scripts.smoke.synthetic_extension import (
    CALLBACK_SIGNATURE_HEADER as PROBE_CALLBACK_SIGNATURE_HEADER,
)
from scripts.smoke.synthetic_extension import (
    CALLBACK_TIMESTAMP_HEADER as PROBE_CALLBACK_TIMESTAMP_HEADER,
)
from scripts.smoke.synthetic_extension import CONNECTION_TYPE_ID as PROBE_CONNECTION_TYPE_ID
from scripts.smoke.synthetic_extension import INGRESS_ACTION_ID as PROBE_INGRESS_ACTION_ID
from scripts.smoke.synthetic_extension import WORKFLOW_ID as PROBE_WORKFLOW_ID
from scripts.smoke.synthetic_extension import installed_probe_extension

#: A private address on purpose: a self-hosted target is what the override below
#: exists for, and it must not come back in a refusal.
PROBE_PRIVATE_BASE_URL = "https://10.255.255.1"
#: The one input the probe's workflow declares.
PROBE_WORKFLOW_INPUT = "payload"

D09_PARALLEL_LAUNCH_REQUEST = (
    "Prepare an email launch review for Flow Steward 1.0. "
    "Audience: operations teams in a 20-person pilot. "
    "Value proposition: reliable scheduled workflows with human approval. "
    "Launch date: next Monday. Draft the launch copy and identify one operational risk "
    "with a mitigation."
)


def _required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass
class Evidence:
    check_id: str
    assertions: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.assertions.append({"name": name, "passed": bool(passed), "detail": str(detail)[:500]})

    def emit(self) -> None:
        passed = bool(self.assertions) and all(row["passed"] for row in self.assertions)
        print(
            json.dumps(
                {
                    "check_id": self.check_id,
                    "candidate_commit": str(
                        os.environ.get("FS_SMOKE_CANDIDATE_COMMIT") or ""
                    ).strip(),
                    "passed": passed,
                    "assertions": self.assertions,
                },
                sort_keys=True,
            )
        )


class CandidateApi:
    def __init__(self) -> None:
        self.base = _required("FS_SMOKE_BASE").rstrip("/")
        self.origin = self.base
        self.account = str(
            os.environ.get("FS_SMOKE_ACCOUNT") or "00000000-0000-0000-0000-000000000002"
        ).strip()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=context))
        status, payload = self.request(
            "POST",
            "/api/auth/login",
            {
                "email": _required("FS_SMOKE_EMAIL"),
                "password": _required("FS_SMOKE_PASSWORD"),
                "account_id": self.account,
            },
            csrf=False,
        )
        if status != 200 or not isinstance(payload, dict) or not payload.get("csrf_token"):
            raise RuntimeError(f"candidate login failed with HTTP {status}")
        self.csrf = str(payload["csrf_token"])

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        csrf: bool = True,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json", "Origin": self.origin}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf and getattr(self, "csrf", ""):
            headers["x-csrf-token"] = self.csrf
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
            status = int(response.status)
            body = response.read().decode()
        except HTTPError as exc:
            status = int(exc.code)
            body = exc.read().decode()
        try:
            return status, json.loads(body or "{}")
        except json.JSONDecodeError:
            return status, {"_raw": body[:500]}

    def request_raw(
        self,
        method: str,
        path: str,
        body: bytes,
        *,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> tuple[int, Any]:
        """POST a prepared body — an archive upload is not JSON."""
        headers = {"Accept": "application/json", "Origin": self.origin}
        if getattr(self, "csrf", ""):
            headers["x-csrf-token"] = self.csrf
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
            status = int(response.status)
            raw = response.read().decode()
        except HTTPError as exc:
            status = int(exc.code)
            raw = exc.read().decode()
        try:
            return status, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return status, {"_raw": raw[:500]}


def _decode_mcp_body(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body or "{}")
        return payload if isinstance(payload, dict) else {"_raw": body[:500]}
    except json.JSONDecodeError:
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line.split(":", 1)[1].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return {"_raw": body[:500]}


def _public_mcp_request(token: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    base = str(os.environ.get("FS_SMOKE_MCP_URL") or "").strip()
    url = base or (_required("FS_SMOKE_BASE").rstrip("/") + "/mcp")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname in {"fs-mcp", "localhost.localdomain"}:
        raise RuntimeError("FS_SMOKE_MCP_URL must be the external HTTPS MCP endpoint")
    context = ssl.create_default_context(cafile=_required("FS_SMOKE_MCP_CA_FILE"))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        response = build_opener(HTTPSHandler(context=context)).open(request, timeout=30)
        status = int(response.status)
        body = response.read().decode()
    except HTTPError as exc:
        status = int(exc.code)
        body = exc.read().decode()
    return status, _decode_mcp_body(body)


def _mcp_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    for item in content or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        candidates = (text, text[text.find("{") :]) if "{" in text else (text,)
        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return {}


def _mcp_response_shape(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return f"top={sorted(payload)}"
    content = result.get("content")
    items = content if isinstance(content, list) else []
    return (
        f"result={sorted(result)}; content="
        f"{[(item.get('type'), sorted(item)) for item in items if isinstance(item, dict)]}"
    )


def _response_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("message") or "")[:120]
    return str(payload.get("code") or error or payload.get("message") or "")[:120]


def _mcp_call(token: str, request_id: str, name: str, arguments: dict[str, Any]):
    return _public_mcp_request(
        token,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _project_name(api: CandidateApi, project_id: str) -> str:
    status, payload = api.request("GET", f"/api/projects/{project_id}")
    row = payload.get("project") if isinstance(payload, dict) else None
    row = row if isinstance(row, dict) else payload
    name = str((row or {}).get("name") or "").strip() if isinstance(row, dict) else ""
    if status != 200 or not name:
        raise RuntimeError(f"project name lookup failed with HTTP {status}")
    return name


def _psql(sql: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "exec",
            _required("FS_SMOKE_PG_CONTAINER"),
            "psql",
            "-U",
            str(os.environ.get("FS_SMOKE_PG_USER") or "flow_steward"),
            "-d",
            str(os.environ.get("FS_SMOKE_PG_DATABASE") or "flow_steward"),
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"candidate psql failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    rows = payload.get(key) if isinstance(payload, dict) else payload
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _mailpit_request(path: str) -> dict[str, Any]:
    base = _required("FS_SMOKE_MAILPIT_BASE").rstrip("/")
    with build_opener().open(Request(base + path), timeout=15) as response:
        payload = json.loads(response.read().decode() or "{}")
    return dict(payload) if isinstance(payload, dict) else {}


def _mailpit_messages_for(recipient: str) -> list[dict[str, Any]]:
    listing = _mailpit_request(f"/api/v1/search?query={quote('to:' + recipient, safe='')}")
    messages: list[dict[str, Any]] = []
    for row in listing.get("messages") or []:
        if not isinstance(row, dict) or not row.get("ID"):
            continue
        messages.append(_mailpit_request(f"/api/v1/message/{quote(str(row['ID']), safe='')}"))
    return messages


def _mailpit_address(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("Address") or value.get("address") or "").strip()
    return str(value or "").strip()


def _mailpit_message_matches(
    message: dict[str, Any],
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> bool:
    recipients = message.get("To") or message.get("to") or []
    if not isinstance(recipients, list):
        recipients = [recipients]
    return (
        _mailpit_address(message.get("From") or message.get("from")) == sender
        and recipient in {_mailpit_address(value) for value in recipients}
        and str(message.get("Subject") or message.get("subject") or "") == subject
        and str(message.get("Text") or message.get("text") or "").replace("\r\n", "\n").rstrip("\n")
        == body.replace("\r\n", "\n").rstrip("\n")
    )


def _a06(_api: CandidateApi, evidence: Evidence) -> None:
    """Prove every extension on the stand is healthy, independent of install state.

    No bundle is named, and none is required: a release ships no extensions (each
    installs from the catalog), so a clean candidate has none and passes, and
    whatever a stand does hold must report healthy.
    """
    command = """
import json
from pathlib import Path
from core.infrastructure.extension_runtime.extension_runtime import run_extension_health, scan_extensions
rows = scan_extensions(Path('/app/extensions'))
health = {row.get('extension_key'): run_extension_health(row) for row in rows}
unhealthy = {key: value for key, value in health.items() if value.get('status') != 'healthy'}
print(json.dumps({'count': len(rows), 'unhealthy': unhealthy}))
"""
    completed = subprocess.run(
        ["docker", "exec", _required("FS_SMOKE_APP_CONTAINER"), "python", "-c", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    unhealthy = payload.get("unhealthy") if isinstance(payload, dict) else None
    unhealthy = unhealthy if isinstance(unhealthy, dict) else {}
    count = payload.get("count") if isinstance(payload, dict) else 0
    evidence.add(
        "packaged_extension_health",
        completed.returncode == 0 and isinstance(count, int) and not unhealthy,
        f"container_exit={completed.returncode}; packaged={count}; unhealthy={sorted(unhealthy)}",
    )


def _workflow(api: CandidateApi) -> dict[str, Any]:
    status, payload = api.request("GET", "/api/workflows?per_page=500")
    if status != 200:
        raise RuntimeError(f"workflow discovery failed with HTTP {status}")
    enabled: list[dict[str, Any]] = []
    for row in _rows(payload, "workflows"):
        workflow_id = str(row.get("workflow_id") or row.get("id") or "").strip()
        if workflow_id and row.get("enabled", True) is not False:
            row["workflow_id"] = workflow_id
            enabled.append(row)
    if enabled:
        return enabled[0]
    raise RuntimeError("candidate has no enabled workflow")


def _workspace(api: CandidateApi) -> str:
    status, payload = api.request("GET", "/api/workspaces")
    if status != 200:
        raise RuntimeError(f"workspace discovery failed with HTTP {status}")
    rows = _rows(payload, "workspaces")
    for row in rows:
        if str(row.get("workspace_key") or "") == "default":
            return str(row.get("id") or "")
    if rows:
        return str(rows[0].get("id") or "")
    raise RuntimeError("candidate has no workspace")


def _project(api: CandidateApi, workflow_id: str) -> str:
    name = f"release-semantic-{uuid.uuid4().hex[:10]}"
    request_payload: dict[str, Any] = {
        "name": name,
        "workspace_id": _workspace(api),
        "workflow_ids": [workflow_id],
    }
    provider = str(os.environ.get("FS_SMOKE_MODEL_PROVIDER") or "").strip()
    model = str(os.environ.get("FS_SMOKE_MODEL") or "").strip()
    if provider and model:
        request_payload.update(default_provider=provider, default_model=model)
    status, payload = api.request(
        "POST",
        "/api/projects",
        request_payload,
    )
    project_id = str(payload.get("project_id") or "") if isinstance(payload, dict) else ""
    if status != 201 or not project_id:
        raise RuntimeError(f"candidate project fixture failed with HTTP {status}: {payload!r}")
    return project_id


def _task(
    api: CandidateApi,
    project_id: str,
    workflow_id: str,
    marker: str,
    *,
    inputs: dict[str, Any] | None = None,
) -> str:
    status, payload = api.request(
        "POST",
        "/api/workflow-runs",
        {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "inputs": dict(inputs or {}),
        },
    )
    job_id = str(payload.get("job_id") or "") if isinstance(payload, dict) else ""
    if status not in {200, 201, 202} or not job_id:
        raise RuntimeError(f"candidate task fixture failed with HTTP {status}: {payload!r}")
    return job_id


def _cleanup_project(api: CandidateApi, project_id: str) -> None:
    jobs_status, jobs_payload = api.request("GET", f"/api/projects/{project_id}/jobs?per_page=100")
    if jobs_status != 200:
        raise RuntimeError(
            f"candidate cleanup could not list project jobs: HTTP {jobs_status}: {jobs_payload!r}"
        )
    terminal = {"completed", "failed", "cancelled", "max_iterations", "preflight_failed"}
    for job in _rows(jobs_payload, "jobs"):
        job_id = str(job.get("id") or job.get("job_id") or "").strip()
        job_status = str(job.get("status") or "").strip().lower()
        if job_id and job_status not in terminal:
            _settle_job_for_cleanup(api, job_id)

    trash_path = f"/api/projects/{project_id}"
    status, payload = api.request("DELETE", trash_path, {})
    if status != 200:
        raise RuntimeError(f"candidate cleanup {trash_path} failed with HTTP {status}: {payload!r}")

    execute_path = f"/api/trash/project/{project_id}/execute"
    deadline = time.monotonic() + 30.0
    while True:
        status, payload = _purge_trashed_project(api, project_id)
        if status == 200:
            break
        retryable = status == 409 and _response_code(payload) == "project_executors_not_quiesced"
        if not retryable or time.monotonic() >= deadline:
            raise RuntimeError(
                f"candidate cleanup {execute_path} failed with HTTP {status}: {payload!r}"
            )
        time.sleep(1.0)
    _assert_product_removed_project_queues(project_id)


def _response_code(payload: Any) -> str:
    return str(payload.get("code") or "") if isinstance(payload, dict) else ""


def _purge_trashed_project(api: CandidateApi, project_id: str) -> tuple[int, Any]:
    """Hard-delete a trashed project the way the product does: plan, then execute it.

    The direct project hard-delete endpoint refuses every project with 409; a
    purge names the plan revision it was shown.
    """
    plan_path = f"/api/trash/project/{project_id}/plan"
    plan_status, plan = api.request("POST", plan_path, {"action_type": "hard_delete"})
    plan_version = str(plan.get("plan_version") or "") if isinstance(plan, dict) else ""
    if plan_status != 200 or not plan_version:
        raise RuntimeError(f"candidate purge {plan_path} failed with HTTP {plan_status}: {plan!r}")
    return api.request(
        "POST",
        f"/api/trash/project/{project_id}/execute",
        {"action_type": "hard_delete", "plan_version": plan_version},
    )


def _project_queue_names() -> set[str]:
    rabbit = _required("FS_SMOKE_RABBITMQ_CONTAINER")
    listed = subprocess.run(
        ["docker", "exec", rabbit, "rabbitmqctl", "list_queues", "--quiet", "name"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"candidate queue inventory failed: {listed.stderr.strip()[:500]}")
    return set(listed.stdout.splitlines())


def _purge_project_queues(project_id: str) -> None:
    rabbit = _required("FS_SMOKE_RABBITMQ_CONTAINER")
    present = _project_queue_names()
    for queue in (
        f"platform.project.{project_id}",
        f"platform.project.{project_id}.dead_letter",
    ):
        if queue not in present:
            continue
        purged = subprocess.run(
            ["docker", "exec", rabbit, "rabbitmqctl", "purge_queue", queue],
            capture_output=True,
            text=True,
            check=False,
        )
        if purged.returncode != 0:
            raise RuntimeError(
                f"candidate queue cleanup {queue} failed: {purged.stderr.strip()[:500]}"
            )


def _assert_product_removed_project_queues(project_id: str) -> None:
    expected = {
        f"platform.project.{project_id}",
        f"platform.project.{project_id}.dead_letter",
    }
    orphaned = sorted(expected & _project_queue_names())
    if not orphaned:
        return
    # Retain a bounded fail-safe so a failed release check does not poison the
    # disposable candidate. The assertion still fails and cannot certify the
    # cleanup that this harness performed itself.
    _purge_project_queues(project_id)
    raise RuntimeError(f"product left project queues after hard delete: {orphaned}")


def _wait_job_terminal(api: CandidateApi, job_id: str, timeout: int = 90) -> str:
    terminal = {"completed", "failed", "cancelled", "max_iterations", "preflight_failed"}
    deadline = time.monotonic() + timeout
    status_text = ""
    while time.monotonic() < deadline:
        status, payload = api.request("GET", f"/api/jobs/{job_id}")
        if status == 200 and isinstance(payload, dict):
            job = payload.get("job") if isinstance(payload.get("job"), dict) else payload
            status_text = str(job.get("status") or "").strip().lower()
            if status_text in terminal:
                return status_text
        time.sleep(2)
    api.request("POST", f"/api/jobs/{job_id}/cancel", {"reason": "semantic timeout"})
    return status_text


def _settle_job_for_cleanup(api: CandidateApi, job_id: str) -> None:
    """Prevent an in-flight delivery from being requeued after fixture deletion."""
    cancel_status, _ = api.request(
        "POST",
        f"/api/jobs/{job_id}/cancel",
        {"reason": "release smoke fixture cleanup"},
    )
    terminal = _wait_job_terminal(api, job_id, timeout=30)
    if cancel_status not in {200, 409} or terminal not in {
        "completed",
        "failed",
        "cancelled",
        "max_iterations",
        "preflight_failed",
    }:
        raise RuntimeError(
            f"candidate job cleanup failed for {job_id}: "
            f"cancel HTTP {cancel_status}; terminal={terminal or 'missing'}"
        )


def _wait_job_state(
    api: CandidateApi,
    job_id: str,
    target_statuses: set[str],
    *,
    timeout: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, payload = api.request("GET", f"/api/jobs/{job_id}")
        if status == 200 and isinstance(payload, dict):
            last = dict(payload.get("job") or payload)
            if str(last.get("status") or "").strip().lower() in target_statuses:
                return last
        time.sleep(2)
    raise RuntimeError(
        f"job {job_id} did not reach {sorted(target_statuses)}; "
        f"last={last.get('status') or '<missing>'!s}"
    )


def _approval_gate(api: CandidateApi, job_id: str) -> dict[str, Any]:
    status, payload = api.request("GET", f"/api/jobs/{job_id}/approval")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"approval lookup for job {job_id} failed with HTTP {status}")
    return dict(payload)


def _approval_history(api: CandidateApi, job_id: str) -> list[dict[str, Any]]:
    status, payload = api.request("GET", f"/api/jobs/{job_id}/approvals")
    if status != 200:
        raise RuntimeError(f"approval history for job {job_id} failed with HTTP {status}")
    return _rows(payload, "approvals")


def _decide_approval(
    api: CandidateApi,
    *,
    thread_id: str,
    approval_id: str,
    approved: bool,
    comment: str,
    idempotency_key: str,
) -> tuple[int, Any]:
    return api.request(
        "POST",
        f"/api/approvals/{thread_id}",
        {"approval_id": approval_id, "approved": approved, "comment": comment},
        extra_headers={"Idempotency-Key": idempotency_key},
    )


def _node(node_id: str, kind: str = "step") -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def _graph_edge(
    source: str,
    target: str,
    outcome: str = "success",
    *,
    case_id: str = "",
    rework_limit: int | None = None,
) -> dict[str, Any]:
    """One execution-graph edge between two steps; a rework edge carries its limit."""
    edge: dict[str, Any] = {
        "source": _node(source),
        "outcome": {"name": outcome, **({"case_id": case_id} if case_id else {})},
        "target": _node(target),
        "rework": rework_limit is not None,
    }
    if rework_limit is not None:
        edge["rework_limit"] = rework_limit
    return edge


def _definition_operations(
    steps: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    entry: str | None = None,
) -> list[dict[str, Any]]:
    """The change set that creates *steps*, sets the entry and connects *edges*."""
    entry_id = entry or str(steps[0]["step_id"])
    return [
        *({"op": "create_step", "step": step} for step in steps),
        {"op": "set_entry", "target": _node(entry_id)},
        *({"op": "connect", "edge": edge} for edge in edges),
    ]


def _linear_operations(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps that run one after another on their success outcome."""
    step_ids = [str(step["step_id"]) for step in steps]
    return _definition_operations(
        list(steps),
        [_graph_edge(source, target) for source, target in pairwise(step_ids)],
    )


def _draft_workflow(
    api: CandidateApi,
    metadata: dict[str, Any],
    operations: list[dict[str, Any]],
) -> tuple[int, Any]:
    """Create a disabled workflow and apply one change set at its revision.

    Core takes a workflow's metadata on create and its steps, groups and graph
    only through /api/workflows/<id>/changes. A disabled workflow keeps an
    invalid draft and reports its issues; the change response is returned.
    """
    workflow_id = str(metadata["workflow_id"])
    status, payload = api.request("POST", "/api/workflows", {**metadata, "enabled": False})
    if status not in {200, 201}:
        raise RuntimeError(f"workflow {workflow_id} create failed with HTTP {status}: {payload!r}")
    read_status, current = api.request("GET", f"/api/workflows/{workflow_id}")
    if read_status != 200 or not isinstance(current, dict):
        raise RuntimeError(f"workflow {workflow_id} read failed with HTTP {read_status}")
    return api.request(
        "POST",
        f"/api/workflows/{workflow_id}/changes",
        {"expected_revision": current.get("revision"), "operations": operations},
    )


def _enable_workflow(api: CandidateApi, workflow_id: str) -> tuple[int, Any]:
    read_status, current = api.request("GET", f"/api/workflows/{workflow_id}")
    if read_status != 200 or not isinstance(current, dict):
        raise RuntimeError(f"workflow {workflow_id} read failed with HTTP {read_status}")
    return api.request(
        "PATCH",
        f"/api/workflows/{workflow_id}",
        {"enabled": True, "expected_revision": current.get("revision")},
    )


def _create_workflow(
    api: CandidateApi,
    metadata: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    label: str,
) -> str:
    """Create, build and enable one valid fixture workflow; any refusal is fatal."""
    workflow_id = str(metadata["workflow_id"])
    change_status, change = _draft_workflow(api, metadata, operations)
    if change_status != 200 or not (change or {}).get("valid"):
        raise RuntimeError(
            f"{label} workflow {workflow_id} change set failed with HTTP {change_status}: "
            f"{change!r}"
        )
    enable_status, enabled = _enable_workflow(api, workflow_id)
    if enable_status != 200:
        raise RuntimeError(
            f"{label} workflow {workflow_id} enable failed with HTTP {enable_status}: {enabled!r}"
        )
    return workflow_id


def _create_hitl_workflow(api: CandidateApi) -> str:
    """Create an isolated deterministic workflow that needs no model provider.

    A transform records one effect, an Approval step reviews it; a rejection
    reworks the transform at most twice, an approval finishes the run.
    """
    workflow_id = f"a23_gate_{uuid.uuid4().hex[:10]}"
    steps = [
        {
            "step_id": "normalize_effect",
            "display_name": "Record deterministic effect",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": "state.inputs.payload"},
                    "result_target": "state.a23_effect",
                    "fields": [{"from": "marker", "to": "marker", "type": "text"}],
                }
            },
        },
        {
            "step_id": "review",
            "display_name": "Deterministic review",
            "step_kind": "approval",
            "override": {"approval": {"message": "Review the deterministic A23 fixture."}},
        },
        {
            "step_id": "done",
            "display_name": "Finish",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "A23 fixture completed."}},
        },
    ]
    edges = [
        _graph_edge("normalize_effect", "review"),
        _graph_edge("review", "done", "approved"),
        _graph_edge("review", "normalize_effect", "rejected", rework_limit=2),
    ]
    return _create_workflow(
        api,
        {
            "workflow_id": workflow_id,
            "display_name": "A23 deterministic approval fixture",
            "description": "Disposable candidate-owned HITL release fixture.",
            "settings": {
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["ui", "api", "mcp"],
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "expected_inputs": [{"name": "payload", "type": "object", "required": True}],
                    "output_mode": "none",
                    "expected_outputs": [],
                }
            },
        },
        _definition_operations(steps, edges),
        label="A23",
    )


def _hitl_effect_count(job_id: str) -> int:
    return int(
        _psql(
            "select count(*) from platform_events where "
            f"job_id={_sql_literal(job_id)}::uuid and event_type='workflow_run.state' "
            "and detail->'step'->>'step_id'='normalize_effect' "
            "and detail->'step'->>'status'='completed'"
        )
        or 0
    )


def _cleanup_hitl_fixture(api: CandidateApi, project_id: str, workflow_id: str) -> None:
    """Remove the release-owned fixture through the product lifecycle API."""
    _cleanup_project(api, project_id)
    for method, path in (
        ("DELETE", f"/api/workflows/{workflow_id}"),
        ("POST", f"/api/trash/registry_workflow/{workflow_id}/hard-delete"),
    ):
        status, payload = api.request(method, path, {})
        if status != 200:
            raise RuntimeError(f"A23 cleanup {path} failed with HTTP {status}: {payload!r}")


def _cutover_base_url() -> str:
    explicit = str(os.environ.get("FS_SMOKE_CUTOVER_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    public = urlsplit(_required("FS_SMOKE_BASE"))
    if public.scheme == "https" and public.port is not None:
        return f"https://traefik:{public.port}"
    return _required("FS_SMOKE_INTERNAL_BASE").rstrip("/")


def _cutover_cli(
    *,
    project_id: str,
    auth_option: str,
    secret: str,
    username: str = "",
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "exec",
        "-i",
        _required("FS_SMOKE_CLI_CONTAINER"),
        "python",
        "-m",
        "core.entrypoints.cli",
        "testing",
        "cutover-smoke",
        "--project-id",
        project_id,
        "--base-url",
        _cutover_base_url(),
        "--timeout-seconds",
        str(int(os.environ.get("FS_SMOKE_CUTOVER_TIMEOUT_SECONDS") or 900)),
        "--poll-seconds",
        str(int(os.environ.get("FS_SMOKE_CUTOVER_POLL_SECONDS") or 2)),
    ]
    if username:
        command.extend(["--username", username])
    command.extend([auth_option, "--confirm", "--plain"])
    return subprocess.run(
        command,
        input=secret + "\n",
        capture_output=True,
        text=True,
        timeout=960,
        check=False,
    )


def _create_cutover_fixture(api: CandidateApi) -> tuple[str, str]:
    """Create a provider-free workflow that emits at least three real messages."""
    workflow_id = f"e12_cutover_{uuid.uuid4().hex[:10]}"
    steps: list[dict[str, Any]] = [
        {
            "step_id": f"length_{index}",
            "display_name": f"Length {index}",
            "step_kind": "connector",
            "connector_id": "connector.std.text",
            "override": {"connector": {"function_name": "length"}},
            "input_mapping_jsonb": {"value": "state.inputs.message"},
            "output_mapping_jsonb": {"result": f"state.length_{index}"},
        }
        for index in range(1, 4)
    ]
    steps.append(
        {
            "step_id": "done",
            "display_name": "Done",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "E12 complete."}},
        }
    )
    _create_workflow(
        api,
        {
            "workflow_id": workflow_id,
            "display_name": "E12 deterministic cutover fixture",
            "description": "Disposable packaged-CLI release fixture.",
            "settings": {
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["ui", "api", "mcp"],
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "expected_inputs": [{"name": "message", "type": "text", "required": True}],
                    "output_mode": "variables",
                    "expected_outputs": [{"name": "length_3", "type": "number", "required": False}],
                }
            },
        },
        _linear_operations(*steps),
        label="E12",
    )
    return workflow_id, _project(api, workflow_id)


def _e12(api: CandidateApi, evidence: Evidence) -> None:
    """Exercise the packaged cutover CLI with a fresh PAT and local login."""
    workflow_id, project_id = _create_cutover_fixture(api)
    create_status, created = api.request(
        "POST",
        "/api/auth/tokens",
        {
            "label": f"release-e12-{uuid.uuid4().hex[:8]}",
            "expires_in_days": 1,
            "scopes": ["admin:all"],
            "allowed_project_ids": [project_id],
        },
    )
    token_row = created.get("token") if isinstance(created, dict) else None
    token_id = str((created or {}).get("token_id") or "") if isinstance(created, dict) else ""
    raw_token = str((token_row or {}).get("raw_token") or "") if isinstance(token_row, dict) else ""
    evidence.add(
        "fresh_candidate_pat",
        create_status == 200 and bool(token_id) and bool(raw_token),
        f"HTTP {create_status}; token_id_present={bool(token_id)}",
    )
    if not raw_token:
        _cleanup_hitl_fixture(api, project_id, workflow_id)
        return

    password = _required("FS_SMOKE_PASSWORD")
    try:
        pat_run = _cutover_cli(
            project_id=project_id,
            auth_option="--bearer-token-stdin",
            secret=raw_token,
        )
        pat_output = pat_run.stdout + pat_run.stderr
        evidence.add(
            "packaged_pat_cutover",
            pat_run.returncode == 0 and "[smoke] PASS" in pat_output,
            f"exit={pat_run.returncode}; pass_marker={'[smoke] PASS' in pat_output}",
        )
        evidence.add(
            "pat_secret_safe",
            raw_token not in pat_output,
            f"output_bytes={len(pat_output)}",
        )

        password_run = _cutover_cli(
            project_id=project_id,
            auth_option="--password-stdin",
            secret=password,
            username=_required("FS_SMOKE_EMAIL"),
        )
        password_output = password_run.stdout + password_run.stderr
        evidence.add(
            "packaged_password_cutover",
            password_run.returncode == 0 and "[smoke] PASS" in password_output,
            f"exit={password_run.returncode}; pass_marker={'[smoke] PASS' in password_output}",
        )
        evidence.add(
            "password_secret_safe",
            password not in password_output,
            f"output_bytes={len(password_output)}",
        )
    finally:
        revoke_status, _ = api.request("POST", f"/api/auth/tokens/{token_id}/revoke", {})
        evidence.add("temporary_pat_revoked", revoke_status == 200, f"HTTP {revoke_status}")
        _cleanup_hitl_fixture(api, project_id, workflow_id)


def _a13(api: CandidateApi, evidence: Evidence) -> None:
    """Prove a workflow's declared input is published and reaches the job envelope.

    The old check reached this through a particular bundle: it imported that
    bundle's release, found its dataset workflow, and asserted the workflow's own
    `request_note` input. The two things it actually proved are the platform's —
    that a declared input appears in the published contract, and that a submitted
    value survives into the job's task envelope — so both now run on the probe's
    own workflow.

    What did not move was already covered elsewhere: that every packaged bundle
    installs and is healthy is `packaged_extension_health`, and that a particular
    bundle publishes its workflows is that bundle's own release suite.
    """
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        api.request("POST", f"/api/extensions/{extension_id}/enable", {})
        _a13_named_input(api, evidence)


def _a13_named_input(api: CandidateApi, evidence: Evidence) -> None:
    workflow_id = _probe_workflow_id(api)
    contract_status, contract = api.request("GET", f"/api/workflows/{workflow_id}/contract")
    interface = contract.get("interface") if isinstance(contract, dict) else {}
    schema = interface.get("input_schema") if isinstance(interface, dict) else {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    declared = PROBE_WORKFLOW_INPUT
    evidence.add(
        "named_input_declared",
        contract_status == 200 and declared in (properties or {}),
        f"HTTP {contract_status}; properties={sorted((properties or {}).keys())}",
    )
    if declared not in (properties or {}):
        return
    project_id = ""
    job_id = ""
    try:
        create_status, created = api.request(
            "POST",
            "/api/projects",
            {
                "name": f"named-input-{uuid.uuid4().hex[:8]}",
                "workspace_id": _workspace(api),
                "workflow_ids": [workflow_id],
            },
        )
        project_id = str(created.get("project_id") or "") if isinstance(created, dict) else ""
        marker = f"named-input-{uuid.uuid4().hex[:10]}"
        submit_status, submitted = api.request(
            "POST",
            "/api/workflow-runs",
            {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "inputs": {declared: {"marker": marker}},
            },
        )
        job_id = str(submitted.get("job_id") or "") if isinstance(submitted, dict) else ""
        persisted = (
            _psql(
                "select coalesce("
                f"meta->'task_envelope'->'inputs'->'{declared}'->>'marker','') "
                f"from jobs where id={_sql_literal(job_id)}::uuid"
            )
            if job_id
            else ""
        )
        evidence.add(
            "named_input_persisted",
            create_status == 201 and submit_status in {200, 201, 202} and persisted == marker,
            f"project={create_status}; submit={submit_status}; job={job_id}; "
            f"persisted={persisted == marker}",
        )
    finally:
        if job_id:
            api.request("POST", f"/api/jobs/{job_id}/cancel", {"reason": "A13 evidence complete"})
        if project_id:
            _cleanup_project(api, project_id)


def _a19(api: CandidateApi, evidence: Evidence) -> None:
    workflow = _workflow(api)
    workflow_id = workflow["workflow_id"]
    project_id = _project(api, workflow_id)
    try:
        job_id = _task(api, project_id, workflow_id, "a19")
        trace_status, trace = api.request("GET", f"/api/jobs/{job_id}/trace")
        evidence.add(
            "trace_surface",
            trace_status == 200 and isinstance(trace, dict),
            f"HTTP {trace_status}; job={job_id}",
        )
        marker = f"candidate-provider-{uuid.uuid4().hex[:12]}"
        ingest_status, ingest = api.request(
            "POST",
            "/api/reactions/ingest",
            {
                "event_type": "provider.semantic.received",
                "job_id": job_id,
                "workflow_id": workflow_id,
                "source": "provider_event_surface",
                "source_id": marker,
                "event_key": marker,
                "dedupe_key": marker,
                "payload": {"candidate_commit": _required("FS_SMOKE_CANDIDATE_COMMIT")},
            },
        )
        event = ingest.get("event") if isinstance(ingest, dict) else None
        evidence.add(
            "provider_event_ingest",
            ingest_status == 200 and isinstance(event, dict),
            f"HTTP {ingest_status}; marker={marker}",
        )
        reactions_status, reactions = api.request("GET", f"/api/jobs/{job_id}/reactions")
        reaction_rows = _rows(reactions, "reactions")
        matched = any(
            str(row.get("source_id") or row.get("event_key") or "") == marker
            for row in reaction_rows
        )
        evidence.add(
            "trace_reaction_provider_fixture_parity",
            reactions_status == 200 and matched,
            f"HTTP {reactions_status}; matched={matched}; events={len(reaction_rows)}",
        )
    finally:
        _cleanup_project(api, project_id)


def _envelope(job_id: str) -> dict[str, Any]:
    raw = _psql(
        "select coalesce(meta->'task_envelope','{}'::jsonb)::text "
        f"from jobs where id='{job_id}'::uuid"
    )
    return json.loads(raw or "{}")


def _a20(api: CandidateApi, evidence: Evidence) -> None:
    workflow_id = _workflow(api)["workflow_id"]
    project_id = _project(api, workflow_id)
    try:
        original_id = _task(api, project_id, workflow_id, "a20")
        original = _envelope(original_id)
        evidence.add(
            "submission_envelope",
            str(original.get("project_id") or "") == project_id
            and str(original.get("requested_workflow_id") or "") == workflow_id,
            f"job={original_id}; project/workflow envelope bound",
        )
        api.request("POST", f"/api/jobs/{original_id}/cancel", {"reason": "semantic restart"})
        _wait_job_terminal(api, original_id)
        restart_status, restart = api.request("POST", f"/api/jobs/{original_id}/restart")
        restarted_id = str(restart.get("new_job_id") or "") if isinstance(restart, dict) else ""
        restarted = _envelope(restarted_id) if restarted_id else {}
        stable_keys = ("project_id", "requested_workflow_id", "inputs")
        evidence.add(
            "restart_envelope",
            restart_status == 200
            and bool(restarted_id)
            and all(restarted.get(key) == original.get(key) for key in stable_keys),
            f"HTTP {restart_status}; original={original_id}; restarted={restarted_id}",
        )
        schedule_status, schedule = api.request(
            "POST",
            f"/api/projects/{project_id}/scheduled-jobs",
            {
                "task_summary": "Candidate schedule envelope",
                "instructions": "Preserve named semantic input",
                "workflow_id": workflow_id,
                "cron_expr": "17 4 * * *",
                "enabled": False,
                "inputs": {},
            },
        )
        schedule_id = (
            str(schedule.get("scheduled_job_id") or "") if isinstance(schedule, dict) else ""
        )
        run_status, run = api.request(
            "POST", f"/api/projects/{project_id}/scheduled-jobs/{schedule_id}/run-now"
        )
        scheduled_job_id = str(run.get("job_id") or "") if isinstance(run, dict) else ""
        scheduled = _envelope(scheduled_job_id) if scheduled_job_id else {}
        evidence.add(
            "schedule_envelope",
            schedule_status == 201
            and run_status == 200
            and scheduled.get("inputs") == {}
            and str(scheduled.get("requested_workflow_id") or "") == workflow_id,
            f"create={schedule_status}; run={run_status}; job={scheduled_job_id}",
        )
    finally:
        _cleanup_project(api, project_id)


def _a21(api: CandidateApi, evidence: Evidence) -> None:
    """Discover reusable enabled workflows through the scoped authoring API."""
    workflow = _workflow(api)
    workflow_id = workflow["workflow_id"]
    project_id = _project(api, workflow_id)
    try:
        catalog_status, catalog_payload = api.request("GET", "/api/workflows?per_page=500")
        enabled_ids = {
            str(row.get("workflow_id") or row.get("id") or "").strip()
            for row in _rows(catalog_payload, "workflows")
            if row.get("enabled", True) is not False
        }
        query = quote(workflow_id, safe="")
        status, payload = api.request(
            "GET",
            f"/api/projects/{project_id}/workflow-authoring/examples?query={query}&limit=50",
        )
        examples = _rows(payload, "examples")
        discovered_ids = {str(row.get("workflow_id") or "").strip() for row in examples}
        evidence.add(
            "authorized_enabled_workflow_reuse",
            catalog_status == 200
            and status == 200
            and workflow_id in discovered_ids
            and discovered_ids <= enabled_ids
            and all(row.get("bundle_yaml") for row in examples),
            f"catalog={catalog_status}; examples={status}; selected={workflow_id}; discovered={sorted(discovered_ids)}",
        )
        disabled_ids = set(
            json.loads(
                _psql(
                    "select coalesce(json_agg(workflow_key),'[]'::json)::text from registry_workflows "
                    f"where account_id={_sql_literal(api.account)}::uuid and not enabled"
                )
                or "[]"
            )
        )
        evidence.add(
            "disabled_workflows_not_selected",
            status == 200 and bool(discovered_ids) and not (discovered_ids & disabled_ids),
            f"discovered={sorted(discovered_ids)}; disabled_catalog_rows={len(disabled_ids)}",
        )
    finally:
        _cleanup_project(api, project_id)


def _a22(api: CandidateApi, evidence: Evidence) -> None:
    workflow_id = _workflow(api)["workflow_id"]
    project_id = _project(api, workflow_id)
    try:
        create_status, created = api.request(
            "POST",
            f"/api/projects/{project_id}/scheduled-jobs",
            {
                "task_summary": "Candidate repeated lock slot",
                "instructions": "Prove exact lock release",
                "workflow_id": workflow_id,
                "cron_expr": "23 4 * * *",
                "enabled": False,
            },
        )
        schedule_id = (
            str(created.get("scheduled_job_id") or "") if isinstance(created, dict) else ""
        )
        job_ids: list[str] = []
        for slot in range(2):
            run_status, run = api.request(
                "POST", f"/api/projects/{project_id}/scheduled-jobs/{schedule_id}/run-now"
            )
            job_id = str(run.get("job_id") or "") if isinstance(run, dict) else ""
            if run_status == 200 and job_id:
                job_ids.append(job_id)
                api.request("POST", f"/api/jobs/{job_id}/cancel", {"reason": f"slot {slot + 1}"})
                _wait_job_terminal(api, job_id)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                active = int(
                    _psql(
                        "select count(*) from scheduled_job_execution_locks "
                        f"where scheduled_job_id='{schedule_id}'::uuid "
                        "and status in ('pre_publish','active')"
                    )
                    or 0
                )
                if active == 0:
                    break
                time.sleep(2)
        rows = _psql(
            "select count(*)||'|'||count(distinct id)||'|'||"
            "count(*) filter (where status='released') "
            "from scheduled_job_execution_locks "
            f"where scheduled_job_id='{schedule_id}'::uuid"
        )
        total, distinct, released = [int(value) for value in rows.split("|")]
        evidence.add(
            "repeated_slots_dispatch",
            create_status == 201 and len(job_ids) == 2 and job_ids[0] != job_ids[1],
            f"create={create_status}; jobs={job_ids}",
        )
        evidence.add(
            "exact_lock_release",
            total >= 2 and distinct == total and released == total,
            f"locks={total}; distinct={distinct}; released={released}",
        )
    finally:
        _cleanup_project(api, project_id)


def _a23(api: CandidateApi, evidence: Evidence) -> None:
    """Create and drive the candidate's own HITL fixture through accepted semantics."""
    workflow_id = _create_hitl_workflow(api)
    evidence.add(
        "gate_fixture_workflow",
        workflow_id.startswith("a23_gate_"),
        f"workflow={workflow_id or '<missing>'}",
    )

    project_id = _project(api, workflow_id)
    try:
        job_id = _task(
            api,
            project_id,
            workflow_id,
            "a23-hitl",
            inputs={"payload": {"marker": "once"}},
        )
        first = _wait_job_state(
            api,
            job_id,
            {"awaiting_approval", "failed", "cancelled", "preflight_failed"},
        )
        evidence.add(
            "gate_created_by_release_runner",
            str(first.get("status") or "").lower() == "awaiting_approval",
            f"job={job_id}; status={first.get('status')}",
        )
        if str(first.get("status") or "").lower() != "awaiting_approval":
            return

        first_gate = _approval_gate(api, job_id)
        approval_id = str(first_gate.get("approval_id") or "").strip()
        thread_id = str(first_gate.get("thread_id") or first.get("thread_id") or "").strip()
        feedback = f"A23 bounded rework {uuid.uuid4().hex[:10]}"
        decision_key = f"a23-reject:{approval_id}"
        before = _approval_history(api, job_id)
        effects_before = _hitl_effect_count(job_id)
        reject_status, reject_payload = _decide_approval(
            api,
            thread_id=thread_id,
            approval_id=approval_id,
            approved=False,
            comment=feedback,
            idempotency_key=decision_key,
        )
        duplicate_status, duplicate_payload = _decide_approval(
            api,
            thread_id=thread_id,
            approval_id=approval_id,
            approved=False,
            comment=feedback,
            idempotency_key=decision_key,
        )
        after = _approval_history(api, job_id)
        effects_after_duplicate = _hitl_effect_count(job_id)
        matching = [row for row in after if str(row.get("comment") or "") == feedback]
        evidence.add(
            "reject_is_rework",
            reject_status == 202
            and str((reject_payload or {}).get("status") or "").lower()
            not in {"failed", "cancelled"},
            f"HTTP {reject_status}; status={(reject_payload or {}).get('status')}",
        )
        evidence.add(
            "feedback_propagated",
            len(matching) == 1 and len(after) == len(before) + 1,
            f"history_before={len(before)}; history_after={len(after)}; feedback_matches={len(matching)}",
        )
        evidence.add(
            "duplicate_decision_suppressed",
            duplicate_status == 202
            and isinstance(duplicate_payload, dict)
            and bool(duplicate_payload.get("deduplicated"))
            and len(matching) == 1,
            f"HTTP {duplicate_status}; deduplicated={bool((duplicate_payload or {}).get('deduplicated'))}",
        )
        evidence.add(
            "duplicate_unchanged_effects",
            effects_before == 1 and effects_after_duplicate == effects_before,
            f"before={effects_before}; after_duplicate={effects_after_duplicate}",
        )

        terminal_status = ""
        approval_attempts = 0
        poll_rounds = 0
        previous_approval_id = approval_id
        while approval_attempts < 12 and poll_rounds < 180:
            poll_rounds += 1
            state = _wait_job_state(
                api,
                job_id,
                {
                    "awaiting_approval",
                    "completed",
                    "failed",
                    "cancelled",
                    "max_iterations",
                    "preflight_failed",
                },
            )
            terminal_status = str(state.get("status") or "").strip().lower()
            if terminal_status != "awaiting_approval":
                break
            gate = _approval_gate(api, job_id)
            current_approval_id = str(gate.get("approval_id") or "").strip()
            current_thread_id = str(
                gate.get("thread_id") or state.get("thread_id") or thread_id
            ).strip()
            if not current_approval_id or current_approval_id == previous_approval_id:
                time.sleep(2)
                continue
            approve_status, _ = _decide_approval(
                api,
                thread_id=current_thread_id,
                approval_id=current_approval_id,
                approved=True,
                comment="A23 approval after bounded rework",
                idempotency_key=f"a23-approve:{current_approval_id}",
            )
            if approve_status != 202:
                raise RuntimeError(
                    f"approval {current_approval_id} failed with HTTP {approve_status}"
                )
            previous_approval_id = current_approval_id
            approval_attempts += 1
        evidence.add(
            "approve_after_bounded_rework",
            terminal_status == "completed" and 1 <= approval_attempts <= 12,
            f"status={terminal_status or '<missing>'}; approvals={approval_attempts}",
        )
        effects_after_rework = _hitl_effect_count(job_id)
        evidence.add(
            "bounded_rework_effects",
            effects_after_rework == 2,
            f"completed_effects={effects_after_rework}",
        )

        cancel_job_id = _task(
            api,
            project_id,
            workflow_id,
            "a23-cancel",
            inputs={"payload": {"marker": "cancel"}},
        )
        cancel_status, _ = api.request(
            "POST",
            f"/api/jobs/{cancel_job_id}/cancel",
            {"reason": "A23 terminal cancel"},
        )
        cancelled = _wait_job_state(api, cancel_job_id, {"cancelled", "failed"}, timeout=90)
        evidence.add(
            "terminal_cancel",
            cancel_status == 200
            and str(cancelled.get("status") or "").strip().lower() == "cancelled"
            and "GeneratorExit" not in str(cancelled.get("error") or ""),
            f"HTTP {cancel_status}; status={cancelled.get('status')}",
        )
    finally:
        _cleanup_hitl_fixture(api, project_id, workflow_id)


def _a24(api: CandidateApi, evidence: Evidence) -> None:
    """Prove the externally reachable TLS MCP boundary with scoped credentials."""
    anonymous_status, _ = _public_mcp_request(
        "",
        {"jsonrpc": "2.0", "id": "a24-anon", "method": "tools/list", "params": {}},
    )
    evidence.add(
        "anonymous_mcp_denied",
        anonymous_status in {401, 403},
        f"HTTP {anonymous_status}",
    )

    workflow_id = ""
    project_id = ""
    foreign_project_id = ""
    service_key = f"release-a24-{uuid.uuid4().hex[:8]}"
    pat_id = ""
    machine_token_id = ""
    pat = ""
    machine_token = ""
    submitted_job_id = ""
    try:
        workflow_id, project_id = _create_cutover_fixture(api)
        foreign_project_id = _project(api, workflow_id)
        project_name = _project_name(api, project_id)
        foreign_project_name = _project_name(api, foreign_project_id)
        pat_status, pat_payload = api.request(
            "POST",
            "/api/auth/tokens",
            {
                "label": f"release-a24-pat-{uuid.uuid4().hex[:8]}",
                "expires_in_days": 1,
                "scopes": ["admin:all"],
                "allowed_project_ids": [project_id],
            },
        )
        pat_row = pat_payload.get("token") if isinstance(pat_payload, dict) else None
        pat_row = pat_row if isinstance(pat_row, dict) else {}
        pat_id = str(pat_payload.get("token_id") or pat_payload.get("id") or "").strip()
        pat = str(pat_row.get("raw_token") or "").strip()

        service_status, _ = api.request(
            "POST",
            "/api/auth/service-accounts",
            {
                "service_account_key": service_key,
                "display_name": "A24 least-privilege reader",
                "scopes": ["projects.read", "jobs.read"],
                "allowed_project_ids": [project_id],
            },
        )
        token_status, token_payload = api.request(
            "POST",
            f"/api/auth/service-accounts/{service_key}/tokens",
            {
                "name": "A24 project reader",
                "expires_in_days": 1,
                "scopes": ["projects.read", "jobs.read"],
                "allowed_project_ids": [project_id],
            },
        )
        machine_row = token_payload.get("token") if isinstance(token_payload, dict) else None
        machine_row = machine_row if isinstance(machine_row, dict) else {}
        machine_token_id = str(
            token_payload.get("token_id") or token_payload.get("id") or ""
        ).strip()
        machine_token = str(machine_row.get("token") or "").strip()
        evidence.add(
            "bounded_credentials_created",
            pat_status in {200, 201}
            and service_status in {200, 201}
            and token_status in {200, 201}
            and bool(pat)
            and bool(machine_token),
            f"pat={pat_status}; service={service_status}; machine={token_status}",
        )
        if not pat or not machine_token:
            return

        initialize = {
            "jsonrpc": "2.0",
            "id": "a24-init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "flow-steward-release-smoke", "version": "1"},
            },
        }
        pat_init_status, pat_init = _public_mcp_request(pat, initialize)
        machine_init_status, machine_init = _public_mcp_request(machine_token, initialize)
        pat_list_status, pat_list = _public_mcp_request(
            pat,
            {"jsonrpc": "2.0", "id": "a24-pat-list", "method": "tools/list", "params": {}},
        )
        machine_list_status, machine_list = _public_mcp_request(
            machine_token,
            {
                "jsonrpc": "2.0",
                "id": "a24-machine-list",
                "method": "tools/list",
                "params": {},
            },
        )
        evidence.add(
            "pat_and_machine_initialize_and_list",
            pat_init_status == machine_init_status == pat_list_status == machine_list_status == 200
            and isinstance(pat_init.get("result"), dict)
            and isinstance(machine_init.get("result"), dict)
            and isinstance(pat_list.get("result"), dict)
            and isinstance(machine_list.get("result"), dict),
            "statuses="
            f"{pat_init_status},{machine_init_status},{pat_list_status},{machine_list_status}",
        )

        read_status, read_response = _mcp_call(
            machine_token,
            "a24-machine-read",
            "project_jobs",
            {"project_name": project_name, "limit": 1},
        )
        read_payload = _mcp_tool_payload(read_response)
        evidence.add(
            "machine_scoped_read",
            read_status == 200 and read_payload.get("ok") is True,
            f"HTTP {read_status}; ok={read_payload.get('ok')}; "
            f"rpc_error={bool(read_response.get('error'))}; {_mcp_response_shape(read_response)}",
        )

        write_status, write_response = _mcp_call(
            pat,
            "a24-pat-write",
            "workflow_trigger",
            {
                "project_name": project_name,
                "workflow_id": workflow_id,
                "test_mode": True,
                "payload": {"message": "main"},
            },
        )
        write_payload = _mcp_tool_payload(write_response)
        write_data = write_payload.get("data") if isinstance(write_payload, dict) else None
        write_error = write_payload.get("error") if isinstance(write_payload, dict) else None
        submitted_job_id = (
            str(write_data.get("job_id") or "") if isinstance(write_data, dict) else ""
        )
        evidence.add(
            "pat_bounded_write",
            write_status == 200
            and write_payload.get("ok") is True
            and isinstance(write_data, dict)
            and bool(write_data.get("job_id")),
            f"HTTP {write_status}; job_id={(write_data or {}).get('job_id') or ''!s}; "
            f"code={(write_error or {}).get('code') or ''!s}; "
            f"message={(write_error or {}).get('message') or ''!s}; "
            f"rpc_error={bool(write_response.get('error'))}; {_mcp_response_shape(write_response)}",
        )

        foreign_status, foreign_response = _mcp_call(
            machine_token,
            "a24-machine-foreign",
            "project_jobs",
            {"project_name": foreign_project_name, "limit": 1},
        )
        foreign_payload = _mcp_tool_payload(foreign_response)
        foreign_error = foreign_payload.get("error") if isinstance(foreign_payload, dict) else None
        foreign_code = str((foreign_error or {}).get("code") or "")
        foreign_message = str((foreign_error or {}).get("message") or "").lower()
        evidence.add(
            "machine_foreign_project_denied",
            foreign_status == 200
            and foreign_payload.get("ok") is False
            and isinstance(foreign_error, dict)
            and foreign_code in {"forbidden", "invalid_argument"}
            and "not in scope" in foreign_message,
            f"HTTP {foreign_status}; code={(foreign_error or {}).get('code') or ''!s}; "
            f"message={(foreign_error or {}).get('message') or ''!s}; "
            f"rpc_error={bool(foreign_response.get('error'))}; "
            f"{_mcp_response_shape(foreign_response)}",
        )
    finally:
        if submitted_job_id:
            _settle_job_for_cleanup(api, submitted_job_id)
        if machine_token_id:
            api.request(
                "POST",
                f"/api/auth/service-accounts/{service_key}/tokens/{machine_token_id}/revoke",
                {},
            )
        api.request("DELETE", f"/api/auth/service-accounts/{service_key}", {})
        if pat_id:
            api.request("POST", f"/api/auth/tokens/{pat_id}/revoke", {})
        for fixture_project_id in (foreign_project_id, project_id):
            if not fixture_project_id:
                continue
            _cleanup_project(api, fixture_project_id)
        if workflow_id:
            api.request("DELETE", f"/api/workflows/{workflow_id}", {})
            api.request("POST", f"/api/trash/registry_workflow/{workflow_id}/hard-delete", {})


def _callback_hmac_headers(
    secret: str,
    payload: dict[str, Any],
    delivery_id: str,
    *,
    timestamp: int | None = None,
    signature_header: str = PROBE_CALLBACK_SIGNATURE_HEADER,
    timestamp_header: str = PROBE_CALLBACK_TIMESTAMP_HEADER,
    delivery_header: str = PROBE_CALLBACK_DELIVERY_HEADER,
) -> dict[str, str]:
    """Sign a delivery the way the receiving extension declared it should be.

    The header names come from the extension's own `callbacks[].auth`, so this
    carries no bundle's spelling of them.
    """
    delivered_at = int(time.time()) if timestamp is None else int(timestamp)
    body = json.dumps(payload).encode()
    canonical = f"{delivered_at}.{delivery_id}.".encode() + body
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return {
        timestamp_header: str(delivered_at),
        delivery_header: delivery_id,
        signature_header: f"sha256={signature}",
    }


def _a25(api: CandidateApi, evidence: Evidence) -> None:
    """Prove the callback lifecycle on a connection the caller owns.

    Registration scoping, redaction in the listing, signature verification,
    expiry, replay rejection and token rotation are the platform's, whichever
    extension receives the delivery — so this runs on the throwaway probe, and
    signs each delivery with the header names that probe itself declares.
    """
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        api.request("POST", f"/api/extensions/{extension_id}/enable", {})
        _a25_callback_lifecycle(api, evidence, extension_id)


def _a25_callback_lifecycle(api: CandidateApi, evidence: Evidence, extension_id: str) -> None:
    workflow_id = _probe_workflow_id(api)
    project_id = _project(api, workflow_id)
    foreign_project_id = _project(api, workflow_id)
    connection_id = ""
    try:
        connection_status, connection = api.request(
            "POST",
            f"/api/projects/{project_id}/connections",
            {
                "extension_id": extension_id,
                "connection_kind": "extension",
                "connection_type": PROBE_CONNECTION_TYPE_ID,
                "display_name": "Probe callback endpoint",
                "runtime_mode": "local",
                "connection_config": {
                    "base_url": "https://api.example.test",
                    "allowed_hosts": ["api.example.test"],
                },
            },
        )
        connection_id = (
            str(connection.get("connection_id") or "") if isinstance(connection, dict) else ""
        )
        missing_status, _ = api.request(
            "POST",
            f"/api/extensions/{extension_id}/callbacks/{PROBE_CALLBACK_ID}",
            {"project_id": project_id},
        )
        foreign_status, _ = api.request(
            "POST",
            f"/api/extensions/{extension_id}/callbacks/{PROBE_CALLBACK_ID}",
            {"project_id": foreign_project_id, "connection_id": connection_id},
        )
        create_status, created = api.request(
            "POST",
            f"/api/extensions/{extension_id}/callbacks/{PROBE_CALLBACK_ID}",
            {"project_id": project_id, "connection_id": connection_id},
        )
        registration_id = str(created.get("id") or "") if isinstance(created, dict) else ""
        token = str(created.get("callback_token") or "") if isinstance(created, dict) else ""
        secret = str(created.get("callback_secret") or "") if isinstance(created, dict) else ""
        callback_path = f"/webhook/extensions/{extension_id}/{PROBE_CALLBACK_ID}/{token}"
        list_status, listed = api.request(
            "GET", f"/api/extensions/{extension_id}/callbacks?project_id={project_id}"
        )
        rows = _rows(listed, "callbacks")
        evidence.add(
            "callback_registration_contract",
            connection_status == 200
            and bool(connection_id)
            and missing_status == 400
            and foreign_status == 400
            and create_status == 200
            and bool(registration_id and token and secret)
            and list_status == 200
            and len(rows) == 1
            and "<redacted>" in str(rows[0].get("callback_url") or "")
            and not any(key in rows[0] for key in ("callback_token", "callback_secret")),
            f"connection={connection_status}; missing={missing_status}; foreign={foreign_status}; "
            f"create={create_status}; list={list_status}; rows={len(rows)}",
        )
        invalid_payload = {"source": "A25-invalid", "event_type": "external.comment.received"}
        invalid_headers = _callback_hmac_headers("invalid", invalid_payload, "a25-invalid")
        invalid_status, invalid_result = api.request(
            "POST",
            callback_path,
            invalid_payload,
            csrf=False,
            extra_headers=invalid_headers,
        )
        expired_payload = {"source": "A25-expired", "event_type": "external.comment.received"}
        expired_status, expired_result = api.request(
            "POST",
            callback_path,
            expired_payload,
            csrf=False,
            extra_headers=_callback_hmac_headers(
                secret,
                expired_payload,
                "a25-expired",
                timestamp=int(time.time()) - 301,
            ),
        )
        delivery_payload = {
            "source": "A25",
            "event_type": "external.comment.received",
            "id": "a25-event",
        }
        delivery_headers = _callback_hmac_headers(secret, delivery_payload, "a25-delivery")
        delivery_status, delivery = api.request(
            "POST",
            callback_path,
            delivery_payload,
            csrf=False,
            extra_headers=delivery_headers,
        )
        replay_status, replay_result = api.request(
            "POST",
            callback_path,
            delivery_payload,
            csrf=False,
            extra_headers=delivery_headers,
        )
        status_status, provider_status = api.request(
            "POST",
            f"/api/extensions/{extension_id}/callbacks/{registration_id}/status",
            {"action_id": PROBE_INGRESS_ACTION_ID, "runtime": {"source": "probe-status"}},
        )
        rotate_status, rotated = api.request(
            "POST",
            f"/api/extensions/{extension_id}/callbacks/{registration_id}/rotate",
            {},
        )
        rotated_token = (
            str(rotated.get("callback_token") or "") if isinstance(rotated, dict) else ""
        )
        old_status, _ = api.request(
            "POST",
            callback_path,
            {},
            csrf=False,
            extra_headers=_callback_hmac_headers(secret, {}, "a25-old-token"),
        )
        rotated_payload = {"source": "A25-rotated", "event_type": "external.comment.received"}
        new_status, new_result = api.request(
            "POST",
            f"/webhook/extensions/{extension_id}/{PROBE_CALLBACK_ID}/{rotated_token}",
            rotated_payload,
            csrf=False,
            extra_headers=_callback_hmac_headers(secret, rotated_payload, "a25-rotated"),
        )
        evidence.add(
            "callback_authenticated_lifecycle",
            invalid_status == 401
            and expired_status == 401
            and delivery_status == 200
            and bool((delivery or {}).get("ok"))
            and replay_status == 409
            and status_status == 200
            and bool((provider_status or {}).get("ok"))
            and rotate_status == 200
            and bool(rotated_token)
            and rotated_token != token
            and old_status == 404
            and new_status == 200,
            f"invalid={invalid_status}:{_response_error(invalid_result)}; "
            f"expired={expired_status}:{_response_error(expired_result)}; "
            f"delivery={delivery_status}:{_response_error(delivery)}; "
            f"replay={replay_status}:{_response_error(replay_result)}; "
            f"status={status_status}; rotate={rotate_status}; old={old_status}; "
            f"new={new_status}:{_response_error(new_result)}",
        )
    finally:
        if connection_id:
            api.request("DELETE", f"/api/projects/{project_id}/connections/{connection_id}")
        _cleanup_project(api, foreign_project_id)
        _cleanup_project(api, project_id)


def _a26_agent(api: CandidateApi) -> tuple[int, str]:
    """A disposable agent to scope the guardrail to: a fresh install ships none."""
    agent_id = f"a26_scope_{uuid.uuid4().hex[:10]}"
    status, payload = api.request(
        "POST",
        "/api/agents",
        {
            "agent_id": agent_id,
            "display_name": "A26 scope fixture",
            "description": "Disposable candidate-owned guardrail scope fixture.",
            "category": "other",
            "enabled": False,
            "prompts": {"system": "Release fixture; never run."},
        },
    )
    created = str(payload.get("agent_id") or "") if isinstance(payload, dict) else ""
    return status, created


def _a26(api: CandidateApi, evidence: Evidence) -> None:
    """PATCH Config fields and prove the explicit agent scope survives reload.

    Every fixture is its own: a fresh install ships no extension, so it has no
    workflow and no agent to borrow.
    """
    workflow_id = _create_hitl_workflow(api)
    project_id = _project(api, workflow_id)
    guardrail_id = ""
    agent_id = ""
    try:
        agents_status, agent_id = _a26_agent(api)
        create_status, created = api.request(
            "POST",
            "/api/guardrails",
            {
                "display_name": "A26 guardrail",
                "description": "before",
                "enabled": True,
                "mode": "deterministic",
                "rule_type": "immutable_paths",
                "severity": "error",
                "config": {"paths": ["README.md"]},
                "prompt_text": "before prompt",
                "agent_prompt": "before guidance",
                "project_id": project_id,
                "agents": [agent_id],
            },
        )
        guardrail_id = str(created.get("id") or "") if isinstance(created, dict) else ""
        patch_status, _ = api.request(
            "PATCH",
            f"/api/guardrails/{guardrail_id}",
            {
                "display_name": "A26 guardrail updated",
                "description": "after",
                "mode": "semantic",
                "rule_type": "immutable_paths",
                "severity": "warning",
                "config": {"paths": ["docs/standards"]},
                "prompt_text": "after prompt",
                "agent_prompt": "after guidance",
            },
        )
        detail_status, detail = api.request("GET", f"/api/guardrails/{guardrail_id}")
        evidence.add(
            "guardrail_config_scope_persistence",
            agents_status in {200, 201}
            and bool(agent_id)
            and create_status == 201
            and bool(guardrail_id)
            and patch_status == 200
            and detail_status == 200
            and detail.get("display_name") == "A26 guardrail updated"
            and detail.get("description") == "after"
            and detail.get("mode") == "semantic"
            and detail.get("severity") == "warning"
            and detail.get("config") == {"paths": ["docs/standards"]}
            and detail.get("prompt_text") == "after prompt"
            and detail.get("agent_prompt") == "after guidance"
            and detail.get("_agents") == [agent_id],
            f"agents={agents_status}; create={create_status}; patch={patch_status}; "
            f"reload={detail_status}; scope={detail.get('_agents') if isinstance(detail, dict) else None}",
        )
    finally:
        if guardrail_id:
            api.request("DELETE", f"/api/guardrails/{guardrail_id}")
        if agent_id:
            api.request("DELETE", f"/api/agents/{agent_id}")
            api.request("POST", f"/api/trash/registry_agent/{agent_id}/hard-delete", {})
        _cleanup_hitl_fixture(api, project_id, workflow_id)


def _populated_project_counts(project_id: str) -> dict[str, int]:
    project = _sql_literal(project_id)
    raw = _psql(
        "select json_build_object("
        f"'project',(select count(*) from projects where id={project}::uuid),"
        f"'workflow',(select count(*) from project_workflows where project_id={project}::uuid),"
        f"'dataset',(select count(*) from project_datasets where project_id={project}::uuid),"
        f"'connection',(select count(*) from project_connections where project_id={project}::uuid),"
        f"'extension_state',(select count(*) from extension_project_state where project_id={project}::uuid),"
        f"'job',(select count(*) from jobs where project_id={project}::uuid)"
        ")::text"
    )
    return {str(key): int(value) for key, value in json.loads(raw or "{}").items()}


def _seed_populated_project_state(*, project_id: str, connection_id: str) -> str:
    project = _sql_literal(project_id)
    connection = _sql_literal(connection_id)
    dataset_key = f"release-smoke-a27-{uuid.uuid4().hex}"
    dataset = _sql_literal(dataset_key)
    _psql(
        "insert into registry_datasets "
        "(dataset_key,workspace_id,display_name,availability_mode) "
        f"select {dataset},p.workspace_id,'A27 project dataset','specific_projects_only' "
        f"from projects p where p.id={project}::uuid on conflict do nothing;"
        "insert into project_datasets (dataset_id,project_id) "
        f"values ({dataset},{project}::uuid) on conflict do nothing;"
        "insert into extension_project_state "
        "(account_id,project_id,extension_id,extension_install_id,connection_id,"
        "namespace,state_key,value_jsonb,created_by) "
        f"select p.account_id,p.id,pc.extension_id,ei.install_id,"
        f"pc.connection_id,'release-smoke','a27-populated','{{\"present\":true}}'::jsonb,"
        f"'release-smoke' from projects p join project_connections pc on pc.project_id=p.id "
        "join lateral (select install_id from extension_installs "
        "where account_id=p.account_id and extension_id=pc.extension_id and state='active' "
        "order by installed_at desc limit 1) ei on true "
        f"where p.id={project}::uuid and pc.connection_id={connection} on conflict do nothing;"
    )
    return dataset_key


def _a27(api: CandidateApi, evidence: Evidence) -> None:
    """Own populated-project restore, stale-plan denial and clean hard-delete retry.

    The trash lifecycle is the platform's whichever extension populated the
    project, so the population is built on the throwaway probe: its connection,
    a completed run of its workflow, and seeded project state.

    The artifact half of the old fixture did not move here. That a produced
    artifact's bytes match its metadata is a bundle behaviour, and the Example
    Showcase release suite asserts it as `showcase_catalog_artifact_bytes`.
    """
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        api.request("POST", f"/api/extensions/{extension_id}/enable", {})
        _a27_populated_project_fixture(api, evidence, extension_id)


def _a27_populated_project_fixture(
    api: CandidateApi, evidence: Evidence, extension_id: str
) -> None:
    """Prove populated-project restore, stale-plan denial, and clean hard-delete retry."""
    workflow_id = _probe_workflow_id(api)
    project_id = _project(api, workflow_id)
    connection_id = ""
    dataset_key = ""
    try:
        connection_status, connection = api.request(
            "POST",
            f"/api/projects/{project_id}/connections",
            {
                "extension_id": extension_id,
                "connection_kind": "extension",
                "connection_type": PROBE_CONNECTION_TYPE_ID,
                "display_name": "Populated project connection",
                "runtime_mode": "local",
                "connection_config": {"base_url": PROBE_PRIVATE_BASE_URL},
            },
        )
        connection_id = (
            str(connection.get("connection_id") or "") if isinstance(connection, dict) else ""
        )
        job_id = _task(
            api,
            project_id,
            workflow_id,
            "populated-delete",
            inputs={"payload": {"marker": "populated"}},
        )
        job_status, _ = _job_detail(api, job_id)
        dataset_key = _seed_populated_project_state(
            project_id=project_id,
            connection_id=connection_id,
        )
        populated = _populated_project_counts(project_id)
        evidence.add(
            "populated_project_fixture",
            connection_status == 200
            and bool(connection_id)
            and job_status == "completed"
            and all(populated.get(key, 0) >= 1 for key in populated),
            f"connection={connection_status}; job={job_status}; counts={populated}",
        )

        trash_status, _ = api.request("DELETE", f"/api/projects/{project_id}", {})
        restore_plan_status, restore_plan = api.request(
            "POST",
            f"/api/trash/project/{project_id}/plan",
            {"action_type": "restore"},
        )
        restore_version = str(restore_plan.get("plan_version") or "")
        restore_status, restored = api.request(
            "POST",
            f"/api/trash/project/{project_id}/execute",
            {"action_type": "restore", "plan_version": restore_version},
        )
        restored_counts = _populated_project_counts(project_id)
        evidence.add(
            "populated_project_restore",
            trash_status == 200
            and restore_plan_status == 200
            and bool(restore_version)
            and restore_status == 200
            and str((restored or {}).get("status") or "") == "restored"
            and restored_counts == populated,
            f"trash={trash_status}; plan={restore_plan_status}; execute={restore_status}; "
            f"counts_equal={restored_counts == populated}",
        )

        api.request("DELETE", f"/api/projects/{project_id}", {})
        hard_plan_status, hard_plan = api.request(
            "POST",
            f"/api/trash/project/{project_id}/plan",
            {"action_type": "hard_delete"},
        )
        hard_version = str(hard_plan.get("plan_version") or "")
        added_connection_id = f"a27-stale-{uuid.uuid4().hex}"
        _psql(
            "insert into project_connections "
            "(connection_id,project_id,account_id,display_name) select "
            f"{_sql_literal(added_connection_id)},{_sql_literal(project_id)}::uuid,account_id,"
            f"'A27 stale dependency' from projects where id={_sql_literal(project_id)}::uuid"
        )
        before_stale = _populated_project_counts(project_id)
        stale_status, stale = api.request(
            "POST",
            f"/api/trash/project/{project_id}/execute",
            {"action_type": "hard_delete", "plan_version": hard_version},
        )
        after_stale = _populated_project_counts(project_id)
        evidence.add(
            "populated_project_stale_plan_atomic",
            hard_plan_status == 200
            and bool(hard_version)
            and stale_status in {400, 409}
            and "stale" in json.dumps(stale).lower()
            and after_stale == before_stale,
            f"plan={hard_plan_status}; stale={stale_status}; unchanged={after_stale == before_stale}",
        )

        retry_plan_status, retry_plan = api.request(
            "POST",
            f"/api/trash/project/{project_id}/plan",
            {"action_type": "hard_delete"},
        )
        retry_status, deleted = api.request(
            "POST",
            f"/api/trash/project/{project_id}/execute",
            {
                "action_type": "hard_delete",
                "plan_version": str(retry_plan.get("plan_version") or ""),
            },
        )
        remaining = _populated_project_counts(project_id)
        # That the project's artifacts go with it is asserted where an artifact
        # actually exists: the showcase extension suite's
        # `showcase_artifact_removed_with_project`. The probe's workflow produces
        # none, and seeding one into the object store from here would assert the
        # seed rather than the cascade.
        evidence.add(
            "populated_project_clean_retry",
            retry_plan_status == 200
            and retry_status == 200
            and str((deleted or {}).get("status") or "") == "hard_deleted"
            and all(value == 0 for value in remaining.values()),
            f"plan={retry_plan_status}; execute={retry_status}; remaining={remaining}",
        )
    finally:
        if _populated_project_counts(project_id).get("project", 0):
            _cleanup_project(api, project_id)
        else:
            _assert_product_removed_project_queues(project_id)
        if dataset_key:
            _psql(f"delete from registry_datasets where dataset_key={_sql_literal(dataset_key)}")


def _extension_projection_counts(extension_id: str, *, active_only: bool) -> dict[str, int]:
    """How many projection rows an extension owns, by artifact kind.

    Named by the caller rather than fixed to a bundle: what the platform owns is
    that projections follow the install's enabled state, whichever extension the
    row belongs to.
    """
    active_clause = "and is_active" if active_only else ""
    quoted = extension_id.replace("'", "''")
    raw = _psql(
        "select coalesce(json_object_agg(artifact_kind, artifact_count), '{}'::json)::text "
        "from (select artifact_kind, count(*)::int artifact_count "
        "from extension_registry_projections "
        f"where extension_id='{quoted}' "
        f"{active_clause} group by artifact_kind order by artifact_kind) counts"
    )
    payload = json.loads(raw or "{}")
    return {str(key): int(value) for key, value in payload.items()}


def _extension_qualified_artifact_ids(extension_id: str) -> dict[str, list[str]]:
    """The catalog ids an extension's projections carry, by artifact kind."""
    quoted = extension_id.replace("'", "''")
    raw = _psql(
        "select coalesce(json_object_agg(artifact_kind, artifact_ids), '{}'::json)::text "
        "from (select artifact_kind, json_agg('extension.'||install_id||'.'||artifact_id "
        "order by artifact_id) artifact_ids from extension_registry_projections "
        f"where extension_id='{quoted}' group by artifact_kind) rows"
    )
    payload = json.loads(raw or "{}")
    return {
        str(kind): [str(value) for value in values or []]
        for kind, values in payload.items()
        if isinstance(values, list)
    }


def _payload_contains_exact(payload: Any, expected: str) -> bool:
    if isinstance(payload, dict):
        return any(_payload_contains_exact(value, expected) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_exact(value, expected) for value in payload)
    return str(payload or "") == expected


def _extension_public_catalog_state(
    api: CandidateApi,
    artifact_ids: dict[str, list[str]],
    *,
    active: bool,
) -> tuple[bool, str]:
    plurals = {
        "agent": "agents",
        "skill": "skills",
        "tool": "tools",
        "workflow": "workflows",
        "dataset": "datasets",
    }
    failures: list[str] = []
    for kind, plural in plurals.items():
        for artifact_id in artifact_ids.get(kind) or []:
            encoded = quote(artifact_id, safe="")
            list_status, listing = api.request(
                "GET", f"/api/{plural}?search={encoded}&per_page=500"
            )
            detail_status, detail = api.request("GET", f"/api/{plural}/{encoded}")
            listed = _payload_contains_exact(listing, artifact_id)
            detailed = detail_status == 200 and _payload_contains_exact(detail, artifact_id)
            if active:
                if list_status != 200 or not listed or not detailed:
                    failures.append(
                        f"{kind}:{artifact_id}:list={list_status}/{listed}:detail={detail_status}/{detailed}"
                    )
            elif listed or detail_status == 200:
                failures.append(
                    f"{kind}:{artifact_id}:disabled-list={list_status}/{listed}:detail={detail_status}"
                )
    saved_query_ids = artifact_ids.get("saved_query") or []
    query_status, query_payload = (
        api.request("GET", "/api/datasets/query-templates") if saved_query_ids else (200, {})
    )
    query_rows = _rows(query_payload, "query_templates")
    for artifact_id in saved_query_ids:
        row = next(
            (
                item
                for item in query_rows
                if str(item.get("query_template_id") or "").strip() == artifact_id
            ),
            None,
        )
        if active:
            dataset_id = str((row or {}).get("dataset_id") or "").strip()
            dataset_status, _ = (
                api.request("GET", f"/api/datasets/{quote(dataset_id, safe='')}")
                if dataset_id
                else (0, {})
            )
            if query_status != 200 or row is None or dataset_status != 200:
                failures.append(
                    f"saved_query:{artifact_id}:list={query_status}/{row is not None}:"
                    f"dataset={dataset_status}"
                )
        elif row is not None:
            failures.append(f"saved_query:{artifact_id}:disabled-list={query_status}/true")
    return not failures, "; ".join(failures[:8]) or "all exact artifacts matched"


def _saved_query_catalog_integrity(api: CandidateApi) -> tuple[bool, str]:
    status, payload = api.request("GET", "/api/datasets/query-templates")
    failures: list[str] = []
    rows = _rows(payload, "query_templates")
    for row in rows:
        query_id = str(row.get("query_template_id") or "").strip()
        dataset_id = str(row.get("dataset_id") or "").strip()
        dataset_status, _ = (
            api.request("GET", f"/api/datasets/{quote(dataset_id, safe='')}")
            if dataset_id
            else (0, {})
        )
        if not query_id or dataset_status != 200:
            failures.append(f"{query_id or '<missing>'}:dataset={dataset_id}/{dataset_status}")
    return status == 200 and not failures, "; ".join(failures[:8]) or f"{len(rows)} valid templates"


def _job_detail(api: CandidateApi, job_id: str) -> tuple[str, dict[str, Any]]:
    status = _wait_job_terminal(api, job_id, timeout=300)
    response_status, payload = api.request("GET", f"/api/jobs/{job_id}")
    detail = dict(payload) if response_status == 200 and isinstance(payload, dict) else {}
    return status, detail


def _workflow_output(detail: dict[str, Any], name: str) -> Any:
    candidates = (
        ("workflow_run", "result", "outputs", name),
        ("workflow_run", "result", "artifacts", name),
        ("thread_state", "workflow_outputs", "outputs", name),
        ("thread_state", "outputs", name),
        ("state", name),
    )
    for path in candidates:
        value: Any = detail
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _download_candidate_artifact(artifact: dict[str, Any]) -> bytes:
    container = _required("FS_SMOKE_APP_CONTAINER")
    script = """
import json
import sys
import urllib.request

descriptor = json.loads(sys.stdin.read() or "{}")
url = str(descriptor.get("download_url") or "").strip()
if not url:
    artifact_id = str(descriptor.get("artifact_id") or "").strip()
    if not artifact_id:
        raise SystemExit(2)
    from core.infrastructure.runtime.http_app_request_helpers import _artifact_plane

    url = str(_artifact_plane.artifact_presigned_url(artifact_id, method="GET") or "").strip()
if not url:
    raise SystemExit(3)
sys.stdout.buffer.write(urllib.request.urlopen(url, timeout=30).read())
"""
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-c", script],
        input=json.dumps(dict(artifact or {})).encode(),
        capture_output=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("candidate artifact download failed")
    return bytes(result.stdout)


def _schema_fixture_value(schema: dict[str, Any]) -> Any:
    for key in ("const", "default", "example"):
        if key in schema:
            return schema[key]
    for key in ("examples", "enum"):
        values = schema.get(key)
        if isinstance(values, list) and values:
            return values[0]
    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict) and variant.get("type") != "null":
                    return _schema_fixture_value(variant)
    return {
        "array": [],
        "boolean": True,
        "integer": 1,
        "number": 1.0,
        "object": {},
        "string": "candidate-smoke",
    }.get(str(schema.get("type") or "string"), "candidate-smoke")


def _workflow_required_inputs(api: CandidateApi, workflow_id: str) -> dict[str, Any]:
    status, payload = api.request("GET", f"/api/workflows/{workflow_id}/contract")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"Workflow contract lookup failed with HTTP {status}: {payload!r}")
    interface = payload.get("interface") if isinstance(payload.get("interface"), dict) else {}
    schema = (
        interface.get("input_schema") if isinstance(interface.get("input_schema"), dict) else {}
    )
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    result: dict[str, Any] = {}
    for raw_name in required:
        name = str(raw_name or "").strip()
        definition = properties.get(name)
        if not name or not isinstance(definition, dict):
            raise RuntimeError(f"Workflow required input schema is incomplete: {name or '<empty>'}")
        result[name] = _schema_fixture_value(definition)
    return result


def _candidate_artifact_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    container = _required("FS_SMOKE_APP_CONTAINER")
    script = """
import json
import sys
from core.infrastructure.runtime.http_app_request_helpers import _artifact_plane

descriptor = json.loads(sys.stdin.read() or "{}")
artifact_id = str(descriptor.get("artifact_id") or "").strip()
if not artifact_id:
    raise SystemExit(2)
metadata = _artifact_plane.get_artifact(artifact_id)
if not isinstance(metadata, dict):
    raise SystemExit(3)
print(json.dumps(metadata, sort_keys=True))
"""
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-c", script],
        input=json.dumps(dict(artifact or {})).encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("candidate artifact metadata lookup failed")
    try:
        payload = json.loads(result.stdout.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("candidate artifact metadata was not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("candidate artifact metadata was not an object")
    return payload


def _global_search_family_registry(api: CandidateApi, evidence: Evidence) -> None:
    marker = f"rs5-085-guaranteed-miss-{uuid.uuid4().hex}"
    unsupported_status, unsupported = api.request(
        "GET",
        f"/api/search?query={marker}&category=not_a_release_family&limit=1",
    )
    unsupported_error = (
        str(unsupported.get("error") or "").strip().lower() if isinstance(unsupported, dict) else ""
    )
    declared_categories = (
        unsupported.get("supported_categories") if isinstance(unsupported, dict) else []
    )
    expected = tuple(
        category
        for value in (declared_categories or [])
        if (category := str(value or "").strip()) and category != "all"
    )
    failures: list[str] = []
    for category in ("all", *expected):
        status, payload = api.request(
            "GET",
            f"/api/search?query={marker}&category={category}&limit=1",
        )
        rows = payload.get("groups") if isinstance(payload, dict) else None
        groups = tuple(
            str(row.get("group") or "").strip() for row in (rows or []) if isinstance(row, dict)
        )
        expected_groups = expected if category == "all" else (category,)
        has_results = any(bool(row.get("items")) for row in (rows or []) if isinstance(row, dict))
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("category") != category
            or groups != expected_groups
            or has_results
        ):
            failures.append(f"{category}:http={status}:groups={groups}:results={has_results}")

    evidence.add(
        "global_search_family_registry",
        bool(expected)
        and len(expected) == len(set(expected))
        and not failures
        and unsupported_status == 400
        and unsupported_error == "unsupported search category",
        f"families={len(expected)}; failures={failures}; "
        f"unsupported_http={unsupported_status}; unsupported_error={unsupported_error or '<missing>'}",
    )


def _d09(api: CandidateApi, evidence: Evidence) -> None:
    """Own extension projection lifecycle, catalog state, storage integrity and cleanup.

    All of it on a throwaway `acme.*` probe, through identifiers the API hands
    back. What the platform owns here is that an install's projections and its
    public catalog rows follow its enabled state, that a rejected write leaves
    nothing behind, and that an uninstall takes the active rows with it — none of
    which is a statement about any particular bundle.

    A platform check may read the platform's own database. The matrix of a
    specific bundle's workflows may not live here; it belongs to that bundle's
    own release suite, which reaches the candidate over the public API only.
    """
    _global_search_family_registry(api, evidence)
    extension_id = ""
    expected_counts: dict[str, int] = {}
    qualified: dict[str, list[str]] = {}
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        expected_counts = _extension_projection_counts(extension_id, active_only=False)
        qualified = _extension_qualified_artifact_ids(extension_id)
        _d09_projection_lifecycle(api, evidence, extension_id, expected_counts, qualified)
        _d09_availability_contract(api, evidence, extension_id, qualified)
        _d09_storage_integrity(api, evidence)

    # The probe is uninstalled by here, which is what makes the next assertion
    # about cleanup rather than about a still-installed extension.
    active_counts = _extension_projection_counts(extension_id, active_only=True)
    durable_counts = _extension_projection_counts(extension_id, active_only=False)
    cleanup_catalog_ok, cleanup_catalog_detail = _extension_public_catalog_state(
        api, qualified, active=False
    )
    cleanup_query_ok, cleanup_query_detail = _saved_query_catalog_integrity(api)
    detail_status, _ = api.request("GET", f"/api/extensions/{extension_id}")
    evidence.add(
        "extension_public_cleanup",
        bool(extension_id)
        and detail_status == 404
        and active_counts == {}
        # A third-party install is removed, not just switched off, so its rows go
        # with it — unlike a system extension, which is disabled in place.
        and durable_counts == {}
        and bool(expected_counts)
        and cleanup_catalog_ok
        and cleanup_query_ok,
        f"extension={extension_id or '<missing>'}; detail={detail_status}; "
        f"active={active_counts}; durable={durable_counts}; catalog={cleanup_catalog_detail}; "
        f"query_integrity={cleanup_query_detail}",
    )


def _d09_projection_lifecycle(
    api: CandidateApi,
    evidence: Evidence,
    extension_id: str,
    expected_counts: dict[str, int],
    qualified: dict[str, list[str]],
) -> None:
    """Projections and public catalog rows follow the install's enabled state."""
    disable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/disable", {})
    disabled_counts = _extension_projection_counts(extension_id, active_only=True)
    disabled_durable = _extension_projection_counts(extension_id, active_only=False)
    disabled_catalog_ok, disabled_catalog_detail = _extension_public_catalog_state(
        api, qualified, active=False
    )
    enable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/enable", {})
    enabled_counts = _extension_projection_counts(extension_id, active_only=True)
    enabled_catalog_ok, enabled_catalog_detail = _extension_public_catalog_state(
        api, qualified, active=True
    )
    # Enabling twice must not duplicate or drop rows: the second call is the one
    # that catches a projection rebuilt by appending.
    reenable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/enable", {})
    reenabled_counts = _extension_projection_counts(extension_id, active_only=True)
    evidence.add(
        "extension_projection_lifecycle",
        bool(expected_counts)
        and disable_status == 200
        and enable_status == 200
        and reenable_status == 200
        and disabled_counts == {}
        # Disabling hides the rows; it does not destroy them.
        and disabled_durable == expected_counts
        and enabled_counts == expected_counts
        and reenabled_counts == expected_counts,
        f"extension={extension_id}; installed={expected_counts}; disabled={disabled_counts}; "
        f"disabled_durable={disabled_durable}; enabled={enabled_counts}; "
        f"reenabled={reenabled_counts}",
    )
    evidence.add(
        "extension_public_catalog_lifecycle",
        bool(qualified) and disabled_catalog_ok and enabled_catalog_ok,
        f"disabled={disabled_catalog_detail}; enabled={enabled_catalog_detail}",
    )


def _d09_availability_contract(
    api: CandidateApi,
    evidence: Evidence,
    extension_id: str,
    qualified: dict[str, list[str]],
) -> None:
    """A disabled extension's workflow is unavailable everywhere, and unrunnable."""
    workflow_id = str((qualified.get("workflow") or [""])[0])
    if not workflow_id:
        evidence.add(
            "workflow_effective_availability_contract",
            False,
            f"probe {extension_id} published no workflow projection to assert on",
        )
        return
    project_id = ""
    stale_project_id = ""
    job_ids: list[str] = []
    try:
        create_status, created = api.request(
            "POST",
            "/api/projects",
            {
                "name": f"probe-availability-{uuid.uuid4().hex[:10]}",
                "workspace_id": _workspace(api),
                "workflow_ids": [workflow_id],
            },
        )
        project_id = str(created.get("project_id") or "") if isinstance(created, dict) else ""
        global_path = f"/api/workflows/{workflow_id}/contract"
        project_path = f"/api/projects/{project_id}/workflows/{workflow_id}/contract"
        validate_path = f"/api/projects/{project_id}/workflows/{workflow_id}/validate"
        enabled_global_status, enabled_global = api.request("GET", global_path)
        enabled_project_status, enabled_project = api.request("GET", project_path)
        disable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/disable", {})
        # Binding a disabled extension's workflow must not succeed. If the
        # platform ever lets it, the project it made is still cleaned up here
        # rather than left behind for the next check to trip over.
        stale_status, stale_payload = api.request(
            "POST",
            "/api/projects",
            {
                "name": f"probe-disabled-{uuid.uuid4().hex[:8]}",
                "workspace_id": _workspace(api),
                "workflow_ids": [workflow_id],
            },
        )
        stale_project_id = (
            str(stale_payload.get("project_id") or "") if isinstance(stale_payload, dict) else ""
        )
        disabled_global_status, _ = api.request("GET", global_path)
        disabled_project_status, _ = api.request("GET", project_path)
        disabled_validate_status, _ = api.request("GET", validate_path)
        submit_status, submitted = api.request(
            "POST",
            "/api/workflow-runs",
            {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "inputs": {},
            },
        )
        enable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/enable", {})
        restored_global_status, restored_global = api.request("GET", global_path)
        restored_project_status, restored_project = api.request("GET", project_path)
        restored_validate_status, restored_validate = api.request("GET", validate_path)
        evidence.add(
            "workflow_effective_availability_contract",
            create_status == 201
            and stale_status not in {200, 201}
            and enabled_global_status == 200
            and enabled_project_status == 200
            # The project view of a contract must be the global one, or a
            # workflow means different things depending on where it is read.
            and enabled_global == enabled_project
            and disable_status == 200
            and disabled_global_status == 404
            and disabled_project_status == 404
            and disabled_validate_status == 404
            and submit_status in {400, 404, 409, 422}
            and not str((submitted or {}).get("job_id") or "").strip()
            and enable_status == 200
            and restored_global_status == 200
            and restored_project_status == 200
            and restored_validate_status == 200
            and bool((restored_validate or {}).get("validation", {}).get("valid"))
            and restored_global == restored_project,
            f"create={create_status}; disabled_bind={stale_status}; "
            f"enabled={enabled_global_status}/{enabled_project_status}; "
            f"disabled={disabled_global_status}/{disabled_project_status}/"
            f"{disabled_validate_status}/{submit_status}; "
            f"restored={restored_global_status}/{restored_project_status}/"
            f"{restored_validate_status}",
        )
        if project_id:
            job_id = _task(api, project_id, workflow_id, "probe-step", inputs={})
            job_ids.append(job_id)
            _job_detail(api, job_id)
            _, detail = api.request("GET", f"/api/jobs/{job_id}")
            detail = detail if isinstance(detail, dict) else {}
            _d09_job_step_evidence(evidence, job_id, detail)
    finally:
        for job_id in job_ids:
            api.request("POST", f"/api/jobs/{job_id}/cancel", {"reason": "D09 probe cleanup"})
        if stale_project_id:
            _cleanup_project(api, stale_project_id)
        if project_id:
            _cleanup_project(api, project_id)


def _d09_job_step_evidence(evidence: Evidence, job_id: str, detail: dict[str, Any]) -> None:
    """One current step, reported identically wherever a job detail surfaces it."""
    step_code = str(detail.get("current_step_id") or "").strip()
    nested_step = str((detail.get("job") or {}).get("current_step_id") or "").strip()
    lifecycle = detail.get("lifecycle") or {}
    lifecycle_step = lifecycle.get("step") if isinstance(lifecycle, dict) else {}
    lifecycle_step = lifecycle_step if isinstance(lifecycle_step, dict) else {}
    lifecycle_code = str(lifecycle_step.get("code") or "").strip()
    lifecycle_label = str(lifecycle_step.get("label") or "").strip()
    step_label = str(detail.get("step_label") or "").strip()
    hero = detail.get("hero_summary") or {}
    hero_facts = hero.get("facts") if isinstance(hero, dict) else []
    hero_step = next(
        (
            str(fact.get("value") or "").strip()
            for fact in hero_facts or []
            if isinstance(fact, dict)
            and str(fact.get("label") or "").strip().lower() == "current step"
        ),
        "",
    )
    evidence.add(
        "job_detail_canonical_current_step",
        bool(step_code)
        and step_code == nested_step == lifecycle_code
        and bool(lifecycle_label)
        and lifecycle_label == step_label == hero_step
        and hero_step != "Not recorded",
        f"job={job_id}; top={step_code}; nested={nested_step}; "
        f"lifecycle={lifecycle_code}/{lifecycle_label}; label={step_label}; hero={hero_step}",
    )


def _d09_storage_integrity(api: CandidateApi, evidence: Evidence) -> None:
    """A rejected authoring write must leave nothing behind to be read later.

    Saved-query storage lives here rather than with a connector bundle, even
    though a dataset needs one: `/api/datasets` is admin-only, so the assertion
    is unreachable with the narrow scopes a release suite is given, and the
    trusted side is the only place it can be made. Whichever connector the
    catalog offers is used — none is named.
    """
    typo_workflow_id = f"probe_override_typo_{uuid.uuid4().hex[:8]}"
    typo_status, _ = _draft_workflow(
        api,
        {
            "workflow_id": typo_workflow_id,
            "display_name": "Override envelope rejection",
            "description": "Disposable invalid authoring fixture.",
        },
        _linear_operations(
            {
                "step_id": "typo",
                "display_name": "Typo",
                "step_kind": "stop_success",
                "override": {
                    "stop_success": {"reason": "ok"},
                    # The envelope key is misspelled; accepting it
                    # silently disables the guard it belongs to.
                    "require_runtime_tool": True,
                },
            }
        ),
    )
    typo_read_status, typo_read = api.request("GET", f"/api/workflows/{typo_workflow_id}")
    stored_steps = (typo_read or {}).get("steps") if isinstance(typo_read, dict) else None
    evidence.add(
        "workflow_override_envelope_rejected_before_persistence",
        typo_status in {400, 422} and typo_read_status == 200 and not stored_steps,
        f"changes={typo_status}; read_back={typo_read_status}; stored_steps={len(stored_steps or [])}",
    )
    api.request("DELETE", f"/api/workflows/{typo_workflow_id}")
    api.request("POST", f"/api/trash/registry_workflow/{typo_workflow_id}/hard-delete", {})

    _d09_saved_query_integrity(api, evidence)


def _d09_saved_query_integrity(api: CandidateApi, evidence: Evidence) -> None:
    """A saved query written under the wrong field must be refused, not stored.

    `statement` is not the field a saved query is read from, so a row accepted
    under it would sit in the catalog and never execute — the failure would
    surface at run time, on somebody's workflow, long after the write.
    """
    connector_status, connectors = api.request("GET", "/api/datasets/connectors")
    connector_id = next(
        (
            str(row.get("connector_id") or "")
            for row in _rows(connectors, "connectors")
            if str(row.get("connector_id") or "").strip()
        ),
        "",
    )
    if connector_status != 200 or not connector_id:
        evidence.add(
            "saved_query_statement_rejected_before_write",
            False,
            f"no connector to build a dataset on: HTTP {connector_status}",
        )
        return

    dataset_id = f"probe-dataset-{uuid.uuid4().hex[:10]}"
    query_id = f"probe-statement-{uuid.uuid4().hex[:10]}"
    dataset_status = 0
    try:
        dataset_status, dataset_payload = api.request(
            "POST",
            "/api/datasets",
            {
                "dataset_id": dataset_id,
                "workspace_id": _workspace(api),
                "connector_id": connector_id,
                "display_name": "Storage integrity probe",
                "resource_scope_jsonb": {"resources": [{"resource_id": "public.probe"}]},
            },
        )
        if dataset_status not in {200, 201}:
            evidence.add(
                "saved_query_statement_rejected_before_write",
                False,
                f"dataset create={dataset_status}: {str(dataset_payload)[:160]}",
            )
            return
        malformed_status, malformed = api.request(
            "POST",
            "/api/datasets/query-templates",
            {
                "query_template_id": query_id,
                "dataset_id": dataset_id,
                "display_name": "Rejected statement alias",
                "query_definition_jsonb": {"statement": "SELECT 1 FROM probe LIMIT 1"},
                "params_schema_jsonb": {"type": "object", "properties": {}},
            },
        )
        listing_status, listing = api.request("GET", "/api/datasets/query-templates")
        persisted = {
            str(row.get("query_template_id") or "").strip()
            for row in _rows(listing, "query_templates")
        }
        detail = malformed.get("detail") if isinstance(malformed, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        evidence.add(
            "saved_query_statement_rejected_before_write",
            malformed_status == 422
            and detail.get("code") == "saved_query_definition_invalid"
            and detail.get("path") == "query_definition_jsonb.statement"
            and listing_status == 200
            and query_id not in persisted,
            f"connector={connector_id}; HTTP {malformed_status}; "
            f"path={detail.get('path') or '<missing>'}; persisted={query_id in persisted}",
        )
    finally:
        if dataset_status in {200, 201}:
            api.request("DELETE", f"/api/datasets/{dataset_id}")


def _d10(api: CandidateApi, evidence: Evidence) -> None:
    """Own external-effect persistence, its statuses, and action scope.

    On the synthetic probe's own action, which contacts nothing: what is under
    test is the row the platform writes around an action that claims an effect,
    the status it records when one refuses, and the fact that an action cannot
    reach across a project or an account boundary. Which operation a particular
    bundle exposes, and what it does for its users, belongs to that bundle's own
    release suite.
    """
    _d10_private_network_override(evidence)
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        api.request("POST", f"/api/extensions/{extension_id}/enable", {})
        _d10_effect_persistence(api, evidence, extension_id)


def _d10_private_network_override(evidence: Evidence) -> None:
    """A self-hosted target lives on a private address, so the app must allow one."""
    override = subprocess.run(
        [
            "docker",
            "exec",
            _required("FS_SMOKE_APP_CONTAINER"),
            "sh",
            "-c",
            'printf %s "$FS_ALLOW_PRIVATE_REMOTE_URLS"',
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    evidence.add(
        "self_hosted_private_network_override",
        override.returncode == 0 and override.stdout.strip() == "1",
        f"container_exit={override.returncode}; enabled={override.stdout.strip() == '1'}",
    )


def _d10_probe_connection(api: CandidateApi, extension_id: str, project_id: str) -> str:
    status, payload = api.request(
        "POST",
        f"/api/projects/{project_id}/connections",
        {
            "extension_id": extension_id,
            "connection_kind": "extension",
            "connection_type": PROBE_CONNECTION_TYPE_ID,
            "display_name": "Probe endpoint",
            "runtime_mode": "local",
            "connection_config": {"base_url": PROBE_PRIVATE_BASE_URL},
        },
    )
    if status not in {200, 201}:
        raise RuntimeError(f"probe connection failed with HTTP {status}: {payload!r}")
    return str((payload or {}).get("connection_id") or "")


def _d10_effect_rows(extension_id: str) -> list[dict[str, Any]]:
    raw = _psql(
        "select coalesce(json_agg(json_build_object("
        "'operation_id',operation_id,'status',status,'effect_kind',effect_kind,"
        "'channel',channel,'target_count',target_count,'project_id',project_id,"
        "'connection_id',connection_id,'error_code',coalesce(error_code,''),"
        "'has_provider_message_id',provider_message_id is not null,"
        "'has_idempotency_key',idempotency_key <> ''"
        ")),'[]')::text from external_effects where extension_id="
        f"{_sql_literal(extension_id)}"
    )
    rows = json.loads(raw or "[]")
    return [row for row in rows if isinstance(row, dict)]


def _d10_effect_persistence(api: CandidateApi, evidence: Evidence, extension_id: str) -> None:
    workflow_id = _probe_workflow_id(api)
    project_id = ""
    other_project_id = ""
    foreign_account_id = ""
    foreign_workspace_id = str(uuid.uuid4())
    foreign_project_id = str(uuid.uuid4())
    marker = uuid.uuid4().hex[:10]
    try:
        project_id = _project(api, workflow_id)
        other_project_id = _project(api, workflow_id)
        connection_id = _d10_probe_connection(api, extension_id, project_id)
        other_connection_id = _d10_probe_connection(api, extension_id, other_project_id)

        action_path = f"/api/extensions/{extension_id}/action/{PROBE_ACTION_ID}"
        success_status, success_payload = api.request(
            "POST",
            action_path,
            {
                "context": {"project_id": project_id},
                "target": {"connection_id": connection_id},
                "input": {"target": f"probe-target-{marker}"},
            },
        )
        refusal_status, _ = api.request(
            "POST",
            action_path,
            {
                "context": {"project_id": project_id},
                "target": {"connection_id": connection_id},
                "input": {"target": f"probe-refused-{marker}", "fail": True},
            },
        )
        rows = _d10_effect_rows(extension_id)
        succeeded = [row for row in rows if row.get("status") == "succeeded"]
        failed = [row for row in rows if row.get("status") == "failed"]
        response = (success_payload or {}).get("action") or {}
        response = response if isinstance(response, dict) else {}
        evidence.add(
            "external_effect_audit_persistence",
            success_status == 200
            and len(succeeded) == 1
            and succeeded[0].get("operation_id") == PROBE_ACTION_ID
            # Straight from the declaration, not from anything the caller sent.
            and succeeded[0].get("effect_kind") == "message_delivery"
            and succeeded[0].get("channel") == "probe"
            # `observability.target_fields` says which input names the target.
            and int(succeeded[0].get("target_count") or 0) == 1
            and succeeded[0].get("project_id") == project_id
            and succeeded[0].get("connection_id") == connection_id
            and bool(succeeded[0].get("has_provider_message_id"))
            # Declared `idempotency: required`, so a replay must be recognisable.
            and bool(succeeded[0].get("has_idempotency_key")),
            f"action_http={success_status}; rows={len(rows)}; succeeded={succeeded}",
        )
        evidence.add(
            "external_effect_failure_status",
            refusal_status >= 400
            and len(failed) == 1
            and failed[0].get("error_code") == "probe_refused"
            # A refusal must not also be recorded as a delivery.
            and not failed[0].get("has_provider_message_id")
            and len(succeeded) == 1,
            f"action_http={refusal_status}; failed={failed}",
        )
        _d10_scope_denial(
            api,
            evidence,
            extension_id=extension_id,
            action_path=action_path,
            project_id=project_id,
            other_connection_id=other_connection_id,
            marker=marker,
            foreign_workspace_id=foreign_workspace_id,
            foreign_project_id=foreign_project_id,
            before=len(rows),
        )
        foreign_account_id = _D10_STATE.get("foreign_account_id", "")
    finally:
        if foreign_account_id:
            _psql(
                f"delete from projects where id={_sql_literal(foreign_project_id)}::uuid;"
                f"delete from workspaces where id={_sql_literal(foreign_workspace_id)}::uuid"
            )
            api.request("POST", f"/api/auth/accounts/{foreign_account_id}/disable", {})
        if other_project_id:
            _cleanup_project(api, other_project_id)
        if project_id:
            _cleanup_project(api, project_id)


#: Set by the scope-denial step so its fixture can be torn down by the caller.
_D10_STATE: dict[str, str] = {}


def _d10_scope_denial(
    api: CandidateApi,
    evidence: Evidence,
    *,
    extension_id: str,
    action_path: str,
    project_id: str,
    other_connection_id: str,
    marker: str,
    foreign_workspace_id: str,
    foreign_project_id: str,
    before: int,
) -> None:
    """An action must not reach a sibling project's connection or a foreign account."""
    denials: list[tuple[str, int, Any]] = []
    cross_status, cross_payload = api.request(
        "POST",
        action_path,
        {
            # The caller's own project, someone else's connection.
            "context": {"project_id": project_id},
            "target": {"connection_id": other_connection_id},
            "input": {"target": f"probe-cross-{marker}"},
        },
    )
    denials.append(("cross_project", cross_status, cross_payload))

    account_status, account_payload = api.request(
        "POST",
        "/api/auth/accounts",
        {
            "account_id": f"probe-foreign-{marker}",
            "display_name": "Probe foreign account fixture",
            "is_active": True,
        },
    )
    account_row = account_payload.get("account") if isinstance(account_payload, dict) else None
    foreign_account_id = (
        str((account_row or {}).get("account_id") or "") if isinstance(account_row, dict) else ""
    )
    _D10_STATE["foreign_account_id"] = foreign_account_id
    if account_status != 200 or not foreign_account_id:
        evidence.add(
            "extension_action_scope_denial",
            False,
            f"foreign account fixture failed with HTTP {account_status}",
        )
        return
    _psql(
        "insert into workspaces (id,account_id,workspace_key,display_name) values ("
        f"{_sql_literal(foreign_workspace_id)}::uuid,{_sql_literal(foreign_account_id)}::uuid,"
        f"{_sql_literal('probe-foreign-' + marker)},{_sql_literal('Probe foreign workspace')});"
        "insert into projects (id,account_id,workspace_id,name) values ("
        f"{_sql_literal(foreign_project_id)}::uuid,{_sql_literal(foreign_account_id)}::uuid,"
        f"{_sql_literal(foreign_workspace_id)}::uuid,"
        f"{_sql_literal('Probe foreign project')})"
    )
    foreign_status, foreign_payload = api.request(
        "POST",
        action_path,
        {
            "context": {"project_id": foreign_project_id},
            "input": {"target": f"probe-foreign-{marker}"},
        },
    )
    denials.append(("foreign_account", foreign_status, foreign_payload))
    serialized = json.dumps(denials, sort_keys=True, default=str).lower()
    after = _d10_effect_rows(extension_id)
    evidence.add(
        "extension_action_scope_denial",
        all(400 <= status < 500 for _, status, _ in denials)
        # A refused action must not have claimed an effect on the way out.
        and len(after) == before
        # Nor may the refusal itself hand back what it refused to reach.
        and PROBE_PRIVATE_BASE_URL.lower() not in serialized
        and other_connection_id.lower() not in serialized
        and "connection_config" not in serialized
        and "secret" not in serialized,
        f"denials={[(label, status) for label, status, _ in denials]}; "
        f"effects_before={before}; effects_after={len(after)}",
    )


def _probe_workflow_id(api: CandidateApi) -> str:
    """The catalog id of the probe's workflow, as the API reports it."""
    status, payload = api.request("GET", "/api/workflows?per_page=500")
    rows = payload if isinstance(payload, list) else _rows(payload, "workflows")
    if status != 200:
        raise RuntimeError(f"workflow listing failed with HTTP {status}")
    for row in rows:
        workflow_id = str(row.get("workflow_id") or "")
        if workflow_id.endswith(f".{PROBE_WORKFLOW_ID}"):
            return workflow_id
    raise RuntimeError("the probe published no workflow to bind")


def _a29(api: CandidateApi, evidence: Evidence) -> None:
    """Prove active-job fencing and product-owned queue cleanup on the candidate."""
    workflow_id = _create_hitl_workflow(api)
    project_id = _project(api, workflow_id)
    job_id = ""
    project_deleted = False
    try:
        job_id = _task(
            api,
            project_id,
            workflow_id,
            "a29-active-delete",
            inputs={"payload": {"marker": "a29-active"}},
        )
        active = _wait_job_state(api, job_id, {"awaiting_approval"}, timeout=90)
        queue_pair = {
            f"platform.project.{project_id}",
            f"platform.project.{project_id}.dead_letter",
        }
        queues_before = _project_queue_names()
        trash_status, _ = api.request("DELETE", f"/api/projects/{project_id}")
        first_delete_status, first_delete = _purge_trashed_project(api, project_id)
        before_counts = json.loads(
            _psql(
                "select json_build_object("
                "'project',(select count(*) from projects where id="
                f"{_sql_literal(project_id)}::uuid),"
                "'job',(select count(*) from jobs where id="
                f"{_sql_literal(job_id)}::uuid))::text"
            )
            or "{}"
        )
        evidence.add(
            "active_job_blocks_hard_delete_before_relational_mutation",
            str(active.get("status") or "") == "awaiting_approval"
            and trash_status == 200
            and first_delete_status == 409
            and _response_code(first_delete) == "project_executors_not_quiesced"
            and before_counts == {"project": 1, "job": 1}
            and f"platform.project.{project_id}" in queues_before,
            f"trash={trash_status}; hard_delete={first_delete_status}; counts={before_counts}; "
            f"queues_present={sorted(queue_pair & queues_before)}; detail={str(first_delete)[:120]}",
        )

        _settle_job_for_cleanup(api, job_id)
        delete_window_start = datetime.now(UTC).isoformat()
        second_delete_status, second_delete = _purge_trashed_project(api, project_id)
        project_deleted = second_delete_status == 200
        if project_deleted:
            _assert_product_removed_project_queues(project_id)
        after_counts = json.loads(
            _psql(
                "select json_build_object("
                "'project',(select count(*) from projects where id="
                f"{_sql_literal(project_id)}::uuid),"
                "'job',(select count(*) from jobs where id="
                f"{_sql_literal(job_id)}::uuid))::text"
            )
            or "{}"
        )
        evidence.add(
            "settled_job_allows_hard_delete_with_product_queue_cleanup",
            project_deleted
            and after_counts == {"project": 0, "job": 0}
            and not (queue_pair & _project_queue_names()),
            f"hard_delete={second_delete_status}; counts={after_counts}; "
            f"detail={str(second_delete)[:120]}",
        )
        time.sleep(3)
        readiness_status, _ = api.request("GET", "/health/ready")
        post_window_counts = json.loads(
            _psql(
                "select json_build_object("
                "'project',(select count(*) from projects where id="
                f"{_sql_literal(project_id)}::uuid),"
                "'job',(select count(*) from jobs where id="
                f"{_sql_literal(job_id)}::uuid))::text"
            )
            or "{}"
        )
        log_window_end = datetime.now(UTC).isoformat()
        log_window = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "candidate_log_window_check.py"),
                "--compose-project",
                _required("FS_SMOKE_COMPOSE_PROJECT"),
                "--since",
                delete_window_start,
                "--until",
                log_window_end,
                "--allowlist",
                os.path.join(os.path.dirname(__file__), "candidate-log-allowlist.json"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        evidence.add(
            "post_delete_executor_quiescence_window",
            project_deleted
            and readiness_status == 200
            and post_window_counts == {"project": 0, "job": 0}
            and not (queue_pair & _project_queue_names())
            and log_window.returncode == 0,
            f"readiness={readiness_status}; counts={post_window_counts}; "
            f"queues={sorted(queue_pair & _project_queue_names())}; "
            f"log_scan_exit={log_window.returncode}; "
            f"log_scan={(log_window.stdout + log_window.stderr)[-180:]}",
        )
    finally:
        if job_id and not project_deleted:
            api.request("POST", f"/api/jobs/{job_id}/cancel", {"reason": "A29 cleanup"})
        if project_id and not project_deleted:
            with suppress(RuntimeError):
                _purge_trashed_project(api, project_id)
                _assert_product_removed_project_queues(project_id)
        for method, path in (
            ("DELETE", f"/api/workflows/{workflow_id}"),
            ("POST", f"/api/trash/registry_workflow/{workflow_id}/hard-delete"),
        ):
            status, payload = api.request(method, path, {})
            if status != 200:
                raise RuntimeError(f"A29 cleanup {path} failed with HTTP {status}: {payload!r}")


def _d11_effective_workflow_defaults(api: CandidateApi, evidence: Evidence) -> None:
    """A new project must not silently inherit a disabled extension's workflow.

    Any extension-published workflow shows this, so it runs on the throwaway
    probe: omitting `workflow_ids` must not bind the workflow of an extension
    that is off, an explicit empty list must bind nothing, and naming it while
    the extension is on must bind exactly that one.
    """
    projects: list[str] = []
    with installed_probe_extension(api) as probe:
        extension_id = str(probe.get("extension_id") or "")
        try:
            detail_status, detail = api.request("GET", f"/api/extensions/{extension_id}")
            initial_state = str((detail or {}).get("state") or "") if detail_status == 200 else ""
            enable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/enable", {})
            qualified = _probe_workflow_id(api)
            disable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/disable", {})
            omitted_status, omitted_project = _d11_project(
                api, label="omitted-disabled", workflow_ids=None
            )
            empty_status, empty_project = _d11_project(api, label="empty", workflow_ids=[])
            projects.extend([value for value in (omitted_project, empty_project) if value])
            omitted_bound = (
                _d11_project_workflows(api, omitted_project) if omitted_project else set()
            )
            empty_bound = (
                _d11_project_workflows(api, empty_project) if empty_project else {"missing"}
            )
            reenable_status, _ = api.request("POST", f"/api/extensions/{extension_id}/enable", {})
            explicit_status, explicit_project = _d11_project(
                api, label="explicit-active", workflow_ids=[qualified]
            )
            if explicit_project:
                projects.append(explicit_project)
            explicit_bound = (
                _d11_project_workflows(api, explicit_project) if explicit_project else set()
            )
            evidence.add(
                "project_effective_workflow_defaults",
                detail_status == 200
                and enable_status == 200
                and disable_status == 200
                and omitted_status == 201
                and qualified not in omitted_bound
                and empty_status == 201
                and empty_bound == set()
                and reenable_status == 200
                and explicit_status == 201
                and explicit_bound == {qualified},
                f"extension={initial_state}; disable={disable_status}; "
                f"omitted={omitted_status}/{qualified in omitted_bound}; "
                f"empty={empty_status}/{len(empty_bound)}; reenable={reenable_status}; "
                f"explicit={explicit_status}/{sorted(explicit_bound)}",
            )
        finally:
            for project_id in reversed(projects):
                if project_id:
                    _cleanup_project(api, project_id)


def _d11_create_workflow(
    api: CandidateApi,
    *,
    label: str,
    operations: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> str:
    return _create_workflow(
        api,
        {
            "workflow_id": f"d11_{label}_{uuid.uuid4().hex[:10]}",
            "display_name": f"D11 {label} fixture",
            "description": "Disposable candidate-owned authored-workflow fixture.",
            "settings": dict(settings or {}),
            "tags": list(tags or []),
        },
        operations,
        label=f"D11 {label}",
    )


def _issue_codes(payload: Any) -> set[str]:
    """Issue codes of a change result, or of a refusal's ``detail``."""
    body = payload.get("detail") if isinstance(payload, dict) and "detail" in payload else payload
    issues = body.get("issues") if isinstance(body, dict) else []
    return {
        str(issue.get("code") or "").strip() for issue in issues or [] if isinstance(issue, dict)
    }


def _d11_project(
    api: CandidateApi,
    *,
    label: str,
    workflow_ids: list[str] | None,
) -> tuple[int, str]:
    payload: dict[str, Any] = {
        "name": f"d11-{label}-{uuid.uuid4().hex[:10]}",
        "workspace_id": _workspace(api),
    }
    if workflow_ids is not None:
        payload["workflow_ids"] = workflow_ids
    status, response = api.request("POST", "/api/projects", payload)
    project_id = str(response.get("project_id") or "") if isinstance(response, dict) else ""
    return status, project_id


def _d11_project_workflows(api: CandidateApi, project_id: str) -> set[str]:
    status, payload = api.request("GET", f"/api/projects/{project_id}/workflows")
    if status != 200:
        raise RuntimeError(f"D11 project workflow lookup failed with HTTP {status}: {payload!r}")
    return {
        str(row.get("workflow_id") or "").strip()
        for row in _rows(payload, "project_workflows")
        if str(row.get("workflow_id") or "").strip()
    }


def _d11_step_status(detail: dict[str, Any], step_id: str) -> str:
    workflow_run = detail.get("workflow_run") if isinstance(detail, dict) else None
    steps = workflow_run.get("steps") if isinstance(workflow_run, dict) else None
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("step_id") or "").strip() == str(step_id or "").strip():
            return str(step.get("status") or "").strip().lower()
    return ""


def _d11_switch(step_id: str, selector: str) -> dict[str, Any]:
    """A Switch with one ``ok`` case; its case and default outcomes are graph edges."""
    return {
        "step_id": step_id,
        "step_kind": "switch",
        "override": {"switch": {"selector": selector, "cases": [{"case_id": "ok", "when": "ok"}]}},
    }


def _d11_rejection_codes(
    api: CandidateApi,
    *,
    label: str,
    operations: list[dict[str, Any]],
) -> tuple[int, set[str], str]:
    """Build a broken definition and try to enable it.

    A disabled workflow keeps an invalid draft and reports its issues; an
    operation the change engine cannot apply is refused outright. Either way the
    workflow must not become runnable: the returned status is the refusal's.
    """
    workflow_id = f"d11_{label}_{uuid.uuid4().hex[:10]}"
    change_status, change = _draft_workflow(
        api,
        {
            "workflow_id": workflow_id,
            "display_name": f"D11 {label} rejection fixture",
            "description": "Must not become runnable.",
            "settings": {},
        },
        operations,
    )
    codes = _issue_codes(change)
    if change_status != 200:
        return change_status, codes, workflow_id
    enable_status, enabled = _enable_workflow(api, workflow_id)
    return enable_status, codes | _issue_codes(enabled), workflow_id


def _d11(api: CandidateApi, evidence: Evidence) -> None:
    """Prove the authored-workflow regressions on one immutable candidate."""
    workflows: list[str] = []
    projects: list[str] = []
    try:
        transform_id = _d11_create_workflow(
            api,
            label="transform",
            settings={
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["api"],
                    "input_mode": "fields",
                    "strict_inputs": False,
                    "expected_inputs": [{"name": "payload", "type": "json", "required": True}],
                }
            },
            operations=_linear_operations(
                {
                    "step_id": "map_payload",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.inputs.payload.value"},
                            "fields": [{"from": "kind", "to": "kind", "type": "text"}],
                            "result_target": "state.transformed",
                        }
                    },
                },
                {
                    "step_id": "done",
                    "step_kind": "stop_success",
                    "override": {"stop_success": {"message": "transform complete"}},
                },
            ),
        )
        workflows.append(transform_id)
        transform_project = _project(api, transform_id)
        projects.append(transform_project)
        invalid_marker = "d11-secret-input-must-not-escape"
        invalid_job = _task(
            api,
            transform_project,
            transform_id,
            "transform-invalid",
            inputs={"payload": {"value": invalid_marker}},
        )
        invalid_status, invalid_detail = _job_detail(api, invalid_job)
        invalid_json = json.dumps(invalid_detail, sort_keys=True)
        valid_job = _task(
            api,
            transform_project,
            transform_id,
            "transform-valid",
            inputs={"payload": {"value": {"kind": "email"}}},
        )
        valid_status, _valid_detail = _job_detail(api, valid_job)
        evidence.add(
            "transform_structured_public_diagnostic",
            invalid_status == "failed"
            and "input_item_not_object" in invalid_json
            and "state.inputs.payload" in invalid_json
            and "map_payload" in invalid_json
            and "must resolve to an object" in invalid_json
            and invalid_marker not in invalid_json,
            f"job={invalid_job}; status={invalid_status}; structured="
            f"{'input_item_not_object' in invalid_json}; selector="
            f"{'state.inputs.payload' in invalid_json}; redacted={invalid_marker not in invalid_json}",
        )
        evidence.add(
            "transform_valid_control",
            valid_status == "completed",
            f"job={valid_job}; status={valid_status}",
        )

        failure_id = _d11_create_workflow(
            api,
            label="expected_failure",
            settings={
                "traits": {"expected_failure": True},
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["api"],
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "expected_inputs": [
                        {"name": "failure_payload", "type": "json", "required": True}
                    ],
                },
            },
            tags=["expected-failure"],
            operations=_linear_operations(
                {
                    "step_id": "normalize_failure",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.inputs.failure_payload"},
                            "fields": [{"from": "reason", "to": "reason", "type": "text"}],
                            "result_target": "state.failure_probe",
                        }
                    },
                },
                {
                    "step_id": "fail",
                    "step_kind": "stop_fail",
                    "override": {"stop_fail": {"reason": "D11 expected failure"}},
                },
            ),
        )
        workflows.append(failure_id)
        failure_project = _project(api, failure_id)
        projects.append(failure_project)
        failure_job = _task(
            api,
            failure_project,
            failure_id,
            "expected-failure",
            inputs={"failure_payload": {"reason": "expected"}},
        )
        failure_status, failure_detail = _job_detail(api, failure_job)
        failure_json = json.dumps(failure_detail, sort_keys=True)
        evidence.add(
            "expected_failure_public_create_and_run",
            failure_status == "failed" and "stop_success" not in failure_json,
            f"workflow={failure_id}; job={failure_job}; status={failure_status}; "
            f"injected_success={'stop_success' in failure_json}",
        )

        mixed_id = _d11_create_workflow(
            api,
            label="mixed_terminal",
            settings={
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["api"],
                    "input_mode": "fields",
                    "strict_inputs": True,
                    "expected_inputs": [{"name": "route", "type": "json", "required": True}],
                }
            },
            operations=_definition_operations(
                [
                    {
                        "step_id": "normalize_outcome",
                        "step_kind": "transform",
                        "override": {
                            "transform": {
                                "input": {"source": "state.inputs.route"},
                                "fields": [{"from": "outcome", "to": "outcome", "type": "text"}],
                                "result_target": "state.routed_input",
                            }
                        },
                    },
                    _d11_switch("route_outcome", "state.inputs.route.outcome"),
                    {"step_id": "success", "step_kind": "stop_success"},
                    {
                        "step_id": "failure",
                        "step_kind": "stop_fail",
                        "override": {"stop_fail": {"reason": "D11 selected failure"}},
                    },
                ],
                [
                    _graph_edge("normalize_outcome", "route_outcome"),
                    _graph_edge("route_outcome", "success", "case", case_id="ok"),
                    _graph_edge("route_outcome", "failure", "default"),
                ],
            ),
        )
        workflows.append(mixed_id)
        mixed_project = _project(api, mixed_id)
        projects.append(mixed_project)
        mixed_results = []
        for outcome in ("ok", "bad"):
            job_id = _task(
                api,
                mixed_project,
                mixed_id,
                f"mixed-{outcome}",
                inputs={"route": {"outcome": outcome}},
            )
            status, _detail = _job_detail(api, job_id)
            mixed_results.append((outcome, status))
        evidence.add(
            "exhaustive_mixed_terminal_routing",
            mixed_results == [("ok", "completed"), ("bad", "failed")],
            f"workflow={mixed_id}; results={mixed_results}",
        )

        # Each broken graph must be refused a run, with the issue that names it.
        topology_negatives = (
            (
                "unreachable_exhaustive_router",
                "workflow.graph.unreachable",
                _definition_operations(
                    [
                        _d11_switch("first_router", "state.inputs.first_outcome"),
                        _d11_switch("unreachable_router", "state.inputs.final_outcome"),
                        {"step_id": "done", "step_kind": "stop_success"},
                        {"step_id": "failed", "step_kind": "stop_fail"},
                    ],
                    [
                        _graph_edge("first_router", "done", "case", case_id="ok"),
                        _graph_edge("first_router", "done", "default"),
                        _graph_edge("unreachable_router", "done", "case", case_id="ok"),
                        _graph_edge("unreachable_router", "failed", "default"),
                    ],
                ),
            ),
            (
                "unreachable_intermediate_step",
                "workflow.graph.unreachable",
                _definition_operations(
                    [
                        _d11_switch("route_outcome", "state.inputs.outcome"),
                        {
                            "step_id": "must_execute",
                            "step_kind": "set_variable",
                            "override": {
                                "set_variable": {"target": "state.must_execute", "value": True}
                            },
                        },
                        {"step_id": "success", "step_kind": "stop_success"},
                        {"step_id": "failure", "step_kind": "stop_fail"},
                    ],
                    [
                        _graph_edge("route_outcome", "success", "case", case_id="ok"),
                        _graph_edge("route_outcome", "failure", "default"),
                        _graph_edge("must_execute", "success"),
                    ],
                ),
            ),
            (
                "action_after_terminal_step",
                "workflow.graph.outcome_unsupported",
                _linear_operations(
                    {"step_id": "success", "step_kind": "stop_success"},
                    {
                        "step_id": "after_stop",
                        "step_kind": "set_variable",
                        "override": {"set_variable": {"target": "state.after_stop", "value": True}},
                    },
                ),
            ),
            (
                "unknown_branch_target",
                "workflow.graph.target_unknown",
                _definition_operations(
                    [
                        _d11_switch("route_outcome", "state.inputs.outcome"),
                        {"step_id": "success", "step_kind": "stop_success"},
                    ],
                    [
                        _graph_edge("route_outcome", "missing_step", "case", case_id="ok"),
                        _graph_edge("route_outcome", "success", "default"),
                    ],
                ),
            ),
        )
        for label, required_code, operations in topology_negatives:
            status, codes, workflow_id = _d11_rejection_codes(
                api,
                label=label,
                operations=operations,
            )
            workflows.append(workflow_id)
            evidence.add(
                f"{label}_rejected",
                status == 400 and required_code in codes,
                f"workflow={workflow_id}; status={status}; codes={sorted(codes)}",
            )

        for reference_label, reference in (
            ("plain", "state.created_dataset.dataset_handle"),
            ("template", "{{ state.created_dataset.dataset_handle }}"),
        ):
            tabular_id = _d11_create_workflow(
                api,
                label=f"tabular_{reference_label}",
                operations=_linear_operations(
                    {
                        "step_id": "create_dataset",
                        "step_kind": "tabular_feed",
                        "override": {
                            "tabular_feed": {
                                "operation": "create_dataset",
                                "rows": [{"sku": "00123", "qty": "2"}],
                                "result_target": "state.created_dataset",
                            }
                        },
                    },
                    {
                        "step_id": "read_rows",
                        "step_kind": "tabular_feed",
                        "override": {
                            "tabular_feed": {
                                "operation": "read_rows",
                                "input": {"dataset_handle": reference},
                                "columns": ["sku", "qty"],
                                "result_target": "state.page",
                            }
                        },
                    },
                    {"step_id": "done", "step_kind": "stop_success"},
                ),
            )
            workflows.append(tabular_id)
            tabular_project = _project(api, tabular_id)
            projects.append(tabular_project)
            tabular_job = _task(api, tabular_project, tabular_id, f"tabular-{reference_label}")
            tabular_status, tabular_detail = _job_detail(api, tabular_job)
            create_status = _d11_step_status(tabular_detail, "create_dataset")
            read_status = _d11_step_status(tabular_detail, "read_rows")
            evidence.add(
                f"tabular_checkpoint_{reference_label}_reference",
                tabular_status == "completed"
                and create_status == "completed"
                and read_status == "completed",
                f"workflow={tabular_id}; job={tabular_job}; status={tabular_status}; "
                f"create_dataset={create_status or '<missing>'}; "
                f"read_rows={read_status or '<missing>'}",
            )

        _d11_effective_workflow_defaults(api, evidence)
    finally:
        for project_id in reversed(projects):
            if project_id:
                _cleanup_project(api, project_id)
        for workflow_id in reversed(workflows):
            for method, path in (
                ("DELETE", f"/api/workflows/{workflow_id}"),
                ("POST", f"/api/trash/registry_workflow/{workflow_id}/hard-delete"),
            ):
                status, payload = api.request(method, path, {})
                if status != 200:
                    raise RuntimeError(f"D11 cleanup {path} failed with HTTP {status}: {payload!r}")


def _cleanup_registry_workflow_fixture(
    api: CandidateApi,
    workflow_id: str,
    *,
    check_id: str,
) -> None:
    delete_path = f"/api/workflows/{workflow_id}"
    delete_status, delete_payload = api.request("DELETE", delete_path, {})
    if delete_status not in {200, 404}:
        raise RuntimeError(
            f"{check_id} cleanup {delete_path} failed with HTTP {delete_status}: {delete_payload!r}"
        )
    hard_delete_path = f"/api/trash/registry_workflow/{workflow_id}/hard-delete"
    hard_delete_status, hard_delete_payload = api.request("POST", hard_delete_path, {})
    if hard_delete_status != 200:
        raise RuntimeError(
            f"{check_id} cleanup {hard_delete_path} failed with HTTP "
            f"{hard_delete_status}: {hard_delete_payload!r}"
        )


def _missing_service_collector() -> tuple[list[str], Callable[[str, str, str], None]]:
    """A place to note services this host does not have, and the note-taker."""
    missing: list[str] = []

    def record(alias: str, container: str, detail: str) -> None:
        missing.append(f"{alias}={container} ({detail.splitlines()[-1] if detail else 'absent'})")

    return missing, record


def _bundle_release_checks(api: CandidateApi, evidence: Evidence) -> None:
    """Run whatever release suites the installed bundles declare, each isolated.

    Nothing is named here, and nothing is executed here. Each suite runs in its
    own container with the bundle mounted read-only, no Core source, no platform
    DSN, no Docker socket and no inherited environment — only the candidate URL,
    a token minted for that run with the scopes the bundle declared, and the
    addresses it declared it observes. A candidate that ships no suite
    contributes no assertions, and that is recorded rather than left silent.
    """
    suites = discover_bundle_suites(os.environ.get("FS_SMOKE_EXTENSIONS_ROOT") or "")
    evidence.add(
        "bundle_release_suites_discovered",
        True,
        f"{len(suites)} declared: "
        + (", ".join(f"{suite.name}({len(suite.checks)})" for suite in suites) or "<none>"),
    )
    if not suites:
        return
    base_url = _required(CANDIDATE_INTERNAL_URL_ENV)
    app_container = _required(CANDIDATE_APP_CONTAINER_ENV)
    app_alias = _required(CANDIDATE_APP_ALIAS_ENV)
    available_services = dict(_service_bindings(os.environ.get(RELEASE_SERVICES_ENV) or ""))
    provider, model, how = resolve_model_route(api)
    model_route_required = suites_require_model_route(suites)
    evidence.add(
        "model_route_for_release_fixtures",
        bool(provider and model) or not model_route_required,
        (
            f"{how}: provider={provider or '<none>'}; model={model or '<none>'}"
            if model_route_required
            else "not required by the selected bundle suites"
        ),
    )
    for suite in suites:
        token = ""
        token_id = ""
        try:
            token, token_id = mint_scoped_token(api, suite)
            if not token:
                evidence.add(
                    f"{suite.name}_release_suite",
                    False,
                    "the bundle declared no RELEASE_SCOPES, so no token was issued",
                )
                continue
            inputs = {name: str(os.environ.get(name) or "") for name in suite.inputs}
            # The trusted side enables what the bundle declared and makes the
            # project it works in: a suite's token can do neither.
            with prepared_fixtures(api, suite) as fixtures:
                inputs.update(fixtures)
                # A network of its own, holding the candidate and the declared
                # test services and nothing else — not the candidate's Compose
                # network, which carries the database, the broker and a route off
                # the host.
                # Only what this bundle declared: a suite gets the services it
                # named on its wire, not every service the stand happens to allow.
                services = [
                    (alias, available_services[alias])
                    for alias in suite.services
                    if alias in available_services
                ]
                undeclared = sorted(set(suite.services) - set(available_services))
                missing, record_missing = _missing_service_collector()
                for alias in undeclared:
                    record_missing(alias, "<not offered by this stand>", "")
                with suite_network(
                    app_container=app_container,
                    app_alias=app_alias,
                    services=services,
                    on_missing_service=record_missing,
                ) as network:
                    rows, created = run_bundle_suite(
                        suite,
                        base_url=base_url,
                        token=token,
                        account=api.account,
                        network=network,
                        inputs=inputs,
                    )
                if missing:
                    evidence.add(
                        f"{suite.name}_declared_services_available",
                        False,
                        f"not on this host: {'; '.join(missing)}",
                    )
                leaked = remove_created_resources(api, created)
                evidence.add(
                    f"{suite.name}_fixtures_removed",
                    not leaked,
                    f"declared={len(created)}; not_removed={leaked or '<none>'}",
                )
            for row in rows:
                evidence.add(
                    str(row.get("name") or f"{suite.name}_unnamed"),
                    bool(row.get("passed")),
                    str(row.get("detail") or ""),
                )
        except Exception as exc:  # one bundle's failure must not hide the rest
            evidence.add(f"{suite.name}_release_suite", False, f"{type(exc).__name__}: {exc}")
        finally:
            # The grant lives no longer than the run that needed it.
            revoke_token(api, token_id)


def main() -> int:
    check_id = str(sys.argv[1] if len(sys.argv) > 1 else "").strip()
    evidence = Evidence(check_id)
    try:
        if str(os.environ.get("FS_SMOKE_ISOLATED") or "") != "1":
            raise RuntimeError("FS_SMOKE_ISOLATED=1 is required for semantic fixture mutation")
        api = CandidateApi()
        {
            "A06": _a06,
            "A13": _a13,
            "A19": _a19,
            "A20": _a20,
            "A21": _a21,
            "A22": _a22,
            "A23": _a23,
            "A24": _a24,
            "A25": _a25,
            "A26": _a26,
            "A27": _a27,
            "A29": _a29,
            "BUNDLES": _bundle_release_checks,
            "D09": _d09,
            "D10": _d10,
            "D11": _d11,
            "E12": _e12,
        }[check_id](api, evidence)
    except Exception as exc:
        evidence.add("semantic_runner", False, f"{type(exc).__name__}: {exc}")
    evidence.emit()
    return 0 if all(row["passed"] for row in evidence.assertions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
