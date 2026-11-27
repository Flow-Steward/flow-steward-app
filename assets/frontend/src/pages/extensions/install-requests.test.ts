import type { ExtensionRecord } from "@/pages/extensions/api";
import type { ExtensionCatalogEntry } from "@/pages/extensions/catalog-api";
import {
  approveExtensionInstallRequest,
  cancelExtensionInstallRequest,
  catalogEntryInstalled,
  declineExtensionInstallRequest,
  enableableExtensions,
  type ExtensionInstallRequest,
  fetchExtensionInstallRequests,
  type InstallRequestActions,
  latestRequestByExtension,
  requestActionLabel,
  requestExtensionInstall,
  requestStateFor,
  runRequestAction,
} from "@/pages/extensions/install-requests";

const fetchJson = vi.hoisted(() => vi.fn(async () => ({})));
vi.mock("@/lib/http/client", () => ({ fetchJson }));

function request(overrides: Partial<ExtensionInstallRequest>): ExtensionInstallRequest {
  return {
    id: "r-1",
    account_id: "a",
    account_name: "Company A",
    extension_id: "acme.crm",
    extension_name: "Acme CRM",
    requested_by: "",
    requested_by_name: "Ann",
    note: "",
    status: "pending",
    decided_at: "",
    decline_reason: "",
    created_at: "2026-11-27T10:00:00Z",
    ...overrides,
  };
}

function actions(overrides: Partial<InstallRequestActions> = {}): InstallRequestActions {
  return {
    canRequest: true,
    latestById: {},
    enableableById: {},
    busyById: {},
    onRequest: vi.fn(),
    onCancel: vi.fn(),
    onEnable: vi.fn(),
    ...overrides,
  };
}

const ENTRY = { extension_id: "acme.crm", display_state: "install" } as ExtensionCatalogEntry;

beforeEach(() => fetchJson.mockClear());

test("the routes are the install request API", async () => {
  await fetchExtensionInstallRequests();
  await requestExtensionInstall("acme.crm", "  for orders ");
  await cancelExtensionInstallRequest("r/1");
  await approveExtensionInstallRequest("r-1");
  await declineExtensionInstallRequest("r-1", " not now ");

  expect(fetchJson.mock.calls).toEqual([
    ["/api/extensions/install-requests", { cache: "no-store" }],
    ["/api/extensions/install-requests", { method: "POST", body: JSON.stringify({ extension_id: "acme.crm", note: "for orders" }) }],
    ["/api/extensions/install-requests/r%2F1/cancel", { method: "POST" }],
    ["/api/extensions/install-requests/r-1/approve", { method: "POST" }],
    ["/api/extensions/install-requests/r-1/decline", { method: "POST", body: JSON.stringify({ reason: "not now" }) }],
  ]);
});

test("the newest request of each extension decides its state", () => {
  const latest = latestRequestByExtension([
    request({ id: "new", status: "declined", decline_reason: "No" }),
    request({ id: "old", status: "cancelled" }),
    request({ id: "other", extension_id: "acme.mail" }),
  ]);

  expect(latest["acme.crm"].id).toBe("new");
  expect(requestStateFor("acme.crm", actions({ latestById: latest }))).toEqual({
    canRequest: true,
    pending: false,
    enableable: false,
    declined: true,
    declineReason: "No",
  });
  expect(requestStateFor("acme.mail", actions({ latestById: latest }))?.pending).toBe(true);
  expect(requestStateFor("acme.crm", undefined)).toBeUndefined();
  expect(requestStateFor("acme.crm", actions({ canRequest: false }))).toBeUndefined();
});

test("an extension installed on the server but off for this company is enabled, not requested", () => {
  const rows = [
    { extension_id: "a.not", account_enablement: "not_enabled" },
    { extension_id: "a.off", account_enablement: "disabled" },
    { extension_id: "a.on", account_enablement: "enabled" },
    { extension_id: "a.own" },
  ] as ExtensionRecord[];

  expect(Object.keys(enableableExtensions(rows)).sort()).toEqual(["a.not", "a.off"]);
});

test("an installed catalog item only needs approving", () => {
  expect(catalogEntryInstalled(undefined)).toBe(false);
  expect(catalogEntryInstalled(ENTRY)).toBe(false);
  expect(catalogEntryInstalled({ ...ENTRY, display_state: "incompatible" })).toBe(false);
  expect(catalogEntryInstalled({ ...ENTRY, display_state: "installed" })).toBe(true);
  expect(catalogEntryInstalled({ ...ENTRY, display_state: "update_available" })).toBe(true);
});

test("each request action runs its own handler with plain words", () => {
  const pending = request({});
  const extension = { extension_id: "acme.crm" } as ExtensionRecord;
  const handlers = actions({ latestById: { "acme.crm": pending }, enableableById: { "acme.crm": extension } });

  runRequestAction("request", ENTRY, handlers);
  runRequestAction("requested", ENTRY, handlers);
  runRequestAction("enable", ENTRY, handlers);

  expect(handlers.onRequest).toHaveBeenCalledWith(ENTRY);
  expect(handlers.onCancel).toHaveBeenCalledWith(pending);
  expect(handlers.onEnable).toHaveBeenCalledWith(extension);
  expect(requestActionLabel("request")).toBe("Request installation");
  expect(requestActionLabel("requested")).toBe("Cancel request");
  expect(requestActionLabel("enable")).toBe("Enable for this company");
});
