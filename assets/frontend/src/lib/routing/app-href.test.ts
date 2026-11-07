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
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("turns an app-absolute href into the router path while the app runs under its base", () => {
    vi.stubEnv("BASE_URL", "/app/");

    expect(appRoutePath("/app/registry/workflows/orders?project_id=p1", "/app/registry/workflows")).toBe("/registry/workflows/orders?project_id=p1");
    expect(appRoutePath("/app", "/app/dashboard")).toBe("/");
  });

  it("gives no router path for an href outside the base, or while the app runs elsewhere", () => {
    vi.stubEnv("BASE_URL", "/app/");

    expect(appRoutePath("/api/workflows/orders/export.yaml", "/app/registry/workflows")).toBeNull();
    expect(appRoutePath("/application/x", "/app/registry/workflows")).toBeNull();
    expect(appRoutePath("/app/registry/workflows/orders", "/")).toBeNull();
  });

  it("keeps app-absolute routes when the router is configured for the root", () => {
    vi.stubEnv("BASE_URL", "/");

    expect(appRoutePath("/registry/workflows/orders?project_id=p1", "/dashboard")).toBe("/registry/workflows/orders?project_id=p1");
    expect(appRoutePath("registry/workflows/orders", "/")).toBeNull();
  });

  it("uses the configured custom base instead of assuming the app prefix", () => {
    vi.stubEnv("BASE_URL", "/console/");

    expect(appRoutePath("/console/registry/workflows/orders?project_id=p1", "/console/dashboard")).toBe("/registry/workflows/orders?project_id=p1");
    expect(appRoutePath("/app/registry/workflows/orders", "/console/dashboard")).toBeNull();
  });
});
