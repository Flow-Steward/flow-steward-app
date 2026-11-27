import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { RegisteredSettingsResourceAdminPage } from "@/pages/settings/hub";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);

const LIST = "/api/modules/fixture/rules";
const PROVIDERS = "/api/modules/fixture/provider-options";

type Responses = Record<string, unknown>;

function serve(responses: Responses) {
  mockedFetchJson.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (init?.method && init.method !== "GET") return {};
    if (path in responses) return responses[path] as never;
    return { items: [] } as never;
  });
}

function renderPage(fields: unknown[], resource: Record<string, unknown> = {}) {
  const page = {
    key: "fixture_rules",
    page_key: "fixture_rules",
    label: "Rules",
    title: "Rules",
    path: "/modules/rules",
    control_surface: {
      kind: "resource_admin",
      resources: [
        {
          key: "rules",
          title: "Rules",
          list_path: LIST,
          item_path: `${LIST}/{id}`,
          collection_key: "items",
          id_field: "id",
          display_field: "name",
          fields,
          ...resource,
        },
      ],
    },
  };
  render(<MemoryRouter><RegisteredSettingsResourceAdminPage page={page} /></MemoryRouter>);
}

function lastWrite(): Record<string, unknown> {
  const call = mockedFetchJson.mock.calls.findLast(([, init]) => init?.method === "POST" || init?.method === "PUT");
  return JSON.parse(String(call?.[1]?.body ?? "{}")) as Record<string, unknown>;
}

const NAME = { key: "name", label: "Name", type: "text" };

