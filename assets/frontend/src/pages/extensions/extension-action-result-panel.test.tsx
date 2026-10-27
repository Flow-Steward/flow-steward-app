import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { extensionActionErrorGuidance, translatedExtensionActionErrorGuidance } from "@/pages/extensions/extension-action-error-guidance";
import { ExtensionActionResultPanel } from "@/pages/extensions/extension-action-result-panel";

const recoveryEffect = {
  effect_id: "60efa34c-2af9-4ea2-9c4d-e3b937c21fc7",
  observed_version: 1,
  operation_id: "create_record",
  status: "timeout_unknown",
  decision_allowed: true,
  retry_supported: false,
  retry_requires_original_request: true as const,
  resolution_ref: null,
  retry_unavailable_reason: null,
};

function recoveryProps(overrides = {}) {
  return {
    effect: recoveryEffect,
    canManage: true,
    originalRequestAvailable: false,
    busy: false,
    error: "",
    onDecision: vi.fn(async () => undefined),
    onRetry: vi.fn(async () => undefined),
    onLater: vi.fn(),
    ...overrides,
  };
}
const recoveryResult = { label: "Create record", ok: false, payload: {} };

it.each([
  ["approve", "Operation completed — continue"],
  ["reject", "Operation not completed — retry"],
] as const)("requires a trimmed comment before the existing effect %s decision", async (action, label) => {
  const user = userEvent.setup();
  const recovery = recoveryProps();
  render(<ExtensionActionResultPanel {...{ result: recoveryResult, recovery }} />);
  const comment = screen.getByRole("textbox", { name: "Comment" });
  expect(comment).toBeRequired();
  expect(screen.getByText("*")).toHaveClass("fs-required");
  await user.type(comment, "   ");
  await user.click(screen.getByRole("button", { name: label }));
  expect(recovery.onDecision).not.toHaveBeenCalled();
  expect(recovery.onRetry).not.toHaveBeenCalled();
  expect(screen.getByText("Add a comment before recording this decision.")).toBeVisible();
  await user.clear(comment);
  await user.type(comment, "  Checked provider records  ");
  await user.click(screen.getByRole("button", { name: label }));
  expect(recovery.onDecision).toHaveBeenCalledExactlyOnceWith(action, "Checked provider records");
});

it("keeps a restored effect visible and explicitly disables retry without its exact original request", () => {
  const recovery = recoveryProps({
    effect: { ...recoveryEffect, observed_version: 2, decision_allowed: false, retry_supported: true,
      resolution_ref: { command_id: "resolution-1", effect_id: recoveryEffect.effect_id, fenced_version: 2, approval_id: "" } },
  });
  render(<ExtensionActionResultPanel {...{ result: recoveryResult, recovery }} />);
  expect(screen.getByText("The original request for this effect is unavailable. Retry is unavailable.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Retry original action" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Operation completed — continue" })).not.toBeInTheDocument();
});

it.each([
  ["timeout_unknown", null, "unknown"],
  ["timeout_unknown", { command_id: "decision-1", effect_id: recoveryEffect.effect_id, fenced_version: 2, approval_id: "" }, "pending"],
  ["claimed", null, "Running"],
  ["running", null, "Running"],
] as const)("labels a recovery %s outcome truthfully instead of failed", (status, resolution_ref, label) => {
  render(<ExtensionActionResultPanel result={recoveryResult} recovery={recoveryProps({ effect: { ...recoveryEffect, status, resolution_ref } })} />);
  expect(screen.getByText(label, { exact: true })).toBeVisible();
  expect(screen.queryByText("failed", { exact: true })).not.toBeInTheDocument();
});

it.each([[true, "succeeded"], [false, "failed"]] as const)("keeps the ordinary action badge %s as %s", (ok, label) => {
  render(<ExtensionActionResultPanel result={{ ...recoveryResult, ok }} />);
  expect(screen.getByText(label, { exact: true })).toBeVisible();
  expect(screen.queryByText("unknown", { exact: true })).not.toBeInTheDocument();
});

it("keeps the effect identifier inside the existing technical details", async () => {
  const user = userEvent.setup();
  render(<ExtensionActionResultPanel result={{ ...recoveryResult, payload: { external_effect: recoveryEffect } }} recovery={recoveryProps()} />);
  expect(screen.queryByText(recoveryEffect.effect_id, { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText(new RegExp(recoveryEffect.effect_id))).not.toBeVisible();
  await user.click(screen.getByText("Technical details"));
  expect(screen.getByText(new RegExp(recoveryEffect.effect_id))).toBeVisible();
});

it("keeps viewer recovery read-only even if contradictory flags offer decisions and retry", () => {
  const recovery = recoveryProps({ canManage: false, originalRequestAvailable: true,
    effect: { ...recoveryEffect, retry_supported: true } });
  render(<ExtensionActionResultPanel {...{ result: recoveryResult, recovery }} />);
  expect(screen.getByText("You can view this effect. An operator must record the decision.")).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "Comment" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Operation completed — continue" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Operation not completed — retry" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry original action" })).not.toBeInTheDocument();
});

it("decides later without submitting either a decision or an action", async () => {
  const user = userEvent.setup();
  const recovery = recoveryProps();
  render(<ExtensionActionResultPanel {...{ result: recoveryResult, recovery }} />);
  await user.click(screen.getByRole("button", { name: "Decide later" }));
  expect(recovery.onLater).toHaveBeenCalledOnce();
  expect(recovery.onDecision).not.toHaveBeenCalled();
  expect(recovery.onRetry).not.toHaveBeenCalled();
});

it("never offers another retry while the authorized effect is already running", () => {
  const recovery = recoveryProps({ originalRequestAvailable: true,
    effect: { ...recoveryEffect, status: "running", decision_allowed: false, retry_supported: false,
      retry_unavailable_reason: "retry_in_progress" } });
  render(<ExtensionActionResultPanel {...{ result: recoveryResult, recovery }} />);
  expect(screen.getByText("The authorized retry is already in progress.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Retry original action" })).toBeDisabled();
});

it.each(["__proto__", "constructor", "toString"])(
  "ignores inherited provider error code %s and preserves the empty fallback",
  (errorCode) => {
    const translate = vi.fn((key: string, fallback?: string) => fallback ?? key);

    expect(translatedExtensionActionErrorGuidance(translate, errorCode)).toBe("");
    expect(translate).not.toHaveBeenCalled();
    expect(extensionActionErrorGuidance(errorCode)).toBe("");

    expect(translatedExtensionActionErrorGuidance(
      translate, errorCode, " PROVIDER_SECRET_VALUE_REQUIRED ",
    )).toBe("Enter a secret value before saving.");
    expect(translate).toHaveBeenCalledExactlyOnceWith(
      "react.extensions.secret_value_required", "Enter a secret value before saving.",
    );
  },
);

it("shows declared actionable guidance as the primary failed action alert", () => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Test connection",
        ok: false,
        payload: {
          action: {
            response: {
              error_code: "provider_unavailable",
            },
          },
        },
      }}
      guidance="Open this extension's Setup page, resolve the provider requirement, then retry."
    />,
  );

  expect(screen.getByRole("alert")).toHaveTextContent(
    "Open this extension's Setup page, resolve the provider requirement, then retry.",
  );
});

