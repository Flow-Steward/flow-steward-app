import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { CreateWorkflowFields } from "@/pages/workflows/list/create-workflow-fields";
import { useCreateWorkflowForm } from "@/pages/workflows/list/create-workflow-state";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

function Form() {
  const form = useCreateWorkflowForm("");
  return (
    <>
      <CreateWorkflowFields form={form} />
      <button onClick={() => void form.submit()} type="button">Create</button>
    </>
  );
}

describe("create workflow name conflict", () => {
  it("lists the visible match and lets the user pick the suggested name without retrying", async () => {
    mockedFetchJson.mockRejectedValueOnce(
      new HttpError({
        status: 409,
        url: "/api/workflows",
        payload: {
          detail: {
            code: "registry_name_conflict",
            message: "Workflow name 'Order Sync' is already used. Choose another name.",
            matches: [
              {
                id: "order_sync",
                display_name: "Order Sync",
                enabled: true,
                used_by: [{ entity_type: "workflow", id: "nightly", display_name: "Nightly import" }],
              },
            ],
            suggested_display_name: "Order Sync 2",
          },
        },
      }),
    );
    render(<Form />);

    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "order sync" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Workflow was not created");
    expect(alert).toHaveTextContent("Workflow name 'Order Sync' is already used.");
    expect(alert).toHaveTextContent("Order Sync order_sync");
    expect(alert).toHaveTextContent("Used by: Nightly import");

    fireEvent.click(screen.getByRole("button", { name: "Use the name “Order Sync 2”" }));

    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Order Sync 2");
    expect(screen.getByRole("textbox", { name: "Workflow ID" })).toHaveValue("order_sync_2");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenCalledTimes(1);
  });
});
