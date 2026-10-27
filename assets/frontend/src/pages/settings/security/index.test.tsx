import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { SettingsSecurityPage } from "@/pages/settings/security";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(async () => ({ projects: [] })),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflows: vi.fn(async () => []),
}));

const mockedFetchJson = vi.mocked(fetchJson);

/** Policy selects are DsSelect: open the combobox, then pick the option. */
async function pick(name: string, option: string) {
  fireEvent.click(screen.getByRole("combobox", { name }));
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

const localeMessages: Record<string, string> = {
  "security_overview.level.hardened": "Sustiprinta",
  "security_overview.level.secure": "Saugu",
  "security_overview.level.needs_attention": "Reikia dėmesio",
  "security_overview.level.at_risk": "Rizikinga",
  "security_overview.level.unknown": "Nežinoma",
  "security_overview.scan_mode": "Tikrinimo režimas",
  "react.settings.security_enforcement_blocking": "Blokavimas vykdant politiką",
  "react.settings.security_mode_enforce_standard": "Standartinis vykdymas",
  "react.settings.security_mode_enforce_strict": "Griežtas vykdymas",
  "react.settings.security_mode_monitor": "Stebėjimas",
  "react.settings.security_mode_disabled": "Išjungta",
  "react.workflows.true": "Tiesa", "react.workflows.false": "Netiesa",
  "react.settings.security_option_scan": "Tikrinti",
  "react.settings.security_option_skip": "Praleisti",
  "react.settings.security_option_hard_redact": "Visiškai paslėpti",
  "react.settings.security_option_prompt": "Užklausa",
  "react.settings.security_option_display": "Rodymas",
  "react.settings.security_option_shell": "Komandų apvalkalas",
  "react.settings.security_option_person_name": "Asmens vardas",
  "react.settings.security_option_credit_card": "Kredito kortelė",
};

function renderPage(initialEntry = "/security/projects/42", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages: locale === "lt" ? localeMessages : {}, t: (key, text = "", params) => interpolate((locale === "lt" ? localeMessages[key] : "") || text, params) }}><QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/security/projects/:projectId" element={<SettingsSecurityPage />} />
          <Route path="/settings/security" element={<SettingsSecurityPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
}

