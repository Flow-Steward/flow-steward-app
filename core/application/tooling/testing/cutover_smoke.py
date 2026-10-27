#!/usr/bin/env python3
"""Phase 5 cutover smoke runner.

Submits one real job, auto-approves gates, and verifies message API output.

Usage:
  docker compose exec -T fs-app \
    flow-steward testing cutover-smoke --project-id <project-uuid>
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import quote

import requests
import urllib3

from .live_auth import authenticated_session, csrf_headers

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "max_iterations"}


def _json_or_empty(resp: requests.Response) -> dict:
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {}


def _post_task(
    session: requests.Session,
    base_url: str,
    verify_ssl: bool,
    project_id: str,
    summary: str,
) -> str:
    workflow_id = _resolve_workflow_id(session, base_url, verify_ssl, project_id)
    payload = {
        "project_id": project_id,
        "workflow_id": workflow_id,
    }
    required_inputs = _required_text_inputs(session, base_url, verify_ssl, project_id, workflow_id)
    if required_inputs:
        payload["inputs"] = dict.fromkeys(required_inputs, summary)
    print(f"[smoke] submit payload={payload}", flush=True)
    resp = session.post(
        f"{base_url.rstrip('/')}/api/workflow-runs",
        json=payload,
        headers=csrf_headers(session),
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    body = _json_or_empty(resp)
    job_id = body.get("job_id") or body.get("id")
    if not job_id:
        raise RuntimeError(f"/api/workflow-runs returned no job id: {body}")
    return str(job_id).strip()


def _required_text_inputs(
    session: requests.Session,
    base_url: str,
    verify_ssl: bool,
    project_id: str,
    workflow_id: str,
) -> list[str]:
    """Required text inputs of the workflow contract; the smoke fills them with its summary."""
    project = quote(str(project_id).strip(), safe="")
    workflow = quote(str(workflow_id).strip(), safe="")
    resp = session.get(
        f"{base_url.rstrip('/')}/api/projects/{project}/workflows/{workflow}/contract",
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    interface = _json_or_empty(resp).get("interface") or {}
    schema = interface.get("input_schema") if isinstance(interface, dict) else {}
    if not isinstance(schema, dict):
        return []
    raw_properties = schema.get("properties")
    properties: dict = raw_properties if isinstance(raw_properties, dict) else {}
    raw_required = schema.get("required")
    required: list = raw_required if isinstance(raw_required, list) else []
    return [
        str(name)
        for name in required
        if isinstance(properties.get(name), dict)
        and properties[name].get("type") in (None, "string", "text")
    ]


def _resolve_workflow_id(
    session: requests.Session,
    base_url: str,
    verify_ssl: bool,
    project_id: str,
) -> str:
    encoded_project_id = quote(str(project_id).strip(), safe="")
    resp = session.get(
        f"{base_url.rstrip('/')}/api/projects/{encoded_project_id}/workflows",
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    body = _json_or_empty(resp)
    rows = body.get("project_workflows")
    if not isinstance(rows, list):
        rows = []

    available: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not bool(row.get("enabled")) or bool(row.get("new_runs_blocked")):
            continue
        if str(row.get("availability_status") or "available").strip().lower() != "available":
            continue
        workflow_id = str(row.get("workflow_id") or "").strip()
        if workflow_id:
            available.append(row)

    default_id = str(body.get("operator_default_workflow_id") or "").strip()
    for row in available:
        workflow_id = str(row.get("workflow_id") or "").strip()
        if workflow_id == default_id:
            return workflow_id
    for row in available:
        if bool(row.get("is_default")):
            return str(row.get("workflow_id") or "").strip()
    if available:
        return str(available[0].get("workflow_id") or "").strip()
    raise RuntimeError(
        f"Project {project_id} has no linked, enabled workflow available for cutover smoke"
    )


def _get_job(session: requests.Session, base_url: str, verify_ssl: bool, job_id: str) -> dict:
    resp = session.get(f"{base_url.rstrip('/')}/api/jobs/{job_id}", verify=verify_ssl, timeout=30)
    resp.raise_for_status()
    return _json_or_empty(resp)


def _get_approval(session: requests.Session, base_url: str, verify_ssl: bool, job_id: str) -> dict:
    resp = session.get(
        f"{base_url.rstrip('/')}/api/jobs/{job_id}/approval", verify=verify_ssl, timeout=30
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return _json_or_empty(resp)


def _approve(
    session: requests.Session,
    base_url: str,
    verify_ssl: bool,
    thread_id: str,
    approval_id: str,
    comment: str,
) -> int:
    headers = csrf_headers(session)
    headers["Idempotency-Key"] = f"cutover-approval:{approval_id}:approve"
    resp = session.post(
        f"{base_url.rstrip('/')}/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True, "comment": comment},
        headers=headers,
        verify=verify_ssl,
        timeout=30,
    )
    return resp.status_code


def _fetch_messages(
    session: requests.Session,
    base_url: str,
    verify_ssl: bool,
    job_id: str,
    limit: int = 2000,
) -> list[dict]:
    resp = session.get(
        f"{base_url.rstrip('/')}/api/jobs/{job_id}/messages",
        params={"limit": max(1, min(limit, 2000))},
        verify=verify_ssl,
        timeout=30,
    )
    resp.raise_for_status()
    body = _json_or_empty(resp)
    rows = body.get("messages")
    return rows if isinstance(rows, list) else []


def run_smoke(
    *,
    base_url: str,
    verify_ssl: bool,
    project_id: str,
    summary: str,
    timeout_seconds: int,
    poll_seconds: int,
    username: str = "",
    password: str = "",
    bearer_token: str = "",
) -> int:
    session = authenticated_session(
        base_url=base_url,
        verify_ssl=verify_ssl,
        username=username,
        password=password,
        bearer_token=bearer_token,
    )
    job_id = _post_task(session, base_url, verify_ssl, project_id, summary)
    print(f"[smoke] job_id={job_id}", flush=True)

    deadline = time.time() + max(30, timeout_seconds)
    seen_status = ""

    while time.time() < deadline:
        try:
            job = _get_job(session, base_url, verify_ssl, job_id)
        except (requests.HTTPError, requests.ConnectionError) as exc:
            if isinstance(exc, requests.ConnectionError):
                print("[smoke] transient job read connection error; retrying", flush=True)
                time.sleep(max(0.1, min(float(poll_seconds), 2.0)))
                continue
            status_code = getattr(exc.response, "status_code", None)
            if status_code not in {409, 502, 503, 504}:
                raise
            print(
                f"[smoke] transient job read HTTP {status_code}; retrying",
                flush=True,
            )
            time.sleep(max(0.1, min(float(poll_seconds), 2.0)))
            continue
        status = str(job.get("status") or "").strip().lower()
        thread_id = str(job.get("thread_id") or "").strip()
        if status != seen_status:
            print(
                f"[smoke] status={status or '<empty>'} thread_id={thread_id or '<none>'}",
                flush=True,
            )
            seen_status = status

        if status in TERMINAL_STATUSES:
            break

        if status == "awaiting_approval":
            approval_payload = _get_approval(session, base_url, verify_ssl, job_id)
            effective_thread_id = thread_id or str(approval_payload.get("thread_id") or "").strip()
            if effective_thread_id:
                code = _approve(
                    session,
                    base_url,
                    verify_ssl,
                    effective_thread_id,
                    str(approval_payload.get("approval_id") or "").strip(),
                    comment="Phase 5 cutover smoke auto-approve",
                )
                print(
                    f"[smoke] approved thread={effective_thread_id[:12]} status_code={code}",
                    flush=True,
                )
            elif not effective_thread_id:
                print("[smoke] awaiting_approval but no thread_id available", flush=True)
                break

        time.sleep(max(1, poll_seconds))

    job = _get_job(session, base_url, verify_ssl, job_id)
    final_status = str(job.get("status") or "").strip().lower()
    print(f"[smoke] final_status={final_status or '<empty>'}", flush=True)

    messages = _fetch_messages(session, base_url, verify_ssl, job_id)
    print(f"[smoke] message_count={len(messages)}", flush=True)
    if messages:
        statuses = sorted({str(m.get("status") or "").lower() for m in messages})
        producers = sorted(
            {
                str(m.get("from_agent") or "")
                for m in messages
                if str(m.get("from_agent") or "").strip()
            }
        )
        steps = sorted(
            {str(m.get("step_id") or "") for m in messages if str(m.get("step_id") or "").strip()}
        )
        print(f"[smoke] message_statuses={statuses}", flush=True)
        print(f"[smoke] message_producers={producers}", flush=True)
        print(f"[smoke] message_steps={steps}", flush=True)

    if final_status != "completed":
        print("[smoke] FAIL: job did not complete", flush=True)
        return 1
    if len(messages) < 3:
        print(
            "[smoke] FAIL: expected at least three agent messages from /api/jobs/{id}/messages",
            flush=True,
        )
        return 2

    print("[smoke] PASS", flush=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run Phase 5 cutover smoke workflow")
    parser.add_argument("--project-id", required=True, help="Target project UUID")
    parser.add_argument(
        "--base-url", default="http://localhost:8099", help="Flow Steward API base URL"
    )
    parser.add_argument("--verify-ssl", action="store_true", help="Enable TLS verification")
    parser.add_argument("--summary", default="Phase 5 smoke: summarize the project status")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args(argv)

    return run_smoke(
        base_url=args.base_url,
        verify_ssl=bool(args.verify_ssl),
        project_id=args.project_id,
        summary=args.summary,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
