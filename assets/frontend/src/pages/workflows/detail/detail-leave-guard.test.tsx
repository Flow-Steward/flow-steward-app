import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { useWorkflowLeaveGuard } from "./detail-leave-guard";

/**
 * Task 7: the browser dialog only while something is not saved on the server,
 * an in-app "Stay" / "Leave and discard", and no write or removal on unload.
 */

const calls = { navigate: [] as string[], discard: 0 };

function Builder({ dirty }: Readonly<{ dirty: boolean }>) {
  const dialog = useWorkflowLeaveGuard(dirty, {
    onNavigate: (pathname) => { calls.navigate.push(pathname); },
    onDiscard: () => { calls.discard += 1; },
  });
  return <><h1>Builder</h1><Link to="/elsewhere">Open elsewhere</Link><ConfirmDialog {...dialog} /></>;
}

function mount(dirty: boolean) {
  const router = createMemoryRouter([
    { path: "/registry/workflows/wf/builder", element: <Builder dirty={dirty} /> },
    { path: "/elsewhere", element: <h1>Elsewhere</h1> },
  ], { initialEntries: ["/registry/workflows/wf/builder"] });
  const view = render(<RouterProvider router={router} />);
  return { router, view };
}

function beforeUnload() {
  const event = new Event("beforeunload", { cancelable: true });
  globalThis.dispatchEvent(event);
  return event.defaultPrevented;
}

beforeEach(() => {
  calls.navigate = [];
  calls.discard = 0;
});

it("asks the browser to confirm a reload only while something is not saved on the server", () => {
  const dirty = mount(true);
  expect(beforeUnload()).toBe(true);
  dirty.view.unmount();
  expect(beforeUnload()).toBe(false); // The listener goes with the page.
  // Clean after Apply, even with an Undo history: no warning.
  const clean = mount(false);
  expect(beforeUnload()).toBe(false);
  clean.view.unmount();
});

it("stays on Stay and leaves on Leave and discard, marking the session only then", async () => {
  const { router } = mount(true);
  await act(async () => { await router.navigate("/elsewhere"); });
  const dialog = await screen.findByRole("dialog", { name: "Leave the builder?" });
  fireEvent.click(screen.getByRole("button", { name: "Stay" }));
  expect(dialog).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Builder" })).toBeInTheDocument();
  expect(calls.discard).toBe(0);
  expect(calls.navigate).toEqual(["/elsewhere"]);

  await act(async () => { await router.navigate("/elsewhere"); });
  fireEvent.click(await screen.findByRole("button", { name: "Leave and discard" }));
  expect(await screen.findByRole("heading", { name: "Elsewhere" })).toBeInTheDocument();
  expect(calls.discard).toBe(1);
});

it("does not block query, hash or panel changes of the same page", async () => {
  const { router } = mount(true);
  await act(async () => { await router.navigate("/registry/workflows/wf/builder?drawer=settings#notes"); });
  expect(screen.queryByRole("dialog", { name: "Leave the builder?" })).not.toBeInTheDocument();
  expect(router.state.location.search).toBe("?drawer=settings");
  expect(calls.navigate.at(-1)).toBe("/registry/workflows/wf/builder");
});

it("lets a clean page leave without asking", async () => {
  const { router } = mount(false);
  await act(async () => { await router.navigate("/elsewhere"); });
  expect(await screen.findByRole("heading", { name: "Elsewhere" })).toBeInTheDocument();
  expect(calls.discard).toBe(0);
  expect(calls.navigate).toEqual(["/elsewhere"]);
});

it.each(["Stay", "Escape"])("returns focus to the live navigation opener when the direct leave guard chooses %s", async (action) => {
  const user = userEvent.setup();
  const { router } = mount(true);
  const opener = screen.getByRole("link", { name: "Open elsewhere" });
  await user.click(opener);
  const stay = await screen.findByRole("button", { name: "Stay" });
  await waitFor(() => expect(stay).toHaveFocus());
  if (action === "Stay") await user.click(stay);
  else await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "Leave the builder?" })).not.toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/registry/workflows/wf/builder");
  expect(calls.discard).toBe(0);
  await waitFor(() => expect(opener).toHaveFocus());
});
