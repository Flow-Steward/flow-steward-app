import { appHref, appRoutePath } from "@/lib/routing/app-href";

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

describe("appRoutePath", () => {
  it("turns an app-absolute href into the router path while the app runs under its base", () => {
    expect(appRoutePath("/app/registry/workflows/orders?project_id=p1", "/app/registry/workflows")).toBe("/registry/workflows/orders?project_id=p1");
    expect(appRoutePath("/app", "/app/dashboard")).toBe("/");
  });

  it("gives no router path for an href outside the base, or while the app runs elsewhere", () => {
    expect(appRoutePath("/api/workflows/orders/export.yaml", "/app/registry/workflows")).toBeNull();
    expect(appRoutePath("/application/x", "/app/registry/workflows")).toBeNull();
    expect(appRoutePath("/app/registry/workflows/orders", "/")).toBeNull();
  });
});
