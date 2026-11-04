import { fetchJson } from "@/lib/http/client";
import { fetchMcpConnector, fetchMcpConnectorOAuthStatus } from "@/pages/registry/mcp/api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

describe("MCP connector API", () => {
  beforeEach(() => mockedFetchJson.mockReset());

  it("normalizes the exact OAuth redirect URI from status", async () => {
    mockedFetchJson.mockResolvedValue({
      status: "auth_required",
      redirect_uri: " https://flow.example.test/api/integrations/oauth/callback ",
    });

    const result = await fetchMcpConnectorOAuthStatus("mcp-1", "project-1");

    expect(result.redirect_uri).toBe("https://flow.example.test/api/integrations/oauth/callback");
  });

  it("keeps the stored-secret header choices: names, secret ids and prefixes", async () => {
    mockedFetchJson.mockResolvedValue({
      connector_id: "mcp-1",
      header_secret_choices: {
        Authorization: { secret_id: "s1", prefix: "Bearer " },
        "X-Broken": { prefix: "no id" },
        "X-Not-An-Object": "s2",
      },
    });

    const connector = await fetchMcpConnector("mcp-1", "project-1");

    expect(connector.header_secret_choices).toEqual({ Authorization: { secret_id: "s1", prefix: "Bearer " } });
  });
});
