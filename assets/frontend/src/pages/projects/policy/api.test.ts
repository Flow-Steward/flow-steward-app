import {
  fetchProjectPolicy,
  normalizeProjectPolicySavePayload,
  PROJECT_POLICY_AUTONOMY_MODES,
  saveProjectPolicy,
} from "@/pages/projects/policy/api";

describe("project policy api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes nested policy payload for save", () => {
    expect(
      normalizeProjectPolicySavePayload({
        policy: {
          autonomy_mode: "supervised",
          autonomy_confidence_threshold: 0.9,
          retry_strategy: "bounded",
          retry_max_attempts: 3,
          retry_backoff_initial_seconds: 4,
          retry_backoff_max_seconds: 60,
          concurrency_cap: 2,
          tool_policy_mode: "registry_only",
        },
      }),
    ).toEqual({
      autonomy_mode: "supervised",
      autonomy_confidence_threshold: 0.9,
      retry_strategy: "bounded",
      retry_max_attempts: 3,
      retry_backoff_initial_seconds: 4,
      retry_backoff_max_seconds: 60,
      concurrency_cap: 2,
      tool_policy_mode: "registry_only",
    });
  });

  it.each(PROJECT_POLICY_AUTONOMY_MODES)("keeps the API-valid autonomy mode %s", (autonomyMode) => {
    expect(
      normalizeProjectPolicySavePayload({
        policy: { autonomy_mode: autonomyMode },
      }).autonomy_mode,
    ).toBe(autonomyMode);
  });

  it("calls project policy endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ project_id: 7, policy: {} }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchProjectPolicy("7")).resolves.toEqual({
      project_id: 7,
      policy: {},
    });

    await expect(
      saveProjectPolicy("7", {
        policy: {
          autonomy_mode: "supervised",
          autonomy_confidence_threshold: 0.8,
          retry_strategy: "bounded",
          retry_max_attempts: 2,
          retry_backoff_initial_seconds: 2,
          retry_backoff_max_seconds: 120,
          concurrency_cap: 1,
          tool_policy_mode: "registry_only",
        },
      }),
    ).resolves.toEqual({ status: "ok" });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/7/policy");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/projects/7/policy");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PUT");
  });
});
