import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";

import { DsDrawer } from "@/components/shared/design-system-overlays";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";

/**
 * AUDIT-041 sub-item (c) / AUDIT-028 — destructive actions used
 * `globalThis.confirm`. A native dialog blocks the page, cannot be driven by
 * browser automation, and dismissing it resets the automation session, which is
 * why neither the registry nor the Build-with-AI scenario could be completed.
 *
 * This is the application modal that replaces it: a real `role="dialog"` with an
 * accessible name and ordinary buttons, so automation and assistive technology
 * can both operate it.
 */

function Harness({ onSettled }: Readonly<{ onSettled: (value: boolean) => void }>) {
  const { confirmDialog, requestConfirmation } = useConfirmDialog();
  const [pending, setPending] = useState<Promise<boolean> | null>(null);

  return (
    <>
      <button
        onClick={() => {
          const promise = requestConfirmation({
            body: "This clears the current conversation.",
            confirmLabel: "Start fresh",
            details: ["Pending questions", "Unapplied draft state"],
            title: "Start a new authoring session?",
          });
          setPending(promise);
          void promise.then(onSettled);
        }}
        type="button"
      >
        Ask
      </button>
      <span data-testid="pending">{pending ? "asked" : "idle"}</span>
      <ConfirmDialog {...confirmDialog} />
    </>
  );
}

function ask() {
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
}

