/**
 * A cold load of a protected route, such as /app/jobs/<id>, while the session
 * read hiccups. One failed /api/app/bootstrap (a 5xx while the runtime
 * restarts, a dropped connection) used to read as "signed out": the guard sent
 * a signed-in user to the login screen and the page they asked for was lost.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { focusManager, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { AuthProvider } from "@/lib/auth/AuthProvider";
import { RequireAuth } from "@/lib/auth/guards";
import { sessionErrorIsTransient } from "@/lib/auth/session-state";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { createQueryClient } from "@/lib/query/client";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetch = vi.mocked(fetchJson);
const JOB_PATH = "/jobs/job-1";

const SIGNED_IN = {
  status: "ok",
  auth: {
    authenticated: true,
    local_auth_enabled: true,
    bootstrap_required: false,
    external_auth_enabled: false,
    auth_source: "local",
    user: { id: "u-1", username: "acme@example.test", roles: ["admin"] },
  },
};

function unavailable() {
  return new HttpError({ status: 502, url: "/api/app/bootstrap", payload: { error: "upstream unavailable" } });
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function renderColdLoad() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <AuthProvider>
        <MemoryRouter initialEntries={[JOB_PATH]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<RequireAuth><p>Job details</p></RequireAuth>} />
            <Route path="/login" element={<p>Sign in</p>} />
            <Route path="/dashboard" element={<p>Dashboard</p>} />
          </Routes>
          <Where />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedFetch.mockReset();
});

test("a session read that fails once is retried, and the job page opens where it was asked for", async () => {
  mockedFetch.mockRejectedValueOnce(unavailable()).mockResolvedValue(SIGNED_IN);

  renderColdLoad();

  expect(await screen.findByText("Job details", {}, { timeout: 3000 })).toBeInTheDocument();
  expect(screen.getByTestId("where")).toHaveTextContent(JOB_PATH);
  expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
  expect(mockedFetch).toHaveBeenCalledTimes(2);
});

test("a retry paused in a background tab still waits instead of signing the user out", async () => {
  // React Query pauses retries while the window is not focused; a paused query
  // is not "loading", so the guard used to redirect a signed-in user to login.
  focusManager.setFocused(false);
  try {
    mockedFetch.mockRejectedValueOnce(unavailable()).mockResolvedValue(SIGNED_IN);

    renderColdLoad();

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 1000));
    expect(screen.queryByText("Sign in")).not.toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent(JOB_PATH);

    act(() => focusManager.setFocused(true));
    expect(await screen.findByText("Job details", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent(JOB_PATH);
  } finally {
    focusManager.setFocused(undefined);
  }
});

test("a session that cannot be read at all is not treated as signed out", async () => {
  mockedFetch.mockRejectedValue(unavailable());

  renderColdLoad();

  expect(await screen.findByRole("alert", {}, { timeout: 4000 })).toHaveTextContent("Your session could not be checked");
  expect(screen.getByTestId("where")).toHaveTextContent(JOB_PATH);
  expect(screen.queryByText("Sign in")).not.toBeInTheDocument();

  mockedFetch.mockResolvedValue(SIGNED_IN);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Job details")).toBeInTheDocument();
  expect(screen.getByTestId("where")).toHaveTextContent(JOB_PATH);
});

test("a signed-out answer still goes to the login screen with the page to return to", async () => {
  mockedFetch.mockRejectedValue(new HttpError({ status: 401, url: "/api/app/bootstrap", payload: {} }));

  renderColdLoad();

  await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(`/login?next=${encodeURIComponent(JOB_PATH)}`));
  expect(mockedFetch).toHaveBeenCalledTimes(1);
});

test("only failures that say nothing about the session are retried", () => {
  expect(sessionErrorIsTransient(unavailable())).toBe(true);
  expect(sessionErrorIsTransient(new TypeError("Failed to fetch"))).toBe(true);
  expect(sessionErrorIsTransient(new HttpError({ status: 403, url: "/api/app/bootstrap", payload: {} }))).toBe(false);
});

test("a deep link waits on the session behind the page loader, not an empty screen", async () => {
  let answer: (value: unknown) => void = () => undefined;
  mockedFetch.mockReturnValue(new Promise((resolve) => { answer = resolve; }));

  renderColdLoad();

  // The same named page loader as the home route; it owns the wait, so the
  // global request loader stays unpainted behind it.
  expect(await screen.findByText("Restoring your session", {}, { timeout: 1000 })).toBeInTheDocument();
  await act(async () => answer(SIGNED_IN));
  expect(await screen.findByText("Job details")).toBeInTheDocument();
  expect(screen.queryByText("Restoring your session")).not.toBeInTheDocument();
});
