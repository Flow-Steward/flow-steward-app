import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AutomationRuleEditor } from "@/pages/workflows/detail/detail-automation-editor";
import type { AutomationScopeKind } from "@/pages/workflows/detail/detail-automation-core";
import { defaultReactionDraft, draftFromReactionRule, reactionPayloadFromDraft } from "@/pages/workflows/detail/detail-automation-core";

afterEach(() => vi.restoreAllMocks());

it("keeps a null template null and does not invent one for a new rule", () => {
  const existing = draftFromReactionRule({ idempotency_key_template: null, event_type: "job.completed" }, "workflow", []);
  expect(reactionPayloadFromDraft(existing, 0).idempotency_key_template).toBeNull();
  expect(reactionPayloadFromDraft(defaultReactionDraft("workflow", []), 0).idempotency_key_template).toBeNull();
});

it.each<AutomationScopeKind>(["workflow", "group", "step"])(
  "preserves the hidden idempotency template through %s save and fresh readback",
  async (scopeKind) => {
    const template = "  ${job.id}:${event.type}:release  ";
    let stored: Record<string, unknown> = {
      reaction_key: "release_notice", display_name: "Release notice", enabled: true,
      event_type: "job.completed", conditions: { custom: { keep: true } },
      action: { type: "notify", target: "project_routes", message: "Before", severity: "info" },
      debounce_seconds: 7, idempotency_key_template: template,
    };
    const writes: Array<Record<string, unknown>> = [];
    const requests: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
      requests.push(`${init?.method || "GET"} ${url.pathname}${url.search}`);
      if (url.pathname !== "/api/workflows/workflow-internal/reactions") {
        throw new Error(`Unexpected request: ${url.pathname}`);
      }
      if (scopeKind !== "workflow") {
        expect(url.searchParams.get("scope_kind")).toBe(scopeKind);
        expect(url.searchParams.get("scope_id")).toBe("scope-internal");
      }
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { reactions: Array<Record<string, unknown>> };
        expect(body.reactions).toHaveLength(1);
        writes.push(body.reactions[0]);
        // The server's scoped replacement treats omission as null, not retain.
        stored = { ...body.reactions[0], idempotency_key_template: body.reactions[0].idempotency_key_template ?? null };
      }
      return new Response(JSON.stringify({ workflow_id: "workflow-internal", reactions: [stored], sources: [] }), {
        headers: { "content-type": "application/json" },
      });
    });
    const mount = () => render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
        <AutomationRuleEditor workflowId="workflow-internal" scopeKind={scopeKind} scopeId="scope-internal" />
      </QueryClientProvider>,
    );
    const { unmount } = mount();
    const user = userEvent.setup();
    // A saved rule's switch writes at once: the saved list with only this switch changed.
    await user.click(await screen.findByRole("switch", { name: /^Enable rule:/ }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({ enabled: false, idempotency_key_template: template, conditions: { custom: { keep: true } } });
    expect(stored.idempotency_key_template).toBe(template);
    await screen.findByText("Rule turned off.");
    unmount();
    const view = mount();
    expect(await screen.findByRole("switch", { name: /^Enable rule:/ })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: /^When / }));
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "After fresh read");
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]).toMatchObject({ idempotency_key_template: template, action: { message: "After fresh read" } });
    expect(requests.filter((request) => request.startsWith("GET ")).length).toBeGreaterThanOrEqual(3);
    view.unmount();
  },
);
