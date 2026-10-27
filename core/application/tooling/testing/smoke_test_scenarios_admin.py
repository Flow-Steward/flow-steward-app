#!/usr/bin/env python3
"""Admin, registry, monitoring, and cleanup smoke scenarios."""

from __future__ import annotations

import sys
import time
import traceback
import uuid
from contextlib import suppress
from typing import Any


def run_admin_smoke_scenarios(context: dict[str, Any]) -> None:
    S = context["S"]
    H = context["H"]
    BASE = context["BASE"]
    PROJECT_ID = context["PROJECT_ID"]
    BUGS = context["BUGS"]
    bug = context["bug"]
    ok = context["ok"]
    get_job = context["get_job"]
    cancel = context["cancel"]
    get_active_jobs = context["get_active_jobs"]
    _enabled_workflow_id = context["_enabled_workflow_id"]
    _scheduled_runs_for_job = context["_scheduled_runs_for_job"]
    _wait_for_scheduled_run = context["_wait_for_scheduled_run"]
    _extract_jobs_rows = context["_extract_jobs_rows"]

    # ============================================================
    # TEST 15: Page smoke tests
    # ============================================================
    print("\n=== TEST 15: Page Smoke Tests ===")
    try:
        pages = [
            ("/projects", "Projects"),
            ("/jobs", "Jobs"),
            ("/monitoring?tab=processes", "Processes"),
            ("/settings", "Settings"),
            ("/registry", "Registry"),
            (f"/projects/{PROJECT_ID}", "Project Detail"),
        ]
        for path, name in pages:
            r = S.get(f"{BASE}{path}", headers=H)
            if r.status_code != 200:
                bug(f"page_{name}", f"{name} ({path}): {r.status_code}")
            else:
                ok(f"{name} page OK")

    except Exception:
        bug("pages", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 16: Restart a completed/cancelled job
    # ============================================================
    print("\n=== TEST 16: Job Restart ===")
    try:
        r = S.get(f"{BASE}/api/jobs?limit=50", headers=H)
        data = r.json()
        jobs = _extract_jobs_rows(data)
        restartable = [
            j
            for j in jobs
            if isinstance(j, dict) and j.get("status") in ("completed", "failed", "cancelled")
        ]

        if restartable:
            target = restartable[0]
            tid = target["id"]
            tstat = target["status"]
            print(f"  Restarting job {tid} (was: {tstat})")
            rr = S.post(f"{BASE}/api/jobs/{tid}/restart", headers=H)
            print(f"  Restart: {rr.status_code}")
            if rr.status_code in (200, 201, 202):
                ok("Restart accepted")
                time.sleep(3)
                jd = get_job(tid)
                new_status = jd.get("status", "")
                if new_status in ("in_progress", "queued", "awaiting_approval"):
                    ok(f"Restarted → {new_status}")
                cancel(tid)
            elif rr.status_code == 404:
                ok("Restart endpoint not found")
            elif rr.status_code == 409:
                ok("Restart rejected (conflict)")
            else:
                bug("restart_unexpected", f"Restart: {rr.status_code} {rr.text[:200]}")
        else:
            ok("No restartable jobs found")

    except Exception:
        bug("restart", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 17: Auth APIs (change password, me, tokens)
    # ============================================================
    print("\n=== TEST 17: Auth APIs ===")
    try:
        # /api/auth/me
        r = S.get(f"{BASE}/api/auth/me", headers=H)
        if r.status_code != 200:
            bug("auth_me", f"/api/auth/me: {r.status_code}")
        else:
            ok("/api/auth/me OK")
            me = r.json()
            if not me.get("authenticated"):
                bug("auth_me_unauthed", "me.authenticated is not True")

        # Change password (wrong current password)
        r = S.post(
            f"{BASE}/api/auth/change-password",
            json={
                "current_password": "wrong-password",  # pragma: allowlist secret
                "new_password": "new-password",  # pragma: allowlist secret
            },
            headers=H,
        )
        if r.status_code in (400, 401, 403, 422):
            ok(f"Change password (wrong current) rejected: {r.status_code}")
        elif r.status_code == 404:
            ok("Change password endpoint not implemented")
        else:
            bug("change_pw_wrong", f"Wrong password accepted: {r.status_code}")

        # Personal tokens list
        r = S.get(f"{BASE}/api/auth/tokens", headers=H)
        if r.status_code == 200:
            ok("Personal tokens list OK")
        elif r.status_code == 404:
            ok("Personal tokens not implemented")
        else:
            bug("tokens_list", f"Tokens list: {r.status_code}")

        # Service accounts list
        r = S.get(f"{BASE}/api/auth/service-accounts", headers=H)
        if r.status_code == 200:
            ok("Service accounts list OK")
        elif r.status_code == 404:
            ok("Service accounts not implemented")
        else:
            bug("sa_list", f"Service accounts: {r.status_code}")

    except Exception:
        bug("auth_apis", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 18: Project Config APIs
    # ============================================================
    print("\n=== TEST 18: Project Config APIs ===")
    try:
        config_endpoints = {
            "workflows": f"/api/projects/{PROJECT_ID}/workflows",
            "policy": f"/api/projects/{PROJECT_ID}/policy",
            "providers": f"/api/projects/{PROJECT_ID}/providers",
            "queue": f"/api/projects/{PROJECT_ID}/queue",
            "history": f"/api/projects/{PROJECT_ID}/history",
            "scheduled-jobs": f"/api/projects/{PROJECT_ID}/scheduled-jobs",
            "notification-routes": f"/api/projects/{PROJECT_ID}/notification-routes",
            "notification-deliveries": f"/api/projects/{PROJECT_ID}/notification-deliveries",
        }
        for name, path in config_endpoints.items():
            r = S.get(f"{BASE}{path}", headers=H)
            if r.status_code == 200:
                ok(f"Project {name} API OK")
            elif r.status_code == 404:
                ok(f"Project {name} not implemented")
            else:
                bug(f"proj_{name}", f"Project {name}: {r.status_code}")

    except Exception:
        bug("project_config", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 18B: Scheduled job run-now smoke
    # ============================================================
    print("\n=== TEST 18B: Scheduled Job Run-Now Smoke ===")
    scheduled_job_id = ""
    scheduled_run_job_id = ""
    try:
        workflow_id = _enabled_workflow_id()
        if not workflow_id:
            ok("No enabled workflow available for scheduled job run-now smoke")
        else:
            summary = f"E2E run-now smoke {uuid.uuid4().hex[:8]}"
            create_resp = S.post(
                f"{BASE}/api/projects/{PROJECT_ID}/scheduled-jobs",
                json={
                    "task_summary": summary,
                    "instructions": "Run-now smoke coverage",
                    "workflow_id": workflow_id,
                    "cron_expr": "11 4 * * *",
                    "enabled": False,
                },
                headers=H,
            )
            if create_resp.status_code != 201:
                bug(
                    "scheduled_run_now_create",
                    f"Create scheduled job failed: {create_resp.status_code} {create_resp.text[:200]}",
                )
            else:
                created = create_resp.json()
                scheduled_job_id = str(
                    created.get("scheduled_job_id") or created.get("id") or ""
                ).strip()
                if not scheduled_job_id:
                    bug("scheduled_run_now_create", f"Missing scheduled_job_id: {created}")
                else:
                    baseline_run_ids = {
                        str(row.get("id") or "").strip()
                        for row in _scheduled_runs_for_job(scheduled_job_id)
                        if str(row.get("id") or "").strip()
                    }
                    run_resp = S.post(
                        f"{BASE}/api/projects/{PROJECT_ID}/scheduled-jobs/{scheduled_job_id}/run-now",
                        headers=H,
                    )
                    if run_resp.status_code not in (200, 201, 202):
                        bug(
                            "scheduled_run_now_post",
                            f"Run-now failed: {run_resp.status_code} {run_resp.text[:200]}",
                        )
                    else:
                        payload = run_resp.json()
                        status = str(payload.get("status") or "").strip().lower()
                        scheduled_run_job_id = str(payload.get("job_id") or "").strip()
                        if status != "enqueued" or not scheduled_run_job_id:
                            bug(
                                "scheduled_run_now_result", f"Unexpected run-now payload: {payload}"
                            )
                        else:
                            ok(f"Run-now enqueued job {scheduled_run_job_id}")
                            run_row = _wait_for_scheduled_run(
                                scheduled_job_id,
                                baseline_run_ids=baseline_run_ids,
                            )
                            if not run_row:
                                bug(
                                    "scheduled_run_now_history",
                                    f"No run history row for scheduled job {scheduled_job_id}",
                                )
                            elif str(run_row.get("job_id") or "").strip() != scheduled_run_job_id:
                                bug(
                                    "scheduled_run_now_history",
                                    f"Run history job mismatch: {run_row}",
                                )
                            else:
                                ok("Run-now recorded scheduled run history")

    except Exception:
        bug("scheduled_run_now", f"Exception: {traceback.format_exc()}")
    finally:
        if scheduled_run_job_id:
            cancel(scheduled_run_job_id)
        if scheduled_job_id:
            with suppress(Exception):
                S.delete(
                    f"{BASE}/api/projects/{PROJECT_ID}/scheduled-jobs/{scheduled_job_id}",
                    headers=H,
                )

    # ============================================================
    # TEST 19: Job Diagnostics APIs
    # ============================================================
    print("\n=== TEST 19: Job Diagnostics APIs ===")
    try:
        # Find any job for diagnostics
        r = S.get(f"{BASE}/api/jobs?limit=5", headers=H)
        data = r.json()
        jobs = _extract_jobs_rows(data)
        if jobs:
            jid = jobs[0].get("id", jobs[0].get("job_id"))
            diag_endpoints = {
                "runtime-journal": f"/api/jobs/{jid}/runtime-journal",
                "decision-overrides": f"/api/jobs/{jid}/decision-overrides",
                "errors": f"/api/jobs/{jid}/errors",
                "causal-chain": f"/api/jobs/{jid}/causal-chain",
                "approvals": f"/api/jobs/{jid}/approvals",
                "reactions": f"/api/jobs/{jid}/reactions",
                "step-replays": f"/api/jobs/{jid}/step-replays",
            }
            for name, path in diag_endpoints.items():
                r2 = S.get(f"{BASE}{path}", headers=H)
                if r2.status_code == 200:
                    ok(f"Job {name} OK")
                elif r2.status_code == 404:
                    ok(f"Job {name} not implemented")
                else:
                    bug(f"diag_{name}", f"Job {name}: {r2.status_code}")
        else:
            ok("No jobs available for diagnostics")

    except Exception:
        bug("diagnostics", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 20: Events API
    # ============================================================
    print("\n=== TEST 20: Events API ===")
    try:
        r = S.get(f"{BASE}/api/events", headers=H)
        if r.status_code == 200:
            ok("Events list OK")
            data = r.json()
            events = data if isinstance(data, list) else data.get("events", [])
            if events:
                eid = events[0].get("id", events[0].get("event_id"))
                if eid:
                    r2 = S.get(f"{BASE}/api/events/{eid}", headers=H)
                    if r2.status_code == 200:
                        ok(f"Event detail OK (id={eid})")
                    else:
                        bug("event_detail", f"Event {eid}: {r2.status_code}")
        elif r.status_code == 404:
            ok("Events API not implemented")
        else:
            bug("events_list", f"Events list: {r.status_code}")

    except Exception:
        bug("events", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 21: Registry Deep APIs
    # ============================================================
    print("\n=== TEST 21: Registry Deep APIs ===")
    try:
        # Registry operations
        for name, path in [
            ("stats", "/api/registry/stats"),
            ("validate", "/api/validate"),
            ("graph", "/api/graph"),
            ("runs", "/api/runs"),
            ("agent-types", "/api/agent-types"),
            ("changelog", "/api/changelog"),
            ("changelog-stats", "/api/changelog/stats"),
        ]:
            r = S.get(f"{BASE}{path}", headers=H)
            if r.status_code == 200:
                ok(f"Registry {name} OK")
            elif r.status_code == 404:
                ok(f"Registry {name} not implemented")
            else:
                bug(f"reg_{name}", f"Registry {name}: {r.status_code}")

        # Agent deep endpoints (on first available agent)
        agents_r = S.get(f"{BASE}/api/agents?enabled_only=true", headers=H)
        if agents_r.status_code == 200:
            agents = agents_r.json()
            agent_list = agents if isinstance(agents, list) else agents.get("agents", [])
            if agent_list:
                aid = agent_list[0].get("agent_id")
                for name, path in [
                    ("tools", f"/api/agents/{aid}/tools"),
                    ("effective-tools", f"/api/agents/{aid}/effective-tools"),
                    ("inputs", f"/api/agents/{aid}/inputs"),
                    ("outputs", f"/api/agents/{aid}/outputs"),
                    ("effective-io", f"/api/agents/{aid}/effective-io"),
                    ("template-vars", f"/api/agents/{aid}/template-variables"),
                    ("performance", f"/api/agents/{aid}/performance"),
                    ("history", f"/api/agents/{aid}/history"),
                ]:
                    r2 = S.get(f"{BASE}{path}", headers=H)
                    if r2.status_code == 200:
                        ok(f"Agent {name} OK")
                    elif r2.status_code == 404:
                        ok(f"Agent {name} not implemented")
                    else:
                        bug(f"agent_{name}", f"Agent {name}: {r2.status_code}")

        # Tool deep endpoints
        tools_r = S.get(f"{BASE}/api/tools?enabled_only=true", headers=H)
        if tools_r.status_code == 200:
            tools = tools_r.json()
            tool_list = tools if isinstance(tools, list) else tools.get("tools", [])
            if tool_list:
                tid = tool_list[0].get("tool_id")
                for name, path in [
                    ("agents", f"/api/tools/{tid}/agents"),
                    ("history", f"/api/tools/{tid}/history"),
                ]:
                    r2 = S.get(f"{BASE}{path}", headers=H)
                    if r2.status_code == 200:
                        ok(f"Tool {name} OK")
                    elif r2.status_code == 404:
                        ok(f"Tool {name} not implemented")
                    else:
                        bug(f"tool_{name}", f"Tool {name}: {r2.status_code}")
                # Whichever tool is first: Core ships none of its own, so the check is
                # that a tool which declares functions also declares what they return.
                detail = S.get(f"{BASE}/api/tools/{tid}", headers=H)
                if detail.status_code == 200:
                    body = detail.json()
                    interface = body.get("interface") if isinstance(body, dict) else {}
                    functions = interface.get("functions") if isinstance(interface, dict) else []
                    if not functions:
                        ok("Tool action contracts skipped (the tool declares no functions)")
                    elif any((fn.get("returns") or {}) for fn in functions):
                        ok("Tool action contracts OK")
                    else:
                        bug(
                            "tool_action_contracts",
                            f"Tool {tid} does not expose function return contracts",
                        )
                else:
                    bug("tool_detail", f"Tool detail: {detail.status_code}")

        # Workflow deep endpoints
        wf_r = S.get(f"{BASE}/api/workflows", headers=H)
        if wf_r.status_code == 200:
            wfs = wf_r.json()
            wf_list = wfs if isinstance(wfs, list) else wfs.get("workflows", [])
            enabled_wfs = [w for w in wf_list if w.get("enabled")]
            if enabled_wfs:
                wid = enabled_wfs[0].get("workflow_id")
                for name, path in [
                    ("dag", f"/api/workflows/{wid}/dag"),
                    ("history", f"/api/workflows/{wid}/history"),
                    ("reactions", f"/api/workflows/{wid}/reactions"),
                ]:
                    r2 = S.get(f"{BASE}{path}", headers=H)
                    if r2.status_code == 200:
                        ok(f"Workflow {name} OK")
                    elif r2.status_code == 404:
                        ok(f"Workflow {name} not implemented")
                    else:
                        bug(f"wf_{name}", f"Workflow {name}: {r2.status_code}")

    except Exception:
        bug("registry_deep", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 22: Extension APIs
    # ============================================================
    print("\n=== TEST 22: Extension APIs ===")
    try:
        r = S.get(f"{BASE}/api/extensions", headers=H)
        if r.status_code == 200:
            ok("Extensions list OK")
            exts = (r.json() or {}).get("extensions", [])
            if exts:
                ext_id = (
                    exts[0].get("extension_id") or exts[0].get("extension_key") or exts[0].get("id")
                )
                if ext_id:
                    for name, path in [
                        ("health", f"/api/extensions/{ext_id}/health"),
                        ("validate", f"/api/extensions/{ext_id}/validate"),
                        ("preview", f"/api/extensions/{ext_id}/preview"),
                        ("compiled-manifest", f"/api/extensions/{ext_id}/compiled-manifest"),
                    ]:
                        if name == "validate":
                            r2 = S.post(f"{BASE}{path}", json={}, headers=H)
                        else:
                            r2 = S.get(f"{BASE}{path}", headers=H)
                        if r2.status_code in (200, 400):
                            ok(f"Extension {name} OK")
                        elif r2.status_code == 404:
                            ok(f"Extension {name} not implemented")
                        else:
                            bug(f"ext_{name}", f"Extension {name}: {r2.status_code}")
        elif r.status_code == 404:
            ok("Extensions not implemented")
        else:
            bug("ext_list", f"Extensions list: {r.status_code}")

    except Exception:
        bug("extensions", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 23: Additional Page Smoke Tests
    # ============================================================
    print("\n=== TEST 23: Additional Page Smoke Tests ===")
    try:
        extra_pages = [
            ("/settings/account", "Account"),
            ("/settings/users", "Users Admin"),
            ("/monitoring?tab=events", "Events"),
            ("/dashboard", "Dashboard"),
        ]
        for path, name in extra_pages:
            r = S.get(f"{BASE}{path}", headers=H)
            if r.status_code == 200:
                ok(f"{name} page OK")
            elif r.status_code == 404:
                ok(f"{name} page not implemented")
            elif r.status_code in (302, 303):
                ok(f"{name} page redirects")
            else:
                bug(f"page_{name.lower()}", f"{name} page: {r.status_code}")

    except Exception:
        bug("extra_pages", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 24: Metrics & Monitoring
    # ============================================================
    print("\n=== TEST 24: Metrics & Monitoring ===")
    try:
        r = S.get(f"{BASE}/metrics")
        if r.status_code in (200, 401, 403):
            ok(f"Metrics endpoint: {r.status_code}")
        elif r.status_code == 404:
            ok("Metrics not implemented")
        else:
            bug("metrics", f"Metrics: {r.status_code}")

        # Watchdog scan
        r = S.get(f"{BASE}/api/watchdog/jobs/scan", headers=H)
        if r.status_code == 200:
            ok("Watchdog scan OK")
        elif r.status_code == 404:
            ok("Watchdog not implemented")
        else:
            bug("watchdog", f"Watchdog: {r.status_code}")

    except Exception:
        bug("monitoring", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 25: Job Log Operations
    # ============================================================
    print("\n=== TEST 25: Job Log Operations ===")
    try:
        r = S.get(f"{BASE}/api/jobs?limit=5", headers=H)
        data = r.json()
        jobs = _extract_jobs_rows(data)
        if jobs:
            jid = jobs[0].get("id", jobs[0].get("job_id"))

            # Single message detail
            msgs_r = S.get(f"{BASE}/api/jobs/{jid}/messages", headers=H)
            if msgs_r.status_code == 200:
                msg_data = msgs_r.json()
                messages = msg_data if isinstance(msg_data, list) else msg_data.get("messages", [])
                if messages:
                    mid = messages[0].get("id") or messages[0].get("message_id")
                    if mid:
                        r2 = S.get(f"{BASE}/api/messages/{mid}", headers=H)
                        if r2.status_code == 200:
                            ok(f"Message detail OK (id={mid})")
                        else:
                            bug("msg_detail", f"Message detail: {r2.status_code}")

            # Acknowledge / unacknowledge
            r2 = S.post(f"{BASE}/api/jobs/{jid}/acknowledge", json={}, headers=H)
            if r2.status_code in (200, 204, 409):
                ok("Acknowledge OK")
            elif r2.status_code == 404:
                ok("Acknowledge not implemented")
            else:
                bug("ack", f"Acknowledge: {r2.status_code}")

            r2 = S.post(f"{BASE}/api/jobs/{jid}/unacknowledge", json={}, headers=H)
            if r2.status_code in (200, 204, 409):
                ok("Unacknowledge OK")
            elif r2.status_code == 404:
                ok("Unacknowledge not implemented")
            else:
                bug("unack", f"Unacknowledge: {r2.status_code}")
        else:
            ok("No jobs for log operations")

    except Exception:
        bug("log_ops", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 26: Webhook & Integration Endpoints
    # ============================================================
    print("\n=== TEST 26: Webhook & Integration Endpoints ===")
    try:
        # Extension webhook (should handle unknown key)
        r = S.post(f"{BASE}/webhook/extensions/nonexistent_key", json={"event": "test"}, headers=H)
        if r.status_code in (400, 401, 404):
            ok(f"Extension webhook (unknown key): {r.status_code}")
        else:
            bug("webhook_ext", f"Extension webhook: {r.status_code}")

        # Reaction ingestion
        r = S.post(
            f"{BASE}/api/reactions/ingest",
            json={
                "source": "smoke_test",
                "type": "positive",
                "content": "Test",
            },
            headers=H,
        )
        if r.status_code in (200, 201, 400, 401, 404):
            ok(f"Reaction ingestion: {r.status_code}")
        else:
            bug("reaction", f"Reaction ingestion: {r.status_code}")

    except Exception:
        bug("webhooks", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # FINAL CLEANUP
    # ============================================================
    print("\n=== FINAL CLEANUP ===")
    active = get_active_jobs()
    if active:
        print(f"Cancelling {len(active)} remaining active jobs")
        for j in active:
            cancel(j["id"])
    else:
        print("No active jobs to clean up")

    # ============================================================
    # SUMMARY
    # ============================================================
    print("\n" + "=" * 60)
    print("SMOKE TEST SUMMARY")
    print("=" * 60)
    if BUGS:
        print(f"\n🐛 BUGS FOUND: {len(BUGS)}")
        for i, (test, msg) in enumerate(BUGS, 1):
            print(f"  {i}. [{test}] {msg}")
        sys.exit(1)
    else:
        print("\n✅ ALL TESTS PASSED — no bugs found")
        sys.exit(0)
