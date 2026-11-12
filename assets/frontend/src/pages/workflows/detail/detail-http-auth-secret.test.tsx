import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { createCatalogSecret, fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import {
  bindWorkflowInputSecret,
  fetchWorkflowSecretApproval,
  saveWorkflowInputSecretValue,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";
import { HttpAuthSecretField } from "./detail-http-auth-secret";
import { createWorkflowInterfaceFieldDraft } from "./detail-interface";
import { splitBasicAuthRef, storedSecretInputName } from "./detail-http-auth-model";

vi.mock("@/pages/registry/secrets/api", () => ({
  CATALOG_SECRETS_QUERY_KEY: ["secrets", "all"],
  fetchCatalogSecrets: vi.fn(),
  createCatalogSecret: vi.fn(),
}));
vi.mock("@/pages/workflows/shared/workflow-secret-catalog-api", () => ({
  bindWorkflowInputSecret: vi.fn(),
  fetchWorkflowSecretApproval: vi.fn(),
  saveWorkflowInputSecretValue: vi.fn(),
  workflowSecretApprovalQueryKey: (projectId: string, workflowId: string, revision?: number | null) => [
    "workflows", workflowId, "secret-approval", projectId, revision ?? null,
  ],
}));

const savedInput = createWorkflowInterfaceFieldDraft({ name: "token", type: "secret", originalAuthoredRow: { name: "token", type: "secret" } });

function renderField(props: Partial<Parameters<typeof HttpAuthSecretField>[0]> = {}) {
  const onAuthRefChange = vi.fn();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <HttpAuthSecretField
        authMode="bearer"
        authRef=""
        inputClass="fs-input"
        inputs={[]}
        onAuthRefChange={onAuthRefChange}
        projectId="p1"
        workflowId="wf1"
        {...props}
      />
    </QueryClientProvider>,
  );
  return { onAuthRefChange };
}

async function pick(name: string, option: RegExp) {
  const trigger = screen.getByRole("combobox", { name });
  await waitFor(() => expect(trigger).toBeEnabled());
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

describe("HttpAuthSecretField", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCatalogSecrets).mockResolvedValue([
      { id: "s1", name: "Shop API key", description: "" },
      { id: "s2", name: "Other key", description: "" },
    ]);
    vi.mocked(fetchWorkflowSecretApproval).mockResolvedValue({
      workflow_id: "wf1", status: "approval_required", fingerprint: "sha256:x",
      secrets: [{ field: "token", secret_id: "s1" }], destinations: [], dynamic_http_steps: [], approved_destinations: [],
    });
  });

  it("turns a stored secret into a bound secret input when the step has none", async () => {
    const addBoundInput = vi.fn().mockResolvedValue(undefined);
    const { onAuthRefChange } = renderField({ addBoundInput });
    await pick("Stored secret for Token", /^Shop API key/);

    await waitFor(() => expect(addBoundInput).toHaveBeenCalledTimes(1));
    expect(addBoundInput.mock.calls[0][0]).toMatchObject({ name: "token", type: "secret", catalogSecretId: "s1" }); // pragma: allowlist secret - a form key / test id, not a secret
    await waitFor(() => expect(onAuthRefChange).toHaveBeenCalledWith("{{ state.inputs.token }}"));
  });

  it("shows the chosen secret, hides the typed value and changes it by rebinding the input", async () => {
    vi.mocked(bindWorkflowInputSecret).mockResolvedValue({} as never);
    renderField({ authRef: "{{ state.inputs.token }}", inputs: [savedInput] });

    await waitFor(() => expect(screen.getByText("Shop API key")).toBeInTheDocument());
    expect(screen.queryByLabelText("Value of Token")).not.toBeInTheDocument();
    await pick("Stored secret for Token", /^Other key/);
    await waitFor(() => expect(bindWorkflowInputSecret).toHaveBeenCalledWith("p1", "wf1", "token", "s2"));
  });

  it("types a value instead; saving the step keeps it as a stored secret of the input", async () => {
    vi.mocked(saveWorkflowInputSecretValue).mockResolvedValue({} as never);
    const tasks = new Map<string, () => Promise<void>>();
    const registerSaveTask = (key: string, task: (() => Promise<void>) | null) => (task ? tasks.set(key, task) : tasks.delete(key));
    renderField({ authRef: "{{ state.inputs.token }}", inputs: [savedInput], registerSaveTask });
    fireEvent.click(await screen.findByRole("button", { name: "Stop using the stored secret for Token" }));

    fireEvent.change(screen.getByLabelText("Value of Token"), { target: { value: "typed-token" } });
    // No button of its own: the step's own save applies it.
    expect(screen.queryByRole("button", { name: "Use this value" })).not.toBeInTheDocument();
    expect(saveWorkflowInputSecretValue).not.toHaveBeenCalled();
    await act(async () => { for (const task of tasks.values()) await task(); });
    expect(saveWorkflowInputSecretValue).toHaveBeenCalledWith("p1", "wf1", "token", "typed-token");
  });

  it("names the input as soon as a value is typed for a step without one, and creates it on save", async () => {
    vi.mocked(createCatalogSecret).mockResolvedValue({ id: "s9", name: "wf1 / token", description: "" });
    const addBoundInput = vi.fn().mockResolvedValue(undefined);
    const tasks = new Map<string, () => Promise<void>>();
    const registerSaveTask = (key: string, task: (() => Promise<void>) | null) => (task ? tasks.set(key, task) : tasks.delete(key));
    const { onAuthRefChange } = renderField({ addBoundInput, registerSaveTask });
    fireEvent.change(screen.getByLabelText("Value of Token"), { target: { value: "typed-token" } });
    expect(onAuthRefChange).toHaveBeenCalledWith("{{ state.inputs.token }}");

    await act(async () => { for (const task of tasks.values()) await task(); });
    expect(createCatalogSecret).toHaveBeenCalledWith({ name: "wf1 / token", description: "", value: "typed-token" });
    expect(addBoundInput.mock.calls[0][0]).toMatchObject({ name: "token", catalogSecretId: "s9" }); // pragma: allowlist secret - a form key / test id, not a secret
  });
});

describe("HTTP auth reference model", () => {
  it("splits basic auth on the colon outside templates and names inputs after secrets", () => {
    expect(splitBasicAuthRef("{{ state.inputs.a }}:{{ state.inputs.b }}")).toEqual(["{{ state.inputs.a }}", "{{ state.inputs.b }}"]);
    expect(storedSecretInputName("Shop API key", [])).toBe("shop_api_key");
    expect(storedSecretInputName("Shop API key", ["shop_api_key"])).toBe("shop_api_key_2");
    expect(storedSecretInputName("2FA", [])).toBe("secret_2fa");
  });
});
