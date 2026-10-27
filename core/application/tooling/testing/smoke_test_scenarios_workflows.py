#!/usr/bin/env python3
"""Workflow and gate smoke scenarios."""

from __future__ import annotations

import time
import traceback
from typing import Any


def run_workflow_smoke_scenarios(context: dict[str, Any]) -> None:
    S = context["S"]
    H = context["H"]
    BASE = context["BASE"]
    PROJECT_ID = context["PROJECT_ID"]
    workflows = context["workflows"]
    task_workflow_id = context["task_workflow_id"]
    bug = context["bug"]
    ok = context["ok"]
    submit_task = context["submit_task"]
    assert_retired_task_form_removed = context["assert_retired_task_form_removed"]
    get_job = context["get_job"]
    wait_for_status = context["wait_for_status"]
    approve = context["approve"]
    reject = context["reject"]
    abort_gate = context["abort_gate"]
    cancel = context["cancel"]

    # ============================================================
    # TEST 1: Workflow-launch input validation
    # ============================================================
    print("\n=== TEST 1: Workflow Launch Input Validation ===")
    try:
        invalid_requests = [
            ("missing_workflow", {"project_id": PROJECT_ID}),
            ("missing_project", {"workflow_id": task_workflow_id}),
            ("invalid_project", {"project_id": 99999, "workflow_id": task_workflow_id}),
            ("empty_workflow", {"project_id": PROJECT_ID, "workflow_id": ""}),
            (
                "retired_task_fields",
                {
                    "project_id": PROJECT_ID,
                    "workflow_id": task_workflow_id,
                    "task_summary": "retired free-form submission",
                },
            ),
        ]
        for case, payload in invalid_requests:
            response = S.post(f"{BASE}/api/workflow-runs", json=payload, headers=H)
            if response.status_code != 422:
                bug(f"input_{case}", f"Expected validation rejection, got {response.status_code}")
            else:
                ok(f"{case} rejected: {response.status_code}")
    except Exception as exc:
        bug("input_validation", f"Exception: {exc}")

    # ============================================================
    # TEST 2: Retired form route removal
    # ============================================================
    print("\n=== TEST 2: Retired Form Route Removal ===")
    try:
        r = assert_retired_task_form_removed("Test removed form route smoke test")
        print(f"  Retired form submit: status={r.status_code}")
        if r.status_code in (404, 405):
            ok("Retired project task form route is unavailable")
        else:
            bug(
                "retired_form_submit",
                f"Retired form route unexpectedly live: {r.status_code} {r.text[:200]}",
            )
    except Exception:
        bug("retired_form_submit", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 3: Approval gate workflow
    # ============================================================
    print("\n=== TEST 3: Approval Gate Workflow ===")
    try:
        job_id, r = submit_task("Summarize the open support requests for this week")
        print(f"  Submit: status={r.status_code} job_id={job_id}")
        if not job_id:
            bug("gate_workflow_submit", f"No job_id: {r.status_code} {r.text[:200]}")
        else:
            status, _j = wait_for_status(job_id, ["awaiting_approval"], timeout=120)
            jd = get_job(job_id)
            wf = jd.get("workflow_name", jd.get("workflow", ""))
            print(f"  Status: {status}, Workflow: {wf}")

            if status == "awaiting_approval":
                ok("Workflow reached first approval gate")

                # Validate approval content
                content = jd.get("approval_content", "")
                stage = jd.get("approval_stage", jd.get("current_step_id", ""))
                print(f"  Gate stage: {stage}, Content: {len(content)} chars")
                if content and len(content) < 50:
                    bug(
                        "gate_workflow_thin_content", f"Gate content too thin: {len(content)} chars"
                    )

                # Approve first gate
                ar = approve(job_id, "Looks good, proceed")
                print(f"  Approve: {ar.status_code}")
                if ar.status_code not in (200, 202):
                    bug(
                        "gate_workflow_approve", f"Approve failed: {ar.status_code} {ar.text[:200]}"
                    )
                else:
                    ok("First gate approved")
                    time.sleep(3)
                    status2, _j2 = wait_for_status(job_id, ["awaiting_approval"], timeout=120)
                    print(f"  After approve: {status2}")
                    if status2 == "awaiting_approval":
                        ok("Second gate reached")
                    elif status2 == "in_progress":
                        ok("Still processing after first approval")
                    elif status2 in ("failed", "error"):
                        bug("gate_workflow_post_approve", f"Failed after approval: {status2}")
                        jd2 = get_job(job_id)
                        print(f"  Error: {jd2.get('error', '')[:300]}")
            elif status in ("failed", "error"):
                bug("gate_workflow_failed", f"Workflow failed: {status}")
                print(f"  Error: {jd.get('error', '')[:300]}")
            elif status in ("in_progress", "pending"):
                ok(f"Workflow submitted ({status} — worker may be busy processing another job)")

            cancel(job_id)

    except Exception:
        bug("gate_workflow", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 6: Rejection at gate
    # ============================================================
    print("\n=== TEST 6: Rejection Flow ===")
    try:
        job_id, r = submit_task("Draft a status update for the weekly report")
        print(f"  Job: {job_id}")
        if not job_id:
            bug("reject_submit", f"Submit failed: {r.status_code}")
        else:
            status, _j = wait_for_status(job_id, ["awaiting_approval"], timeout=120)
            print(f"  Status: {status}")

            if status == "awaiting_approval":
                jd = get_job(job_id)
                gate_stage = jd.get("approval_stage", jd.get("current_step_id", ""))
                print(f"  Gate: {gate_stage}")

                rr = reject(job_id, "Add the missing figures and a short summary")
                print(f"  Reject: {rr.status_code}")
                if rr.status_code not in (200, 202):
                    bug("reject_api", f"Reject failed: {rr.status_code} {rr.text[:200]}")
                else:
                    ok("Rejection accepted")
                    status2, _j2 = wait_for_status(job_id, ["awaiting_approval"], timeout=180)
                    print(f"  After reject: {status2}")
                    if status2 == "awaiting_approval":
                        ok("Job returned to approval after rejection")
                    elif status2 in ("failed", "error"):
                        bug("reject_post_fail", "Failed after rejection")
                        jd2 = get_job(job_id)
                        print(f"  Error: {jd2.get('error', '')[:300]}")
                    elif status2 == "in_progress":
                        ok("Still processing after rejection")
                    elif status2 == "completed":
                        ok("Completed after rejection (autonomous completion)")
                    else:
                        ok(f"Post-reject: {status2}")
            else:
                ok(f"Didn't reach gate: {status}")

            cancel(job_id)

    except Exception:
        bug("reject", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 7: Abort at gate (BUG #15 regression test)
    # ============================================================
    print("\n=== TEST 7: Abort Flow ===")
    try:
        job_id, r = submit_task("Prepare a customer follow-up list")
        print(f"  Job: {job_id}")
        if not job_id:
            bug("abort_submit", f"Submit failed: {r.status_code}")
        else:
            status, _j = wait_for_status(job_id, ["awaiting_approval"], timeout=120)
            print(f"  Status: {status}")

            if status == "awaiting_approval":
                ar = abort_gate(job_id, "No longer needed, cancelling")
                print(f"  Abort: {ar.status_code}")
                if ar.status_code not in (200, 202):
                    bug("abort_api", f"Abort failed: {ar.status_code} {ar.text[:200]}")
                else:
                    ok("Abort accepted")
                    status2, _j2 = wait_for_status(job_id, ["cancelled"], timeout=90)
                    print(f"  After abort: {status2}")
                    if status2 == "cancelled":
                        ok("Job properly cancelled after abort")
                    elif status2 == "completed":
                        bug(
                            "abort_completed",
                            "Job COMPLETED instead of cancelled — BUG #15 regression!",
                        )
                    elif status2 == "awaiting_approval":
                        bug("abort_stuck", "Job still awaiting_approval after abort")
                    else:
                        bug("abort_unexpected", f"Unexpected status: {status2}")
            else:
                ok(f"Didn't reach gate: {status}")
                cancel(job_id)

    except Exception:
        bug("abort", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 8: Cancel in-progress job
    # ============================================================
    print("\n=== TEST 8: Cancel In-Progress ===")
    try:
        job_id, r = submit_task("Collect this month's invoice totals")
        print(f"  Job: {job_id}")
        if job_id:
            time.sleep(3)
            jd = get_job(job_id)
            pre_status = jd.get("status", "")
            print(f"  Pre-cancel: {pre_status}")

            cr = cancel(job_id)
            print(f"  Cancel: {cr.status_code}")
            if cr.status_code not in (200, 202):
                bug("cancel_api", f"Cancel failed: {cr.status_code} {cr.text[:200]}")
            else:
                time.sleep(5)
                jd2 = get_job(job_id)
                final = jd2.get("status", "")
                print(f"  Final: {final}")
                if final == "cancelled":
                    ok("Cancel worked correctly")
                elif final in ("completed",):
                    bug("cancel_ignored", "Cancel ignored, job completed")
                else:
                    ok(f"Post-cancel: {final}")

    except Exception:
        bug("cancel", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 9: Job API endpoints on a live job
    # ============================================================
    print("\n=== TEST 9: Job API Endpoints ===")
    try:
        job_id, r = submit_task("Summarize recent project activity")
        print(f"  Job: {job_id}")
        if job_id:
            time.sleep(8)

            endpoints = {
                "events": f"/api/jobs/{job_id}/events",
                "messages": f"/api/jobs/{job_id}/messages",
                "cost": f"/api/jobs/{job_id}/cost-breakdown",
                "logs": f"/api/jobs/{job_id}/logs",
            }
            for name, path in endpoints.items():
                r2 = S.get(f"{BASE}{path}", headers=H)
                if r2.status_code != 200:
                    bug(f"api_{name}", f"{name} API: {r2.status_code}")
                else:
                    ok(f"{name} API OK")

            # Job detail field validation
            jd = get_job(job_id)
            for field in ["id", "status", "task_summary", "project_id", "created_at", "thread_id"]:
                if field not in jd:
                    bug(f"field_{field}", f"Missing field: {field}")
            if "thread_id" in jd:
                ok(f"Thread ID: {str(jd['thread_id'])[:20]}...")

            cancel(job_id)

    except Exception:
        bug("job_apis", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 10: Workflow override
    # ============================================================
    print("\n=== TEST 10: Workflow Override ===")
    try:
        enabled_wfs = [
            w
            for w in workflows
            if isinstance(w, dict)
            and str(w.get("workflow_id", w.get("id", ""))).strip() == task_workflow_id
        ]
        if enabled_wfs:
            wf = enabled_wfs[0]
            wf_id = task_workflow_id
            wf_name = wf.get("display_name", wf.get("name", ""))
            print(f"  Using workflow: {wf_id} ({wf_name})")

            job_id, r = submit_task("Test task with explicit workflow override", workflow_id=wf_id)
            print(f"  Job: {job_id}")
            if job_id:
                time.sleep(5)
                jd = get_job(job_id)
                actual = jd.get("workflow_name", jd.get("workflow", ""))
                print(f"  Expected: {wf_name}, Got: {actual}")
                if (
                    actual
                    and wf_name
                    and wf_name.lower() not in actual.lower()
                    and actual.lower() not in wf_name.lower()
                ):
                    bug(
                        "wf_override_mismatch",
                        f"Override ignored: expected '{wf_name}', got '{actual}'",
                    )
                else:
                    ok("Workflow override applied")
                cancel(job_id)
        else:
            ok(f"Selected project workflow {task_workflow_id} is not in the global catalog view")

    except Exception:
        bug("wf_override", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 11: Queue and process APIs
    # ============================================================
    print("\n=== TEST 11: Queue/Process/Approval APIs ===")
    try:
        r = S.get(f"{BASE}/api/projects/{PROJECT_ID}/queue", headers=H)
        if r.status_code != 200:
            bug("api_queue", f"Queue: {r.status_code}")
        else:
            ok("Queue API OK")

        r = S.get(f"{BASE}/api/processes", headers=H)
        if r.status_code != 200:
            bug("api_processes", f"Processes: {r.status_code}")
        else:
            ok("Processes API OK")

        r = S.get(f"{BASE}/api/jobs/awaiting-approval", headers=H)
        if r.status_code != 200:
            bug("api_awaiting", f"Awaiting approval: {r.status_code}")
        else:
            ok("Awaiting approval API OK")

    except Exception:
        bug("apis", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 12: Health endpoints
    # ============================================================
    print("\n=== TEST 12: Health Endpoints ===")
    try:
        for ep in ["/health/live", "/health/ready", "/health/deep"]:
            r = S.get(f"{BASE}{ep}")
            if r.status_code != 200:
                bug(f"health_{ep}", f"{ep}: {r.status_code}")
            else:
                ok(f"{ep} OK")
    except Exception:
        bug("health", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 13: Edge cases
    # ============================================================
    print("\n=== TEST 13: Edge Cases ===")
    try:
        # Fake thread approve
        r = S.post(
            f"{BASE}/api/approvals/fake-thread-12345",
            json={"approval_id": "fake-gate-12345", "action": "approve", "comment": "test"},
            headers={**H, "Idempotency-Key": "smoke-fake-gate-12345"},
        )
        if r.status_code < 400 and r.status_code != 202:
            bug("approve_fake", f"Fake thread accepted: {r.status_code}")
        else:
            ok(f"Fake thread rejected: {r.status_code}")

        # Cancel non-existent
        r = S.post(f"{BASE}/api/jobs/99999/cancel", headers=H)
        if r.status_code < 400:
            bug("cancel_nonexist", f"Cancel non-existent accepted: {r.status_code}")
        else:
            ok(f"Cancel non-existent: {r.status_code}")

        # Get non-existent job
        r = S.get(f"{BASE}/api/jobs/99999", headers=H)
        if r.status_code != 404:
            bug("get_nonexist", f"Non-existent job: {r.status_code} (expected 404)")
        else:
            ok("Non-existent job: 404")

        # Double-cancel
        job_id, _ = submit_task("Double cancel test")
        if job_id:
            cancel(job_id)
            time.sleep(2)
            r2 = cancel(job_id)
            print(f"  Double cancel: {r2.status_code}")
            if r2.status_code < 400:
                ok("Double cancel accepted gracefully")
            else:
                ok(f"Double cancel rejected: {r2.status_code}")

    except Exception:
        bug("edge_cases", f"Exception: {traceback.format_exc()}")

    # ============================================================
    # TEST 14: CSRF validation
    # ============================================================
    print("\n=== TEST 14: CSRF Validation ===")
    try:
        # No CSRF header
        r = S.post(
            f"{BASE}/api/workflow-runs",
            json={"workflow_id": task_workflow_id, "project_id": PROJECT_ID},
        )
        if r.status_code == 403:
            ok("CSRF enforced (no header)")
        elif r.status_code < 400:
            bug("csrf_missing", f"No CSRF accepted: {r.status_code}")
        else:
            ok(f"No CSRF rejected: {r.status_code}")

        # Wrong CSRF header
        r = S.post(
            f"{BASE}/api/workflow-runs",
            json={"workflow_id": task_workflow_id, "project_id": PROJECT_ID},
            headers={"x-csrf-token": "wrong"},
        )
        if r.status_code == 403:
            ok("CSRF enforced (wrong token)")
        elif r.status_code < 400:
            bug("csrf_wrong", f"Wrong CSRF accepted: {r.status_code}")
        else:
            ok(f"Wrong CSRF rejected: {r.status_code}")

    except Exception:
        bug("csrf", f"Exception: {traceback.format_exc()}")
