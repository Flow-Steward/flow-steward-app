import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { cancelJob, restartJob, submitApprovalDecision } from "./api";
import { useJobDetailActions } from "./use-job-detail-actions";

vi.mock("./api", () => ({ cancelJob: vi.fn(), restartJob: vi.fn(), submitApprovalDecision: vi.fn() }));

function ActionsHarness() {
  const [, setMessage] = useState("");
  const [, setError] = useState("");
  const actions = useJobDetailActions("owner-job", setMessage, setError);
  return <><button type="button" onClick={actions.requestCancelJob}>Cancel</button><button type="button" onClick={actions.requestRestartJob}>Restart</button></>;
}

it.each([
  ["Cancel", "Atšaukti šią užduotį? Dabartinis vykdymas bus sustabdytas."],
  ["Restart", "Paleisti šią užduotį iš naujo su tais pačiais parametrais?"],
])("localizes the %s confirmation and does not submit when declined", async (action, prompt) => {
  vi.clearAllMocks();
  const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
  const messages: Record<string, string> = {
    "react.jobs.confirm_cancel_current_run": "Atšaukti šią užduotį? Dabartinis vykdymas bus sustabdytas.",
    "react.jobs.confirm_restart_same_parameters": "Paleisti šią užduotį iš naujo su tais pačiais parametrais?",
  };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><QueryClientProvider client={new QueryClient()}><MemoryRouter><ActionsHarness /></MemoryRouter></QueryClientProvider></I18nContext.Provider>);
  await userEvent.setup().click(screen.getByRole("button", { name: action }));
  expect(confirm).toHaveBeenCalledWith(prompt);
  expect(cancelJob).not.toHaveBeenCalled();
  expect(restartJob).not.toHaveBeenCalled();
  expect(submitApprovalDecision).not.toHaveBeenCalled();
  confirm.mockRestore();
});
