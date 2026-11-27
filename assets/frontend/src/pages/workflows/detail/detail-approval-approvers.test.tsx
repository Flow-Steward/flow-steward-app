import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";

import { ApprovalStepApprovers } from "./detail-approval-approvers";

const BASE = "/api/modules/acme.approvals/approval-steps";
const people = [
  { user_id: "u-ann", username: "ann", display_name: "Ann Example" },
  { user_id: "u-bob", username: "bob", display_name: "Bob Example" },
  { user_id: "u-cy", username: "cy@example.test", display_name: "Cy Example" },
];

// The candidates are asked for this workflow: a person limited to projects the
// workflow does not run in is not offered.
const CANDIDATES = `${BASE}/candidates?workflow_id=acme.invoices`;

type Calls = Array<{ url: string; method: string; body: unknown }>;

function stubServer(initial: typeof people = []): Calls {
  const calls: Calls = [];
  let approvers = initial;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as unknown : undefined;
    calls.push({ url, method, body });
    if (url === CANDIDATES) return Response.json({ users: people });
    if (method === "PUT") {
      const ids = (body as { user_ids: string[] }).user_ids;
      approvers = people.filter((person) => ids.includes(person.user_id));
    }
    return Response.json({ approvers, restricted: approvers.length > 0 });
  }));
  return calls;
}

function view({ flags, endpoints, stepOnServer = true }: {
  flags: Record<string, boolean>;
  endpoints?: Record<string, string>;
  stepOnServer?: boolean;
}) {
  const session = { feature_flags: flags, feature_endpoints: endpoints };
  return render(
    <AuthContext.Provider value={{ session } as AuthContextValue}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ApprovalStepApprovers stepId="review" stepOnServer={stepOnServer} workflowId="acme.invoices" />
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

afterEach(() => { vi.unstubAllGlobals(); });

it("renders nothing and asks nothing without the feature flag", () => {
  const calls = stubServer();
  const { container } = view({ flags: {}, endpoints: { approval_assignees: BASE } });
  expect(container).toBeEmptyDOMElement();
  expect(screen.queryByText("Approvers")).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});

it("renders nothing when the flag is on but no endpoint is named", () => {
  const calls = stubServer();
  const { container } = view({ flags: { approval_assignees: true } });
  expect(container).toBeEmptyDOMElement();
  expect(calls).toEqual([]);
});

it("asks to save the step first while the server does not have it", () => {
  const calls = stubServer();
  view({ flags: { approval_assignees: true }, endpoints: { approval_assignees: BASE }, stepOnServer: false });
  expect(screen.getByText("Save the workflow with this step to choose who may decide it.")).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Approvers" })).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});

it("loads the current approvers and saves a new choice with PUT", async () => {
  const user = userEvent.setup();
  const calls = stubServer([people[0]]);
  view({ flags: { approval_assignees: true }, endpoints: { approval_assignees: BASE } });

  await waitFor(() => expect(screen.getByText("Ann Example")).toBeInTheDocument());
  expect(screen.getByText("Only the people chosen here can decide this step.")).toBeInTheDocument();
  expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual(expect.arrayContaining([
    `GET ${CANDIDATES}`,
    `GET ${BASE}/acme.invoices/review/approvers`,
  ]));
  const save = screen.getByRole("button", { name: "Save approvers" });
  expect(save).toBeDisabled();

  await user.click(screen.getByRole("combobox", { name: "Approvers" }));
  await user.click(await screen.findByRole("option", { name: /Bob Example/ }));
  expect(save).toBeEnabled();
  await user.click(save);

  await waitFor(() => expect(calls.find((call) => call.method === "PUT")).toEqual({
    url: `${BASE}/acme.invoices/review/approvers`,
    method: "PUT",
    body: { user_ids: ["u-ann", "u-bob"] },
  }));
  await waitFor(() => expect(screen.getByText("Approvers saved.")).toBeInTheDocument());
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approvers" })).toBeDisabled());
});

it("words an empty choice as open to anyone who may decide approvals", async () => {
  const user = userEvent.setup();
  const calls = stubServer([people[0]]);
  view({ flags: { approval_assignees: true }, endpoints: { approval_assignees: BASE } });

  await waitFor(() => expect(screen.getByText("Ann Example")).toBeInTheDocument());
  await user.click(screen.getByRole("button", { name: "Remove Ann Example" }));
  expect(screen.getByText("No one chosen: anyone who may decide approvals can decide this step.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save approvers" }));
  await waitFor(() => expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ user_ids: [] }));
});

it("lists chosen approvers under the search and keeps adding and removing possible", async () => {
  const user = userEvent.setup();
  const calls = stubServer([people[0], people[1]]);
  view({ flags: { approval_assignees: true }, endpoints: { approval_assignees: BASE } });

  const list = await screen.findByRole("list", { name: "Approvers" });
  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(2));
  const input = screen.getByRole("combobox", { name: "Approvers" });
  expect(input).toHaveValue("");
  expect(input).toHaveAttribute("placeholder", "Search people");

  // With two chosen, the search still finds and adds a third.
  await user.type(input, "cy@");
  await user.click(await screen.findByRole("option", { name: /Cy Example/ }));
  expect(within(list).getAllByRole("listitem")).toHaveLength(3);
  expect(within(list).getByText("cy@example.test")).toBeInTheDocument();

  // Removing one keeps the others and returns focus to the search.
  await user.click(within(list).getByRole("button", { name: "Remove Bob Example" }));
  expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  expect(input).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Save approvers" }));
  await waitFor(() => expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ user_ids: ["u-ann", "u-cy"] }));
});
