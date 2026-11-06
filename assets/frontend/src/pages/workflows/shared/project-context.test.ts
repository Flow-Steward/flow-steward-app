import { fetchWorkflowLinkedProjects, findWorkflowLinkedProjectId } from "./project-context";

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}

describe("workflow project links", () => {
  let requests: string[] = [];
  let reply: (path: string) => Response = () => json({});

  beforeEach(() => {
    requests = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = new URL(String(input), "http://localhost").pathname;
      requests.push(path);
      return reply(path);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("finds the linked project with one read instead of one per project", async () => {
    reply = () => json({ workflow_id: "wine sync", projects: [{ id: "p-2", name: "Second" }, { id: "p-5", name: "Fifth" }] });

    await expect(findWorkflowLinkedProjectId("wine sync")).resolves.toBe("p-2");
    expect(requests).toEqual(["/api/workflows/wine%20sync/linked-projects"]);
  });

  it("lists every linked project in the server's order", async () => {
    reply = () => json({ workflow_id: "wf", projects: [{ id: "p-2", name: "Second" }, { id: "", name: "Broken" }, { id: "p-5", name: "Fifth" }] });

    await expect(fetchWorkflowLinkedProjects("wf")).resolves.toEqual([{ id: "p-2", name: "Second" }, { id: "p-5", name: "Fifth" }]);
    expect(requests).toEqual(["/api/workflows/wf/linked-projects"]);
  });

  it("answers no project when nothing links the workflow", async () => {
    reply = () => json({ workflow_id: "wf", projects: [] });

    await expect(findWorkflowLinkedProjectId("wf")).resolves.toBe("");
  });

  it("keeps a failed read an error rather than no links", async () => {
    reply = () => json({ detail: "Project bindings unavailable" }, 503);

    await expect(fetchWorkflowLinkedProjects("wf")).rejects.toThrow();
    await expect(findWorkflowLinkedProjectId("wf")).rejects.toThrow();
  });

  it("reads nothing for an empty workflow id", async () => {
    await expect(findWorkflowLinkedProjectId(" ")).resolves.toBe("");
    await expect(fetchWorkflowLinkedProjects("")).resolves.toEqual([]);
    expect(requests).toEqual([]);
  });
});
