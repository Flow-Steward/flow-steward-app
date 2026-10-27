import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { createElement } from "react";
import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { stepVisual } from "./canvas-core";

describe("workflow family appearance", () => {
  it("keeps every card neutral while sharing icon colors within families", () => {
    const families = [
      ["agent", "connector", "invoke_workflow", "http_request"],
      ["dataset_query", "set_variable", "transform", "tabular_feed", "file_from_base64", "template"],
      ["if", "switch", "for_each", "batch", "approval", "wait"],
    ];
    for (const kinds of families) {
      expect(new Set(kinds.map((kind) => stepVisual(kind).iconClass)).size).toBe(1);
    }
    const kinds = [...families.flat(), "stop_success", "stop_fail"];
    expect(new Set(kinds.map((kind) => stepVisual(kind).cardClass)).size).toBe(1);
    expect(new Set(kinds.map((kind) => stepVisual(kind).kindClass)).size).toBe(1);
    expect(stepVisual("stop_success").iconClass).not.toBe(stepVisual("stop_fail").iconClass);
    expect(stepVisual("agent").iconClass).not.toBe(stepVisual("transform").iconClass);
  });
  it.each(["owner_custom_kind", "__proto__", "constructor", "toString", "SET_VARIABLE", " set_variable "])(
    "renders a safe shared fallback icon for unknown kind %s", (kind) => {
      expect(() => render(createElement(WorkflowStepIcon, { kind }))).not.toThrow();
      expect(stepVisual(kind).Icon).toBe(stepVisual("owner_custom_kind").Icon);
      expect(stepVisual(kind).cardClass).toBe(stepVisual("agent").cardClass);
    },
  );

});
