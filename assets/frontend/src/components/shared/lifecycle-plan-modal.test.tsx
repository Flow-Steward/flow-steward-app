import { useCallback, useEffect, useRef, useState } from "react";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  LifecyclePlanModal,
  type LifecyclePlan,
} from "@/components/shared/lifecycle-plan-modal";

function plan(actionType: "restore" | "hard_delete"): LifecyclePlan {
  return {
    action_type: actionType,
    plan_version: `${actionType}-revision`,
    root_entity: {
      entity_type: "mcp_connector",
      entity_id: "mcp_deleted",
      label: "Deleted connector",
    },
    tree: {
      node_id: "mcp_connector:mcp_deleted",
      entity_type: "mcp_connector",
      entity_id: "mcp_deleted",
      label: "Deleted connector",
      selectable: true,
      selected: true,
      mandatory: true,
      status: "trashed",
      children: [],
    },
  };
}

function Harness({ actionType }: Readonly<{ actionType: "restore" | "hard_delete" }>) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        Open {actionType}
      </button>
      <LifecyclePlanModal
        onClose={() => setOpen(false)}
        onConfirm={() => undefined}
        open={open}
        plan={plan(actionType)}
      />
    </>
  );
}

function ShadowRootHarness() {
  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const openDialog = useCallback(() => setOpen(true), []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const shadowRoot = host.attachShadow({ mode: "open" });
    const opener = document.createElement("button");
    opener.textContent = "Restore connector";
    opener.type = "button";
    opener.addEventListener("click", openDialog);
    shadowRoot.append(opener);
    return () => opener.removeEventListener("click", openDialog);
  }, [openDialog]);

  return (
    <>
      <div data-testid="shadow-grid-host" ref={hostRef} />
      <LifecyclePlanModal
        onClose={() => setOpen(false)}
        onConfirm={() => undefined}
        open={open}
        plan={plan("restore")}
      />
    </>
  );
}

it.each([
  ["restore", "Restore plan"],
  ["hard_delete", "Hard delete plan"],
] as const)(
  "gives the %s plan an accessible focus and Escape lifecycle",
  async (actionType, dialogName) => {
    const user = userEvent.setup();
    render(<Harness actionType={actionType} />);
    const opener = screen.getByRole("button", { name: `Open ${actionType}` });

    await user.click(opener);

    const dialog = await screen.findByRole("dialog", { name: dialogName });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: `Close ${dialogName}` })).toHaveFocus(),
    );

    await user.keyboard("{Escape}");

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  },
);

/* eslint-disable testing-library/no-node-access -- This regression must inspect focus inside a real shadow root. */
it("returns focus to the invoking button inside a platform-grid shadow root", async () => {
  const user = userEvent.setup();
  render(<ShadowRootHarness />);
  const host = screen.getByTestId("shadow-grid-host");
  const opener = host.shadowRoot?.querySelector<HTMLButtonElement>("button");
  expect(opener).toBeTruthy();

  opener?.focus();
  await user.click(opener as HTMLButtonElement);
  await screen.findByRole("dialog", { name: "Restore plan" });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Close Restore plan" })).toHaveFocus(),
  );

  await user.keyboard("{Escape}");

  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(host.shadowRoot?.activeElement).toBe(opener));
});
/* eslint-enable testing-library/no-node-access */
