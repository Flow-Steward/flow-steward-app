import { fetchEventDetail, fetchEventsList, redriveEvent } from "@/pages/monitoring/shared/events/api";

describe("events api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes object-shaped list payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          events: [
            {
              event_id: "evt-1",
              event_type: "job.failed",
              event_type_label: "Job Failed",
              status: "dead_lettered",
              status_label: "Dead-lettered",
            },
          ],
          total: 1,
          page: 2,
          per_page: 25,
          facets: {
            statuses: [{ value: "dead_lettered", label: "Dead-lettered", count: 1, tone: "red" }],
          },
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await expect(
      fetchEventsList({
        workflowId: "",
        eventFamily: "",
        eventCategory: "",
        eventType: "",
        status: "",
        fromDate: "",
        toDate: "",
        page: 2,
        perPage: 25,
      }),
    ).resolves.toEqual({
      events: [
        {
          event_id: "evt-1",
          event_type: "job.failed",
          event_type_label: "Job Failed",
          status: "dead_lettered",
          status_label: "Dead-lettered",
        },
      ],
      total: 1,
      page: 2,
      per_page: 25,
      facets: {
        statuses: [{ value: "dead_lettered", label: "Dead-lettered", count: 1, tone: "red" }],
      },
    });
  });

  it("normalizes array-shaped list payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([{ event_id: "evt-9" }]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(
      fetchEventsList({
        workflowId: "",
        eventFamily: "",
        eventCategory: "",
        eventType: "",
        status: "",
        fromDate: "",
        toDate: "",
        page: 1,
        perPage: 50,
      }),
    ).resolves.toEqual({
      events: [{ event_id: "evt-9" }],
      total: 1,
      page: 1,
      per_page: 50,
    });
  });

  it("passes workflow and hierarchical event filters to the events endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          events: [],
          total: 0,
          page: 1,
          per_page: 25,
          facets: {
            event_families: [{ value: "workflow", count: 3 }],
          },
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await fetchEventsList({
      workflowId: "weekly_report",
      eventFamily: "workflow",
      eventCategory: "ingress",
      eventType: "workflow.ingress.rejected",
      status: "recorded",
      fromDate: "2026-05-01",
      toDate: "2026-05-12",
      page: 1,
      perPage: 25,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/events?event_type=workflow.ingress.rejected&workflow_id=weekly_report&event_family=workflow&event_category=ingress&status=recorded&from_date=2026-05-01&to_date=2026-05-12&page=1&per_page=25",
    );
  });

  it("calls detail and redrive endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ event_id: "evt-42", status: "failed" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "queued", redriven_dispatches: ["dispatch-1"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchEventDetail("evt-42")).resolves.toEqual({
      event_id: "evt-42",
      status: "failed",
    });
    await expect(redriveEvent("evt-42")).resolves.toEqual({
      status: "queued",
      redriven_dispatches: ["dispatch-1"],
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/events/evt-42");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/events/evt-42/redrive");
    const secondInit = fetchMock.mock.calls[1]?.[1];
    expect(secondInit?.method).toBe("POST");
  });
});
