import {
  insertTemplateVariable,
  openTemplateQuery,
  rankVariables,
  type RankedVariable,
} from "@/pages/workflows/detail/detail-variable-filter";

const entries: RankedVariable[] = [
  "state.resume_stage",
  "state.rework_instructions",
  "state.risk_level",
  "state.project_config",
  "project_config.stack.language",
  "state.project_config.stack.language",
  "message.step.analysis.artifacts.project_brief",
].map((value) => ({ group: "Global variables", option: { value } }) as RankedVariable);

const values = (ranked: RankedVariable[]) => ranked.map((entry) => entry.option.value);

describe("rankVariables (item 11)", () => {
  it("keeps only the paths containing the typed text, case-insensitively", () => {
    expect(values(rankVariables(entries, "RISK"))).toEqual(["state.risk_level"]);
  });

  it("puts the paths that start with it -- with or without state. -- first", () => {
    expect(values(rankVariables(entries, "project_"))).toEqual([
      "state.project_config",
      "project_config.stack.language",
      "state.project_config.stack.language",
      "message.step.analysis.artifacts.project_brief", // contains it, does not start with it
    ]);
  });

  it("shows everything for an empty field and nothing for no match", () => {
    expect(values(rankVariables(entries, "  "))).toEqual(values(entries));
    expect(rankVariables(entries, "zzz")).toEqual([]);
  });

  it("lists a path once even when two groups offer it", () => {
    const twice = [
      ...entries,
      { group: "analysis", option: { value: "state.risk_level" } } as RankedVariable,
    ];
    expect(values(rankVariables(twice, "risk"))).toEqual(["state.risk_level"]);
  });
});


it("matches business labels even when the serialized path and description do not contain the query", () => {
  const labeled: RankedVariable[] = [
    { group: "Inputs", option: { value: "state.inputs.customer.name", label: "Customer → Client name", description: "", meta: "string", stepId: "", variant: "workflow" } },
    { group: "Inputs", option: { value: "state.inputs.customer.nickname", label: "Customer → Nickname", description: "", meta: "string", stepId: "", variant: "workflow" } },
    { group: "Previous steps", option: { value: "state.normalized_customer.name", label: "Normalize customer → Client name", description: "", meta: "string", stepId: "normalize_customer", variant: "state_alias" } },
  ];
  expect(values(rankVariables(labeled, "Client"))).toEqual([
    "state.inputs.customer.name",
    "state.normalized_customer.name",
  ]);
});

describe("template fields (item 11)", () => {
  it("filter by what follows an open {{ and nothing else", () => {
    expect(openTemplateQuery("https://x/{{ proj")).toBe("proj");
    expect(openTemplateQuery("https://x/{{ state.a }}/items")).toBeNull();
    expect(openTemplateQuery("plain text")).toBeNull();
  });

  it("put the chosen path in place of the open {{ and keep the text before it", () => {
    expect(insertTemplateVariable("https://x/{{ proj", "state.project_config")).toBe(
      "https://x/{{ state.project_config }}",
    );
  });
});

describe("rankVariables matches anywhere in the path or the description (B24)", () => {
  const described: RankedVariable[] = [
    { group: "Global variables", option: { value: "state.current_group_id", description: "The Group the run is in" } },
    { group: "Global variables", option: { value: "state.current_step_id", description: "The step that runs now" } },
    { group: "Global variables", option: { value: "state.risk_level", description: "" } },
    { group: "fetch", option: { value: "state.fetch.response", description: "Step output: current response" } },
  ] as RankedVariable[];

  it("keeps every path containing the text, and a description that contains it", () => {
    expect(values(rankVariables(described, "current"))).toEqual([
      "state.current_group_id",
      "state.current_step_id",
      "state.fetch.response", // its description says "current"
    ]);
  });

  it("matches a mid-word fragment, also after the State path field's state. prefix", () => {
    expect(values(rankVariables(described, "rrent_st"))).toEqual(["state.current_step_id"]);
    expect(values(rankVariables(described, "state.rrent_st"))).toEqual(["state.current_step_id"]);
    expect(values(rankVariables(described, "state.RUNS NOW"))).toEqual(["state.current_step_id"]);
  });
});
