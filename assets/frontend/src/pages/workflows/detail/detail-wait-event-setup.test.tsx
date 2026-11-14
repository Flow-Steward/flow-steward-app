import { fireEvent, render, screen } from "@testing-library/react";

import { stepNumericFieldErrors } from "./detail-step-numeric-fields";
import { WaitEventSetup } from "./detail-wait-event-setup";

const t = (key: string, defaultText?: string) => defaultText ?? key;

const CONFIG = {
  event: { source: "acme.crawler", event_types: ["run_completed"], correlation_key: "state.run.run_id" },
  timeout_ms: 86_400_000,
};

describe("WaitEventSetup", () => {
  it("edits the source, types, key, timeout and payload target of an event Wait", () => {
    const onChange = vi.fn();
    render(<WaitEventSetup config={CONFIG} errorId="e" errors={{}} inputClass="" onChange={onChange} />);

    expect(screen.getByLabelText("Extension that sends the event")).toHaveValue("acme.crawler");
    expect(screen.getByLabelText("Correlation key")).toHaveValue("state.run.run_id");

    fireEvent.change(screen.getByLabelText("Event types (comma separated)"), {
      target: { value: "run_completed, run_failed," },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      event: { ...CONFIG.event, event_types: ["run_completed", "run_failed"] },
    });

    fireEvent.change(screen.getByLabelText("Timeout, milliseconds"), { target: { value: "3600000" } });
    expect(onChange).toHaveBeenLastCalledWith({ timeout_ms: 3_600_000 });

    fireEvent.change(screen.getByLabelText("Save the event payload to"), { target: { value: "state.crawl" } });
    expect(onChange).toHaveBeenLastCalledWith({ output_mapping: { payload: "state.crawl" } });
  });

  it("asks for a timeout from 1 ms to 365 days", () => {
    expect(stepNumericFieldErrors("wait", CONFIG, t)).toEqual({});
    expect(stepNumericFieldErrors("wait", { ...CONFIG, timeout_ms: 0 }, t).timeout_ms).toBe(
      "Enter a whole number from 1 to 31536000000.",
    );
    // An event Wait has no duration: the duration rule does not apply to it.
    expect(stepNumericFieldErrors("wait", { ...CONFIG }, t).duration_ms).toBeUndefined();
  });
});
