import { fetchJson } from "@/lib/http/client";
import { fetchMcpConnectorOAuthStatus } from "@/pages/registry/mcp/api";

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
});
