/**
 * The Marketplace install flow through the real page, including the
 * browser-authority handshake.
 *
 * These exist because the card used to report `Installed.` the moment a `412`
 * opened the approval modal — the install had not happened, and cancelling or
 * retrying updated a different piece of state. The whole lifecycle lives in
 * the page, so it is the page that has to be driven here. Install itself is on
 * the extension's page, next to the permissions it asks for.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { HttpError } from "@/lib/http/errors";
import { ExtensionsHubPage } from "@/pages/extensions";
import {
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtensions,
} from "@/pages/extensions/api";
import {
  fetchExtensionCatalog,
  installExtensionFromCatalog,
  type ExtensionCatalogEntry,
  type ExtensionCatalogListing,
} from "@/pages/extensions/catalog-api";

vi.mock("@/pages/extensions/api", () => ({
  checkExtensionHealth: vi.fn(),
  confirmExtensionUninstall: vi.fn(),
  createExtensionCallbackRegistration: vi.fn(),
  executeExtensionAction: vi.fn(),
  executeExtensionQuery: vi.fn(),
  fetchExtension: vi.fn(),
  fetchExtensionArchiveLifecycleAvailability: vi.fn(),
  fetchExtensionUninstallPlan: vi.fn(),
  fetchExtensionCallbacks: vi.fn(),
  fetchExtensionCompiledManifest: vi.fn(),
  fetchExtensionPreview: vi.fn(),
  fetchExtensions: vi.fn(),
  fetchLlmProviderSources: vi.fn(),
  installExtensionFromFile: vi.fn(),
  normalizeExtensionRecord: vi.fn((value: unknown) => value),
  refreshExtensionCallbackStatus: vi.fn(),
  refreshLlmProviderModels: vi.fn(),
  saveLlmProviderCredentials: vi.fn(),
  setExtensionEnabled: vi.fn(),
  setLlmProviderModelEnabled: vi.fn(),
}));

vi.mock("@/pages/extensions/catalog-api", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return {
    ...original,
    fetchExtensionCatalog: vi.fn(),
    installExtensionFromCatalog: vi.fn(),
  };
});

vi.mock("@/pages/workflows/catalog/api", () => ({ fetchInstallationVersion: vi.fn(async () => "1.0.0") }));

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: vi.fn(() => true) }));

vi.mock("@/pages/extensions/extension-oauth-api", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    fetchExtensionOAuthConfigurations: vi.fn(),
    fetchExtensionOAuthSetupStatus: vi.fn(),
  };
});

vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));

vi.mock("@/pages/projects/connections/api", () => ({
  createProjectConnection: vi.fn(),
  deleteExtensionProjectState: vi.fn(),
  deleteProjectConnection: vi.fn(),
  fetchExtensionProjectState: vi.fn(),
  fetchProjectConnectionsList: vi.fn(),
  putExtensionProjectState: vi.fn(),
  saveProjectConnectionSecrets: vi.fn(),
  startProjectConnectionOAuth: vi.fn(),
  updateProjectConnection: vi.fn(),
}));

const mockedFetchCatalog = vi.mocked(fetchExtensionCatalog);
const mockedInstall = vi.mocked(installExtensionFromCatalog);
const mockedFetchExtensions = vi.mocked(fetchExtensions);
const mockedArchiveLifecycle = vi.mocked(fetchExtensionArchiveLifecycleAvailability);

const DIGEST = "a".repeat(64);
const IMPACT_HASH = "b".repeat(64);
const CANCEL_MESSAGE = "Install cancelled; browser authority was not approved.";
const ENTRY_URL = "/extensions?mk_entry=acme_crm#marketplace";

/** The row the Installed tab renders once the extension is present. */
const INSTALLED_RECORD = {
  extension_id: "acme_crm",
  display_name: "Acme CRM",
  version: "1.10.0",
  state: "active",
  active: true,
  vendor_name: "Acme",
  category: "sales",
  description: "Sync contacts with Acme.",
};

function catalogEntry(overrides: Partial<ExtensionCatalogEntry> = {}): ExtensionCatalogEntry {
  return {
    extension_id: "acme_crm",
    version: "1.10.0",
    name: "Acme CRM",
    summary: "Sync contacts with Acme.",
    description: "",
    categories: ["sales"],
    capabilities: ["tool"],
    repository_url: "https://github.example.test/acme_crm",
    release_zip_url: "https://catalog.example.test/acme_crm-1.10.0.zip",
    release_zip_bytes: 1024,
    release_zip_sha256: DIGEST,
    permissions_summary: ["extension:invoke"],
    setup_summary: "",
    publication_status: "published",
    verification_status: "verified",
    has_icon: true,
    icon_url: "/api/extensions/catalog/acme_crm/icon?v=abc",
    display_state: "install",
    compatible: true,
    installed_version: "",
    installed_install_id: "",
    installed_state: "",
    ...overrides,
  };
}

