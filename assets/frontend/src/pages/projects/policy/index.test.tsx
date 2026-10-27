import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { ProjectPolicyEditor } from "@/pages/projects/policy";
import { fetchProjectPolicy, saveProjectPolicy } from "@/pages/projects/policy/api";

vi.mock("@/pages/projects/policy/api", async () => {
  const actual = await vi.importActual("@/pages/projects/policy/api");
  return {
    ...actual,
    fetchProjectPolicy: vi.fn(),
    saveProjectPolicy: vi.fn(),
  };
});

const mockedFetchProjectPolicy = vi.mocked(fetchProjectPolicy);
const mockedSaveProjectPolicy = vi.mocked(saveProjectPolicy);

function renderPolicyEditor(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? {
    "react.projects.autonomy_mode_manual": "Rankinis",
    "react.projects.autonomy_mode_supervised": "Prižiūrimas",
    "react.projects.autonomy_mode_auto": "Automatinis",
  } : {};
  const view = render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <ProjectPolicyEditor projectId="project-123" />
    </QueryClientProvider>
    </I18nContext.Provider>,
  );
  return { ...view, queryClient };
}

describe("ProjectPolicyEditor", () => {
  beforeEach(() => {
    mockedFetchProjectPolicy.mockReset();
    mockedSaveProjectPolicy.mockReset();
    mockedSaveProjectPolicy.mockResolvedValue({ status: "ok" } as never);
  });

  it("offers only the autonomy modes the backend accepts", async () => {
    const user = userEvent.setup();
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: {},
    } as never);

    renderPolicyEditor();

    await user.click(await screen.findByRole("combobox", { name: "Autonomy mode" }));

    // requests.py declares Literal["manual", "supervised", "auto"]; anything
    // else is rejected on save.
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "manual",
      "supervised",
      "auto",
    ]);
  });

  it.each([
    ["manual", "Rankinis"],
    ["supervised", "Prižiūrimas"],
    ["auto", "Automatinis"],
  ])("localizes autonomy option %s while saving its canonical enum", async (mode, label) => {
    const user = userEvent.setup();
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: { autonomy_mode: "supervised", concurrency_cap: 3 },
    } as never);

    renderPolicyEditor("lt");

    await waitFor(() => expect(screen.getByLabelText("Concurrency cap")).toHaveValue(3));
    await user.click(screen.getByRole("combobox", { name: "Autonomy mode" }));
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Rankinis", "Prižiūrimas", "Automatinis",
    ]);
    await user.click(screen.getByRole("option", { name: label }));
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(mockedSaveProjectPolicy).toHaveBeenCalledWith(
      "project-123",
      expect.objectContaining({ policy: expect.objectContaining({ autonomy_mode: mode, concurrency_cap: 3 }) }),
    ));
  });

  it("keeps in-progress edits when the policy refetches", async () => {
    const user = userEvent.setup();
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: { concurrency_cap: 1 },
    } as never);

    const { queryClient } = renderPolicyEditor();

    const cap = await screen.findByLabelText("Concurrency cap");
    await user.clear(cap);
    await user.type(cap, "5");

    // The next poll carries a different server value; the typed one must win
    // until it is saved, or the 20s interval would erase it mid-edit.
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: { concurrency_cap: 9 },
    } as never);
    await queryClient.invalidateQueries({ queryKey: ["projects", "project-123", "policy"] });

    await waitFor(() => expect(mockedFetchProjectPolicy).toHaveBeenCalledTimes(2));
    expect(cap).toHaveValue(5);
  });

  it("labels retry count as retries after the first attempt", async () => {
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: {},
    } as never);

    renderPolicyEditor();

    const input = await screen.findByLabelText("Maximum retries after the first attempt");
    expect(input).toHaveValue(2);
    expect(screen.getByText("A value of 2 allows up to three total invocations.")).toBeInTheDocument();
    expect(screen.queryByText("Retry max attempts")).not.toBeInTheDocument();
  });

  it("blocks negative retry values and initial backoff above the maximum", async () => {
    const user = userEvent.setup();
    mockedFetchProjectPolicy.mockResolvedValue({
      policy: {},
    } as never);

    renderPolicyEditor();

    const retries = await screen.findByLabelText("Maximum retries after the first attempt");
    await user.clear(retries);
    await user.type(retries, "-1");
    expect(screen.getByText("Retry values must be nonnegative.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();

    await user.clear(retries);
    await user.type(retries, "2");
    const initial = screen.getByLabelText("Retry backoff initial seconds");
    const maximum = screen.getByLabelText("Retry backoff max seconds");
    await user.clear(initial);
    await user.type(initial, "10");
    await user.clear(maximum);
    await user.type(maximum, "5");

    expect(
      screen.getByText("Initial retry backoff cannot exceed the maximum retry backoff."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  });
});
