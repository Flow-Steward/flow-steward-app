import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import type { WorkflowExportPolicy } from "@/pages/workflows/shared/api";
import { WorkflowSummaryHeader } from "@/pages/workflows/shared/header";
import {
  savedWorkflowChannelState,
  workflowChannelTabHidden,
  workflowPageTabs,
  workflowPageTabsForDetail,
} from "@/pages/workflows/shared/workflow-page-tabs";

vi.mock("@/pages/workflows/shared/api", () => ({ fetchWorkflowExportPolicy: vi.fn() }));
vi.mock("@/pages/workflows/shared/project-context", () => ({ findWorkflowLinkedProjectId: vi.fn() }));

const keys = (tabs: { key: string }[]) => tabs.map((tab) => tab.key);
const ALL = ["smart-setup", "webhook", "schedules", "automation", "history"];

describe("workflowPageTabs", () => {
  it("lists Hooks, then the settings tabs, with a channel's tab only while that channel is on", () => {
    expect(keys(workflowPageTabs({ canAuthor: true, channels: { enabled: true, channels: ["webhook", "schedule"] }, hasHooks: true })))
      .toEqual(["hooks", ...ALL]);
    expect(keys(workflowPageTabs({ canAuthor: true, channels: { enabled: true, channels: ["schedule"] }, hasHooks: false })))
      .toEqual(["smart-setup", "schedules", "automation", "history"]);
    expect(keys(workflowPageTabs({ canAuthor: true, channels: { enabled: true, channels: ["webhook"] }, hasHooks: false })))
      .toEqual(["smart-setup", "webhook", "automation", "history"]);
    expect(keys(workflowPageTabs({ canAuthor: true, channels: { enabled: true, channels: ["ui"] }, hasHooks: false })))
      .toEqual(["smart-setup", "automation", "history"]);
  });

  it("hides both channel tabs while the workflow interface is off", () => {
    expect(keys(workflowPageTabs({ canAuthor: true, channels: { enabled: false, channels: ["webhook", "schedule"] }, hasHooks: false })))
      .toEqual(["smart-setup", "automation", "history"]);
  });

  it("gives everyone who cannot author the Hooks tab at most", () => {
    expect(keys(workflowPageTabs({ canAuthor: false, channels: { enabled: true, channels: ["webhook", "schedule"] }, hasHooks: true }))).toEqual(["hooks"]);
    expect(workflowPageTabs({ canAuthor: false, channels: null, hasHooks: false })).toEqual([]);
  });

  it("reads the saved channels, and an unsaved draft replaces them", () => {
    const detail = { settings: { callable: { enabled: true, trigger_channels: ["schedule"] } }, steps: [] };
    expect(savedWorkflowChannelState(detail)).toEqual({ enabled: true, channels: ["schedule"] });
    expect(savedWorkflowChannelState({})).toEqual({ enabled: false, channels: [] });
    expect(keys(workflowPageTabsForDetail(detail, true, ((_key: string, text: string) => text) as never))).toEqual(["smart-setup", "schedules", "automation", "history"]);
    expect(keys(workflowPageTabsForDetail(detail, true, ((_key: string, text: string) => text) as never, { enabled: true, channels: ["webhook"] })))
      .toEqual(["smart-setup", "webhook", "automation", "history"]);
  });

  it("knows the Webhook tab by both its drawer key and its page key", () => {
    const ctx = { workflowInterfaceEnabledDraft: true, workflowInterfaceTriggerChannelsDraft: ["schedule"] };
    expect(workflowChannelTabHidden("triggers", ctx)).toBe(true);
    expect(workflowChannelTabHidden("webhook", ctx)).toBe(true);
    expect(workflowChannelTabHidden("schedules", ctx)).toBe(false);
    // Channels not loaded yet: nothing is hidden.
    expect(workflowChannelTabHidden("schedules", {})).toBe(false);
  });
});

describe("WorkflowSummaryHeader page tabs", () => {
  it("renders Overview and Projects, then the given tabs, each linking to the overview's ?tab=", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <WorkflowSummaryHeader
            activePage="overview"
            activeTab="schedules"
            description="d"
            enabled
            errorCount={0}
            extraTabs={[{ key: "hooks", label: "Hooks" }, { key: "smart-setup", label: "Smart setup" }, { key: "schedules", label: "Schedules" }]}
            isValid
            portability={{ clone_allowed: true } as WorkflowExportPolicy}
            projectId="p-1"
            stepCount={1}
            title="WF"
            warningCount={0}
            workflowId="wf-1"
          >
            <p>panel</p>
          </WorkflowSummaryHeader>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const links = within(screen.getByRole("navigation", { name: "Workflow views" })).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Overview", "Projects", "Hooks", "Smart setup", "Schedules"]);
    expect(links[3]).toHaveAttribute("href", "/registry/workflows/wf-1?project_id=p-1&tab=smart-setup");
    expect(links[4]).toHaveAttribute("aria-current", "page");
    expect(links[0]).not.toHaveAttribute("aria-current");
  });
});