function listing(items: ExtensionCatalogEntry[] = [catalogEntry()]): ExtensionCatalogListing {
  return {
    kind: "extension_catalog_listing",
    availability: {
      kind: "extension_catalog_availability",
      configured: true,
      available: true,
      item_count: items.length,
      stale: false,
    },
    items,
  };
}

const INSTALLED_LISTING = listing([
  catalogEntry({ display_state: "installed", installed_version: "1.10.0", installed_state: "active" }),
]);

function approvalRequired(): HttpError {
  return new HttpError({
    status: 412,
    url: "/api/extensions/catalog/install",
    payload: {
      error: "browser_authority_approval_required",
      extension_id: "acme_crm",
      impact_hash: IMPACT_HASH,
      remote_origins: ["https://acme.example.test"],
      remote_script_urls: [],
      local_asset_refs: [],
    },
  });
}

function authValue(): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated: true,
      roles: ["admin"],
      user: { roles: ["admin"] },
    } as AuthContextValue["session"],
    status: "authenticated",
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.search}${location.hash}`}</output>;
}

function renderHub(url = ENTRY_URL) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AuthContext.Provider value={authValue()}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route
              element={
                <>
                  <ExtensionsHubPage />
                  <LocationProbe />
                </>
              }
              path="/extensions"
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

async function clickInstall() {
  fireEvent.click(await screen.findByRole("button", { name: "Install" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedFetchExtensions.mockResolvedValue([] as never);
  mockedArchiveLifecycle.mockResolvedValue({
    available: true,
    reason_code: "available",
    message: "",
    remediation: { install_update: [], uninstall: [] },
  } as never);
  mockedFetchCatalog.mockResolvedValue(listing());
});

test("a plain install reports Installed only after the request returns, then offers the installed extension", async () => {
  mockedInstall.mockImplementation(async () => {
    mockedFetchCatalog.mockResolvedValue(INSTALLED_LISTING);
    return { status: "installed" };
  });
  renderHub();

  await clickInstall();

  await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(1));
  expect(mockedInstall).toHaveBeenCalledWith("acme_crm", "1.10.0", DIGEST, "");
  expect(await screen.findByText("Installed.")).toBeInTheDocument();
  const open = await screen.findByRole("link", { name: "Open installed extension" });
  expect(open).toHaveAttribute("href", "/extensions/acme_crm#installed");
  await waitFor(() => expect(open).toHaveFocus());
  expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
});

test("a 412 opens the approval modal and does not claim the install happened", async () => {
  mockedInstall.mockRejectedValue(approvalRequired());
  renderHub();

  await clickInstall();

  expect(await screen.findByRole("dialog", { name: "Approve external browser code" })).toBeInTheDocument();
  expect(screen.getByText("Waiting for browser authority approval.")).toBeInTheDocument();
  expect(screen.queryByText("Installed.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Awaiting approval" })).toBeDisabled();
});

test("412 then cancel leaves the extension cancelled and installable again", async () => {
  mockedInstall.mockRejectedValue(approvalRequired());
  renderHub();

  await clickInstall();
  await screen.findByText("Waiting for browser authority approval.");

  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(await screen.findByText(CANCEL_MESSAGE)).toBeInTheDocument();
  expect(screen.queryByText("Installed.")).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Install" })).toBeEnabled());
  expect(mockedInstall).toHaveBeenCalledTimes(1);
});

test("412 then confirm replays the approved hash and only then reports Installed", async () => {
  mockedInstall
    .mockRejectedValueOnce(approvalRequired())
    .mockResolvedValueOnce({ status: "installed" });
  renderHub();

  await clickInstall();
  await screen.findByText("Waiting for browser authority approval.");

  fireEvent.click(screen.getByRole("button", { name: "Confirm and continue" }));

  await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(2));
  expect(mockedInstall).toHaveBeenLastCalledWith("acme_crm", "1.10.0", DIGEST, IMPACT_HASH);
  expect(await screen.findByText("Installed.")).toBeInTheDocument();
});

test("a failed install shows the server's reason under the header and Install comes back", async () => {
  mockedInstall.mockRejectedValue(
    new HttpError({
      status: 409,
      url: "/api/extensions/catalog/install",
      payload: { detail: "The downloaded archive did not match the catalog's checksum." },
    }),
  );
  renderHub();

  await clickInstall();

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The extension was not installed");
  expect(alert).toHaveTextContent("The downloaded archive did not match the catalog's checksum.");
  expect(screen.getByRole("button", { name: "Install" })).toBeEnabled();
});

test("cancelling writes the message once, not onto the installed row too", async () => {
  // The row has to be on screen for the leak to be observable: the shared
  // per-extension status map is keyed by extension id and is what the
  // Installed tab renders.
  mockedFetchExtensions.mockResolvedValue([INSTALLED_RECORD] as never);
  mockedInstall.mockRejectedValue(approvalRequired());
  renderHub();

  await clickInstall();
  await screen.findByText("Waiting for browser authority approval.");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(await screen.findAllByText(CANCEL_MESSAGE)).toHaveLength(1);
  const installedRows = screen.getByRole("table", { name: "Installed extensions" });
  expect(within(installedRows).queryByText(CANCEL_MESSAGE)).not.toBeInTheDocument();
});

test("a cancelled install leaves nothing behind once it later succeeds", async () => {
  mockedInstall
    .mockRejectedValueOnce(approvalRequired())
    .mockResolvedValueOnce({ status: "installed" });
  renderHub();

  await clickInstall();
  await screen.findByText("Waiting for browser authority approval.");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await screen.findByText(CANCEL_MESSAGE);

  // The extension exists from here on, so the Installed tab renders its row.
  mockedFetchExtensions.mockResolvedValue([INSTALLED_RECORD] as never);
  await clickInstall();

  expect(await screen.findByText("Installed.")).toBeInTheDocument();

  // The Installed tab is what renders the shared per-extension status, so the
  // row has to exist for its absence to mean anything.
  const installedRows = await screen.findByRole("table", { name: "Installed extensions" });
  expect(await within(installedRows).findByText("Acme CRM")).toBeInTheDocument();
  expect(within(installedRows).queryByText(CANCEL_MESSAGE)).not.toBeInTheDocument();
  expect(screen.queryByText(CANCEL_MESSAGE)).not.toBeInTheDocument();
});

test("an administrator on an installation without archive installs reads why Install is off", async () => {
  mockedArchiveLifecycle.mockResolvedValue({
    available: false,
    reason_code: "unavailable",
    message: "",
    remediation: { install_update: [], uninstall: [] },
  } as never);
  renderHub();

  const install = await screen.findByRole("button", { name: "Install" });
  // The page's own words, the same as the alert above the tabs.
  await waitFor(() =>
    expect(install).toHaveAccessibleDescription("Extension archive changes are unavailable from this web interface."),
  );
  expect(install).toBeDisabled();
});

/* ---- Update to a newer catalog release ---------------------------------- */

const UPDATE_LISTING = listing([
  catalogEntry({ display_state: "update_available", installed_version: "1.9.0", installed_state: "active" }),
]);
const UPDATE_CANCEL_MESSAGE = "Update cancelled; browser authority was not approved.";

async function pressUpdate() {
  fireEvent.click(await screen.findByRole("button", { name: "Update to v1.10.0" }));
}

test("Update runs the catalog install call, says Updated to the version, and the page becomes Installed", async () => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedFetchExtensions.mockResolvedValue([{ ...INSTALLED_RECORD, version: "1.9.0" }] as never);
  let release: () => void = () => undefined;
  mockedInstall.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = () => {
          mockedFetchCatalog.mockResolvedValue(INSTALLED_LISTING);
          resolve({ extension: { lifecycle_operation: "updated", from_version: "1.9.0", to_version: "1.10.0" } });
        };
      }),
  );
  renderHub();

  await pressUpdate();

  expect(await screen.findByRole("button", { name: "Updating…" })).toBeDisabled();
  release();
  await waitFor(() => expect(mockedInstall).toHaveBeenCalledWith("acme_crm", "1.10.0", DIGEST, ""));
  expect(await screen.findByText("Updated to 1.10.0.")).toBeInTheDocument();
  const open = await screen.findByRole("link", { name: "Open installed extension" });
  await waitFor(() => expect(open).toHaveFocus());
  expect(screen.queryByRole("button", { name: /Update to/ })).not.toBeInTheDocument();
});

test("an Update that needs browser authority waits for approval, then replays the hash", async () => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall
    .mockRejectedValueOnce(approvalRequired())
    .mockResolvedValueOnce({ status: "updated" });
  renderHub();

  await pressUpdate();

  expect(await screen.findByRole("dialog", { name: "Approve external browser code" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Awaiting approval" })).toBeDisabled();
  expect(screen.queryByText("Updated to 1.10.0.")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Confirm and continue" }));

  await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(2));
  expect(mockedInstall).toHaveBeenLastCalledWith("acme_crm", "1.10.0", DIGEST, IMPACT_HASH);
  expect(await screen.findByText("Updated to 1.10.0.")).toBeInTheDocument();
});

test("a cancelled Update says so and offers Update again", async () => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall.mockRejectedValue(approvalRequired());
  renderHub();

  await pressUpdate();
  await screen.findByText("Waiting for browser authority approval.");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(await screen.findByText(UPDATE_CANCEL_MESSAGE)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Update to v1.10.0" })).toBeEnabled());
  expect(mockedInstall).toHaveBeenCalledTimes(1);
});

test("a failed Update shows the server's reason and Update comes back", async () => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall.mockRejectedValue(
    new HttpError({
      status: 422,
      url: "/api/extensions/catalog/install",
      payload: { detail: "Data migration 2 failed; the previous version is still installed." },
    }),
  );
  renderHub();

  await pressUpdate();

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The extension was not updated");
  expect(alert).toHaveTextContent("Data migration 2 failed; the previous version is still installed.");
  expect(screen.getByRole("button", { name: "Update to v1.10.0" })).toBeEnabled();
  expect(screen.queryByText("Updated to 1.10.0.")).not.toBeInTheDocument();
});

test("Marketplace keeps its mk_ params while open, and the Installed tab drops them", async () => {
  mockedFetchCatalog.mockResolvedValue(
    listing([catalogEntry(), catalogEntry({ extension_id: "acme_mail", name: "Acme Mail", categories: ["messaging"] })]),
  );
  renderHub("/extensions#marketplace");
  const currentUrl = () => screen.getByTestId("location").textContent;

  const category = await screen.findByRole("group", { name: "Category" });
  fireEvent.click(within(category).getByRole("checkbox", { name: /messaging/ }));
  await waitFor(() => expect(currentUrl()).toBe("?mk_category=messaging#marketplace"));

  fireEvent.click(screen.getByRole("tab", { name: "Installed" }));
  await waitFor(() => expect(currentUrl()).toBe("#installed"));

  fireEvent.click(screen.getByRole("tab", { name: "Catalog" }));
  await waitFor(() => expect(currentUrl()).toBe("#marketplace"));
  expect(await screen.findByRole("heading", { name: "2 extensions" })).toBeInTheDocument();
});

test("the Catalog tab closes an open extension's page and keeps the filters", async () => {
  mockedFetchCatalog.mockResolvedValue(
    listing([catalogEntry(), catalogEntry({ extension_id: "acme_mail", name: "Acme Mail", categories: ["messaging"] })]),
  );
  renderHub("/extensions?mk_category=messaging&mk_entry=acme_mail#marketplace");
  const currentUrl = () => screen.getByTestId("location").textContent;

  expect(await screen.findByRole("heading", { level: 2, name: /Acme Mail/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Catalog" }));

  await waitFor(() => expect(currentUrl()).toBe("?mk_category=messaging#marketplace"));
  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
});

/* ---- Install and update straight from the results ----------------------- */

const LIST_URL = "/extensions?mk_view=list#marketplace";
const GRID_URL = "/extensions#marketplace";
const ARCHIVE_MISMATCH = "The downloaded archive did not match the catalog's checksum.";

function quickUpdate(version: string) {
  return screen.findByRole("button", { name: `Update to v${version}: Acme CRM` });
}

function noticeOn(extensionId: string) {
  return document.querySelector(`[data-marketplace-install-notice="${extensionId}"]`);
}

test.each([
  ["grid card", GRID_URL],
  ["list row", LIST_URL],
])("an update pressed on a %s that fails says why there, and Update comes back", async (_surface, url) => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall.mockRejectedValue(
    new HttpError({
      status: 422,
      url: "/api/extensions/catalog/install",
      payload: { error: ARCHIVE_MISMATCH, code: "extension_catalog_artifact_mismatch", message: ARCHIVE_MISMATCH },
    }),
  );
  renderHub(url);

  fireEvent.click(await quickUpdate("1.10.0"));

  await waitFor(() => expect(noticeOn("acme_crm")).toHaveTextContent(ARCHIVE_MISMATCH));
  expect(noticeOn("acme_crm")).toHaveClass("is-failed");
  expect(
    screen.getAllByRole("alert").some((alert) => alert.textContent === `Acme CRM: ${ARCHIVE_MISMATCH}`),
  ).toBe(true);
  expect(await quickUpdate("1.10.0")).toBeEnabled();
});

/* ---- A release published after the listing loaded ------------------------ */

const NEWER_DIGEST = "c".repeat(64);
const CHANGED_MESSAGE =
  "The catalog entry for 'acme_crm' changed since it was listed (now 1.11.0). Reload the catalog and try again.";
const NEWER_LISTING = listing([
  catalogEntry({
    version: "1.11.0",
    release_zip_sha256: NEWER_DIGEST,
    display_state: "update_available",
    installed_version: "1.9.0",
    installed_state: "active",
  }),
]);

function releaseChanged(payload: Record<string, unknown> = { code: "extension_catalog_release_changed" }): HttpError {
  return new HttpError({
    status: 409,
    url: "/api/extensions/catalog/install",
    payload: { error: CHANGED_MESSAGE, message: CHANGED_MESSAGE, ...payload },
  });
}

/** The install refuses the listed release; the catalog meanwhile lists 1.11.0. */
function publishNewerDuringTheClick(error: HttpError = releaseChanged()) {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall.mockImplementationOnce(async () => {
    mockedFetchCatalog.mockResolvedValue(NEWER_LISTING);
    throw error;
  });
}

test("a card whose release was superseded shows the new version and installs nothing until pressed again", async () => {
  publishNewerDuringTheClick();
  renderHub(GRID_URL);

  fireEvent.click(await quickUpdate("1.10.0"));

  expect(await quickUpdate("1.11.0")).toBeEnabled();
  await waitFor(() => expect(noticeOn("acme_crm")).toHaveTextContent("A newer version 1.11.0 was published."));
  expect(noticeOn("acme_crm")).not.toHaveClass("is-failed");
  expect(screen.getByRole("link", { name: "Acme CRM" })).toHaveTextContent("1.11.0");
  expect(mockedInstall).toHaveBeenCalledTimes(1);

  mockedInstall.mockResolvedValueOnce({ status: "updated" });
  fireEvent.click(await quickUpdate("1.11.0"));
  await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(2));
  expect(mockedInstall).toHaveBeenLastCalledWith("acme_crm", "1.11.0", NEWER_DIGEST, "");
});

test("the extension's page follows a superseded release to the new version", async () => {
  publishNewerDuringTheClick();
  renderHub();

  await pressUpdate();

  expect(await screen.findByRole("button", { name: "Update to v1.11.0" })).toBeEnabled();
  expect(screen.getByText("A newer version 1.11.0 was published.")).toBeInTheDocument();
  expect(screen.queryByText("The extension was not updated")).not.toBeInTheDocument();
  expect(mockedInstall).toHaveBeenCalledTimes(1);
});

test("a release superseded during browser approval ends the obsolete approval and requires a new pinned click", async () => {
  mockedFetchCatalog.mockResolvedValue(UPDATE_LISTING);
  mockedInstall
    .mockRejectedValueOnce(approvalRequired())
    .mockImplementationOnce(async () => {
      mockedFetchCatalog.mockResolvedValue(NEWER_LISTING);
      throw releaseChanged();
    });
  renderHub();
  await pressUpdate();
  await screen.findByRole("dialog", { name: "Approve external browser code" });

  fireEvent.click(screen.getByRole("button", { name: "Confirm and continue" }));

  const update = await screen.findByRole("button", { name: "Update to v1.11.0" });
  expect(update).toBeEnabled();
  expect(screen.queryByRole("dialog", { name: "Approve external browser code" })).not.toBeInTheDocument();
  expect(screen.getByText("A newer version 1.11.0 was published.")).toBeInTheDocument();
  expect(mockedInstall).toHaveBeenCalledTimes(2);
  expect(mockedInstall).toHaveBeenLastCalledWith("acme_crm", "1.10.0", DIGEST, IMPACT_HASH);

  mockedInstall.mockResolvedValueOnce({ status: "updated" });
  fireEvent.click(update);
  await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(3));
  expect(mockedInstall).toHaveBeenLastCalledWith("acme_crm", "1.11.0", NEWER_DIGEST, "");
});

test("the refusal is recognised by its code, never by its wording", async () => {
  // The same words without the code are an ordinary failure: no reload.
  publishNewerDuringTheClick(releaseChanged({ code: "extension_catalog_artifact_mismatch" }));
  renderHub(GRID_URL);

  fireEvent.click(await quickUpdate("1.10.0"));

  await waitFor(() => expect(noticeOn("acme_crm")).toHaveTextContent(CHANGED_MESSAGE));
  expect(noticeOn("acme_crm")).toHaveClass("is-failed");
  expect(await quickUpdate("1.10.0")).toBeEnabled();
});
