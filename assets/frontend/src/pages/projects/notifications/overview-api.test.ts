import { fetchProjectNotificationOverview } from "@/pages/projects/notifications/overview-api";

function respond(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("project notification overview api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reads the overview endpoint and never shows an unknown state as working", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(respond({
      project_id: "p 1",
      entries: [
        { entry_id: "subscription:a", kind: "subscription", name: "A", state: "something_new", settings: { kind: "subscription", route_key: "a" } },
        { name: "no id is dropped" },
      ],
    }));

    const entries = await fetchProjectNotificationOverview("p 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/p%201/notification-overview");
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ kind: "subscription", state: "setup_problem", connection: null });
    expect(entries[0].delivery).toMatchObject({ method: "matching_subscriptions", decided_at_run_time: false, recipients: [] });
  });

  it("refuses an answer without entries instead of showing it as empty", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(respond({ project_id: "p1" }));

    await expect(fetchProjectNotificationOverview("p1")).rejects.toThrow("without entries");
  });
});
