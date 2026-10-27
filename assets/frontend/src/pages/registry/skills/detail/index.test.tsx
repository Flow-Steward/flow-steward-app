import { createElement } from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { fetchJson } from "@/lib/http/client";
import { fetchRegistryDetail, fetchRegistryList } from "@/pages/registry/shared/api";
import { RegistrySkillsDetailPage } from "@/pages/registry/skills/detail";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

vi.mock("@/pages/registry/shared/api", () => ({
  fetchRegistryDetail: vi.fn(),
  fetchRegistryList: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchRegistryDetail = vi.mocked(fetchRegistryDetail);
const mockedFetchRegistryList = vi.mocked(fetchRegistryList);
const mockedFetchJson = vi.mocked(fetchJson);

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const view = createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        MemoryRouter,
        { initialEntries: ["/registry/skills/review_summary"] },
        createElement(
          Routes,
          null,
          createElement(Route, {
            element: createElement(RegistrySkillsDetailPage),
            path: "/registry/skills/:skillId",
          }),
        ),
      ),
    );
  const messages: Record<string, string> = locale === "lt" ? { "trash.columns.category": "Kategorija" } : {};
  return render(createElement(I18nContext.Provider, { value: { ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) } }, view));
}

describe("RegistrySkillsDetailPage", () => {
  beforeEach(() => {
    mockedFetchRegistryDetail.mockReset();
    mockedFetchRegistryList.mockReset();
    mockedFetchJson.mockReset();
  });

  it("localizes the skill category placeholder without changing the saved category value", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({ skill_id: "review_summary", display_name: "Owner skill", enabled: true, category: "owner-category", description: "Owner description", expected_inputs: [], expected_outputs: [], instructions: [] });
    mockedFetchRegistryList.mockResolvedValue([]);
    mockedFetchJson.mockResolvedValue([]);
    renderPage("lt");
    expect(await screen.findByLabelText("Kategorija")).toHaveAttribute("placeholder", "Kategorija");
    await waitFor(() => {
      expect(screen.getByLabelText("Kategorija")).toHaveValue("owner-category");
      expect(screen.getByDisplayValue("Owner description")).toBeInTheDocument();
    });
  });

  it("restores editable I/O contract controls and saves skill inputs", async () => {
    mockedFetchRegistryDetail.mockImplementation(async () => ({
      category: "quality",
      description: "Validate implementation",
      display_name: "Review Summary",
      enabled: true,
      expected_inputs: [{ name: "story_id", required: true, type: "text" }],
      expected_outputs: [{ name: "review_verdict", required: true, type: "text" }],
      instructions: [],
      skill_id: "review_summary",
    }));

    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/fields") {
        return [
          {
            data_contract_id: "",
            description: "Primary story id",
            display_name: "Story ID",
            field_key: "story_id",
            value_type: "text",
          },
        ];
      }
      if (endpoint === "/api/data-contracts") {
        return [];
      }
      throw new Error(`Unexpected fetchRegistryList call: ${endpoint}`);
    });

    mockedFetchJson.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);

      if (url.endsWith("/history")) {
        return [];
      }
      if (url.endsWith("/agents")) {
        return ["checker"];
      }
      if (url.endsWith("/inputs") && (!init?.method || init.method === "GET")) {
        return [{ name: "story_id", required: true, type: "text" }];
      }
      if (url.endsWith("/outputs") && (!init?.method || init.method === "GET")) {
        return [{ name: "review_verdict", required: true, type: "text" }];
      }
      if (url.endsWith("/inputs") && init?.method === "PUT") {
        return JSON.parse(firstStringFromUnknownValue([init.body, "[]"]));
      }

      throw new Error(`Unexpected fetchJson call: ${url}`);
    });

    renderPage();

    expect(await screen.findByText("Review Summary")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "I/O" }));

    expect(await screen.findByRole("button", { name: "+ Add Input" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Inputs" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Outputs" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "+ Add Input" }));

    const nameInputs = screen.getAllByRole("textbox", { name: "Name" });
    fireEvent.change(nameInputs[1], {
      target: { value: "acceptance_notes" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Inputs" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith("/api/skills/review_summary/inputs", {
        body: JSON.stringify([
          { contract_id: "", description: "", name: "story_id", required: true, type: "text" },
          {
            contract_id: "",
            description: "",
            name: "acceptance_notes",
            required: false,
            type: "text",
          },
        ]),
        method: "PUT",
      });
    });

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("autofills output contract fields from the catalog and keeps new outputs required by default", async () => {
    mockedFetchRegistryDetail.mockImplementation(async () => ({
      category: "quality",
      description: "Validate implementation",
      display_name: "Review Summary",
      enabled: true,
      expected_inputs: [],
      expected_outputs: [],
      instructions: [],
      skill_id: "review_summary",
    }));

    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/fields") {
        return [
          {
            data_contract_id: "dc-approval",
            description: "Structured review approval payload",
            display_name: "Approval Packet",
            field_key: "approval_packet",
            value_type: "json",
          },
        ];
      }
      if (endpoint === "/api/data-contracts") {
        return [{ contract_id: "dc-approval", display_name: "Approval Contract" }];
      }
      throw new Error(`Unexpected fetchRegistryList call: ${endpoint}`);
    });

    mockedFetchJson.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);

      if (url.endsWith("/history")) {
        return [];
      }
      if (url.endsWith("/agents")) {
        return ["checker"];
      }
      if (url.endsWith("/inputs") && (!init?.method || init.method === "GET")) {
        return [];
      }
      if (url.endsWith("/outputs") && (!init?.method || init.method === "GET")) {
        return [];
      }
      if (url.endsWith("/outputs") && init?.method === "PUT") {
        return JSON.parse(firstStringFromUnknownValue([init.body, "[]"]));
      }

      throw new Error(`Unexpected fetchJson call: ${url}`);
    });

    renderPage();

    expect(await screen.findByText("Review Summary")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "I/O" }));

    fireEvent.click(await screen.findByRole("button", { name: "+ Add Output" }));

    const catalogPickers = screen.getAllByLabelText("Catalog Field");
    fireEvent.click(catalogPickers[0]);
    fireEvent.change(screen.getByLabelText("Catalog Field Search"), {
      target: { value: "approval" },
    });
    fireEvent.click(screen.getByRole("option", { name: /Approval Packet/ }));

    const nameInputs = screen.getAllByRole("textbox", { name: "Name" });
    expect(nameInputs[0]).toHaveValue("approval_packet");

    const descriptionInputs = screen.getAllByRole("textbox", { name: "Description" });
    expect(descriptionInputs[0]).toHaveValue("Structured review approval payload");

    const dataContractPickers = screen.getAllByLabelText("Data Contract");
    expect(dataContractPickers[0]).toHaveTextContent("Approval Contract");

    const typeSelects = screen.getAllByLabelText("Type");
    expect(typeSelects[0]).toHaveTextContent("json");

    const requiredCheckboxes = screen.getAllByLabelText("Req");
    expect(requiredCheckboxes[0]).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Save Outputs" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith("/api/skills/review_summary/outputs", {
        body: JSON.stringify([
          {
            contract_id: "dc-approval",
            description: "Structured review approval payload",
            name: "approval_packet",
            required: true,
            type: "json",
          },
        ]),
        method: "PUT",
      });
    });
  });

  it("disables data contract selection for non-structured field types and clears stale contract ids", async () => {
    mockedFetchRegistryDetail.mockImplementation(async () => ({
      category: "quality",
      description: "Validate implementation",
      display_name: "Review Summary",
      enabled: true,
      expected_inputs: [],
      expected_outputs: [],
      instructions: [],
      skill_id: "review_summary",
    }));

    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/fields") {
        return [
          {
            data_contract_id: "dc-approval",
            description: "Structured review approval payload",
            display_name: "Approval Packet",
            field_key: "approval_packet",
            value_type: "json",
          },
        ];
      }
      if (endpoint === "/api/data-contracts") {
        return [{ contract_id: "dc-approval", display_name: "Approval Contract" }];
      }
      throw new Error(`Unexpected fetchRegistryList call: ${endpoint}`);
    });

    mockedFetchJson.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);

      if (url.endsWith("/history")) {
        return [];
      }
      if (url.endsWith("/agents")) {
        return ["checker"];
      }
      if (url.endsWith("/inputs") && (!init?.method || init.method === "GET")) {
        return [];
      }
      if (url.endsWith("/outputs") && (!init?.method || init.method === "GET")) {
        return [];
      }
      if (url.endsWith("/outputs") && init?.method === "PUT") {
        return JSON.parse(firstStringFromUnknownValue([init.body, "[]"]));
      }

      throw new Error(`Unexpected fetchJson call: ${url}`);
    });

    renderPage();

    expect(await screen.findByText("Review Summary")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "I/O" }));
    fireEvent.click(await screen.findByRole("button", { name: "+ Add Output" }));

    const catalogPicker = screen.getAllByLabelText("Catalog Field")[0];
    fireEvent.click(catalogPicker);
    fireEvent.click(screen.getByRole("option", { name: /Approval Packet/ }));

    const typeSelect = screen.getAllByLabelText("Type")[0];
    const contractPicker = screen.getAllByLabelText("Data Contract")[0];
    expect(contractPicker).toHaveTextContent("Approval Contract");
    expect(contractPicker).toBeEnabled();

    fireEvent.click(typeSelect);
    fireEvent.click(screen.getByRole("option", { name: "text" }));

    expect(contractPicker).toBeDisabled();
    expect(contractPicker).toHaveTextContent("Only for JSON/list");

    fireEvent.click(screen.getByRole("button", { name: "Save Outputs" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith("/api/skills/review_summary/outputs", {
        body: JSON.stringify([
          {
            contract_id: "",
            description: "Structured review approval payload",
            name: "approval_packet",
            required: true,
            type: "text",
          },
        ]),
        method: "PUT",
      });
    });
  });

  it("keeps I/O editor controls width-constrained to prevent overlap", async () => {
    mockedFetchRegistryDetail.mockImplementation(async () => ({
      category: "quality",
      description: "Validate implementation",
      display_name: "Review Summary",
      enabled: true,
      expected_inputs: [{ name: "story_id", required: true, type: "text" }],
      expected_outputs: [{ name: "review_verdict", required: true, type: "text" }],
      instructions: [],
      skill_id: "review_summary",
    }));

    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/fields") {
        return [];
      }
      if (endpoint === "/api/data-contracts") {
        return [];
      }
      throw new Error(`Unexpected fetchRegistryList call: ${endpoint}`);
    });

    mockedFetchJson.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);

      if (url.endsWith("/history")) {
        return [];
      }
      if (url.endsWith("/agents")) {
        return ["checker"];
      }
      if (url.endsWith("/inputs") && (!init?.method || init.method === "GET")) {
        return [{ name: "story_id", required: true, type: "text" }];
      }
      if (url.endsWith("/outputs") && (!init?.method || init.method === "GET")) {
        return [{ name: "review_verdict", required: true, type: "text" }];
      }

      throw new Error(`Unexpected fetchJson call: ${url}`);
    });

    renderPage();

    expect(await screen.findByText("Review Summary")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "I/O" }));

    const catalogField = (await screen.findAllByLabelText("Catalog Field"))[0];
    const nameField = screen.getByDisplayValue("story_id");
    const typeField = screen.getAllByLabelText("Type")[0];
    const contractField = screen.getAllByLabelText("Data Contract")[0];
    const descriptionField = screen.getAllByRole("textbox", { name: "Description" })[0];
    // Width now comes from the design system, not from a `w-full` utility:
    // `.fs-input, .fs-input-shell, .fs-select-trigger, .fs-combo-field,
    // .fs-multi-field` share one `width: 100%; min-width: 0` rule. Asserting the
    // role is asserting the mechanism; `w-full` would pin a class that no longer
    // carries the behaviour — and it cannot reach the element `DsSelect` labels,
    // because its `className` prop lands on the wrapper.
    expect(catalogField).toHaveClass("fs-combo-field");
    expect(nameField).toHaveClass("fs-input");
    expect(typeField).toHaveClass("fs-select-trigger");
    expect(contractField).toHaveClass("fs-combo-field");
    expect(descriptionField).toHaveClass("fs-input");
    // Requiredness is still reachable by name, which is what the control owes.
    expect(screen.getAllByRole("checkbox", { name: "Req" })[0]).toBeInTheDocument();
    expect(screen.getAllByTestId("skill-contract-catalog-field")[0]).toHaveClass(
      "md:col-span-3",
    );
    expect(screen.getAllByTestId("skill-contract-description-field")[0]).toHaveClass(
      "md:col-span-9",
    );
  });
});
