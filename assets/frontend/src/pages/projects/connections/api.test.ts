import {
  createProjectConnection,
  deleteProjectConnection,
  disconnectProjectConnectionOAuth,
  fetchProjectConnectionsList,
  saveProjectConnectionSecrets,
  startProjectConnectionOAuth,
  updateProjectConnection,
} from "@/pages/projects/connections/api";

describe("project connections api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes list payload from /connections endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ connections: [{ connection_id: "conn-1" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchProjectConnectionsList("11")).resolves.toEqual([{ connection_id: "conn-1" }]);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/11/connections");
  });

  it("calls the connection mutation endpoints the extension page uses", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const ok = () =>
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    fetchMock
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(new Response(JSON.stringify({ authorization_url: "https://idp.example.com/auth" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(ok());

    await createProjectConnection("11", { connection_kind: "extension" });
    await updateProjectConnection("11", "conn-1", { enabled: false });
    await saveProjectConnectionSecrets("11", "conn-1", { api_token: "secret" });
    await startProjectConnectionOAuth("11", "conn-1", { return_path: "/projects/11?tab=connections" });
    await disconnectProjectConnectionOAuth("11", "conn-1");
    await deleteProjectConnection("11", "conn-1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/11/connections");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/projects/11/connections/conn-1");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/projects/11/connections/conn-1/secrets");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/projects/11/connections/conn-1/oauth/start");
    expect(fetchMock.mock.calls[4]?.[0]).toBe("/api/projects/11/connections/conn-1/oauth/disconnect");
    expect(fetchMock.mock.calls[5]?.[0]).toBe("/api/projects/11/connections/conn-1");
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PUT");
    expect(fetchMock.mock.calls[2]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[4]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[5]?.[1]?.method).toBe("DELETE");
  });
});
