import { fireEvent, screen, within } from "@testing-library/react";
import { mockedUseIsAdmin, mockedUseAuth, renderPage } from "./index.test-helpers";

// Simulate a preceding viewer consumer without depending on test-file order.
// The shared per-test fixture must restore its documented admin baseline.
beforeAll(() => {
  mockedUseIsAdmin.mockReturnValue(false);
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, roles: ["viewer"], user: { roles: ["viewer"] } } } as ReturnType<typeof mockedUseAuth>);
});

afterAll(() => {
  mockedUseIsAdmin.mockReturnValue(true);
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, roles: ["admin"], user: { roles: ["admin"] } } } as ReturnType<typeof mockedUseAuth>);
});

it("restores authoring permissions at the shared fixture boundary after a viewer override", async () => {
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  const drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("combobox", { name: "Input mode" })).toBeEnabled();
  expect(within(drawer).getByRole("button", { name: "Save workflow settings" })).toBeInTheDocument();
});
