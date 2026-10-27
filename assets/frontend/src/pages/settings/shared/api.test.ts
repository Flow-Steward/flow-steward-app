import { fetchJson } from "@/lib/http/client";
import {
  createPersonalToken,
  createUser,
  fetchPersonalTokensCollection,
  fetchUsersCollection,
  saveAccountProfileRecord,
  setTemporaryUserPassword,
  updateUser,
} from "@/pages/settings/shared/api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

describe("settings account and access API", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue({} as never);
  });

  it("serializes every users filter into the server page request", async () => {
    await fetchUsersCollection({ search: " maya ", role: "admin", status: "inactive", page: 3, perPage: 20 });
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/users?page=3&per_page=20&search=maya&role=admin&active=false");
  });

  it("uses the backend personal-token page envelope", async () => {
    await fetchPersonalTokensCollection(2, 10);
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/tokens?page=2&per_page=10");
  });

  it("sends exact create and changed-only update user payloads", async () => {
    const createPayload = { display_name: "Maya Chen", email: "maya@example.com", password: "temporary-pass", roles: ["viewer"], account_ids: ["acc-1"], primary_account_id: "acc-1", is_active: true, must_change_password: true };
    await createUser(createPayload);
    await updateUser("u1", { roles: ["operator"], primary_account_id: "acc-2" });
    expect(mockedFetchJson).toHaveBeenNthCalledWith(1, "/api/auth/users", { method: "POST", body: JSON.stringify(createPayload) });
    expect(mockedFetchJson).toHaveBeenNthCalledWith(2, "/api/auth/users/u1", { method: "PUT", body: JSON.stringify({ roles: ["operator"], primary_account_id: "acc-2" }) });
  });

  it("keeps the temporary-password requirement explicit", async () => {
    await setTemporaryUserPassword("u1", "temporary-pass", false);
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/users/u1", {
      method: "PUT",
      body: JSON.stringify({ password: "temporary-pass", must_change_password: false }),
    });
  });

  it("keeps token scope and restriction payloads exact", async () => {
    const payload = { label: "Local CLI", expires_in_days: 30, scopes: ["projects.read" as const], allowed_project_ids: ["7"], allowed_workflow_ids: ["wf-1"] };
    await createPersonalToken(payload);
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/tokens", { method: "POST", body: JSON.stringify(payload) });
  });

  it("reports partial profile success without dropping failed fields", async () => {
    mockedFetchJson.mockImplementation(async (url) => {
      if (url === "/api/users/me/timezone") throw new Error("Timezone unavailable");
      return {};
    });
    const result = await saveAccountProfileRecord({ display_name: "Maya", timezone: "UTC" });
    expect(result.succeeded).toEqual(["display_name"]);
    expect(result.failed).toEqual({ timezone: "Timezone unavailable" });
  });
});
