import { describe, expect, it } from "vitest";

import { type fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { normalizeAvailableWorkflowVariableSuggestions } from "@/pages/workflows/detail/detail-core";

const messages: Record<string, string> = {
  "react.workflows.variable_description.task_summary": "Užduoties pavadinimas / aprašymas",
  "react.workflows.variable_description.project_conventions": "Projekto programavimo susitarimai",
  "react.workflows.variable_description.state_output": "Būsenos išvestis, kurią įrašė {source}",
};

const t = (key: string, defaultText = "", params?: Parameters<typeof fallbackContext.t>[2]) =>
  interpolate(messages[key] ?? defaultText, params);

describe("workflow variable description localization", () => {
  it("translates platform metadata in global variables, dotted paths, and prior outputs", () => {
    const groups = normalizeAvailableWorkflowVariableSuggestions(
      {
        state: [{
          name: "task_summary",
          type: "text",
          description: "Task title / description",
          description_key: "react.workflows.variable_description.task_summary",
        }],
        dotted_path: [{
          name: "project_config.conventions",
          description: "Project coding conventions",
          description_key: "react.workflows.variable_description.project_conventions",
        }],
        prior_outputs: [{
          name: "result",
          step_id: "prepare",
          description: "State output written by set_variable",
          description_key: "react.workflows.variable_description.state_output",
          description_params: { source: "set_variable" },
        }],
      },
      t,
    );

    expect(groups.flatMap((group) => group.options).map(({ value, description }) => ({ value, description }))).toEqual([
      { value: "state.task_summary", description: "Užduoties pavadinimas / aprašymas" },
      { value: "project_config.conventions", description: "Projekto programavimo susitarimai" },
      { value: "state.project_config.conventions", description: "Projekto programavimo susitarimai" },
      { value: "state.result", description: "Būsenos išvestis, kurią įrašė set_variable" },
      { value: "message.step.prepare.artifacts.result", description: "Būsenos išvestis, kurią įrašė set_variable" },
    ]);
  });

  it("preserves descriptions without explicit provenance even when names and text resemble platform descriptions", () => {
    const groups = normalizeAvailableWorkflowVariableSuggestions({
      state: [{ name: "task_summary", description: "Task title / description" }],
      prior_outputs: [{ name: "custom_result", source: "set_variable", description: "State output written by set_variable" }],
    }, t);

    expect(groups.flatMap((group) => group.options).map(({ value, description }) => ({ value, description }))).toEqual([
      { value: "state.task_summary", description: "Task title / description" },
      { value: "state.custom_result", description: "State output written by set_variable" },
    ]);
  });

  it("retains the English fallback when a platform description translation is absent", () => {
    const groups = normalizeAvailableWorkflowVariableSuggestions({
      state: [{
        name: "task_id",
        description: "Unique task identifier",
        description_key: "react.workflows.variable_description.task_id",
      }],
    }, t);

    expect(groups[0].options[0].description).toBe("Unique task identifier");
  });
});
