import { buildHttpHeaders, fetchJson } from "@/lib/http/client";
import { hasPendingRequests } from "@/lib/http/request-activity";

describe("fetchJson", () => {
  it.each([undefined, "foreground", "background"] as const)("keeps %s presentation local while preserving request auth, body, cache and signal", async (presentation) => {
    Object.defineProperty(document, "cookie", { configurable: true, value: "fs_csrf=transport-token" });
    let resolve!: (response: Response) => void;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise((finish) => { resolve = finish; }));
    const controller = new AbortController();
    const init = { method: "POST", body: JSON.stringify({ name: "draft" }), cache: "no-store" as const, signal: controller.signal };
    const pending = fetchJson("/api/transport", init, presentation ? { presentation } : undefined);
    try {
      expect(hasPendingRequests()).toBe(presentation !== "background");
      const [url, actual] = fetchMock.mock.calls[0];
      expect(url).toBe("/api/transport");
      expect(actual).toEqual({ ...init, credentials: "same-origin", headers: expect.any(Headers) });
      expect(new Headers(actual?.headers).get("x-csrf-token")).toBe("transport-token");
      expect(new Headers(actual?.headers).get("Accept")).toBe("application/json");
      expect(new Headers(actual?.headers).get("Content-Type")).toBe("application/json");
      expect(actual).not.toHaveProperty("presentation");
      resolve(new Response(JSON.stringify({ ok: true, data: { saved: true }, kind: "detail", error: null }), { headers: { "content-type": "application/json" } }));
      await expect(pending).resolves.toEqual({ saved: true });
    } finally {
      resolve(new Response("{}"));
      await pending;
    }
    expect(hasPendingRequests()).toBe(false);
  });

  it.each([401, 403, 503])("preserves background HTTP %s failures without starting global activity", async (status) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ error: "Existing server error" }), { status, headers: { "content-type": "application/json", "Retry-After": "12" } }));
    await expect(fetchJson("/api/status", {}, { presentation: "background" })).rejects.toMatchObject({ status, message: "Existing server error", retryAfterSeconds: 12 });
    expect(hasPendingRequests()).toBe(false);
  });

  it("preserves AbortError identity and credentials for a background read", async () => {
    const controller = new AbortController();
    const aborted = new DOMException("Request cancelled", "AbortError");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(aborted), { once: true });
    }));
    const request = fetchJson("/api/background", { signal: controller.signal, credentials: "include" }, { presentation: "background" });
    expect(hasPendingRequests()).toBe(false);
    controller.abort();
    await expect(request).rejects.toBe(aborted);
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe("include");
    expect(hasPendingRequests()).toBe(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("injects the csrf header for unsafe methods", async () => {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      value: "fs_csrf=csrf-token",
    });

    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await fetchJson("/api/auth/logout", { method: "POST" });

    const [, requestInit] = fetchMock.mock.calls[0];
    const headers = requestInit?.headers as Headers;
    expect(headers.get("x-csrf-token")).toBe("csrf-token");
  });

  it("builds csrf-aware headers for streaming unsafe requests", () => {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      value: "fs_csrf=stream-token",
    });

    const headers = buildHttpHeaders({
      method: "POST",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
      body: "{}",
    });

    expect(headers.get("x-csrf-token")).toBe("stream-token");
    expect(headers.get("Accept")).toBe("text/event-stream");
  });

  it("raises normalized HttpError instances", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: "Invalid credentials" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchJson("/api/auth/me")).rejects.toEqual(
      expect.objectContaining({
        kind: "unauthorized",
        message: "Invalid credentials",
        status: 401,
      }),
    );
  });

  it("preserves Retry-After response metadata on HttpError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: "Too many requests" }), {
        status: 429,
        headers: {
          "content-type": "application/json",
          "Retry-After": "37",
        },
      }),
    );

    await expect(fetchJson("/api/auth/password-reset/request")).rejects.toEqual(
      expect.objectContaining({
        retryAfterSeconds: 37,
        status: 429,
      }),
    );
  });

  it("unwraps successful v2 envelope payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          kind: "detail",
          data: { id: 42, status: "in_progress" },
          error: null,
          meta: { resource: "jobs" },
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await expect(fetchJson<{ id: number; status: string }>("/api/v2/jobs/42")).resolves.toEqual({
      id: 42,
      status: "in_progress",
    });
  });

  it("raises HttpError for failed v2 envelope payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: false,
          kind: "detail",
          data: null,
          error: { code: "forbidden", message: "Admin role required", status: 403 },
          meta: { resource: "jobs" },
        }),
        {
          status: 403,
          headers: { "content-type": "application/json" },
        },
      ),
    );

    await expect(fetchJson("/api/v2/jobs/42")).rejects.toEqual(
      expect.objectContaining({
        kind: "forbidden",
        message: "Admin role required",
        status: 403,
      }),
    );
  });
});
