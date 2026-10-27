import { fireEvent, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { buildCanvasLayout, type StartNodeData, type StepNodeData } from "./layout";
import { WorkflowStartNode, TerminalStepNode } from "./canvas-step-nodes";
import type { WorkflowCanvasGraph } from "../shared/api";
import { hookFrame, withHookFrames } from "./hook-frames";

vi.mock("./canvas-schedule-summary", () => ({ CanvasScheduleSummary: () => <p>0 9 * * * · Europe/Berlin</p> }));
const graph: WorkflowCanvasGraph = {
 workflow:{id:"safe", display_name:"Safe", enabled:true,callable_by_parent:false},
 interface_contract:{trigger_channels:["ui","api","schedule"],input_mode:"fields",expected_inputs:[{name:"api_token",required:true,default:"DO_NOT_RENDER_SECRET"},{name:"priority"}]},
 output_contract:{expected_outputs:[{name:"receipt",default:"PRIVATE_RESULT"}],returned_files:[{name:"orders.csv",type:"file",description:"PRIVATE_DESCRIPTION",source:{step_id:"export",step_name:"PRIVATE_CHANGE",result:"file",target:"state.orders"}}]},
 entry:{kind:"step",id:"success"},groups:[],steps:[{id:"success",step_id:"success",kind:"stop_success",display_name:"Receipt ready",config_summary:"Done",ports:[]},{id:"fail",step_id:"fail",kind:"stop_fail",display_name:"Rejected",config_summary:"Failed",ports:[]}],edges:[],validation_issues:[],automation_rules:[],
};
function layout() { return buildCanvasLayout({canvasGraph:graph,onOpenInputs:vi.fn(),onOpenGroup:vi.fn(),onOpenGroupAutomation:vi.fn(),onOpenStep:vi.fn(),onOpenStepAutomation:vi.fn()}); }
it("projects global names only onto Start and success, preserving explicit stop identity",()=>{
 const result=layout();
 const start=result.nodes.find(n=>n.id==="visual:start")!;
 const end=result.nodes.find(n=>n.id==="success")!;
 const fail=result.nodes.find(n=>n.id==="fail")!;
 expect(start.data).toMatchObject({inputs:["api_token*","priority"],triggerChannels:["ui","api","schedule"]});
 expect(end.data).toMatchObject({contractSummary:{outputs:["receipt"],artifacts:["orders.csv"]},step:{display_name:"Receipt ready"}});
 expect(end.data).not.toHaveProperty("contractSummary.externalChanges");
 expect(fail.data).not.toHaveProperty("contractSummary");
 expect(JSON.stringify(start.data)+JSON.stringify(end.data)).not.toMatch(/DO_NOT_RENDER_SECRET|PRIVATE_RESULT|PRIVATE_DESCRIPTION|PRIVATE_CHANGE/);
 expect(end.position.y).toBe(start.position.y);
 expect(start.position.x+(start.width||0)).toBeLessThan(end.position.x);
 expect(start.handles).toEqual([expect.objectContaining({id:"entry",type:"source",position:"right"})]);
});
it("opens the existing Inputs action and renders contract names without values",()=>{
 const result=layout();
 const start=result.nodes.find(n=>n.id==="visual:start")!.data as StartNodeData;
 const end=result.nodes.find(n=>n.id==="success")!.data as StepNodeData;
 render(<ReactFlowProvider><WorkflowStartNode {...({data:start} as Parameters<typeof WorkflowStartNode>[0])}/><TerminalStepNode {...({data:end} as Parameters<typeof TerminalStepNode>[0])}/></ReactFlowProvider>);
 fireEvent.click(screen.getByRole("button",{name:"Start — workflow inputs"}));
 expect(start.onOpenInputs).toHaveBeenCalledOnce();
 expect(screen.getByText("api_token")).toBeVisible();
 expect(screen.getByLabelText("Required")).toBeVisible();
 expect(screen.getByText("Receipt ready")).toBeVisible();
 expect(screen.getByText("receipt")).toBeVisible();
 // A returned file is an output: listed under Outputs, with no Artifacts or External changes block.
 expect(screen.getByText("orders.csv")).toBeVisible();
 expect(screen.queryByText("Artifacts")).not.toBeInTheDocument();
 expect(screen.queryByText("External changes")).not.toBeInTheDocument();
 expect(document.body).not.toHaveTextContent(/DO_NOT_RENDER_SECRET|PRIVATE_RESULT|PRIVATE_DESCRIPTION|PRIVATE_CHANGE/);
});

it("projects json_schema input names and required flags, including converted children", () => {
 const schemaGraph = {...graph, interface_contract: {input_mode: "json_schema", input_schema: {properties: {order_id: {type: "string", default: "SECRET_DEFAULT"}, priority: {type: "string"}}, required: ["order_id"]}}};
 const result = buildCanvasLayout({canvasGraph:schemaGraph,onOpenGroup:vi.fn(),onOpenGroupAutomation:vi.fn(),onOpenStep:vi.fn(),onOpenStepAutomation:vi.fn(),measuredStepHeights:{"visual:start":350}});
 expect(result.nodes.find(n=>n.id==="visual:start")?.data).toMatchObject({inputs:["order_id*", "priority"]});
 expect(JSON.stringify(result.nodes.find(n=>n.id==="visual:start")?.data)).not.toContain("SECRET_DEFAULT");
 expect(result.nodes.find(n=>n.id==="success")!.position.y).toBe(result.nodes.find(n=>n.id==="visual:start")!.position.y);
 expect(result.nodes.find(n=>n.id==="visual:start")!.position.x+288).toBeLessThan(result.nodes.find(n=>n.id==="success")!.position.x);
});

it.each(["archived", "hook"] as const)("preserves literal suffixes and unknown requiredness on actual %s name-only output and artifact cards", (source) => {
 const options = { onOpenInputs: vi.fn(), onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() };
 const child: WorkflowCanvasGraph = { ...graph, workflow: { ...graph.workflow, id: "literal-child" }, output_contract: { expected_outputs: [{ name: "receipt*", default: "PRIVATE_RESULT" }], returned_files: [{ name: "report*", description: "PRIVATE_DESCRIPTION" }] } };
 const frame = hookFrame({ hookId: "before", stepId: "before", displayName: "Before", description: "", connected: true, targetWorkflowId: "literal-child", targetName: "Literal child", bindingRevision: 1 }, child);
 const author: WorkflowCanvasGraph = { ...graph, entry: { kind: "step", id: "before" }, steps: [{ id: "before", kind: "invoke_workflow", display_name: "Before", config_summary: "", ports: [] }] };
 const result = source === "archived"
  ? buildCanvasLayout({ ...options, canvasGraph: graph, readOnly: true, archivedContract: { input_mode: "fields", output_mode: "fields", inputs: [], outputs: ["receipt*"], artifacts: ["report*"], trigger_channels: [] } })
  : buildCanvasLayout({ ...options, canvasGraph: withHookFrames(author, new Map([["before", frame]])) });
 const node = result.nodes.find((item) => item.id === (source === "archived" ? "success" : "hook:before:success"))!;
 const data = node.data as StepNodeData;
 expect(data.contractSummary?.outputs).toEqual(["receipt*"]);
 expect(data.contractSummary?.artifacts).toEqual(["report*"]);
 expect(data.contractSummary).not.toHaveProperty("outputSignatures");
 expect(data.contractSummary).not.toHaveProperty("artifactSignatures");
 render(<ReactFlowProvider><TerminalStepNode id={node.id} data={data} type="terminal" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} /></ReactFlowProvider>);
 expect(screen.getByText("receipt*", { exact: true })).toBeVisible();
 expect(screen.getByText("report*", { exact: true })).toBeVisible();
 expect(screen.queryByLabelText("Required")).not.toBeInTheDocument();
 expect(screen.queryByText("* Required", { exact: true })).not.toBeInTheDocument();
 expect(document.body).not.toHaveTextContent(/PRIVATE_RESULT|PRIVATE_DESCRIPTION/);
});
