/**
 * Authoring a brand-new extension Action Step.
 *
 * Every existing rendered test starts from a saved step that already carries
 * `connector_install_id`, so the identity was never derived from the catalog —
 * it was already in the draft. The clean-install path is the one that broke:
 * the catalog rows the source-change handler searches were never passed to the
 * renderer, so it wrote empty strings over the identity and the API refused the
 * save with `workflow.extension_install_missing`.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  fetchSpy,
  canvasGraphFixture,
  mockedFetchWorkflowCanvasGraph,
  dryRunChangeSets,
  postedChangeSets,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  renderPage,
} from "./index.test-helpers";
import { listFromUnknown, objectOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

const CONNECTOR_ID = "extension.fixture.mailer";
const INSTALL_ID = "install_outgoing_email_9f2c";
const EXTENSION_ID = "fixture.mailer";
const CONNECTION_ID = "conn_813e9df8d70d4835";

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function installContract() {
  return {
    install_id: INSTALL_ID,
    extension_id: EXTENSION_ID,
    install_state: "active",
    operations: [
      {
        operation_id: "send_email",
        display_name: "Send Email",
        connection_type_ids: ["smtp_account"],
        connection_label: "email account",
        connection_required: true,
      },
    ],
    connections: [
      {
        connection_id: CONNECTION_ID,
        connection_type: "smtp_account",
        display_name: "Acme Mailer",
        connected_email: "bot@example.test",
        oauth_lifecycle_status: "connected",
        runtime_status: "ready",
      },
    ],
  };
}

function mockCleanInstallStack() {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  fetchSpy.mockImplementation(async (input, options) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/meta/connector-catalog")) {
      return jsonResponse({
        connectors: [
          {
            connector_id: CONNECTOR_ID,
            display_name: "Acme Mailer",
            extension_id: EXTENSION_ID,
            connector_install_id: INSTALL_ID,
            operations: [
              { operation_id: "send_email", display_name: "Send Email" },
            ],
          },
        ],
      });
    }
    if (url.includes("/meta/extension-install-contract")) {
      return jsonResponse(installContract());
    }
    if (url.endsWith("/changes") && objectOrEmpty(options).method === "POST") {
      return jsonResponse({ workflow_id: "notify_customer", revision: 2, valid: true, issues: [], workflow: {} });
    }
    return jsonResponse({ sections: [] });
  });
  mockedFetchWorkflowDetail.mockResolvedValue({
    workflow_id: "notify_customer",
    revision: 1,
    display_name: "Notify customer",
    enabled: true,
    steps: [], groups: [],
  } as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({
    validation: { valid: true, errors: [], warnings: [] },
  } as never);
}

async function openNewActionStep() {
  const user = userEvent.setup();
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "+ Add Step" }));
  const kindSelect = await screen.findByRole("combobox", { name: /step kind/i });
  fireEvent.change(kindSelect, { target: { value: "extension_tools" } });
  return user;
}

describe("authoring a new extension action step", () => {
  it("keeps the catalog install identity when the source is selected", async () => {
    mockCleanInstallStack();
    const user = await openNewActionStep();

    const sourceSelect = await screen.findByRole("combobox", {
      name: /action source/i,
    });
    // The catalog resolves asynchronously; selecting before it lands changes
    // nothing and the assertions below would test the wrong thing.
    await user.click(sourceSelect);
    const sourceOptions = screen.getByRole("listbox", { name: "Action source options" });
    await user.click(await within(sourceOptions).findByRole("option", { name: "Acme Mailer" }));

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    await user.click(actionSelect);
    const actionOptions = screen.getByRole("listbox", { name: "Action options" });
    await user.click(await within(actionOptions).findByRole("option", { name: /Send Email/i }));

    const connectionSelect = await screen.findByRole("combobox", { name: "Connection" });
    await user.click(connectionSelect);
    const connectionOptions = screen.getByRole("listbox", { name: "Connection options" });
    await user.click(await within(connectionOptions).findByRole("option", { name: "Acme Mailer (bot@example.test)" }));

    fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply changes" }));

    await waitFor(() => {
      const saveCall = fetchSpy.mock.calls.find(
        ([url, init]) =>
          stringFromUnknownValue(url).endsWith("/changes")
          && objectOrEmpty(init).method === "POST",
      );
      expect(saveCall).toBeDefined();
      const changeSet = JSON.parse(String(objectOrEmpty(saveCall?.[1]).body || "{}"));
      expect(changeSet.expected_revision).toBe(1);
      expect(changeSet.operations[0].op).toBe("create_step");
      const body = changeSet.operations[0].step;

      // Derived from the catalog row, not carried in from a saved step.
      expect(body.connector_id).toBe(CONNECTOR_ID);
      expect(body.connector_install_id).toBe(INSTALL_ID);
      expect(body.override.connector.extension_id).toBe(EXTENSION_ID);
      expect(body.override.connector.operation_id).toBe("send_email");
      expect(body.override.connector.connection_binding.connection_id).toBe(CONNECTION_ID);

      // Canonical persistence is unchanged: identity is top-level only.
      expect(body.override.connector).not.toHaveProperty("connector_id");
      expect(body.override.connector).not.toHaveProperty("connector_install_id");
    });
  }, 15_000);

  it("does not call a never-selected action stale", async () => {
    mockCleanInstallStack();
    const user = await openNewActionStep();

    const sourceSelect = await screen.findByRole("combobox", {
      name: /action source/i,
    });
    // The catalog resolves asynchronously; selecting before it lands changes
    // nothing and the assertions below would test the wrong thing.
    await user.click(sourceSelect);
    const sourceOptions = screen.getByRole("listbox", { name: "Action source options" });
    await user.click(await within(sourceOptions).findByRole("option", { name: "Acme Mailer" }));

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    await user.click(actionSelect);
    const actionOptions = screen.getByRole("listbox", { name: "Action options" });
    await within(actionOptions).findByRole("option", { name: /Send Email/i });

    // The contract loaded and the install is active; the author simply has not
    // chosen an action yet. That is the neutral state, not a disappeared one.
    expect(
      screen.queryByText(/no longer available for this installed release/i),
    ).not.toBeInTheDocument();
    expect(
      within(actionOptions).getByRole("option", { name: /Choose action/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("does not ask authors to map the connection picker a second time", async () => {
    mockCleanInstallStack();
    fetchSpy.mockImplementation(async (input, options) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return jsonResponse({
          connectors: [
            {
              connector_id: CONNECTOR_ID,
              display_name: "Acme Mailer",
              extension_id: EXTENSION_ID,
              connector_install_id: INSTALL_ID,
              operations: [
                { operation_id: "send_email", display_name: "Send Email" },
              ],
            },
          ],
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        const baseContract = installContract();
        const contract = {
          ...baseContract,
          operations: [
            {
              ...baseContract.operations[0],
              parameters: [
                {
                  name: "connection_ref",
                  value_type: "connection_ref",
                  required: true,
                },
              ],
            },
          ],
        };
        return jsonResponse(contract);
      }
      if (url.includes("/meta/extension-form-schema")) {
        return jsonResponse({
          operation_id: "send_email",
          sections: [{ section: "General", fields: [] }],
          errors: [],
        });
      }
      if (url.includes("/steps") && objectOrEmpty(options).method === "POST") {
        return jsonResponse({ step_id: "send_the_email" });
      }
      return jsonResponse({ ok: true });
    });

    const user = await openNewActionStep();
    const sourceSelect = await screen.findByRole("combobox", {
      name: /action source/i,
    });
    await user.click(sourceSelect);
    const sourceOptions = screen.getByRole("listbox", { name: "Action source options" });
    await user.click(await within(sourceOptions).findByRole("option", { name: "Acme Mailer" }));

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    await user.click(actionSelect);
    const actionOptions = screen.getByRole("listbox", { name: "Action options" });
    await user.click(await within(actionOptions).findByRole("option", { name: /Send Email/i }));
    const connectionSelect = await screen.findByRole("combobox", { name: "Connection" });
    await user.click(connectionSelect);
    const connectionOptions = screen.getByRole("listbox", { name: "Connection options" });
    await user.click(await within(connectionOptions).findByRole("option", { name: "Acme Mailer (bot@example.test)" }));

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        `/api/workflows/meta/extension-form-schema?install_id=${INSTALL_ID}&operation_id=send_email&locale=en`,
        expect.objectContaining({ method: "GET" }),
      );
    });
    expect(screen.queryByRole("option", { name: "connection_ref" }))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply changes" }));
    await waitFor(() => {
      const saveCall = fetchSpy.mock.calls.find(
        ([url, init]) =>
          stringFromUnknownValue(url).endsWith("/changes")
          && objectOrEmpty(init).method === "POST",
      );
      expect(saveCall).toBeDefined();
      const changeSet = JSON.parse(String(objectOrEmpty(saveCall?.[1]).body || "{}"));
      expect(changeSet.expected_revision).toBe(1);
      expect(changeSet.operations[0].op).toBe("create_step");
      const body = changeSet.operations[0].step;
      expect(body.input_mapping_jsonb).toEqual({});
      expect(body.override.connector.connection_binding.connection_id).toBe(CONNECTION_ID);
    });
  });

  it("leaves optional-only MCP outputs unmapped until the author adds one", async () => {
    mockCleanInstallStack();
    const optionalOutputs = {
      type: "object",
      properties: {
        drafts: { type: "array", items: { type: "object" } },
        nextPageToken: { type: "string" },
      },
    };
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return jsonResponse({
          connectors: [{
            connector_id: CONNECTOR_ID,
            display_name: "Acme Mailer",
            extension_id: EXTENSION_ID,
            connector_install_id: INSTALL_ID,
            operations: [{
              operation_id: "list_drafts",
              display_name: "List Drafts",
              returns: optionalOutputs,
            }],
          }],
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        return jsonResponse({
          ...installContract(),
          operations: [{
            operation_id: "list_drafts",
            display_name: "List Drafts",
            connection_type_ids: ["smtp_account"],
            connection_required: true,
            returns: optionalOutputs,
          }],
        });
      }
      return jsonResponse({ sections: [] });
    });

    const user = await openNewActionStep();
    const sourceSelect = await screen.findByRole("combobox", {
      name: /action source/i,
    });
    await user.click(sourceSelect);
    const sourceOptions = screen.getByRole("listbox", { name: "Action source options" });
    await user.click(await within(sourceOptions).findByRole("option", { name: "Acme Mailer" }));

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    await user.click(actionSelect);
    const actionOptions = screen.getByRole("listbox", { name: "Action options" });
    await user.click(await within(actionOptions).findByRole("option", { name: "List Drafts" }));

    expect(screen.queryByDisplayValue("state.drafts")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("state.nextPageToken")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add output mapping" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));
    const override = JSON.parse(
      (screen.getByLabelText("Override JSON") as HTMLTextAreaElement).value,
    );
    expect(override.connector.output_mapping).toEqual({});

    fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    fireEvent.click(screen.getByRole("button", { name: "Add output mapping" }));
    expect(screen.getByRole("combobox", { name: "Returned field 1" })).toHaveValue(
      "drafts",
    );
    expect(screen.getByLabelText("Output target 1")).toHaveValue("state.drafts");
  });

  it("still warns when a saved action really has disappeared", async () => {
    // The other half of the rule: a non-empty operation id absent from the
    // loaded contract is exactly the case the warning exists for.
    mockCleanInstallStack();
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return jsonResponse({
          connectors: [
            {
              connector_id: CONNECTOR_ID,
              display_name: "Acme Mailer",
              extension_id: EXTENSION_ID,
              connector_install_id: INSTALL_ID,
            },
          ],
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        const contract = installContract();
        contract.operations = [
          {
            operation_id: "send_sms",
            display_name: "Send SMS",
            connection_type_ids: ["smtp_account"],
            connection_label: "email account",
            connection_required: true,
          },
        ];
        return jsonResponse(contract);
      }
      return jsonResponse({ sections: [] });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "notify_customer",
      display_name: "Notify customer",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-1111111110aa",
              step_id: "send_the_email",
              display_name: "Send the email",
              step_kind: "connector",
              connector_id: CONNECTOR_ID,
              connector_install_id: INSTALL_ID,
              override: {
                connector: {
                  operation_id: "send_email",
                  connection_binding: { connection_id: CONNECTION_ID },
                },
              },
            }], groups: [],
    } as never);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Send the email/i }));

    expect(
      await screen.findByText("Saved action is no longer available for this installed release."),
    ).toBeInTheDocument();
    const action = await screen.findByRole("combobox", { name: "Action" });
    expect(action).toHaveTextContent(/Unavailable action/i);
    expect(action).not.toHaveTextContent("send_email");
    fireEvent.click(action);
    const choices = screen.getByRole("listbox", { name: "Action options" });
    expect(within(choices).getByRole("option", { name: /Unavailable action/i })).toHaveAttribute("aria-selected", "true");
    expect(within(choices).getByRole("option", { name: /Send SMS/i })).toBeInTheDocument();
    fireEvent.keyDown(choices, { key: "Escape" });

    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));
    const override = JSON.parse((screen.getByLabelText("Override JSON") as HTMLTextAreaElement).value);
    expect(override.connector.operation_id).toBe("send_email");
    expect(override.connector.connection_binding.connection_id).toBe(CONNECTION_ID);
  });
});

describe("the action source catalog follows the interface language", () => {
  it("asks the catalog in the user's language, so a translated extension name is shown", async () => {
    mockCleanInstallStack();
    await openNewActionStep();
    await screen.findByRole("combobox", { name: /action source/i });

    const catalogUrls = fetchSpy.mock.calls
      .map((call) => stringFromUnknownValue(call[0]))
      .filter((url) => url.includes("/meta/connector-catalog"));
    expect(catalogUrls.length).toBeGreaterThan(0);
    for (const url of catalogUrls) {
      expect(new URL(url, "http://localhost").searchParams.get("locale")).toBeTruthy();
      expect(url).toContain("project_id=project_alpha");
    }
  });
});

describe("the extension a new action step comes from", () => {
  it("shows its logo beside the action source, with no step card or source chip in the drawer", async () => {
    mockCleanInstallStack();
    const cleanInstall = fetchSpy.getMockImplementation();
    fetchSpy.mockImplementation(async (input, options) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return jsonResponse({
          connectors: [
            {
              connector_id: CONNECTOR_ID,
              display_name: "Acme Mailer",
              source: "extension",
              extension_id: EXTENSION_ID,
              connector_install_id: INSTALL_ID,
              operations: [{ operation_id: "send_email", display_name: "Send Email" }],
            },
          ],
        });
      }
      return cleanInstall!(input, options);
    });
    const user = await openNewActionStep();

    await user.click(await screen.findByRole("combobox", { name: /action source/i }));
    const sourceOptions = screen.getByRole("listbox", { name: "Action source options" });
    const option = await within(sourceOptions).findByRole("option", { name: "Acme Mailer" });
    expect(within(option).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-inline");
    expect(within(option).getByRole("presentation")).toHaveAttribute("src", `/api/extensions/${EXTENSION_ID}/icon`);
    await user.click(option);

    // The chosen source keeps its logo in the picker. The drawer has no inset step card
    // any more (title and step id sit in the header), and no source chip repeats the picker.
    const source = await screen.findByRole("combobox", { name: /action source/i });
    expect(source).toHaveTextContent("Acme Mailer");
    expect(within(source).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-inline");
    expect(within(source).getByRole("presentation")).toHaveAttribute("src", `/api/extensions/${EXTENSION_ID}/icon`);
    expect(screen.queryByRole("img", { name: "From extension Acme Mailer" })).not.toBeInTheDocument();
    expect(screen.queryByText("extension", { exact: true })).not.toBeInTheDocument();
  });
});

// Exercise the actual canvas command, prepared drawer and canonical local graph.
it.each([
  ["System tools", "connector", "Tool"],
  ["MCP tools", "mcp_tools", "Tool"],
  ["Extension tools", "extension_tools", "Action source"],
])("opens %s setup after palette creation without persisting a presentation kind", async (label, category, sourceLabel) => {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture());
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const tools = await screen.findByRole("toolbar", { name: "Canvas tools" });
  fireEvent.click(within(tools).getByRole("button", { name: "Add step" }));
  fireEvent.click(await screen.findByRole("option", { name: label }));
  const drawer = await screen.findByRole("dialog", { name: "Step editor" });
  expect(within(drawer).getByRole("combobox", { name: "Step kind" })).toHaveValue(category);
  expect(within(drawer).getByRole("combobox", { name: sourceLabel })).toBeInTheDocument();
  expect(within(drawer).getByRole("combobox", { name: "Action" })).toHaveTextContent("Choose action");
  await waitFor(() => {
    const created = dryRunChangeSets().flatMap((body) => listFromUnknown(body.operations)).find((operation) => operation.op === "create_step");
    expect(created).toMatchObject({ op: "create_step", step: { step_kind: "connector", display_name: label } });
    const step = objectOrEmpty(created?.step);
    expect(step).not.toHaveProperty("toolCategory");
    expect(objectOrEmpty(objectOrEmpty(step.override).connector).operation_id).toBe("");
    expect(step.connector_id).toBe("");
    expect(step.connector_install_id).toBe("");
  });
  expect(postedChangeSets()).toEqual([]);
}, 15_000);

/** JSDOM has no hit-testing layout. Model only this dispatched card click's
 * known target stack, preserving the production secondary-action hit-test path. */