describe("resource_admin choice fields", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
  });

  it("shows a single allowed option as read-only text and submits it", async () => {
    serve({});
    renderPage([
      NAME,
      { key: "kind", label: "Type", type: "select", options: [{ value: "standard", label: "Standard sign-in" }] },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    const fixed = screen.getByLabelText("Type");
    expect(fixed).toHaveValue("Standard sign-in");
    expect(fixed).toHaveAttribute("readonly");
    expect(screen.queryByRole("combobox", { name: "Type" })).not.toBeInTheDocument();
    expect(screen.queryByText("standard")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Company sign-in" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(lastWrite()).toEqual({ name: "Company sign-in", kind: "standard" }));
  });

  it("keeps the spaces a person types between words", async () => {
    serve({});
    renderPage([NAME]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    await userEvent.setup().type(screen.getByLabelText("Name"), "Main rule set");
    expect(screen.getByLabelText("Name")).toHaveValue("Main rule set");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(lastWrite()).toEqual({ name: "Main rule set" }));
  });

  it("hides a single-option field that declares hidden_when_single but still submits it", async () => {
    serve({});
    renderPage([
      NAME,
      { key: "kind", label: "Type", type: "select", hidden_when_single: true, options: ["standard"] },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    expect(screen.queryByLabelText("Type")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Company sign-in" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(lastWrite()).toEqual({ name: "Company sign-in", kind: "standard" }));
  });

  it("shows conditional fields for a fixed choice's one option, not for its stored default", async () => {
    const kinds = "/api/modules/fixture/kinds";
    serve({ [kinds]: { options: [{ value: "generic", label: "Any provider" }] } });
    renderPage([
      NAME,
      { key: "kind", label: "Type", type: "select", default: "acme", hidden_when_single: true, options_path: kinds },
      { key: "acme_tenant", label: "Acme tenant", type: "text", visible_when: { field: "kind", contains: "acme" } },
      { key: "issuer", label: "Issuer", type: "text", visible_when: { field: "kind", contains: "generic" } },
    ]);
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith(kinds));
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    await waitFor(() => expect(screen.getByLabelText("Issuer")).toBeInTheDocument());
    expect(screen.queryByLabelText("Acme tenant")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Type")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Main" } });
    fireEvent.change(screen.getByLabelText("Issuer"), { target: { value: "https://idp.example" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(lastWrite()).toEqual({ name: "Main", kind: "generic", acme_tenant: "", issuer: "https://idp.example" }),
    );
  });

  it("reads a read-only single option as the condition's value", async () => {
    serve({});
    renderPage([
      NAME,
      { key: "kind", label: "Type", type: "select", default: "acme", options: [{ value: "generic", label: "Any" }] },
      { key: "acme_tenant", label: "Acme tenant", type: "text", visible_when: { field: "kind", contains: "acme" } },
      { key: "issuer", label: "Issuer", type: "text", visible_when: { field: "kind", contains: "generic" } },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    expect(screen.getByLabelText("Type")).toHaveValue("Any");
    expect(screen.getByLabelText("Issuer")).toBeInTheDocument();
    expect(screen.queryByLabelText("Acme tenant")).not.toBeInTheDocument();
  });

  it("does not count a stored value that the loaded options do not offer", async () => {
    const kinds = "/api/modules/fixture/kinds";
    serve({
      [kinds]: {
        options: [
          { value: "generic", label: "Any provider" },
          { value: "beta", label: "Beta" },
        ],
      },
    });
    renderPage([
      NAME,
      { key: "kind", label: "Type", type: "select", default: "acme", options_path: kinds },
      { key: "acme_tenant", label: "Acme tenant", type: "text", visible_when: { field: "kind", contains: "acme" } },
    ]);
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith(kinds));
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    // The loaded options are drawn: "acme" is not among them, so nothing is chosen.
    fireEvent.click(screen.getByRole("combobox", { name: "Type" }));
    await waitFor(() => expect(screen.getByText("Beta")).toBeInTheDocument());
    expect(screen.queryByLabelText("Acme tenant")).not.toBeInTheDocument();
  });

  it("offers option labels with their help text and shows labels in the summary, never values", async () => {
    serve({ [LIST]: { items: [{ id: "r1", name: "Sales", mode: "jit" }] } });
    renderPage(
      [
        NAME,
        {
          key: "mode",
          label: "Who may sign in",
          type: "select",
          default: "invite_only",
          help: "What happens at a first sign-in.",
          options: [
            { value: "invite_only", label: "Only people already linked" },
            { value: "jit", label: "Create people on first sign-in" },
          ],
        },
      ],
      { summary_fields: ["mode"], show_id: false },
    );

    await screen.findByText("Sales");
    const row = screen.getByRole("table", { name: "Rules" });
    expect(within(row).getByText("Create people on first sign-in")).toBeInTheDocument();
    expect(within(row).queryByText("jit")).not.toBeInTheDocument();
    // show_id: false keeps the record id out of the table.
    expect(within(row).queryByText("r1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText("What happens at a first sign-in.")).toBeInTheDocument();
    expect(screen.getByText("Only people already linked")).toBeInTheDocument();
    expect(screen.queryByText("invite_only")).not.toBeInTheDocument();
  });

  it("blocks adding while a required reference has nothing to choose", async () => {
    serve({ [PROVIDERS]: { options: [] } });
    renderPage([
      {
        key: "provider_key",
        label: "Provider",
        type: "reference",
        required: true,
        options_path: PROVIDERS,
        empty_options_label: "Add an identity provider first.",
      },
      NAME,
    ]);

    await waitFor(() => expect(screen.getByText("Add an identity provider first.")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Add" })).toBeDisabled());
    expect(mockedFetchJson).toHaveBeenCalledWith(PROVIDERS);
  });

  it("preselects the only referenced record by its name and submits its value", async () => {
    serve({ [PROVIDERS]: { options: [{ value: "staff-directory", label: "Staff directory" }] } });
    renderPage([
      { key: "provider_key", label: "Provider", type: "reference", required: true, options_path: PROVIDERS },
      NAME,
    ]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Add" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByLabelText("Provider")).toHaveValue("Staff directory");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(lastWrite()).toEqual({ provider_key: "staff-directory", name: "example.com" }));
  });

  it("refuses a reference whose options leave the same-origin API", async () => {
    serve({});
    renderPage([
      NAME,
      { key: "provider_key", label: "Provider", type: "reference", options_path: "https://evil.example/options" },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    expect(screen.queryByLabelText("Provider")).not.toBeInTheDocument();
    expect(mockedFetchJson).not.toHaveBeenCalledWith("https://evil.example/options");
  });

  it("draws a multi-choice field with the shared multi-select and submits a list", async () => {
    serve({});
    renderPage([
      NAME,
      {
        key: "roles",
        label: "Roles",
        type: "multi_choice",
        required: true,
        default: ["viewer"],
        options: [
          { value: "operator", label: "Operator" },
          { value: "viewer", label: "Viewer" },
        ],
      },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));

    expect(screen.getByRole("combobox", { name: "Roles" })).toBeInTheDocument();
    expect(screen.getByText("Viewer")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Rule" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(lastWrite()).toEqual({ name: "Rule", roles: ["viewer"] }));
  });

  it("keeps a stored secret when its field stays blank and clears fields the form hides", async () => {
    serve({ [LIST]: { items: [{ id: "p1", name: "Company sign-in", issuer: "https://idp.example", tenants: ["a"] }] } });
    renderPage([
      NAME,
      { key: "issuer", label: "Provider address", type: "text" },
      { key: "client_secret", label: "Client secret", type: "secret" },
      { key: "tenants", label: "Allowed tenants", type: "csv", visible_when: { field: "issuer", contains: "{" } },
      { key: "claim", label: "Claim", type: "text", advanced: true },
    ]);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.queryByLabelText("Allowed tenants")).not.toBeInTheDocument();
    expect(screen.getByText("Advanced settings")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Provider address"), { target: { value: "https://idp.example/{tenant}" } });
    expect(screen.getByLabelText("Allowed tenants")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Provider address"), { target: { value: "https://idp.example" } });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(lastWrite()).toEqual({ name: "Company sign-in", issuer: "https://idp.example", tenants: [], claim: "" }),
    );
  });
});

describe("resource_report record filters", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
  });

  it("loads a select filter's choices from its options path instead of asking for an id", async () => {
    const workflows = "/api/modules/fixture/journal-workflows";
    serve({ [workflows]: { options: [{ value: "wf-1", label: "Invoice approval" }] } });
    const page = {
      key: "journal",
      page_key: "journal",
      title: "Journal",
      path: "/modules/journal",
      control_surface: {
        kind: "resource_report",
        list_path: "/api/modules/fixture/journal",
        columns: [{ key: "workflow_name", label: "Workflow", type: "text" }],
        filters: [{ key: "workflow_id", label: "Workflow", type: "select", options_path: workflows }],
      },
    };
    render(<MemoryRouter><RegisteredSettingsResourceAdminPage page={page} /></MemoryRouter>);

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith(workflows));
    expect(screen.getByRole("table", { name: "Journal" })).toBeInTheDocument();
  });
});
