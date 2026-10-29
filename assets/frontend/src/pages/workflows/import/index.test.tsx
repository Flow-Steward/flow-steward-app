import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import {
  applyWorkflowImport,
  fetchWorkflowImportOptions,
  preflightWorkflowImport,
  type WorkflowImportPreflight,
} from "@/pages/workflows/import/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { WorkflowsImportPage } from "@/pages/workflows/import";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(),
}));

vi.mock("@/pages/workflows/import/api", () => ({
  applyWorkflowImport: vi.fn(),
  fetchWorkflowImportOptions: vi.fn(),
  preflightWorkflowImport: vi.fn(),
}));

const mockedApplyWorkflowImport = vi.mocked(applyWorkflowImport);
const mockedFetchWorkflowImportOptions = vi.mocked(fetchWorkflowImportOptions);
const mockedPreflightWorkflowImport = vi.mocked(preflightWorkflowImport);
const mockedFetchProjectsList = vi.mocked(fetchProjectsList);

function renderPage(initialEntry = "/registry/workflows/import", locale = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, fallback = "", params) => fallbackContext.t(key, locale === "lt" ? ({
      "react.workflows.import_preflight": "Išankstinė patikra",
      "react.workflows.import_resolve": "Priklausomybių susiejimas",
      "react.workflows.cannot_be_resolved_in_this_wizard": "Negalima išspręsti šiame vedlyje.",
      "react.workflows.fix_the_source_yaml_before_import": "Prieš importuodami pataisykite pradinį YAML.",
    } as Record<string, string>)[key] || fallback : fallback, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <WorkflowsImportPage />
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept;
    reject = refuse;
  });
  return { promise, resolve, reject };
}

function readyPreflight(workflowId = "wf_ready"): WorkflowImportPreflight {
  return {
    schema_version: "platform.workflow_bundle.v1",
    kind: "workflow_import_preflight",
    ok: true,
    can_apply: true,
    summary: { workflow_count: 1, reference_count: 0, dependency_count: 0, blocking_count: 0 },
    workflows: [{ workflow_id: workflowId }],
    dependencies: [],
    required_resolution_actions: [],
  };
}