function clickCard(target: HTMLElement, init: MouseEventInit = {}) {
  const descriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  const stack: Element[] = [];

  for (let element: Element | null = target; element; element = element.parentElement) stack.push(element);
  Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: (x: number, y: number) => {
    if (x !== (init.clientX ?? 0) || y !== (init.clientY ?? 0)) throw new Error("Unmodelled hit-test point outside the dispatched card click");
    return stack;
  } });
  try { fireEvent.click(target, init); }
  finally {
    if (descriptor) Object.defineProperty(document, "elementsFromPoint", descriptor);
    else Reflect.deleteProperty(document, "elementsFromPoint");
  }
}

it.each(["MCP tools", "Extension tools"])("retains an unfinished %s category after the real page drawer closes and reopens", async (label) => {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture());
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const tools = await screen.findByRole("toolbar", { name: "Canvas tools" });
  fireEvent.click(within(tools).getByRole("button", { name: "Add step" }));
  fireEvent.click(await screen.findByRole("option", { name: label }));
  const category = label === "MCP tools" ? "mcp_tools" : "extension_tools";
  let drawer = await screen.findByRole("dialog", { name: "Step editor" });
  expect(within(drawer).getByRole("combobox", { name: "Step kind" })).toHaveValue(category);
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  clickCard(within(canvas).getByRole("group", { name: `Open step ${label}` }));
  drawer = await screen.findByRole("dialog", { name: "Step editor" });
  expect(within(drawer).getByRole("combobox", { name: "Step kind" })).toHaveValue(category);
  expect(within(drawer).getByRole("combobox", { name: label === "MCP tools" ? "Tool" : "Action source" })).toHaveTextContent(/Choose/);
  expect(within(drawer).getByRole("combobox", { name: "Action" })).toHaveTextContent("Choose action");
  expect(postedChangeSets()).toEqual([]);
}, 15_000);
