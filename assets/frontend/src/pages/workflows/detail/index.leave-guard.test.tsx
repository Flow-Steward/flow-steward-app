import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderPage } from "./index.test-helpers";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

describe("builder route leave guard", () => {
  it.each(["back", "push"])("keeps unapplied structure when %s navigation is cancelled", async (navigation) => {
    const { router } = renderPage({ surface: "builder" });
    fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("listbox", { name: "Add step" });
    fireEvent.click(within(palette).getByRole("option", { name: "Stop · success" }));

    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Local step" } });
    expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success");
    fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied"));
    await act(async () => { if (navigation === "back") await router.navigate(-1);
      else await router.navigate("/previous"); });
    const dialog = await screen.findByRole("dialog", { name: "Leave the builder?" });
    expect(screen.queryByRole("heading", { name: "Previous page" })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied");
    await act(async () => { if (navigation === "back") await router.navigate(-1);
      else await router.navigate("/previous"); });
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Leave the builder?" })).getByRole("button", { name: "Leave and discard" }));
    expect(await screen.findByRole("heading", { name: "Previous page" })).toBeInTheDocument();
  });
});
