import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { useWorkflowDetailDomBridge } from "./detail-dom-bridge";
import { WorkflowStepKindSmartSetup } from "./detail-step-smart-setup";

function AgentSetup({ onChange }: Readonly<{ onChange: (value: string) => void }>) {
  const [stepAgentLocked, setStepAgentLocked] = useState(false);
  const [stepAgentId, setStepAgentId] = useState("writer");
  useWorkflowDetailDomBridge({
    workflowId: "agent_lock_audit",
    workflowContractQuery: { data: {} },
    setStepAgentLocked,
  });
  return <WorkflowStepKindSmartSetup ctx={{
    stepKindDraft: "agent",
    stepAgentId,
    stepAgentLocked,
    agentCatalogQuery: { isLoading: false },
    stepAgentOptions: [{ value: "writer", label: "Writer" }, { value: "reviewer", label: "Reviewer" }],
    setStepAgentId: (next: string) => { setStepAgentId(next); onChange(next); },
  }} />;
}

it("locks the open Agent menu through the real bridge without changing the chosen agent", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(<AgentSetup onChange={onChange} />);
  const trigger = screen.getByRole("combobox", { name: "Agent" });
  trigger.focus();
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "Writer" })).toHaveFocus());
  act(() => window._setStepExecutorControlState?.({ mappedAgents: ["writer"], resolvedAgent: "writer" }));
  expect(trigger).toBeDisabled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.keyboard("{ArrowDown}{Enter}");
  expect(onChange).not.toHaveBeenCalled();
  expect(trigger).toHaveTextContent("Writer");
  act(() => window._setStepExecutorControlState?.({ mappedAgents: [], resolvedAgent: "" }));
  expect(trigger).toBeEnabled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.click(trigger);
  await user.click(screen.getByRole("option", { name: "Reviewer" }));
  expect(onChange).toHaveBeenCalledExactlyOnceWith("reviewer");
  expect(trigger).toHaveTextContent("Reviewer");
});
