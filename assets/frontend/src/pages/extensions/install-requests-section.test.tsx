/**
 * Requests to install an extension (ADR 0084) on the Extensions page: the
 * installation administrator's "Installation requests" section and the
 * company administrator's request dialog.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ExtensionRecord } from "@/pages/extensions/api";
import {
  fetchExtensionCatalog,
  type ExtensionCatalogEntry,
  type ExtensionCatalogInstallState,
} from "@/pages/extensions/catalog-api";
import {
  approveExtensionInstallRequest,
  declineExtensionInstallRequest,
  fetchExtensionInstallRequests,
  requestExtensionInstall,
  type ExtensionInstallRequest,
} from "@/pages/extensions/install-requests";
import { useExtensionInstallRequests } from "@/pages/extensions/install-requests-section";

vi.mock("@/pages/extensions/install-requests", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return {
    ...original,
    approveExtensionInstallRequest: vi.fn(async () => ({})),
    cancelExtensionInstallRequest: vi.fn(async () => ({})),
    declineExtensionInstallRequest: vi.fn(async () => ({})),
    fetchExtensionInstallRequests: vi.fn(),
    requestExtensionInstall: vi.fn(async () => ({})),
  };
});

vi.mock("@/pages/extensions/catalog-api", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return { ...original, fetchExtensionCatalog: vi.fn() };
});

const PENDING: ExtensionInstallRequest = {
  id: "r-1",
  account_id: "a",
  account_name: "Company A",
  extension_id: "acme.crm",
  extension_name: "Acme CRM",
  requested_by: "u",
  requested_by_name: "Ann",
  note: "For our orders",
  status: "pending",
  decided_at: "",
  decline_reason: "",
  created_at: "2026-11-27T10:00:00Z",
};

function entry(display_state: ExtensionCatalogEntry["display_state"]): ExtensionCatalogEntry {
  return { extension_id: "acme.crm", name: "Acme CRM", display_state } as ExtensionCatalogEntry;
}

function Harness({
  canRequest = false,
  canAnswer = false,
  install,
  installStateById = {},
  onReady,
}: Readonly<{
  canRequest?: boolean;
  canAnswer?: boolean;
  install: (entry: ExtensionCatalogEntry, repository?: string, onInstalled?: () => void) => void;
  installStateById?: Record<string, ExtensionCatalogInstallState>;
  onReady?: (actions: ReturnType<typeof useExtensionInstallRequests>["actions"]) => void;
}>) {
  const requests = useExtensionInstallRequests({
    canRequest,
    canAnswer,
    extensions: [] as ExtensionRecord[],
    installStateById,
    install,
    onEnable: vi.fn(),
  });
  onReady?.(requests.actions);
  return (
    <>
      {requests.section}
      {requests.dialog}
    </>
  );
}

function renderHarness(props: Parameters<typeof Harness>[0]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(fetchExtensionInstallRequests).mockClear();
  vi.mocked(fetchExtensionInstallRequests).mockResolvedValue({ scope: "installation", items: [PENDING] });
  vi.mocked(approveExtensionInstallRequest).mockClear();
  vi.mocked(declineExtensionInstallRequest).mockClear();
  vi.mocked(requestExtensionInstall).mockClear();
});

test("the installation administrator sees each pending request with its company, person and note", async () => {
  renderHarness({ canAnswer: true, install: vi.fn() });

  const section = await screen.findByTestId("extension-install-requests");
  expect(within(section).getByRole("heading", { name: /Installation requests\s*1/ })).toBeInTheDocument();
  expect(within(section).getByText("Company A")).toBeInTheDocument();
  expect(within(section).getByText("Acme CRM")).toBeInTheDocument();
  expect(within(section).getByText("Ann")).toBeInTheDocument();
  expect(within(section).getByText("For our orders")).toBeInTheDocument();
});

test("nobody else sees the section, and it stays away while nothing waits", async () => {
  renderHarness({ install: vi.fn() });
  await waitFor(() => expect(fetchExtensionInstallRequests).not.toHaveBeenCalled());
  expect(screen.queryByTestId("extension-install-requests")).not.toBeInTheDocument();
});

test("Install runs the catalog install and approves only after it succeeded", async () => {
  vi.mocked(fetchExtensionCatalog).mockResolvedValue({ items: [entry("install")] } as never);
  const install = vi.fn();
  renderHarness({ canAnswer: true, install });

  fireEvent.click(await screen.findByRole("button", { name: "Install: Acme CRM" }));

  await waitFor(() => expect(install).toHaveBeenCalledTimes(1));
  expect(install.mock.calls[0][0].extension_id).toBe("acme.crm");
  expect(approveExtensionInstallRequest).not.toHaveBeenCalled();
  const onInstalled = install.mock.calls[0][2] as () => void;
  act(() => onInstalled());
  await waitFor(() => expect(approveExtensionInstallRequest).toHaveBeenCalledWith("r-1"));
});

test("a failed install leaves the request pending and shows the install's own error", async () => {
  renderHarness({
    canAnswer: true,
    install: vi.fn(),
    installStateById: { "acme.crm": { phase: "idle", status: "Install failed: no space left", outcome: "failed" } },
  });

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Install failed: no space left"));
  expect(approveExtensionInstallRequest).not.toHaveBeenCalled();
});

test("an extension installed meanwhile is only enabled for the company", async () => {
  vi.mocked(fetchExtensionCatalog).mockResolvedValue({ items: [entry("installed")] } as never);
  const install = vi.fn();
  renderHarness({ canAnswer: true, install });

  fireEvent.click(await screen.findByRole("button", { name: "Install: Acme CRM" }));

  await waitFor(() => expect(approveExtensionInstallRequest).toHaveBeenCalledWith("r-1"));
  expect(install).not.toHaveBeenCalled();
});

test("Decline asks for an optional reason and sends it", async () => {
  renderHarness({ canAnswer: true, install: vi.fn() });

  fireEvent.click(await screen.findByRole("button", { name: "Decline" }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.change(within(dialog).getByTestId("install-request-text"), { target: { value: "We use another tool" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Decline request" }));

  await waitFor(() => expect(declineExtensionInstallRequest).toHaveBeenCalledWith("r-1", "We use another tool"));
});

test("a company administrator sends a request with an optional note", async () => {
  vi.mocked(fetchExtensionInstallRequests).mockResolvedValue({ scope: "account", items: [] });
  let actions: ReturnType<typeof useExtensionInstallRequests>["actions"];
  renderHarness({ canRequest: true, install: vi.fn(), onReady: (value) => { actions = value; } });

  await waitFor(() => expect(actions?.canRequest).toBe(true));
  expect(screen.queryByTestId("extension-install-requests")).not.toBeInTheDocument();
  act(() => actions?.onRequest(entry("install")));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("Request installation of Acme CRM?")).toBeInTheDocument();
  fireEvent.change(within(dialog).getByTestId("install-request-text"), { target: { value: "For orders" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Send request" }));

  await waitFor(() => expect(requestExtensionInstall).toHaveBeenCalledWith("acme.crm", "For orders"));
});
