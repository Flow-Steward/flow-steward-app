import {
  createProjectScheduledJob,
  deleteProjectScheduledJob,
  fetchProjectScheduledJobs,
  previewProjectCron,
  runProjectScheduledJobNow,
  toggleProjectScheduledJob,
} from "@/pages/projects/scheduled-jobs/api";

describe("project scheduled jobs api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reads the canonical scheduled jobs payload", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          project_id: "7",
          timezone_context: { display_timezone: "Europe/Vilnius" },
          jobs: [{ id: 1, workflow_id: "wf_triage" }],
          runs: [{ id: 10, scheduled_job_id: 1, status: "enqueued" }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          jobs: [{ id: 2 }],
          runs: [{ id: 20 }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(fetchProjectScheduledJobs("7", 50)).resolves.toEqual({
      project_id: "7",
      timezone_context: { display_timezone: "Europe/Vilnius" },
      jobs: [{ id: 1, workflow_id: "wf_triage" }],
      runs: [{ id: 10, scheduled_job_id: 1, status: "enqueued" }],
    });

    await expect(fetchProjectScheduledJobs("9", 20)).resolves.toEqual({
      project_id: "9",
      timezone_context: {},
      jobs: [{ id: 2 }],
      runs: [{ id: 20 }],
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/7/scheduled-jobs?limit_runs=50");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/projects/9/scheduled-jobs?limit_runs=20");
  });

  it("calls scheduled job mutation endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const okResponse = () =>
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    fetchMock
      .mockResolvedValueOnce(okResponse())
      .mockResolvedValueOnce(okResponse())
      .mockResolvedValueOnce(okResponse())
      .mockResolvedValueOnce(okResponse())
      .mockResolvedValueOnce(okResponse());

    await createProjectScheduledJob("11", {
      workflow_id: "wf_triage",
      cron_expr: "0 9 * * *",
      enabled: true,
    });
    await toggleProjectScheduledJob("11", "3", false);
    await runProjectScheduledJobNow("11", "3");
    await deleteProjectScheduledJob("11", "3");
    await previewProjectCron("11", "*/15 * * * *", 3);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/11/scheduled-jobs");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/projects/11/scheduled-jobs/3/toggle");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/projects/11/scheduled-jobs/3/run-now");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/projects/11/scheduled-jobs/3");
    expect(fetchMock.mock.calls[4]?.[0]).toBe("/api/projects/11/cron-preview?cron_expr=*%2F15+*+*+*+*&count=3");

    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PATCH");
    expect(fetchMock.mock.calls[2]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe("DELETE");
    expect(fetchMock.mock.calls[4]?.[1]?.method).toBeUndefined();
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      workflow_id: "wf_triage", cron_expr: "0 9 * * *", enabled: true,
    });
  });
});
