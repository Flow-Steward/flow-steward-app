import {
  fetchChangelogDiff,
  fetchChangelogHistory,
  fetchChangelogList,
  fetchRegistryStats,
  fetchRegistryValidation,
  fetchWorkflowCurrentRevision,
  restoreChangelogEntry,
} from "@/pages/registry/changelog/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

describe("registry changelog api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([undefined, 0, -3, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "workflow_restore_requires_loaded_revision: %s sends nothing",
    async (expectedRevision) => {
      const fetchMock = vi.spyOn(globalThis, "fetch");
      await expect(restoreChangelogEntry({
        entityType: "workflow", entityId: "wf", entryId: "old", expectedRevision,
      })).rejects.toThrow(/revision that was shown/u);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("workflow_restore_posts_exact_loaded_revision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "restored" }), {
        status: 200, headers: { "content-type": "application/json" },
      }),
    );
    await restoreChangelogEntry({ entityType: "workflow", entityId: "wf", entryId: "12", expectedRevision: 4 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/workflows/wf/restore");
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      entry_id: "12", expected_revision: 4,
    });
  });

  it("answers a stale workflow restore with its 409, without re-reading the revision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Workflow changed since revision 4" }), {
        status: 409, headers: { "content-type": "application/json" },
      }),
    );
    await expect(restoreChangelogEntry({
      entityType: "workflow", entityId: "wf", entryId: "old", expectedRevision: 4,
    })).rejects.toMatchObject({ status: 409 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/workflows/wf/restore");
  });

  it("reads the workflow's current revision once, for the reader to restore against", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ workflow_id: "wf/1", revision: 4 }), {
        status: 200, headers: { "content-type": "application/json" },
      }),
    );
    await expect(fetchWorkflowCurrentRevision("wf/1")).resolves.toBe(4);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/workflows/wf%2F1");
  });

  it.each([{}, { revision: 0 }, { revision: "4" }, { revision: 2.5 }])(
    "refuses a current revision the workflow does not report: %j",
    async (payload) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
        new Response(JSON.stringify(payload), {
          status: 200, headers: { "content-type": "application/json" },
        }),
      );
      await expect(fetchWorkflowCurrentRevision("wf")).rejects.toThrow(/current revision/u);
    },
  );

  it("restores other entities through the generic changelog route", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "restored" }), {
        status: 200, headers: { "content-type": "application/json" },
      }),
    );
    await restoreChangelogEntry({ entityType: "tool", entityId: "t1", entryId: "e1" });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/changelog/tool/t1/restore");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ entry_id: "e1" });
  });

  it("normalizes array payloads from /api/changelog", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: 101,
            entity_type: "agent",
            entity_id: "writer",
          },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(
      fetchChangelogList({
        entityType: "agent",
        entityId: "writer",
        limit: 20,
      }),
    ).resolves.toEqual([
      {
        id: 101,
        entity_type: "agent",
        entity_id: "writer",
      },
    ]);

    const url = firstStringFromUnknownValue([fetchMock.mock.calls[0]?.[0], ""]);
    expect(url).toContain("/api/changelog?");
    expect(url).toContain("entity_type=agent");
    expect(url).toContain("entity_id=writer");
    expect(url).toContain("limit=20");
  });

  it("normalizes object payloads from /api/changelog", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          rows: [
            {
              id: 22,
              entity_type: "workflow",
              entity_id: "wf_demo",
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(
      fetchChangelogList({
        entityType: "",
        entityId: "",
        limit: 50,
      }),
    ).resolves.toEqual([
      {
        id: 22,
        entity_type: "workflow",
        entity_id: "wf_demo",
      },
    ]);
  });

  it("calls history, diff, stats, and validation endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify([{ id: 10 }, { id: 9 }]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ summary: { total: 3 }, changes: [{ path: "enabled", change: "changed" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ agents: 12, tools: 33 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ valid: true, errors: [], warnings: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchChangelogHistory("agent", "writer", 25)).resolves.toEqual([{ id: 10 }, { id: 9 }]);
    await expect(
      fetchChangelogDiff({
        entityType: "agent",
        entityId: "writer",
        fromEntryId: "9",
        toEntryId: "10",
      }),
    ).resolves.toEqual({
      summary: { total: 3 },
      changes: [{ path: "enabled", change: "changed" }],
    });
    await expect(fetchRegistryStats()).resolves.toEqual({ agents: 12, tools: 33 });
    await expect(fetchRegistryValidation()).resolves.toEqual({
      valid: true,
      errors: [],
      warnings: [],
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/changelog/agent/writer/history?limit=25");
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/changelog/agent/writer/diff?from_entry_id=9&to_entry_id=10",
    );
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/changelog/stats");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/changelog/validate");
  });
});
