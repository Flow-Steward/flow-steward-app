import { describe, expect, it } from "vitest";

import { normalizeWorkflowStep } from "./detail-workflow-validation";

describe("workflow step kind normalization", () => {
  it.each(["agent", undefined])(
    "keeps the agent kind when step_kind is %s and a child wrapper is present",
    (stepKind) => {
      const raw = {
        ...(stepKind ? { step_kind: stepKind } : {}),
        agent_id: "planner",
        override: { invoke_workflow: { workflow_id: "child" } },
      };
      const step = normalizeWorkflowStep(raw, 0);
      expect(step.stepKind).toBe("agent");
      expect(step.agentId).toBe("planner");
      expect(step.raw.override).toEqual(raw.override);
    },
  );

  it("retains the explicit child kind and its canonical configuration", () => {
    const raw = {
      step_kind: "invoke_workflow",
      override: {
        invoke_workflow: {
          workflow_id: "child",
          input_mapping: {},
          output_mapping: {},
          artifact_mapping: {},
        },
      },
    };
    const step = normalizeWorkflowStep(raw, 0);
    expect(step.stepKind).toBe("invoke_workflow");
    expect(step.raw.override).toEqual(raw.override);
  });
});
