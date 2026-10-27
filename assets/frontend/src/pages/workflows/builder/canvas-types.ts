import type { MissingExtensionNoticeModel } from "@/pages/workflows/shared/missing-extension";
import type { ToolCategory } from "@/pages/workflows/detail/detail-tool-categories";
import type { ContainerRuntimeMarks } from "./runtime-container-marks";
import type { CanvasRunBarAction, CanvasRunBarProps } from "./canvas-run-bar";
import type { ReactFlowInstance } from "@xyflow/react";
import type { ReactNode } from "react";
import type { HookExecutionMarks } from "@/pages/workflows/detail/detail-hook-execution-marks";
import type {
  WorkflowCanvasGraph,
  WorkflowCanvasIssue,
  WorkflowDetail,
  WorkflowGraphEdge,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import type { HookFrame } from "@/pages/workflows/builder/hook-frames";
import type { CanvasPosition } from "@/pages/workflows/builder/canvas-core";
import type { GraphIntentToken } from "@/pages/workflows/builder/canvas-history";
import type { ClipboardAuthority, ClipboardCommand } from "@/pages/workflows/detail/detail-clipboard-commands";
import type { useWorkflowDeletionGuard } from "@/pages/workflows/detail/detail-canvas-handlers";

export type CanvasCreationIntent = {
  source: WorkflowNodeRef;
  outcome: WorkflowOutcome;
  scope: { groupId: string; forEachId: string; laneId: string };
  point?: { x: number; y: number };
  clientPoint?: { x: number; y: number };
  /** Canvas selection when the author opened this exact palette request. */
  selectionGeneration?: number;
};

/** A real catalog choice. Canvas coordinates never become step configuration. */
export type LocalStepChoice = {
  nodeKind?: "step";
  catalogKey: string;
  stepKind: string;
  /** Editor-only category; never a workflow field or runtime step kind. */
  toolCategory?: ToolCategory;
  displayName: string;
  family?: "Actions" | "Data" | "Flow" | "Finish" | "Groups";
  sourceLabel?: string;
  extensionId?: string;
  iconUrl?: string;
  blockKey?: string;
  agentId?: string;
  override?: Record<string, unknown>;
};

export type LocalContainerChoice = {
  nodeKind: "group" | "for_each";
  catalogKey: string;
  displayName: string;
  family?: "Groups" | "Flow";
  sourceLabel?: string;
  extensionId?: string;
  iconUrl?: string;
  stepKind?: never;
  override?: never;
  containerDefaults?: NewContainerDefaults;
  /** Canvas-only presentation: a For each drawn as one card that lists its body. */
  presentation?: "inline";
};
export type LocalCanvasChoice = LocalStepChoice | LocalContainerChoice;
/** Canvas › Note in Add step: a note is not structure, so it never reaches the creation command. */
export type PaletteNoteChoice = { nodeKind: "note"; catalogKey: "canvas:note"; displayName: string; family: "Canvas"; stepKind?: never; override?: never };
export type PaletteChoice = LocalCanvasChoice | PaletteNoteChoice;
/** Where a step asked for at a point (a right-click, a drop) lands: flow and client coordinates. */
export type CanvasPlacementPoint = { point: { x: number; y: number }; clientPoint: { x: number; y: number } };

export type CreateLocalStepRequest = {
  requestId: string;
  choice: LocalCanvasChoice;
  context: { groupId?: string; forEachId?: string; laneId?: string; insert?: { source: WorkflowNodeRef; outcome: WorkflowOutcome; label: string; append?: boolean } | null };
  intent?: CanvasCreationIntent;
  at?: CanvasPlacementPoint;
  expectedEdge?: WorkflowGraphEdge;
  createAsEntry?: boolean;
  signal?: AbortSignal;
};
export type CreateLocalStepResult =
  | { status: "created"; nodeKind?: "step"; stepId: string; canvasStepId: string; canvasNodeId?: string }
  | { status: "created"; nodeKind: "group" | "for_each"; nodeId: string; canvasNodeId: string }
  | { status: "refused" | "cancelled" | "failed"; message?: string };

/** A completed creation, bound to the graph journal item and its visual owner. */
export type CanvasConfirmedCreation = {
  nodeId: string;
  nonce: number;
  target: WorkflowNodeRef;
  token: GraphIntentToken;
  ownerWorkflowId: string;
  viewKey: string;
  viewEpoch: number;
  point?: CanvasPosition;
  intent?: CanvasCreationIntent;
  selectionGeneration?: number;
};

/** Select a successfully created node once its preview is present. */
export type CanvasCreationSelection = { nodeId: string; nonce: number; selectionGeneration?: number };
export type GraphDeleteSource = "editor" | "step-more" | "canvas-key";
export type GraphDeleteSelection = { kind: WorkflowNodeRef["kind"]; canvasId: string };
export type CanvasCommandSelection = { ids: readonly string[]; generation: number };
export type RequestGraphDelete = (
  selection: readonly GraphDeleteSelection[],
  source: GraphDeleteSource,
  currentCanvasSelection: () => CanvasCommandSelection,
) => Promise<boolean>;
export type CanvasDeleteNotice = {
  state: "checking" | "protected" | "unknown";
  message: string;
  canOpenAutomation: boolean;
};

export type CanvasClipboardSnapshot = {
  ids: readonly string[];
  worldPositions: ReadonlyMap<string, CanvasPosition>;
  duplicateOrigin: CanvasPosition;
  generation: number;
  /** Paste only: the copy's top left lands here (the pointer), or one grid step further on each time. */
  pasteOrigin?: CanvasPosition;
  pasteMode?: "pointer" | "offset";
};
/** Where a paste lands: at the pointer over the canvas, else offset from the middle of the view. */
export type CanvasPasteTarget = { origin: CanvasPosition; mode: "pointer" | "offset" };
export type CanvasClipboardVisualSelection = { ids: readonly string[]; nonce: number };

/** `automation` arrives only from a link that names one rule (the project notification overview). */
export type StepEditorTab = "smart-setup" | "test-run" | "automation";

export type BuilderSurfaceProps = {
  advancedMode: boolean;
  availabilityNotice?: string;
  /** The structured reason behind the notice when a required extension is missing. */
  missingExtensions?: MissingExtensionNoticeModel | null;
  /** Context shown in the header after the workflow name (the hooks' project, R2-10). */
  headerContext?: ReactNode;
  backHref?: string;
  onToggleAdvancedMode: () => void;
  onCanvasReady?: (instance: ReactFlowInstance | null) => void;
  onCreateStep?: (request: CreateLocalStepRequest) => Promise<CreateLocalStepResult>;
  projectId?: string;
  /** Presentation capability only; the creation command and server recheck authority. */
  allowHookDeclaration?: boolean;
  /** Stable identity of the displayed Hook view, beyond workflow/project identity. */
  commandViewIdentity?: string;
  /** Latest owner reader; the canvas supplies current selection and world geometry. */
  onReadClipboardAuthority?: (snapshot: CanvasClipboardSnapshot, command: ClipboardCommand) => ClipboardAuthority | null;
  clipboardGuard?: Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDelete"> & Partial<Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDeleteBatch">>;
  /** The canvas graph of the definition on screen: the server's plus the unapplied change set. */
  canvasGraph: WorkflowCanvasGraph;
  changeSet: WorkflowChangeSetController;
  /** Resolve one XY gesture to exactly one real author or active Hook-child owner. */
  resolvePlacementOwner?: (nodeIds: readonly string[]) => WorkflowChangeSetController | null;
  /** Real owners whose placement Undo/Redo tokens may affect this displayed canvas. */
  placementHistoryOwners?: readonly WorkflowChangeSetController[];
  creationSelection?: CanvasCreationSelection | null;
  creationIntentRetireNonce?: number;
  /** Centre the canvas on this node once per nonce (a step just added or inserted). */
  focusNodeId?: string | null;
  focusNodeNonce?: number;
  focusNodeCreation?: CanvasConfirmedCreation | null;
  /** Place the focused node at the viewport centre first: a root step added without a connection. */
  focusNodePlace?: boolean;
  runtimeActiveNodeId?: string | null;
  runtimeMarks?: HookExecutionMarks;
  runtimeEdges?: ReadonlySet<string> | ReadonlyMap<string, number>;
  containerRuntimeMarks?: ContainerRuntimeMarks;
  exportYamlHref?: string;
  exportYamlLabel?: string;
  onCloseInspector: () => void;
  onConnectPort: (
    source: WorkflowNodeRef,
    outcome: WorkflowOutcome,
    target: WorkflowTargetRef,
    rework?: boolean,
  ) => Promise<boolean> | boolean;
  onDisconnectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => void;
  onInsertStep: (edge: WorkflowGraphEdge, intent?: CanvasCreationIntent) => void;
  /** Open the new-step editor for a step connected after this free outcome. */
  onAppendStep?: (source: WorkflowNodeRef, outcome: WorkflowOutcome, intent?: CanvasCreationIntent) => void;
  onOpenGroup: (groupId: string, defaultTab?: string) => void;
  onOpenGroupAutomation: (groupId: string) => void;
  onOpenNewGroup: (defaults?: NewContainerDefaults) => void;
  onOpenNewStep: (context?: { groupId?: string; forEachId?: string; laneId?: string }, at?: CanvasPlacementPoint) => void;
  onOpenStep: (stepId: string, requestedTab?: StepEditorTab) => boolean | void | Promise<boolean | void>;
  /** Published owner permission and role gate for this live projected step. Navigation only. */
  stepTestRunAllowed?: (stepId: string) => boolean;
  onRequestGraphDelete?: RequestGraphDelete;
  onDeleteSelectionInvalidated?: () => void;
  deleteGuardNotice?: CanvasDeleteNotice | null;
  onRetryGraphDelete?: () => void;
  onOpenDeleteAutomation?: () => void;
  onOpenStepAutomation: (stepId: string) => void;
  onOpenWorkflowAutomation: () => void;
  onOpenWorkflowSettings: () => void;
  onOpenWorkflowInterface?: (tab: "inputs" | "outputs") => void;
  onOpenWorkflowHistory?: () => void;
  /** The local copy and the server differ: compare them and choose (edit session). */
  onCompareVersions?: () => void;
  /** "Move to…": open the step's editor at Placement, the choice focused. */
  onOpenStepPlacement?: (stepId: string) => void;
  /** Group › Convert to child workflow…: open the conversion for a saved Group. */
  onConvertGroup?: (groupId: string, name: string) => void;
  /** Why Convert to child workflow cannot run right now. */
  convertGroupBlockedReason?: string;
  inspectorOpen?: boolean;
  /** Occupied right edge from the existing contextual drawer width owner. */
  inspectorWidth?: number;
  canvasRunBar?: CanvasRunBarProps;
  canvasTestRunAction?: CanvasRunBarAction;
  onValidationIssue: (issue: WorkflowCanvasIssue) => void;
  workflowId: string;
  scheduleProjectHint?: string;
  workflow?: WorkflowDetail;
  onAnnotationsSaved?: (revision: number) => Promise<unknown>;
  onAnnotationsReloaded?: (revision: number) => Promise<unknown>;
  /** A protected author graph: the real structure, read-only; hooks stay connectable. */
  structureLocked?: boolean;
  /** Hook steps by author step id, drawn as frames holding their connected workflow's steps (B14). */
  hookFrames?: ReadonlyMap<string, HookFrame>;
  /** A palette request the canvas carries out: place the next created node, or add a note. */
  canvasPlacementCommand?: CanvasPlacementCommand | null;
};

export type CanvasPlacementCommand =
  | { kind: "place-next"; point: { x: number; y: number }; nonce: number }
  | { kind: "add-note"; point?: { x: number; y: number }; nonce: number };

/** What a new container starts as: a Group or a For each, optionally inside a sequence Group. */
export type NewContainerDefaults = {
  containerKind?: "group" | "for_each";
  bodyKind?: "sequence" | "lanes";
  parentGroupId?: string;
  /** Canvas-only owner when creating a container inside a hook. */
  hookId?: string;
};