it("does not infer provider guidance for an unrelated extension result", () => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Test another extension",
        ok: false,
        payload: {
          error_code: "authorization_required",
          message: "Another extension needs authorization",
        },
      }}
    />,
  );

  // The extension's own message is shown, but no provider-specific copy is inferred.
  expect(screen.getByRole("alert")).toHaveTextContent("Another extension needs authorization");
  expect(screen.queryByText(/Workspace/i)).not.toBeInTheDocument();
});

it.each([
  ["project_context_required", "Select a project before running this action."],
  ["provider_secret_storage_unavailable", "Secret storage is unavailable."],
  ["provider_secret_storage_failed", "The secret was not saved."],
  ["project_connection_storage_unavailable", "The connection could not be saved."],
] as const)("turns stable operation code %s into actionable guidance", (errorCode, guidance) => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Save connection",
        ok: false,
        payload: {
          action: {
            status: "failed",
            response: { ok: false, error: errorCode },
          },
        },
      }}
    />,
  );

  expect(screen.getByRole("alert")).toHaveTextContent(guidance);
  expect(screen.getByRole("alert")).not.toHaveTextContent(errorCode);
});

/* eslint-disable testing-library/no-node-access -- asserting disclosure placement. */
it("shows extension-owned Acme Notifier copy and keeps its machine code in technical details", () => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Test Acme Notifier delivery",
        ok: false,
        payload: {
          action: {
            status: "failed",
            response: {
              ok: false,
              error_code: "project_context_required",
              error: "Select a project before running this Acme Notifier action.",
              result: {
                error: "project_context_required",
                error_code: "project_context_required",
              },
            },
          },
        },
      }}
    />,
  );

  expect(screen.getByRole("alert")).toHaveTextContent(
    "Select a project before running this Acme Notifier action.",
  );
  expect(screen.getByRole("alert")).not.toHaveTextContent("project_context_required");
  expect(screen.getByText("Technical details").closest("details")?.textContent)
    .toContain("project_context_required");
});

/*
 * Whether a value is in the summary or only inside the disclosure is a fact about
 * where it sits in the tree, which role and text queries cannot express.
 */
it("summarises a successful result and keeps the runtime payload behind a disclosure", () => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Test connection",
        ok: true,
        payload: {
          action: {
            status: "succeeded",
            runtime_command: ["python", "-m", "extension"],
            bundle_path: "/srv/extensions/acme_storefront",
            account_id: "acct_9f2",
            response: {
              ok: true,
              result: {
                site_name: "Example Store",
                site_state: "ready",
                store_available: true,
                request_id: "req_8812",
              },
            },
          },
        },
      }}
    />,
  );

  // What the user asked for is on screen without being unfolded...
  expect(screen.getByText("Site name")).toBeInTheDocument();
  expect(screen.getByText("Example Store")).toBeInTheDocument();
  expect(screen.getByText("Store available")).toBeInTheDocument();
  expect(screen.getByText("Yes")).toBeInTheDocument();
  // ...while the diagnostics are not part of the summary.
  expect(screen.queryByText("Request id")).not.toBeInTheDocument();
  const summary = screen.getByText("Site name").closest("dl");
  expect(summary?.textContent).not.toContain("/srv/extensions");
  expect(summary?.textContent).not.toContain("acct_9f2");

  // They remain reachable for whoever needs them.
  const disclosure = screen.getByText("Technical details");
  expect(disclosure).toBeInTheDocument();
  expect(disclosure.closest("details")?.textContent).toContain("/srv/extensions");
});

it("redacts secrets from the technical details", () => {
  render(
    <ExtensionActionResultPanel
      result={{
        label: "Test connection",
        ok: true,
        payload: { action: { status: "succeeded", response: { ok: true, result: { api_token: "shhh" } } } },
      }}
    />,
  );

  expect(screen.queryByText(/shhh/)).not.toBeInTheDocument();
  expect(screen.getByText("Technical details").closest("details")?.textContent)
    .toContain("[REDACTED]");
});
/* eslint-enable testing-library/no-node-access */
