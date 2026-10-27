import { describe, expect, it, vi } from "vitest";

import {
  clearWorkflowInputSecret,
  setWorkflowInputSecret,
} from "@/pages/workflows/shared/api";

vi.mock("@/lib/http/client", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, fetchJson: vi.fn(async () => ({ default_configured: true })) };
});

const { fetchJson } = await import("@/lib/http/client");
const mockedFetchJson = vi.mocked(fetchJson as never) as unknown as ReturnType<typeof vi.fn>;

describe("workflow secret input lifecycle", () => {
  it("sends the value to the write-only endpoint", async () => {
    mockedFetchJson.mockClear();

    await setWorkflowInputSecret({
      workflowId: "orders",
      name: "login",
      value: "s3cret",
      expectedRevision: 3,
    });

    const [url, init] = mockedFetchJson.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/workflows/orders/inputs/login/secret");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      value: "s3cret",
      expected_revision: 3,
    });
  });

  it("escapes a workflow or field name that would change the path", async () => {
    mockedFetchJson.mockClear();

    await setWorkflowInputSecret({
      workflowId: "a/b",
      name: "c d",
      value: "x",
      expectedRevision: 1,
    });

    const [url] = mockedFetchJson.mock.calls[0] as [string];
    expect(url).toBe("/api/workflows/a%2Fb/inputs/c%20d/secret");
  });

  it("clears through DELETE, carrying the revision", async () => {
    mockedFetchJson.mockClear();

    await clearWorkflowInputSecret({
      workflowId: "orders",
      name: "login",
      expectedRevision: 4,
    });

    const [url, init] = mockedFetchJson.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/workflows/orders/inputs/login/secret?expected_revision=4");
    expect(init.method).toBe("DELETE");
  });
});
