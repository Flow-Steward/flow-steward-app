import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { ExtensionUninstallPlanModal } from "@/components/shared/extension-uninstall-plan-modal";
import type { ExtensionUninstallPlan } from "@/pages/extensions/api";

const plan = (extensionId: string): ExtensionUninstallPlan => ({
  extension_id: extensionId,
  install_id: `install-${extensionId}`,
  release_id: `release-${extensionId}`,
  revision: `revision-${extensionId}`,
  remove: [],
  affected: [],
});

function KeyboardHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">Open uninstall plan</button>
      <ExtensionUninstallPlanModal
        onClose={() => setOpen(false)}
        onConfirm={() => undefined}
        plan={open ? plan("fixture.provider") : null}
      />
    </>
  );
}

test("names, traps, dismisses, and restores focus for the uninstall dialog", async () => {
  const user = userEvent.setup();
  render(<KeyboardHarness />);
  const opener = screen.getByRole("button", { name: "Open uninstall plan" });

  await user.click(opener);

  const dialog = screen.getByRole("dialog", { name: "Extension uninstall plan" });
  // DsDialog owns placement: focus lands inside the dialog, on its close button,
  // rather than on the acknowledgement at the foot of a long plan.
  const close = screen.getByRole("button", { name: "Close Extension uninstall plan" });
  await waitFor(() => expect(close).toHaveFocus());

  // Uninstall stays disabled until acknowledged, so Tab from Cancel wraps to the
  // first control and Shift+Tab comes back: the trap holds.
  screen.getByRole("button", { name: "Cancel" }).focus();
  await user.keyboard("{Tab}");
  expect(close).toHaveFocus();
  await user.keyboard("{Shift>}{Tab}{/Shift}");
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  expect(dialog).toBeInTheDocument();

  await user.keyboard("{Escape}");

  await waitFor(() => expect(dialog).not.toBeInTheDocument());
  await waitFor(() => expect(opener).toHaveFocus());
});

test("resets acknowledgement when a different uninstall plan opens", () => {
  const props = {
    onClose: () => undefined,
    onConfirm: () => undefined,
  };
  const renderModal = (currentPlan: ExtensionUninstallPlan | null) => (
    <ExtensionUninstallPlanModal
      {...props}
      key={currentPlan ? `${currentPlan.extension_id}:${currentPlan.revision}` : "closed"}
      plan={currentPlan}
    />
  );
  const { rerender } = render(renderModal(plan("first")));

  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Uninstall extension" })).toBeEnabled();

  rerender(renderModal(null));
  rerender(renderModal(plan("second")));

  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Uninstall extension" })).toBeDisabled();
});

test("shows the specific artifacts that uninstall will remove", () => {
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("fixture.provider"),
        remove: [
          {
            kind: "workflow",
            id: "publish_report",
            display_name: "Publish weekly report",
            count: 1,
          },
        ],
      }}
    />,
  );

  expect(screen.getByText("Publish weekly report")).toBeInTheDocument();
});

test("shows retained redacted audit history separately", () => {
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("fixture.provider"),
        retained: [
          {
            kind: "external_effect_audit",
            id: "pi-1",
            display_name: "Redacted external-effect audit history",
            count: 4,
          },
        ],
      }}
    />,
  );

  expect(screen.getByText("Retained audit history")).toBeInTheDocument();
  expect(screen.getByText("Redacted external-effect audit history")).toBeInTheDocument();
  expect(screen.getByText("4")).toBeInTheDocument();
});

test("shows named workflows that will stop instead of raw dependent ids", () => {
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("fixture.storage"),
        affected: [{
          kind: "workflow",
          id: "stock_drive_sync",
          display_name: "Stock storage sync",
          reason: "Uses external storage and will stop until the step is replaced.",
        }],
      }}
    />,
  );

  expect(screen.getByText("Stock storage sync")).toBeInTheDocument();
  expect(screen.getByText("Workflow")).toBeInTheDocument();
  expect(screen.queryByText("workflow: stock_drive_sync")).not.toBeInTheDocument();
  expect(
    screen.getByText("Uses external storage and will stop until the step is replaced."),
  ).toBeInTheDocument();
});

test("says what each retained row is, not just its value", () => {
  // Seen in a real browser: the retained list rendered "true 1", "0.1.2 1" and
  // the same id three times, on the screen that authorises the cascade.
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("fixture.provider"),
        retained: [
          {
            kind: "runtime_package",
            id: "fixture.provider",
            label: "Extension files (another install still uses them)",
            display_name: "Fixture Provider 1.0.0",
            count: 1,
          },
          { kind: "project_workflow_assignment", id: "assignment-1", count: 0 },
        ],
      }}
    />,
  );

  const rows = screen.getAllByRole("listitem").map((row) => row.textContent || "");
  expect(rows).toEqual([
    "Extension files (another install still uses them)Fixture Provider 1.0.0",
  ]);
  // A row with nothing in it is not shown at all.
  expect(screen.queryByText(/assignment/i)).not.toBeInTheDocument();
});