describe("SettingsSecurityPage", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockImplementation(async (path, init) => {
      const url = stringFromUnknownValue(path);
      if (url === "/api/security/projects/42/policy?include_sources=1") {
        return {
          effective_policy: {
            max_failed_login_attempts: 5,
            blocked_roles: ["guest"],
          },
          global_policy: { max_failed_login_attempts: 10 },
          project_override: { max_failed_login_attempts: 5 },
          source_map: {
            max_failed_login_attempts: "project_override",
          },
        };
      }
      if (url === "/api/security/projects/42/policy" && init?.method === "PUT") {
        return { status: "updated" };
      }
      if (url === "/api/security/policy" && init?.method === "PUT") {
        return { status: "updated" };
      }
      if (url === "/api/security/policy") {
        return {
          enabled: true,
          level: "standard",
          scanners: {
            secrets: { enabled: true, severity_threshold: "low", allowlist: [] },
            injection: {
              enabled: true,
              severity_threshold: "medium",
              contexts: ["prompt", "display"],
            },
            pii: {
              enabled: true,
              severity_threshold: "medium",
              mode: "pseudonymize",
              detectors: {
                email: { enabled: true },
                ssn: { enabled: false },
              },
            },
          },
          surfaces: {
            inbound: { action: "scan" },
            outbound: { action: "scan" },
            tool: { action: "skip" },
          },
          custom_rules: [
            {
              rule_id: "custom.ssn",
              scanner: "pii",
              severity: "high",
              description: "Check customer SSNs.",
            },
          ],
        };
      }
      if (url === "/api/security/posture") {
        return { score: 100, level: "hardened", status: "no_data" };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
  });

  it.each([{ level: "hardened", label: "Sustiprinta" }, { level: "secure", label: "Saugu" }, { level: "needs_attention", label: "Reikia dėmesio" }, { level: "at_risk", label: "Rizikinga" }, { level: "unknown", label: "Nežinoma" }])("localizes the canonical posture level $level", async ({ level, label }) => {
    const original = mockedFetchJson.getMockImplementation();
    mockedFetchJson.mockImplementation(async (input, init) => input === "/api/security/posture" ? { level, custom_owner_value: "OwnerOpaque123" } : original?.(input, init));
    renderPage("/settings/security", "lt");
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByText("OwnerOpaque123")).toBeInTheDocument();
  });

  it.each([{ mode: "enforce_standard", blocking: true, label: "Standartinis vykdymas", value: "Tiesa" }, { mode: "monitor", blocking: false, label: "Stebėjimas", value: "Netiesa" }])("localizes canonical posture mode and blocking values for $mode", async ({ mode, blocking, label, value }) => {
    const original = mockedFetchJson.getMockImplementation();
    mockedFetchJson.mockImplementation(async (input, init) => input === "/api/security/posture" ? { scan_mode: mode, enforcement_blocking: blocking, custom_owner_value: "OwnerOpaque123" } : original?.(input, init));
    renderPage("/settings/security", "lt");
    expect(await screen.findByText("Tikrinimo režimas")).toBeInTheDocument();
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText("Blokavimas vykdant politiką")).toBeInTheDocument();
    expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.getByText("OwnerOpaque123")).toBeInTheDocument();
  });

  it("localizes first-party security choices while saving canonical enum values and owner rules", async () => {
    const original = mockedFetchJson.getMockImplementation();
    mockedFetchJson.mockImplementation(async (input, init) => input === "/api/security/policy" && init?.method !== "PUT" ? {
      enabled: true, level: "standard", scanners: {
        injection: { enabled: true, severity_threshold: "medium", contexts: ["prompt", "display", "shell"] },
        pii: { enabled: true, severity_threshold: "medium", mode: "hard_redact", detectors: { person_name: { enabled: true }, credit_card: { enabled: true } } },
      }, surfaces: { tool: { action: "scan" } }, custom_rules: [{ rule_id: "owner.rule", scanner: "pii", severity: "high", description: "Owner rule description" }],
    } : original?.(input, init));
    renderPage("/settings/security", "lt");
    await screen.findByText("Owner rule description");
    expect(screen.getByText("Užklausa")).toBeInTheDocument();
    expect(screen.getByText("Rodymas")).toBeInTheDocument();
    expect(screen.getByText("Komandų apvalkalas")).toBeInTheDocument();
    expect(screen.getByText("Asmens vardas")).toBeInTheDocument();
    expect(screen.getByText("Kredito kortelė")).toBeInTheDocument();
    await pick("PII mode", "Visiškai paslėpti");
    await pick("Tool activity action", "Praleisti");
    await pick("Tool activity action", "Tikrinti");
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => {
      const call = mockedFetchJson.mock.calls.find(([path, init]) => path === "/api/security/policy" && init?.method === "PUT");
      expect(call).toBeTruthy();
      const body = JSON.parse(String(call?.[1]?.body));
      expect(body.scanners.injection.contexts).toEqual(["prompt", "display", "shell"]);
      expect(body.scanners.pii.mode).toBe("hard_redact");
      expect(body.scanners.pii.detectors).toEqual({ person_name: { enabled: true }, credit_card: { enabled: true } });
      expect(body.surfaces.tool.action).toBe("scan");
      expect(body.custom_rules[0]).toMatchObject({ rule_id: "owner.rule", description: "Owner rule description" });
    });
  });

  it("says blocking is off when the runtime is only monitoring", async () => {
    // AC17-R74: a production install reported "Score 100%, Hardened" with every scanner "On"
    // while SECURITY_SCAN_MODE was monitor — scanning active, blocking disabled. The runtime
    // logs that at every start; this page never said it.
    mockedFetchJson.mockImplementation(async (input: unknown) => {
      const url = stringFromUnknownValue(input);
      if (url === "/api/security/posture") {
        return {
          score: 100,
          level: "hardened",
          status: "active",
          scan_mode: "monitor",
          enforcement_blocking: false,
        };
      }
      return { scanners: {} };
    });

    renderPage("/settings/security");

    const notice = await screen.findByTestId("security-enforcement-notice");
    expect(notice).toHaveTextContent("Scanning is active, but nothing is being blocked.");
    expect(notice).toHaveTextContent(/monitor/);
  });

  it("stays quiet when the runtime is actually enforcing", async () => {
    mockedFetchJson.mockImplementation(async (input: unknown) => {
      const url = stringFromUnknownValue(input);
      if (url === "/api/security/posture") {
        return {
          score: 100,
          level: "hardened",
          status: "active",
          scan_mode: "enforce_standard",
          enforcement_blocking: true,
        };
      }
      return { scanners: {} };
    });

    renderPage("/settings/security");

    await screen.findByText("Security posture");
    expect(screen.queryByTestId("security-enforcement-notice")).not.toBeInTheDocument();
  });

  it("stays quiet against a runtime that does not report the mode at all", async () => {
    // Older runtimes omit the field; absence must not be read as "not enforcing".
    renderPage("/settings/security");

    await screen.findByText("Security posture");
    expect(screen.queryByTestId("security-enforcement-notice")).not.toBeInTheDocument();
  });

  it("uses project-scoped policy endpoints for project security routes", async () => {
    renderPage();

    expect(await screen.findByText("Project security")).toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/api/security/projects/42/policy?include_sources=1",
    );
    expect(mockedFetchJson).not.toHaveBeenCalledWith("/api/security/policy");

    expect(await screen.findByLabelText("Max Failed Login Attempts")).toBeInTheDocument();
    expect(mockedFetchJson).not.toHaveBeenCalledWith("/api/auth/tokens?page=1&per_page=200");

    fireEvent.change(screen.getByLabelText("Max Failed Login Attempts"), {
      target: { value: "6" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith("/api/security/projects/42/policy", {
        method: "PUT",
        body: expect.stringContaining('"max_failed_login_attempts":6'),
      });
    });
    expect(mockedFetchJson).not.toHaveBeenCalledWith(
      "/api/security/policy",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("clears a project level override back to global inheritance", async () => {
    let projectOverride: Record<string, unknown> = { level: "strict" };
    mockedFetchJson.mockImplementation(async (path, init) => {
      const url = stringFromUnknownValue(path);
      if (url === "/api/security/projects/42/policy?include_sources=1") {
        return {
          effective_policy: { enabled: true, level: projectOverride.level || "standard" },
          global_policy: { enabled: true, level: "standard" },
          project_override: projectOverride,
          source_map: { enabled: "global", level: projectOverride.level ? "project_override" : "global" },
        };
      }
      if (url === "/api/security/projects/42/policy" && init?.method === "PUT") {
        projectOverride = JSON.parse(String(init.body)) as Record<string, unknown>;
        return { status: "updated" };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    renderPage();

    expect(await screen.findByRole("combobox", { name: "Level" })).toHaveTextContent("Strict");
    fireEvent.click(screen.getByRole("button", { name: "Inherit Level" }));
    expect(screen.getByRole("combobox", { name: "Level" })).toHaveTextContent("Standard");
    expect(screen.getByText("Pending override changes: level")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith("/api/security/projects/42/policy", {
        method: "PUT",
        body: "{}",
      });
    });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Level" })).toHaveTextContent("Standard"));
    expect(screen.queryByRole("button", { name: "Inherit Level" })).not.toBeInTheDocument();
  });

  it("explains global posture and policy internals without rendering raw structured fields", async () => {
    renderPage("/settings/security");

    expect(await screen.findByText("No posture snapshot yet.")).toBeInTheDocument();
    expect(screen.getByText(/This page does not start a separate scan/i)).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
    expect(screen.queryByText("Hardened")).not.toBeInTheDocument();

    expect(screen.getByText("Scanner coverage")).toBeInTheDocument();
    expect(screen.getByLabelText("Secrets enabled")).toBeInTheDocument();
    expect(screen.getByText("Prompt injection")).toBeInTheDocument();
    expect(screen.getByText("PII")).toBeInTheDocument();
    expect(screen.getByText("Scan surfaces")).toBeInTheDocument();
    expect(screen.getByText("Inbound input")).toBeInTheDocument();
    expect(screen.getByText("Tool activity")).toBeInTheDocument();
    expect(screen.getByText("custom.ssn")).toBeInTheDocument();
    expect(screen.queryByText("Structured fields")).not.toBeInTheDocument();
  });

  it("edits scanner settings, scan surfaces, and custom rules before saving", async () => {
    renderPage("/settings/security");

    expect(await screen.findByLabelText("Secrets enabled")).toBeChecked();
    const removeExistingRule = screen.getByRole("button", { name: "Remove" });
    fireEvent.click(screen.getByLabelText("Secrets enabled"));
    expect(screen.getByRole("button", { name: "Remove" })).toBe(removeExistingRule);
    await pick("Secrets threshold", "High");
    await pick("PII mode", "Hard Redact");
    await pick("Tool activity action", "Scan");
    fireEvent.change(screen.getByLabelText("Custom rule id"), {
      target: { value: "custom.prompt" },
    });
    await pick("Custom rule scanner", "Injection");
    await pick("Custom rule severity", "Critical");
    fireEvent.change(screen.getByLabelText("Custom rule pattern"), {
      target: { value: "ignore previous instructions" },
    });
    fireEvent.change(screen.getByLabelText("Custom rule description"), {
      target: { value: "Block prompt override wording." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add custom rule" }));

    expect(await screen.findByText("custom.prompt")).toBeInTheDocument();
    fireEvent.click(removeExistingRule);
    expect(screen.queryByText("custom.ssn")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => {
      const putCall = mockedFetchJson.mock.calls.find(([path, init]) => {
        const requestInit = init as RequestInit | undefined;
        return path === "/api/security/policy" && requestInit?.method === "PUT";
      });
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String((putCall?.[1] as RequestInit | undefined)?.body || "{}"));
      expect(body.scanners.secrets.enabled).toBe(false);
      expect(body.scanners.secrets.severity_threshold).toBe("high");
      expect(body.scanners.pii.mode).toBe("hard_redact");
      expect(body.surfaces.tool.action).toBe("scan");
      expect(body.custom_rules).toEqual([
        expect.objectContaining({
          pattern: "ignore previous instructions",
          rule_id: "custom.prompt",
          scanner: "injection",
          severity: "critical",
        }),
      ]);
    });
  });
});