describe("WorkflowsImportPage", () => {
  it("localizes generated progress chips while retaining authored YAML", async () => {
    renderPage(undefined, "lt");
    const input = await screen.findByLabelText("Workflow bundle YAML");
    const yaml = "workflow_id: Preflight\ndisplay_name: Resolve Ąžuolas\n";
    fireEvent.change(input, { target: { value: yaml } });
    expect(screen.getByText(/Išankstinė patikra/)).toBeInTheDocument();
    expect(screen.getByText(/Priklausomybių susiejimas/)).toBeInTheDocument();
    expect(input).toHaveValue(yaml);
    expect(screen.getByText(/^1\. YAML$/)).toBeInTheDocument();
  });
  beforeEach(() => {
    mockedApplyWorkflowImport.mockReset();
    mockedFetchWorkflowImportOptions.mockReset();
    mockedPreflightWorkflowImport.mockReset();
    mockedFetchProjectsList.mockReset();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [
        { id: "project-123", name: "Release Candidate" },
        { id: "project-456", name: "Integration Lab" },
      ],
      page: 1,
      per_page: 200,
      total: 2,
      total_pages: 1,
    });
    mockedFetchWorkflowImportOptions.mockResolvedValue({
      kind: "workflow_import_dependency_options",
      options: {
        agent: [
          {
            value: "writer",
            label: "Writer",
            description: "Business researcher",
            enabled: true,
          },
        ],
      },
    });
  });

  it("requires a server check of the exact resolution plan before import", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: false,
      can_apply: false,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 2,
        blocking_count: 2,
      },
      workflows: [{ workflow_id: "wf_existing", display_name: "Existing" }],
      dependencies: [
        {
          dependency_key: "workflow:wf_existing",
          dependency_type: "workflow",
          ref: "wf_existing",
          label: "wf_existing",
          status: "conflict",
          blocking: true,
          required: true,
          sources: ["bundle.workflows"],
          resolution_actions: ["rename_workflow", "abort"],
          metadata: { code: "workflow_id_conflict" },
        },
        {
          dependency_key: "agent:missing_agent",
          dependency_type: "agent",
          ref: "missing_agent",
          label: "missing_agent",
          status: "requires_mapping",
          blocking: true,
          required: true,
          sources: ["analysis.plan"],
          resolution_actions: ["map_existing", "abort"],
          metadata: {},
        },
      ],
      required_resolution_actions: [
        "abort",
        "map_existing",
        "rename_workflow",
      ],
    });
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      ...readyPreflight("wf_existing_copy"),
      summary: { workflow_count: 1, reference_count: 0, dependency_count: 1, blocking_count: 0 },
      // The accepted root rename is absent from the effective response. The
      // editable original row must still provide its identity at Apply.
      dependencies: [{
        dependency_key: "agent:missing_agent",
        dependency_type: "agent",
        ref: "missing_agent",
        status: "resolved",
        blocking: false,
        resolution_actions: ["use_existing", "abort"],
      }],
    });
    mockedApplyWorkflowImport.mockResolvedValueOnce({
      status: "imported",
      workflow_id: "wf_existing_copy",
      imported_workflows: [
        {
          workflow_id: "wf_existing_copy",
          source_workflow_id: "wf_existing",
          enabled: false,
          import_state: "draft",
        },
      ],
      preflight: {} as never,
    });

    renderPage();

    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(await screen.findByText("Blocked")).toBeInTheDocument();
    const importButton = screen.getByRole("button", { name: /import workflow/i });
    expect(importButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("New workflow id for wf_existing"), {
      target: { value: "wf_existing_copy" },
    });
    fireEvent.focus(screen.getByLabelText("Existing agent for missing_agent"));
    fireEvent.change(screen.getByLabelText("Existing agent for missing_agent"), {
      target: { value: "Writer" },
    });
    fireEvent.click(screen.getByRole("option", { name: /Writer/ }));

    expect(importButton).toBeDisabled();
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => {
      expect(mockedPreflightWorkflowImport).toHaveBeenNthCalledWith(2, {
        bundleYaml: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle",
        targetWorkflowId: "",
        resolutions: [
          {
            action: "rename_workflow",
            dependency_key: "workflow:wf_existing",
            dependency_type: "workflow",
            ref: "wf_existing",
            target_workflow_id: "wf_existing_copy",
          },
          {
            action: "map_existing",
            dependency_key: "agent:missing_agent",
            dependency_type: "agent",
            ref: "missing_agent",
            target_ref: "writer",
          },
        ],
      });
    });
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(importButton).toBeEnabled();
    expect(screen.getByLabelText("New workflow id for wf_existing")).toHaveValue("wf_existing_copy");
    fireEvent.click(importButton);

    await waitFor(() => {
      expect(mockedApplyWorkflowImport).toHaveBeenCalledWith({
        bundleYaml: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle",
        targetWorkflowId: "",
        projectId: "",
        resolutions: [
          {
            action: "rename_workflow",
            dependency_key: "workflow:wf_existing",
            dependency_type: "workflow",
            ref: "wf_existing",
            target_workflow_id: "wf_existing_copy",
          },
          {
            action: "map_existing",
            dependency_key: "agent:missing_agent",
            dependency_type: "agent",
            ref: "missing_agent",
            target_ref: "writer",
          },
        ],
      });
    });
    expect(await screen.findByText("Open imported workflow")).toHaveAttribute(
      "href",
      "/registry/workflows/wf_existing_copy/builder",
    );
  });

  it("accepts a checked fresh explicit root target without a redundant row rename", async () => {
    const conflict = {
      dependency_key: "workflow:wf_existing", dependency_type: "workflow" as const,
      ref: "wf_existing", status: "conflict" as const, blocking: true,
      resolution_actions: ["rename_workflow", "abort"], metadata: { code: "workflow_id_conflict" },
    };
    mockedPreflightWorkflowImport
      .mockResolvedValueOnce({
        ...readyPreflight("wf_existing"), ok: false, can_apply: false,
        summary: { workflow_count: 1, reference_count: 0, dependency_count: 1, blocking_count: 1 },
        dependencies: [conflict],
      })
      .mockResolvedValueOnce(readyPreflight("wf_fresh"));
    mockedApplyWorkflowImport.mockResolvedValueOnce({
      status: "imported", workflow_id: "wf_fresh", imported_workflows: [], preflight: {} as never,
    });
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const rowTarget = await screen.findByLabelText("New workflow id for wf_existing");
    expect(rowTarget).toHaveValue("");
    fireEvent.change(screen.getByLabelText("Target workflow ID"), { target: { value: "wf_fresh" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(mockedPreflightWorkflowImport).toHaveBeenNthCalledWith(2, {
      bundleYaml: "bundle", targetWorkflowId: "wf_fresh", resolutions: [],
    }));
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(rowTarget).toHaveValue("");
    const importButton = screen.getByRole("button", { name: /import workflow/i });
    expect(importButton).toBeEnabled();
    fireEvent.click(importButton);
    await waitFor(() => expect(mockedApplyWorkflowImport).toHaveBeenCalledWith({
      bundleYaml: "bundle", targetWorkflowId: "wf_fresh", projectId: "", resolutions: [],
    }));
  });

  it("keeps an occupied rename blocked until a free target is checked", async () => {
    const conflict = {
      dependency_key: "workflow:wf_existing",
      dependency_type: "workflow" as const,
      ref: "wf_existing",
      status: "conflict" as const,
      blocking: true,
      resolution_actions: ["rename_workflow", "abort"],
      metadata: { code: "workflow_id_conflict" },
    };
    const blocked = {
      ...readyPreflight("wf_existing"),
      ok: false,
      can_apply: false,
      summary: { workflow_count: 1, reference_count: 0, dependency_count: 1, blocking_count: 1 },
      dependencies: [conflict],
    };
    mockedPreflightWorkflowImport.mockResolvedValueOnce(blocked);
    mockedPreflightWorkflowImport.mockResolvedValueOnce(blocked);
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight("wf_free"));
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const target = await screen.findByLabelText("New workflow id for wf_existing");
    const importButton = screen.getByRole("button", { name: /import workflow/i });
    fireEvent.change(target, { target: { value: "wf_occupied" } });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(mockedPreflightWorkflowImport).toHaveBeenCalledTimes(2));
    expect(importButton).toBeDisabled();
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    fireEvent.change(target, { target: { value: "wf_free" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(importButton).toBeEnabled();
  });

  it("discards a preflight result after the YAML changes before it settles", async () => {
    const first = deferred<WorkflowImportPreflight>();
    mockedPreflightWorkflowImport.mockReturnValueOnce(first.promise);
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight("wf_new"));
    renderPage();
    const yaml = screen.getByLabelText("Workflow bundle YAML");
    fireEvent.change(yaml, { target: { value: "bundle A" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    fireEvent.change(yaml, { target: { value: "bundle B" } });
    first.resolve(readyPreflight("wf_old"));
    await waitFor(() => expect(mockedPreflightWorkflowImport).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("2. Preflight summary")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(mockedPreflightWorkflowImport).toHaveBeenNthCalledWith(2, {
      bundleYaml: "bundle B", targetWorkflowId: "", resolutions: [],
    });
  });

  it("invalidates accepted root and project scope immediately, even if the root value is restored", async () => {
    mockedPreflightWorkflowImport.mockResolvedValue(readyPreflight("wf_scope"));
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const importButton = await screen.findByRole("button", { name: /import workflow/i });
    await waitFor(() => expect(importButton).toBeEnabled());

    const target = screen.getByLabelText("Target workflow ID");
    fireEvent.change(target, { target: { value: "wf_other" } });
    expect(importButton).toBeDisabled();
    fireEvent.change(target, { target: { value: "" } });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(importButton).toBeEnabled());

    const project = await screen.findByLabelText("Project");
    await screen.findByRole("option", { name: "Release Candidate" });
    fireEvent.change(project, { target: { value: "project-123" } });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(importButton).toBeEnabled());
    expect(mockedFetchWorkflowImportOptions).toHaveBeenLastCalledWith({ projectId: "project-123" });
  });

  it("keeps the newer check pending when an older check rejects and finally settles", async () => {
    const older = deferred<WorkflowImportPreflight>();
    const newer = deferred<WorkflowImportPreflight>();
    mockedPreflightWorkflowImport.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    fireEvent.change(screen.getByLabelText("Target workflow ID"), { target: { value: "wf_new" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(mockedPreflightWorkflowImport).toHaveBeenCalledTimes(2));
    await act(async () => { older.reject(new Error("Old check failed")); });
    expect(screen.queryByText("Old check failed")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /running preflight/i })).toBeDisabled();
    await act(async () => { newer.resolve(readyPreflight("wf_new")); });
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(mockedPreflightWorkflowImport).toHaveBeenNthCalledWith(2, {
      bundleYaml: "bundle", targetWorkflowId: "wf_new", resolutions: [],
    });
  });

  it("ignores a late options response from an obsolete preflight", async () => {
    const oldOptions = deferred<Awaited<ReturnType<typeof fetchWorkflowImportOptions>>>();
    const missing = {
      ...readyPreflight("wf_options"), ok: false, can_apply: false,
      summary: { workflow_count: 1, reference_count: 0, dependency_count: 1, blocking_count: 1 },
      dependencies: [{
        dependency_key: "agent:missing_agent", dependency_type: "agent" as const,
        ref: "missing_agent", status: "requires_mapping" as const, blocking: true,
        resolution_actions: ["map_existing", "abort"],
      }],
    };
    mockedPreflightWorkflowImport.mockResolvedValue(missing);
    mockedFetchWorkflowImportOptions.mockReturnValueOnce(oldOptions.promise).mockResolvedValueOnce({
      kind: "workflow_import_dependency_options",
      options: { agent: [{ value: "writer", label: "Writer", enabled: true }] },
    });
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    fireEvent.change(screen.getByLabelText("Target workflow ID"), { target: { value: "wf_new" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const picker = await screen.findByLabelText("Existing agent for missing_agent");
    fireEvent.focus(picker);
    expect(screen.getByRole("option", { name: /Writer/ })).toBeInTheDocument();
    await act(async () => { oldOptions.resolve({
      kind: "workflow_import_dependency_options",
      options: { agent: [{ value: "legacy", label: "Legacy", enabled: true }] },
    }); });
    fireEvent.focus(picker);
    expect(screen.getByRole("option", { name: /Writer/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Legacy/ })).not.toBeInTheDocument();
    expect(mockedFetchWorkflowImportOptions).toHaveBeenCalledTimes(2);
  });

  it("revokes a checked resolution immediately and ignores an Apply result after a newer edit", async () => {
    const conflict = {
      dependency_key: "workflow:wf_existing", dependency_type: "workflow" as const,
      ref: "wf_existing", status: "conflict" as const, blocking: true,
      resolution_actions: ["rename_workflow", "abort"], metadata: { code: "workflow_id_conflict" },
    };
    mockedPreflightWorkflowImport
      .mockResolvedValueOnce({
        ...readyPreflight("wf_existing"), ok: false, can_apply: false,
        summary: { workflow_count: 1, reference_count: 0, dependency_count: 1, blocking_count: 1 },
        dependencies: [conflict],
      })
      .mockResolvedValueOnce(readyPreflight("wf_copy"))
      .mockResolvedValueOnce(readyPreflight("wf_copy"));
    const apply = deferred<Awaited<ReturnType<typeof applyWorkflowImport>>>();
    mockedApplyWorkflowImport.mockReturnValueOnce(apply.promise);
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const target = await screen.findByLabelText("New workflow id for wf_existing");
    fireEvent.change(target, { target: { value: "wf_copy" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const importButton = screen.getByRole("button", { name: /import workflow/i });
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.change(target, { target: { value: "wf_other" } });
    expect(importButton).toBeDisabled();
    fireEvent.change(target, { target: { value: "wf_copy" } });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);
    expect(mockedApplyWorkflowImport).toHaveBeenCalledWith({
      bundleYaml: "bundle", targetWorkflowId: "", projectId: "",
      resolutions: [{
        action: "rename_workflow", dependency_key: "workflow:wf_existing",
        dependency_type: "workflow", ref: "wf_existing", target_workflow_id: "wf_copy",
      }],
    });
    fireEvent.change(target, { target: { value: "wf_later" } });
    await act(async () => { apply.resolve({
      status: "imported", workflow_id: "wf_copy", imported_workflows: [], preflight: {} as never,
    }); });
    expect(screen.queryByText("Open imported workflow")).not.toBeInTheDocument();
    expect(importButton).toBeDisabled();
  });

  it("shows an Apply-time server collision refusal without inventing a result", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight("wf_import"));
    mockedApplyWorkflowImport.mockRejectedValueOnce(new Error("Workflow id became reserved before import."));
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    const importButton = await screen.findByRole("button", { name: /import workflow/i });
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);
    expect(await screen.findByText("Workflow id became reserved before import.")).toBeInTheDocument();
    expect(mockedApplyWorkflowImport).toHaveBeenCalledWith({
      bundleYaml: "bundle", targetWorkflowId: "", projectId: "", resolutions: [],
    });
    expect(screen.queryByText("Open imported workflow")).not.toBeInTheDocument();
  });

  it("surfaces malformed YAML errors without moving into preflight", async () => {
    mockedPreflightWorkflowImport.mockRejectedValueOnce(
      new Error("YAML bundle could not be parsed."),
    );

    renderPage();

    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: [\nkind: workflow_bundle\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(await screen.findByText("YAML bundle could not be parsed.")).toBeInTheDocument();
    expect(screen.queryByText("2. Preflight summary")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /import workflow/i })).not.toBeInTheDocument();
  });

  it("loads project-scoped connection options from the import URL", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: true,
      can_apply: true,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 0,
        blocking_count: 0,
      },
      workflows: [{ workflow_id: "wf_project_import", display_name: "Project import" }],
      dependencies: [],
      required_resolution_actions: [],
    });

    renderPage("/registry/workflows/import?project_id=project-123");

    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    await waitFor(() => {
      expect(mockedFetchWorkflowImportOptions).toHaveBeenCalledWith({
        projectId: "project-123",
      });
    });
  });

  it("keeps project context when mapping an imported project connection", async () => {
    mockedFetchWorkflowImportOptions.mockResolvedValueOnce({
      kind: "workflow_import_dependency_options",
      options: {
        project_connection: [
          {
            value: "conn-existing",
            label: "Supplier SFTP",
            enabled: true,
          },
        ],
      },
    });
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: false,
      can_apply: false,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 1,
        blocking_count: 1,
      },
      workflows: [{ workflow_id: "wf_project_import", display_name: "Project import" }],
      dependencies: [
        {
          dependency_key: "project_connection:conn-source",
          dependency_type: "project_connection",
          ref: "conn-source",
          label: "Supplier connection",
          status: "requires_project_connection",
          blocking: true,
          required: true,
          sources: ["fetch.connection_binding"],
          resolution_actions: ["map_existing", "abort"],
          metadata: {},
        },
      ],
      required_resolution_actions: ["map_existing", "abort"],
    });
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight("wf_project_import"));
    mockedApplyWorkflowImport.mockResolvedValueOnce({
      status: "imported",
      workflow_id: "wf_project_import",
      imported_workflows: [],
      preflight: {} as never,
    });

    renderPage("/registry/workflows/import?project_id=project-123");
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    const picker = await screen.findByLabelText(
      "Existing project connection for conn-source",
    );
    fireEvent.focus(picker);
    fireEvent.click(screen.getByRole("option", { name: /Supplier SFTP/ }));
    const importButton = screen.getByRole("button", { name: /import workflow/i });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);

    await waitFor(() => {
      expect(mockedApplyWorkflowImport).toHaveBeenCalledWith(
        expect.objectContaining({
          resolutions: [
            expect.objectContaining({
              action: "map_existing",
              dependency_key: "project_connection:conn-source",
              project_id: "project-123",
              target_ref: "conn-existing",
            }),
          ],
        }),
      );
    });
  });

  it.each(["en", "lt"])("shows blocked child workflow None as a source YAML defect, not a mappable workflow in %s", async (locale) => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: false,
      can_apply: false,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 1,
        blocking_count: 1,
      },
      workflows: [{ workflow_id: "wf_source", display_name: "Source" }],
      dependencies: [
        {
          dependency_key: "child_workflow:None",
          dependency_type: "child_workflow",
          ref: "None",
          label: "Missing workflow id",
          status: "blocked",
          blocking: true,
          required: true,
          sources: ["analysis.step_1"],
          resolution_actions: ["abort"],
          message:
            "Bundle references a child workflow dependency without a usable id. Fix the source YAML or exported workflow before import.",
          metadata: { code: "invalid_dependency_ref", raw_ref: "None" },
        },
      ],
      required_resolution_actions: ["abort"],
    });

    renderPage("/registry/workflows/import", locale);

    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle" },
    });
    fireEvent.change(screen.getByLabelText("Target workflow ID"), {
      target: { value: "wf_source_imported" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(await screen.findByText("Missing workflow id")).toBeInTheDocument();
    expect(screen.getByText("child_workflow / None")).toBeInTheDocument();
    expect(screen.getByText(/without a usable id/i)).toBeInTheDocument();
    expect(screen.getByText(locale === "lt" ? "Negalima išspręsti šiame vedlyje." : "Cannot be resolved in this wizard.")).toBeInTheDocument();
    expect(screen.getByText(locale === "lt" ? "Prieš importuodami pataisykite pradinį YAML." : "Fix the source YAML before import.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Existing workflow for None")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import workflow/i })).toBeDisabled();
  });

  it("explains missing workflow_id as a workflow definition problem", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: false,
      can_apply: false,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 1,
        blocking_count: 1,
      },
      workflows: [{ workflow_id: "", display_name: "Workflow 1" }],
      dependencies: [
        {
          dependency_key: "workflow:workflows[0]",
          dependency_type: "workflow",
          ref: "workflows[0]",
          label: "Workflow 1",
          status: "blocked",
          blocking: true,
          required: true,
          sources: ["bundle.workflows"],
          resolution_actions: ["abort"],
          message: "Workflow definition is missing workflow_id.",
          metadata: { code: "workflow_id_missing" },
        },
      ],
      required_resolution_actions: ["abort"],
    });

    renderPage();

    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), {
      target: { value: "schema_version: platform.workflow_bundle.v1\nkind: workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(await screen.findByText("Workflow 1")).toBeInTheDocument();
    expect(screen.getByText("Workflow definition is missing workflow_id.")).toBeInTheDocument();
    expect(screen.getByText("Fix the source YAML before import.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import workflow/i })).toBeDisabled();
  });

  // IMPORT-001: the catalog button navigated without the project query, and this
  // page reads only `?project_id=`, so normal navigation silently dropped the
  // project that project connections resolve against.
  it("offers a project when navigation did not carry one", async () => {
    renderPage("/registry/workflows/import");

    const picker = await screen.findByLabelText("Project");

    expect(picker).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "Release Candidate" })).toBeInTheDocument();
  });

  // The page decided Apply purely from dependency resolutions, so a bundle whose
  // only defect was structural showed an enabled Import button that the backend
  // then refused. Structure is not something a resolution on this page can fix.
  it("refuses to import a bundle the backend has already ruled out", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: false,
      can_apply: false,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 0,
        blocking_count: 0,
      },
      workflows: [{ workflow_id: "wf_broken", display_name: "Broken" }],
      dependencies: [],
      required_resolution_actions: [],
      topology_issues: [
        {
          code: "workflow.topology.branch_target_missing",
          message: "Edge target 'step_gone' does not exist.",
          path: "settings.execution_graph.edges[1].target",
          severity: "error",
          workflow_id: "wf_broken",
          blocking: true,
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByLabelText(/bundle/i), {
      target: { value: "kind: platform.workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    // The operator is told which structural defect stops the import, and that
    // the dependency form below cannot clear it.
    expect(
      await screen.findByText(/workflow\.topology\.branch_target_missing/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Resolving dependencies below will not clear these/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText("Dependency blockers")).toBeInTheDocument();
    expect(screen.getByText("Structure blockers: 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import workflow/i })).toBeDisabled();
    expect(mockedApplyWorkflowImport).not.toHaveBeenCalled();
  });

  // Not every structural finding blocks: an unfinished draft is a normal thing
  // to import and complete in the editor, so it is reported without stopping.
  it("imports a draft whose structure is merely unfinished", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: true,
      can_apply: true,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 0,
        blocking_count: 0,
      },
      workflows: [{ workflow_id: "wf_draft", display_name: "Draft" }],
      dependencies: [],
      required_resolution_actions: [],
      topology_issues: [
        {
          code: "workflow.topology.path_unterminated",
          message: "No path reaches a terminal step.",
          path: "steps.draft_step",
          severity: "error",
          workflow_id: "wf_draft",
          blocking: false,
        },
      ],
    });

    renderPage();

    fireEvent.change(screen.getByLabelText(/bundle/i), {
      target: { value: "kind: platform.workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(
      await screen.findByText(/workflow\.topology\.path_unterminated/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/imports as a draft and can be finished in the editor/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /import workflow/i }),
    ).toBeEnabled();
  });

  // The page asked which project to import into, used the answer to look up
  // dependency options, then dropped it: apply never carried it, so the
  // workflow was stored associated with nothing.
  it("sends the chosen project with the import", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      schema_version: "platform.workflow_bundle.v1",
      kind: "workflow_import_preflight",
      ok: true,
      can_apply: true,
      summary: {
        workflow_count: 1,
        reference_count: 0,
        dependency_count: 0,
        blocking_count: 0,
      },
      workflows: [{ workflow_id: "wf_incoming", display_name: "Incoming" }],
      dependencies: [],
      required_resolution_actions: [],
    });
    mockedApplyWorkflowImport.mockResolvedValueOnce({
      status: "imported",
      workflow_id: "wf_incoming",
      imported_workflows: [
        {
          workflow_id: "wf_incoming",
          source_workflow_id: "wf_incoming",
          enabled: false,
          import_state: "draft",
        },
      ],
      preflight: {} as never,
      project_id: "project-123",
    });

    renderPage("/registry/workflows/import?project_id=project-123");

    fireEvent.change(screen.getByLabelText(/bundle/i), {
      target: { value: "kind: platform.workflow_bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    fireEvent.click(await screen.findByRole("button", { name: /import workflow/i }));

    await waitFor(() => {
      expect(mockedApplyWorkflowImport).toHaveBeenCalledWith(
        expect.objectContaining({ projectId: "project-123" }),
      );
    });
  });

  it("does not ask for a project when navigation carried one", async () => {
    renderPage("/registry/workflows/import?project_id=project-123");

    await waitFor(() => {
      expect(screen.queryByLabelText("Project")).not.toBeInTheDocument();
    });
    expect(mockedFetchProjectsList).not.toHaveBeenCalled();
  });

  it("lets a taken agent from the file be reused or created under a new id", async () => {
    const conflict = {
      dependency_key: "agent:writer",
      dependency_type: "agent",
      ref: "writer",
      status: "conflict" as const,
      blocking: true,
      resolution_actions: ["use_existing", "rename", "abort"],
      metadata: { definition: true, action: "conflict", target_ref: "writer" },
    };
    const created = {
      dependency_key: "skill:draft",
      dependency_type: "skill",
      ref: "draft",
      status: "create_from_bundle" as const,
      blocking: false,
      resolution_actions: ["rename", "abort"],
      metadata: { definition: true, action: "create", target_ref: "draft" },
    };
    const blocked = {
      ...readyPreflight(),
      ok: false,
      can_apply: false,
      summary: { workflow_count: 1, reference_count: 0, dependency_count: 2, blocking_count: 1 },
      dependencies: [conflict, created],
    };
    mockedPreflightWorkflowImport.mockResolvedValueOnce(blocked);
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight());
    renderPage("/registry/workflows/import?project_id=project-123");
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    const action = await screen.findByLabelText("Resolution for writer");
    expect(action).toHaveValue("use_existing");
    expect(screen.getByText("created from file")).toBeInTheDocument();
    expect(screen.getByText(/Uses the one already here, unchanged/)).toBeInTheDocument();
    fireEvent.change(action, { target: { value: "rename" } });
    fireEvent.change(screen.getByLabelText("New id for writer"), { target: { value: "writer_2" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    await waitFor(() => expect(mockedPreflightWorkflowImport).toHaveBeenCalledTimes(2));
    expect(mockedPreflightWorkflowImport).toHaveBeenLastCalledWith(
      expect.objectContaining({
        projectId: "project-123",
        resolutions: [
          expect.objectContaining({ dependency_key: "agent:writer", action: "rename", target_ref: "writer_2" }),
        ],
      }),
    );
  });

  it("asks for the project when the file has project guardrails", async () => {
    mockedFetchProjectsList.mockResolvedValue({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 1 });
    mockedPreflightWorkflowImport.mockResolvedValueOnce({
      ...readyPreflight(),
      ok: false,
      can_apply: false,
      dependencies: [
        {
          dependency_key: "guardrail_project:project",
          dependency_type: "guardrail_project",
          ref: "project",
          status: "blocked",
          blocking: true,
          resolution_actions: ["abort"],
          message: "This file has guardrails for a project. Choose the project to attach them to.",
        },
      ],
    });
    renderPage();
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));

    expect(
      (await screen.findAllByText("This file has guardrails for a project. Choose the project to attach them to.")).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /import workflow/i })).toBeDisabled();
  });

  it("reports what the file created and which agents stayed disabled", async () => {
    mockedPreflightWorkflowImport.mockResolvedValueOnce(readyPreflight("wf_incoming"));
    mockedApplyWorkflowImport.mockResolvedValueOnce({
      status: "imported",
      workflow_id: "wf_incoming",
      imported_workflows: [
        { workflow_id: "wf_incoming", source_workflow_id: "wf_incoming", enabled: false, import_state: "draft" },
      ],
      preflight: {} as never,
      definitions: {
        created: [
          { kind: "agent", source_id: "writer", id: "writer" },
          { kind: "skill", source_id: "draft", id: "draft" },
        ],
        reused: [],
        agents_left_disabled: [{ agent_id: "writer", reason: "The project has no configured model." }],
      },
    });
    renderPage("/registry/workflows/import?project_id=project-123");
    fireEvent.change(screen.getByLabelText("Workflow bundle YAML"), { target: { value: "bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /run preflight/i }));
    fireEvent.click(await screen.findByRole("button", { name: /import workflow/i }));

    expect(
      await screen.findByText("Created from the file: 2 agents, skills, data contracts and guardrails."),
    ).toBeInTheDocument();
    expect(screen.getByText("Some agents were left disabled")).toBeInTheDocument();
    expect(screen.getByText("writer: The project has no configured model.")).toBeInTheDocument();
  });
});
