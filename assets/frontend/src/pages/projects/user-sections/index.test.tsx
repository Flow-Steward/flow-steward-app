import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { useHasCapability } from "@/lib/auth/capabilities";
import { fetchSectionJobs, fetchSectionProject } from "@/pages/projects/user-sections/api";
import { ProjectSectionPage } from "@/pages/projects/user-sections";
import type { ProjectUserSectionView } from "@/pages/projects/user-sections/types";
import { runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));
vi.mock("@/lib/auth/run-upload-limits", () => ({
  useRunUploadLimits: () => ({ max_file_bytes: 5_000_000, max_total_bytes: 5_000_000, max_files: 10 }),
  exceedsRunUploadLimits: () => false,
}));
vi.mock("@/pages/projects/user-sections/api", () => ({
  fetchSectionProject: vi.fn(),
  fetchSectionJobs: vi.fn(),
}));
vi.mock("@/pages/workflows/shared/run-api", () => ({
  runBuilderWorkflow: vi.fn(),
  readWorkflowRunFile: (inputName: string, file: File) =>
    Promise.resolve({ input_name: inputName, filename: file.name, mime_type: file.type, content_base64: "eA==" }),
}));

function section(overrides: Partial<ProjectUserSectionView> = {}): ProjectUserSectionView {
  return {
    id: "receipts",
    title: "Receipts",
    workflow_id: "wf.receipts",
    fields: [],
    columns: [{ id: "supplier", source: "output", path: "supplier", label: "Supplier" }],
    submission: { mode: "foreach", target_input: "receipt" },
    effects_acknowledged: true,
    runtime: {
      workflow_name: "Receipts",
      workflow_revision: 4,
      input_schema: {
        type: "object",
        properties: { receipt: { type: "object", "x-fs-type": "file" }, company: { type: "string", title: "Company" } },
        required: ["receipt", "company"],
      },
      output_schema: { type: "object", properties: { supplier: { type: "string" } } },
      runnable: true,
      issues: [],
    },
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p1/sections/receipts"]}>
        <Routes>
          <Route element={<ProjectSectionPage />} path="/projects/:projectId/sections/:sectionId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(fetchSectionJobs).mockResolvedValue({
    jobs: [{ job_id: "j1", status: "completed", section_values: { supplier: "ACME" }, submitted_by: { id: "op" } }],
    total: 1,
    page: 1,
    per_page: 25,
    total_pages: 1,
  });
  vi.mocked(runBuilderWorkflow).mockReset();
});

it("lets an operator send two receipts as two runs with their own keys", async () => {
  vi.mocked(useHasCapability).mockReturnValue(true);
  vi.mocked(fetchSectionProject).mockResolvedValue({ id: "p1", name: "Acme", workflow_ids: [], user_sections: [section()] });
  vi.mocked(runBuilderWorkflow).mockResolvedValueOnce({ job_id: "a" }).mockResolvedValueOnce({ job_id: "b" });
  const { container } = renderPage();

  await screen.findByText("ACME");
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
  expect(fileInput.multiple).toBe(true);
  fireEvent.change(fileInput, {
    target: { files: [new File(["a"], "a.jpg", { type: "image/jpeg" }), new File(["b"], "b.jpg", { type: "image/jpeg" })] },
  });
  const company = container.querySelector('[data-field="company"] input') as HTMLInputElement;
  fireEvent.change(company, { target: { value: "Acme" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  await waitFor(() => expect(runBuilderWorkflow).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(runBuilderWorkflow).mock.calls;
  expect(calls[0][0]).toBe("wf.receipts");
  expect(calls[0][1]).toMatchObject({ project_id: "p1", expected_revision: 4, acknowledged: true, inputs: { company: "Acme" } });
  expect(calls[0][2]).not.toBe(calls[1][2]);
  await screen.findByText("2 of 2 accepted");
});

it("shows a viewer the history without a form", async () => {
  vi.mocked(useHasCapability).mockReturnValue(false);
  vi.mocked(fetchSectionProject).mockResolvedValue({ id: "p1", name: "Acme", workflow_ids: [], user_sections: [section()] });
  renderPage();

  await screen.findByText("ACME");
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

it("keeps the history and explains why an unlinked workflow's form is paused", async () => {
  vi.mocked(useHasCapability).mockReturnValue(true);
  const blocked = section();
  blocked.runtime = {
    ...blocked.runtime,
    runnable: false,
    issues: [{ code: "workflow_unlinked", message: "The workflow is not linked to this project", path: "x" }],
  };
  vi.mocked(fetchSectionProject).mockResolvedValue({ id: "p1", name: "Acme", workflow_ids: [], user_sections: [blocked] });
  renderPage();

  await screen.findByText("The workflow is not linked to this project");
  expect(await screen.findByText("ACME")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});
