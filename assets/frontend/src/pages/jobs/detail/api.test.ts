import {
  fetchJobApproval,
  fetchJobApprovalHistory,
  fetchJobDetail,
  fetchJobExecutionStepsPage,
} from "@/pages/jobs/detail/api";

describe("job detail api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("represents a successful no-active-approval response as absent", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ code: "approval_not_awaiting", error: "Job is not awaiting approval" }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchJobApproval("42")).resolves.toBeUndefined();
  });

  it("preserves fail-closed multiple-gate conflicts", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          code: "multiple_approval_gates_unsupported",
          error: "Multiple simultaneous approval gates are not supported",
        }),
        {
          status: 409,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await expect(fetchJobApproval("42")).rejects.toMatchObject({
      status: 409,
      payload: { code: "multiple_approval_gates_unsupported" },
    });
  });

  it("does not hide unrelated approval endpoint failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: "Unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchJobApproval("42")).rejects.toMatchObject({ status: 503 });
  });

  it("unwraps the v2 detail envelope without losing flat job metadata", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          kind: "detail",
          data: {
            id: "3d17bb17-0e29-4b2f-a671-f07728e96183",
            status: "completed",
            selected_workflow: "release_audit_gmail_acme_docs_drive_live_1788933013",
            started_at: "2026-09-09T15:05:01.821355+00:00",
            finished_at: "2026-09-09T15:05:14.527729+00:00",
            runtime: {
              selected_workflow: "release_audit_gmail_acme_docs_drive_live_1788933013",
            },
            execution_steps: [{ id: "start", status: "completed" }],
          },
          error: null,
          meta: { resource: "jobs" },
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await expect(fetchJobDetail("3d17bb17-0e29-4b2f-a671-f07728e96183")).resolves.toMatchObject({
      status: "completed",
      selected_workflow: "release_audit_gmail_acme_docs_drive_live_1788933013",
      started_at: "2026-09-09T15:05:01.821355+00:00",
      finished_at: "2026-09-09T15:05:14.527729+00:00",
    });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v2/jobs/3d17bb17-0e29-4b2f-a671-f07728e96183?compact=true&step_limit=0",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("loads an older execution-step page through its opaque cursor", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          kind: "list",
          data: {
            job_id: "42",
            steps: [{ id: "older-step" }],
            page: { has_more: true, next_cursor: "10:message-10" },
          },
          error: null,
          meta: { resource: "job-execution-steps" },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(fetchJobExecutionStepsPage("42", "60:message-60")).resolves.toMatchObject({
      steps: [{ id: "older-step" }],
      page: { next_cursor: "10:message-10" },
    });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v2/jobs/42/execution-steps?limit=50&cursor=60%3Amessage-60",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("loads durable approval history independently of the compact job summary", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: "42",
          approvals: [{ action: "reject", approved: false, comment: "Fix the mapping" }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(fetchJobApprovalHistory("42")).resolves.toMatchObject({
      approvals: [{ action: "reject", comment: "Fix the mapping" }],
    });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/jobs/42/approvals",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

});