test("shows the extension by name, with its id only as secondary text", () => {
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("acme.model-provider"),
        extension_name: "Remote Ollama LLM Provider",
        extension_version: "1.0.0",
      }}
    />,
  );

  expect(screen.getByText("Remote Ollama LLM Provider 1.0.0")).toHaveClass("fs-plan-dialog-subject");
  expect(screen.getByText("acme.model-provider")).toHaveClass("fs-plan-dialog-id");
  // Plain words for the frame of the dialog too, not "irreversible cascade".
  expect(
    screen.getByText("Everything listed under “Will be removed” is deleted with the extension."),
  ).toBeInTheDocument();
  expect(screen.getByText("Nothing else depends on this extension.")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "I understand this can't be undone." })).not.toBeChecked();
});

test("lists plain labels and names, never row ids", () => {
  // The product owner's screenshot: "Release — d0593697-…", "Projection —
  // e7360f4c-…", "Enabled State Record — false". The plan rows now carry a
  // plain label and a name; ids stay out of the dialog even when a row has
  // no name.
  const installId = "e7360f4c-1111-4222-8333-444455556666";
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("acme.model-provider"),
        remove: [
          {
            kind: "runtime_package",
            id: "acme.model-provider",
            label: "Extension files",
            display_name: "Remote Ollama LLM Provider 1.0.0",
            count: 1,
          },
          {
            kind: "extension_records",
            id: "acme.model-provider",
            label: "Extension registration and history",
            display_name: "Settings, installed versions and activity log",
            count: 5,
          },
          {
            kind: "project_connection",
            id: "d0593697-aaaa-4bbb-8ccc-ddddeeeeffff",
            label: "Project connection",
            display_name: "Ollama on the LAN",
            project_name: "Alpha",
            count: 1,
          },
          { kind: "project_dataset_assignment", id: installId, count: 0 },
          { kind: "callback_registration", id: installId, count: 1 },
        ],
        affected: [
          {
            kind: "agent",
            id: "a3c1b2d4-0000-4000-8000-000000000001",
            label: "Agent",
            display_name: "Support triage",
            reason: "Uses the model llama3 (Ollama); the agent can't run until it is given another model.",
          },
        ],
      }}
    />,
  );

  const dialog = screen.getByRole("dialog");
  const rows = screen.getAllByRole("listitem").map((row) => row.textContent || "");
  expect(rows).toEqual([
    "Extension filesRemote Ollama LLM Provider 1.0.0",
    "Extension registration and historySettings, installed versions and activity log5",
    "Project connectionOllama on the LANProject: Alpha",
    // An older row without a label still reads as words, not an id.
    "Callback registration",
    "Support triageAgentUses the model llama3 (Ollama); the agent can't run until it is given another model.",
  ]);
  expect(dialog).not.toHaveTextContent(/[0-9a-f]{8}-[0-9a-f]{4}-/);
  expect(dialog).not.toHaveTextContent(/\b(true|false)\b/);
});

test("puts a refusal at the top of the plan and scrolls it into view", () => {
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  render(
    <ExtensionUninstallPlanModal
      error="Installed extension active release path is invalid"
      onClose={() => undefined}
      onConfirm={() => undefined}
      plan={{
        ...plan("fixture.provider"),
        remove: [{ kind: "workflow", id: "w", label: "Workflow", display_name: "Daily sync", count: 1 }],
      }}
    />,
  );

  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("Installed extension active release path is invalid");
  // Before the plan, not under it.
  const heading = screen.getByRole("heading", { name: "Will be removed" });
  expect(alert.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
});

test("opens the confirmation for an extension that declares an OAuth provider", async () => {
  // File Transfer (dropbox_oauth) and Google Workspace (workspace_oauth) carry
  // their sign-in app and its secrets in the plan; both must be confirmable.
  const user = userEvent.setup();
  const onConfirm = vi.fn();
  render(
    <ExtensionUninstallPlanModal
      onClose={() => undefined}
      onConfirm={onConfirm}
      plan={{
        ...plan("fixture.oauth-provider"),
        extension_name: "Example Files",
        extension_version: "0.2.0",
        remove: [
          { kind: "oauth_configuration", id: "cfg-1", label: "Sign-in app", display_name: "Dropbox", count: 1 },
          { kind: "oauth_configuration_credentials", id: "cfg-1", label: "Sign-in app secrets", count: 1 },
        ],
      }}
    />,
  );

  const dialog = screen.getByRole("dialog", { name: "Extension uninstall plan" });
  expect(dialog).toHaveTextContent("Example Files 0.2.0");
  expect(dialog).toHaveTextContent("Sign-in appDropbox");
  expect(dialog).toHaveTextContent("Sign-in app secrets");
  const confirm = screen.getByRole("button", { name: "Uninstall extension" });
  expect(confirm).toBeDisabled();

  await user.click(screen.getByText("I understand this can't be undone."));
  await user.click(confirm);

  expect(onConfirm).toHaveBeenCalledTimes(1);
});
