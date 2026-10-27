import { fetchJson } from "@/lib/http/client";
import { replaceAgentRelationships } from "./api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

describe("agent relationship API", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue([]);
  });

  it("replaces skills and tools atomically with an exact expected snapshot", async () => {
    mockedFetchJson.mockResolvedValue({ status: "updated" });
    await expect(
      replaceAgentRelationships(
        "agent/one",
        { skills: ["old", "keep"], tools: ["tool-old"] },
        { skills: ["keep", "new", "new"], tools: ["tool-new", "tool-new"] },
      ),
    ).resolves.toEqual({ status: "updated" });

    expect(mockedFetchJson.mock.calls).toEqual([
      ["/api/agents/agent%2Fone/relationships", {
        method: "PUT",
        body: JSON.stringify({
          expected_skills: ["old", "keep"],
          expected_tools: ["tool-old"],
          skills: ["keep", "new"],
          tools: ["tool-new"],
        }),
      }],
    ]);
  });

  it("reports no_change without issuing requests", async () => {
    await expect(
      replaceAgentRelationships(
        "agent",
        { skills: ["one"], tools: ["tool"] },
        { skills: ["one", "one"], tools: ["tool"] },
      ),
    ).resolves.toEqual({ status: "no_change" });
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });
});