describe("useConfirmDialog / ConfirmDialog (AUDIT-041, AUDIT-028)", () => {
  it("renders nothing until a confirmation is requested", () => {
    render(<Harness onSettled={() => {}} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens a real dialog that automation can find by name", () => {
    render(<Harness onSettled={() => {}} />);

    ask();

    const dialog = screen.getByRole("dialog", { name: "Start a new authoring session?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("shows the body and the details", () => {
    render(<Harness onSettled={() => {}} />);

    ask();

    expect(screen.getByText("This clears the current conversation.")).toBeInTheDocument();
    expect(screen.getByText("Pending questions")).toBeInTheDocument();
    expect(screen.getByText("Unapplied draft state")).toBeInTheDocument();
  });

  it("resolves true when confirmed and closes", async () => {
    const settled: boolean[] = [];
    render(<Harness onSettled={(value) => settled.push(value)} />);

    ask();
    fireEvent.click(screen.getByRole("button", { name: "Start fresh" }));

    await waitFor(() => expect(settled).toEqual([true]));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("resolves false when cancelled and closes", async () => {
    const settled: boolean[] = [];
    render(<Harness onSettled={(value) => settled.push(value)} />);

    ask();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(settled).toEqual([false]));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("returns focus to the requesting control after cancellation", async () => {
    const user = userEvent.setup();
    render(<Harness onSettled={() => {}} />);

    const trigger = screen.getByRole("button", { name: "Ask" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("never leaves an earlier request unresolved", async () => {
    const settled: boolean[] = [];
    render(<Harness onSettled={(value) => settled.push(value)} />);

    ask();
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Start fresh" }));

    // A stray double click must not leave a promise hanging forever.
    await waitFor(() => expect(settled).toEqual([false, true]));
  });

  it("uses a custom cancel label when given one", () => {
    function CustomHarness() {
      const { confirmDialog, requestConfirmation } = useConfirmDialog();
      return (
        <>
          <button
            onClick={() => {
              void requestConfirmation({ cancelLabel: "Keep it", title: "Delete?" });
            }}
            type="button"
          >
            Ask
          </button>
          <ConfirmDialog {...confirmDialog} />
        </>
      );
    }
    render(<CustomHarness />);

    ask();

    expect(screen.getByRole("button", { name: "Keep it" })).toBeInTheDocument();
  });
});

function BuilderConfirmationHarness({ onSettled, explicitReturn = false, disableWhilePending = false }: Readonly<{
  onSettled: (confirmed: boolean) => void;
  explicitReturn?: boolean;
  disableWhilePending?: boolean;
}>) {
  const { confirmDialog, requestConfirmation } = useConfirmDialog();
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [pending, setPending] = useState(false);
  const returnRef = useRef<HTMLButtonElement>(null);
  return <section data-testid="positioned-builder" style={{ position: "fixed", zIndex: 200 }}>
    <button ref={returnRef} type="button">Canvas return target</button>
    <DsDrawer focusContext="hook" onClose={() => setInspectorOpen(false)} open={inspectorOpen} title="Step editor">
      <button disabled={disableWhilePending && pending} onClick={() => {
        setPending(true);
        void requestConfirmation({
          title: "Clear the hook?", body: "Remove the steps from this hook.",
          cancelLabel: "Keep steps", confirmLabel: "Clear the hook", destructive: true,
          details: ["Fix the customer name"],
          ...(explicitReturn ? { returnFocus: returnRef.current } : {}),
        }).then(onSettled).finally(() => setPending(false));
      }} type="button">Clear the hook</button>
    </DsDrawer>
    <ConfirmDialog {...confirmDialog} />
  </section>;
}

describe("confirmation above a contextual inspector", () => {
  it("escapes the builder, traps focus, and cancels once without closing the inspector", async () => {
    const user = userEvent.setup();
    const settled: boolean[] = [];
    render(<BuilderConfirmationHarness onSettled={(value) => settled.push(value)} />);
    const inspector = screen.getByRole("dialog", { name: "Step editor" });
    const clear = within(inspector).getByRole("button", { name: "Clear the hook" });
    await user.click(clear);
    const confirmation = screen.getByRole("dialog", { name: "Clear the hook?" });
    expect(screen.getByTestId("positioned-builder")).not.toContainElement(confirmation);
    // Portal ancestry is the stacking contract; jsdom cannot prove hit testing.
    // eslint-disable-next-line testing-library/no-node-access
    expect(confirmation.closest(".fs-dialog-layer")?.parentElement).toBe(document.body);
    expect(confirmation).toHaveAttribute("aria-modal", "true");
    expect(confirmation).toHaveAccessibleDescription("Remove the steps from this hook.");
    const cancel = within(confirmation).getByRole("button", { name: "Keep steps" });
    const confirm = within(confirmation).getByRole("button", { name: "Clear the hook" });
    expect(confirm).toHaveClass("danger");
    await waitFor(() => expect(cancel).toHaveFocus());
    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab();
    const close = within(confirmation).getByRole("button", { name: "Close Clear the hook?" });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(settled).toEqual([false]));
    expect(screen.queryByRole("dialog", { name: "Clear the hook?" })).not.toBeInTheDocument();
    expect(inspector).toBeInTheDocument();
    await waitFor(() => expect(clear).toHaveFocus());
  });

  it.each(["cancel", "confirm"])("honors an explicit return target after %s", async (action) => {
    const user = userEvent.setup();
    const settled: boolean[] = [];
    render(<BuilderConfirmationHarness explicitReturn onSettled={(value) => settled.push(value)} />);
    await user.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Clear the hook" }));
    const confirmation = screen.getByRole("dialog", { name: "Clear the hook?" });
    await user.click(within(confirmation).getByRole("button", { name: action === "cancel" ? "Keep steps" : "Clear the hook" }));
    await waitFor(() => expect(settled).toEqual([action === "confirm"]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Canvas return target" })).toHaveFocus());
    expect(screen.getByRole("dialog", { name: "Step editor" })).toBeInTheDocument();
  });
});

it.each(["Cancel", "Escape"])("restores a temporarily disabled requesting control after %s", async (action) => {
  const user = userEvent.setup();
  const settled: boolean[] = [];
  render(<BuilderConfirmationHarness disableWhilePending onSettled={(value) => settled.push(value)} />);
  const inspector = screen.getByRole("dialog", { name: "Step editor" });
  const clear = within(inspector).getByRole("button", { name: "Clear the hook" });
  await user.click(clear);
  expect(clear).toBeDisabled();
  if (action === "Cancel") await user.click(screen.getByRole("button", { name: "Keep steps" }));
  else await user.keyboard("{Escape}");
  await waitFor(() => expect(clear).toBeEnabled());
  await waitFor(() => expect(clear).toHaveFocus());
  expect(settled).toEqual([false]);
  expect(inspector).toBeInTheDocument();
});

it("cancels deferred return focus when a new confirmation takes ownership", async () => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
  try {
    const user = userEvent.setup();
    render(<Harness onSettled={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Ask" }));
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveFocus();
    await act(() => vi.advanceTimersByTime(32));
    expect(cancel).toHaveFocus();
  } finally {
    vi.useRealTimers();
  }
});

it("retires deferred return focus when the confirmation owner unmounts", async () => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
  try {
    const user = userEvent.setup();
    function Owner() {
      const { confirmDialog, requestConfirmation } = useConfirmDialog();
      return <>
        <button onClick={() => { void requestConfirmation({ title: "Leave?", returnFocus: screen.getByRole("button", { name: "External return" }) }); }} type="button">Ask</button>
        <ConfirmDialog {...confirmDialog} />
      </>;
    }
    const view = render(<><button type="button">External return</button><button type="button">New focus owner</button><Owner /></>);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    view.rerender(<><button type="button">External return</button><button type="button">New focus owner</button></>);
    const next = screen.getByRole("button", { name: "New focus owner" });
    next.focus();
    await act(() => vi.advanceTimersByTime(32));
    expect(next).toHaveFocus();
  } finally {
    vi.useRealTimers();
  }
});
