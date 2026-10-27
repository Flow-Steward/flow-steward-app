import { fireEvent, screen, within } from "@testing-library/react";

import { fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { jobPayload, mockedFetchJobApproval, primeApi, renderPage } from "./index.test-helpers";

const messages: Record<string, string> = {
  "react.jobs.completed_successfully": "Sėkmingai užbaigta",
  "react.jobs.status_group_completed": "Užbaigta",
  "react.jobs.started_by": "Pradėjo: {trigger}",
  "project_detail.scheduled.fields.schedule": "Tvarkaraštis",
  "react.jobs.completed_requested_work": "Flow Steward atliko prašytą darbą.",
  "react.jobs.current_step": "Dabartinis žingsnis",
  "project_detail.jobs.grid.started": "Pradėta",
  "jobs.columns.duration": "Trukmė",
  "react.jobs.last_update": "Paskutinis atnaujinimas",
  "react.jobs.no_action_required": "Jokių veiksmų nereikia",
  "react.jobs.completed_restart_help": "Šis vykdymas sėkmingai baigtas. Pradėkite darbą iš naujo tik jei reikia dar kartą atlikti tą pačią užduotį.",
  "jobs.status.completed": "Užbaigta",
  "react.jobs.field_rules": "Taisyklės",
  "react.jobs.field_value": "Reikšmė",
  "react.jobs.field_source": "Šaltinis",
  "react.jobs.field_result_target": "Rezultato tikslas",
  "react.jobs.field_step_id": "Žingsnio ID",
  "react.jobs.field_step_kind": "Žingsnio tipas",
  "react.jobs.review": "Peržiūra",
  "react.jobs.value1_review": "{value1} peržiūra",
  "react.jobs.active_human_decision": "Šiam vykdymui laukiamas žmogaus sprendimas.",
};

function renderLithuanianJob() {
  return renderPage({}, { ...fallbackContext, locale: "lt", messages, t: (key, defaultText = "", params) => interpolate(messages[key] || defaultText, params) });
}

describe("Saved Switch runtime reason locale", () => {
  const switchMessages: Record<string, string> = {
    "react.jobs.step_switch_no_value_default": "„switch“ parinkiklis neturi reikšmės: numatytasis atvejis",
    "react.jobs.step_switch_no_value_case": "„switch“ parinkiklis neturi reikšmės: atvejis „{case_id}“",
    "react.jobs.step_switch_value_default": "„switch“ parinkiklio reikšmė: {value}; numatytasis atvejis",
    "react.jobs.step_switch_value_case": "„switch“ parinkiklio reikšmė: {value}; atvejis „{case_id}“",
  };
  const reasons = [
    { name: "missing", value: null, key: "react.jobs.step_switch_no_value_default", params: {}, english: "switch selector has no value: the default case", lithuanian: "„switch“ parinkiklis neturi reikšmės: numatytasis atvejis", outcome: { name: "default" } },
    { name: "null matched", value: null, key: "react.jobs.step_switch_no_value_case", params: { case_id: "owner" }, english: "switch selector has no value: case 'owner'", lithuanian: "„switch“ parinkiklis neturi reikšmės: atvejis „owner“", outcome: { name: "case", case_id: "owner" } },
    { name: "false", value: false, key: "react.jobs.step_switch_value_default", params: { value: "false" }, english: "switch selector resolved to false: the default case", lithuanian: "„switch“ parinkiklio reikšmė: false; numatytasis atvejis", outcome: { name: "default" } },
    { name: "zero", value: 0, key: "react.jobs.step_switch_value_default", params: { value: "0" }, english: "switch selector resolved to 0: the default case", lithuanian: "„switch“ parinkiklio reikšmė: 0; numatytasis atvejis", outcome: { name: "default" } },
    { name: "empty", value: "", key: "react.jobs.step_switch_value_case", params: { value: '""', case_id: "owner" }, english: 'switch selector resolved to "": case \'owner\'', lithuanian: '„switch“ parinkiklio reikšmė: ""; atvejis „owner“', outcome: { name: "case", case_id: "owner" } },
    { name: "matched", value: "email", key: "react.jobs.step_switch_value_case", params: { value: '"email"', case_id: "owner" }, english: 'switch selector resolved to "email": case \'owner\'', lithuanian: '„switch“ parinkiklio reikšmė: "email"; atvejis „owner“', outcome: { name: "case", case_id: "owner" } },
    { name: "unmatched", value: "unknown", key: "react.jobs.step_switch_value_default", params: { value: '"unknown"' }, english: 'switch selector resolved to "unknown": the default case', lithuanian: '„switch“ parinkiklio reikšmė: "unknown"; numatytasis atvejis', outcome: { name: "default" } },
  ];

  describe.each(["en", "lt"] as const)("%s", (locale) => {
    it.each(reasons)("renders $name in the row and reopened inspector from the saved projection", async (reason) => {
      const savedStep = {
        id: "switch-message", step_id: "route", title: "Owner route", status: "completed",
        summary: reason.english, summary_key: reason.key, summary_params: reason.params,
        inputs: { selector: "state.inputs.channel", value: reason.value }, outputs: reason.outcome,
        raw: { artifacts: { selector: reason.value, _outcome: reason.outcome } },
      };
      primeApi(jobPayload({ status: "completed", execution_steps: [savedStep] }));
      const expected = locale === "lt" ? reason.lithuanian : reason.english;
      const context = {
        ...fallbackContext, locale, messages: locale === "lt" ? switchMessages : {},
        t: (key: string, defaultText = "", params?: Parameters<typeof interpolate>[1]) => interpolate(locale === "lt" ? switchMessages[key] || defaultText : defaultText, params),
      };
      const view = renderPage({}, context);
      const row = await screen.findByRole("button", { name: /Owner route/ });
      expect(within(row).getByText(expected)).toBeInTheDocument();
      fireEvent.click(row);
      expect(within(await screen.findByTestId("job-step-inspector-drawer")).getAllByText(expected)).toHaveLength(2);
      view.unmount();

      renderPage({}, context);
      fireEvent.click(await screen.findByRole("button", { name: /Owner route/ }));
      expect(within(await screen.findByTestId("job-step-inspector-drawer")).getAllByText(expected)).toHaveLength(2);
      expect(savedStep.raw.artifacts.selector).toBe(reason.value);
      expect(savedStep.raw.artifacts._outcome).toEqual(reason.outcome);
      expect(savedStep.inputs.value).toBe(reason.value);
    });
  });
});

describe("Lithuanian completed Schedule job", () => {
  it("localizes explicitly keyed built-in artifact evidence without changing provider evidence labels", async () => {
    primeApi(jobPayload({ status: "completed", execution_steps: [{
      id: "owner-step", title: "Owner transform", status: "completed",
      evidence: [
        { title: "Step ID", title_key: "react.jobs.field_step_id", summary: "transform_parameters" },
        { title: "Step Kind", title_key: "react.jobs.field_step_kind", summary: "transform" },
        { title: "Provider field name", summary: "Provider original diagnostic." },
      ],
    }] }));

    renderLithuanianJob();
    fireEvent.click(await screen.findByRole("button", { name: /Owner transform/ }));

    const inspector = await screen.findByTestId("job-step-inspector-drawer");
    expect(within(inspector).getByText("Žingsnio ID")).toBeInTheDocument();
    expect(within(inspector).getByText("Žingsnio tipas")).toBeInTheDocument();
    expect(within(inspector).getByText("transform_parameters")).toBeInTheDocument();
    expect(within(inspector).getByText("Provider field name")).toBeInTheDocument();
    expect(within(inspector).getByText("Provider original diagnostic.")).toBeInTheDocument();
  });

  it("localizes the generated review-stage fallback without translating owner review content", async () => {
    primeApi(jobPayload({ status: "awaiting_approval", review_workspace: undefined }));
    mockedFetchJobApproval.mockResolvedValue({ job_id: 42, payload: {
      approval_id: "owner-gate", thread_id: "owner-thread", content: "Owner review document.",
    } } as never);

    renderLithuanianJob();

    const workspace = await screen.findByTestId("job-review-workspace");
    expect(await within(workspace).findByRole("heading", { name: "Peržiūra" })).toBeInTheDocument();
    expect(within(workspace).getByText("Šiam vykdymui laukiamas žmogaus sprendimas.")).toBeInTheDocument();
    expect(within(workspace).getByText("Owner review document.")).toBeInTheDocument();
  });

  it("localizes the human-decision intro only when the workspace supplies no message", async () => {
    primeApi(jobPayload({ status: "awaiting_approval", review_workspace: {
      kind: "document_review", title: "Owner review title", stage_label: "Owner stage",
      content: "Owner review document.", thread_id: "owner-thread", approval_id: "owner-gate",
    } }));
    mockedFetchJobApproval.mockResolvedValue({ job_id: 42, approval_id: "owner-gate", payload: {
      approval_id: "owner-gate", thread_id: "owner-thread", content: "Owner review document.",
      review_workspace: {
        kind: "document_review", title: "Owner review title", stage_label: "Owner stage",
        content: "Owner review document.", thread_id: "owner-thread", approval_id: "owner-gate",
      },
    } } as never);

    renderLithuanianJob();

    const workspace = await screen.findByTestId("job-review-workspace");
    expect(await within(workspace).findByText("Šiam vykdymui laukiamas žmogaus sprendimas.")).toBeInTheDocument();
    expect(within(workspace).getByText("Owner review title")).toBeInTheDocument();
    expect(within(workspace).getByText("Owner review document.")).toBeInTheDocument();
  });

  it("localizes canonical job copy and timeline status without translating step evidence", async () => {
    primeApi(jobPayload({
      status: "completed",
      finished_at: "2026-04-25T10:02:00Z",
      job: { trigger: { kind: "schedule", label: "Schedule", label_key: "project_detail.scheduled.fields.schedule", detail: "cron * * * * *" } },
      lifecycle: { status: { code: "completed", label: "Completed successfully", label_key: "react.jobs.completed_successfully", terminal: true } },
      projection: {
        hero_summary: {
          headline: "Flow Steward completed the requested work.",
          headline_key: "react.jobs.completed_requested_work",
          summary: "Flow Steward completed the requested work.",
          summary_key: "react.jobs.completed_requested_work",
          facts: [
            { id: "current_step", label: "Current step", label_key: "react.jobs.current_step", value: "Owner step name" },
            { id: "started", label: "Started", label_key: "project_detail.jobs.grid.started", value: "2026-04-25 10:00" },
            { id: "duration", label: "Duration", label_key: "jobs.columns.duration", value: "2m" },
            { id: "last_update", label: "Last update", label_key: "react.jobs.last_update", value: "2026-04-25 10:02" },
          ],
        },
        next_action: {
          kind: "complete",
          title: "No action is required",
          title_key: "react.jobs.no_action_required",
          body: "This run finished successfully. Restart the job only if you need Flow Steward to run the same task again.",
          body_key: "react.jobs.completed_restart_help",
        },
      },
      execution_steps: [{ id: "owner-step", title: "Owner step name", status: "completed", status_label: "Completed", summary: "Provider returned its original diagnostic message." }],
    }));

    renderLithuanianJob();

    const overview = await screen.findByTestId("job-run-overview");
    // A completed run reads as its status group, in Lithuanian.
    expect(await within(overview).findByText("Užbaigta")).toBeInTheDocument();
    expect(screen.getByTestId("job-trigger")).toHaveTextContent("Pradėjo: Tvarkaraštis (cron * * * * *)");
    expect(within(overview).getByText("Flow Steward atliko prašytą darbą.")).toBeInTheDocument();
    for (const label of ["Dabartinis žingsnis", "Pradėta", "Trukmė", "Paskutinis atnaujinimas"]) {
      expect(within(overview).getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("Jokių veiksmų nereikia")).toBeInTheDocument();
    expect(screen.getByText(messages["react.jobs.completed_restart_help"])).toBeInTheDocument();
    const step = screen.getByRole("button", { name: /Owner step name/ });
    expect(within(step).getByText("Užbaigta")).toBeInTheDocument();
    expect(within(step).getByText("Provider returned its original diagnostic message.")).toBeInTheDocument();
    expect(within(overview).getByText("Owner step name")).toBeInTheDocument();
    expect(screen.queryByText("Completed successfully")).not.toBeInTheDocument();
    expect(screen.queryByText("No action is required")).not.toBeInTheDocument();
  });

  it("leaves unkeyed dynamic projection messages intact", async () => {
    primeApi(jobPayload({
      status: "completed",
      projection: {
        hero_summary: { headline: "Owner-authored completion summary.", facts: [] },
        next_action: { kind: "inspect_step", title: "Owner next action", body: "Inspect the provider-specific evidence." },
      },
      execution_steps: [],
    }));

    renderLithuanianJob();

    expect(await screen.findByText("Owner-authored completion summary.")).toBeInTheDocument();
    expect(screen.getByText("Owner next action")).toBeInTheDocument();
    expect(screen.getByText("Inspect the provider-specific evidence.")).toBeInTheDocument();
  });

  it("localizes built-in step schema labels while retaining owner data keys and labels", async () => {
    primeApi(jobPayload({
      status: "completed",
      execution_steps: [{
        id: "owner-step",
        title: "Owner transform",
        status: "completed",
        inputs: { rules: "Owner rule", source: "state.inputs.rows", step_id: "owner-step", step_kind: "transform", Batchlabel: "Owner batch", Parameters: { team: "Team A", region: "EU" } },
        outputs: { result_target: "state.output", value: { team: "Team A", region: "EU" } },
        input_field_label_keys: { rules: "react.jobs.field_rules", source: "react.jobs.field_source", step_id: "react.jobs.field_step_id", step_kind: "react.jobs.field_step_kind" },
        output_field_label_keys: { result_target: "react.jobs.field_result_target", value: "react.jobs.field_value" },
      }],
    }));

    renderLithuanianJob();
    fireEvent.click(await screen.findByRole("button", { name: /Owner transform/ }));

    const inspector = await screen.findByTestId("job-step-inspector-drawer");
    for (const label of ["Taisyklės", "Reikšmė", "Šaltinis", "Rezultato tikslas", "Žingsnio ID", "Žingsnio tipas"]) {
      expect(within(inspector).getByText(label, { selector: "dt" })).toBeInTheDocument();
    }
    expect(within(inspector).getByText("Batchlabel", { selector: "dt" })).toBeInTheDocument();
    expect(within(inspector).getByText("Parameters", { selector: "dt" })).toBeInTheDocument();
    expect(within(inspector).getAllByText("team:")).toHaveLength(2);
    expect(within(inspector).getAllByText("region:")).toHaveLength(2);
    expect(within(inspector).getByText("Owner rule")).toBeInTheDocument();
  });
});


describe("Workflow builder Test Run trigger locale", () => {
  it.each(["en", "lt"] as const)("renders the public trigger key in %s without changing owner details", async (locale) => {
    const rawLabel = "Test run from the workflow builder";
    const detail = "Owner Ąžuolas Ω";
    const labelKey = "react.jobs.workflow_builder_test_run";
    primeApi(jobPayload({
      status: "completed",
      job: { trigger: { kind: "test_run", label: rawLabel, label_key: labelKey, detail } },
      execution_steps: [],
    }));
    const translated = locale === "lt" ? "Bandomasis paleidimas iš darbo eigos kūrimo priemonės" : rawLabel;
    const localeMessages: Record<string, string> = {
      "react.jobs.started_by": locale === "lt" ? "Paleidimo šaltinis: {trigger}" : "Started by: {trigger}",
      [labelKey]: translated,
    };
    renderPage({}, { ...fallbackContext, locale, messages: localeMessages, t: (key, text = "", params) => interpolate(localeMessages[key] || text, params) });
    const trigger = await screen.findByTestId("job-trigger");
    expect(trigger).toHaveTextContent(`${locale === "lt" ? "Paleidimo šaltinis" : "Started by"}: ${translated} (${detail})`);
    if (locale === "lt") {
      expect(trigger).not.toHaveTextContent(rawLabel);
    }
  });
});
