import { McpJsonImportError, parseMcpJson } from "@/pages/registry/mcp/mcp-json-import";

function errorCode(text: string): string {
  try {
    parseMcpJson(text);
  } catch (error) {
    return error instanceof McpJsonImportError ? error.code : "other";
  }
  return "none";
}

describe("parseMcpJson", () => {
  it("reads a Claude/Cursor mcpServers entry with an Authorization header as bearer", () => {
    const result = parseMcpJson(JSON.stringify({
      mcpServers: {
        github: {
          type: "http",
          url: "https://api.githubcopilot.com/mcp/",
          headers: { Authorization: "Bearer ghp-test" },
        },
      },
    }));
    expect(result.skipped).toEqual([]);
    expect(result.servers).toEqual([{
      name: "github",
      url: "https://api.githubcopilot.com/mcp/",
      headers: { Authorization: "Bearer ghp-test" },
      headersToFill: [],
      description: "",
      authMode: "bearer",
    }]);
  });

  it("reads VS Code servers and leaves input placeholders for the operator", () => {
    const result = parseMcpJson(JSON.stringify({
      inputs: [{ id: "token", type: "promptString", password: true }],
      servers: { linear: { type: "http", url: "https://mcp.linear.app/mcp", headers: { "X-Api-Key": "${input:token}" } } },
    }));
    expect(result.servers[0]).toMatchObject({
      name: "linear",
      headers: { "X-Api-Key": "" },
      headersToFill: ["X-Api-Key"],
      authMode: "api_key",
    });
  });

  it("accepts a fragment without outer braces and with a trailing comma", () => {
    const result = parseMcpJson('"notion": { "url": "https://mcp.notion.com/mcp", },');
    expect(result.servers.map((server) => [server.name, server.url, server.authMode])).toEqual([
      ["notion", "https://mcp.notion.com/mcp", "none"],
    ]);
  });

  it("reads Windsurf serverUrl and a bare server object", () => {
    expect(parseMcpJson('{"mcpServers": {"w": {"serverUrl": "https://w.example.test/mcp"}}}').servers[0].url)
      .toBe("https://w.example.test/mcp");
    expect(parseMcpJson('{"url": "https://bare.example.test/mcp"}').servers[0]).toMatchObject({
      name: "",
      url: "https://bare.example.test/mcp",
    });
  });

  it("unwraps mcp-remote with its header and env substitution", () => {
    const result = parseMcpJson(JSON.stringify({
      mcpServers: {
        remote: {
          command: "npx",
          args: ["-y", "mcp-remote", "https://remote.example.test/mcp", "--header", "Authorization:${AUTH_HEADER}"],
          env: { AUTH_HEADER: "Bearer remote-test" },
        },
      },
    }));
    expect(result.servers[0]).toMatchObject({
      name: "remote",
      url: "https://remote.example.test/mcp",
      headers: { Authorization: "Bearer remote-test" },
      authMode: "bearer",
    });
  });

  it("reads an MCP Registry server.json streamable-http remote", () => {
    const result = parseMcpJson(JSON.stringify({
      name: "io.github.example/weather",
      title: "Weather",
      description: "Forecasts",
      remotes: [
        { type: "sse", url: "https://weather.example.test/sse" },
        { type: "streamable-http", url: "https://weather.example.test/mcp", headers: [{ name: "X-Api-Key", isRequired: true, isSecret: true }] },
      ],
    }));
    expect(result.servers[0]).toEqual({
      name: "Weather",
      url: "https://weather.example.test/mcp",
      headers: { "X-Api-Key": "" },
      headersToFill: ["X-Api-Key"],
      description: "Forecasts",
      authMode: "api_key",
    });
  });

  it("skips stdio and SSE servers with the reason and keeps the remote ones", () => {
    const result = parseMcpJson(JSON.stringify({
      mcpServers: {
        files: { command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] },
        legacy: { type: "sse", url: "https://legacy.example.test/sse" },
        implicit: { url: "https://implicit.example.test/sse/" },
        good: { url: "https://good.example.test/mcp" },
      },
    }));
    expect(result.servers.map((server) => server.name)).toEqual(["good"]);
    expect(result.skipped).toEqual([
      { name: "files", reason: "stdio" },
      { name: "legacy", reason: "sse" },
      { name: "implicit", reason: "sse" },
    ]);
  });

  it("reports text that is not JSON and JSON without a server", () => {
    expect(errorCode("not json at all")).toBe("invalid_json");
    expect(errorCode('{"theme": "dark"}')).toBe("no_servers");
  });
});
