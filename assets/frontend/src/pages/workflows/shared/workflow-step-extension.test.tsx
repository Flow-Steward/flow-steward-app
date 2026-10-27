import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { GenericStepNode } from "@/pages/workflows/builder/canvas-step-nodes";
import { canvasGraphWithDefinition } from "@/pages/workflows/builder/change-set";
import type { StepNodeData } from "@/pages/workflows/builder/layout";
import { stepExtensionId } from "@/pages/workflows/shared/workflow-step-extension";
import { WorkflowStepExtensionMark } from "@/pages/workflows/shared/workflow-step-icon";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";

describe("stepExtensionId: which extension a step comes from", () => {
  it.each([
    [{ step_kind: "connector", override: { connector: { extension_id: "acme.crm" } } }, "acme.crm"],
    [{ step_kind: "connector", connector_id: "extension.acme.crm", override: {} }, "acme.crm"],
    [{ kind: "connector", connector_id: "extension.acme.crm" }, "acme.crm"],
    // An install-scoped id names an install, not an extension.
    [{ step_kind: "connector", connector_id: "extension.pi_abc.send" }, ""],
    [{ step_kind: "connector", connector_id: "extension.0f8c1e2a-1111-4222-8333-944455556666.send" }, ""],
    [{ step_kind: "connector", connector_id: "connector.registry_tool" }, ""],
    [{ step_kind: "connector", override: { connector: { extension_id: "Not An Id" } } }, ""],
    [{ step_kind: "agent", override: { connector: { extension_id: "acme.crm" } } }, ""],
    [null, ""],
  ])("%j -> %s", (step, expected) => {
    expect(stepExtensionId(step as Record<string, unknown> | null)).toBe(expected);
  });
});

describe("WorkflowStepExtensionMark", () => {
  it("shows the extension's logo, named for assistive technology", () => {
    render(<WorkflowStepExtensionMark extensionId="acme.crm" extensionName="Acme CRM" />);

    const mark = screen.getByRole("img", { name: "From extension Acme CRM" });
    expect(mark).toHaveAttribute("title", "From extension Acme CRM");
    expect(mark).toHaveAttribute("data-step-extension", "acme.crm");
    expect(within(mark).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-inline");
    expect(within(mark).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/acme.crm/icon");
  });

  it("renders nothing for a step that is not from an extension", () => {
    render(<WorkflowStepExtensionMark extensionId="" />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});

function stepNode(extensionId: string | null) {
  const step = {
    id: "send",
    step_id: "send",
    kind: "connector",
    display_name: "Send to CRM",
    config_summary: "",
    extension_id: extensionId,
  };
  const data: StepNodeData = {
    step,
    automationRules: [],
    validationIssues: [],
    isEntry: false,
    local: false,
    unreachable: false,
    onAutomation: () => undefined,
    onOpenStep: () => undefined,
    onEditStep: () => undefined,
  };
  render(
    <ReactFlowProvider>
      <GenericStepNode
        data={data}
        deletable
        draggable
        dragging={false}
        id="send"
        isConnectable={false}
        positionAbsoluteX={0}
        positionAbsoluteY={0}
        selectable
        selected={false}
        type="action"
        zIndex={1}
      />
    </ReactFlowProvider>,
  );
  return screen.getByRole("group", { name: "Open step Send to CRM" });
}

describe("a step node on the canvas", () => {
  it("carries the logo of the extension the step comes from", () => {
    const node = stepNode("acme.crm");
    const mark = within(node).getByRole("img", { name: "Extension" });
    expect(within(mark).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/acme.crm/icon");
  });

  it("carries no logo for a step that is not from an extension", () => {
    const node = stepNode(null);
    expect(within(node).queryByRole("img", { name: "Extension" })).not.toBeInTheDocument();
  });
});

describe("canvasGraphWithDefinition", () => {
  it("names each connector step's extension from the definition, saved or new", () => {
    const graph: WorkflowCanvasGraph = {
      workflow_id: "wf",
      entry: "saved",
      steps: [{ id: "saved", step_id: "saved", kind: "connector", display_name: "Saved", config_summary: "", extension_id: "acme.stale" }],
      edges: [],
    } as unknown as WorkflowCanvasGraph;
    const definition = {
      steps: [
        { step_id: "saved", step_kind: "connector", connector_id: "extension.acme.crm", override: { connector: { extension_id: "acme.crm" } } },
        { step_id: "fresh", step_kind: "connector", connector_id: "extension.acme.mail" },
        { step_id: "think", step_kind: "agent" },
      ],
      groups: [],
      forEach: [],
      graph: { entry: "saved", edges: [] },
    };

    const result = canvasGraphWithDefinition(graph, definition as never);

    expect(Object.fromEntries(result.steps.map((step) => [step.step_id, step.extension_id]))).toEqual({
      saved: "acme.crm",
      fresh: "acme.mail",
      think: null,
    });
  });
});


describe("unavailable extension icon names", () => {
  it.each([undefined, "   ", "acme.crm"])("uses translated neutral copy for %s", (extensionName) => {
    render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", t: (key, text, params) => fallbackContext.t(key, key === "extension_page.extension_fallback" ? "Plėtinys" : text, params) }}>
      <WorkflowStepExtensionMark extensionId="acme.crm" extensionName={extensionName} />
    </I18nContext.Provider>);
    const mark = screen.getByRole("img", { name: "Plėtinys" });
    expect(mark).toHaveAttribute("title", "Plėtinys");
    expect(mark).toHaveAttribute("data-step-extension", "acme.crm");
    expect(within(mark).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/acme.crm/icon");
    expect(screen.queryByRole("img", { name: /acme.crm/ })).not.toBeInTheDocument();
  });
});
