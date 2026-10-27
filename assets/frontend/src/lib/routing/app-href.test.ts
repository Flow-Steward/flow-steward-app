import { appHref } from "@/lib/routing/app-href";

describe("appHref", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    globalThis.history.replaceState({}, "", "/");
  });

  it("prefixes logical routes with the configured frontend base", () => {
    vi.stubEnv("BASE_URL", "/app/");

    expect(appHref("/registry/agents/auditor")).toBe(
      "/app/registry/agents/auditor",
    );
  });

  it("does not prefix an already based route twice", () => {
    vi.stubEnv("BASE_URL", "/app/");

    expect(appHref("/app/registry/agents/auditor")).toBe(
      "/app/registry/agents/auditor",
    );
  });
});
